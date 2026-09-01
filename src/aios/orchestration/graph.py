"""LangGraph wiring: the state machine that executes coordinator decisions.

    START -> plan -> schedule -> (fan-out execute | reconcile | signoff
                                  | replan | finalize | halt)

Every node transition is checkpointed, so the run survives the pause at the
sign-off gate and resumes from the same state. Ready tasks are fanned out with
`Send`, one branch per task, and merged back through the task reducer - that is
where the parallelism in a plan actually comes from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from aios.a2a import Envelope, ReplyStatus
from aios.agents import Agent
from aios.config import Settings
from aios.llm import LlmClient
from aios.mcp_gateway import McpGateway, Principal
from aios.memory import SemanticMemory
from aios.observability import EventKind, TraceStore
from aios.orchestration.coordinator import Action, Coordinator
from aios.orchestration.planner import Planner
from aios.orchestration.state import (
    AgentType,
    Conflict,
    ConflictStatus,
    Dispatch,
    RunState,
    RunStatus,
    Signoff,
    Task,
    TaskStatus,
)

SCHEDULE = "schedule"
EXECUTE = "execute"


@dataclass
class Deps:
    """Everything the graph nodes need, wired once per run."""

    settings: Settings
    trace: TraceStore
    llm: LlmClient
    planner: Planner
    coordinator: Coordinator
    agents: dict[AgentType, Agent]
    gateway: McpGateway
    memory: SemanticMemory


def build_graph(deps: Deps) -> StateGraph:
    """Assemble the run graph. Compile it with a checkpointer to execute."""
    builder = StateGraph(RunState)
    builder.add_node("plan", _plan_node(deps))
    builder.add_node(SCHEDULE, _schedule_node(deps))
    builder.add_node(EXECUTE, _execute_node(deps))
    builder.add_node("reconcile", _reconcile_node(deps))
    builder.add_node("signoff", _signoff_node(deps))
    builder.add_node("finalize", _finalize_node(deps))
    builder.add_node("halt", _halt_node(deps))

    builder.add_edge(START, "plan")
    builder.add_edge("plan", SCHEDULE)
    builder.add_conditional_edges(
        SCHEDULE,
        _router(deps),
        [EXECUTE, "reconcile", "signoff", "plan", "finalize", "halt"],
    )
    builder.add_edge(EXECUTE, SCHEDULE)
    builder.add_edge("reconcile", SCHEDULE)
    builder.add_edge("signoff", SCHEDULE)
    builder.add_edge("finalize", END)
    builder.add_edge("halt", END)
    return builder


def _plan_node(deps: Deps):
    """Produce the initial plan, or revise it around a blocked critical task."""

    def plan(state: RunState) -> dict:
        revision = state["plan_revision"]
        if revision == 0:
            tasks = deps.planner.plan(state["goal"], state["precedents"])
            return {
                "tasks": tasks,
                "plan_revision": 1,
                "status": RunStatus.RUNNING,
            }
        reason = _blocked_reason(state)
        tasks = deps.planner.replan(
            state["goal"], state["tasks"], reason, revision + 1
        )
        return {
            "tasks": tasks,
            "plan_revision": revision + 1,
            "replan_reason": reason,
            "status": RunStatus.RUNNING,
        }

    return plan


def _schedule_node(deps: Deps):
    """Reconcile the claim space before deciding what to run next."""

    def schedule(state: RunState) -> dict:
        tasks = state["tasks"]
        known = state["conflicts"]
        changed = deps.coordinator.detect_conflicts(tasks, known)

        for conflict_id, conflict in changed.items():
            if conflict_id not in known:
                deps.trace.emit(
                    EventKind.CONFLICT_DETECTED,
                    f"{conflict.subject}.{conflict.metric} contested: {conflict.values}",
                    conflict_id=conflict_id,
                )
            elif conflict.status is ConflictStatus.RESOLVED:
                deps.trace.emit(
                    EventKind.CONFLICT_RESOLVED,
                    f"{conflict.subject}.{conflict.metric}: {conflict.resolution}",
                    conflict_id=conflict_id,
                )

        merged = {**known, **changed}
        for conflict_id, conflict in merged.items():
            if (
                conflict.status is ConflictStatus.OPEN
                and conflict.attempts >= deps.coordinator.max_conflict_attempts
            ):
                escalated = deps.coordinator.escalate(conflict)
                changed[conflict_id] = escalated
                deps.trace.emit(
                    EventKind.CONFLICT_ESCALATED,
                    f"{conflict.subject}.{conflict.metric}: {escalated.resolution}",
                    conflict_id=conflict_id,
                )
        return {"conflicts": changed} if changed else {}

    return schedule


def _router(deps: Deps):
    """Turn the coordinator's decision into the next graph step."""

    def route(state: RunState):
        decision = deps.coordinator.decide(state)
        detail = (
            ", ".join(decision.task_ids or decision.conflict_ids) or decision.reason
        )
        deps.trace.emit(
            EventKind.RUN_STATUS,
            f"{decision.action.value}: {detail}" if detail else decision.action.value,
        )
        if decision.action is Action.DISPATCH:
            return [
                Send(EXECUTE, _dispatch(state, state["tasks"][task_id]))
                for task_id in decision.task_ids
            ]
        if decision.action is Action.RECONCILE:
            return "reconcile"
        if decision.action is Action.SIGNOFF:
            return "signoff"
        if decision.action is Action.REPLAN:
            return "plan"
        if decision.action is Action.FINALIZE:
            return "finalize"
        return "halt"

    return route


def _execute_node(deps: Deps):
    """Run one dispatched task on its agent and apply the retry policy."""

    def execute(payload: Dispatch) -> dict:
        task = Task.model_validate(payload["task"])
        attempt = payload["attempt"]
        if attempt > 1:
            time.sleep(deps.coordinator.retry.delay_for(attempt - 1))

        envelope = Envelope(
            run_id=payload["run_id"],
            task_id=task.task_id,
            recipient=task.agent,
            goal=payload["goal"],
            instruction=task.description,
            success_criteria=task.success_criteria,
            attempt=attempt,
            context=payload["context"],
        )
        deps.trace.emit(
            EventKind.DISPATCH,
            f"{task.task_id} -> {task.agent.value} (attempt {attempt})",
            task_id=task.task_id,
            message_id=envelope.message_id,
        )

        reply = deps.agents[task.agent].handle(envelope)
        if reply.status is ReplyStatus.OK:
            status = (
                TaskStatus.AWAITING_SIGNOFF
                if task.requires_signoff
                else TaskStatus.DONE
            )
            updated = task.model_copy(
                update={
                    "status": status,
                    "attempts": attempt,
                    "result": reply.result,
                    "error": None,
                    "context": {},
                }
            )
            return {"tasks": {task.task_id: updated}}

        error = reply.error or "unknown agent failure"
        updated = deps.coordinator.on_failure(
            task.model_copy(update={"attempts": attempt}), error
        )
        if updated.status is TaskStatus.PENDING:
            deps.trace.emit(
                EventKind.TASK_RETRY,
                f"{task.task_id} attempt {attempt} failed: {error}",
                task_id=task.task_id,
                next_delay_s=deps.coordinator.retry.delay_for(attempt),
            )
        elif updated.status is TaskStatus.DEGRADED:
            deps.trace.emit(
                EventKind.TASK_DEGRADED,
                f"{task.task_id} degraded after {attempt} attempts: {error}",
                task_id=task.task_id,
            )
        else:
            deps.trace.emit(
                EventKind.TASK_BLOCKED,
                f"{task.task_id} blocked after {attempt} attempts: {error}",
                task_id=task.task_id,
            )
        return {"tasks": {task.task_id: updated}}

    return execute


def _reconcile_node(deps: Deps):
    """Re-query the weaker claimant of each contested figure."""

    def reconcile(state: RunState) -> dict:
        tasks = state["tasks"]
        task_updates: dict[str, Task] = {}
        conflict_updates: dict[str, Conflict] = {}

        for conflict in state["conflicts"].values():
            if conflict.status is not ConflictStatus.OPEN:
                continue
            if conflict.attempts >= deps.coordinator.max_conflict_attempts:
                continue
            target_id = deps.coordinator.resolution_target(conflict, tasks)
            if target_id is None:
                continue
            target = tasks[target_id]
            task_updates[target_id] = target.model_copy(
                update={
                    "status": TaskStatus.PENDING,
                    "context": {
                        "conflict": _conflict_brief(conflict, tasks),
                    },
                }
            )
            conflict_updates[conflict.conflict_id] = conflict.model_copy(
                update={
                    "attempts": conflict.attempts + 1,
                    "requeried_task": target_id,
                }
            )
            deps.trace.emit(
                EventKind.RUN_STATUS,
                f"re-querying {target_id} to settle {conflict.conflict_id}",
                task_id=target_id,
            )
        return {"tasks": task_updates, "conflicts": conflict_updates}

    return reconcile


def _signoff_node(deps: Deps):
    """Pause the run and wait for a human decision."""

    def signoff(state: RunState) -> dict:
        tasks = state["tasks"]
        pending = [
            task for task in tasks.values() if task.status is TaskStatus.AWAITING_SIGNOFF
        ]
        deps.trace.emit(
            EventKind.SIGNOFF_REQUESTED,
            f"{len(pending)} task(s) awaiting human sign-off",
            task_ids=[task.task_id for task in pending],
        )

        decision = interrupt(
            {
                "run_id": state["run_id"],
                "goal": state["goal"],
                "tasks": [task.task_id for task in pending],
                "draft": [
                    task.result.summary for task in pending if task.result is not None
                ],
                "report_path": _report_path(tasks),
                "open_conflicts": [
                    conflict.model_dump()
                    for conflict in state["conflicts"].values()
                    if conflict.status is not ConflictStatus.RESOLVED
                ],
                "degraded": [
                    {"task_id": task.task_id, "error": task.error}
                    for task in tasks.values()
                    if task.status is TaskStatus.DEGRADED
                ],
            }
        )
        record = Signoff.model_validate(decision)
        deps.gateway.call(
            Principal.human(record.reviewer),
            "record_signoff",
            reviewer=record.reviewer,
            approved=record.approved,
            note=record.note,
        )
        deps.trace.emit(
            EventKind.SIGNOFF_RECORDED,
            f"{record.reviewer} {'approved' if record.approved else 'rejected'} the run",
            actor=f"human:{record.reviewer}",
            note=record.note,
        )

        status = TaskStatus.DONE if record.approved else TaskStatus.DEGRADED
        updates = {
            task.task_id: task.model_copy(
                update={
                    "status": status,
                    "error": None if record.approved else "rejected by reviewer",
                }
            )
            for task in pending
        }
        return {"signoff": record, "tasks": updates}

    return signoff


def _finalize_node(deps: Deps):
    """Promote approved findings to durable memory and close the run."""

    def finalize(state: RunState) -> dict:
        tasks = state["tasks"]
        record = state.get("signoff")
        approved = record is None or record.approved
        report_task = _report_task(tasks)

        if approved and report_task is not None and report_task.result is not None:
            promoted = deps.memory.promote(
                report_task.result.claims,
                run_id=state["run_id"],
                confidence=report_task.result.confidence,
                human_approved=record is not None and record.approved,
            )
            if promoted:
                deps.trace.emit(
                    EventKind.MEMORY_PROMOTE,
                    f"promoted {len(promoted)} fact(s) to durable memory",
                    facts=[fact.as_context() for fact in promoted],
                )

        report_path = _report_path(tasks)
        summary = report_task.result.summary if report_task and report_task.result else None
        degraded = [
            task.task_id for task in tasks.values() if task.status is TaskStatus.DEGRADED
        ]
        deps.trace.emit(
            EventKind.REPORT,
            f"run {'completed' if approved else 'rejected'}"
            + (f", {len(degraded)} degraded task(s)" if degraded else ""),
            report_path=report_path,
            degraded=degraded,
        )
        return {
            "status": RunStatus.COMPLETED if approved else RunStatus.FAILED,
            "report": summary,
            "report_path": report_path,
        }

    return finalize


def _halt_node(deps: Deps):
    """Stop the run when no progress is possible."""

    def halt(state: RunState) -> dict:
        reason = deps.coordinator.decide(state).reason
        deps.trace.emit(EventKind.RUN_STATUS, f"halted: {reason}")
        return {"status": RunStatus.FAILED, "replan_reason": reason}

    return halt


def _dispatch(state: RunState, task: Task) -> Dispatch:
    """Build the fan-out payload for one ready task."""
    context = dict(task.context)
    if state["precedents"]:
        context["durable_memory"] = "\n".join(state["precedents"])
    upstream = _upstream_brief(state, task)
    if upstream:
        context["upstream_results"] = upstream
    gaps = _gaps_brief(state)
    if gaps and task.agent is AgentType.REPORTING:
        context["gaps_and_conflicts"] = gaps
    return Dispatch(
        run_id=state["run_id"],
        goal=state["goal"],
        task=task.model_dump(mode="json"),
        attempt=task.attempts + 1,
        context=context,
    )


def _upstream_brief(state: RunState, task: Task) -> str:
    lines: list[str] = []
    for dependency in task.depends_on:
        upstream = state["tasks"].get(dependency)
        if upstream is None or upstream.result is None:
            continue
        claims = "; ".join(
            f"{claim.subject}.{claim.metric}={claim.value}"
            for claim in upstream.result.claims
        )
        lines.append(
            f"{dependency} [{upstream.status.value}, "
            f"confidence {upstream.result.confidence:.2f}] "
            f"{upstream.result.summary}"
            + (f"\n  claims: {claims}" if claims else "")
        )
    return "\n".join(lines)


def _gaps_brief(state: RunState) -> str:
    lines = [
        f"degraded task {task.task_id}: {task.error}"
        for task in state["tasks"].values()
        if task.status is TaskStatus.DEGRADED
    ]
    lines += [
        f"{conflict.status.value} conflict on {conflict.subject}."
        f"{conflict.metric}: {conflict.values} - {conflict.resolution or 'unresolved'}"
        for conflict in state["conflicts"].values()
    ]
    return "\n".join(lines)


def _conflict_brief(conflict: Conflict, tasks: dict[str, Task]) -> str:
    lines = [
        f"Your claim {conflict.subject}.{conflict.metric} is contested. "
        "Re-check it against the source and state which period each figure covers."
    ]
    for task_id, value in conflict.values.items():
        agent = tasks[task_id].agent.value if task_id in tasks else "unknown"
        lines.append(f"- {task_id} ({agent}) reported {value}")
    return "\n".join(lines)


def _report_task(tasks: dict[str, Task]) -> Task | None:
    candidates = [
        task
        for task in tasks.values()
        if task.agent is AgentType.REPORTING and task.result is not None
    ]
    return candidates[-1] if candidates else None


def _report_path(tasks: dict[str, Task]) -> str | None:
    task = _report_task(tasks)
    if task is None or task.result is None or not task.result.evidence:
        return None
    return task.result.evidence[0]


def _blocked_reason(state: RunState) -> str:
    return "; ".join(
        f"{task.task_id} failed: {task.error}"
        for task in state["tasks"].values()
        if task.status is TaskStatus.BLOCKED
    ) or "plan could not make progress"

"""Planner agent: goal decomposition, plan validation and replanning.

A plan is only accepted once it passes structural validation - unique ids, known
agents, resolvable dependencies, no cycles, a reporting task to land on. An
invalid plan is rejected and the planner is re-prompted with the reason, which is
cheaper and far more predictable than letting the coordinator dispatch a broken
DAG.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from aios.llm import LlmClient, call_key
from aios.observability import EventKind, TraceStore
from aios.orchestration.state import (
    WORKER_AGENTS,
    AgentType,
    Task,
    TaskStatus,
)

PROMPT_TEMPLATE = """You are the Planner of a multi-agent orchestration platform.

You decompose one enterprise goal into a directed acyclic graph of tasks and
assign each task to exactly one worker agent.

Worker agents and what they can reach:
{capabilities}

Rules:
1. Between 4 and 8 tasks. Every task must be independently verifiable.
2. task_id is lowercase snake_case and stable, e.g. t2_exception_scan.
3. depends_on lists only task_ids declared in this same plan. No cycles.
4. Exactly one terminal reporting task; it depends on every task whose output
   belongs in the report, and it sets requires_signoff to true.
5. critical is false only for tasks the report can survive without.
6. success_criteria states the observable condition that makes the task done.
"""

DEFAULT_CAPABILITIES = {
    "research": "policy documents, prior audit memos, external benchmarks.",
    "data": "the transaction warehouse and the vendor ledger (named SQL queries).",
    "reporting": "synthesis of other agents' results into the final document.",
}


def build_system_prompt(capabilities: dict[str, str] | None = None) -> str:
    """The planner prompt, with the agent roster this deployment actually has.

    A plan can only be as good as this description. If it claims an agent can
    reach something it cannot, the planner writes tasks that are certain to fail.
    """
    roster = capabilities or DEFAULT_CAPABILITIES
    return PROMPT_TEMPLATE.format(
        capabilities="\n".join(
            f"- {agent}: {reach}" for agent, reach in roster.items()
        )
    )


SYSTEM_PROMPT = build_system_prompt()


class InvalidPlan(ValueError):
    """The planner produced a structurally unusable DAG."""


class PlannedTask(BaseModel):
    """One task as proposed by the planner."""

    task_id: str
    description: str
    agent: AgentType
    depends_on: list[str] = Field(default_factory=list)
    success_criteria: str
    critical: bool = True
    requires_signoff: bool = False

    def to_task(self) -> Task:
        return Task(**self.model_dump())


class Plan(BaseModel):
    """The planner's structured output."""

    rationale: str
    tasks: list[PlannedTask]


def validate_plan(tasks: list[PlannedTask]) -> None:
    """Raise `InvalidPlan` unless the proposed DAG is dispatchable."""
    if not 1 <= len(tasks) <= 12:
        raise InvalidPlan(f"expected between 1 and 12 tasks, got {len(tasks)}")

    ids = [task.task_id for task in tasks]
    duplicates = {task_id for task_id in ids if ids.count(task_id) > 1}
    if duplicates:
        raise InvalidPlan(f"duplicate task_ids: {sorted(duplicates)}")

    known = set(ids)
    for task in tasks:
        if task.agent not in WORKER_AGENTS:
            raise InvalidPlan(f"{task.task_id}: {task.agent.value} is not a worker agent")
        unknown = [dep for dep in task.depends_on if dep not in known]
        if unknown:
            raise InvalidPlan(f"{task.task_id}: unknown dependencies {unknown}")
        if task.task_id in task.depends_on:
            raise InvalidPlan(f"{task.task_id}: depends on itself")

    if not any(task.agent is AgentType.REPORTING for task in tasks):
        raise InvalidPlan("no reporting task, the run cannot produce an output")

    cycle = _find_cycle({task.task_id: task.depends_on for task in tasks})
    if cycle:
        raise InvalidPlan(f"dependency cycle: {' -> '.join(cycle)}")


def _find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle as a path, or None if the graph is acyclic."""
    unvisited, visiting, done = 0, 1, 2
    marks = dict.fromkeys(edges, unvisited)
    path: list[str] = []

    def walk(node: str) -> list[str] | None:
        marks[node] = visiting
        path.append(node)
        for dependency in edges[node]:
            if marks[dependency] == visiting:
                return path[path.index(dependency):] + [dependency]
            if marks[dependency] == unvisited:
                found = walk(dependency)
                if found:
                    return found
        marks[node] = done
        path.pop()
        return None

    for node in edges:
        if marks[node] == unvisited:
            cycle = walk(node)
            if cycle:
                return cycle
    return None


class Planner:
    """Turns a goal into a validated task DAG, and revises it on failure."""

    def __init__(
        self,
        llm: LlmClient,
        trace: TraceStore,
        max_attempts: int = 3,
        system_prompt: str | None = None,
    ) -> None:
        self._llm = llm
        self._trace = trace
        self._max_attempts = max_attempts
        self._system_prompt = system_prompt or SYSTEM_PROMPT

    def plan(self, goal: str, precedents: list[str]) -> dict[str, Task]:
        """Produce the initial task graph."""
        prompt = _initial_prompt(goal, precedents)
        plan = self._solicit(prompt, stage="plan")
        self._trace.emit(
            EventKind.PLAN,
            f"{len(plan.tasks)} tasks: {plan.rationale}",
            actor=AgentType.PLANNER.value,
            task_ids=[task.task_id for task in plan.tasks],
        )
        return {task.task_id: task.to_task() for task in plan.tasks}

    def replan(
        self,
        goal: str,
        tasks: dict[str, Task],
        reason: str,
        revision: int,
    ) -> dict[str, Task]:
        """Revise the plan around a failure, preserving finished work."""
        prompt = _replan_prompt(goal, tasks, reason)
        plan = self._solicit(prompt, stage="replan", attempt=revision)
        self._trace.emit(
            EventKind.REPLAN,
            f"revision {revision}: {plan.rationale}",
            actor=AgentType.PLANNER.value,
            reason=reason,
        )
        return _merge_revision(tasks, plan, revision)

    def _solicit(self, prompt: str, *, stage: str, attempt: int = 1) -> Plan:
        """Ask for a plan until one validates, or give up with the last error."""
        feedback = ""
        last_error = "planner returned no usable plan"
        for round_number in range(1, self._max_attempts + 1):
            plan = self._llm.structured(
                key=call_key(
                    AgentType.PLANNER.value,
                    stage,
                    attempt=max(attempt, round_number),
                ),
                system=self._system_prompt,
                prompt=prompt + feedback,
                output_model=Plan,
                actor=AgentType.PLANNER.value,
            )
            try:
                validate_plan(plan.tasks)
                return plan
            except InvalidPlan as error:
                last_error = str(error)
                self._trace.emit(
                    EventKind.PLAN_REJECTED,
                    f"attempt {round_number}: {last_error}",
                    actor=AgentType.PLANNER.value,
                )
                feedback = (
                    f"\n\nYour previous plan was rejected: {last_error}. "
                    "Return a corrected plan."
                )
        raise InvalidPlan(last_error)


def _merge_revision(
    tasks: dict[str, Task], plan: Plan, revision: int
) -> dict[str, Task]:
    """Keep finished tasks, adopt the revised ones, retire what was dropped."""
    proposed = {task.task_id: task for task in plan.tasks}
    merged: dict[str, Task] = {}
    for task_id, existing in tasks.items():
        if existing.resolved:
            merged[task_id] = existing
        elif task_id not in proposed:
            merged[task_id] = existing.model_copy(
                update={
                    "status": TaskStatus.SUPERSEDED,
                    "error": f"superseded by plan revision {revision}",
                }
            )
    for task_id, planned in proposed.items():
        if task_id not in merged:
            merged[task_id] = planned.to_task()
    return merged


def _initial_prompt(goal: str, precedents: list[str]) -> str:
    recalled = "\n".join(f"- {item}" for item in precedents) or "- none on record"
    return (
        f"Goal: {goal}\n\n"
        f"Durable memory recalled for this goal:\n{recalled}\n\n"
        "Produce the task DAG."
    )


def _replan_prompt(goal: str, tasks: dict[str, Task], reason: str) -> str:
    lines = [
        f"- {task.task_id} [{task.status.value}] {task.description}"
        + (f" (error: {task.error})" if task.error else "")
        for task in tasks.values()
    ]
    return (
        f"Goal: {goal}\n\n"
        f"Current plan state:\n" + "\n".join(lines) + "\n\n"
        f"Replanning is required because: {reason}\n\n"
        "Return the full revised plan. Repeat completed tasks unchanged with the "
        "same task_id, and route around what failed rather than retrying it "
        "unchanged."
    )

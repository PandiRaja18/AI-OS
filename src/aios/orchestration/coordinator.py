"""Coordinator: the scheduler of the platform.

This module is pure policy over run state - no LangGraph, no I/O - so every
scheduling rule is unit-testable in isolation. `graph.py` executes the decisions
made here.

Precedence, highest first: reconcile contradictions, collect a pending human
decision, dispatch ready work, replan around a blocked critical task, finalize.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field

from aios.orchestration.state import (
    Claim,
    Conflict,
    ConflictStatus,
    RunState,
    Task,
    TaskStatus,
)


class Action(enum.StrEnum):
    DISPATCH = "dispatch"
    RECONCILE = "reconcile"
    SIGNOFF = "signoff"
    REPLAN = "replan"
    FINALIZE = "finalize"
    HALT = "halt"


@dataclass(frozen=True)
class Decision:
    """What the coordinator wants the graph to do next."""

    action: Action
    task_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries with exponential backoff, per task."""

    max_attempts: int = 3
    backoff_seconds: float = 0.25

    def delay_for(self, attempt: int) -> float:
        return self.backoff_seconds * (2 ** max(attempt - 1, 0))


@dataclass
class Coordinator:
    """Scheduling, retry, conflict and escalation policy."""

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    max_conflict_attempts: int = 2
    max_plan_revisions: int = 2

    def ready(self, tasks: dict[str, Task]) -> list[Task]:
        """Pending tasks whose dependencies have all resolved."""
        return [
            task
            for task in tasks.values()
            if task.status is TaskStatus.PENDING
            and all(tasks[dep].resolved for dep in task.depends_on if dep in tasks)
        ]

    def decide(self, state: RunState) -> Decision:
        """Choose the next action for a run."""
        tasks = state["tasks"]
        conflicts = state["conflicts"]

        reconcilable = tuple(
            conflict_id
            for conflict_id, conflict in conflicts.items()
            if self._is_reconcilable(conflict, tasks)
        )
        if reconcilable:
            return Decision(Action.RECONCILE, conflict_ids=reconcilable)

        pending_signoff = tuple(
            task_id
            for task_id, task in tasks.items()
            if task.status is TaskStatus.AWAITING_SIGNOFF
        )
        if pending_signoff and state.get("signoff") is None:
            return Decision(Action.SIGNOFF, task_ids=pending_signoff)

        ready = tuple(task.task_id for task in self.ready(tasks))
        if ready:
            return Decision(Action.DISPATCH, task_ids=ready)

        blocked = tuple(
            task_id
            for task_id, task in tasks.items()
            if task.status is TaskStatus.BLOCKED
        )
        if blocked:
            if state["plan_revision"] < self.max_plan_revisions:
                reasons = "; ".join(
                    f"{task_id} failed: {tasks[task_id].error}" for task_id in blocked
                )
                return Decision(Action.REPLAN, task_ids=blocked, reason=reasons)
            return Decision(
                Action.HALT,
                task_ids=blocked,
                reason="critical tasks blocked and the replan budget is exhausted",
            )

        if tasks and all(task.resolved for task in tasks.values()):
            return Decision(Action.FINALIZE)

        return Decision(Action.HALT, reason="no dispatchable work remains")

    def _is_reconcilable(self, conflict: Conflict, tasks: dict[str, Task]) -> bool:
        """Whether a contradiction is worth another re-query right now.

        A re-query already in flight is left alone: the claimant is dispatched
        again first, and the conflict is re-evaluated against its new answer.
        """
        if conflict.status is not ConflictStatus.OPEN:
            return False
        if conflict.attempts >= self.max_conflict_attempts:
            return False
        target_id = self.resolution_target(conflict, tasks)
        if target_id is None:
            return False
        return tasks[target_id].status not in (
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
        )

    def on_failure(self, task: Task, error: str) -> Task:
        """Apply the retry policy to a failed task."""
        attempts = task.attempts
        if attempts < self.retry.max_attempts:
            return task.model_copy(
                update={"status": TaskStatus.PENDING, "error": error}
            )
        status = TaskStatus.BLOCKED if task.critical else TaskStatus.DEGRADED
        return task.model_copy(update={"status": status, "error": error})

    def detect_conflicts(
        self, tasks: dict[str, Task], known: dict[str, Conflict]
    ) -> dict[str, Conflict]:
        """Compare claims across agents and open, close or keep conflicts."""
        grouped: dict[tuple[str, str], dict[str, str]] = {}
        for task in tasks.values():
            if task.result is None or task.status is TaskStatus.SUPERSEDED:
                continue
            for claim in task.result.claims:
                key = (_normalize(claim.subject), _normalize(claim.metric))
                grouped.setdefault(key, {})[task.task_id] = claim.value

        changed: dict[str, Conflict] = {}
        for (subject, metric), values in grouped.items():
            conflict_id = f"c_{subject}_{metric}"
            existing = known.get(conflict_id)
            contested = len(set(values.values())) > 1

            if contested and len(values) > 1:
                if existing is None:
                    changed[conflict_id] = Conflict(
                        conflict_id=conflict_id,
                        subject=subject,
                        metric=metric,
                        values=values,
                    )
                elif existing.status is ConflictStatus.OPEN:
                    changed[conflict_id] = existing.model_copy(
                        update={"values": values}
                    )
            elif existing is not None and existing.status is ConflictStatus.OPEN:
                resolution = _resolution_note(existing, tasks)
                changed[conflict_id] = existing.model_copy(
                    update={
                        "status": ConflictStatus.RESOLVED,
                        "values": values,
                        "resolution": resolution,
                    }
                )
        return changed

    def resolution_target(
        self, conflict: Conflict, tasks: dict[str, Task]
    ) -> str | None:
        """The claimant to re-query: the one that asserted with least confidence."""
        candidates = [
            tasks[task_id]
            for task_id in conflict.values
            if task_id in tasks and tasks[task_id].result is not None
        ]
        if len(candidates) < 2:
            return None
        weakest = min(candidates, key=lambda task: task.result.confidence)
        return weakest.task_id

    def escalate(self, conflict: Conflict) -> Conflict:
        """Hand an unresolvable contradiction to the human reviewer."""
        return conflict.model_copy(
            update={
                "status": ConflictStatus.ESCALATED,
                "resolution": (
                    f"unresolved after {conflict.attempts} re-queries; "
                    "requires human judgement"
                ),
            }
        )

    def approved_claims(self, tasks: dict[str, Task]) -> list[Claim]:
        """Claims eligible for promotion to durable memory."""
        return [
            claim
            for task in tasks.values()
            if task.status is TaskStatus.DONE and task.result is not None
            for claim in task.result.claims
        ]


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _resolution_note(conflict: Conflict, tasks: dict[str, Task]) -> str:
    task_id = conflict.requeried_task
    task = tasks.get(task_id) if task_id else None
    if task is not None and task.result is not None:
        return f"re-query of {task_id}: {task.result.summary}"
    return "claims converged after re-query"

"""Scheduling, retry, conflict and escalation policy."""

from __future__ import annotations

from aios.orchestration.coordinator import Action, Coordinator, RetryPolicy
from aios.orchestration.state import (
    ConflictStatus,
    RunStatus,
    Signoff,
    TaskStatus,
    new_run_state,
)
from tests.conftest import done, make_result, make_task


def state_with(tasks, conflicts=None, **overrides):
    state = new_run_state("run-test", "goal", [])
    state["tasks"] = {task.task_id: task for task in tasks}
    state["conflicts"] = conflicts or {}
    state["plan_revision"] = 1
    state["status"] = RunStatus.RUNNING
    state.update(overrides)
    return state


def test_only_tasks_with_resolved_dependencies_are_ready():
    first = make_task("t1")
    second = make_task("t2", depends_on=["t1"])
    coordinator = Coordinator()

    tasks = {"t1": first, "t2": second}
    assert [task.task_id for task in coordinator.ready(tasks)] == ["t1"]

    tasks["t1"] = done(first, make_result())
    assert [task.task_id for task in coordinator.ready(tasks)] == ["t2"]


def test_a_degraded_dependency_does_not_block_its_dependents():
    blocked = make_task("t1").model_copy(update={"status": TaskStatus.DEGRADED})
    dependent = make_task("t2", depends_on=["t1"])
    coordinator = Coordinator()
    ready = coordinator.ready({"t1": blocked, "t2": dependent})
    assert [task.task_id for task in ready] == ["t2"]


def test_dispatch_is_chosen_for_ready_work():
    decision = Coordinator().decide(state_with([make_task("t1")]))
    assert decision.action is Action.DISPATCH
    assert decision.task_ids == ("t1",)


def test_finalize_once_everything_is_resolved():
    decision = Coordinator().decide(
        state_with([done(make_task("t1"), make_result())])
    )
    assert decision.action is Action.FINALIZE


def test_signoff_takes_precedence_over_finalizing():
    awaiting = make_task("t1").model_copy(
        update={"status": TaskStatus.AWAITING_SIGNOFF, "requires_signoff": True}
    )
    decision = Coordinator().decide(state_with([awaiting]))
    assert decision.action is Action.SIGNOFF


def test_a_recorded_signoff_is_not_requested_again():
    approved = make_task("t1").model_copy(
        update={"status": TaskStatus.AWAITING_SIGNOFF}
    )
    state = state_with(
        [approved], signoff=Signoff(approved=True, reviewer="reviewer")
    )
    assert Coordinator().decide(state).action is Action.HALT


def test_retry_then_degrade_a_non_critical_task():
    coordinator = Coordinator(retry=RetryPolicy(max_attempts=2, backoff_seconds=0))
    task = make_task("t1", critical=False).model_copy(update={"attempts": 1})

    retried = coordinator.on_failure(task, "boom")
    assert retried.status is TaskStatus.PENDING

    exhausted = coordinator.on_failure(
        task.model_copy(update={"attempts": 2}), "boom"
    )
    assert exhausted.status is TaskStatus.DEGRADED


def test_a_critical_task_blocks_and_triggers_a_replan():
    coordinator = Coordinator(retry=RetryPolicy(max_attempts=1, backoff_seconds=0))
    task = make_task("t1", critical=True).model_copy(update={"attempts": 1})
    blocked = coordinator.on_failure(task, "ledger down")
    assert blocked.status is TaskStatus.BLOCKED

    decision = coordinator.decide(state_with([blocked]))
    assert decision.action is Action.REPLAN
    assert "ledger down" in decision.reason


def test_the_replan_budget_is_bounded():
    coordinator = Coordinator(max_plan_revisions=1)
    blocked = make_task("t1").model_copy(update={"status": TaskStatus.BLOCKED})
    decision = coordinator.decide(state_with([blocked], plan_revision=1))
    assert decision.action is Action.HALT


def test_backoff_grows_with_each_attempt():
    retry = RetryPolicy(max_attempts=4, backoff_seconds=0.5)
    assert [retry.delay_for(n) for n in (1, 2, 3)] == [0.5, 1.0, 2.0]


def test_contradicting_claims_open_a_conflict():
    coordinator = Coordinator()
    tasks = {
        "t1": done(make_task("t1"), make_result(("Vendor A", "exposure_usd", "100"))),
        "t2": done(
            make_task("t2", agent="research"),
            make_result(("vendor a", "Exposure USD", "200"), confidence=0.5),
        ),
    }
    conflicts = coordinator.detect_conflicts(tasks, {})

    assert len(conflicts) == 1
    conflict = next(iter(conflicts.values()))
    assert conflict.status is ConflictStatus.OPEN
    assert conflict.values == {"t1": "100", "t2": "200"}
    assert coordinator.resolution_target(conflict, tasks) == "t2"


def test_agreeing_claims_do_not_open_a_conflict():
    tasks = {
        "t1": done(make_task("t1"), make_result(("Vendor A", "exposure_usd", "100"))),
        "t2": done(
            make_task("t2", agent="research"),
            make_result(("Vendor A", "exposure_usd", "100")),
        ),
    }
    assert Coordinator().detect_conflicts(tasks, {}) == {}


def test_a_conflict_closes_once_the_requery_agrees():
    coordinator = Coordinator()
    tasks = {
        "t1": done(make_task("t1"), make_result(("Vendor A", "exposure_usd", "100"))),
        "t2": done(
            make_task("t2", agent="research"),
            make_result(("Vendor A", "exposure_usd", "200"), confidence=0.5),
        ),
    }
    opened = coordinator.detect_conflicts(tasks, {})
    conflict_id = next(iter(opened))
    opened[conflict_id] = opened[conflict_id].model_copy(
        update={"attempts": 1, "requeried_task": "t2"}
    )

    tasks["t2"] = done(
        make_task("t2", agent="research"),
        make_result(("Vendor A", "exposure_usd", "100"), confidence=0.85),
    )
    closed = coordinator.detect_conflicts(tasks, opened)

    assert closed[conflict_id].status is ConflictStatus.RESOLVED
    assert "re-query of t2" in closed[conflict_id].resolution


def test_a_requery_in_flight_is_not_reconciled_again():
    coordinator = Coordinator()
    tasks = {
        "t1": done(make_task("t1"), make_result(("Vendor A", "exposure_usd", "100"))),
        "t2": done(
            make_task("t2", agent="research"),
            make_result(("Vendor A", "exposure_usd", "200"), confidence=0.5),
        ),
    }
    conflicts = coordinator.detect_conflicts(tasks, {})
    assert coordinator.decide(state_with(tasks.values(), conflicts)).action is (
        Action.RECONCILE
    )

    conflict_id = next(iter(conflicts))
    conflicts[conflict_id] = conflicts[conflict_id].model_copy(
        update={"attempts": 1, "requeried_task": "t2"}
    )
    tasks["t2"] = tasks["t2"].model_copy(update={"status": TaskStatus.PENDING})

    decision = coordinator.decide(state_with(tasks.values(), conflicts))
    assert decision.action is Action.DISPATCH
    assert decision.task_ids == ("t2",)


def test_escalation_describes_the_unresolved_conflict():
    coordinator = Coordinator()
    tasks = {
        "t1": done(make_task("t1"), make_result(("Vendor A", "exposure_usd", "100"))),
        "t2": done(
            make_task("t2", agent="research"),
            make_result(("Vendor A", "exposure_usd", "200"), confidence=0.5),
        ),
    }
    conflict = next(iter(coordinator.detect_conflicts(tasks, {}).values()))
    escalated = coordinator.escalate(conflict.model_copy(update={"attempts": 2}))
    assert escalated.status is ConflictStatus.ESCALATED
    assert "human judgement" in escalated.resolution


def test_superseded_task_claims_are_ignored():
    coordinator = Coordinator()
    superseded = done(
        make_task("t1"), make_result(("Vendor A", "exposure_usd", "100"))
    ).model_copy(update={"status": TaskStatus.SUPERSEDED})
    tasks = {
        "t1": superseded,
        "t2": done(
            make_task("t2", agent="research"),
            make_result(("Vendor A", "exposure_usd", "200")),
        ),
    }
    assert coordinator.detect_conflicts(tasks, {}) == {}

"""End-to-end runs against the recorded scenarios.

These exercise the real graph, coordinator, gateway, tools and checkpointer -
only the model calls are replayed.
"""

from __future__ import annotations

from pathlib import Path

from aios.config import Settings
from aios.observability import EventKind, read_trace
from aios.orchestration.state import ConflictStatus, RunStatus, Signoff, TaskStatus
from aios.runtime import Runtime


def run_to_signoff(settings: Settings, scenario: str = "audit") -> tuple:
    runtime = Runtime(settings, offline=True, scenario=scenario)
    outcome = runtime.start("Prepare the FY26-Q3 quarterly audit review")
    return runtime, outcome


def test_the_run_pauses_at_the_signoff_gate(settings: Settings):
    _, outcome = run_to_signoff(settings)

    assert outcome.paused
    assert outcome.status is RunStatus.AWAITING_SIGNOFF
    pending = outcome.pending_signoff
    assert pending["tasks"] == ["t6_audit_report"]
    assert "86,900 USD" in pending["draft"][0]
    assert Path(pending["report_path"]).exists()


def test_approving_completes_the_run_and_promotes_findings(settings: Settings):
    runtime, outcome = run_to_signoff(settings)
    outcome = runtime.resume(
        outcome.run_id, Signoff(approved=True, reviewer="auditor", note="ok")
    )

    assert outcome.status is RunStatus.COMPLETED
    tasks = outcome.state["tasks"]
    assert tasks["t6_audit_report"].status is TaskStatus.DONE
    assert {fact.subject for fact in runtime.memory.facts()} >= {
        "Northwind Logistics",
        "Meridian Consulting",
        "FY26-Q3",
    }
    assert Path(outcome.state["report_path"]).read_text(encoding="utf-8").startswith(
        "# FY26-Q3"
    )


def test_rejecting_fails_the_run_and_promotes_nothing(settings: Settings):
    runtime, outcome = run_to_signoff(settings)
    outcome = runtime.resume(
        outcome.run_id, Signoff(approved=False, reviewer="auditor", note="figures")
    )

    assert outcome.status is RunStatus.FAILED
    assert runtime.memory.facts() == []


def test_a_flaky_tool_is_retried_until_it_succeeds(settings: Settings):
    _, outcome = run_to_signoff(settings)
    task = outcome.state["tasks"]["t4_ledger_verification"]

    assert task.status is TaskStatus.DONE
    assert task.attempts == settings.injected_ledger_failures + 1


def test_an_unreachable_tool_degrades_a_non_critical_task(settings: Settings):
    _, outcome = run_to_signoff(settings)
    task = outcome.state["tasks"]["t5_peer_benchmark"]

    assert task.status is TaskStatus.DEGRADED
    assert task.attempts == settings.max_task_attempts
    assert "unreachable" in task.error


def test_contradicting_figures_are_reconciled_by_a_requery(settings: Settings):
    _, outcome = run_to_signoff(settings)
    conflict = next(iter(outcome.state["conflicts"].values()))

    assert conflict.status is ConflictStatus.RESOLVED
    assert conflict.requeried_task == "t3_vendor_precedent"
    assert outcome.state["tasks"]["t3_vendor_precedent"].attempts == 2
    assert (
        outcome.state["tasks"]["t3_vendor_precedent"].result.claims[0].value
        == "55700.00"
    )


def test_independent_tasks_are_dispatched_in_one_wave(settings: Settings):
    _, outcome = run_to_signoff(settings)
    events = list(read_trace(settings.trace_dir, outcome.run_id))
    dispatches = [
        event
        for event in events
        if event.kind is EventKind.RUN_STATUS
        and event.message.startswith("dispatch: ")
    ]
    fan_out = [event for event in dispatches if event.message.count(",") == 2]
    assert fan_out, "expected one dispatch wave with three parallel tasks"


def test_the_trace_is_replayable_from_disk(settings: Settings):
    _, outcome = run_to_signoff(settings)
    events = list(read_trace(settings.trace_dir, outcome.run_id))

    assert events[0].kind is EventKind.RUN_STARTED
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    kinds = {event.kind for event in events}
    assert {
        EventKind.PLAN,
        EventKind.DISPATCH,
        EventKind.TOOL_CALL,
        EventKind.TOOL_ERROR,
        EventKind.TASK_RETRY,
        EventKind.TASK_DEGRADED,
        EventKind.CONFLICT_DETECTED,
        EventKind.CONFLICT_RESOLVED,
        EventKind.SIGNOFF_REQUESTED,
    } <= kinds


def test_the_trace_stays_ordered_across_a_resume(settings: Settings):
    runtime, outcome = run_to_signoff(settings)
    before = list(read_trace(settings.trace_dir, outcome.run_id))
    runtime.resume(outcome.run_id, Signoff(approved=True, reviewer="auditor"))
    after = list(read_trace(settings.trace_dir, outcome.run_id))

    assert len(after) > len(before)
    assert [event.seq for event in after] == list(range(1, len(after) + 1))
    assert after[-1].kind is EventKind.REPORT


def test_a_blocked_critical_task_forces_a_replan(settings: Settings):
    configured = settings.model_copy(update={"outage_tools": ("ledger_lookup",)})
    runtime = Runtime(configured, offline=True, scenario="audit_outage")
    outcome = runtime.start("Prepare the FY26-Q3 quarterly audit review")
    outcome = runtime.resume(
        outcome.run_id, Signoff(approved=True, reviewer="auditor")
    )

    tasks = outcome.state["tasks"]
    assert outcome.state["plan_revision"] == 2
    assert tasks["t4_ledger_verification"].status is TaskStatus.SUPERSEDED
    assert tasks["t4b_memo_crosscheck"].status is TaskStatus.DONE
    assert outcome.status is RunStatus.COMPLETED


def test_a_report_below_the_confidence_bar_is_not_promoted(settings: Settings):
    configured = settings.model_copy(update={"outage_tools": ("ledger_lookup",)})
    runtime = Runtime(configured, offline=True, scenario="audit_outage")
    outcome = runtime.start("Prepare the FY26-Q3 quarterly audit review")
    runtime.resume(outcome.run_id, Signoff(approved=True, reviewer="auditor"))

    assert runtime.memory.facts() == []

"""End-to-end tests for the hosted platform.

These exercise the real queue, leases, workers, tenancy, budgets, review inbox
and orchestration graph. Only the model calls are replayed, so the whole suite
runs in seconds with no credentials and no infrastructure.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from aios.config import Settings
from aios.platform import Lane, Platform, Rejected, RunStatus, TenantPolicy, Worker
from aios.platform.budget import Budget
from aios.platform.models import Lease, PrincipalKind, ReviewStatus, now
from aios.platform.sql.reviews import NotAHumanDecision

GOAL = "Prepare the FY26-Q3 quarterly audit review"


def expire_lease(platform: Platform, run_id: str) -> None:
    """Simulate a worker dying: its lease stops being renewed."""
    platform.database.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00.000000+00:00", run_id),
    )


def carry_to_gate(platform: Platform, caller, **submit) -> tuple:
    """Submit a run and let one worker take it to the sign-off gate."""
    record = platform.submit(caller, GOAL, **submit)
    worker = Worker(platform, "worker-1")
    worker.run_once()
    return record, platform.reviews.for_run(record.run_id), worker


# --- the happy path -----------------------------------------------------------


def test_a_run_pauses_at_the_gate_and_frees_its_worker(platform, alice):
    record, review, _ = carry_to_gate(platform, alice)
    stored = platform.runs.get(record.run_id)

    assert stored.status is RunStatus.AWAITING_SIGNOFF
    assert stored.lease is None, "a paused run must not hold a worker"
    assert review is not None and review.status is ReviewStatus.PENDING
    assert "86,900 USD" in review.draft
    assert review.degraded_tasks == 1


def test_approval_completes_the_run_and_promotes_findings(platform, alice, cfo):
    record, review, worker = carry_to_gate(platform, alice)
    platform.decide(cfo, review.review_id, approved=True, note="signed")

    assert platform.runs.get(record.run_id).status is RunStatus.QUEUED
    worker.run_once()

    stored = platform.runs.get(record.run_id)
    assert stored.status is RunStatus.COMPLETED
    assert stored.report_uri and platform.objects.exists(stored.report_uri)
    assert {fact.subject for fact in platform.facts.facts("acme")} >= {
        "Northwind Logistics",
        "Meridian Consulting",
        "FY26-Q3",
    }


def test_rejection_fails_the_run_and_promotes_nothing(platform, alice, cfo):
    record, review, worker = carry_to_gate(platform, alice)
    platform.decide(cfo, review.review_id, approved=False, note="figures unclear")
    worker.run_once()

    assert platform.runs.get(record.run_id).status is RunStatus.FAILED
    assert platform.facts.facts("acme") == []


def test_the_report_lives_in_object_storage_not_on_the_worker(platform, alice, cfo):
    record, review, worker = carry_to_gate(platform, alice)
    platform.decide(cfo, review.review_id, approved=True)
    worker.run_once()

    uri = platform.runs.get(record.run_id).report_uri
    assert uri.startswith("aios://acme/")
    assert platform.objects.get(uri).startswith("# FY26-Q3")


# --- durability ---------------------------------------------------------------


def test_a_dead_worker_hands_the_run_to_another_without_redoing_work(
    platform, alice, cfo
):
    record, review, worker_a = carry_to_gate(platform, alice)
    platform.decide(cfo, review.review_id, approved=True)
    attempts_before = len(platform.runs.attempts(record.run_id))

    # Worker B leases the resumed run and then dies holding it.
    leased = platform.queue.lease("worker-b", for_seconds=120)
    assert leased.run_id == record.run_id
    expire_lease(platform, record.run_id)

    assert platform.sweep().requeued == [record.run_id]
    assert platform.runs.get(record.run_id).lease_attempts == 1

    Worker(platform, "worker-c").run_once()
    stored = platform.runs.get(record.run_id)

    assert stored.status is RunStatus.COMPLETED
    assert len(platform.runs.attempts(record.run_id)) == attempts_before, (
        "resuming from the checkpoint must not re-run finished tasks"
    )


def test_a_run_that_keeps_outliving_workers_is_failed_not_requeued(platform, alice):
    record = platform.submit(alice, GOAL)
    for expected in range(1, platform.queue.max_lease_attempts + 1):
        platform.queue.lease(f"worker-{expected}", for_seconds=120)
        expire_lease(platform, record.run_id)
        platform.sweep()
        assert platform.runs.get(record.run_id).lease_attempts == expected

    platform.queue.lease("worker-last", for_seconds=120)
    expire_lease(platform, record.run_id)
    platform.sweep()

    stored = platform.runs.get(record.run_id)
    assert stored.status is RunStatus.FAILED
    assert "handover limit" in stored.failure_reason


def test_two_workers_never_take_the_same_run(platform, alice):
    first = platform.submit(alice, GOAL)
    second = platform.submit(alice, "Investigate the APAC churn spike")

    a = platform.queue.lease("worker-a", for_seconds=60)
    b = platform.queue.lease("worker-b", for_seconds=60)
    c = platform.queue.lease("worker-c", for_seconds=60)

    assert {a.run_id, b.run_id} == {first.run_id, second.run_id}
    assert c is None, "an empty queue must not hand out a third run"


def test_a_foreign_worker_cannot_extend_a_lease(platform, alice):
    record = platform.submit(alice, GOAL)
    platform.queue.lease("worker-a", for_seconds=60)

    assert platform.queue.heartbeat(record.run_id, "worker-a") is True
    assert platform.queue.heartbeat(record.run_id, "worker-b") is False


def test_interactive_work_is_leased_before_batch(platform, alice):
    platform.policy.upsert_tenant(
        platform.policy.tenant("acme").model_copy(update={"max_concurrent_runs": 5})
    )
    platform.submit(alice, "batch one", lane=Lane.BATCH)
    platform.submit(alice, "backfill one", lane=Lane.BACKFILL)
    urgent = platform.submit(alice, GOAL, lane=Lane.INTERACTIVE)

    assert platform.queue.lease("worker-a", for_seconds=60).run_id == urgent.run_id


# --- tenancy ------------------------------------------------------------------


def test_a_tenant_cannot_see_another_tenants_runs(platform, alice, bob):
    mine = platform.submit(alice, GOAL)
    theirs = platform.submit(bob, GOAL)

    assert platform.runs.get(mine.run_id, "globex") is None
    assert platform.runs.get(theirs.run_id, "acme") is None
    assert [r.run_id for r in platform.runs.list("globex")] == [theirs.run_id]


def test_facts_do_not_leak_between_tenants(platform, alice, bob, cfo, dana):
    for caller, reviewer in ((alice, cfo), (bob, dana)):
        record, review, worker = carry_to_gate(platform, caller)
        platform.decide(reviewer, review.review_id, approved=True)
        worker.run_once()

    acme = {(f.subject, f.value) for f in platform.facts.facts("acme")}
    globex = {(f.subject, f.value) for f in platform.facts.facts("globex")}
    assert acme and globex
    for fact in platform.facts.facts("acme"):
        assert fact.provenance in {
            run.run_id for run in platform.runs.list("acme")
        }


def test_a_reviewer_cannot_decide_another_tenants_review(platform, alice, dana):
    _, review, _ = carry_to_gate(platform, alice)
    with pytest.raises(Exception):
        platform.decide(dana, review.review_id, approved=True)


def test_only_a_human_principal_may_sign_off(platform, alice):
    _, review, _ = carry_to_gate(platform, alice)
    robot = platform.tokens.resolve(
        platform.token_for("acme", "nightly-job", PrincipalKind.SERVICE)
    )
    with pytest.raises(NotAHumanDecision):
        platform.decide(robot, review.review_id, approved=True)


def test_a_tool_the_tenant_was_not_granted_cannot_be_reached(platform, alice):
    platform.policy.revoke_grant("acme", "sql_query")
    record, review, _ = carry_to_gate(platform, alice)

    trace = platform.trace.query(run_id=record.run_id)
    messages = " ".join(event.message for event in trace)
    assert "unknown tool: sql_query" in messages
    assert platform.runs.get(record.run_id).status is not RunStatus.COMPLETED


# --- limits -------------------------------------------------------------------


def test_the_concurrency_cap_rejects_before_the_run_exists(platform, alice):
    for _ in range(2):
        platform.submit(alice, GOAL)
    with pytest.raises(Rejected) as rejected:
        platform.submit(alice, GOAL)

    assert "concurrency cap" in str(rejected.value)
    assert rejected.value.decision.retry_after_seconds == 30
    assert len(platform.runs.list("acme")) == 2


def test_a_run_halts_when_it_crosses_its_token_budget(platform, alice):
    record = platform.submit(alice, GOAL, budget=Budget(max_input_tokens=200))
    Worker(platform, "worker-1").run_once()

    stored = platform.runs.get(record.run_id)
    assert stored.status is RunStatus.FAILED
    assert "budget exceeded" in stored.failure_reason
    assert stored.spend.llm_calls > 0, "it should halt mid-run, not before starting"


def test_spend_is_metered_on_every_model_call(platform, alice):
    record, _, _ = carry_to_gate(platform, alice)
    spend = platform.runs.get(record.run_id).spend

    assert spend.llm_calls >= 13
    assert spend.input_tokens > 0 and spend.output_tokens > 0


def test_a_run_past_its_deadline_does_not_start(platform, alice):
    record = platform.submit(alice, GOAL, deadline_seconds=-1)
    Worker(platform, "worker-1").run_once()

    stored = platform.runs.get(record.run_id)
    assert stored.status is RunStatus.FAILED
    assert "deadline" in stored.failure_reason


# --- the human gate -----------------------------------------------------------


def test_a_review_nobody_answers_expires_and_closes_the_run(platform, alice):
    record, review, _ = carry_to_gate(platform, alice)
    platform.database.execute(
        "UPDATE reviews SET expires_at = ? WHERE review_id = ?",
        ("2000-01-01T00:00:00.000000+00:00", review.review_id),
    )

    swept = platform.sweep()
    stored = platform.runs.get(record.run_id)

    assert swept.expired == [review.review_id]
    assert stored.status is RunStatus.EXPIRED
    assert "no reviewer decision" in stored.failure_reason


def test_a_review_can_only_be_decided_once(platform, alice, cfo):
    _, review, _ = carry_to_gate(platform, alice)
    platform.decide(cfo, review.review_id, approved=True)

    with pytest.raises(Exception):
        platform.decide(cfo, review.review_id, approved=False)


def test_a_run_waiting_on_a_review_is_not_leasable(platform, alice):
    carry_to_gate(platform, alice)
    assert platform.queue.lease("worker-b", for_seconds=60) is None


def test_the_inbox_shows_a_reviewer_only_their_assignments(platform, alice, cfo):
    _, review, _ = carry_to_gate(platform, alice)
    assert [r.review_id for r in platform.pending_reviews(cfo)] == [review.review_id]

    unassigned = platform.tokens.resolve(platform.token_for("acme", "intern"))
    assert platform.pending_reviews(unassigned) == []


# --- replanning ---------------------------------------------------------------


def test_a_dead_tool_forces_a_replan_and_the_report_is_not_promoted(
    outage_platform, outage_caller, outage_reviewer
):
    record, review, worker = carry_to_gate(
        outage_platform, outage_caller, scenario="audit_outage"
    )
    outage_platform.decide(outage_reviewer, review.review_id, approved=True)
    worker.run_once()

    stored = outage_platform.runs.get(record.run_id)
    kinds = [e.kind for e in outage_platform.trace.query(run_id=record.run_id)]

    assert stored.status is RunStatus.COMPLETED
    assert "task_blocked" in kinds and "replan" in kinds
    assert outage_platform.facts.facts("acme") == [], (
        "a report below the confidence bar must not enter durable memory"
    )


# --- observability ------------------------------------------------------------


def test_the_trace_is_continuous_across_the_pause(platform, alice, cfo):
    record, review, worker = carry_to_gate(platform, alice)
    platform.decide(cfo, review.review_id, approved=True)
    worker.run_once()

    events = platform.trace.query(run_id=record.run_id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[-1].kind == "report"


def test_every_task_execution_is_recorded_including_failures(platform, alice):
    record, _, _ = carry_to_gate(platform, alice)
    attempts = platform.runs.attempts(record.run_id)

    outcomes = {attempt.outcome for attempt in attempts}
    ledger = [a for a in attempts if a.task_id == "t4_ledger_verification"]
    assert len(ledger) == 3, "two failures and the success are all on the record"
    assert "tool_error" in outcomes and "ok" in outcomes

"""Rebuilding a readable run view from nothing but the trace."""

from __future__ import annotations

from aios.observability import TraceStore, build_run_view
from aios.observability.runview import TaskState
from aios.platform import Platform, TenantPolicy

GOAL = "Which urgent problems are overdue, and who needs telling?"


def completed_run(platform: Platform, scenario: str = "support"):
    """Run the recorded scenario to completion and return its trace."""
    from aios.orchestration.state import Signoff
    from aios.platform.worker import Worker

    operator = platform.tokens.resolve(platform.token_for("acme", "alice"))
    record = platform.submit(operator, GOAL, scenario=scenario)
    worker = Worker(platform, "worker-1")
    worker.run_once()

    review = platform.reviews.for_run(record.run_id)
    reviewer = platform.tokens.resolve(platform.token_for("acme", "cfo"))
    platform.decide(reviewer, review.review_id, approved=True)
    worker.run_once()
    return record.run_id, platform.trace.query("acme", record.run_id, limit=2000)


def support_platform(settings, tmp_path):
    from aios.config import write_active_domain
    from aios.demo.support import build_support_demo

    _, pack = build_support_demo(settings)
    write_active_domain(settings.workspace, pack)
    configured = settings.model_copy(update={"domain_file": pack})
    platform = Platform(configured, database_url=f"sqlite:///{tmp_path / 'rv.db'}")
    platform.provision_tenant(
        TenantPolicy(tenant_id="acme", max_concurrent_runs=4, reviewers=("cfo",))
    )
    return platform


def test_the_task_graph_is_rebuilt_from_the_trace_alone(settings, tmp_path):
    platform = support_platform(settings, tmp_path)
    run_id, events = completed_run(platform)
    view = build_run_view(run_id, events)

    assert view.goal == GOAL
    assert [task.task_id for task in view.tasks] == [
        "t1_deadline_rule",
        "t2_live_count",
        "t3_last_summary",
        "t4_review",
    ]
    assert all(task.state is TaskState.DONE for task in view.tasks)


def test_dependency_depth_puts_parallel_work_on_the_same_row(settings, tmp_path):
    platform = support_platform(settings, tmp_path)
    run_id, events = completed_run(platform)
    depths = {task.task_id: task.depth for task in build_run_view(run_id, events).tasks}

    assert depths["t1_deadline_rule"] == 0
    assert depths["t2_live_count"] == depths["t3_last_summary"] == 1
    assert depths["t4_review"] == 2


def test_a_task_carries_its_evidence_and_claims(settings, tmp_path):
    platform = support_platform(settings, tmp_path)
    run_id, events = completed_run(platform)
    tasks = {task.task_id: task for task in build_run_view(run_id, events).tasks}
    data = tasks["t2_live_count"]

    assert data.agent == "data"
    assert data.tools == ["describe_schema", "sql_query"]
    assert data.confidence == 0.94
    assert any(claim.value == "8" for claim in data.claims)
    assert "tickets.csv" in data.evidence


def test_a_requeried_task_shows_both_attempts(settings, tmp_path):
    platform = support_platform(settings, tmp_path)
    run_id, events = completed_run(platform)
    tasks = {task.task_id: task for task in build_run_view(run_id, events).tasks}

    assert tasks["t3_last_summary"].attempts == 2
    assert tasks["t1_deadline_rule"].attempts == 1


def test_a_disagreement_keeps_both_values_and_how_it_ended(settings, tmp_path):
    platform = support_platform(settings, tmp_path)
    run_id, events = completed_run(platform)
    view = build_run_view(run_id, events)
    conflict = next(c for c in view.conflicts if c.metric == "count")

    assert conflict.subject == "overdue_urgent_problems"
    assert conflict.state == "resolved"
    assert conflict.values == {"t2_live_count": "8", "t3_last_summary": "4"}
    assert conflict.requeried == "t3_last_summary"
    assert "snapshot" in conflict.resolution


def test_events_are_grouped_by_the_task_they_belong_to(settings, tmp_path):
    platform = support_platform(settings, tmp_path)
    run_id, events = completed_run(platform)
    grouped = build_run_view(run_id, events).events_by_task

    assert set(grouped) == {
        "t1_deadline_rule",
        "t2_live_count",
        "t3_last_summary",
        "t4_review",
    }
    assert all(group for group in grouped.values())


def test_milestones_skip_the_routine_chatter(settings, tmp_path):
    platform = support_platform(settings, tmp_path)
    run_id, events = completed_run(platform)
    view = build_run_view(run_id, events)
    kinds = {milestone.kind for milestone in view.milestones}

    assert "plan" in kinds and "conflict_resolved" in kinds
    assert "tool_call" not in kinds, "a tool call is not a milestone"
    assert len(view.milestones) < len(events)


def test_a_degraded_task_is_visible_in_the_graph(settings, tmp_path):
    """The audit scenario loses a tool, and the graph should show it."""
    platform = Platform(settings, database_url=f"sqlite:///{tmp_path / 'audit.db'}")
    platform.provision_tenant(
        TenantPolicy(tenant_id="acme", max_concurrent_runs=4, reviewers=("cfo",)),
        from_domain=False,
    )
    run_id, events = completed_run(platform, scenario="audit")
    tasks = {task.task_id: task for task in build_run_view(run_id, events).tasks}

    assert tasks["t5_peer_benchmark"].state is TaskState.DEGRADED
    assert tasks["t5_peer_benchmark"].attempts == 3
    assert "unreachable" in tasks["t5_peer_benchmark"].error


def test_an_empty_trace_does_not_crash():
    view = build_run_view("run-none", [])

    assert view.tasks == []
    assert view.conflicts == []
    assert view.goal == ""


def test_counts_summarise_the_run(settings, tmp_path):
    platform = support_platform(settings, tmp_path)
    run_id, events = completed_run(platform)
    view = build_run_view(run_id, events)

    assert view.tool_calls >= 4
    assert view.model_calls >= 9
    assert view.plan_revisions == 1

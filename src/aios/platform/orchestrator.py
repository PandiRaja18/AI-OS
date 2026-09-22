"""Runs the v1 graph inside the platform.

This is the adapter, and it is deliberately the only place that knows about both
worlds. The graph, the coordinator, the planner and the agents are untouched:
what changes is where their trace goes, whose facts they recall, which tools they
are granted, and who pays for the model calls.

One `advance` carries a run as far as it can go - to a terminal state, or to the
sign-off gate, where it opens a review and hands the worker back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from langgraph.types import Command

from aios.agents import build_agents
from aios.config import Settings
from aios.llm import LlmClient
from aios.mcp_gateway import McpGateway
from aios.mcp_gateway.tools import build_tools, report_markdown
from aios.memory import checkpointer
from aios.observability import EventKind, TraceStore
from aios.orchestration.coordinator import Coordinator, RetryPolicy
from aios.orchestration.graph import Deps, build_graph
from aios.orchestration.planner import Planner
from aios.orchestration.state import (
    Claim,
    ConflictStatus,
    RunStatus as GraphStatus,
    Signoff,
    TaskStatus,
    new_run_state,
)
from aios.platform.budget import BudgetExceeded, MeteredLlm, RunBudgetMeter
from aios.platform.models import (
    AttemptOutcome,
    ReviewRequest,
    RunRecord,
    RunStatus,
    TaskAttempt,
    TenantPolicy,
    now,
)
from aios.platform.objects import FileObjectStore
from aios.platform.sql.facts import SqlFactStore
from aios.platform.sql.policy import SqlPolicyStore
from aios.platform.sql.reviews import SqlReviewInbox
from aios.platform.sql.runs import SqlRunStore
from aios.platform.sql.trace import SqlTraceSink
from aios.platform.tenancy import apply_grants
from aios.providers import build_llm, model_label

RECURSION_LIMIT = 80

STATUS_MAP: dict[GraphStatus, RunStatus] = {
    GraphStatus.PLANNING: RunStatus.PLANNING,
    GraphStatus.RUNNING: RunStatus.RUNNING,
    GraphStatus.AWAITING_SIGNOFF: RunStatus.AWAITING_SIGNOFF,
    GraphStatus.COMPLETED: RunStatus.COMPLETED,
    GraphStatus.FAILED: RunStatus.FAILED,
}


@dataclass
class AdvanceResult:
    """Where one pass of the graph left the run."""

    status: RunStatus
    review: ReviewRequest | None = None
    report_uri: str | None = None
    failure_reason: str | None = None


class TenantMemory:
    """Binds a tenant to the fact store so the graph can stay tenant-unaware."""

    def __init__(self, facts: SqlFactStore, tenant_id: str) -> None:
        self._facts = facts
        self._tenant_id = tenant_id

    def recall(self, query: str, limit: int = 5) -> list:
        return self._facts.recall(self._tenant_id, query, limit)

    def promote(
        self,
        claims: list[Claim],
        *,
        run_id: str,
        confidence: float,
        human_approved: bool,
    ) -> list:
        return self._facts.promote(
            self._tenant_id, claims, run_id, confidence, human_approved
        )


class Orchestrator:
    """Carries one run forward using the platform's stores."""

    def __init__(
        self,
        settings: Settings,
        policy: SqlPolicyStore,
        runs: SqlRunStore,
        reviews: SqlReviewInbox,
        facts: SqlFactStore,
        trace_sink: SqlTraceSink,
        objects: FileObjectStore,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._runs = runs
        self._reviews = reviews
        self._facts = facts
        self._trace_sink = trace_sink
        self._objects = objects

    def advance(self, record: RunRecord) -> AdvanceResult:
        """Run the graph until it finishes, needs a human, or cannot continue."""
        if record.past_deadline():
            return AdvanceResult(RunStatus.FAILED, failure_reason="run deadline passed")

        tenant = self._policy.tenant(record.tenant_id) or TenantPolicy(
            tenant_id=record.tenant_id
        )
        sink = self._trace_sink.for_tenant(record.tenant_id)
        trace = TraceStore(
            record.run_id,
            trace_dir=None,
            listener=sink.emit,
            start_seq=sink.max_seq(record.run_id),
        )
        deps = self._build_deps(record, trace)

        config = {
            "configurable": {"thread_id": record.run_id},
            "recursion_limit": RECURSION_LIMIT,
        }
        with checkpointer(self._settings.checkpoint_db) as saver:
            graph = build_graph(deps).compile(checkpointer=saver)
            snapshot = graph.get_state(config)
            payload = self._payload(record, snapshot, deps, trace)
            if payload is _BLOCKED:
                return AdvanceResult(
                    RunStatus.AWAITING_SIGNOFF,
                    review=self._reviews.for_run(record.run_id),
                )
            try:
                state = graph.invoke(payload, config)
            except BudgetExceeded as exceeded:
                trace.emit(
                    EventKind.RUN_STATUS, f"halted on budget: {exceeded}"
                )
                return AdvanceResult(
                    RunStatus.FAILED, failure_reason=f"budget exceeded: {exceeded}"
                )

        if state.get("__interrupt__"):
            review = self._open_review(record, tenant, state)
            return AdvanceResult(RunStatus.AWAITING_SIGNOFF, review=review)

        graph_status = GraphStatus(state.get("status", GraphStatus.RUNNING))
        return AdvanceResult(
            status=STATUS_MAP.get(graph_status, RunStatus.FAILED),
            report_uri=state.get("report_path"),
            failure_reason=state.get("replan_reason")
            if graph_status is GraphStatus.FAILED
            else None,
        )

    def _build_deps(self, record: RunRecord, trace: TraceStore) -> Deps:
        settings = self._settings
        meter = RunBudgetMeter(self._runs, record.run_id, record.budget)
        model = model_label(settings, record.offline)
        llm = MeteredLlm(self._base_llm(record, trace), meter, model)

        def write_report(title: str, sections) -> str:
            return self._objects.put(
                record.tenant_id,
                record.run_id,
                "report.md",
                report_markdown(title, list(sections)),
            )

        tools = apply_grants(
            build_tools(settings, record.run_id, report_writer=write_report),
            self._policy.grants(record.tenant_id),
        )
        gateway = McpGateway(tools, trace, settings.tool_timeout_seconds)
        coordinator = Coordinator(
            retry=RetryPolicy(
                max_attempts=record.budget.max_task_attempts,
                backoff_seconds=settings.retry_backoff_seconds,
            ),
            max_plan_revisions=record.budget.max_plan_revisions,
        )
        return Deps(
            settings=settings,
            trace=trace,
            llm=llm,
            planner=Planner(llm, trace, settings.max_plan_attempts),
            coordinator=coordinator,
            agents=build_agents(llm, gateway, trace),
            gateway=gateway,
            memory=TenantMemory(self._facts, record.tenant_id),
            on_attempt=self._attempt_recorder(record),
        )

    def _base_llm(self, record: RunRecord, trace: TraceStore) -> LlmClient:
        return build_llm(
            self._settings, trace, record.offline, record.scenario
        )

    def _attempt_recorder(self, record: RunRecord):
        def on_attempt(
            *,
            task_id: str,
            attempt: int,
            agent: str,
            outcome: str,
            error: str | None,
            confidence: float | None,
            duration_ms: int,
        ) -> None:
            self._runs.append_attempt(
                TaskAttempt(
                    run_id=record.run_id,
                    tenant_id=record.tenant_id,
                    task_id=task_id,
                    attempt=attempt,
                    agent=agent,
                    outcome=AttemptOutcome(outcome),
                    error=error,
                    confidence=confidence,
                    duration_ms=duration_ms,
                )
            )

        return on_attempt

    def _payload(
        self, record: RunRecord, snapshot, deps: Deps, trace: TraceStore
    ) -> Any:
        """Decide how to enter the graph: fresh, resumed, or continued."""
        if not snapshot.values:
            precedents = [
                fact.as_context()
                for fact in deps.memory.recall(record.goal)
            ]
            trace.emit(
                EventKind.RUN_STARTED,
                f"goal: {record.goal}",
                tenant=record.tenant_id,
                llm=deps.llm.name,
            )
            trace.emit(
                EventKind.MEMORY_RECALL,
                f"{len(precedents)} durable fact(s) recalled",
                facts=precedents,
            )
            return new_run_state(record.run_id, record.goal, precedents)

        if snapshot.interrupts:
            review = self._reviews.for_run(record.run_id)
            if review is None or review.decision is None:
                return _BLOCKED
            return Command(resume=review.decision.model_dump())

        # Mid-run handover after a lease lapsed: continue from the checkpoint.
        return None

    def _open_review(
        self, record: RunRecord, tenant: TenantPolicy, state
    ) -> ReviewRequest:
        """Turn the graph's pause into a row a reviewer can act on."""
        interrupt = state["__interrupt__"][0].value
        tasks = state.get("tasks", {})
        request = ReviewRequest(
            run_id=record.run_id,
            tenant_id=record.tenant_id,
            goal=record.goal,
            task_ids=tuple(interrupt.get("tasks", ())),
            draft="\n\n".join(interrupt.get("draft", ())),
            report_uri=interrupt.get("report_path"),
            open_conflicts=sum(
                1
                for conflict in state.get("conflicts", {}).values()
                if conflict.status is not ConflictStatus.RESOLVED
            ),
            degraded_tasks=sum(
                1
                for task in tasks.values()
                if task.status is TaskStatus.DEGRADED
            ),
            assigned_to=tenant.reviewers,
            expires_at=now() + timedelta(hours=tenant.review_ttl_hours),
        )
        existing = self._reviews.for_run(record.run_id)
        if existing is not None:
            return existing
        self._reviews.open(request)
        return request


class _Blocked:
    """Sentinel: the run is parked at the gate and has no decision yet."""


_BLOCKED = _Blocked()


def signoff_from(review: ReviewRequest) -> Signoff | None:
    return review.decision

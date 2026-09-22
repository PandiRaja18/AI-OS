"""Runtime: assembles the platform and drives one run at a time.

`start` plans and executes until the graph either finishes or pauses at the
sign-off gate; `resume` supplies the human decision and lets it finish. Both go
through the same checkpointer, so a run can be resumed in a different process.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.types import Command

from aios.agents import build_agents
from aios.config import Settings
from aios.llm import LlmClient
from aios.mcp_gateway import McpGateway
from aios.mcp_gateway.tools import build_tools
from aios.memory import PromotionPolicy, RunIndex, SemanticMemory, checkpointer
from aios.observability import EventKind, Listener, TraceStore
from aios.orchestration.coordinator import Coordinator, RetryPolicy
from aios.orchestration.graph import Deps, build_graph
from aios.orchestration.planner import Planner
from aios.orchestration.state import RunStatus, Signoff, new_run_state
from aios.providers import build_llm

RECURSION_LIMIT = 80


@dataclass
class RunOutcome:
    """The result of starting or resuming a run."""

    run_id: str
    status: RunStatus
    state: Mapping[str, Any]
    pending_signoff: dict[str, Any] | None
    trace_path: Path

    @property
    def paused(self) -> bool:
        return self.pending_signoff is not None


class Runtime:
    """Entry point for executing goals on the platform."""

    def __init__(
        self,
        settings: Settings,
        *,
        offline: bool = False,
        scenario: str = "audit",
        listener: Listener | None = None,
    ) -> None:
        settings.ensure_dirs()
        self.settings = settings
        self._offline = offline
        self._scenario = scenario
        self._listener = listener
        self.index = RunIndex(settings.index_db)
        self.memory = SemanticMemory(
            settings.semantic_db,
            PromotionPolicy(min_confidence=settings.promotion_confidence),
        )

    def start(self, goal: str, run_id: str | None = None) -> RunOutcome:
        """Plan and execute a goal until it completes or needs a human."""
        run_id = run_id or _new_run_id()
        deps, trace = self._build(run_id)
        self.index.start(run_id, goal)

        precedents = [fact.as_context() for fact in self.memory.recall(goal)]
        trace.emit(
            EventKind.RUN_STARTED,
            f"goal: {goal}",
            run_id=run_id,
            llm=deps.llm.name,
        )
        trace.emit(
            EventKind.MEMORY_RECALL,
            f"{len(precedents)} durable fact(s) recalled",
            facts=precedents,
        )
        state = new_run_state(run_id, goal, precedents)
        return self._invoke(run_id, deps, trace, state)

    def resume(self, run_id: str, signoff: Signoff) -> RunOutcome:
        """Deliver a human decision to a paused run and let it finish."""
        deps, trace = self._build(run_id)
        return self._invoke(
            run_id, deps, trace, Command(resume=signoff.model_dump())
        )

    def _build(self, run_id: str) -> tuple[Deps, TraceStore]:
        settings = self.settings
        trace = TraceStore(run_id, settings.trace_dir, self._listener)
        gateway = McpGateway(
            build_tools(settings, run_id), trace, settings.tool_timeout_seconds
        )
        llm = self._llm(trace)
        coordinator = Coordinator(
            retry=RetryPolicy(
                max_attempts=settings.max_task_attempts,
                backoff_seconds=settings.retry_backoff_seconds,
            )
        )
        deps = Deps(
            settings=settings,
            trace=trace,
            llm=llm,
            planner=Planner(llm, trace, settings.max_plan_attempts),
            coordinator=coordinator,
            agents=build_agents(llm, gateway, trace),
            gateway=gateway,
            memory=self.memory,
        )
        return deps, trace

    def _llm(self, trace: TraceStore) -> LlmClient:
        return build_llm(self.settings, trace, self._offline, self._scenario)

    def _invoke(
        self,
        run_id: str,
        deps: Deps,
        trace: TraceStore,
        payload: Any,
    ) -> RunOutcome:
        config = {
            "configurable": {"thread_id": run_id},
            "recursion_limit": RECURSION_LIMIT,
        }
        with checkpointer(self.settings.checkpoint_db) as saver:
            graph = build_graph(deps).compile(checkpointer=saver)
            state = graph.invoke(payload, config)

        interrupts = state.get("__interrupt__") or ()
        pending = dict(interrupts[0].value) if interrupts else None
        status = (
            RunStatus.AWAITING_SIGNOFF
            if pending
            else RunStatus(state.get("status", RunStatus.RUNNING))
        )
        self.index.update(run_id, status.value, state.get("report_path"))
        return RunOutcome(
            run_id=run_id,
            status=status,
            state=state,
            pending_signoff=pending,
            trace_path=trace.path,
        )


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:4]}"

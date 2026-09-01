"""Gateway authorization, timeouts and tracing."""

from __future__ import annotations

import time

import pytest

from aios.config import Settings
from aios.mcp_gateway import (
    McpGateway,
    Principal,
    PrincipalKind,
    ToolAccessDenied,
    ToolError,
    ToolSpec,
    ToolTimeout,
)
from aios.mcp_gateway.tools import build_tools
from aios.observability import EventKind, TraceStore
from aios.orchestration.state import AgentType

DATA = Principal.for_agent(AgentType.DATA)
RESEARCH = Principal.for_agent(AgentType.RESEARCH)
REVIEWER = Principal.human("reviewer")


def test_an_agent_may_only_call_its_granted_tools(
    settings: Settings, trace: TraceStore
):
    gateway = McpGateway(build_tools(settings, "run-test"), trace)

    assert gateway.tools_for(DATA) == ["ledger_lookup", "sql_query"]
    assert "peer_benchmark" in gateway.tools_for(RESEARCH)

    with pytest.raises(ToolAccessDenied):
        gateway.call(RESEARCH, "sql_query", query="quarter_totals", quarter="FY26-Q3")

    assert trace.events[-1].kind is EventKind.ACCESS_DENIED


def test_human_only_tools_reject_agent_principals(
    settings: Settings, trace: TraceStore
):
    gateway = McpGateway(build_tools(settings, "run-test"), trace)

    assert gateway.tools_for(REVIEWER) == ["record_signoff"]
    with pytest.raises(ToolAccessDenied):
        gateway.call(DATA, "record_signoff", reviewer="data", approved=True)

    entry = gateway.call(REVIEWER, "record_signoff", reviewer="reviewer", approved=True)
    assert entry["approved"] is True


def test_an_unknown_tool_is_an_error(settings: Settings, trace: TraceStore):
    gateway = McpGateway(build_tools(settings, "run-test"), trace)
    with pytest.raises(ToolError, match="unknown tool"):
        gateway.call(DATA, "rm_rf")


def test_a_named_query_returns_rows(settings: Settings, trace: TraceStore):
    gateway = McpGateway(build_tools(settings, "run-test"), trace)
    rows = gateway.call(
        DATA,
        "sql_query",
        query="unapproved_above_threshold",
        quarter="FY26-Q3",
        threshold=25000,
    )
    assert {row["txn_id"] for row in rows} == {"T-3001", "T-3002", "T-3005"}
    assert trace.events[-1].kind is EventKind.TOOL_CALL
    assert trace.events[-1].duration_ms is not None


def test_an_unregistered_query_name_is_refused(
    settings: Settings, trace: TraceStore
):
    gateway = McpGateway(build_tools(settings, "run-test"), trace)
    with pytest.raises(ToolError, match="unknown query"):
        gateway.call(DATA, "sql_query", query="DROP TABLE vendors")


def test_a_flaky_tool_succeeds_once_its_failures_are_exhausted(
    settings: Settings, trace: TraceStore
):
    gateway = McpGateway(build_tools(settings, "run-test"), trace)
    for _ in range(settings.injected_ledger_failures):
        with pytest.raises(ToolError):
            gateway.call(DATA, "ledger_lookup", vendor="V-101", quarter="FY26-Q3")
    balance = gateway.call(
        DATA, "ledger_lookup", vendor="V-101", quarter="FY26-Q3"
    )
    assert balance["balance_usd"] == 95500.0


def test_an_outage_tool_always_fails(settings: Settings, trace: TraceStore):
    configured = settings.model_copy(update={"outage_tools": ("ledger_lookup",)})
    gateway = McpGateway(build_tools(configured, "run-test"), trace)
    for _ in range(3):
        with pytest.raises(ToolError, match="unavailable"):
            gateway.call(DATA, "ledger_lookup", vendor="V-101", quarter="FY26-Q3")


def test_a_slow_tool_is_cut_off_at_its_timeout(trace: TraceStore):
    slow = ToolSpec(
        name="slow",
        description="sleeps",
        handler=lambda: time.sleep(5),
        allowed_agents=frozenset({AgentType.DATA.value}),
        allowed_kinds=frozenset({PrincipalKind.AGENT}),
        timeout_seconds=0.05,
    )
    gateway = McpGateway([slow], trace)
    with pytest.raises(ToolTimeout):
        gateway.call(DATA, "slow")
    assert trace.events[-1].kind is EventKind.TOOL_ERROR

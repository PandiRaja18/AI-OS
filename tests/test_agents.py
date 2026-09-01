"""The worker agent contract: plan tools, execute them, return a typed result."""

from __future__ import annotations

from aios.a2a import Envelope, ReplyStatus
from aios.agents.base import _coerce
from aios.agents.data import DataAgent
from aios.agents.research import ResearchAgent
from aios.config import Settings
from aios.mcp_gateway import McpGateway
from aios.mcp_gateway.tools import build_tools
from aios.observability import EventKind, TraceStore
from aios.orchestration.state import AgentType

SCAN_PLAN = {
    "calls": [
        {
            "tool": "sql_query",
            "arguments": [
                {"name": "query", "value": "quarter_totals"},
                {"name": "quarter", "value": "FY26-Q3"},
            ],
            "purpose": "Total the quarter.",
        }
    ]
}
SCAN_RESULT = {
    "summary": "FY26-Q3 booked 174,150 USD across 8 transactions.",
    "claims": [
        {"subject": "FY26-Q3", "metric": "total_usd", "value": "174150.00"}
    ],
    "confidence": 0.9,
    "evidence": ["sql:quarter_totals"],
}


class StubLlm:
    """Returns a response per call stage, and records what it was asked."""

    name = "stub"

    def __init__(self, **by_stage: dict) -> None:
        self._by_stage = by_stage
        self.keys: list[str] = []

    def structured(self, *, key, system, prompt, output_model, **_):
        self.keys.append(key)
        self.prompts = getattr(self, "prompts", [])
        self.prompts.append(prompt)
        stage = key.split(".")[-1].split("@")[0]
        return output_model.model_validate(self._by_stage[stage])


def envelope(task_id: str, agent: AgentType, attempt: int = 1) -> Envelope:
    return Envelope(
        run_id="run-test",
        task_id=task_id,
        recipient=agent,
        goal="Prepare the FY26-Q3 quarterly audit review",
        instruction="Total the quarter",
        success_criteria="a total is reported",
        attempt=attempt,
        context={"durable_memory": "prior flag on Northwind Logistics"},
    )


def gateway_for(settings: Settings, trace: TraceStore) -> McpGateway:
    return McpGateway(build_tools(settings, "run-test"), trace)


def test_an_agent_plans_tools_then_returns_a_typed_result(
    settings: Settings, trace: TraceStore
):
    llm = StubLlm(plan=SCAN_PLAN, result=SCAN_RESULT)
    agent = DataAgent(llm, gateway_for(settings, trace), trace)

    reply = agent.handle(envelope("t2", AgentType.DATA))

    assert reply.status is ReplyStatus.OK
    assert reply.result.claims[0].value == "174150.00"
    assert llm.keys == ["data.t2.plan", "data.t2.result"]
    assert trace.events[-1].kind is EventKind.AGENT_RESULT


def test_the_prompt_carries_the_tool_catalogue_and_context(
    settings: Settings, trace: TraceStore
):
    llm = StubLlm(plan=SCAN_PLAN, result=SCAN_RESULT)
    DataAgent(llm, gateway_for(settings, trace), trace).handle(
        envelope("t2", AgentType.DATA)
    )

    plan_prompt = llm.prompts[0]
    assert "sql_query" in plan_prompt
    assert "peer_benchmark" not in plan_prompt
    assert "prior flag on Northwind Logistics" in plan_prompt


def test_a_retry_is_keyed_separately_so_it_can_be_answered_differently(
    settings: Settings, trace: TraceStore
):
    llm = StubLlm(plan=SCAN_PLAN, result=SCAN_RESULT)
    DataAgent(llm, gateway_for(settings, trace), trace).handle(
        envelope("t2", AgentType.DATA, attempt=2)
    )
    assert llm.keys == ["data.t2.plan@2", "data.t2.result@2"]


def test_a_tool_failure_is_reported_not_raised(
    settings: Settings, trace: TraceStore
):
    plan = {
        "calls": [
            {
                "tool": "peer_benchmark",
                "arguments": [{"name": "category", "value": "freight"}],
                "purpose": "Benchmark.",
            }
        ]
    }
    llm = StubLlm(plan=plan, result=SCAN_RESULT)
    agent = ResearchAgent(llm, gateway_for(settings, trace), trace)

    reply = agent.handle(envelope("t5", AgentType.RESEARCH))

    assert reply.status is ReplyStatus.FAILED
    assert "unreachable" in reply.error
    assert reply.result is None


def test_calling_a_tool_outside_the_grant_fails_the_task(
    settings: Settings, trace: TraceStore
):
    plan = {
        "calls": [
            {
                "tool": "sql_query",
                "arguments": [{"name": "query", "value": "quarter_totals"}],
                "purpose": "Overstep.",
            }
        ]
    }
    llm = StubLlm(plan=plan, result=SCAN_RESULT)
    agent = ResearchAgent(llm, gateway_for(settings, trace), trace)

    reply = agent.handle(envelope("t1", AgentType.RESEARCH))

    assert reply.status is ReplyStatus.FAILED
    assert "may not call sql_query" in reply.error
    assert any(event.kind is EventKind.ACCESS_DENIED for event in trace.events)


def test_text_arguments_are_coerced_to_tool_scalars():
    assert _coerce("25000") == 25000
    assert _coerce("58.3") == 58.3
    assert _coerce("true") is True
    assert _coerce("none") is None
    assert _coerce("FY26-Q3") == "FY26-Q3"

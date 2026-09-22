"""The self-hosted model client: schema handling, repair loop, failure modes."""

from __future__ import annotations

import json

import httpx
import pytest

from aios.agents.base import ToolPlan
from aios.llm import LlmError
from aios.llm_local import ApiStyle, LocalLlmClient, flatten_schema
from aios.observability import EventKind, TraceStore
from aios.orchestration.planner import Plan
from aios.orchestration.state import AgentResult

GOOD_RESULT = {
    "summary": "Three exceptions totalling 86,900 USD.",
    "claims": [{"subject": "Acme", "metric": "exposure_usd", "value": "55700.00"}],
    "confidence": 0.9,
    "evidence": ["sql:exceptions"],
}


def client(trace: TraceStore, handler, style=ApiStyle.OPENAI, **kwargs) -> LocalLlmClient:
    return LocalLlmClient(
        base_url="http://localhost:9999",
        model="qwen2.5:32b",
        trace=trace,
        api_style=style,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def openai_reply(content: dict | str, status: int = 200) -> httpx.Response:
    body = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(
        status,
        json={
            "choices": [{"message": {"content": body}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40},
        },
    )


def ollama_reply(content: dict | str) -> httpx.Response:
    body = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(
        200,
        json={
            "message": {"content": body},
            "prompt_eval_count": 120,
            "eval_count": 40,
        },
    )


# --- schema preparation -------------------------------------------------------


def test_nested_models_are_inlined_because_backends_mishandle_refs():
    flat = json.dumps(flatten_schema(AgentResult.model_json_schema()))
    assert "$ref" not in flat and "$defs" not in flat
    assert "subject" in flat, "the nested Claim fields must survive inlining"


def test_objects_are_closed_so_the_decoder_cannot_invent_fields():
    flat = flatten_schema(ToolPlan.model_json_schema())
    assert flat["additionalProperties"] is False
    assert set(flat["required"]) == {"calls"}


def test_every_model_the_platform_asks_for_flattens():
    for model in (AgentResult, ToolPlan, Plan):
        flat = json.dumps(flatten_schema(model.model_json_schema()))
        assert "$ref" not in flat, model.__name__


def test_an_unresolvable_reference_is_reported_not_silently_dropped():
    with pytest.raises(LlmError, match="unresolvable"):
        flatten_schema({"properties": {"x": {"$ref": "#/$defs/Missing"}}})


# --- the two API styles -------------------------------------------------------


def test_openai_style_sends_the_schema_and_parses_the_answer(trace: TraceStore):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["url"] = str(request.url)
        return openai_reply(GOOD_RESULT)

    result = client(trace, handler).structured(
        key="data.t1.result", system="s", prompt="p", output_model=AgentResult
    )

    assert result.claims[0].value == "55700.00"
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["response_format"]["json_schema"]["strict"] is True
    assert seen["temperature"] == 0.0
    assert "$defs" not in json.dumps(seen["response_format"])


def test_ollama_style_uses_the_format_parameter(trace: TraceStore):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["url"] = str(request.url)
        return ollama_reply(GOOD_RESULT)

    result = client(trace, handler, style=ApiStyle.OLLAMA).structured(
        key="data.t1.result", system="s", prompt="p", output_model=AgentResult
    )

    assert result.confidence == 0.9
    assert seen["url"].endswith("/api/chat")
    assert seen["format"]["type"] == "object"
    assert seen["stream"] is False


def test_usage_is_reported_so_budgets_still_bind(trace: TraceStore):
    local = client(trace, lambda request: openai_reply(GOOD_RESULT))
    local.structured(key="k", system="s", prompt="p", output_model=AgentResult)

    assert local.last_usage.input_tokens == 120
    assert local.last_usage.output_tokens == 40


# --- the repair loop ----------------------------------------------------------


def test_a_malformed_answer_is_repaired_rather_than_returned(trace: TraceStore):
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["messages"][-1]["content"])
        if len(prompts) == 1:
            return openai_reply({"summary": "no confidence field"})
        return openai_reply(GOOD_RESULT)

    result = client(trace, handler).structured(
        key="data.t1.result", system="s", prompt="p", output_model=AgentResult
    )

    assert result.confidence == 0.9
    assert len(prompts) == 2
    assert "did not match the required schema" in prompts[1]
    assert "confidence" in prompts[1], "the repair prompt names the failing field"
    assert any(
        "repaired x1" in event.message
        for event in trace.events
        if event.kind is EventKind.LLM_CALL
    )


def test_repairs_are_bounded_and_fail_loudly(trace: TraceStore):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return openai_reply({"nonsense": True})

    with pytest.raises(LlmError, match="could not produce valid AgentResult"):
        client(trace, handler, max_repairs=2).structured(
            key="k", system="s", prompt="p", output_model=AgentResult
        )

    assert len(calls) == 3, "one attempt plus two repairs"


def test_non_json_output_is_treated_as_a_repairable_failure(trace: TraceStore):
    replies = iter(["Sure! Here is the JSON you asked for.", json.dumps(GOOD_RESULT)])

    result = client(
        trace, lambda request: openai_reply(next(replies))
    ).structured(key="k", system="s", prompt="p", output_model=AgentResult)

    assert result.summary.startswith("Three exceptions")


# --- failure modes ------------------------------------------------------------


def test_a_server_error_names_the_model_and_the_body(trace: TraceStore):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="guided decoding backend unavailable")

    with pytest.raises(LlmError, match="guided decoding backend unavailable"):
        client(trace, handler).structured(
            key="k", system="s", prompt="p", output_model=AgentResult
        )


def test_an_unreachable_server_says_so(trace: TraceStore):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LlmError, match="cannot reach"):
        client(trace, handler).structured(
            key="k", system="s", prompt="p", output_model=AgentResult
        )


def test_an_empty_choice_list_is_an_error_not_a_crash(trace: TraceStore):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [], "usage": {}})

    with pytest.raises(LlmError, match="no choices"):
        client(trace, handler).structured(
            key="k", system="s", prompt="p", output_model=AgentResult
        )


def test_probe_reports_the_served_model(trace: TraceStore):
    def handler(request: httpx.Request) -> httpx.Response:
        return openai_reply({"ok": True, "model_name": "qwen2.5:32b-instruct"})

    assert client(trace, handler).probe() == "qwen2.5:32b-instruct"

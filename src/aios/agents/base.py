"""Worker agent base class.

Every worker follows the same two-phase contract. First it asks the model which
of *its own* permitted tools to call and with what arguments; the gateway
executes those calls and rejects anything outside the agent's grant. Then it asks
the model to turn the collected evidence into an `AgentResult` - a typed object
with claims, confidence and evidence, never free prose.

A tool failure is not handled here. The agent reports it and the coordinator
decides whether to retry, degrade or replan.
"""

from __future__ import annotations

import json
from abc import ABC
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from aios.a2a import Envelope, Reply
from aios.llm import LlmClient, LlmError, call_key
from aios.mcp_gateway import McpGateway, Principal, ToolAccessDenied, ToolError
from aios.observability import EventKind, TraceStore
from aios.orchestration.state import AgentResult, AgentType

MAX_TOOL_CALLS = 4


class ToolArgument(BaseModel):
    """One argument, carried as text and coerced on the way to the tool."""

    name: str
    value: str


class ToolCall(BaseModel):
    tool: str
    arguments: list[ToolArgument] = Field(default_factory=list)
    purpose: str


class ToolPlan(BaseModel):
    """The tool calls an agent intends to make for one task."""

    calls: list[ToolCall]


PLAN_SYSTEM_SUFFIX = """
You are choosing tool calls for one task. Rules:
- Use only the tools listed. Calling anything else is refused by the gateway.
- At most {max_calls} calls. Pass every argument the tool description names.
- Argument values are written as text; numbers as plain digits (e.g. 25000).
"""

RESULT_SYSTEM_SUFFIX = """
You are turning collected evidence into a typed result. Rules:
- summary: two or three sentences, specific and quantified.
- claims: every figure or determination another agent might contradict, as
  subject / metric / value triples. Use snake_case metrics and plain values
  (e.g. subject "Northwind Logistics", metric "unapproved_exposure_usd",
  value "55700.00"). Omit claims you cannot support from the evidence.
- confidence: your calibrated confidence in the claims, 0 to 1.
- evidence: the document names, query names or ids the claims came from.
"""


class Agent(ABC):
    """Shared execution contract for worker agents."""

    agent_type: ClassVar[AgentType]
    role_prompt: ClassVar[str]

    def __init__(
        self, llm: LlmClient, gateway: McpGateway, trace: TraceStore
    ) -> None:
        self._llm = llm
        self._gateway = gateway
        self._trace = trace
        self.principal = Principal.for_agent(self.agent_type)

    @property
    def name(self) -> str:
        return self.agent_type.value

    def handle(self, envelope: Envelope) -> Reply:
        """Execute one dispatched task and reply to the coordinator."""
        try:
            evidence = self._gather(envelope)
            result = self._interpret(envelope, evidence)
        except (ToolError, ToolAccessDenied, LlmError) as error:
            return Reply.failed(envelope, str(error))
        self._trace.emit(
            EventKind.AGENT_RESULT,
            f"{result.summary} (confidence {result.confidence:.2f})",
            actor=self.name,
            task_id=envelope.task_id,
            claims=[claim.model_dump() for claim in result.claims],
            evidence=result.evidence,
        )
        return Reply.ok(envelope, result)

    def _gather(self, envelope: Envelope) -> list[dict[str, Any]]:
        """Plan tool calls with the model, then execute them via the gateway."""
        plan = self._llm.structured(
            key=call_key(self.name, "plan", envelope.task_id, envelope.attempt),
            system=self._system(PLAN_SYSTEM_SUFFIX.format(max_calls=MAX_TOOL_CALLS)),
            prompt=self._plan_prompt(envelope),
            output_model=ToolPlan,
            task_id=envelope.task_id,
            actor=self.name,
        )
        evidence: list[dict[str, Any]] = []
        for call in plan.calls[:MAX_TOOL_CALLS]:
            arguments = {
                argument.name: _coerce(argument.value) for argument in call.arguments
            }
            output = self._gateway.call(
                self.principal, call.tool, task_id=envelope.task_id, **arguments
            )
            evidence.append(
                {"tool": call.tool, "purpose": call.purpose, "output": output}
            )
        return evidence

    def _interpret(
        self, envelope: Envelope, evidence: list[dict[str, Any]]
    ) -> AgentResult:
        """Turn gathered evidence into the typed result contract."""
        return self._llm.structured(
            key=call_key(self.name, "result", envelope.task_id, envelope.attempt),
            system=self._system(RESULT_SYSTEM_SUFFIX),
            prompt=self._result_prompt(envelope, evidence),
            output_model=AgentResult,
            task_id=envelope.task_id,
            actor=self.name,
        )

    def _system(self, suffix: str) -> str:
        return f"{self.role_prompt.strip()}\n{suffix.strip()}"

    def _plan_prompt(self, envelope: Envelope) -> str:
        return "\n".join(
            [
                f"Goal: {envelope.goal}",
                f"Task: {envelope.instruction}",
                f"Done when: {envelope.success_criteria}",
                f"Attempt: {envelope.attempt}",
                "",
                "Tools you may call:",
                self._gateway.describe(self.principal),
                "",
                _render_context(envelope),
            ]
        )

    def _result_prompt(
        self, envelope: Envelope, evidence: list[dict[str, Any]]
    ) -> str:
        return "\n".join(
            [
                f"Goal: {envelope.goal}",
                f"Task: {envelope.instruction}",
                f"Done when: {envelope.success_criteria}",
                "",
                _render_context(envelope),
                "",
                "Tool output:",
                json.dumps(evidence, indent=2, default=str),
            ]
        )


def _render_context(envelope: Envelope) -> str:
    if not envelope.context:
        return "Context: none."
    return "Context:\n" + "\n".join(
        f"[{key}]\n{value}" for key, value in envelope.context.items()
    )


def _coerce(value: str) -> Any:
    """Turn a text argument into the scalar a tool expects."""
    text = value.strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("none", "null"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text

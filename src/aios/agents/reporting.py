"""Reporting agent: synthesis into a reviewable document.

This agent replaces the generic interpretation step with a report draft, renders
it through the gateway, and returns the rendered path as its evidence.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from aios.a2a import Envelope
from aios.agents.base import Agent
from aios.llm import call_key
from aios.orchestration.state import AgentResult, AgentType, Claim

ROLE_PROMPT = """You are the Reporting agent of an audit platform.

You synthesize other agents' typed results into a document a reviewer can sign
off. You never introduce a figure that no upstream result supports. Anything
that failed, was contradicted, or landed below the confidence bar is stated
plainly in its own section rather than smoothed over.
"""

DRAFT_SYSTEM_SUFFIX = """
Produce the report draft. Rules:
- Open with an executive summary section, then findings, then a section titled
  "Control gaps and open items" listing degraded tasks, escalated conflicts and
  anything needing human judgement. Write "None." if there are none.
- Attribute every figure to the task or document it came from.
- key_claims: the figures a reviewer would sign off on, as subject / metric /
  value triples matching the upstream claims.
- confidence: your confidence that the report is fit for review, 0 to 1.
"""


class ReportSection(BaseModel):
    heading: str
    body: str


class ReportDraft(BaseModel):
    """The reporting agent's structured output, before rendering."""

    title: str
    summary: str
    sections: list[ReportSection]
    key_claims: list[Claim] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    open_questions: list[str] = Field(default_factory=list)


class ReportingAgent(Agent):
    agent_type: ClassVar[AgentType] = AgentType.REPORTING
    role_prompt: ClassVar[str] = ROLE_PROMPT

    def _interpret(
        self, envelope: Envelope, evidence: list[dict[str, Any]]
    ) -> AgentResult:
        draft = self._llm.structured(
            key=call_key(self.name, "draft", envelope.task_id, envelope.attempt),
            system=self._system(DRAFT_SYSTEM_SUFFIX),
            prompt=self._result_prompt(envelope, evidence),
            output_model=ReportDraft,
            task_id=envelope.task_id,
            actor=self.name,
        )
        path = self._gateway.call(
            self.principal,
            "render_report",
            task_id=envelope.task_id,
            title=draft.title,
            sections=[section.model_dump() for section in draft.sections],
        )
        return AgentResult(
            summary=draft.summary,
            claims=draft.key_claims,
            confidence=draft.confidence,
            evidence=[str(path)] + draft.open_questions,
        )

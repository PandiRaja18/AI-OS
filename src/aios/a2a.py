"""Agent-to-agent message envelope.

Agents never call each other directly: the coordinator wraps every dispatch in an
`Envelope` and every reply in a `Reply`. A single message type is what makes
tracing, retries and conflict detection possible.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from aios.orchestration.state import AgentResult, AgentType


class ReplyStatus(enum.StrEnum):
    OK = "ok"
    FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Envelope(BaseModel):
    """A unit of work handed from the coordinator to exactly one agent."""

    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    run_id: str
    task_id: str
    sender: str = "coordinator"
    recipient: AgentType
    goal: str
    instruction: str
    success_criteria: str
    attempt: int = 1
    context: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)


class Reply(BaseModel):
    """An agent's response to an `Envelope`."""

    message_id: str
    run_id: str
    task_id: str
    sender: AgentType
    recipient: str = "coordinator"
    status: ReplyStatus
    result: AgentResult | None = None
    error: str | None = None
    created_at: str = Field(default_factory=_now)

    @classmethod
    def ok(cls, envelope: Envelope, result: AgentResult) -> "Reply":
        return cls(
            message_id=envelope.message_id,
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            sender=envelope.recipient,
            status=ReplyStatus.OK,
            result=result,
        )

    @classmethod
    def failed(cls, envelope: Envelope, error: str) -> "Reply":
        return cls(
            message_id=envelope.message_id,
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            sender=envelope.recipient,
            status=ReplyStatus.FAILED,
            error=error,
        )

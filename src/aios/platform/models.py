"""Records the platform owns, as opposed to the state a graph run carries.

A v1 run belonged to a shell. These records make a run something the system
owns: it has a tenant, a budget, a lease, an append-only execution history and a
review that gates what it is allowed to remember.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, Field

from aios.orchestration.state import Signoff


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Lane(enum.StrEnum):
    """Priority lanes. A backfill must never delay a reviewer's resume."""

    INTERACTIVE = "interactive"
    BATCH = "batch"
    BACKFILL = "backfill"


LANE_PRIORITY: dict[Lane, int] = {
    Lane.INTERACTIVE: 100,
    Lane.BATCH: 50,
    Lane.BACKFILL: 10,
}


class PrincipalKind(enum.StrEnum):
    HUMAN = "human"
    SERVICE = "service"


class Principal(BaseModel):
    """An authenticated caller, resolved from a token at the edge."""

    tenant_id: str
    subject: str
    kind: PrincipalKind
    scopes: tuple[str, ...] = ()

    def may(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.subject}@{self.tenant_id}"


class Budget(BaseModel):
    """Hard ceilings for one run. Exceeding one halts the run, it never overruns."""

    max_input_tokens: int = 2_000_000
    max_output_tokens: int = 200_000
    max_currency: Decimal = Decimal("5.00")
    max_wall_clock_seconds: int = 1_800
    max_plan_revisions: int = 2
    max_task_attempts: int = 3


class Spend(BaseModel):
    """What a run has consumed so far."""

    input_tokens: int = 0
    output_tokens: int = 0
    currency: Decimal = Decimal("0")
    tool_calls: int = 0
    llm_calls: int = 0

    def plus(self, other: "Spend") -> "Spend":
        return Spend(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            currency=self.currency + other.currency,
            tool_calls=self.tool_calls + other.tool_calls,
            llm_calls=self.llm_calls + other.llm_calls,
        )

    def breach(self, budget: Budget) -> str | None:
        """The first ceiling this spend has crossed, if any."""
        if self.input_tokens > budget.max_input_tokens:
            return f"input tokens {self.input_tokens} over {budget.max_input_tokens}"
        if self.output_tokens > budget.max_output_tokens:
            return f"output tokens {self.output_tokens} over {budget.max_output_tokens}"
        if self.currency > budget.max_currency:
            return f"spend {self.currency} over {budget.max_currency}"
        return None


class Lease(BaseModel):
    """A worker's time-bounded claim on a run."""

    worker_id: str
    expires_at: datetime

    def expired(self, at: datetime | None = None) -> bool:
        return self.expires_at <= (at or now())


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_SIGNOFF = "awaiting_signoff"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.EXPIRED,
        RunStatus.CANCELLED,
    }
)


class RunRecord(BaseModel):
    """The platform's record of one run."""

    run_id: str = Field(default_factory=lambda: new_id("run"))
    tenant_id: str
    submitted_by: str
    goal: str
    domain: str = "audit"
    scenario: str = "audit"
    offline: bool = True
    status: RunStatus = RunStatus.QUEUED
    lane: Lane = Lane.INTERACTIVE
    priority: int = LANE_PRIORITY[Lane.INTERACTIVE]
    budget: Budget = Field(default_factory=Budget)
    spend: Spend = Field(default_factory=Spend)
    lease: Lease | None = None
    lease_attempts: int = 0
    report_uri: str | None = None
    failure_reason: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    deadline_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def past_deadline(self, at: datetime | None = None) -> bool:
        return self.deadline_at is not None and self.deadline_at <= (at or now())


class AttemptOutcome(enum.StrEnum):
    OK = "ok"
    TOOL_ERROR = "tool_error"
    LLM_ERROR = "llm_error"
    DENIED = "denied"
    TIMEOUT = "timeout"


class TaskAttempt(BaseModel):
    """One execution of one task. Append-only: the history is the audit trail."""

    attempt_id: str = Field(default_factory=lambda: new_id("att"))
    run_id: str
    tenant_id: str
    task_id: str
    attempt: int
    agent: str
    outcome: AttemptOutcome
    error: str | None = None
    result_uri: str | None = None
    confidence: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    at: datetime = Field(default_factory=now)


class ToolGrant(BaseModel):
    """A tenant's permission for one tool. Checked on every call, not at connect."""

    tenant_id: str
    tool: str
    allowed_agents: tuple[str, ...] = ()
    allowed_kinds: tuple[str, ...] = ("agent",)
    rate_limit_per_minute: int | None = None
    timeout_seconds: float = 20.0
    secret_ref: str | None = None


class ReviewStatus(enum.StrEnum):
    PENDING = "pending"
    DECIDED = "decided"
    EXPIRED = "expired"


class ReviewRequest(BaseModel):
    """The human gate, as a record rather than a terminal prompt."""

    review_id: str = Field(default_factory=lambda: new_id("rev"))
    run_id: str
    tenant_id: str
    goal: str
    task_ids: tuple[str, ...] = ()
    draft: str = ""
    report_uri: str | None = None
    open_conflicts: int = 0
    degraded_tasks: int = 0
    assigned_to: tuple[str, ...] = ()
    status: ReviewStatus = ReviewStatus.PENDING
    decision: Signoff | None = None
    created_at: datetime = Field(default_factory=now)
    expires_at: datetime = Field(default_factory=lambda: now() + timedelta(hours=24))
    decided_at: datetime | None = None

    def expired(self, at: datetime | None = None) -> bool:
        return (
            self.status is ReviewStatus.PENDING
            and self.expires_at <= (at or now())
        )


class TenantPolicy(BaseModel):
    """Per-tenant limits, loaded once per run."""

    tenant_id: str
    name: str = ""
    max_concurrent_runs: int = 4
    default_budget: Budget = Field(default_factory=Budget)
    monthly_currency_cap: Decimal = Decimal("500.00")
    review_ttl_hours: int = 24
    reviewers: tuple[str, ...] = ()

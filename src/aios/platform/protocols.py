"""The seams between the orchestrator and everything it depends on.

Each protocol is a place where the single-process v1 implementation is swapped
for a shared one without the orchestration code noticing. The design document
sketches these as async; they are synchronous here because the graph itself is
synchronous and the database drivers block. The signatures are otherwise the
same, and the API layer runs them in a threadpool.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aios.orchestration.state import Claim, Signoff
from aios.platform.models import (
    Budget,
    Lane,
    Principal,
    ReviewRequest,
    RunRecord,
    RunStatus,
    Spend,
    TaskAttempt,
    TenantPolicy,
    ToolGrant,
)


class RunQueue(Protocol):
    """Durable work distribution with leases."""

    def submit(self, record: RunRecord) -> str: ...

    def lease(
        self, worker_id: str, lanes: tuple[Lane, ...], for_seconds: int
    ) -> RunRecord | None:
        """Claim the highest-priority queued run, or None if there is none."""

    def heartbeat(self, run_id: str, worker_id: str, for_seconds: int) -> bool:
        """Extend a lease. False means the lease was lost and work must stop."""

    def release(self, run_id: str, worker_id: str, status: RunStatus) -> None: ...

    def requeue_expired(self, at: datetime | None = None) -> list[str]:
        """Return runs whose lease lapsed back to the queue."""

    def depth(self, tenant_id: str | None = None) -> int: ...


class RunStore(Protocol):
    """The record of what runs exist and what they cost."""

    def get(self, run_id: str, tenant_id: str | None = None) -> RunRecord | None: ...

    def list(
        self,
        tenant_id: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
    ) -> list[RunRecord]: ...

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        report_uri: str | None = None,
        failure_reason: str | None = None,
    ) -> None: ...

    def add_spend(self, run_id: str, delta: Spend) -> Spend:
        """Apply spend atomically and return the new total."""

    def append_attempt(self, attempt: TaskAttempt) -> None: ...

    def attempts(self, run_id: str) -> list[TaskAttempt]: ...

    def active_run_count(self, tenant_id: str) -> int: ...

    def spend_since(self, tenant_id: str, since: datetime) -> Spend: ...


class FactStore(Protocol):
    """Durable memory, tenant-scoped and versioned."""

    def recall(self, tenant_id: str, query: str, limit: int = 5) -> list: ...

    def promote(
        self,
        tenant_id: str,
        claims: list[Claim],
        run_id: str,
        confidence: float,
        human_approved: bool,
    ) -> list: ...

    def history(self, tenant_id: str, subject: str, metric: str) -> list: ...


class TraceSink(Protocol):
    """Write path is fire-and-forget; read path serves the console and evals."""

    def emit(self, event) -> None: ...

    def query(
        self,
        tenant_id: str | None = None,
        run_id: str | None = None,
        kinds: tuple[str, ...] = (),
        since_seq: int = 0,
        limit: int = 500,
    ) -> list: ...

    def counts_by_kind(self, tenant_id: str | None = None) -> dict[str, int]: ...


class ReviewInbox(Protocol):
    """The human gate as a service."""

    def open(self, request: ReviewRequest) -> str: ...

    def get(self, review_id: str, tenant_id: str | None = None) -> ReviewRequest | None: ...

    def pending(self, tenant_id: str, reviewer: str | None = None) -> list[ReviewRequest]: ...

    def for_run(self, run_id: str) -> ReviewRequest | None: ...

    def decide(self, review_id: str, decision: Signoff, principal: Principal) -> ReviewRequest:
        """Record a human decision. Only human principals may call this."""

    def expire(self, at: datetime | None = None) -> list[ReviewRequest]: ...


class PolicyStore(Protocol):
    """Per-tenant tool grants and budgets."""

    def tenant(self, tenant_id: str) -> TenantPolicy | None: ...

    def upsert_tenant(self, policy: TenantPolicy) -> None: ...

    def grants(self, tenant_id: str) -> list[ToolGrant]: ...

    def upsert_grant(self, grant: ToolGrant) -> None: ...

    def default_budget(self, tenant_id: str) -> Budget: ...


class BudgetMeter(Protocol):
    """Consulted before every model call, updated after every one."""

    def check(self, run_id: str) -> None:
        """Raise BudgetExceeded if the run has crossed a ceiling."""

    def record(self, run_id: str, delta: Spend) -> Spend: ...


class ObjectStore(Protocol):
    """Large payloads live here, not in the checkpoint."""

    def put(self, tenant_id: str, run_id: str, name: str, body: str) -> str: ...

    def get(self, uri: str) -> str: ...

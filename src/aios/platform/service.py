"""The platform facade.

Everything that needs the platform - the API, the workers, the console, the
tests - goes through this one object, so the wiring exists in exactly one place
and the rules (admission, identity, review authority) cannot be bypassed by
using a different entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from aios.config import Settings
from aios.orchestration.state import Signoff
from aios.platform.admission import AdmissionControl, Decision
from aios.platform.budget import Budget
from aios.platform.models import (
    Lane,
    LANE_PRIORITY,
    Principal,
    PrincipalKind,
    ReviewRequest,
    RunRecord,
    RunStatus,
    TenantPolicy,
    now,
)
from aios.platform.objects import FileObjectStore
from aios.platform.orchestrator import Orchestrator
from aios.platform.principal import TokenIssuer
from aios.platform.sql import (
    Database,
    SqlFactStore,
    SqlPolicyStore,
    SqlRunQueue,
    SqlRunStore,
    SqlTraceSink,
    create_schema,
)
from aios.platform.sql.reviews import SqlReviewInbox
from aios.platform.tenancy import provision


class Rejected(RuntimeError):
    """Admission control refused the run."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


@dataclass
class SweepResult:
    """What the janitor did on one pass."""

    requeued: list[str]
    expired: list[str]


class Platform:
    """Wires the stores, the orchestrator and the policy checks together."""

    def __init__(
        self,
        settings: Settings | None = None,
        database_url: str | None = None,
        token_secret: str = "dev-secret-change-me",
    ) -> None:
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        url = database_url or f"sqlite:///{self.settings.workspace / 'platform.db'}"
        self.database = Database(url)
        create_schema(self.database)

        self.policy = SqlPolicyStore(self.database)
        self.runs = SqlRunStore(self.database)
        self.queue = SqlRunQueue(self.database)
        self.reviews = SqlReviewInbox(self.database)
        self.facts = SqlFactStore(self.database)
        self.trace = SqlTraceSink(self.database)
        self.objects = FileObjectStore(self.settings.workspace / "objects")
        self.tokens = TokenIssuer(token_secret)
        self.admission = AdmissionControl(self.policy, self.runs, self.queue)

    # --- provisioning ---------------------------------------------------------

    def provision_tenant(self, tenant: TenantPolicy) -> TenantPolicy:
        return provision(self.policy, tenant)

    def token_for(
        self,
        tenant_id: str,
        subject: str,
        kind: PrincipalKind = PrincipalKind.HUMAN,
        scopes: tuple[str, ...] = ("runs:submit", "runs:read", "reviews:decide"),
    ) -> str:
        return self.tokens.mint(tenant_id, subject, kind, scopes)

    # --- run lifecycle --------------------------------------------------------

    def submit(
        self,
        principal: Principal,
        goal: str,
        *,
        domain: str = "audit",
        scenario: str = "audit",
        offline: bool = True,
        lane: Lane = Lane.INTERACTIVE,
        budget: Budget | None = None,
        deadline_seconds: int | None = None,
    ) -> RunRecord:
        """Admit and enqueue a run, or raise `Rejected` with the reason."""
        decision = self.admission.admit(principal, lane)
        if not decision.admitted:
            raise Rejected(decision)

        record = RunRecord(
            tenant_id=principal.tenant_id,
            submitted_by=principal.subject,
            goal=goal,
            domain=domain,
            scenario=scenario,
            offline=offline,
            lane=lane,
            priority=LANE_PRIORITY[lane],
            budget=budget or decision.budget or Budget(),
            deadline_at=(
                now() + timedelta(seconds=deadline_seconds)
                if deadline_seconds
                else None
            ),
        )
        self.queue.submit(record)
        return record

    def cancel(self, principal: Principal, run_id: str) -> RunRecord | None:
        record = self.runs.get(run_id, principal.tenant_id)
        if record is None or record.terminal:
            return record
        self.runs.update_status(
            run_id, RunStatus.CANCELLED, failure_reason=f"cancelled by {principal}"
        )
        return self.runs.get(run_id, principal.tenant_id)

    # --- the human gate -------------------------------------------------------

    def decide(
        self, principal: Principal, review_id: str, approved: bool, note: str | None = None
    ) -> ReviewRequest:
        """Record a decision and put the run back on the queue to finish."""
        decision = Signoff(
            approved=approved, reviewer=principal.subject, note=note
        )
        review = self.reviews.decide(review_id, decision, principal)
        self.queue.requeue(review.run_id)
        return review

    def pending_reviews(self, principal: Principal) -> list[ReviewRequest]:
        return self.reviews.pending(principal.tenant_id, principal.subject)

    # --- background -----------------------------------------------------------

    def sweep(self, at: datetime | None = None) -> SweepResult:
        """Return lapsed runs to the queue and close reviews nobody answered."""
        requeued = self.queue.requeue_expired(at)
        expired = self.reviews.expire(at)
        for review in expired:
            self.runs.update_status(
                review.run_id,
                RunStatus.EXPIRED,
                failure_reason="no reviewer decision before the deadline",
            )
        return SweepResult(requeued=requeued, expired=[r.review_id for r in expired])

    def orchestrator(self) -> Orchestrator:
        return Orchestrator(
            self.settings,
            self.policy,
            self.runs,
            self.reviews,
            self.facts,
            self.trace,
            self.objects,
        )

    def close(self) -> None:
        self.database.close()

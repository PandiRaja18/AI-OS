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

    def provision_tenant(
        self, tenant: TenantPolicy, from_domain: bool = True
    ) -> TenantPolicy:
        """Create a tenant, granting the tools its configured domain publishes.

        Grants left over from a previous domain are revoked. Switching domains
        otherwise accumulates grants for tools that no longer exist, which makes
        the stored policy a misleading record of what a tenant can reach.
        """
        tools = None
        if from_domain and self.settings.domain_file is not None:
            from aios.domain import load_pack
            from aios.mcp_gateway import build_run_tools

            tools = build_run_tools(
                self.settings, "provisioning", pack=load_pack(self.settings)
            )

        policy = provision(self.policy, tenant, tools=tools)
        if tools is not None:
            published = {tool.name for tool in tools}
            for grant in self.policy.grants(tenant.tenant_id):
                if grant.tool not in published:
                    self.policy.revoke_grant(tenant.tenant_id, grant.tool)
        return policy

    def active_domain(self) -> dict[str, object]:
        """Which data a run would use, for the API and the console header."""
        if self.settings.domain_file is None:
            return {"name": "demo", "is_demo": True, "pack": None, "tools": []}

        from aios.domain import load_pack
        from aios.mcp_gateway import build_run_tools

        pack = load_pack(self.settings)
        tools = build_run_tools(self.settings, "inspect", pack=pack)
        return {
            "name": pack.name,
            "is_demo": False,
            "pack": str(self.settings.domain_file),
            "tools": sorted(tool.name for tool in tools),
        }

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

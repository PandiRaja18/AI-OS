"""Admission control.

Rejecting a run before it exists is far cheaper than discovering mid-run that a
tenant is out of budget, and it is the only thing standing between one tenant and
the whole worker pool. Every check here answers with a reason the caller can act
on, not a bare refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aios.platform.models import Budget, Lane, Principal, TenantPolicy, now
from aios.platform.protocols import PolicyStore, RunQueue, RunStore

MAX_QUEUE_DEPTH = 200


@dataclass(frozen=True)
class Decision:
    """Whether a run may be created, and the budget it gets if so."""

    admitted: bool
    reason: str = ""
    budget: Budget | None = None
    retry_after_seconds: int | None = None


class AdmissionControl:
    """Tenant caps, spend caps and queue backpressure, checked at the edge."""

    def __init__(
        self,
        policy: PolicyStore,
        runs: RunStore,
        queue: RunQueue,
        max_queue_depth: int = MAX_QUEUE_DEPTH,
    ) -> None:
        self._policy = policy
        self._runs = runs
        self._queue = queue
        self._max_queue_depth = max_queue_depth

    def admit(self, principal: Principal, lane: Lane = Lane.INTERACTIVE) -> Decision:
        tenant = self._policy.tenant(principal.tenant_id)
        if tenant is None:
            return Decision(False, f"unknown tenant {principal.tenant_id}")

        active = self._runs.active_run_count(principal.tenant_id)
        if active >= tenant.max_concurrent_runs:
            return Decision(
                False,
                f"tenant at its concurrency cap: {active} of "
                f"{tenant.max_concurrent_runs} runs active",
                retry_after_seconds=30,
            )

        spent = self._month_to_date(tenant)
        if spent >= tenant.monthly_currency_cap:
            return Decision(
                False,
                f"monthly spend cap reached: {spent} of {tenant.monthly_currency_cap}",
            )

        if lane is not Lane.INTERACTIVE:
            depth = self._queue.depth()
            if depth >= self._max_queue_depth:
                return Decision(
                    False,
                    f"queue depth {depth} is over the shed threshold "
                    f"{self._max_queue_depth}; interactive work takes precedence",
                    retry_after_seconds=60,
                )

        return Decision(True, budget=self._remaining_budget(tenant, spent))

    def _month_to_date(self, tenant: TenantPolicy) -> Decimal:
        start = now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
        )
        return self._runs.spend_since(tenant.tenant_id, start).currency

    def _remaining_budget(self, tenant: TenantPolicy, spent: Decimal) -> Budget:
        """Never grant a run more headroom than the tenant has left this month."""
        budget = tenant.default_budget
        remaining = tenant.monthly_currency_cap - spent
        if remaining < budget.max_currency:
            return budget.model_copy(update={"max_currency": max(remaining, Decimal(0))})
        return budget


def month_start(at: datetime | None = None) -> datetime:
    moment = at or now()
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def default_deadline(seconds: int) -> datetime:
    return now() + timedelta(seconds=seconds)

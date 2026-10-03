"""Spend metering and hard ceilings.

The capacity model says the constraint on this platform is money, not compute, so
the budget is not advisory. It is checked before every model call and the run
halts with partial results rather than overrunning.

The meter wraps whatever `LlmClient` the run is using, so metering cannot be
bypassed by an agent - there is no unmetered path to a model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aios.llm import LlmClient, Usage
from aios.platform.models import Budget, Spend
from aios.platform.protocols import RunStore

# US dollars per million tokens. Verify against current published rates; these
# move, and a stale table quietly misprices every run.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
    "replay": (Decimal("0"), Decimal("0")),
}

CHARS_PER_TOKEN = 4


class BudgetExceeded(RuntimeError):
    """A run crossed one of its ceilings."""


def price(model: str, usage: Usage) -> Decimal:
    """Cost of one call, at the configured rates.

    A self-hosted model is billed in GPU time, not per token, so `local:` prices
    at zero - its token ceilings still bind, which is what bounds a runaway run.
    An unrecognised model is priced at the most expensive rate rather than free,
    so a typo overstates cost instead of hiding it.
    """
    if model.startswith("local:"):
        return Decimal(0)
    rates = PRICING.get(model, PRICING["claude-opus-5"])
    return (
        Decimal(usage.input_tokens) * rates[0]
        + Decimal(usage.output_tokens) * rates[1]
    ) / Decimal(1_000_000)


class RunBudgetMeter:
    """Tracks one run's spend against its ceilings."""

    def __init__(self, runs: RunStore, run_id: str, budget: Budget) -> None:
        self._runs = runs
        self._run_id = run_id
        self._budget = budget
        record = runs.get(run_id)
        self._spend = record.spend if record else Spend()

    @property
    def spend(self) -> Spend:
        return self._spend

    def check(self, run_id: str | None = None) -> None:
        breach = self._spend.breach(self._budget)
        if breach is not None:
            raise BudgetExceeded(breach)

    def record(self, delta: Spend, run_id: str | None = None) -> Spend:
        self._spend = self._runs.add_spend(self._run_id, delta)
        return self._spend


class MeteredLlm:
    """An `LlmClient` that cannot be called without paying for it."""

    def __init__(self, inner: LlmClient, meter: RunBudgetMeter, model: str) -> None:
        self._inner = inner
        self._meter = meter
        self._model = model
        self.name = inner.name

    def structured(
        self,
        *,
        key: str,
        system: str,
        prompt: str,
        output_model: type,
        task_id: str | None = None,
        actor: str = "planner",
    ) -> Any:
        self._meter.check()
        result = self._inner.structured(
            key=key,
            system=system,
            prompt=prompt,
            output_model=output_model,
            task_id=task_id,
            actor=actor,
        )
        usage = getattr(self._inner, "last_usage", None) or _estimate(
            system, prompt, result
        )
        self._meter.record(
            Spend(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                currency=price(self._model, usage),
                llm_calls=1,
            )
        )
        return result


def _estimate(system: str, prompt: str, result: Any) -> Usage:
    """Rough usage for clients that do not report it, so budgets still bind."""
    rendered = result.model_dump_json() if hasattr(result, "model_dump_json") else ""
    return Usage(
        input_tokens=(len(system) + len(prompt)) // CHARS_PER_TOKEN,
        output_tokens=len(rendered) // CHARS_PER_TOKEN,
    )

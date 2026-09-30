"""The cost guard, and the ledger it reads.

Arch §10.2, which this file implements literally:

    spend < 80% of monthly cap   ->  normal operation
    spend >= 80%                 ->  operator warning raised
    spend >= 100% (hard cap)     ->  connector auto-paused, alert raised
    per-run charge > max         ->  run rejected before it starts

"Hard caps are enforced BEFORE a run starts, not reconciled after. A runaway
Apify Actor or an LLM retry storm cannot silently consume a month's margin."

That last sentence is the design. A guard that runs after the call has already
been paid for is a report, not a control.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.operations.models import AuditEvent, CostEvent

logger = logging.getLogger(__name__)

#: Arch §10.2's warning threshold.
WARN_AT = Decimal("0.80")

#: The provider name used for every model call in the cost ledger. One string,
#: so month-to-date LLM spend is a single query rather than a guess about which
#: model names were in use that month.
LLM_PROVIDER = "llm"


class CostCapExceeded(RuntimeError):
    """Raised instead of making the call.

    Caught by the callers that can degrade gracefully — the gate falls back to
    "not assessed" rather than "rejected", because a budget stop must never
    look like a content decision.
    """


@dataclass(frozen=True)
class Budget:
    spent: Decimal
    cap: Decimal

    @property
    def remaining(self) -> Decimal:
        return max(self.cap - self.spent, Decimal(0))

    @property
    def fraction(self) -> Decimal:
        if self.cap <= 0:
            return Decimal(0)
        return self.spent / self.cap

    @property
    def is_warning(self) -> bool:
        return self.cap > 0 and self.fraction >= WARN_AT

    @property
    def is_exhausted(self) -> bool:
        return self.cap > 0 and self.spent >= self.cap


def monthly_cap() -> Decimal:
    """The LLM hard cap, from the environment.

    `LLM_MONTHLY_CAP_USD` has been sitting in `deploy/.env.example` unread
    since the skeleton. This is the code that reads it.
    """
    return Decimal(str(getattr(settings, "LLM_MONTHLY_CAP_USD", 0) or 0))


def spend_this_month(provider: str = LLM_PROVIDER) -> Decimal:
    """Month-to-date spend from the cost ledger.

    Calendar month, matching how the cap is expressed and how the invoice
    arrives. A rolling window would be defensible but would not line up with
    the bill anyone has to explain.
    """
    now = timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = CostEvent.objects.filter(provider=provider, at__gte=start).aggregate(
        total=Sum("estimated_usd")
    )["total"]
    return Decimal(total or 0)


def budget(provider: str = LLM_PROVIDER) -> Budget:
    return Budget(spent=spend_this_month(provider), cap=monthly_cap())


def check_or_raise(estimated: Decimal, *, purpose: str, provider: str = LLM_PROVIDER) -> Budget:
    """Refuse the call if it would breach the cap. Warn at 80%.

    Returns the budget so the caller can log it; raises `CostCapExceeded` when
    the call must not happen.
    """
    current = budget(provider)

    # An unset cap means unmetered, not free. Say so once, loudly, rather than
    # silently running without a ceiling.
    if current.cap <= 0:
        logger.warning(
            "LLM_MONTHLY_CAP_USD is unset — running without a spend ceiling. "
            "Arch §10.2 expects a hard cap enforced before each call."
        )
        return current

    per_call_max = Decimal(str(getattr(settings, "LLM_PER_CALL_MAX_USD", 0) or 0))
    if per_call_max > 0 and estimated > per_call_max:
        raise CostCapExceeded(
            f"{purpose}: estimated ${estimated:.4f} exceeds the per-call maximum "
            f"of ${per_call_max:.4f}. Refused before the call (Arch §10.2)."
        )

    if current.spent + estimated > current.cap:
        _alert(
            f"LLM monthly cap reached: ${current.spent:.2f} of ${current.cap:.2f} spent; "
            f"a {purpose} call estimated at ${estimated:.4f} was refused.",
            context={"purpose": purpose, "spent": str(current.spent), "cap": str(current.cap)},
        )
        raise CostCapExceeded(
            f"{purpose}: ${current.spent:.2f} of ${current.cap:.2f} monthly cap already spent. "
            f"Refused before the call (Arch §10.2)."
        )

    if current.is_warning:
        logger.warning(
            "LLM spend at %.0f%% of cap ($%.2f of $%.2f)", current.fraction * 100,
            current.spent, current.cap,
        )

    return current


def record(
    *,
    model: str,
    purpose: str,
    cost: Decimal,
    units: dict,
    run_id: str = "",
    provider: str = LLM_PROVIDER,
) -> CostEvent:
    """Write the call to the cost ledger.

    One `CostEvent` per external call, per Arch §10.1. `source_id` carries the
    purpose so spend can be split between the gate and extraction without
    joining back to ModelRun — which matters because the gate is high-volume
    and cheap, and extraction is the opposite.
    """
    return CostEvent.objects.create(
        provider=provider,
        source_id=purpose,
        run_id=run_id,
        units={**units, "model": model},
        estimated_usd=cost,
    )


def _alert(message: str, *, context: dict) -> None:
    """Arch §13's alert philosophy: page on anything that silently produces
    wrong output. A budget stop does not produce wrong output — it produces no
    output — so it is audited and logged, not paged."""
    logger.error(message)
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.SYSTEM,
        actor_realm=AuditEvent.Realm.SYSTEM,
        actor_label="cost-guard",
        message=message,
        context=context,
    )

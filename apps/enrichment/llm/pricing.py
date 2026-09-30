"""What a call costs.

Arch §10.3 carries $180–250/month as a *budget reserve, not a measured cost* —
"with placeholder token rates and no real usage data, the reserve is the safer
planning number". These rates are what turn that reserve into a measurement.

Prices are USD per million tokens, from Anthropic's published rates as of
2026-09. They are DATA, not constants scattered through the code, so that a
price change is one edit and a visible diff rather than a hunt.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Rates:
    """Per-million-token prices for one model."""

    input: Decimal
    output: Decimal
    #: A cache read is roughly a tenth of a fresh input token, which is the
    #: whole reason prompt caching is worth the complexity — see Arch §10.3's
    #: "extraction runs once per content item, ever".
    cache_read: Decimal
    #: Writing to the cache costs a premium over a plain input token. Worth it
    #: only when the prefix will be read back many times, which for the
    #: relevance gate (one domain pack, hundreds of items) it always is.
    cache_write: Decimal


#: Only the models this system actually sends. Adding a tier means adding a row
#: here first — `estimate` raises on an unknown model rather than guessing,
#: because a silent zero would understate the bill.
RATES: dict[str, Rates] = {
    # Cheap tier — the relevance gate. Arch §6.2: "uses the cheapest viable
    # model tier; this is a classification task, not a reasoning task."
    "claude-haiku-4-5": Rates(
        input=Decimal("1.00"),
        output=Decimal("5.00"),
        # Derived at the standard 0.1x / 1.25x multiples; confirm against the
        # published rate before the first invoice is reconciled.
        cache_read=Decimal("0.10"),
        cache_write=Decimal("1.25"),
    ),
    # Middle tier — extraction. Structured, but it needs judgement about what
    # is a claim and what is throat-clearing.
    "claude-sonnet-5-5": Rates(
        input=Decimal("2.00"),
        output=Decimal("10.00"),
        cache_read=Decimal("0.20"),
        cache_write=Decimal("2.50"),
    ),
    # Strong tier — client-facing drafting only (Arch §10.3).
    "claude-opus-5-5": Rates(
        input=Decimal("4.00"),
        output=Decimal("20.00"),
        cache_read=Decimal("0.20"),
        cache_write=Decimal("5.00"),
    ),
}


class UnknownModel(KeyError):
    """Raised rather than costing an unpriced call at zero."""


def rates_for(model: str) -> Rates:
    try:
        return RATES[model]
    except KeyError as exc:
        raise UnknownModel(
            f"No price on file for {model!r}. Add it to apps/enrichment/llm/pricing.py "
            f"— an unpriced model would bill as zero and silently break the cost ledger."
        ) from exc


def cost_of(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Actual cost of a completed call."""
    r = rates_for(model)
    return (
        Decimal(input_tokens) * r.input
        + Decimal(output_tokens) * r.output
        + Decimal(cache_read_tokens) * r.cache_read
        + Decimal(cache_write_tokens) * r.cache_write
    ) / MILLION


def estimate(model: str, *, input_tokens: int, max_output_tokens: int) -> Decimal:
    """Worst-case cost of a call not yet made.

    Deliberately pessimistic — it prices the full `max_tokens` as though the
    model will use all of it. Arch §10.2 enforces caps BEFORE a run starts, and
    a guard that assumes the cheap case is not a guard.
    """
    return cost_of(model, input_tokens=input_tokens, output_tokens=max_output_tokens)

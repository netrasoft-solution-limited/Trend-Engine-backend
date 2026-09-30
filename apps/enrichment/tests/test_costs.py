"""Cost governance — Arch §10.

"A $500 ceiling cannot be enforced by monthly review." These tests are the
difference between that sentence being true and being a comment.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.enrichment.llm import costs, pricing
from apps.enrichment.llm.client import LLMClient, Tier
from apps.enrichment.models import ModelRun
from apps.operations.models import CostEvent

from .fakes import FakeTransport, respond

pytestmark = pytest.mark.django_db


from pydantic import BaseModel  # noqa: E402


class Answer(BaseModel):
    ok: bool


# ── Pricing ─────────────────────────────────────────────────────────────────


def test_an_unpriced_model_raises_rather_than_billing_zero():
    """A missing price would bill as zero and silently break the ledger — the
    cap would then never trigger, which is worse than a loud failure."""
    with pytest.raises(pricing.UnknownModel):
        pricing.cost_of("some-model-nobody-priced", input_tokens=1000)


def test_a_cache_read_is_much_cheaper_than_a_fresh_input_token():
    """The whole reason prompt caching is worth the complexity (Arch §10.3)."""
    fresh = pricing.cost_of("claude-haiku-4-5", input_tokens=1_000_000)
    cached = pricing.cost_of("claude-haiku-4-5", cache_read_tokens=1_000_000)

    assert cached < fresh / 5


def test_the_estimate_is_pessimistic():
    """Arch §10.2 enforces caps before the call. A guard that assumes the cheap
    case is not a guard, so the estimate prices the full output ceiling."""
    estimated = pricing.estimate("claude-haiku-4-5", input_tokens=1000, max_output_tokens=512)
    actual_if_terse = pricing.cost_of("claude-haiku-4-5", input_tokens=1000, output_tokens=10)

    assert estimated > actual_if_terse


def test_the_gate_tier_is_cheaper_than_the_drafting_tier():
    """Arch §10.3's tiering, asserted rather than assumed: "cheap tier for
    relevance classification, stronger tier only for client-facing drafting"."""
    same_work = {"input_tokens": 100_000, "output_tokens": 2_000}
    gate_cost = pricing.cost_of("claude-haiku-4-5", **same_work)
    draft_cost = pricing.cost_of("claude-opus-5-5", **same_work)

    assert gate_cost < draft_cost


# ── The budget ──────────────────────────────────────────────────────────────


def test_spend_is_read_from_the_cost_ledger(settings):
    settings.LLM_MONTHLY_CAP_USD = "10.00"
    CostEvent.objects.create(provider=costs.LLM_PROVIDER, estimated_usd=Decimal("3.00"))
    CostEvent.objects.create(provider=costs.LLM_PROVIDER, estimated_usd=Decimal("1.50"))
    # Another vendor's spend must not count against the LLM cap.
    CostEvent.objects.create(provider="apify", estimated_usd=Decimal("99.00"))

    budget = costs.budget()

    assert budget.spent == Decimal("4.50")
    assert budget.remaining == Decimal("5.50")


def test_the_warning_fires_at_eighty_percent(settings):
    settings.LLM_MONTHLY_CAP_USD = "10.00"
    CostEvent.objects.create(provider=costs.LLM_PROVIDER, estimated_usd=Decimal("8.00"))

    assert costs.budget().is_warning is True
    assert costs.budget().is_exhausted is False


def test_a_call_that_would_breach_the_cap_is_refused(settings):
    settings.LLM_MONTHLY_CAP_USD = "10.00"
    CostEvent.objects.create(provider=costs.LLM_PROVIDER, estimated_usd=Decimal("9.99"))

    with pytest.raises(costs.CostCapExceeded):
        costs.check_or_raise(Decimal("0.50"), purpose="extraction")


def test_a_single_extravagant_call_is_refused_even_under_the_cap(settings):
    """A runaway prompt or a document pasted into a classifier. The monthly cap
    would absorb it; the per-call ceiling is what catches it the first time."""
    settings.LLM_MONTHLY_CAP_USD = "1000.00"
    settings.LLM_PER_CALL_MAX_USD = "0.50"

    with pytest.raises(costs.CostCapExceeded):
        costs.check_or_raise(Decimal("5.00"), purpose="extraction")


def test_an_unset_cap_runs_but_says_so(settings, caplog):
    """Zero means unmetered, not free — and it should be noisy, because an
    unmetered production system is a surprise waiting to happen."""
    settings.LLM_MONTHLY_CAP_USD = "0"

    budget = costs.check_or_raise(Decimal("5.00"), purpose="extraction")

    assert budget.cap == 0
    assert any("without a spend ceiling" in r.message for r in caplog.records)


# ── The client's accounting ─────────────────────────────────────────────────


def _call(transport, **settings_kwargs) -> None:
    LLMClient(transport=transport).structured(
        schema=Answer,
        tier=Tier(model="claude-haiku-4-5", max_output_tokens=256),
        purpose=ModelRun.Purpose.RELEVANCE_GATE,
        prompt_version="test/1",
        system="stable",
        user="question",
    )


def test_a_successful_call_writes_both_a_model_run_and_a_cost_event(settings):
    settings.LLM_MONTHLY_CAP_USD = "10.00"
    _call(FakeTransport([respond(Answer(ok=True))]))

    run = ModelRun.objects.get()
    event = CostEvent.objects.get(provider=costs.LLM_PROVIDER)

    assert run.outcome == ModelRun.Outcome.SUCCEEDED
    assert run.cost_usd > 0
    assert event.estimated_usd == run.cost_usd
    assert event.units["model"] == "claude-haiku-4-5"
    assert event.run_id == str(run.pk)


def test_a_failed_call_still_records_the_run(settings):
    """It still cost money, and it still says something about reliability."""
    settings.LLM_MONTHLY_CAP_USD = "10.00"

    with pytest.raises(RuntimeError):
        _call(FakeTransport(raises=RuntimeError("upstream 500")))

    run = ModelRun.objects.get()
    assert run.outcome == ModelRun.Outcome.ERRORED
    assert "upstream 500" in run.error


def test_a_capped_call_is_recorded_but_not_billed(settings):
    """A refused call is a fact about the system, not a fact about the bill."""
    settings.LLM_MONTHLY_CAP_USD = "0.0000001"
    transport = FakeTransport([respond(Answer(ok=True))])

    with pytest.raises(costs.CostCapExceeded):
        _call(transport)

    assert transport.calls == []
    assert ModelRun.objects.get().outcome == ModelRun.Outcome.CAPPED
    assert CostEvent.objects.filter(provider=costs.LLM_PROVIDER).count() == 0


def test_cache_tokens_are_recorded_separately(settings):
    """A bill that ignores the cache split is wrong by an order of magnitude on
    the cached portion."""
    settings.LLM_MONTHLY_CAP_USD = "10.00"
    _call(
        FakeTransport(
            [respond(Answer(ok=True), input_tokens=100, cache_read_input_tokens=50_000)]
        )
    )

    run = ModelRun.objects.get()
    assert run.cache_read_tokens == 50_000
    # Priced at the cache rate, not the input rate.
    assert run.cost_usd < pricing.cost_of("claude-haiku-4-5", input_tokens=50_100)

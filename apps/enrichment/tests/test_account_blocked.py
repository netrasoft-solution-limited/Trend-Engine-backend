"""16–19 Nov: the Anthropic workspace reached its usage limit. 18,000 calls
failed, and detection crashed four mornings running without recording a run."""
import pytest
from django.core.cache import cache

from apps.enrichment.llm import client as llm
from apps.enrichment.llm.client import LLMClient, LLMUnavailable, gate_tier
from apps.enrichment.models import ModelRun
from apps.enrichment.tests.fakes import FakeTransport

pytestmark = pytest.mark.django_db

LIMIT = Exception(
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'You have reached your specified workspace API usage limits. You will "
    "regain access on 2026-12-01 at 00:00 UTC.'}}"
)


@pytest.fixture(autouse=True)
def _clear():
    cache.delete(llm.BLOCKED_KEY)
    yield
    cache.delete(llm.BLOCKED_KEY)


def ask(transport):
    from pydantic import BaseModel

    class Answer(BaseModel):
        ok: bool

    return LLMClient(transport=transport).structured(
        schema=Answer, tier=gate_tier(), purpose="relevance_gate",
        prompt_version="t", system="s", user="u",
    )


def test_a_usage_limit_is_unavailable_and_stops_further_calls():
    transport = FakeTransport([LIMIT])

    with pytest.raises(LLMUnavailable, match="regain access on 2026-12-01"):
        ask(transport)
    with pytest.raises(LLMUnavailable, match="not sent"):
        ask(transport)

    assert len(transport.calls) == 1, "the second call is refused before it is sent"
    assert ModelRun.objects.filter(outcome="errored").count() == 1


def test_pending_items_are_not_dispatched_while_blocked():
    from apps.enrichment import tasks

    cache.set(llm.BLOCKED_KEY, "usage limits", 60)

    assert tasks.assess_pending()["dispatched"] == 0


# ── 20 Nov reconciliation ───────────────────────────────────────────────────


def test_a_cut_off_answer_is_billed_at_its_ceiling():
    from apps.enrichment.llm.client import LLMTruncated
    from apps.enrichment.tests.fakes import cut_off

    with pytest.raises(LLMTruncated):
        ask(FakeTransport([cut_off()]))

    run = ModelRun.objects.get()
    assert run.output_tokens == gate_tier().max_output_tokens


def test_reconciliation_records_the_provider_gap_once():
    from django.core.management import call_command

    from apps.enrichment.llm import costs

    costs.record(model="claude-haiku-4-5", purpose="t", cost=costs.Decimal("10"), units={})
    call_command("reconcile_llm_spend", "15.50", "--as-of", "now", "--by", "abu")

    assert costs.spend_this_month() == costs.Decimal("15.50")
    call_command("reconcile_llm_spend", "15.50", "--as-of", "now", "--by", "abu")
    assert costs.CostEvent.objects.filter(source_id="provider-reconciliation").count() == 1


def test_an_item_read_once_with_nothing_to_extract_is_not_paid_for_again():
    from apps.enrichment import extraction
    from apps.evidence.models import ContentItem
    from apps.sources.models import Source

    source = Source.objects.create(name="s", route="podcast")
    item = ContentItem.objects.create(source=source, external_id="x", title="t", content_hash="x",
                                      content_state="transcribed")
    ModelRun.objects.create(purpose="extraction", outcome="succeeded", model="m",
                            prompt_version="v", content_item=item)

    result = extraction.extract(item, domain_pack="d")

    assert result.skipped == "already extracted; nothing to extract"

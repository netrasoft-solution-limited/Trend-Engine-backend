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


def test_a_crashed_detection_run_is_recorded_as_failed(monkeypatch):
    from apps.intelligence import detection, health, tasks

    def crash(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(detection, "detect", crash)
    with pytest.raises(RuntimeError):
        tasks.detect_signals()

    assert health.failing_streak()[0].context["errors"] == ["RuntimeError: boom"]


def test_pending_items_are_not_dispatched_while_blocked():
    from apps.enrichment import tasks

    cache.set(llm.BLOCKED_KEY, "usage limits", 60)

    assert tasks.assess_pending()["dispatched"] == 0

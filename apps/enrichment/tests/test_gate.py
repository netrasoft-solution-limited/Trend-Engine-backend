"""The relevance gate — Arch §6.2.

The gate decides what the system spends money to transcribe, so these tests
are about money and about blind spots, not about classification accuracy:

  · it must never read the expensive thing it exists to avoid fetching
  · a budget stop must not be recorded as a content decision
  · rejections must be sampled, or the filter calcifies silently
"""
from __future__ import annotations

import random

import pytest
from django.utils import timezone

from apps.enrichment import gate
from apps.enrichment.llm.client import LLMClient
from apps.enrichment.models import ModelRun
from apps.evidence.models import ContentItem, TranscriptArtifact
from apps.sources.models import Source

from .fakes import FakeTransport, respond

pytestmark = pytest.mark.django_db

DOMAIN = "Dietary supplements and nutraceuticals."


@pytest.fixture
def source(db):
    return Source.objects.create(name="Test podcast", route=Source.Route.PODCAST)


def make_item(source, **kwargs) -> ContentItem:
    defaults = {
        "external_id": "ep-1",
        "title": "Creatine beyond the gym",
        "description": "A conversation about cognition and healthy ageing.",
        "content_hash": ContentItem.hash_content("ep-1", "v1"),
    }
    return ContentItem.objects.create(source=source, **{**defaults, **kwargs})


def client_returning(**fields) -> tuple[LLMClient, FakeTransport]:
    verdict = gate.GateVerdict(
        relevant=fields.get("relevant", True),
        reason=fields.get("reason", "Discusses creatine and cognition."),
        confidence=fields.get("confidence", 90),
    )
    transport = FakeTransport([respond(verdict)])
    return LLMClient(transport=transport), transport


def test_a_relevant_item_passes_and_earns_a_transcript(source):
    item = make_item(source)
    llm, _ = client_returning(relevant=True)

    result = gate.assess(item, domain_pack=DOMAIN, client=llm)
    item.refresh_from_db()

    assert result.decision == ContentItem.GateDecision.RELEVANT
    assert item.gate_decision == ContentItem.GateDecision.RELEVANT
    assert gate.needs_transcript(item)


def test_the_gate_never_sees_the_transcript(source):
    """Arch §6.2: the gate runs on metadata ONLY.

    A gate that reads the full text has already paid the cost it exists to
    avoid. This asserts on what was actually sent, not on intent.
    """
    item = make_item(source, description="Short blurb.")
    TranscriptArtifact.objects.create(
        content_item=item,
        method=TranscriptArtifact.Method.PUBLISHER,
        text="SECRET_TRANSCRIPT_BODY " * 500,
    )
    llm, transport = client_returning()

    gate.assess(item, domain_pack=DOMAIN, client=llm)

    sent = str(transport.calls[0])
    assert "SECRET_TRANSCRIPT_BODY" not in sent


def test_a_rejection_is_recorded_with_its_reason(source):
    """Arch §6.2: "Rejection reasons are stored, making filter quality
    auditable and tunable." A rejection with no reason cannot be reviewed."""
    item = make_item(source)
    llm, _ = client_returning(
        relevant=False, reason="Marathon training interview, no ingredient discussion."
    )

    result = gate.assess(item, domain_pack=DOMAIN, client=llm, rng=random.Random(1))
    item.refresh_from_db()

    assert result.decision == ContentItem.GateDecision.REJECTED
    assert "Marathon training" in item.gate_reason
    assert not gate.needs_transcript(item)


def test_some_rejections_are_sampled_through_anyway(source):
    """The guard against calcification.

    Without this the filter narrows to what the taxonomy already knows and
    reports a healthy pass rate while doing it. A seeded RNG below the sample
    rate forces the branch.
    """
    item = make_item(source)
    llm, _ = client_returning(relevant=False, reason="Looks off-topic.")

    class AlwaysSample(random.Random):
        def random(self) -> float:
            return 0.0

    result = gate.assess(item, domain_pack=DOMAIN, client=llm, rng=AlwaysSample())
    item.refresh_from_db()

    assert result.sampled is True
    assert item.gate_decision == ContentItem.GateDecision.SAMPLED
    # The point of sampling: it gets the full treatment despite the rejection.
    assert gate.needs_transcript(item)


def test_an_outage_leaves_the_item_pending_not_rejected(source):
    """A budget stop or an outage must never look like a content decision.

    If it did, the item would never be reassessed — the system would have
    silently decided that something it could not afford to read was irrelevant.
    """
    item = make_item(source)
    llm = LLMClient(transport=FakeTransport(raises=RuntimeError("connection reset")))

    with pytest.raises(RuntimeError):
        gate.assess(item, domain_pack=DOMAIN, client=llm)

    item.refresh_from_db()
    assert item.gate_decision == ContentItem.GateDecision.PENDING


def test_the_cap_refuses_before_the_call_is_made(source, settings):
    """Arch §10.2: enforced BEFORE a run starts, not reconciled after."""
    settings.LLM_MONTHLY_CAP_USD = "0.0000001"
    item = make_item(source)
    transport = FakeTransport([respond(gate.GateVerdict(relevant=True, reason="x", confidence=1))])

    gate.assess(item, domain_pack=DOMAIN, client=LLMClient(transport=transport))
    item.refresh_from_db()

    assert transport.calls == [], "the cap must refuse before the request is sent"
    assert item.gate_decision == ContentItem.GateDecision.PENDING
    assert ModelRun.objects.filter(outcome=ModelRun.Outcome.CAPPED).count() == 1


def test_assessing_twice_does_not_call_the_model_again(source):
    """Idempotent: a retried run must not re-bill for work already done."""
    item = make_item(source)
    llm, transport = client_returning()

    gate.assess(item, domain_pack=DOMAIN, client=llm)
    gate.assess(item, domain_pack=DOMAIN, client=llm)

    assert len(transport.calls) == 1


def test_every_call_records_a_model_run(source):
    """Arch §8.2: the model and prompt version are persisted at write time,
    because they cannot be reconstructed later."""
    item = make_item(source)
    llm, _ = client_returning()

    gate.assess(item, domain_pack=DOMAIN, client=llm)

    run = ModelRun.objects.get(purpose=ModelRun.Purpose.RELEVANCE_GATE)
    assert run.outcome == ModelRun.Outcome.SUCCEEDED
    assert run.model == "claude-haiku-4-5", "the gate must use the cheap tier (Arch §10.3)"
    assert run.prompt_version == gate.PROMPT_VERSION
    assert run.content_item_id == item.pk
    assert run.cost_usd > 0


def test_the_funnel_reports_the_shape_of_the_filter(source):
    """Arch §6.2's ~65 -> ~26, made visible rather than asserted."""
    now = timezone.now()
    for i in range(6):
        ContentItem.objects.create(
            source=source,
            external_id=f"e{i}",
            title=f"Item {i}",
            content_hash=f"h{i}",
            gate_decision=(
                ContentItem.GateDecision.RELEVANT if i < 2 else ContentItem.GateDecision.REJECTED
            ),
            gate_assessed_at=now,
        )
    ContentItem.objects.filter(external_id="e5").update(
        gate_decision=ContentItem.GateDecision.SAMPLED
    )

    stats = gate.funnel_stats()

    assert stats["passed"] == 2
    assert stats["rejected"] == 4  # rejected + sampled
    assert stats["sampled"] == 1
    assert stats["pass_rate"] == pytest.approx(2 / 6, abs=0.01)

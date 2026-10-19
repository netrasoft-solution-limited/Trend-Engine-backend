"""Signal detection — what the model may and may not decide.

The model groups claims and words the development. Everything else is checked
here, because the failure modes are the quiet ones: an invented claim ID that
becomes "evidence", one loud episode presented as a cross-source trend, or a
second copy of a signal the operator is already reviewing.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.enrichment.domain import DOMAIN_PACK_VERSION
from apps.enrichment.llm.client import LLMClient
from apps.enrichment.models import Claim, ModelRun
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.evidence.models import ContentItem, ContentSegment
from apps.intelligence import detection
from apps.intelligence.detection import DetectedSignal, Detection
from apps.intelligence.models import Signal, SignalEvidence
from apps.sources.models import Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def corpus(db):
    """Three items from three creators across two source types."""
    podcast = Source.objects.create(name="A podcast", route=Source.Route.PODCAST)
    youtube = Source.objects.create(name="A channel", route=Source.Route.YOUTUBE)
    now = timezone.now()
    claims = {}

    def item(key, source, creator, days_ago):
        return ContentItem.objects.create(
            source=source,
            external_id=key,
            title=f"Episode {key}",
            creator=creator,
            content_hash=ContentItem.hash_content(key),
            content_state=ContentItem.ContentState.TRANSCRIBED,
            published_at=now - timedelta(days=days_ago),
        )

    def claim(name, content_item, text, *, cited=False, sponsored=False, ordinal=1):
        segment = ContentSegment.objects.create(
            content_item=content_item, ordinal=ordinal, text=text,
            start_char=0, end_char=len(text), start_seconds=60.0 * ordinal,
        )
        claims[name] = Claim.objects.create(
            content_item=content_item, segment=segment, kind="comparison",
            subject="Magnesium forms", text=text, quote=text,
            is_cited=cited, is_sponsored=sponsored,
        )

    a = item("a", podcast, "Dr Huberman", 2)
    b = item("b", youtube, "Thomas DeLauer", 4)
    c = item("c", podcast, "Rhonda Patrick", 6)
    claim("glycinate", a, "Magnesium glycinate is the form to take for sleep.", cited=True)
    claim("threonate", b, "Magnesium L-threonate is the form for focus.")
    claim("doubt", c, "The form of magnesium matters far less than the dose.", ordinal=2)
    claim("ad", c, "AG1 covers your magnesium needs.", sponsored=True, ordinal=3)
    claim("solo_1", a, "Creatine helps sleep-deprived cognition.", ordinal=4)
    claim("solo_2", a, "Creatine at ten grams reaches the brain.", ordinal=5)
    return claims


def detecting(*signals: DetectedSignal) -> tuple[LLMClient, FakeTransport]:
    transport = FakeTransport([respond(Detection(signals=list(signals)))])
    return LLMClient(transport=transport), transport


def magnesium(corpus, **overrides) -> DetectedSignal:
    fields = dict(
        title="Magnesium becomes a choice by goal",
        statement="Creators now recommend magnesium forms by goal: glycinate for sleep, "
        "L-threonate for focus.",
        signal_type="convergence",
        supporting_claim_ids=[corpus["glycinate"].pk, corpus["threonate"].pk],
        contradicting_claim_ids=[corpus["doubt"].pk],
        needs_scientific_routing=True,
    )
    return DetectedSignal(**{**fields, **overrides})


# ── A good detection is stored whole ────────────────────────────────────────


def test_a_detected_signal_is_stored_with_its_evidence_and_provenance(corpus):
    llm, _ = detecting(magnesium(corpus))

    result = detection.detect(client=llm)

    [signal] = Signal.objects.all()
    assert result.created == [signal.code]
    assert signal.statement.startswith("Creators now recommend")
    assert signal.signal_type == Signal.Type.CONVERGENCE
    assert signal.state == Signal.State.CANDIDATE
    assert signal.domain_pack_version == DOMAIN_PACK_VERSION
    assert signal.prompt_version == detection.PROMPT_VERSION
    assert signal.detected_by.purpose == ModelRun.Purpose.DETECTION
    roles = dict(SignalEvidence.objects.values_list("claim_id", "role"))
    assert roles == {
        corpus["glycinate"].pk: "supports",
        corpus["threonate"].pk: "supports",
        corpus["doubt"].pk: "contradicts",
    }, "the dissenting claim is kept, not dropped"


def test_a_detected_signal_is_scored_from_its_evidence(corpus):
    llm, _ = detecting(magnesium(corpus))

    detection.detect(client=llm)
    signal = Signal.objects.get()

    assert signal.scored_at is not None
    assert (signal.item_count, signal.creator_count, signal.source_count) == (2, 2, 2)
    assert set(signal.components) == set(detection.components.WEIGHTS)
    assert signal.components["momentum"]["value"] is None, "a two-day-old corpus has no baseline"
    assert 0 < signal.domain_score <= 100
    assert any("contradict" in r for r in signal.confidence_reasons)


# ── What is never trusted ───────────────────────────────────────────────────


def test_an_invented_claim_id_is_dropped_and_counted(corpus):
    llm, _ = detecting(
        magnesium(corpus, supporting_claim_ids=[
            corpus["glycinate"].pk, corpus["threonate"].pk, 999_999,
        ])
    )

    result = detection.detect(client=llm)

    assert result.dropped_unknown_ids == 1
    assert not SignalEvidence.objects.filter(claim_id=999_999).exists()


def test_one_item_repeating_itself_is_not_a_signal(corpus):
    """Two claims, one episode. PRD §6.3: cap repeated creator influence."""
    llm, _ = detecting(
        magnesium(
            corpus,
            title="Creatine for the brain",
            supporting_claim_ids=[corpus["solo_1"].pk, corpus["solo_2"].pk],
            contradicting_claim_ids=[],
        )
    )

    result = detection.detect(client=llm)

    assert result.dropped_single_source == 1
    assert not Signal.objects.exists()


def test_an_unknown_signal_type_becomes_convergence(corpus):
    llm, _ = detecting(magnesium(corpus, signal_type="viral moment"))

    detection.detect(client=llm)

    assert Signal.objects.get().signal_type == Signal.Type.CONVERGENCE


def test_sponsored_claims_are_never_shown_to_the_model(corpus):
    llm, transport = detecting()

    detection.detect(client=llm)

    sent = transport.calls[0]["messages"][0]["content"]
    assert "AG1" not in sent
    assert f"C{corpus['glycinate'].pk} |" in sent


def test_nothing_is_called_when_there_is_nothing_to_connect(db):
    source = Source.objects.create(name="A podcast", route=Source.Route.PODCAST)
    item = ContentItem.objects.create(
        source=source, external_id="x", title="x", content_hash="x",
        content_state=ContentItem.ContentState.TRANSCRIBED, published_at=timezone.now(),
    )
    segment = ContentSegment.objects.create(
        content_item=item, ordinal=1, text="t", start_char=0, end_char=1,
    )
    Claim.objects.create(content_item=item, segment=segment, kind="effect", text="t", quote="t")
    llm, transport = detecting()

    result = detection.detect(client=llm)

    assert result.skipped
    assert transport.calls == [], "one item cannot hold a cross-source development"


# ── Continuity ──────────────────────────────────────────────────────────────


def test_a_continuing_development_extends_its_signal_rather_than_duplicating_it(corpus):
    llm, _ = detecting(
        magnesium(corpus, supporting_claim_ids=[corpus["glycinate"].pk, corpus["threonate"].pk])
    )
    detection.detect(client=llm)
    code = Signal.objects.get().code

    llm, transport = detecting(
        magnesium(
            corpus,
            extends_code=code,
            title="A different title the operator never saw",
            supporting_claim_ids=[corpus["glycinate"].pk, corpus["doubt"].pk],
            contradicting_claim_ids=[],
        )
    )
    result = detection.detect(client=llm)

    assert result.extended == [code] and result.created == []
    signal = Signal.objects.get()
    assert signal.title == "Magnesium becomes a choice by goal", "the reviewed title is kept"
    sent = transport.calls[0]["messages"][0]["content"]
    assert f"{code}: Magnesium becomes a choice by goal" in sent
    assert signal.evidence.count() == 3


def test_an_approved_signals_wording_is_not_rewritten(corpus):
    llm, _ = detecting(magnesium(corpus))
    detection.detect(client=llm)
    signal = Signal.objects.get()
    signal.state = Signal.State.APPROVED
    signal.save(update_fields=["state"])

    llm, _ = detecting(magnesium(corpus, extends_code=signal.code, statement="Rewritten."))
    detection.detect(client=llm)

    signal.refresh_from_db()
    assert signal.statement.startswith("Creators now recommend")


def test_an_illustrative_signal_is_never_offered_for_extension(corpus):
    Signal.objects.create(
        code="SIG-2041", title="Seeded", domain_score=82, is_illustrative=True
    )
    llm, transport = detecting(magnesium(corpus, extends_code="SIG-2041"))

    detection.detect(client=llm)

    assert "SIG-2041" not in transport.calls[0]["messages"][0]["content"]
    assert Signal.objects.filter(is_illustrative=False).count() == 1
    assert Signal.objects.get(code="SIG-2041").evidence.count() == 0


def test_codes_continue_from_the_highest_existing(corpus):
    Signal.objects.create(code="SIG-3007", title="Old", domain_score=10)
    llm, _ = detecting(magnesium(corpus))

    result = detection.detect(client=llm)

    assert result.created == ["SIG-3008"]


def test_a_missing_creator_counts_as_its_show_not_as_nobody(corpus):
    """Some feeds carry no creator. Two episodes of one show are one voice."""
    ContentItem.objects.update(creator="")
    llm, _ = detecting(magnesium(corpus))

    detection.detect(client=llm)

    assert Signal.objects.get().creator_count == 2, "two different shows, two voices"


# ── One conversation, published twice (Mark, 19 Oct) ────────────────────────


def _same_conversation(duplicate: Claim, original: Claim) -> None:
    ContentItem.objects.filter(pk=duplicate.content_item_id).update(
        duplicate_of=original.content_item_id, duplicate_status="confirmed"
    )


def test_a_podcast_and_its_upload_cannot_make_a_development_alone(corpus):
    """The YouTube copy is not a second voice."""
    _same_conversation(corpus["threonate"], corpus["glycinate"])
    llm, _ = detecting(
        magnesium(corpus, supporting_claim_ids=[corpus["glycinate"].pk, corpus["threonate"].pk],
                  contradicting_claim_ids=[])
    )

    result = detection.detect(client=llm)

    assert result.dropped_single_source == 1
    assert not Signal.objects.exists()


def test_a_reupload_adds_no_item_creator_or_source_type(corpus):
    llm, _ = detecting(magnesium(corpus, contradicting_claim_ids=[]))
    detection.detect(client=llm)
    signal = Signal.objects.get()
    before = (signal.item_count, signal.creator_count, signal.source_count)
    assert before == (2, 2, 2)

    _same_conversation(corpus["threonate"], corpus["glycinate"])
    detection.rescore(signal, now=timezone.now())

    assert (signal.item_count, signal.creator_count, signal.source_count) == (1, 1, 1)
    assert "A single item is not cross-source evidence." in signal.confidence_reasons


def test_the_prompt_shows_the_conversation_not_the_copy(corpus):
    _same_conversation(corpus["threonate"], corpus["glycinate"])
    llm, transport = detecting()

    detection.detect(client=llm)

    sent = transport.calls[0]["messages"][0]["content"]
    original = corpus["glycinate"].content_item_id
    assert f"C{corpus['threonate'].pk} | I{original} |" in sent


def test_a_development_proposed_again_extends_the_open_one(corpus):
    """SIG-3006 and SIG-3007 on production were one development, created twice."""
    llm, _ = detecting(magnesium(corpus))
    detection.detect(client=llm)
    code = Signal.objects.get().code

    llm, _ = detecting(magnesium(corpus, title="Same thing, new name"))
    result = detection.detect(client=llm)

    assert result.created == []
    assert result.reused == [code]
    assert Signal.objects.count() == 1


def test_weekly_coverage_applies_both_thresholds_and_skips_duplicates(db, settings):
    settings.SIGNAL_COVERED_WEEK_MIN_ITEMS = 3
    settings.SIGNAL_COVERED_WEEK_MIN_SOURCES = 2
    now = timezone.now()
    one = Source.objects.create(name="One", route=Source.Route.PODCAST)
    two = Source.objects.create(name="Two", route=Source.Route.YOUTUBE)

    def collected(source, key, days_ago, **extra):
        return ContentItem.objects.create(
            source=source, external_id=key, title=key, content_hash=key,
            content_state=ContentItem.ContentState.TRANSCRIBED,
            gate_decision=ContentItem.GateDecision.RELEVANT,
            published_at=now - timedelta(days=days_ago), **extra,
        )

    first = collected(one, "a", 1)
    collected(one, "b", 2)
    collected(two, "c", 3)
    collected(two, "dupe", 3, duplicate_of=first, duplicate_status="confirmed")
    collected(one, "d", 9)  # last week: one source only

    rows = detection.weekly_coverage(now)

    assert (rows[0]["relevant_transcribed"], rows[0]["sources"], rows[0]["covered"]) == (3, 2, True)
    assert (rows[1]["relevant_transcribed"], rows[1]["covered"]) == (1, False)

"""Extraction — claims anchored to the spans that said them.

The assertion that matters most is the unanchored one. A model returning a
plausible claim with a quote that is not in the text is the failure mode this
pipeline has to survive, because PRD §6.6's citation gate ("quoted and
paraphrased source claims map to stored evidence spans") is downstream of here
and cannot fix what was never anchored.
"""
from __future__ import annotations

import pytest

from apps.enrichment import extraction
from apps.enrichment.llm.client import LLMClient
from apps.enrichment.models import Claim, ModelRun, Question
from apps.evidence.models import ContentItem, TranscriptArtifact
from apps.sources.models import Source

from .fakes import FakeTransport, respond

pytestmark = pytest.mark.django_db

DOMAIN = "Dietary supplements and nutraceuticals."

TRANSCRIPT = (
    "Creatine monohydrate is the most studied form. "
    "Five grams daily is enough, and there is no need for a loading phase. "
    "A small crossover trial found working memory improved under sleep restriction. "
    "Listeners keep asking which magnesium form is best for sleep."
)


@pytest.fixture
def item(db) -> ContentItem:
    source = Source.objects.create(name="Podcast", route=Source.Route.PODCAST)
    content = ContentItem.objects.create(
        source=source,
        external_id="ep-1",
        title="Creatine and cognition",
        content_hash="h1",
        content_state=ContentItem.ContentState.TRANSCRIBED,
    )
    TranscriptArtifact.objects.create(
        content_item=content,
        method=TranscriptArtifact.Method.PUBLISHER,
        text=TRANSCRIPT,
    )
    return content


def extraction_returning(claims=None, questions=None) -> tuple[LLMClient, FakeTransport]:
    payload = extraction.Extraction(claims=claims or [], questions=questions or [])
    transport = FakeTransport([respond(payload)])
    return LLMClient(transport=transport), transport


def test_segments_carry_offsets_into_the_original_text(item):
    """PRD §6.2: evidence references stable segment ids with character offsets,
    so a span survives re-segmentation at a different size later."""
    segments = extraction.build_segments(item)

    assert segments
    for segment in segments:
        assert TRANSCRIPT[segment.start_char:segment.end_char].strip() == segment.text


def test_segments_are_built_once(item):
    """Ordinals are referenced by claims — re-segmenting would orphan spans."""
    first = extraction.build_segments(item)
    second = extraction.build_segments(item)

    assert [s.pk for s in first] == [s.pk for s in second]


def test_an_anchored_claim_is_stored(item):
    extraction.build_segments(item)
    llm, _ = extraction_returning(
        claims=[
            extraction.ExtractedClaim(
                segment_ordinal=1,
                kind="dosage",
                subject="creatine monohydrate",
                text="Five grams daily is sufficient.",
                quote="Five grams daily is enough",
                is_cited=False,
                is_sponsored=False,
            )
        ]
    )

    result = extraction.extract(item, domain_pack=DOMAIN, client=llm)

    assert result.claims == 1
    assert result.dropped_unanchored == 0
    claim = Claim.objects.get()
    assert claim.segment.content_item_id == item.pk
    assert claim.quote in claim.segment.text


def test_a_claim_whose_quote_is_not_in_the_text_is_dropped(item):
    """The model invented the sentence. It cannot be evidenced, so it must not
    become a row — a claim nobody can locate is an assertion."""
    extraction.build_segments(item)
    llm, _ = extraction_returning(
        claims=[
            extraction.ExtractedClaim(
                segment_ordinal=1,
                kind="effect",
                subject="creatine",
                text="Creatine cures fatigue.",
                quote="Creatine completely eliminates all fatigue",  # never said
                is_cited=True,
                is_sponsored=False,
            )
        ]
    )

    result = extraction.extract(item, domain_pack=DOMAIN, client=llm)

    assert result.claims == 0
    assert result.dropped_unanchored == 1
    assert Claim.objects.count() == 0


def test_a_claim_naming_a_segment_that_does_not_exist_is_dropped(item):
    extraction.build_segments(item)
    llm, _ = extraction_returning(
        claims=[
            extraction.ExtractedClaim(
                segment_ordinal=999,
                kind="effect",
                subject="creatine",
                text="Something.",
                quote="Five grams daily is enough",
                is_cited=False,
                is_sponsored=False,
            )
        ]
    )

    result = extraction.extract(item, domain_pack=DOMAIN, client=llm)

    assert result.dropped_unanchored == 1
    assert Claim.objects.count() == 0


def test_whitespace_differences_do_not_drop_a_real_quote(item):
    """A model reflowing a line break is not the failure being guarded
    against — inventing the sentence is."""
    extraction.build_segments(item)
    llm, _ = extraction_returning(
        claims=[
            extraction.ExtractedClaim(
                segment_ordinal=1,
                kind="dosage",
                subject="creatine",
                text="Five grams daily.",
                quote="Five   grams\n daily is enough",
                is_cited=False,
                is_sponsored=False,
            )
        ]
    )

    result = extraction.extract(item, domain_pack=DOMAIN, client=llm)

    assert result.claims == 1
    assert result.dropped_unanchored == 0


def test_a_metadata_only_item_is_never_extracted(item):
    """Arch §7.1: metadata-only items are surfaced honestly as such and never
    treated as understood content. Extracting from a title would manufacture
    exactly the false confidence that rule prevents."""
    item.content_state = ContentItem.ContentState.METADATA
    item.save(update_fields=["content_state"])
    llm, transport = extraction_returning()

    result = extraction.extract(item, domain_pack=DOMAIN, client=llm)

    assert result.skipped == "metadata-only"
    assert transport.calls == []


def test_extraction_runs_once_ever(item):
    """Arch §10.3: "Extraction runs once per content item, ever." ~80% of LLM
    cost is here; re-running it per tenant would delete the multi-client
    economics the architecture is built on."""
    extraction.build_segments(item)
    claim = extraction.ExtractedClaim(
        segment_ordinal=1, kind="dosage", subject="creatine",
        text="Five grams.", quote="Five grams daily is enough",
        is_cited=False, is_sponsored=False,
    )
    transport = FakeTransport(
        [
            respond(extraction.Extraction(claims=[claim])),
            respond(extraction.Extraction(claims=[claim])),
        ]
    )
    llm = LLMClient(transport=transport)

    extraction.extract(item, domain_pack=DOMAIN, client=llm)
    second = extraction.extract(item, domain_pack=DOMAIN, client=llm)

    assert len(transport.calls) == 1
    assert second.skipped == "already extracted"


def test_questions_are_stored_separately_from_claims(item):
    """PRD §6.3 lists a recurring consumer question as its own signal type. A
    question asserts nothing and must never be weighed as though it did."""
    extraction.build_segments(item)
    last = item.segments.order_by("-ordinal").first()
    llm, _ = extraction_returning(
        questions=[
            extraction.ExtractedQuestion(
                segment_ordinal=last.ordinal,
                text="Which magnesium form is best for sleep?",
                topic="magnesium",
            )
        ]
    )

    result = extraction.extract(item, domain_pack=DOMAIN, client=llm)

    assert result.questions == 1
    assert Claim.objects.count() == 0
    assert Question.objects.get().topic == "magnesium"


def test_extraction_uses_the_middle_tier_and_records_the_run(item):
    extraction.build_segments(item)
    llm, _ = extraction_returning()

    extraction.extract(item, domain_pack=DOMAIN, client=llm)

    run = ModelRun.objects.get(purpose=ModelRun.Purpose.EXTRACTION)
    assert run.model == "claude-sonnet-5-5"
    assert run.prompt_version == extraction.PROMPT_VERSION

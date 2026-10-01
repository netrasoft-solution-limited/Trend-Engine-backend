"""Turning stored claims into a brief's body.

The rule worth guarding hardest is the sponsored one. A sponsor read is a
competitor's marketing copy; carrying it into a client brief as evidence of
what experts believe is the failure `Claim.is_sponsored` exists to prevent, and
it fails silently — the text reads exactly like editorial content, which is why
it got flagged in the first place.
"""
from __future__ import annotations

import pytest

from apps.enrichment.models import Claim, Question
from apps.evidence.models import ContentItem, ContentSegment, TranscriptArtifact
from apps.outputs import drafting
from apps.sources.models import Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def item():
    source = Source.objects.create(name="A podcast", route=Source.Route.PODCAST)
    content = ContentItem.objects.create(
        source=source,
        external_id="ep-1",
        title="Magnesium and sleep",
        creator="Dr Example",
        content_hash=ContentItem.hash_content("ep-1"),
        content_state=ContentItem.ContentState.TRANSCRIBED,
    )
    TranscriptArtifact.objects.create(
        content_item=content, method=TranscriptArtifact.Method.TADDY, text="…"
    )
    return content


def claim(
    item,
    *,
    kind="effect",
    sponsored=False,
    cited=False,
    subject="A subject",
    text="Magnesium glycinate is well tolerated",
    at=125.0,
):
    segment = ContentSegment.objects.create(
        content_item=item,
        ordinal=ContentSegment.objects.filter(content_item=item).count() + 1,
        text="Magnesium glycinate is well tolerated.",
        start_char=0,
        end_char=38,
        start_seconds=at,
    )
    return Claim.objects.create(
        content_item=item,
        segment=segment,
        kind=kind,
        subject=subject,
        text=text,
        quote="Magnesium glycinate is well tolerated.",
        is_sponsored=sponsored,
        is_cited=cited,
    )


def text_of(sections, heading_fragment):
    return next(s["text"] for s in sections if heading_fragment in s["heading"])


# ── The rule that must not fail quietly ─────────────────────────────────────


def test_sponsored_claims_are_excluded_by_default(item):
    claim(item, text="An editorial observation")
    claim(item, text="AG1 Pro contains five grams of creatine", sponsored=True)

    body = " ".join(s["text"] for s in drafting.sections_from_claims())

    assert "An editorial observation" in body
    assert "AG1 Pro" not in body, "a sponsor read must not read as evidence"


def test_sponsored_claims_can_be_asked_for_explicitly(item):
    claim(item, text="AG1 Pro launched", kind="market", sponsored=True)

    excluded = drafting.sections_from_claims()
    included = drafting.sections_from_claims(include_sponsored=True)

    assert not any("What moved" in s["heading"] for s in excluded)
    assert any("What moved" in s["heading"] for s in included)


# ── What every paragraph must carry ─────────────────────────────────────────


def test_each_paragraph_carries_the_quote_the_speaker_and_the_timestamp(item):
    claim(item, at=125.0)

    body = text_of(drafting.sections_from_claims(), "evidence says")

    assert "“Magnesium glycinate is well tolerated.”" in body, "verbatim quote"
    assert "Dr Example" in body, "who said it"
    assert "Magnesium and sleep" in body, "where"
    assert "2:05" in body, "when, so it can be checked against the recording"


def test_a_claim_with_no_timing_still_renders(item):
    """An article has no audio position. Dropping the claim would be worse than
    dropping the timestamp."""
    claim(item, at=None)

    body = text_of(drafting.sections_from_claims(), "evidence says")

    assert "Magnesium glycinate is well tolerated." in body


def test_study_backed_claims_lead_their_section(item):
    claim(item, subject="Zebra", text="An uncited observation", cited=False)
    claim(item, subject="Alpha", text="A study-backed finding", cited=True)

    body = text_of(drafting.sections_from_claims(), "evidence says")

    assert body.index("A study-backed finding") < body.index("An uncited observation")


def test_a_cited_claim_says_so(item):
    claim(item, cited=True)

    assert "cites a study" in text_of(drafting.sections_from_claims(), "evidence says")


# ── Structure ───────────────────────────────────────────────────────────────


def test_sections_follow_the_reading_order_not_the_database_order(item):
    claim(item, kind="dosage", subject="Dose")
    claim(item, kind="market", subject="Launch")
    claim(item, kind="mechanism", subject="Pathway")

    headings = [s["heading"] for s in drafting.sections_from_claims()]

    assert headings.index("What moved in the category") < headings.index("Why it works that way")
    assert headings.index("Why it works that way") < headings.index("Doses being discussed")


def test_an_empty_kind_produces_no_empty_section(item):
    claim(item, kind="effect")

    headings = [s["heading"] for s in drafting.sections_from_claims()]

    assert "Safety and interactions" not in headings


def test_open_questions_are_carried_when_there_are_any(item):
    claim(item)
    # Question.segment is NOT NULL: a question is anchored to where it was
    # asked, exactly as a claim is anchored to where it was said.
    anchored = claim(item, kind="safety")
    Question.objects.create(
        content_item=item,
        segment=anchored.segment,
        text="Which magnesium form actually helps sleep?",
        topic="magnesium",
    )

    headings = [s["heading"] for s in drafting.sections_from_claims()]

    assert "Questions being asked and not answered" in headings


def test_the_methodology_section_names_its_sources_and_its_limits(item):
    """PRD §6.6 requires methodology. Here it is also where the gap between
    what the system does today and what it will do is stated plainly."""
    claim(item)

    body = text_of(drafting.sections_from_claims(), "What this is based on")

    assert "Magnesium and sleep" in body, "the episode is listed"
    assert "sponsor" in body.lower(), "the exclusion is declared"
    assert "not yet built" in body, "the absence of scoring is admitted, not hidden"


def test_nothing_to_draft_from_still_returns_a_methodology_section(item):
    """Called with no claims it must not fabricate; the command refuses on an
    empty body rather than publishing a shell."""
    sections = drafting.sections_from_claims()

    assert [s["heading"] for s in sections] == ["What this is based on"]


def test_each_section_is_capped(item):
    for n in range(10):
        claim(item, text=f"Observation number {n:02d}")

    body = text_of(drafting.sections_from_claims(limit_per_section=3), "evidence says")

    assert sum(f"Observation number {n:02d}" in body for n in range(10)) == 3

"""Segment timing.

`ContentSegment.start_seconds` is what lets a stored claim answer "where in the
episode was this said". It comes from the transcript's anchors, which are sparse
— one per caption cue — so the mapping has to resolve a character offset that
falls between two anchors, and resolve it to the *earlier* one: the moment the
speech containing that character began.
"""
from __future__ import annotations

import pytest

from apps.enrichment.extraction import _seconds_at, build_segments
from apps.evidence.models import ContentItem, TranscriptArtifact
from apps.sources.models import Source

pytestmark = pytest.mark.django_db

ANCHORS = [[0.0, 0], [10.0, 100], [20.0, 200], [30.0, 300]]


@pytest.mark.parametrize(
    "offset,expected",
    [
        (0, 0.0),
        (50, 0.0),     # between the first two anchors → the earlier one
        (100, 10.0),   # exactly on an anchor
        (199, 10.0),
        (250, 20.0),
        (9999, 30.0),  # past the last anchor → the last one
    ],
)
def test_offsets_resolve_to_the_speech_that_contains_them(offset, expected):
    assert _seconds_at(ANCHORS, offset) == expected


def test_no_anchors_means_no_timing_rather_than_zero():
    """An article has no audio position. Reporting 0:00 would be a lie that
    looks like data; None is the honest answer and the column is nullable."""
    assert _seconds_at([], 42) is None


def test_segments_carry_timing_from_the_transcript():
    source = Source.objects.create(name="Timed source", route=Source.Route.YOUTUBE)
    item = ContentItem.objects.create(
        source=source,
        external_id="timed-1",
        title="A timed episode",
        content_hash=ContentItem.hash_content("timed-1"),
        content_state=ContentItem.ContentState.TRANSCRIBED,
    )
    text = ("Magnesium glycinate is well tolerated. " * 120).strip()
    TranscriptArtifact.objects.create(
        content_item=item,
        method=TranscriptArtifact.Method.APIFY_SUBTITLES,
        text=text,
        anchors=[[float(i), i * 40] for i in range(0, 120)],
    )

    segments = build_segments(item)

    assert len(segments) > 1, "the fixture must be long enough to split"
    assert all(s.start_seconds is not None for s in segments)
    # Time runs forward with the text.
    assert [s.start_seconds for s in segments] == sorted(s.start_seconds for s in segments)
    assert segments[0].start_seconds == 0.0


def test_untimed_transcripts_still_segment():
    """No anchors is the normal case for articles and papers — it must not stop
    segmentation, only leave the timing columns null."""
    source = Source.objects.create(name="Text source", route=Source.Route.RESEARCH)
    item = ContentItem.objects.create(
        source=source,
        external_id="text-1",
        title="A paper",
        content_hash=ContentItem.hash_content("text-1"),
        content_state=ContentItem.ContentState.TRANSCRIBED,
    )
    TranscriptArtifact.objects.create(
        content_item=item,
        method=TranscriptArtifact.Method.MANUAL,
        text="Zinc absorption is reduced by phytates. " * 100,
    )

    segments = build_segments(item)

    assert segments
    assert all(s.start_seconds is None for s in segments)

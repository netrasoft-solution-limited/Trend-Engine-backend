"""Turning connector rows into evidence.

Two properties carry real weight here. Idempotency, because the collection
schedule re-runs over overlapping windows by design and duplicated evidence
would inflate every count downstream. And content state, because an item
recorded as `TRANSCRIBED` when it has no transcript is an item extraction will
try to quote from.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.evidence.models import ContentItem, TranscriptArtifact
from apps.evidence.services.ingest import ingest_rows
from apps.sources.models import AcquisitionProvider, ProviderPolicyVersion, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def policy():
    provider = AcquisitionProvider.objects.create(
        kind=AcquisitionProvider.Kind.APIFY, name="Apify"
    )
    return ProviderPolicyVersion.objects.create(
        provider=provider,
        version="test",
        access_basis="Publicly available pages and published caption tracks.",
        approved_at=timezone.now(),
    )


@pytest.fixture
def source(policy):
    return Source.objects.create(
        name="Test YouTube feed", route=Source.Route.YOUTUBE, policy=policy
    )


def row(**overrides) -> dict:
    base = {
        "external_id": "vid-1",
        "title": "Magnesium forms compared",
        "url": "https://example.test/watch?v=vid-1",
        "creator": "A Channel",
        "description": "Show notes mentioning glycinate and threonate.",
        "transcript": "Magnesium glycinate is well tolerated. Five hundred milligrams is typical.",
        "transcript_anchors": [[0.0, 0], [4.5, 38]],
        "gate_metadata": {"duration": "10:02"},
        "raw_metadata": {"viewCount": 1234},
    }
    base.update(overrides)
    return base


def test_rows_become_evidence_with_provenance(source, policy):
    result = ingest_rows(
        source=source,
        policy=policy,
        rows=[row()],
        cost_per_item=Decimal("0.004"),
    )

    assert result.created == 1
    item = result.items[0]
    assert item.content_state == ContentItem.ContentState.TRANSCRIBED
    assert item.is_metadata_only is False
    assert item.policy == policy
    assert item.acquisition_route == "apify_subtitles"
    assert item.fetched_at is not None
    # Provider-specific fields stay in raw_metadata, out of the contract.
    assert item.raw_metadata["viewCount"] == 1234

    transcript = item.transcript
    assert transcript.anchors == [[0.0, 0], [4.5, 38]]
    assert transcript.cost_usd == Decimal("0.0040")


def test_the_same_rows_twice_create_nothing_new(source, policy):
    """Arch §6.3: the key is (source, external_id, content_hash).

    The collection schedule re-runs over overlapping windows on purpose, so
    this is the ordinary case, not an edge case.
    """
    first = ingest_rows(source=source, policy=policy, rows=[row()])
    second = ingest_rows(source=source, policy=policy, rows=[row()])

    assert (first.created, first.seen_before) == (1, 0)
    assert (second.created, second.seen_before) == (0, 1)
    assert ContentItem.objects.count() == 1
    assert TranscriptArtifact.objects.count() == 1


def test_changed_content_is_a_second_row_not_an_overwrite(source, policy):
    """A re-uploaded video with a different transcript is new evidence.

    It must not overwrite the first: a claim may already point at a segment of
    it, and rewriting the text under that claim would leave the quote anchored
    to something that is no longer there.
    """
    ingest_rows(source=source, policy=policy, rows=[row()])
    ingest_rows(
        source=source,
        policy=policy,
        rows=[row(transcript="An entirely different recording about zinc.")],
    )

    assert ContentItem.objects.filter(external_id="vid-1").count() == 2


def test_no_transcript_means_metadata_only(source, policy):
    """Arch §7.1: a metadata-only item surfaces honestly as such.

    Extraction skips it. Treating a rich description as good enough would mean
    quoting someone on what they said they would talk about.
    """
    result = ingest_rows(source=source, policy=policy, rows=[row(transcript=None)])

    item = result.items[0]
    assert item.content_state == ContentItem.ContentState.METADATA
    assert item.is_metadata_only is True
    assert result.metadata_only == 1
    assert not TranscriptArtifact.objects.filter(content_item=item).exists()


def test_a_row_without_identity_is_refused(source, policy):
    """No external_id means no idempotency key, so a re-run would duplicate it
    on every pass. Dropping it is the containable failure."""
    result = ingest_rows(
        source=source, policy=policy, rows=[row(external_id=""), row(external_id="vid-2")]
    )

    assert result.created == 1
    assert ContentItem.objects.count() == 1

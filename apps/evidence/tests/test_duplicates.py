"""One conversation, published more than once.

Mark's 19 October review: a podcast, its YouTube upload and clips from the same
conversation must not count as independent confirmation. These tests defend the
comparison (containment of the shorter transcript in the longer), the choice of
which item stays canonical, and the rule that a guess from metadata alone never
removes evidence.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.evidence.models import ContentItem, TranscriptArtifact
from apps.evidence.services import duplicates
from apps.sources.models import Source

pytestmark = pytest.mark.django_db

#: A two-hour conversation, compressed: 3,000 distinct words in order.
EPISODE = " ".join(f"word{i}" for i in range(3000))
CLIP = " ".join(f"word{i}" for i in range(1200, 1500))
UNRELATED = " ".join(f"other{i}" for i in range(3000))


# ── The comparison ──────────────────────────────────────────────────────────


def test_a_clip_is_contained_in_its_episode():
    """3% of the episode, but nearly all of the clip — containment, not overlap."""
    assert duplicates.containment(duplicates.shingles(CLIP), duplicates.shingles(EPISODE)) > 0.95


def test_an_unrelated_conversation_is_not():
    assert duplicates.containment(duplicates.shingles(UNRELATED), duplicates.shingles(EPISODE)) == 0


def test_punctuation_and_case_do_not_hide_a_reupload():
    captions = EPISODE.upper().replace(" ", ", ")

    assert duplicates.containment(duplicates.shingles(captions), duplicates.shingles(EPISODE)) == 1


# ── Against stored items ────────────────────────────────────────────────────


@pytest.fixture
def sources(db):
    return (
        Source.objects.create(name="Huberman Lab", route=Source.Route.PODCAST),
        Source.objects.create(name="Andrew Huberman", route=Source.Route.YOUTUBE),
    )


def item(source, key, *, text=None, title=None, days_ago=1, duration=None):
    created = ContentItem.objects.create(
        source=source,
        external_id=key,
        title=title or f"Episode {key}",
        creator=source.name,
        content_hash=key,
        published_at=timezone.now() - timedelta(days=days_ago),
        content_state=(
            ContentItem.ContentState.TRANSCRIBED if text else ContentItem.ContentState.METADATA
        ),
        gate_metadata={"duration": duration} if duration else {},
    )
    if text:
        TranscriptArtifact.objects.create(
            content_item=created, method=TranscriptArtifact.Method.TADDY, text=text
        )
    return ContentItem.objects.select_related("transcript").get(pk=created.pk)


def test_a_youtube_reupload_is_marked_the_same_conversation(sources):
    podcast, youtube = sources
    episode = item(podcast, "ep", text=EPISODE + " word3000")
    upload = item(youtube, "vid", text=EPISODE)

    match = duplicates.check(upload)

    upload.refresh_from_db()
    assert match.status == "confirmed" and "re-upload" in match.reason
    assert upload.duplicate_of == episode, "the longer transcript stays canonical"
    assert upload.is_duplicate and upload.canonical == episode
    assert not ContentItem.objects.get(pk=episode.pk).is_duplicate


def test_a_clip_points_at_its_episode(sources):
    podcast, youtube = sources
    episode = item(podcast, "ep", text=EPISODE)
    clip = item(youtube, "clip", text=CLIP)

    duplicates.check(clip)

    clip.refresh_from_db()
    assert clip.duplicate_of == episode and "clip" in clip.duplicate_reason


def test_an_episode_arriving_after_its_clip_takes_over(sources):
    podcast, youtube = sources
    clip = item(youtube, "clip", text=CLIP, days_ago=3)
    episode = item(podcast, "ep", text=EPISODE, days_ago=2)

    duplicates.check(episode)

    clip.refresh_from_db()
    episode.refresh_from_db()
    assert clip.duplicate_of == episode, "the clip is re-pointed, not the episode demoted"
    assert not episode.is_duplicate


def test_chains_resolve_to_the_root(sources):
    podcast, youtube = sources
    episode = item(podcast, "ep", text=EPISODE)
    upload = item(youtube, "vid", text=EPISODE[: len(EPISODE) - 50])
    duplicates.check(upload)
    clip = item(youtube, "clip", text=CLIP)

    duplicates.check(clip)

    clip.refresh_from_db()
    assert clip.canonical == episode


def test_an_unrelated_episode_is_left_alone(sources):
    podcast, youtube = sources
    item(podcast, "ep", text=EPISODE)
    other = item(youtube, "vid", text=UNRELATED)

    assert duplicates.check(other) is None
    other.refresh_from_db()
    assert other.duplicate_status == ""


def test_the_same_words_a_month_apart_are_not_compared(sources):
    podcast, youtube = sources
    item(podcast, "ep", text=EPISODE, days_ago=40)

    assert duplicates.check(item(youtube, "vid", text=EPISODE, days_ago=1)) is None


def test_without_a_transcript_it_is_only_ever_suspected(sources):
    """A guess from titles and durations is shown, never allowed to remove evidence."""
    podcast, youtube = sources
    item(podcast, "ep", title="Magnesium for sleep with Dr Smith", duration=3600)
    upload = item(youtube, "vid", title="Magnesium for sleep with Dr Smith", duration="60:10")

    match = duplicates.check(upload)

    upload.refresh_from_db()
    assert match.status == "suspected"
    assert upload.duplicate_status == "suspected"
    assert not upload.is_duplicate and upload.canonical == upload

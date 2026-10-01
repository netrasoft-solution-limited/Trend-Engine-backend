"""Walking the transcript fallback ladder.

The rungs themselves are tested against their vendors in
`apps/connectors/tests/`. What is tested here is the WALK: that it stops at the
first success, that it keeps going past a rung that cannot serve an item, and —
the one that would quietly destroy evidence if it were wrong — that it tells a
missing transcript apart from a broken one.

An item marked metadata-only because AssemblyAI was down for ten minutes stays
metadata-only forever, and extraction refuses it for the rest of its life. That
is the failure this file exists to prevent.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.evidence.models import ContentItem, TranscriptArtifact
from apps.evidence.services import transcripts
from apps.evidence.services.transcripts import (
    Failed,
    Status,
    Transcript,
    Unavailable,
    acquire,
)
from apps.sources.models import Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def podcast_item():
    source = Source.objects.create(name="A podcast", route=Source.Route.PODCAST)
    return ContentItem.objects.create(
        source=source,
        external_id="ep-1",
        title="Magnesium and sleep",
        url="https://example.test/ep1.mp3",
        content_hash=ContentItem.hash_content("ep-1"),
        content_state=ContentItem.ContentState.METADATA,
        gate_decision=ContentItem.GateDecision.RELEVANT,
        gate_metadata={"duration": 2700},
    )


@pytest.fixture
def rungs(monkeypatch):
    """Replace the registry so the walk is tested, not the vendors."""

    def install(**behaviours):
        registry = dict(transcripts.RUNGS)
        for name, behaviour in behaviours.items():
            registry[name] = behaviour
        monkeypatch.setattr(transcripts, "RUNGS", registry)

    return install


def succeeds(method, text="Magnesium glycinate is well tolerated.", cost="0"):
    def rung(item):
        return Transcript(
            text=text, anchors=[(0.0, 0)], method=method, quality=90, cost_usd=Decimal(cost)
        )

    return rung


def unavailable(reason="nothing here"):
    def rung(item):
        raise Unavailable(reason)

    return rung


def fails(reason="upstream 500"):
    def rung(item):
        raise Failed(reason)

    return rung


# ── The happy path ──────────────────────────────────────────────────────────


def test_the_first_rung_that_works_wins_and_the_rest_are_not_tried(podcast_item, rungs):
    """The ladder is ordered cheapest-first. Continuing past a success would
    pay for a transcript already in hand."""
    called = []

    def record(name, inner):
        def rung(item):
            called.append(name)
            return inner(item)

        return rung

    rungs(
        taddy=record("taddy", succeeds(TranscriptArtifact.Method.TADDY)),
        assemblyai=record("assemblyai", succeeds(TranscriptArtifact.Method.ASSEMBLYAI)),
    )

    outcome = acquire(podcast_item)

    assert outcome.succeeded
    assert outcome.method == TranscriptArtifact.Method.TADDY
    assert called == ["taddy"], "a later rung must not run once one succeeded"


def test_a_success_is_stored_and_the_item_becomes_transcribable(podcast_item, rungs):
    rungs(taddy=succeeds(TranscriptArtifact.Method.TADDY, cost="0.004"))

    acquire(podcast_item)
    podcast_item.refresh_from_db()

    artifact = podcast_item.transcript
    assert artifact.method == TranscriptArtifact.Method.TADDY
    assert artifact.anchors == [[0.0, 0]]
    assert artifact.cost_usd == Decimal("0.0040")
    assert podcast_item.content_state == ContentItem.ContentState.TRANSCRIBED
    assert podcast_item.is_metadata_only is False
    # Arch §7.2: the rung that produced it, recorded per item.
    assert podcast_item.acquisition_route == TranscriptArtifact.Method.TADDY


def test_the_walk_continues_past_a_rung_that_cannot_serve_the_item(podcast_item, rungs):
    rungs(
        taddy=unavailable("Taddy has no transcript for this episode"),
        assemblyai=succeeds(TranscriptArtifact.Method.ASSEMBLYAI),
    )

    outcome = acquire(podcast_item)

    assert outcome.method == TranscriptArtifact.Method.ASSEMBLYAI
    statuses = {r.step: r.status for r in outcome.rungs}
    assert statuses["taddy"] == Status.UNAVAILABLE
    assert statuses["assemblyai"] == Status.SUCCEEDED


# ── The distinction that matters ────────────────────────────────────────────


def test_an_item_nothing_can_transcribe_is_honestly_metadata_only(podcast_item, rungs):
    """Arch §7.1: a final answer, not a failure. Extraction refusing it from
    here on is correct."""
    rungs(taddy=unavailable(), assemblyai=unavailable())

    outcome = acquire(podcast_item)
    podcast_item.refresh_from_db()

    assert not outcome.succeeded
    assert not outcome.retryable
    assert podcast_item.content_state == ContentItem.ContentState.METADATA
    assert podcast_item.acquisition_route == "metadata_only"


def test_a_transient_failure_does_NOT_write_the_item_off(podcast_item, rungs):
    """The one that would quietly destroy evidence.

    AssemblyAI being down for ten minutes must not mark an item metadata-only
    for the rest of its life. Nothing has been learned about the item, so its
    state is left exactly as it was for the next pass.
    """
    rungs(taddy=unavailable(), assemblyai=fails("AssemblyAI timed out"))

    outcome = acquire(podcast_item)
    podcast_item.refresh_from_db()

    assert not outcome.succeeded
    assert outcome.retryable, "a broken rung must leave the item retryable"
    assert podcast_item.content_state == ContentItem.ContentState.METADATA
    assert podcast_item.acquisition_route != "metadata_only", (
        "the item must not be recorded as terminally metadata-only"
    )


def test_a_success_after_a_failure_is_still_a_success(podcast_item, rungs):
    rungs(taddy=fails("Taddy 502"), assemblyai=succeeds(TranscriptArtifact.Method.ASSEMBLYAI))

    outcome = acquire(podcast_item)

    assert outcome.succeeded
    assert outcome.retryable is False, "retryable is about ending WITHOUT a transcript"


def test_an_unconfigured_provider_does_not_write_the_item_off(podcast_item, rungs):
    """"No credential" / "no approved policy" / "paused at its cap" describe
    the DEPLOYMENT, not the item.

    Treating them as "no rung can ever serve this" would mark every item that
    arrived during a misconfiguration as metadata-only, permanently — and
    extraction would refuse them for the rest of their lives once it was fixed.
    """
    from apps.connectors.factory import ConnectorUnavailable

    def unconfigured(item):
        raise ConnectorUnavailable("AssemblyAI has no 'api_key' credential")

    import apps.evidence.services.transcripts as mod

    rungs(taddy=unavailable(), assemblyai=mod._assemblyai)
    # Drive the real rung so the factory raises for real.
    podcast_item.source.policy = None
    podcast_item.source.save()

    outcome = acquire(podcast_item)
    podcast_item.refresh_from_db()

    assert outcome.retryable, "a misconfiguration must leave the item retryable"
    assert podcast_item.acquisition_route != "metadata_only"


def test_an_unexpected_error_counts_as_transient(podcast_item, rungs):
    """An unrecognised exception is not evidence that the item has no
    transcript, so it must keep the item alive rather than bury it."""

    def explodes(item):
        raise ZeroDivisionError("something nobody predicted")

    rungs(taddy=explodes, assemblyai=unavailable())

    outcome = acquire(podcast_item)
    podcast_item.refresh_from_db()

    assert outcome.retryable
    assert podcast_item.acquisition_route != "metadata_only"
    assert "ZeroDivisionError" in next(
        r.detail for r in outcome.rungs if r.step == "taddy"
    )


# ── Idempotency and configuration ───────────────────────────────────────────


def test_an_item_that_already_has_a_transcript_is_left_alone(podcast_item, rungs):
    """Transcripts are the expensive artifact and the relation is one-to-one,
    so a duplicate dispatch must cost nothing rather than fail."""
    TranscriptArtifact.objects.create(
        content_item=podcast_item,
        method=TranscriptArtifact.Method.PUBLISHER,
        text="Already held.",
    )

    def must_not_run(item):
        raise AssertionError("no rung may run for an item that already has a transcript")

    rungs(taddy=must_not_run, assemblyai=must_not_run)

    outcome = acquire(podcast_item)

    assert outcome.succeeded
    assert outcome.method == TranscriptArtifact.Method.PUBLISHER
    assert TranscriptArtifact.objects.filter(content_item=podcast_item).count() == 1


def test_a_rung_named_in_a_chain_but_not_registered_is_skipped_not_fatal(
    podcast_item, monkeypatch
):
    """`chains.py` owns the ORDER and lists rungs nobody has written yet, so a
    step with no implementation must be recorded and stepped over."""
    registry = {
        step: rung for step, rung in transcripts.RUNGS.items() if step != "taddy"
    }
    registry["assemblyai"] = succeeds(TranscriptArtifact.Method.ASSEMBLYAI)
    monkeypatch.setattr(transcripts, "RUNGS", registry)

    outcome = acquire(podcast_item)

    assert outcome.succeeded
    assert outcome.method == TranscriptArtifact.Method.ASSEMBLYAI
    # The unregistered step is reported as skipped, not silently absent.
    taddy = next(r for r in outcome.rungs if r.step == "taddy")
    assert taddy.status == Status.SKIPPED
    assert "no implementation" in taddy.detail


def test_an_unknown_route_degrades_to_metadata_only(rungs):
    """A source added with a route nobody wrote a chain for should degrade,
    not stop the pipeline."""
    source = Source.objects.create(name="A feed", route=Source.Route.SOCIAL)
    item = ContentItem.objects.create(
        source=source,
        external_id="s-1",
        title="A post",
        content_hash=ContentItem.hash_content("s-1"),
    )

    outcome = acquire(item)
    item.refresh_from_db()

    assert not outcome.succeeded
    assert not outcome.retryable
    assert item.content_state == ContentItem.ContentState.METADATA


def test_the_chain_comes_from_the_items_route(podcast_item):
    """youtube and podcast get different ladders, from chains.py."""
    assert "taddy" in transcripts.chain_for(podcast_item)

    youtube = Source.objects.create(name="A channel", route=Source.Route.YOUTUBE)
    video = ContentItem.objects.create(
        source=youtube,
        external_id="v-1",
        title="A video",
        content_hash=ContentItem.hash_content("v-1"),
    )
    assert "apify_subtitles" in transcripts.chain_for(video)
    assert "taddy" not in transcripts.chain_for(video)


# ── Pricing an ASR call before making it ────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (2700, 2700.0),
        (2700.5, 2700.5),
        ("45:00", 2700.0),
        ("1:15:00", 4500.0),
        (None, None),
        ("unknown", None),
    ],
)
def test_duration_is_read_from_either_connectors_shape(raw, expected):
    """Taddy reports seconds, Apify reports MM:SS. Returning None rather than
    guessing means the cap is simply not enforced for that item, which beats
    pricing a three-hour episode as though it were four minutes."""
    item = ContentItem(gate_metadata={"duration": raw} if raw is not None else {})

    assert transcripts._duration_seconds(item) == expected

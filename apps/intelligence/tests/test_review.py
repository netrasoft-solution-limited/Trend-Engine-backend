"""The signal state machine (PRD §6.5).

The screen offers only the moves this allows, so these rules are what stop an
"Approve" button appearing where approval makes no sense.
"""
from __future__ import annotations

import pytest

from apps.enrichment.models import Claim
from apps.evidence.models import ContentItem, ContentSegment
from apps.intelligence import review
from apps.intelligence.models import Signal, SignalEvidence
from apps.operations.models import AuditEvent
from apps.sources.models import Source

pytestmark = pytest.mark.django_db
S = Signal.State


def signal(code="SIG-3001", **fields):
    return Signal.objects.create(code=code, title=code, domain_score=50, **fields)


def claim(key):
    source, _ = Source.objects.get_or_create(name="A podcast", route=Source.Route.PODCAST)
    item = ContentItem.objects.create(
        source=source, external_id=key, title=key, creator=key, content_hash=key,
        content_state=ContentItem.ContentState.TRANSCRIBED,
    )
    segment = ContentSegment.objects.create(
        content_item=item, ordinal=1, text=key, start_char=0, end_char=1
    )
    return Claim.objects.create(
        content_item=item, segment=segment, kind="effect", text=key, quote=key
    )


def test_a_candidate_can_be_approved_and_the_move_is_audited():
    s = signal()

    review.move(s, S.APPROVED, actor_label="abu")

    s.refresh_from_db()
    assert s.state == S.APPROVED and not s.awaiting_operator_judgment
    event = AuditEvent.objects.get(kind=AuditEvent.Kind.REVIEW)
    assert event.actor_label == "abu"
    assert event.context["from"] == S.CANDIDATE and event.context["to"] == S.APPROVED


def test_a_move_the_machine_does_not_allow_is_refused():
    s = signal(state=S.REJECTED)

    with pytest.raises(review.ReviewError, match="cannot move"):
        review.move(s, S.APPROVED, actor_label="abu")


def test_rejecting_needs_a_reason():
    with pytest.raises(review.ReviewError, match="Say why"):
        review.move(signal(), S.REJECTED, actor_label="abu")


def test_a_rejected_signal_can_be_reopened_with_a_reason():
    s = signal(state=S.REJECTED)

    review.move(s, S.CANDIDATE, actor_label="abu", reason="Rejected in error.")

    assert s.state == S.CANDIDATE


def test_a_seeded_signal_cannot_be_approved_into_a_brief():
    with pytest.raises(review.ReviewError, match="illustrative"):
        review.move(signal(is_illustrative=True), S.APPROVED, actor_label="abu")


def test_merging_moves_the_evidence_and_rejects_the_duplicate_with_a_note():
    keep, dupe = signal("SIG-3001"), signal("SIG-3002")
    shared, extra = claim("a"), claim("b")
    SignalEvidence.objects.create(signal=keep, claim=shared)
    SignalEvidence.objects.create(signal=dupe, claim=shared, role="contradicts")
    SignalEvidence.objects.create(signal=dupe, claim=extra)

    review.merge(dupe, keep, actor_label="abu")

    dupe.refresh_from_db()
    keep.refresh_from_db()
    assert dupe.state == S.REJECTED
    roles = dict(keep.evidence.values_list("claim_id", "role"))
    assert roles == {shared.pk: "supports", extra.pk: "supports"}, "the target keeps its own role"
    assert keep.scored_at is not None, "the merged signal is rescored"
    assert AuditEvent.objects.filter(context__reason="Merged into SIG-3001.").exists()


def test_a_signal_cannot_be_merged_into_itself_or_into_a_rejected_one():
    s, rejected = signal("SIG-3001"), signal("SIG-3002", state=S.REJECTED)

    with pytest.raises(review.ReviewError, match="itself"):
        review.merge(s, s, actor_label="abu")
    with pytest.raises(review.ReviewError, match="reopen it"):
        review.merge(s, rejected, actor_label="abu")


def test_observations_attach_by_number_and_are_audited():
    from apps.evidence.models import Observation

    s = signal()
    o = Observation.objects.create(
        url="https://www.reddit.com/r/PCOS/x", title="Protein powder bloating",
        source_type="reddit", observed_at="2026-10-20", entered_by="abu",
    )

    attached, missing = review.attach_observations(
        s, review.parse_observation_ids(f"O{o.pk}, O999999"), role="supports", actor_label="abu"
    )

    assert attached == [o.pk] and missing == [999999]
    assert s.observations.get().added_by == "abu"
    assert AuditEvent.objects.filter(message__contains="attached observation").exists()

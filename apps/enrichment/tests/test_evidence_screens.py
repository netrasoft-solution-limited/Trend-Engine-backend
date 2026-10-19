"""The Ingestion Runs screen and the item page behind it.

Mark's 8 October review: collection timestamps alone do not show transcript
coverage, failures, or freshness. What is worth defending here:

  · the numbers are right — in particular cost, where joining claims and model
    runs in one query would multiply each sum by the other's row count
  · a failure says why, in its own words
  · every claim links back to the moment it was said
  · any signed-in operator can read it; nobody else can
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.enrichment.models import Claim, ModelRun
from apps.evidence.models import ContentItem, ContentSegment, TranscriptArtifact
from apps.ingestion.models import IngestionRun
from apps.operations.models import OperatorUser
from apps.sources.models import Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(db):
    """A plain Operator, not a Platform Admin — reviewing evidence is their job."""
    return OperatorUser.objects.create_user(
        email="operator@netrasoft.test", password="x" * 24, name="Operator"
    )


@pytest.fixture
def run(db):
    source = Source.objects.create(name="Huberman Lab", route=Source.Route.YOUTUBE)
    return IngestionRun.objects.create(
        source=source,
        status=IngestionRun.Status.SUCCEEDED,
        items_collected=3,
        items_created=2,
        items_seen_before=1,
    )


def _item(run, external_id, **fields):
    return ContentItem.objects.create(
        source=run.source,
        run=run,
        external_id=external_id,
        title=f"Episode {external_id}",
        content_hash=ContentItem.hash_content(external_id),
        **fields,
    )


@pytest.fixture
def transcribed(run):
    """Relevant, transcribed for $0.15, three claims, two model calls."""
    item = _item(
        run,
        "vid-1",
        url="https://www.youtube.com/watch?v=abc123",
        gate_decision=ContentItem.GateDecision.RELEVANT,
        gate_reason="Discusses magnesium forms and sleep.",
        content_state=ContentItem.ContentState.TRANSCRIBED,
        transcript_attempts=[
            {
                "at": "2026-10-12T08:00:00+00:00",
                "method": "assemblyai",
                "chars": 900,
                "cost_usd": "0.15",
                "outcome": "transcribed",
                "steps": [
                    {"step": "creator_captions", "status": "unavailable", "detail": "no captions"},
                    {"step": "assemblyai", "status": "succeeded", "detail": ""},
                ],
            }
        ],
    )
    TranscriptArtifact.objects.create(
        content_item=item,
        method=TranscriptArtifact.Method.ASSEMBLYAI,
        text="…",
        cost_usd=Decimal("0.15"),
    )
    for ordinal, seconds in enumerate((441.0, 506.0, 765.0), start=1):
        segment = ContentSegment.objects.create(
            content_item=item, ordinal=ordinal, text="t", start_char=0, end_char=1,
            start_seconds=seconds,
        )
        Claim.objects.create(
            content_item=item, segment=segment, kind="effect",
            text=f"Claim {ordinal}", quote=f"quote {ordinal}", is_cited=ordinal == 1,
        )
    for purpose, cost in (("relevance_gate", "0.001"), ("extraction", "0.040")):
        ModelRun.objects.create(
            purpose=purpose, outcome="succeeded", model="m", prompt_version="p",
            content_item=item, cost_usd=Decimal(cost),
        )
    return item


@pytest.fixture
def rejected(run):
    item = _item(
        run,
        "vid-2",
        gate_decision=ContentItem.GateDecision.REJECTED,
        gate_reason="A cooking episode.",
    )
    ModelRun.objects.create(
        purpose="relevance_gate", outcome="succeeded", model="m", prompt_version="p",
        content_item=item, cost_usd=Decimal("0.001"),
    )
    return item


# ── Who can read it ─────────────────────────────────────────────────────────


def test_a_plain_operator_can_read_the_runs(client, operator, run):
    client.force_login(operator)

    assert client.get(reverse("ops-runs")).status_code == 200


def test_an_anonymous_visitor_cannot(client, run):
    assert client.get(reverse("ops-runs")).status_code in (302, 403)


# ── The runs list ───────────────────────────────────────────────────────────


def test_the_runs_list_counts_and_costs_each_run(client, operator, transcribed, rejected):
    client.force_login(operator)

    row = client.get(reverse("ops-runs")).context["runs"][0]

    assert (row["relevant"], row["rejected"], row["transcribed"]) == (1, 1, 1)
    # 0.15 transcript + 0.001 + 0.040 + 0.001 model calls — and not multiplied
    # by the three claims, which a single join would do.
    assert row["cost"] == Decimal("0.192")


def test_coverage_is_transcribed_over_relevant(client, operator, transcribed, rejected, run):
    _item(run, "vid-3", gate_decision=ContentItem.GateDecision.RELEVANT,
          content_state=ContentItem.ContentState.METADATA)
    client.force_login(operator)

    coverage = client.get(reverse("ops-runs")).context["coverage"]

    assert coverage["relevant"] == 2
    assert coverage["transcribed"] == 1
    assert coverage["metadata_only"] == 1
    assert coverage["coverage_pct"] == 50
    assert coverage["claims"] == 3


def test_a_failed_run_says_why(client, operator, run):
    IngestionRun.objects.create(
        source=run.source, status=IngestionRun.Status.FAILED, blocking=True,
        error="no rss_url or series_name",
    )
    client.force_login(operator)

    page = client.get(reverse("ops-runs")).content.decode()

    assert "needs attention" in page
    assert "no rss_url or series_name" in page


def test_the_list_can_be_narrowed_to_one_source(client, operator, run):
    other = Source.objects.create(name="Another show", route=Source.Route.PODCAST)
    IngestionRun.objects.create(source=other)
    client.force_login(operator)

    rows = client.get(reverse("ops-runs"), {"source": other.pk}).context["runs"]

    assert [r["obj"].source for r in rows] == [other]


# ── One run ─────────────────────────────────────────────────────────────────


def test_a_run_lists_its_items_with_their_decision_and_cost(
    client, operator, run, transcribed, rejected
):
    client.force_login(operator)

    response = client.get(reverse("ops-run", args=[run.pk]))
    items = {i.external_id: i for i in response.context["items"]}

    assert items["vid-1"].claim_count == 3
    assert items["vid-1"].total_cost == Decimal("0.191")
    assert items["vid-2"].total_cost == Decimal("0.001")
    page = response.content.decode()
    assert "A cooking episode." in page
    assert "3 found · 2 new" in page


# ── One item ────────────────────────────────────────────────────────────────


def test_an_item_shows_every_route_tried_not_just_the_winner(client, operator, transcribed):
    client.force_login(operator)

    page = client.get(reverse("ops-item", args=[transcribed.pk])).content.decode()

    assert "creator_captions" in page and "no captions" in page
    assert "Discusses magnesium forms and sleep." in page


def test_each_claim_links_to_the_moment_it_was_said(client, operator, transcribed):
    client.force_login(operator)

    response = client.get(reverse("ops-item", args=[transcribed.pk]))
    first = response.context["claims"][0]

    assert first.at == "7:21"
    assert first.link == "https://www.youtube.com/watch?v=abc123&t=441s"
    assert "&amp;t=441s" in response.content.decode()


def test_an_item_with_no_transcript_says_claims_need_one(client, operator, rejected):
    client.force_login(operator)

    page = client.get(reverse("ops-item", args=[rejected.pk])).content.decode()

    assert "Claims are only extracted from a transcript." in page


# ── Deep links ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "seconds", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc", 75.9, "https://www.youtube.com/watch?v=abc&t=75s"),
        ("https://youtu.be/abc", 75, "https://youtu.be/abc?t=75s"),
        # No common convention for podcast pages: the plain URL, timestamp shown beside it.
        ("https://example.test/ep1.mp3", 75, "https://example.test/ep1.mp3"),
        ("https://www.youtube.com/watch?v=abc", None, "https://www.youtube.com/watch?v=abc"),
        ("", 75, ""),
    ],
)
def test_link_at(url, seconds, expected):
    assert ContentItem(url=url).link_at(seconds) == expected


def test_an_item_says_when_it_is_the_same_conversation_as_another(
    client, operator, transcribed, run
):
    upload = _item(
        run, "vid-9", content_state=ContentItem.ContentState.TRANSCRIBED,
        duplicate_of=transcribed, duplicate_status="confirmed",
        duplicate_reason="re-upload: 98% of the shorter transcript appears in the longer",
    )
    client.force_login(operator)

    page = client.get(reverse("ops-item", args=[upload.pk])).content.decode()
    original = client.get(reverse("ops-item", args=[transcribed.pk])).content.decode()

    assert "Same conversation as" in page and "98%" in page
    assert "adds no item, creator or source type" in page
    assert "Also published as" in original

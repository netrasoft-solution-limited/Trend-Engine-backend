"""TikTok, Instagram and Reddit into the evidence pipeline (Mark, 31 Oct).

Defended: only the research window is kept; the same post found by two topic
streams is one item; every collection keeps its own reading of the counts;
Reddit arrives as text; Instagram is read from caption and comments only, and
says so.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.connectors.providers.virlo import VirloConnector
from apps.evidence import collection
from apps.evidence.models import ContentItem, MetricSnapshot, TranscriptArtifact
from apps.evidence.services import transcripts
from apps.evidence.services.ingest import ingest_rows
from apps.sources.models import AcquisitionProvider, ProviderPolicyVersion, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def policy():
    provider = AcquisitionProvider.objects.create(kind=AcquisitionProvider.Kind.VIRLO,
                                                  name="Virlo")
    return ProviderPolicyVersion.objects.create(
        provider=provider, version="t", access_basis="Virlo raw API.", approved_at=timezone.now()
    )


def social(name, platform, policy):
    return Source.objects.create(name=name, route=Source.Route.SOCIAL, policy=policy,
                                 config={"platform": platform, "queries": ["protein"]})


def row(video_id, *, days_old=3, views=100, platform="tiktok"):
    return {
        "external_id": f"{platform}:{video_id}", "platform": platform,
        "title": f"Video {video_id}", "url": f"https://www.tiktok.com/@a/video/{video_id}",
        "creator": "@a", "published_at": timezone.now() - timedelta(days=days_old),
        "description": "Protein first #protein", "transcript": None,
        "engagement": {"views": views, "likes": 5, "comments": None, "shares": 1, "saves": 0},
        "route": "virlo_tiktok",
    }


class FakeVirlo:
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    @property
    def cost_usd(self):
        return Decimal("0.01") * self.calls

    def search(self, platform, query, pages=2):
        self.calls += pages
        return self.rows


def test_collection_keeps_only_the_research_window(policy, monkeypatch):
    fake = FakeVirlo([row("new"), row("old", days_old=45), row("new")])
    monkeypatch.setattr("apps.connectors.factory.virlo_for", lambda s, max_calls=0: fake)
    source = social("TikTok — protein", "tiktok", policy)

    rows = collection.collect(source)

    assert [r["external_id"] for r in rows] == ["tiktok:new"]
    assert source.collection_cost_usd == Decimal("0.02")


def test_one_post_found_by_two_streams_is_one_item_with_two_readings(policy):
    first = social("TikTok — protein", "tiktok", policy)
    second = social("TikTok — digestion", "tiktok", policy)

    a = ingest_rows(source=first, policy=policy, rows=[row("7", views=100)])
    b = ingest_rows(source=second, policy=policy, rows=[row("7", views=250)])

    assert a.created == 1 and b.created == 0 and b.seen_before == 1
    assert ContentItem.objects.count() == 1
    readings = list(MetricSnapshot.objects.values_list("views", flat=True))
    assert readings == [100, 250]


def test_a_reddit_post_arrives_transcribed_as_its_own_words(policy):
    source = social("Reddit — urolithin", "reddit", policy)
    post = {**row("1xi5hy7", platform="reddit"), "transcript": "Title\n\nBody",
            "engagement": {}}

    ingest_rows(source=source, policy=policy, rows=[post],
                transcript_method=TranscriptArtifact.Method.POST_TEXT)

    item = ContentItem.objects.get()
    assert item.content_state == ContentItem.ContentState.TRANSCRIBED
    assert item.transcript.method == "post_text" and item.platform == "reddit"
    assert not MetricSnapshot.objects.exists()


def test_instagram_is_read_from_caption_and_comments_and_labelled(policy, monkeypatch):
    source = social("Instagram — protein", "instagram", policy)
    ingest_rows(source=source, policy=policy, rows=[row("DAb", platform="instagram")])
    item = ContentItem.objects.get()

    class Comments(VirloConnector):
        def __init__(self):
            self.calls = 1

        def comments(self, platform, video_id, pages=1):
            return [{"text": "Does it upset your stomach?"}]

    monkeypatch.setattr(transcripts, "_virlo", lambda item: Comments())
    outcome = transcripts.acquire(item)

    item.refresh_from_db()
    assert outcome.method == "caption_comments"
    assert item.transcript.text.startswith("[Caption and viewer comments only.")
    assert "Does it upset your stomach?" in item.transcript.text
    assert [r.step for r in outcome.rungs] == ["virlo_transcript", "caption_comments"]



def test_virlo_stops_at_its_monthly_cap(policy, monkeypatch):
    from apps.ingestion.models import IngestionRun

    policy.provider.monthly_cap_usd = Decimal("1.00")
    policy.provider.save()
    source = social("TikTok — protein", "tiktok", policy)
    IngestionRun.objects.create(source=source, status="succeeded", cost_usd=Decimal("0.99"))
    monkeypatch.setattr("apps.connectors.factory.virlo_for",
                        lambda s, max_calls=0: pytest.fail("called past the cap"))

    with pytest.raises(collection.NotCollectable, match="monthly cap"):
        collection.collect(source)

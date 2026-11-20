"""The trend brief names its channel gaps and Instagram's caption-only limit."""
from __future__ import annotations

import pytest

from apps.evidence.models import ContentItem, TranscriptArtifact
from apps.evidence.services.ingest import ingest_rows
from apps.evidence.tests.test_social import policy, row, social  # noqa: F401, F811

pytestmark = pytest.mark.django_db


def test_a_brief_names_its_channel_gaps_and_the_caption_only_limit(policy):  # noqa: F811
    from apps.outputs.briefing import _source_line, coverage_gaps

    source = social("Instagram — protein", "instagram", policy)
    ingest_rows(source=source, policy=policy, rows=[row("DAb", platform="instagram")])
    item = ContentItem.objects.get()
    TranscriptArtifact.objects.create(content_item=item, method="caption_comments", text="x")

    gaps = coverage_gaps([item])

    assert "no collected item from podcasts, YouTube, TikTok or Reddit is cited" in gaps
    assert "captions and comments only" in gaps
    assert "Monthly keyword volumes need a paid keyword tool" in gaps
    line = _source_line(ContentItem.objects.get())
    assert line.endswith("— Instagram (caption and comments only; speech not transcribed)")


@pytest.mark.parametrize("caption", [
    "Loving these gummies #uropartner", "Skin glow #gifted", "Found it #TikTokShop",
    "Day 3 #kucibrandambassador",
])
def test_brand_partner_and_shop_tags_mark_a_post_as_promotional(caption):
    from apps.evidence.models import promotion_reason

    assert promotion_reason(caption)


def test_an_ordinary_caption_is_not_promotional():
    from apps.evidence.models import promotion_reason

    assert not promotion_reason("My partner and I tried mushrooms #longevity")



@pytest.mark.django_db
def test_a_channel_that_stopped_collecting_is_stated_with_its_dates():
    """20 Nov: Reddit returned nothing from 15 Nov while every run succeeded."""
    from datetime import UTC, datetime

    from apps.ingestion.models import IngestionRun
    from apps.outputs.briefing import collection_outages
    from apps.sources.models import Source

    source = Source.objects.create(name="Reddit — x", route="social",
                                   config={"platform": "reddit", "queries": ["x"]})
    for day, items in ((8, 20), (15, 0), (19, 0)):
        run = IngestionRun.objects.create(source=source, status="succeeded", items_collected=items)
        IngestionRun.objects.filter(pk=run.pk).update(
            started_at=datetime(2026, 11, day, 18, 0, tzinfo=UTC))

    [line] = collection_outages()

    assert "Reddit collection has returned no posts since 15 Nov 2026" in line
    assert "collected up to 8 Nov 2026" in line

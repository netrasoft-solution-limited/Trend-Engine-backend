"""Virlo, without Virlo, and the Reddit Actor, without Apify.

The rows are trimmed from Virlo's published examples (dev.virlo.ai/docs/raw)
and from a live 10-post run of `shahab_adl/reddit-scraper-no-login` on 1 Nov.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from apps.connectors.providers.apify import REDDIT, ApifyActorConnector
from apps.connectors.providers.virlo import (
    VirloBudgetReached,
    VirloConnector,
    VirloError,
    VirloUnsupported,
)
from apps.connectors.tests.test_apify import FakePolicy, FakeSource

TIKTOK = {
    "id": "7491823365019208974",
    "platform": "tiktok",
    "url": "https://www.tiktok.com/@mealprepmia/video/7491823365019208974",
    "caption": "3 protein breakfasts under 5 minutes #protein\nmore below",
    "hashtags": ["protein"],
    "published_at": "2026-10-14T13:22:05Z",
    "duration_seconds": 38,
    "author": {"handle": "mealprepmia", "display_name": "Mia", "verified": False},
    "stats": {"views": 418200, "likes": 31540, "comments": 212, "shares": 1890, "saves": 9620},
}


def virlo(*responses, max_calls=0):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    connector = VirloConnector(
        source=FakeSource(), policy=FakePolicy(), api_key="virlo_tkn_x",
        max_calls=max_calls, transport=httpx.MockTransport(handler), sleep=lambda s: None,
    )
    connector.seen = seen
    return connector


def page(*videos, more=False):
    return httpx.Response(200, json={
        "data": list(videos),
        "pagination": {"next_cursor": "c2" if more else None, "has_more": more},
    })


def test_search_normalises_a_video_and_follows_two_pages():
    connector = virlo(page(TIKTOK, more=True), page({**TIKTOK, "id": "2"}))

    rows = connector.search("tiktok", "protein breakfast", pages=2)

    assert [r["external_id"] for r in rows] == ["tiktok:7491823365019208974", "tiktok:2"]
    row = rows[0]
    assert row["title"] == "3 protein breakfasts under 5 minutes #protein"
    assert row["creator"] == "@mealprepmia"
    assert row["published_at"].isoformat() == "2026-10-14T13:22:05+00:00"
    assert row["engagement"]["views"] == 418200
    assert connector.seen[1].url.params["cursor"] == "c2"
    assert connector.seen[0].headers["Authorization"] == "Bearer virlo_tkn_x"
    assert connector.calls == 2 and connector.cost_usd == Decimal("0.02")


def test_the_call_ceiling_is_enforced_before_the_call():
    connector = virlo(page(TIKTOK, more=True), max_calls=1)

    with pytest.raises(VirloBudgetReached):
        connector.search("tiktok", "q", pages=3)

    assert len(connector.seen) == 1


def test_an_empty_balance_blocks_rather_than_retries():
    connector = virlo(httpx.Response(402, json={"code": "insufficient_credits"}))

    with pytest.raises(VirloError) as raised:
        connector.search("tiktok", "q")

    assert raised.value.blocking and connector.calls == 0


def test_a_rate_limit_waits_once_then_continues():
    connector = virlo(httpx.Response(429, json={"retry_after": 2}), page(TIKTOK))

    assert len(connector.search("tiktok", "q", pages=1)) == 1


def test_instagram_has_no_transcript_route():
    with pytest.raises(VirloUnsupported):
        virlo(page()).transcript("instagram", "DAbC12xYz9Q")


def test_a_tiktok_transcript_keeps_its_timing():
    connector = virlo(httpx.Response(200, json={"data": {
        "has_transcript": True, "text": "Hi. Protein first.",
        "segments": [{"start_seconds": 0.0, "text": "Hi."},
                     {"start_seconds": 1.5, "text": "Protein first."}],
    }}))

    text, anchors = connector.transcript("tiktok", "1")

    assert text == "Hi. Protein first." and anchors == [[0.0, 0], [1.5, 4]]


def test_reddit_posts_arrive_as_text_with_their_id_prefixed():
    row = {"type": "post", "id": "1xi5hy7", "subreddit": "Supplements", "author": "a",
           "title": "Mitopure Urolithin a issues", "text": "Has anyone had stomach upset?",
           "url": "https://www.reddit.com/r/Supplements/comments/1xi5hy7/x/",
           "createdAt": "2026-10-29T00:29:51+00:00"}
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json=[row])

    connector = ApifyActorConnector(source=FakeSource(), policy=FakePolicy(), token="tok",
                                    config=REDDIT, transport=httpx.MockTransport(handler))
    [out] = connector.search_posts(["urolithin a"], since="2026-10-02")

    assert out["external_id"] == "reddit:1xi5hy7"
    assert out["transcript"] == "Mitopure Urolithin a issues\n\nHas anyone had stomach upset?"
    assert out["route"] == "reddit_post"
    # The token is a header, never in a URL a log line would print.
    assert "tok" not in str(seen[0].url)
    assert seen[0].headers["Authorization"] == "Bearer tok"

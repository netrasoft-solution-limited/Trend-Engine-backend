"""The Apify connector, without Apify.

The row fixture below is trimmed from a real `streamers/youtube-scraper`
response, so the field names are the ones the Actor actually emits rather than
the ones it would be convenient for it to emit. That is the whole risk this
connector carries: the field map is the only thing standing between a schema
change at the vendor and evidence with silently empty columns.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from apps.connectors.base import Capability, ConnectorPolicyError, FetchPlan, ItemRef
from apps.connectors.providers.apify import YOUTUBE, ApifyActorConnector, ApifyError

ACTOR_ROW = {
    "id": "-kEb_4XKveA",
    "title": "Creatine Supplements - Who benefits most?",
    "url": "https://www.youtube.com/watch?v=-kEb_4XKveA",
    "channelName": "Christy Risinger, MD",
    "date": "2026-02-11T14:00:07.000Z",
    "text": "References: https://pubmed.ncbi.nlm.nih.gov/12345678/",
    "duration": "12:41",
    "viewCount": 48210,
    "likes": 1902,
    "commentsCount": 211,
    "numberOfSubscribers": 320000,
    "hashtags": ["creatine"],
    "subtitles": [
        {
            "language": "en",
            "srt": (
                "1\n00:00:01,000 --> 00:00:04,000\nCreatine monohydrate is the most studied form.\n"
                "\n2\n00:00:04,000 --> 00:00:07,000\nFive grams a day is enough for most people.\n"
            ),
        }
    ],
}


class FakeSource:
    pk = 1

    def __repr__(self) -> str:
        return "<Source YouTube>"


class FakePolicy:
    is_live = True


def connector_returning(*responses) -> ApifyActorConnector:
    """A connector wired to canned HTTP responses instead of Apify.

    Keeps the last request on `.seen`, so a test can assert on what was sent
    as well as on what came back.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        handler.seen = request
        return responses[min(handler.calls, len(responses) - 1)]

    handler.calls = 0

    connector = ApifyActorConnector(
        source=FakeSource(),
        policy=FakePolicy(),
        token="tok",
        transport=httpx.MockTransport(handler),
    )
    connector.handler = handler
    return connector


def rows(*payload) -> httpx.Response:
    return httpx.Response(200, json=list(payload))


@pytest.fixture
def ok_connector() -> ApifyActorConnector:
    return connector_returning(rows(ACTOR_ROW))


# ── The contract ────────────────────────────────────────────────────────────


def test_no_policy_no_run():
    """PRD §7.2, enforced at construction so there is no path around it."""
    with pytest.raises(ConnectorPolicyError):
        ApifyActorConnector(source=FakeSource(), policy=None, token="tok")


def test_a_missing_token_fails_loudly_rather_than_anonymously():
    with pytest.raises(ValueError, match="APIFY_TOKEN"):
        ApifyActorConnector(source=FakeSource(), policy=FakePolicy(), token="")


def test_cost_is_known_before_the_run(ok_connector):
    """Arch §10.2: caps are enforced ahead of a run, not reconciled after."""
    plan = FetchPlan(
        refs=[ItemRef(source_id=1, external_item_id=str(n)) for n in range(25)],
        want={Capability.TRANSCRIPT},
    )
    estimate = ok_connector.estimate_cost(plan)

    assert estimate.currency == "USD"
    assert estimate.amount == Decimal("0.004") * 25
    assert estimate.units["actor"] == YOUTUBE.actor


# ── Normalisation ───────────────────────────────────────────────────────────


def test_actor_fields_map_onto_the_evidence_contract(ok_connector):
    (row,) = ok_connector.search(["creatine"])

    assert row["external_id"] == "-kEb_4XKveA"
    assert row["title"] == "Creatine Supplements - Who benefits most?"
    assert row["creator"] == "Christy Risinger, MD"
    assert row["published_at"].year == 2026
    assert row["published_at"].tzinfo is not None
    assert "pubmed" in row["description"]


def test_subtitles_become_quotable_prose_with_timing(ok_connector):
    (row,) = ok_connector.search(["creatine"])

    assert row["transcript"] == (
        "Creatine monohydrate is the most studied form. "
        "Five grams a day is enough for most people."
    )
    # The second anchor points at the first character of the second cue, not
    # at the space joining them — which is what makes a segment's timing the
    # moment that speech began rather than a moment earlier.
    first, second = row["transcript_anchors"]
    assert first == (1.0, 0)
    assert second[0] == 4.0
    assert row["transcript"][second[1]:].startswith("Five grams")


def test_the_gate_sees_metadata_and_nothing_else(ok_connector):
    """Arch §6.2: the gate reads metadata only — never the transcript, because
    fetching and reading that is the expensive step the gate exists to avoid."""
    (row,) = ok_connector.search(["creatine"])

    assert row["gate_metadata"] == {
        "duration": "12:41",
        "hashtags": ["creatine"],
        "channel_subscribers": 320000,
    }
    assert "transcript" not in row["gate_metadata"]


def test_provider_fields_stay_in_raw_metadata(ok_connector):
    (row,) = ok_connector.search(["creatine"])

    assert row["raw_metadata"]["viewCount"] == 48210
    assert row["engagement"] == {"views": 48210, "likes": 1902, "comments": 211}


def test_a_video_without_captions_is_metadata_only():
    connector = connector_returning(rows({**ACTOR_ROW, "subtitles": []}))

    (row,) = connector.search(["creatine"])
    assert row["transcript"] is None

    result = connector.fetch_content(ItemRef(source_id=1, external_item_id="x", url="http://x"))
    assert result.is_metadata_only is True
    assert result.route == "metadata_only"


def test_an_unparseable_date_does_not_lose_the_item():
    """A malformed timestamp costs the ordering, not the evidence."""
    connector = connector_returning(rows({**ACTOR_ROW, "date": "sometime last Tuesday"}))

    (row,) = connector.search(["creatine"])
    assert row["published_at"] is None
    assert row["external_id"] == "-kEb_4XKveA"


# ── Failure classification ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,blocking",
    [(401, True), (402, True), (403, True), (429, False), (500, False), (504, False)],
)
def test_auth_and_quota_failures_block_while_the_rest_retry(status, blocking):
    """Arch §6.3: retrying an access violation is futile and a compliance risk.

    A 402 is Apify's "you are out of credit" — retrying that in a loop is how a
    connector turns a billing problem into a rate-limit ban.
    """
    connector = connector_returning(httpx.Response(status, text="nope"))

    with pytest.raises(ApifyError) as caught:
        connector.search(["creatine"])

    assert caught.value.blocking is blocking


def test_the_build_is_pinned_in_the_request(ok_connector):
    """An Actor that changes its output shape overnight would otherwise start
    producing evidence with empty columns and no error anywhere."""
    ok_connector.search(["creatine"])

    url = str(ok_connector.handler.seen.url)
    assert f"build={YOUTUBE.build}" in url
    assert YOUTUBE.actor in url

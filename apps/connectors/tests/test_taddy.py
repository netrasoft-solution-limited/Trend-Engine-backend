"""Taddy, without Taddy.

Two things here are worth more than the rest. That a missing user id is caught
at construction with a message naming it — because the live failure looks like
a bad key and sends you reissuing the wrong thing. And that GraphQL errors in a
200 body are not mistaken for an empty result, which is how an auth failure
turns into "this podcast has no episodes".
"""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from apps.connectors.base import Capability, ConnectorPolicyError, FetchPlan, ItemRef
from apps.connectors.providers.taddy import (
    TaddyConnector,
    TaddyError,
    TranscribeStatus,
    _flatten,
)

SERIES = {
    "uuid": "series-1",
    "name": "The Nutrition Science Podcast",
    "episodes": [
        {
            "uuid": "ep-1",
            "guid": "guid-1",
            "name": "Magnesium forms and sleep",
            "description": "Glycinate, threonate and what the trials show.",
            "audioUrl": "https://example.test/ep1.mp3",
            "datePublished": 1772000000,
            "duration": 2700,
            "taddyTranscribeStatus": "COMPLETED",
        },
        {
            "uuid": "ep-2",
            "guid": "guid-2",
            "name": "Creatine beyond the gym",
            "description": "Cognition, ageing and dosing.",
            "audioUrl": "https://example.test/ep2.mp3",
            "datePublished": 1771000000,
            "duration": 3300,
            "taddyTranscribeStatus": "NOT_TRANSCRIBING",
        },
    ],
}

#: Milliseconds, as the field names imply and as Taddy does not document.
TRANSCRIPT_ITEMS = [
    {"id": "1", "text": "Magnesium glycinate is well tolerated.", "speaker": "Host",
     "startTimecode": 1000, "endTimecode": 4000},
    {"id": "2", "text": "Five hundred milligrams is typical.", "speaker": "Guest",
     "startTimecode": 62000, "endTimecode": 65000},
]


class FakeSource:
    pk = 1

    def __repr__(self) -> str:
        return "<Source Podcast>"


class FakePolicy:
    is_live = True


def connector_returning(*responses) -> TaddyConnector:
    def handler(request: httpx.Request) -> httpx.Response:
        handler.seen.append(request)
        return responses[min(len(handler.seen) - 1, len(responses) - 1)]

    handler.seen = []

    connector = TaddyConnector(
        source=FakeSource(),
        policy=FakePolicy(),
        api_key="key",
        user_id="7",
        transport=httpx.MockTransport(handler),
    )
    connector.handler = handler
    return connector


def data(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": payload})


# ── The contract ────────────────────────────────────────────────────────────


def test_no_policy_no_run():
    with pytest.raises(ConnectorPolicyError):
        TaddyConnector(source=FakeSource(), policy=None, api_key="k", user_id="7")


@pytest.mark.parametrize(
    "api_key,user_id,named",
    [("k", "", "user_id"), ("", "7", "api_key"), ("", "", "api_key")],
)
def test_a_missing_half_is_named_at_construction(api_key, user_id, named):
    """The failure this prevents cost an afternoon once already.

    Taddy rejects a key without a user id with an ordinary-looking auth error,
    and the natural response — reissuing the key — does not fix it.
    """
    with pytest.raises(ValueError) as caught:
        TaddyConnector(
            source=FakeSource(), policy=FakePolicy(), api_key=api_key, user_id=user_id
        )

    assert named in str(caught.value)
    assert "both" in str(caught.value).lower()


def test_both_headers_are_sent():
    connector = connector_returning(data({"getPodcastSeries": SERIES}))
    connector.episodes(rss_url="https://example.test/feed.xml")

    headers = connector.handler.seen[0].headers
    assert headers["X-API-KEY"] == "key"
    assert headers["X-USER-ID"] == "7"


def test_transcripts_are_not_priced_in_dollars():
    """Taddy bills a subscription with a monthly allowance, not per call.

    Inventing a per-call price would put fiction in the cost ledger; the
    constraint is a credit count and it is reported as one.
    """
    connector = connector_returning(data({}))
    plan = FetchPlan(
        refs=[ItemRef(source_id=1, external_item_id="ep-1")], want={Capability.TRANSCRIPT}
    )
    estimate = connector.estimate_cost(plan)

    assert estimate.amount == Decimal("0")
    assert estimate.units["transcript_requests"] == 1


# ── Episodes ────────────────────────────────────────────────────────────────


def test_episodes_map_onto_the_evidence_contract():
    connector = connector_returning(data({"getPodcastSeries": SERIES}))

    episodes = connector.episodes(rss_url="https://example.test/feed.xml")

    assert len(episodes) == 2
    first = episodes[0]
    assert first["external_id"] == "ep-1"
    assert first["title"] == "Magnesium forms and sleep"
    assert first["creator"] == "The Nutrition Science Podcast"
    assert first["url"] == "https://example.test/ep1.mp3"
    assert first["published_at"].year == 2026
    assert first["published_at"].tzinfo is not None
    assert first["route"] == "taddy"


def test_the_gate_is_told_whether_a_transcript_exists():
    """So a caller does not spend a call per episode to be told 'no'."""
    connector = connector_returning(data({"getPodcastSeries": SERIES}))

    ready, not_ready = connector.episodes(rss_url="https://example.test/feed.xml")

    assert ready["gate_metadata"]["transcript_available"] is True
    assert ready["transcribe_status"] == TranscribeStatus.COMPLETED
    assert not_ready["gate_metadata"]["transcript_available"] is False


def test_an_unknown_series_returns_nothing_rather_than_raising():
    connector = connector_returning(data({"getPodcastSeries": None}))

    assert connector.episodes(rss_url="https://example.test/nope.xml") == []


def test_a_query_needs_a_series_to_look_up():
    connector = connector_returning(data({}))

    with pytest.raises(ValueError, match="rss_url or a name"):
        connector.episodes()


def test_variables_are_sent_rather_than_interpolated():
    """A podcast name with a quote in it would otherwise change the query."""
    connector = connector_returning(data({"getPodcastSeries": SERIES}))
    connector.episodes(name='The "Real" Nutrition Show')

    body = json.loads(connector.handler.seen[0].content)
    assert body["variables"]["name"] == 'The "Real" Nutrition Show'
    assert "Real" not in body["query"]


# ── Transcripts ─────────────────────────────────────────────────────────────


def test_a_transcript_flattens_to_prose_with_anchors():
    connector = connector_returning(data({"getEpisodeTranscript": TRANSCRIPT_ITEMS}))

    result = connector.transcript("ep-1")

    assert result.text == (
        "Magnesium glycinate is well tolerated. Five hundred milligrams is typical."
    )
    assert result.speakers == ["Host", "Guest"]
    assert result.anchors[0] == (1.0, 0)
    # The second line begins where the first ends, plus the joining space.
    assert result.text[result.anchors[1][1]:].startswith("Five hundred")


def test_millisecond_timecodes_are_read_as_milliseconds():
    """62000 is a minute in, not seventeen hours."""
    result = _flatten(TRANSCRIPT_ITEMS)

    assert result.anchors[1][0] == 62.0


def test_second_timecodes_are_not_multiplied_by_a_thousand():
    """Taddy does not document the unit. Assuming milliseconds unconditionally
    would put every claim 1000× too late while looking entirely plausible."""
    in_seconds = [
        {"text": "Magnesium glycinate is well tolerated.", "startTimecode": 1},
        {"text": "Five hundred milligrams is typical.", "startTimecode": 62},
    ]

    result = _flatten(in_seconds)

    assert result.anchors[0][0] == 1.0
    assert result.anchors[1][0] == 62.0


def test_anchors_run_forward_and_stay_in_range():
    result = _flatten(TRANSCRIPT_ITEMS)

    assert result.anchors == sorted(result.anchors)
    assert all(0 <= p < len(result.text) for _, p in result.anchors)


def test_no_transcript_is_a_routine_answer_not_a_failure():
    """It means 'try the next rung', not 'something broke'."""
    connector = connector_returning(data({"getEpisodeTranscript": None}))

    result = connector.transcript("ep-2")

    assert result.text == ""
    assert result.anchors == []


def test_fetch_content_reports_metadata_only_when_there_is_no_transcript():
    connector = connector_returning(data({"getEpisodeTranscript": None}))

    result = connector.fetch_content(ItemRef(source_id=1, external_item_id="ep-2"))

    assert result.is_metadata_only is True
    assert result.route == "metadata_only"


# ── Failure ─────────────────────────────────────────────────────────────────


def test_a_graphql_error_in_a_200_is_not_mistaken_for_an_empty_result():
    """GraphQL reports failure in the body. A status check alone would turn an
    auth error into 'this podcast has no episodes'."""
    connector = connector_returning(
        httpx.Response(200, json={"errors": [{"message": "Invalid API key or user id"}]})
    )

    with pytest.raises(TaddyError) as caught:
        connector.episodes(rss_url="https://example.test/feed.xml")

    assert caught.value.blocking is True


def test_a_credit_limit_blocks_rather_than_retries():
    connector = connector_returning(
        httpx.Response(200, json={"errors": [{"message": "Monthly credit limit reached"}]})
    )

    with pytest.raises(TaddyError) as caught:
        connector.transcript("ep-1")

    assert caught.value.blocking is True


PLAN_LIMIT = httpx.Response(
    200,
    json={
        "errors": [
            {
                "message": (
                    "You need to be a Pro or Business Taddy API user to "
                    "access the transcript for this episode."
                )
            }
        ]
    },
)


def test_a_plan_limitation_falls_through_instead_of_stopping_the_run():
    """The exact message a free-tier account gets, from a live call.

    Taddy HAS this transcript; the plan does not include it. That means "try
    the next rung", not "the connector is broken" — and on a free-tier account
    it is the answer for essentially every episode, so raising would halt the
    pipeline permanently.
    """
    connector = connector_returning(PLAN_LIMIT)

    result = connector.transcript("ep-1")

    assert result.text == ""
    assert result.status == TranscribeStatus.PLAN_LIMITED


def test_a_plan_limitation_elsewhere_still_raises():
    """Only the transcript rung has a next rung to fall through to. The same
    limit hit while listing episodes is a real stop."""
    connector = connector_returning(PLAN_LIMIT)

    with pytest.raises(TaddyError) as caught:
        connector.episodes(name="Whatever")

    assert caught.value.blocking is True


@pytest.mark.parametrize(
    "message",
    [
        "Internal server error",
        "Request timed out",
        "Service temporarily unavailable",
        "Please try again",
    ],
)
def test_only_clearly_transient_errors_stay_retryable(message):
    """Anything else is assumed permanent. Collection is scheduled, so a
    transient failure is picked up next cycle anyway — while a permanent one
    retried in a loop burns quota and invites a block."""
    connector = connector_returning(
        httpx.Response(200, json={"errors": [{"message": message}]})
    )

    with pytest.raises(TaddyError) as caught:
        connector.transcript("ep-1")

    assert caught.value.blocking is False


@pytest.mark.parametrize(
    "status,blocking", [(401, True), (403, True), (429, False), (500, False)]
)
def test_http_failures_are_classified(status, blocking):
    connector = connector_returning(httpx.Response(status, text="nope"))

    with pytest.raises(TaddyError) as caught:
        connector.transcript("ep-1")

    assert caught.value.blocking is blocking

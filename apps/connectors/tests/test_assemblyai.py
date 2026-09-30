"""AssemblyAI, without AssemblyAI.

The response fixture is shaped like the real API: `text` is punctuated and
cased, while `words` carries the raw tokens with millisecond offsets. Those two
do NOT correspond character for character, which is the reason the anchor
mapping locates each word in the text rather than assuming a join. Getting that
wrong puts a claim at the wrong moment in the episode, confidently.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from apps.connectors.base import Capability, ConnectorPolicyError, FetchPlan, ItemRef
from apps.connectors.providers.assemblyai import (
    RATES,
    AssemblyAIConnector,
    AssemblyAIError,
    _anchors_from_words,
)

TEXT = "Creatine monohydrate is the most studied form. Five grams a day is enough."

COMPLETED = {
    "id": "t-1",
    "status": "completed",
    "text": TEXT,
    "audio_duration": 2700.0,  # 45 minutes
    "confidence": 0.9312,
    "words": [
        {"text": "Creatine", "start": 1000, "end": 1400, "confidence": 0.99},
        {"text": "monohydrate", "start": 1400, "end": 2000, "confidence": 0.97},
        {"text": "is", "start": 2600, "end": 2700, "confidence": 0.99},
        {"text": "Five", "start": 46000, "end": 46400, "confidence": 0.95},
        {"text": "grams", "start": 47100, "end": 47500, "confidence": 0.96},
    ],
}


class FakeSource:
    pk = 1

    def __repr__(self) -> str:
        return "<Source Podcast>"


class FakePolicy:
    is_live = True


def connector_returning(*responses, **kwargs) -> AssemblyAIConnector:
    def handler(request: httpx.Request) -> httpx.Response:
        handler.seen.append(request)
        index = min(len(handler.seen) - 1, len(responses) - 1)
        return responses[index]

    handler.seen = []

    connector = AssemblyAIConnector(
        source=FakeSource(),
        policy=FakePolicy(),
        api_key="key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    connector.handler = handler
    return connector


# ── The contract ────────────────────────────────────────────────────────────


def test_no_policy_no_run():
    with pytest.raises(ConnectorPolicyError):
        AssemblyAIConnector(source=FakeSource(), policy=None, api_key="key")


def test_a_missing_key_names_where_to_set_it():
    with pytest.raises(ValueError, match="operator admin|ASSEMBLYAI_API_KEY"):
        AssemblyAIConnector(source=FakeSource(), policy=FakePolicy(), api_key="")


def test_an_unknown_model_is_refused_rather_than_billed_at_an_unknown_price():
    with pytest.raises(ValueError, match="Unknown AssemblyAI model"):
        AssemblyAIConnector(
            source=FakeSource(), policy=FakePolicy(), api_key="k", model="whisper-xl"
        )


def test_it_claims_only_what_it_does():
    """It transcribes audio another connector found; it is not a source."""
    connector = connector_returning(httpx.Response(200, json=COMPLETED))

    assert connector.capabilities == {Capability.TRANSCRIPT}
    with pytest.raises(NotImplementedError):
        list(connector.discover(None))
    with pytest.raises(NotImplementedError):
        list(connector.fetch_metadata([]))


# ── Cost ────────────────────────────────────────────────────────────────────


def test_a_45_minute_episode_is_priced_from_its_duration():
    connector = connector_returning(httpx.Response(200, json=COMPLETED))

    # 2700s = 0.75h at $0.15/h
    assert connector.price_for(2700) == Decimal("0.112500")


def test_the_pro_model_costs_more():
    cheap = connector_returning(httpx.Response(200, json=COMPLETED))
    pro = connector_returning(httpx.Response(200, json=COMPLETED), model="universal-3-5-pro")

    assert pro.price_for(3600) > cheap.price_for(3600)
    assert cheap.price_for(3600) == RATES["universal-2"]


def test_an_over_cap_item_is_refused_before_submission(monkeypatch):
    """Arch §10.2. Once the job is submitted the money is committed, so a cap
    consulted afterwards is a report, not a control."""
    connector = connector_returning(httpx.Response(200, json={"id": "t-1"}))

    with pytest.raises(AssemblyAIError) as caught:
        connector.transcribe(
            "https://example.test/a.mp3",
            max_cost_usd=Decimal("0.05"),
            expected_duration_seconds=3600,
        )

    assert caught.value.blocking is True
    assert connector.handler.seen == [], "nothing may be sent once the cap refuses"


def test_a_within_cap_item_proceeds():
    connector = connector_returning(
        httpx.Response(200, json={"id": "t-1"}), httpx.Response(200, json=COMPLETED)
    )

    result = connector.transcribe(
        "https://example.test/a.mp3",
        max_cost_usd=Decimal("0.50"),
        expected_duration_seconds=2700,
    )
    assert result.text == TEXT


def test_estimate_cost_prices_an_hour_per_item():
    connector = connector_returning(httpx.Response(200, json=COMPLETED))
    plan = FetchPlan(
        refs=[ItemRef(source_id=1, external_item_id=str(n)) for n in range(4)],
        want={Capability.TRANSCRIPT},
    )

    assert connector.estimate_cost(plan).amount == RATES["universal-2"] * 4


# ── The transcript ──────────────────────────────────────────────────────────


def test_a_completed_job_returns_text_quality_and_cost():
    connector = connector_returning(
        httpx.Response(200, json={"id": "t-1"}), httpx.Response(200, json=COMPLETED)
    )

    result = connector.transcribe("https://example.test/a.mp3")

    assert result.text == TEXT
    assert result.quality == 93, "quality is AssemblyAI's own mean word confidence"
    assert result.duration_seconds == 2700.0
    assert result.cost_usd == Decimal("0.112500")
    assert result.model == "universal-2"


def test_word_timings_become_anchors_into_the_punctuated_text():
    """`words` holds raw tokens; `text` is punctuated and cased. Anchors must
    index into `text`, which is what a ContentSegment's offsets refer to."""
    anchors = _anchors_from_words(COMPLETED["words"], TEXT)

    assert anchors, "timed audio must produce anchors"
    for _seconds, position in anchors:
        assert 0 <= position < len(TEXT)
    # Monotonic in both dimensions, which `extraction._seconds_at` relies on.
    assert anchors == sorted(anchors)
    # The first anchor is the first word, at its real offset.
    assert anchors[0] == (1.0, TEXT.index("Creatine"))


def test_anchors_are_thinned_to_about_one_per_second():
    """A 45-minute episode has ~7,000 words. An anchor each would store more
    timing than transcript, for a resolution nobody reads."""
    anchors = _anchors_from_words(COMPLETED["words"], TEXT)

    assert len(anchors) < len(COMPLETED["words"])


def test_a_word_that_cannot_be_located_is_skipped_not_guessed():
    """A wrong anchor is worse than a missing one: it places a claim at the
    wrong moment in the episode with full confidence."""
    words = [
        {"text": "Creatine", "start": 1000},
        {"text": "[inaudible]", "start": 5000},
        {"text": "grams", "start": 47000},
    ]
    anchors = _anchors_from_words(words, TEXT)

    assert all(TEXT[pos:].startswith(("Creatine", "grams")) for _, pos in anchors)


def test_an_empty_transcript_scores_zero_quality():
    connector = connector_returning(
        httpx.Response(200, json={"id": "t-1"}),
        httpx.Response(
            200, json={"status": "completed", "text": "", "audio_duration": 10, "words": []}
        ),
    )

    result = connector.transcribe("https://example.test/a.mp3")
    assert result.text == ""
    assert result.quality == 0


# ── Failure ─────────────────────────────────────────────────────────────────


def test_a_failed_job_reports_the_reason():
    connector = connector_returning(
        httpx.Response(200, json={"id": "t-1"}),
        httpx.Response(200, json={"status": "error", "error": "Download failed"}),
    )

    with pytest.raises(AssemblyAIError, match="Download failed"):
        connector.transcribe("https://example.test/a.mp3")


@pytest.mark.parametrize(
    "status,blocking", [(401, True), (402, True), (403, True), (429, False), (500, False)]
)
def test_auth_and_balance_failures_block_while_the_rest_retry(status, blocking):
    """Arch §6.3: retrying an exhausted balance turns a billing problem into a
    rate-limit ban."""
    connector = connector_returning(httpx.Response(status, text="nope"))

    with pytest.raises(AssemblyAIError) as caught:
        connector.transcribe("https://example.test/a.mp3")

    assert caught.value.blocking is blocking


def test_a_job_that_never_finishes_gives_up_and_says_it_is_still_billed():
    connector = connector_returning(
        httpx.Response(200, json={"id": "t-1"}),
        httpx.Response(200, json={"status": "processing"}),
    )

    with pytest.raises(AssemblyAIError, match="still be billed"):
        connector.transcribe(
            "https://example.test/a.mp3", poll_seconds=0.0, timeout_seconds=0.0
        )


def test_the_key_is_sent_and_the_addons_are_not():
    """The AI add-ons are separately billed and duplicate work the enrichment
    layer already does with its own cost accounting."""
    connector = connector_returning(
        httpx.Response(200, json={"id": "t-1"}), httpx.Response(200, json=COMPLETED)
    )
    connector.transcribe("https://example.test/a.mp3")

    submit = connector.handler.seen[0]
    assert submit.headers["Authorization"] == "key"

    import json

    body = json.loads(submit.content)
    assert body["speech_models"] == ["universal-2"]
    assert body["auto_chapters"] is False
    assert body["summarization"] is False

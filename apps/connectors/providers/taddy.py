"""Taddy — podcast discovery, metadata, and the cheap transcript rungs.

Taddy sits ABOVE Apify and AssemblyAI in the fallback chain (Arch §7.2), and
the reason is its pricing shape rather than its quality: a transcript the
podcast itself published costs nothing and consumes no credit, on any plan. So
the order is publisher transcript (free, here) → Taddy on-demand (a monthly
credit allowance) → creator captions via Apify ($0.004) → AssemblyAI ($0.15/hr).
Each rung is roughly an order of magnitude dearer than the one above it.

    POST https://api.taddy.org
    X-API-KEY + X-USER-ID — BOTH. Taddy rejects either alone, which is why
    `AcquisitionProvider` stores a mapping of named secrets rather than one
    string, and why the operator screen refuses a half-entered pair.

AUTHENTICATION IS THE THING THAT BITES HERE. A missing user id returns an
ordinary-looking auth error that reads like a bad key, and the natural response
— reissuing the key — does not fix it.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from typing import Any

import httpx
from django.utils.html import strip_tags

from apps.connectors.base import (
    BaseConnector,
    Capability,
    ContentResult,
    CostEstimate,
    DateWindow,
    FetchPlan,
    ItemRef,
)

logger = logging.getLogger(__name__)

API = "https://api.taddy.org"


class TaddyError(RuntimeError):
    """A failed Taddy call.

    `blocking` separates the classes Arch §6.3 treats differently: a bad key or
    an exhausted credit allowance stops the connector and alerts an operator;
    a timeout or a 5xx backs off and retries.
    """

    def __init__(self, message: str, *, blocking: bool = False) -> None:
        super().__init__(message)
        self.blocking = blocking


class TranscribeStatus:
    """Taddy's own view of whether a transcript exists yet.

    Worth modelling rather than treating as a string, because the difference
    between PROCESSING and NOT_TRANSCRIBING decides whether the right response
    is to wait or to fall through to a paid rung.
    """

    COMPLETED = "COMPLETED"
    PROCESSING = "PROCESSING"
    TRANSCRIBING = "TRANSCRIBING"
    NOT_TRANSCRIBING = "NOT_TRANSCRIBING"
    #: Ours, not Taddy's: Taddy HAS the transcript but this account's plan does
    #: not include it. Distinct from NOT_TRANSCRIBING, which means no
    #: transcript exists at all — the first is fixed by money, the second is
    #: not fixed by anything.
    PLAN_LIMITED = "PLAN_LIMITED"

    #: Worth polling again shortly. NOT_TRANSCRIBING is not here: Taddy has not
    #: queued it and waiting would be waiting forever.
    PENDING = {PROCESSING, TRANSCRIBING}


@dataclass(frozen=True)
class TaddyTranscript:
    text: str
    #: `(start_seconds, char_index)` — the same shape `srt.to_text` and the
    #: AssemblyAI connector produce, so `TranscriptArtifact.anchors` is
    #: populated identically whichever rung supplied the transcript.
    anchors: list[tuple[float, int]]
    status: str
    speakers: list[str]


#: One query, asking for exactly the evidence contract's fields and nothing
#: more. Written as a constant so the field list is reviewable in one place —
#: this is the connector's entire coupling to Taddy's schema.
EPISODES_BY_RSS = """
query Episodes($rssUrl: String!, $limit: Int) {
  getPodcastSeries(rssUrl: $rssUrl) {
    uuid
    name
    episodes(limitPerPage: $limit, sortOrder: LATEST) {
      uuid
      guid
      name
      description
      audioUrl
      datePublished
      duration
      taddyTranscribeStatus
    }
  }
}
"""

EPISODES_BY_NAME = """
query Episodes($name: String!, $limit: Int) {
  getPodcastSeries(name: $name) {
    uuid
    name
    episodes(limitPerPage: $limit, sortOrder: LATEST) {
      uuid
      guid
      name
      description
      audioUrl
      datePublished
      duration
      taddyTranscribeStatus
    }
  }
}
"""

TRANSCRIPT = """
query Transcript($uuid: ID!) {
  getEpisodeTranscript(uuid: $uuid) {
    id
    text
    speaker
    startTimecode
    endTimecode
  }
}
"""


class TaddyConnector(BaseConnector):
    """Podcast series, their episodes, and their transcripts."""

    capabilities = {
        Capability.DISCOVERY,
        Capability.METADATA,
        Capability.TRANSCRIPT,
    }

    def __init__(
        self,
        *,
        source,
        policy,
        api_key: str,
        user_id: str,
        transport: Any | None = None,
    ) -> None:
        super().__init__(source=source, policy=policy)
        # Both, or neither works. Naming the missing one here saves the hour
        # otherwise spent reissuing a key that was never the problem.
        missing = [
            name
            for name, value in (("api_key", api_key), ("user_id", user_id))
            if not value
        ]
        if missing:
            raise ValueError(
                f"Taddy needs both an api_key and a user_id; missing: "
                f"{', '.join(missing)}. Set them together at "
                f"/ops/sources/providers/, or as TADDY_API_KEY and TADDY_USER_ID."
            )
        self._api_key = api_key
        self._user_id = user_id
        self._transport = transport

    # ── Cost ────────────────────────────────────────────────────────────────

    def estimate_cost(self, plan: FetchPlan) -> CostEstimate:
        """Zero dollars, and that is the honest answer.

        Taddy bills a flat subscription with a monthly transcript allowance,
        not per call. A podcast-provided transcript consumes no allowance at
        all. So the constraint here is a credit count, not a dollar amount, and
        reporting an invented per-call price would put fiction in the cost
        ledger. The credits are surfaced in `units` for the caller that tracks
        them.
        """
        return CostEstimate(
            currency="USD",
            amount=Decimal("0"),
            units={"transcript_requests": len(plan.refs), "provider": "taddy"},
        )

    # ── Discovery and metadata ──────────────────────────────────────────────

    def episodes(
        self, *, rss_url: str = "", name: str = "", limit: int = 25
    ) -> list[dict]:
        """The latest episodes of one series, normalised.

        Cheap and metadata-only on purpose: this is what the relevance gate
        reads (Arch §6.2), and the transcript — the expensive part — is fetched
        only for episodes that pass it.
        """
        if rss_url:
            query, variables = EPISODES_BY_RSS, {"rssUrl": rss_url, "limit": limit}
        elif name:
            query, variables = EPISODES_BY_NAME, {"name": name, "limit": limit}
        else:
            raise ValueError("Give either an rss_url or a name.")

        series = self._call(query, variables).get("getPodcastSeries")
        if not series:
            logger.warning("Taddy found no series for %s", rss_url or name)
            return []

        return [
            self._normalise(episode, series)
            for episode in (series.get("episodes") or [])
        ]

    def discover(self, window: DateWindow) -> Iterable[ItemRef]:
        """Discovery is per-series, and the series comes from the Source.

        `Source` carries no feed URL field yet, so this stays explicit rather
        than guessing at one. `episodes()` is the entry point until it does.
        """
        raise NotImplementedError(
            "Taddy discovery is per-series; call `episodes(rss_url=...)`. "
            "Wire it to Source once that model carries a feed URL."
        )

    def fetch_metadata(self, refs: list[ItemRef]) -> Iterable[dict]:
        raise NotImplementedError(
            "Taddy returns a series' episodes in one call; use `episodes()`."
        )

    # ── Transcripts ─────────────────────────────────────────────────────────

    def transcript(self, episode_uuid: str) -> TaddyTranscript:
        """The transcript for one episode, with timings.

        Returns an EMPTY transcript rather than raising in the two cases that
        mean "this rung cannot serve it": Taddy holds no transcript, and the
        account's plan does not cover the one it holds. Both are routine
        answers meaning "try the next rung" — Apify captions, then ASR — and
        raising would stop a collection run over an expected condition.

        A free-tier account gets the plan answer for essentially every episode,
        so treating it as an outage would halt the pipeline permanently.
        """
        try:
            items = self._call(TRANSCRIPT, {"uuid": episode_uuid}).get(
                "getEpisodeTranscript"
            )
        except TaddyError as exc:
            if not _is_plan_limited(str(exc)):
                raise
            logger.info(
                "Taddy has a transcript for %s but this plan does not include it; "
                "falling through to the next rung.",
                episode_uuid,
            )
            return TaddyTranscript(
                text="", anchors=[], status=TranscribeStatus.PLAN_LIMITED, speakers=[]
            )

        if not items:
            return TaddyTranscript(text="", anchors=[], status="", speakers=[])
        return _flatten(items)

    def fetch_content(self, ref: ItemRef) -> ContentResult:
        result = self.transcript(ref.external_item_id)
        return ContentResult(
            text=result.text or None,
            route="taddy" if result.text else "metadata_only",
            is_metadata_only=not result.text,
            raw_checksum="",
        )

    # ── HTTP ────────────────────────────────────────────────────────────────

    def _call(self, query: str, variables: dict) -> dict:
        """One GraphQL call.

        Variables rather than interpolation: a podcast name with a quote in it
        would otherwise change the shape of the query.
        """
        with httpx.Client(
            timeout=60.0,
            transport=self._transport,
            headers={
                "Content-Type": "application/json",
                "X-API-KEY": self._api_key,
                "X-USER-ID": self._user_id,
            },
        ) as http:
            response = http.post(API, json={"query": query, "variables": variables})

        if response.status_code >= 400:
            raise TaddyError(
                f"Taddy returned {response.status_code}: {response.text[:300]}",
                blocking=response.status_code in {401, 402, 403},
            )

        payload = response.json()

        # GraphQL reports failure in a 200 body, so a status check alone would
        # let an auth error through as an empty result.
        errors = payload.get("errors")
        if errors:
            message = "; ".join(e.get("message", "") for e in errors)
            raise TaddyError(f"Taddy: {message}", blocking=_is_permanent(message))

        return payload.get("data") or {}

    # ── Mapping ─────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise(episode: dict, series: dict) -> dict:
        """One episode onto the evidence contract (PRD §6.2).

        Show notes arrive as raw HTML — Taddy passes the feed's `<description>`
        through unchanged. The relevance gate reads this field (Arch §6.2), so
        left alone every `<p><a href="…utm_campaign=…">` is tokens paid for on
        every item and noise diluting the signal the gate is looking for. One
        real episode's notes are ~4,600 characters of which a third is markup
        and tracking URLs.
        """
        published = episode.get("datePublished")
        if isinstance(published, (int, float)):
            # Taddy publishes a Unix timestamp in seconds.
            published = datetime.fromtimestamp(published, tz=UTC)
        else:
            published = None

        status = episode.get("taddyTranscribeStatus") or ""

        return {
            "external_id": episode.get("uuid") or episode.get("guid") or "",
            "title": episode.get("name") or "",
            "url": episode.get("audioUrl") or "",
            "creator": series.get("name") or "",
            "published_at": published,
            "description": _plain_text(episode.get("description") or ""),
            "route": "taddy",
            "gate_metadata": {
                "duration": episode.get("duration"),
                "series": series.get("name"),
                "transcript_available": status == TranscribeStatus.COMPLETED,
                #: Podcasters list their chapters in the show notes with
                #: timestamps. That is the richest metadata signal the gate
                #: gets for an episode it has not transcribed.
                "chapters": _chapters(episode.get("description") or ""),
            },
            "raw_metadata": episode,
            #: Whether fetching a transcript is worth attempting at all, so a
            #: caller does not burn a call per episode to be told "no".
            "transcribe_status": status,
            "series_uuid": series.get("uuid"),
        }


#: Phrases that mean "this will never succeed as asked". Everything a GraphQL
#: body reports is treated as permanent UNLESS it looks like one of these.
#:
#: The default is inverted deliberately, and a live call is why. Taddy answers
#: a transcript request from a free-tier account with "You need to be a Pro or
#: Business Taddy API user" — a plan limitation that matched none of the
#: obvious auth keywords, so a keyword list would have retried it on every run
#: forever. Retrying a permanent error is the worse mistake: collection is
#: scheduled, so a transient failure is picked up on the next cycle anyway,
#: while a permanent one retried in a loop burns quota and invites a block.
_TRANSIENT = (
    "timeout",
    "timed out",
    "internal server error",
    "temporarily",
    "try again",
    "unavailable",
)


def _is_permanent(message: str) -> bool:
    lowered = message.lower()
    return not any(phrase in lowered for phrase in _TRANSIENT)


#: "You need to be a Pro or Business Taddy API user to access the transcript
#: for this episode." — the verbatim free-tier answer, from a live call.
_PLAN_LIMITED = ("pro or business", "upgrade", "not included in your plan")


def _is_plan_limited(message: str) -> bool:
    lowered = message.lower()
    return any(phrase in lowered for phrase in _PLAN_LIMITED)


#: `Lloyd's path from academic medicine to drug development [3:15];`
#: Podcasters write chapters this way in the show notes, and a timestamped
#: line is a far stronger relevance signal than the surrounding prose.
_CHAPTER = re.compile(r"<li[^>]*>(.*?)\[\d+:\d+(?::\d+)?\]", re.IGNORECASE | re.DOTALL)


def _plain_text(html: str) -> str:
    """Show notes as readable text.

    `strip_tags` alone leaves the entity references and the run-together
    whitespace that collapsing tags produces, both of which reach the gate
    prompt as tokens.
    """
    if not html:
        return ""
    text = unescape(strip_tags(html))
    return re.sub(r"\s+", " ", text).strip()


def _chapters(html: str) -> list[str]:
    """The chapter titles, without their timestamps.

    Capped because some feeds list fifty, and the gate is given metadata to
    judge relevance, not an index to read.
    """
    found = [_plain_text(match).strip(" ;,") for match in _CHAPTER.findall(html)]
    return [chapter for chapter in found if chapter][:20]


def _flatten(items: list[dict]) -> TaddyTranscript:
    """Transcript lines into prose plus anchors.

    TIMECODE UNITS ARE NOT DOCUMENTED by Taddy. Milliseconds is the
    overwhelmingly common convention and what the field names suggest, so that
    is the assumption — but it is CHECKED rather than trusted: if the largest
    timecode is small enough that milliseconds would make the episode a few
    seconds long, they are read as seconds instead. Getting this wrong by
    1000× would put every claim at the wrong moment in the episode while
    looking entirely plausible.
    """
    starts = [i.get("startTimecode") for i in items if i.get("startTimecode") is not None]
    largest = max((float(s) for s in starts), default=0.0)

    # A podcast episode runs minutes at least. If the largest start time is
    # under 600 "units", they cannot be milliseconds — that would be a
    # ten-second episode — so they are seconds.
    divisor = 1000.0 if largest >= 600 else 1.0

    parts: list[str] = []
    anchors: list[tuple[float, int]] = []
    speakers: list[str] = []
    position = 0

    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue

        speaker = (item.get("speaker") or "").strip()
        if speaker and speaker not in speakers:
            speakers.append(speaker)

        start = item.get("startTimecode")
        if start is not None:
            anchors.append((float(start) / divisor, position))

        parts.append(text)
        position += len(text) + 1  # the joining space

    return TaddyTranscript(
        text=" ".join(parts),
        anchors=anchors,
        status=TranscribeStatus.COMPLETED if parts else "",
        speakers=speakers,
    )

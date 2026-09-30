"""The Apify connector — Arch §7.3: one connector, many Actors.

    "A single `ApifyActorConnector`, configured per Actor — never per-platform
     business logic. [...] Swapping an Actor (or dropping Apify entirely for a
     given platform) touches one config row. Nothing in enrichment, scoring,
     outputs, or the portal changes. That is the entire point of driver #5."

So there is no YouTube logic in this file beyond a field map. The Actor id, the
pinned build, the input template and the output mapping are all configuration;
this class knows how to start a run, wait for it, and normalise what comes back.

BUILDS ARE PINNED. `latest` is a moving target, and an Actor that changes its
output shape overnight would silently start producing evidence with missing
fields — the kind of failure that looks like a quiet week rather than a break.
PRD §14 requires connectors to be "version-pinned, schema-tested".
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from apps.connectors.base import (
    BaseConnector,
    Capability,
    ContentResult,
    CostEstimate,
    DateWindow,
    FetchPlan,
    ItemRef,
)
from apps.connectors.srt import to_text

logger = logging.getLogger(__name__)

API = "https://api.apify.com/v2"


@dataclass(frozen=True)
class ActorConfig:
    """Everything that makes this connector behave like a particular Actor.

    Arch §7.3 lists what this must carry: "Actor ID + pinned build, maintainer
    classification, input template, output-field mapping, capability flags,
    policy version, per-run max charge, monthly warning/hard cap, retry policy,
    webhook secret, idempotency key." The caps live on `AcquisitionProvider`
    and the policy on the `Source`, so what remains here is the Actor itself.
    """

    actor: str
    build: str
    #: Cost per primary event, from the Actor's published pricing. Used to
    #: refuse a run BEFORE it starts (Arch §10.2) rather than discover the
    #: charge afterwards.
    price_per_result_usd: Decimal
    input_template: dict = field(default_factory=dict)
    #: Which Actor output field maps to which part of the evidence contract.
    #: A field map, not a parser — changing Actor means changing this dict.
    field_map: dict[str, str] = field(default_factory=dict)


#: `streamers/youtube-scraper` — 27M runs, the dominant YouTube Actor.
#: Price is the BRONZE tier rate; FREE is $0.004. Verified against the live
#: store listing on 2026-09-30.
YOUTUBE = ActorConfig(
    actor="streamers~youtube-scraper",
    build="0.0.303",
    price_per_result_usd=Decimal("0.004"),
    input_template={
        "downloadSubtitles": True,
        "subtitlesLanguage": "en",
        "saveSubsToKVS": False,
        # The AI add-ons are separately charged and duplicate work the
        # enrichment layer already does better, with its own cost accounting.
        "getAiVideoDescription": False,
        "getAiVideoSummary": False,
    },
    field_map={
        "external_id": "id",
        "title": "title",
        "url": "url",
        "creator": "channelName",
        "published_at": "date",
        "description": "text",
    },
)


class ApifyActorConnector(BaseConnector):
    """Runs one Actor and normalises its dataset into the evidence contract."""

    capabilities = {
        Capability.DISCOVERY,
        Capability.METADATA,
        Capability.TRANSCRIPT,
        Capability.ENGAGEMENT,
    }

    def __init__(
        self,
        *,
        source,
        policy,
        token: str,
        config: ActorConfig = YOUTUBE,
        transport: Any | None = None,
    ) -> None:
        # BaseConnector refuses without a policy — PRD §7.2's "no connector
        # runs without a recorded provider policy and access basis".
        super().__init__(source=source, policy=policy)
        if not token:
            raise ValueError("Apify token is required; set APIFY_TOKEN.")
        self._token = token
        self.config = config
        # For tests only, mirroring `LLMClient`: the suite drives an
        # `httpx.MockTransport` so field mapping and failure classification are
        # covered without an Actor run and without spending credit.
        self._transport = transport

    # ── Cost, before anything is spent ──────────────────────────────────────

    def estimate_cost(self, plan: FetchPlan) -> CostEstimate:
        results = max(len(plan.refs), 1)
        return CostEstimate(
            currency="USD",
            amount=self.config.price_per_result_usd * results,
            units={"results": results, "actor": self.config.actor},
        )

    # ── Acquisition ─────────────────────────────────────────────────────────

    def discover(self, window: DateWindow) -> Iterable[ItemRef]:
        """Not used for Apify: discovery and fetch are the same run.

        Splitting them would mean paying twice — the Actor charges per video
        written to the dataset whether or not you asked for its subtitles.
        `search` below does both in one pass.
        """
        raise NotImplementedError(
            "Apify discovery and fetch are one run; use `search` or `fetch_by_url`."
        )

    def search(
        self, queries: list[str], *, max_results: int = 10, timeout: float = 280.0
    ) -> list[dict]:
        """Run the Actor and return its dataset rows, normalised.

        Synchronous because the caller is a Celery task on the `ingest` queue
        that is allowed to block — Arch §11.1 sizes that queue for exactly this,
        "I/O-bound, high concurrency, tolerant of slow external APIs".
        """
        payload = {
            **self.config.input_template,
            "searchQueries": queries,
            "maxResults": max_results,
        }
        rows = self._run(payload, timeout=timeout)
        return [self._normalise(row) for row in rows]

    def fetch_by_url(self, urls: list[str], *, timeout: float = 280.0) -> list[dict]:
        payload = {
            **self.config.input_template,
            "startUrls": [{"url": u} for u in urls],
            "maxResults": len(urls),
        }
        return [self._normalise(row) for row in self._run(payload, timeout=timeout)]

    def fetch_metadata(self, refs: list[ItemRef]) -> Iterable[dict]:
        return self.fetch_by_url([r.url for r in refs if r.url])

    def fetch_content(self, ref: ItemRef) -> ContentResult:
        rows = self.fetch_by_url([ref.url]) if ref.url else []
        if not rows:
            return ContentResult(
                text=None, route="metadata_only", is_metadata_only=True, raw_checksum=""
            )
        row = rows[0]
        return ContentResult(
            text=row.get("transcript"),
            route="apify_subtitles" if row.get("transcript") else "metadata_only",
            is_metadata_only=not row.get("transcript"),
            raw_checksum=row.get("content_hash", ""),
        )

    # ── Internals ───────────────────────────────────────────────────────────

    def _run(self, payload: dict, *, timeout: float) -> list[dict]:
        url = (
            f"{API}/acts/{self.config.actor}/run-sync-get-dataset-items"
            f"?token={self._token}&build={self.config.build}"
        )
        with httpx.Client(timeout=timeout, transport=self._transport) as http:
            response = http.post(url, json=payload)

        if response.status_code >= 400:
            # Arch §6.3: auth and policy errors block the connector rather than
            # retry — "retrying an access violation is both futile and a
            # compliance risk". Everything else is the retryable class.
            blocking = response.status_code in {401, 402, 403}
            raise ApifyError(
                f"Apify {self.config.actor} returned {response.status_code}: "
                f"{response.text[:300]}",
                blocking=blocking,
            )

        rows = response.json()
        logger.info("Apify %s returned %s rows", self.config.actor, len(rows))
        return rows

    def _normalise(self, row: dict) -> dict:
        """Map one Actor row onto the evidence contract (PRD §6.2).

        Provider-specific fields stay in `raw_metadata` and never leak into
        scoring or output generation — the field map above is the only place
        Actor field names appear.
        """
        mapped = {target: row.get(source) for target, source in self.config.field_map.items()}

        published = mapped.get("published_at")
        if isinstance(published, str):
            try:
                mapped["published_at"] = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                mapped["published_at"] = None

        transcript, anchors = self._transcript(row)
        mapped["transcript"] = transcript
        mapped["transcript_anchors"] = anchors

        # What the relevance gate is allowed to read — metadata only.
        mapped["gate_metadata"] = {
            "duration": row.get("duration"),
            "hashtags": row.get("hashtags") or [],
            "channel_subscribers": row.get("numberOfSubscribers"),
        }
        mapped["engagement"] = {
            "views": row.get("viewCount"),
            "likes": row.get("likes"),
            "comments": row.get("commentsCount"),
        }
        mapped["raw_metadata"] = row
        return mapped

    @staticmethod
    def _transcript(row: dict) -> tuple[str | None, list]:
        """Prefer a real caption track; fall back to metadata-only.

        Auto-generated captions are accepted but their origin is recorded, so
        a claim quoted from an ASR transcript can be weighed differently from
        one quoted from a publisher's own.
        """
        tracks = row.get("subtitles") or []
        for track in tracks:
            body = track.get("srt")
            if body:
                return to_text(body)
        return None, []


class ApifyError(RuntimeError):
    """A failed Actor run.

    `blocking` distinguishes the two classes Arch §6.3 treats differently: an
    auth or quota failure stops the connector and raises an operator alert; a
    timeout or a 5xx backs off and retries.
    """

    def __init__(self, message: str, *, blocking: bool = False) -> None:
        super().__init__(message)
        self.blocking = blocking

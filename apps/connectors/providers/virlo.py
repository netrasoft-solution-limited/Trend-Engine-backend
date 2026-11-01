"""Virlo's raw-data API — TikTok and Instagram Reels (Mark, 31 Oct).

Pure Play compared Virlo with Apify and chose Virlo for TikTok and Instagram:
one synchronous GET per page, $0.01 a call, from a prepaid balance. Only the
raw routes are used. Virlo's own AI reports duplicate what enrichment already
does, with its own cost accounting, so they are not bought.

WHAT THIS RETURNS IS NORMALISED ROWS, the same contract the Apify and Taddy
connectors return, so ingestion, the relevance gate, extraction and the briefs
need nothing platform-specific.

Every call is counted. `calls` and `cost_usd` on the connector are what one
collection spent, and `max_calls` refuses the call that would exceed the
ceiling BEFORE it is made (Arch §10.2), not after.

Instagram has no transcript route: Virlo answers 400 `unsupported_platform`.
That is raised as `VirloUnsupported` so the transcript ladder can fall through
to the caption-and-comments rung, which labels what it is. Spoken claims that
nobody transcribed are never presented as evidence.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
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

logger = logging.getLogger(__name__)

API = "https://api.virlo.ai/v1/raw"
PLATFORMS = ("tiktok", "instagram")
#: Every successful call, on every route, empty pages included.
PRICE_PER_CALL_USD = Decimal("0.01")
#: Instagram search is documented at about 10 seconds; 30 is Virlo's advice.
TIMEOUT = 30.0


class VirloError(RuntimeError):
    """A failed call. `blocking` as in `ApifyError`: auth and an empty balance
    stop the source for an operator; timeouts and 5xx are retried."""

    def __init__(self, message: str, *, blocking: bool = False) -> None:
        super().__init__(message)
        self.blocking = blocking


class VirloUnsupported(VirloError):
    """The platform has no such route (Instagram transcripts)."""


class VirloBudgetReached(VirloError):
    """The next call would pass this run's ceiling. Not charged, not retried."""


class VirloConnector(BaseConnector):
    capabilities = {
        Capability.DISCOVERY,
        Capability.METADATA,
        Capability.TRANSCRIPT,
        Capability.ENGAGEMENT,
        Capability.COMMENTS,
    }

    def __init__(
        self,
        *,
        source,
        policy,
        api_key: str,
        max_calls: int = 0,
        transport: Any | None = None,
        sleep=time.sleep,
    ) -> None:
        super().__init__(source=source, policy=policy)
        if not api_key:
            raise ValueError("Virlo API key is required.")
        self._key = api_key
        self._transport = transport
        self._sleep = sleep
        #: Zero means no ceiling for this connector; collection always sets one.
        self.max_calls = max_calls
        self.calls = 0

    @property
    def cost_usd(self) -> Decimal:
        return PRICE_PER_CALL_USD * self.calls

    # ── Cost, before anything is spent ──────────────────────────────────────

    def estimate_cost(self, plan: FetchPlan) -> CostEstimate:
        calls = max(len(plan.refs), 1)
        return CostEstimate(
            currency="USD", amount=PRICE_PER_CALL_USD * calls, units={"calls": calls}
        )

    # ── Acquisition ─────────────────────────────────────────────────────────

    def search(self, platform: str, query: str, *, pages: int = 2) -> list[dict]:
        """Up to `pages` pages of keyword results, normalised.

        Page size is set by the platform, so pages, not results, is the spend
        control: two pages per topic and platform is Pure Play's model.
        """
        rows: list[dict] = []
        cursor = None
        for _ in range(max(pages, 1)):
            params = {"q": query, **({"cursor": cursor} if cursor else {})}
            body = self._get(f"/{platform}/search", params)
            rows.extend(self._normalise(v, query=query) for v in body.get("data") or [])
            page = body.get("pagination") or {}
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                break
        return rows

    def transcript(self, platform: str, video_id: str) -> tuple[str, list]:
        """The spoken text and its timing anchors; ("", []) when there is none.

        A video with no speech returns `has_transcript: false`. That is a
        normal, charged answer, not an error.
        """
        if platform not in ("tiktok",):
            raise VirloUnsupported(f"Virlo has no transcript route for {platform}.")
        body = self._get(f"/{platform}/video/transcript", {"id": video_id})
        data = body.get("data", body)
        if not data.get("has_transcript"):
            return "", []
        segments = data.get("segments") or []
        if not segments:
            return data.get("text") or "", []
        text, anchors = "", []
        for segment in segments:
            piece = (segment.get("text") or "").strip()
            if not piece:
                continue
            if text:
                text += " "
            if segment.get("start_seconds") is not None:
                anchors.append([float(segment["start_seconds"]), len(text)])
            text += piece
        return text, anchors

    def comments(self, platform: str, video_id: str, *, pages: int = 1) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(max(pages, 1)):
            params = {"id": video_id, **({"cursor": cursor} if cursor else {})}
            body = self._get(f"/{platform}/video/comments", params)
            out.extend(body.get("data") or [])
            page = body.get("pagination") or {}
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                break
        return out

    def discover(self, window: DateWindow) -> Iterable[ItemRef]:
        raise NotImplementedError("Virlo discovery is `search`; it returns full rows.")

    def fetch_metadata(self, refs: list[ItemRef]) -> Iterable[dict]:
        raise NotImplementedError("Search rows already carry the metadata.")

    def fetch_content(self, ref: ItemRef) -> ContentResult:
        raise NotImplementedError("Use `transcript`; the ladder decides the fallback.")

    # ── Internals ───────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict) -> dict:
        if self.max_calls and self.calls >= self.max_calls:
            raise VirloBudgetReached(
                f"Virlo call ceiling reached ({self.max_calls} calls, "
                f"${self.cost_usd}) for {self.source}.",
            )
        for attempt in range(2):
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as http:
                response = http.get(
                    f"{API}{path}", params=params,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
            if response.status_code == 429 and attempt == 0:
                wait = _retry_after(response)
                logger.info("Virlo rate limit; waiting %ss", wait)
                self._sleep(wait)
                continue
            break

        if response.status_code >= 400:
            code = _code(response)
            message = f"Virlo {path} returned {response.status_code} {code}".strip()
            if code == "unsupported_platform":
                raise VirloUnsupported(message)
            # 401: bad key. 402: the prepaid balance is empty. Neither changes
            # by retrying, so the source stops and an operator is told.
            raise VirloError(message, blocking=response.status_code in {401, 402, 403})

        self.calls += 1
        return response.json()

    @staticmethod
    def _normalise(video: dict, *, query: str = "") -> dict:
        platform = video.get("platform") or ""
        caption = (video.get("caption") or "").strip()
        author = video.get("author") or {}
        stats = video.get("stats") or {}
        published = video.get("published_at")
        if isinstance(published, str):
            try:
                published = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                published = None
        handle = author.get("handle") or ""
        return {
            # Prefixed so a TikTok id and an Instagram shortcode can never
            # collide, and so the same video found by two topic streams is
            # recognised as one (`ingest_rows` deduplicates on this).
            "external_id": f"{platform}:{video.get('id')}",
            "platform": platform,
            "title": _title(caption) or f"{platform.title()} video by @{handle}",
            "url": video.get("url") or "",
            "creator": f"@{handle}" if handle else (author.get("display_name") or ""),
            "published_at": published,
            "description": caption,
            "transcript": None,
            "transcript_anchors": [],
            "gate_metadata": {
                "platform": platform,
                "duration": video.get("duration_seconds"),
                "hashtags": video.get("hashtags") or [],
                "verified": author.get("verified"),
                "query": query,
            },
            "engagement": {
                "views": stats.get("views"),
                "likes": stats.get("likes"),
                "comments": stats.get("comments"),
                "shares": stats.get("shares"),
                "saves": stats.get("saves"),
            },
            "route": f"virlo_{platform}",
            "raw_metadata": video,
        }


def _title(caption: str) -> str:
    first = caption.split("\n", 1)[0].strip()
    return first[:200]


def _code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("code") or "")
    except ValueError:
        return ""


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(float(response.json().get("retry_after") or 5), 60.0)
    except (ValueError, AttributeError):
        return 5.0

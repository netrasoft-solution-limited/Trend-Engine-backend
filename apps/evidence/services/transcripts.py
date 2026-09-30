"""Walking the transcript fallback ladder — Arch §7.2.

    "Chains are declarative data, not branching code."

So this module is a registry, not a decision tree. `connectors/chains.py`
declares the order; each rung is a function that either produces a transcript
or explains why it could not. Adding Supadata later means adding one entry to
the registry — no branch here changes.

THE DISTINCTION THAT MATTERS is between the two ways a ladder can end without
a transcript:

  · UNAVAILABLE — no rung can ever serve this item. A YouTube video with
    captions disabled, a podcast nobody transcribed. The item is honestly
    metadata-only (Arch §7.1) and that is a final answer.
  · FAILED — a rung broke transiently. AssemblyAI timed out, Apify 500'd.
    Nothing has been learned about the item at all.

Collapsing those loses real evidence: an item marked metadata-only because
AssemblyAI was down for ten minutes stays metadata-only forever, and extraction
will refuse it for the rest of its life. So a transient failure leaves the item
exactly as it was, for the next scheduled pass to retry — the same reasoning
that makes the relevance gate leave items PENDING rather than rejecting them
when it cannot afford to ask.

This lives in `evidence` rather than `enrichment` because acquiring a
transcript produces evidence; it does not enrich it. `evidence` (L2) may import
`connectors` (L1), which is what lets the whole walk sit in one place.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from apps.connectors.chains import DEFAULT_CHAINS, TERMINAL_STEP
from apps.evidence.models import ContentItem, TranscriptArtifact

logger = logging.getLogger(__name__)


class Status:
    SUCCEEDED = "succeeded"
    #: This rung cannot serve this item, and never will.
    UNAVAILABLE = "unavailable"
    #: This rung broke. Nothing was learned; try again later.
    FAILED = "failed"
    #: Not implemented, or not configured — no credential, no approved policy.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Transcript:
    """What a rung returns when it succeeds."""

    text: str
    anchors: list
    method: str
    quality: int = 60
    cost_usd: Decimal = Decimal("0")


@dataclass(frozen=True)
class RungResult:
    step: str
    status: str
    detail: str = ""


@dataclass
class Acquisition:
    """The outcome of one walk, rung by rung.

    `rungs` is kept whole rather than reduced to a winner because Arch §7.2
    wants the route attributable per item — and because "Taddy said no, Apify
    had no captions, AssemblyAI produced it for $0.11" is the answer to most
    questions anyone later asks about an item's cost.
    """

    item_id: int
    method: str | None = None
    chars: int = 0
    cost_usd: Decimal = Decimal("0")
    rungs: list[RungResult] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.method is not None

    @property
    def retryable(self) -> bool:
        """Whether anything transient stood in the way.

        True means the ladder ended without a transcript BUT something broke
        rather than being absent — so the item must not be written off as
        metadata-only.
        """
        return not self.succeeded and any(r.status == Status.FAILED for r in self.rungs)

    def __str__(self) -> str:
        trail = " → ".join(f"{r.step}:{r.status}" for r in self.rungs)
        if self.succeeded:
            return f"{self.method} produced {self.chars:,} chars (${self.cost_usd}) [{trail}]"
        return f"no transcript [{trail}]"


# ── The rungs ───────────────────────────────────────────────────────────────
# Each returns a Transcript, or raises Unavailable / Failed. Anything a rung
# does not handle propagates as Failed, because an unrecognised error is not
# evidence that the item has no transcript.


class Unavailable(Exception):
    """This rung cannot serve this item. Move on; do not retry later."""


class Failed(Exception):
    """This rung broke. Move on, but the item is still worth retrying."""


def _taddy(item: ContentItem) -> Transcript:
    from apps.connectors.factory import ConnectorUnavailable, taddy_for
    from apps.connectors.providers.taddy import TaddyError, TranscribeStatus

    try:
        connector = taddy_for(item.source)
    except ConnectorUnavailable as exc:
        raise Unavailable(str(exc)) from exc

    try:
        result = connector.transcript(item.external_id)
    except TaddyError as exc:
        # A blocking Taddy error is permanent for this item; a transient one is
        # worth another pass.
        raise (Unavailable if exc.blocking else Failed)(str(exc)) from exc

    if result.status == TranscribeStatus.PLAN_LIMITED:
        raise Unavailable("Taddy holds this transcript but the plan excludes it")
    if not result.text:
        raise Unavailable("Taddy has no transcript for this episode")

    return Transcript(
        text=result.text,
        anchors=result.anchors,
        method=TranscriptArtifact.Method.TADDY,
        # Publisher- or Taddy-generated, with speaker labels and real timings.
        quality=90,
    )


def _apify_subtitles(item: ContentItem) -> Transcript:
    from apps.connectors.factory import ConnectorUnavailable, apify_for
    from apps.connectors.providers.apify import YOUTUBE, ApifyError

    if not item.url:
        raise Unavailable("no URL to fetch captions from")

    try:
        connector = apify_for(item.source)
    except ConnectorUnavailable as exc:
        raise Unavailable(str(exc)) from exc

    try:
        rows = connector.fetch_by_url([item.url])
    except ApifyError as exc:
        raise (Unavailable if exc.blocking else Failed)(str(exc)) from exc

    if not rows or not rows[0].get("transcript"):
        raise Unavailable("no caption track published for this video")

    row = rows[0]
    return Transcript(
        text=row["transcript"],
        anchors=row.get("transcript_anchors") or [],
        method=TranscriptArtifact.Method.APIFY_SUBTITLES,
        # Auto-generated captions: usable, but they mishear exactly the proper
        # nouns that matter here — ingredient names.
        quality=60,
        cost_usd=YOUTUBE.price_per_result_usd,
    )


def _assemblyai(item: ContentItem) -> Transcript:
    from apps.connectors.factory import ConnectorUnavailable, assemblyai_for
    from apps.connectors.providers.assemblyai import AssemblyAIError
    from apps.sources.models import AcquisitionProvider

    if not item.url:
        raise Unavailable("no audio URL to transcribe")

    try:
        connector = assemblyai_for(item.source)
    except ConnectorUnavailable as exc:
        raise Unavailable(str(exc)) from exc

    # Arch §10.2: the cap is checked BEFORE the call. This is the one rung that
    # bills by the hour, so a three-hour episode is where a per-item ceiling
    # earns its keep.
    provider = AcquisitionProvider.objects.filter(
        kind=AcquisitionProvider.Kind.ASSEMBLYAI
    ).first()
    cap = provider.per_run_max_usd if provider and provider.per_run_max_usd else None
    duration = _duration_seconds(item)

    try:
        result = connector.transcribe(
            item.url, max_cost_usd=cap, expected_duration_seconds=duration
        )
    except AssemblyAIError as exc:
        raise (Unavailable if exc.blocking else Failed)(str(exc)) from exc

    if not result.text:
        raise Unavailable("AssemblyAI returned an empty transcript")

    return Transcript(
        text=result.text,
        anchors=result.anchors,
        method=TranscriptArtifact.Method.ASSEMBLYAI,
        quality=result.quality,
        cost_usd=result.cost_usd,
    )


def _duration_seconds(item: ContentItem) -> float | None:
    """Episode length, for pricing an ASR call before making it.

    Connectors put it in `gate_metadata` as seconds (Taddy) or as `MM:SS`
    (Apify). Returning None rather than guessing means the cap is not enforced
    for that item — visible in the logs, and better than pricing a three-hour
    episode as though it were four minutes.
    """
    raw = (item.gate_metadata or {}).get("duration")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and ":" in raw:
        try:
            parts = [int(p) for p in raw.split(":")]
        except ValueError:
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return float(seconds)
    return None


def _not_implemented(name: str) -> Callable[[ContentItem], Transcript]:
    def rung(item: ContentItem) -> Transcript:
        raise Unavailable(f"the '{name}' rung is not built yet")

    return rung


#: step name → how to attempt it. `chains.py` owns the ORDER; this owns the
#: HOW. A rung named in a chain but absent here is skipped and recorded, so an
#: unbuilt rung never breaks a walk.
RUNGS: dict[str, Callable[[ContentItem], Transcript]] = {
    "taddy": _taddy,
    "apify_subtitles": _apify_subtitles,
    "assemblyai": _assemblyai,
    # Named in the chains, not built. Listed explicitly so the log says "not
    # built yet" rather than "unknown rung", which reads like a typo.
    "publisher_transcript": _not_implemented("publisher_transcript"),
    "creator_captions": _not_implemented("creator_captions"),
    "supadata": _not_implemented("supadata"),
    "ncbi_eutils": _not_implemented("ncbi_eutils"),
    "crossref": _not_implemented("crossref"),
    "allowlist_crawler": _not_implemented("allowlist_crawler"),
    # A person uploading a transcript is not something a scheduled job can do.
    "manual": _not_implemented("manual"),
}


# ── The walk ────────────────────────────────────────────────────────────────


def chain_for(item: ContentItem) -> tuple[str, ...]:
    """The ladder for this item's route.

    An unknown route gets the terminal rung alone rather than an exception:
    a source added with a route nobody wrote a chain for should degrade to
    metadata-only, not stop the pipeline.
    """
    route = getattr(item.source, "route", "") or ""
    chain = DEFAULT_CHAINS.get(route)
    if chain is None:
        logger.warning("No fallback chain for route %r; metadata only.", route)
        return (TERMINAL_STEP,)
    return chain


def acquire(item: ContentItem) -> Acquisition:
    """Walk the ladder until a rung produces a transcript.

    Idempotent: an item that already has one is returned untouched. Transcripts
    are the expensive artifact, and `TranscriptArtifact` is one-to-one, so a
    duplicate dispatch must cost nothing rather than fail.
    """
    outcome = Acquisition(item_id=item.pk)

    existing = TranscriptArtifact.objects.filter(content_item=item).first()
    if existing is not None:
        outcome.method = existing.method
        outcome.chars = len(existing.text)
        outcome.rungs.append(RungResult(existing.method, Status.SUCCEEDED, "already held"))
        return outcome

    for step in chain_for(item):
        if step == TERMINAL_STEP:
            outcome.rungs.append(RungResult(step, Status.UNAVAILABLE, "terminal rung"))
            break

        rung = RUNGS.get(step)
        if rung is None:
            outcome.rungs.append(RungResult(step, Status.SKIPPED, "no implementation registered"))
            continue

        try:
            transcript = rung(item)
        except Unavailable as exc:
            outcome.rungs.append(RungResult(step, Status.UNAVAILABLE, str(exc)[:300]))
            continue
        except Failed as exc:
            outcome.rungs.append(RungResult(step, Status.FAILED, str(exc)[:300]))
            continue
        except Exception as exc:
            # An unrecognised error is NOT evidence that the item has no
            # transcript, so it counts as transient and keeps the item alive.
            logger.exception("Rung %s raised unexpectedly for item %s", step, item.pk)
            detail = f"{type(exc).__name__}: {exc}"[:300]
            outcome.rungs.append(RungResult(step, Status.FAILED, detail))
            continue

        _store(item, transcript)
        outcome.method = transcript.method
        outcome.chars = len(transcript.text)
        outcome.cost_usd = transcript.cost_usd
        outcome.rungs.append(RungResult(step, Status.SUCCEEDED))
        break

    if not outcome.succeeded and not outcome.retryable:
        _mark_metadata_only(item)

    logger.info("Transcript ladder for item %s: %s", item.pk, outcome)
    return outcome


@transaction.atomic
def _store(item: ContentItem, transcript: Transcript) -> None:
    TranscriptArtifact.objects.create(
        content_item=item,
        method=transcript.method,
        text=transcript.text,
        anchors=transcript.anchors,
        quality=transcript.quality,
        cost_usd=transcript.cost_usd,
    )
    item.content_state = ContentItem.ContentState.TRANSCRIBED
    # Arch §7.2: the rung that succeeded, recorded per item.
    item.acquisition_route = transcript.method
    item.save(update_fields=["content_state", "acquisition_route"])


def _mark_metadata_only(item: ContentItem) -> None:
    """A final answer, not a failure (Arch §7.1).

    Only reached when every rung said "never", so extraction refusing this item
    from here on is correct rather than a loss.
    """
    if item.content_state == ContentItem.ContentState.TRANSCRIBED:
        return
    item.content_state = ContentItem.ContentState.METADATA
    item.acquisition_route = TERMINAL_STEP
    item.save(update_fields=["content_state", "acquisition_route"])

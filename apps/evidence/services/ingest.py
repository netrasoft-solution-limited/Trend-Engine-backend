"""Connector rows in, evidence out.

This lives in `evidence` rather than `connectors` because of the layer stack:
`evidence` sits above `ingestion` above `connectors`, so this is the lowest
place that can see both a normalised row and the models it becomes. A connector
never imports a model, which is what keeps "swap the vendor" a config change.

Two things matter here and nothing else does.

IDEMPOTENCY (Arch §6.3). The key is `(source, external_id, content_hash)`.
Re-running a collection over a window that has already been collected must not
duplicate evidence, and — the subtler half — must not silently *replace* it
either. An item whose content has genuinely changed hashes differently and
becomes a second row; the first stays, because a claim may already point at it.

HONESTY ABOUT STATE (Arch §7.1). An item with no transcript is `METADATA`, not
`TRANSCRIBED`, and `is_metadata_only` follows from that. Extraction skips it and
the portal labels it. The temptation is to treat a rich description as good
enough; it is not, because a claim extracted from a description is a claim about
what someone *said they would say*.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.evidence.models import ContentItem, TranscriptArtifact

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    created: int = 0
    seen_before: int = 0
    transcripts: int = 0
    metadata_only: int = 0
    items: list[ContentItem] = field(default_factory=list)
    #: The NEW ones, specifically. Kept as its own list rather than left to be
    #: sliced off `items` by count: rows arrive interleaved, so the created
    #: ones are not a prefix, and a caller that assumes they are will gate the
    #: wrong items — silently, since both lists hold ContentItems.
    created_items: list[ContentItem] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.created} new, {self.seen_before} already held, "
            f"{self.transcripts} with transcripts, {self.metadata_only} metadata-only"
        )


def ingest_rows(
    *,
    source,
    policy,
    rows: list[dict],
    run=None,
    transcript_method: str = TranscriptArtifact.Method.APIFY_SUBTITLES,
    transcript_quality: int = 60,
    cost_per_item: Decimal = Decimal("0"),
) -> IngestResult:
    """Persist normalised connector rows as evidence.

    `transcript_quality` defaults to 60 because the common case is an
    auto-generated caption track: usable, but missing punctuation and prone to
    mishearing exactly the proper nouns that matter here — ingredient names.
    A publisher's own transcript is passed a higher score by its caller.
    """
    result = IngestResult()

    for row in rows:
        external_id = (row.get("external_id") or "").strip()
        if not external_id:
            # No stable identity means no idempotency key, so a re-run would
            # duplicate it forever. Refusing is better than accumulating.
            logger.warning("Skipping a row from %s with no external_id", source)
            continue

        text = row.get("transcript") or ""
        content_hash = ContentItem.hash_content(
            external_id, row.get("title") or "", text[:4096]
        )

        with transaction.atomic():
            item, created = ContentItem.objects.get_or_create(
                source=source,
                external_id=external_id,
                content_hash=content_hash,
                defaults=_fields(row, policy=policy, run=run, has_text=bool(text)),
            )

            if created and text:
                TranscriptArtifact.objects.create(
                    content_item=item,
                    method=transcript_method,
                    text=text,
                    anchors=row.get("transcript_anchors") or [],
                    quality=transcript_quality,
                    cost_usd=cost_per_item,
                )

        result.items.append(item)
        if created:
            result.created_items.append(item)
            result.created += 1
            result.transcripts += 1 if text else 0
            result.metadata_only += 0 if text else 1
        else:
            result.seen_before += 1

    logger.info("Ingested from %s: %s", source, result)
    return result


def _fields(row: dict, *, policy, run, has_text: bool) -> dict:
    """The evidence contract's fields, from a normalised row.

    `acquisition_route` records which rung produced this — Arch §7.2 wants the
    route attributable per item, so that a later question about why one episode
    has a transcript and its neighbour does not has an answer in the data.
    """
    return {
        "run": run,
        "policy": policy,
        "url": row.get("url") or "",
        "title": (row.get("title") or "")[:500],
        "creator": (row.get("creator") or "")[:300],
        "published_at": row.get("published_at"),
        "description": row.get("description") or "",
        "gate_metadata": row.get("gate_metadata") or {},
        "raw_metadata": row.get("raw_metadata") or {},
        "content_state": (
            ContentItem.ContentState.TRANSCRIBED
            if has_text
            else ContentItem.ContentState.METADATA
        ),
        "acquisition_route": row.get("route") or "apify_subtitles",
        "fetched_at": timezone.now(),
    }

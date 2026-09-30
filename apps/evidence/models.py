"""evidence — L2, GLOBAL.

PRD §8 models: ContentItem, TranscriptArtifact, ContentSegment, MetricSnapshot,
Tombstone.

Tenant-agnostic by design. Nothing in this app may import from clients,
scoring, outputs, publication, portal or billing (Arch §4).

This is the L2/L5 boundary — "the single most important line in the system"
(Arch §3). Everything here is shared: one podcast episode collected once
supports every client in the vertical. That is why client #2 costs a fraction
of client #1, and why no model in this file carries an organisation.

`ContentItem`, `TranscriptArtifact` and `ContentSegment` exist so far — the
minimum the relevance gate and extraction need. MetricSnapshot and Tombstone
(deletion/retention state, PRD §7.2) are not modelled yet.
"""
from __future__ import annotations

import hashlib

from django.db import models


class ContentItem(models.Model):
    """One collected thing: an episode, a video, a paper, a page.

    Carries the normalized evidence contract from PRD §6.2 — identity, timing,
    content state, provenance and rights. Provider-specific fields stay in
    `raw_metadata` and never leak into scoring or output generation.
    """

    class ContentState(models.TextChoices):
        #: Discovered, nothing fetched beyond what discovery returned.
        DISCOVERED = "discovered", "Discovered"
        #: Title, description, show notes — enough for the relevance gate.
        METADATA = "metadata", "Metadata only"
        #: Full text present and usable for extraction.
        TRANSCRIBED = "transcribed", "Transcribed"

    class GateDecision(models.TextChoices):
        PENDING = "pending", "Not yet assessed"
        RELEVANT = "relevant", "Relevant"
        REJECTED = "rejected", "Rejected"
        #: Rejected, but pulled through anyway by the ~5% sample (Arch §6.2).
        SAMPLED = "sampled", "Rejected but sampled"

    source = models.ForeignKey(
        "sources.Source", on_delete=models.PROTECT, related_name="content_items"
    )
    run = models.ForeignKey(
        "ingestion.IngestionRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="content_items",
    )

    # ── Identity (PRD §6.2) ─────────────────────────────────────────────────
    external_id = models.CharField(max_length=255)
    url = models.URLField(max_length=1000, blank=True)
    title = models.CharField(max_length=500)
    creator = models.CharField(max_length=300, blank=True)

    # ── Timing ──────────────────────────────────────────────────────────────
    published_at = models.DateTimeField(null=True, blank=True)
    discovered_at = models.DateTimeField(auto_now_add=True)
    fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Content ─────────────────────────────────────────────────────────────
    language = models.CharField(max_length=12, default="en")
    description = models.TextField(blank=True)
    #: Show notes, chapter titles, guest names — what the relevance gate reads.
    #: PRD §6.1 and Arch §6.2: the gate sees metadata ONLY, never audio and
    #: never a full transcript, because fetching those is the expensive step
    #: the gate exists to avoid.
    gate_metadata = models.JSONField(default=dict, blank=True)
    content_state = models.CharField(
        max_length=16, choices=ContentState.choices, default=ContentState.DISCOVERED
    )

    #: Arch §6.3 idempotency key is (source, external_id, content_hash).
    #: Re-running a job never duplicates evidence.
    content_hash = models.CharField(max_length=64, db_index=True)

    # ── Relevance gate (Arch §6.2) ──────────────────────────────────────────
    gate_decision = models.CharField(
        max_length=16, choices=GateDecision.choices, default=GateDecision.PENDING, db_index=True
    )
    #: Stored so filter quality is auditable and tunable, per Arch §6.2.
    gate_reason = models.TextField(blank=True)
    gate_assessed_at = models.DateTimeField(null=True, blank=True)

    # ── Provenance & rights (PRD §6.2, §7.2) ────────────────────────────────
    #: Which fallback rung produced the content — Arch §7.2 requires the route
    #: to be attributable per item, for provenance and for cost.
    acquisition_route = models.CharField(max_length=64, blank=True)
    raw_metadata = models.JSONField(default=dict, blank=True)
    #: PRD §7.2: no connector runs without a recorded policy and access basis.
    policy = models.ForeignKey(
        "sources.ProviderPolicyVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="content_items",
    )

    class Meta:
        ordering = ("-published_at", "-discovered_at")
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id", "content_hash"],
                name="uniq_content_idempotency",
            )
        ]
        indexes = [
            models.Index(fields=["content_state", "gate_decision"], name="content_state_gate"),
        ]

    def __str__(self) -> str:
        return self.title[:80]

    @property
    def is_metadata_only(self) -> bool:
        """Arch §7.1: metadata-only items must be surfaced honestly as such,
        never presented as fully understood content. Extraction skips them."""
        return self.content_state != self.ContentState.TRANSCRIBED

    @staticmethod
    def hash_content(*parts: str) -> str:
        digest = hashlib.sha256()
        for part in parts:
            digest.update((part or "").encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()


class TranscriptArtifact(models.Model):
    """The full text of one item, and how it was obtained.

    Kept apart from ContentItem because the route, cost and quality of a
    transcript are facts about the ACQUISITION, not about the thing acquired —
    and because one item may be re-transcribed by a different rung later.
    """

    class Method(models.TextChoices):
        PUBLISHER = "publisher_transcript", "Publisher transcript"
        TADDY = "taddy", "Taddy"
        CREATOR_CAPTIONS = "creator_captions", "Creator captions"
        APIFY_SUBTITLES = "apify_subtitles", "Apify subtitles"
        ASSEMBLYAI = "assemblyai", "AssemblyAI"
        MANUAL = "manual", "Manual import"

    content_item = models.OneToOneField(
        "evidence.ContentItem", on_delete=models.CASCADE, related_name="transcript"
    )
    method = models.CharField(max_length=32, choices=Method.choices)
    text = models.TextField()
    #: 0-100. Publisher transcripts score high; ASR on poor audio scores low.
    quality = models.PositiveSmallIntegerField(default=0)
    #: What this rung cost, so transcript spend is attributable per item.
    cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.content_item_id} via {self.method}"


class ContentSegment(models.Model):
    """A span of one item's text.

    PRD §6.2: "Long text is stored in segments so evidence references stable
    segment IDs with character or timestamp offsets." Every claim the system
    later makes points at one of these, which is what makes an output's
    evidence resolvable rather than asserted.
    """

    content_item = models.ForeignKey(
        "evidence.ContentItem", on_delete=models.CASCADE, related_name="segments"
    )
    ordinal = models.PositiveIntegerField()
    text = models.TextField()
    #: Character offsets into the transcript, so a span survives re-segmentation.
    start_char = models.PositiveIntegerField()
    end_char = models.PositiveIntegerField()
    #: Seconds, where the source is timed (audio/video). Null for text sources.
    start_seconds = models.FloatField(null=True, blank=True)
    end_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ("content_item", "ordinal")
        constraints = [
            models.UniqueConstraint(
                fields=["content_item", "ordinal"], name="uniq_segment_ordinal"
            )
        ]

    def __str__(self) -> str:
        return f"{self.content_item_id}#{self.ordinal}"

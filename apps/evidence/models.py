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
from urllib.parse import urlsplit

from django.db import models

#: How many ladder walks an item keeps. A retryable item can be walked on every
#: poll; the recent history answers "why no transcript yet" without growing
#: without bound.
MAX_TRANSCRIPT_ATTEMPTS = 10


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
    # ── Same conversation, published twice (PRD §6.3) ──────────────────────
    #: A podcast episode, its YouTube upload and clips cut from either are one
    #: conversation, and must not count as independent confirmation. Set by
    #: `services.duplicates`. CONFIRMED (transcript overlap) changes counting:
    #: the item adds no item, creator or source type to any score and is not
    #: extracted. SUSPECTED (metadata only) is recorded and shown, never acted on.
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates"
    )
    duplicate_status = models.CharField(
        max_length=12,
        choices=[("", "Not a duplicate"), ("confirmed", "Same conversation"),
                 ("suspected", "Suspected same conversation")],
        blank=True,
        default="",
    )
    #: Share of the shorter transcript found in the longer one (0–1), or the
    #: title similarity for a suspected match.
    duplicate_overlap = models.FloatField(null=True, blank=True)
    duplicate_reason = models.CharField(max_length=300, blank=True)

    #: Every walk of the transcript fallback ladder, oldest first: when, each
    #: step's outcome in its own words, and what it produced. PRD §14 asks that
    #: every item show its transcript source, method, status and cost; the
    #: winning step alone cannot say why the cheaper ones were passed over.
    #: Capped at the last `MAX_TRANSCRIPT_ATTEMPTS` walks.
    transcript_attempts = models.JSONField(default=list, blank=True)
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

    @property
    def canonical(self) -> ContentItem:
        """The item this conversation counts as. Itself unless it is a
        confirmed duplicate; chains are followed to their root."""
        item, seen = self, set()
        while (
            item.duplicate_status == "confirmed"
            and item.duplicate_of_id
            and item.pk not in seen
        ):
            seen.add(item.pk)
            item = item.duplicate_of
        return item

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_status == "confirmed" and self.duplicate_of_id is not None

    def link_at(self, seconds: float | None) -> str:
        """The source URL, opened at a moment where the platform supports it.

        YouTube honours `t=`, so a reader can check a quote in one click.
        Podcast episode pages have no common convention, so they get the plain
        URL and the reader is given the timestamp alongside it.
        """
        if not self.url:
            return ""
        if seconds is None or seconds < 0:
            return self.url
        host = urlsplit(self.url).netloc.lower()
        if host.endswith("youtube.com") or host.endswith("youtu.be"):
            separator = "&" if "?" in self.url else "?"
            return f"{self.url}{separator}t={int(seconds)}s"
        return self.url

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
    #: `[[start_seconds, char_index], ...]` — where each piece of speech begins,
    #: in both the audio and the flattened text. Captions and ASR carry timing;
    #: an article does not, so this is empty for text sources. It is what lets
    #: `ContentSegment.start_seconds` be a real position rather than a guess,
    #: and so what makes "at 14:32 he says" resolvable from a stored claim.
    anchors = models.JSONField(default=list, blank=True)
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


class Observation(models.Model):
    """One piece of evidence entered by hand: a post, a listing, a search figure.

    The traceable route for what is not collected automatically — Reddit,
    Instagram, TikTok Shop, keyword estimates, Google Trends — agreed with Pure
    Play on 21 Oct 2026. No paid access is assumed.

    Mark's rules, enforced here rather than remembered:

    - Every observation has its URL, its date, and its source type.
    - A figure says what KIND of figure it is. A rolling monthly search
      estimate, Google Trends relative interest, cumulative engagement and
      lifetime Shop sales measure different things and are never added,
      compared as like for like, or read as recent growth.
    - A search figure says its query, provider, geography and period.
    - Ads, brand content, old material and missing coverage are labelled.
    - `event_key` marks observations that are one underlying event — the same
      interview in audio and video, or repeated coverage of one study — so
      they count once towards independent corroboration.
    """

    class SourceType(models.TextChoices):
        REDDIT = "reddit", "Reddit / community"
        INSTAGRAM = "instagram", "Instagram"
        TIKTOK_SHOP = "tiktok_shop", "TikTok Shop"
        TIKTOK = "tiktok", "TikTok"
        YOUTUBE = "youtube", "YouTube"
        PODCAST = "podcast", "Podcast"
        WEB = "web", "Web page or article"
        RESEARCH = "research", "Research paper or registry"
        SEARCH_ESTIMATE = "search_estimate", "Search volume estimate"
        GOOGLE_TRENDS = "google_trends", "Google Trends"
        CATALOGUE = "catalogue", "Product or catalogue page"
        OTHER = "other", "Other"

    class MetricKind(models.TextChoices):
        NONE = "", "No figure"
        SEARCH_ESTIMATE = "rolling_monthly_search_estimate", "Rolling monthly search estimate"
        TRENDS = "trends_relative_interest", "Google Trends relative interest"
        ENGAGEMENT = "cumulative_engagement", "Cumulative engagement at observation"
        SHOP_SALES = "lifetime_shop_sales", "Global lifetime items sold"
        VIEWS = "cumulative_views", "Cumulative views at observation"
        OTHER = "other", "Other figure (say what in the label)"

    #: The kinds whose figure is meaningless without query, provider,
    #: geography and period.
    SEARCH_KINDS = (MetricKind.SEARCH_ESTIMATE, MetricKind.TRENDS)

    url = models.URLField(max_length=1000)
    title = models.CharField(max_length=500)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    #: When it was published, if known; when it was looked at, always.
    published_at = models.DateField(null=True, blank=True)
    observed_at = models.DateField()
    #: What it is about, in plain words — "protein and digestion",
    #: "spermidine". The link to signals and candidates is explicit; this is for
    #: finding it.
    topic = models.CharField(max_length=200, blank=True, db_index=True)
    excerpt = models.TextField(blank=True, help_text="What it says, quoted or summarised.")

    metric_kind = models.CharField(
        max_length=40, choices=MetricKind.choices, blank=True, default=""
    )
    metric_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    metric_label = models.CharField(
        max_length=200, blank=True, help_text="e.g. '4.6K items sold, 383 reviews'"
    )
    query = models.CharField(max_length=300, blank=True)
    provider = models.CharField(max_length=200, blank=True)
    geography = models.CharField(max_length=100, blank=True)
    period = models.CharField(max_length=100, blank=True)

    is_ad = models.BooleanField(default=False)
    is_brand_content = models.BooleanField(default=False)
    is_old_material = models.BooleanField(default=False)
    coverage_gap = models.BooleanField(default=False)
    limitations = models.TextField(blank=True)

    #: Same underlying event → same key → counted once. Blank means its own event.
    event_key = models.CharField(max_length=200, blank=True, db_index=True)

    entered_by = models.CharField(max_length=200)
    entered_at = models.DateTimeField(auto_now_add=True)
    import_batch = models.CharField(max_length=100, blank=True)
    #: Where the entry came from, when it is a transcription of someone else's
    #: research — e.g. "Pure Play Portfolio Trend Brief, Oct 2026, source [2]".
    provenance = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ("-observed_at", "-pk")

    def __str__(self) -> str:
        return f"{self.get_source_type_display()}: {self.title[:60]}"

    @property
    def independence_key(self) -> str:
        return self.event_key.strip().lower() or f"observation-{self.pk}"

    @property
    def labels(self) -> list[str]:
        out = []
        if self.is_ad:
            out.append("advertisement")
        if self.is_brand_content:
            out.append("brand content")
        if self.is_old_material:
            out.append("older material")
        if self.coverage_gap:
            out.append("missing coverage")
        return out

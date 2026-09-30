"""enrichment — L3-L4, GLOBAL.

PRD §8 models: Entity, DomainEntity, EntityMention, Topic, TopicMention, Claim,
Question, Relationship, ModelRun, DomainAnalysisRun.

Tenant-agnostic by design. Nothing in this app may import from clients,
scoring, outputs, publication, portal or billing (Arch §4). Extraction happens
ONCE per content item and serves every tenant — Arch §10.3 calls this "the
multi-client economic advantage, expressed in code", because ~80% of LLM cost
sits here and is shared, while only scoring and drafting duplicate per client.

`ModelRun`, `Claim` and `Question` exist so far — what the relevance gate and
extraction need. Entity/Topic resolution, Relationship and DomainAnalysisRun
are not modelled yet.
"""
from __future__ import annotations

from django.db import models


class ModelRun(models.Model):
    """One call to a model, and everything needed to explain it later.

    Arch §8.2: "if components aren't persisted at write time, no amount of
    frontend work can reconstruct them later." The same argument applies to the
    call itself — which model, which prompt version, what it cost. Arch §10.3
    adds the reason this is not optional: the $180–250/month figure is a
    RESERVE, not a measurement, and these rows are what eventually replace it
    with a real number.
    """

    class Purpose(models.TextChoices):
        #: Cheap tier, metadata only, high volume (Arch §6.2).
        RELEVANCE_GATE = "relevance_gate", "Relevance gate"
        #: Stronger tier, full text, once per item ever.
        EXTRACTION = "extraction", "Extraction"
        #: Strongest tier, client-facing (Arch §10.3).
        DRAFTING = "drafting", "Drafting"

    class Outcome(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        REFUSED = "refused", "Model declined"
        INVALID = "invalid", "Output failed validation"
        ERRORED = "errored", "Call failed"
        #: Refused before the call, by the cost guard (Arch §10.2).
        CAPPED = "capped", "Refused by cost cap"

    purpose = models.CharField(max_length=32, choices=Purpose.choices, db_index=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)

    #: The exact model string sent. Not a tier name — "the cheap one" is not
    #: reproducible, `claude-haiku-4-5` is.
    model = models.CharField(max_length=64)
    #: Versioned separately from the code, so a prompt change is visible in the
    #: data without a deploy being needed to explain it.
    prompt_version = models.CharField(max_length=64)

    content_item = models.ForeignKey(
        "evidence.ContentItem",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="model_runs",
    )

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    #: Split out because a cache read is ~10x cheaper than a fresh input token,
    #: so a bill that ignores the split is wrong (Arch §10.3).
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)

    latency_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-at",)
        indexes = [models.Index(fields=["purpose", "-at"], name="modelrun_purpose_at")]

    def __str__(self) -> str:
        return f"{self.purpose} {self.model} ${self.cost_usd}"


class Claim(models.Model):
    """A factual assertion made in the evidence, with where it was said.

    PRD §5 principle 1: "No recommendation exists without a stored signal and
    supporting evidence spans." A claim with no segment is not evidence, so
    `segment` is required — extraction drops any claim it cannot locate.
    """

    class Kind(models.TextChoices):
        MECHANISM = "mechanism", "Mechanism"
        EFFECT = "effect", "Effect or outcome"
        DOSAGE = "dosage", "Dose or protocol"
        SAFETY = "safety", "Safety or interaction"
        COMPARISON = "comparison", "Comparison"
        MARKET = "market", "Market or product"

    content_item = models.ForeignKey(
        "evidence.ContentItem", on_delete=models.CASCADE, related_name="claims"
    )
    segment = models.ForeignKey(
        "evidence.ContentSegment", on_delete=models.CASCADE, related_name="claims"
    )
    model_run = models.ForeignKey(
        "enrichment.ModelRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="claims",
    )

    kind = models.CharField(max_length=16, choices=Kind.choices)
    text = models.TextField()
    #: The subject in the domain's own terms — "creatine monohydrate", not "it".
    subject = models.CharField(max_length=200, blank=True)
    #: Verbatim from the segment. PRD §6.6 requires quoted and paraphrased
    #: claims to map back to stored spans, so the exact words are kept.
    quote = models.TextField(blank=True)

    #: Whether the speaker cited anything. Practitioner commentary and a cited
    #: trial are both evidence — of different weight — and conflating them is
    #: the failure PRD §6.3's separate scientific track exists to prevent.
    is_cited = models.BooleanField(default=False)
    #: PRD §6.1: sponsored content is separated where identifiable.
    is_sponsored = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("content_item", "segment")

    def __str__(self) -> str:
        return self.text[:80]


class Question(models.Model):
    """Something an audience asked, or a host answered unprompted.

    PRD §6.3 lists "recurring consumer question or unmet use case" as a signal
    type in its own right. A question is not a claim: it carries no assertion
    and must never be treated as one downstream.
    """

    content_item = models.ForeignKey(
        "evidence.ContentItem", on_delete=models.CASCADE, related_name="questions"
    )
    segment = models.ForeignKey(
        "evidence.ContentSegment", on_delete=models.CASCADE, related_name="questions"
    )
    model_run = models.ForeignKey(
        "enrichment.ModelRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="questions",
    )

    text = models.TextField()
    topic = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("content_item", "segment")

    def __str__(self) -> str:
        return self.text[:80]

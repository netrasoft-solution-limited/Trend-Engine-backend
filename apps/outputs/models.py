"""outputs — L6, TENANT-SCOPED.

PRD §8 models: Output, OutputVersion, ExportArtifact, Approval, ExpertReview.

Every model here carries `organization` and uses TenantScopedManager as its
default manager. That is not optional: an unscoped default manager on a tenant
model is the leak Arch §5.2 exists to prevent.

Nothing under `apps/portal/` may import this module. The portal resolves
content exclusively through `apps.publication` (Arch §9.3), enforced by the
`gate-is-the-only-door` contract in .importlinter.
"""
from __future__ import annotations

from django.db import models

from apps.tenancy.managers import TenantScopedModel


class OutputType(models.TextChoices):
    """The six types PRD §6.6 defines. `requires_expert_review` is derived in
    `Output.requires_expert_review` rather than stored, so it cannot drift per
    row."""

    TREND_BRIEF = "trend_brief", "Trend intelligence brief"
    CONTENT_QUEUE = "content_queue", "Monthly content queue"
    CONTENT_BRIEF = "content_brief", "Content brief / draft"
    PRODUCT_MEMO = "product_memo", "Product / opportunity memo"
    RESEARCH_ALERT = "research_alert", "Research alert"
    VISIBILITY_BENCHMARK = "visibility_benchmark", "Visibility benchmark"


#: Health and scientific types. Arch §9.2 requires a recorded expert sign-off
#: BEFORE publication for these — a stricter gate than internal approval.
EXPERT_REVIEW_REQUIRED = frozenset(
    {
        OutputType.TREND_BRIEF,
        OutputType.CONTENT_BRIEF,
        OutputType.PRODUCT_MEMO,
        OutputType.RESEARCH_ALERT,
    }
)


class OutputState(models.TextChoices):
    """PRD §6.5, implemented exactly.

    `published` is distinct from `approved` and from export. Export is an
    ACTION on an approved version, never a state.
    """

    DRAFTING = "drafting", "Drafting"
    DRAFT = "draft", "Draft"
    REVIEW_READY = "review ready", "Review ready"
    APPROVED = "approved", "Approved"
    PUBLISHED = "published", "Published"
    DELIVERED = "delivered", "Delivered"
    ARCHIVED = "archived", "Archived"


class Output(TenantScopedModel):
    """One deliverable, across all its versions."""

    type = models.CharField(max_length=32, choices=OutputType.choices)
    title = models.CharField(max_length=300)
    state = models.CharField(
        max_length=20, choices=OutputState.choices, default=OutputState.DRAFTING
    )

    #: PRD §6.4: every output records exactly one tenant, one client-profile
    #: version and one domain-pack version. The tenant is `organization`.
    client_profile_version = models.CharField(max_length=64, blank=True)
    domain_pack_version = models.CharField(max_length=64, blank=True)

    #: Demonstration or test content, not a finding. Set once when the output
    #: is first drafted and carried onto every export as a notice, because a
    #: document that leaves this system can no longer be told apart by where
    #: it came from. Always on for a fixture organisation.
    is_illustrative = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)

    def __str__(self) -> str:
        return f"{self.get_type_display()} — {self.title}"

    @property
    def requires_expert_review(self) -> bool:
        return self.type in EXPERT_REVIEW_REQUIRED


class OutputVersion(TenantScopedModel):
    """An exact version. Version history is INTERNAL — PRD §6.9 allows only one
    published version visible to a tenant at a time, and the portal never sees
    this model at all."""

    output = models.ForeignKey("outputs.Output", on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    summary = models.CharField(max_length=500, blank=True)

    #: The rendered body, as ordered sections. Snapshotted onto a Publication
    #: when published, so the portal never resolves back to this row.
    body = models.JSONField(default=list)

    state = models.CharField(max_length=20, choices=OutputState.choices, default=OutputState.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by_label = models.CharField(max_length=254, blank=True)

    class Meta:
        ordering = ("-number",)
        constraints = [
            models.UniqueConstraint(fields=["output", "number"], name="uniq_version_per_output")
        ]

    def __str__(self) -> str:
        return f"{self.output_id} v{self.number}"


class Approval(TenantScopedModel):
    """Internal sign-off on an exact version. Does NOT make it client-visible."""

    version = models.ForeignKey(
        "outputs.OutputVersion", on_delete=models.CASCADE, related_name="approvals"
    )
    stage = models.CharField(max_length=64)
    approved_at = models.DateTimeField(auto_now_add=True)
    #: Denormalised: approvals may come from operators or external reviewers.
    approver_label = models.CharField(max_length=254)

    class Meta:
        ordering = ("-approved_at",)


class ExpertReview(TenantScopedModel):
    """Arch §9.2: health and scientific outputs require a recorded sign-off
    BEFORE publication, not merely before approval.

    `signed_off_at` being null is what blocks `publication.services.publish`.
    """

    output = models.ForeignKey(
        "outputs.Output", on_delete=models.CASCADE, related_name="expert_reviews"
    )
    reviewer_label = models.CharField(max_length=254)
    discipline = models.CharField(max_length=120, default="Scientific / legal claims")
    signed_off_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ("-signed_off_at",)

    @property
    def is_signed_off(self) -> bool:
        return self.signed_off_at is not None


class ExportArtifact(TenantScopedModel):
    """A record that a document was rendered — PRD §8, PRD §7.1.

    THE EVENT, NOT THE BYTES. `STORAGES["default"]` is S3 and no bucket or
    credentials are configured, so a `FileField.save()` here would raise a boto3
    credentials error inside the delivery transaction. The bytes are in memory
    for the email anyway, and a file can be re-rendered on demand — which is
    exactly what `sha256` and `renderer_version` make verifiable.

    THE HASH IS ONLY WORTH STORING BECAUSE THE RENDERERS ARE DETERMINISTIC.
    `tests/outputs/test_exports.py` asserts byte-equality across renders. Without
    that, this column would identify a blob nobody has, while looking like
    evidence — worse than storing nothing. `renderer_version` is what
    distinguishes "the content changed" from "the template changed" when a
    regenerated file does not match.

    No FK to `Publication`: that model is a layer above this one. The
    client-facing fact — who it was sent to — is `publication.Delivery`.
    """

    class Format(models.TextChoices):
        PDF = "pdf", "PDF"
        DOCX = "docx", "Word"
        HTML = "html", "Web page"
        MARKDOWN = "markdown", "Markdown"
        CSV = "csv", "Spreadsheet"

    version = models.ForeignKey(
        OutputVersion, on_delete=models.CASCADE, related_name="exports"
    )
    format = models.CharField(max_length=16, choices=Format.choices)
    filename = models.CharField(max_length=300)
    byte_size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    renderer_version = models.CharField(max_length=16, default="1")
    #: Whether the rendered file carried the DRAFT watermark. An export of an
    #: unpublished version is legitimate — the operator may be reviewing it —
    #: but it has to be distinguishable afterwards from one that went out.
    was_draft = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by_label = models.CharField(max_length=254, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.filename} ({self.get_format_display()})"


class ExpertReviewer(models.Model):
    """Who may record an expert sign-off (PRD §6.6, Arch §9.2).

    Global, not per tenant: the reviewer is Pure Play's expert, engaged across
    clients. Until 20 Oct a sign-off accepted any typed name, which is how the
    1 October brief came to carry "Dr M. Alvarez, nutrition science" — the
    placeholder our own tests use — with no review behind it. A sign-off now
    has to name someone on this list, and that someone cannot be the operator
    recording it.
    """

    name = models.CharField(max_length=200, unique=True)
    qualification = models.CharField(
        max_length=200, help_text="e.g. Registered dietitian; PhD nutrition science."
    )
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    added_at = models.DateTimeField(auto_now_add=True)
    added_by_label = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name}, {self.qualification}"


class RecordNote(TenantScopedModel):
    """A correction or clarification attached to an output, after the fact.

    Append-only, and never part of an export: the artifact a client received
    (or, on 1 October, did not) stays exactly as it was, and the note travels
    with the record in the operator app instead. Rewriting history to fix it is
    the thing this exists to avoid.
    """

    output = models.ForeignKey(Output, on_delete=models.CASCADE, related_name="record_notes")
    text = models.TextField()
    author_label = models.CharField(max_length=200)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("output", "at")

    def __str__(self) -> str:
        return f"Note on output {self.output_id} by {self.author_label}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Record notes are written once; add another note to correct one.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Record notes cannot be deleted.")

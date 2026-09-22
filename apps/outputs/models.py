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
    state = models.CharField(max_length=20, choices=OutputState.choices, default=OutputState.DRAFTING)

    #: PRD §6.4: every output records exactly one tenant, one client-profile
    #: version and one domain-pack version. The tenant is `organization`.
    client_profile_version = models.CharField(max_length=64, blank=True)
    domain_pack_version = models.CharField(max_length=64, blank=True)

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

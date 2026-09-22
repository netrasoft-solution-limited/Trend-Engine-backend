"""publication — L6–L7, TENANT-SCOPED. THE GATE.

PRD §8 models: Publication.

Arch §9.2: "The portal resolves content exclusively through a Publication
record. There is no query path from portal views to Output or OutputVersion."

That is why `Publication` carries a frozen SNAPSHOT of the version that went
live — `title`, `summary`, `body` — rather than reading through to
`OutputVersion`. Two consequences worth keeping:

  · The portal can render a publication without the outputs app being
    reachable at all, so the import contract holds without an exception.
  · What the client read stays what the client read, even after the output is
    edited. A withdrawal notice that silently re-rendered the current draft
    would be worse than useless.
"""
from __future__ import annotations

from django.db import models

from apps.tenancy.managers import TenantScopedModel


class Publication(TenantScopedModel):
    """One version, made visible to one tenant, at one moment.

    PRD §6.9:
      · publication is a separate, explicit action from approval
      · it is reversible, and the reversal is audited
      · only one published version per output per tenant is visible at a time
    """

    output = models.ForeignKey(
        "outputs.Output", on_delete=models.PROTECT, related_name="publications"
    )
    version = models.ForeignKey(
        "outputs.OutputVersion", on_delete=models.PROTECT, related_name="publications"
    )
    version_number = models.PositiveIntegerField()

    # ── Frozen snapshot: what the client actually sees ──────────────────────
    type = models.CharField(max_length=32)
    type_label = models.CharField(max_length=64)
    title = models.CharField(max_length=300)
    summary = models.TextField(blank=True)
    body = models.JSONField(default=list)

    published_at = models.DateTimeField(auto_now_add=True, db_index=True)
    published_by_label = models.CharField(max_length=254)

    #: Null means live. Set means withdrawn — the row stays for the audit trail,
    #: but `published_for()` must never return it.
    unpublished_at = models.DateTimeField(null=True, blank=True)
    unpublished_by_label = models.CharField(max_length=254, blank=True)
    unpublished_reason = models.TextField(blank=True)

    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-published_at",)
        indexes = [models.Index(fields=["organization", "unpublished_at", "-published_at"])]
        constraints = [
            # At most one LIVE publication per output per tenant. Postgres
            # treats NULLs as distinct, so this partial unique index is what
            # actually enforces PRD §6.9's "only one published version visible
            # at a time" — an application-level check would race.
            models.UniqueConstraint(
                fields=["organization", "output"],
                condition=models.Q(unpublished_at__isnull=True),
                name="uniq_live_publication_per_output",
            )
        ]

    def __str__(self) -> str:
        return f"{self.title} v{self.version_number}"

    @property
    def is_live(self) -> bool:
        return self.unpublished_at is None

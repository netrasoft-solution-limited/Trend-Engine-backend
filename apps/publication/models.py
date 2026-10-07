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


class Delivery(TenantScopedModel):
    """A publication was sent to named people. The client-facing fact.

    Separate from `outputs.ExportArtifact`, which records that a document was
    RENDERED. Rendering is internal and happens whenever an operator downloads
    something; delivery is the moment content leaves for a client, and the two
    are different audit facts with different consequences. Keeping the recipient
    list here also puts client PII at L7 beside the gate rather than at L6.

    RECIPIENTS ARE DENORMALISED EMAIL STRINGS, not a relation to
    `clients.ClientContact`. A delivery record has to survive the contact row
    being edited or deactivated: "who did this actually go to?" must stay
    answerable after someone leaves the client's team, and a foreign key would
    answer it with today's list instead of that day's.

    Not unique per publication — re-delivering to a newly added contact is
    legitimate, and each attempt is its own row, so the trail shows how many
    times something went out and to whom.
    """

    publication = models.ForeignKey(
        Publication, on_delete=models.PROTECT, related_name="deliveries"
    )
    recipients = models.JSONField(default=list)
    formats = models.JSONField(default=list)
    #: The covering note the operator wrote, if any. Part of what the client
    #: received, so it belongs in the record of what was sent.
    note = models.TextField(blank=True)
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_by_label = models.CharField(max_length=254)

    class Meta:
        ordering = ("-sent_at",)
        indexes = [models.Index(fields=["organization", "-sent_at"])]
        verbose_name_plural = "deliveries"

    def __str__(self) -> str:
        return f"{self.publication_id} to {len(self.recipients)} recipient(s)"

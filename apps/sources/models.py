"""sources — L1, GLOBAL.

PRD §8 models: AcquisitionProvider, ProviderPolicyVersion, Source, DomainSource, ClientSource.

Tenant-agnostic by design. Nothing in this app may import from
clients, scoring, outputs, publication, portal or billing (Arch §4).

`Source`, `AcquisitionProvider` and `ProviderPolicyVersion` exist so far. The
domain/client joins (DomainSource, ClientSource) are not modelled yet.

PRD §7.2 and Arch §7.1 are the reason the policy model exists at all: "No
connector runs without a recorded provider policy and access basis. Compliance
is a precondition, not a follow-up." `apps/connectors/base.py` already refuses
to construct without one — until now there was no model that could supply it.
"""
from __future__ import annotations

from django.db import models


class Source(models.Model):
    """One place evidence is collected from — a feed, a channel set, a query."""

    class Route(models.TextChoices):
        PODCAST = "podcast", "Podcast"
        YOUTUBE = "youtube", "YouTube"
        RESEARCH = "research", "Research"
        SOCIAL = "social", "Social"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DEGRADED = "degraded", "Degraded"
        #: Counts as a blocking failure on the Triage home.
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=200, unique=True)
    route = models.CharField(max_length=16, choices=Route.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    last_success_at = models.DateTimeField(null=True, blank=True)

    #: PRD §7.2: a source cannot collect without an access basis. Nullable at
    #: the database level only so existing rows migrate; `can_collect` is what
    #: the pipeline checks, and it refuses when this is unset.
    policy = models.ForeignKey(
        "sources.ProviderPolicyVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sources",
    )

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @property
    def can_collect(self) -> bool:
        """No policy, no run — Arch §7.1.

        Checked before a run starts rather than relied upon at the connector,
        so a source with a withdrawn policy stops collecting immediately rather
        than at its next construction.
        """
        return self.status != self.Status.FAILED and self.policy is not None and self.policy.is_live


class AcquisitionProvider(models.Model):
    """A vendor evidence is acquired through — Taddy, Apify, AssemblyAI, NCBI.

    Separate from `Source` because one provider serves many sources, and
    because Arch §7.3's "one Apify connector, many Actors" means the provider,
    not the source, is what carries credentials and caps.
    """

    class Kind(models.TextChoices):
        TADDY = "taddy", "Taddy"
        APIFY = "apify", "Apify"
        ASSEMBLYAI = "assemblyai", "AssemblyAI"
        NCBI = "ncbi", "NCBI E-utilities"
        CROSSREF = "crossref", "Crossref"
        RSS = "rss", "Direct RSS"
        MANUAL = "manual", "Manual import"

    kind = models.CharField(max_length=32, choices=Kind.choices, unique=True)
    name = models.CharField(max_length=200)

    #: Arch §10.2: enforced BEFORE a run starts, not reconciled after. Zero
    #: means unmetered (a free public API), not "no budget".
    monthly_cap_usd = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    per_run_max_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    #: Auto-paused at 100% of cap. An operator clears this deliberately.
    paused_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class ProviderPolicyVersion(models.Model):
    """The recorded access basis a connector runs under.

    PRD §6.2 requires every item to carry rights provenance: access basis,
    policy version, licence, retention, deletion and attribution obligations,
    and the excerpt length permitted. This is where those live, versioned — so
    an item collected in March is still governed by March's terms after the
    provider changes them in June.

    Arch §12: a gated source cannot become active without one of these
    approved. That is enforced in `is_live`, not left to operator discipline.
    """

    provider = models.ForeignKey(
        "sources.AcquisitionProvider", on_delete=models.CASCADE, related_name="policies"
    )
    version = models.CharField(max_length=32)

    #: The legal route to the data, in a sentence an operator could defend.
    access_basis = models.TextField()
    licence = models.CharField(max_length=200, blank=True)
    attribution_required = models.BooleanField(default=False)
    #: 0 means full retention is permitted; otherwise the maximum excerpt in
    #: characters that may be stored or reproduced (PRD §7.2).
    max_excerpt_chars = models.PositiveIntegerField(default=0)
    retention_days = models.PositiveIntegerField(null=True, blank=True)
    #: Whether the provider obliges us to honour upstream deletions.
    honours_deletion = models.BooleanField(default=True)

    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by_label = models.CharField(max_length=254, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "version"], name="uniq_policy_version_per_provider"
            )
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.version}"

    @property
    def is_live(self) -> bool:
        """Approved and not withdrawn. An unapproved policy is not a policy."""
        return self.approved_at is not None and self.withdrawn_at is None

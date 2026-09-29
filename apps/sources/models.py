"""sources — L1, GLOBAL.

PRD §8 models: AcquisitionProvider, ProviderPolicyVersion, Source, DomainSource, ClientSource.

Tenant-agnostic by design. Nothing in this app may import from
clients, scoring, outputs, publication, portal or billing (Arch §4).

Only `Source` exists so far, with the fields the Triage home's health counts
need. Providers, policy versions and the domain/client joins are not modelled
yet.
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

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

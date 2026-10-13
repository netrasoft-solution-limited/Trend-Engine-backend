"""ingestion — L1-L2, GLOBAL.

PRD §8 models: IngestionRun, RawItem.

Tenant-agnostic by design. Nothing in this app may import from
clients, scoring, outputs, publication, portal or billing (Arch §4).

Only `IngestionRun` exists so far. RawItem is not modelled; per-item cost is
read from the items a run created rather than stored twice here.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class IngestionRun(models.Model):
    """One collection pass over one source."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    source = models.ForeignKey("sources.Source", on_delete=models.PROTECT, related_name="runs")
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    #: A failed run that holds the pipeline up, as opposed to one the next
    #: scheduled run will simply retry. Only failed AND blocking runs count as
    #: blocking failures on the Triage home.
    blocking = models.BooleanField(default=False)

    #: What the poll returned, what was new, and what the idempotency key had
    #: already seen. "Collected 12, 0 new" and "collected nothing" are different
    #: facts about a source, and only the counts tell them apart.
    items_collected = models.PositiveIntegerField(default=0)
    items_created = models.PositiveIntegerField(default=0)
    items_seen_before = models.PositiveIntegerField(default=0)
    #: Why a failed run failed, in the vendor's or the config's own words.
    #: A failure the screen cannot explain sends the operator to the logs.
    error = models.TextField(blank=True)

    class Meta:
        ordering = ("-started_at",)
        indexes = [
            models.Index(fields=["status", "blocking"], name="ingestionrun_status_blocking"),
        ]

    def __str__(self) -> str:
        return f"{self.source} @ {self.started_at:%Y-%m-%d %H:%M} ({self.status})"

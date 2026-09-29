"""intelligence — L4, GLOBAL.

PRD §8 models: Signal, SignalEvidence.

Tenant-agnostic by design. Nothing in this app may import from
clients, scoring, outputs, publication, portal or billing (Arch §4).

`Digest` is not in PRD §8. It is here because the Triage home counts "new
candidates since the last digest", and a digest summarises signals, which are
global — so it cannot live at L5 or above without pulling tenant context into
a global count.

Only the fields the Triage home summary needs exist so far. SignalEvidence,
score components and suggested actions are not modelled yet.
"""
from __future__ import annotations

from django.core.validators import MaxValueValidator
from django.db import models
from django.utils import timezone

#: Every score in the system is 0–100 (PRD §6.3).
SCORE_VALIDATORS = [MaxValueValidator(100)]


class Signal(models.Model):
    """A detected movement in the category, shared by every tenant.

    The per-client view of the same signal is `scoring.ClientSignalScore`. The
    ranking that combines the two lives in `apps.scoring.ranking`, because this
    layer may not import tenant-scoped code.
    """

    class State(models.TextChoices):
        # Values match the strings the ops frontend already renders
        # (`StateChip`), as `OutputState` does.
        CANDIDATE = "candidate", "Candidate"
        IN_REVIEW = "in review", "In review"
        WATCHING = "watching", "Watching"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    #: The operator-facing identifier, e.g. "SIG-2041". The primary key stays
    #: an integer, like every other model here.
    code = models.CharField(max_length=16, unique=True)
    title = models.CharField(max_length=300)
    state = models.CharField(max_length=20, choices=State.choices, default=State.CANDIDATE)
    first_seen_at = models.DateTimeField(default=timezone.now)

    #: Routed to the research track (Research inbox) — health or scientific
    #: claims that need expert handling before anything is said about them.
    needs_scientific_routing = models.BooleanField(default=False)
    #: Scoring is done and an operator decision is the next step.
    awaiting_operator_judgment = models.BooleanField(default=False)

    #: "Is this moving in the category?" — computed once per domain pack and
    #: shared by all tenants (Arch §8.1).
    domain_score = models.PositiveSmallIntegerField(validators=SCORE_VALIDATORS)

    class Meta:
        ordering = ("-domain_score", "code")
        indexes = [
            # new_candidates: state = candidate AND first_seen_at > <digest>.
            models.Index(fields=["state", "first_seen_at"], name="signal_state_first_seen"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(domain_score__lte=100), name="signal_domain_score_lte_100"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.title}"


class Digest(models.Model):
    """One collection window's summary, produced when the window closes.

    "New candidates" are signals first seen after the PREVIOUS digest was
    created, so the history of these rows is what defines "new".
    """

    window_start = models.DateField()
    window_end = models.DateField()
    #: Settable rather than `auto_now_add`, so a seed or a backfill can record
    #: when a past digest actually ran.
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        get_latest_by = "created_at"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(window_start__lte=models.F("window_end")),
                name="digest_window_start_lte_end",
            ),
        ]

    def __str__(self) -> str:
        return f"Digest {self.window_start} – {self.window_end}"

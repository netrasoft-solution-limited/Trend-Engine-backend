"""intelligence — L4, GLOBAL.

PRD §8 models: Signal, SignalEvidence.

Tenant-agnostic by design. Nothing in this app may import from
clients, scoring, outputs, publication, portal or billing (Arch §4).

`Digest` is not in PRD §8. It is here because the Triage home counts "new
candidates since the last digest", and a digest summarises signals, which are
global — so it cannot live at L5 or above without pulling tenant context into
a global count.

A signal is DETECTED, not entered: `detection.py` groups claims from different
sources into a development, and `components.py` scores it. Every number on a
signal is stored with the inputs it came from (Arch §8.2), because a score that
cannot be explained at the moment someone asks about it cannot be explained
later either. Suggested actions are per client and live in `apps.scoring`.
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

    class Type(models.TextChoices):
        """PRD §6.3's signal types."""

        RISING_TOPIC = "rising_topic", "Rising, new or resurfacing topic"
        CONVERGENCE = "convergence", "Cross-source convergence"
        COMPETITOR_MOVE = "competitor_move", "Competitor move"
        CONSUMER_QUESTION = "consumer_question", "Recurring consumer question or unmet need"
        ASSET_OPPORTUNITY = "asset_opportunity", "Product or asset opportunity"
        CONTRADICTION = "contradiction", "Contradiction, safety issue or correction"
        RESEARCH = "research", "New research or trial update"

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

    # ── What it is ──────────────────────────────────────────────────────────
    signal_type = models.CharField(max_length=32, choices=Type.choices, blank=True)
    #: What changed, in one or two plain sentences. Written at detection from
    #: the supporting claims and nothing else.
    statement = models.TextField(blank=True)

    # ── Why it scored what it did (Arch §8.2) ───────────────────────────────
    #: `{component: {"weight", "value" | None, "detail"}}`. `value` is None when
    #: the input does not exist yet — no baseline, no engagement metrics — and
    #: the domain score is then the weighted mean of what IS available, which
    #: `components_available` makes visible rather than leaving as a quiet gap.
    components = models.JSONField(default=dict, blank=True)
    #: Weekly item counts behind momentum and acceleration, and how long the
    #: corpus has been watched — the measurement the score was taken against.
    baseline = models.JSONField(default=dict, blank=True)
    class Maturity(models.TextChoices):
        """What the development can honestly be called (Mark, 19 Oct): only a
        measured, rising momentum makes a trend; until coverage supports that
        measurement it is a recurring discussion or a watch item."""

        MEASURED_TREND = "measured_trend", "Measured rising trend"
        RECURRING = "recurring", "Recurring discussion"
        WATCH = "watch", "Watch item"

    maturity = models.CharField(
        max_length=20, choices=Maturity.choices, default=Maturity.WATCH, blank=True
    )

    #: PRD §6.3: confidence is tracked separately from importance.
    confidence = models.PositiveSmallIntegerField(default=0, validators=SCORE_VALIDATORS)
    confidence_reasons = models.JSONField(default=list, blank=True)
    source_count = models.PositiveSmallIntegerField(default=0)
    creator_count = models.PositiveSmallIntegerField(default=0)
    item_count = models.PositiveSmallIntegerField(default=0)

    # ── Provenance ──────────────────────────────────────────────────────────
    detected_by = models.ForeignKey(
        "enrichment.ModelRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="signals",
    )
    prompt_version = models.CharField(max_length=64, blank=True)
    domain_pack_version = models.CharField(max_length=64, blank=True)
    scored_at = models.DateTimeField(null=True, blank=True)
    #: Seeded or demonstration signals. They carry the label everywhere they are
    #: shown, so a hand-written number is never mistaken for a detected one.
    is_illustrative = models.BooleanField(default=False)

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

    @property
    def components_available(self) -> tuple[int, int]:
        """(available, total) — how much of the score rests on measured inputs."""
        values = list((self.components or {}).values())
        return sum(1 for c in values if c.get("value") is not None), len(values)


class SignalEvidence(models.Model):
    """One claim behind one signal, and which way it points.

    Contradicting claims are kept, not discarded — PRD §6.3: "retain
    contradictions rather than discarding them". A signal whose evidence all
    agrees and one with three dissenting experts are different findings.
    """

    class Role(models.TextChoices):
        SUPPORTS = "supports", "Supports"
        CONTRADICTS = "contradicts", "Contradicts"

    signal = models.ForeignKey(Signal, on_delete=models.CASCADE, related_name="evidence")
    claim = models.ForeignKey(
        "enrichment.Claim", on_delete=models.CASCADE, related_name="signal_evidence"
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.SUPPORTS)
    added_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("signal", "role", "added_at")
        constraints = [
            models.UniqueConstraint(fields=["signal", "claim"], name="uniq_signal_claim"),
        ]

    def __str__(self) -> str:
        return f"{self.signal.code} {self.role} claim {self.claim_id}"


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


class SignalObservation(models.Model):
    """A hand-entered observation attached to a signal by an operator.

    The second kind of evidence beside `SignalEvidence` claims: a Reddit
    question, a TikTok Shop listing, a search estimate. Attached deliberately,
    never by a model, because these are what the operator found and chose to
    rely on — the attachment records who decided that.
    """

    signal = models.ForeignKey(Signal, on_delete=models.CASCADE, related_name="observations")
    observation = models.ForeignKey(
        "evidence.Observation", on_delete=models.CASCADE, related_name="signal_links"
    )
    role = models.CharField(
        max_length=16, choices=SignalEvidence.Role.choices, default=SignalEvidence.Role.SUPPORTS
    )
    added_by = models.CharField(max_length=200)
    added_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("signal", "role", "added_at")
        constraints = [
            models.UniqueConstraint(
                fields=["signal", "observation"], name="uniq_signal_observation"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.signal.code} {self.role} observation {self.observation_id}"

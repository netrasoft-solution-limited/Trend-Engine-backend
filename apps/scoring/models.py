"""scoring — L5, TENANT-SCOPED.

PRD §8 models: ClientSignalScore, Recommendation, Feedback.

Every model here carries `organization` and uses TenantScopedManager
as its default manager. That is not optional: an unscoped default
manager on a tenant model is the leak Arch §5.2 exists to prevent.

Only `ClientSignalScore` exists so far, with the fields candidate ranking
needs. Score components, Recommendation and Feedback are not modelled yet.
"""
from __future__ import annotations

from django.db import models

from apps.intelligence.models import SCORE_VALIDATORS
from apps.tenancy.managers import TenantScopedModel


class ClientSignalScore(TenantScopedModel):
    """How much one global signal matters to one client.

    The client is `organization`: Organization maps 1:1 to Client (see
    README.md), and there is no separate Client model to point at.
    """

    signal = models.ForeignKey(
        "intelligence.Signal", on_delete=models.CASCADE, related_name="client_scores"
    )
    #: "Does this matter to this client?" — recomputed per client profile
    #: version (Arch §8.1).
    fit_score = models.PositiveSmallIntegerField(validators=SCORE_VALIDATORS)
    #: "How much should we trust this?" — tracked separately from importance,
    #: on purpose (PRD §6.3).
    confidence = models.PositiveSmallIntegerField(validators=SCORE_VALIDATORS)

    class Meta:
        ordering = ("organization", "-fit_score")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "signal"], name="uniq_client_score_per_signal"
            ),
            models.CheckConstraint(
                condition=models.Q(fit_score__lte=100, confidence__lte=100),
                name="client_score_scores_lte_100",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.signal_id}@{self.organization_id}: "
            f"fit {self.fit_score}, conf {self.confidence}"
        )


class ClaimRelevance(TenantScopedModel):
    """How much one claim matters to one client, and why.

    This is the row that makes two clients receive different briefs from one
    shared corpus — the thing PRD §14 tests as "one public signal receives
    different scores for Jarrow and the second-client fixture". The evidence
    layer is shared by design (PRD §6.7); this is where it stops being shared.

    COMPONENTS, NOT A SCALAR. Arch §8.2: "Scores are not stored as scalars…
    The operator UI renders this directly… if components aren't persisted at
    write time, no amount of frontend work can reconstruct them later." Each
    component is kept, along with `matches` — the actual asset or term that
    matched, and the word it matched on. An operator who disagrees with a score
    can see the reason and fix it by editing the profile, which is the only
    kind of score anyone will trust.

    WHAT THIS DELIBERATELY DOES NOT INCLUDE. PRD §6.3's client rank score is
    60% domain signal + 20% asset relevance + 10% audience fit + 10% strategic
    priority. The 60% is not built: it needs momentum and baselines over a
    28-day window the corpus does not yet have. It is also IDENTICAL for every
    client by construction, so the entire client-to-client difference lives in
    the 40% computed here — which is why this is useful before the rest exists.

    `fit_score` is therefore normalised across the components that do exist,
    and `weights_used` records which those were. When the domain component
    lands the weights change, and an old row still says what it was computed
    from rather than being silently re-interpreted.
    """

    #: The weights actually applied today. PRD §6.3's names and relative
    #: proportions, with the unbuilt domain component left out rather than
    #: faked at zero — a zero would read as "we measured it and it was nothing".
    WEIGHTS = {"asset": 20, "audience": 10, "priority": 10}

    claim = models.ForeignKey(
        "enrichment.Claim", on_delete=models.CASCADE, related_name="client_relevance"
    )
    profile = models.ForeignKey(
        "clients.ClientProfileVersion", on_delete=models.CASCADE, related_name="claim_scores"
    )

    asset_component = models.PositiveSmallIntegerField(validators=SCORE_VALIDATORS, default=0)
    audience_component = models.PositiveSmallIntegerField(validators=SCORE_VALIDATORS, default=0)
    priority_component = models.PositiveSmallIntegerField(validators=SCORE_VALIDATORS, default=0)

    #: 0–100, normalised over `weights_used`. Zero means nothing in this
    #: client's profile matched, which is a real answer: the claim is not
    #: theirs, and it is kept out of their brief.
    fit_score = models.PositiveSmallIntegerField(validators=SCORE_VALIDATORS, default=0)

    weights_used = models.JSONField(default=dict)
    #: [{"facet": "asset", "name": "Magnesium Glycinate", "term": "magnesium",
    #:   "weight": 80}, …] — the audit trail for the number above.
    matches = models.JSONField(default=list)

    scored_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("organization", "-fit_score")
        constraints = [
            # One score per claim per profile VERSION — not per claim per org.
            # Re-profiling a client must not overwrite the scores an already
            # published brief was built from.
            models.UniqueConstraint(
                fields=["claim", "profile"], name="uniq_claim_relevance_per_profile"
            ),
        ]
        indexes = [models.Index(fields=["organization", "profile", "-fit_score"])]

    def __str__(self) -> str:
        return f"claim {self.claim_id} @ {self.organization_id}: fit {self.fit_score}"

    @property
    def matched(self) -> bool:
        return self.fit_score > 0

    @property
    def why(self) -> str:
        """One line an operator can read without opening the JSON."""
        if not self.matches:
            return "Nothing in this client's profile matched."
        seen: list[str] = []
        for match in self.matches:
            label = f"{match['name']} ({match['term']})"
            if label not in seen:
                seen.append(label)
        return "Matched " + ", ".join(seen[:4]) + ("…" if len(seen) > 4 else "")

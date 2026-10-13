"""scoring — L5, TENANT-SCOPED.

PRD §8 models: ClientSignalScore, Recommendation, Feedback.

Every model here carries `organization` and uses TenantScopedManager
as its default manager. That is not optional: an unscoped default
manager on a tenant model is the leak Arch §5.2 exists to prevent.

`ClientSignalScore` carries PRD §6.3's client rank with its components;
`Recommendation` is the client-specific "why it matters, what to do" an operator
reviews. Feedback is not modelled yet.
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

    #: PRD §6.3: 60% domain signal + 20% asset relevance + 10% audience fit +
    #: 10% strategic priority. Null on rows written before it was computed
    #: (and on seeded rows), which rank after every computed one.
    rank_score = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=SCORE_VALIDATORS
    )
    #: `{"domain"|"asset"|"audience"|"priority": {"value", "weight", "detail"}}`
    #: — the four parts of `rank_score`, as computed (Arch §8.2).
    components = models.JSONField(default=dict, blank=True)
    #: Which of this client's products, audiences and priorities matched, and
    #: on which word — the answer to "why is this Jarrow's business?".
    matches = models.JSONField(default=list, blank=True)
    profile = models.ForeignKey(
        "clients.ClientProfileVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="signal_scores",
    )
    scored_at = models.DateTimeField(null=True, blank=True)

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


class Recommendation(TenantScopedModel):
    """Why one signal matters to one client, and what they should do about it.

    The analytical step Mark's 8 October review found missing: connecting a
    development to a client's portfolio and a concrete response. Drafted by a
    model from the signal's own evidence and the client's own profile — never
    another client's (PRD §6.4) — and then reviewed by an operator. Nothing
    here reaches a client until a brief built from APPROVED recommendations
    goes through the publication gate.

    GROUNDED OR NOT STORED. Every action cites at least one of the signal's
    claims; a product is named only if it is in the client's profile. What the
    validator dropped is kept in `validation`, so a model that keeps reaching
    past its evidence is visible rather than quietly corrected.
    """

    class State(models.TextChoices):
        # PRD §6.5's recommendation states.
        PROPOSED = "proposed", "Proposed"
        EDITED = "operator edited", "Operator edited"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    #: The states in which a recommendation is the current one for its signal.
    OPEN_STATES = (State.PROPOSED, State.EDITED, State.APPROVED)

    signal = models.ForeignKey(
        "intelligence.Signal", on_delete=models.CASCADE, related_name="recommendations"
    )
    profile = models.ForeignKey(
        "clients.ClientProfileVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendations",
    )
    state = models.CharField(max_length=20, choices=State.choices, default=State.PROPOSED)

    why_it_matters = models.TextField()
    #: Example Outputs B2's "what it does not prove" — the limits of the
    #: evidence, carried into the brief so a client never reads more into a
    #: development than the evidence supports.
    limits = models.TextField(blank=True)
    #: [{"owner", "action", "success_measure", "claim_ids": [..]}, ...]
    actions = models.JSONField(default=list)
    #: Product names, each one present in `profile`.
    products = models.JSONField(default=list, blank=True)
    cited_claim_ids = models.JSONField(default=list, blank=True)
    #: What the validator removed: invented claim IDs, uncited actions, products
    #: not in the profile.
    validation = models.JSONField(default=dict, blank=True)

    model_run = models.ForeignKey(
        "enrichment.ModelRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendations",
    )
    prompt_version = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    edited_by_label = models.CharField(max_length=200, blank=True)
    decided_by_label = models.CharField(max_length=200, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ("organization", "-created_at")
        constraints = [
            # History is kept — a rejected recommendation stays on record — but
            # only one is current per signal per client, so "what do we
            # recommend?" has one answer.
            models.UniqueConstraint(
                fields=["organization", "signal"],
                condition=models.Q(state__in=["proposed", "operator edited", "approved"]),
                name="one_open_recommendation_per_signal",
            ),
        ]

    def __str__(self) -> str:
        return f"Recommendation on {self.signal_id} @ {self.organization_id} ({self.state})"

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

"""Candidate ranking — the one ordering the candidate list and the Triage
summary's `top_candidate_id` must share.

Domain score first (global), then this client's fit, then this client's
confidence, all descending; `code` last so ties are deterministic. A candidate
with no score for the client ranks after every scored candidate on the same
domain score, rather than being dropped — it is still a candidate.

With no client (`organization=None` — an operator looking across every
tenant), fit and confidence have no single value, so the order is domain score
then `code`.

Lives in `scoring`, not `intelligence`: the ordering needs the tenant-scoped
`ClientSignalScore`, and L4 may not import it.
"""
from __future__ import annotations

from django.db.models import F, OuterRef, QuerySet, Subquery

from apps.intelligence.models import Signal

from .models import ClientSignalScore


def ranked_candidates(organization=None) -> QuerySet[Signal]:
    """Candidate signals, best first.

    With an organisation, annotated with that client's `fit_score` and
    `confidence`. Reads scores through the default-deny manager, so a tenant
    (or operator scope) must be bound — same as any other tenant-scoped read.
    """
    candidates = Signal.objects.filter(state=Signal.State.CANDIDATE)
    if organization is None:
        return candidates.order_by(F("domain_score").desc(), "code")

    scores = ClientSignalScore.objects.for_organization(organization).filter(signal=OuterRef("pk"))
    return candidates.annotate(
        fit_score=Subquery(scores.values("fit_score")[:1]),
        confidence=Subquery(scores.values("confidence")[:1]),
    ).order_by(
        F("domain_score").desc(),
        F("fit_score").desc(nulls_last=True),
        F("confidence").desc(nulls_last=True),
        "code",
    )

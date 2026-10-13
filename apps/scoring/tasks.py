"""Ranking signals per client, and drafting their recommendations.

Dispatched by name at the end of signal detection (`apps.intelligence` sits
below this app and may not import it), and runnable by hand through
`manage.py rank_signals`.

Drafting is bounded: only the client's top-ranked signals that have no current
recommendation, at most `RECOMMEND_TOP` a run. One model call each, so a run's
cost is capped by count, not only by the per-call ceiling.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.db.models import F, OuterRef, Subquery

from apps.intelligence.models import Signal
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

from . import recommendations, signal_rank
from .models import ClientSignalScore, Recommendation

logger = logging.getLogger(__name__)

#: How many new recommendations one run may draft per client.
RECOMMEND_TOP = 3


def recommend_top(organization, *, limit: int = RECOMMEND_TOP, client=None) -> dict:
    """Draft recommendations for the best-ranked signals that have never had one.

    "Never", not "no open one": a signal whose recommendation an operator
    rejected is not redrafted automatically — that would put the rejected
    advice back in the queue every morning. Scoped by the caller.
    """
    have = set(
        Recommendation.objects.filter(organization=organization).values_list("signal_id", flat=True)
    )
    drafted, declined, failed = [], [], []
    for signal in ranked_open(organization):
        if len(drafted) + len(declined) >= limit:
            break
        if signal.pk in have:
            continue
        try:
            result = recommendations.draft(organization, signal, client=client)
        except recommendations.RecommendationError as exc:
            logger.warning("No recommendation for %s: %s", signal.code, exc)
            failed.append(signal.code)
            continue
        (drafted if result.recommendation else declined).append(signal.code)
    return {"drafted": drafted, "declined": declined, "failed": failed}


def ranked_open(organization):
    """Every detected, open signal this client has a rank for, best first."""
    scores = ClientSignalScore.objects.filter(organization=organization, signal=OuterRef("pk"))
    return (
        Signal.objects.filter(state__in=signal_rank.RANKABLE_STATES, is_illustrative=False)
        .annotate(rank_score=Subquery(scores.values("rank_score")[:1]))
        .filter(rank_score__isnull=False)
        .order_by(F("rank_score").desc(), "code")
    )


@shared_task(name="apps.scoring.tasks.rank_all_clients")
def rank_all_clients() -> dict:
    """Rank for every client with a live profile, then draft their top recommendations."""
    from apps.clients import services as client_services

    with operator_scope():
        organizations = list(Organization.objects.all())

    summary = {}
    for organization in organizations:
        with scoped(organization):
            if client_services.current_for(organization) is None:
                continue
            ranked = signal_rank.rank_for(organization)
            summary[organization.slug] = {**ranked, **recommend_top(organization)}
    return summary

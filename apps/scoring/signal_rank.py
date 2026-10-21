"""Ranking a global signal for one client — PRD §6.3's client rank score.

    client rank = 60% domain signal + 20% asset relevance
                + 10% audience fit  + 10% strategic priority

The domain signal is the same for every client (`intelligence.components`).
The other forty points are this client's alone, and they are read from the
claim-level relevance rows that already explain themselves: for each facet, the
strongest match among the signal's SUPPORTING claims. A signal is Jarrow's
business when the evidence behind it touches Jarrow's products, audiences or
priorities — not when its headline happens to share a word.

This is where PRD §14's tenancy criterion becomes literal: "one public signal
receives different scores for Jarrow and the second-client fixture". The signal
row is shared; this row is not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.intelligence.models import Signal, SignalEvidence

from . import relevance
from .models import ClaimRelevance, ClientSignalScore

logger = logging.getLogger(__name__)

#: PRD §6.3.
RANK_WEIGHTS = {"domain": 60, "asset": 20, "audience": 10, "priority": 10}
#: The client-specific share of the rank, normalised to 0–100 — what the
#: operator screens call "fit" (Example Outputs A1's "Jarrow fit").
FIT_WEIGHTS = {"asset": 20, "audience": 10, "priority": 10}

#: Signals a client can still act on.
RANKABLE_STATES = (
    Signal.State.CANDIDATE,
    Signal.State.IN_REVIEW,
    Signal.State.WATCHING,
    Signal.State.APPROVED,
)


def components_for(signal: Signal, rows: list[ClaimRelevance]) -> tuple[dict, list[dict]]:
    """The four parts of the rank, and the matches behind them. Pure."""
    best = {"asset": 0, "audience": 0, "priority": 0}
    matches: dict[tuple, dict] = {}
    for row in rows:
        best["asset"] = max(best["asset"], row.asset_component)
        best["audience"] = max(best["audience"], row.audience_component)
        best["priority"] = max(best["priority"], row.priority_component)
        for match in row.matches or []:
            key = (match["facet"], match["name"])
            entry = matches.setdefault(key, {**match, "claim_ids": []})
            if getattr(row, "observation_id", None):
                entry.setdefault("observation_ids", []).append(row.observation_id)
            else:
                entry["claim_ids"].append(row.claim_id)

    def named(facet: str) -> str:
        names = sorted({m["name"] for m in matches.values() if _facet_group(m["facet"]) == facet})
        return ", ".join(names) if names else "nothing in the profile matched"

    components = {
        "domain": {
            "value": signal.domain_score,
            "weight": RANK_WEIGHTS["domain"],
            "detail": (
                "Started by an analyst from observations: no movement is measured, so this "
                "part is zero." if signal.origin == Signal.Origin.ANALYST
                else "The signal's own score — the same for every client."
            ),
        },
        "asset": {
            "value": best["asset"],
            "weight": RANK_WEIGHTS["asset"],
            "detail": f"Products and ingredients: {named('asset')}.",
        },
        "audience": {
            "value": best["audience"],
            "weight": RANK_WEIGHTS["audience"],
            "detail": f"Audiences: {named('audience')}.",
        },
        "priority": {
            "value": best["priority"],
            "weight": RANK_WEIGHTS["priority"],
            "detail": f"Priorities and competitors: {named('priority')}.",
        },
    }
    return components, sorted(matches.values(), key=lambda m: (-m["weight"], m["name"]))


@dataclass
class ObservationRelevance:
    """An attached observation scored like a claim, against the same profile."""

    observation_id: int
    asset_component: int
    audience_component: int
    priority_component: int
    matches: list[dict]
    claim_id: None = None


def observation_rows(signal: Signal, profile) -> list[ObservationRelevance]:
    rows = []
    for link in signal.observations.filter(role=SignalEvidence.Role.SUPPORTS).select_related(
        "observation"
    ):
        o = link.observation
        text = " \n".join(part for part in (o.title, o.excerpt, o.topic) if part)
        asset, asset_hits = relevance._component(profile.assets, text)
        audience, audience_hits = relevance._component(profile.audiences, text)
        priority, priority_hits = relevance._component(profile.priorities, text)
        rows.append(ObservationRelevance(
            o.pk, asset, audience, priority, asset_hits + audience_hits + priority_hits
        ))
    return rows


def _facet_group(facet: str) -> str:
    return "priority" if facet in ("priority", "competitor") else facet


def rank_score(components: dict) -> int:
    return round(sum(c["value"] * c["weight"] for c in components.values()) / 100)


def fit_score(components: dict) -> int:
    total = sum(FIT_WEIGHTS.values())
    return round(sum(components[k]["value"] * w for k, w in FIT_WEIGHTS.items()) / total)


@transaction.atomic
def rank_for(organization) -> dict:
    """Score every open, detected signal for one client. Idempotent.

    Re-scores the claims against the live profile first — free, and the only
    way a profile edit reaches the rank. Seeded signals are left alone: their
    numbers were written by hand and must not be mixed with computed ones.
    """
    from apps.clients import services as client_services

    profile = client_services.current_for(organization)
    if profile is None:
        raise ValueError(f"{organization} has no live client profile, so nothing can be ranked.")

    relevance.score_for(organization, profile_version=profile)
    by_claim = {
        row.claim_id: row
        for row in ClaimRelevance.objects.filter(organization=organization, profile=profile)
    }

    compiled = relevance.compile_profile(profile)
    now = timezone.now()
    ranked = 0
    signals = Signal.objects.filter(state__in=RANKABLE_STATES, is_illustrative=False)
    for signal in signals:
        claim_ids = SignalEvidence.objects.filter(
            signal=signal, role=SignalEvidence.Role.SUPPORTS
        ).values_list("claim_id", flat=True)
        rows = [by_claim[c] for c in claim_ids if c in by_claim]
        rows += observation_rows(signal, compiled)
        components, matches = components_for(signal, rows)
        ClientSignalScore.objects.update_or_create(
            organization=organization,
            signal=signal,
            defaults={
                "rank_score": rank_score(components),
                "fit_score": fit_score(components),
                # Confidence is in the evidence, not in the client: the same
                # number for everyone, copied so the ranking can read it here.
                "confidence": signal.confidence,
                "components": components,
                "matches": matches,
                "profile": profile,
                "scored_at": now,
            },
        )
        ranked += 1

    logger.info(
        "Ranked %d signal(s) for %s against profile v%s", ranked, organization, profile.number
    )
    return {"organization": organization.name, "profile": profile.citation, "ranked": ranked}

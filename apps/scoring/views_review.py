"""Triage and Signal Review — PRD §6.5, server-rendered.

Mark's 8 October review: "We could not find a ranked signal queue or a view
showing cross-source connections, change over time, baselines, confidence,
contradictions, or why one development ranks above another." These two pages
are that view, and every number on them is read from what was stored when it
was computed (Arch §8.2) — nothing here recalculates a score for display.

THE CLIENT IS IN THE URL, as on the client screens: a bookmarked review page
always means the client it names, and no session state can show one client's
rank beside another client's recommendation. Signals and evidence are global;
scores and recommendations are read inside `scoped(organization)`.

Open to any signed-in operator. Reviewing signals is the Operator role's work
(PRD §3.2); the Platform Admin gate on client setup does not apply here.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings
from django.contrib import messages
from django.db.models import F, OuterRef, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.clients import services as client_services
from apps.clients.models import ClientAsset
from apps.intelligence import detection, review
from apps.intelligence.components import WEIGHTS as DOMAIN_WEIGHTS
from apps.intelligence.models import Signal, SignalEvidence
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

from . import recommendations, signal_rank
from .models import ClientSignalScore, Recommendation

logger = logging.getLogger(__name__)

COMPONENT_LABELS = {
    "momentum": "Momentum",
    "acceleration": "Acceleration",
    "source_diversity": "Source diversity",
    "evidence_quality": "Evidence quality",
    "engagement": "Engagement",
    "novelty": "Novelty",
    "recency": "Recency",
    "domain": "Signal score",
    "asset": "Products",
    "audience": "Audiences",
    "priority": "Priorities",
}

OPEN = (Signal.State.CANDIDATE, Signal.State.IN_REVIEW, Signal.State.WATCHING,
        Signal.State.APPROVED)

#: Blank action rows offered under a recommendation's existing ones.
SPARE_ACTION_ROWS = 1


def _actor(request) -> str:
    return getattr(request.user, "email", str(request.user))


def _clients() -> list[Organization]:
    with operator_scope():
        return list(Organization.objects.order_by("is_fixture", "name"))


def _organization(slug: str) -> Organization:
    with operator_scope():
        return get_object_or_404(Organization, slug=slug)


# ── Triage ──────────────────────────────────────────────────────────────────


class TriageIndexView(View):
    """No client chosen: send the operator to the first real one."""

    def get(self, request):
        clients = _clients()
        real = [c for c in clients if not c.is_fixture] or clients
        if not real:
            messages.info(request, "Add a client before triaging signals for one.")
            return redirect(reverse("ops-clients"))
        return redirect(reverse("ops-triage", args=[real[0].slug]))


class TriageView(View):
    """One client's ranked queue."""

    def get(self, request, slug):
        organization = _organization(slug)
        show_rejected = request.GET.get("state") == "rejected"
        states = (Signal.State.REJECTED,) if show_rejected else OPEN

        with scoped(organization):
            profile = client_services.current_for(organization)
            scores = ClientSignalScore.objects.filter(
                organization=organization, signal=OuterRef("pk")
            )
            recs = Recommendation.objects.filter(
                organization=organization, signal=OuterRef("pk")
            ).order_by("-created_at")
            signals = list(
                Signal.objects.filter(state__in=states)
                .annotate(
                    rank_score=Subquery(scores.values("rank_score")[:1]),
                    fit_score=Subquery(scores.values("fit_score")[:1]),
                    recommendation_state=Subquery(recs.values("state")[:1]),
                )
                .order_by(
                    F("rank_score").desc(nulls_last=True), F("domain_score").desc(), "code"
                )
            )

        for signal in signals:
            signal.available, signal.component_total = signal.components_available

        with scoped(organization):
            # What a brief would contain right now: both approvals in place.
            # Counted here rather than asked of `apps.outputs`, which sits above
            # this layer and may not be imported from it.
            approved = Recommendation.objects.filter(
                organization=organization,
                state=Recommendation.State.APPROVED,
                signal__state=Signal.State.APPROVED,
                signal__is_illustrative=False,
            ).count()

        return render(
            request,
            "ops/scoring/triage.html",
            {
                "organization": organization,
                "clients": _clients(),
                "profile": profile,
                "signals": signals,
                "show_rejected": show_rejected,
                "approved_developments": approved,
                "unranked": sum(
                    1 for s in signals if s.rank_score is None and not s.is_illustrative
                ),
            },
        )

    def post(self, request, slug):
        """Two buttons: re-rank (free) and detect now (one model call)."""
        organization = _organization(slug)
        back = redirect(reverse("ops-triage", args=[slug]))

        if request.POST.get("action") == "detect":
            result = detection.detect()
            if result.skipped:
                messages.warning(request, f"Detection skipped: {result.skipped}")
            else:
                messages.success(
                    request,
                    f"Detection read {result.claims_considered} claim(s): "
                    f"{len(result.created)} new signal(s), {len(result.extended)} extended.",
                )

        with scoped(organization):
            try:
                summary = signal_rank.rank_for(organization)
            except ValueError as exc:
                messages.error(request, str(exc))
                return back
        messages.info(
            request, f"Ranked {summary['ranked']} signal(s) against {summary['profile']}."
        )
        return back


# ── Signal review ───────────────────────────────────────────────────────────


class SignalReviewView(View):
    """Everything about one signal, for one client, and every decision on it."""

    def get(self, request, slug, code):
        organization = _organization(slug)
        signal = get_object_or_404(Signal.objects.select_related("detected_by"), code=code)

        evidence = list(
            SignalEvidence.objects.filter(signal=signal)
            .select_related("claim__segment", "claim__content_item__source")
            .order_by("role", "-claim__content_item__published_at")
        )
        for row in evidence:
            item = row.claim.content_item
            seconds = row.claim.segment.start_seconds if row.claim.segment_id else None
            row.at = _timestamp(seconds)
            row.link = item.link_at(seconds)
            row.said_on = item.published_at or item.discovered_at

        with scoped(organization):
            score = ClientSignalScore.objects.filter(
                organization=organization, signal=signal
            ).first()
            recommendation = (
                Recommendation.objects.filter(
                    organization=organization, signal=signal,
                    state__in=Recommendation.OPEN_STATES,
                ).first()
            )
            history = list(
                Recommendation.objects.filter(
                    organization=organization, signal=signal,
                    state=Recommendation.State.REJECTED,
                ).order_by("-decided_at")[:3]
            )
            profile = client_services.current_for(organization)
            products = (
                list(ClientAsset.objects.filter(profile=profile).values_list("name", flat=True))
                if profile else []
            )

        cited = set(recommendation.cited_claim_ids) if recommendation else set()
        for row in evidence:
            row.is_cited_by_recommendation = row.claim_id in cited

        weekly = list(reversed((signal.baseline or {}).get("weekly_items") or []))
        peak = max(weekly, default=0) or 1
        # Collection behind each week, oldest first like the bars, so the
        # operator can see whether a quiet week was the category or the feed.
        coverage = list(reversed((signal.baseline or {}).get("coverage") or []))
        for n, week in zip(weekly, coverage, strict=False):
            week["supporting"] = n

        return render(
            request,
            "ops/scoring/signal.html",
            {
                "organization": organization,
                "signal": signal,
                "components": _components(signal.components, DOMAIN_WEIGHTS),
                "available": signal.components_available,
                "weekly": [{"n": n, "pct": round(100 * n / peak)} for n in weekly],
                "coverage": coverage,
                "covered_week": {
                    "items": settings.SIGNAL_COVERED_WEEK_MIN_ITEMS,
                    "sources": settings.SIGNAL_COVERED_WEEK_MIN_SOURCES,
                },
                "supporting": [r for r in evidence if r.role == SignalEvidence.Role.SUPPORTS],
                "contradicting": [
                    r for r in evidence if r.role == SignalEvidence.Role.CONTRADICTS
                ],
                "score": score,
                "client_components": _components(score.components, signal_rank.RANK_WEIGHTS)
                if score else [],
                "recommendation": recommendation,
                "action_rows": _action_rows(recommendation),
                "history": history,
                "products": products,
                "owners": recommendations.OWNERS,
                "moves": [(to, Signal.State(to).label) for to in review.allowed(signal)],
                "needs_reason": review.NEEDS_REASON,
                "merge_targets": Signal.objects.filter(state__in=OPEN, is_illustrative=False)
                .exclude(pk=signal.pk)
                .order_by("code"),
            },
        )

    def post(self, request, slug, code):
        organization = _organization(slug)
        signal = get_object_or_404(Signal, code=code)
        action = request.POST.get("action") or ""
        actor = _actor(request)
        back = redirect(reverse("ops-signal", args=[slug, code]))

        try:
            if action == "signal.move":
                review.move(
                    signal, request.POST.get("to") or "", actor_label=actor,
                    reason=request.POST.get("reason") or "",
                )
                messages.success(request, f"{code} is now {signal.get_state_display().lower()}.")
            elif action == "signal.merge":
                into = Signal.objects.filter(code=request.POST.get("into") or "").first()
                if into is None:
                    messages.error(request, "Choose the signal to merge into.")
                    return back
                review.merge(signal, into, actor_label=actor)
                messages.success(request, f"Merged {code} into {into.code}.")
                return redirect(reverse("ops-signal", args=[slug, into.code]))
            else:
                with scoped(organization):
                    self._recommendation(request, organization, signal, action, actor)
        except (review.ReviewError, recommendations.RecommendationError) as exc:
            # Both services write their refusals for a person; shown verbatim.
            messages.error(request, str(exc))
        return back

    def _recommendation(self, request, organization, signal, action, actor) -> None:
        current = Recommendation.objects.filter(
            organization=organization, signal=signal, state__in=Recommendation.OPEN_STATES
        ).first()

        if action == "rec.draft":
            signal_rank.rank_for(organization)
            result = recommendations.draft(organization, signal)
            if result.recommendation is None:
                messages.warning(
                    request, f"No recommendation — the model declined: {result.declined}"
                )
            else:
                dropped = result.recommendation.validation
                messages.success(request, "Drafted. Read it against the evidence before approving.")
                if any(dropped.values()):
                    messages.warning(request, f"The validator removed: {_describe(dropped)}.")
            return

        if current is None:
            raise recommendations.RecommendationError("There is no open recommendation here.")

        if action == "rec.edit":
            edited = recommendations.edit(current, _parse_edit(request.POST), actor_label=actor)
            messages.success(request, "Saved.")
            if any(edited.validation.values()):
                messages.warning(request, f"Removed on save: {_describe(edited.validation)}.")
        elif action == "rec.approve":
            recommendations.decide(current, approve=True, actor_label=actor)
            messages.success(request, "Approved. It can now go into a brief for this client.")
        elif action == "rec.reject":
            recommendations.decide(
                current, approve=False, actor_label=actor, note=request.POST.get("note") or ""
            )
            messages.info(request, "Rejected. It will not be redrafted automatically.")
        else:
            messages.error(request, "Unknown action.")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _components(stored: dict, weights: dict) -> list[dict]:
    rows = []
    for key, weight in weights.items():
        component = (stored or {}).get(key) or {}
        rows.append(
            {
                "key": key,
                "label": COMPONENT_LABELS.get(key, key),
                "weight": component.get("weight", weight),
                "effective_weight": component.get("effective_weight"),
                "value": component.get("value"),
                "detail": component.get("detail", "Not computed."),
            }
        )
    return rows


def _action_rows(recommendation) -> list[dict]:
    rows = [
        {**a, "claims": ", ".join(f"C{i}" for i in a.get("claim_ids", []))}
        for a in (recommendation.actions if recommendation else [])
    ]
    if recommendation and len(rows) < recommendations.MAX_ACTIONS:
        rows += [{"owner": "", "action": "", "success_measure": "", "claims": ""}] * min(
            SPARE_ACTION_ROWS, recommendations.MAX_ACTIONS - len(rows)
        )
    return rows


def _parse_edit(post) -> dict:
    actions = []
    for i in range(recommendations.MAX_ACTIONS):
        text = (post.get(f"action_{i}") or "").strip()
        if not text:
            continue
        actions.append(
            {
                "owner": post.get(f"owner_{i}") or "",
                "action": text,
                "success_measure": post.get(f"measure_{i}") or "",
                "claim_ids": [int(n) for n in re.findall(r"\d+", post.get(f"claims_{i}") or "")],
            }
        )
    return {
        "why_it_matters": post.get("why_it_matters") or "",
        "limits": post.get("limits") or "",
        "products": post.getlist("products"),
        "actions": actions,
    }


def _describe(validation: dict) -> str:
    parts = []
    if validation.get("invented_claim_ids"):
        parts.append(
            "claim number(s) not in this signal's evidence: "
            + ", ".join(f"C{i}" for i in validation["invented_claim_ids"])
        )
    if validation.get("uncited_actions"):
        parts.append(f"{validation['uncited_actions']} action(s) citing no evidence")
    if validation.get("unknown_products"):
        parts.append("product(s) not in the profile: " + ", ".join(validation["unknown_products"]))
    return "; ".join(parts)


def _timestamp(seconds) -> str:
    if seconds is None:
        return ""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

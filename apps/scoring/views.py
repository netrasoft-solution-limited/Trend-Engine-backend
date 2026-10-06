"""Triage · Signal review — server-rendered views.

The relevance screen lives here rather than on the client page because
`apps.scoring` sits ABOVE `apps.clients` in the layer contract, and that
ordering is saying something true: a profile describes a client, while a score
is a judgement about evidence made using that profile. The client screen may not
reach up to read scores, so it links here instead.

This is also the first half of the Signal Review screen PRD §6.5 asks for — the
part that shows WHY a claim scored what it did. Arch §8.2 requires components to
be persisted precisely so this page can render them, and a score an operator
cannot argue with is a score nobody trusts.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.clients import services as client_services
from apps.operations.permissions import require_platform_admin
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

from . import relevance
from .models import ClaimRelevance

logger = logging.getLogger(__name__)

#: Enough to judge whether the profile is working, without paginating.
TOP_N = 25


def _organization(slug: str) -> Organization:
    with operator_scope():
        return get_object_or_404(Organization, slug=slug)


class ClientRelevanceView(View):
    """What this client's share of the corpus is, and why.

    Shows the unmatched claims as well as the matched ones. An obviously
    relevant claim sitting at zero is the single most useful thing on this page:
    it is the prompt to add the term the speaker actually used, and it is how a
    profile gets better by being used.
    """

    def get(self, request, slug):
        require_platform_admin(request, action="Viewing relevance")
        organization = _organization(slug)

        with scoped(organization):
            profile = client_services.current_for(organization)
            rows = ClaimRelevance.objects.filter(profile=profile) if profile else None
            matched = unmatched = total = 0
            top: list = []
            missed: list = []
            if rows is not None:
                total = rows.count()
                matched = rows.filter(fit_score__gt=0).count()
                unmatched = total - matched
                top = list(
                    rows.filter(fit_score__gt=0)
                    .select_related("claim", "claim__content_item")
                    .order_by("-fit_score")[:TOP_N]
                )
                missed = list(
                    rows.filter(fit_score=0)
                    .select_related("claim", "claim__content_item")
                    .order_by("claim__subject")[:TOP_N]
                )

        return render(
            request,
            "ops/scoring/relevance.html",
            {
                "organization": organization,
                "profile": profile,
                "total": total,
                "matched": matched,
                "unmatched": unmatched,
                "top": top,
                "missed": missed,
                "weights": ClaimRelevance.WEIGHTS,
            },
        )


class ClientScoreView(View):
    def post(self, request, slug):
        require_platform_admin(request, action="Scoring")
        organization = _organization(slug)

        with scoped(organization):
            try:
                summary = relevance.score_for(organization)
            except ValueError as exc:
                # The service's refusal names what is missing and how to fix it.
                messages.error(request, str(exc))
                return redirect(reverse("ops-client", args=[slug]))

        messages.success(
            request,
            f"Scored {summary['claims']} claims against profile "
            f"v{summary['profile_version']}: {summary['matched']} are this client's.",
        )
        if summary["matched"] == 0:
            messages.warning(
                request,
                "Nothing matched. The profile's terms are probably narrower than the "
                "words people actually say — look at the unmatched claims below and "
                "add the terms they use.",
            )
        return redirect(reverse("ops-client-relevance", args=[slug]))

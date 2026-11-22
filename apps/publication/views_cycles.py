"""Research cycles — the one page a brand's monthly work runs from.

Start a cycle, watch each stage, see what failed or came back empty and what
it cost, make the review decisions it lists, then draft both briefs. Also where
a brand's sources are linked (Mark, 20 Nov).
"""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.clients.models import BrandSource
from apps.outputs.models import OutputVersion
from apps.scoring.views_review import _actor, _organization
from apps.sources.models import Source
from apps.tenancy.context import scoped

from . import cycle as cycles
from .models import ResearchCycle
from .tasks import advance_cycle, draft_cycle_briefs

STAGE_LABELS = {
    "collect": "Collect fresh research",
    "process": "Check relevance, transcribe, extract claims",
    "detect": "Connect claims into stories and rank them",
    "recommend": "Draft recommendations",
    "discover": "Discover new ingredients; refresh candidate evidence",
    "search": "Refresh search volumes and interest",
    "review": "Review (people): approve stories, recommendations, candidates",
    "briefs": "Draft both briefs",
}


class CycleView(View):
    def get(self, request, slug):
        organization = _organization(slug)
        with scoped(organization):
            history = list(ResearchCycle.objects.filter(organization=organization)[:10])
            current = history[0] if history else None
            linked = set(BrandSource.objects.filter(organization=organization)
                         .values_list("source_id", flat=True))
            versions = (
                list(OutputVersion.objects.filter(pk__in=current.output_version_ids)
                     .select_related("output"))
                if current else []
            )
        sources = list(Source.objects.order_by("route", "name"))
        stages = []
        if current:
            for name in ResearchCycle.STAGES:
                record = current.stages.get(name, {})
                stages.append({"name": name, "label": STAGE_LABELS[name], **record,
                               "current": name == current.stage})
        return render(request, "ops/outputs/cycle.html", {
            "organization": organization,
            "cycle": current,
            "stages": stages,
            "history": history,
            "cost": cycles.cost(current) if current else None,
            "sources": sources,
            "linked": linked,
            "linked_count": len(linked),
            "versions": versions,
        })

    def post(self, request, slug):
        organization = _organization(slug)
        action = request.POST.get("action") or ""
        back = redirect(reverse("ops-cycle", args=[slug]))
        with scoped(organization):
            if action == "start":
                try:
                    started = cycles.start(organization, actor_label=_actor(request))
                except cycles.StageFailed as exc:
                    messages.error(request, str(exc))
                    return back
                advance_cycle.delay(started.pk)
                messages.success(request, f"Research cycle #{started.pk} started. This page "
                                          "shows each stage as it runs.")
            elif action == "briefs":
                current = get_object_or_404(ResearchCycle, organization=organization,
                                            pk=int(request.POST.get("cycle") or 0))
                current.stages.setdefault("briefs", {}).update(
                    state="running", detail="Drafting both briefs, with their written summary.")
                current.save(update_fields=["stages"])
                draft_cycle_briefs.apply_async(args=[current.pk, _actor(request)], queue="enrich")
                messages.info(request, "Drafting both briefs. This takes a minute or two; "
                                       "refresh this page to see them.")
            elif action == "sources":
                chosen = {int(x) for x in request.POST.getlist("source")}
                BrandSource.objects.filter(organization=organization).exclude(
                    source_id__in=chosen).delete()
                have = set(BrandSource.objects.filter(organization=organization)
                           .values_list("source_id", flat=True))
                for source_id in chosen - have:
                    BrandSource.objects.create(organization=organization, source_id=source_id,
                                               added_by_label=_actor(request))
                messages.success(request, f"{len(chosen)} source(s) linked to "
                                          f"{organization.name}.")
        return back


class SetupView(View):
    """The checklist for making a new brand ready for its first cycle."""

    def get(self, request, slug):
        from . import onboarding

        organization = _organization(slug)
        with scoped(organization):
            state = onboarding.status(organization)
        return render(request, "ops/outputs/setup.html", {"organization": organization, **state})

    def post(self, request, slug):
        from apps.enrichment.llm.client import LLMInvalidOutput, LLMUnavailable
        from apps.enrichment.llm.costs import CostCapExceeded

        from . import onboarding

        organization = _organization(slug)
        action = request.POST.get("action") or ""
        with scoped(organization):
            try:
                if action == "families":
                    done = onboarding.assign_families(organization, actor_label=_actor(request))
                    messages.success(
                        request,
                        f"Proposed {done['families']} families; {done['assigned']} listing(s) "
                        f"assigned, {done['unassigned']} left. Review them on the draft profile, "
                        "then make it live.")
                elif action == "sources":
                    done = onboarding.create_sources(organization, actor_label=_actor(request))
                    messages.success(
                        request,
                        f"{done['created']} source(s) created from {done['families']} families; "
                        f"{done['linked']} newly linked, shared podcasts included.")
            except onboarding.OnboardingError as exc:
                messages.error(request, str(exc))
            except (CostCapExceeded, LLMUnavailable, LLMInvalidOutput) as exc:
                messages.error(request, f"Could not propose families: {exc}")
        return redirect(reverse("ops-cycle-setup", args=[slug]))

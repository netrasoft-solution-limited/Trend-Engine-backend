"""New-product candidates and the AI benchmark — operator screens.

The client is in the URL, as on Triage. Candidates and benchmark answers are
tenant data and are read inside `scoped(organization)`; observations are
global evidence and are attached by number.
"""
from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.clients import services as client_services
from apps.intelligence import review
from apps.tenancy.context import scoped

from . import benchmark, opportunities
from .models import BenchmarkAnswer, Candidate
from .views_review import _actor, _organization

MAX_UPLOAD = 2_000_000


class CandidateListView(View):
    def get(self, request, slug):
        organization = _organization(slug)
        with scoped(organization):
            candidates = list(
                Candidate.objects.filter(organization=organization)
                .prefetch_related("observations")
            )
            profile = client_services.current_for(organization)
        return render(request, "ops/scoring/candidates.html", {
            "organization": organization,
            "candidates": candidates,
            "profile": profile,
            "approved": sum(1 for c in candidates if c.is_approved and c.status != "rejected"),
        })

    def post(self, request, slug):
        organization = _organization(slug)
        with scoped(organization):
            try:
                candidate = opportunities.create(
                    organization,
                    name=request.POST.get("name") or "",
                    aliases=request.POST.get("aliases") or "",
                    theme=request.POST.get("theme") or "",
                    actor_label=_actor(request),
                )
            except opportunities.OpportunityError as exc:
                messages.error(request, str(exc))
                return redirect(reverse("ops-candidates", args=[slug]))
        messages.success(
            request, f"Created {candidate.name}. {opportunities.gap_sentence(candidate)}"
        )
        return redirect(reverse("ops-candidate", args=[slug, candidate.pk]))


class CandidateView(View):
    def get(self, request, slug, pk):
        organization = _organization(slug)
        with scoped(organization):
            candidate = get_object_or_404(Candidate, organization=organization, pk=pk)
            observations = list(candidate.observations.order_by("published_at", "pk"))
            profile = client_services.current_for(organization)
            listings = (
                list(profile.assets.order_by("family", "name").values_list("name", flat=True))
                if profile else []
            )
        return render(request, "ops/scoring/candidate.html", {
            "organization": organization,
            "candidate": candidate,
            "observations": observations,
            "gap": opportunities.gap_sentence(candidate),
            "statuses": Candidate.Status.choices,
            "test_types": Candidate.TestType.choices,
            "checks": Candidate.InternalCheck.choices,
            "listings": listings,
        })

    def post(self, request, slug, pk):
        organization = _organization(slug)
        action = request.POST.get("action") or ""
        actor = _actor(request)
        back = redirect(reverse("ops-candidate", args=[slug, pk]))
        with scoped(organization):
            candidate = get_object_or_404(Candidate, organization=organization, pk=pk)
            try:
                if action == "gap":
                    opportunities.check_gap(candidate, actor_label=actor)
                    messages.success(request, opportunities.gap_sentence(candidate))
                elif action == "adjacent.suggest":
                    candidate.adjacent = opportunities.suggest_adjacent(candidate)
                    candidate.save(update_fields=["adjacent", "updated_at"])
                    messages.info(request, "Suggested from the theme. Edit as needed.")
                elif action == "adjacent.set":
                    unknown = opportunities.set_adjacent(
                        candidate, request.POST.get("adjacent") or "", actor_label=actor
                    )
                    if unknown:
                        messages.error(request, "Not listings in the live profile: "
                                       + ", ".join(unknown))
                    else:
                        messages.success(request, "Adjacent listings saved.")
                elif action == "obs.attach":
                    attached, missing = opportunities.attach(
                        candidate, review.parse_observation_ids(request.POST.get("ids") or ""),
                        actor_label=actor,
                    )
                    if attached:
                        messages.success(request, f"Attached {len(attached)} observation(s).")
                    if missing:
                        messages.error(request, "No observation "
                                       + ", ".join(f"O{m}" for m in missing) + ".")
                elif action == "obs.collect":
                    new = opportunities.collect(candidate, actor_label=actor)
                    if new:
                        messages.success(
                            request,
                            f"Attached {len(new)} collected episode(s) that discuss "
                            f"{candidate.name}. Read them, then draft or edit the case "
                            "and approve again.",
                        )
                    else:
                        messages.info(
                            request,
                            f"No analysed podcast or video from the last "
                            f"{opportunities.COLLECT_DAYS} days names {candidate.name} "
                            "that is not already attached.",
                        )
                elif action == "obs.detach":
                    opportunities.detach(
                        candidate, int(request.POST.get("observation") or 0), actor_label=actor
                    )
                    messages.info(request, "Detached.")
                elif action == "edit":
                    was = candidate.is_approved
                    opportunities.edit(candidate, request.POST, actor_label=actor)
                    messages.success(
                        request, "Saved." + (" Approval withdrawn: approve again." if was else "")
                    )
                elif action == "draft":
                    opportunities.draft(candidate, actor_label=actor)
                    messages.success(
                        request, "Drafted from the attached observations. Read and edit it "
                        "before approving."
                    )
                elif action == "approve":
                    opportunities.approve(candidate, actor_label=actor)
                    messages.success(request, f"{candidate.name} approved for the brief.")
            except opportunities.OpportunityError as exc:
                messages.error(request, str(exc))
        return back


class BenchmarkView(View):
    def get(self, request, slug):
        organization = _organization(slug)
        with scoped(organization):
            summary = benchmark.summary(organization)
            total = BenchmarkAnswer.objects.filter(organization=organization).count()
        return render(request, "ops/scoring/benchmark.html", {
            "organization": organization,
            "summary": summary,
            "total": total,
            "named": sum(s.named for s in summary),
            "columns": benchmark.COLUMNS,
        })

    def post(self, request, slug):
        organization = _organization(slug)
        back = redirect(reverse("ops-benchmark", args=[slug]))
        upload = request.FILES.get("file")
        if upload is None or upload.size > MAX_UPLOAD:
            messages.error(request, "Choose a CSV file under 2 MB.")
            return back
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            messages.error(request, "Save the sheet as CSV (UTF-8) and try again.")
            return back
        rows, errors = benchmark.parse_csv(text, client_names=[organization.name])
        if errors:
            messages.error(request, "Nothing imported. Fix these rows and import again:")
            for error in errors[:20]:
                messages.error(request, error)
            return back
        with scoped(organization):
            saved = benchmark.save_all(
                organization, rows, entered_by=_actor(request),
                batch=f"{upload.name} · {timezone.now():%Y-%m-%d %H:%M}",
            )
        messages.success(request, f"Imported {saved} answer(s) from {upload.name}.")
        return back


class BenchmarkTemplateView(View):
    def get(self, request, slug):
        response = HttpResponse(benchmark.template_csv(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="ai-benchmark-template.csv"'
        return response

"""Observations — entering and finding hand-collected evidence.

Open to any signed-in operator: entering what was found is the operator's job.
Nothing here is client-facing; observations reach a client only through a
brief built from what an operator attached and approved.
"""
from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from .models import Observation
from .services import observations as service

MAX_UPLOAD = 2_000_000


def _actor(request) -> str:
    return getattr(request.user, "email", str(request.user))


class ObservationListView(View):
    def get(self, request):
        qs = Observation.objects.all()
        topic = (request.GET.get("topic") or "").strip()
        source = (request.GET.get("source") or "").strip()
        if topic:
            qs = qs.filter(topic__icontains=topic)
        if source:
            qs = qs.filter(source_type=source)
        return render(request, "ops/observations/list.html", {
            "observations": qs[:300],
            "topic": topic,
            "source": source,
            "sources": Observation.SourceType.choices,
            "kinds": Observation.MetricKind.choices,
            "columns": service.COLUMNS,
        })


class ObservationTemplateView(View):
    def get(self, request):
        response = HttpResponse(service.template_csv(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="observations-template.csv"'
        return response


class ObservationImportView(View):
    def post(self, request):
        back = redirect(reverse("ops-observations"))
        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, "Choose a CSV file.")
            return back
        if upload.size > MAX_UPLOAD:
            messages.error(request, "That file is too large for an observation sheet.")
            return back
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            messages.error(request, "Save the sheet as CSV (UTF-8) and try again.")
            return back
        rows, errors = service.parse_csv(text)
        if errors:
            # All or nothing: a partly imported sheet is harder to fix than a
            # refused one, because the operator has to work out what landed.
            messages.error(request, "Nothing imported. Fix these rows and import again:")
            for error in errors[:20]:
                messages.error(request, error)
            return back
        batch = f"{upload.name} · {timezone.now():%Y-%m-%d %H:%M}"
        saved = service.save_all(rows, entered_by=_actor(request), batch=batch)
        messages.success(request, f"Imported {len(saved)} observation(s) from {upload.name}.")
        return back


class ObservationEditView(View):
    """Add (pk=None) or correct one observation."""

    def get(self, request, pk=None):
        obs = get_object_or_404(Observation, pk=pk) if pk else None
        return render(request, "ops/observations/edit.html", {
            "obs": obs,
            "sources": Observation.SourceType.choices,
            "kinds": Observation.MetricKind.choices,
        })

    def post(self, request, pk=None):
        obs = get_object_or_404(Observation, pk=pk) if pk else None
        raw = {k: request.POST.get(k, "") for k in service.COLUMNS}
        for flag in ("is_ad", "is_brand_content", "is_old_material", "coverage_gap"):
            raw[flag] = "yes" if request.POST.get(flag) else "no"
        try:
            data = service.clean(raw)
        except service.ObservationError as exc:
            messages.error(request, str(exc))
            return render(request, "ops/observations/edit.html", {
                "obs": obs, "draft": raw,
                "sources": Observation.SourceType.choices, "kinds": Observation.MetricKind.choices,
            })
        if obs is None:
            obs = Observation.objects.create(
                **data, entered_by=_actor(request), import_batch="form"
            )
            messages.success(request, "Observation added.")
        else:
            for key, value in data.items():
                setattr(obs, key, value)
            obs.save()
            messages.success(request, "Observation saved.")
        return redirect(reverse("ops-observations") + f"?topic={obs.topic}")

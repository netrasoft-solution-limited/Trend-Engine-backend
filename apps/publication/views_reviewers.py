"""Expert reviewers — registered by a Platform Admin on screen (Mark, 24 Oct).

The register itself lives in `apps.outputs.reviewers`; this is its screen, next
to the outputs whose sign-off it governs.
"""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.operations.permissions import require_platform_admin
from apps.outputs import reviewers
from apps.outputs.models import ExpertReviewer


def _actor(request) -> str:
    return getattr(request.user, "email", str(request.user))


class ReviewerListView(View):
    def get(self, request):
        require_platform_admin(request, action="Managing expert reviewers")
        return render(request, "ops/outputs/reviewers.html", {
            "reviewers": ExpertReviewer.objects.all(),
            "next": request.GET.get("next", ""),
        })

    def post(self, request):
        require_platform_admin(request, action="Managing expert reviewers")
        action = request.POST.get("action") or ""
        try:
            if action == "add":
                reviewer = reviewers.register(
                    name=request.POST.get("name") or "",
                    qualification=request.POST.get("qualification") or "",
                    email=request.POST.get("email") or "",
                    actor_label=_actor(request),
                )
                messages.success(
                    request, f"{reviewer.name} can now be named on an expert sign-off."
                )
            elif action in ("deactivate", "reactivate"):
                reviewer = get_object_or_404(ExpertReviewer, pk=request.POST.get("reviewer"))
                reviewers.set_active(reviewer, action == "reactivate", actor_label=_actor(request))
                messages.info(request, f"{reviewer.name} is now "
                              f"{'active' if reviewer.is_active else 'inactive'}.")
        except reviewers.ReviewerError as exc:
            messages.error(request, str(exc))
        target = request.POST.get("next") or ""
        # Only back to an output page on this site, never an arbitrary URL.
        if target.startswith("/output/"):
            return redirect(target)
        return redirect(reverse("ops-reviewers"))

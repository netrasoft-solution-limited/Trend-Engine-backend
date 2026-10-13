"""URL patterns for scoring, mounted at the ops origin root by config/urls_ops.py."""
from django.urls import path

from . import views, views_review

urlpatterns = [
    # Triage · Signal review (PRD §6.5). The client is in the path, as on the
    # client screens, so a review page always means the client it names.
    path("triage/", views_review.TriageIndexView.as_view(), name="ops-triage-index"),
    path("triage/<slug:slug>/", views_review.TriageView.as_view(), name="ops-triage"),
    path(
        "triage/<slug:slug>/<str:code>/",
        views_review.SignalReviewView.as_view(),
        name="ops-signal",
    ),
    path(
        "relevance/<slug:slug>/",
        views.ClientRelevanceView.as_view(),
        name="ops-client-relevance",
    ),
    path(
        "relevance/<slug:slug>/score/",
        views.ClientScoreView.as_view(),
        name="ops-client-score",
    ),
]

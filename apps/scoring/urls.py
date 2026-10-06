"""URL patterns for scoring, mounted at the ops origin root by config/urls_ops.py."""
from django.urls import path

from . import views

urlpatterns = [
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

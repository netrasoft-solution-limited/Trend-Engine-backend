"""URL patterns for the evidence screens, mounted at the ops origin root.

`runs/` is PRD §6.5's Ingestion Runs screen. It lives in this app rather than
`apps.ingestion` because it shows claims, and only `enrichment` may read runs,
items and claims together — see the module docstring of `views.py`.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("runs/", views.RunListView.as_view(), name="ops-runs"),
    path("runs/<int:pk>/", views.RunDetailView.as_view(), name="ops-run"),
    path("items/<int:pk>/", views.ItemDetailView.as_view(), name="ops-item"),
    path("items/<int:pk>/retry/", views.RetryAnalysisView.as_view(), name="ops-item-retry"),
    path("runs/retry/", views.RetryAnalysisView.as_view(), name="ops-runs-retry"),
]

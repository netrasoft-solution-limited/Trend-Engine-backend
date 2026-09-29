"""URL patterns for the Triage JSON endpoints, mounted at `ops/api/triage/` by
config/urls_ops.py."""
from django.urls import path

from . import api

urlpatterns = [
    path("summary", api.TriageSummaryView.as_view(), name="ops-triage-summary"),
]

"""URL patterns for the Triage JSON endpoints, mounted at `api/triage/` by
config/urls_ops.py, on the `ops.<domain>` origin."""
from django.urls import path

from . import api

urlpatterns = [
    path("summary", api.TriageSummaryView.as_view(), name="ops-triage-summary"),
]

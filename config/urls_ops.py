"""Operator plane URLconf — mounted at /ops/ by Caddy.

Every operator screen from PRD §6.5 lives here. The portal process does not
import this module.
"""
from django.urls import include, path

urlpatterns = [
    # The JSON API for the React ops client. Prefixed like the portal's
    # `portal/`: Caddy forwards `/ops/*` here without stripping it.
    path("ops/api/auth/", include("apps.operations.api_urls")),
    path("ops/api/triage/", include("apps.scoring.api_urls")),
    # PRD §6.5 required screens, each owned by the app that owns its data.
    path("", include("apps.scoring.urls")),          # Triage · Signal review
    path("research/", include("apps.research.urls")),  # Research inbox
    path("sources/", include("apps.sources.urls")),    # Source registry
    path("runs/", include("apps.ingestion.urls")),     # Ingestion runs
    path("resolution/", include("apps.evidence.urls")),  # Resolution queue
    path("client/", include("apps.clients.urls")),     # Client profile
    path("output/", include("apps.outputs.urls")),     # Output builder
    path("tenants/", include("apps.billing.urls")),    # Organizations
    path("operations/", include("apps.operations.urls")),  # Operations
]

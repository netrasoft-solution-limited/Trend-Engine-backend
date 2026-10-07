"""Operator plane URLconf — the whole of `ops.<domain>`.

The planes are split by ORIGIN, not by path (Arch §5.3 as amended), so this
plane owns its origin's root. There is no `/ops/` prefix: on `ops.<domain>`
it would be redundant, and a half-applied one is worse than none — the API
carried it while `LOGIN_URL` did not, so the two disagreed about where the
plane began.

Every operator screen from PRD §6.5 lives here. The portal process does not
import this module.
"""
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    # The bare origin. Without this, https://ops.<domain>/ is a 404 — which is
    # what anyone typing the hostname from a message gets, and it reads as "the
    # thing is broken" rather than "you need a path". Anonymous visitors are
    # bounced on to /login by LoginRequiredMiddleware from here.
    path("", RedirectView.as_view(pattern_name="ops-clients", permanent=False)),
    # The JSON API for the React ops client, at this origin's root.
    path("api/auth/", include("apps.operations.api_urls")),
    # Server-rendered sign-in at the origin root. LOGIN_URL points at /login,
    # so without this every operator screen redirects to a 404. A separate
    # module from `operations.urls` below, which is mounted at a prefix — one
    # module under two prefixes served every login page twice.
    path("", include("apps.operations.auth_urls")),
    path("api/triage/", include("apps.scoring.api_urls")),
    # PRD §6.5 required screens, each owned by the app that owns its data.
    path("", include("apps.scoring.urls")),          # Triage · Signal review
    path("research/", include("apps.research.urls")),  # Research inbox
    path("sources/", include("apps.sources.urls")),    # Source registry
    path("runs/", include("apps.ingestion.urls")),     # Ingestion runs
    path("resolution/", include("apps.evidence.urls")),  # Resolution queue
    path("client/", include("apps.clients.urls")),     # Client profile
    # The output builder lives in `publication`: its screens drive the gate,
    # and the gate is a layer above `outputs`.
    path("output/", include("apps.publication.urls")),  # Output builder
    path("tenants/", include("apps.billing.urls")),    # Organizations
    path("operations/", include("apps.operations.urls")),  # Operations
]

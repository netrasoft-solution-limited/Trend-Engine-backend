"""Tenant plane URLconf — mounted at /portal/ by Caddy.

One include, deliberately. Everything the client can reach is inside
`apps.portal`, and `apps.portal` reads content only through `apps.publication`.
Adding a second include here would be the moment the gate stopped being the
only door.
"""
from django.urls import include, path

urlpatterns = [
    path("", include("apps.portal.urls")),
]

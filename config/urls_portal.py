"""Tenant plane URLconf — the apex domain's /portal/ prefix.

One include, deliberately. Everything the client can reach is inside
`apps.portal`, and `apps.portal` reaches content only through
`apps.publication`. Adding a second include here would be the moment the gate
stopped being the only door.
"""
from django.urls import include, path

urlpatterns = [
    path("portal/", include("apps.portal.urls")),
]

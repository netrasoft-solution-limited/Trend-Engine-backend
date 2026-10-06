"""URL patterns for sources, mounted at `sources/` by config/urls_ops.py.

Operator plane only. `config/urls_portal.py` does not include this module, so
there is no route from the portal process to any of it.
"""
from django.urls import path

from . import views

urlpatterns = [
    # The registry — which shows and channels are watched (PRD §6.5).
    path("", views.SourceListView.as_view(), name="ops-sources"),
    path("<int:pk>/", views.SourceDetailView.as_view(), name="ops-source"),
    # Vendor credentials. Listed after the registry so that `sources/` is the
    # registry rather than the credential screen, which is the rarer errand.
    path("providers/", views.ProviderListView.as_view(), name="ops-providers"),
    path(
        "providers/<int:pk>/credentials/",
        views.ProviderCredentialsView.as_view(),
        name="ops-provider-credentials",
    ),
]

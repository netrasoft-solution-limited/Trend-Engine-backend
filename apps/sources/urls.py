"""URL patterns for sources, mounted at `sources/` by config/urls_ops.py.

Operator plane only. `config/urls_portal.py` does not include this module, so
there is no route from the portal process to any of it.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("providers/", views.ProviderListView.as_view(), name="ops-providers"),
    path(
        "providers/<int:pk>/credentials/",
        views.ProviderCredentialsView.as_view(),
        name="ops-provider-credentials",
    ),
]

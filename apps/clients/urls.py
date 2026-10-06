"""URL patterns for clients, mounted at `client/` by config/urls_ops.py.

Operator plane only. Every per-client route carries the slug, so a page is
bookmarkable and no hidden session state can decide which client you are
looking at.

Scoring is not here: `apps.scoring` is a layer above this app, so it owns its
own screen and this one links to it.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.ClientListView.as_view(), name="ops-clients"),
    path("<slug:slug>/", views.ClientHubView.as_view(), name="ops-client"),
    path("<slug:slug>/discover/", views.ClientDiscoverView.as_view(), name="ops-client-discover"),
    path("<slug:slug>/contacts/", views.ClientContactsView.as_view(), name="ops-client-contacts"),
    path(
        "<slug:slug>/profile/draft/",
        views.ClientProfileDraftView.as_view(),
        name="ops-client-profile-draft",
    ),
    path(
        "<slug:slug>/profile/<int:number>/",
        views.ClientProfileView.as_view(),
        name="ops-client-profile",
    ),
    path(
        "<slug:slug>/profile/<int:number>/activate/",
        views.ClientProfileActivateView.as_view(),
        name="ops-client-profile-activate",
    ),
]

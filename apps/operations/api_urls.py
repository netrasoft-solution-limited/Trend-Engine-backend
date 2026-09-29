"""URL patterns for the operator plane's auth API.

Mounted at `ops/api/auth/` by config/urls_ops.py. Caddy forwards `/ops/*` to
the ops process without stripping the prefix, as it does `/portal/*` for the
portal.
"""
from django.urls import path

from . import api

urlpatterns = [
    path("csrf", api.CsrfView.as_view(), name="ops-csrf"),
    path("login", api.LoginView.as_view(), name="ops-login"),
    path("logout", api.LogoutView.as_view(), name="ops-logout"),
    path("session", api.SessionView.as_view(), name="ops-session"),
]

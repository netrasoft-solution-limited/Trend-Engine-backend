"""URL patterns for the operator plane's auth API.

Mounted at `api/auth/` by config/urls_ops.py, on the `ops.<domain>` origin.
"""
from django.urls import path

from . import api

urlpatterns = [
    path("csrf", api.CsrfView.as_view(), name="ops-csrf"),
    path("login", api.LoginView.as_view(), name="ops-login"),
    path("logout", api.LogoutView.as_view(), name="ops-logout"),
    path("session", api.SessionView.as_view(), name="ops-session"),
    # The second factor. All three are reachable only from a pending state —
    # a correct password — and none of them creates a session on its own.
    path("mfa/setup", api.MfaSetupView.as_view(), name="ops-mfa-setup"),
    path("mfa/confirm", api.MfaConfirmView.as_view(), name="ops-mfa-confirm"),
    path("mfa/verify", api.MfaVerifyView.as_view(), name="ops-mfa-verify"),
]

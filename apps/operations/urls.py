"""Operator sign-in, mounted at the root of `ops.<domain>`.

Server-rendered, because the React ops client is still a mock and
`LOGIN_URL` has to point at something that exists — without these,
`LoginRequiredMiddleware` redirects every operator screen to a 404.

The JSON equivalents in `api_urls.py` stay: both share the service layer in
`apps.operations.mfa`, so the browser flow and the API cannot disagree about
what a second factor means.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("login", views.LoginView.as_view(), name="ops-login"),
    path("logout", views.LogoutView.as_view(), name="ops-logout-form"),
    path("mfa", views.MfaView.as_view(), name="ops-mfa"),
    path("mfa/setup", views.MfaSetupView.as_view(), name="ops-mfa-setup"),
]

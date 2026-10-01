"""Operator sign-in, mounted at the ROOT of `ops.<domain>`.

Separate from `urls.py`, which config/urls_ops.py mounts at `operations/` for
the Operations dashboard (PRD §6.5). Putting these there served every login
page twice — once at /login and once at /operations/login — because the same
module was included under both prefixes.

Server-rendered, because the React ops client is still a mock and `LOGIN_URL`
has to point at something that exists: without these, LoginRequiredMiddleware
redirects every operator screen to a 404.

The JSON equivalents in `api_urls.py` stay. Both share the service layer in
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

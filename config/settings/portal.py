"""Tenant plane — `/portal/*`.

Loaded only by `config.wsgi_portal`. Arch §5.3:

    entry point    /portal/*
    session cookie __Host-te_portal
    cookie path    /portal
    user model     OrgUser
    auth backend   portal backend
    exposure       public internet, invite-only accounts
    tenant binding hard-bound to the session's org — cannot be widened

ADR #3 accepts one extra process to buy this. The point is that a compromised
portal session has no shared artifact with an operator session and no code path
to operator scope: the operator URLconf is not even loaded here.
"""
from .base import *  # noqa: F401,F403
from .base import COMMON_MIDDLEWARE

ROOT_URLCONF = "config.urls_portal"
WSGI_APPLICATION = "config.wsgi_portal.application"

AUTH_USER_MODEL = "portal.OrgUser"

AUTHENTICATION_BACKENDS = [
    "apps.portal.auth.PortalBackend",
]

SESSION_COOKIE_NAME = "__Host-te_portal"
SESSION_COOKIE_PATH = "/portal"
CSRF_COOKIE_NAME = "__Host-te_portal_csrf"
CSRF_COOKIE_PATH = "/portal"

LOGIN_URL = "/portal/login/"

MIDDLEWARE = COMMON_MIDDLEWARE + [
    # Marks the context portal-side, then hard-binds the session's org.
    # `bind_operator_all()` raises once this has run.
    "apps.tenancy.middleware.PortalTenantMiddleware",
]

# PRD §6.8: invite-based provisioning only. There is no signup view, and this
# flag exists so that adding one is a deliberate settings change someone has to
# justify in review rather than a URL someone can quietly append.
PORTAL_ALLOW_SELF_SIGNUP = False

# PRD §7.6: portal users are identifiable individuals, so GDPR/CCPA applies to
# this plane independently of the evidence layer's rights framework.
PORTAL_LOGIN_AUDIT_RETENTION_DAYS = 730

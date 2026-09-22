"""Tenant plane — `/portal/*` on the apex domain.

Loaded only by `config.wsgi_portal`. Arch §5.3, as amended:

    entry point    https://<domain>/portal/*
    session cookie __Host-te_portal
    cookie path    /            (see the note below)
    user model     portal.OrgUser
    auth backend   portal backend
    exposure       public internet, invite-only accounts
    tenant binding hard-bound to an organisation the session's user is a
                   member of — re-verified on every request

ADR #3 accepts one extra process to buy this. A compromised portal session has
no shared artifact with an operator session and no code path to operator scope:
the operator URLconf is not loaded here, `AUTHENTICATION_BACKENDS` lists only
the portal backend, and `TENANT_PLANE = "portal"` makes
`bind_operator_all()` raise.

ON THE COOKIE PATH
------------------
Arch §5.3 originally specified `__Host-te_portal` with `Path=/portal`. That
combination does not work: the `__Host-` prefix requires `Path=/` exactly
(RFC 6265bis §4.1.3.2), so every browser silently drops the `Set-Cookie` and
nobody can log in. The planes are now separated by ORIGIN — operator on
`ops.<domain>` — which is a stronger boundary than a cookie path was ever going
to be, and it lets both planes keep a genuine `__Host-` prefix at `Path=/`.
"""
from .base import *  # noqa: F401,F403
from .base import _INSECURE_COOKIES, COMMON_MIDDLEWARE, env

TENANT_PLANE = "portal"

ROOT_URLCONF = "config.urls_portal"
WSGI_APPLICATION = "config.wsgi_portal.application"

AUTH_USER_MODEL = "portal.OrgUser"

AUTHENTICATION_BACKENDS = [
    "apps.portal.auth.PortalBackend",
]

# `__Host-` mandates Secure + Path=/ + no Domain. Local development
# cannot satisfy Secure over plain HTTP, so the prefix is dropped there
# and only there — production always gets the prefixed name.
SESSION_COOKIE_NAME = "te_portal" if _INSECURE_COOKIES else "__Host-te_portal"
SESSION_COOKIE_PATH = "/"
#: Longer than ops: clients read at their own pace, and PRD §6.8 asks for
#: session management rather than aggressive expiry.
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_SAVE_EVERY_REQUEST = True

LOGIN_URL = "/portal/login"

ALLOWED_HOSTS = [h for h in env("PORTAL_ALLOWED_HOSTS", "").split(",") if h] or ALLOWED_HOSTS
CSRF_TRUSTED_ORIGINS = [o for o in env("PORTAL_TRUSTED_ORIGINS", "").split(",") if o]

MIDDLEWARE = COMMON_MIDDLEWARE + [
    # Binds the active organisation, or nothing at all for anonymous requests.
    # Exists in this process only.
    "apps.tenancy.middleware.PortalTenantMiddleware",
    # Django 5.1+. Routing default-deny to match the ORM's: a view without
    # @login_not_required is authenticated-only, so a forgotten decorator fails
    # closed rather than open. Must come after the tenant middleware so that
    # 401s are decided once the session is understood.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
]

# PRD §6.8: invite-based provisioning only. This flag exists so that adding a
# signup view is a deliberate settings change someone has to justify in review,
# rather than a URL someone can quietly append.
PORTAL_ALLOW_SELF_SIGNUP = False

# PRD §7.6: portal users are identifiable individuals, so GDPR/CCPA applies to
# this plane independently of the evidence layer's rights framework.
PORTAL_LOGIN_AUDIT_RETENTION_DAYS = 730

#: Failed logins per email per window, before the attempt is refused outright.
PORTAL_LOGIN_MAX_ATTEMPTS = 8
PORTAL_LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60

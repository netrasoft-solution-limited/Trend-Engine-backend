"""Operator plane — `ops.<domain>`.

Loaded only by `config.wsgi_ops`. Arch §5.3, as amended:

    entry point    https://ops.<domain>/
    session cookie __Host-te_ops
    cookie path    /
    user model     operations.OperatorUser
    auth backend   operator backend + MFA
    exposure       identity-aware / IP-restricted — never simply public
    tenant binding OPERATOR_ALL by default, narrowed by UI selection

The planes were originally split by URL path on one domain. They are now split
by ORIGIN. Two reasons: cookie `Path` is matched against the *request* URL, not
the calling page, so a stored XSS on a portal page could still have issued
`fetch('/ops/…', {credentials: 'include'})`; and `__Host-` cookies require
`Path=/`, which a path-split deployment cannot provide.
"""
from .base import *  # noqa: F401,F403
from .base import _INSECURE_COOKIES, COMMON_MIDDLEWARE, env

TENANT_PLANE = "operator"

ROOT_URLCONF = "config.urls_ops"
WSGI_APPLICATION = "config.wsgi_ops.application"

AUTH_USER_MODEL = "operations.OperatorUser"

AUTHENTICATION_BACKENDS = [
    "apps.operations.auth.OperatorBackend",
]

# `__Host-` mandates Secure + Path=/ + no Domain. Local development
# cannot satisfy Secure over plain HTTP, so the prefix is dropped there
# and only there — production always gets the prefixed name.
SESSION_COOKIE_NAME = "te_ops" if _INSECURE_COOKIES else "__Host-te_ops"
SESSION_COOKIE_PATH = "/"
#: Materially shorter than the portal. Operator sessions can reach every
#: tenant's data; they should not sit open for a fortnight.
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

LOGIN_URL = "/login"

ALLOWED_HOSTS = [h for h in env("OPS_ALLOWED_HOSTS", "").split(",") if h] or ALLOWED_HOSTS
CSRF_TRUSTED_ORIGINS = [o for o in env("OPS_TRUSTED_ORIGINS", "").split(",") if o]

MIDDLEWARE = COMMON_MIDDLEWARE + [
    # Binds OPERATOR_ALL unless the operator has narrowed scope in the UI.
    "apps.tenancy.middleware.OperatorTenantMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
]

# PRD §7.1: no open registration anywhere, MFA where supported.
OPERATOR_REQUIRE_MFA = True

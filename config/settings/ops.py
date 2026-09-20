"""Operator plane — `/ops/*`.

Loaded only by `config.wsgi_ops`. Arch §5.3:

    entry point    /ops/*
    session cookie __Host-te_ops
    cookie path    /ops
    user model     OperatorUser
    auth backend   operator backend + MFA
    exposure       identity-aware / IP-restricted
    tenant binding OPERATOR_ALL by default, narrowed by UI selection
"""
from .base import *  # noqa: F401,F403
from .base import COMMON_MIDDLEWARE

ROOT_URLCONF = "config.urls_ops"
WSGI_APPLICATION = "config.wsgi_ops.application"

AUTH_USER_MODEL = "operations.OperatorUser"

AUTHENTICATION_BACKENDS = [
    "apps.operations.auth.OperatorBackend",
]

# Distinct cookie name AND path. The path is what stops the browser from ever
# sending an operator session to /portal/* in the first place.
SESSION_COOKIE_NAME = "__Host-te_ops"
SESSION_COOKIE_PATH = "/ops"
CSRF_COOKIE_NAME = "__Host-te_ops_csrf"
CSRF_COOKIE_PATH = "/ops"

LOGIN_URL = "/ops/login/"

MIDDLEWARE = COMMON_MIDDLEWARE + [
    # Binds OPERATOR_ALL unless the operator has narrowed scope in the UI.
    # This middleware exists in this process only.
    "apps.tenancy.middleware.OperatorTenantMiddleware",
]

# PRD §7.1: no open registration anywhere, MFA where supported.
OPERATOR_REQUIRE_MFA = True

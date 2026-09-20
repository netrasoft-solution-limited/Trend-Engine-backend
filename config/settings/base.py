"""Settings shared by both planes.

This module installs no auth backend, no session cookie and no tenant
middleware. Those differ per plane and are set in `ops.py` and `portal.py` —
keeping them out of here means neither plane can inherit the other's by
accident.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

# ── Applications ────────────────────────────────────────────────────────────
# Ordered by layer, lowest first. The ordering is documentation: dependencies
# point downward only, and import-linter enforces it (Arch §4).

DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

LOCAL_APPS = [
    "apps.tenancy",       # L0  · tenant root and scoping
    "apps.sources",       # L1  · provider registry, policy versions
    "apps.connectors",    # L1  · adapters
    "apps.ingestion",     # L1–L2 · runs, raw items, normalization
    "apps.evidence",      # L2  · content items, segments, rights
    "apps.domains",       # L3  · domain packs
    "apps.enrichment",    # L3–L4 · extraction, embeddings
    "apps.research",      # L3–L4 · scientific track
    "apps.intelligence",  # L4  · clustering, signals, confidence
    "apps.clients",       # L5  · client profiles          [tenant]
    "apps.scoring",       # L5  · tenant scoring           [tenant]
    "apps.outputs",       # L6  · output builder           [tenant]
    "apps.publication",   # L6–L7 · THE GATE               [tenant]
    "apps.portal",        # L7  · client portal            [tenant]
    "apps.billing",       # L7  · subscriptions            [tenant]
    "apps.operations",    # cross · health, cost, audit
]

INSTALLED_APPS = DJANGO_APPS + LOCAL_APPS

# Middleware common to both planes. Each plane appends its own auth and tenant
# binding — see ops.py and portal.py.
COMMON_MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# ── Database ────────────────────────────────────────────────────────────────
# One Postgres with pgvector, shared schema, row-level tenant scoping.
# Arch §5.1 rejects database-per-tenant and schema-per-tenant: both would break
# evidence sharing, which is the business model.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "trend_engine"),
        "USER": env("POSTGRES_USER", "trend_engine"),
        "PASSWORD": env("POSTGRES_PASSWORD"),
        "HOST": env("POSTGRES_HOST", "postgres"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Cache, broker, storage ──────────────────────────────────────────────────

REDIS_URL = env("REDIS_URL", "redis://redis:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

STORAGES = {
    "default": {"BACKEND": "storages.backends.s3.S3Storage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ── Templates ───────────────────────────────────────────────────────────────
# ADR #9: Django templates + HTMX. No SPA. The React prototype in ../frontend
# is a design reference these templates are built from, not a deployed artifact.

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

# ── Security (both planes) ──────────────────────────────────────────────────
# Arch §12. TLS terminates at Caddy, so the proxy header is what tells Django
# the original request was secure.

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ── Logging ─────────────────────────────────────────────────────────────────
# Arch §12: no client-private content in logs or error trackers.
# Arch §13: a TenantScopeError in production is a P1 — route it loudly.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "apps.tenancy": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

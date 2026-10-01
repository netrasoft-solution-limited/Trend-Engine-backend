"""Settings shared by both planes.

What is deliberately NOT here: the auth backend, the user model, the session
and CSRF cookie *names and paths*, the tenant middleware, and `TENANT_PLANE`.
Those differ per plane and are set in `ops.py` and `portal.py` — keeping them
out of this module means neither plane can inherit the other's by accident.

What IS here: the cookie security flags, the password policy and the session
machinery, which must be identical on both planes. A weaker password rule on
one plane than the other would be a silent asymmetry.
"""
from __future__ import annotations

import os
from pathlib import Path

from .mail import email_settings as _email_settings

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

# `django.contrib.admin` IS DELIBERATELY ABSENT. Its `LogEntry` carries a
# foreign key to `settings.AUTH_USER_MODEL`, which Django records in the
# migration as a swappable dependency — so the same `django_admin_log.user_id`
# column points at `operations_operatoruser` in the ops process and
# `portal_orguser` in the portal process. That is precisely the silent join
# across two identity tables that `apps/operations/models.py` forbids, and it
# is NOT caught by the dual `makemigrations --check`, because a swappable
# dependency is settings-dependent by design and reports no changes under
# either. Provider configuration has its own operator screen instead; see
# `apps/sources/views.py`. `tests/test_migrations_are_settings_independent.py`
# now fails if anything reintroduces such a dependency.
DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
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

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# INSTALLED_APPS is IDENTICAL on both planes, on purpose. Excluding the
# operator apps from the portal registry would be tempting, but it makes the
# `migrate` plan diverge between the two settings modules, which destroys the
# ability to assert that both produce the same schema (see the dual
# `makemigrations --check` in CI). The planes are separated by URLconf,
# AUTHENTICATION_BACKENDS, .importlinter and TENANT_PLANE — not by registry.

#: Which plane this PROCESS serves. Overridden to "portal" in portal.py.
#: `apps.tenancy.context.bind_operator_all()` refuses when this is "portal",
#: which is what makes the refusal hold for Celery tasks and management
#: commands, not only for requests that reached the middleware.
TENANT_PLANE = "operator"

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
        # Check a reused connection before handing it to a request. A managed
        # Postgres that suspends when idle (Neon) closes connections this
        # process still holds; without the check, the first request after a
        # suspend fails with "server closed the connection unexpectedly".
        "CONN_HEALTH_CHECKS": True,
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

# `config/celery.py` calls `config_from_object("django.conf:settings",
# namespace="CELERY")`, so Celery reads CELERY_-prefixed settings from here and
# NOTHING ELSE. Without this line it never sees REDIS_URL and silently falls
# back to its own default broker — amqp://guest@localhost:5672, a RabbitMQ that
# does not exist in this deployment.
#
# That failure is quiet in the worst way: every worker and beat start, report
# healthy to `docker compose ps`, and sit in a reconnect loop. The whole async
# pipeline — collection, the relevance gate, transcripts, extraction — is dead
# while the stack looks fine.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", REDIS_URL)

# No result backend, deliberately. Tasks return a dict for the logs and the
# Ingestion Runs screen reads the database, not Celery — so storing every
# result would write rows to Redis that nothing ever reads back.
CELERY_TASK_IGNORE_RESULT = True

STORAGES = {
    "default": {"BACKEND": "storages.backends.s3.S3Storage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ── Templates ───────────────────────────────────────────────────────────────
# ADR #9 as amended (Arch §15.1 A1): Django templates + HTMX for the OPERATOR
# plane. The tenant plane is a React SPA against the JSON API in apps/portal,
# so these templates currently serve email bodies and the operator UI only.

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

#: How many proxies sit between the internet and this process — 1 for the
#: Caddy in docker-compose. `apps.operations.throttle.client_ip` counts that
#: many entries from the RIGHT of X-Forwarded-For, because Caddy appends the
#: real peer to whatever arrived and everything to its left is caller-supplied.
#:
#: ZERO means the header is ignored entirely. That is the safe default: trusting
#: a forged X-Forwarded-For would let one attacker present a new address per
#: request and never be throttled at all, which is worse than no throttle,
#: because the logs would look fine.
TRUSTED_PROXY_DEPTH = int(env("TRUSTED_PROXY_DEPTH", "0"))
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# Secure by default. A plain-HTTP dev server cannot set a Secure cookie, so
# local development opts out explicitly rather than the default being weak.
# Note the `__Host-` cookie name also mandates Secure, so with this off the
# dev settings must use an unprefixed name — see ops.py / portal.py.
_INSECURE_COOKIES = env("DJANGO_INSECURE_COOKIES", "0") == "1"
SESSION_COOKIE_SECURE = not _INSECURE_COOKIES
CSRF_COOKIE_SECURE = not _INSECURE_COOKIES

# The session cookie carries the identity; the CSRF token lives in the session
# rather than a second cookie. One fewer cookie to path-scope correctly, and it
# means a token cannot be read by script on either plane.
CSRF_USE_SESSIONS = True

# Argon2 first. This is a credentialed multi-tenant SaaS holding a client's
# commercial intelligence; the default PBKDF2 is not the right trade here.
# Argon2 at OWASP's minimum memory rather than Django's 100 MiB, which
# out-of-memory-kills a worker on the current instance — see config/hashers.py.
PASSWORD_HASHERS = [
    "config.hashers.LowMemoryArgon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

# PRD §7.1. Absent entirely before this — neither plane validated password
# strength at all.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# DRF serves JSON to the React portal. Session authentication, not tokens: the
# cookie stays HttpOnly and out of reach of script. Authorisation is decided
# per view — there is no permissive default.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "EXCEPTION_HANDLER": "config.api.exception_handler",
}

# ── Email ───────────────────────────────────────────────────────────────────
# Resend, over its HTTP API (config/email.py). Selected automatically when
# RESEND_API_KEY is present; an explicit DJANGO_EMAIL_BACKEND still overrides,
# and EMAIL_HOST still selects SMTP for any other provider. Parsed in
# config/settings/mail.py.
#
# With NOTHING configured the backend is the CONSOLE backend in every
# environment, production included. Nothing fails: every message is written to
# the process log instead of being sent, and that includes the one-time links
# in invitations and password resets. `PortalConfig.ready()` logs a warning at
# startup when this happens with DEBUG off.
#
# RESEND WILL ONLY SEND FROM A DOMAIN VERIFIED IN THE RESEND ACCOUNT, which is
# a DNS task on that domain and not something a setting can arrange. Set
# DEFAULT_FROM_EMAIL to an address on a verified domain, or mail leaves here
# and is refused there.
_email = _email_settings(os.environ)
EMAIL_BACKEND = _email["EMAIL_BACKEND"]
EMAIL_HOST = _email["EMAIL_HOST"]
EMAIL_PORT = _email["EMAIL_PORT"]
EMAIL_HOST_USER = _email["EMAIL_HOST_USER"]
EMAIL_HOST_PASSWORD = _email["EMAIL_HOST_PASSWORD"]
EMAIL_USE_TLS = _email["EMAIL_USE_TLS"]
EMAIL_TIMEOUT = _email["EMAIL_TIMEOUT"]
DEFAULT_FROM_EMAIL = _email["DEFAULT_FROM_EMAIL"]
RESEND_API_KEY = _email["RESEND_API_KEY"]
PORTAL_PUBLIC_URL = env("PORTAL_PUBLIC_URL", "http://localhost:5173")

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ── The model layer (Arch §10.3) ────────────────────────────────────────────
# `LLM_API_KEY` and `LLM_MONTHLY_CAP_USD` have been in deploy/.env.example
# since the skeleton without anything reading them. These are the settings that
# read them.
#
# With no LLM_API_KEY the SDK falls back to ANTHROPIC_API_KEY, then
# ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile — so a developer who
# has logged in needs no key here at all.
LLM_API_KEY = env("LLM_API_KEY", "")

#: Arch §10.2's hard cap, enforced BEFORE each call, not reconciled after.
#: Zero disables the ceiling and logs a warning — it does not mean "free".
LLM_MONTHLY_CAP_USD = env("LLM_MONTHLY_CAP_USD", "0")

#: A single call this expensive is a bug — a runaway prompt, a document pasted
#: into a classifier. Refused before it is made.
LLM_PER_CALL_MAX_USD = env("LLM_PER_CALL_MAX_USD", "1.00")

# Tiered by task, per Arch §10.3: "Cheap tier for relevance classification
# (high volume, low stakes); stronger tier only for client-facing drafting."
# Overridable so a tier can be re-pointed when a price or capability changes,
# without a deploy.
LLM_MODEL_GATE = env("LLM_MODEL_GATE", "claude-haiku-4-5")
LLM_MODEL_EXTRACTION = env("LLM_MODEL_EXTRACTION", "claude-sonnet-5-5")
LLM_MODEL_DRAFTING = env("LLM_MODEL_DRAFTING", "claude-opus-5-5")

# ── Provider credentials (Arch §12) ─────────────────────────────────────────
# Vendor keys live in the database, encrypted, so an operator can rotate one
# without a deploy. This is the key that encrypts them.
#
# Left unset it is DERIVED from SECRET_KEY, which keeps development and CI
# working with no extra configuration. The consequence is real and worth
# stating: rotating SECRET_KEY then makes every stored credential unreadable
# and they have to be re-entered. Production sets this explicitly so the two
# rotate independently.
#
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CREDENTIALS_ENCRYPTION_KEY = env("CREDENTIALS_ENCRYPTION_KEY", "")

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

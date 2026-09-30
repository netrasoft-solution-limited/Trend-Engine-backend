"""Outbound email configuration, parsed from the environment.

A pure function of a mapping rather than module-level `os.environ` reads, so
the parsing can be tested without re-importing the settings. `base.py` calls
it once with `os.environ`.

PRD §13's undecided provider is now Resend, over its HTTP API rather than
SMTP — see `config/email.py` for why. The SMTP variables remain, because
nothing here is Resend-specific: setting EMAIL_HOST and friends still works
for any provider, and the console backend is still the default when neither is
configured.

Backend selection, in order: an explicit DJANGO_EMAIL_BACKEND wins; then a
RESEND_API_KEY picks the Resend backend; then an EMAIL_HOST picks Django's
SMTP backend; otherwise the console. That order means a developer can override
anything, and a deploy that sets only the API key does the right thing rather
than printing mail to a log nobody reads.

A variable that is present but blank counts as unset. A dashboard field left
empty should behave like a field never added, not like an empty host.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONSOLE_EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
SMTP_EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
RESEND_EMAIL_BACKEND = "config.email.ResendBackend"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _text(environ: Mapping[str, str], name: str, default: str) -> str:
    value = environ.get(name, "").strip()
    return value or default


def _bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    value = environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise RuntimeError(f"{name} must be one of 1/0, true/false, yes/no, on/off; got {value!r}")


def _int(environ: Mapping[str, str], name: str, default: int) -> int:
    value = environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer; got {value!r}") from None


def email_settings(environ: Mapping[str, str]) -> dict[str, Any]:
    """The Django EMAIL_* settings for this environment.

    Never includes a secret in an error message: the only values echoed back
    on a parse failure are the boolean and the port.
    """
    return {
        "EMAIL_BACKEND": _backend(environ),
        "EMAIL_HOST": _text(environ, "EMAIL_HOST", "localhost"),
        "EMAIL_PORT": _int(environ, "EMAIL_PORT", 25),
        "EMAIL_HOST_USER": _text(environ, "EMAIL_HOST_USER", ""),
        # Not stripped: a password may legitimately begin or end with a space.
        "EMAIL_HOST_PASSWORD": environ.get("EMAIL_HOST_PASSWORD", ""),
        "EMAIL_USE_TLS": _bool(environ, "EMAIL_USE_TLS", False),
        # Seconds. Django's default is no timeout, so an SMTP server that
        # accepts the connection and then goes quiet holds the worker until
        # gunicorn kills it. With a timeout the send fails with a real error,
        # which on_commit(robust=True) logs.
        "EMAIL_TIMEOUT": _int(environ, "EMAIL_TIMEOUT", 10),
        "DEFAULT_FROM_EMAIL": _text(
            environ, "DEFAULT_FROM_EMAIL", "Trend Engine <no-reply@localhost>"
        ),
        # Not a Django setting — `config.email.ResendBackend` reads it off
        # settings. Kept here so every email decision is made in one tested
        # function rather than half here and half in base.py.
        "RESEND_API_KEY": environ.get("RESEND_API_KEY", "").strip(),
    }


def _backend(environ: Mapping[str, str]) -> str:
    """Which backend, from what is actually configured.

    An explicit choice always wins — a developer pointing this at the console
    backend while a key sits in their environment means it.
    """
    explicit = _text(environ, "DJANGO_EMAIL_BACKEND", "")
    if explicit:
        return explicit
    if environ.get("RESEND_API_KEY", "").strip():
        return RESEND_EMAIL_BACKEND
    if environ.get("EMAIL_HOST", "").strip():
        return SMTP_EMAIL_BACKEND
    return CONSOLE_EMAIL_BACKEND

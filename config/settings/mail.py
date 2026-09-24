"""Outbound email configuration, parsed from the environment.

A pure function of a mapping rather than module-level `os.environ` reads, so
the parsing can be tested without re-importing the settings. `base.py` calls
it once with `os.environ`.

PRD §13 leaves the transactional email provider undecided, so nothing here
names a vendor: any provider that speaks SMTP is configured with these
variables. Defaults are Django's own, except the backend, which is the console
backend until someone chooses otherwise — see the note in `base.py`.

A variable that is present but blank counts as unset. A dashboard field left
empty should behave like a field never added, not like an empty host.
"""
from __future__ import annotations

from typing import Any, Mapping

CONSOLE_EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

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
        "EMAIL_BACKEND": _text(environ, "DJANGO_EMAIL_BACKEND", CONSOLE_EMAIL_BACKEND),
        "EMAIL_HOST": _text(environ, "EMAIL_HOST", "localhost"),
        "EMAIL_PORT": _int(environ, "EMAIL_PORT", 25),
        "EMAIL_HOST_USER": _text(environ, "EMAIL_HOST_USER", ""),
        # Not stripped: a password may legitimately begin or end with a space.
        "EMAIL_HOST_PASSWORD": environ.get("EMAIL_HOST_PASSWORD", ""),
        "EMAIL_USE_TLS": _bool(environ, "EMAIL_USE_TLS", False),
        "DEFAULT_FROM_EMAIL": _text(
            environ, "DEFAULT_FROM_EMAIL", "Trend Engine <no-reply@localhost>"
        ),
    }

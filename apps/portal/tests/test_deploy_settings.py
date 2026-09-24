"""Deployment settings: email from the environment, the console-backend
warning, and database connection health checks.

No database needed. Runs under either settings module; CI runs it with
`--ds=config.settings.portal` alongside the registration tests.
"""
from __future__ import annotations

import logging
import os

import pytest
from django.conf import settings as django_settings

from apps.portal.notifications import warn_if_emails_go_to_the_log
from config.settings.mail import CONSOLE_EMAIL_BACKEND, email_settings

SMTP = "django.core.mail.backends.smtp.EmailBackend"


def _ours(caplog):
    return [r for r in caplog.records if r.name == "apps.portal.notifications"]

# ── Parsing ─────────────────────────────────────────────────────────────────


def test_defaults_when_nothing_is_set():
    assert email_settings({}) == {
        "EMAIL_BACKEND": CONSOLE_EMAIL_BACKEND,
        "EMAIL_HOST": "localhost",
        "EMAIL_PORT": 25,
        "EMAIL_HOST_USER": "",
        "EMAIL_HOST_PASSWORD": "",
        "EMAIL_USE_TLS": False,
        "DEFAULT_FROM_EMAIL": "Trend Engine <no-reply@localhost>",
    }


def test_values_from_the_environment():
    assert email_settings(
        {
            "DJANGO_EMAIL_BACKEND": SMTP,
            "EMAIL_HOST": "smtp.example.test",
            "EMAIL_PORT": "587",
            "EMAIL_HOST_USER": "fake-user",
            "EMAIL_HOST_PASSWORD": "fake-password",
            "EMAIL_USE_TLS": "1",
            "DEFAULT_FROM_EMAIL": "Example <no-reply@example.test>",
        }
    ) == {
        "EMAIL_BACKEND": SMTP,
        "EMAIL_HOST": "smtp.example.test",
        "EMAIL_PORT": 587,
        "EMAIL_HOST_USER": "fake-user",
        "EMAIL_HOST_PASSWORD": "fake-password",
        "EMAIL_USE_TLS": True,
        "DEFAULT_FROM_EMAIL": "Example <no-reply@example.test>",
    }


def test_blank_values_count_as_unset():
    blank = {
        name: "  "
        for name in (
            "DJANGO_EMAIL_BACKEND",
            "EMAIL_HOST",
            "EMAIL_PORT",
            "EMAIL_HOST_USER",
            "EMAIL_USE_TLS",
            "DEFAULT_FROM_EMAIL",
        )
    }
    assert email_settings(blank) == email_settings({})


def test_the_password_is_passed_through_verbatim():
    assert email_settings({"EMAIL_HOST_PASSWORD": " fake pw "})["EMAIL_HOST_PASSWORD"] == " fake pw "


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("False", False), ("no", False), ("off", False),
    ],
)
def test_use_tls_accepts_the_usual_spellings(raw, expected):
    assert email_settings({"EMAIL_USE_TLS": raw})["EMAIL_USE_TLS"] is expected


@pytest.mark.parametrize(("name", "raw"), [("EMAIL_USE_TLS", "maybe"), ("EMAIL_PORT", "smtp")])
def test_malformed_values_fail_naming_the_variable(name, raw):
    with pytest.raises(RuntimeError, match=name):
        email_settings({name: raw})


def test_the_settings_module_uses_the_parser():
    """base.py wires every parsed value through. EMAIL_BACKEND is excluded:
    the test runner swaps it for the in-memory backend."""
    expected = email_settings(os.environ)
    del expected["EMAIL_BACKEND"]
    assert {name: getattr(django_settings, name) for name in expected} == expected


# ── The console-backend warning ─────────────────────────────────────────────


def test_warns_when_debug_is_off_and_email_goes_to_the_log(settings, caplog):
    settings.DEBUG = False
    settings.EMAIL_BACKEND = CONSOLE_EMAIL_BACKEND

    with caplog.at_level(logging.WARNING, logger="apps.portal.notifications"):
        assert warn_if_emails_go_to_the_log() is True

    [record] = _ours(caplog)
    assert record.levelno == logging.WARNING
    assert "DJANGO_EMAIL_BACKEND" in record.getMessage()


@pytest.mark.parametrize(
    ("debug", "backend"),
    [(True, CONSOLE_EMAIL_BACKEND), (False, SMTP)],
    ids=["debug-on-console", "debug-off-smtp"],
)
def test_no_warning_otherwise(settings, caplog, debug, backend):
    settings.DEBUG = debug
    settings.EMAIL_BACKEND = backend

    with caplog.at_level(logging.WARNING, logger="apps.portal.notifications"):
        assert warn_if_emails_go_to_the_log() is False

    assert _ours(caplog) == []


# ── Database ────────────────────────────────────────────────────────────────


def test_reused_database_connections_are_health_checked():
    assert django_settings.DATABASES["default"]["CONN_HEALTH_CHECKS"] is True

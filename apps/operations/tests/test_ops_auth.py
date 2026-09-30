"""Operator JSON auth — csrf, login, logout, session — and the MFA guard.

Runs under the OPERATOR settings (the pytest default):

    pytest apps/operations/tests/test_ops_auth.py
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.operations.models import OperatorUser

pytestmark = pytest.mark.django_db

CSRF = "/api/auth/csrf"
LOGIN = "/api/auth/login"
LOGOUT = "/api/auth/logout"
SESSION = "/api/auth/session"

EMAIL = "abubakar@pureplay.example"
PASSWORD = "correct-horse-battery-9"


@pytest.fixture(autouse=True)
def _operator_plane(settings):
    assert settings.ROOT_URLCONF == "config.urls_ops", "Run these tests with the ops settings"
    # Isolated from Redis, and from every other test's throttle buckets.
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ops-auth-tests",
        }
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def mfa_off(settings):
    settings.OPERATOR_REQUIRE_MFA = False


@pytest.fixture
def mfa_on(settings):
    settings.OPERATOR_REQUIRE_MFA = True


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def operator_user(db):
    return OperatorUser.objects.create_user(
        email=EMAIL, password=PASSWORD, name="Abubakar", role=OperatorUser.Role.OPERATOR
    )


def login(api, password=PASSWORD, email=EMAIL):
    return api.post(LOGIN, {"email": email, "password": password}, format="json")


# ── csrf ────────────────────────────────────────────────────────────────────


def test_csrf_endpoint_is_public_and_returns_a_token(api):
    response = api.get(CSRF)

    assert response.status_code == 200
    assert isinstance(response.json()["csrfToken"], str) and response.json()["csrfToken"]


# ── login with MFA off (local development and tests only) ──────────────────


def test_login_succeeds_and_says_no_mfa_was_required(api, operator_user, mfa_off):
    response = login(api)

    assert response.status_code == 200
    assert response.json() == {
        "id": operator_user.pk,
        "email": EMAIL,
        "name": "Abubakar",
        "role": "operator",
        "role_label": "Operator",
        "mfa_enabled": False,
        "mfa_required": False,
    }
    assert api.get(SESSION).status_code == 200


def test_email_is_matched_case_insensitively(api, operator_user, mfa_off):
    assert login(api, email=EMAIL.upper()).status_code == 200


def test_wrong_password_is_401_and_creates_no_session(api, operator_user, mfa_off):
    response = login(api, password="wrong-password-123")

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert api.get(SESSION).status_code == 401


def test_unknown_email_gets_the_same_answer_as_a_wrong_password(api, operator_user, mfa_off):
    unknown = login(api, email="nobody@pureplay.example")
    wrong = login(api, password="wrong-password-123")

    assert (unknown.status_code, unknown.json()) == (wrong.status_code, wrong.json())


def test_an_inactive_operator_cannot_log_in(api, operator_user, mfa_off):
    operator_user.is_active = False
    operator_user.save(update_fields=["is_active"])

    assert login(api).status_code == 401


def test_login_is_rate_limited_per_email(api, operator_user, mfa_off, settings):
    settings.OPERATOR_LOGIN_MAX_ATTEMPTS = 2
    login(api, password="wrong-1")
    login(api, password="wrong-2")

    # Even the right password is refused once the bucket is full.
    response = login(api)

    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


# ── the MFA guard ───────────────────────────────────────────────────────────


def test_with_mfa_required_a_correct_password_only_starts_the_challenge(
    api, operator_user, mfa_on
):
    """It used to refuse outright, because there was no second factor to ask
    for. Now the password buys a pending state and nothing else — an account
    with no authenticator is sent to enrol, which is what keeps "require MFA"
    from deadlocking a system where nobody has enrolled."""
    response = login(api)

    assert response.status_code == 200
    body = response.json()
    assert body["mfa_required"] is True
    assert body["code"] == "mfa_setup_required"
    # Still nothing that resembles a session.
    assert "id" not in body and "email" not in body


def test_with_mfa_required_no_session_is_created(api, operator_user, mfa_on):
    login(api)

    assert "_auth_user_id" not in api.session
    assert api.get(SESSION).status_code == 401


def test_with_mfa_required_a_wrong_password_is_still_just_invalid_credentials(
    api, operator_user, mfa_on
):
    """The password is checked first, so a 403 always means "right password"."""
    response = login(api, password="wrong-password-123")

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


# ── session and logout ──────────────────────────────────────────────────────


def test_session_is_401_when_signed_out(api):
    response = api.get(SESSION)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_session_reports_the_signed_in_operator(api, operator_user, mfa_off):
    login(api)

    response = api.get(SESSION)

    assert response.status_code == 200
    assert response.json()["email"] == EMAIL
    assert "mfa_required" not in response.json()


def test_logout_ends_the_session(api, operator_user, mfa_off):
    login(api)

    assert api.post(LOGOUT).status_code == 204
    assert api.get(SESSION).status_code == 401


def test_logout_needs_a_session(api):
    assert api.post(LOGOUT).status_code == 401

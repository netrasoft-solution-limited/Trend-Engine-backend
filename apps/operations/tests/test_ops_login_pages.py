"""Operator sign-in in a browser.

The JSON flow is covered in test_operator_mfa.py. What is covered here is that
the browser flow enforces the SAME things — a password never yields a session,
enrolment needs the pending state, codes are rate-limited — because the two
share `apps.operations.mfa` and a divergence would mean one door is weaker.

The reason these views exist at all is also worth a test: `LOGIN_URL` pointed
at `/login`, which was not a route, so every operator screen redirected to a
404 and the provider-credentials page could not be reached in a browser.
"""
from __future__ import annotations

import pyotp
import pytest
from django.urls import reverse

from apps.operations import mfa
from apps.operations.models import OperatorUser

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery-9"
EMAIL = "abubakar@pureplay.example"


@pytest.fixture(autouse=True)
def _ops_plane(settings):
    assert settings.ROOT_URLCONF == "config.urls_ops", "Run these with the ops settings"
    settings.OPERATOR_REQUIRE_MFA = True
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ops-login-pages",
        }
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def operator(db):
    return OperatorUser.objects.create_user(
        email=EMAIL, password=PASSWORD, name="Abubakar",
        role=OperatorUser.Role.PLATFORM_ADMIN,
    )


@pytest.fixture
def enrolled(operator):
    enrolment = mfa.begin_enrolment(operator)
    mfa.confirm_enrolment(operator, pyotp.TOTP(enrolment.secret).now())
    # Confirming spends the current step; a real next sign-in is minutes later.
    operator.totp_last_counter = 0
    operator.save(update_fields=["totp_last_counter"])
    return operator, enrolment.secret


def sign_in(client, password=PASSWORD):
    return client.post(reverse("ops-login"), {"email": EMAIL, "password": password})


# ── The gap these views close ───────────────────────────────────────────────


def test_login_url_actually_resolves(settings):
    """It pointed at a 404, so LoginRequiredMiddleware sent every operator
    screen nowhere."""
    from django.urls import resolve

    assert resolve(settings.LOGIN_URL).view_name == "ops-login"


def test_a_signed_out_visitor_to_a_protected_screen_lands_on_the_login_page(client):
    response = client.get(reverse("ops-providers"), follow=True)

    assert response.status_code == 200
    assert b"Operator sign in" in response.content


# ── A password alone is never enough ────────────────────────────────────────


def test_the_login_page_renders(client):
    response = client.get(reverse("ops-login"))

    assert response.status_code == 200
    assert b"Operator sign in" in response.content


def test_a_correct_password_creates_no_session(client, enrolled):
    sign_in(client)

    assert "_auth_user_id" not in client.session
    assert client.get(reverse("ops-providers")).status_code in (302, 403)


def test_an_enrolled_account_is_sent_to_the_challenge(client, enrolled):
    assert sign_in(client)["Location"] == reverse("ops-mfa")


def test_an_unenrolled_account_is_sent_to_enrol(client, operator):
    assert sign_in(client)["Location"] == reverse("ops-mfa-setup")


def test_a_wrong_password_says_so_without_saying_which_half(client, enrolled):
    response = sign_in(client, password="wrong-password-123")

    assert response.status_code == 401
    assert b"do not match" in response.content


def test_the_password_step_is_rate_limited(client, enrolled, settings):
    settings.OPERATOR_LOGIN_MAX_ATTEMPTS = 2
    sign_in(client, password="wrong-1")
    sign_in(client, password="wrong-2")

    assert sign_in(client).status_code == 429


# ── The challenge ───────────────────────────────────────────────────────────


def test_the_full_flow_signs_the_operator_in(client, enrolled):
    _, secret = enrolled
    sign_in(client)

    response = client.post(reverse("ops-mfa"), {"code": pyotp.TOTP(secret).now()})

    # The client list, not the provider screen: an operator lands where the
    # work starts, and nothing can be scored or delivered for a client the
    # system has no profile for.
    assert response["Location"] == reverse("ops-clients")
    assert "_auth_user_id" in client.session


def test_the_challenge_needs_the_password_first(client, enrolled):
    assert client.get(reverse("ops-mfa"))["Location"] == reverse("ops-login")


def test_a_wrong_code_leaves_the_operator_signed_out(client, enrolled):
    sign_in(client)

    response = client.post(reverse("ops-mfa"), {"code": "000000"})

    assert response.status_code == 401
    assert "_auth_user_id" not in client.session


def test_codes_are_rate_limited_and_the_pending_state_is_discarded(
    client, enrolled, settings
):
    settings.OPERATOR_MFA_MAX_ATTEMPTS = 1
    _, secret = enrolled
    sign_in(client)

    client.post(reverse("ops-mfa"), {"code": "000000"})
    response = client.post(reverse("ops-mfa"), {"code": pyotp.TOTP(secret).now()})

    assert response.status_code == 429
    # The password must be entered again, so the ceiling is not a speed bump.
    assert client.get(reverse("ops-mfa"))["Location"] == reverse("ops-login")


# ── Enrolment ───────────────────────────────────────────────────────────────


def test_enrolment_shows_a_scannable_qr_and_the_key(client, operator):
    sign_in(client)

    response = client.get(reverse("ops-mfa-setup"))

    assert response.status_code == 200
    assert b"<svg" in response.content, "the QR is rendered inline, not served"
    assert b"otpauth://totp/" in response.content


def test_enrolment_needs_the_password_first(client, operator):
    """Otherwise it hands secrets to anonymous callers."""
    assert client.get(reverse("ops-mfa-setup"))["Location"] == reverse("ops-login")


def test_confirming_signs_in_and_shows_the_recovery_codes_once(client, operator):
    sign_in(client)
    client.get(reverse("ops-mfa-setup"))
    operator.refresh_from_db()
    from apps.operations import totp

    secret = totp.decrypt_secret(operator.totp_secret)

    response = client.post(reverse("ops-mfa-setup"), {"code": pyotp.TOTP(secret).now()})

    assert response.status_code == 200
    assert b"Save your recovery codes" in response.content
    assert "_auth_user_id" in client.session
    operator.refresh_from_db()
    assert operator.mfa_ready


def test_a_mistyped_code_does_not_invalidate_the_scanned_secret(client, operator):
    """Issuing a new secret because someone fat-fingered six digits would mean
    re-scanning, every time."""
    sign_in(client)
    client.get(reverse("ops-mfa-setup"))
    operator.refresh_from_db()
    from apps.operations import totp

    before = totp.decrypt_secret(operator.totp_secret)

    response = client.post(reverse("ops-mfa-setup"), {"code": "000000"})
    operator.refresh_from_db()

    assert response.status_code == 400
    assert totp.decrypt_secret(operator.totp_secret) == before
    assert not operator.mfa_ready


def test_an_enrolled_account_cannot_re_enrol_in_a_browser(client, enrolled):
    """Anyone with the password would otherwise replace a working
    authenticator, which is most of what the factor is for."""
    sign_in(client)

    assert client.get(reverse("ops-mfa-setup"))["Location"] == reverse("ops-mfa")


# ── The screen this unblocked ───────────────────────────────────────────────


def test_a_signed_in_platform_admin_reaches_the_credentials_screen(client, enrolled):
    operator, secret = enrolled
    sign_in(client)
    client.post(reverse("ops-mfa"), {"code": pyotp.TOTP(secret).now()})

    response = client.get(reverse("ops-providers"))

    assert response.status_code == 200
    assert b"Provider credentials" in response.content

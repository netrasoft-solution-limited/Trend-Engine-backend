"""The operator second factor, end to end.

Until this existed, `OPERATOR_REQUIRE_MFA` made ops login refuse everyone in
production — so the whole operator plane was unreachable on a real deployment.
These tests are what say it is reachable AND still closed.

The four properties worth the most, in order:

  · a password alone never produces a session;
  · a code cannot be replayed inside its 30-second step;
  · a lost authenticator is recoverable, once per code;
  · the enrolment endpoints are not an open door — they need a correct
    password first, and they refuse an account that is already enrolled.
"""
from __future__ import annotations

import time

import pyotp
import pytest
from rest_framework.test import APIClient

from apps.operations import mfa, totp
from apps.operations.models import AuditEvent, OperatorUser, RecoveryCode

pytestmark = pytest.mark.django_db

LOGIN = "/api/auth/login"
SESSION = "/api/auth/session"
SETUP = "/api/auth/mfa/setup"
CONFIRM = "/api/auth/mfa/confirm"
VERIFY = "/api/auth/mfa/verify"

EMAIL = "abubakar@pureplay.example"
PASSWORD = "correct-horse-battery-9"


@pytest.fixture(autouse=True)
def _operator_plane(settings):
    assert settings.ROOT_URLCONF == "config.urls_ops", "Run these with the ops settings"
    settings.OPERATOR_REQUIRE_MFA = True
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ops-mfa-tests",
        }
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def operator(db):
    return OperatorUser.objects.create_user(
        email=EMAIL, password=PASSWORD, name="Abubakar", role=OperatorUser.Role.OPERATOR
    )


@pytest.fixture
def enrolled(operator):
    """An operator with a confirmed authenticator, and the secret to drive it.

    Confirming consumes the current 30-second step — that is the replay guard
    doing its job. In life the next sign-in is minutes later, so the counter is
    wound back to represent an account enrolled at some point in the past
    rather than one enrolled microseconds ago. The replay tests below set the
    counter themselves by signing in, so nothing here weakens them.
    """
    enrolment = mfa.begin_enrolment(operator)
    mfa.confirm_enrolment(operator, pyotp.TOTP(enrolment.secret).now())
    operator.totp_last_counter = 0
    operator.save(update_fields=["totp_last_counter"])
    operator.refresh_from_db()
    return operator, enrolment.secret


def login(api, password=PASSWORD, email=EMAIL):
    return api.post(LOGIN, {"email": email, "password": password}, format="json")


def code_for(secret: str, *, at: float | None = None) -> str:
    return pyotp.TOTP(secret).at(at if at is not None else time.time())


# ── A password alone is never enough ────────────────────────────────────────


def test_a_correct_password_creates_no_session(api, enrolled):
    login(api)

    assert "_auth_user_id" not in api.session
    assert api.get(SESSION).status_code == 401


def test_an_enrolled_account_is_sent_to_the_challenge(api, enrolled):
    body = login(api).json()

    assert body["code"] == "mfa_challenge"
    assert body["mfa_required"] is True


def test_the_full_flow_signs_the_operator_in(api, enrolled):
    operator, secret = enrolled
    login(api)

    response = api.post(VERIFY, {"code": code_for(secret)}, format="json")

    assert response.status_code == 200
    assert response.json()["email"] == EMAIL
    assert api.get(SESSION).status_code == 200


def test_a_wrong_code_leaves_the_operator_signed_out(api, enrolled):
    login(api)

    response = api.post(VERIFY, {"code": "000000"}, format="json")

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_code"
    assert api.get(SESSION).status_code == 401


def test_the_challenge_cannot_be_reached_without_the_password(api, enrolled):
    """The pending state is the only authority these endpoints accept."""
    _, secret = enrolled

    response = api.post(VERIFY, {"code": code_for(secret)}, format="json")

    assert response.status_code == 401
    assert response.json()["code"] == "no_pending_login"


def test_a_wrong_password_never_reaches_the_challenge(api, enrolled):
    _, secret = enrolled
    login(api, password="wrong-password-123")

    assert api.post(VERIFY, {"code": code_for(secret)}, format="json").status_code == 401


def test_the_pending_state_expires(api, enrolled, monkeypatch):
    """An unattended browser on a shared machine is not an open door."""
    _, secret = enrolled
    login(api)

    monkeypatch.setattr(mfa, "PENDING_SECONDS", -1)
    response = api.post(VERIFY, {"code": code_for(secret)}, format="json")

    assert response.status_code == 401
    assert response.json()["code"] == "no_pending_login"


# ── Replay ──────────────────────────────────────────────────────────────────


def test_a_code_cannot_be_used_twice(api, enrolled):
    """A TOTP code is valid for its whole 30-second step, so one seen over a
    shoulder works again until that step ends — unless the counter is kept."""
    operator, secret = enrolled
    code = code_for(secret)

    login(api)
    assert api.post(VERIFY, {"code": code}, format="json").status_code == 200

    second = APIClient()
    login(second)
    response = second.post(VERIFY, {"code": code}, format="json")

    assert response.status_code == 401
    assert second.get(SESSION).status_code == 401


def test_a_replayed_code_is_indistinguishable_from_a_wrong_one(api, enrolled):
    """Telling them apart would tell an attacker their guess was right."""
    _, secret = enrolled
    code = code_for(secret)
    login(api)
    api.post(VERIFY, {"code": code}, format="json")

    replay_client, wrong_client = APIClient(), APIClient()
    login(replay_client)
    login(wrong_client)
    replay = replay_client.post(VERIFY, {"code": code}, format="json")
    wrong = wrong_client.post(VERIFY, {"code": "000000"}, format="json")

    assert (replay.status_code, replay.json()) == (wrong.status_code, wrong.json())


def test_the_next_step_still_works_after_one_is_used(enrolled):
    """The guard must reject replays, not lock the account out."""
    operator, secret = enrolled
    later = time.time() + totp.STEP_SECONDS

    assert mfa.verify_code(operator, code_for(secret, at=later))


# ── Enrolment ───────────────────────────────────────────────────────────────


def test_an_unenrolled_account_is_sent_to_set_up(api, operator):
    """The bootstrap: requiring MFA while nobody has enrolled would otherwise
    be a deadlock."""
    body = login(api).json()

    assert body["code"] == "mfa_setup_required"


def test_setup_hands_back_a_provisioning_uri(api, operator):
    login(api)

    body = api.get(SETUP).json()

    assert body["secret"]
    assert body["provisioning_uri"].startswith("otpauth://totp/")
    assert "Trend%20Engine" in body["provisioning_uri"]


def test_setup_needs_a_correct_password_first(api, operator):
    """Otherwise it hands secrets to anonymous callers."""
    assert api.get(SETUP).status_code == 401


def test_confirming_enrols_signs_in_and_returns_recovery_codes(api, operator):
    login(api)
    secret = api.get(SETUP).json()["secret"]

    response = api.post(CONFIRM, {"code": code_for(secret)}, format="json")

    assert response.status_code == 200
    body = response.json()
    assert len(body["recovery_codes"]) == totp.RECOVERY_CODE_COUNT
    assert api.get(SESSION).status_code == 200

    operator.refresh_from_db()
    assert operator.mfa_enabled and operator.totp_confirmed_at


def test_a_wrong_code_does_not_enrol(api, operator):
    login(api)
    api.get(SETUP)

    response = api.post(CONFIRM, {"code": "000000"}, format="json")

    assert response.status_code == 400
    operator.refresh_from_db()
    assert not operator.mfa_enabled, "a secret nobody proved they hold is not a factor"
    assert api.get(SESSION).status_code == 401


def test_an_enrolled_account_cannot_re_enrol_from_the_web(api, enrolled):
    """Otherwise anyone with the password replaces a working authenticator,
    which is most of what the factor is for. Resetting is SSH-only."""
    login(api)

    response = api.get(SETUP)

    assert response.status_code == 409
    assert response.json()["code"] == "already_enrolled"


def test_the_secret_is_not_stored_in_plaintext(operator):
    """A dump would otherwise let whoever holds it generate valid codes
    forever, while the account looked entirely normal."""
    enrolment = mfa.begin_enrolment(operator)
    operator.refresh_from_db()

    assert enrolment.secret not in operator.totp_secret
    assert totp.decrypt_secret(operator.totp_secret) == enrolment.secret


def test_enrolment_is_audited(api, operator):
    login(api)
    secret = api.get(SETUP).json()["secret"]
    api.post(CONFIRM, {"code": code_for(secret)}, format="json")

    assert AuditEvent.objects.filter(
        kind=AuditEvent.Kind.CONFIG, message__icontains="enrolled a second factor"
    ).exists()


# ── Recovery ────────────────────────────────────────────────────────────────


def test_a_recovery_code_completes_the_challenge(api, operator):
    login(api)
    secret = api.get(SETUP).json()["secret"]
    codes = api.post(CONFIRM, {"code": code_for(secret)}, format="json").json()[
        "recovery_codes"
    ]

    fresh = APIClient()
    login(fresh)
    response = fresh.post(VERIFY, {"code": codes[0]}, format="json")

    assert response.status_code == 200
    assert fresh.get(SESSION).status_code == 200


def test_a_recovery_code_works_once(api, operator):
    login(api)
    secret = api.get(SETUP).json()["secret"]
    codes = api.post(CONFIRM, {"code": code_for(secret)}, format="json").json()[
        "recovery_codes"
    ]

    first, second = APIClient(), APIClient()
    login(first)
    first.post(VERIFY, {"code": codes[0]}, format="json")
    login(second)
    response = second.post(VERIFY, {"code": codes[0]}, format="json")

    assert response.status_code == 401
    assert second.get(SESSION).status_code == 401


def test_recovery_codes_are_stored_hashed(operator):
    """They are credentials. Storing one reversibly would mean the database
    held a second usable way into the account."""
    enrolment = mfa.begin_enrolment(operator)
    codes = mfa.confirm_enrolment(operator, pyotp.TOTP(enrolment.secret).now())

    stored = set(RecoveryCode.objects.values_list("code_hash", flat=True))
    for code in codes:
        assert code not in stored
        assert totp.normalise_recovery_code(code) not in stored


def test_recovery_codes_are_formatted_to_be_read_aloud(operator):
    """No I, L, O or U — a code read off a screen and typed on a phone should
    not fail on a character nobody can tell apart."""
    for code in totp.new_recovery_codes():
        assert set("ILOU").isdisjoint(set(code))


# ── Reset ───────────────────────────────────────────────────────────────────


def test_reset_clears_the_factor_and_its_recovery_codes(operator):
    enrolment = mfa.begin_enrolment(operator)
    mfa.confirm_enrolment(operator, pyotp.TOTP(enrolment.secret).now())

    mfa.reset(operator, actor_label="abubakar (shell)")
    operator.refresh_from_db()

    assert not operator.mfa_ready
    assert operator.totp_secret == ""
    assert not RecoveryCode.objects.filter(operator_id=operator.pk).exists()
    assert AuditEvent.objects.filter(message__icontains="reset by").exists()


def test_there_is_no_web_route_that_resets_a_factor(api, enrolled):
    """It turns the account back into password-only until the next sign-in, so
    it is deliberately not reachable from a browser."""
    from django.urls import get_resolver

    patterns = str(get_resolver("config.urls_ops").url_patterns)
    assert "reset" not in patterns.lower()


# ── Rate limiting ───────────────────────────────────────────────────────────


def test_codes_are_rate_limited(api, enrolled, settings):
    """Six digits alone against unlimited guesses is not a second factor."""
    settings.OPERATOR_MFA_MAX_ATTEMPTS = 2
    _, secret = enrolled
    login(api)

    api.post(VERIFY, {"code": "000000"}, format="json")
    api.post(VERIFY, {"code": "000001"}, format="json")
    response = api.post(VERIFY, {"code": code_for(secret)}, format="json")

    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


def test_hitting_the_limit_discards_the_pending_state(api, enrolled, settings):
    """The password has to be entered again, so the ceiling is not a speed
    bump someone waits out with the session still half-open."""
    settings.OPERATOR_MFA_MAX_ATTEMPTS = 1
    _, secret = enrolled
    login(api)

    api.post(VERIFY, {"code": "000000"}, format="json")
    api.post(VERIFY, {"code": "000001"}, format="json")

    assert api.post(VERIFY, {"code": code_for(secret)}, format="json").json()[
        "code"
    ] == "no_pending_login"

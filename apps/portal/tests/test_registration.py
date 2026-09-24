"""Self-service organisation registration — register, verify, resend.

Runs under the PORTAL settings, and refuses to run under any other:

    pytest apps/portal/tests/test_registration.py --ds=config.settings.portal

Under the operator settings the /portal/ URLconf is not loaded, so every
request here would 404 — and the flag-off test would pass for the wrong reason.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.portal.models import (
    EmailVerificationToken,
    OrgMembership,
    OrgRole,
    OrgUser,
    PortalLoginEvent,
)
from apps.tenancy.models import Organization

pytestmark = pytest.mark.django_db

REGISTER = "/portal/api/auth/register"
VERIFY = "/portal/api/auth/verify"
RESEND = "/portal/api/auth/verify/resend"
LOGIN = "/portal/api/auth/login"
SESSION = "/portal/api/auth/session"

EMAIL = "ada@acme.example"
PASSWORD = "correct-horse-battery-9"


@pytest.fixture(autouse=True)
def _portal_plane(settings):
    assert settings.ROOT_URLCONF == "config.urls_portal", (
        "Run these tests with --ds=config.settings.portal"
    )
    settings.PORTAL_ALLOW_SELF_SIGNUP = True
    # Isolated from Redis, and from every other test's throttle buckets.
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "portal-registration-tests",
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
def post(api, django_capture_on_commit_callbacks):
    """POST as JSON, running on-commit callbacks so emails actually send."""

    def _post(path, payload, client=None):
        with django_capture_on_commit_callbacks(execute=True):
            return (client or api).post(path, payload, format="json")

    return _post


def register(post, **overrides):
    payload = {
        "organization_name": "Acme Botanicals",
        "name": "Ada Admin",
        "email": EMAIL,
        "password": PASSWORD,
        **overrides,
    }
    return post(REGISTER, payload)


def token_in(message) -> str:
    match = re.search(r"/portal/verify-email/([A-Za-z0-9_-]+)", message.body)
    assert match, f"no verification link in: {message.body!r}"
    return match.group(1)


@pytest.fixture
def registered(post, mailoutbox):
    """A registration, and the raw token from its email."""
    response = register(post)
    assert response.status_code == 202
    assert len(mailoutbox) == 1
    return token_in(mailoutbox[0])


# ── register ────────────────────────────────────────────────────────────────


def test_register_creates_an_onboarding_org_with_a_pending_admin(post, mailoutbox):
    response = register(post)

    assert response.status_code == 202
    org = Organization.objects.get(name="Acme Botanicals")
    assert org.status == Organization.Status.ONBOARDING
    assert org.slug == "acme-botanicals"

    membership = OrgMembership.objects.get(organization=org)
    assert membership.org_user.email == EMAIL
    assert membership.role == OrgRole.ADMIN
    assert membership.status == OrgMembership.Status.PENDING_VERIFICATION

    assert [m.to for m in mailoutbox] == [[EMAIL]]
    raw = token_in(mailoutbox[0])
    stored = EmailVerificationToken.objects.get(membership=membership)
    assert stored.token_hash != raw, "the raw token must never be stored"
    assert stored.expires_at - timezone.now() <= timedelta(hours=24)

    assert PortalLoginEvent.objects.filter(
        email_attempted=EMAIL, outcome=PortalLoginEvent.Outcome.REGISTERED
    ).exists()


def test_role_is_always_admin_whatever_the_request_says(post):
    register(post, role=OrgRole.VIEWER)
    assert OrgMembership.objects.get(org_user__email=EMAIL).role == OrgRole.ADMIN


def test_slugs_stay_unique_for_the_same_name(post):
    register(post)
    register(post, email="bob@other.example")
    assert sorted(Organization.objects.values_list("slug", flat=True)) == [
        "acme-botanicals",
        "acme-botanicals-2",
    ]


def test_weak_password_is_refused_before_anything_is_created(post):
    response = register(post, password="short")
    assert response.status_code == 400
    assert "password" in response.json()
    assert not Organization.objects.exists()


# ── the account-enumeration oracle ──────────────────────────────────────────


def test_duplicate_email_gets_the_identical_response_and_creates_nothing(post, mailoutbox):
    fresh = register(post)
    orgs, users, mails = Organization.objects.count(), OrgUser.objects.count(), len(mailoutbox)

    duplicate = register(
        post, organization_name="Someone Else Ltd", email=EMAIL.upper(), password="another-pass-4567"
    )

    assert duplicate.status_code == fresh.status_code
    assert duplicate.json() == fresh.json()
    assert Organization.objects.count() == orgs
    assert OrgUser.objects.count() == users
    assert len(mailoutbox) == mails


def test_existing_invited_account_gets_the_identical_response(post, mailoutbox):
    OrgUser.objects.create_user(email="invitee@acme.example", password=None, name="Invitee")
    fresh = register(post)

    existing = register(post, email="invitee@acme.example", organization_name="Takeover Inc")

    assert (existing.status_code, existing.json()) == (fresh.status_code, fresh.json())
    assert not Organization.objects.filter(name="Takeover Inc").exists()
    assert len(mailoutbox) == 1


# ── login before and after verification ─────────────────────────────────────


def test_unverified_user_cannot_log_in(api, post, registered):
    response = post(LOGIN, {"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 403
    assert response.json()["code"] == "email_not_verified"
    assert "_auth_user_id" not in api.session
    assert api.get(SESSION).status_code == 401


def test_verify_activates_the_org_and_membership_without_logging_in(api, post, registered):
    response = post(VERIFY, {"token": registered})

    assert response.status_code == 200
    membership = OrgMembership.objects.select_related("organization").get(org_user__email=EMAIL)
    assert membership.status == OrgMembership.Status.ACTIVE
    assert membership.accepted_at is not None
    assert membership.organization.status == Organization.Status.ACTIVE
    assert EmailVerificationToken.objects.get(membership=membership).used_at is not None
    assert PortalLoginEvent.objects.filter(outcome=PortalLoginEvent.Outcome.VERIFIED).exists()

    # Verification is not a login.
    assert api.get(SESSION).status_code == 401

    login = post(LOGIN, {"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["role"] == OrgRole.ADMIN


def test_a_used_token_cannot_be_reused(post, registered):
    assert post(VERIFY, {"token": registered}).status_code == 200

    again = post(VERIFY, {"token": registered})

    assert again.status_code == 400
    assert again.json()["code"] == "invalid_token"
    assert PortalLoginEvent.objects.filter(outcome=PortalLoginEvent.Outcome.VERIFY_FAILED).exists()


def test_an_expired_token_fails_and_activates_nothing(post, registered):
    EmailVerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    response = post(VERIFY, {"token": registered})

    assert response.status_code == 400
    membership = OrgMembership.objects.select_related("organization").get(org_user__email=EMAIL)
    assert membership.status == OrgMembership.Status.PENDING_VERIFICATION
    assert membership.organization.status == Organization.Status.ONBOARDING


def test_an_unknown_token_fails(post):
    response = post(VERIFY, {"token": "not-a-real-token"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_token"


# ── resend ──────────────────────────────────────────────────────────────────


def test_resend_invalidates_the_earlier_link(post, registered, mailoutbox):
    response = post(RESEND, {"email": EMAIL})

    assert response.status_code == 202
    assert len(mailoutbox) == 2
    fresh = token_in(mailoutbox[1])
    assert fresh != registered

    assert post(VERIFY, {"token": registered}).status_code == 400
    assert post(VERIFY, {"token": fresh}).status_code == 200


def test_resend_answers_identically_for_an_unknown_address(post, registered, mailoutbox):
    known = post(RESEND, {"email": EMAIL})
    unknown = post(RESEND, {"email": "nobody@nowhere.example"})

    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
    assert len(mailoutbox) == 2  # the registration email + one resend, nothing for "nobody"


# ── rate limits ─────────────────────────────────────────────────────────────


def test_register_is_rate_limited_per_email(post, settings):
    settings.PORTAL_SIGNUP_MAX_PER_EMAIL = 2
    register(post)
    register(post)

    response = register(post)

    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


def test_register_is_rate_limited_per_ip(post, settings):
    settings.PORTAL_SIGNUP_MAX_PER_IP = 2
    register(post, email="one@acme.example")
    register(post, email="two@acme.example")

    response = register(post, email="three@acme.example")

    assert response.status_code == 429
    assert not OrgUser.objects.filter(email="three@acme.example").exists()


def test_resend_is_rate_limited(post, registered, settings):
    settings.PORTAL_VERIFY_RESEND_MAX_PER_EMAIL = 1
    assert post(RESEND, {"email": EMAIL}).status_code == 202
    assert post(RESEND, {"email": EMAIL}).status_code == 429


# ── the flag ────────────────────────────────────────────────────────────────


def test_register_is_404_when_self_signup_is_off(post, settings):
    settings.PORTAL_ALLOW_SELF_SIGNUP = False

    response = register(post)

    assert response.status_code == 404
    assert not Organization.objects.exists()
    assert not OrgUser.objects.exists()

"""Self-service organisation registration — register, and be signed in.

Runs under the PORTAL settings, and refuses to run under any other:

    pytest apps/portal/tests/test_registration.py --ds=config.settings.portal

Under the operator settings the /portal/ URLconf is not loaded, so every
request here would 404 — and the flag-off test would pass for the wrong reason.
"""
from __future__ import annotations

import pytest
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

CSRF = "/portal/api/auth/csrf"
REGISTER = "/portal/api/auth/register"
LOGIN = "/portal/api/auth/login"
LOGOUT = "/portal/api/auth/logout"
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
    """POST as JSON, running on-commit callbacks so any email actually sends."""

    def _post(path, payload, client=None):
        with django_capture_on_commit_callbacks(execute=True):
            return (client or api).post(path, payload, format="json")

    return _post


def register(post, client=None, **overrides):
    payload = {
        "organization_name": "Acme Botanicals",
        "name": "Ada Admin",
        "email": EMAIL,
        "password": PASSWORD,
        **overrides,
    }
    return post(REGISTER, payload, client=client)


# ── register ────────────────────────────────────────────────────────────────


def test_register_creates_an_active_org_with_an_active_admin(post, mailoutbox):
    response = register(post)

    assert response.status_code == 201
    org = Organization.objects.get(name="Acme Botanicals")
    assert org.status == Organization.Status.ACTIVE
    assert org.slug == "acme-botanicals"

    membership = OrgMembership.objects.get(organization=org)
    assert membership.org_user.email == EMAIL
    assert membership.role == OrgRole.ADMIN
    assert membership.status == OrgMembership.Status.ACTIVE
    assert membership.accepted_at is not None

    # No verification step: nothing to email, no token to store.
    assert mailoutbox == []
    assert not EmailVerificationToken.objects.exists()

    assert PortalLoginEvent.objects.filter(
        email_attempted=EMAIL, outcome=PortalLoginEvent.Outcome.REGISTERED
    ).exists()


def test_register_signs_the_admin_in(api, post):
    response = register(post)

    body = response.json()
    assert body["email"] == EMAIL
    assert body["role"] == OrgRole.ADMIN
    assert body["organization"]["name"] == "Acme Botanicals"

    session = api.get(SESSION)
    assert session.status_code == 200
    assert session.json()["email"] == EMAIL
    assert PortalLoginEvent.objects.filter(
        email_attempted=EMAIL, outcome=PortalLoginEvent.Outcome.SUCCESS
    ).exists()


def test_a_registered_admin_can_log_out_and_back_in(api, post):
    register(post)
    assert post(LOGOUT, {}).status_code == 204
    assert api.get(SESSION).status_code == 401

    login = post(LOGIN, {"email": EMAIL, "password": PASSWORD})

    assert login.status_code == 200
    assert login.json()["role"] == OrgRole.ADMIN


def test_role_is_always_admin_whatever_the_request_says(post):
    register(post, role=OrgRole.VIEWER)
    assert OrgMembership.objects.get(org_user__email=EMAIL).role == OrgRole.ADMIN


def test_slugs_stay_unique_for_the_same_name(post):
    register(post)
    register(post, client=APIClient(), email="bob@other.example")
    assert sorted(Organization.objects.values_list("slug", flat=True)) == [
        "acme-botanicals",
        "acme-botanicals-2",
    ]


def _email_of_length(n: int) -> str:
    """A syntactically valid address exactly `n` characters long (n >= 72).

    EmailValidator caps each domain label at 63 characters, so the length is
    spread over 60-character labels rather than one long one.
    """
    local = "a" * 64
    tail = ".example"
    middle_len = n - len(local) - 1 - len(tail)  # the 1 is the "@"
    labels = []
    while middle_len > 0:
        size = min(60, middle_len)
        labels.append("b" * size)
        middle_len -= size + 1  # +1 for the dot that follows each label
    address = f"{local}@{'.'.join(labels)}{tail}"
    assert len(address) == n, (len(address), n)
    return address


def test_email_over_254_characters_is_a_400_not_a_500(post):
    """OrgUser.email is varchar(254); EmailValidator alone admits up to 320."""
    response = register(post, email=_email_of_length(255))

    assert response.status_code == 400
    assert response.json() == {"email": ["Ensure this field has no more than 254 characters."]}
    assert not OrgUser.objects.exists()
    assert not Organization.objects.exists()


def test_email_of_exactly_254_characters_registers(post):
    address = _email_of_length(254)

    response = register(post, email=address)

    assert response.status_code == 201
    assert OrgUser.objects.filter(email=address).exists()


def test_weak_password_is_refused_before_anything_is_created(post):
    response = register(post, password="short")
    assert response.status_code == 400
    assert "password" in response.json()
    assert not Organization.objects.exists()


# ── an address that already has an account ──────────────────────────────────


def test_duplicate_email_is_refused_and_creates_nothing(post):
    register(post)
    orgs, users = Organization.objects.count(), OrgUser.objects.count()

    duplicate = register(
        post,
        client=APIClient(),
        organization_name="Someone Else Ltd",
        email=EMAIL.upper(),
        password="another-pass-4567",
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "email_taken"
    assert Organization.objects.count() == orgs
    assert OrgUser.objects.count() == users


def test_duplicate_email_does_not_sign_anyone_in(post):
    register(post)
    other = APIClient()

    register(post, client=other, organization_name="Someone Else Ltd")

    assert other.get(SESSION).status_code == 401


def test_existing_invited_account_cannot_be_taken_over(post):
    OrgUser.objects.create_user(email="invitee@acme.example", password=None, name="Invitee")

    response = register(post, email="invitee@acme.example", organization_name="Takeover Inc")

    assert response.status_code == 409
    assert not Organization.objects.filter(name="Takeover Inc").exists()


# ── the verification endpoints are gone ─────────────────────────────────────


@pytest.mark.parametrize("path", ["/portal/api/auth/verify", "/portal/api/auth/verify/resend"])
def test_verification_endpoints_no_longer_exist(post, path):
    assert post(path, {"token": "anything", "email": EMAIL}).status_code == 404


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
    register(post, client=APIClient(), email="two@acme.example")

    response = register(post, client=APIClient(), email="three@acme.example")

    assert response.status_code == 429
    assert not OrgUser.objects.filter(email="three@acme.example").exists()


# ── the flag ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [True, False])
def test_csrf_endpoint_reports_whether_self_signup_is_enabled(api, settings, enabled):
    settings.PORTAL_ALLOW_SELF_SIGNUP = enabled

    response = api.get(CSRF)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"csrfToken", "selfSignupEnabled"}
    assert body["selfSignupEnabled"] is enabled
    assert isinstance(body["csrfToken"], str) and body["csrfToken"]


def test_register_is_404_when_self_signup_is_off(post, settings):
    settings.PORTAL_ALLOW_SELF_SIGNUP = False

    response = register(post)

    assert response.status_code == 404
    assert not Organization.objects.exists()
    assert not OrgUser.objects.exists()

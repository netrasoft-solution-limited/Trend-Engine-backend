"""The operator screen where vendor keys are entered.

The test that matters most is the boring one: that a stored secret never
appears in the rendered page. Everything else here protects that — the role
check, the audit trail, the refusal of a half-entered pair.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.operations.models import AuditEvent, OperatorUser
from apps.sources.models import AcquisitionProvider

pytestmark = pytest.mark.django_db

#: Shaped like an Apify token, and deliberately not one. A real key in a
#: test fixture is a key in the repository, in every clone of it, and in
#: the history after it is "removed".
SECRET = "apify_api_EXAMPLEONLYnotarealkey0000000000"


@pytest.fixture
def platform_admin(db):
    return OperatorUser.objects.create_user(
        email="admin@pureplay.test",
        password="not-a-real-password",
        name="Platform Admin",
        role=OperatorUser.Role.PLATFORM_ADMIN,
    )


@pytest.fixture
def plain_operator(db):
    return OperatorUser.objects.create_user(
        email="operator@pureplay.test",
        password="not-a-real-password",
        name="Operator",
        role=OperatorUser.Role.OPERATOR,
    )


@pytest.fixture
def apify(db):
    return AcquisitionProvider.objects.create(
        kind=AcquisitionProvider.Kind.APIFY, name="Apify"
    )


# ── Authorisation ───────────────────────────────────────────────────────────


def test_a_plain_operator_cannot_reach_the_screen(client, plain_operator, apify):
    """PRD §3.2 keeps Operator and Platform Admin distinct even where one
    person holds both. Reading a provider's caps is an operator task; changing
    the key that spends against them is not."""
    client.force_login(plain_operator)

    assert client.get(reverse("ops-providers")).status_code == 403


def test_a_plain_operator_cannot_post_a_key_either(client, plain_operator, apify):
    """The screen being hidden is not the control; the check on the write is."""
    client.force_login(plain_operator)

    response = client.post(
        reverse("ops-provider-credentials", args=[apify.pk]), {"api_key": SECRET}
    )

    assert response.status_code == 403
    apify.refresh_from_db()
    assert not apify.has_credential


def test_an_anonymous_visitor_is_sent_to_login(client, apify):
    response = client.get(reverse("ops-providers"))
    assert response.status_code in (302, 403)


# ── The screen ──────────────────────────────────────────────────────────────


def test_the_screen_lists_providers_and_where_the_key_would_come_from(
    client, platform_admin, apify
):
    client.force_login(platform_admin)

    body = client.get(reverse("ops-providers")).content.decode()

    assert "Apify" in body
    assert "APIFY_TOKEN" in body, "an unset provider names its environment fallback"


def test_a_stored_secret_is_never_rendered(client, platform_admin, apify):
    """The one that would matter if it broke.

    Round-tripping a secret through a form is how it ends up in a browser
    cache, a screenshot, or a bug report.
    """
    apify.set_credentials({"api_key": SECRET}, actor_label="someone@test")
    apify.save()
    client.force_login(platform_admin)

    body = client.get(reverse("ops-providers")).content.decode()

    assert SECRET not in body
    assert SECRET[:20] not in body
    # Only the masked hint, which is the last four characters.
    assert f"····{SECRET[-4:]}" in body


# ── Setting a key ───────────────────────────────────────────────────────────


def test_posting_a_key_stores_it_encrypted(client, platform_admin, apify):
    client.force_login(platform_admin)

    client.post(reverse("ops-provider-credentials", args=[apify.pk]), {"api_key": SECRET})

    apify.refresh_from_db()
    assert apify.credential("api_key") == SECRET
    assert SECRET not in apify.credential_ciphertext
    assert apify.credential_updated_by_label == "admin@pureplay.test"


def test_setting_a_key_is_audited_without_the_value(client, platform_admin, apify):
    """PRD §7.1. A credential change is the configuration event most worth
    having — it is what explains why a connector started or stopped working."""
    client.force_login(platform_admin)

    client.post(reverse("ops-provider-credentials", args=[apify.pk]), {"api_key": SECRET})

    event = AuditEvent.objects.filter(kind=AuditEvent.Kind.CONFIG).latest("at")
    assert event.actor_label == "admin@pureplay.test"
    assert "credentials set" in event.message
    assert SECRET not in str(event.context)
    assert event.context["secrets"] == ["api_key"]


def test_submitting_nothing_leaves_the_stored_key_alone(client, platform_admin, apify):
    """Blank means "keep what is there" — the inputs are always empty, so a
    blank submission must not wipe a working key."""
    apify.set_credentials({"api_key": SECRET}, actor_label="someone@test")
    apify.save()
    client.force_login(platform_admin)

    client.post(reverse("ops-provider-credentials", args=[apify.pk]), {"api_key": ""})

    apify.refresh_from_db()
    assert apify.credential("api_key") == SECRET


def test_a_half_entered_pair_is_refused(client, platform_admin):
    """Taddy authenticates with a key AND a user id, and with neither alone.

    Storing one of two would fail at the next run as a 401 nobody can explain.
    """
    taddy = AcquisitionProvider.objects.create(
        kind=AcquisitionProvider.Kind.TADDY, name="Taddy"
    )
    client.force_login(platform_admin)

    client.post(
        reverse("ops-provider-credentials", args=[taddy.pk]),
        {"api_key": "k-123456", "user_id": ""},
    )

    taddy.refresh_from_db()
    assert not taddy.has_credential


def test_both_halves_together_are_accepted(client, platform_admin):
    taddy = AcquisitionProvider.objects.create(
        kind=AcquisitionProvider.Kind.TADDY, name="Taddy"
    )
    client.force_login(platform_admin)

    client.post(
        reverse("ops-provider-credentials", args=[taddy.pk]),
        {"api_key": "k-123456", "user_id": "9876"},
    )

    taddy.refresh_from_db()
    assert taddy.credential("api_key") == "k-123456"
    assert taddy.credential("user_id") == "9876"


def test_clearing_removes_the_key_and_is_audited(client, platform_admin, apify):
    apify.set_credentials({"api_key": SECRET}, actor_label="someone@test")
    apify.save()
    client.force_login(platform_admin)

    client.post(reverse("ops-provider-credentials", args=[apify.pk]), {"clear": "1"})

    apify.refresh_from_db()
    assert not apify.has_credential
    assert AuditEvent.objects.filter(message__contains="cleared").exists()


def test_rotating_records_it_as_a_replacement(client, platform_admin, apify):
    apify.set_credentials({"api_key": "the-old-key"}, actor_label="someone@test")
    apify.save()
    client.force_login(platform_admin)

    client.post(reverse("ops-provider-credentials", args=[apify.pk]), {"api_key": SECRET})

    apify.refresh_from_db()
    assert apify.credential("api_key") == SECRET
    event = AuditEvent.objects.filter(kind=AuditEvent.Kind.CONFIG).latest("at")
    assert "replaced" in event.message

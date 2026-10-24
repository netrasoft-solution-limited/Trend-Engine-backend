"""Expert reviewers registered on screen by a Platform Admin (Mark, 24 Oct)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.operations.models import AuditEvent, OperatorUser
from apps.outputs.models import ExpertReviewer

pytestmark = pytest.mark.django_db

URL = "ops-reviewers"


@pytest.fixture
def admin(db):
    return OperatorUser.objects.create_user(
        email="admin@netrasoft.test", password="x" * 24, name="Platform Admin",
        role=OperatorUser.Role.PLATFORM_ADMIN,
    )


@pytest.fixture
def operator(db):
    return OperatorUser.objects.create_user(
        email="operator@netrasoft.test", password="x" * 24, name="Operator"
    )


def add(client, **fields):
    data = {"action": "add", "name": "Dr Jane Doe", "qualification": "RD, PhD",
            "email": "Jane@Example.com", **fields}
    return client.post(reverse(URL), data, follow=True)


def test_an_admin_adds_a_reviewer_and_it_is_audited(client, admin):
    client.force_login(admin)

    page = add(client).content.decode()

    reviewer = ExpertReviewer.objects.get()
    assert reviewer.email == "jane@example.com" and reviewer.is_active
    assert reviewer.added_by_label == "admin@netrasoft.test"
    assert "Dr Jane Doe can now be named on an expert sign-off" in page
    assert AuditEvent.objects.filter(message__startswith="Registered expert reviewer").exists()


def test_a_duplicate_or_a_bad_email_is_refused_in_words(client, admin):
    client.force_login(admin)
    add(client)

    assert "already registered" in add(client).content.decode()
    assert "not an email address" in add(client, name="Dr B", email="nope").content.decode()
    assert ExpertReviewer.objects.count() == 1


def test_deactivating_keeps_the_reviewer_on_record(client, admin):
    client.force_login(admin)
    add(client)
    reviewer = ExpertReviewer.objects.get()

    client.post(reverse(URL), {"action": "deactivate", "reviewer": reviewer.pk})
    reviewer.refresh_from_db()
    assert not reviewer.is_active

    client.post(reverse(URL), {"action": "reactivate", "reviewer": reviewer.pk})
    reviewer.refresh_from_db()
    assert reviewer.is_active


def test_a_plain_operator_cannot_manage_reviewers(client, operator):
    client.force_login(operator)

    assert client.get(reverse(URL)).status_code == 403
    assert add(client).status_code == 403
    assert not ExpertReviewer.objects.exists()


def test_it_returns_only_to_an_output_page(client, admin):
    client.force_login(admin)

    back = client.post(reverse(URL), {"action": "add", "name": "Dr C", "qualification": "RD",
                                      "email": "c@example.com", "next": "/output/6/"})
    away = client.post(reverse(URL), {"action": "add", "name": "Dr D", "qualification": "RD",
                                      "email": "d@example.com", "next": "https://evil.test/"})

    assert back.url == "/output/6/"
    assert away.url == reverse(URL)

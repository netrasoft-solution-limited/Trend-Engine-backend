"""The detection run record an operator can open (Mark, 15 Nov)."""
import pytest
from django.urls import reverse

from apps.operations.models import AuditEvent, OperatorUser

pytestmark = pytest.mark.django_db


def test_the_page_lists_runs_and_shows_a_failure(client):
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.SYSTEM, actor_realm=AuditEvent.Realm.SYSTEM,
        actor_label="signal-detection", message="Signal detection failed: cut off",
        context={"failed": True, "failed_batches": 1, "errors": ["cut off"], "claims": 150,
                 "created": [], "extended": []},
    )
    client.force_login(OperatorUser.objects.create_user(
        email="o@x.test", password="x" * 24, name="O"))

    page = client.get(reverse("ops-detection-runs")).content.decode()

    assert "1 failing" in page and "1 batch(es) failed: cut off" in page
    assert reverse("ops-detection-runs") in client.get(reverse("ops-runs")).content.decode()


def test_detection_can_be_started_by_hand(client, monkeypatch):
    sent = []
    from celery import current_app

    monkeypatch.setattr(current_app, "send_task", lambda name, **kw: sent.append((name, kw)))
    client.force_login(OperatorUser.objects.create_user(
        email="o@x.test", password="x" * 24, name="O"))

    response = client.post(reverse("ops-detection-runs"))

    assert response.status_code == 302
    assert sent == [("apps.intelligence.tasks.detect_signals", {"queue": "enrich"})]
    assert AuditEvent.objects.filter(message="Started signal detection by hand").exists()

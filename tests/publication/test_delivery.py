"""Sending a published output to a client.

This is the last step of the whole system and the only one that cannot be taken
back, so most of what is tested here is refusal.

The sharpest rule: delivery requires a LIVE publication, while export does not.
`unpublish()` can pull a page; nothing can unsend an email. Letting this accept
an unpublished version would not merely reintroduce the single-mis-click
disclosure the gate exists to prevent — it would make it irreversible.
"""
from __future__ import annotations

import pytest
from django.core import mail

from apps.clients.models import ClientContact
from apps.operations.models import AuditEvent
from apps.outputs.models import ExportArtifact, OutputState
from apps.publication import delivery as delivery_service
from apps.publication import services
from apps.publication.models import Delivery
from apps.tenancy.context import operator_scope

pytestmark = [pytest.mark.django_db, pytest.mark.publication]

# Formats that need no third-party package, so this suite runs anywhere.
PLAIN = ("markdown", "csv")


@pytest.fixture
def readers(organization):
    with operator_scope():
        ClientContact.objects.create(
            organization=organization, name="Mark", email="mark@jarrow.example"
        )
        ClientContact.objects.create(
            organization=organization, name="Dana", email="dana@jarrow.example"
        )
        ClientContact.objects.create(
            organization=organization, name="Accounts", email="ap@jarrow.example",
            receives_outputs=False,
        )


@pytest.fixture
def live(approved_version, organization, operator, django_capture_on_commit_callbacks):
    with operator_scope(), django_capture_on_commit_callbacks(execute=True):
        return services.publish(
            version=approved_version, organization=organization, actor_label=operator.email
        )


def send(publication, operator, callbacks, **kwargs):
    with operator_scope(), callbacks(execute=True):
        return delivery_service.deliver(
            publication=publication,
            formats=kwargs.pop("formats", PLAIN),
            actor_label=operator.email,
            **kwargs,
        )


# ── Refusals ────────────────────────────────────────────────────────────────


def test_a_withdrawn_publication_cannot_be_delivered(
    live, operator, readers, django_capture_on_commit_callbacks
):
    """The content is wrong enough to have been pulled. Sending it afterwards
    is the one mistake that cannot be undone."""
    with operator_scope(), django_capture_on_commit_callbacks(execute=True):
        services.unpublish(publication=live, actor_label=operator.email, reason="wrong")
    mail.outbox.clear()

    with pytest.raises(delivery_service.DeliveryError, match="was withdrawn"):
        send(live, operator, django_capture_on_commit_callbacks)

    assert mail.outbox == []


def test_a_client_with_nobody_to_send_to_is_refused(
    live, operator, django_capture_on_commit_callbacks
):
    """No `readers` fixture. A delivery to nobody succeeds and is then
    remembered as a delivery, which is much harder to notice than a failure."""
    with pytest.raises(delivery_service.DeliveryError, match="nobody set to receive"):
        send(live, operator, django_capture_on_commit_callbacks)

    with operator_scope():
        assert Delivery.objects.count() == 0


def test_a_contact_who_does_not_receive_outputs_is_not_sent_to(
    live, operator, readers, django_capture_on_commit_callbacks
):
    send(live, operator, django_capture_on_commit_callbacks)

    addressed = [address for message in mail.outbox for address in message.to]
    assert not any("ap@jarrow.example" in address for address in addressed)


def test_an_unavailable_format_is_refused_by_name(
    live, operator, readers, django_capture_on_commit_callbacks
):
    with pytest.raises(delivery_service.DeliveryError, match="Available:"):
        send(live, operator, django_capture_on_commit_callbacks, formats=("wordperfect",))


# ── What actually goes out ──────────────────────────────────────────────────


def test_each_recipient_gets_their_own_message_with_every_format_attached(
    live, operator, readers, django_capture_on_commit_callbacks
):
    send(live, operator, django_capture_on_commit_callbacks)

    assert len(mail.outbox) == 2
    for message in mail.outbox:
        assert len(message.to) == 1
        assert sorted(name.rsplit(".", 1)[1] for name, _c, _t in message.attachments) == [
            "csv",
            "md",
        ]


def test_the_attachment_carries_its_media_type(
    live, operator, readers, django_capture_on_commit_callbacks
):
    """The backend passes this to Resend rather than letting it infer from the
    filename."""
    send(live, operator, django_capture_on_commit_callbacks)

    types = {t.split(";")[0] for _n, _c, t in mail.outbox[0].attachments}
    assert types == {"text/markdown", "text/csv"}


def test_the_message_carries_no_link(
    live, operator, readers, django_capture_on_commit_callbacks
):
    """There is nowhere to send them — the document is attached, and a link
    would point at a portal that is not deployed."""
    send(live, operator, django_capture_on_commit_callbacks)

    assert "http" not in mail.outbox[0].body


def test_a_covering_note_reaches_the_client(
    live, operator, readers, django_capture_on_commit_callbacks
):
    send(live, operator, django_capture_on_commit_callbacks, note="Flagging section two.")

    assert "Flagging section two." in mail.outbox[0].body


def test_nothing_is_sent_before_the_delivery_commits(
    live, operator, readers, django_capture_on_commit_callbacks
):
    with operator_scope(), django_capture_on_commit_callbacks(execute=False) as callbacks:
        delivery_service.deliver(
            publication=live, formats=PLAIN, actor_label=operator.email
        )
        assert mail.outbox == [], "sent from inside the transaction"

    assert len(callbacks) == 1


# ── The record ──────────────────────────────────────────────────────────────


def test_a_delivery_records_who_it_went_to(
    live, operator, readers, django_capture_on_commit_callbacks
):
    record = send(live, operator, django_capture_on_commit_callbacks)

    assert sorted(record.recipients) == ["dana@jarrow.example", "mark@jarrow.example"]
    assert record.formats == list(PLAIN)
    assert record.sent_by_label == operator.email


def test_the_recipients_survive_a_contact_being_removed(
    live, operator, readers, django_capture_on_commit_callbacks
):
    """Denormalised strings, not a relation: "who did this go to?" has to stay
    answerable after someone leaves the client's team."""
    record = send(live, operator, django_capture_on_commit_callbacks)

    with operator_scope():
        ClientContact.objects.filter(email="dana@jarrow.example").delete()
        record.refresh_from_db()

    assert "dana@jarrow.example" in record.recipients


def test_an_export_artifact_is_written_per_format(
    live, operator, readers, django_capture_on_commit_callbacks
):
    send(live, operator, django_capture_on_commit_callbacks)

    with operator_scope():
        artifacts = ExportArtifact.objects.all()
        assert sorted(a.format for a in artifacts) == ["csv", "markdown"]
        assert all(len(a.sha256) == 64 for a in artifacts)
        assert all(a.was_draft is False for a in artifacts), "a live publication"


def test_the_delivery_is_audited_as_an_export_event(
    live, operator, readers, django_capture_on_commit_callbacks
):
    """PRD §7.1 requires export events in the audit log. `Kind.EXPORT` existed
    and nothing wrote it until now."""
    send(live, operator, django_capture_on_commit_callbacks)

    with operator_scope():
        event = AuditEvent.objects.filter(kind=AuditEvent.Kind.EXPORT).first()
    assert event is not None
    assert "2 recipient(s)" in event.message
    assert sorted(event.context["recipients"]) == [
        "dana@jarrow.example",
        "mark@jarrow.example",
    ]


def test_delivering_moves_the_output_to_delivered(
    live, operator, readers, django_capture_on_commit_callbacks
):
    """`OutputState.DELIVERED` existed and nothing wrote it."""
    send(live, operator, django_capture_on_commit_callbacks)

    live.refresh_from_db()
    with operator_scope():
        live.output.refresh_from_db()
    assert live.output.state == OutputState.DELIVERED
    assert live.notified_at is not None, "stamped when a document actually went out"


def test_delivering_twice_records_two_deliveries(
    live, operator, readers, django_capture_on_commit_callbacks
):
    """Re-sending to a contact added later is legitimate, and the trail should
    show how many times something went out."""
    send(live, operator, django_capture_on_commit_callbacks)
    send(live, operator, django_capture_on_commit_callbacks)

    with operator_scope():
        assert Delivery.objects.count() == 2


# ── Mail failure is not delivery failure ────────────────────────────────────


def test_a_mail_outage_leaves_the_record_standing(
    live, operator, readers, django_capture_on_commit_callbacks, settings
):
    settings.EMAIL_BACKEND = "tests.publication.test_publication_email.BrokenBackend"

    record = send(live, operator, django_capture_on_commit_callbacks)

    with operator_scope():
        assert Delivery.objects.filter(pk=record.pk).exists()
        assert AuditEvent.objects.filter(kind=AuditEvent.Kind.EXPORT).exists()


# ── Withdrawing after delivery ──────────────────────────────────────────────


def test_withdrawing_after_delivery_says_the_email_cannot_be_recalled(
    live, operator, readers, django_capture_on_commit_callbacks
):
    """`output.state` goes back to APPROVED either way, which after a delivery
    reads as "never sent" about something sitting in a client's inbox."""
    send(live, operator, django_capture_on_commit_callbacks)

    with operator_scope(), django_capture_on_commit_callbacks(execute=True):
        services.unpublish(publication=live, actor_label=operator.email, reason="bad quote")

    with operator_scope():
        event = AuditEvent.objects.filter(
            kind=AuditEvent.Kind.PUBLICATION, message__contains="Unpublished"
        ).first()
    assert "cannot be recalled" in event.message
    assert len(event.context["delivered_to"]) == 2


def test_withdrawing_something_never_delivered_says_nothing_about_recall(
    live, operator, readers, django_capture_on_commit_callbacks
):
    with operator_scope(), django_capture_on_commit_callbacks(execute=True):
        services.unpublish(publication=live, actor_label=operator.email, reason="early")

    with operator_scope():
        event = AuditEvent.objects.filter(
            kind=AuditEvent.Kind.PUBLICATION, message__contains="Unpublished"
        ).first()
    assert "cannot be recalled" not in event.message
    assert event.context["delivered_to"] == []

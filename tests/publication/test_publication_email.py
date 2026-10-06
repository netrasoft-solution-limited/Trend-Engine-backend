"""What the gate tells the client, now that there is no portal to tell them about.

The shape of this changed when the client portal was mothballed, and the change
is the point:

  · PUBLISHING NO LONGER EMAILS ANYONE. It is the internal gate. An email at
    publish time would carry no attachment (nothing is rendered yet) and no link
    (there is nowhere to send them), then be followed moments later by the real
    one. One thing, two emails.
  · WITHDRAWING STILL DOES. A client sent a document that turns out to be wrong
    has to be told, portal or no portal (PRD §6.9).
  · The recipient list is `clients.ClientContact`, not `portal.OrgUser`.

The invariants from the original version of this file all still hold and are
still tested below: sent after the commit, never from inside the gate's
transaction; a mail failure never undoes committed work; one message per
recipient rather than one addressed to everybody.

This lives in the `publication` suite because what it constrains is the gate's
behaviour, and CI asserts this suite ran (Arch §5.4).
"""
from __future__ import annotations

import pytest
from django.core import mail

from apps.clients.models import ClientContact
from apps.publication import services
from apps.tenancy.context import operator_scope

pytestmark = [pytest.mark.django_db, pytest.mark.publication]


def contact(organization, name, email, **overrides):
    with operator_scope():
        return ClientContact.objects.create(
            organization=organization, name=name, email=email, **overrides
        )


@pytest.fixture
def readers(organization):
    """Two who should hear, and two who should not."""
    contact(organization, "Mark", "mark@jarrow.example")
    contact(organization, "Dana", "dana@jarrow.example")
    contact(organization, "Finance", "ap@jarrow.example", receives_outputs=False)
    contact(organization, "Departed", "old@jarrow.example", is_active=False)


def publish(version, organization, operator, callbacks):
    with operator_scope(), callbacks(execute=True):
        return services.publish(
            version=version, organization=organization, actor_label=operator.email
        )


def withdraw(publication, operator, callbacks, reason="Quote misattributed"):
    with operator_scope(), callbacks(execute=True):
        services.unpublish(
            publication=publication, actor_label=operator.email, reason=reason
        )


# ── Publishing is internal now ──────────────────────────────────────────────


def test_publishing_emails_nobody(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    """The gate records that a version is cleared to go out. Sending it is a
    separate, deliberate act — and the only one that can carry the document."""
    publish(approved_version, organization, operator, django_capture_on_commit_callbacks)

    assert mail.outbox == []


def test_publishing_does_not_claim_the_client_was_notified(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    """`notified_at` used to be stamped here, which recorded an intention rather
    than a notification. Now it stays NULL until a document actually goes out."""
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )

    assert publication.notified_at is None


# ── Withdrawal still reaches the client ─────────────────────────────────────


def test_withdrawing_tells_every_contact_who_receives_outputs(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )
    withdraw(publication, operator, django_capture_on_commit_callbacks)

    assert [m.to for m in mail.outbox] == [["dana@jarrow.example"], ["mark@jarrow.example"]]


def test_each_recipient_gets_their_own_message(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    """Not one message with the whole client in `To`. They are colleagues, but
    their addresses are still theirs."""
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )
    withdraw(publication, operator, django_capture_on_commit_callbacks)

    assert len(mail.outbox) == 2
    for message in mail.outbox:
        assert len(message.to) == 1


def test_a_contact_who_does_not_receive_outputs_is_skipped(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    """Accounts payable should exist on the record without being sent briefs."""
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )
    withdraw(publication, operator, django_capture_on_commit_callbacks)

    assert "ap@jarrow.example" not in [a for m in mail.outbox for a in m.to]


def test_a_deactivated_contact_is_skipped(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )
    withdraw(publication, operator, django_capture_on_commit_callbacks)

    assert "old@jarrow.example" not in [a for m in mail.outbox for a in m.to]


def test_the_withdrawal_notice_carries_no_link(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    """There is nothing to open. The old version linked into the portal, which
    would now 404."""
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )
    withdraw(publication, operator, django_capture_on_commit_callbacks)

    body = mail.outbox[0].body
    assert "http" not in body
    assert publication.title in body


def test_the_withdrawal_notice_does_not_leak_the_internal_reason(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    """The reason is an operator's note for the audit trail. "Quote
    misattributed" is not something to put in front of the person who read the
    quote."""
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )
    withdraw(publication, operator, django_capture_on_commit_callbacks)

    assert "misattributed" not in mail.outbox[0].body.lower()


# ── Ordering against the transaction ────────────────────────────────────────


def test_nothing_is_sent_before_the_withdrawal_commits(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    """Capturing WITHOUT executing is what makes this a real assertion: the
    callbacks list proves the send was deferred rather than skipped."""
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )

    with operator_scope(), django_capture_on_commit_callbacks(execute=False) as callbacks:
        services.unpublish(
            publication=publication, actor_label=operator.email, reason="wrong"
        )
        assert mail.outbox == [], "sent from inside the gate's transaction"

    assert len(callbacks) == 1, "the send was never queued"


def test_a_rolled_back_withdrawal_emails_nobody(
    approved_version, organization, operator, readers, django_capture_on_commit_callbacks
):
    """A client told a document was withdrawn, when it was not, cannot be
    un-told."""
    from django.db import transaction

    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )

    with operator_scope(), django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            services.unpublish(
                publication=publication, actor_label=operator.email, reason="wrong"
            )
            raise RuntimeError("something later in the operator's request failed")

    assert mail.outbox == []


# ── Mail failure is not gate failure ────────────────────────────────────────


def test_a_mail_outage_does_not_undo_the_withdrawal(
    approved_version, organization, operator, readers,
    django_capture_on_commit_callbacks, settings,
):
    """By the time the send runs the withdrawal is committed and audited.
    Raising would hand the operator an exception for work that succeeded."""
    settings.EMAIL_BACKEND = "tests.publication.test_publication_email.BrokenBackend"

    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )
    withdraw(publication, operator, django_capture_on_commit_callbacks)

    publication.refresh_from_db()
    assert publication.unpublished_at is not None
    with operator_scope():
        assert services.published_for(organization).count() == 0


def test_a_client_with_no_contacts_withdraws_without_error(
    approved_version, organization, operator, django_capture_on_commit_callbacks
):
    """No `readers` fixture here. A client nobody has been added for must still
    be able to have a publication withdrawn."""
    publication = publish(
        approved_version, organization, operator, django_capture_on_commit_callbacks
    )
    withdraw(publication, operator, django_capture_on_commit_callbacks)

    publication.refresh_from_db()
    assert publication.unpublished_at is not None
    assert mail.outbox == []


class BrokenBackend:
    """Stands in for the mail provider being down."""

    def __init__(self, *args, **kwargs):
        pass

    def send_messages(self, messages):
        raise OSError("mail provider unreachable")

    def open(self):
        pass

    def close(self):
        pass

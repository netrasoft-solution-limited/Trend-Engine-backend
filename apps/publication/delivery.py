"""Sending a published output to a client.

With no portal, this is how work reaches anyone: a document attached to an
email. It is the last step of the whole system and the only one that cannot be
taken back.

WHY THIS IS NOT IN `services.py`. That module opens by calling itself "the most
safety-critical component in the system, and the one most likely to be
'simplified' by someone who doesn't see why it exists". It is about state
transitions and it stays short enough to read in one sitting. A renderer and a
mail call do not belong in it.

DELIVERY REQUIRES A LIVE PUBLICATION. Export does not — an operator can
download any version, watermarked DRAFT, and do as they like with it. The line
is drawn there because `unpublish()` can pull a page and **nothing can unsend
an email**. Accepting an `OutputVersion` here would not merely reintroduce the
single-mis-click disclosure the gate exists to prevent; it would make it
irreversible, which is strictly worse than the state before the gate was built.

NOT IDEMPOTENT, deliberately. Re-sending to a contact added later is a real
thing to want, and each call writes its own `Delivery` row, so the trail records
how many times something went out and to whom — rather than quietly collapsing
two sends into one record.
"""
from __future__ import annotations

import logging

from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from apps.clients.contacts import recipients_for
from apps.operations.models import AuditEvent
from apps.outputs import exports
from apps.outputs.models import ExportArtifact, OutputState

from .exports import document_for_publication
from .models import Delivery, Publication

logger = logging.getLogger(__name__)

#: What goes out when the operator does not choose. PDF because it is what a
#: client forwards and what opens the same way everywhere; Word because it is
#: what a client edits.
DEFAULT_FORMATS = ("pdf", "docx")


class DeliveryError(RuntimeError):
    """The publication cannot be sent."""


@transaction.atomic
def deliver(
    *,
    publication: Publication,
    formats: tuple[str, ...] | list[str] = DEFAULT_FORMATS,
    actor_label: str,
    note: str = "",
) -> Delivery:
    """Render, record, and send. Returns the Delivery row.

    The ordering mirrors `services.publish()` and is load-bearing:

    1. Refuse a withdrawn publication.
    2. Refuse an empty recipient list.
    3. Render every format in memory — before anything is recorded, so a
       renderer failing leaves no half-delivery behind.
    4. In one transaction: the artifacts, the delivery, the audit event.
    5. After the commit: build the message and send it.
    """
    if publication.unpublished_at is not None:
        raise DeliveryError(
            f"“{publication.title}” was withdrawn on "
            f"{publication.unpublished_at:%d %B %Y}. Publish a corrected version "
            f"before sending anything else to this client."
        )

    recipients = recipients_for(publication.organization)
    if not recipients:
        # Refused rather than reported as sent. A delivery to nobody succeeds
        # and then gets remembered as a delivery, which is much harder to notice
        # than a failure — the same reasoning that makes `clients.services`
        # refuse to activate an empty profile.
        raise DeliveryError(
            f"{publication.organization.name} has nobody set to receive outputs. "
            f"Add a contact first — a delivery with no recipients would be "
            f"recorded as sent."
        )

    chosen = [fmt for fmt in formats if fmt in exports.available()]
    if not chosen:
        raise DeliveryError(
            f"None of {list(formats)} can be rendered here. "
            f"Available: {', '.join(exports.available())}."
        )

    document = document_for_publication(publication)
    rendered = [exports.render(document, fmt) for fmt in chosen]

    version = publication.version
    ExportArtifact.objects.bulk_create(
        [
            ExportArtifact(
                organization=publication.organization,
                version=version,
                format=item.format,
                filename=item.filename,
                byte_size=item.byte_size,
                sha256=item.sha256(),
                renderer_version=exports.RENDERER_VERSION,
                was_draft=document.is_draft,
                created_by_label=actor_label,
            )
            for item in rendered
        ]
    )

    delivery = Delivery.objects.create(
        organization=publication.organization,
        publication=publication,
        recipients=[recipient.email for recipient in recipients],
        formats=chosen,
        note=note,
        sent_by_label=actor_label,
    )

    publication.notified_at = timezone.now()
    publication.save(update_fields=["notified_at"])

    output = publication.output
    output.state = OutputState.DELIVERED
    output.save(update_fields=["state"])
    version.state = OutputState.DELIVERED
    version.save(update_fields=["state"])

    AuditEvent.objects.create(
        kind=AuditEvent.Kind.EXPORT,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label,
        organization=publication.organization,
        message=(
            f"Delivered {publication.title} v{publication.version_number} to "
            f"{len(recipients)} recipient(s) at {publication.organization.name}"
        ),
        context={
            "publication_id": publication.pk,
            "delivery_id": delivery.pk,
            "formats": chosen,
            "recipients": [recipient.email for recipient in recipients],
            "sha256": {item.format: item.sha256() for item in rendered},
        },
    )

    # After the commit, for the reason the gate's own notices are: never hold a
    # transaction open across a call to the mail provider, and never send
    # something announcing work that may still roll back.
    transaction.on_commit(
        lambda: _send(publication, recipients, rendered, note=note, delivery_id=delivery.pk)
    )
    return delivery


def _send(publication, recipients, rendered, *, note: str, delivery_id) -> int:
    """One message per recipient, each carrying every chosen format.

    A mail failure does not raise: by the time this runs the delivery is
    committed and audited, and throwing would hand the operator an exception for
    work that succeeded. The `Delivery` row is the durable record either way,
    and a send that failed is visible as a row with nothing in the mail log.
    """
    sent = 0
    for recipient in recipients:
        message = EmailMessage(
            subject=f"{publication.type_label}: {publication.title}",
            body=_body(publication, note=note),
            to=[recipient.header()],
        )
        for item in rendered:
            message.attach(item.filename, item.content, item.mimetype)
        try:
            sent += message.send(fail_silently=False)
        except Exception:
            logger.exception(
                "Delivery %s: could not send to %s; the record stands",
                delivery_id,
                recipient.email,
            )
    logger.info("Delivery %s: sent to %d of %d", delivery_id, sent, len(recipients))
    return sent


def _body(publication, *, note: str) -> str:
    """Plain text, and no link.

    There is nowhere to send them: the document is attached. A link would point
    at a portal that is not deployed.
    """
    lines = [publication.title, ""]
    if note.strip():
        lines += [note.strip(), ""]
    elif publication.summary:
        lines += [publication.summary, ""]
    lines += [
        "The full document is attached.",
        "",
        "Every claim in it is quoted from a transcript we hold, with the speaker "
        "and the moment in the recording named, so anything in it can be checked "
        "against its source.",
        "",
    ]
    return "\n".join(lines)


def delivered_recipients(publication) -> list[str]:
    """Everyone this publication has ever actually been sent to.

    Used by `unpublish()`: a withdrawal after delivery has to say so, because
    the email cannot be recalled and the state field alone would read as
    "never delivered".
    """
    seen: list[str] = []
    for delivery in publication.deliveries.all():
        for address in delivery.recipients:
            if address not in seen:
                seen.append(address)
    return seen

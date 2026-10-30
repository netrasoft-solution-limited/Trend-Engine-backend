"""Emailing an output to Pure Play for review — never to the client.

Mark, 30 October: everything goes only to him; Pure Play handles onward
delivery. `clients.contacts.recipients_for` therefore answers with
`OUTPUT_REVIEW_RECIPIENTS`, and the wording below says so. A draft can be sent
for review before publication and while expert review is pending, labelled as
exactly that (`send_for_review`).

The original reasoning, kept because the gate still applies to a published
send:

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


NO_REVIEWER = (
    "No review address is configured (OUTPUT_REVIEW_RECIPIENTS), so nothing can be "
    "sent — a delivery to nobody would be recorded as sent."
)


def review_status(output) -> str:
    """The expert-review position in words, for the email and the audit row."""
    signed = [r for r in output.expert_reviews.all() if r.is_signed_off]
    if signed:
        r = signed[0]
        return f"Expert review: signed off by {r.reviewer_label} on {r.signed_off_at:%-d %B %Y}."
    return "Expert review: pending. No specialist has reviewed this version."


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
        raise DeliveryError(NO_REVIEWER)

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
            f"Sent {publication.title} v{publication.version_number} for review to "
            f"{', '.join(r.email for r in recipients)} (not to {publication.organization.name})"
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
    status = review_status(publication.output)
    transaction.on_commit(
        lambda: _send(
            publication, recipients, rendered, note=note, delivery_id=delivery.pk,
            status=f"Published version v{publication.version_number}. {status}",
        )
    )
    return delivery


@transaction.atomic
def send_for_review(
    *,
    version,
    formats: tuple[str, ...] | list[str] = DEFAULT_FORMATS,
    actor_label: str,
    note: str = "",
) -> list[str]:
    """Email any version, published or not, to the review address.

    Usable while expert review is pending, because it goes only to Pure Play: an
    unpublished version is watermarked DRAFT in every file, and the email says
    plainly whether it is published and whether a specialist has reviewed it.
    Returns the addresses it went to.
    """
    from apps.outputs.exports.adapters import document_for_version

    recipients = recipients_for(version.organization)
    if not recipients:
        raise DeliveryError(NO_REVIEWER)
    chosen = [fmt for fmt in formats if fmt in exports.available()]
    if not chosen:
        raise DeliveryError(f"None of {list(formats)} can be rendered here.")

    document = document_for_version(version)
    rendered = [exports.render(document, fmt) for fmt in chosen]
    ExportArtifact.objects.bulk_create([
        ExportArtifact(
            organization=version.organization, version=version, format=item.format,
            filename=item.filename, byte_size=item.byte_size, sha256=item.sha256(),
            renderer_version=exports.RENDERER_VERSION, was_draft=document.is_draft,
            created_by_label=actor_label,
        )
        for item in rendered
    ])
    output = version.output
    stage = (
        f"Draft v{version.number}, not published — every file is watermarked DRAFT."
        if document.is_draft else f"Published version v{version.number}."
    )
    status = f"{stage} {review_status(output)}"
    addresses = [r.email for r in recipients]
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.EXPORT,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label,
        organization=version.organization,
        message=f"Sent {output.title} v{version.number} for review to {', '.join(addresses)}",
        context={
            "output_id": output.pk, "version": version.number, "formats": chosen,
            "recipients": addresses, "was_draft": document.is_draft, "status": status,
            "sha256": {item.format: item.sha256() for item in rendered},
        },
    )
    review = _Review(output, version.organization)
    transaction.on_commit(
        lambda: _send(review, recipients, rendered, note=note, delivery_id=None, status=status)
    )
    return addresses


class _Review:
    """What `_send` reads from a publication, for a version sent as a draft."""

    def __init__(self, output, organization) -> None:
        self.type_label = output.get_type_display()
        self.title = output.title
        self.summary = ""
        self.organization = organization


def _send(publication, recipients, rendered, *, note: str, delivery_id, status: str = "") -> int:
    """One message per recipient, each carrying every chosen format.

    A mail failure does not raise: by the time this runs the delivery is
    committed and audited, and throwing would hand the operator an exception for
    work that succeeded. The `Delivery` row is the durable record either way,
    and a send that failed is visible as a row with nothing in the mail log.
    """
    sent = 0
    for recipient in recipients:
        message = EmailMessage(
            subject=f"[For review] {publication.type_label}: {publication.title}",
            body=_body(publication, note=note, status=status),
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


def _body(publication, *, note: str, status: str = "") -> str:
    """Plain text, and no link: the document is attached.

    Addressed to Pure Play, so it says what it is — a review copy that has not
    gone to the client — and where it stands on publication and expert review.
    """
    from django.conf import settings

    client = publication.organization.name
    brand = getattr(settings, "EXPORT_BRAND", "") or "Pure Play"
    lines = [publication.title, ""]
    if status:
        lines += [status, ""]
    if note.strip():
        lines += [note.strip(), ""]
    elif publication.summary:
        lines += [publication.summary, ""]
    lines += [
        f"Review copy for {brand}. It has not been sent to {client} or to anyone at "
        f"{client}; onward delivery is yours.",
        "",
        "The full document is attached. Every claim in it is linked to a dated "
        "source listed at the end.",
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

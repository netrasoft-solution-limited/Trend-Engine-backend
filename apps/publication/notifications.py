"""Telling the client. Owned here because the gate decides what reaches them.

MOVED FROM `apps.portal`, AND NARROWED. The portal used to be where a client
read their work, so publishing emailed them a link to it. There is no portal
any more: an output reaches a client as a document attached to an email, sent
by `delivery.deliver()`.

That changes what publishing means for the client, and the honest answer is
NOTHING. Publishing is now the internal gate — it records that this version is
the one cleared to go out. An email at publish time could carry no attachment
(nothing has been rendered yet) and no link (there is nowhere to send them), so
it would announce that something exists while giving no way to get it, and then
be followed moments later by the real one. One thing, two emails. So there is
no publication email here, deliberately.

WITHDRAWAL IS DIFFERENT and still belongs at this layer. PRD §6.9 makes
publication reversible; a client who has been sent a document that turns out to
be wrong has to be told, and that is true whether or not a portal exists.

Recipients come from `apps.clients.contacts` (L5), read from here at L7 —
downward, legal, and no longer routed through a signal. The signal indirection
existed only because `apps.portal` sat ABOVE `apps.publication` and could not be
imported; with the recipient list below us, the mandatory client notice no
longer depends on an `AppConfig.ready()` having run.
"""
from __future__ import annotations

import logging

from django.core.mail import send_mass_mail

from apps.clients.contacts import recipients_for

logger = logging.getLogger(__name__)


def _send(messages: list[tuple], *, what: str, publication_id) -> int:
    """Send, and never let a mail failure surface as a gate failure.

    Called from `transaction.on_commit`, so the publication or withdrawal is
    already committed and audited. Raising here would propagate out of the
    outermost atomic block and present succeeded work to the operator as an
    exception.
    """
    if not messages:
        logger.warning("%s for publication %s had no recipients", what, publication_id)
        return 0
    try:
        sent = send_mass_mail(tuple(messages), fail_silently=False)
    except Exception:
        logger.exception(
            "%s email failed for publication %s; the audit record stands", what, publication_id
        )
        return 0
    logger.info("%s emailed to %d recipient(s) for publication %s", what, sent, publication_id)
    return sent


def send_withdrawal_email(publication) -> int:
    """A document that was sent out is no longer current.

    One message per recipient, never one addressed to everybody — the contacts
    at a client are colleagues, but their addresses are still theirs.

    Carries no link (there is nothing to open) and does not repeat the internal
    reason: `unpublished_reason` is an operator's note for the audit trail, and
    "quote misattributed" is not something to put in front of the person who
    read the quote.

    NOT YET GATED ON ACTUAL DELIVERY. Once `delivery.Delivery` exists, a
    publication that was never sent to anyone should withdraw silently —
    telling a client that something they never received has been pulled is
    confusing rather than careful.
    """
    recipients = recipients_for(publication.organization)

    return _send(
        [
            (
                "A document we sent you has been withdrawn",
                (
                    f"“{publication.title}” has been withdrawn while it is corrected. "
                    f"Please disregard the copy you were sent; a corrected version "
                    f"will follow.\n"
                ),
                None,
                [recipient.email],
            )
            for recipient in recipients
        ],
        what="Withdrawal notice",
        publication_id=publication.pk,
    )

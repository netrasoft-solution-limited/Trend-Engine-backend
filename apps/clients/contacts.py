"""Who to send a client's work to — which is never the client.

Mark, 30 October: "all briefs, reports, exports and automated email
deliveries go only to me for review. Never email Jarrow or any client contact
directly." So `recipients_for` answers with `OUTPUT_REVIEW_RECIPIENTS` from
settings, whatever the organisation, and client contacts are a record of who
Pure Play forwards to, not an address list this system mails.

One function, because it is the single place that answers "who is this going
to?" and that question should have exactly one answer. The publication notice
and the delivery of an actual document must not be able to disagree about the
recipient list — that disagreement is how someone ends up told a brief exists
and never sent it.

Deliberately shaped like the `portal.notifications._active_recipients()` it
replaces, so the two filters stay comparable: active row, wants the mail,
deduplicated, ordered.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from email.utils import parseaddr

from .models import ClientContact

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Recipient:
    name: str
    email: str

    def header(self) -> str:
        """`Mark Costigliola <mark@…>` — so the client sees their own name in
        the To field rather than a bare address."""
        return f"{self.name} <{self.email}>" if self.name else self.email


def recipients_for(organization) -> list[Recipient]:
    """Who outputs about this client are emailed to: Pure Play's reviewer.

    The organisation is taken so callers read naturally, and is deliberately
    unused for addressing. An empty list is a meaningful answer, not an error —
    `publication.delivery` refuses to send on one rather than reporting a
    successful delivery to nobody.
    """
    from django.conf import settings

    recipients: list[Recipient] = []
    seen: set[str] = set()
    for entry in getattr(settings, "OUTPUT_REVIEW_RECIPIENTS", []):
        name, email = parseaddr(entry)
        email = email.strip().lower()
        if "@" not in email or email in seen:
            continue
        if is_client_contact(email):
            # A client address put into the setting by mistake is exactly the
            # disclosure this exists to prevent, so it is dropped, loudly.
            logger.error("%s is a client contact; outputs are never emailed to it", email)
            continue
        seen.add(email)
        recipients.append(Recipient(name=name, email=email))

    if not recipients:
        logger.warning(
            "OUTPUT_REVIEW_RECIPIENTS is empty: outputs for %s cannot be sent", organization
        )
    return recipients


def is_client_contact(email: str) -> bool:
    """Whether an address belongs to any client's contacts — the backstop the
    senders check before anything leaves."""
    from apps.tenancy.context import operator_scope

    with operator_scope():
        return ClientContact.objects.filter(email__iexact=email.strip()).exists()


def addresses_for(organization) -> list[str]:
    """Just the addresses, for callers that take a plain recipient list."""
    return [recipient.email for recipient in recipients_for(organization)]

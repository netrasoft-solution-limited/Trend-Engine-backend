"""Who to send a client's work to.

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
    """Everyone at this client who should receive outputs.

    An empty list is a meaningful answer, not an error — callers decide what to
    do with it, and `publication.delivery` refuses to send on one rather than
    reporting a successful delivery to nobody.
    """
    rows = (
        ClientContact.objects.filter(
            organization=organization, is_active=True, receives_outputs=True
        )
        .order_by("name", "email")
    )

    seen: set[str] = set()
    recipients: list[Recipient] = []
    for row in rows:
        # The unique constraint already prevents this per organisation; the
        # guard is here because a caller may one day merge two clients' lists.
        if row.email in seen:
            continue
        seen.add(row.email)
        recipients.append(Recipient(name=row.name, email=row.email))

    if not recipients:
        logger.warning("%s has no active contacts who receive outputs", organization)
    return recipients


def addresses_for(organization) -> list[str]:
    """Just the addresses, for callers that take a plain recipient list."""
    return [recipient.email for recipient in recipients_for(organization)]

"""The publication gate — PRD §6.9, Arch §9.

Arch §9 opens: "The most safety-critical component in the system, and the one
most likely to be 'simplified' by someone who doesn't see why it exists."

Approval and publication are two states with two actions:

    approved   the operator signed off on an exact version; claims validation
               passed. Nothing client-facing happens.
    published  that version is visible in the client portal right now, and the
               organisation has been notified.

An operator may legitimately approve ahead of an agreed delivery date, or for a
reviewer packet, without wanting it client-visible at that instant. Collapsing
the two makes accidental disclosure a single mis-click — and with the manual
email-forwarding step gone, there is no human backstop left.

This module is the ONLY door. `apps/portal/` resolves content exclusively
through `Publication` and never touches `Output` or `OutputVersion`. That is
asserted by a CI test (Arch §9.3) and by the import-linter contract in
`.importlinter`.
"""
from __future__ import annotations

from django.db import transaction


class PublicationError(RuntimeError):
    """A publish or unpublish that the gate refused."""


class NotApproved(PublicationError):
    """The version has not been approved. Approval comes first, separately."""


class ExpertReviewMissing(PublicationError):
    """Health or scientific content without a recorded expert sign-off.

    Arch §9.2 requires the sign-off BEFORE publication — a stricter gate than
    the internal approval flow, because publication is what reaches the client.
    """


@transaction.atomic
def publish(*, version, organization, actor) -> "object":
    """Make exactly one approved version visible to one tenant.

    Steps, in this order and all inside one transaction:

    1. Refuse unless the version is `approved`.
    2. Refuse unless any required expert review is signed off.
    3. Retire the tenant's currently published version, if any — PRD §6.9
       allows only one visible version per output per tenant at a time.
    4. Create the Publication record carrying a frozen snapshot of the content,
       so the portal never needs to resolve an Output.
    5. Write an immutable audit event: which version, which tenant, by whom, when.
    6. Queue the notification.

    Returns the new Publication.
    """
    raise NotImplementedError(
        "Implement with the Output and Publication models. "
        "Keep the ordering above: the checks precede the write, and the audit "
        "event shares the transaction so a publish can never go unrecorded."
    )


@transaction.atomic
def unpublish(*, publication, actor, reason: str) -> None:
    """Withdraw a published version.

    PRD §6.9 requires publication to be reversible and the reversal audited.
    A withdrawn record must resolve to nothing in the portal — not to an older
    version, which would silently show the client something they were not
    reading before.
    """
    raise NotImplementedError(
        "Set unpublished_at, write the audit event in the same transaction, "
        "and notify — a withdrawal is exactly when silence is worst."
    )


def published_for(organization) -> "object":
    """Every currently visible Publication for one tenant.

    The single read path the portal is allowed to use. Excludes withdrawn
    records. Tenant scoping comes from the bound context, not from an argument
    the caller could get wrong — see apps/tenancy/managers.py.
    """
    raise NotImplementedError(
        "Return Publication.objects.filter(unpublished_at__isnull=True). "
        "The manager applies the tenant filter; do not add one here, or the "
        "default-deny guarantee moves out of the persistence layer."
    )

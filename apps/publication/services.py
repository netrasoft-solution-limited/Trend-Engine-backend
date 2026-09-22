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
through the functions here and never touches `Output` or `OutputVersion`,
enforced by the `gate-is-the-only-door` contract in `.importlinter`.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.operations.models import AuditEvent
from apps.outputs.models import Output, OutputState, OutputVersion

from . import signals
from .models import Publication


class PublicationError(RuntimeError):
    """A publish or unpublish that the gate refused."""


class NotApproved(PublicationError):
    """The version has not been approved. Approval comes first, separately."""


class ExpertReviewMissing(PublicationError):
    """Health or scientific content without a recorded expert sign-off.

    Arch §9.2 requires the sign-off BEFORE publication — a stricter gate than
    the internal approval flow, because publication is what reaches the client.
    """


def _audit(*, kind, actor_label, organization, message, context=None) -> None:
    AuditEvent.objects.create(
        kind=kind,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label,
        organization=organization,
        message=message,
        context=context or {},
    )


@transaction.atomic
def publish(*, version: OutputVersion, organization, actor_label: str) -> Publication:
    """Make exactly one approved version visible to one tenant.

    Ordering matters and is load-bearing:

    1. Refuse unless the version is `approved`.
    2. Refuse unless any required expert review is signed off.
    3. Retire the tenant's currently live publication for this output.
    4. Create the Publication with a frozen snapshot of the content.
    5. Write the audit event — in the same transaction, so a publication can
       never exist unrecorded.
    6. Queue the client notification.
    """
    output: Output = version.output

    if version.state != OutputState.APPROVED:
        raise NotApproved(
            f"{output} v{version.number} is '{version.state}'. "
            f"Approval is a separate action and must happen first."
        )

    if output.requires_expert_review:
        signed = output.expert_reviews.filter(signed_off_at__isnull=False).exists()
        if not signed:
            raise ExpertReviewMissing(
                f"{output.get_type_display()} carries health or scientific content. "
                f"Arch §9.2 requires a recorded expert sign-off before publication, "
                f"which is stricter than approval."
            )

    # Step 3 — retire the incumbent. The partial unique index
    # `uniq_live_publication_per_output` makes this mandatory rather than
    # merely tidy: without it the INSERT below fails.
    now = timezone.now()
    Publication.objects.filter(output=output, unpublished_at__isnull=True).update(
        unpublished_at=now,
        unpublished_by_label=actor_label,
        unpublished_reason="Superseded by a newer published version",
    )

    publication = Publication.objects.create(
        organization=organization,
        output=output,
        version=version,
        version_number=version.number,
        type=output.type,
        type_label=output.get_type_display(),
        title=output.title,
        summary=version.summary,
        body=version.body,
        published_by_label=actor_label,
        notified_at=now,
    )

    output.state = OutputState.PUBLISHED
    output.save(update_fields=["state"])
    version.state = OutputState.PUBLISHED
    version.save(update_fields=["state"])

    _audit(
        kind=AuditEvent.Kind.PUBLICATION,
        actor_label=actor_label,
        organization=organization,
        message=f"Published {output.title} v{version.number} to {organization.name}",
        context={"output_id": output.pk, "version": version.number, "publication_id": publication.pk},
    )

    # Emitted, not called. `portal` is ABOVE `publication` in the layer
    # stack, so importing it here would invert the dependency rule. The
    # receiver runs inside this transaction.
    signals.published.send(sender=Publication, publication=publication)
    return publication


@transaction.atomic
def unpublish(*, publication: Publication, actor_label: str, reason: str) -> None:
    """Withdraw a published version.

    PRD §6.9 requires publication to be reversible and the reversal audited. A
    withdrawn record must resolve to NOTHING in the portal — not to an older
    version, which would silently show the client something they were not
    reading before.
    """
    if publication.unpublished_at is not None:
        return

    publication.unpublished_at = timezone.now()
    publication.unpublished_by_label = actor_label
    publication.unpublished_reason = reason
    publication.save(
        update_fields=["unpublished_at", "unpublished_by_label", "unpublished_reason"]
    )

    output = publication.output
    output.state = OutputState.APPROVED
    output.save(update_fields=["state"])

    _audit(
        kind=AuditEvent.Kind.PUBLICATION,
        actor_label=actor_label,
        organization=publication.organization,
        message=(
            f"Unpublished {publication.title} v{publication.version_number} "
            f"from {publication.organization.name} — {reason}"
        ),
        context={"publication_id": publication.pk, "reason": reason},
    )

    signals.unpublished.send(sender=Publication, publication=publication, reason=reason)


def published_for(organization=None):
    """Every currently visible Publication for the bound tenant.

    The single read path the portal is allowed to use. The tenant filter comes
    from `TenantScopedManager` via the bound context — `organization` is
    accepted only for operator-side callers that have narrowed scope
    explicitly, and is NOT how the portal scopes itself.
    """
    qs = Publication.objects.filter(unpublished_at__isnull=True)
    if organization is not None:
        qs = qs.filter(organization=organization)
    return qs


def publication_by_id(publication_id: int):
    """One live publication, or None. Withdrawn records resolve to None."""
    return published_for().filter(pk=publication_id).first()


#: PRD §6.8 — the client-facing delivery tracker. Every internal
#: pre-publication state collapses to ONE value. The client never learns which
#: internal review stage something is sitting in, and never sees `approved`,
#: because approval is not a promise of delivery.
_CLIENT_FACING_STATE = {
    OutputState.DRAFTING: "in preparation",
    OutputState.DRAFT: "in preparation",
    OutputState.REVIEW_READY: "in preparation",
    OutputState.APPROVED: "in preparation",
    OutputState.PUBLISHED: "published",
    OutputState.DELIVERED: "delivered",
}


def delivery_status_for(organization=None):
    """The delivery tracker rows.

    This lives HERE and not in `apps.portal` for a structural reason: the
    collapse needs to read `Output`, and `.importlinter` forbids
    `portal → outputs`. `publication` may read `outputs` (downward), so the
    portal receives only the collapsed value and the contract holds without an
    exception.
    """
    qs = Output.objects.exclude(state=OutputState.ARCHIVED)
    if organization is not None:
        qs = qs.filter(organization=organization)

    rows = []
    for output in qs.order_by("-updated_at"):
        state = _CLIENT_FACING_STATE.get(output.state)
        if state is None:
            continue
        rows.append(
            {
                "id": output.pk,
                "type": output.get_type_display(),
                "title": output.title,
                "state": state,
                "updated_at": output.updated_at,
            }
        )
    return rows

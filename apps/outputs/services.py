"""Drafting and approval — the states before the publication gate.

`apps/publication/services.py` has been finished and tested for some time and
has had nothing to act on: it refuses anything that is not an APPROVED
`OutputVersion`, and nothing in the system created one. `seed_demo` built them
inline for its fixtures, which is why the gate could be tested at all. This is
that path extracted, so a Celery task, a management command or the Output
Builder screen can all reach it.

The three functions here are the ones `publication.publish()` already assumes
exist:

    draft()                 → an OutputVersion in DRAFT
    approve()               → APPROVED, which is what NotApproved waits for
    record_expert_signoff() → what ExpertReviewMissing waits for

APPROVAL IS NOT PUBLICATION, and keeping that true is the whole reason this
module stops where it does. PRD §6.9: an operator may approve for the internal
record, for a reviewer packet, or ahead of an agreed delivery date, without
wanting it client-visible. `approve()` therefore creates no `Publication` and
calls nothing in `apps.publication` — the two are joined by an operator's
second, deliberate action, never by this code.

Actors are strings, not user objects, for the reason given on
`operations.AuditEvent`: an audit row must outlive the account it names, and
the operator and org realms are two different identity tables.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.operations.models import AuditEvent

from .models import Approval, ExpertReview, Output, OutputState, OutputVersion

logger = logging.getLogger(__name__)


class DraftingError(RuntimeError):
    """The output cannot move to the state asked for."""


def _audit(*, kind: str, actor_label: str, organization, message: str, context: dict) -> None:
    AuditEvent.objects.create(
        kind=kind,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label,
        organization=organization,
        message=message,
        context=context,
    )


@transaction.atomic
def draft(
    organization,
    *,
    type: str,
    title: str,
    sections: list[dict],
    summary: str = "",
    actor_label: str,
    client_profile_version: str = "",
    domain_pack_version: str = "",
) -> OutputVersion:
    """A new version of an output, in DRAFT.

    PRD §6.4: every output records exactly one tenant, one client-profile
    version and one domain-pack version. The last two are carried here rather
    than looked up, because an output must stay explainable against the
    versions it was actually built from — not against whatever is current when
    someone asks about it later.

    Versions are numbered per output and never reused; `uniq_version_per_output`
    enforces that, and the max+1 here runs inside the transaction so two
    simultaneous drafts cannot both take the same number.
    """
    if not sections:
        raise DraftingError("An output with no sections is not a draft, it is an empty row.")

    output, created = Output.objects.get_or_create(
        organization=organization,
        title=title,
        defaults={
            "type": type,
            "state": OutputState.DRAFTING,
            "client_profile_version": client_profile_version,
            "domain_pack_version": domain_pack_version,
        },
    )

    previous = output.versions.order_by("-number").first()
    version = OutputVersion.objects.create(
        organization=organization,
        output=output,
        number=(previous.number + 1) if previous else 1,
        summary=summary,
        body=sections,
        state=OutputState.DRAFT,
        created_by_label=actor_label,
    )

    output.state = OutputState.DRAFT
    output.save(update_fields=["state"])

    _audit(
        kind=AuditEvent.Kind.CONFIG,
        actor_label=actor_label,
        organization=organization,
        message=f"Drafted {output.get_type_display()} '{title}' v{version.number}",
        context={
            "output_id": output.pk,
            "version": version.number,
            "sections": len(sections),
            "new_output": created,
        },
    )
    return version


@transaction.atomic
def approve(version: OutputVersion, *, actor_label: str, stage: str = "operator") -> Approval:
    """Sign off on an exact version. Does NOT make it client-visible.

    The separation is the point, and this function's silence about
    `apps.publication` is how it is kept: publishing is an operator's second
    action, taken knowingly (PRD §6.9).
    """
    if version.state == OutputState.PUBLISHED:
        raise DraftingError(
            f"v{version.number} is already published. Withdraw it before re-approving."
        )

    approval = Approval.objects.create(
        organization=version.organization,
        version=version,
        stage=stage,
        approver_label=actor_label,
    )

    version.state = OutputState.APPROVED
    version.save(update_fields=["state"])
    version.output.state = OutputState.APPROVED
    version.output.save(update_fields=["state"])

    _audit(
        kind=AuditEvent.Kind.APPROVAL,
        actor_label=actor_label,
        organization=version.organization,
        message=(
            f"Approved {version.output.title} v{version.number} — "
            f"internal sign-off only, not published"
        ),
        context={"output_id": version.output.pk, "version": version.number, "stage": stage},
    )
    return approval


@transaction.atomic
def record_expert_signoff(
    output: Output, *, reviewer_label: str, discipline: str = "", note: str = ""
) -> ExpertReview:
    """The recorded sign-off Arch §9.2 requires BEFORE publication.

    Stricter than approval, and deliberately so: approval is an internal
    judgement, while publication puts a health or scientific claim in front of
    a client. `publication.publish()` refuses without this for every type in
    `EXPERT_REVIEW_REQUIRED`.
    """
    if not reviewer_label.strip():
        raise DraftingError(
            "An expert sign-off needs a named reviewer. An anonymous one is not a record."
        )

    review = ExpertReview.objects.create(
        organization=output.organization,
        output=output,
        reviewer_label=reviewer_label,
        discipline=discipline or "Scientific / legal claims",
        note=note,
        signed_off_at=timezone.now(),
    )

    _audit(
        kind=AuditEvent.Kind.REVIEW,
        actor_label=reviewer_label,
        organization=output.organization,
        message=f"Expert sign-off recorded for {output.title}",
        context={"output_id": output.pk, "discipline": review.discipline},
    )
    return review

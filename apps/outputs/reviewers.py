"""The expert reviewer register — one path for the screen and the command.

A sign-off must name an active reviewer from this register, and the operator
recording it cannot be that reviewer. Deactivating keeps past sign-offs intact,
so a reviewer is never deleted.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from apps.operations.models import AuditEvent

from .models import ExpertReviewer


class ReviewerError(ValueError):
    """The register cannot change as asked; the message says why."""


@transaction.atomic
def register(*, name: str, qualification: str, email: str, actor_label: str) -> ExpertReviewer:
    name, qualification, email = name.strip(), qualification.strip(), email.strip().lower()
    if not (name and qualification and email):
        raise ReviewerError("A reviewer needs a name, a qualification and an email.")
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ReviewerError(f"{email} is not an email address.") from exc
    if ExpertReviewer.objects.filter(email=email).exists():
        raise ReviewerError(f"{email} is already registered.")
    if ExpertReviewer.objects.filter(name__iexact=name).exists():
        raise ReviewerError(f"A reviewer named {name} is already registered.")
    reviewer = ExpertReviewer.objects.create(
        name=name, qualification=qualification, email=email, added_by_label=actor_label
    )
    _audit(actor_label, f"Registered expert reviewer {name} ({qualification})", reviewer)
    return reviewer


@transaction.atomic
def set_active(reviewer: ExpertReviewer, active: bool, *, actor_label: str) -> ExpertReviewer:
    if reviewer.is_active != active:
        reviewer.is_active = active
        reviewer.save(update_fields=["is_active"])
        _audit(
            actor_label,
            f"{'Reactivated' if active else 'Deactivated'} expert reviewer {reviewer.name}",
            reviewer,
        )
    return reviewer


def _audit(actor_label: str, message: str, reviewer: ExpertReviewer) -> None:
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label or "system",
        message=message,
        context={"email": reviewer.email},
    )

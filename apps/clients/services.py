"""Creating and switching client profile versions.

A profile is edited by superseding it, never by mutating it. PRD §8 requires
every output to record the client-profile version it was built from, and that
record is worthless if the version it names can change underneath it — a brief
from March would silently start explaining itself in terms of September's
priorities.

So the lifecycle is: `draft()` a new version (optionally copying the current
one), change it freely while nothing cites it, then `activate()` it. From that
moment it is the one new outputs cite, and it stops being edited.
"""
from __future__ import annotations

import logging

from django.db import transaction

from apps.operations.models import AuditEvent

from .models import ClientAsset, ClientProfileVersion, ClientTerm

logger = logging.getLogger(__name__)


class ProfileError(RuntimeError):
    """The profile cannot move to the state asked for."""


def current_for(organization) -> ClientProfileVersion | None:
    """The version new outputs should cite. `None` means this client has not
    been profiled yet, which callers must handle rather than assume away."""
    return ClientProfileVersion.objects.filter(
        organization=organization, is_current=True
    ).first()


@transaction.atomic
def draft(
    organization,
    *,
    label: str = "",
    actor_label: str = "",
    copy_current: bool = True,
) -> ClientProfileVersion:
    """A new, not-yet-live version.

    Copying the current one by default is the behaviour an operator expects:
    profiles are edited far more often than they are written from nothing, and
    starting empty every time invites a half-filled profile going live.

    Numbering runs inside the transaction so two simultaneous drafts cannot
    take the same number — `uniq_profile_version_per_org` would reject the
    second, which is correct but is a worse error than not racing.
    """
    previous = (
        ClientProfileVersion.objects.filter(organization=organization)
        .order_by("-number")
        .first()
    )
    version = ClientProfileVersion.objects.create(
        organization=organization,
        number=(previous.number + 1) if previous else 1,
        label=label,
        created_by_label=actor_label,
        is_current=False,
    )

    source = current_for(organization)
    if copy_current and source is not None:
        _clone_contents(source, version)

    _audit(
        actor_label,
        organization,
        f"Drafted client profile v{version.number}",
        {"profile_version": version.number, "copied_from": source.number if source else None},
    )
    return version


def _clone_contents(source: ClientProfileVersion, target: ClientProfileVersion) -> None:
    """Bulk-created, so a large profile is two queries rather than two hundred."""
    ClientAsset.objects.bulk_create(
        [
            ClientAsset(
                organization=target.organization,
                profile=target,
                kind=asset.kind,
                name=asset.name,
                terms=list(asset.terms),
                weight=asset.weight,
                notes=asset.notes,
            )
            for asset in source.assets.all()
        ]
    )
    ClientTerm.objects.bulk_create(
        [
            ClientTerm(
                organization=target.organization,
                profile=target,
                facet=term.facet,
                name=term.name,
                terms=list(term.terms),
                weight=term.weight,
                notes=term.notes,
            )
            for term in source.terms.all()
        ]
    )


@transaction.atomic
def activate(version: ClientProfileVersion, *, actor_label: str = "") -> ClientProfileVersion:
    """Make this the version new outputs cite.

    Refuses an empty profile. A profile with no assets, audiences or priorities
    matches nothing, which would not fail — it would quietly produce briefs
    with no claims in them, and read as "there was no news this week".
    """
    if version.is_current:
        return version

    if not version.assets.exists() and not version.terms.exists():
        raise ProfileError(
            f"Profile v{version.number} is empty. Activating it would match no "
            f"claims at all, and the briefs would come out empty rather than wrong — "
            f"which is much harder to notice. Add at least one asset or term first."
        )

    superseded = current_for(version.organization)
    if superseded is not None:
        # Cleared first: `one_current_profile_per_org` is a real database
        # constraint, so setting the new one first would fail the insert.
        ClientProfileVersion.objects.filter(pk=superseded.pk).update(is_current=False)

    ClientProfileVersion.objects.filter(pk=version.pk).update(is_current=True)
    version.refresh_from_db()

    _audit(
        actor_label,
        version.organization,
        f"Activated client profile v{version.number}",
        {
            "profile_version": version.number,
            "superseded": superseded.number if superseded else None,
            "assets": version.assets.count(),
            "terms": version.terms.count(),
        },
    )
    return version


def _audit(actor_label: str, organization, message: str, context: dict) -> None:
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label or "system",
        organization=organization,
        message=message,
        context=context,
    )


def to_spec(version) -> dict:
    """A profile version in the shape the `client_profile load` command accepts.

    This is what closes the discovery loop. `discover` derives a draft with flat
    weights and no competitors; without an export the operator has no way to
    correct it short of retyping the whole thing, and a draft nobody can edit is
    a draft nobody activates.

    Round-trips: loading this back produces an equivalent profile, one version
    later.
    """
    from .models import ClientTerm

    facet_keys = {
        ClientTerm.Facet.AUDIENCE: "audiences",
        ClientTerm.Facet.PRIORITY: "priorities",
        ClientTerm.Facet.COMPETITOR: "competitors",
    }
    spec: dict = {
        "label": version.label,
        "assets": [
            {
                "name": asset.name,
                "kind": asset.kind,
                "weight": asset.weight,
                "terms": list(asset.terms),
                **({"notes": asset.notes} if asset.notes else {}),
            }
            for asset in version.assets.all()
        ],
        "audiences": [],
        "priorities": [],
        "competitors": [],
    }
    for term in version.terms.all():
        spec[facet_keys[term.facet]].append(
            {
                "name": term.name,
                "weight": term.weight,
                "terms": list(term.terms),
                **({"notes": term.notes} if term.notes else {}),
            }
        )
    return spec

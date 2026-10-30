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
                family=asset.family,
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


#: Spec keys for the three term facets, shared by `load_spec` and `to_spec`.
SPEC_FACETS = {
    "audiences": ClientTerm.Facet.AUDIENCE,
    "priorities": ClientTerm.Facet.PRIORITY,
    "competitors": ClientTerm.Facet.COMPETITOR,
}


@transaction.atomic
def load_spec(
    organization,
    spec: dict,
    *,
    actor_label: str = "",
    label: str = "",
    copy_current: bool = False,
) -> ClientProfileVersion:
    """Draft a new version from a spec — the shape `to_spec` emits.

    Never activates. The command's `--activate` and the screen's activate button
    are separate, deliberate steps, so an imported file is always looked at
    before anything cites it.

    `copy_current=False` is the default because a JSON file is the whole
    profile: merging it onto the previous version would make "what is in this
    profile?" a question about history rather than about the file. A CSV
    product list is the opposite case — it is only the products — so its caller
    copies the live profile and lets duplicate names fall away here.
    """
    version = draft(
        organization,
        label=label or spec.get("label", ""),
        actor_label=actor_label,
        copy_current=copy_current,
    )
    existing = {name.lower() for name in version.assets.values_list("name", flat=True)}

    for row in spec.get("assets", []):
        name = str(row.get("name") or "").strip()
        terms = [str(t).strip() for t in row.get("terms") or [] if str(t).strip()]
        if not name or name.lower() in existing:
            continue
        existing.add(name.lower())
        kind = row.get("kind") or ClientAsset.Kind.PRODUCT
        ClientAsset.objects.create(
            organization=organization,
            profile=version,
            name=name,
            kind=kind if kind in ClientAsset.Kind.values else ClientAsset.Kind.PRODUCT,
            # A row with no terms would match nothing; its own name is the
            # least-wrong vocabulary until the operator adds better words.
            terms=terms or [name.lower()],
            weight=_clamp(row.get("weight", 50)),
            notes=str(row.get("notes") or ""),
            family=str(row.get("family") or "").strip()[:120],
        )

    for key, facet in SPEC_FACETS.items():
        for row in spec.get(key, []):
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            terms = [str(t).strip() for t in row.get("terms") or [] if str(t).strip()]
            ClientTerm.objects.create(
                organization=organization,
                profile=version,
                facet=facet,
                name=name,
                terms=terms or [name.lower()],
                weight=_clamp(row.get("weight", 50)),
                notes=str(row.get("notes") or ""),
            )
    return version


def spec_from_csv(text: str) -> dict:
    """A product list exported from a spreadsheet, as a spec of assets.

    The columns are found by header, case-insensitively: `name` is required;
    `kind`, `terms`, `weight`, `url` and `notes` are optional (`url` and
    `notes` are kept together in the entry's notes). Terms are separated by `;` or
    `|`, because a comma is the column separator. A row whose `kind` is
    audience, priority or competitor lands on that facet instead, so one sheet
    can carry a whole profile if the operator wants it to.

    This is the route for a storefront that refuses to be read — jarrow.com
    rate-limits the catalogue crawl — and for any client whose real list lives
    in a spreadsheet rather than on a shop.
    """
    import csv
    import io
    import re

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ProfileError("That file is empty.")
    columns = {(f or "").strip().lower(): f for f in reader.fieldnames}
    if "name" not in columns:
        raise ProfileError(
            "The product list needs a 'name' column. Optional columns: kind, terms "
            "(separated by ; or |), weight."
        )

    facet_kinds = {"audience": "audiences", "priority": "priorities", "competitor": "competitors"}
    spec: dict = {"assets": [], "audiences": [], "priorities": [], "competitors": []}
    def cell(row: dict, key: str) -> str:
        return str(row.get(columns[key]) or "").strip() if key in columns else ""

    for row in reader:
        name = cell(row, "name")
        if not name:
            continue
        notes = " · ".join(part for part in (cell(row, "url"), cell(row, "notes")) if part)
        entry = {
            **({"family": cell(row, "family")} if cell(row, "family") else {}),
            "name": name,
            "terms": [t.strip() for t in re.split(r"[;|]", cell(row, "terms")) if t.strip()],
            "weight": cell(row, "weight") or 50,
            **({"notes": notes} if notes else {}),
        }
        kind = cell(row, "kind").lower()
        if kind in facet_kinds:
            spec[facet_kinds[kind]].append(entry)
        else:
            spec["assets"].append({**entry, "kind": kind or ClientAsset.Kind.PRODUCT})

    if not any(spec.values()):
        raise ProfileError("No rows with a name were found in that file.")
    return spec


def _clamp(raw, *, default: int = 50) -> int:
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return default


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
                **({"family": asset.family} if asset.family else {}),
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

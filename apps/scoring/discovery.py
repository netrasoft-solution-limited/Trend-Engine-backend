"""Discovery: ingredients the window's conversations raise that a client lacks.

Mark, 15 Nov: "please demonstrate that discovery can surface opportunities
beyond a fixed list rather than only refreshing those three names."

What it does:

  1. Takes the subjects of every collected, non-promotional claim published
     in the research window, the most discussed first.
  2. Asks the model once to name which subjects are supplement ingredients and
     to group spellings of the same one (cheap, one call, logged with its cost).
  3. Counts distinct items, creators and channels per ingredient from the
     claims themselves, never from the model.
  4. Drops anything the client's live catalogue already names, and anything
     already a candidate, then keeps the best supported.

The result is a list for an analyst, not a recommendation: nothing becomes a
candidate, or reaches a brief as one, until someone adds it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from pydantic import BaseModel, Field

from apps.clients import services as client_services
from apps.clients.models import ClientAsset
from apps.enrichment.llm.client import LLMClient, cached_system, extraction_tier
from apps.enrichment.models import Claim, ModelRun
from apps.operations.models import AuditEvent

from . import opportunities
from .models import Candidate, DiscoveredIngredient

logger = logging.getLogger(__name__)

PROMPT_VERSION = "discovery/2026-11-15"
WINDOW_DAYS = 30
#: Subjects sent to the model, most discussed first. Subjects are specific
#: ("berberine and insulin sensitivity"), so most are raised by one item and
#: the counting happens after the model groups them; 1,500 short lines is
#: still one inexpensive call.
MAX_SUBJECTS = 1500
#: Subjects per call, so each answer stays well under its ceiling.
CHUNK = 300
MIN_ITEMS = 2
MIN_CREATORS = 2
KEEP = 10

INSTRUCTIONS = """You name supplement ingredients.

You are given numbered subjects of claims people made in podcasts, videos and
posts. Return the subjects that name a specific ingredient that is, or could
be, sold as a dietary supplement: a nutrient or a specific form of one, a
compound, a botanical or extract, a probiotic strain or species, an amino
acid, a fibre.

- Group subjects that are the same ingredient under one common name, e.g.
  "urolithin-a" and "Urolithin A (Mitopure)" are "Urolithin A".
- Keep specific forms apart when the form is the point: "Magnesium glycinate"
  and "Magnesium L-threonate" are different ingredients.
- Leave out foods and diets (pomegranate, yogurt, fasting), drugs and
  hormones given by prescription, conditions, behaviours, brands, and generic
  categories ("supplements", "probiotics", "vitamins", "protein").
- Use only the subject numbers given. Return nothing you are not sure of."""


class Named(BaseModel):
    ingredient: str = Field(description="The common ingredient name.")
    subjects: list[int] = Field(description="The numbers of the subjects that name it.")


class Naming(BaseModel):
    ingredients: list[Named] = Field(default_factory=list)


def window() -> tuple:
    end = timezone.localdate()
    return end - timedelta(days=WINDOW_DAYS), end


def subject_claims(start, end) -> dict[str, list[Claim]]:
    """Normalised subject → its claims, the most discussed first."""
    claims = (
        Claim.objects.filter(is_sponsored=False)
        .exclude(content_item__duplicate_status="confirmed")
        .annotate(said=Coalesce("content_item__published_at", "content_item__discovered_at"))
        .filter(said__date__gte=start, said__date__lte=end)
        .exclude(subject="")
        .select_related("content_item__source")
    )
    grouped: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        grouped[" ".join(claim.subject.lower().split())].append(claim)
    ranked = sorted(grouped.items(), key=lambda kv: (-len({c.content_item_id for c in kv[1]}),
                                                     kv[0]))
    return dict(ranked[:MAX_SUBJECTS])


def _catalogue_text(organization) -> list[str]:
    profile = client_services.current_for(organization)
    if profile is None:
        return []
    return [
        f"{a.name} {' '.join(a.terms)} {a.notes}".lower()
        for a in profile.assets.exclude(kind=ClientAsset.Kind.CATEGORY)
    ]


def _known(organization) -> list[str]:
    names = []
    for candidate in Candidate.objects.filter(organization=organization):
        names += [n.lower() for n in candidate.names]
    return names


def _sold(name: str, catalogue: list[str]) -> bool:
    pattern = opportunities._pattern(name)
    return any(pattern.search(text) for text in catalogue)


def discover(organization, *, client: LLMClient | None = None,
             actor_label: str = "system") -> list[DiscoveredIngredient]:
    """Propose ingredients for this client from the current window. One call."""
    start, end = window()
    subjects = subject_claims(start, end)
    if not subjects:
        return []
    numbered = list(subjects)
    llm = client or LLMClient()
    # Ingredient name (lower case) → (display name, subject numbers), merged
    # across chunks: one call over every subject outgrew its answer ceiling.
    named_subjects: dict[str, tuple[str, set[int]]] = {}
    for first in range(0, len(numbered), CHUNK):
        chunk = numbered[first:first + CHUNK]
        listing = "\n".join(f"{n}. {s}" for n, s in enumerate(chunk, start=1))
        naming = llm.structured(
            schema=Naming,
            tier=extraction_tier(),
            purpose=ModelRun.Purpose.DISCOVERY,
            prompt_version=PROMPT_VERSION,
            system=cached_system(INSTRUCTIONS),
            user=f"Subjects:\n{listing}",
        )
        for named in naming.ingredients:
            name = " ".join(named.ingredient.split())
            if not name:
                continue
            shown, ids = named_subjects.setdefault(name.lower(), (name, set()))
            ids.update(first + i for i in named.subjects if 1 <= i <= len(chunk))

    catalogue = _catalogue_text(organization)
    known = _known(organization)
    proposals = []
    for name, ids in named_subjects.values():
        rows = [c for i in sorted(ids) for c in subjects[numbered[i - 1]]]
        if not rows or name.lower() in known or _sold(name, catalogue):
            continue
        items = {c.content_item_id for c in rows}
        creators = {
            (c.content_item.creator or c.content_item.source.name).strip().lower() for c in rows
        }
        if len(items) < MIN_ITEMS or len(creators) < MIN_CREATORS:
            continue
        channels = sorted({c.content_item.platform for c in rows})
        proposals.append((name, items, creators, channels, rows, sorted(ids)))

    proposals.sort(key=lambda p: (-len(p[3]), -len(p[1]), p[0]))
    now = timezone.now()
    # Only the writes are atomic. The model calls above are not: a call that
    # fails inside a transaction rolls back its own cost record, and the bill
    # still arrives (Mark, 20 Nov reconciliation).
    with transaction.atomic():
        return _save(organization, proposals, numbered, len(subjects), start, end, now,
                     actor_label)


def _save(organization, proposals, numbered, subject_count, start, end, now, actor_label):
    # One list per run: the previous unassessed proposals are replaced.
    DiscoveredIngredient.objects.filter(
        organization=organization, status=DiscoveredIngredient.Status.NEW
    ).delete()
    found = [
        DiscoveredIngredient.objects.create(
            organization=organization,
            name=name,
            items=len(items),
            creators=len(creators),
            channels=channels,
            subjects=[numbered[i - 1] for i in subject_ids if 1 <= i <= len(numbered)],
            claim_ids=[c.pk for c in rows[:12]],
            window_start=start,
            window_end=end,
            discovered_at=now,
        )
        for name, items, creators, channels, rows, subject_ids in proposals[:KEEP]
    ]
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.SYSTEM,
        actor_realm=AuditEvent.Realm.SYSTEM,
        actor_label=actor_label,
        organization=organization,
        message=(
            f"Discovery read {subject_count} subject(s) and proposed {len(found)} "
            f"ingredient(s) {organization.name} does not appear to sell."
        ),
        context={"proposed": [d.name for d in found]},
    )
    return found


def latest(organization) -> list[DiscoveredIngredient]:
    """The most recent run's proposals that are still open or were added."""
    newest = (
        DiscoveredIngredient.objects.filter(organization=organization)
        .order_by("-discovered_at").first()
    )
    if newest is None:
        return []
    return list(
        DiscoveredIngredient.objects.filter(
            organization=organization, discovered_at=newest.discovered_at
        ).exclude(status=DiscoveredIngredient.Status.DISMISSED).order_by("-items", "name")
    )


def add_as_candidate(found: DiscoveredIngredient, *, actor_label: str) -> Candidate:
    """Make a proposal a candidate, with its current evidence collected."""
    candidate = opportunities.create(
        found.organization, name=found.name, actor_label=actor_label,
        theme=", ".join(found.subjects[:3]),
    )
    opportunities.collect(candidate, actor_label=actor_label)
    found.status = DiscoveredIngredient.Status.ADDED
    found.candidate = candidate
    found.save(update_fields=["status", "candidate"])
    return candidate


def dismiss(found: DiscoveredIngredient, *, actor_label: str) -> None:
    found.status = DiscoveredIngredient.Status.DISMISSED
    found.save(update_fields=["status"])
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label,
        organization=found.organization,
        message=f"Dismissed discovered ingredient {found.name}",
        context={"discovered_id": found.pk},
    )

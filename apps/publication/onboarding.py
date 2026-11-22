"""Setting up a new brand so its research cycle runs like Jarrow's.

Mark, 20 Nov: "Repeat that workflow for a second brand without rebuilding the
research and report process outside the application." The steps, each shown
on the brand's setup page with its state:

  1. The brand exists (Clients page).
  2. A catalogue: a storefront read or an inventory import, as a draft profile.
  3. Listening families: every listing in one family, each family with its
     listening terms and one search query. An inventory CSV brings these with
     it; a storefront read does not, so `assign_families` proposes them in one
     model call, written to the draft for an operator to review.
  4. The profile made live (client page).
  5. Sources: one standing search per family on YouTube, TikTok, Instagram and
     Reddit (`create_sources`), plus the shared podcast feeds, all linked to
     the brand. They poll when the brand's cycle runs, not on a timer.
  6. The first research cycle.
"""
from __future__ import annotations

import logging

from django.db import transaction
from pydantic import BaseModel, Field

from apps.clients import services as client_services
from apps.clients.models import BrandSource, ClientAsset, ClientProfileVersion
from apps.enrichment.llm.client import LLMClient, cached_system, extraction_tier
from apps.enrichment.models import ModelRun
from apps.operations.models import AuditEvent
from apps.sources.models import Source

logger = logging.getLogger(__name__)

PROMPT_VERSION = "families/2026-11-22"
QUERY_NOTE = "Search query: "
PLATFORMS = (
    ("youtube", "YouTube"),
    ("tiktok", "TikTok"),
    ("instagram", "Instagram"),
    ("reddit", "Reddit"),
)


class OnboardingError(RuntimeError):
    pass


# ── Status ──────────────────────────────────────────────────────────────────


def latest_draft(organization) -> ClientProfileVersion | None:
    return (
        ClientProfileVersion.objects.filter(organization=organization, is_current=False)
        .order_by("-number").first()
    )


def _listings(version) -> list[ClientAsset]:
    return list(version.assets.exclude(kind=ClientAsset.Kind.CATEGORY)) if version else []


def status(organization) -> dict:
    live = client_services.current_for(organization)
    draft = latest_draft(organization)
    working = draft if draft and (not live or draft.number > live.number) else live
    listings = _listings(working)
    unassigned = [a for a in listings if not a.family]
    families = families_of(working)
    linked = BrandSource.objects.filter(organization=organization).count()
    from .models import ResearchCycle

    cycles = ResearchCycle.objects.filter(organization=organization).count()
    return {
        "live": live,
        "working": working,
        "working_is_draft": working is not None and working is draft and working is not live,
        "listings": len(listings),
        "unassigned": len(unassigned),
        "families": families,
        "linked": linked,
        "cycles": cycles,
        "steps": [
            ("Catalogue imported or read", bool(listings)),
            ("Every listing in a listening family", bool(listings) and not unassigned),
            ("Profile live", live is not None and bool(_listings(live))),
            ("Sources created and linked", linked > 0),
            ("First research cycle run", cycles > 0),
        ],
    }


def families_of(version) -> list[dict]:
    if version is None:
        return []
    out = []
    for category in version.assets.filter(kind=ClientAsset.Kind.CATEGORY).order_by("name"):
        query = ""
        for line in (category.notes or "").splitlines():
            if line.startswith(QUERY_NOTE):
                query = line[len(QUERY_NOTE):].strip()
        out.append({
            "name": category.family or category.name,
            "terms": category.terms,
            "query": query or (category.terms[0] if category.terms else category.name),
            "listings": version.assets.exclude(kind=ClientAsset.Kind.CATEGORY)
            .filter(family=category.family or category.name).count(),
        })
    return out


# ── Families ────────────────────────────────────────────────────────────────

INSTRUCTIONS = """You organise a supplement brand's catalogue into listening \
families: the 8 to 15 health areas its products serve, used to decide what to \
listen for in podcasts, videos and social posts.

For each family give:
- `name`: a short plain name, e.g. "Bone health", "Sleep and relaxation".
- `terms`: 4 to 8 words or phrases people use when talking about that area or its \
ingredients, lower case, e.g. "bone density", "vitamin k2".
- `query`: one search query of 2 to 4 words that would find current discussion \
of the area, e.g. "vitamin k2 d3".
- `listings`: the numbers of the listings that belong to it.

Every listing belongs to exactly one family. Use only the listing numbers given."""


class Family(BaseModel):
    name: str
    terms: list[str] = Field(default_factory=list)
    query: str
    listings: list[int] = Field(default_factory=list)


class Families(BaseModel):
    families: list[Family] = Field(default_factory=list)


def assign_families(organization, *, actor_label: str, client=None) -> dict:
    """Propose families for a draft profile's listings that have none. One call."""
    draft = latest_draft(organization)
    live = client_services.current_for(organization)
    if draft is None or (live is not None and draft.number < live.number):
        raise OnboardingError(
            "Families are written to a draft profile. Read the storefront or import the "
            "catalogue first (or start a draft from the live profile), then try again."
        )
    listings = [a for a in _listings(draft) if not a.family]
    if not listings:
        raise OnboardingError("Every listing in the draft already has a family.")
    numbered = "\n".join(f"{n}. {a.name}" + (f" — {a.notes[:160]}" if a.notes else "")
                         for n, a in enumerate(listings, start=1))
    answer = (client or LLMClient()).structured(
        schema=Families,
        tier=extraction_tier(),
        purpose=ModelRun.Purpose.DISCOVERY,
        prompt_version=PROMPT_VERSION,
        system=cached_system(INSTRUCTIONS),
        user=f"Brand: {organization.name}\n\nListings:\n{numbered[:60000]}",
    )
    assigned = 0
    with transaction.atomic():
        for family in answer.families:
            name = " ".join(family.name.split())[:120]
            members = [listings[i - 1] for i in family.listings if 1 <= i <= len(listings)]
            if not name or not members:
                continue
            terms = list(dict.fromkeys(t.strip().lower() for t in family.terms if t.strip()))[:8]
            category, _ = ClientAsset.objects.get_or_create(
                organization=organization, profile=draft, kind=ClientAsset.Kind.CATEGORY,
                name=name, defaults={"family": name, "terms": terms},
            )
            category.family, category.terms = name, terms or category.terms
            category.notes = (f"{QUERY_NOTE}{family.query.strip()[:80]}\n"
                              "Proposed by the model; review.")
            category.save()
            for asset in members:
                if not asset.family:
                    asset.family = name
                    asset.save(update_fields=["family"])
                    assigned += 1
        AuditEvent.objects.create(
            kind=AuditEvent.Kind.CONFIG, actor_realm=AuditEvent.Realm.OPERATOR,
            actor_label=actor_label, organization=organization,
            message=f"Proposed {len(answer.families)} listening families for draft "
                    f"v{draft.number}; {assigned} listing(s) assigned",
            context={"version": draft.number},
        )
    left = sum(1 for a in _listings(draft) if not a.family)
    return {"families": len(answer.families), "assigned": assigned, "unassigned": left}


# ── Sources ─────────────────────────────────────────────────────────────────


def _policy_for(route: str, platform: str = ""):
    sources = Source.objects.filter(route=route, policy__isnull=False)
    if platform:
        sources = sources.filter(config__platform=platform)
    found = sources.order_by("-pk").first()
    return found.policy if found else None


@transaction.atomic
def create_sources(organization, *, actor_label: str) -> dict:
    """One standing search per live family and platform, plus the shared podcasts,
    linked to the brand. Polled when the brand's cycle runs."""
    live = client_services.current_for(organization)
    families = families_of(live)
    if not families:
        raise OnboardingError("Make a profile with listening families live first.")
    created, linked = [], 0
    for family in families:
        for platform, label in PLATFORMS:
            route = "youtube" if platform == "youtube" else "social"
            policy = _policy_for(route, "" if route == "youtube" else platform)
            config = (
                {"limit": 2, "family": family["name"], "queries": [family["query"]],
                 "date_filter": "month"}
                if route == "youtube"
                else {"platform": platform, "queries": [family["query"]], "family": family["name"],
                      **({"limit": 20} if platform == "reddit" else {})}
            )
            name = f"{organization.name} · {label} — {family['name']}"[:200]
            source, made = Source.objects.get_or_create(
                name=name,
                defaults={"route": route, "config": config, "policy": policy,
                          "poll_interval_minutes": 0},
            )
            if made:
                created.append(source.pk)
            _, new = BrandSource.objects.get_or_create(
                organization=organization, source=source, defaults={"added_by_label": actor_label})
            linked += int(new)
    for podcast in Source.objects.filter(route=Source.Route.PODCAST, status=Source.Status.ACTIVE):
        _, new = BrandSource.objects.get_or_create(
            organization=organization, source=podcast, defaults={"added_by_label": actor_label})
        linked += int(new)
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.CONFIG, actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label, organization=organization,
        message=f"Created {len(created)} source(s) from {len(families)} families; "
                f"{linked} newly linked to {organization.name}",
        context={"sources": created},
    )
    return {"families": len(families), "created": len(created), "linked": linked}

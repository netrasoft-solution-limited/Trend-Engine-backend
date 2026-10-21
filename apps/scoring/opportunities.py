"""New-product opportunities — Mark's second Jarrow workflow (20 Oct).

An analyst names a candidate the client may not sell; this module answers the
questions his New Product Opportunity Brief answers for each one:

  · IS IT A GAP? `check_gap` searches every name and alias across the live
    profile's listing names, terms and notes. A public catalogue cannot see
    retail-only lines or the pipeline, so the result stays "not found in N
    listings" until the client confirms it (`internal_check`).
  · WHERE WOULD IT SIT? `suggest_adjacent` proposes listings that share the
    candidate's theme; the analyst keeps or edits them.
  · WHAT IS THE EVIDENCE? Observations, attached by hand, each traceable.
  · WHAT SHOULD THE CLIENT DO? A commercial opening, a test, a next step and
    the gate it must pass. Drafted by one grounded model call or written by the
    analyst, and approved before any brief can use it.

The ranking is commercial judgement and is set by hand (`priority`). Nothing
here computes a market size or calls a candidate a winner.
"""
from __future__ import annotations

import logging
import re

from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel, Field

from apps.clients import services as client_services
from apps.clients.models import ClientAsset
from apps.enrichment.llm.client import (
    LLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    cached_system,
    recommendation_tier,
)
from apps.enrichment.llm.costs import CostCapExceeded
from apps.enrichment.models import ModelRun
from apps.evidence.models import Observation
from apps.operations.models import AuditEvent

from .models import Candidate

logger = logging.getLogger(__name__)

PROMPT_VERSION = "opportunity/2026-10-21"

#: Fields an analyst edits on the candidate page.
EDITABLE = ("opening", "why_it_matters", "test_plan", "next_step", "gate", "limits",
            "internal_note", "theme")


class OpportunityError(RuntimeError):
    """The candidate cannot be created, drafted or approved as asked."""


def split_names(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\n]", text or "") if p.strip()]


@transaction.atomic
def create(organization, *, name: str, aliases: str = "", theme: str = "",
           actor_label: str) -> Candidate:
    name = name.strip()
    if not name:
        raise OpportunityError("Give the candidate a name.")
    if Candidate.objects.filter(organization=organization, name__iexact=name).exists():
        raise OpportunityError(f"{name} is already a candidate.")
    last = Candidate.objects.filter(organization=organization).order_by("-priority").first()
    candidate = Candidate.objects.create(
        organization=organization, name=name, aliases=split_names(aliases),
        theme=theme.strip(), priority=(last.priority + 1) if last else 1,
        created_by_label=actor_label,
    )
    _audit(organization, actor_label, f"Created candidate {name}", {"candidate_id": candidate.pk})
    check_gap(candidate, actor_label=actor_label)
    candidate.adjacent = suggest_adjacent(candidate)
    candidate.save(update_fields=["adjacent"])
    return candidate


def _pattern(term: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


def check_gap(candidate: Candidate, *, actor_label: str = "") -> dict:
    """Every listing in the live profile that names the candidate, and where."""
    profile = client_services.current_for(candidate.organization)
    if profile is None:
        raise OpportunityError(
            f"{candidate.organization.name} has no live profile, so there is no catalogue "
            f"to check {candidate.name} against."
        )
    listings = list(profile.assets.exclude(kind=ClientAsset.Kind.CATEGORY))
    patterns = [(n, _pattern(n)) for n in candidate.names]
    found = []
    for asset in listings:
        fields = {"name": asset.name, "terms": " ".join(asset.terms), "notes": asset.notes}
        for field, value in fields.items():
            hit = next((n for n, p in patterns if p.search(value.lower())), None)
            if hit:
                found.append({"name": asset.name, "family": asset.family,
                              "field": field, "term": hit})
                break
    result = {
        "checked_at": timezone.now().isoformat(),
        "profile": profile.citation,
        "listings": len(listings),
        "found": found,
    }
    candidate.gap_check = result
    candidate.save(update_fields=["gap_check", "updated_at"])
    _audit(candidate.organization, actor_label, f"Gap check on {candidate.name}",
           {"candidate_id": candidate.pk, "found": len(found), "listings": len(listings)})
    return result


def gap_sentence(candidate: Candidate) -> str:
    gap = candidate.gap_check or {}
    if not gap:
        return "Not yet checked against the catalogue."
    if gap["found"]:
        names = ", ".join(sorted({f["name"] for f in gap["found"]}))
        return f"Found in {len(gap['found'])} of {gap['listings']} listings: {names}."
    return f"Not found in the titles, terms or notes of {gap['listings']} listings."


def suggest_adjacent(candidate: Candidate, limit: int = 4) -> list[str]:
    """Listings that share a theme word (4+ letters) in their family, name or terms."""
    profile = client_services.current_for(candidate.organization)
    words = {w for w in re.findall(r"[a-z]{4,}", candidate.theme.lower())}
    if profile is None or not words:
        return []
    scored = []
    for asset in profile.assets.exclude(kind=ClientAsset.Kind.CATEGORY):
        haystack = " ".join([asset.family, asset.name, *asset.terms]).lower()
        score = sum(1 for w in words if w in haystack)
        if score:
            scored.append((-score, asset.name))
    return [name for _, name in sorted(scored)[:limit]]


def set_adjacent(candidate: Candidate, text: str, *, actor_label: str) -> list[str]:
    """Keep only names that are listings in the live profile; return the rest.

    Listing names contain commas ("NMN NAD+ Precursor, 300 mg"), so known names
    are found in the text first, longest first, and only what is left over is
    split on separators.
    """
    profile = client_services.current_for(candidate.organization)
    known = sorted(
        (a.name for a in profile.assets.all()) if profile else [], key=len, reverse=True
    )
    rest, found = text or "", []
    for name in known:
        at = rest.lower().find(name.lower())
        if at >= 0:
            found.append((at, name))
            rest = rest[:at] + " ; " + rest[at + len(name):]
    candidate.adjacent = [name for _, name in sorted(found)]
    _unapprove(candidate)
    candidate.save(update_fields=["adjacent", "approved_at", "approved_by_label", "updated_at"])
    _audit(candidate.organization, actor_label, f"Set adjacent listings on {candidate.name}",
           {"candidate_id": candidate.pk, "adjacent": candidate.adjacent})
    return [n for n in split_names(rest) if n.strip(" ;,")]


def attach(candidate: Candidate, ids: list[int], *, actor_label: str) -> tuple[list, list]:
    found = list(Observation.objects.filter(pk__in=ids))
    candidate.observations.add(*found)
    attached = [o.pk for o in found]
    if attached and _unapprove(candidate):
        candidate.save(update_fields=["approved_at", "approved_by_label", "updated_at"])
    if attached:
        _audit(candidate.organization, actor_label,
               f"Attached {len(attached)} observation(s) to {candidate.name}",
               {"candidate_id": candidate.pk, "observation_ids": attached})
    return attached, [i for i in ids if i not in attached]


def detach(candidate: Candidate, observation_id: int, *, actor_label: str) -> None:
    candidate.observations.remove(observation_id)
    if _unapprove(candidate):
        candidate.save(update_fields=["approved_at", "approved_by_label", "updated_at"])
    _audit(candidate.organization, actor_label, f"Detached O{observation_id} from {candidate.name}",
           {"candidate_id": candidate.pk, "observation_id": observation_id})


def _unapprove(candidate: Candidate) -> bool:
    """Anything the brief would print changed: the approval no longer covers it."""
    was = candidate.is_approved
    candidate.approved_at, candidate.approved_by_label = None, ""
    return was


def edit(candidate: Candidate, raw: dict, *, actor_label: str) -> Candidate:
    """An edit to an approved candidate withdraws the approval."""
    for key in EDITABLE:
        if key in raw:
            setattr(candidate, key, str(raw[key] or "").strip())
    if raw.get("status") in Candidate.Status.values:
        candidate.status = raw["status"]
    if raw.get("test_type", None) in Candidate.TestType.values:
        candidate.test_type = raw["test_type"]
    if raw.get("internal_check") in Candidate.InternalCheck.values:
        candidate.internal_check = raw["internal_check"]
    if str(raw.get("priority") or "").isdigit():
        candidate.priority = int(raw["priority"])
    was_approved = candidate.is_approved
    candidate.approved_at, candidate.approved_by_label = None, ""
    candidate.save()
    _audit(candidate.organization, actor_label, f"Edited candidate {candidate.name}",
           {"candidate_id": candidate.pk, "approval_withdrawn": was_approved})
    return candidate


def approve(candidate: Candidate, *, actor_label: str) -> Candidate:
    missing = [label for label, value in (
        ("the commercial opening", candidate.opening),
        ("the next step", candidate.next_step),
        ("the gate", candidate.gate),
    ) if not value.strip()]
    if not candidate.gap_check:
        missing.append("a catalogue gap check")
    if candidate.status != Candidate.Status.REJECTED and not candidate.observations.exists():
        missing.append("at least one observation")
    if missing:
        raise OpportunityError(f"{candidate.name} needs {', '.join(missing)} before approval.")
    candidate.approved_at, candidate.approved_by_label = timezone.now(), actor_label
    candidate.save(update_fields=["approved_at", "approved_by_label", "updated_at"])
    _audit(candidate.organization, actor_label, f"Approved candidate {candidate.name}",
           {"candidate_id": candidate.pk, "status": candidate.status}, kind=AuditEvent.Kind.REVIEW)
    return candidate


# ── Drafting the case ────────────────────────────────────────────────────────


class ProposedOpportunity(BaseModel):
    opening: str = Field(description="One sentence: the commercial opening for this client.")
    why_it_matters: str = Field(
        description="Two to four sentences on why the evidence could matter commercially, "
        "citing observations as O-numbers. Never claim efficacy."
    )
    test_type: str = Field(description="One of: customer, supplier, price, monitor.")
    test_plan: str = Field(description="What to test next, concretely, in two to four sentences.")
    next_step: str = Field(description="The next step in under fifteen words.")
    gate: str = Field(description="What must be true before advancing, in one or two sentences.")
    limits: str = Field(description="What this evidence does not show.")


INSTRUCTIONS = """\
You are a commercial analyst at Pure Play Ecommerce Consulting assessing whether
a supplement brand should test a new ingredient. You are given the candidate,
the catalogue gap check, the listings it would sit beside, and numbered
observations (O-numbers), each with its date, kind of figure and labels.

Rules:
- Use only the observations given. Cite them as O-numbers. Invent nothing.
- Keep figure kinds apart: a search estimate is a rolling monthly average; TikTok
  Shop counts are global lifetime totals; engagement is cumulative. Never add
  figures of different kinds, and never call a lifetime count growth.
- An advertisement or brand content shows response to marketing, not independent
  demand. Say so where it matters.
- A gap in the public catalogue is not a confirmed gap until the client checks
  retail-only lines and its pipeline.
- This is commercial research, not a launch recommendation. Make no health claims.
- Prefer a monitor recommendation when the evidence is old, single-channel or
  mostly paid.
"""


def _render(candidate: Candidate, observations: list[Observation]) -> str:
    lines = [
        f"Client: {candidate.organization.name}",
        f"Candidate: {candidate.name}"
        + (f" (also sold as {', '.join(candidate.aliases)})" if candidate.aliases else ""),
        f"Catalogue: {gap_sentence(candidate)}",
        f"Would sit beside: {', '.join(candidate.adjacent) or 'not chosen'}",
        "",
        "Observations:",
    ]
    for o in observations:
        figure = (
            f" Figure: {o.metric_label or o.metric_value} ({o.get_metric_kind_display()})"
            if o.metric_kind else ""
        )
        search = (
            f" Query '{o.query}', {o.provider}, {o.geography}, {o.period}."
            if o.metric_kind in Observation.SEARCH_KINDS else ""
        )
        labels = f" Labels: {', '.join(o.labels)}." if o.labels else ""
        lines.append(
            f"O{o.pk}. {o.get_source_type_display()}, published "
            f"{o.published_at or 'unknown'}, observed {o.observed_at}: {o.title}. "
            f"{o.excerpt}{figure}{search}{labels} {o.limitations}".strip()
        )
    return "\n".join(lines)


def draft(candidate: Candidate, *, actor_label: str, client: LLMClient | None = None) -> Candidate:
    """Fill the case from the attached observations. The analyst then edits it."""
    observations = list(candidate.observations.order_by("published_at", "pk"))
    if not observations:
        raise OpportunityError(f"Attach observations to {candidate.name} before drafting.")
    if not candidate.gap_check:
        check_gap(candidate, actor_label=actor_label)
    llm = client or LLMClient()
    try:
        proposed = llm.structured(
            schema=ProposedOpportunity,
            tier=recommendation_tier(),
            purpose=ModelRun.Purpose.DRAFTING,
            prompt_version=PROMPT_VERSION,
            system=cached_system(INSTRUCTIONS),
            user=_render(candidate, observations),
        )
    except (CostCapExceeded, LLMUnavailable, LLMInvalidOutput) as exc:
        raise OpportunityError(f"Drafting failed: {exc}") from exc

    allowed = {o.pk for o in observations}
    content = {
        key: _drop_unknown(getattr(proposed, key), allowed)
        for key in ("opening", "why_it_matters", "test_plan", "next_step", "gate", "limits")
    }
    for key, value in content.items():
        setattr(candidate, key, value)
    test_type = proposed.test_type.strip().lower()
    candidate.test_type = test_type if test_type in Candidate.TestType.values else ""
    candidate.model_run = (
        ModelRun.objects.filter(purpose=ModelRun.Purpose.DRAFTING, prompt_version=PROMPT_VERSION)
        .order_by("-at").first()
    )
    candidate.approved_at, candidate.approved_by_label = None, ""
    candidate.save()
    _audit(candidate.organization, actor_label, f"Drafted the case for {candidate.name}",
           {"candidate_id": candidate.pk, "prompt_version": PROMPT_VERSION})
    return candidate


def _drop_unknown(text: str, allowed: set[int]) -> str:
    """Remove O-numbers the model was not given."""
    def keep(match):
        return match.group(0) if int(match.group(1)) in allowed else ""
    cleaned = re.sub(r"\bO(\d+)\b", keep, text.strip())
    cleaned = re.sub(r"\s*[,;]\s*(?=[,;)])", "", cleaned)
    cleaned = re.sub(r"(?<=\()\s*[,;]\s*", "", cleaned)
    cleaned = re.sub(r"\s*\(\s*\)", "", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _audit(organization, actor_label: str, message: str, context: dict, *, kind: str = "") -> None:
    AuditEvent.objects.create(
        kind=kind or AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label or "system",
        organization=organization,
        message=message,
        context=context,
    )

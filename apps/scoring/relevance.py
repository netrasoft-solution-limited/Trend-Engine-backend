"""Scoring one shared claim against one client's profile.

This is the mechanism behind PRD §14's tenancy criterion — "one public signal
receives different scores for Jarrow and the second-client fixture" — and
behind the plainer requirement underneath it: two clients must not receive the
same brief built from the same corpus.

The matcher is literal and the scoring is arithmetic, both on purpose. Arch
§8.2 requires components to be persisted so the operator UI can render *why*,
and a model-generated relevance score has no components to render. A term match
can name the asset, the word, and the sentence it appeared in — which an
operator can disagree with and fix by editing a word.

Cost is the other reason. Extraction already runs a model once per content item
and that cost is bounded. Relevance runs per claim PER CLIENT, so it grows with
both corpus and client count; at 169 claims it would already be 338 calls for
two clients, repeated every time a profile is edited. Arch §10 treats cost as a
design constraint, not an afterthought.

The known limitation, stated rather than hidden: literal matching has no
recall. "Mg glycinate" does not match "magnesium" unless somebody adds the
term. That is why `ClaimRelevance.matches` is shown in the operator UI — an
obviously-relevant claim scoring zero is the prompt to add the missing word,
and the profile gets better by being used. Embeddings would do better and are
not available.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from django.db import transaction

from apps.clients.models import ClientAsset, ClientProfileVersion, ClientTerm
from apps.enrichment.models import Claim

from .models import ClaimRelevance

logger = logging.getLogger(__name__)

#: An extra distinct match is worth something, but much less than the strongest
#: one. A claim touching a hero product and two long-tail SKUs should outrank a
#: claim touching only the hero — but not by enough that three weak matches beat
#: one strong one, which is what summing would do.
BREADTH_BONUS = 5


@dataclass(frozen=True)
class Matcher:
    """One profile entry, with its terms compiled once.

    Compiled once per profile rather than per claim: at 169 claims and a
    profile of any size, recompiling per claim is the difference between a
    scoring pass that is instant and one an operator waits for.
    """

    facet: str
    name: str
    weight: int
    patterns: tuple[tuple[str, re.Pattern], ...]

    def find(self, text: str) -> list[dict]:
        hits = []
        for term, pattern in self.patterns:
            if pattern.search(text):
                hits.append(
                    {"facet": self.facet, "name": self.name, "term": term, "weight": self.weight}
                )
        return hits


@dataclass
class Profile:
    """A client profile compiled into something that can be matched against."""

    version: ClientProfileVersion
    assets: list[Matcher] = field(default_factory=list)
    audiences: list[Matcher] = field(default_factory=list)
    priorities: list[Matcher] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.assets or self.audiences or self.priorities)


def _compile(term: str) -> re.Pattern | None:
    """Word-boundary, case-insensitive, phrase-safe.

    Boundaries matter more than they look: without them "iron" matches
    "environment" and "zinc" matches "zincation", and the resulting scores are
    confidently wrong in a way nobody audits. A term that is only punctuation
    or whitespace compiles to nothing and is dropped rather than matching
    everything.
    """
    cleaned = (term or "").strip()
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        return None
    return re.compile(rf"(?<!\w){re.escape(cleaned)}(?!\w)", re.IGNORECASE)


def _matchers(rows, facet_of) -> list[Matcher]:
    built = []
    for row in rows:
        patterns = tuple(
            (term, pattern)
            for term in (row.terms or [])
            if (pattern := _compile(term)) is not None
        )
        if not patterns:
            logger.warning(
                "Profile entry %r has no usable terms and will never match", row.name
            )
            continue
        built.append(
            Matcher(facet=facet_of(row), name=row.name, weight=row.weight, patterns=patterns)
        )
    return built


def compile_profile(version: ClientProfileVersion) -> Profile:
    """Load a profile version and prepare it for matching."""
    assets = _matchers(
        ClientAsset.objects.filter(profile=version), lambda _row: "asset"
    )

    terms = list(ClientTerm.objects.filter(profile=version))
    audiences = _matchers(
        [t for t in terms if t.facet == ClientTerm.Facet.AUDIENCE], lambda _row: "audience"
    )
    # Competitors feed the strategic-priority component — see the note on
    # `ClientTerm`. The facet is kept in `matches` so the reason stays legible.
    priorities = _matchers(
        [
            t
            for t in terms
            if t.facet in (ClientTerm.Facet.PRIORITY, ClientTerm.Facet.COMPETITOR)
        ],
        lambda row: row.facet,
    )

    return Profile(version=version, assets=assets, audiences=audiences, priorities=priorities)


def _component(matchers: list[Matcher], text: str) -> tuple[int, list[dict]]:
    """Strongest match, plus a little for breadth. Capped at 100."""
    hits: list[dict] = []
    for matcher in matchers:
        hits.extend(matcher.find(text))
    if not hits:
        return 0, []

    strongest = max(hit["weight"] for hit in hits)
    distinct = len({hit["name"] for hit in hits})
    return min(100, strongest + BREADTH_BONUS * (distinct - 1)), hits


def searchable(claim: Claim) -> str:
    """What a claim is matched against.

    The quote is included as well as the extracted text because the speaker's
    own words carry product and brand names that the normalised claim text
    often drops — "I take the Jarrow one" survives in the quote and not in a
    tidied-up restatement of it.
    """
    return " \n".join(part for part in (claim.subject, claim.text, claim.quote) if part)


def score_claim(claim: Claim, profile: Profile) -> dict:
    """The components for one claim against one profile. Pure — writes nothing."""
    text = searchable(claim)

    asset, asset_hits = _component(profile.assets, text)
    audience, audience_hits = _component(profile.audiences, text)
    priority, priority_hits = _component(profile.priorities, text)

    weights = ClaimRelevance.WEIGHTS
    weighted = asset * weights["asset"] + audience * weights["audience"] + priority * weights["priority"]
    total_weight = sum(weights.values())

    return {
        "asset_component": asset,
        "audience_component": audience,
        "priority_component": priority,
        # Normalised over the weights in force, so the number reads 0–100 today
        # and keeps meaning the same thing when the domain component is added
        # and `weights_used` changes with it.
        "fit_score": round(weighted / total_weight) if total_weight else 0,
        "weights_used": dict(weights),
        "matches": asset_hits + audience_hits + priority_hits,
    }


@transaction.atomic
def score_for(organization, *, profile_version: ClientProfileVersion | None = None) -> dict:
    """Score every stored claim for one client. Returns a short summary.

    Idempotent: re-running against the same profile version updates the rows
    rather than duplicating them, so an operator can edit a profile, re-score,
    and compare without cleaning up first.

    Claims are read unfiltered by tenant ON PURPOSE. The evidence layer is
    shared (PRD §6.7) — every client is scored against the same corpus, and
    that is the property the whole design rests on. What is tenant-scoped is
    the OUTPUT of this function, not its input.
    """
    from apps.clients import services as client_services

    version = profile_version or client_services.current_for(organization)
    if version is None:
        raise ValueError(
            f"{organization} has no active client profile. Nothing can be scored for "
            f"a client the system knows nothing about — create one with "
            f"clients.services.draft() and activate it."
        )

    profile = compile_profile(version)
    if profile.is_empty:
        logger.warning("Profile v%s has no usable matchers", version.number)

    claims = Claim.objects.select_related("segment", "content_item").all()

    existing = {
        row.claim_id: row
        for row in ClaimRelevance.objects.filter(organization=organization, profile=version)
    }

    created = updated = matched = 0
    for claim in claims:
        values = score_claim(claim, profile)
        row = existing.get(claim.pk)
        if row is None:
            ClaimRelevance.objects.create(
                organization=organization, claim=claim, profile=version, **values
            )
            created += 1
        else:
            for attr, value in values.items():
                setattr(row, attr, value)
            row.save(update_fields=[*values, "scored_at"])
            updated += 1
        if values["fit_score"] > 0:
            matched += 1

    summary = {
        "organization": organization.name,
        "profile_version": version.number,
        "claims": created + updated,
        "created": created,
        "updated": updated,
        "matched": matched,
    }
    logger.info("Scored %(claims)d claims for %(organization)s: %(matched)d matched", summary)
    return summary


def relevant_claims(organization, *, profile_version=None, minimum: int = 1):
    """The claims that are this client's, best first.

    `minimum=1` rather than 0 is the filtering rule: a claim nothing in the
    profile matched is not this client's news, and including it would recreate
    the undifferentiated brief this module exists to replace.
    """
    from apps.clients import services as client_services

    version = profile_version or client_services.current_for(organization)
    if version is None:
        return ClaimRelevance.objects.none()

    return (
        ClaimRelevance.objects.filter(
            organization=organization, profile=version, fit_score__gte=minimum
        )
        .select_related("claim", "claim__segment", "claim__content_item")
        .order_by("-fit_score", "claim__subject")
    )

"""Turning stored claims into the body of an output.

PRD §5 principle 1: no recommendation exists without a stored signal and
supporting evidence spans. This is that principle made literal — every
paragraph carries the speaker's own words, the episode they came from, and the
moment they said it, because those are already on the `Claim` rows and the
alternative is prose nobody can check.

DELIBERATELY TEMPLATED, NOT GENERATED. A model could write a smoother brief,
and it would cost a `ModelRun`, add a failure mode, and produce sentences
nobody could trace to a quote. Scoring and drafting are PRD Phase 4–5 and are
not built; pretending otherwise by generating fluent text over unscored
evidence would be the single most misleading thing this system could do. What
this produces is honest about what it is: the evidence, organised.

The layer rule allows this. `.importlinter` puts `apps.outputs` above
`apps.enrichment` and `apps.evidence`, so reading them here is downward; the
forbidden direction is enrichment reaching up into outputs.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from apps.enrichment.models import Claim, Question

from .services import DraftingError

logger = logging.getLogger(__name__)

#: Reading order for a Trend Intelligence Brief. Mechanism before effect before
#: dosage mirrors how a formulator reads: what it does, then what it does for
#: people, then how much. Market movements come last because they age fastest.
SECTION_ORDER: list[tuple[str, str]] = [
    ("market", "What moved in the category"),
    ("effect", "What the evidence says it does"),
    ("mechanism", "Why it works that way"),
    ("dosage", "Doses being discussed"),
    ("safety", "Safety and interactions"),
    ("comparison", "Forms and products compared"),
]

MAX_PER_SECTION = 6


def _timestamp(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}:{remainder:02d}"


def _paragraph(claim: Claim) -> str:
    """One claim, with everything needed to check it.

    The quote is verbatim and the timestamp is real, so a reader who doubts a
    line can open the episode at that moment. PRD §6.6 requires exactly this of
    a brief's evidence.
    """
    item = claim.content_item
    where = f"{item.creator}, “{item.title}”" if item.creator else f"“{item.title}”"
    at = _timestamp(claim.segment.start_seconds if claim.segment_id else None)
    attribution = f"{where} at {at}" if at else where

    line = f"{claim.text.rstrip('.')}. "
    line += f"“{claim.quote.strip()}” — {attribution}."
    if claim.is_cited:
        line += " The speaker cites a study."
    return line


def sections_from_claims(
    *,
    claims=None,
    limit_per_section: int = MAX_PER_SECTION,
    include_sponsored: bool = False,
    rank: dict[int, int] | None = None,
    profile_label: str = "",
) -> list[dict]:
    """The body of a brief, as the ordered sections `OutputVersion.body` holds.

    `claims` defaults to the whole corpus, which is the UNTAILORED brief — the
    same document for every client. `sections_for()` below is what production
    uses; this signature stays open because the renderer is the same either way
    and a test should be able to hand it three rows.

    `rank` maps claim id to that client's fit score, and when present it leads
    the sort. Without it the ordering falls back to study-backed-first, which
    is the only ordering an unscored corpus honestly supports.

    Sponsored claims are excluded by default. A sponsor read is a competitor's
    marketing copy, and carrying it into a client brief as evidence of what
    experts believe is the failure the `is_sponsored` flag exists to prevent —
    it is not a filter that can safely default the other way.
    """
    if claims is None:
        claims = Claim.objects.select_related("segment", "content_item").all()
    if not include_sponsored:
        # Filtered in Python as well as in SQL, because `claims` may arrive as
        # a list from the relevance layer — where excluding sponsored content
        # is not that layer's job and must not be assumed done.
        claims = [claim for claim in claims if not claim.is_sponsored]

    grouped: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        grouped[claim.kind].append(claim)

    ranking = rank or {}
    for bucket in grouped.values():
        bucket.sort(
            key=lambda c: (-ranking.get(c.pk, 0), not c.is_cited, c.subject or "")
        )

    sections: list[dict] = []
    used: set[int] = set()

    for kind, heading in SECTION_ORDER:
        bucket = grouped.get(kind, [])[:limit_per_section]
        if not bucket:
            continue
        sections.append(
            {
                "heading": heading,
                "text": "\n\n".join(_paragraph(claim) for claim in bucket),
            }
        )
        used.update(claim.content_item_id for claim in bucket)

    questions = list(Question.objects.select_related("content_item")[:5])
    if questions:
        sections.append(
            {
                "heading": "Questions being asked and not answered",
                "text": "\n\n".join(
                    f"“{q.text.strip()}” — {q.content_item.title}" for q in questions
                ),
            }
        )

    sections.append(_methodology(used, profile_label=profile_label))
    return sections


def sections_for(
    organization,
    *,
    limit_per_section: int = MAX_PER_SECTION,
    include_sponsored: bool = False,
) -> list[dict]:
    """One client's brief, built from the shared corpus and their own profile.

    This is the function that makes two clients receive different documents
    from the same evidence — PRD §14's tenancy criterion, and the plainer
    requirement that a brief should be about the client it is sent to.

    Claims that nothing in the profile matched are not included. That is the
    whole mechanism: the corpus is shared, the selection is not.

    Refuses outright when the client has no active profile. The alternative —
    silently falling back to the untailored corpus — would produce a brief that
    looks finished, reads plausibly, and is the same one every other client
    receives. A system that fails that way is worse than one that stops.
    """
    from apps.clients import services as client_services
    from apps.scoring import relevance

    profile = client_services.current_for(organization)
    if profile is None:
        raise DraftingError(
            f"{organization} has no active client profile, so there is nothing to "
            f"tailor this brief to. Create one with clients.services.draft() and "
            f"activate it — an untailored brief would be the same document every "
            f"other client receives."
        )

    scores = list(relevance.relevant_claims(organization, profile_version=profile))
    if not scores:
        raise DraftingError(
            f"No stored claim matched {organization}'s profile v{profile.number}. "
            f"Either the corpus holds nothing in their categories yet, or the "
            f"profile's terms are too narrow — run scoring and inspect the "
            f"zero-score claims before assuming the former."
        )

    return sections_from_claims(
        claims=[score.claim for score in scores],
        limit_per_section=limit_per_section,
        include_sponsored=include_sponsored,
        rank={score.claim_id: score.fit_score for score in scores},
        profile_label=profile.citation,
    )


def _methodology(content_item_ids: set[int], *, profile_label: str = "") -> dict:
    """What this was built from, and what it is not.

    PRD §6.6 lists methodology among a brief's required contents. It is also
    the most useful section in this particular brief, because it is where the
    gap between what the system does today and what it will do is stated
    plainly rather than papered over.
    """
    from apps.evidence.models import ContentItem

    items = ContentItem.objects.filter(pk__in=content_item_ids).order_by("-published_at")
    sources = "\n".join(
        f"· {item.title} — {item.creator or 'unattributed'}"
        + (f" ({item.published_at:%d %b %Y})" if item.published_at else "")
        for item in items
    )
    evidence = (
        "Every claim above is quoted verbatim from a transcript held in the system, "
        "with the speaker and the moment in the recording named so it can be checked. "
        "Paid sponsor segments are identified and excluded."
    )

    if profile_label:
        # Tailored. PRD §8 requires the output to name the profile version it
        # was built from, and the limit has to be named with it — ordering here
        # reflects how well a claim fits THIS client, not how much the category
        # is moving, and those are different questions.
        selection = (
            f"Selected and ordered for this client against profile {profile_label}: "
            f"their products, audiences, priorities and competitors. Claims matching "
            f"nothing in that profile are not included, so another client's brief "
            f"drawn from the same evidence will differ.\n\n"
            f"What this ordering does not yet include is how fast the category is "
            f"moving. Momentum and acceleration need a 28-day baseline the corpus is "
            f"still accumulating, so a long-standing topic and a breaking one rank "
            f"the same here if they fit the client equally."
        )
    else:
        selection = (
            "This brief is not tailored to a client profile. Claims that cite a study "
            "are listed first within each section; they are not ranked by how much the "
            "category is moving, and ordering by anything else would imply a judgement "
            "the system has not made."
        )

    return {
        "heading": "What this is based on",
        "text": f"{evidence}\n\n{selection}\n\nSources ({items.count()}):\n{sources}",
    }

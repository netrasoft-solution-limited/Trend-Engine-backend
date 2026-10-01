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
    limit_per_section: int = MAX_PER_SECTION,
    include_sponsored: bool = False,
) -> list[dict]:
    """The body of a brief, as the ordered sections `OutputVersion.body` holds.

    Sponsored claims are excluded by default. A sponsor read is a competitor's
    marketing copy, and carrying it into a client brief as evidence of what
    experts believe is the failure the `is_sponsored` flag exists to prevent —
    it is not a filter that can safely default the other way.
    """
    claims = (
        Claim.objects.select_related("segment", "content_item")
        .filter(is_sponsored=False) if not include_sponsored
        else Claim.objects.select_related("segment", "content_item")
    )

    grouped: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        grouped[claim.kind].append(claim)

    # Study-backed claims lead each section. Scoring does not exist yet, so this
    # is the only ordering the data honestly supports — and it is stated in the
    # methodology section below rather than left as an unexplained sort.
    for bucket in grouped.values():
        bucket.sort(key=lambda c: (not c.is_cited, c.subject))

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

    sections.append(_methodology(used))
    return sections


def _methodology(content_item_ids: set[int]) -> dict:
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
    return {
        "heading": "What this is based on",
        "text": (
            "Every claim above is quoted verbatim from a transcript held in the system, "
            "with the speaker and the moment in the recording named so it can be checked. "
            "Paid sponsor segments are identified and excluded.\n\n"
            "Claims that cite a study are listed first within each section. They are not "
            "ranked by how much the category is moving: signal scoring and per-client "
            "ranking are not yet built, and ordering by anything else would imply a "
            "judgement the system has not made.\n\n"
            f"Sources ({items.count()}):\n{sources}"
        ),
    }

"""One conversation, published more than once — PRD §6.3's cross-post rule.

A podcast episode, its YouTube upload, and clips cut from either are the same
conversation. Counted separately they look like independent confirmation: two
items, usually two creators (the podcast is named after the show, the channel
after the host), and two source types — which is exactly what the confidence
score rewards. Mark's 19 October review asked for this to be prevented, not
just displayed.

HOW. Transcripts are compared, not titles: a clip has a different title, a
re-upload often has a different one, and two unrelated episodes can share one.
Each transcript becomes a set of overlapping five-word sequences ("shingles").
If most of the SHORTER transcript's sequences appear in the longer one, the
shorter is the same conversation — a clip is contained in its episode, and a
re-upload in its original. Containment rather than overlap of the two sets is
the point: a three-minute clip shares 3% of a two-hour episode's sequences but
nearly all of its own.

WHICH IS KEPT. The longer transcript is the canonical item; the shorter is
marked `duplicate_of` it. When the full episode arrives after one of its clips,
the clip is re-pointed at the episode rather than the other way round.

WITHOUT A TRANSCRIPT nothing is merged. Matching titles and durations are
recorded as SUSPECTED, shown to the operator, and change no score — a guess
about identity is not allowed to remove evidence.

Pure functions at the top, so the comparison is testable without a database.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q

from apps.evidence.models import ContentItem

from .transcripts import _duration_seconds

logger = logging.getLogger(__name__)

SHINGLE_WORDS = 5
#: Enough for a full episode; beyond this a long conversation is compared on
#: its first two hours or so, which is where a re-upload or clip will match.
MAX_WORDS = 20_000
#: Share of the shorter transcript that must appear in the longer one.
CONTAINMENT = 0.6
#: Below this, a "transcript" is too short to tell anything apart.
MIN_SHINGLES = 50
#: Re-uploads and clips appear within days of the original.
WINDOW = timedelta(days=14)
#: Metadata-only matching: title similarity and duration agreement.
TITLE_SIMILARITY = 0.6
DURATION_TOLERANCE = 0.05

_WORD = re.compile(r"[a-z0-9']+")


def shingles(text: str, *, size: int = SHINGLE_WORDS, max_words: int = MAX_WORDS) -> set[int]:
    words = _WORD.findall((text or "").lower())[:max_words]
    return {hash(tuple(words[i:i + size])) for i in range(len(words) - size + 1)}


def containment(a: set[int], b: set[int]) -> float:
    """How much of the smaller set is inside the larger one."""
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return len(small & large) / len(small)


def title_similarity(a: str, b: str) -> float:
    left, right = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True)
class Match:
    item_id: int
    other_id: int
    overlap: float
    status: str
    #: The item that stays canonical.
    keep_id: int
    reason: str


# ── Against the database ────────────────────────────────────────────────────


def _date(item: ContentItem):
    return item.published_at or item.discovered_at


def _transcript(item: ContentItem) -> str:
    transcript = getattr(item, "transcript", None)
    return transcript.text if transcript is not None else ""


def _candidates(item: ContentItem):
    when = _date(item)
    qs = ContentItem.objects.exclude(pk=item.pk).select_related("transcript", "source")
    if when:
        qs = qs.filter(
            Q(published_at__range=(when - WINDOW, when + WINDOW))
            | Q(published_at__isnull=True, discovered_at__range=(when - WINDOW, when + WINDOW))
        )
    return qs


def find(item: ContentItem) -> Match | None:
    """The strongest same-conversation match for one item, or None. Writes nothing."""
    text = _transcript(item)
    mine = shingles(text) if text else set()

    best: Match | None = None
    for other in _candidates(item):
        if mine and len(mine) >= MIN_SHINGLES:
            theirs_text = _transcript(other)
            if not theirs_text:
                continue
            theirs = shingles(theirs_text)
            if len(theirs) < MIN_SHINGLES:
                continue
            overlap = containment(mine, theirs)
            if overlap < CONTAINMENT:
                continue
            keep = other if len(theirs) >= len(mine) else item
            similar_length = min(len(mine), len(theirs)) / max(len(mine), len(theirs)) > 0.8
            kind = "re-upload" if similar_length else "clip"
            candidate = Match(
                item.pk, other.pk, overlap, "confirmed", keep.pk,
                f"{kind}: {overlap:.0%} of the shorter transcript appears in the longer",
            )
        elif not text:
            similarity = title_similarity(item.title, other.title)
            if similarity < TITLE_SIMILARITY or not _durations_agree(item, other):
                continue
            candidate = Match(
                item.pk, other.pk, similarity, "suspected", other.pk,
                f"suspected: titles {similarity:.0%} alike and durations agree; "
                f"no transcript to compare",
            )
        else:
            continue
        if best is None or candidate.overlap > best.overlap:
            best = candidate
    return best


def check(item: ContentItem) -> Match | None:
    """Find and record the match for one item. Returns it, or None.

    Called when a transcript arrives, before extraction, so a confirmed
    duplicate is never extracted — it would only re-buy claims the canonical
    item already has.
    """
    if item.is_duplicate:
        return None
    match = find(item)
    if match is None:
        return None

    other = ContentItem.objects.get(pk=match.other_id)
    if match.status == "suspected":
        _mark(item, other, match)
        return match

    duplicate, keep = (item, other) if match.keep_id == other.pk else (other, item)
    _mark(duplicate, keep, match)
    # Anything that pointed at the item now being demoted follows it to the root.
    ContentItem.objects.filter(duplicate_of=duplicate, duplicate_status="confirmed").update(
        duplicate_of=keep
    )
    logger.info("Item %s is the same conversation as %s (%s)", duplicate.pk, keep.pk, match.reason)
    return match


def _mark(duplicate: ContentItem, keep: ContentItem, match: Match) -> None:
    duplicate.duplicate_of = keep.canonical if match.status == "confirmed" else keep
    duplicate.duplicate_status = match.status
    duplicate.duplicate_overlap = round(match.overlap, 3)
    duplicate.duplicate_reason = match.reason[:300]
    duplicate.save(
        update_fields=["duplicate_of", "duplicate_status", "duplicate_overlap", "duplicate_reason"]
    )


def _durations_agree(a: ContentItem, b: ContentItem) -> bool:
    left, right = _duration_seconds(a), _duration_seconds(b)
    if not left or not right:
        return False
    return abs(left - right) / max(left, right) <= DURATION_TOLERANCE

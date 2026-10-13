"""The domain signal score and confidence — arithmetic, not a model.

PRD §6.3 sets the weights; Arch §8.2 requires every component to be stored with
what it was computed from. So each component here returns its value AND a
sentence saying where the value came from, and the caller persists both.

Three rules shape every function in this module:

1. VOLUME COUNTS ITEMS, NOT CLAIMS. One two-hour episode can yield thirty claims
   about magnesium. Counting claims would let a single creator manufacture a
   trend — PRD §6.3's "cap repeated creator influence". An item counts once in
   the week it was published, however much it said.

2. A MISSING INPUT IS MISSING, NOT ZERO. Momentum needs a baseline the corpus
   may not have yet; engagement needs view counts that are not collected yet.
   Such a component gets `value=None` and a reason, and the domain score is the
   weighted mean of the components that DO have values. Scoring a missing input
   as 0 would quietly rank every new development as "not moving", and scoring
   it as 50 would invent a measurement. The screen shows how many components
   the score rests on.

3. CONFIDENCE IS NOT IMPORTANCE. A loud development with thin evidence and a
   quiet one with three independent studies behind it are different findings,
   and conflating them is the failure mode the PRD names. Confidence is its own
   number with its own reasons.

Pure functions over `Fact`s, so the whole thing is testable without a database.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

#: PRD §6.3. Operator-configured in the PRD's words; a literal until there is
#: a screen to configure them, and stored on every signal so a later change
#: does not rewrite the explanation of an earlier score.
WEIGHTS: dict[str, int] = {
    "momentum": 25,
    "acceleration": 20,
    "source_diversity": 15,
    "evidence_quality": 15,
    "engagement": 10,
    "novelty": 10,
    "recency": 5,
}

#: Momentum compares this week with a 28-day baseline before it, so it needs
#: five weeks of watching before it means anything.
MOMENTUM_NEEDS_DAYS = 35
#: Acceleration compares two week-on-week changes: three weeks.
ACCELERATION_NEEDS_DAYS = 21
#: Recency is full within this many days of the newest evidence, zero at the
#: end of the signal window.
FRESH_DAYS = 2
WINDOW_DAYS = 28


@dataclass(frozen=True)
class Fact:
    """One claim's worth of evidence, reduced to what scoring needs."""

    item_id: int
    creator: str
    route: str
    at: datetime
    is_cited: bool


@dataclass
class Scored:
    domain_score: int
    components: dict
    baseline: dict
    confidence: int
    confidence_reasons: list[str] = field(default_factory=list)
    item_count: int = 0
    creator_count: int = 0
    source_count: int = 0


def score(
    supporting: Iterable[Fact],
    contradicting: Iterable[Fact] = (),
    *,
    now: datetime,
    watching_since: datetime | None,
    first_seen_at: datetime,
) -> Scored:
    supporting = list(supporting)
    contradicting = list(contradicting)
    watched_days = (now - watching_since).days if watching_since else 0
    weekly = _weekly_items(supporting, now=now, weeks=6)

    components = {
        "momentum": _momentum(weekly, watched_days),
        "acceleration": _acceleration(weekly, watched_days),
        "source_diversity": _diversity(supporting),
        "evidence_quality": _quality(supporting),
        "engagement": _component(
            None, "Engagement metrics (views, comments) are not collected yet."
        ),
        "novelty": _novelty(first_seen_at, now),
        "recency": _recency(supporting, now),
    }
    for name, component in components.items():
        component["weight"] = WEIGHTS[name]

    available = {n: c for n, c in components.items() if c["value"] is not None}
    total_weight = sum(c["weight"] for c in available.values())
    domain_score = (
        round(sum(c["value"] * c["weight"] for c in available.values()) / total_weight)
        if total_weight
        else 0
    )

    confidence, reasons = _confidence(supporting, contradicting)
    return Scored(
        domain_score=_clamp(domain_score),
        components=components,
        baseline={
            "weekly_items": weekly,
            "weeks_label": "newest first; each entry is one 7-day week ending now",
            "watching_since": watching_since.date().isoformat() if watching_since else None,
            "watched_days": watched_days,
        },
        confidence=confidence,
        confidence_reasons=reasons,
        item_count=len({f.item_id for f in supporting}),
        creator_count=len({f.creator for f in supporting if f.creator}),
        source_count=len({f.route for f in supporting if f.route}),
    )


# ── The components ──────────────────────────────────────────────────────────


def _component(value: float | None, detail: str) -> dict:
    return {"value": None if value is None else _clamp(round(value)), "detail": detail}


def _weekly_items(facts: list[Fact], *, now: datetime, weeks: int) -> list[int]:
    """Distinct items per 7-day week, newest week first."""
    buckets: list[set[int]] = [set() for _ in range(weeks)]
    for fact in facts:
        age = (now - fact.at).days
        index = age // 7
        if 0 <= index < weeks:
            buckets[index].add(fact.item_id)
    return [len(b) for b in buckets]


def _momentum(weekly: list[int], watched_days: int) -> dict:
    if watched_days < MOMENTUM_NEEDS_DAYS:
        return _component(
            None,
            f"Needs {MOMENTUM_NEEDS_DAYS} days of collection for a 28-day baseline; "
            f"the corpus has {watched_days}.",
        )
    current, baseline = weekly[0], sum(weekly[1:5]) / 4
    # log2 of the ratio: level is 50, double the baseline is 75, four times is
    # 100, half is 25. +1 on both sides so a first appearance is finite.
    ratio = (current + 1) / (baseline + 1)
    return _component(
        50 + 25 * math.log2(ratio),
        f"{current} item(s) this week against a baseline of {baseline:.1f} a week "
        f"over the four weeks before.",
    )


def _acceleration(weekly: list[int], watched_days: int) -> dict:
    if watched_days < ACCELERATION_NEEDS_DAYS:
        return _component(
            None,
            f"Needs {ACCELERATION_NEEDS_DAYS} days of collection to compare two "
            f"week-on-week changes; the corpus has {watched_days}.",
        )
    latest, previous = weekly[0] - weekly[1], weekly[1] - weekly[2]
    return _component(
        50 + 12.5 * (latest - previous),
        f"Week-on-week change of {latest:+d} item(s), against {previous:+d} the week before.",
    )


def _diversity(facts: list[Fact]) -> dict:
    creators = {f.creator for f in facts if f.creator}
    routes = {f.route for f in facts if f.route}
    # Four independent creators is full marks; a third source type (podcast,
    # YouTube, research) is the rest. One creator saying it often is not
    # confirmation, which is why this counts people rather than mentions.
    value = 70 * min(len(creators), 4) / 4 + 30 * min(len(routes), 3) / 3
    return _component(
        value,
        f"{len(creators)} independent creator(s) across {len(routes)} source type(s).",
    )


def _quality(facts: list[Fact]) -> dict:
    if not facts:
        return _component(None, "No supporting evidence.")
    cited = sum(1 for f in facts if f.is_cited)
    return _component(
        100 * cited / len(facts),
        f"{cited} of {len(facts)} supporting claim(s) cite a study or named source.",
    )


def _novelty(first_seen_at: datetime, now: datetime) -> dict:
    weeks = max(0, (now - first_seen_at).days) // 7
    return _component(
        100 - 20 * weeks,
        "First detected this week." if weeks == 0
        else f"First detected {weeks} week(s) ago; novelty falls 20 points a week.",
    )


def _recency(facts: list[Fact], now: datetime) -> dict:
    if not facts:
        return _component(None, "No supporting evidence.")
    newest = max(f.at for f in facts)
    age = max(0, (now - newest).days)
    if age <= FRESH_DAYS:
        value = 100.0
    else:
        value = 100 * (1 - (age - FRESH_DAYS) / (WINDOW_DAYS - FRESH_DAYS))
    return _component(value, f"Newest supporting evidence is {age} day(s) old.")


# ── Confidence ──────────────────────────────────────────────────────────────


def _confidence(supporting: list[Fact], contradicting: list[Fact]) -> tuple[int, list[str]]:
    """How sure we are that this is real — not how much it matters.

    Built from parts so each reason can be shown: quantity of evidence,
    independence of sources, citation, and the contradiction ratio (PRD §6.3).
    """
    items = len({f.item_id for f in supporting})
    creators = len({f.creator for f in supporting if f.creator})
    routes = len({f.route for f in supporting if f.route})
    cited = sum(1 for f in supporting if f.is_cited)
    against = len({f.item_id for f in contradicting})

    parts = [
        (min(30, 8 * items), f"{items} supporting item(s)"),
        (min(25, 9 * creators), f"{creators} independent creator(s)"),
        (15 if routes > 1 else 0, f"corroborated across {routes} source types"),
        (min(20, 7 * cited), f"{cited} claim(s) citing a study"),
        (10 if not against else 0, "no contradicting evidence" if not against else ""),
    ]
    value = sum(points for points, _ in parts)
    reasons = [f"+{points}: {why}" for points, why in parts if why and points]

    if against:
        ratio = against / (items + against)
        penalty = round(40 * ratio)
        value -= penalty
        reasons.append(
            f"−{penalty}: {against} item(s) contradict it ({ratio:.0%} of the evidence)"
        )
    if items < 2:
        reasons.append("A single item is not cross-source evidence.")

    return _clamp(value), reasons


def _clamp(value: float) -> int:
    return int(max(0, min(100, value)))


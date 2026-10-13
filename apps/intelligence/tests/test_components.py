"""The domain signal score and confidence.

Arithmetic, so it is tested as arithmetic — and on the three rules that keep it
honest: volume counts items not claims, a missing input is not a zero, and
confidence is not importance.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.intelligence import components
from apps.intelligence.components import Fact

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def fact(item, *, creator="A", route="podcast", days_ago=1, cited=False):
    return Fact(
        item_id=item, creator=creator, route=route,
        at=NOW - timedelta(days=days_ago), is_cited=cited,
    )


def score(supporting, contradicting=(), *, watched_days=60, first_seen_days=0):
    return components.score(
        supporting,
        contradicting,
        now=NOW,
        watching_since=NOW - timedelta(days=watched_days),
        first_seen_at=NOW - timedelta(days=first_seen_days),
    )


# ── A missing input is missing ──────────────────────────────────────────────


def test_a_young_corpus_has_no_momentum_and_says_why():
    """Prod began collecting on 1 October; until early November there is no
    28-day baseline, and the screen must say so rather than show a number."""
    scored = score([fact(1), fact(2, creator="B")], watched_days=12)

    assert scored.components["momentum"]["value"] is None
    assert "has 12" in scored.components["momentum"]["detail"]
    assert scored.components["acceleration"]["value"] is None


def test_engagement_is_never_invented():
    scored = score([fact(1), fact(2, creator="B")])

    assert scored.components["engagement"]["value"] is None
    assert "not collected" in scored.components["engagement"]["detail"]


def test_the_score_is_the_weighted_mean_of_what_is_available():
    scored = score([fact(1), fact(2, creator="B")], watched_days=12)
    available = {k: c for k, c in scored.components.items() if c["value"] is not None}

    expected = round(
        sum(c["value"] * c["weight"] for c in available.values())
        / sum(c["weight"] for c in available.values())
    )
    assert scored.domain_score == expected
    assert set(available) == {"source_diversity", "evidence_quality", "novelty", "recency"}


def test_every_component_records_its_weight():
    scored = score([fact(1)])

    assert {k: c["weight"] for k, c in scored.components.items()} == components.WEIGHTS
    assert sum(components.WEIGHTS.values()) == 100


# ── Volume counts items, not claims ─────────────────────────────────────────


def test_thirty_claims_from_one_episode_are_one_item_of_volume():
    loud = score([fact(1) for _ in range(30)])

    assert loud.baseline["weekly_items"][0] == 1
    assert loud.item_count == 1


def test_momentum_rises_when_this_week_beats_the_baseline():
    baseline = [fact(i, days_ago=8 + 7 * (i % 4)) for i in range(4)]  # one a week
    surge = [fact(100 + i, creator=f"C{i}", days_ago=1) for i in range(4)]

    rising = score(baseline + surge)
    flat = score(baseline + surge[:1])

    assert rising.components["momentum"]["value"] > flat.components["momentum"]["value"]
    assert flat.components["momentum"]["value"] == 50, "level with the baseline is 50"


def test_one_creator_repeating_is_not_diversity():
    same = score([fact(i, creator="A") for i in range(4)])
    many = score([fact(i, creator=f"C{i}") for i in range(4)])

    diversity = "source_diversity"
    assert many.components[diversity]["value"] > same.components[diversity]["value"]


def test_quality_is_the_share_of_claims_citing_a_study():
    scored = score(
        [fact(1, cited=True), fact(2, creator="B"), fact(3, creator="C"), fact(4, creator="D")]
    )

    assert scored.components["evidence_quality"]["value"] == 25


def test_recency_falls_with_the_age_of_the_newest_evidence():
    fresh = score([fact(1, days_ago=1)])
    stale = score([fact(1, days_ago=20)])

    assert fresh.components["recency"]["value"] == 100
    assert 0 < stale.components["recency"]["value"] < 100


def test_novelty_fades_week_by_week():
    assert score([fact(1)], first_seen_days=0).components["novelty"]["value"] == 100
    assert score([fact(1)], first_seen_days=15).components["novelty"]["value"] == 60


# ── Confidence is not importance ────────────────────────────────────────────


def test_contradiction_lowers_confidence_and_says_by_how_much():
    support = [fact(1), fact(2, creator="B"), fact(3, creator="C")]

    agreed = score(support)
    disputed = score(support, [fact(9, creator="Z")])

    assert disputed.confidence < agreed.confidence
    assert any("contradict" in r for r in disputed.confidence_reasons)


def test_a_single_item_is_flagged_as_not_cross_source():
    scored = score([fact(1)])

    assert "A single item is not cross-source evidence." in scored.confidence_reasons


def test_corroboration_across_source_types_raises_confidence():
    podcasts = score([fact(1), fact(2, creator="B")])
    mixed = score([fact(1), fact(2, creator="B", route="youtube")])

    assert mixed.confidence > podcasts.confidence

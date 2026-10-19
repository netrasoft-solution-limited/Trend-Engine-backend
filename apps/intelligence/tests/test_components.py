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


def coverage(covered_weeks: int, *, weeks: int = 6) -> list[dict]:
    """Weekly collection coverage, newest first: the first `covered_weeks` covered."""
    return [
        {"relevant_transcribed": 12 if i < covered_weeks else 4, "sources": 4,
         "covered": i < covered_weeks}
        for i in range(weeks)
    ]


def score(supporting, contradicting=(), *, covered_weeks=6, first_seen_days=0):
    return components.score(
        supporting,
        contradicting,
        now=NOW,
        watching_since=NOW - timedelta(days=60),
        first_seen_at=NOW - timedelta(days=first_seen_days),
        coverage=coverage(covered_weeks),
    )


# ── A missing input is missing ──────────────────────────────────────────────


def test_thin_coverage_has_no_momentum_and_says_why():
    """Mark, 19 Oct: measurement depends on adequate, consistent coverage as
    well as elapsed time. Two covered weeks is not a 28-day baseline."""
    scored = score([fact(1), fact(2, creator="B")], covered_weeks=2)

    assert scored.components["momentum"]["value"] is None
    assert "2 so far" in scored.components["momentum"]["detail"]
    assert scored.components["acceleration"]["value"] is None


def test_a_gap_in_coverage_breaks_the_run():
    rows = coverage(6)
    rows[1]["covered"] = False
    scored = components.score(
        [fact(1)], now=NOW, watching_since=NOW - timedelta(days=60), first_seen_at=NOW,
        coverage=rows,
    )

    assert scored.baseline["covered_streak"] == 1
    assert scored.components["momentum"]["value"] is None


def test_no_coverage_data_means_nothing_is_measured():
    scored = components.score(
        [fact(1)], now=NOW, watching_since=NOW - timedelta(days=60), first_seen_at=NOW,
    )

    assert scored.components["momentum"]["value"] is None


def test_engagement_is_never_invented():
    scored = score([fact(1), fact(2, creator="B")])

    assert scored.components["engagement"]["value"] is None
    assert "not collected" in scored.components["engagement"]["detail"]


def test_the_score_is_the_weighted_mean_of_what_is_available():
    scored = score([fact(1), fact(2, creator="B")], covered_weeks=0)
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



# ── Explicit about what the score rests on (Mark, 19 Oct) ───────────────────


def test_effective_weights_are_recorded_and_sum_to_one_hundred():
    scored = score([fact(1), fact(2, creator="B")], covered_weeks=0)
    c = scored.components

    assert c["source_diversity"]["effective_weight"] == 33.3
    assert c["recency"]["effective_weight"] == 11.1
    assert c["momentum"]["effective_weight"] is None
    total = sum(x["effective_weight"] for x in c.values() if x["effective_weight"])
    assert round(total) == 100
    assert scored.baseline["weighting_share"] == 45, "the score rests on 45% of the plan"


def test_unmeasured_evidence_spanning_weeks_is_a_recurring_discussion():
    scored = score([fact(1, days_ago=1), fact(2, creator="B", days_ago=9)], covered_weeks=0)

    assert scored.maturity == components.RECURRING


def test_a_single_week_unmeasured_is_a_watch_item():
    assert score([fact(1), fact(2, creator="B")], covered_weeks=0).maturity == components.WATCH


def test_only_measured_rising_momentum_is_a_trend():
    baseline = [fact(i, days_ago=8 + 7 * (i % 4)) for i in range(4)]
    surge = [fact(100 + i, creator=f"C{i}", days_ago=1) for i in range(4)]

    assert score(baseline + surge).maturity == components.MEASURED_TREND
    assert score(baseline + surge, covered_weeks=0).maturity != components.MEASURED_TREND

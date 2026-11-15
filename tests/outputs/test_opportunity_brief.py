"""The New Product Opportunity Brief, in the shape Mark delivered (20 Oct)."""
from __future__ import annotations

from datetime import date

import pytest

from apps.evidence.models import Observation
from apps.outputs import opportunity_brief
from apps.outputs.services import DraftingError
from apps.scoring import opportunities
from apps.scoring.tests import test_signal_rank as shared
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _brief_date(monkeypatch):
    """These observations are dated October 2026: build the brief as of 20 Oct,
    so they fall inside its 30-day window, whatever today is."""
    from apps.outputs import briefing

    monkeypatch.setattr(briefing, "_today", lambda: date(2026, 10, 20))

evidence = shared.evidence
jarrow = shared.jarrow


def obs(**kw):
    defaults = dict(url="https://example.com/x", title="An observation", source_type="reddit",
                    observed_at=date(2026, 10, 20), published_at=date(2026, 10, 1),
                    entered_by="abu")
    return Observation.objects.create(**{**defaults, **kw})


def approved(org, name, observations, *, status="test", priority=1, **case):
    with scoped(org):
        c = opportunities.create(org, name=name, actor_label="abu")
        opportunities.attach(c, [o.pk for o in observations], actor_label="abu")
        opportunities.edit(c, {"status": status, "priority": str(priority),
                               "opening": f"{name} opening.", "next_step": "Test formats.",
                               "gate": "Workable margins.", **case}, actor_label="abu")
        return opportunities.approve(c, actor_label="abu")


def brief(org):
    with scoped(org):
        return opportunity_brief.sections_for(org)


def test_nothing_approved_is_refused(jarrow):
    with scoped(jarrow):
        opportunities.create(jarrow, name="Spermidine", actor_label="abu")
    with pytest.raises(DraftingError, match="1 candidate"):
        brief(jarrow)


def test_the_brief_has_the_delivered_sections_in_priority_order(jarrow):
    search = obs(source_type="search_estimate", metric_kind="rolling_monthly_search_estimate",
                 metric_value=14800, query="spermidine", provider="Keyword tool",
                 geography="US", period="12-month average to Oct 2026",
                 url="https://example.com/kw")
    ad = obs(source_type="instagram", is_ad=True, title="Primeadine Reel",
             url="https://instagram.com/r", published_at=date(2026, 10, 12))
    shop = obs(source_type="tiktok_shop", metric_kind="lifetime_shop_sales",
               metric_value=4600, metric_label="4.6K items sold", url="https://tiktok.com/s")
    approved(jarrow, "Spermidine", [search, ad, shop], why_it_matters="Visible purchasing.")
    approved(jarrow, "Ergothioneine", [obs(url="https://reddit.com/e")],
             status="monitor", priority=2)

    result = brief(jarrow)
    headings = [s["heading"] for s in result.sections]
    text = {s["heading"]: s["text"] for s in result.sections}

    # Mark, 15 Nov: decision summary first, evidence in appendices.
    assert headings[:3] == ["About this brief", "Decision summary",
                            "What changed since the last memo"]
    assert "A1. Candidate 1: Spermidine — test now" in headings
    assert "A2. Candidate 2: Ergothioneine — keep on watch" in headings
    assert headings[-3:] == ["Appendix C: Search demand", "Appendix D: How to read the evidence",
                             "Appendix E: Sources"]
    summary = text["Decision summary"]
    assert "Our recommendation. Start with Spermidine." in summary
    assert "| 1 | Spermidine | Test now | Spermidine opening. | Test formats. |" in summary
    assert "None of the 2 was found" in summary
    assert "not a launch recommendation" in summary
    assert "| Spermidine | spermidine | 14,800 | US |" in text["Appendix C: Search demand"]
    assert "must not be added together" in text["Appendix C: Search demand"]
    assert "retrieved 20 Oct 2026; not refreshed" in text["Appendix C: Search demand"]
    corroborated = text["Appendix B: What is corroborated across channels"]
    assert (
        "| Spermidine | None current | 1 independent current event(s); 1 paid, brand, older or "
        "undated | None current | No fresh reading; 1 older or estimate(s) |"
    ) in corroborated
    assert "Not in every channel: Spermidine (none current in podcast or video, reddit and " \
        "communities, search)" in corroborated
    case = text["A1. Candidate 1: Spermidine — test now"]
    assert "Labelled: advertisement." in case
    assert "4.6K items sold (global lifetime items sold)" in case
    assert "Gate. Workable margins." in case
    assert result.profile, "cites the profile the gap check used"


def test_every_source_is_numbered_and_cited(jarrow):
    approved(jarrow, "Spermidine", [obs(url="https://reddit.com/a"),
                                    obs(url="https://reddit.com/b")], test_type="customer")

    text = {s["heading"]: s["text"] for s in brief(jarrow).sections}

    assert text["Appendix E: Sources"].count("· [") == 2
    case = text["A1. Candidate 1: Spermidine — test now"]
    assert "[1]" in case and "[2]" in case
    assert "Start with a Spermidine customer concept test." in text["Decision summary"]



def test_a_lead_on_watch_is_not_presented_as_a_test(jarrow):
    approved(jarrow, "Spermidine", [obs(url="https://reddit.com/a")], status="monitor",
             test_type="monitor", next_step="Set monthly Trends tracking.")

    summary = {s["heading"]: s["text"] for s in brief(jarrow).sections}["Decision summary"]

    assert "Our recommendation. Keep Spermidine on watch first" in summary
    assert "agree the monitoring step: set monthly Trends tracking." in summary
    assert "budget" not in summary

def test_analyst_o_numbers_become_this_briefs_source_numbers(jarrow):
    reddit = obs(url="https://reddit.com/a")
    approved(jarrow, "Spermidine", [reddit],
             why_it_matters=f"Shoppers ask for it (O{reddit.pk}, O999999).")

    text = {s["heading"]: s["text"] for s in brief(jarrow).sections}
    case = text["A1. Candidate 1: Spermidine — test now"]

    assert "Shoppers ask for it [1]." in case
    assert f"O{reddit.pk}" not in case and "O999999" not in case


def test_older_material_does_not_set_the_window(jarrow):
    approved(jarrow, "Ergothioneine", [
        obs(url="https://reddit.com/now", published_at=date(2026, 10, 5)),
        obs(url="https://reddit.com/later", published_at=date(2026, 10, 12)),
        obs(url="https://podcasts.example/old", source_type="podcast",
            published_at=date(2025, 10, 1), is_old_material=True),
    ])

    about = {s["heading"]: s["text"] for s in brief(jarrow).sections}["About this brief"]

    assert "published between 5 October and 12 October 2026" in about


# ── Mark's 15 Nov review ────────────────────────────────────────────────────


def test_older_and_undated_observations_never_count_as_current(jarrow):
    """23 of 84 sources predated the window and several were not labelled."""
    approved(jarrow, "Spermidine", [
        obs(url="https://reddit.com/now", published_at=date(2026, 10, 18)),
        obs(url="https://reddit.com/old", published_at=date(2026, 9, 10)),  # not flagged
        obs(url="https://reddit.com/none", published_at=None),
    ])

    result = brief(jarrow)
    text = {s["heading"]: s["text"] for s in result.sections}
    case = text["A1. Candidate 1: Spermidine — test now"]

    assert "1 independent current event(s); 2 paid, brand, older or undated" in (
        text["Appendix B: What is corroborated across channels"]
    )
    assert "Older context, published before 20 Sep 2026: not counted" in case
    assert "Undated: not counted" in case
    assert "Older or undated context" in text["Appendix E: Sources"]


def test_every_channel_includes_search(jarrow):
    """Urolithin A had no search data and was said to have current evidence
    'in every channel' (Mark, 15 Nov)."""
    approved(jarrow, "Urolithin A", [
        obs(url="https://podcasts.example/u", source_type="podcast",
            published_at=date(2026, 10, 10)),
        obs(url="https://tiktok.com/u", source_type="tiktok", published_at=date(2026, 10, 11)),
        obs(url="https://reddit.com/u", published_at=date(2026, 10, 12)),
    ])

    text = {s["heading"]: s["text"] for s in brief(jarrow).sections}
    corroborated = text["Appendix B: What is corroborated across channels"]

    assert "every channel, search included" not in corroborated
    assert "Not in every channel: Urolithin A (none current in search)" in corroborated
    assert "No search data of any kind for: Urolithin A." in text["Appendix C: Search demand"]


def test_the_same_event_counts_once_across_channels(jarrow):
    approved(jarrow, "Spermidine", [
        obs(url="https://youtube.com/a", source_type="youtube", event_key="study-x",
            published_at=date(2026, 10, 10)),
        obs(url="https://reddit.com/a", event_key="study-x", published_at=date(2026, 10, 11)),
    ])

    corroborated = {s["heading"]: s["text"] for s in brief(jarrow).sections}[
        "Appendix B: What is corroborated across channels"
    ]

    assert corroborated.count("1 independent current event(s)") == 1


def test_only_a_freshly_retrieved_trends_reading_counts_as_current_search(jarrow):
    stale = obs(source_type="google_trends", metric_kind="trends_relative_interest",
                metric_value=40, query="spermidine", provider="Google Trends", geography="US",
                period="20 Sep–17 Oct 2026", observed_at=date(2026, 10, 1),
                url="https://trends.google.com/a")
    fresh = obs(source_type="google_trends", metric_kind="trends_relative_interest",
                metric_value=45, query="spermidine", provider="Google Trends", geography="US",
                period="22 Sep–19 Oct 2026", observed_at=date(2026, 10, 19),
                url="https://trends.google.com/b")
    approved(jarrow, "Spermidine", [stale, fresh])

    text = {s["heading"]: s["text"] for s in brief(jarrow).sections}

    assert "1 fresh Trends reading(s); 1 older or estimate(s)" in (
        text["Appendix B: What is corroborated across channels"]
    )
    assert "(not current: retrieved 1 Oct 2026)" in text["Appendix C: Search demand"]

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

    assert headings[:4] == ["About this brief", "Executive summary", "Search demand",
                            "What is corroborated across channels"]
    assert headings[4:6] == ["Candidate 1: Spermidine — test now",
                             "Candidate 2: Ergothioneine — keep on watch"]
    assert headings[-2:] == ["How to read the evidence", "Sources"]
    summary = text["Executive summary"]
    assert "Our recommendation. Start with Spermidine." in summary
    assert "| 1 | Spermidine | Test now | Spermidine opening. | Test formats. |" in summary
    assert "None of the 2 was found" in summary
    assert "not a launch recommendation" in summary
    assert "| Spermidine | spermidine | 14,800 | US |" in text["Search demand"]
    assert "must not be added together" in text["Search demand"]
    corroborated = text["What is corroborated across channels"]
    assert (
        "| Spermidine | None found | 1 independent current event(s); 1 paid, brand or older | "
        "None found | 1 estimate(s) |"
    ) in corroborated
    assert "No candidate has independent, current evidence in every channel" in corroborated
    case = text["Candidate 1: Spermidine — test now"]
    assert "Labelled: advertisement." in case
    assert "4.6K items sold (global lifetime items sold)" in case
    assert "Gate. Workable margins." in case
    assert result.profile, "cites the profile the gap check used"


def test_every_source_is_numbered_and_cited(jarrow):
    approved(jarrow, "Spermidine", [obs(url="https://reddit.com/a"),
                                    obs(url="https://reddit.com/b")], test_type="customer")

    text = {s["heading"]: s["text"] for s in brief(jarrow).sections}

    assert text["Sources"].count("· [") == 2
    case = text["Candidate 1: Spermidine — test now"]
    assert "[1]" in case and "[2]" in case
    assert "Start with a Spermidine customer concept test." in text["Executive summary"]


def test_analyst_o_numbers_become_this_briefs_source_numbers(jarrow):
    reddit = obs(url="https://reddit.com/a")
    approved(jarrow, "Spermidine", [reddit],
             why_it_matters=f"Shoppers ask for it (O{reddit.pk}, O999999).")

    text = {s["heading"]: s["text"] for s in brief(jarrow).sections}
    case = text["Candidate 1: Spermidine — test now"]

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

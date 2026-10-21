"""Developments an analyst starts from observations (Mark's delivered briefs).

Defended: the status is earned from independent events in different weeks and
is never a measured trend; the origin is labelled; a recommendation may rest on
observations and loses any it was not given; and the client rank finds the
client's products in the observations' own words.
"""
from __future__ import annotations

from datetime import date

import pytest
from django.urls import reverse

from apps.evidence.models import Observation
from apps.intelligence import review
from apps.intelligence.models import Signal
from apps.operations.models import OperatorUser
from apps.scoring import recommendations, signal_rank
from apps.scoring.models import ClientSignalScore
from apps.scoring.recommendations import ProposedAction
from apps.scoring.tests import test_signal_rank as shared
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db

evidence = shared.evidence
jarrow = shared.jarrow


def obs(published, **kw):
    defaults = dict(url=f"https://www.reddit.com/r/x/{published}", title="Whey bloating",
                    source_type="reddit", observed_at=date(2026, 10, 20),
                    published_at=published, excerpt="Magnesium glycinate before bed?",
                    entered_by="abu")
    return Observation.objects.create(**{**defaults, **kw})


def fields(actions):
    return {"why_it_matters": "Shoppers cannot read magnesium labels.",
            "limits": "Targeted examples.", "products": ["Magnesium Glycinate"],
            "actions": actions}


def start(ids, **kw):
    return review.start_from_observations(
        title=kw.get("title", "Choosing a magnesium for sleep"),
        statement="Shoppers ask which magnesium to take.", observation_ids=ids,
        actor_label="abu",
    )


def test_two_independent_events_in_different_weeks_make_it_recurring(db):
    a, b = obs(date(2026, 10, 1)), obs(date(2026, 10, 9))

    signal, missing = start([a.pk, b.pk, 999_999])

    assert signal.origin == Signal.Origin.ANALYST
    assert signal.maturity == Signal.Maturity.RECURRING
    assert signal.observations.count() == 2 and missing == [999_999]


def test_one_event_told_twice_is_only_a_watch_item(db):
    a = obs(date(2026, 10, 1), event_key="same-study")
    b = obs(date(2026, 10, 9), event_key="same-study")

    signal, _ = start([a.pk, b.pk])

    assert signal.maturity == Signal.Maturity.WATCH


def test_nothing_to_start_from_is_refused(db):
    with pytest.raises(review.ReviewError, match="at least one observation"):
        start([123])


def test_the_rank_finds_the_clients_products_in_the_observations(jarrow):
    signal, _ = start([obs(date(2026, 10, 1)).pk])

    with scoped(jarrow):
        signal_rank.rank_for(jarrow)
        score = ClientSignalScore.objects.get(organization=jarrow, signal=signal)

    names = {m["name"] for m in score.matches}
    assert "Magnesium Glycinate" in names
    assert all(m.get("observation_ids") for m in score.matches)
    assert "Started by an analyst" in score.components["domain"]["detail"]


def test_a_recommendation_can_rest_on_observations_and_drops_invented_ones(jarrow):
    a, b = obs(date(2026, 10, 1)), obs(date(2026, 10, 9))
    signal, _ = start([a.pk, b.pk])

    result, transport = shared.draft(jarrow, signal, **fields(actions=[
        ProposedAction(owner="Content/SEO", action="Publish a label guide.",
                       success_measure="Visits.", claim_ids=[],
                       observation_ids=[a.pk, 888_888]),
        ProposedAction(owner="Brand", action="An action citing nothing.",
                       success_measure="", claim_ids=[], observation_ids=[]),
    ]))

    rec = result.recommendation
    assert rec.actions == [{"owner": "Content/SEO", "action": "Publish a label guide.",
                            "success_measure": "Visits.", "claim_ids": [],
                            "observation_ids": [a.pk]}]
    assert rec.validation["invented_observation_ids"] == [888_888]
    assert rec.validation["uncited_actions"] == 1
    sent = transport.calls[0]["messages"][0]["content"]
    assert f"O{a.pk} | supports | Reddit" in sent
    assert "not detected by automatic collection" in sent


def test_an_operator_edit_can_cite_observations(jarrow):
    a = obs(date(2026, 10, 1))
    signal, _ = start([a.pk])
    rec = shared.draft(jarrow, signal, **fields(actions=[
        ProposedAction(owner="Brand", action="Do it.", success_measure="",
                       claim_ids=[], observation_ids=[a.pk]),
    ]))[0].recommendation

    with scoped(jarrow):
        edited = recommendations.edit(rec, {
            "why_it_matters": "Because.", "actions": [
                {"owner": "Brand", "action": "Do it better.", "claim_ids": [],
                 "observation_ids": [a.pk]}],
        }, actor_label="abu")

    assert edited.actions[0]["observation_ids"] == [a.pk]


@pytest.fixture
def operator(db):
    return OperatorUser.objects.create_user(
        email="operator@netrasoft.test", password="x" * 24, name="Operator"
    )


def test_triage_starts_a_development_and_opens_its_review(client, operator, jarrow):
    client.force_login(operator)
    a, b = obs(date(2026, 10, 1)), obs(date(2026, 10, 9))

    response = client.post(reverse("ops-triage", args=[jarrow.slug]), {
        "action": "start", "title": "Choosing a magnesium for sleep",
        "statement": "Shoppers ask which magnesium to take.", "ids": f"O{a.pk}, O{b.pk}",
    }, follow=True)

    page = response.content.decode()
    assert "analyst-led" in page and "not detected by automatic collection" in page
    assert "recurring discussion" in page.lower()

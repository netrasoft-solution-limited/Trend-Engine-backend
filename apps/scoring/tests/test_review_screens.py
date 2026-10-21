"""Triage and Signal Review screens.

What is defended: the queue is in client-rank order; the page offers only the
moves the state machine allows; every decision goes through the services (so it
is validated and audited); and one client's recommendation never appears on
another client's page.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.enrichment.llm.client import LLMClient
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.intelligence.models import Signal
from apps.operations.models import OperatorUser
from apps.scoring import recommendations, signal_rank
from apps.scoring.models import Recommendation
from apps.scoring.recommendations import ProposedRecommendation
from apps.scoring.tests import test_signal_rank as shared
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db

evidence = shared.evidence
jarrow = shared.jarrow
sports_brand = shared.sports_brand


@pytest.fixture
def operator(db):
    return OperatorUser.objects.create_user(
        email="operator@netrasoft.test", password="x" * 24, name="Operator"
    )


@pytest.fixture
def ranked(jarrow, evidence):
    with scoped(jarrow):
        signal_rank.rank_for(jarrow)
    return evidence


@pytest.fixture
def proposed(jarrow, ranked):
    signal, claims = ranked
    return shared.draft(jarrow, signal, **shared.grounded(claims))[0].recommendation


def page(client, org, code="SIG-3001"):
    return client.get(reverse("ops-signal", args=[org.slug, code]))


def post(client, org, data, code="SIG-3001"):
    return client.post(reverse("ops-signal", args=[org.slug, code]), data, follow=True)


# ── Access ──────────────────────────────────────────────────────────────────


def test_a_plain_operator_can_triage(client, operator, jarrow, ranked):
    client.force_login(operator)

    assert client.get(reverse("ops-triage", args=[jarrow.slug])).status_code == 200
    assert page(client, jarrow).status_code == 200


def test_an_anonymous_visitor_cannot(client, jarrow, ranked):
    assert page(client, jarrow).status_code in (302, 403)


# ── The queue ───────────────────────────────────────────────────────────────


def test_the_queue_is_in_client_rank_order(client, operator, jarrow, ranked):
    Signal.objects.create(code="SIG-3002", title="Loud but not Jarrow's", domain_score=95)
    with scoped(jarrow):
        signal_rank.rank_for(jarrow)
    client.force_login(operator)

    signals = client.get(reverse("ops-triage", args=[jarrow.slug])).context["signals"]

    assert [s.code for s in signals] == ["SIG-3001", "SIG-3002"]
    assert signals[0].rank_score > signals[1].rank_score


def test_rejected_signals_are_out_of_the_queue_but_findable(client, operator, jarrow, ranked):
    Signal.objects.filter(code="SIG-3001").update(state=Signal.State.REJECTED)
    client.force_login(operator)

    url = reverse("ops-triage", args=[jarrow.slug])
    assert [s.code for s in client.get(url).context["signals"]] == []
    rejected = client.get(url, {"state": "rejected"}).context["signals"]
    assert [s.code for s in rejected] == ["SIG-3001"]


def test_the_index_goes_to_a_real_client_before_a_fixture(client, operator, jarrow, sports_brand):
    from apps.tenancy.context import operator_scope

    with operator_scope():
        jarrow.is_fixture = False
        jarrow.save(update_fields=["is_fixture"])
    client.force_login(operator)

    response = client.get(reverse("ops-triage-index"))

    assert response["Location"] == reverse("ops-triage", args=[jarrow.slug])


# ── The review page ─────────────────────────────────────────────────────────


def test_the_page_shows_why_and_on_what_evidence(client, operator, jarrow, ranked):
    _, claims = ranked
    client.force_login(operator)

    response = page(client, jarrow)
    body = response.content.decode()

    assert "Creators recommend magnesium forms by goal." in body
    assert {c["key"] for c in response.context["components"]} >= {"momentum", "recency"}
    assert [r.claim_id for r in response.context["contradicting"]] == [claims["doubt"].pk]
    assert "MagMind" in body, "the client match is shown"


def test_only_allowed_moves_are_offered(client, operator, jarrow, ranked):
    Signal.objects.filter(code="SIG-3001").update(state=Signal.State.APPROVED)
    client.force_login(operator)

    moves = [to for to, _ in page(client, jarrow).context["moves"]]

    assert moves == [Signal.State.IN_REVIEW]


def test_a_rejection_without_a_reason_is_refused_on_screen(client, operator, jarrow, ranked):
    client.force_login(operator)

    response = post(client, jarrow, {"action": "signal.move", "to": "rejected"})

    assert "Say why" in response.content.decode()
    assert Signal.objects.get(code="SIG-3001").state == Signal.State.CANDIDATE


# ── Recommendations through the screen ──────────────────────────────────────


def test_drafting_from_the_screen_calls_the_service(client, operator, jarrow, ranked, monkeypatch):
    _, claims = ranked
    transport = FakeTransport([respond(ProposedRecommendation(**shared.grounded(claims)))])
    monkeypatch.setattr(recommendations, "LLMClient", lambda: LLMClient(transport=transport))
    client.force_login(operator)

    response = post(client, jarrow, {"action": "rec.draft"})

    assert "Drafted." in response.content.decode()
    with scoped(jarrow):
        assert Recommendation.objects.get().state == Recommendation.State.PROPOSED


def test_an_edit_reads_claim_numbers_as_written_and_reports_removals(
    client, operator, jarrow, proposed
):
    client.force_login(operator)
    kept = proposed.actions[0]["claim_ids"][0]

    response = post(client, jarrow, {
        "action": "rec.edit",
        "why_it_matters": "Edited on screen.",
        "products": ["MagMind"],
        "owner_0": "Content/SEO",
        "action_0": "Write the guide.",
        "measure_0": "Ranks.",
        "claims_0": f"C{kept}, C999999",
    })

    proposed.refresh_from_db()
    assert proposed.state == Recommendation.State.EDITED
    assert proposed.actions[0]["claim_ids"] == [kept]
    assert proposed.products == ["MagMind"]
    assert "C999999" in response.content.decode(), "the operator is told what was removed"


def test_approve_and_reject_go_through_the_service(client, operator, jarrow, proposed):
    client.force_login(operator)

    post(client, jarrow, {"action": "rec.reject"})
    proposed.refresh_from_db()
    assert proposed.state == Recommendation.State.PROPOSED, "no reason, no rejection"

    post(client, jarrow, {"action": "rec.approve"})
    proposed.refresh_from_db()
    assert proposed.state == Recommendation.State.APPROVED
    assert proposed.decided_by_label == "operator@netrasoft.test"


def test_one_clients_recommendation_never_shows_on_anothers_page(
    client, operator, jarrow, sports_brand, proposed
):
    with scoped(sports_brand):
        signal_rank.rank_for(sports_brand)
    client.force_login(operator)

    response = page(client, sports_brand)

    assert response.context["recommendation"] is None
    assert proposed.why_it_matters not in response.content.decode()


def test_the_page_shows_effective_weights_maturity_and_coverage(client, operator, jarrow, ranked):
    from django.utils import timezone

    from apps.intelligence import detection

    signal = Signal.objects.get(code="SIG-3001")
    detection.rescore(signal, now=timezone.now())
    client.force_login(operator)

    response = page(client, jarrow)
    body = response.content.decode()

    by_key = {c["key"]: c for c in response.context["components"]}
    assert by_key["momentum"]["effective_weight"] is None
    assert by_key["source_diversity"]["effective_weight"] > 15
    assert "of the plan&#x27;s weighting" in body or "of the plan's weighting" in body
    assert signal.get_maturity_display() in body
    assert "Counts towards a baseline" in body
    assert len(response.context["coverage"]) == 6



def test_an_approval_can_be_withdrawn_with_a_reason_and_edited_again(
    client, operator, jarrow, proposed
):
    client.force_login(operator)
    post(client, jarrow, {"action": "rec.approve"})

    refused = post(client, jarrow, {"action": "rec.withdraw", "note": ""})
    assert "the reason is the record" in refused.content.decode()

    post(client, jarrow, {"action": "rec.withdraw", "note": "A comfort claim slipped in."})
    with scoped(jarrow):
        proposed.refresh_from_db()
    assert proposed.state == Recommendation.State.EDITED
    assert proposed.decision_note == "A comfort claim slipped in."
    from apps.operations.models import AuditEvent
    with scoped(jarrow):
        assert AuditEvent.objects.filter(message__startswith="Withdrew approval").exists()

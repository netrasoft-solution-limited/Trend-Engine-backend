"""Client ranking and recommendations.

PRD §14's tenancy criterion is the first thing tested here, literally: "one
public signal receives different scores for Jarrow and the second-client
fixture". Then the rules a recommendation is held to — grounded, this client's
products only, and never another client's profile in the prompt.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.clients import services as profiles
from apps.clients.models import ClientAsset, ClientTerm
from apps.enrichment.llm.client import LLMClient
from apps.enrichment.models import Claim, ModelRun
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.evidence.models import ContentItem, ContentSegment
from apps.intelligence.models import Signal, SignalEvidence
from apps.operations.models import AuditEvent
from apps.scoring import recommendations, signal_rank
from apps.scoring.models import ClientSignalScore, Recommendation
from apps.scoring.ranking import ranked_candidates
from apps.scoring.recommendations import ProposedAction, ProposedRecommendation
from apps.scoring.tasks import recommend_top
from apps.sources.models import Source
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db


# ── A shared signal, and two clients who see it differently ─────────────────


@pytest.fixture
def evidence(db):
    source = Source.objects.create(name="A podcast", route=Source.Route.PODCAST)
    claims = {}
    for n, (key, creator, text) in enumerate(
        [
            ("glycinate", "Dr A", "Magnesium glycinate before bed improves sleep in older adults"),
            ("threonate", "Dr B", "Magnesium L-threonate is the form people choose for focus"),
            ("doubt", "Dr C", "The magnesium form matters less than the dose"),
        ],
        start=1,
    ):
        item = ContentItem.objects.create(
            source=source, external_id=key, title=f"Episode {key}", creator=creator,
            content_hash=key, content_state=ContentItem.ContentState.TRANSCRIBED,
            published_at=timezone.now(),
        )
        segment = ContentSegment.objects.create(
            content_item=item, ordinal=n, text=text, start_char=0, end_char=len(text),
        )
        claims[key] = Claim.objects.create(
            content_item=item, segment=segment, kind="comparison",
            subject="Magnesium forms", text=text, quote=text, is_cited=key == "glycinate",
        )
    signal = Signal.objects.create(
        code="SIG-3001", title="Magnesium becomes a choice by goal",
        statement="Creators recommend magnesium forms by goal.",
        signal_type=Signal.Type.CONVERGENCE, domain_score=70, confidence=55,
        confidence_reasons=["+16: 2 supporting item(s)"],
    )
    roles = {"glycinate": "supports", "threonate": "supports", "doubt": "contradicts"}
    for key, role in roles.items():
        SignalEvidence.objects.create(signal=signal, claim=claims[key], role=role)
    return signal, claims


def live_profile(organization, *, assets, audiences=(), priorities=()):
    with scoped(organization):
        version = profiles.draft(organization, label="test", copy_current=False)
        for name, terms, weight in assets:
            ClientAsset.objects.create(
                organization=organization, profile=version, name=name, terms=terms, weight=weight
            )
        for facet, rows in ((ClientTerm.Facet.AUDIENCE, audiences),
                            (ClientTerm.Facet.PRIORITY, priorities)):
            for name, terms, weight in rows:
                ClientTerm.objects.create(
                    organization=organization, profile=version, facet=facet,
                    name=name, terms=terms, weight=weight,
                )
        return profiles.activate(version, actor_label="test")


@pytest.fixture
def jarrow(org_a, evidence):
    """Sells magnesium; cares about sleep and older adults."""
    live_profile(
        org_a,
        assets=[("MagMind", ["l-threonate", "threonate"], 90),
                ("Magnesium Glycinate", ["magnesium glycinate", "glycinate"], 80)],
        audiences=[("Older adults", ["older adults"], 60)],
        priorities=[("Sleep", ["sleep"], 70)],
    )
    return org_a


@pytest.fixture
def sports_brand(org_b, evidence):
    """Sells creatine; magnesium is incidental to it."""
    live_profile(org_b, assets=[("Creatine Monohydrate", ["creatine"], 95)])
    return org_b


def rank(organization):
    with scoped(organization):
        signal_rank.rank_for(organization)
        return ClientSignalScore.objects.get(organization=organization)


def test_one_public_signal_ranks_differently_for_two_clients(jarrow, sports_brand):
    """PRD §14, literally."""
    for_jarrow, for_sports = rank(jarrow), rank(sports_brand)

    assert for_jarrow.signal_id == for_sports.signal_id
    assert for_jarrow.rank_score > for_sports.rank_score
    assert for_jarrow.fit_score > 0 and for_sports.fit_score == 0


def test_the_rank_is_the_prd_formula_over_stored_components(jarrow):
    score = rank(jarrow)
    c = score.components

    assert {k: v["weight"] for k, v in c.items()} == signal_rank.RANK_WEIGHTS
    assert c["domain"]["value"] == 70
    assert c["asset"]["value"] == 90, "the strongest product match among supporting claims"
    assert c["audience"]["value"] == 60
    assert c["priority"]["value"] == 70
    assert score.rank_score == round((70 * 60 + 90 * 20 + 60 * 10 + 70 * 10) / 100)
    assert "MagMind" in c["asset"]["detail"]
    assert score.profile.is_current
    assert score.confidence == 55, "confidence is the evidence's, not the client's"


def test_a_contradicting_claim_does_not_raise_the_clients_fit(jarrow):
    """Only SUPPORTING evidence decides whether a signal is the client's business."""
    matched = {m["name"] for m in rank(jarrow).matches}

    assert matched == {"MagMind", "Magnesium Glycinate", "Older adults", "Sleep"}


def test_a_seeded_signal_is_not_ranked(jarrow):
    Signal.objects.create(code="SIG-2041", title="Seeded", domain_score=82, is_illustrative=True)

    with scoped(jarrow):
        summary = signal_rank.rank_for(jarrow)

    assert summary["ranked"] == 1


def test_a_client_without_a_profile_cannot_be_ranked(org_b, evidence):
    with scoped(org_b), pytest.raises(ValueError, match="no live client profile"):
        signal_rank.rank_for(org_b)


def test_the_candidate_list_orders_by_client_rank(jarrow, evidence):
    weak = Signal.objects.create(code="SIG-3002", title="Weak", domain_score=95)
    with scoped(jarrow):
        signal_rank.rank_for(jarrow)
        order = list(ranked_candidates(jarrow).values_list("code", flat=True))

    # SIG-3002 has the higher domain score but no evidence touching Jarrow.
    assert order.index("SIG-3001") < order.index(weak.code)


# ── Recommendations ─────────────────────────────────────────────────────────


def proposing(**fields) -> tuple[LLMClient, FakeTransport]:
    transport = FakeTransport([respond(ProposedRecommendation(**fields))])
    return LLMClient(transport=transport), transport


def grounded(claims, **overrides) -> dict:
    return {
        "why_it_matters": "Jarrow sells both forms people are choosing between.",
        "limits": "Popularity is not efficacy; one dissenting expert says dose matters more.",
        "products": ["MagMind", "magnesium glycinate"],
        "actions": [
            ProposedAction(
                owner="Content/SEO", action="Publish a 'which magnesium for which goal' guide.",
                success_measure="Ranks for 'magnesium for sleep'.",
                claim_ids=[claims["glycinate"].pk, claims["threonate"].pk],
            )
        ],
        **overrides,
    }


def draft(organization, signal, **fields):
    llm, transport = proposing(**fields)
    with scoped(organization):
        signal_rank.rank_for(organization)
        return recommendations.draft(organization, signal, client=llm), transport


def test_a_grounded_recommendation_is_stored_with_its_provenance(jarrow, evidence):
    signal, claims = evidence

    result, _ = draft(jarrow, signal, **grounded(claims))

    rec = result.recommendation
    assert rec.state == Recommendation.State.PROPOSED
    assert rec.products == ["MagMind", "Magnesium Glycinate"], "named as the profile names them"
    assert rec.actions[0]["claim_ids"] == [claims["glycinate"].pk, claims["threonate"].pk]
    assert set(rec.cited_claim_ids) == {claims["glycinate"].pk, claims["threonate"].pk}
    assert rec.model_run.purpose == ModelRun.Purpose.DRAFTING
    assert rec.profile.is_current
    with scoped(jarrow):
        assert AuditEvent.objects.filter(message__contains="Drafted a recommendation").exists()


def test_the_prompt_carries_this_clients_profile_and_no_other(jarrow, sports_brand, evidence):
    """PRD §6.4: another tenant's private context is never available to the prompt."""
    signal, claims = evidence

    _, transport = draft(jarrow, signal, **grounded(claims))

    sent = transport.calls[0]["messages"][0]["content"]
    assert "MagMind" in sent
    assert "Creatine Monohydrate" not in sent
    assert f"C{claims['doubt'].pk} | CONTRADICTS" in sent


def test_what_is_not_grounded_is_removed_and_recorded(jarrow, evidence):
    signal, claims = evidence
    fields = grounded(claims)
    fields["products"] = ["MagMind", "AG1"]
    fields["actions"] = fields["actions"] + [
        ProposedAction(owner="Ecommerce", action="Bundle with a sleep mask.",
                       success_measure="AOV", claim_ids=[999_999]),
        ProposedAction(owner="Marketing guru", action="Refresh the MagMind page FAQ.",
                       success_measure="Conversion", claim_ids=[claims["threonate"].pk, 424_242]),
    ]

    rec = draft(jarrow, signal, **fields)[0].recommendation

    assert rec.products == ["MagMind"]
    assert len(rec.actions) == 2
    assert rec.actions[1]["owner"] == "Brand", "an invented owner is not stored"
    assert rec.validation == {
        "invented_claim_ids": [999_999, 424_242],
        "uncited_actions": 1,
        "unknown_products": ["AG1"],
    }


def test_a_recommendation_with_no_grounded_action_is_not_stored(jarrow, evidence):
    signal, claims = evidence
    fields = grounded(claims)
    fields["actions"] = [ProposedAction(owner="Brand", action="Go viral.",
                                        success_measure="Views", claim_ids=[])]

    with pytest.raises(recommendations.RecommendationError, match="No action survived"):
        draft(jarrow, signal, **fields)
    with scoped(jarrow):
        assert not Recommendation.objects.exists()


def test_the_model_may_decline(jarrow, evidence):
    signal, _ = evidence

    result, _ = draft(jarrow, signal, decline_reason="Nothing here Jarrow can act on.")

    assert result.recommendation is None
    assert result.declined == "Nothing here Jarrow can act on."


def test_an_open_recommendation_is_not_drafted_over(jarrow, evidence):
    """Regenerating over an operator's edit would throw their work away."""
    signal, claims = evidence
    draft(jarrow, signal, **grounded(claims))

    with pytest.raises(recommendations.RecommendationError, match="already has an open"):
        draft(jarrow, signal, **grounded(claims))


# ── Operator review ─────────────────────────────────────────────────────────


@pytest.fixture
def proposed(jarrow, evidence):
    signal, claims = evidence
    return draft(jarrow, signal, **grounded(claims))[0].recommendation


def test_an_edit_is_held_to_the_same_rules(jarrow, proposed, evidence):
    _, claims = evidence
    raw = {
        "why_it_matters": "Edited.",
        "products": ["MagMind", "Not a Jarrow product"],
        "actions": [{"owner": "Content/SEO", "action": "Edited action.",
                     "success_measure": "x", "claim_ids": [claims["glycinate"].pk]}],
    }

    with scoped(jarrow):
        rec = recommendations.edit(proposed, raw, actor_label="abu@netrasoft")

    assert rec.state == Recommendation.State.EDITED
    assert rec.products == ["MagMind"]
    assert rec.validation["unknown_products"] == ["Not a Jarrow product"]
    assert rec.edited_by_label == "abu@netrasoft"


def test_a_rejection_needs_a_reason(jarrow, proposed):
    with scoped(jarrow), pytest.raises(recommendations.RecommendationError, match="Say why"):
        recommendations.decide(proposed, approve=False, actor_label="abu")


def test_an_approved_recommendation_cannot_be_edited(jarrow, proposed, evidence):
    with scoped(jarrow):
        recommendations.decide(proposed, approve=True, actor_label="abu")
        with pytest.raises(recommendations.RecommendationError, match="cannot be edited"):
            recommendations.edit(proposed, {"why_it_matters": "x"}, actor_label="abu")
    assert proposed.decided_by_label == "abu"


# ── The daily run ───────────────────────────────────────────────────────────


def test_a_rejected_recommendation_is_not_redrafted_every_morning(jarrow, proposed):
    with scoped(jarrow):
        recommendations.decide(proposed, approve=False, actor_label="abu", note="Off-brand.")
        llm, transport = proposing(decline_reason="unused")
        outcome = recommend_top(jarrow, client=llm)

    assert outcome == {"drafted": [], "declined": [], "failed": []}
    assert transport.calls == []

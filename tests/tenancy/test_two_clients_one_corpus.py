"""One shared corpus, two clients, two different briefs.

PRD §14's tenancy criterion: "one public signal receives different scores for
Jarrow and the second-client fixture; private context never crosses". PRD §6.7:
"one shared evidence layer, multiple tenant-scoped scoring layers". Arch §5.4
makes this suite deployment-blocking.

The property is easy to state and easy to get wrong in a way nothing notices:
every piece of evidence is visible to every client's SCORER, and no piece of one
client's profile is visible to another's. Get the first wrong and clients pay
for collection twice; get the second wrong and a competitor's strategy leaks
through a brief.

Both clients here are profiled against the SAME four claims. Nothing about the
evidence differs. If the briefs come out the same, the tailoring does nothing —
which is exactly the state this code was written to end, and is why the
assertions compare the two documents rather than checking each one alone.
"""
from __future__ import annotations

import pytest

from apps.clients import services as profiles
from apps.clients.models import ClientAsset, ClientTerm
from apps.enrichment.models import Claim
from apps.evidence.models import ContentItem, ContentSegment, TranscriptArtifact
from apps.outputs import drafting
from apps.scoring import relevance
from apps.scoring.models import ClaimRelevance
from apps.sources.models import Source
from apps.tenancy.context import operator_scope, scoped

pytestmark = [pytest.mark.django_db, pytest.mark.tenancy]


# ── One corpus, shared by everybody ─────────────────────────────────────────


@pytest.fixture
def corpus(db):
    """Four claims, none of which belongs to any tenant. L2 evidence is shared."""
    source = Source.objects.create(name="A nutrition podcast", route=Source.Route.PODCAST)
    item = ContentItem.objects.create(
        source=source,
        external_id="ep-77",
        title="Sleep, joints and the gut",
        creator="Dr Example",
        content_hash=ContentItem.hash_content("ep-77"),
        content_state=ContentItem.ContentState.TRANSCRIBED,
    )
    TranscriptArtifact.objects.create(
        content_item=item, method=TranscriptArtifact.Method.TADDY, text="…"
    )

    def claim(text, *, kind="effect", subject="Subject", ordinal):
        segment = ContentSegment.objects.create(
            content_item=item,
            ordinal=ordinal,
            text=text,
            start_char=0,
            end_char=len(text),
            start_seconds=60 * ordinal,
        )
        return Claim.objects.create(
            content_item=item,
            segment=segment,
            kind=kind,
            subject=subject,
            text=text,
            quote=text,
        )

    return {
        "magnesium": claim(
            "Magnesium glycinate improves sleep onset in older adults",
            subject="Magnesium glycinate",
            ordinal=1,
        ),
        "collagen": claim(
            "Collagen peptides reduced joint pain in runners over twelve weeks",
            subject="Collagen peptides",
            ordinal=2,
        ),
        "creatine": claim(
            "Creatine monohydrate at five grams daily is the established dose",
            kind="dosage",
            subject="Creatine monohydrate",
            ordinal=3,
        ),
        "probiotic": claim(
            "A multi-strain probiotic shifted gut diversity in eight weeks",
            subject="Probiotics",
            ordinal=4,
        ),
        # Same kind as the collagen claim, so the two land in one section and
        # the ordering inside it is decided by fit rather than by SECTION_ORDER.
        "creatine_effect": claim(
            "Creatine monohydrate improved recovery between training sessions",
            subject="Creatine monohydrate",
            ordinal=5,
        ),
    }


def profile_for(organization, *, assets, audiences=(), priorities=(), competitors=()):
    """Build and activate a profile. Returns the live version."""
    with scoped(organization):
        version = profiles.draft(organization, label="test", copy_current=False)
        for name, terms, weight in assets:
            ClientAsset.objects.create(
                organization=organization,
                profile=version,
                name=name,
                terms=terms,
                weight=weight,
            )
        for facet, rows in (
            (ClientTerm.Facet.AUDIENCE, audiences),
            (ClientTerm.Facet.PRIORITY, priorities),
            (ClientTerm.Facet.COMPETITOR, competitors),
        ):
            for name, terms, weight in rows:
                ClientTerm.objects.create(
                    organization=organization,
                    profile=version,
                    facet=facet,
                    name=name,
                    terms=terms,
                    weight=weight,
                )
        return profiles.activate(version, actor_label="test")


@pytest.fixture
def sleep_brand(org_a, corpus):
    """Sells magnesium. Cares about sleep."""
    profile_for(
        org_a,
        assets=[("Magnesium Glycinate 400mg", ["magnesium", "magnesium glycinate"], 90)],
        audiences=[("Older adults", ["older adults"], 60)],
    )
    with scoped(org_a):
        relevance.score_for(org_a)
    return org_a


@pytest.fixture
def sports_brand(org_b, corpus):
    """Sells creatine and collagen. Cares about runners."""
    profile_for(
        org_b,
        assets=[
            ("Creatine Monohydrate", ["creatine", "creatine monohydrate"], 95),
            ("Collagen Complex", ["collagen", "collagen peptides"], 70),
        ],
        audiences=[("Endurance athletes", ["runners"], 60)],
    )
    with scoped(org_b):
        relevance.score_for(org_b)
    return org_b


# ── The evidence really is shared ───────────────────────────────────────────


def test_both_clients_are_scored_against_every_claim(sleep_brand, sports_brand, corpus):
    """Shared collection is the economic argument for the whole design: a second
    client in the same category costs a fraction of the first. If scoring only
    saw a subset, that argument quietly stops being true."""
    for organization in (sleep_brand, sports_brand):
        with scoped(organization):
            assert ClaimRelevance.objects.filter(organization=organization).count() == len(
                corpus
            )


def test_the_same_claim_scores_differently_for_the_two_clients(
    sleep_brand, sports_brand, corpus
):
    """PRD §14, stated as directly as it can be."""
    with scoped(sleep_brand):
        for_sleep = ClaimRelevance.objects.get(claim=corpus["creatine"]).fit_score
    with scoped(sports_brand):
        for_sports = ClaimRelevance.objects.get(claim=corpus["creatine"]).fit_score

    assert for_sports > for_sleep, "creatine is the sports brand's product, not the sleep brand's"
    assert for_sleep == 0, "a claim matching nothing in the profile is not theirs"


def test_a_claim_nobody_sells_is_nobody_s(sleep_brand, sports_brand, corpus):
    """The probiotic claim is real evidence and belongs in neither brief. A
    scorer that found something for everyone would be matching noise."""
    for organization in (sleep_brand, sports_brand):
        with scoped(organization):
            assert ClaimRelevance.objects.get(claim=corpus["probiotic"]).fit_score == 0


# ── The briefs differ, which is the point ───────────────────────────────────


def test_two_clients_receive_different_briefs_from_one_corpus(sleep_brand, sports_brand):
    with scoped(sleep_brand):
        sleep_body = " ".join(s["text"] for s in drafting.sections_for(sleep_brand))
    with scoped(sports_brand):
        sports_body = " ".join(s["text"] for s in drafting.sections_for(sports_brand))

    assert sleep_body != sports_body

    assert "Magnesium glycinate" in sleep_body
    assert "Creatine" not in sleep_body and "Collagen" not in sleep_body

    assert "Creatine" in sports_body and "Collagen" in sports_body
    assert "Magnesium" not in sports_body


def test_matching_the_product_and_the_audience_outranks_matching_the_product_alone(
    sports_brand, corpus
):
    """The collagen claim names runners, who are this client's audience; the
    creatine claim names only the product, and is weighted higher as a product
    (95 against 70).

    Collagen still wins, and should: a claim that lands on both the thing they
    sell and the people they sell it to is more useful than one that lands on a
    heavier product alone. This is the component model doing the work it exists
    for, and it is the behaviour most likely to be "fixed" by someone who
    expects the asset weight to dominate.
    """
    with scoped(sports_brand):
        collagen = ClaimRelevance.objects.get(claim=corpus["collagen"])
        creatine = ClaimRelevance.objects.get(claim=corpus["creatine_effect"])

    assert collagen.asset_component < creatine.asset_component
    assert collagen.audience_component > 0 and creatine.audience_component == 0
    assert collagen.fit_score > creatine.fit_score

    with scoped(sports_brand):
        effects = next(
            s["text"]
            for s in drafting.sections_for(sports_brand)
            if "evidence says" in s["heading"]
        )
    assert effects.index("Collagen") < effects.index("Creatine"), "fit decides the order"


def test_fit_orders_within_a_section_but_never_across_them(sports_brand, corpus):
    """SECTION_ORDER is how a formulator reads — mechanism, then effect, then
    dose. A high-fit dosage claim jumping above the effects would be ordering
    by the wrong question, however well it fits."""
    with scoped(sports_brand):
        headings = [s["heading"] for s in drafting.sections_for(sports_brand)]

    assert headings.index("What the evidence says it does") < headings.index(
        "Doses being discussed"
    )


def test_a_brief_names_the_profile_version_it_was_built_from(sleep_brand):
    """PRD §8: one tenant, one client-profile version, one domain-pack version."""
    with scoped(sleep_brand):
        sections = drafting.sections_for(sleep_brand)

    methodology = next(s for s in sections if "based on" in s["heading"])
    assert "v1" in methodology["text"]


# ── Private context does not cross ──────────────────────────────────────────


def test_one_clients_profile_is_invisible_to_the_other(sleep_brand, sports_brand):
    """PRD §6.4: "Another tenant's private context is never available to the
    prompt or query." An asset list is commercial strategy — which products a
    brand is pushing this quarter — and is the most sensitive thing here."""
    with scoped(sleep_brand):
        visible = list(ClientAsset.objects.values_list("name", flat=True))

    assert visible == ["Magnesium Glycinate 400mg"]
    assert "Creatine Monohydrate" not in visible


def test_one_clients_scores_are_invisible_to_the_other(sleep_brand, sports_brand, corpus):
    with scoped(sleep_brand):
        assert not ClaimRelevance.objects.filter(organization=sports_brand).exists()


def test_the_scores_are_default_denied_without_a_tenant(sleep_brand):
    """Arch §5.2. The manager is the enforcement point, not the view."""
    from apps.tenancy.exceptions import TenantScopeError

    with pytest.raises(TenantScopeError):
        ClaimRelevance.objects.count()


def test_an_operator_sees_every_clients_scores(sleep_brand, sports_brand, corpus):
    """The deliberate, auditable opt-out — the operator plane compares clients."""
    with operator_scope():
        assert ClaimRelevance.objects.count() == len(corpus) * 2


# ── Refusals that matter more than they look ────────────────────────────────


def test_a_client_with_no_profile_is_refused_rather_than_sent_a_generic_brief(org_a, corpus):
    """The failure this guards against is the quiet one: a brief that renders,
    reads well, and is identical to every other client's."""
    from apps.outputs.services import DraftingError

    with scoped(org_a), pytest.raises(DraftingError, match="no active client profile"):
        drafting.sections_for(org_a)


def test_an_empty_profile_cannot_be_activated(org_a):
    """It would match nothing, and the briefs would come out empty rather than
    wrong — which is much harder to notice."""
    with scoped(org_a):
        version = profiles.draft(org_a, copy_current=False)
        with pytest.raises(profiles.ProfileError, match="empty"):
            profiles.activate(version)


def test_a_client_whose_profile_matches_nothing_is_refused(org_a, corpus):
    with scoped(org_a):
        profile_for(org_a, assets=[("Dog Food", ["kibble", "dog food"], 80)])
        relevance.score_for(org_a)

        from apps.outputs.services import DraftingError

        with pytest.raises(DraftingError, match="No stored claim matched"):
            drafting.sections_for(org_a)

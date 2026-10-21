"""New-product candidates and the AI benchmark (Mark, 20 and 21 Oct).

Defended: the gap check searches every name across names, terms and notes and
says what it found; approval needs the case, a gap check and evidence; an edit
withdraws approval; the model's case keeps only observations it was given; and
benchmark shares are counts of imported rows.
"""
from __future__ import annotations

from datetime import date

import pytest
from django.urls import reverse

from apps.clients.models import ClientAsset
from apps.enrichment.llm.client import LLMClient
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.evidence.models import Observation
from apps.operations.models import AuditEvent, OperatorUser
from apps.scoring import benchmark, opportunities
from apps.scoring.models import BenchmarkAnswer, Candidate
from apps.scoring.opportunities import ProposedOpportunity
from apps.scoring.tests import test_signal_rank as shared
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db

evidence = shared.evidence
jarrow = shared.jarrow
sports_brand = shared.sports_brand


@pytest.fixture
def catalogue(jarrow):
    with scoped(jarrow):
        profile = ClientAsset.objects.first().profile
        ClientAsset.objects.create(
            organization=jarrow, profile=profile, name="NMN NAD+ Precursor",
            terms=["nmn", "nad"], family="Healthy Aging",
        )
        ClientAsset.objects.create(
            organization=jarrow, profile=profile, name="Ergothioneine Complex",
            terms=["ergo"], family="Healthy Aging", notes="With L-ergothioneine.",
        )
    return jarrow


def observation(**kw):
    defaults = dict(
        url="https://www.tiktok.com/x", title="Spermidine listing", source_type="tiktok_shop",
        observed_at=date(2026, 10, 20), published_at=date(2026, 10, 12), entered_by="abu",
    )
    return Observation.objects.create(**{**defaults, **kw})


def create(org, name, **kw):
    with scoped(org):
        return opportunities.create(org, name=name, actor_label="abu", **kw)


def test_a_candidate_not_in_the_catalogue_says_so_with_the_count(catalogue):
    candidate = create(catalogue, "Spermidine", aliases="wheat germ extract",
                       theme="healthy aging")

    assert candidate.gap_check["found"] == []
    assert opportunities.gap_sentence(candidate) == (
        "Not found in the titles, terms or notes of 4 listings."
    )
    assert candidate.adjacent == ["Ergothioneine Complex", "NMN NAD+ Precursor"]


def test_the_gap_check_finds_a_name_in_the_notes_and_says_where(catalogue):
    candidate = create(catalogue, "L-Ergothioneine")

    found = candidate.gap_check["found"]
    assert found == [{"name": "Ergothioneine Complex", "family": "Healthy Aging",
                      "field": "notes", "term": "L-Ergothioneine"}]


def test_approval_needs_the_case_a_gap_check_and_evidence(catalogue):
    candidate = create(catalogue, "Spermidine")

    with scoped(catalogue), pytest.raises(opportunities.OpportunityError) as refused:
        opportunities.approve(candidate, actor_label="abu")
    assert "the commercial opening" in str(refused.value)
    assert "at least one observation" in str(refused.value)

    with scoped(catalogue):
        opportunities.attach(candidate, [observation().pk], actor_label="abu")
        opportunities.edit(candidate, {"opening": "A simpler aging routine.",
                                       "next_step": "Test capsule versus powder.",
                                       "gate": "Advance only with workable margins."},
                           actor_label="abu")
        opportunities.approve(candidate, actor_label="abu")
        assert candidate.is_approved

        opportunities.edit(candidate, {"gate": "Stricter."}, actor_label="abu")
    assert not candidate.is_approved, "an edit withdraws approval"
    assert AuditEvent.objects.filter(message="Approved candidate Spermidine").exists()


def test_the_drafted_case_keeps_only_observations_it_was_given(catalogue):
    candidate = create(catalogue, "Spermidine")
    shop = observation(metric_kind="lifetime_shop_sales", metric_value=4600,
                       metric_label="4.6K items sold")
    with scoped(catalogue):
        opportunities.attach(candidate, [shop.pk], actor_label="abu")
    transport = FakeTransport([respond(ProposedOpportunity(
        opening="A simpler addition to a healthy-aging routine.",
        why_it_matters=f"Purchasing is visible on one listing (O{shop.pk}, O99999).",
        test_type="customer", test_plan="Show two concepts.", next_step="Test formats.",
        gate="Workable margins.", limits="Lifetime count, not growth.",
    ))])

    with scoped(catalogue):
        opportunities.draft(candidate, actor_label="abu", client=LLMClient(transport=transport))

    assert candidate.why_it_matters == f"Purchasing is visible on one listing (O{shop.pk})."
    assert candidate.test_type == Candidate.TestType.CUSTOMER
    sent = transport.calls[0]["messages"][0]["content"]
    assert "Global lifetime items sold" in sent and "Not found in" in sent


def test_candidates_belong_to_one_client(catalogue, sports_brand):
    create(catalogue, "Spermidine")

    with scoped(sports_brand):
        assert not Candidate.objects.filter(organization=sports_brand).exists()
    create(sports_brand, "Spermidine")  # the same name is fine for another client


# ── AI benchmark ────────────────────────────────────────────────────────────

SHEET = (
    "run_date,question,assistant,model,brands,client_named,position,excerpt\n"
    "2026-10-15,Best probiotic for bloating?,ChatGPT,,Seed; Culturelle; Jarrow Formulas,,,\n"
    "2026-10-15,Best probiotic for bloating?,Perplexity,,Seed; Align,,,\n"
    "2026-10-16,Best probiotic for bloating?,Gemini,,Jarrow Formulas; Seed,yes,1,\n"
)


def test_benchmark_shares_are_counts_of_imported_rows(jarrow):
    rows, errors = benchmark.parse_csv(SHEET, client_names=["Jarrow"])
    assert errors == []
    assert [r["position"] for r in rows] == [3, None, 1]
    with scoped(jarrow):
        benchmark.save_all(jarrow, rows, entered_by="abu", batch="t")
        [s] = benchmark.summary(jarrow)
        line = benchmark.brief_lines(jarrow, [s.question])[0]

    assert s.share == "2 of 3"
    assert s.leaders[0] == ("Seed", 3)
    assert "named in 2 of 3 answers" in line and "Seed (3)" in line


def test_a_bad_benchmark_row_is_named_and_nothing_is_saved(jarrow):
    rows, errors = benchmark.parse_csv(
        "run_date,question,assistant\nnot-a-date,Q?,ChatGPT\n", client_names=["Jarrow"]
    )
    assert rows == [] and errors[0].startswith("Row 2: 'run_date'")


def test_tied_questions_with_no_answers_say_not_imported(jarrow):
    with scoped(jarrow):
        assert benchmark.brief_lines(jarrow, ["Anything?"]) == [
            "AI benchmark not imported for these questions yet."
        ]
        assert benchmark.brief_lines(jarrow, []) == []


# ── Screens ─────────────────────────────────────────────────────────────────


@pytest.fixture
def operator(db):
    return OperatorUser.objects.create_user(
        email="operator@netrasoft.test", password="x" * 24, name="Operator"
    )


def test_an_operator_runs_a_candidate_from_creation_to_approval(client, operator, catalogue):
    client.force_login(operator)
    shop = observation()

    response = client.post(reverse("ops-candidates", args=[catalogue.slug]),
                           {"name": "Spermidine", "theme": "healthy aging"}, follow=True)
    assert b"Not found in the titles" in response.content
    with scoped(catalogue):
        candidate = Candidate.objects.get(organization=catalogue, name="Spermidine")
    url = reverse("ops-candidate", args=[catalogue.slug, candidate.pk])
    client.post(url, {"action": "obs.attach", "ids": f"O{shop.pk}"})
    client.post(url, {"action": "edit", "status": "test", "priority": "1",
                      "opening": "A simpler routine.", "next_step": "Test formats.",
                      "gate": "Margins.", "test_type": "customer",
                      "internal_check": "pending"})
    response = client.post(url, {"action": "approve"}, follow=True)

    assert b"Spermidine approved for the brief" in response.content
    page = client.get(reverse("ops-candidates", args=[catalogue.slug]))
    assert b"Draft the opportunity brief from 1 candidate(s)" in page.content


def test_the_benchmark_screen_imports_and_summarises(client, operator, jarrow):
    import io

    from apps.tenancy.context import operator_scope

    with operator_scope():
        jarrow.name = "Jarrow"
        jarrow.save(update_fields=["name"])
    client.force_login(operator)
    upload = io.BytesIO(SHEET.encode())
    upload.name = "brand-test.csv"

    response = client.post(reverse("ops-benchmark", args=[jarrow.slug]),
                           {"file": upload}, follow=True)

    assert b"Imported 3 answer(s)" in response.content
    assert b"2 of 3" in response.content
    with scoped(jarrow):
        assert BenchmarkAnswer.objects.filter(organization=jarrow).count() == 3

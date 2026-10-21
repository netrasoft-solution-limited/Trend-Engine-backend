"""The client brief and the content brief.

Mark's review asked for a brief that reaches a client-specific conclusion, with
clickable sources, what changed, why it matters, and specific actions. These
tests defend that shape — and the things that must never be in it: anything
unapproved, another client's advice, sponsored evidence, and internal scores.
"""
from __future__ import annotations

import io
import re

import pytest

from apps.enrichment.llm.client import LLMClient
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.evidence.models import ContentItem
from apps.intelligence.models import Signal
from apps.outputs import briefing, content_brief, exports
from apps.outputs.content_brief import ContentBriefDraft, OutlineSection
from apps.outputs.exports.document import document_from_body
from apps.outputs.services import DraftingError
from apps.scoring import recommendations, signal_rank
from apps.scoring.tests import test_signal_rank as shared
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db

evidence = shared.evidence
jarrow = shared.jarrow
sports_brand = shared.sports_brand


@pytest.fixture
def approved(jarrow, evidence):
    """SIG-3001 approved, with Jarrow's recommendation on it approved."""
    signal, claims = evidence
    ContentItem.objects.filter(external_id="glycinate").update(
        url="https://www.youtube.com/watch?v=abc"
    )
    claims["glycinate"].segment.start_seconds = 60.0
    claims["glycinate"].segment.save(update_fields=["start_seconds"])
    rec = shared.draft(jarrow, signal, **shared.grounded(claims))[0].recommendation
    with scoped(jarrow):
        recommendations.decide(rec, approve=True, actor_label="abu")
    Signal.objects.filter(pk=signal.pk).update(state=Signal.State.APPROVED)
    rec.refresh_from_db()
    return rec


def brief_for(organization):
    with scoped(organization):
        return briefing.sections_for_brief(organization)


def text_of(brief) -> str:
    return "\n\n".join(f"## {s['heading']}\n{s['text']}" for s in brief.sections)


# ── Only what was approved ──────────────────────────────────────────────────


def test_nothing_approved_is_refused_with_what_is_waiting(jarrow, evidence):
    signal, claims = evidence
    shared.draft(jarrow, signal, **shared.grounded(claims))

    with pytest.raises(DraftingError, match="1 recommendation"):
        brief_for(jarrow)


def test_an_approved_recommendation_on_an_unapproved_signal_is_not_enough(jarrow, approved):
    Signal.objects.filter(code="SIG-3001").update(state=Signal.State.IN_REVIEW)

    with pytest.raises(DraftingError, match="Nothing is approved"):
        brief_for(jarrow)


def test_another_clients_approved_advice_never_reaches_this_brief(jarrow, sports_brand, approved):
    with scoped(sports_brand):
        signal_rank.rank_for(sports_brand)

    with pytest.raises(DraftingError):
        brief_for(sports_brand)


# ── The shape Mark asked for ────────────────────────────────────────────────


def test_the_brief_says_what_changed_why_it_matters_and_what_to_do(jarrow, approved):
    brief = brief_for(jarrow)
    headings = [s["heading"] for s in brief.sections]
    body = text_of(brief)

    assert headings[:2] == ["About this brief", "Executive summary"]
    assert "Magnesium becomes a choice by goal" in headings
    assert headings[-2:] == ["Coverage and method", "Sources"]
    assert "Next 14 days" in headings
    assert "What changed. Creators recommend magnesium forms by goal." in body
    assert f"Why it matters to {jarrow.name}. {approved.why_it_matters}" in body
    assert "Content/SEO: Publish a 'which magnesium for which goal' guide." in body
    assert "What this does not prove. Popularity is not efficacy" in body
    assert "Products concerned: MagMind, Magnesium Glycinate." in body
    assert brief.profile == "v1" and brief.developments == 1


def test_every_quote_links_to_the_moment_it_was_said(jarrow, approved):
    body = text_of(brief_for(jarrow))

    assert "[Episode glycinate](https://www.youtube.com/watch?v=abc&t=60s) at 1:00" in body
    assert "The speaker cites a study." in body


def test_disagreement_is_shown_not_dropped(jarrow, approved):
    body = text_of(brief_for(jarrow))

    assert "Where sources disagree:" in body
    assert "The magnesium form matters less than the dose" in body
    assert "1 item(s) disagree" in body


def test_no_internal_numbers_reach_the_client(jarrow, approved):
    """PRD §3.2: internal scores, vendors and models stay internal."""
    body = text_of(brief_for(jarrow)).lower()

    for internal in (
        "/100", "rank score", "fit score", "confidence", "domain score", "claude", "opus", "$"
    ):
        assert internal not in body, internal


def test_sponsored_claims_are_not_quoted(jarrow, approved, evidence):
    _, claims = evidence
    claims["threonate"].is_sponsored = True
    claims["threonate"].save(update_fields=["is_sponsored"])

    assert "L-threonate is the form people choose" not in text_of(brief_for(jarrow))


# ── Links survive every format ──────────────────────────────────────────────


@pytest.fixture
def linked_document():
    return document_from_body(
        title="Brief", type_label="Trend intelligence brief", organization_name="Jarrow",
        is_draft=False,
        body=[{
            "heading": "Evidence",
            "text": "· “Quote” — [An <episode>](https://x.test/v?a=1&t=60s) at 1:00.",
        }],
    )


def test_html_and_pdf_get_real_anchors_and_escaped_text(linked_document):
    html = exports.render(linked_document, "html").content.decode()

    assert '<a href="https://x.test/v?a=1&amp;t=60s">An &lt;episode&gt;</a>' in html


def test_markdown_keeps_markdown_links(linked_document):
    md = exports.render(linked_document, "markdown").content.decode()

    assert "[An <episode>](https://x.test/v?a=1&t=60s)" in md


def test_csv_writes_the_url_out(linked_document):
    csv = exports.render(linked_document, "csv").content.decode()

    assert "An <episode> (https://x.test/v?a=1&t=60s)" in csv


def test_word_gets_a_real_hyperlink(linked_document):
    from docx import Document

    opened = Document(io.BytesIO(exports.render(linked_document, "docx").content))
    targets = [r.target_ref for r in opened.part.rels.values() if "hyperlink" in r.reltype]

    assert targets == ["https://x.test/v?a=1&t=60s"]


def test_a_non_web_link_stays_text():
    html = exports.render(
        document_from_body(
            title="t", type_label="t", organization_name="o", is_draft=False,
            body=[{"heading": "h", "text": "[click](javascript:alert(1))"}],
        ),
        "html",
    ).content.decode()

    assert "<a href" not in html.split("<body>", 1)[1]


# ── The content brief ───────────────────────────────────────────────────────


def writing(**fields) -> LLMClient:
    defaults = dict(
        working_title="Magnesium glycinate vs L-threonate: choosing by goal",
        audience="older adults",
        core_question="Which magnesium should I take?",
        search_intent="Compare forms by goal",
        angle="Help readers choose by goal, not by hype.",
        outline=[],
        guardrails=["Do not say one form is best."],
    )
    draft = ContentBriefDraft(**{**defaults, **fields})
    return LLMClient(transport=FakeTransport([respond(draft)]))


def test_a_content_brief_needs_an_approved_recommendation(jarrow, evidence):
    signal, claims = evidence
    rec = shared.draft(jarrow, signal, **shared.grounded(claims))[0].recommendation

    with scoped(jarrow), pytest.raises(DraftingError, match="approved recommendation"):
        content_brief.sections_for(jarrow, rec, client=writing())


def test_a_content_brief_is_grounded_and_says_where_it_is_not(jarrow, approved, evidence):
    _, claims = evidence
    llm = writing(
        outline=[
            OutlineSection(heading="Why form matters", purpose="Set up the choice",
                           claim_ids=[claims["glycinate"].pk, 999_999]),
            OutlineSection(heading="What it costs", purpose="Price comparison", claim_ids=[]),
        ]
    )

    with scoped(jarrow):
        sections, title, profile = content_brief.sections_for(jarrow, approved, client=llm)
    body = {s["heading"]: s["text"] for s in sections}

    assert title.startswith("Magnesium glycinate vs")
    assert "· For: Older adults" in body["The brief"], "named as the profile names it"
    assert "· Products: MagMind, Magnesium Glycinate" in body["The brief"]
    assert "1. Why form matters — Set up the choice (draws on E1)" in body["Outline"]
    assert "2. What it costs — Price comparison (needs a source before drafting)" in body["Outline"]
    assert body["Evidence to use"].count("· E") == 1, "the invented claim number is gone"
    assert content_brief.ALWAYS in body["Claims guardrail"]
    assert profile == "v1"


# ── Wording a client actually reads ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ("One small trial on sleep latency (C8), and thin data (C5).",
         "One small trial on sleep latency, and thin data."),
        ("Both forms are discussed (C5, C8).", "Both forms are discussed."),
        ("Nothing to strip here.", "Nothing to strip here."),
        ("Confidence is low (42/100), from two items.", "Confidence is low, from two items."),
    ],
)
def test_claim_numbers_are_removed_from_client_prose(written, read):
    """The rehearsal on 13 October found "(C8)" in a brief: an operator's
    citation, meaningless to the client."""
    assert briefing.client_facing(written) == read


def test_claim_numbers_never_reach_the_brief(jarrow, approved):
    approved.why_it_matters = "Jarrow sells both forms (C5, C8). Sleep is a priority."
    approved.save(update_fields=["why_it_matters"])

    body = text_of(brief_for(jarrow))

    assert "(C5" not in body and "C8" not in body
    assert "Jarrow sells both forms. Sleep is a priority." in body


def test_a_single_day_and_a_name_ending_in_s_read_naturally(jarrow, approved):
    from apps.tenancy.context import operator_scope

    with operator_scope():
        jarrow.name = "Jarrow Formulas"
        jarrow.save(update_fields=["name"])
    sections = {s["heading"]: s["text"] for s in brief_for(jarrow).sections}

    assert "Jarrow Formulas' profile" in sections["Coverage and method"]
    about = sections["About this brief"]
    assert re.search(r" on \d{1,2} [A-Z][a-z]+ \d{4}", about) and "between" not in about


def test_the_recommendation_prompt_states_evidence_strength_in_words(jarrow, evidence):
    """Found in the 13 October rehearsal: a limits paragraph quoting "(42/100)",
    because the model had been shown the number."""
    signal, claims = evidence
    _, transport = shared.draft(jarrow, signal, **shared.grounded(claims))

    sent = transport.calls[0]["messages"][0]["content"]
    assert "/100" not in sent
    assert "1 claim(s) cite a study; 1 claim(s) disagree" in sent


# ── Never call an unmeasured development a trend (Mark, 19 Oct) ─────────────


def test_each_development_states_its_status(jarrow, approved):
    Signal.objects.filter(code="SIG-3001").update(maturity=Signal.Maturity.RECURRING)

    brief = brief_for(jarrow)
    body = text_of(brief)

    assert "Status. Recurring discussion: raised in more than one week" in body
    assert "| Recurring discussion |" in brief.sections[1]["text"]


def test_the_recommendation_prompt_carries_the_status_and_the_rule(jarrow, evidence):
    signal, claims = evidence
    Signal.objects.filter(pk=signal.pk).update(maturity=Signal.Maturity.WATCH)
    signal.refresh_from_db()

    _, transport = shared.draft(jarrow, signal, **shared.grounded(claims))

    call = transport.calls[0]
    assert "Status: Watch item." in call["messages"][0]["content"]
    assert "do not call it rising" in str(call["system"])


# ── The full content package (Mark, 20 Oct) ─────────────────────────────────


def test_the_content_package_carries_every_piece_labelled_as_a_draft(jarrow, approved, evidence):
    from apps.evidence.models import Observation
    from apps.intelligence.models import SignalObservation
    from apps.outputs.content_brief import FAQ

    _, claims = evidence
    reddit = Observation.objects.create(
        url="https://www.reddit.com/r/Supplements/x", title="Which magnesium for sleep?",
        source_type="reddit", observed_at="2026-10-20", published_at="2026-10-08",
        excerpt="Shopper cannot reconcile label numbers", entered_by="abu",
    )
    SignalObservation.objects.create(signal=approved.signal, observation=reddit, added_by="abu")
    llm = writing(
        outline=[OutlineSection(heading="Start with the question", purpose="Hook",
                                claim_ids=[claims["glycinate"].pk],
                                observation_ids=[reddit.pk, 777_777])],
        sab_specialty="A registered dietitian",
        article_draft="If the numbers on a magnesium label confuse you, you are not alone.",
        faqs=[FAQ(product="MagMind", question="How do they differ?", answer="Compare the panels."),
              FAQ(product="Some Rival Product", question="Q?", answer="A.")],
        video_script="Why magnesium labels look confusing.",
        email_subject="Which magnesium?", email_body="A label guide.",
        email_button="Read the guide",
        social_posts=["Label numbers make no sense? You are not alone."],
        ai_questions=["Which magnesium is best for sleep?"],
        what_to_measure=["Article visits", "Email clicks"],
    )

    with scoped(jarrow):
        sections, _, _ = content_brief.sections_for(jarrow, approved, client=llm)
    body = {s["heading"].split(":")[0]: s["text"] for s in sections}

    assert "draws on E1, E2" in body["Outline"], "the invented observation number is gone"
    assert "Which magnesium for sleep?" in body["Evidence to use"]
    assert content_brief.DRAFT_LABEL.format(client=jarrow.name) in body["Article draft"]
    assert "MagMind: How do they differ?" in body["Product-page FAQ drafts"]
    assert "Some Rival Product" not in body["Product-page FAQ drafts"]
    assert "Subject: Which magnesium?" in body["Email draft"]
    for heading in ("Short video script", "Social posts", "AI questions to win",
                    "What to measure", "Suggested Science Advisory Board specialty"):
        assert heading in body, heading


def test_an_observation_line_names_the_kind_of_figure(db):
    from apps.evidence.models import Observation

    o = Observation(
        url="https://www.tiktok.com/x", title="Spermidine listing", source_type="tiktok_shop",
        observed_at=__import__("datetime").date(2026, 10, 20), metric_kind="lifetime_shop_sales",
        metric_value=4600, metric_label="4.6K items sold", is_ad=True, entered_by="abu",
    )

    line = briefing.observation_line(o)

    assert "4.6K items sold (global lifetime items sold)" in line
    assert "Labelled: advertisement." in line


# ── The delivered portfolio shape (Mark, 20 Oct) ────────────────────────────


def test_the_family_map_assigns_every_listing_and_marks_the_featured_family(jarrow, approved):
    from apps.clients.models import ClientAsset

    with scoped(jarrow):
        ClientAsset.objects.filter(name="MagMind").update(family="Sleep & Mood")
        ClientAsset.objects.filter(name="Magnesium Glycinate").update(family="Minerals")
        ClientAsset.objects.create(
            organization=jarrow, profile=ClientAsset.objects.get(name="MagMind").profile,
            name="Saccharomyces Boulardii", terms=["boulardii"], family="Gut & Digestion",
        )

    sections = {s["heading"]: s["text"] for s in brief_for(jarrow).sections}

    family = sections["The full portfolio"]
    assert "Each of the 3 listings" in family
    assert "| Sleep & Mood | 1 | Featured story | MagMind |" in family
    assert "| Gut & Digestion | 1 | Monitoring | Saccharomyces Boulardii |" in family
    assert "3 listings reviewed · 3 listening families" in sections["Executive summary"]


def test_sources_are_numbered_once_and_cited_inline(jarrow, approved):
    from apps.evidence.models import Observation
    from apps.intelligence.models import SignalObservation

    reddit = Observation.objects.create(
        url="https://www.reddit.com/r/Supplements/x", title="Which magnesium for sleep?",
        source_type="reddit", observed_at="2026-10-20", published_at="2026-10-08",
        excerpt="Shopper cannot reconcile label numbers", is_brand_content=True,
        entered_by="abu",
    )
    SignalObservation.objects.create(signal=approved.signal, observation=reddit, added_by="abu")

    brief = brief_for(jarrow)
    sections = {s["heading"]: s["text"] for s in brief.sections}
    sources = sections["Sources"].splitlines()[1:]
    numbers = [re.match(r"· \[(\d+)\]", line).group(1) for line in sources]

    assert numbers == [str(n) for n in range(1, len(sources) + 1)]
    reddit_line = next(line for line in sources if "reddit.com" in line)
    n = re.match(r"· \[(\d+)\]", reddit_line).group(1)
    assert "8 Oct 2026" in reddit_line and "brand content" in reddit_line.lower()
    assert "Shopper cannot reconcile label numbers" in text_of(brief)
    assert f"[{n}]" in sections["Magnesium becomes a choice by goal"]
    assert "1 Reddit" in sections["Coverage and method"]


def test_outline_numbers_hold_when_a_later_section_adds_a_claim(jarrow, approved, evidence):
    from apps.evidence.models import Observation
    from apps.intelligence.models import SignalObservation

    _, claims = evidence
    reddit = Observation.objects.create(
        url="https://www.reddit.com/r/Supplements/x", title="Which magnesium for sleep?",
        source_type="reddit", observed_at="2026-10-20", entered_by="abu",
    )
    SignalObservation.objects.create(signal=approved.signal, observation=reddit, added_by="abu")
    first, second = claims["glycinate"].pk, claims["threonate"].pk
    llm = writing(outline=[
        OutlineSection(heading="Open", purpose="Hook", claim_ids=[first],
                       observation_ids=[reddit.pk]),
        OutlineSection(heading="Compare", purpose="Explain", claim_ids=[second]),
    ])

    with scoped(jarrow):
        sections, _, _ = content_brief.sections_for(jarrow, approved, client=llm)
    body = {s["heading"].split(":")[0]: s["text"] for s in sections}

    assert "Open — Hook (draws on E1, E3)" in body["Outline"]
    assert "E3. " in body["Evidence to use"] and "reddit.com" in body["Evidence to use"]
    assert "[1]" not in body["Evidence to use"], "E-numbers only, no source numbers"


def test_a_story_carries_its_content_plan_and_says_which_version(jarrow, approved):
    from apps.outputs import services
    from apps.outputs.models import OutputType

    with scoped(jarrow):
        services.draft(
            jarrow, type=OutputType.CONTENT_BRIEF, title="Which magnesium?", actor_label="abu",
            recommendation=approved,
            sections=[
                {"heading": "The brief", "text": "· Working title: Which magnesium for you?"},
                {"heading": "Outline", "text": "· 1. Open — Hook (draws on E1, E2)"},
                {"heading": "Suggested Science Advisory Board specialty",
                 "text": "A registered dietitian."},
                {"heading": "Short video script", "text": "Labels confuse."},
                {"heading": "Product-page FAQ drafts",
                 "text": "Draft label.\n\nMagMind: How do they differ?\n· Compare panels."},
            ],
        )

    story = {s["heading"]: s["text"] for s in brief_for(jarrow).sections}[
        "Magnesium becomes a choice by goal"
    ]

    assert (
        "From content package v1 (a draft, not yet approved): “Which magnesium for you?”"
    ) in story
    assert "Suggested Science Advisory Board specialty. A registered dietitian." in story
    assert "Article plan:\n· 1. Open — Hook\n" in story, "package-local E-numbers dropped"
    assert "All the ways to use it: product-page FAQs and a short video." in story
    assert "MagMind: How do they differ?" in story


def test_a_story_without_a_package_has_no_content_plan(jarrow, approved):
    assert "The content plan" not in text_of(brief_for(jarrow))


def test_an_observation_with_no_public_page_names_where_it_came_from(db):
    from apps.evidence.models import Observation

    o = Observation(
        url="", title="spermidine", source_type="search_estimate",
        observed_at=__import__("datetime").date(2026, 10, 20),
        metric_kind="rolling_monthly_search_estimate", metric_value=14800, query="spermidine",
        provider="Keyword data", geography="US", period="12-month average",
        provenance="Pure Play opportunity brief [2]", entered_by="abu",
    )

    assert "spermidine (no public link; Pure Play opportunity brief [2])" in (
        briefing.observation_line(o)
    )
    assert "](" not in briefing.observation_line(o)

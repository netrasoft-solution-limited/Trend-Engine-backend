"""Prose for the briefs, held to the brief's own sources (Mark, 20 Nov)."""
from apps.enrichment.llm.client import LLMClient
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.outputs import narrative

SECTIONS = [
    {"heading": "Decision summary", "text": "1. Magnesium story"},
    {"heading": "A1. Magnesium becomes a choice by goal",
     "text": "Status. Recurring discussion.\\n· “Glycinate before bed” — Dr A [1]\\n· Dr B [2]"},
    {"heading": "Appendix D: Sources", "text": "· [1] x\\n· [2] y\\n· [3] z"},
]
ITEMS = [("A1. Magnesium becomes a choice by goal", "Magnesium becomes a choice by goal",
          "recurring")]


def test_only_cited_sentences_from_the_items_own_sources_survive():
    text = ("Creators choose magnesium by goal [1]. Glycinate is a rising trend [2]. "
            "It cures insomnia. A study shows more [3]. Review the glycinate page [R].")

    kept = narrative.checked(text, {"1", "2"}, "recurring")

    assert kept == "Creators choose magnesium by goal [1]. Review the glycinate page."


def test_the_prose_is_placed_at_the_top_and_in_its_story(db, settings):
    settings.LLM_PER_CALL_MAX_USD = "1.00"  # the drafting tier's ceiling, as on production
    transport = FakeTransport([respond(narrative.Narrative(
        summary="Magnesium is chosen by goal [1]. Nobody knows why.",
        items=[narrative.Paragraph(index=1, text="People pick glycinate for sleep [1].")],
    ))])

    written = narrative.write(SECTIONS, items=ITEMS, client=LLMClient(transport=transport))
    sections = narrative.apply(SECTIONS, written, ITEMS, summary_heading="Decision summary")

    assert sections[0]["text"].startswith("Magnesium is chosen by goal [1].\n\n1.")
    assert "Nobody knows why" not in sections[0]["text"]
    assert sections[1]["text"].startswith("In brief. People pick glycinate for sleep [1].")


def test_without_a_model_the_brief_is_unchanged(db):
    assert narrative.apply(SECTIONS, None, ITEMS, summary_heading="Decision summary") == SECTIONS

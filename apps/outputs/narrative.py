"""Written prose for a brief, from what the brief already contains.

Mark, 20 Nov: the briefs must be "in clear, useful language". Assembled from
approved fragments they read as lists, and Pure Play rewrote them by hand.
This adds what a writer adds — a short summary at the top and a plain-language
paragraph for each story or candidate — under three rules:

  · The model sees only the brief's own sections, so it can say nothing the
    brief does not already support.
  · Every sentence ends with the source numbers it rests on, or [R] when it
    restates the approved recommendation. A sentence with no citation, or with
    a number that is not in that story's section, is dropped.
  · Trend words are checked against each story's status, as for approved text.

One model call per brief. If the call cannot be made, the brief is built
without the prose, and says nothing it cannot back.
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from apps.enrichment.llm.client import (
    LLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    cached_system,
    drafting_tier,
)
from apps.enrichment.llm.costs import CostCapExceeded
from apps.enrichment.models import ModelRun

logger = logging.getLogger(__name__)

PROMPT_VERSION = "narrative/2026-11-21"

INSTRUCTIONS = """You write for a brand's commercial team: plain, specific, \
calm British English. You are given sections of a research brief that has already \
been checked and approved. Write:

1. `summary`: 3 to 5 sentences for the top of the brief: what changed in this \
period, what matters most for the brand, and what to do first.
2. `items`: for each numbered story or candidate, one paragraph of 2 to 4 sentences \
that a busy reader can act on: what is happening, why it matters to the brand's \
products or portfolio, and the recommended next step.

Rules:
- Use ONLY what the sections say. Add no facts, numbers, studies, products or claims.
- End EVERY sentence with its sources in square brackets: the source numbers shown \
in that story's section, e.g. [3] or [3, 7], or [R] when the sentence restates the \
recommended action or why it matters. A sentence without a citation will be deleted.
- Describe a story's status exactly as given. Never call anything rising, growing, \
surging or a trend unless its status is "Measured rising trend".
- Make no health or efficacy claims; report what people said as what they said.
- Do not mention internal scores, ranks, models or this process."""


class Paragraph(BaseModel):
    index: int = Field(description="The story or candidate number given.")
    text: str


class Narrative(BaseModel):
    summary: str
    items: list[Paragraph] = Field(default_factory=list)


_CITE = re.compile(r"\[([0-9R][0-9R,\s]*)\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"])")


def _allowed_numbers(text: str) -> set[str]:
    return {n for group in re.findall(r"\[(\d+(?:\s*,\s*\d+)*)\]", text)
            for n in re.split(r"\s*,\s*", group)}


def checked(text: str, allowed: set[str], maturity: str | None = None) -> str:
    """Keep only sentences whose every citation is allowed; [R] is shown as nothing."""
    from apps.scoring.recommendations import status_conflicts

    kept = []
    for sentence in _SENTENCE.split((text or "").strip()):
        groups = _CITE.findall(sentence)
        if not groups:
            continue
        cited = {c.strip() for g in groups for c in g.split(",") if c.strip()}
        if any(c != "R" and c not in allowed for c in cited):
            continue
        if maturity is not None and status_conflicts({"why_it_matters": sentence}, maturity):
            continue
        cleaned = _CITE.sub(
            lambda m: "" if set(x.strip() for x in m.group(1).split(",")) == {"R"}
            else "[" + ", ".join(x.strip() for x in m.group(1).split(",") if x.strip() != "R")
            + "]",
            sentence,
        )
        kept.append(re.sub(r"\s+([.,;])", r"\1", re.sub(r"\s{2,}", " ", cleaned)).strip())
    return " ".join(kept)


def write(sections: list[dict], *, items: list[tuple[str, str, str | None]],
          client: LLMClient | None = None) -> Narrative | None:
    """`items`: (heading of the item's section, label shown to the model, maturity)."""
    by_heading = {s["heading"]: s["text"] for s in sections}
    brief_text = "\n\n".join(f"## {s['heading']}\n{s['text']}" for s in sections
                             if not s["heading"].startswith(("Appendix D", "Appendix E")))
    listing = "\n".join(f"{n}. {label}" for n, (_, label, _) in enumerate(items, start=1))
    try:
        result = (client or LLMClient()).structured(
            schema=Narrative,
            tier=drafting_tier(),
            purpose=ModelRun.Purpose.DRAFTING,
            prompt_version=PROMPT_VERSION,
            system=cached_system(INSTRUCTIONS),
            user=f"Numbered items:\n{listing}\n\nThe brief:\n\n{brief_text[:120000]}",
        )
    except (CostCapExceeded, LLMUnavailable, LLMInvalidOutput) as exc:
        logger.warning("Brief written without prose: %s", exc)
        return None

    everything = _allowed_numbers(brief_text)
    summary = checked(result.summary, everything)
    paragraphs = []
    for paragraph in result.items:
        if not 1 <= paragraph.index <= len(items):
            continue
        heading, _, maturity = items[paragraph.index - 1]
        allowed = _allowed_numbers(by_heading.get(heading, ""))
        text = checked(paragraph.text, allowed, maturity)
        if text:
            paragraphs.append(Paragraph(index=paragraph.index, text=text))
    return Narrative(summary=summary, items=paragraphs)


def apply(sections: list[dict], narrative: Narrative | None,
          items: list[tuple[str, str, str | None]], *, summary_heading: str) -> list[dict]:
    """Put the summary at the top of the decision summary and each paragraph at
    the top of its item's section."""
    if narrative is None:
        return sections
    by_index = {p.index: p.text for p in narrative.items}
    headings = {heading: by_index.get(n) for n, (heading, _, _) in enumerate(items, start=1)}
    out = []
    for section in sections:
        text = section["text"]
        if section["heading"] == summary_heading and narrative.summary:
            text = f"{narrative.summary}\n\n{text}"
        elif headings.get(section["heading"]):
            text = f"In brief. {headings[section['heading']]}\n\n{text}"
        out.append({**section, "text": text})
    return out

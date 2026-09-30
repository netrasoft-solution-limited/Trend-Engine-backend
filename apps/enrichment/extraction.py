"""Extraction — claims and questions, each tied to the span that said it.

PRD §5 principle 1: "Evidence precedes recommendations. No recommendation
exists without a stored signal and supporting evidence spans."

That makes the hard part of this module not the extraction but the ANCHORING.
A model will happily return a claim with a quote that does not appear in the
text, or paraphrase while insisting it is quoting. A claim nobody can locate is
an assertion, and PRD §6.6's citation gate — "quoted and paraphrased source
claims map to stored evidence spans" — exists to stop those reaching a client.
So anything that cannot be anchored to a real segment is DROPPED, loudly.

Arch §10.3: "Extraction runs once per content item, ever. Results are
persisted; re-scoring reuses stored extractions rather than re-calling the
model." This module refuses to re-extract an item that already has claims.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.db import transaction
from pydantic import BaseModel, Field

from apps.evidence.models import ContentItem, ContentSegment

from .llm.client import (
    LLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    cached_system,
    extraction_tier,
)
from .llm.costs import CostCapExceeded
from .models import Claim, Question

logger = logging.getLogger(__name__)

PROMPT_VERSION = "extraction/2026-09-29"

#: Segments are chunked to roughly this many characters. Small enough that a
#: quote resolves to a useful location, large enough that a claim spanning two
#: sentences is not split down the middle.
SEGMENT_CHARS = 1200


class ExtractedClaim(BaseModel):
    segment_ordinal: int = Field(description="Which numbered segment this came from.")
    kind: str = Field(description="mechanism | effect | dosage | safety | comparison | market")
    subject: str = Field(description="What the claim is about, in domain terms.", max_length=200)
    text: str = Field(description="The claim in one plain sentence.")
    quote: str = Field(description="The exact words from the segment. Verbatim, not paraphrased.")
    is_cited: bool = Field(description="True only if the speaker cited a study or source.")
    is_sponsored: bool = Field(description="True if this sits inside a sponsored segment.")


class ExtractedQuestion(BaseModel):
    segment_ordinal: int
    text: str = Field(description="The question as asked.")
    topic: str = Field(default="", max_length=200)


class Extraction(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)
    questions: list[ExtractedQuestion] = Field(default_factory=list)


@dataclass(frozen=True)
class ExtractionResult:
    claims: int
    questions: int
    dropped_unanchored: int
    skipped: str = ""


INSTRUCTIONS = """You extract structured evidence from transcripts for a category \
intelligence system.

The text is presented as numbered segments. Every claim and question you return \
must name the segment it came from, and every claim must carry a quote copied \
EXACTLY from that segment — character for character. Do not tidy the grammar, \
do not merge two sentences, do not paraphrase into the quote field. A quote \
that does not appear verbatim in its segment will be discarded and the claim \
lost, so copy rather than compose.

Extract only what is actually asserted. A host wondering aloud is not a claim. \
An ad read is a claim, but mark it sponsored.

`is_cited` means the speaker referenced a study, a trial or a named source. \
Practitioner opinion is not citation. This distinction matters more than \
almost anything else you return: downstream, cited and uncited claims are \
weighed differently, and marking opinion as cited would let commentary pass as \
evidence.

Return an empty list rather than inventing content. Many segments contain \
nothing extractable, and that is a correct answer."""


def _segment(text: str, size: int = SEGMENT_CHARS) -> list[tuple[int, int, str]]:
    """Split text into (start_char, end_char, body).

    Breaks on sentence boundaries where one is near the target, so a quote is
    unlikely to straddle two segments. Offsets are into the ORIGINAL string, so
    a span survives re-segmentation at a different size later.
    """
    chunks: list[tuple[int, int, str]] = []
    position = 0
    length = len(text)

    while position < length:
        end = min(position + size, length)
        if end < length:
            window = text[position:end]
            # Prefer a sentence end in the last quarter of the window.
            match = None
            for match in re.finditer(r"[.!?]\s", window):
                pass
            if match and match.end() > size * 0.75:
                end = position + match.end()
        body = text[position:end].strip()
        if body:
            chunks.append((position, end, body))
        position = end

    return chunks


def build_segments(item: ContentItem) -> list[ContentSegment]:
    """Persist the segments for an item, once.

    Returns existing segments unchanged if they are already there — the
    ordinals are referenced by claims, so re-segmenting an item would orphan
    every span pointing at it.
    """
    existing = list(item.segments.all())
    if existing:
        return existing

    transcript = getattr(item, "transcript", None)
    if transcript is None or not transcript.text.strip():
        return []

    segments = [
        ContentSegment(
            content_item=item,
            ordinal=ordinal,
            text=body,
            start_char=start,
            end_char=end,
        )
        for ordinal, (start, end, body) in enumerate(_segment(transcript.text), start=1)
    ]
    ContentSegment.objects.bulk_create(segments)
    return list(item.segments.all())


def _render(segments: list[ContentSegment]) -> str:
    return "\n\n".join(f"[segment {s.ordinal}]\n{s.text}" for s in segments)


def _anchored(quote: str, segment: ContentSegment) -> bool:
    """Whether the quote really appears in the segment it names.

    Whitespace is normalised before comparing, because a model reflowing a line
    break is not the failure this guards against — inventing the sentence is.
    """
    if not quote.strip():
        return False
    normalise = lambda s: " ".join(s.split()).casefold()  # noqa: E731
    return normalise(quote) in normalise(segment.text)


@transaction.atomic
def extract(
    item: ContentItem,
    *,
    domain_pack: str,
    client: LLMClient | None = None,
) -> ExtractionResult:
    """Extract claims and questions from one item, once.

    Skips — without calling the model — when the item is metadata-only, when it
    has no transcript, or when it has already been extracted. Each of those is
    a case where calling would spend money to learn nothing.
    """
    if item.is_metadata_only:
        # Arch §7.1: metadata-only items are surfaced honestly as such and are
        # never treated as understood content. Extracting from a title would
        # manufacture exactly the false confidence that rule prevents.
        return ExtractionResult(0, 0, 0, skipped="metadata-only")

    if item.claims.exists() or item.questions.exists():
        return ExtractionResult(
            item.claims.count(), item.questions.count(), 0, skipped="already extracted"
        )

    segments = build_segments(item)
    if not segments:
        return ExtractionResult(0, 0, 0, skipped="no transcript")

    by_ordinal = {s.ordinal: s for s in segments}
    llm = client or LLMClient()

    try:
        result = llm.structured(
            schema=Extraction,
            tier=extraction_tier(),
            purpose="extraction",
            prompt_version=PROMPT_VERSION,
            system=cached_system(f"{INSTRUCTIONS}\n\n## The domain\n\n{domain_pack}"),
            user=_render(segments),
            content_item=item,
        )
    except (CostCapExceeded, LLMUnavailable, LLMInvalidOutput) as exc:
        logger.warning("Extraction failed for %s: %s", item.pk, exc)
        return ExtractionResult(0, 0, 0, skipped=str(exc))

    kinds = {choice for choice, _ in Claim.Kind.choices}
    claims: list[Claim] = []
    dropped = 0

    for candidate in result.claims:
        segment = by_ordinal.get(candidate.segment_ordinal)
        if segment is None or not _anchored(candidate.quote, segment):
            # The claim named a segment that does not exist, or quoted words
            # that are not in it. Either way it cannot be evidenced, so it does
            # not become a row. Counted, so a prompt regression shows up as a
            # rising drop rate rather than as quietly thinner extraction.
            dropped += 1
            continue

        claims.append(
            Claim(
                content_item=item,
                segment=segment,
                kind=candidate.kind if candidate.kind in kinds else Claim.Kind.EFFECT,
                subject=candidate.subject,
                text=candidate.text,
                quote=candidate.quote,
                is_cited=candidate.is_cited,
                is_sponsored=candidate.is_sponsored,
            )
        )

    questions = [
        Question(
            content_item=item,
            segment=by_ordinal[q.segment_ordinal],
            text=q.text,
            topic=q.topic,
        )
        for q in result.questions
        if q.segment_ordinal in by_ordinal
    ]

    Claim.objects.bulk_create(claims)
    Question.objects.bulk_create(questions)

    if dropped:
        logger.warning(
            "Dropped %s unanchored claim(s) on item %s — quotes not found in their segments",
            dropped, item.pk,
        )

    return ExtractionResult(len(claims), len(questions), dropped)

"""The relevance gate — Arch §6.2.

    "The cheap metadata-only relevance check before full transcription is what
     makes the cost model work: ~65 episodes/month become ~26 transcripts.
     This is load-bearing."

Four design requirements, all of them enforced below rather than described:

  · Runs on title, description, show notes, chapters and guest info ONLY —
    never fetches audio or a full transcript first. That is the whole point:
    the gate exists to avoid the expensive step, so a gate that reads the
    expensive thing is not a gate.
  · Uses the cheapest viable model tier. Classification, not reasoning.
  · SAMPLES ~5% OF REJECTIONS and puts them through anyway. "Without this, the
    filter quietly calcifies around what you already know."
  · Stores rejection reasons, so filter quality is auditable and tunable.

The sampling is the part most likely to be removed by someone optimising, and
it is the part that protects against the failure that has no symptom: a filter
that gets narrower every month and reports a rising pass rate while doing it.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from django.utils import timezone
from pydantic import BaseModel, Field

from apps.evidence.models import ContentItem

from .llm.client import LLMClient, LLMInvalidOutput, LLMUnavailable, cached_system, gate_tier
from .llm.costs import CostCapExceeded

logger = logging.getLogger(__name__)

#: Bump when the prompt below changes. It is written to every ModelRun, so a
#: shift in pass rate can be attributed to a prompt change rather than guessed
#: at (Arch §8.2).
PROMPT_VERSION = "gate/2026-09-29"

#: Arch §6.2's "~5% of rejections". Not a constant to tune away: at 65 items a
#: month this is roughly two items, which is the price of knowing the taxonomy
#: is still the right shape.
SAMPLE_RATE = 0.05


class GateVerdict(BaseModel):
    """What the gate is allowed to answer.

    Deliberately narrow. A classifier that can return prose will, and then
    something downstream will start parsing the prose.
    """

    relevant: bool = Field(
        description="True if this item plausibly discusses the domain, even in passing."
    )
    reason: str = Field(
        description="One sentence. If rejecting, name what made it off-topic.",
        max_length=300,
    )
    confidence: int = Field(ge=0, le=100, description="0-100 confidence in the verdict.")


@dataclass(frozen=True)
class GateResult:
    decision: str
    reason: str
    confidence: int
    sampled: bool = False


INSTRUCTIONS = """You screen podcast episodes, videos and articles for a category \
intelligence system. You see ONLY the metadata — a title, a description, show \
notes, chapter titles and guest names. You never see the full transcript, and \
you must not pretend to.

Your job is cheap triage, not judgement. Something that plausibly touches the \
domain should pass, because the cost of a wrong rejection (a missed \
development) is much higher than the cost of a wrong acceptance (one wasted \
transcription).

Pass an item if the domain is discussed at all, even in one segment. Reject \
only when the metadata makes clear the item is about something else entirely.

Be specific in the reason. "Off-topic" is useless to the person tuning this; \
"interview about marathon training, no supplement or ingredient discussion" is \
what lets them see the filter drifting."""


def _domain_prompt(domain_pack: str) -> list[dict]:
    """The cached half is the instructions plus the taxonomy — identical for
    every item in a run, so it is read back at a tenth of the input price.
    Nothing item-specific goes in here; see `cached_system`."""
    return cached_system(f"{INSTRUCTIONS}\n\n## The domain\n\n{domain_pack}")


def _describe(item: ContentItem) -> str:
    """Only the fields Arch §6.2 permits the gate to see."""
    lines = [f"Title: {item.title}"]
    if item.creator:
        lines.append(f"Creator: {item.creator}")
    if item.published_at:
        lines.append(f"Published: {item.published_at:%Y-%m-%d}")
    if item.description:
        lines.append(f"\nDescription:\n{item.description[:4000]}")

    extras = item.gate_metadata or {}
    for label, key in (("Show notes", "show_notes"), ("Chapters", "chapters"), ("Guests", "guests")):
        value = extras.get(key)
        if not value:
            continue
        rendered = "\n".join(str(v) for v in value) if isinstance(value, list) else str(value)
        lines.append(f"\n{label}:\n{rendered[:2000]}")

    return "\n".join(lines)


def assess(
    item: ContentItem,
    *,
    domain_pack: str,
    client: LLMClient | None = None,
    sample_rate: float = SAMPLE_RATE,
    rng: random.Random | None = None,
) -> GateResult:
    """Screen one item and persist the verdict.

    Returns without calling the model if the item has already been assessed —
    the gate is idempotent, because a retried run must not re-bill for work
    already done.
    """
    if item.gate_decision != ContentItem.GateDecision.PENDING:
        return GateResult(
            decision=item.gate_decision, reason=item.gate_reason, confidence=0
        )

    llm = client or LLMClient()
    try:
        verdict = llm.structured(
            schema=GateVerdict,
            tier=gate_tier(),
            purpose="relevance_gate",
            prompt_version=PROMPT_VERSION,
            system=_domain_prompt(domain_pack),
            user=_describe(item),
            content_item=item,
        )
    except (CostCapExceeded, LLMUnavailable, LLMInvalidOutput) as exc:
        # Leave it PENDING. A budget stop or an outage must never be recorded
        # as a content decision — an item nobody could afford to assess is not
        # an item that was found irrelevant, and the next run should retry it.
        logger.warning("Gate could not assess %s: %s", item.pk, exc)
        return GateResult(decision=ContentItem.GateDecision.PENDING, reason=str(exc), confidence=0)

    sampled = False
    if verdict.relevant:
        decision = ContentItem.GateDecision.RELEVANT
    else:
        # Arch §6.2's guard against calcification. Decided per item, after the
        # verdict, so the sample is drawn from real rejections rather than from
        # a queue someone pre-filtered.
        chance = rng.random() if rng else random.random()
        sampled = chance < sample_rate
        decision = (
            ContentItem.GateDecision.SAMPLED if sampled else ContentItem.GateDecision.REJECTED
        )

    item.gate_decision = decision
    item.gate_reason = verdict.reason
    item.gate_assessed_at = timezone.now()
    item.save(update_fields=["gate_decision", "gate_reason", "gate_assessed_at"])

    if sampled:
        logger.info(
            "Sampled a rejection for full processing (%s): %s", item.pk, verdict.reason
        )

    return GateResult(
        decision=decision, reason=verdict.reason, confidence=verdict.confidence, sampled=sampled
    )


def needs_transcript(item: ContentItem) -> bool:
    """Whether to spend money transcribing this item.

    Both RELEVANT and SAMPLED pass — that is what "puts them through full
    processing anyway" means. A sampled item that turns out to be relevant is
    the signal that the taxonomy has a gap.
    """
    return item.gate_decision in {
        ContentItem.GateDecision.RELEVANT,
        ContentItem.GateDecision.SAMPLED,
    }


def funnel_stats(queryset=None) -> dict:
    """The numbers behind Arch §6.2's ~65 -> ~26.

    Surfaced on the Ingestion Runs screen so the funnel is visible rather than
    asserted, and so a pass rate drifting upward month over month is something
    someone can see.
    """
    items = queryset if queryset is not None else ContentItem.objects.all()
    counts = {
        decision: items.filter(gate_decision=decision).count()
        for decision, _ in ContentItem.GateDecision.choices
    }
    assessed = sum(
        counts[d]
        for d in (
            ContentItem.GateDecision.RELEVANT,
            ContentItem.GateDecision.REJECTED,
            ContentItem.GateDecision.SAMPLED,
        )
    )
    rejected = counts[ContentItem.GateDecision.REJECTED] + counts[ContentItem.GateDecision.SAMPLED]
    return {
        "discovered": items.count(),
        "assessed": assessed,
        "passed": counts[ContentItem.GateDecision.RELEVANT],
        "rejected": rejected,
        "sampled": counts[ContentItem.GateDecision.SAMPLED],
        "pending": counts[ContentItem.GateDecision.PENDING],
        "pass_rate": round(counts[ContentItem.GateDecision.RELEVANT] / assessed, 3)
        if assessed
        else None,
    }

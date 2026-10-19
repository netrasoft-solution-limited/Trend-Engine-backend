"""Signal detection — connecting claims across sources into a development.

Mark's 8 October review named this as the missing centre of the product:
"identify what is changing across sources". Extraction gives single claims, each
tied to the words that said it. This module finds the claims from DIFFERENT
items that are about the same development, states what changed, and keeps the
claims that cut the other way.

WHY A MODEL GROUPS AND ARITHMETIC SCORES. Deciding that "glycinate before bed",
"L-threonate for focus" and "which magnesium should I take?" are one
development is a judgement about meaning — embeddings would be the usual tool,
and Anthropic has no embeddings endpoint (recorded in the README). How much
that development matters is NOT a judgement call: it is counted, in
`components.py`, so the operator can see and dispute every number.

WHAT IS NEVER TRUSTED FROM THE MODEL:
  · a claim ID it was not given — dropped, and counted
  · a "signal" resting on one item — dropped, and counted. One episode saying
    something five times is not a cross-source development (PRD §6.3)
  · a signal type outside PRD §6.3's list — coerced to convergence
  · extending a signal code that is not open — becomes a new signal
The model's job ends at grouping and wording. Every score is computed here.

One call per run, over the whole window, because grouping is a comparison
across items: it cannot be done one item at a time. The call is cost-capped by
the client like every other.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Min
from django.db.models.functions import Coalesce
from django.utils import timezone
from pydantic import BaseModel, Field

from apps.enrichment.domain import DOMAIN_PACK_VERSION, domain_pack
from apps.enrichment.llm.client import (
    LLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    cached_system,
    extraction_tier,
)
from apps.enrichment.llm.costs import CostCapExceeded
from apps.enrichment.models import Claim, ModelRun
from apps.evidence.models import ContentItem

from . import components
from .models import Signal, SignalEvidence

logger = logging.getLogger(__name__)

PROMPT_VERSION = "detection/2026-10-12"

#: How far back a run looks for evidence. Matches the momentum baseline, so a
#: development's history is in view when it is detected.
WINDOW_DAYS = 35

#: The most claims one call is given, newest first. Bounds the cost of a run
#: whatever the corpus grows to; at roughly 60 tokens a claim this is well
#: inside one call's budget.
MAX_CLAIMS = 400

#: Signals that new evidence may still be attached to.
OPEN_STATES = (
    Signal.State.CANDIDATE,
    Signal.State.IN_REVIEW,
    Signal.State.WATCHING,
    Signal.State.APPROVED,
)


# ── What the model returns ──────────────────────────────────────────────────


class DetectedSignal(BaseModel):
    extends_code: str = Field(
        default="",
        description="The code of an existing open signal this continues, or empty for a new one.",
    )
    title: str = Field(description="A short name for the development.", max_length=160)
    statement: str = Field(
        description="What changed, in one or two plain sentences, from these claims only."
    )
    signal_type: str = Field(description="One of the signal types listed in the instructions.")
    supporting_claim_ids: list[int] = Field(description="Claim numbers that support it.")
    contradicting_claim_ids: list[int] = Field(
        default_factory=list, description="Claim numbers that cut against it."
    )
    needs_scientific_routing: bool = Field(
        description="True if it turns on health or clinical claims an expert must assess."
    )


class Detection(BaseModel):
    signals: list[DetectedSignal] = Field(default_factory=list)


INSTRUCTIONS = f"""You connect evidence into developments for a category \
intelligence system.

You are given numbered claims, each extracted verbatim from a podcast episode, \
video or paper, with the item it came from (I-number), its creator, its source \
type and its date. Find DEVELOPMENTS: something changing in the category that \
several independent items speak to — a topic rising or resurfacing, sources \
converging on a view, a competitor move, a question consumers keep asking, an \
opportunity for a product, a contradiction or safety concern, or new research.

Rules:
- A development must be supported by claims from AT LEAST TWO DIFFERENT items \
(different I-numbers). One item repeating itself is not a development; leave it out.
- Use only the claim numbers you were given. Never invent one.
- Put claims that disagree with a development in contradicting_claim_ids. Keep \
disagreement visible; do not drop it.
- The statement says what changed, using only what the claims say. No advice, no \
claims about any brand or product the claims do not mention, no efficacy claims \
beyond what was said.
- If a development continues one of the existing open signals listed, set \
extends_code to that signal's code instead of creating a duplicate.
- signal_type is one of: {", ".join(Signal.Type.values)}.
- needs_scientific_routing is true when the development rests on health, \
clinical or safety claims that need an expert before anyone acts on them.
- Returning few developments, or none, is a correct answer when the evidence \
does not support more. Do not stretch thin evidence into a trend."""


# ── The run ─────────────────────────────────────────────────────────────────


@dataclass
class DetectionResult:
    claims_considered: int = 0
    created: list[str] = field(default_factory=list)
    extended: list[str] = field(default_factory=list)
    dropped_unknown_ids: int = 0
    dropped_single_source: int = 0
    #: Proposed as new, but their evidence was already an open signal's — kept
    #: as extensions of it. See `_already_open`.
    reused: list[str] = field(default_factory=list)
    rescored: int = 0
    skipped: str = ""


def evidence_date():
    """When a claim was said: the item's publication date, else when we found it."""
    return Coalesce("content_item__published_at", "content_item__discovered_at")


def window_claims(now: datetime) -> list[Claim]:
    return list(
        Claim.objects.filter(is_sponsored=False)
        .annotate(said_at=evidence_date())
        .filter(said_at__gte=now - timedelta(days=WINDOW_DAYS))
        .select_related("content_item__source", "content_item__duplicate_of__source")
        .order_by("-said_at", "-pk")[:MAX_CLAIMS]
    )


def detect(*, client: LLMClient | None = None, now: datetime | None = None) -> DetectionResult:
    """Detect developments in the current window, persist them, and score them."""
    now = now or timezone.now()
    result = DetectionResult()

    claims = window_claims(now)
    result.claims_considered = len(claims)
    if len({_conversation(c) for c in claims}) < 2:
        result.skipped = "fewer than two items with claims in the window"
        result.rescored = rescore_open(now=now)
        return result

    open_signals = {
        s.code: s
        for s in Signal.objects.filter(state__in=OPEN_STATES, is_illustrative=False)
    }
    llm = client or LLMClient()
    try:
        detection = llm.structured(
            schema=Detection,
            tier=extraction_tier(),
            purpose=ModelRun.Purpose.DETECTION,
            prompt_version=PROMPT_VERSION,
            system=cached_system(f"{INSTRUCTIONS}\n\n## The domain\n\n{domain_pack()}"),
            user=_render(claims, open_signals.values()),
        )
    except (CostCapExceeded, LLMUnavailable, LLMInvalidOutput) as exc:
        logger.warning("Signal detection failed: %s", exc)
        result.skipped = str(exc)
        return result

    # `structured` returns the parsed answer, not its ledger row; detection is
    # a single scheduled call, so the newest detection run IS this one.
    model_run = ModelRun.objects.filter(purpose=ModelRun.Purpose.DETECTION).order_by("-at").first()

    by_id = {c.pk: c for c in claims}
    with transaction.atomic():
        for proposed in detection.signals:
            _apply(proposed, by_id, open_signals, model_run, now, result)

    result.rescored = rescore_open(now=now)
    logger.info(
        "Detection over %d claims: %d new, %d extended, %d invented ID(s) dropped, "
        "%d single-item proposal(s) dropped, %d duplicate proposal(s) kept as extensions",
        result.claims_considered, len(result.created), len(result.extended),
        result.dropped_unknown_ids, result.dropped_single_source, len(result.reused),
    )
    return result


def _apply(proposed, by_id, open_signals, model_run, now, result) -> None:
    supporting = [i for i in dict.fromkeys(proposed.supporting_claim_ids) if i in by_id]
    contradicting = [
        i for i in dict.fromkeys(proposed.contradicting_claim_ids)
        if i in by_id and i not in supporting
    ]
    result.dropped_unknown_ids += (
        len(set(proposed.supporting_claim_ids) | set(proposed.contradicting_claim_ids))
        - len(supporting) - len(contradicting)
    )

    # Distinct CONVERSATIONS, not items: a podcast and its YouTube upload are
    # one voice saying one thing, however many times it was published.
    if len({_conversation(by_id[i]) for i in supporting}) < 2:
        result.dropped_single_source += 1
        return

    signal_type = (
        proposed.signal_type if proposed.signal_type in Signal.Type.values
        else Signal.Type.CONVERGENCE
    )
    signal = open_signals.get(proposed.extends_code.strip())
    if signal is None:
        # The model sometimes proposes a development it already proposed,
        # rather than extending it — SIG-3006 and SIG-3007 on 18–19 Oct were one
        # creatine-dose development with the same claims. Identity is decided
        # by evidence here, not by the model's choice of field.
        signal = _already_open(supporting, open_signals)
        if signal is not None:
            result.reused.append(signal.code)
    if signal is not None:
        # The wording of a signal an operator has already acted on is what they
        # reviewed; new evidence attaches to it without rewriting it.
        if signal.state in (Signal.State.CANDIDATE, Signal.State.IN_REVIEW):
            signal.statement = proposed.statement.strip()
            signal.detected_by = model_run
            signal.prompt_version = PROMPT_VERSION
            signal.save(update_fields=["statement", "detected_by", "prompt_version"])
        result.extended.append(signal.code)
    else:
        signal = Signal.objects.create(
            code=_next_code(),
            title=proposed.title.strip()[:300],
            statement=proposed.statement.strip(),
            signal_type=signal_type,
            state=Signal.State.CANDIDATE,
            first_seen_at=now,
            domain_score=0,
            needs_scientific_routing=proposed.needs_scientific_routing,
            detected_by=model_run,
            prompt_version=PROMPT_VERSION,
            domain_pack_version=DOMAIN_PACK_VERSION,
        )
        open_signals[signal.code] = signal
        result.created.append(signal.code)

    for ids, role in (
        (supporting, SignalEvidence.Role.SUPPORTS),
        (contradicting, SignalEvidence.Role.CONTRADICTS),
    ):
        for claim_id in ids:
            SignalEvidence.objects.get_or_create(
                signal=signal, claim_id=claim_id, defaults={"role": role, "added_at": now}
            )


#: Share of a proposal's supporting claims that, already supporting one open
#: signal, make the proposal that signal rather than a new one.
SAME_SIGNAL_OVERLAP = 0.5


def _already_open(supporting: list[int], open_signals: dict) -> Signal | None:
    if not supporting:
        return None
    counts: dict[int, int] = {}
    for signal_id in SignalEvidence.objects.filter(
        claim_id__in=supporting,
        role=SignalEvidence.Role.SUPPORTS,
        signal__in=list(open_signals.values()),
    ).values_list("signal_id", flat=True):
        counts[signal_id] = counts.get(signal_id, 0) + 1
    if not counts:
        return None
    best_id, shared = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    if shared / len(supporting) < SAME_SIGNAL_OVERLAP:
        return None
    return next(s for s in open_signals.values() if s.pk == best_id)


def _conversation(claim: Claim) -> int:
    """The item a claim counts under: itself, or what it duplicates."""
    return claim.content_item.canonical.pk


def _next_code() -> str:
    numbers = [
        int(m.group(1))
        for code in Signal.objects.values_list("code", flat=True)
        if (m := re.fullmatch(r"SIG-(\d+)", code))
    ]
    return f"SIG-{max(numbers, default=3000) + 1}"


def _render(claims: list[Claim], open_signals) -> str:
    lines = []
    for claim in claims:
        item = claim.content_item.canonical
        tags = [claim.kind] + (["cites a study"] if claim.is_cited else [])
        lines.append(
            f"C{claim.pk} | I{item.pk} | {item.creator or 'unattributed'} | "
            f"{item.source.get_route_display()} | {claim.said_at:%Y-%m-%d} | "
            f"{', '.join(tags)} | about: {claim.subject or '-'} | {claim.text}"
        )
    existing = "\n".join(
        f"{s.code}: {s.title} — {s.statement}" for s in open_signals
    ) or "(none)"
    return (
        f"## Existing open signals\n\n{existing}\n\n"
        f"## Claims ({len(claims)})\n\n" + "\n".join(lines)
    )


# ── Scoring ─────────────────────────────────────────────────────────────────


def watching_since() -> datetime | None:
    """When collection began — the earliest item we hold. Momentum's baseline
    can only cover weeks we were actually watching."""
    return ContentItem.objects.aggregate(first=Min("discovered_at"))["first"]


def weekly_coverage(now: datetime, *, weeks: int = 6) -> list[dict]:
    """The whole collection's coverage per 7-day week, newest first.

    Relevant, transcribed, not a duplicate, by evidence date — the same weeks
    `components` counts a signal's support in. A week is COVERED when it meets
    both thresholds in settings; momentum and acceleration are measured only
    over unbroken runs of covered weeks.
    """
    min_items = settings.SIGNAL_COVERED_WEEK_MIN_ITEMS
    min_sources = settings.SIGNAL_COVERED_WEEK_MIN_SOURCES
    items = list(
        ContentItem.objects.filter(
            gate_decision__in=(ContentItem.GateDecision.RELEVANT, ContentItem.GateDecision.SAMPLED),
            content_state=ContentItem.ContentState.TRANSCRIBED,
        )
        .exclude(duplicate_status="confirmed")
        .annotate(said_at=Coalesce("published_at", "discovered_at"))
        .filter(said_at__gte=now - timedelta(days=7 * weeks))
        .values_list("said_at", "source_id")
    )
    rows = []
    for index in range(weeks):
        end, start = now - timedelta(days=7 * index), now - timedelta(days=7 * (index + 1))
        in_week = [source for at, source in items if start <= at < end]
        count, sources = len(in_week), len(set(in_week))
        rows.append({
            "week_ending": end.date().isoformat(),
            "relevant_transcribed": count,
            "sources": sources,
            "covered": count >= min_items and sources >= min_sources,
        })
    return rows


def rescore(
    signal: Signal,
    *,
    now: datetime,
    since: datetime | None = None,
    coverage: list[dict] | None = None,
) -> Signal:
    """Recompute every component from the stored evidence.

    Run after detection and again on every scheduled run, because momentum,
    novelty and recency change with time even when the evidence does not.
    """
    facts = {SignalEvidence.Role.SUPPORTS: [], SignalEvidence.Role.CONTRADICTS: []}
    for row in (
        SignalEvidence.objects.filter(signal=signal)
        .select_related("claim__content_item__source", "claim__content_item__duplicate_of__source")
    ):
        # Counted as the conversation it belongs to: a re-upload or a clip adds
        # no item, no creator and no source type (PRD §6.3).
        item = row.claim.content_item.canonical
        facts[row.role].append(
            components.Fact(
                item_id=item.pk,
                # The show or channel stands in for an unrecorded creator: two
                # episodes of one show are one voice, not two confirmations.
                creator=(item.creator or item.source.name).strip().lower(),
                route=item.source.route,
                at=item.published_at or item.discovered_at,
                is_cited=row.claim.is_cited,
            )
        )

    scored = components.score(
        facts[SignalEvidence.Role.SUPPORTS],
        facts[SignalEvidence.Role.CONTRADICTS],
        now=now,
        watching_since=since if since is not None else watching_since(),
        first_seen_at=signal.first_seen_at,
        coverage=coverage if coverage is not None else weekly_coverage(now),
    )
    signal.domain_score = scored.domain_score
    signal.maturity = scored.maturity
    signal.components = scored.components
    signal.baseline = scored.baseline
    signal.confidence = scored.confidence
    signal.confidence_reasons = scored.confidence_reasons
    signal.item_count = scored.item_count
    signal.creator_count = scored.creator_count
    signal.source_count = scored.source_count
    signal.scored_at = now
    signal.awaiting_operator_judgment = signal.state == Signal.State.CANDIDATE
    signal.save(
        update_fields=[
            "domain_score", "components", "baseline", "confidence", "confidence_reasons",
            "maturity",
            "item_count", "creator_count", "source_count", "scored_at",
            "awaiting_operator_judgment",
        ]
    )
    return signal


def rescore_open(*, now: datetime) -> int:
    since, coverage = watching_since(), weekly_coverage(now)
    signals = Signal.objects.filter(state__in=OPEN_STATES, is_illustrative=False)
    for signal in signals:
        rescore(signal, now=now, since=since, coverage=coverage)
    return len(signals)

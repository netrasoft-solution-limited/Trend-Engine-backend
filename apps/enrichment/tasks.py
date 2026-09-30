"""Celery tasks for enrichment. Queue routing lives in config/celery.py.

These land on the `enrich` queue, which runs at concurrency 2 — "LLM-bound,
LOW concurrency, rate-limit aware, cost-capped" (Arch §11.1). That is
deliberate and should not be raised to make a backlog disappear: the queue is
narrow because the spend behind it is the largest variable in the cost model,
and a wide queue turns a retry storm into a month's margin.

Retries: `config/celery.py` sets backoff with jitter and `max_retries=3` for
everything. Neither task below retries a cost cap or a refusal — one is a
budget decision and the other is the model's answer, and retrying either just
spends more to be told the same thing.
"""
from __future__ import annotations

import logging

from celery import shared_task

from apps.evidence.models import ContentItem
from apps.evidence.services import transcripts

from . import extraction, gate
from .llm.costs import CostCapExceeded

logger = logging.getLogger(__name__)


def _domain_pack() -> str:
    """The taxonomy the gate and extractor are given.

    A literal for now. `apps.domains` will own the versioned pack (PRD §5
    principle 4 keeps supplements-specific concepts out of the core schema),
    and when it does this function is the single place that changes.
    """
    return (
        "Dietary supplements and nutraceuticals: ingredients, formulations, "
        "dosing protocols, mechanisms of action, clinical evidence, safety and "
        "interactions, category and brand movements, and the consumer questions "
        "around them. Adjacent: sports nutrition, healthy ageing, gut health, "
        "cognition, sleep, micronutrients."
    )


@shared_task(bind=True, name="apps.enrichment.tasks.assess_relevance")
def assess_relevance(self, content_item_id: int) -> dict:
    """Screen one item on its metadata alone (Arch §6.2)."""
    item = ContentItem.objects.filter(pk=content_item_id).first()
    if item is None:
        return {"skipped": "no such item"}

    try:
        result = gate.assess(item, domain_pack=_domain_pack())
    except CostCapExceeded as exc:
        # Do not retry. The cap will still be reached in 30 seconds, and each
        # attempt writes another ModelRun.
        logger.warning("Gate capped on item %s: %s", content_item_id, exc)
        return {"skipped": "cost cap"}

    # Only spend on a transcript for items that earned one — including the
    # sampled rejections, which is the point of sampling them.
    if gate.needs_transcript(item):
        acquire_transcript.delay(item.pk)

    return {
        "decision": result.decision,
        "sampled": result.sampled,
        "confidence": result.confidence,
    }


@shared_task(bind=True, name="apps.enrichment.tasks.acquire_transcript")
def acquire_transcript(self, content_item_id: int) -> dict:
    """Walk the fallback ladder for one item's full text.

    The walk itself is in `apps.evidence.services.transcripts` — acquiring a
    transcript produces evidence rather than enriching it, and that module can
    see both the connectors and the models. This task is the queue boundary and
    the retry policy, nothing else.

    It runs on `enrich` rather than `ingest`, despite being I/O-bound, because
    the AssemblyAI rung bills by the hour. Arch §11.1 sizes `enrich` narrow for
    exactly that reason: "the queue is narrow because the spend behind it is
    the largest variable in the cost model".
    """
    item = ContentItem.objects.filter(pk=content_item_id).first()
    if item is None:
        return {"skipped": "no such item"}

    outcome = transcripts.acquire(item)

    if outcome.succeeded:
        # Only now is there anything to extract from.
        extract_evidence.delay(item.pk)
    elif outcome.retryable:
        # Something broke rather than being absent, so the item has NOT been
        # written off as metadata-only. Retry with the backoff in
        # config/celery.py; on the last attempt let it settle and leave the
        # item for the next scheduled pass rather than failing loudly.
        broken = [r for r in outcome.rungs if r.status == transcripts.Status.FAILED]
        logger.warning(
            "Transcript rungs failed transiently for item %s: %s",
            content_item_id,
            "; ".join(f"{r.step}: {r.detail}" for r in broken),
        )
        raise self.retry(exc=RuntimeError(str(outcome)))

    return {
        "method": outcome.method,
        "chars": outcome.chars,
        "cost_usd": str(outcome.cost_usd),
        "rungs": [(r.step, r.status) for r in outcome.rungs],
    }


@shared_task(bind=True, name="apps.enrichment.tasks.extract_evidence")
def extract_evidence(self, content_item_id: int) -> dict:
    """Extract claims and questions from one transcribed item.

    Arch §10.3: once per item, ever. `extraction.extract` enforces that itself,
    so a duplicate dispatch is cheap rather than billable.
    """
    item = ContentItem.objects.filter(pk=content_item_id).first()
    if item is None:
        return {"skipped": "no such item"}

    try:
        result = extraction.extract(item, domain_pack=_domain_pack())
    except CostCapExceeded as exc:
        logger.warning("Extraction capped on item %s: %s", content_item_id, exc)
        return {"skipped": "cost cap"}

    return {
        "claims": result.claims,
        "questions": result.questions,
        "dropped_unanchored": result.dropped_unanchored,
        "skipped": result.skipped,
    }


@shared_task(name="apps.enrichment.tasks.assess_pending")
def assess_pending(limit: int = 100) -> dict:
    """Screen everything still waiting.

    The periodic entry point. Items are left PENDING by a cap or an outage, so
    this picks up what the last run could not afford — which is why the gate
    records those as PENDING rather than as rejections.
    """
    pending = ContentItem.objects.filter(
        gate_decision=ContentItem.GateDecision.PENDING
    ).order_by("discovered_at")[:limit]

    dispatched = 0
    for item in pending:
        assess_relevance.delay(item.pk)
        dispatched += 1

    return {"dispatched": dispatched}

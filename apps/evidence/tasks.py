"""Collection tasks — the front of the pipeline.

These land on the `ingest` queue, which Arch §11.1 sizes wide: "I/O-bound, high
concurrency, tolerant of slow external APIs". That is the opposite of `enrich`,
and deliberately so — polling a feed is cheap and slow, while the work it feeds
is expensive and fast to start.

The shape is fan-out: one periodic task finds what is due and dispatches a task
per source, rather than one task looping over every source. A loop would mean
one slow vendor delaying every source behind it, and one exception losing the
whole pass.
"""
from __future__ import annotations

import logging

from celery import current_app, shared_task
from django.utils import timezone

from apps.ingestion.models import IngestionRun
from apps.sources.models import Source

from . import collection
from .collection import NotCollectable
from .services.ingest import ingest_rows

logger = logging.getLogger(__name__)


@shared_task(name="apps.evidence.tasks.poll_due_sources")
def poll_due_sources() -> dict:
    """Dispatch a poll for every source whose interval has elapsed.

    Deliberately does no collecting itself. It is the cheapest possible task —
    a query and some dispatches — so that beat never blocks and a vendor outage
    cannot delay the scheduler.
    """
    due = [s for s in Source.objects.all() if s.is_due]

    for source in due:
        poll_source.delay(source.pk)

    # Sources that WANT polling but cannot is the interesting number: it is
    # usually a policy that was never approved, and it is otherwise invisible
    # because nothing errors — the items simply never arrive.
    blocked = Source.objects.filter(
        poll_interval_minutes__gt=0, policy__isnull=True
    ).count()
    if blocked:
        logger.warning(
            "%s source(s) are scheduled to poll but have no provider policy, "
            "so they collect nothing (PRD §7.2).",
            blocked,
        )

    return {"dispatched": len(due), "blocked_without_policy": blocked}


@shared_task(bind=True, name="apps.evidence.tasks.poll_source")
def poll_source(self, source_id: int, limit: int | None = None) -> dict:
    """Collect one source's latest items and put them through the gate.

    Records an `IngestionRun` either way. A run that found nothing and a run
    that failed are different facts, and the Ingestion Runs screen needs to
    tell them apart — PRD §6.5 puts blocking failures on the Triage home.
    """
    source = Source.objects.filter(pk=source_id).first()
    if source is None:
        return {"skipped": "no such source"}

    run = IngestionRun.objects.create(source=source)

    try:
        # Called through the module rather than bound at import, so the name
        # resolves at call time — otherwise a test that patches
        # `collection.collect` silently reaches the real vendor instead.
        rows = (
            collection.collect(source, limit=limit)
            if limit
            else collection.collect(source)
        )
    except NotCollectable as exc:
        # Misconfiguration, not an outage. Retrying asks the same broken
        # question again, so this is blocking and needs an operator.
        logger.error("Cannot collect from %s: %s", source.name, exc)
        _finish(run, status=IngestionRun.Status.FAILED, blocking=True)
        source.mark_polled(succeeded=False)
        return {"failed": str(exc), "blocking": True}
    except Exception as exc:
        # A vendor problem. The next scheduled poll picks it up, so this is not
        # blocking — but the run is still recorded as failed.
        logger.warning("Poll of %s failed: %s", source.name, exc)
        _finish(run, status=IngestionRun.Status.FAILED, blocking=False)
        source.mark_polled(succeeded=False)
        raise self.retry(exc=exc) from exc

    result = ingest_rows(source=source, policy=source.policy, rows=rows, run=run)

    # Only NEW items are screened. Re-gating something already judged would
    # spend on a question that has an answer (Arch §6.2), and the idempotency
    # key is what makes "new" meaningful across overlapping polls.
    #
    # Dispatched BY NAME, not by import: `apps.enrichment` sits ABOVE
    # `apps.evidence` in the layer stack, so importing its tasks here would
    # invert the dependency rule that `.importlinter` enforces. A task name is
    # a string on a queue, which is exactly the decoupling the layering wants —
    # the same reason `apps.publication` emits a signal rather than calling the
    # portal.
    for item in result.created_items:
        current_app.send_task(
            "apps.enrichment.tasks.assess_relevance", args=[item.pk], queue="enrich"
        )
    dispatched = len(result.created_items)

    _finish(run, status=IngestionRun.Status.SUCCEEDED, blocking=False)
    source.mark_polled(succeeded=True)

    return {
        "collected": len(rows),
        "created": result.created,
        "seen_before": result.seen_before,
        "gated": dispatched,
    }


def _finish(run: IngestionRun, *, status: str, blocking: bool) -> None:
    run.status = status
    run.blocking = blocking
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "blocking", "finished_at"])

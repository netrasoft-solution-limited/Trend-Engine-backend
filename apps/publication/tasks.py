"""Celery tasks for publication. Queue routing lives in config/celery.py.

Delivery runs here rather than in the request because rendering a PDF takes a
second or two per format and the operator should not be watching a spinner to
find out whether a client was emailed. `config/celery.py`'s `default` queue
exists for exactly this — "short tasks (exports, email) that must not queue
behind a transcription".

The operator's own download stays synchronous: they are waiting for the file,
and a job queue between a click and a save dialog helps nobody.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.publication.tasks.deliver_publication", max_retries=0)
def deliver_publication(
    *, publication_id: int, formats: list[str], actor_label: str, note: str = ""
) -> dict:
    """Send one publication to its client's contacts.

    No retries. A delivery that failed its checks — withdrawn, no recipients —
    will fail them again, and one that got as far as sending has already
    recorded itself; retrying would email the client twice for one action.
    """
    from apps.tenancy.context import operator_scope

    from . import delivery as delivery_service
    from .models import Publication

    with operator_scope():
        publication = Publication.objects.filter(pk=publication_id).first()
        if publication is None:
            logger.error("deliver_publication: no publication %s", publication_id)
            return {"ok": False, "error": "publication not found"}

        try:
            record = delivery_service.deliver(
                publication=publication,
                formats=tuple(formats),
                actor_label=actor_label,
                note=note,
            )
        except delivery_service.DeliveryError as exc:
            # Returned rather than raised: the operator is looking at a page,
            # and "this client has nobody to send to" is a normal answer.
            logger.warning("Delivery refused for %s: %s", publication_id, exc)
            return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "delivery_id": record.pk,
        "recipients": len(record.recipients),
        "formats": record.formats,
    }


@shared_task(name="apps.publication.tasks.advance_cycle", max_retries=0)
def advance_cycle(cycle_id: int) -> dict:
    """Run a research cycle's current stage, then schedule the next call.

    Heavy stages (detection, drafting, discovery) are sent to the `enrich`
    queue so exports and email on `default` never wait behind them.
    """
    from apps.tenancy.context import operator_scope

    from . import cycle as cycles
    from .models import ResearchCycle

    with operator_scope():
        current = ResearchCycle.objects.filter(pk=cycle_id).first()
    if current is None:
        return {"skipped": "no such cycle"}
    delay = cycles.advance(current)
    if delay is not None:
        queue = "enrich" if current.stage in HEAVY_STAGES else "default"
        advance_cycle.apply_async(args=[cycle_id], countdown=delay, queue=queue)
    return {"cycle": cycle_id, "stage": current.stage, "state": current.state, "next": delay}


HEAVY_STAGES = {"detect", "recommend", "discover", "search"}


@shared_task(name="apps.publication.tasks.draft_cycle_briefs", max_retries=0)
def draft_cycle_briefs(cycle_id: int, actor_label: str) -> dict:
    """Draft both briefs for a cycle. In a task, not the request: the written
    prose is two model calls and outlived the web worker's timeout (21 Nov)."""
    from apps.tenancy.context import operator_scope, scoped

    from . import cycle as cycles
    from .models import ResearchCycle

    with operator_scope():
        current = ResearchCycle.objects.select_related("organization").filter(pk=cycle_id).first()
    if current is None:
        return {"skipped": "no such cycle"}
    with scoped(current.organization):
        made = cycles.briefs(current, actor_label=actor_label)
    return {"cycle": cycle_id, "versions": made}

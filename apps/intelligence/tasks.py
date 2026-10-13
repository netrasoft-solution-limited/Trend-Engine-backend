"""Scheduled signal detection.

On the `enrich` queue: it is one model call over the window, the same kind of
work as extraction, and it should wait behind extraction rather than race it —
a detection run that starts before the morning's items are extracted simply
misses them until tomorrow.
"""
from __future__ import annotations

from dataclasses import asdict

from celery import current_app, shared_task

from . import detection


@shared_task(name="apps.intelligence.tasks.detect_signals")
def detect_signals() -> dict:
    result = asdict(detection.detect())
    # Per-client ranking is L5 and this is L4, so it is dispatched by NAME —
    # the same decoupling `apps.evidence` uses to hand items to the gate.
    current_app.send_task("apps.scoring.tasks.rank_all_clients", queue="enrich")
    return result

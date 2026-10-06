"""Celery tasks for operations. Queue routing lives in config/celery.py.

Both tasks here are thin: the work and every decision about it live in
`backups.py`, so the same code is reachable from a management command and from
a test without a broker. What a task adds is the schedule and the retry policy.

`acks_late` is not set on either. A backup interrupted halfway and redelivered
would start a second `pg_dump` beside the first; a missed night is cheaper than
two concurrent dumps on a small droplet, and the missing `BackupRun` row makes
the miss visible.
"""
from __future__ import annotations

import logging

from celery import shared_task

from . import backups

logger = logging.getLogger(__name__)


@shared_task(
    name="apps.operations.tasks.nightly_backup",
    # One retry, well after the first attempt. A transient S3 blip deserves a
    # second try; a broken credential deserves a recorded failure someone looks
    # at, not twelve attempts that bury it.
    max_retries=1,
    default_retry_delay=15 * 60,
)
def nightly_backup() -> dict:
    run = backups.run_backup()
    return {
        "outcome": run.outcome,
        "artifact": run.artifact,
        "size_bytes": run.size_bytes,
        "seconds": run.duration_seconds,
    }


@shared_task(
    name="apps.operations.tasks.restore_test",
    max_retries=0,
)
def restore_test() -> dict:
    """Arch §11.3 calls this an explicit acceptance criterion.

    No retries: a restore that failed is the finding. Retrying until it passes
    would be a way of not hearing it.
    """
    run = backups.run_restore_test()
    return {"outcome": run.outcome, "artifact": run.artifact, "detail": run.detail}

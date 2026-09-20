"""Celery, with three queues — Arch §11.1.

The queues are separate on purpose:

    ingest   I/O-bound, high concurrency, tolerant of slow external APIs
    enrich   LLM-bound, LOW concurrency, rate-limit aware, cost-capped
    default  short tasks (exports, email) that must not queue behind a
             40-minute transcription job

"Mixing these into one queue is the classic failure: a batch of transcriptions
starves a user-facing export."

Workers load the operator settings: they run on the operator plane and bind
tenant scope explicitly per task via `tenancy.context.scoped()`. A task that
forgets to bind raises TenantScopeError on its first tenant-scoped query, which
is the intended outcome.
"""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.ops")

app = Celery("trend_engine")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.task_default_queue = "default"

# Route by module. A task's queue is a property of what it does, not a decision
# each caller makes at the call site.
app.conf.task_routes = {
    "apps.ingestion.tasks.*": {"queue": "ingest"},
    "apps.connectors.tasks.*": {"queue": "ingest"},
    "apps.enrichment.tasks.*": {"queue": "enrich"},
    "apps.outputs.tasks.*": {"queue": "default"},
    "apps.publication.tasks.*": {"queue": "default"},
    "apps.operations.tasks.*": {"queue": "default"},
}

# Arch §6.3: exponential backoff with jitter for network and rate-limit errors.
# Auth and policy errors must NOT retry — they block the connector and raise an
# operator alert, because retrying an access violation is both futile and a
# compliance risk. That distinction is enforced in the tasks themselves; these
# are only the defaults for the retryable class.
app.conf.task_acks_late = True
app.conf.task_reject_on_worker_lost = True
app.conf.task_default_retry_delay = 30
app.conf.task_annotations = {
    "*": {"max_retries": 3, "retry_backoff": True, "retry_jitter": True},
}

app.conf.beat_schedule = {
    "poll-due-sources": {
        "task": "apps.ingestion.tasks.poll_due_sources",
        "schedule": crontab(minute="*/15"),
    },
    "poll-deletions": {
        # Platform deletion and retention obligations, per connector (PRD §7.2).
        "task": "apps.evidence.tasks.poll_deletions",
        "schedule": crontab(hour="3", minute="30"),
    },
    "refresh-cost-ledger": {
        "task": "apps.operations.tasks.refresh_cost_ledger",
        "schedule": crontab(minute="0"),
    },
    "nightly-backup": {
        "task": "apps.operations.tasks.nightly_backup",
        "schedule": crontab(hour="3", minute="0"),
    },
    "weekly-restore-test": {
        # Arch §11.3: "an untested backup is not a backup." This is an explicit
        # acceptance criterion, not a nice-to-have.
        "task": "apps.operations.tasks.restore_test",
        "schedule": crontab(day_of_week="sun", hour="4", minute="0"),
    },
}

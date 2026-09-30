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
    # Collection — polling feeds and normalising what comes back. I/O-bound and
    # slow, never expensive, so it belongs on the wide queue with the rest of
    # acquisition rather than on the narrow cost-capped one. Without this line
    # these fall through to `default`, where they would sit behind exports.
    "apps.evidence.tasks.*": {"queue": "ingest"},
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
    # The front of the pipeline. This only DISPATCHES — per-source polling is a
    # task each, so one slow vendor cannot delay every source behind it and one
    # exception cannot lose the whole pass.
    #
    # Every 15 minutes is the scheduler's resolution, not the poll rate: each
    # source carries its own `poll_interval_minutes`, and this tick simply asks
    # which have elapsed. A weekly podcast is still polled weekly.
    "poll-due-sources": {
        "task": "apps.evidence.tasks.poll_due_sources",
        "schedule": crontab(minute="*/15"),
    },
    # Screen whatever the last pass could not afford or could not reach. The
    # gate leaves those PENDING rather than rejecting them, so this is what
    # picks them back up.
    "assess-pending-relevance": {
        "task": "apps.enrichment.tasks.assess_pending",
        "schedule": crontab(minute="*/30"),
    },
}

# ── Scheduled but not yet implemented ───────────────────────────────────────
# These were on the beat schedule pointing at tasks that do not exist, so beat
# dispatched messages no worker could resolve — a silent failure that looks
# exactly like a quiet system. They are listed here rather than scheduled, and
# each moves back above when its task is written:
#
#   apps.evidence.tasks.poll_deletions         daily 03:30    (PRD §7.2)
#   apps.operations.tasks.refresh_cost_ledger  hourly
#   apps.operations.tasks.nightly_backup       daily 03:00    (scripts/backup.sh)
#   apps.operations.tasks.restore_test         weekly         (Arch §11.3)

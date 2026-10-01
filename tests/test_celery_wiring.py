"""The queue is actually connected to the broker we think it is.

This exists because it was not, in production, and nothing noticed. `REDIS_URL`
was set and used for the cache, but Celery reads only CELERY_-prefixed settings
through `config_from_object(..., namespace="CELERY")` — so it never saw it and
fell back to its own default, amqp://guest@localhost:5672.

The failure mode is the reason this is a test rather than a note. Every worker
and beat STARTED. `docker compose ps` showed six services running. They sat in
a reconnect loop against a RabbitMQ that does not exist, and the entire async
pipeline — collection, the relevance gate, transcripts, extraction — was dead
behind a stack that looked healthy. Nothing in the suite touched a broker, so
nothing failed.
"""
from __future__ import annotations

import pytest
from django.conf import settings


@pytest.fixture(scope="module")
def celery_app():
    from config.celery import app

    return app


def test_the_broker_is_configured_at_all(celery_app):
    assert celery_app.conf.broker_url, "Celery has no broker; tasks go nowhere"


def test_the_broker_is_not_celerys_rabbitmq_default(celery_app):
    """The specific wrong answer, named.

    Falling back to amqp://localhost is not a crash — it is a reconnect loop
    in a container that reports healthy.
    """
    broker = str(celery_app.conf.broker_url)

    assert not broker.startswith("amqp://"), (
        f"Celery fell back to its RabbitMQ default ({broker}). It reads only "
        f"CELERY_-prefixed Django settings, so CELERY_BROKER_URL must be set — "
        f"REDIS_URL alone is invisible to it."
    )


def test_the_broker_is_the_redis_this_deployment_runs(celery_app):
    assert str(celery_app.conf.broker_url) == settings.REDIS_URL


def test_every_scheduled_task_routes_to_a_queue_a_worker_consumes(celery_app):
    """A task routed to a queue nobody runs is dispatched and never executed —
    the same silence as a missing broker, one layer further in.

    The queues come from docker-compose: `celery worker -Q ingest|enrich|default`.
    """
    running = {"ingest", "enrich", "default"}

    for name, entry in celery_app.conf.beat_schedule.items():
        queue = celery_app.amqp.router.route({}, entry["task"]).get("queue")
        queue_name = getattr(queue, "name", queue) or "default"
        assert queue_name in running, (
            f"beat entry {name!r} routes {entry['task']} to {queue_name!r}, "
            f"which no worker in docker-compose.yml consumes"
        )


def test_every_scheduled_task_exists(celery_app):
    """Beat dispatching a name no worker can resolve is the failure the
    schedule's own comment describes: it looks exactly like a quiet system."""
    celery_app.loader.import_default_modules()

    missing = [
        entry["task"]
        for entry in celery_app.conf.beat_schedule.values()
        if entry["task"] not in celery_app.tasks
    ]
    assert not missing, f"scheduled but unregistered: {missing}"

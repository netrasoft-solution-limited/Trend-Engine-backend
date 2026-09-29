"""IngestionRun: the status fields behind "blocking failures"."""
from __future__ import annotations

import pytest

from apps.ingestion.models import IngestionRun
from apps.sources.models import Source

pytestmark = pytest.mark.django_db

Status = IngestionRun.Status


@pytest.fixture
def source():
    return Source.objects.create(name="PubMed", route=Source.Route.RESEARCH)


def test_run_statuses_and_defaults(source):
    run = IngestionRun.objects.create(source=source)

    assert Status.values == ["running", "succeeded", "failed"]
    assert run.status == Status.RUNNING
    assert run.blocking is False
    assert run.finished_at is None


def test_only_failed_and_blocking_runs_match_the_blocking_filter(source):
    IngestionRun.objects.create(source=source, status=Status.FAILED, blocking=True)
    IngestionRun.objects.create(source=source, status=Status.FAILED, blocking=False)
    IngestionRun.objects.create(source=source, status=Status.SUCCEEDED, blocking=True)

    assert IngestionRun.objects.filter(status=Status.FAILED, blocking=True).count() == 1

"""The failure banner on every operator page (Mark, 24 Oct)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.enrichment.models import ModelRun
from apps.evidence.tests.test_collection import podcast, policy  # noqa: F401
from apps.ingestion.models import IngestionRun
from apps.operations.models import OperatorUser
from apps.scoring.alerts import problems

pytestmark = pytest.mark.django_db


def run(source, status, error=""):
    return IngestionRun.objects.create(source=source, status=status, error=error)


def model_run(outcome, error=""):
    return ModelRun.objects.create(
        purpose=ModelRun.Purpose.DETECTION, outcome=outcome, model="claude-sonnet-5-5",
        prompt_version="t", error=error,
    )


def test_a_source_failing_since_its_last_success_is_reported_in_plain_words(podcast):  # noqa: F811
    run(podcast, IngestionRun.Status.SUCCEEDED)
    run(podcast, IngestionRun.Status.FAILED, '403: {"type": "build-not-found"}')
    run(podcast, IngestionRun.Status.FAILED, '403: {"type": "build-not-found"}')

    [problem] = problems()

    assert problem["what"] == f"Collection from {podcast.name} is failing"
    assert problem["why"] == "the collector build no longer exists"
    assert problem["count"] == 2


def test_a_success_clears_it(podcast):  # noqa: F811
    run(podcast, IngestionRun.Status.FAILED, "boom")
    run(podcast, IngestionRun.Status.SUCCEEDED)

    assert problems() == []


def test_an_empty_ai_balance_is_named(db):
    model_run(ModelRun.Outcome.SUCCEEDED)
    model_run(ModelRun.Outcome.ERRORED, "Error code: 400 - Your credit balance is too low")

    [problem] = problems()

    assert problem["what"] == "AI calls are failing"
    assert "out of credit" in problem["why"]


def test_the_banner_shows_on_operator_pages(client, podcast):  # noqa: F811
    run(podcast, IngestionRun.Status.FAILED, "boom")
    client.force_login(OperatorUser.objects.create_user(
        email="o@x.test", password="x" * 24, name="O"))

    page = client.get(reverse("ops-observations")).content.decode()

    assert f"Collection from {podcast.name} is failing" in page

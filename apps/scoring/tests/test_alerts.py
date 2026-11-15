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


# ── Detection (9–14 Nov: six failed days nobody saw) ────────────────────────


def detection_run(failed, error="detection: the answer was cut off after 512 characters."):
    from types import SimpleNamespace

    from apps.intelligence import health

    return health.record(SimpleNamespace(
        failed=failed, failed_batches=int(failed), errors=[error] if failed else [],
        claims_considered=400, created=[], extended=[],
    ))


def test_failing_detection_is_on_the_banner_until_a_run_works(db):
    detection_run(False)
    detection_run(True)
    detection_run(True)

    [problem] = problems()

    assert problem["what"] == "Signal detection is failing"
    assert problem["why"] == "the AI answer was cut off part-way"
    assert problem["count"] == 2

    detection_run(False)
    assert problems() == []


def test_the_review_address_is_emailed_once_per_streak(db, settings, mailoutbox):
    from apps.intelligence import health

    settings.OUTPUT_REVIEW_RECIPIENTS = ["mark@omnialta.com"]
    detection_run(True)
    assert not health.alert_if_failing(), "one bad morning is a banner, not an email"
    detection_run(True)
    assert health.alert_if_failing()
    detection_run(True)
    assert not health.alert_if_failing(), "a long outage is one message"

    [message] = mailoutbox
    assert message.to == ["mark@omnialta.com"]
    assert "Signal detection is failing" in message.subject
    assert "cut off" in message.body

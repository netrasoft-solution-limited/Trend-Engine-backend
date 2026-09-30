"""The collection tasks.

What matters here is not the collecting — that is tested in test_collection.py
— but the bookkeeping around it: that a run is recorded either way, that a
misconfigured source is told apart from a vendor outage, and that only NEW
items are sent to the gate.

That last one is money. Re-gating an item on every overlapping poll would spend
a model call to answer a question that already has an answer.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.evidence import collection, tasks
from apps.evidence.collection import NotCollectable
from apps.evidence.models import ContentItem
from apps.ingestion.models import IngestionRun
from apps.sources.models import AcquisitionProvider, ProviderPolicyVersion, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def policy():
    provider = AcquisitionProvider.objects.create(
        kind=AcquisitionProvider.Kind.TADDY, name="Taddy"
    )
    return ProviderPolicyVersion.objects.create(
        provider=provider,
        version="test",
        access_basis="Public podcast index.",
        approved_at=timezone.now(),
    )


@pytest.fixture
def source(policy):
    return Source.objects.create(
        name="A podcast",
        route=Source.Route.PODCAST,
        policy=policy,
        config={"rss_url": "https://example.test/feed.xml"},
    )


@pytest.fixture
def gated(monkeypatch):
    """Capture what would be put on the enrich queue."""
    sent: list[tuple[str, list]] = []
    monkeypatch.setattr(
        tasks.current_app,
        "send_task",
        lambda name, args=None, **kw: sent.append((name, args or [])),
    )
    return sent


def rows(*ids) -> list[dict]:
    return [
        {
            "external_id": i,
            "title": f"Episode {i}",
            "description": "Something about magnesium.",
            "transcript": None,
        }
        for i in ids
    ]


# ── Fan-out ─────────────────────────────────────────────────────────────────


def test_only_due_sources_are_dispatched(source, policy, monkeypatch):
    """One task per source, not one task looping over all of them: a loop
    means one slow vendor delays every source behind it."""
    recent = Source.objects.create(
        name="Polled just now",
        route=Source.Route.PODCAST,
        policy=policy,
        poll_interval_minutes=60,
        last_polled_at=timezone.now(),
    )

    dispatched: list[int] = []
    monkeypatch.setattr(
        tasks.poll_source, "delay", lambda pk: dispatched.append(pk), raising=False
    )

    result = tasks.poll_due_sources()

    assert dispatched == [source.pk]
    assert recent.pk not in dispatched
    assert result["dispatched"] == 1


def test_sources_scheduled_without_a_policy_are_counted(policy, monkeypatch):
    """Otherwise invisible: nothing errors, the items simply never arrive."""
    Source.objects.create(
        name="No basis", route=Source.Route.PODCAST, policy=None, poll_interval_minutes=60
    )
    monkeypatch.setattr(tasks.poll_source, "delay", lambda pk: None, raising=False)

    result = tasks.poll_due_sources()

    assert result["blocked_without_policy"] == 1
    assert result["dispatched"] == 0, "a source with no access basis must not poll"


# ── One poll ────────────────────────────────────────────────────────────────


def test_a_successful_poll_ingests_and_gates_the_new_items(source, gated, monkeypatch):
    monkeypatch.setattr(collection, "collect", lambda s, **kw: rows("ep-1", "ep-2"))

    result = tasks.poll_source(source.pk)

    assert result["created"] == 2
    assert result["gated"] == 2
    assert ContentItem.objects.count() == 2
    assert [name for name, _ in gated] == [
        "apps.enrichment.tasks.assess_relevance"
    ] * 2


def test_only_new_items_reach_the_gate(source, gated, monkeypatch):
    """Overlapping polls are the design, not an edge case. Re-gating a known
    item spends a model call to answer a question that has an answer."""
    monkeypatch.setattr(collection, "collect", lambda s, **kw: rows("ep-1"))
    tasks.poll_source(source.pk)
    gated.clear()

    # The same episode, plus one new one.
    monkeypatch.setattr(collection, "collect", lambda s, **kw: rows("ep-1", "ep-2"))
    result = tasks.poll_source(source.pk)

    assert result["collected"] == 2
    assert result["created"] == 1
    assert result["seen_before"] == 1
    assert len(gated) == 1, "only the new episode may be gated"


def test_a_successful_poll_records_a_run_and_a_success(source, gated, monkeypatch):
    monkeypatch.setattr(collection, "collect", lambda s, **kw: rows("ep-1"))

    tasks.poll_source(source.pk)
    source.refresh_from_db()
    run = IngestionRun.objects.latest("started_at")

    assert run.status == IngestionRun.Status.SUCCEEDED
    assert run.finished_at is not None
    assert run.blocking is False
    assert source.last_success_at is not None


def test_a_poll_that_finds_nothing_is_still_a_success(source, gated, monkeypatch):
    """A quiet publisher is not a failure, and the Ingestion Runs screen has to
    tell the two apart."""
    monkeypatch.setattr(collection, "collect", lambda s, **kw: [])

    result = tasks.poll_source(source.pk)
    run = IngestionRun.objects.latest("started_at")

    assert result["created"] == 0
    assert run.status == IngestionRun.Status.SUCCEEDED


# ── Failure, of two kinds ───────────────────────────────────────────────────


def test_a_misconfigured_source_fails_blocking_and_does_not_retry(source, monkeypatch):
    """Retrying asks the same broken question again. It needs an operator, so
    it goes on the Triage home as a blocking failure (PRD §6.5)."""
    def refuse(s, **kw):
        raise NotCollectable("no rss_url or series_name")

    monkeypatch.setattr(collection, "collect", refuse)

    def must_not_retry(**kwargs):
        raise AssertionError("a misconfiguration must not be retried")

    monkeypatch.setattr(tasks.poll_source, "retry", must_not_retry, raising=False)

    result = tasks.poll_source(source.pk)
    source.refresh_from_db()
    run = IngestionRun.objects.latest("started_at")

    assert result["blocking"] is True
    assert run.status == IngestionRun.Status.FAILED
    assert run.blocking is True
    assert source.last_polled_at is not None, "a failed attempt still moves due-ness"
    assert source.last_success_at is None


def test_a_vendor_outage_fails_non_blocking_and_retries(source, monkeypatch):
    """The next scheduled poll would pick it up anyway, so it is not something
    an operator needs to see on the Triage home."""
    def explode(s, **kw):
        raise RuntimeError("Taddy 503")

    monkeypatch.setattr(collection, "collect", explode)

    retried: list[str] = []

    def fake_retry(exc=None, **kwargs):
        retried.append(str(exc))
        raise RuntimeError("retry called")

    monkeypatch.setattr(tasks.poll_source, "retry", fake_retry, raising=False)

    with pytest.raises(RuntimeError, match="retry called"):
        tasks.poll_source(source.pk)

    run = IngestionRun.objects.latest("started_at")
    assert run.status == IngestionRun.Status.FAILED
    assert run.blocking is False, "a vendor outage is not a blocking failure"
    assert retried


def test_a_missing_source_is_not_an_error():
    assert tasks.poll_source(999_999) == {"skipped": "no such source"}

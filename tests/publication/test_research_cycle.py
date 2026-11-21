"""The research cycle: one repeatable run per brand (Mark, 20 Nov)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.clients.models import BrandSource
from apps.evidence.models import ContentItem
from apps.ingestion.models import IngestionRun
from apps.publication import cycle as cycles
from apps.publication.models import ResearchCycle
from apps.sources.models import Source
from apps.tenancy.context import scoped
from tests.publication import test_output_builder as builder

pytestmark = pytest.mark.django_db

evidence = builder.evidence
jarrow = builder.jarrow
approved = builder.approved
real_client = builder.real_client
admin = builder.admin


@pytest.fixture
def sent(monkeypatch):
    calls = []
    from celery import current_app

    monkeypatch.setattr(current_app, "send_task", lambda name, **kw: calls.append((name, kw)))
    return calls


@pytest.fixture
def linked(jarrow):
    sources = [
        Source.objects.create(name="Pod", route=Source.Route.PODCAST, config={"series_name": "P"}),
        Source.objects.create(name="Reddit — x", route=Source.Route.SOCIAL,
                              config={"platform": "reddit", "queries": ["x"]}),
    ]
    with scoped(jarrow):
        for s in sources:
            BrandSource.objects.create(organization=jarrow, source=s)
    return sources


def started(org):
    with scoped(org):
        return cycles.start(org, actor_label="abu")


def test_a_brand_with_no_linked_sources_cannot_start(jarrow):
    with pytest.raises(cycles.StageFailed, match="no sources linked"):
        started(jarrow)


def test_only_one_cycle_runs_at_a_time(jarrow, linked):
    started(jarrow)
    with pytest.raises(cycles.StageFailed, match="already running"):
        started(jarrow)


def test_collect_polls_every_linked_source_then_reports_failed_and_empty(jarrow, linked, sent):
    cycle = started(jarrow)

    assert cycles.advance(cycle) == 30
    assert sorted(kw["args"][0] for _, kw in sent) == sorted(s.pk for s in linked)

    pod, reddit = linked
    IngestionRun.objects.create(source=pod, status="succeeded", items_collected=3,
                                items_created=2, finished_at=timezone.now())
    IngestionRun.objects.create(source=reddit, status="succeeded", items_collected=0,
                                finished_at=timezone.now())
    assert cycles.advance(cycle) == 1

    record = cycle.stages["collect"]
    assert record["state"] == "done" and cycle.stage == "process"
    assert record["counts"]["reddit"]["empty"] == 1
    assert "podcast: 2 new" in record["detail"]
    assert "1 source(s) returned nothing" in record["detail"]


def test_process_waits_for_items_still_being_checked(jarrow, linked, sent):
    cycle = started(jarrow)
    pod = linked[0]
    run = IngestionRun.objects.create(source=pod, status="succeeded", items_collected=1,
                                      finished_at=timezone.now())
    item = ContentItem.objects.create(source=pod, run=run, external_id="e1", title="Ep",
                                      content_hash="e1")
    cycle.run_ids = [run.pk]
    cycle.stage = "process"
    cycle.save()

    assert cycles.advance(cycle) == 60
    assert "1 item(s) still being checked" in cycle.stages["process"]["detail"]

    ContentItem.objects.filter(pk=item.pk).update(gate_decision="rejected")
    assert cycles.advance(cycle) == 1
    assert cycle.stage == "detect"


def test_process_gives_up_after_its_limit_and_says_so(jarrow, linked, sent):
    cycle = started(jarrow)
    pod = linked[0]
    run = IngestionRun.objects.create(source=pod, status="succeeded", finished_at=timezone.now())
    ContentItem.objects.create(source=pod, run=run, external_id="e1", title="Ep", content_hash="e1")
    cycle.run_ids = [run.pk]
    cycle.stage = "process"
    cycle.stages["process"] = {
        "state": "running",
        "started": (timezone.now() - cycles.PROCESS_LIMIT - timedelta(minutes=1)).isoformat(),
    }
    cycle.save()

    cycles.advance(cycle)

    assert "left for the next cycle" in cycle.stages["process"]["detail"]


def test_search_is_skipped_plainly_without_a_provider(jarrow, linked):
    cycle = started(jarrow)
    cycle.stage = "search"
    cycle.save()

    cycles.advance(cycle)

    assert cycle.stages["search"]["state"] == "skipped"
    assert "No search-data provider is configured" in cycle.stages["search"]["detail"]


def test_review_lists_waiting_decisions_and_stops_for_a_person(jarrow, linked, evidence):
    from apps.scoring.tests import test_signal_rank as shared

    signal, claims = evidence
    shared.draft(jarrow, signal, **shared.grounded(claims))
    cycle = started(jarrow)
    cycle.stage = "review"
    cycle.save()

    assert cycles.advance(cycle) is None
    assert cycle.state == ResearchCycle.State.REVIEW
    [waiting] = cycle.stages["review"]["counts"]["recommendations"]
    assert waiting["code"] == "SIG-3001"


def test_a_stage_that_breaks_stops_the_cycle_and_records_why(jarrow, linked, monkeypatch):
    cycle = started(jarrow)
    cycle.stage = "detect"
    cycle.save()

    def broken(cycle, record):
        raise RuntimeError("database went away")

    monkeypatch.setitem(cycles.STAGE_FUNCTIONS, "detect", broken)
    assert cycles.advance(cycle) is None
    assert cycle.state == ResearchCycle.State.FAILED
    assert "database went away" in cycle.stages["detect"]["detail"]


def test_briefs_are_drafted_from_what_is_approved(real_client, linked, approved):
    cycle = started(real_client)
    cycle.state = ResearchCycle.State.REVIEW
    cycle.save()

    with scoped(real_client):
        made = cycles.briefs(cycle, actor_label="abu")

    assert len(made) == 1, "the trend brief; no candidate is approved for the memo"
    assert cycle.state == ResearchCycle.State.DONE
    assert "No candidate is approved" in cycle.stages["briefs"]["detail"]


def test_the_page_starts_a_cycle_and_links_sources(client, admin, real_client, linked, sent,
                                                   monkeypatch):
    from apps.publication import views_cycles

    monkeypatch.setattr(views_cycles.advance_cycle, "delay", lambda pk: None)
    client.force_login(admin)
    url = reverse("ops-cycle", args=[real_client.slug])

    client.post(url, {"action": "sources", "source": [linked[0].pk]})
    with scoped(real_client):
        assert list(BrandSource.objects.values_list("source_id", flat=True)) == [linked[0].pk]

    client.post(url, {"action": "start"})
    page = client.get(url).content.decode()
    assert "Cycle #" in page and "Collect fresh research" in page



def test_drafting_briefs_runs_in_the_background(client, admin, real_client, linked, monkeypatch):
    from apps.publication import views_cycles

    queued = []
    monkeypatch.setattr(views_cycles.draft_cycle_briefs, "apply_async",
                        lambda args, queue: queued.append((args, queue)))
    cycle = started(real_client)
    cycle.state = ResearchCycle.State.REVIEW
    cycle.save()
    client.force_login(admin)

    client.post(reverse("ops-cycle", args=[real_client.slug]),
                {"action": "briefs", "cycle": cycle.pk})

    assert queued == [([cycle.pk, admin.email], "enrich")]
    with scoped(real_client):
        cycle.refresh_from_db()
    assert cycle.stages["briefs"]["state"] == "running"

"""Polling sources, and deciding which are due.

Two failure modes drive this file, and both are silent rather than loud.

A source that collects nothing looks identical to a publisher who has gone
quiet. So a route with no collector, and a source with no access basis, must
raise something an operator can read — not return an empty list.

And a source that is persistently broken must not be retried on every tick
while healthy ones wait, which is why due-ness moves on the ATTEMPT rather
than on the success.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.evidence import collection
from apps.evidence.collection import NotCollectable, collect
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
def podcast(policy):
    return Source.objects.create(
        name="A podcast",
        route=Source.Route.PODCAST,
        policy=policy,
        config={"rss_url": "https://example.test/feed.xml"},
    )


# ── Due-ness ────────────────────────────────────────────────────────────────


def test_a_source_never_polled_is_due(podcast):
    assert podcast.last_polled_at is None
    assert podcast.is_due


def test_a_source_polled_recently_is_not_due(podcast):
    podcast.poll_interval_minutes = 60
    podcast.last_polled_at = timezone.now()
    podcast.save()

    assert not podcast.is_due


def test_a_source_polled_longer_ago_than_its_interval_is_due(podcast):
    podcast.poll_interval_minutes = 60
    podcast.last_polled_at = timezone.now() - timezone.timedelta(minutes=61)
    podcast.save()

    assert podcast.is_due


def test_a_zero_interval_means_never_automatically(podcast):
    """A one-off import, polled only by hand."""
    podcast.poll_interval_minutes = 0
    podcast.save()

    assert not podcast.is_due


def test_a_source_without_a_live_policy_is_never_due(podcast):
    """Not "not yet due" — not collectable at all. PRD §7.2, and the two should
    not be confused when someone asks why nothing is arriving."""
    podcast.policy = None
    podcast.save()

    assert not podcast.can_collect
    assert not podcast.is_due


def test_a_failed_poll_still_moves_due_ness_forward(podcast):
    """Otherwise a broken source is retried on every single beat tick while
    healthy ones wait their turn."""
    podcast.mark_polled(succeeded=False)
    podcast.refresh_from_db()

    assert podcast.last_polled_at is not None
    assert podcast.last_success_at is None, "a failure is not a success"
    assert not podcast.is_due


def test_a_successful_poll_records_both(podcast):
    podcast.mark_polled(succeeded=True)
    podcast.refresh_from_db()

    assert podcast.last_polled_at is not None
    assert podcast.last_success_at is not None


# ── Configuration ───────────────────────────────────────────────────────────


def test_a_podcast_without_a_feed_or_a_name_says_so(policy):
    source = Source.objects.create(
        name="Misconfigured", route=Source.Route.PODCAST, policy=policy, config={}
    )

    with pytest.raises(NotCollectable, match="rss_url or series_name"):
        collect(source)


def test_a_youtube_source_without_queries_or_urls_says_so(policy):
    source = Source.objects.create(
        name="Empty channel", route=Source.Route.YOUTUBE, policy=policy, config={}
    )

    with pytest.raises(NotCollectable, match="queries or urls"):
        collect(source)


def test_a_source_without_an_access_basis_is_refused(podcast):
    podcast.policy = None
    podcast.save()

    with pytest.raises(NotCollectable, match="access basis"):
        collect(podcast)


def test_a_route_with_no_collector_names_itself(policy):
    """Returning zero items would look exactly like a quiet publisher."""
    source = Source.objects.create(
        name="A feed", route=Source.Route.SOCIAL, policy=policy, config={"queries": ["x"]}
    )

    with pytest.raises(NotCollectable, match="social"):
        collect(source)


# ── Dispatch ────────────────────────────────────────────────────────────────


def test_the_feed_url_is_preferred_over_the_series_name(podcast, monkeypatch):
    """Taddy matches `name` EXACTLY — "Huberman Lab" resolves and "huberman
    lab" does not, which is a sharp edge to leave an operator standing on."""
    podcast.config = {
        "rss_url": "https://example.test/feed.xml",
        "series_name": "A Podcast",
    }
    podcast.save()

    seen = {}

    class FakeConnector:
        def episodes(self, *, rss_url="", name="", limit=25):
            seen.update(rss_url=rss_url, name=name, limit=limit)
            return [{"external_id": "ep-1"}]

    monkeypatch.setattr(
        "apps.connectors.factory.taddy_for", lambda source: FakeConnector()
    )

    rows = collect(podcast)

    assert rows == [{"external_id": "ep-1"}]
    assert seen["rss_url"] == "https://example.test/feed.xml"
    assert seen["name"] == ""


def test_a_source_may_lower_its_own_limit(podcast, monkeypatch):
    """Apify charges per video returned, so the ceiling is a spend control."""
    podcast.config = {"rss_url": "https://example.test/feed.xml", "limit": 3}
    podcast.save()

    seen = {}

    class FakeConnector:
        def episodes(self, *, rss_url="", name="", limit=25):
            seen["limit"] = limit
            return []

    monkeypatch.setattr(
        "apps.connectors.factory.taddy_for", lambda source: FakeConnector()
    )

    collect(podcast, limit=50)

    assert seen["limit"] == 3, "the source's own limit must win when it is lower"


def test_every_route_has_an_entry_so_none_falls_through_silently():
    """A route added to the model without a collector should raise by name,
    not vanish into a KeyError or an empty list."""
    for route, _ in Source.Route.choices:
        assert route in collection.COLLECTORS, f"no COLLECTORS entry for {route!r}"

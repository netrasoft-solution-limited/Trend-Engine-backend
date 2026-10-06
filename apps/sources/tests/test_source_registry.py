"""The source registry screen — which shows and channels are watched.

The failure this screen exists to prevent is specific and was already live: a
`Source` saves perfectly with the wrong config keys, reports itself active, and
then collects nothing forever. Two of the four sources in the dev database were
in exactly that state. It reads as a quiet category rather than a broken source,
which is why nobody notices.

So most of what is tested here is refusal: a source that cannot collect is not
saved, and one that cannot collect says why on every row afterwards.
"""
from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.operations.models import OperatorUser
from apps.sources import registry
from apps.sources.models import AcquisitionProvider, ProviderPolicyVersion, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(db):
    return OperatorUser.objects.create_user(
        email="admin@netrasoft.test", password="x" * 24, name="Admin",
        role=OperatorUser.Role.PLATFORM_ADMIN,
    )


@pytest.fixture
def plain_operator(db):
    return OperatorUser.objects.create_user(
        email="op@netrasoft.test", password="x" * 24, name="Operator"
    )


@pytest.fixture
def policy(db):
    provider = AcquisitionProvider.objects.create(name="Taddy", kind=AcquisitionProvider.Kind.TADDY)
    return ProviderPolicyVersion.objects.create(
        provider=provider,
        version="2026-01",
        access_basis="Paid API subscription with transcript rights.",
        approved_at=timezone.now(),
        approved_by_label="mark@pureplay.example",
    )


@pytest.fixture
def apify_policy(db):
    """YouTube is collected through Apify, so a YouTube source's access basis
    has to be Apify's."""
    provider = AcquisitionProvider.objects.create(
        name="Apify", kind=AcquisitionProvider.Kind.APIFY
    )
    return ProviderPolicyVersion.objects.create(
        provider=provider,
        version="2026-01",
        access_basis="Apify platform terms, maintained public-data Actor.",
        approved_at=timezone.now(),
        approved_by_label="mark@pureplay.example",
    )


@pytest.fixture
def unapproved_policy(db):
    provider = AcquisitionProvider.objects.create(name="Apify", kind=AcquisitionProvider.Kind.APIFY)
    return ProviderPolicyVersion.objects.create(
        provider=provider, version="draft", access_basis="Not approved yet."
    )


# ── The config contract ─────────────────────────────────────────────────────


def test_a_podcast_needs_a_feed_or_a_name():
    problem = registry.config_problem(Source.Route.PODCAST, {})

    assert "rss feed address or the show's name" in problem.lower()


def test_either_one_is_enough():
    assert registry.config_problem(Source.Route.PODCAST, {"rss_url": "https://x/f"}) == ""
    assert registry.config_problem(Source.Route.PODCAST, {"series_name": "The Drive"}) == ""


def test_youtube_needs_queries_or_urls():
    assert registry.config_problem(Source.Route.YOUTUBE, {}) != ""
    assert registry.config_problem(Source.Route.YOUTUBE, {"queries": ["magnesium"]}) == ""


def test_the_social_route_explains_that_it_is_gated_not_merely_unbuilt():
    """PRD §4.2: each platform needs its own access basis, cost line and
    approval. "Not built yet" would invite someone to build it."""
    problem = registry.config_problem(Source.Route.SOCIAL, {})

    assert "commercial decision" in problem
    assert "approval" in problem


def test_the_research_route_says_it_is_a_package_not_a_setting():
    assert "planned package" in registry.config_problem(Source.Route.RESEARCH, {})


# ── Who may reach it ────────────────────────────────────────────────────────


def test_a_plain_operator_cannot_reach_the_registry(client, plain_operator):
    client.force_login(plain_operator)

    assert client.get(reverse("ops-sources")).status_code == 403


def test_a_plain_operator_cannot_add_a_source(client, plain_operator, policy):
    client.force_login(plain_operator)

    response = client.post(
        reverse("ops-sources"),
        {"name": "Sneaky", "route": "podcast", "config_rss_url": "https://x/f",
         "policy": policy.pk, "interval_hours": "24"},
    )

    assert response.status_code == 403
    assert not Source.objects.filter(name="Sneaky").exists()


# ── Adding a source ─────────────────────────────────────────────────────────


def test_a_podcast_can_be_added_from_the_browser(client, admin, policy):
    client.force_login(admin)

    client.post(
        reverse("ops-sources"),
        {"name": "The Peter Attia Drive", "route": "podcast",
         "config_rss_url": "https://feeds.example.com/attia",
         "policy": policy.pk, "interval_hours": "168"},
        follow=True,
    )

    source = Source.objects.get(name="The Peter Attia Drive")
    assert source.config == {"rss_url": "https://feeds.example.com/attia"}
    assert source.poll_interval_minutes == 168 * 60, "hours in the form, minutes in the database"
    assert source.can_collect


def test_youtube_search_terms_are_stored_as_a_list(client, admin, apify_policy):
    """One comma-separated input rather than asking an operator to type JSON."""
    client.force_login(admin)

    client.post(
        reverse("ops-sources"),
        {"name": "Supplements search", "route": "youtube",
         "config_queries": "magnesium sleep, creatine cognition ,  ",
         "policy": apify_policy.pk, "interval_hours": "24"},
        follow=True,
    )

    assert Source.objects.get(name="Supplements search").config == {
        "queries": ["magnesium sleep", "creatine cognition"]
    }


def test_a_source_that_could_never_collect_is_refused(client, admin, policy):
    """The whole reason this screen exists: it would save perfectly and then
    return nothing forever."""
    client.force_login(admin)

    response = client.post(
        reverse("ops-sources"),
        {"name": "Nothing to watch", "route": "podcast",
         "policy": policy.pk, "interval_hours": "24"},
        follow=True,
    )

    assert "harder problem to spot" in response.content.decode()
    assert not Source.objects.filter(name="Nothing to watch").exists()


def test_a_source_without_an_access_basis_is_refused(client, admin):
    """PRD §7.2: no connector runs without a recorded one, so a source saved
    without it would sit there looking active and never collect."""
    client.force_login(admin)

    response = client.post(
        reverse("ops-sources"),
        {"name": "Unlicensed", "route": "podcast",
         "config_series_name": "Some Show", "interval_hours": "24"},
        follow=True,
    )

    assert "access basis" in response.content.decode()
    assert not Source.objects.filter(name="Unlicensed").exists()


def test_an_unapproved_policy_does_not_count(client, admin, unapproved_policy):
    """`is_live` is approved AND not withdrawn. An unapproved policy is not a
    policy — it must not be selectable into a working source."""
    client.force_login(admin)

    client.post(
        reverse("ops-sources"),
        {"name": "Premature", "route": "podcast", "config_series_name": "X",
         "policy": unapproved_policy.pk, "interval_hours": "24"},
        follow=True,
    )

    assert not Source.objects.filter(name="Premature").exists()


def test_a_gated_route_cannot_be_added_at_all(client, admin, policy):
    client.force_login(admin)

    response = client.post(
        reverse("ops-sources"),
        {"name": "TikTok pilot", "route": "social", "policy": policy.pk,
         "interval_hours": "24"},
        follow=True,
    )

    assert "commercial decision" in response.content.decode()
    assert not Source.objects.filter(name="TikTok pilot").exists()


def test_two_sources_cannot_share_a_name(client, admin, policy):
    client.force_login(admin)
    payload = {"name": "The Drive", "route": "podcast", "config_series_name": "The Drive",
               "policy": policy.pk, "interval_hours": "24"}
    client.post(reverse("ops-sources"), payload, follow=True)

    response = client.post(reverse("ops-sources"), payload, follow=True)

    assert "already called" in response.content.decode()
    assert Source.objects.filter(name="The Drive").count() == 1


# ── The list tells the truth about broken sources ───────────────────────────


def test_a_misconfigured_source_is_flagged_on_the_list(client, admin, policy):
    """Rows created before this screen existed, or by a command, must still be
    diagnosable from it."""
    client.force_login(admin)
    Source.objects.create(name="Legacy, empty", route=Source.Route.PODCAST, policy=policy)

    body = client.get(reverse("ops-sources")).content.decode()

    assert "Nothing to collect from" in body


def test_a_source_with_no_policy_shows_why_it_is_not_collecting(client, admin):
    client.force_login(admin)
    Source.objects.create(
        name="No basis", route=Source.Route.PODCAST, config={"series_name": "X"}
    )

    body = client.get(reverse("ops-sources")).content.decode()

    assert "no access basis" in body


# ── Editing one ─────────────────────────────────────────────────────────────


def test_pausing_stops_collection(client, admin, policy):
    client.force_login(admin)
    source = Source.objects.create(
        name="Noisy", route=Source.Route.PODCAST, config={"series_name": "X"}, policy=policy
    )

    client.post(reverse("ops-source", args=[source.pk]), {"action": "pause"}, follow=True)

    source.refresh_from_db()
    assert source.can_collect is False
    assert source.is_due is False


def test_resuming_starts_it_again(client, admin, policy):
    client.force_login(admin)
    source = Source.objects.create(
        name="Noisy", route=Source.Route.PODCAST, config={"series_name": "X"},
        policy=policy, status=Source.Status.FAILED,
    )

    client.post(reverse("ops-source", args=[source.pk]), {"action": "resume"}, follow=True)

    source.refresh_from_db()
    assert source.can_collect is True


def test_an_edit_that_would_break_collection_is_refused(client, admin, policy):
    client.force_login(admin)
    source = Source.objects.create(
        name="Working", route=Source.Route.PODCAST,
        config={"series_name": "The Drive"}, policy=policy,
    )

    client.post(
        reverse("ops-source", args=[source.pk]),
        {"action": "save", "name": "Working", "config_series_name": "", "interval_hours": "24"},
        follow=True,
    )

    source.refresh_from_db()
    assert source.config == {"series_name": "The Drive"}, "the working config survives"


def test_polling_by_hand_is_refused_when_the_source_cannot_collect(client, admin):
    """Dispatching a job that is certain to fail wastes a worker and writes a
    confusing run record."""
    client.force_login(admin)
    source = Source.objects.create(
        name="No basis", route=Source.Route.PODCAST, config={"series_name": "X"}
    )

    response = client.post(
        reverse("ops-source", args=[source.pk]), {"action": "poll"}, follow=True
    )

    assert "no live access basis" in response.content.decode()


def test_polling_by_hand_dispatches_by_task_name(client, admin, policy, monkeypatch):
    """`apps.evidence` sits ABOVE `apps.sources`, so the task is sent by name
    over the queue rather than imported. import-linter enforces the rest."""
    client.force_login(admin)
    source = Source.objects.create(
        name="Working", route=Source.Route.PODCAST,
        config={"series_name": "The Drive"}, policy=policy,
    )
    sent = []

    from celery import current_app

    monkeypatch.setattr(
        current_app, "send_task", lambda name, **kw: sent.append((name, kw))
    )

    client.post(reverse("ops-source", args=[source.pk]), {"action": "poll"}, follow=True)

    assert sent == [("apps.evidence.tasks.poll_source", {"args": [source.pk], "queue": "ingest"})]


def test_an_interval_of_zero_means_only_when_asked(client, admin, policy):
    """Kept rather than clamped to a minimum: a source polled only by hand is
    how a one-off import behaves."""
    client.force_login(admin)
    source = Source.objects.create(
        name="Manual only", route=Source.Route.PODCAST,
        config={"series_name": "X"}, policy=policy,
    )

    client.post(
        reverse("ops-source", args=[source.pk]),
        {"action": "save", "name": "Manual only", "config_series_name": "X",
         "interval_hours": "0"},
        follow=True,
    )

    source.refresh_from_db()
    assert source.poll_interval_minutes == 0
    assert source.is_due is False, "never automatically due"
    assert source.can_collect is True, "but still collectable by hand"


def test_a_nonsense_interval_falls_back_rather_than_failing(client, admin, policy):
    client.force_login(admin)
    source = Source.objects.create(
        name="Typo", route=Source.Route.PODCAST, config={"series_name": "X"}, policy=policy
    )

    client.post(
        reverse("ops-source", args=[source.pk]),
        {"action": "save", "name": "Typo", "config_series_name": "X",
         "interval_hours": "every tuesday"},
        follow=True,
    )

    source.refresh_from_db()
    assert source.poll_interval_minutes == 24 * 60


# ── Audit ───────────────────────────────────────────────────────────────────


def test_every_change_is_audited_without_recording_the_config_values(client, admin, policy):
    """PRD §7.1 wants configuration changes in the audit log. The keys are
    recorded, not the values — a source config can carry a signed feed URL."""
    from apps.operations.models import AuditEvent

    client.force_login(admin)
    client.post(
        reverse("ops-sources"),
        {"name": "Audited", "route": "podcast", "config_rss_url": "https://secret.example/f?token=abc",
         "policy": policy.pk, "interval_hours": "24"},
        follow=True,
    )

    event = AuditEvent.objects.filter(message__contains="Audited").first()
    assert event is not None
    assert event.context["config_keys"] == ["rss_url"]
    assert "secret.example" not in str(event.context)


# ── The access basis must belong to the vendor that actually collects ───────


def test_a_source_cannot_be_filed_under_the_wrong_vendors_policy(client, admin, policy):
    """YouTube is fetched by Apify. Recording it under Taddy's access basis
    would be a legally wrong record that reads correctly in every later view —
    the exact failure PRD §7.2 exists to prevent, and one no other check here
    would catch."""
    client.force_login(admin)

    response = client.post(
        reverse("ops-sources"),
        {"name": "Wrong basis", "route": "youtube", "config_queries": "magnesium",
         "policy": policy.pk, "interval_hours": "24"},
        follow=True,
    )

    assert "does not collect youtube sources" in response.content.decode()
    assert not Source.objects.filter(name="Wrong basis").exists()


def test_each_route_is_only_offered_its_own_vendors_policies(client, admin, policy, apify_policy):
    """Filtering the dropdown is the convenience; the refusal above is the rule.
    Both exist because an operator shown one list of every live policy picks the
    first plausible entry."""
    from apps.sources import registry

    live = [policy, apify_policy]

    podcast_options = registry.policies_for(Source.Route.PODCAST, live)
    youtube_options = registry.policies_for(Source.Route.YOUTUBE, live)

    assert podcast_options == [policy]
    assert youtube_options == [apify_policy]


def test_a_route_with_no_approved_policy_says_so_rather_than_offering_a_wrong_one(
    client, admin, policy
):
    """Only Taddy is approved here, so the YouTube form must refuse rather than
    fall back to it."""
    client.force_login(admin)

    body = client.get(reverse("ops-sources")).content.decode()

    assert "No approved access basis exists for the vendor that collects" in body

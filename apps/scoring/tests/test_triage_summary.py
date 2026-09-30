"""GET ops.<domain>/api/triage/summary — counts, window, ranking, errors, auth."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ingestion.models import IngestionRun
from apps.intelligence.models import Digest, Signal
from apps.scoring.models import ClientSignalScore
from apps.scoring.triage import Window, default_window, triage_summary
from apps.sources.models import Source
from apps.tenancy.context import operator_scope
from apps.tenancy.middleware import OPERATOR_SCOPE_SESSION_KEY
from apps.tenancy.models import Organization

pytestmark = pytest.mark.django_db

SUMMARY = "/api/triage/summary"


@pytest.fixture(autouse=True)
def _operator_plane(settings):
    assert settings.ROOT_URLCONF == "config.urls_ops", "Run these tests with the ops settings"


@pytest.fixture
def api():
    """Signed in as an operator."""
    client = APIClient()
    user = get_user_model().objects.create_user(
        email="abubakar@pureplay.example", password="x-password-2026", name="Abubakar"
    )
    client.force_login(user)
    return client


def at(day: int, hour: int = 12) -> datetime:
    """A moment in September 2026."""
    return datetime(2026, 9, day, hour, tzinfo=UTC)


#: Inside the mock's window, Sep 13 – Sep 20.
IN_WINDOW = at(15)


def signal(code, *, first_seen=IN_WINDOW, domain=50, state=Signal.State.CANDIDATE, **flags):
    return Signal.objects.create(
        code=code, title=code, state=state, domain_score=domain, first_seen_at=first_seen, **flags
    )


def digests():
    """The mock's history: the previous digest closed Sep 13, the latest Sep 20."""
    Digest.objects.create(
        window_start=date(2026, 9, 6), window_end=date(2026, 9, 13), created_at=at(13, 6)
    )
    Digest.objects.create(
        window_start=date(2026, 9, 13), window_end=date(2026, 9, 20), created_at=at(20, 6)
    )


def get(api, **params):
    return api.get(SUMMARY, params)


# ── auth ────────────────────────────────────────────────────────────────────


def test_unauthenticated_access_is_rejected_with_401_json():
    response = APIClient().get(SUMMARY)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


# ── the response shape, end to end against the seed ─────────────────────────


def test_the_seeded_data_gives_the_mock_summary(api):
    call_command("seed_triage", stdout=StringIO())

    response = get(api)

    assert response.status_code == 200
    assert response.json() == {
        "window": {"from": "2026-09-13", "to": "2026-09-20"},
        "new_candidates": 12,
        "review_ready": 4,
        "research_alerts": 1,
        "blocking_failures": 0,
        "top_candidate_id": "SIG-2041",
    }


# ── counts ──────────────────────────────────────────────────────────────────


def test_each_count_uses_its_own_definition(api):
    digests()
    signal("SIG-1", domain=80)
    signal("SIG-2", domain=70, needs_scientific_routing=True)
    signal("SIG-3", state=Signal.State.IN_REVIEW, awaiting_operator_judgment=True)
    signal("SIG-4", state=Signal.State.WATCHING, awaiting_operator_judgment=True)
    signal("SIG-5", state=Signal.State.REJECTED)

    body = get(api).json()

    assert body["new_candidates"] == 2
    assert body["review_ready"] == 2
    assert body["research_alerts"] == 1
    assert body["top_candidate_id"] == "SIG-1"


def test_blocking_failures_are_failed_blocking_runs_plus_failed_sources(api):
    ok = Source.objects.create(name="ok", route=Source.Route.PODCAST)
    Source.objects.create(name="broken", route=Source.Route.YOUTUBE, status=Source.Status.FAILED)
    Source.objects.create(name="slow", route=Source.Route.SOCIAL, status=Source.Status.DEGRADED)
    IngestionRun.objects.create(source=ok, status=IngestionRun.Status.FAILED, blocking=True)
    IngestionRun.objects.create(source=ok, status=IngestionRun.Status.FAILED, blocking=False)
    IngestionRun.objects.create(source=ok, status=IngestionRun.Status.SUCCEEDED, blocking=True)
    IngestionRun.objects.create(source=ok, status=IngestionRun.Status.RUNNING, blocking=True)

    assert get(api).json()["blocking_failures"] == 2


def test_backlog_counts_ignore_the_window(api):
    """Review ready and research alerts are the current backlog."""
    signal("SIG-1", first_seen=at(1), awaiting_operator_judgment=True,
           needs_scientific_routing=True, state=Signal.State.IN_REVIEW)

    body = get(api, **{"from": "2026-09-13", "to": "2026-09-20"}).json()

    assert (body["review_ready"], body["research_alerts"]) == (1, 1)


def test_the_counts_take_a_fixed_number_of_queries(django_assert_num_queries):
    """Aggregates, not a query per row: 50 signals cost the same as none."""
    digests()
    for n in range(50):
        signal(f"SIG-{n:03d}", domain=n, awaiting_operator_judgment=n % 2 == 0)

    with operator_scope(), django_assert_num_queries(5):  # 1 digest + 4 aggregates
        triage_summary(default_window())
    with operator_scope(), django_assert_num_queries(4):
        triage_summary(Window(date(2026, 9, 13), date(2026, 9, 20)))


# ── the window ──────────────────────────────────────────────────────────────


def test_without_dates_the_window_is_the_latest_digests(api):
    digests()

    assert get(api).json()["window"] == {"from": "2026-09-13", "to": "2026-09-20"}


def test_by_default_candidates_before_the_previous_digest_are_not_new(api):
    digests()
    signal("SIG-OLD", first_seen=at(10), domain=99)
    signal("SIG-NEW", first_seen=at(14), domain=10)

    body = get(api).json()

    assert body["new_candidates"] == 1
    assert body["top_candidate_id"] == "SIG-NEW"


def test_the_date_filter_selects_candidates_by_first_seen_day(api):
    digests()
    signal("SIG-A", first_seen=at(2), domain=90)
    signal("SIG-B", first_seen=at(4), domain=60)
    signal("SIG-C", first_seen=at(15), domain=70)

    body = get(api, **{"from": "2026-09-01", "to": "2026-09-05"}).json()

    assert body["window"] == {"from": "2026-09-01", "to": "2026-09-05"}
    assert body["new_candidates"] == 2
    assert body["top_candidate_id"] == "SIG-A"


def test_both_ends_of_the_window_are_inclusive_days(api):
    signal("SIG-START", first_seen=at(13, 0))           # first moment of `from`
    signal("SIG-END", first_seen=at(20, 23))            # late on `to`
    signal("SIG-AFTER", first_seen=at(21, 0))           # first moment after `to`
    signal("SIG-BEFORE", first_seen=at(13, 0) - timedelta(seconds=1))

    body = get(api, **{"from": "2026-09-13", "to": "2026-09-20"}).json()

    assert body["new_candidates"] == 2


def test_a_single_day_window_is_allowed(api):
    signal("SIG-1", first_seen=at(15))

    body = get(api, **{"from": "2026-09-15", "to": "2026-09-15"}).json()

    assert body["new_candidates"] == 1


# ── ranking of the top candidate ────────────────────────────────────────────


def test_top_candidate_uses_the_narrowed_clients_fit(api):
    jarrow = Organization.objects.create(slug="jarrow", name="Jarrow Formulas")
    a, b = signal("SIG-A", domain=70), signal("SIG-B", domain=70)
    with operator_scope():
        ClientSignalScore.objects.create(organization=jarrow, signal=a, fit_score=20, confidence=50)
        ClientSignalScore.objects.create(organization=jarrow, signal=b, fit_score=90, confidence=50)
    window = {"from": "2026-09-13", "to": "2026-09-20"}

    # Across every tenant there is no single fit: domain score, then code.
    assert get(api, **window).json()["top_candidate_id"] == "SIG-A"

    session = api.session
    session[OPERATOR_SCOPE_SESSION_KEY] = jarrow.pk
    session.save()

    assert get(api, **window).json()["top_candidate_id"] == "SIG-B"


# ── the empty case ──────────────────────────────────────────────────────────


def test_with_no_data_at_all_every_count_is_zero(api):
    response = get(api)

    assert response.status_code == 200
    today = timezone.localdate()
    assert response.json() == {
        "window": {"from": (today - timedelta(days=7)).isoformat(), "to": today.isoformat()},
        "new_candidates": 0,
        "review_ready": 0,
        "research_alerts": 0,
        "blocking_failures": 0,
        "top_candidate_id": None,
    }


def test_a_window_with_no_candidates_has_a_null_top_candidate(api):
    signal("SIG-1", first_seen=at(15))

    body = get(api, **{"from": "2026-08-01", "to": "2026-08-31"}).json()

    assert (body["new_candidates"], body["top_candidate_id"]) == (0, None)


# ── invalid input ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-13-01", "to": "2026-09-20"},   # no month 13
        {"from": "2026-02-30", "to": "2026-03-01"},   # no Feb 30
        {"from": "yesterday", "to": "2026-09-20"},
        {"from": "20/09/2026", "to": "2026-09-20"},   # wrong format
        {"from": "2026-09-13T00:00", "to": "2026-09-20"},
        {"from": "2026-09-20", "to": "2026-09-13"},   # from after to
        {"from": "2026-09-13"},                       # only one end
        {"to": "2026-09-20"},
    ],
    ids=["month-13", "feb-30", "word", "dmy", "datetime", "reversed", "from-only", "to-only"],
)
def test_invalid_dates_are_a_400(api, params):
    assert get(api, **params).status_code == 400

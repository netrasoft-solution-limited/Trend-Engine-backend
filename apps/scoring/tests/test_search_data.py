"""Search data inside the cycle (Mark, 20 Nov)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.connectors.providers.dataforseo import DataForSeo, DataForSeoError
from apps.evidence.models import Observation
from apps.operations.models import CostEvent
from apps.scoring import opportunities, search_data
from apps.scoring.models import Candidate
from apps.scoring.tests import test_signal_rank as shared
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db

evidence = shared.evidence
jarrow = shared.jarrow


def ok(result):
    return {"status_code": 20000, "status_message": "Ok.",
            "tasks": [{"status_code": 20000, "cost": 0.05, "result": result}]}


def transport(calls):
    def respond(url, payload):
        calls.append((url, payload))
        if "search_volume" in url:
            return ok([{"keyword": k, "search_volume": 14800, "competition": "LOW",
                        "monthly_searches": [{"year": 2026, "month": 10, "search_volume": 12100},
                                             {"year": 2026, "month": 9, "search_volume": 14800}]}
                       for k in payload[0]["keywords"]])
        start = date.fromisoformat(payload[0]["date_from"])
        data = []
        for n in range(56):
            day = start + timedelta(days=n)
            value = 60 if n >= 28 else 40
            data.append({"date_from": day.isoformat(), "date_to": day.isoformat(),
                         "values": [value] * len(payload[0]["keywords"]),
                         "missing_data": False})
        return ok([{"items": [{"type": "google_trends_graph",
                               "keywords": payload[0]["keywords"], "data": data}]}])
    return respond


def test_a_failed_task_is_an_error_not_zeros():
    api = DataForSeo(login="l", password="p", transport=lambda url, payload: {
        "status_code": 20000, "tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]})

    with pytest.raises(DataForSeoError, match="40501 Invalid Field"):
        api.volumes(["spermidine"])


def test_refresh_stores_dated_readings_and_attaches_them_without_withdrawing_approval(jarrow):
    with scoped(jarrow):
        candidate = opportunities.create(jarrow, name="Spermidine", actor_label="abu")
        Candidate.objects.filter(pk=candidate.pk).update(approved_at="2026-11-20T00:00:00Z")
    calls = []
    api = DataForSeo(login="l", password="p", transport=transport(calls))

    with scoped(jarrow):
        result = search_data.refresh(jarrow, actor_label="cycle", client=api)
        candidate.refresh_from_db()

    volume = Observation.objects.get(metric_kind="rolling_monthly_search_estimate")
    trends = Observation.objects.get(metric_kind="trends_relative_interest")
    assert volume.metric_label == "14,800 a month (12-month average); 12,100 in Oct 2026"
    assert volume.period == "12 months to Oct 2026" and "rounded into ranges" in volume.limitations
    assert trends.metric_label.endswith(": +50%")
    assert result["attached"] == 2 and candidate.approved_at is not None
    assert CostEvent.objects.get(provider="dataforseo").estimated_usd == Decimal("0.10")


def test_without_a_provider_the_cycle_says_so():
    assert search_data.available() is False

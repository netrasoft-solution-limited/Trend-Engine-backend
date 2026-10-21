"""Hand-entered observations (Mark, 20 Oct): traceable, typed, labelled."""
from __future__ import annotations

import pytest

from apps.evidence.models import Observation
from apps.evidence.services import observations as service

ROW = {
    "url": "https://www.tiktok.com/shop/pdp/spermidine",
    "title": "Black Forest spermidine on TikTok Shop",
    "source_type": "tiktok_shop",
    "observed_at": "2026-10-20",
    "metric_kind": "lifetime_shop_sales",
    "metric_value": "4,600",
    "metric_label": "4.6K items sold globally",
    "topic": "spermidine",
    "is_ad": "no",
}


def test_a_complete_row_is_cleaned():
    data = service.clean(ROW)

    assert data["metric_value"] == 4600
    assert data["metric_kind"] == "lifetime_shop_sales"
    assert data["is_ad"] is False


@pytest.mark.parametrize("missing", ["url", "title", "observed_at"])
def test_an_untraceable_row_is_refused(missing):
    with pytest.raises(service.ObservationError, match=missing):
        service.clean({**ROW, missing: ""})


def test_a_search_figure_needs_its_query_provider_geography_and_period():
    row = {**ROW, "source_type": "search_estimate",
           "metric_kind": "rolling_monthly_search_estimate", "metric_value": "14800",
           "query": "spermidine"}

    with pytest.raises(service.ObservationError, match="provider, geography, period"):
        service.clean(row)


def test_a_figure_must_say_what_it_measures():
    with pytest.raises(service.ObservationError, match="metric_kind"):
        service.clean({**ROW, "metric_kind": ""})


def test_a_sheet_with_one_bad_row_reports_it_by_number():
    header = ",".join(service.COLUMNS)
    good = ",".join(str(service.EXAMPLE[c]) for c in service.COLUMNS)
    bad = good.replace("https://www.reddit.com", "not-a-url", 1)

    rows, errors = service.parse_csv(f"{header}\n{good}\n{bad}\n")

    assert len(rows) == 1
    assert errors == ["Row 3: 'url' must be a full web address (http or https)."]


@pytest.mark.django_db
def test_one_interview_in_audio_and_video_counts_once():
    def make(title, key):
        return Observation.objects.create(
            url="https://x.test/" + title, title=title, source_type="podcast",
            observed_at="2026-10-20", event_key=key, entered_by="t",
        )

    obs = [make("audio", "perlman-14oct"), make("video", "Perlman-14Oct"), make("other", "")]

    assert service.independent_count(obs) == 2


@pytest.mark.django_db
def test_the_import_screen_is_all_or_nothing(client):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse

    from apps.operations.models import OperatorUser

    client.force_login(
        OperatorUser.objects.create_user(email="o@x.test", password="x" * 24, name="O")
    )
    header = ",".join(service.COLUMNS)
    good = ",".join(str(service.EXAMPLE[c]) for c in service.COLUMNS)
    sheet = f"{header}\n{good}\n{good.replace('2026-10-20', 'yesterday')}\n"

    response = client.post(
        reverse("ops-observations-import"),
        {"file": SimpleUploadedFile("obs.csv", sheet.encode())}, follow=True,
    )

    assert "Nothing imported" in response.content.decode()
    assert not Observation.objects.exists()


def test_a_figure_with_no_public_page_needs_its_provenance():
    row = {**service.EXAMPLE, "url": "", "source_type": "search_estimate",
           "metric_kind": "rolling_monthly_search_estimate", "metric_value": "14,800",
           "query": "spermidine", "provider": "Keyword data", "geography": "US",
           "period": "12-month average"}

    with pytest.raises(service.ObservationError, match="unless 'provenance'"):
        service.clean(row)
    assert service.clean({**row, "provenance": "Pure Play opportunity brief [2]"})["url"] == ""

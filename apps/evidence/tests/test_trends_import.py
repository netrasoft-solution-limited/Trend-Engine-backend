"""Google Trends readings from Google's export, with exact periods (Mark, 15 Nov)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from apps.evidence.models import Observation
from apps.evidence.services import trends_import


def export(days=90, end=date(2026, 11, 14)):
    start = end - timedelta(days=days - 1)
    rows = ["Category: All categories", "",
            "Day,spermidine: (United States),urolithin a: (United States)"]
    for n in range(days):
        day = start + timedelta(days=n)
        recent = day > end - timedelta(days=28)
        rows.append(f"{day.isoformat()},{60 if recent else 40},{'<1' if n % 2 else 2}")
    return "\n".join(rows) + "\n"


def test_an_export_is_recognised_and_an_observation_sheet_is_not():
    assert trends_import.looks_like_export(export())
    assert not trends_import.looks_like_export("url,title,source_type\nhttps://x,y,reddit\n")


def test_the_latest_28_days_are_compared_with_the_28_before():
    spermidine, urolithin = trends_import.parse(export())

    assert spermidine.term == "spermidine" and spermidine.geography == "US"
    assert spermidine.period == "18 Oct–14 Nov 2026 vs 20 Sep–17 Oct 2026"
    assert (spermidine.current, spermidine.baseline) == (60, 40)
    assert spermidine.change == "+50%"
    assert "Average interest 60 (18 Oct–14 Nov) against 40 (20 Sep–17 Oct): +50%" == (
        spermidine.label
    )
    assert urolithin.baseline == pytest.approx(1.25, abs=0.1), "'<1' is read as 0.5"


def test_a_short_export_is_refused_with_what_to_download():
    with pytest.raises(trends_import.TrendsImportError, match="Past 90 days"):
        trends_import.parse(export(days=30))


@pytest.mark.django_db
def test_each_term_becomes_a_dated_trends_observation():
    saved = trends_import.save(trends_import.parse(export()), entered_by="abu")

    first = Observation.objects.get(pk=saved[0].pk)
    assert first.source_type == "google_trends"
    assert first.metric_kind == "trends_relative_interest"
    assert first.published_at == date(2026, 11, 14)
    assert first.provider == "Google Trends (CSV export)"
    assert "not search counts" in first.limitations
    assert "Exported together with: urolithin a." in first.limitations

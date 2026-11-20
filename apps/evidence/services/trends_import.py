"""Google Trends readings from Google's own CSV export, with exact periods.

Why an import and not a connector: Google Trends has no official API, and its
public endpoint answers our server with HTTP 429 (tested 15 Nov 2026). The
export a person downloads from trends.google.com ("multiTimeline.csv") is the
same data, so it is read here and turned into one observation per search term:
the average relative interest over the latest 28 days against the 28 days
before, both periods named, retrieved today.

Mark, 15 Nov: the memo's Trends comparison ended 17 Oct and was retrieved
20 Oct; a reading must say exactly which periods it compares and when it was
taken, and an old one must not pass as current research.

Values are relative within one export (0–100, the peak of all terms in the
file is 100), so two terms are comparable only when exported together, and a
reading is never a count of searches.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from apps.evidence.models import Observation

PERIOD_DAYS = 28
PROVIDER = "Google Trends (CSV export)"
GEOGRAPHIES = {"united states": "US", "worldwide": "Worldwide", "united kingdom": "GB"}
LIMITATIONS = (
    "Sampled, normalised relative interest (0–100 within this export), not search counts. "
    "Compares the average over the latest {days} days with the {days} days before. Does not "
    "establish a breakout."
)


class TrendsImportError(ValueError):
    pass


@dataclass(frozen=True)
class Reading:
    term: str
    geography: str
    current_from: date
    current_to: date
    baseline_from: date
    baseline_to: date
    current: float
    baseline: float

    @property
    def change(self) -> str:
        if not self.baseline:
            return "no baseline interest" if not self.current else "from zero"
        return f"{(self.current - self.baseline) / self.baseline:+.0%}"

    @property
    def period(self) -> str:
        return (
            f"{self.current_from:%-d %b}–{self.current_to:%-d %b %Y} vs "
            f"{self.baseline_from:%-d %b}–{self.baseline_to:%-d %b %Y}"
        )

    @property
    def label(self) -> str:
        return (
            f"Average interest {self.current:.0f} ({self.current_from:%-d %b}–"
            f"{self.current_to:%-d %b}) against {self.baseline:.0f} ({self.baseline_from:%-d %b}–"
            f"{self.baseline_to:%-d %b}): {self.change}"
        )


_HEADER = re.compile(r"^(?P<term>.+?):\s*\((?P<geo>[^)]*)\)\s*$")


def _value(cell: str) -> float:
    cell = (cell or "").strip()
    if cell.startswith("<"):
        return 0.5
    try:
        return float(cell)
    except ValueError:
        return 0.0


def looks_like_export(text: str) -> bool:
    """A Trends export, not an observation sheet: a Day/Week column, no url column."""
    head = [line.strip().lower() for line in text.lstrip("\ufeff").splitlines()[:5]]
    return any(line.startswith(("day,", "week,", "month,")) for line in head) and not any(
        line.startswith("url,") for line in head
    )


def parse(text: str) -> list[Reading]:
    """Readings from a multiTimeline.csv. Daily or weekly rows both work."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    start = next(
        (i for i, r in enumerate(rows) if r and r[0].strip().lower() in ("day", "week", "month")),
        None,
    )
    if start is None:
        raise TrendsImportError(
            "This is not a Google Trends export: no Day/Week column. Download it with the "
            "arrow on the 'Interest over time' chart."
        )
    header, data = rows[start], rows[start + 1:]
    series: list[tuple[date, list[float]]] = []
    for row in data:
        if not row or not row[0].strip():
            continue
        try:
            day = date.fromisoformat(row[0].strip()[:10])
        except ValueError:
            continue
        series.append((day, [_value(c) for c in row[1:]]))
    if not series:
        raise TrendsImportError("The export has no dated rows.")
    series.sort()
    last = series[-1][0]
    step = (series[1][0] - series[0][0]).days if len(series) > 1 else 1
    # A weekly row is dated by its first day; it covers six more.
    current_to = last + timedelta(days=step - 1)
    current_from = current_to - timedelta(days=PERIOD_DAYS - 1)
    baseline_to = current_from - timedelta(days=1)
    baseline_from = baseline_to - timedelta(days=PERIOD_DAYS - 1)
    if series[0][0] > baseline_from:
        raise TrendsImportError(
            f"The export starts {series[0][0]:%-d %b %Y}; it needs {2 * PERIOD_DAYS} days to "
            f"compare two {PERIOD_DAYS}-day periods. Export 'Past 90 days'."
        )

    readings = []
    for column, name in enumerate(header[1:]):
        found = _HEADER.match(name.strip())
        term = (found.group("term") if found else name).strip()
        geo = (found.group("geo") if found else "").strip()

        def average(lo, hi, column=column):
            values = [v[column] for d, v in series if lo <= d <= hi and column < len(v)]
            return sum(values) / len(values) if values else 0.0

        readings.append(Reading(
            term=term,
            geography=GEOGRAPHIES.get(geo.lower(), geo or "Worldwide"),
            current_from=current_from, current_to=current_to,
            baseline_from=baseline_from, baseline_to=baseline_to,
            current=average(current_from, current_to),
            baseline=average(baseline_from, baseline_to),
        ))
    return readings


@transaction.atomic
def save(readings: list[Reading], *, entered_by: str, batch: str = "",
         provider: str = PROVIDER, provenance: str = "") -> list[Observation]:
    """One google_trends observation per term, retrieved today."""
    today = timezone.localdate()
    batch = batch or f"trends-{today:%Y%m%d}"
    terms = [r.term for r in readings]
    explore = (
        "https://trends.google.com/trends/explore?date=today%203-m&geo=US&q="
        + ",".join(quote(t) for t in terms)
    )
    created = []
    for r in readings:
        created.append(Observation.objects.create(
            url=explore,
            title=f"Google Trends: {r.term}, {PERIOD_DAYS}-day comparison",
            source_type=Observation.SourceType.GOOGLE_TRENDS,
            published_at=r.current_to,
            observed_at=today,
            topic=r.term,
            metric_kind=Observation.MetricKind.TRENDS,
            metric_value=Decimal(f"{r.current:.2f}"),
            metric_label=r.label,
            query=r.term,
            provider=provider,
            geography=r.geography,
            period=r.period,
            limitations=LIMITATIONS.format(days=PERIOD_DAYS)
            + (f" Exported together with: {', '.join(t for t in terms if t != r.term)}."
               if len(terms) > 1 else ""),
            entered_by=entered_by,
            import_batch=batch,
            provenance=provenance or f"Google Trends export imported {today:%-d %b %Y}",
        ))
    return created

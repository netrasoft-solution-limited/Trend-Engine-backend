"""Entering observations — one validation path for the form and the CSV import.

A row is refused rather than half-saved: an observation without its URL or
date cannot be traced, and a search figure without its query, provider,
geography and period cannot be read. Every refusal says which row and why, so
a sheet can be fixed and re-imported.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.evidence.models import Observation

#: The CSV template's columns, in order. `url`, `title`, `source_type` and
#: `observed_at` are required.
COLUMNS = [
    "url", "title", "source_type", "published_at", "observed_at", "topic", "excerpt",
    "metric_kind", "metric_value", "metric_label", "query", "provider", "geography", "period",
    "is_ad", "is_brand_content", "is_old_material", "coverage_gap", "limitations",
    "event_key", "provenance",
]

EXAMPLE = {
    "url": "https://www.reddit.com/r/Supplements/comments/example",
    "title": "After years whey suddenly causing bloating",
    "source_type": "reddit",
    "published_at": "2026-10-06",
    "observed_at": "2026-10-20",
    "topic": "protein and digestion",
    "excerpt": "Shopper asks why whey shakes became hard to tolerate.",
    "metric_kind": "", "metric_value": "", "metric_label": "",
    "query": "", "provider": "", "geography": "", "period": "",
    "is_ad": "no", "is_brand_content": "no", "is_old_material": "no", "coverage_gap": "no",
    "limitations": "Individual anecdote; not a product efficacy claim.",
    "event_key": "", "provenance": "",
}

_TRUE = {"yes", "y", "true", "1", "x"}


class ObservationError(ValueError):
    """A row that cannot be saved, with the reason in words."""


def template_csv() -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerow(EXAMPLE)
    return out.getvalue()


def clean(raw: dict) -> dict:
    """One row or form submission → model fields. Raises ObservationError."""
    get = lambda key: str(raw.get(key) or "").strip()  # noqa: E731

    data: dict = {}
    for key in ("url", "title"):
        if not get(key):
            raise ObservationError(f"'{key}' is required.")
        data[key] = get(key)
    if not data["url"].startswith(("http://", "https://")):
        raise ObservationError("'url' must be a full web address (http or https).")

    source = get("source_type").lower()
    if source not in Observation.SourceType.values:
        raise ObservationError(
            f"'source_type' must be one of: {', '.join(Observation.SourceType.values)}."
        )
    data["source_type"] = source

    data["observed_at"] = _date(get("observed_at"), "observed_at", required=True)
    data["published_at"] = _date(get("published_at"), "published_at", required=False)

    kind = get("metric_kind").lower()
    if kind not in Observation.MetricKind.values:
        raise ObservationError(
            f"'metric_kind' must be blank or one of: "
            f"{', '.join(v for v in Observation.MetricKind.values if v)}."
        )
    data["metric_kind"] = kind
    value = get("metric_value").replace(",", "")
    if value:
        if not kind:
            raise ObservationError(
                "A 'metric_value' needs a 'metric_kind' saying what it measures."
            )
        try:
            data["metric_value"] = Decimal(value)
        except InvalidOperation as exc:
            raise ObservationError(f"'metric_value' {value!r} is not a number.") from exc
    else:
        data["metric_value"] = None

    for key in ("query", "provider", "geography", "period", "topic", "excerpt",
                "metric_label", "limitations", "event_key", "provenance"):
        data[key] = get(key)
    if kind in Observation.SEARCH_KINDS:
        missing = [k for k in ("query", "provider", "geography", "period") if not data[k]]
        if missing:
            raise ObservationError(
                f"A search figure needs its {', '.join(missing)} — without them it cannot be read."
            )

    for key in ("is_ad", "is_brand_content", "is_old_material", "coverage_gap"):
        data[key] = get(key).lower() in _TRUE
    return data


def _date(value: str, key: str, *, required: bool) -> date | None:
    if not value:
        if required:
            raise ObservationError(f"'{key}' is required (YYYY-MM-DD).")
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ObservationError(f"'{key}' {value!r} is not a date (YYYY-MM-DD).") from exc


def parse_csv(text: str) -> tuple[list[dict], list[str]]:
    """(clean rows, errors as 'Row n: reason'). Nothing is saved here."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "url" not in [f.strip() for f in reader.fieldnames]:
        return [], ["The file needs the template's header row (download the template)."]
    rows, errors = [], []
    for n, raw in enumerate(reader, start=2):
        if not any(str(v or "").strip() for v in raw.values()):
            continue
        try:
            rows.append(clean({(k or "").strip(): v for k, v in raw.items()}))
        except ObservationError as exc:
            errors.append(f"Row {n}: {exc}")
    return rows, errors


@transaction.atomic
def save_all(rows: list[dict], *, entered_by: str, batch: str) -> list[Observation]:
    return [
        Observation.objects.create(**row, entered_by=entered_by, import_batch=batch)
        for row in rows
    ]


def independent_count(observations) -> int:
    """How many independent events these are: one per event key."""
    return len({o.independence_key for o in observations})

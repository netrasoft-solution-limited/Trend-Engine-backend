"""What is broken right now, for the banner on every operator page.

Found the hard way: AI calls failed for two days in October (the API balance
ran out) and YouTube collection failed for five (a removed collector build),
and both were recorded but nobody saw them. A problem shows here until the
next successful run clears it, so the banner never needs dismissing.
"""
from __future__ import annotations

from apps.enrichment.models import ModelRun
from apps.ingestion.models import IngestionRun
from apps.sources.models import Source

#: Vendor errors an operator should read in plain words.
PLAIN = (
    ("credit balance is too low", "the Anthropic account is out of credit — top it up"),
    ("build-not-found", "the collector build no longer exists"),
    ("invalid x-api-key", "the API key was rejected"),
    ("401", "the provider rejected our credentials"),
)


def _plain(error: str) -> str:
    lowered = (error or "").lower()
    for needle, words in PLAIN:
        if needle.lower() in lowered:
            return words
    first = (error or "unknown error").strip().splitlines()[0]
    return first[:140]


def problems() -> list[dict]:
    found = []
    for source in Source.objects.all():
        runs = IngestionRun.objects.filter(source=source).order_by("-pk")
        latest = runs.first()
        if latest is None or latest.status != IngestionRun.Status.FAILED:
            continue
        last_ok = runs.filter(status=IngestionRun.Status.SUCCEEDED).first()
        failing = runs.filter(pk__gt=last_ok.pk) if last_ok else runs
        first_failure = failing.order_by("pk").first()
        found.append({
            "what": f"Collection from {source.name} is failing",
            "why": _plain(latest.error or ""),
            "since": first_failure.started_at,
            "count": failing.filter(status=IngestionRun.Status.FAILED).count(),
        })

    runs = ModelRun.objects.order_by("-at")
    latest = runs.first()
    if latest is not None and latest.outcome == ModelRun.Outcome.ERRORED:
        last_ok = runs.filter(outcome=ModelRun.Outcome.SUCCEEDED).first()
        failing = runs.filter(at__gt=last_ok.at) if last_ok else runs
        found.append({
            "what": "AI calls are failing",
            "why": _plain(latest.error or ""),
            "since": failing.order_by("at").first().at,
            "count": failing.filter(outcome=ModelRun.Outcome.ERRORED).count(),
        })
    return found

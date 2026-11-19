"""What is broken right now, for the banner on every operator page.

Found the hard way: AI calls failed for two days in October (the API balance
ran out) and YouTube collection failed for five (a removed collector build),
and both were recorded but nobody saw them. A problem shows here until the
next successful run clears it, so the banner never needs dismissing.
"""
from __future__ import annotations

from apps.enrichment import failures
from apps.enrichment.models import ModelRun
from apps.ingestion.models import IngestionRun
from apps.intelligence import health
from apps.sources.models import Source

#: Vendor errors an operator should read in plain words.
PLAIN = (
    ("credit balance is too low", "the Anthropic account is out of credit — top it up"),
    ("usage limits", "the Anthropic workspace has reached its usage limit — raise it in the "
     "Anthropic console (Settings → Limits)"),
    ("build-not-found", "the collector build no longer exists"),
    ("invalid x-api-key", "the API key was rejected"),
    ("cut off before it finished", "the AI answer was cut off part-way"),
    ("answer was cut off", "the AI answer was cut off part-way"),
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

    # A source that used to bring items and now brings none, while every run
    # "succeeds". 15–19 Nov: the Reddit actor returned 0 rows with an error in
    # its own log, and 18 runs said "succeeded".
    empty = _collecting_nothing()
    if empty:
        found.append({
            "what": f"{len(empty)} source(s) are collecting nothing",
            "why": "the last two runs found no items where earlier runs did: "
            + ", ".join(s.name for s, _ in empty[:4]) + ("…" if len(empty) > 4 else ""),
            "since": min(since for _, since in empty),
            "count": len(empty),
            "unit": "source",
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

    # Collection can run for days while detection fails, and the research
    # collected is never connected into signals (9–14 Nov).
    streak = health.failing_streak()
    if streak:
        found.append({
            "what": "Signal detection is failing",
            "why": _plain(health.last_error(streak)),
            "since": streak[-1].at,
            "count": len(streak),
            "unit": "run",
            # Appended to the Runs link: /runs/detection/.
            "anchor": "detection/",
        })

    stuck = list(failures.unanalysed()[:50])
    if stuck:
        found.append({
            "what": f"{len(stuck)} transcribed item(s) have not been analysed",
            "why": _plain(failures.last_error(stuck[0])),
            "since": min((i.transcript.created_at for i in stuck), default=None),
            "count": len(stuck),
            "unit": "item",
            "anchor": "#unanalysed",
        })
    return found


#: Consecutive empty, successful runs before a source counts as collecting nothing.
EMPTY_RUNS = 2


def _collecting_nothing() -> list[tuple]:
    """(source, since) for each active source whose latest runs succeeded with
    no items at all, though an earlier run collected some."""
    out = []
    for source in Source.objects.filter(status=Source.Status.ACTIVE):
        runs = list(IngestionRun.objects.filter(source=source).order_by("-pk")[:EMPTY_RUNS])
        if len(runs) < EMPTY_RUNS or any(
            r.status != IngestionRun.Status.SUCCEEDED or r.items_collected for r in runs
        ):
            continue
        last_full = IngestionRun.objects.filter(source=source, items_collected__gt=0).order_by(
            "-pk").first()
        if last_full is None:
            continue
        first_empty = (
            IngestionRun.objects.filter(source=source, pk__gt=last_full.pk).order_by("pk").first()
        )
        out.append((source, first_empty.started_at))
    return out

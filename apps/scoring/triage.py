"""The Triage home summary — the four counts and the top candidate.

A fixed number of aggregate queries (four, plus one to find the default window),
however many signals, runs or sources there are:

    1. Signal aggregate: new candidates, review ready, research alerts
    2. count of failed, blocking ingestion runs
    3. count of failed sources
    4. the top-ranked candidate's code

Definitions (agreed in stage 1):

  new_candidates     state = candidate AND first seen inside the window. The
                     default window is the latest digest's, and a digest's
                     window starts where the previous digest ended, so by
                     default this is "first seen since the previous digest".
  review_ready       awaiting_operator_judgment — the current backlog, NOT
                     limited to the window: a signal first seen three weeks ago
                     still needs a decision.
  research_alerts    needs_scientific_routing — current backlog, same reason.
  blocking_failures  failed AND blocking ingestion runs, plus failed sources.
                     Also current, not windowed. There is no "resolved" marker
                     on a run yet, so a failed blocking run counts until its
                     row changes.
  top_candidate_id   the best candidate first seen inside the window, in
                     `ranked_candidates` order, or None.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.ingestion.models import IngestionRun
from apps.intelligence.models import Digest, Signal
from apps.sources.models import Source

from .ranking import ranked_candidates

#: With no digest at all yet, the default window is the week ending today.
#: Digests are weekly (the mock's window is Sep 13 – Sep 20).
FALLBACK_WINDOW_DAYS = 7


@dataclass(frozen=True)
class Window:
    start: date
    end: date  # inclusive

    def bounds(self) -> tuple[datetime, datetime]:
        """[start 00:00, day after end 00:00) in the project timezone (UTC)."""
        tz = timezone.get_current_timezone()
        return (
            datetime.combine(self.start, time.min, tzinfo=tz),
            datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=tz),
        )


def default_window() -> Window:
    """The last collection window: the latest digest's."""
    latest = Digest.objects.order_by("-created_at").values("window_start", "window_end").first()
    if latest is not None:
        return Window(latest["window_start"], latest["window_end"])
    today = timezone.localdate()
    return Window(today - timedelta(days=FALLBACK_WINDOW_DAYS), today)


def triage_summary(window: Window, organization=None) -> dict:
    """The summary for `window`. `organization` picks whose fit and confidence
    rank the candidates; None ranks on domain score alone."""
    since, until = window.bounds()
    in_window = Q(first_seen_at__gte=since, first_seen_at__lt=until)

    signals = Signal.objects.aggregate(
        new_candidates=Count("pk", filter=Q(state=Signal.State.CANDIDATE) & in_window),
        review_ready=Count("pk", filter=Q(awaiting_operator_judgment=True)),
        research_alerts=Count("pk", filter=Q(needs_scientific_routing=True)),
    )
    blocking_runs = IngestionRun.objects.filter(
        status=IngestionRun.Status.FAILED, blocking=True
    ).count()
    failed_sources = Source.objects.filter(status=Source.Status.FAILED).count()
    top = ranked_candidates(organization).filter(in_window).values_list("code", flat=True).first()

    return {
        "window": {"from": window.start.isoformat(), "to": window.end.isoformat()},
        "new_candidates": signals["new_candidates"],
        "review_ready": signals["review_ready"],
        "research_alerts": signals["research_alerts"],
        "blocking_failures": blocking_runs + failed_sources,
        "top_candidate_id": top,
    }

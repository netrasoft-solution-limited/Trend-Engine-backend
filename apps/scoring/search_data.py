"""Search volumes and recent search interest, refreshed inside the cycle.

Mark, 20 Nov: "Bring fresh Google search volumes and recent search interest
into the workflow, with source dates and uncertainty. We should not have to
assemble this separately each month."

For each brand, the keywords are its candidates' names and the catalogue terms
its approved stories matched. Each refresh stores, per keyword:

  · a `search_estimate` observation: Google Ads monthly volume (the 12-month
    average and the latest month), with the month it covers and the retrieval
    date; Google Ads figures are rounded ranges, and the limitation says so;
  · a `google_trends` observation: average relative interest over the latest
    28 days against the 28 days before, both periods named.

Readings for a candidate's name are attached to it; stories find theirs by
query when a brief is built. Spend is recorded per call and held to the
provider's monthly cap.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.evidence.models import Observation
from apps.evidence.services import trends_import
from apps.operations.models import CostEvent
from apps.sources.models import AcquisitionProvider

from .models import Candidate, ClientSignalScore, Recommendation

logger = logging.getLogger(__name__)

PROVIDER_LABEL = "Google Ads keyword data via DataForSEO"
TRENDS_LABEL = "Google Trends via DataForSEO"
COST_PROVIDER = "dataforseo"
TERMS_PER_STORY = 2
MAX_KEYWORDS = 60
VOLUME_LIMITATIONS = (
    "Google Ads search volume: an estimate rounded into ranges by Google, for the United "
    "States, in English. The 12-month average smooths seasonality; the latest month is shown "
    "beside it. Searches are not buyers, and overlapping queries must not be added together."
)


def _provider() -> AcquisitionProvider | None:
    return AcquisitionProvider.objects.filter(kind=AcquisitionProvider.Kind.DATAFORSEO).first()


def available() -> bool:
    provider = _provider()
    return bool(
        provider and provider.paused_at is None
        and provider.credential("login") and provider.credential("password")
    )


def month_spend() -> Decimal:
    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = CostEvent.objects.filter(provider=COST_PROVIDER, at__gte=start).aggregate(
        t=Sum("estimated_usd"))["t"]
    return Decimal(total or 0)


def keywords_for(organization) -> list[str]:
    """Candidate names, then the catalogue terms approved stories matched."""
    words: list[str] = []
    for candidate in Candidate.objects.filter(organization=organization).exclude(
        status=Candidate.Status.REJECTED
    ):
        words.append(candidate.name.lower())
    approved = Recommendation.objects.filter(
        organization=organization, state=Recommendation.State.APPROVED
    ).values_list("signal_id", flat=True)
    scores = ClientSignalScore.objects.filter(organization=organization, signal_id__in=approved)
    for score in scores:
        terms = [m["term"].lower() for m in (score.matches or []) if m.get("facet") == "asset"]
        words.extend(list(dict.fromkeys(terms))[:TERMS_PER_STORY])
    return list(dict.fromkeys(w for w in words if 2 <= len(w) <= 80))[:MAX_KEYWORDS]


def refresh(organization, *, actor_label: str, client=None) -> dict:
    from apps.connectors.factory import dataforseo_for

    from . import opportunities

    provider = _provider()
    cap = Decimal(provider.monthly_cap_usd or 0) if provider else Decimal(0)
    if cap and month_spend() >= cap:
        return {"refused": f"DataForSEO's monthly cap (${cap}) is reached."}
    keywords = keywords_for(organization)
    if not keywords:
        return {"keywords": 0}
    api = client or dataforseo_for()
    today = timezone.localdate()

    saved: list[Observation] = []
    for volume in api.volumes(keywords):
        if volume.average is None:
            continue
        latest = volume.monthly[0] if volume.monthly else None
        label = f"{volume.average:,} a month (12-month average)"
        period = "12 months"
        if latest:
            label += f"; {latest[2]:,} in {_month(latest[0], latest[1])}"
            period = f"12 months to {_month(latest[0], latest[1])}"
        saved.append(Observation.objects.create(
            url="https://ads.google.com/aw/keywordplanner/",
            title=f"Google search volume: {volume.keyword}",
            source_type=Observation.SourceType.SEARCH_ESTIMATE,
            published_at=today, observed_at=today, topic=volume.keyword,
            metric_kind=Observation.MetricKind.SEARCH_ESTIMATE,
            metric_value=Decimal(volume.average), metric_label=label,
            query=volume.keyword, provider=PROVIDER_LABEL, geography="US", period=period,
            limitations=VOLUME_LIMITATIONS
            + (f" Advertiser competition: {volume.competition.lower()}." if volume.competition
               else ""),
            entered_by=actor_label, import_batch=f"dataforseo-{today:%Y%m%d}",
            provenance=f"DataForSEO search volume, retrieved {today:%-d %b %Y}",
        ))

    end = today - timedelta(days=1)
    start = end - timedelta(days=2 * trends_import.PERIOD_DAYS - 1)
    for first in range(0, len(keywords), 5):
        group = keywords[first:first + 5]
        try:
            series = api.trends(group, date_from=start, date_to=end)
        except Exception as exc:  # one failed group must not lose the rest
            logger.warning("Trends failed for %s: %s", group, exc)
            continue
        saved.extend(trends_import.save(
            _readings(series), entered_by=actor_label, batch=f"dataforseo-{today:%Y%m%d}",
            provider=TRENDS_LABEL,
            provenance=f"DataForSEO Google Trends, retrieved {today:%-d %b %Y}",
        ))

    if api.cost_usd:
        CostEvent.objects.create(provider=COST_PROVIDER, source_id="search-refresh",
                                 run_id="", units={"keywords": len(keywords)},
                                 estimated_usd=api.cost_usd)

    attached = 0
    for candidate in Candidate.objects.filter(organization=organization).exclude(
        status=Candidate.Status.REJECTED
    ):
        names = {n.lower() for n in candidate.names}
        mine = [o.pk for o in saved if o.query.lower() in names]
        if mine:
            done, _ = opportunities.attach(candidate, mine, actor_label=actor_label)
            attached += len(done)
    return {"keywords": len(keywords), "readings": len(saved), "attached": attached,
            "cost_usd": str(api.cost_usd)}


def _readings(series) -> list:
    """Average each keyword over the latest 28 days and the 28 before."""
    points = sorted(series.points, key=lambda p: p[0])
    if not points:
        return []
    last = points[-1][1]
    current_from = last - timedelta(days=trends_import.PERIOD_DAYS - 1)
    baseline_to = current_from - timedelta(days=1)
    baseline_from = baseline_to - timedelta(days=trends_import.PERIOD_DAYS - 1)
    readings = []
    for column, keyword in enumerate(series.keywords):
        def average(lo, hi, column=column):
            values = [p[2][column] for p in points
                      if lo <= p[0] <= hi and not p[3] and column < len(p[2])]
            return sum(values) / len(values) if values else 0.0

        readings.append(trends_import.Reading(
            term=keyword, geography="US",
            current_from=current_from, current_to=last,
            baseline_from=baseline_from, baseline_to=baseline_to,
            current=average(current_from, last), baseline=average(baseline_from, baseline_to),
        ))
    return readings


def describe(result: dict) -> str:
    if result.get("refused"):
        return result["refused"]
    if not result.get("keywords"):
        return "No candidate or approved story has a search term yet."
    return (f"{result['readings']} search reading(s) for {result['keywords']} keyword(s), "
            f"{result['attached']} attached to candidates; cost ${result['cost_usd']}.")


def story_readings(signal, organization) -> list[Observation]:
    """The newest volume and Trends reading for each term a story matched."""
    score = ClientSignalScore.objects.filter(organization=organization, signal=signal).first()
    terms = list(dict.fromkeys(
        m["term"].lower() for m in (score.matches if score else []) if m.get("facet") == "asset"
    ))[:TERMS_PER_STORY]
    out = []
    for term in terms:
        for kind in (Observation.MetricKind.SEARCH_ESTIMATE, Observation.MetricKind.TRENDS):
            found = Observation.objects.filter(query__iexact=term, metric_kind=kind).order_by(
                "-observed_at", "-pk").first()
            if found:
                out.append(found)
    return out


def _month(year: int, month: int) -> str:
    from datetime import date

    return f"{date(year, month, 1):%b %Y}"

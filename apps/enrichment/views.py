"""Ingestion runs and the evidence they produced — server-rendered screens.

PRD §6.5's Ingestion Runs screen, and the item page behind it. Mark's
8 October review: "collection timestamps alone do not show transcript coverage,
failures, or the freshness of the material being used." These pages are the
answer — every run, what it found, what the relevance gate decided and why, how
each item was transcribed and what that cost, and the claims that came out.

WHY THIS LIVES IN `apps.enrichment` AND NOT `apps.ingestion`. A run page that
cannot show the claims a run produced cannot answer the question it exists for.
`ingestion` sits BELOW `evidence` in the layer contract and `evidence` sits
below `enrichment`, so `enrichment` is the lowest app that may read runs,
items, transcripts and claims all at once. Placing the screen here is the
contract being right: a page about what was understood belongs with the
understanding.

Read-only, and open to any signed-in operator rather than Platform Admin alone.
Reviewing evidence is the Operator role's job (PRD §3.2); nothing here changes
anything. Everything is global evidence — no tenant is bound or needed.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

from apps.evidence.models import ContentItem
from apps.ingestion.models import IngestionRun
from apps.sources.models import Source

from .models import Claim, ModelRun

#: Runs per page. A source polled every hour fills a day with 24; this is
#: enough to see a pattern without paginating.
RUN_LIMIT = 60

#: The window the coverage summary describes. Long enough to include a weekly
#: source, short enough to say something about now rather than ever.
COVERAGE_DAYS = 30

ZERO = Value(Decimal("0"), output_field=DecimalField(max_digits=12, decimal_places=6))
RELEVANT = (ContentItem.GateDecision.RELEVANT, ContentItem.GateDecision.SAMPLED)


def _model_cost_by_item():
    """What the model calls on one item cost — gate plus extraction.

    A subquery rather than a join: joining claims and model runs in one query
    multiplies each sum by the other relation's row count, which reads as a
    plausible number and is wrong.
    """
    return Coalesce(
        Subquery(
            ModelRun.objects.filter(content_item=OuterRef("pk"))
            .values("content_item")
            .annotate(total=Sum("cost_usd"))
            .values("total")[:1]
        ),
        ZERO,
    )


def _claim_count_by_item():
    return Coalesce(
        Subquery(
            Claim.objects.filter(content_item=OuterRef("pk"))
            .values("content_item")
            .annotate(n=Count("pk"))
            .values("n")[:1]
        ),
        0,
    )


def _items():
    return ContentItem.objects.select_related("source", "transcript").annotate(
        model_cost=_model_cost_by_item(),
        claim_count=_claim_count_by_item(),
    )


def _item_cost(item) -> Decimal:
    """Transcript plus model calls — everything this item has cost so far."""
    transcript_cost = item.transcript.cost_usd if _has_transcript(item) else Decimal("0")
    return transcript_cost + (item.model_cost or Decimal("0"))


def _has_transcript(item) -> bool:
    try:
        return item.transcript is not None
    except ObjectDoesNotExist:
        return False


def coverage_since(since) -> dict:
    """Of what the gate judged relevant, how much has a usable transcript.

    The number PRD §14 asks to be visible: an engine that collects plenty and
    transcribes little is reading titles, not evidence.
    """
    window = ContentItem.objects.filter(discovered_at__gte=since)
    counts = window.aggregate(
        collected=Count("pk"),
        pending=Count("pk", filter=Q(gate_decision=ContentItem.GateDecision.PENDING)),
        rejected=Count("pk", filter=Q(gate_decision=ContentItem.GateDecision.REJECTED)),
        relevant=Count("pk", filter=Q(gate_decision__in=RELEVANT)),
        transcribed=Count(
            "pk",
            filter=Q(
                gate_decision__in=RELEVANT,
                content_state=ContentItem.ContentState.TRANSCRIBED,
            ),
        ),
        metadata_only=Count(
            "pk",
            filter=Q(
                gate_decision__in=RELEVANT,
                content_state=ContentItem.ContentState.METADATA,
            ),
        ),
    )
    counts["awaiting"] = counts["relevant"] - counts["transcribed"] - counts["metadata_only"]
    counts["coverage_pct"] = (
        round(100 * counts["transcribed"] / counts["relevant"]) if counts["relevant"] else None
    )
    counts["claims"] = Claim.objects.filter(content_item__discovered_at__gte=since).count()
    return counts


class RunListView(View):
    """Every recent run, newest first, and transcript coverage for the month."""

    def get(self, request):
        runs = IngestionRun.objects.select_related("source")
        source_id = request.GET.get("source") or ""
        source = None
        if source_id.isdigit():
            source = Source.objects.filter(pk=int(source_id)).first()
            if source is not None:
                runs = runs.filter(source=source)
        runs = list(runs[:RUN_LIMIT])

        per_run = {
            row["run"]: row
            for row in ContentItem.objects.filter(run__in=runs)
            .values("run")
            .annotate(
                relevant=Count("pk", filter=Q(gate_decision__in=RELEVANT)),
                rejected=Count("pk", filter=Q(gate_decision=ContentItem.GateDecision.REJECTED)),
                transcribed=Count(
                    "pk", filter=Q(content_state=ContentItem.ContentState.TRANSCRIBED)
                ),
                transcript_cost=Coalesce(Sum("transcript__cost_usd"), ZERO),
            )
        }
        model_cost = {
            row["content_item__run"]: row["total"]
            for row in ModelRun.objects.filter(content_item__run__in=runs)
            .values("content_item__run")
            .annotate(total=Sum("cost_usd"))
        }

        rows = []
        for run in runs:
            stats = per_run.get(run.pk, {})
            rows.append(
                {
                    "obj": run,
                    "relevant": stats.get("relevant", 0),
                    "rejected": stats.get("rejected", 0),
                    "transcribed": stats.get("transcribed", 0),
                    "cost": stats.get("transcript_cost", Decimal("0"))
                    + (model_cost.get(run.pk) or Decimal("0")),
                }
            )

        since = timezone.now() - timedelta(days=COVERAGE_DAYS)
        return render(
            request,
            "ops/evidence/runs.html",
            {
                "runs": rows,
                "source": source,
                "sources": Source.objects.order_by("name"),
                "coverage": coverage_since(since),
                "coverage_days": COVERAGE_DAYS,
            },
        )


class RunDetailView(View):
    """One run and every item it created."""

    def get(self, request, pk: int):
        run = get_object_or_404(IngestionRun.objects.select_related("source"), pk=pk)
        items = list(_items().filter(run=run).order_by("-published_at", "-discovered_at"))
        for item in items:
            item.total_cost = _item_cost(item)
            item.latest_attempt = (item.transcript_attempts or [None])[-1]

        return render(
            request,
            "ops/evidence/run.html",
            {
                "run": run,
                "items": items,
                "total_cost": sum((i.total_cost for i in items), Decimal("0")),
            },
        )


class ItemDetailView(View):
    """One collected item: where it came from, what was decided, what it said."""

    def get(self, request, pk: int):
        item = get_object_or_404(_items().select_related("run", "policy"), pk=pk)
        transcript = item.transcript if _has_transcript(item) else None
        claims = list(
            Claim.objects.filter(content_item=item)
            .select_related("segment")
            .order_by("segment__ordinal", "pk")
        )
        for claim in claims:
            seconds = claim.segment.start_seconds if claim.segment_id else None
            claim.at = _timestamp(seconds)
            claim.link = item.link_at(seconds)

        return render(
            request,
            "ops/evidence/item.html",
            {
                "item": item,
                "transcript": transcript,
                "attempts": list(reversed(item.transcript_attempts or [])),
                "model_runs": list(ModelRun.objects.filter(content_item=item).order_by("at")),
                "claims": claims,
                "total_cost": _item_cost(item),
            },
        )


def _timestamp(seconds: float | None) -> str:
    if seconds is None:
        return ""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

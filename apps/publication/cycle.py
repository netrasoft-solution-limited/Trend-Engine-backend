"""A research cycle: fresh collection through to draft briefs, for one brand.

Mark, 20 Nov: "a working delivery path", not components. Until now each step
was its own button or command, run by hand in the right order. A cycle runs
them in order and records every stage, so the status page answers "what
happened, what failed, what is waiting, and what did it cost":

    collect    poll every source linked to the brand (BrandSource)
    process    relevance check → transcript → extraction for what was collected
    detect     connect the new claims into signals, then rank them for the brand
    recommend  draft recommendations for the best-ranked signals without one
    discover   propose ingredients the catalogue lacks; refresh candidate evidence
    search     search volumes and interest (waits on the search-data provider)
    review     MANUAL: an operator approves stories, recommendations, candidates
    briefs     draft both briefs from what is approved

Each stage is a function that returns when it is finished, or raises `Wait`
when it has to come back later (items still being transcribed). The task in
`tasks.py` calls `advance` repeatedly with a countdown, so no worker sits
waiting. A stage that fails records why and the cycle stops there, visibly.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from apps.clients.models import BrandSource
from apps.enrichment.models import Claim
from apps.evidence.models import ContentItem
from apps.ingestion.models import IngestionRun
from apps.operations.models import CostEvent
from apps.sources.models import Source

from .models import ResearchCycle

logger = logging.getLogger(__name__)

#: How long `process` waits for collected items to settle before moving on.
PROCESS_LIMIT = timedelta(minutes=120)
#: An item untouched this long is dispatched again.
STUCK_AFTER = timedelta(minutes=15)
#: Recommendations drafted per cycle (one model call each).
RECOMMEND_LIMIT = 5
RELEVANT = (ContentItem.GateDecision.RELEVANT, ContentItem.GateDecision.SAMPLED)


class Wait(Exception):
    """The stage is not finished; come back after `seconds`."""

    def __init__(self, detail: str, seconds: int = 60) -> None:
        super().__init__(detail)
        self.detail = detail
        self.seconds = seconds


class StageFailed(Exception):
    """The stage cannot complete; the cycle stops and says why."""


# ── Starting and advancing ──────────────────────────────────────────────────


def start(organization, *, actor_label: str) -> ResearchCycle:
    running = ResearchCycle.objects.filter(
        organization=organization, state=ResearchCycle.State.RUNNING
    ).first()
    if running:
        raise StageFailed(f"A cycle is already running for {organization.name} (#{running.pk}).")
    if not BrandSource.objects.filter(organization=organization).exists():
        raise StageFailed(
            f"{organization.name} has no sources linked to it. Link sources on its "
            f"research page before starting a cycle."
        )
    return ResearchCycle.objects.create(
        organization=organization,
        started_by_label=actor_label,
        stages={name: {"state": "waiting"} for name in ResearchCycle.STAGES},
    )


def advance(cycle: ResearchCycle) -> int | None:
    """Run the current stage once. Returns seconds until the next call, or
    None when there is nothing more to do automatically."""
    if cycle.state != ResearchCycle.State.RUNNING:
        return None
    name = cycle.stage
    record = cycle.stages.setdefault(name, {})
    if record.get("state") in (None, "waiting"):
        record.update(state="running", started=timezone.now().isoformat())
    try:
        result = STAGE_FUNCTIONS[name](cycle, record)
    except Wait as wait:
        record["detail"] = wait.detail
        cycle.save(update_fields=["stages"])
        return wait.seconds
    except StageFailed as exc:
        _finish_stage(record, "failed", str(exc))
        cycle.state = ResearchCycle.State.FAILED
        cycle.finished_at = timezone.now()
        cycle.save(update_fields=["stages", "state", "finished_at"])
        return None
    except Exception as exc:  # recorded, never silent
        logger.exception("Cycle %s failed in %s", cycle.pk, name)
        _finish_stage(record, "failed", f"{type(exc).__name__}: {exc}"[:500])
        cycle.state = ResearchCycle.State.FAILED
        cycle.finished_at = timezone.now()
        cycle.save(update_fields=["stages", "state", "finished_at"])
        return None

    _finish_stage(record, result.get("state", "done"), result.get("detail", ""),
                  result.get("counts"))
    if name == "review":
        # The manual stage: the cycle waits for a person.
        cycle.state = ResearchCycle.State.REVIEW
        cycle.save(update_fields=["stages", "state"])
        return None
    following = ResearchCycle.STAGES.index(name) + 1
    cycle.stage = ResearchCycle.STAGES[following]
    cycle.save(update_fields=["stages", "stage"])
    return 1


def _finish_stage(record: dict, state: str, detail: str, counts: dict | None = None) -> None:
    record.update(state=state, finished=timezone.now().isoformat(), detail=detail)
    if counts is not None:
        record["counts"] = counts


# ── Stages ──────────────────────────────────────────────────────────────────


def _sources(cycle) -> list[Source]:
    return list(
        Source.objects.filter(
            brand_links__organization=cycle.organization, status=Source.Status.ACTIVE
        ).order_by("route", "name")
    )


def collect(cycle: ResearchCycle, record: dict) -> dict:
    """Poll every linked source, then wait for every poll to finish."""
    from celery import current_app

    if not cycle.run_ids and not record.get("dispatched"):
        sources = _sources(cycle)
        if not sources:
            raise StageFailed("No active source is linked to this brand.")
        before = IngestionRun.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
        for source in sources:
            current_app.send_task(
                "apps.evidence.tasks.poll_source", args=[source.pk], queue="ingest"
            )
        record.update(dispatched=[s.pk for s in sources], first_run_after=before,
                      dispatched_at=timezone.now().isoformat())
        raise Wait(f"Collecting from {len(sources)} source(s).", 30)

    expected = set(record["dispatched"])
    runs = IngestionRun.objects.filter(pk__gt=record["first_run_after"], source_id__in=expected)
    finished = runs.exclude(finished_at__isnull=True)
    if {r.source_id for r in finished} < expected:
        waited = timezone.now() - _when(record["dispatched_at"])
        if waited < timedelta(minutes=30):
            raise Wait(f"{finished.count()} of {len(expected)} source(s) finished.", 30)
    cycle.run_ids = list(finished.values_list("pk", flat=True))
    cycle.save(update_fields=["run_ids"])
    return {"detail": _collection_summary(finished, expected),
            "counts": _collection_counts(finished)}


def _collection_counts(runs) -> dict:
    counts: dict[str, dict] = {}
    for run in runs.select_related("source"):
        platform = _platform(run.source)
        row = counts.setdefault(platform, {"runs": 0, "collected": 0, "new": 0,
                                           "failed": 0, "empty": 0})
        row["runs"] += 1
        row["collected"] += run.items_collected or 0
        row["new"] += run.items_created or 0
        if run.status == IngestionRun.Status.FAILED:
            row["failed"] += 1
        elif not run.items_collected:
            row["empty"] += 1
    return counts


def _collection_summary(runs, expected) -> str:
    counts = _collection_counts(runs)
    parts = [f"{p}: {c['new']} new" for p, c in sorted(counts.items())]
    missing = len(expected) - runs.count()
    problems = sum(c["failed"] for c in counts.values())
    empty = sum(c["empty"] for c in counts.values())
    text = "; ".join(parts) or "nothing collected"
    if problems:
        text += f". {problems} source(s) failed"
    if empty:
        text += f". {empty} source(s) returned nothing"
    if missing > 0:
        text += f". {missing} source(s) did not finish in time"
    return text + "."


def _platform(source: Source) -> str:
    if source.route == Source.Route.SOCIAL:
        return (source.config or {}).get("platform") or "social"
    return source.route


def _cycle_items(cycle):
    return ContentItem.objects.filter(run_id__in=cycle.run_ids)


def process(cycle: ResearchCycle, record: dict) -> dict:
    """Wait for the relevance check, transcripts and extraction to settle."""
    from apps.enrichment.models import ModelRun
    from apps.enrichment.tasks import acquire_transcript, assess_relevance, extract_evidence

    items = _cycle_items(cycle)
    now = timezone.now()
    pending = items.filter(gate_decision=ContentItem.GateDecision.PENDING)
    relevant = items.filter(gate_decision__in=RELEVANT).exclude(duplicate_status="confirmed")
    untranscribed = relevant.exclude(content_state=ContentItem.ContentState.TRANSCRIBED).filter(
        Q(transcript_attempts=[]) | Q(transcript_attempts__isnull=True)
    )
    unextracted = [
        i for i in relevant.filter(content_state=ContentItem.ContentState.TRANSCRIBED)
        if not i.claims.exists() and not i.questions.exists()
        and not ModelRun.objects.filter(content_item=i, purpose="extraction").exists()
    ]
    waiting = pending.count() + untranscribed.count() + len(unextracted)
    counts = {
        "collected": items.count(),
        "relevant": relevant.count(),
        "transcribed": relevant.filter(content_state=ContentItem.ContentState.TRANSCRIBED).count(),
        "claims": Claim.objects.filter(content_item__in=items).count(),
        "waiting": waiting,
    }
    record["counts"] = counts
    if waiting == 0:
        return {"detail": _process_summary(counts), "counts": counts}

    started = _when(record["started"])
    if now - started > PROCESS_LIMIT:
        return {"state": "done", "counts": counts,
                "detail": _process_summary(counts) + f" {waiting} item(s) were still waiting "
                f"after {int(PROCESS_LIMIT.total_seconds() // 60)} minutes and are left for "
                f"the next cycle."}

    # Re-dispatch anything that has sat untouched (a lost task, a restart).
    last = _when(record.get("nudged") or record["started"])
    if now - last > STUCK_AFTER:
        for item in pending:
            assess_relevance.delay(item.pk)
        for item in untranscribed:
            acquire_transcript.delay(item.pk)
        for item in unextracted:
            extract_evidence.delay(item.pk)
        record["nudged"] = now.isoformat()
    raise Wait(f"{waiting} item(s) still being checked, transcribed or analysed.", 60)


def _process_summary(c: dict) -> str:
    return (f"{c['collected']} item(s) collected; {c['relevant']} relevant; "
            f"{c['transcribed']} transcribed; {c['claims']} claim(s) extracted.")


def detect(cycle: ResearchCycle, record: dict) -> dict:
    from apps.intelligence import detection, health
    from apps.scoring import signal_rank
    from apps.tenancy.context import scoped

    new_claims = Claim.objects.filter(content_item__in=_cycle_items(cycle)).count()
    batches = max(detection.DAILY_BATCHES, math.ceil(new_claims / detection.MAX_CLAIMS) + 1)
    result = detection.detect(batches=batches)
    health.alert_if_failing()
    if result.failed:
        raise StageFailed(f"Detection failed: {result.errors[-1] if result.errors else 'unknown'}")
    with scoped(cycle.organization):
        ranked = signal_rank.rank_for(cycle.organization)
    counts = {"claims_read": result.claims_considered, "new": len(result.created),
              "extended": len(result.extended), "ranked": ranked.get("ranked", 0)}
    return {"counts": counts,
            "detail": f"Read {result.claims_considered} claim(s): {len(result.created)} new "
                      f"signal(s), {len(result.extended)} extended; signals ranked for the brand."}


def recommend(cycle: ResearchCycle, record: dict) -> dict:
    from apps.scoring.tasks import recommend_top
    from apps.tenancy.context import scoped

    with scoped(cycle.organization):
        result = recommend_top(cycle.organization, limit=RECOMMEND_LIMIT)
    counts = {k: len(v) for k, v in result.items()}
    detail = (f"{counts['drafted']} recommendation(s) drafted for the best-ranked signals"
              + (f"; {counts['declined']} declined by the model as not actionable"
                 if counts["declined"] else "")
              + (f"; {counts['failed']} could not be drafted" if counts["failed"] else "") + ".")
    return {"counts": {**counts, "codes": result}, "detail": detail}


def discover(cycle: ResearchCycle, record: dict) -> dict:
    from apps.scoring import discovery, opportunities
    from apps.scoring.models import Candidate
    from apps.tenancy.context import scoped

    with scoped(cycle.organization):
        found = discovery.discover(cycle.organization, actor_label=f"research cycle {cycle.pk}")
        refreshed = {}
        for candidate in Candidate.objects.filter(organization=cycle.organization).exclude(
            status=Candidate.Status.REJECTED
        ):
            new = opportunities.collect(candidate, actor_label=f"research cycle {cycle.pk}")
            if new:
                refreshed[candidate.name] = len(new)
    return {
        "counts": {"proposed": len(found), "refreshed": refreshed},
        "detail": (
            f"{len(found)} ingredient(s) the catalogue lacks proposed; new current evidence "
            "attached to "
            + (", ".join(f"{k} (+{v})" for k, v in refreshed.items()) or "no candidate")
            + "."
        ),
    }


def search(cycle: ResearchCycle, record: dict) -> dict:
    from apps.scoring import search_data
    from apps.tenancy.context import scoped

    if not search_data.available():
        return {"state": "skipped",
                "detail": "No search-data provider is configured yet; search figures stay as "
                          "last imported, with their dates."}
    with scoped(cycle.organization):
        result = search_data.refresh(cycle.organization, actor_label=f"research cycle {cycle.pk}")
    return {"counts": result, "detail": search_data.describe(result)}


def review(cycle: ResearchCycle, record: dict) -> dict:
    """List every decision waiting for a person. The cycle stops here."""
    from apps.intelligence.models import Signal
    from apps.scoring.models import Candidate, Recommendation
    from apps.tenancy.context import scoped

    with scoped(cycle.organization):
        recs = list(
            Recommendation.objects.filter(
                organization=cycle.organization,
                state__in=(Recommendation.State.PROPOSED, Recommendation.State.EDITED),
            ).select_related("signal").order_by("-pk")[:20]
        )
        candidates = list(
            Candidate.objects.filter(organization=cycle.organization, approved_at__isnull=True)
            .exclude(status=Candidate.Status.REJECTED)
        )
        approved = Recommendation.objects.filter(
            organization=cycle.organization, state=Recommendation.State.APPROVED,
            signal__state=Signal.State.APPROVED,
        ).count()
    counts = {
        "recommendations": [{"code": r.signal.code, "id": r.pk, "title": r.signal.title,
                             "signal_state": r.signal.state} for r in recs],
        "candidates": [{"id": c.pk, "name": c.name} for c in candidates],
        "approved_stories": approved,
    }
    return {"counts": counts,
            "detail": f"{len(recs)} recommendation(s) and {len(candidates)} candidate(s) are "
                      f"waiting for a decision. Approve or reject them, then draft the briefs."}


def briefs(cycle: ResearchCycle, *, actor_label: str) -> list:
    """Draft both briefs from what is approved. Called from the status page."""
    from apps.enrichment.domain import DOMAIN_PACK_VERSION
    from apps.outputs import services
    from apps.outputs.models import OutputType
    from apps.tenancy.context import scoped

    from .generate import generate

    record = cycle.stages.setdefault("briefs", {})
    record.update(state="running", started=timezone.now().isoformat())
    made, problems = [], []
    with scoped(cycle.organization):
        for type_ in (OutputType.TREND_BRIEF, OutputType.PRODUCT_MEMO):
            try:
                built = generate(cycle.organization, type_)
                version = services.draft(
                    cycle.organization, type=type_, title=built.title, sections=built.sections,
                    summary=built.summary, actor_label=actor_label,
                    client_profile_version=built.profile, domain_pack_version=DOMAIN_PACK_VERSION,
                )
                made.append(version.pk)
            except services.DraftingError as exc:
                problems.append(f"{dict(OutputType.choices)[type_]}: {exc}")
    cycle.output_version_ids = made
    _finish_stage(record, "failed" if problems and not made else "done",
                  ("; ".join(problems) if problems else
                   "Both briefs drafted. Open them to read, export and send for review."))
    if made:
        cycle.state = ResearchCycle.State.DONE
        cycle.finished_at = timezone.now()
    cycle.stage = "briefs"
    cycle.save()
    return made


STAGE_FUNCTIONS = {
    "collect": collect,
    "process": process,
    "detect": detect,
    "recommend": recommend,
    "discover": discover,
    "search": search,
    "review": review,
}


# ── Reporting ───────────────────────────────────────────────────────────────


def cost(cycle: ResearchCycle) -> dict:
    """What this cycle spent: AI calls in its time span, and Virlo for its items."""
    from apps.evidence.models import TranscriptArtifact

    end = cycle.finished_at or timezone.now()
    ai = CostEvent.objects.filter(
        provider="llm", at__gte=cycle.started_at, at__lte=end
    ).exclude(source_id="provider-reconciliation").aggregate(t=Sum("estimated_usd"))["t"]
    runs = IngestionRun.objects.filter(pk__in=cycle.run_ids).aggregate(t=Sum("cost_usd"))["t"]
    transcripts = TranscriptArtifact.objects.filter(
        content_item__run_id__in=cycle.run_ids
    ).aggregate(t=Sum("cost_usd"))["t"]
    return {"ai": Decimal(ai or 0), "collection": Decimal(runs or 0) + Decimal(transcripts or 0)}


def _when(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)

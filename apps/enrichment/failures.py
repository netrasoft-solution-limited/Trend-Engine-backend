"""Transcribed items the model has not managed to read.

Found on 30 Oct: The Proof's episode on probiotic foods (item 77) was
transcribed, failed extraction twice, and showed nowhere — the failed calls
were rolled back with the extraction. An item is listed here until it has
claims or questions, so a failure stays in front of an operator until a retry
works.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.evidence.models import ContentItem

from .models import Claim, ModelRun, Question

#: A transcript this old with no extraction run at all was dropped somewhere —
#: the task is normally dispatched the moment the transcript lands.
STALLED_AFTER = timedelta(hours=1)

RELEVANT = (ContentItem.GateDecision.RELEVANT, ContentItem.GateDecision.SAMPLED)


def unanalysed():
    """Relevant, transcribed, not a duplicate, nothing extracted, and either a
    failed extraction call or none at all for over an hour."""
    runs = ModelRun.objects.filter(content_item=OuterRef("pk"), purpose="extraction")
    return (
        ContentItem.objects.filter(
            gate_decision__in=RELEVANT,
            content_state=ContentItem.ContentState.TRANSCRIBED,
            transcript__isnull=False,
        )
        .exclude(duplicate_status="confirmed")
        .exclude(Exists(Claim.objects.filter(content_item=OuterRef("pk"))))
        .exclude(Exists(Question.objects.filter(content_item=OuterRef("pk"))))
        .filter(
            Q(Exists(runs.exclude(outcome=ModelRun.Outcome.SUCCEEDED)))
            | (~Exists(runs) & Q(transcript__created_at__lt=timezone.now() - STALLED_AFTER))
        )
        .select_related("source")
        .order_by("-published_at")
    )


def last_error(item) -> str:
    run = (
        ModelRun.objects.filter(content_item=item, purpose="extraction")
        .exclude(outcome=ModelRun.Outcome.SUCCEEDED)
        .order_by("-at")
        .first()
    )
    return run.error if run else "never analysed — the analysis step did not run"

"""Whether detection is working, so a failure is seen the day it starts.

Found the hard way: detection failed every day from 9 to 14 Nov while
collection kept running, and nothing on screen said so — each failure was a
ModelRun row that the next successful extraction buried. A week of research was
collected and never connected into signals.

Every run of `detection.detect` leaves one audit event saying whether it
worked. The operator banner reads the current streak of failures; the daily
task emails the review address once when the streak reaches `ALERT_AFTER`.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

from apps.operations.models import AuditEvent

logger = logging.getLogger(__name__)

ACTOR = "signal-detection"

#: Consecutive failed runs before the review address is emailed. Two, so one
#: bad morning is a banner and a second one is a message.
ALERT_AFTER = 2


def record(result) -> AuditEvent:
    """One event per run, worked or not."""
    if result.failed:
        message = f"Signal detection failed: {result.errors[-1]}"
    else:
        message = (
            f"Signal detection read {result.claims_considered} claim(s): "
            f"{len(result.created)} new, {len(result.extended)} extended."
        )
    return AuditEvent.objects.create(
        kind=AuditEvent.Kind.SYSTEM,
        actor_realm=AuditEvent.Realm.SYSTEM,
        actor_label=ACTOR,
        message=message[:2000],
        context={
            "failed": result.failed,
            "failed_batches": result.failed_batches,
            "errors": [e[:500] for e in result.errors[-3:]],
            "claims": result.claims_considered,
            "created": result.created,
            "extended": result.extended,
        },
    )


def record_crash(exc: Exception) -> AuditEvent:
    """A run that stopped on an unexpected error, recorded like any failed run."""
    error = f"{type(exc).__name__}: {exc}"[:500]
    return AuditEvent.objects.create(
        kind=AuditEvent.Kind.SYSTEM,
        actor_realm=AuditEvent.Realm.SYSTEM,
        actor_label=ACTOR,
        message=f"Signal detection failed: {error}"[:2000],
        context={"failed": True, "failed_batches": 0, "errors": [error], "claims": 0,
                 "created": [], "extended": []},
    )


def failing_streak() -> list[AuditEvent]:
    """The failed runs since the last one that worked, newest first."""
    streak = []
    for event in AuditEvent.objects.filter(actor_label=ACTOR).order_by("-at", "-pk")[:60]:
        if not (event.context or {}).get("failed"):
            break
        streak.append(event)
    return streak


def last_error(streak: list[AuditEvent]) -> str:
    if not streak:
        return ""
    errors = (streak[0].context or {}).get("errors") or []
    return errors[-1] if errors else ""


def alert_if_failing() -> bool:
    """Email the review address once, when the streak reaches `ALERT_AFTER`.

    Exactly at the threshold, so a long outage is one message, not one a day;
    the banner carries it from there.
    """
    streak = failing_streak()
    recipients = list(getattr(settings, "OUTPUT_REVIEW_RECIPIENTS", []) or [])
    if len(streak) != ALERT_AFTER or not recipients:
        return False
    body = (
        f"Signal detection has failed on {len(streak)} consecutive runs, "
        f"since {streak[-1].at:%d %b %Y, %H:%M} UTC.\n\n"
        f"Latest error: {last_error(streak) or 'not recorded'}\n\n"
        "Collection is unaffected, but new research is not being connected into "
        "signals until this is fixed. The banner on every operator page shows it "
        "until a run succeeds.\n"
    )
    try:
        send_mail(
            subject="[Trend Engine] Signal detection is failing",
            message=body,
            from_email=None,
            recipient_list=recipients,
        )
    except Exception:
        logger.exception("Could not send the detection alert")
        return False
    return True

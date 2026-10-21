"""An operator's decisions about a signal — PRD §6.5's signal state machine.

    candidate → in review → approved / rejected / watching

Each move is one function call that checks the move is allowed, records who
made it and why, and audits it. The screen offers only the moves this module
allows, so "Approve" cannot appear on a signal that cannot be approved — the
gap the 20 September frontend review found in the prototype.

A signal is global; a decision about it is not a client's business. So these
live at L4 with the signal, and the client-specific half of review — the
recommendation — stays in `apps.scoring`.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.operations.models import AuditEvent

from .models import Signal, SignalEvidence

S = Signal.State

#: Where each state may go next. Rejected may be reopened, because a wrong
#: rejection should be fixable without a database shell; approved may go back
#: to review, because new contradicting evidence is a reason to look again.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    S.CANDIDATE: (S.IN_REVIEW, S.APPROVED, S.WATCHING, S.REJECTED),
    S.IN_REVIEW: (S.APPROVED, S.WATCHING, S.REJECTED),
    S.WATCHING: (S.IN_REVIEW, S.APPROVED, S.REJECTED),
    S.APPROVED: (S.IN_REVIEW,),
    S.REJECTED: (S.CANDIDATE,),
}

#: Moves that need a written reason. A rejection without one teaches nobody
#: anything; reopening an approved signal changes what a brief may contain.
NEEDS_REASON = {S.REJECTED, S.CANDIDATE}


class ReviewError(RuntimeError):
    """The move is not allowed, or is missing what it needs."""


def allowed(signal: Signal) -> tuple[str, ...]:
    return TRANSITIONS.get(signal.state, ())


@transaction.atomic
def move(signal: Signal, to: str, *, actor_label: str, reason: str = "") -> Signal:
    if to not in allowed(signal):
        raise ReviewError(
            f"{signal.code} is {signal.get_state_display().lower()}; it cannot move to "
            f"'{Signal.State(to).label.lower() if to in Signal.State.values else to}'."
        )
    if to in NEEDS_REASON and not reason.strip():
        raise ReviewError("Say why — the reason is what the next reviewer reads.")
    if signal.is_illustrative and to == S.APPROVED:
        raise ReviewError(
            f"{signal.code} is illustrative — seeded, not detected — and cannot be "
            f"approved into a brief."
        )

    previous = signal.state
    signal.state = to
    signal.awaiting_operator_judgment = to == S.CANDIDATE
    signal.save(update_fields=["state", "awaiting_operator_judgment"])
    _audit(
        actor_label,
        f"{signal.code}: {Signal.State(previous).label} → {Signal.State(to).label}",
        {"signal": signal.code, "from": previous, "to": to, "reason": reason.strip()},
    )
    return signal


@transaction.atomic
def merge(signal: Signal, into: Signal, *, actor_label: str) -> Signal:
    """Fold a duplicate into the signal that continues.

    The evidence moves — a claim already on the target keeps the target's role —
    and the duplicate is rejected with a note naming where it went, so nothing
    is lost and the history still reads.
    """
    if signal.pk == into.pk:
        raise ReviewError("A signal cannot be merged into itself.")
    if into.state == S.REJECTED:
        raise ReviewError(f"{into.code} is rejected; reopen it before merging into it.")
    if S.REJECTED not in allowed(signal):
        raise ReviewError(
            f"{signal.code} is {signal.get_state_display().lower()} and cannot be merged."
        )

    held = set(SignalEvidence.objects.filter(signal=into).values_list("claim_id", flat=True))
    moved = 0
    for row in SignalEvidence.objects.filter(signal=signal):
        if row.claim_id not in held:
            SignalEvidence.objects.create(
                signal=into, claim_id=row.claim_id, role=row.role, added_at=row.added_at
            )
            moved += 1

    move(signal, S.REJECTED, actor_label=actor_label, reason=f"Merged into {into.code}.")
    _audit(
        actor_label,
        f"{signal.code} merged into {into.code} ({moved} claim(s) moved)",
        {"signal": signal.code, "into": into.code, "moved": moved},
    )

    from .detection import rescore

    rescore(into, now=timezone.now())
    return into


def _audit(actor_label: str, message: str, context: dict) -> None:
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.REVIEW,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label or "operator",
        organization=None,
        message=message,
        context=context,
    )


# ── Hand-entered evidence ───────────────────────────────────────────────────


def parse_observation_ids(text: str) -> list[int]:
    """'O12, O15 17' → [12, 15, 17]."""
    import re

    return [int(n) for n in re.findall(r"\d+", text or "")]


@transaction.atomic
def attach_observations(
    signal: Signal, ids: list[int], *, role: str, actor_label: str
) -> tuple[list[int], list[int]]:
    """Attach observations by number. Returns (attached, not found)."""
    from apps.evidence.models import Observation

    from .models import SignalObservation

    if role not in SignalEvidence.Role.values:
        raise ReviewError("Choose whether the observations support or contradict it.")
    found = {o.pk: o for o in Observation.objects.filter(pk__in=ids)}
    attached = []
    for pk in ids:
        if pk in found:
            _, created = SignalObservation.objects.get_or_create(
                signal=signal, observation=found[pk],
                defaults={"role": role, "added_by": actor_label},
            )
            if created:
                attached.append(pk)
    if attached:
        _audit(
            actor_label,
            f"{signal.code}: attached observation(s) {', '.join(f'O{i}' for i in attached)}",
            {"signal": signal.code, "observations": attached, "role": role},
        )
    return attached, [pk for pk in ids if pk not in found]


@transaction.atomic
def detach_observation(signal: Signal, observation_id: int, *, actor_label: str) -> None:
    from .models import SignalObservation

    deleted, _ = SignalObservation.objects.filter(
        signal=signal, observation_id=observation_id
    ).delete()
    if deleted:
        _audit(
            actor_label,
            f"{signal.code}: detached observation O{observation_id}",
            {"signal": signal.code, "observation": observation_id},
        )


@transaction.atomic
def start_from_observations(
    *, title: str, statement: str, observation_ids: list[int], actor_label: str
) -> tuple[Signal, list[int]]:
    """A development an analyst starts from observations, as Mark's October
    briefs were built. Returns (signal, observation numbers not found).

    Its status is earned from the observations, never asserted: two or more
    independent events in different weeks make it a recurring discussion,
    anything less a watch item. It is never a measured trend, because nothing
    here measures change over time.
    """
    from apps.evidence.models import Observation

    from .detection import _next_code

    title, statement = title.strip(), statement.strip()
    if not title or not statement:
        raise ReviewError("Give the development a title and a one-paragraph statement.")
    found = list(Observation.objects.filter(pk__in=observation_ids))
    if not found:
        raise ReviewError("Start from at least one observation (by number, e.g. O12).")

    events = {o.independence_key: o for o in found}
    weeks = {
        (o.published_at or o.observed_at).isocalendar()[:2]
        for o in events.values() if (o.published_at or o.observed_at)
    }
    maturity = (
        Signal.Maturity.RECURRING if len(events) >= 2 and len(weeks) >= 2
        else Signal.Maturity.WATCH
    )
    signal = Signal.objects.create(
        code=_next_code(),
        title=title[:300],
        statement=statement,
        state=Signal.State.CANDIDATE,
        maturity=maturity,
        domain_score=0,
        origin=Signal.Origin.ANALYST,
        created_by_label=actor_label,
        item_count=len(events),
        creator_count=len(events),
        source_count=len({o.source_type for o in found}),
    )
    attach_observations(
        signal, [o.pk for o in found], role=SignalEvidence.Role.SUPPORTS,
        actor_label=actor_label,
    )
    _audit(
        actor_label,
        f"{signal.code}: started from {len(found)} observation(s) "
        f"({signal.get_maturity_display()})",
        {"signal": signal.code, "observations": [o.pk for o in found],
         "independent_events": len(events)},
    )
    return signal, [i for i in observation_ids if i not in {o.pk for o in found}]

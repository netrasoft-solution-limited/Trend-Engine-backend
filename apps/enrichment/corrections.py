"""Correcting a transcription error in a quote, openly.

A quote is verbatim, so it is never rewritten silently. An operator records
the wrong words and the right ones; every output then shows the right words in
square brackets with a note, the standard editorial form, and the original
stays on the claim. Observations collected from the same claim (their excerpt
is a copy of the quote) are corrected the same way.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.evidence.models import Observation
from apps.operations.models import AuditEvent

from .models import CORRECTION_NOTE, Claim


class CorrectionError(ValueError):
    pass


@transaction.atomic
def correct_quote(claim: Claim, *, wrong: str, right: str, actor_label: str) -> Claim:
    wrong, right = wrong.strip(), right.strip()
    if not wrong or not right:
        raise CorrectionError("Give both the words as transcribed and the correct words.")
    if wrong not in (claim.quote or ""):
        raise CorrectionError(f"“{wrong}” is not in the quote of C{claim.pk}.")
    claim.quote_corrections = [
        c for c in claim.quote_corrections or [] if c.get("wrong") != wrong
    ] + [{"wrong": wrong, "right": right, "by": actor_label,
          "at": timezone.now().isoformat()}]
    claim.save(update_fields=["quote_corrections"])

    copies = Observation.objects.filter(provenance__contains=f"claim C{claim.pk}")
    for o in copies:
        if wrong in o.excerpt:
            o.excerpt = o.excerpt.replace(wrong, f"[{right}]")
            if CORRECTION_NOTE not in o.limitations:
                o.limitations = f"{o.limitations} {CORRECTION_NOTE}".strip()
            o.save(update_fields=["excerpt", "limitations"])

    AuditEvent.objects.create(
        kind=AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label,
        message=f"Corrected a transcription error in C{claim.pk}: “{wrong}” → “{right}”",
        context={"claim_id": claim.pk, "wrong": wrong, "right": right,
                 "observations": [o.pk for o in copies]},
    )
    return claim

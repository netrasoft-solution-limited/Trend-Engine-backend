"""Client recommendations — why a signal matters to one client, and what to do.

The step Mark's review asked for by name: connect "a demonstrated shift ... to
Jarrow's portfolio and a concrete content or ecommerce response". A model drafts
it because it is a judgement in prose; everything around the model decides what
it may say:

  · IT SEES ONE CLIENT. The prompt carries this client's profile and nothing
    else (PRD §6.4: "Another tenant's private context is never available to the
    prompt or query"). Called inside the caller's tenant scope.
  · IT SEES ONE SIGNAL'S EVIDENCE. The claims it may cite are the signal's own,
    numbered; any other number is dropped.
  · EVERY ACTION IS GROUNDED. An action that cites no valid claim is removed. A
    recommendation left with no actions is not stored.
  · ONLY THE CLIENT'S PRODUCTS. A product not in the profile is removed —
    a recommendation to promote something the client does not sell is worse than
    none, and it is exactly the error a fluent model makes.
  · IT MAY DECLINE. "This is not actionable for this client" is a valid answer
    and is returned as such, not forced into an action.

Then an operator reviews it (`edit`, `decide`). Approval here is not publication
— a recommendation reaches a client only inside a brief that passes the gate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel, Field

from apps.clients.models import ClientAsset, ClientTerm
from apps.enrichment.domain import domain_pack
from apps.enrichment.llm.client import (
    LLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    cached_system,
    recommendation_tier,
)
from apps.enrichment.llm.costs import CostCapExceeded
from apps.enrichment.models import ModelRun
from apps.intelligence.models import Signal, SignalEvidence
from apps.operations.models import AuditEvent

from .models import ClientSignalScore, Recommendation

logger = logging.getLogger(__name__)

PROMPT_VERSION = "recommendation/2026-10-13c"

#: Example Outputs B1's owners. Anything else is mapped to the nearest
#: catch-all rather than stored as whatever the model invented.
OWNERS = ("Content/SEO", "Ecommerce", "Scientific/Legal", "Innovation", "Brand")
MAX_ACTIONS = 4


class RecommendationError(RuntimeError):
    """The recommendation cannot be drafted, saved or decided as asked."""


# ── What the model returns ──────────────────────────────────────────────────


class ProposedAction(BaseModel):
    owner: str = Field(description=f"One of: {', '.join(OWNERS)}.")
    action: str = Field(description="A specific thing to do in the next 14 days.")
    success_measure: str = Field(description="How the client will know it worked.")
    claim_ids: list[int] = Field(description="The claim numbers this action rests on.")


class ProposedRecommendation(BaseModel):
    decline_reason: str = Field(
        default="",
        description="Set ONLY if the evidence does not justify any action for this client.",
    )
    why_it_matters: str = Field(
        default="",
        description="Two to four sentences connecting the development to this client.",
    )
    limits: str = Field(default="", description="What this evidence does not prove.")
    products: list[str] = Field(
        default_factory=list, description="Names of this client's products it concerns."
    )
    actions: list[ProposedAction] = Field(default_factory=list)
    cited_claim_ids: list[int] = Field(default_factory=list)


INSTRUCTIONS = f"""You advise one brand on one development in its category.

You are given the brand's profile (its products, audiences, priorities and \
competitors) and one development detected across independent sources, with \
its numbered evidence. Write why this development matters to THIS brand and \
what it should do in the next 14 days.

Rules:
- Ground everything in the numbered claims. Every action lists the claim \
numbers it rests on, in its claim_ids field. Use only the numbers given. Do not \
write claim numbers into the prose fields: the brand reads those sentences, and \
the numbers mean nothing to them.
- Name only products that appear in the brand's profile, exactly as written \
there. Never suggest a product the brand does not list.
- Actions are concrete and owned: content to write, a product page to change, \
a claim to send to scientific/legal review, a test to run. Owners are one of: \
{", ".join(OWNERS)}.
- Do not make efficacy, dosage or health claims beyond what the evidence says, \
and never recommend that the brand make such a claim without scientific/legal \
review. If the evidence is contested, say so in `limits`.
- `limits` states plainly what the evidence does NOT show: popularity is not \
efficacy, a podcast claim is not a trial result.
- Never quote internal measures (scores, confidence numbers, ranks). Describe \
the strength of the evidence in words.
- If this development does not justify any action for this brand, set \
decline_reason and leave the rest empty. That is a correct answer."""


@dataclass
class Drafted:
    recommendation: Recommendation | None
    declined: str = ""


# ── Drafting ────────────────────────────────────────────────────────────────


def draft(organization, signal: Signal, *, client: LLMClient | None = None) -> Drafted:
    """Draft the current recommendation for one signal and one client.

    Caller binds the tenant scope. Refuses while an open recommendation exists:
    regenerating over an operator's edit would throw their work away.
    """
    from apps.clients import services as client_services

    profile = client_services.current_for(organization)
    if profile is None:
        raise RecommendationError(f"{organization} has no live client profile.")
    if Recommendation.objects.filter(
        organization=organization, signal=signal, state__in=Recommendation.OPEN_STATES
    ).exists():
        raise RecommendationError(
            f"{signal.code} already has an open recommendation for {organization}. "
            f"Reject it first to draft a new one."
        )

    evidence = list(
        SignalEvidence.objects.filter(signal=signal).select_related("claim__content_item__source")
    )
    if not evidence:
        raise RecommendationError(f"{signal.code} has no evidence to recommend from.")

    llm = client or LLMClient()
    try:
        proposed = llm.structured(
            schema=ProposedRecommendation,
            tier=recommendation_tier(),
            purpose=ModelRun.Purpose.DRAFTING,
            prompt_version=PROMPT_VERSION,
            system=cached_system(f"{INSTRUCTIONS}\n\n## The category\n\n{domain_pack()}"),
            user=_render(organization, profile, signal, evidence),
        )
    except (CostCapExceeded, LLMUnavailable, LLMInvalidOutput) as exc:
        raise RecommendationError(f"Drafting failed: {exc}") from exc

    if proposed.decline_reason.strip() and not proposed.actions:
        logger.info("%s declined for %s: %s", signal.code, organization, proposed.decline_reason)
        return Drafted(None, declined=proposed.decline_reason.strip())

    content, validation = validate(
        proposed.model_dump(),
        allowed_claim_ids={e.claim_id for e in evidence},
        product_names=list(profile.assets.values_list("name", flat=True)),
    )
    model_run = (
        ModelRun.objects.filter(purpose=ModelRun.Purpose.DRAFTING, prompt_version=PROMPT_VERSION)
        .order_by("-at")
        .first()
    )
    with transaction.atomic():
        recommendation = Recommendation.objects.create(
            organization=organization,
            signal=signal,
            profile=profile,
            model_run=model_run,
            prompt_version=PROMPT_VERSION,
            validation=validation,
            **content,
        )
        _audit(
            organization,
            "system",
            f"Drafted a recommendation on {signal.code}",
            {"recommendation_id": recommendation.pk, "validation": validation},
        )
    return Drafted(recommendation)


def validate(
    raw: dict, *, allowed_claim_ids: set[int], product_names: list[str]
) -> tuple[dict, dict]:
    """Keep what is grounded; record what was not. Used for drafts AND edits.

    Raises when nothing actionable survives, because a recommendation with no
    grounded action is an opinion, and storing it would put an opinion in the
    review queue looking like analysis.
    """
    canonical = {name.lower(): name for name in product_names}
    validation = {"invented_claim_ids": [], "uncited_actions": 0, "unknown_products": []}

    actions = []
    for action in (raw.get("actions") or [])[:MAX_ACTIONS]:
        ids = list(dict.fromkeys(action.get("claim_ids") or []))
        kept = [i for i in ids if i in allowed_claim_ids]
        validation["invented_claim_ids"] += [i for i in ids if i not in allowed_claim_ids]
        text = (action.get("action") or "").strip()
        if not kept or not text:
            validation["uncited_actions"] += 1
            continue
        owner = (action.get("owner") or "").strip()
        actions.append(
            {
                "owner": owner if owner in OWNERS else "Brand",
                "action": text,
                "success_measure": (action.get("success_measure") or "").strip(),
                "claim_ids": kept,
            }
        )
    if not actions:
        raise RecommendationError(
            "No action survived validation: every proposed action either cited no claim "
            "from this signal's evidence or was empty."
        )

    products = []
    for name in raw.get("products") or []:
        match = canonical.get((name or "").strip().lower())
        if match is None:
            validation["unknown_products"].append(name)
        elif match not in products:
            products.append(match)

    cited = [i for i in dict.fromkeys(raw.get("cited_claim_ids") or []) if i in allowed_claim_ids]
    for action in actions:
        cited += [i for i in action["claim_ids"] if i not in cited]

    why = (raw.get("why_it_matters") or "").strip()
    if not why:
        raise RecommendationError("A recommendation has to say why it matters.")

    return (
        {
            "why_it_matters": why,
            "limits": (raw.get("limits") or "").strip(),
            "actions": actions,
            "products": products,
            "cited_claim_ids": cited,
        },
        validation,
    )


def _render(organization, profile, signal: Signal, evidence: list[SignalEvidence]) -> str:
    def rows(facet):
        return "\n".join(
            f"- {t.name} (weight {t.weight}){': ' + t.notes if t.notes else ''}"
            for t in profile.terms.filter(facet=facet)
        ) or "- (none listed)"

    assets = "\n".join(
        f"- {a.name} [{a.get_kind_display()}, weight {a.weight}]{': ' + a.notes if a.notes else ''}"
        for a in profile.assets.order_by("-weight", "name")
    ) or "- (none listed)"

    score = ClientSignalScore.objects.filter(organization=organization, signal=signal).first()
    matched = (
        "; ".join(f"{m['name']} (matched '{m['term']}')" for m in (score.matches or [])[:10])
        if score else ""
    ) or "nothing matched directly"

    lines = []
    for row in evidence:
        claim, item = row.claim, row.claim.content_item
        tags = ["CONTRADICTS" if row.role == SignalEvidence.Role.CONTRADICTS else "supports"]
        if claim.is_cited:
            tags.append("cites a study")
        when = item.published_at or item.discovered_at
        lines.append(
            f"C{claim.pk} | {', '.join(tags)} | {item.creator or item.source.name} | "
            f"\"{item.title}\" | {when:%Y-%m-%d} | {claim.text} | quote: \"{claim.quote}\""
        )

    return (
        f"## The brand: {organization.name}\n\n"
        f"### Products and ingredients\n{assets}\n\n"
        f"### Audiences\n{rows(ClientTerm.Facet.AUDIENCE)}\n\n"
        f"### Priorities\n{rows(ClientTerm.Facet.PRIORITY)}\n\n"
        f"### Competitors\n{rows(ClientTerm.Facet.COMPETITOR)}\n\n"
        f"## The development: {signal.title}\n\n{signal.statement}\n\n"
        f"Type: {signal.get_signal_type_display() or 'unspecified'}. "
        # In words, not as our 0–100 measures: whatever the model is shown, it
        # may repeat, and these sentences are read by the brand.
        f"Evidence: {signal.item_count} item(s) from {signal.creator_count} independent "
        f"creator(s); {sum(1 for e in evidence if e.claim.is_cited)} claim(s) cite a study; "
        f"{sum(1 for e in evidence if e.role == SignalEvidence.Role.CONTRADICTS)} claim(s) "
        f"disagree.\n"
        f"What in the brand's profile it touched: {matched}.\n\n"
        f"## Evidence ({len(evidence)} claims)\n\n" + "\n".join(lines)
    )


# ── Operator review ─────────────────────────────────────────────────────────


@transaction.atomic
def edit(recommendation: Recommendation, raw: dict, *, actor_label: str) -> Recommendation:
    """An operator's edit, held to the same grounding rules as the draft."""
    if recommendation.state not in (Recommendation.State.PROPOSED, Recommendation.State.EDITED):
        raise RecommendationError(
            f"A recommendation that is {recommendation.get_state_display().lower()} "
            f"cannot be edited."
        )
    evidence = SignalEvidence.objects.filter(signal=recommendation.signal)
    allowed = set(evidence.values_list("claim_id", flat=True))
    assets = ClientAsset.objects.filter(profile=recommendation.profile)
    products = list(assets.values_list("name", flat=True)) if recommendation.profile_id else []
    content, validation = validate(raw, allowed_claim_ids=allowed, product_names=products)
    for field, value in content.items():
        setattr(recommendation, field, value)
    recommendation.validation = validation
    recommendation.state = Recommendation.State.EDITED
    recommendation.edited_by_label = actor_label
    recommendation.save()
    _audit(
        recommendation.organization,
        actor_label,
        f"Edited the recommendation on {recommendation.signal.code}",
        {"recommendation_id": recommendation.pk, "validation": validation},
    )
    return recommendation


@transaction.atomic
def decide(
    recommendation: Recommendation, *, approve: bool, actor_label: str, note: str = ""
) -> Recommendation:
    if recommendation.state not in (Recommendation.State.PROPOSED, Recommendation.State.EDITED):
        raise RecommendationError(
            f"This recommendation is already {recommendation.get_state_display().lower()}."
        )
    if not approve and not note.strip():
        raise RecommendationError("Say why it is rejected — the reason is the record.")
    recommendation.state = (
        Recommendation.State.APPROVED if approve else Recommendation.State.REJECTED
    )
    recommendation.decided_by_label = actor_label
    recommendation.decided_at = timezone.now()
    recommendation.decision_note = note.strip()
    recommendation.save(
        update_fields=["state", "decided_by_label", "decided_at", "decision_note", "updated_at"]
    )
    _audit(
        recommendation.organization,
        actor_label,
        f"{'Approved' if approve else 'Rejected'} the recommendation on "
        f"{recommendation.signal.code}",
        {"recommendation_id": recommendation.pk, "note": note.strip()},
        kind=AuditEvent.Kind.REVIEW,
    )
    return recommendation


def _audit(
    organization, actor_label: str, message: str, context: dict, *, kind: str = ""
) -> None:
    AuditEvent.objects.create(
        kind=kind or AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_label=actor_label or "system",
        organization=organization,
        message=message,
        context=context,
    )

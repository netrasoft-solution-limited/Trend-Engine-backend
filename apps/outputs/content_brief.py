"""The content brief — one approved recommendation turned into a writer's packet.

Example Outputs B3: a working title, who it is for, the question it answers,
the angle, an outline in which every section says what evidence it needs, and
the production lanes it must clear (scientific, legal, search, conversion).

A model drafts the editorial parts because they are craft — a title, an angle,
an outline. It is held to the same rules as the recommendation it comes from:

  · ONLY FROM AN APPROVED RECOMMENDATION. The brief cannot say more than the
    operator already agreed this client should do.
  · EVIDENCE BY NUMBER. Outline sections cite the signal's claims; an invented
    number is dropped, and a section left with none is kept but marked "needs a
    source before drafting" — the writer is told, rather than the gap hidden.
  · THE CLIENT'S OWN AUDIENCE AND PRODUCTS. The audience must be one in the
    profile; products come from the approved recommendation, not the model.
  · A FIXED GUARDRAIL. Whatever the model adds, the brief always says that no
    efficacy, dosage or health claim goes out without Scientific/Legal review.
"""
from __future__ import annotations

from collections import OrderedDict

from pydantic import BaseModel, Field

from apps.clients.models import ClientTerm
from apps.enrichment.domain import domain_pack
from apps.enrichment.llm.client import (
    LLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    cached_system,
    content_tier,
)
from apps.enrichment.llm.costs import CostCapExceeded
from apps.enrichment.models import ModelRun
from apps.intelligence.models import SignalEvidence
from apps.scoring.models import Recommendation

from . import briefing
from .services import DraftingError

PROMPT_VERSION = "content-package/2026-10-24b"

ALWAYS = (
    "No efficacy, dosage or health claim is published without Scientific/Legal "
    "review, however it is worded."
)


class OutlineSection(BaseModel):
    heading: str
    purpose: str = Field(description="What this section must do for the reader.")
    claim_ids: list[int] = Field(description="Claim numbers it draws on.")
    observation_ids: list[int] = Field(
        default_factory=list, description="Observation numbers (O-numbers) it draws on."
    )


class FAQ(BaseModel):
    product: str = Field(description="One product from the recommendation, exactly as named.")
    question: str
    answer: str = Field(description="Within the product's published description; no new claims.")


class ContentBriefDraft(BaseModel):
    working_title: str = Field(max_length=160)
    audience: str = Field(description="One audience from the brand's profile, exactly as named.")
    core_question: str = Field(description="The reader's question, in their words.")
    search_intent: str = Field(description="What someone searching or asking an assistant wants.")
    angle: str = Field(description="The editorial angle in two or three sentences.")
    outline: list[OutlineSection]
    guardrails: list[str] = Field(
        default_factory=list, description="What the piece must not say, given this evidence."
    )
    # The rest of the package (Mark, 20 Oct): what the delivered brief carries
    # for its lead story.
    sab_specialty: str = Field(
        default="", description="The expert specialty that should byline it."
    )
    article_draft: str = Field(
        default="", description="The article in the expert's voice, 450-700 words, paragraphs."
    )
    faqs: list[FAQ] = Field(default_factory=list)
    video_script: str = Field(default="", description="A 30-60 second script.")
    email_subject: str = ""
    email_body: str = ""
    email_button: str = ""
    social_posts: list[str] = Field(default_factory=list)
    ai_questions: list[str] = Field(
        default_factory=list, description="Questions people ask AI assistants that this answers."
    )
    what_to_measure: list[str] = Field(default_factory=list)


INSTRUCTIONS = """You turn an approved recommendation into a content brief a \
writer can work from.

Rules:
- Stay inside the approved recommendation. Do not add products, claims or advice \
it does not contain.
- Choose the audience from the brand's audiences, exactly as named.
- Every outline section lists the claim numbers it draws on, using only the \
numbers given. A section that needs evidence the claims do not provide gets an \
empty list — do not stretch a claim to fit.
- The angle is educational: help the reader choose well. Never promise outcomes.
- Guardrails name what this specific evidence does not support saying.

Then draft the package that follows from it, all as drafts for Scientific Affairs review:
- the article, drafted for the reviewing specialist to edit and sign (structure/function \
language only; no disease claims — treat, cure, prevent, reverse; products never in \
the body of the article)
- product-page FAQs, only for the products in the recommendation, staying within \
what the product's label and published description already say
- a short video script, an email (subject, body, button) and social posts
- the questions people ask AI assistants that this content answers, and what to \
measure after it runs.

Voice and endorsement — these drafts go out under a real person's name later, so:
- Write in a neutral voice, never the first person. Never invent experience the \
specialist has not told us about: no "in my practice", "in my conversations", "my \
patients/clients", "I often see", "as a dietitian I…".
- Refer to the reviewer only as "our specialist" or "the reviewing expert": no name, \
no gender, no credentials that have not been supplied.
- Never imply that any person, expert, publisher or institution endorses the brand or \
its products. A source discussing a topic is not endorsing anything.
- Describe what sources said only as the evidence shows; no invented quotes.
- Never say the content has been reviewed, checked or approved by anyone: that \
review has not happened yet, and the reviewer adds such a line themselves."""


def sections_for(organization, recommendation: Recommendation, *, client=None):
    """The content brief's body, its working title, and its profile citation."""
    if recommendation.state != Recommendation.State.APPROVED:
        raise DraftingError(
            "A content brief is drafted from an approved recommendation. Approve it in "
            "Triage first."
        )
    signal = recommendation.signal
    profile = recommendation.profile
    evidence = list(
        SignalEvidence.objects.filter(signal=signal, claim__is_sponsored=False)
        .select_related("claim__segment", "claim__content_item__source")
    )
    allowed = {row.claim_id: row for row in evidence}
    observations = list(
        signal.observations.select_related("observation").order_by("role", "added_at")
    )
    allowed_obs = {link.observation_id: link.observation for link in observations}
    audiences = (
        list(profile.terms.filter(facet=ClientTerm.Facet.AUDIENCE).values_list("name", flat=True))
        if profile else []
    )

    llm = client or LLMClient()
    try:
        draft = llm.structured(
            schema=ContentBriefDraft,
            tier=content_tier(),
            purpose=ModelRun.Purpose.DRAFTING,
            prompt_version=PROMPT_VERSION,
            system=cached_system(f"{INSTRUCTIONS}\n\n## The category\n\n{domain_pack()}"),
            user=_render(organization, recommendation, audiences, evidence, observations),
        )
    except (CostCapExceeded, LLMUnavailable, LLMInvalidOutput) as exc:
        raise DraftingError(f"The content brief could not be drafted: {exc}") from exc

    audience = next(
        (a for a in audiences if a.lower() == draft.audience.strip().lower()),
        audiences[0] if audiences else "",
    )
    used: OrderedDict[int, SignalEvidence] = OrderedDict()
    used_obs: OrderedDict[int, object] = OrderedDict()

    # Claims are numbered first, then observations, so every reference is
    # resolved only once the whole outline has been read.
    picked = []
    for section in draft.outline:
        ids = [i for i in dict.fromkeys(section.claim_ids) if i in allowed]
        obs_ids = [i for i in dict.fromkeys(section.observation_ids) if i in allowed_obs]
        for i in ids:
            used.setdefault(i, allowed[i])
        for i in obs_ids:
            used_obs.setdefault(i, allowed_obs[i])
        picked.append((section, ids, obs_ids))

    outline_lines = []
    for n, (section, ids, obs_ids) in enumerate(picked, start=1):
        refs = [f"E{list(used).index(i) + 1}" for i in ids] + [
            f"E{len(used) + list(used_obs).index(i) + 1}" for i in obs_ids
        ]
        rests_on = "draws on " + ", ".join(refs) if refs else "needs a source before drafting"
        outline_lines.append(f"· {n}. {section.heading} — {section.purpose} ({rests_on})")

    evidence_lines = [
        f"· E{n}. {briefing.quote_line(row.claim)}"
        for n, row in enumerate(used.values(), start=1)
    ] + [
        f"· E{len(used) + n}. {briefing.observation_line(o)}"
        for n, o in enumerate(used_obs.values(), start=1)
    ]
    products = {p.lower(): p for p in recommendation.products}
    faqs = [f for f in draft.faqs if f.product.strip().lower() in products]
    guardrails = [*(g.strip() for g in draft.guardrails if g.strip()), ALWAYS]
    if recommendation.limits:
        guardrails.insert(
            0, f"The evidence does not show: {briefing.client_facing(recommendation.limits)}"
        )

    sections = [
        {
            "heading": "The brief",
            "text": "\n".join(
                [
                    f"· Working title: {draft.working_title}",
                    f"· For: {audience or 'audience to be confirmed'}",
                    f"· Their question: {draft.core_question}",
                    f"· What they are looking for: {draft.search_intent}",
                    f"· Products: {', '.join(recommendation.products) or 'none named'}",
                ]
            ),
        },
        {"heading": "Angle", "text": draft.angle.strip()},
        {"heading": "Outline", "text": "\n".join(outline_lines)},
        {
            "heading": "Evidence to use",
            "text": "\n".join(evidence_lines) or "No evidence was cited by the outline.",
        },
        {"heading": "Claims guardrail", "text": "\n".join(f"· {g}" for g in guardrails)},
        {
            "heading": "Production checks",
            "text": "\n".join(
                [
                    "· Evidence (Scientific): every statement traces to an item above.",
                    "· Claims (Legal): wording reviewed against the guardrail.",
                    "· Search and AI answers (Content/SEO): answers the reader's question "
                    "directly, in the first paragraph.",
                    "· Conversion (Ecommerce/Brand): links to the named products, makes no "
                    "outcome promise.",
                ]
            ),
        },
        *_package_sections(draft, faqs, products, organization.name),
        {
            "heading": "Why this piece",
            "text": (
                f"{signal.title}. {signal.statement}\n\n"
                f"{briefing.client_facing(recommendation.why_it_matters)}"
            ),
        },
    ]
    flags = voice_flags(sections)
    if flags:
        sections.insert(0, {
            "heading": "Review flags",
            "text": "Fix before review: these phrases invent the specialist's experience or "
            "imply an endorsement.\n" + "\n".join(f"· {f}" for f in flags),
        })
    return sections, draft.working_title.strip(), (profile.citation if profile else "")


#: Phrases that invent the specialist's experience or imply an endorsement. The
#: prompt forbids them; this catches a draft that slips, and flags it for the
#: reviewer rather than rewriting it silently.
VOICE_FLAGS = (
    r"\bin my (?:practice|clinic|conversations?|experience|work)\b",
    r"\bmy (?:patients|clients)\b",
    r"\bI (?:often|regularly|frequently|always) (?:see|hear|recommend|tell)\b",
    r"\bas an? (?:doctor|dietitian|nutritionist|physician|pharmacist|clinician)\b",
    r"\b(?:has|have) been (?:reviewed|checked|approved|vetted) by\b",
    r"\b(?:reviewed|checked|approved|vetted) by (?:our|a|an) "
    r"(?:specialist|expert|dietitian|doctor|nutritionist)\b",
    r"\b(?:recommended|endorsed|approved|trusted) by "
    r"(?:doctors|experts|dietitians|nutritionists)\b",
)


def voice_flags(sections: list[dict]) -> list[str]:
    """Each flagged phrase, quoted with the section it is in."""
    import re

    found = []
    for section in sections:
        for pattern in VOICE_FLAGS:
            for match in re.finditer(pattern, section["text"], flags=re.IGNORECASE):
                found.append(f"{section['heading']}: “{match.group(0)}”")
    return found


DRAFT_LABEL = (
    "Draft for {client} Scientific Affairs review. Not published, and not medically or "
    "legally approved."
)


def _package_sections(draft, faqs, products, client: str) -> list[dict]:
    """The rest of the package, each piece labelled as a draft for review."""
    label = DRAFT_LABEL.format(client=client)
    out = []
    if draft.sab_specialty.strip():
        out.append({
            "heading": "Suggested Science Advisory Board specialty",
            "text": draft.sab_specialty.strip(),
        })
    if draft.article_draft.strip():
        out.append({
            "heading": f"Article draft: {draft.working_title.strip()}",
            "text": f"{label}\n\n{draft.article_draft.strip()}",
        })
    if faqs:
        out.append({
            "heading": "Product-page FAQ drafts",
            "text": f"{label}\n\n" + "\n\n".join(
                f"{products[f.product.strip().lower()]}: {f.question.strip()}\n· {f.answer.strip()}"
                for f in faqs
            ),
        })
    if draft.video_script.strip():
        out.append({"heading": "Short video script", "text": draft.video_script.strip()})
    if draft.email_subject.strip() or draft.email_body.strip():
        out.append({
            "heading": "Email draft",
            "text": "\n".join([
                f"· Subject: {draft.email_subject.strip()}",
                f"· Body: {draft.email_body.strip()}",
                f"· Button: {draft.email_button.strip()}",
            ]),
        })
    if draft.social_posts:
        out.append({
            "heading": "Social posts",
            "text": "\n".join(f"· {p.strip()}" for p in draft.social_posts if p.strip()),
        })
    if draft.ai_questions:
        out.append({
            "heading": "AI questions to win",
            "text": "\n".join(f"· {q.strip()}" for q in draft.ai_questions if q.strip()),
        })
    if draft.what_to_measure:
        out.append({
            "heading": "What to measure",
            "text": "\n".join(f"· {m.strip()}" for m in draft.what_to_measure if m.strip()),
        })
    return out


def _render(organization, recommendation, audiences, evidence, observations=()) -> str:
    actions = "\n".join(
        f"- {a['owner']}: {a['action']} (success: {a.get('success_measure', '')})"
        for a in recommendation.actions
    )
    claims = "\n".join(
        f"C{row.claim_id} | {'CONTRADICTS' if row.role == 'contradicts' else 'supports'} | "
        f"{row.claim.content_item.creator or row.claim.content_item.source.name} | "
        f"{row.claim.text} | quote: \"{row.claim.quote}\""
        for row in evidence
    )
    observed = "\n".join(
        f"O{link.observation_id} | {'CONTRADICTS' if link.role == 'contradicts' else 'supports'} | "
        f"{link.observation.get_source_type_display()} | {link.observation.title} | "
        f"{link.observation.excerpt} | labels: {', '.join(link.observation.labels) or 'none'}"
        for link in observations
    ) or "(none attached)"
    return (
        f"## Brand: {organization.name}\n"
        f"Audiences: {', '.join(audiences) or '(none listed)'}\n"
        f"Products in the recommendation: {', '.join(recommendation.products) or '(none)'}\n\n"
        f"## The development: {recommendation.signal.title}\n{recommendation.signal.statement}\n\n"
        f"## Approved recommendation\nWhy it matters: {recommendation.why_it_matters}\n"
        f"Limits: {recommendation.limits}\nActions:\n{actions}\n\n"
        f"## Evidence: claims\n{claims}\n\n## Evidence: observations\n{observed}"
    )

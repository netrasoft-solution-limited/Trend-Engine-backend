"""The client brief — approved developments, explained for one client.

Mark's 8 October review: the earlier export "largely groups transcript
quotations and claims. It does not show clickable source links, a clear
explanation of what changed, product/audience relevance, or specific
recommended actions for Jarrow." This builds the brief in the shape of Example
Outputs B1/B2 from what the operator has APPROVED, and nothing else:

    In brief              one line per development
    <each development>    what changed · why it matters to the client ·
                          next steps · what the evidence does not prove ·
                          the evidence, quoted, each with a link to the moment
    Next 14 days          every action, by owner
    Watch list            developments being watched, not acted on
    How this was put together

WHAT A CLIENT NEVER SEES HERE (PRD §3.2): internal scores, vendor names, costs,
model names, rejected candidates. Evidence strength is said in words — how many
independent sources, how many cite a study, how many disagree — because those
facts are the client's to weigh, and a 0–100 number is ours.

Templated, not generated. Every sentence of substance was written at
recommendation time and approved by an operator; this module arranges it. A
model asked to "write the brief" at this point would be free to say things no
one approved.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass

from django.db.models import Q

from apps.enrichment.models import Claim
from apps.evidence.models import ContentItem
from apps.intelligence.models import Signal, SignalEvidence
from apps.scoring.models import ClientSignalScore, Recommendation

from .services import DraftingError

#: Quotes per development in the brief. Enough to show it is several voices,
#: few enough that the brief is read rather than skimmed.
EVIDENCE_PER_DEVELOPMENT = 5
DISSENT_PER_DEVELOPMENT = 2
QUOTE_CHARS = 220

#: "(C8)", "(C5, C8)", "C12" — the claim numbers a recommendation uses to cite
#: its evidence. They are how an operator checks it; to a client they are noise
#: that looks like a broken footnote.
_CLAIM_REF = re.compile(r"\s*\((?:C\d+(?:\s*,\s*|\s+and\s+)?)+\)|\bC\d+\b")


#: "(42/100)" — an internal score, which PRD §3.2 keeps from the client.
_SCORE = re.compile(r"\s*\(?\b\d{1,3}\s*/\s*100\b\)?")


def client_facing(text: str) -> str:
    """Operator-written prose with internal claim references and scores removed.

    A backstop: the recommendation prompt already asks for neither. Anything a
    model was ever shown, it may repeat, and these sentences go to the client.
    """
    cleaned = _SCORE.sub("", _CLAIM_REF.sub("", text or ""))
    return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", cleaned)).strip()


@dataclass
class Development:
    signal: Signal
    recommendation: Recommendation
    rank: int | None


def approved_developments(organization, codes: list[str] | None = None) -> list[Development]:
    """Signals the operator approved, with this client's approved recommendation.

    Both approvals are required. An approved signal says "this is real"; an
    approved recommendation says "and this is what it means for this client".
    Either alone is not something to send them. Scoped by the caller.
    """
    recommendations = (
        Recommendation.objects.filter(
            organization=organization,
            state=Recommendation.State.APPROVED,
            signal__state=Signal.State.APPROVED,
            signal__is_illustrative=False,
        )
        .select_related("signal", "profile")
    )
    if codes:
        recommendations = recommendations.filter(signal__code__in=codes)
    ranks = dict(
        ClientSignalScore.objects.filter(organization=organization).values_list(
            "signal_id", "rank_score"
        )
    )
    developments = [Development(r.signal, r, ranks.get(r.signal_id)) for r in recommendations]
    developments.sort(key=lambda d: (-(d.rank or 0), d.signal.code))
    return developments


@dataclass
class Brief:
    sections: list[dict]
    #: The profile version(s) the approved recommendations were made against —
    #: recorded on the output (PRD §8).
    profile: str
    developments: int

    @property
    def summary(self) -> str:
        return f"{self.developments} approved development(s), with recommended next steps."


def sections_for_brief(organization, codes: list[str] | None = None) -> Brief:
    """The brief's body, and the profile version it was built against.

    Refuses rather than producing an empty or partial document: a brief with
    nothing in it reads as "nothing happened", which is a claim.
    """
    developments = approved_developments(organization, codes)
    if not developments:
        pending = Recommendation.objects.filter(
            organization=organization, state__in=Recommendation.OPEN_STATES
        ).exclude(state=Recommendation.State.APPROVED).count()
        raise DraftingError(
            f"Nothing is approved for {organization} yet. A development reaches a brief "
            f"when its signal AND {organization}'s recommendation on it are both approved "
            f"in Triage"
            + (f" — {pending} recommendation(s) are waiting for review." if pending else ".")
        )

    profiles = {d.recommendation.profile.citation for d in developments if d.recommendation.profile}
    sources: OrderedDict[int, ContentItem] = OrderedDict()

    sections = [_in_brief(developments)]
    for development in developments:
        sections.append(_development(organization, development, sources))
    sections.append(_next_steps(developments))
    watch = _watch_list(organization, exclude={d.signal.pk for d in developments})
    if watch:
        sections.append(watch)
    sections.append(_method(organization, developments, sources, profiles))
    return Brief(sections, ", ".join(sorted(profiles)), len(developments))


# ── Sections ────────────────────────────────────────────────────────────────


def _in_brief(developments: list[Development]) -> dict:
    lines = [
        f"· {d.signal.title}: {_first_sentence(client_facing(d.recommendation.why_it_matters))}"
        for d in developments
    ]
    return {"heading": "In brief", "text": "\n".join(lines)}


def _development(organization, development: Development, sources) -> dict:
    signal, rec = development.signal, development.recommendation
    supporting, dissent = _evidence(signal, sources)

    parts = [f"What changed. {signal.statement}"]
    parts.append(f"Why it matters to {organization.name}. {client_facing(rec.why_it_matters)}")
    if rec.products:
        parts.append(f"Products concerned: {', '.join(rec.products)}.")
    parts.append(
        "Recommended next steps:\n"
        + "\n".join(f"· {a['owner']}: {client_facing(a['action'])}" for a in rec.actions)
    )
    if rec.limits:
        parts.append(f"What this does not prove. {client_facing(rec.limits)}")
    parts.append(_strength(signal))
    if supporting:
        parts.append("The evidence:\n" + "\n".join(f"· {line}" for line in supporting))
    if dissent:
        parts.append(
            "Where sources disagree:\n" + "\n".join(f"· {line}" for line in dissent)
        )
    return {"heading": signal.title, "text": "\n\n".join(parts)}


def _next_steps(developments: list[Development]) -> dict:
    by_owner: OrderedDict[str, list[str]] = OrderedDict()
    for d in developments:
        for action in d.recommendation.actions:
            line = client_facing(action["action"])
            if action.get("success_measure"):
                line += f" Success looks like: {client_facing(action['success_measure'])}"
            by_owner.setdefault(action["owner"], []).append(f"{line} ({d.signal.title})")
    text = "\n\n".join(
        f"{owner}:\n" + "\n".join(f"· {line}" for line in lines)
        for owner, lines in by_owner.items()
    )
    return {"heading": "Next 14 days", "text": text}


def _watch_list(organization, *, exclude: set[int]) -> dict | None:
    watching = (
        Signal.objects.filter(state=Signal.State.WATCHING, is_illustrative=False)
        .exclude(pk__in=exclude)
        .filter(
            pk__in=ClientSignalScore.objects.filter(
                organization=organization, fit_score__gt=0
            ).values("signal_id")
        )
        .order_by("-domain_score", "code")[:6]
    )
    if not watching:
        return None
    return {
        "heading": "Watch list",
        "text": "Not yet strong enough to act on, but relevant and being followed:\n"
        + "\n".join(f"· {s.title}: {_first_sentence(s.statement)}" for s in watching),
    }


def _method(organization, developments, sources: OrderedDict, profiles: set[str]) -> dict:
    dates = [d for d in ((i.published_at or i.discovered_at) for i in sources.values()) if d]
    if not dates:
        period = ""
    elif min(dates).date() == max(dates).date():
        period = f" published on {min(dates):%-d %B %Y}"
    else:
        period = f" published between {min(dates):%-d %B} and {max(dates):%-d %B %Y}"
    creators = {(i.creator or i.source.name).strip().lower() for i in sources.values()}
    lines = "\n".join(f"· {_source_line(item)}" for item in sources.values())
    return {
        "heading": "How this was put together",
        "text": (
            f"Each development above was detected across independent sources, reviewed, "
            f"and approved by a Pure Play analyst, as was every recommendation made to "
            f"{organization.name}. Each recommended step rests on quoted evidence; "
            f"every quote links to the moment it was said. Paid sponsor segments are "
            f"identified and excluded.\n\n"
            f"Built from {len(sources)} source item(s) from {len(creators)} creator(s)"
            f"{period}, against {_possessive(organization.name)} profile "
            f"{', '.join(sorted(profiles)) or '(unversioned)'}.\n\n"
            f"Sources:\n{lines}"
        ),
    }


# ── Evidence lines ──────────────────────────────────────────────────────────


def _evidence(signal: Signal, sources: OrderedDict) -> tuple[list[str], list[str]]:
    rows = list(
        SignalEvidence.objects.filter(signal=signal)
        .filter(Q(claim__is_sponsored=False))
        .select_related("claim__segment", "claim__content_item__source")
        .order_by("-claim__is_cited", "-claim__content_item__published_at", "claim_id")
    )
    supporting, dissent, seen_items = [], [], set()
    for row in rows:
        claim = row.claim
        if row.role == SignalEvidence.Role.CONTRADICTS:
            if len(dissent) < DISSENT_PER_DEVELOPMENT:
                dissent.append(quote_line(claim, sources))
            continue
        # One quote per item first, so five quotes are five voices where possible.
        if claim.content_item_id in seen_items or len(supporting) >= EVIDENCE_PER_DEVELOPMENT:
            continue
        seen_items.add(claim.content_item_id)
        supporting.append(quote_line(claim, sources))
    return supporting, dissent


def quote_line(claim: Claim, sources: OrderedDict) -> str:
    item = claim.content_item
    sources.setdefault(item.pk, item)
    seconds = claim.segment.start_seconds if claim.segment_id else None
    quote = (claim.quote or claim.text).strip()
    if len(quote) > QUOTE_CHARS:
        quote = quote[: QUOTE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    who = item.creator or item.source.name
    title = _label(item.title)
    link = item.link_at(seconds)
    where = f"[{title}]({link})" if link else f"“{title}”"
    at = f" at {_timestamp(seconds)}" if seconds is not None else ""
    cited = " The speaker cites a study." if claim.is_cited else ""
    return f"“{quote}” — {who}, {where}{at}.{cited}"


def _source_line(item: ContentItem) -> str:
    who = item.creator or item.source.name
    when = item.published_at or item.discovered_at
    title = _label(item.title)
    named = f"[{title}]({item.url})" if item.url else title
    return f"{named} — {who}" + (f", {when:%-d %b %Y}" if when else "")


def _strength(signal: Signal) -> str:
    cited = (
        SignalEvidence.objects.filter(
            signal=signal, role=SignalEvidence.Role.SUPPORTS, claim__is_cited=True
        ).count()
    )
    against = (
        SignalEvidence.objects.filter(signal=signal, role=SignalEvidence.Role.CONTRADICTS)
        .values("claim__content_item")
        .distinct()
        .count()
    )
    parts = [
        f"Seen in {signal.item_count} item(s) from {signal.creator_count} independent "
        f"creator(s)",
        f"{cited} claim(s) cite a study" if cited else "none cites a study",
    ]
    if against:
        parts.append(f"{against} item(s) disagree")
    return "Evidence strength: " + "; ".join(parts) + "."


# ── Small helpers ───────────────────────────────────────────────────────────


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    for end in (". ", "? ", "! "):
        if end in text:
            return text.split(end, 1)[0] + end.strip()
    return text


def _possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _label(title: str) -> str:
    """A title safe inside link syntax: brackets would end the label early."""
    return (title or "untitled").replace("[", "(").replace("]", ")")


def _timestamp(seconds: float) -> str:
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

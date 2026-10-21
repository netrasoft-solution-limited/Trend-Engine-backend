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

from apps.clients import services as client_services
from apps.clients.models import ClientAsset
from apps.enrichment.models import Claim
from apps.evidence.models import ContentItem
from apps.intelligence.models import Signal, SignalEvidence
from apps.scoring import benchmark
from apps.scoring.models import ClientSignalScore, Recommendation

from .models import Output, OutputState, OutputType
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


#: How each maturity reads to a client. Only a measured, rising development is
#: called a trend (Mark, 19 Oct); the others say what they are.
STATUS = {
    Signal.Maturity.MEASURED_TREND: "Measured rising trend: discussion is measurably above "
    "its baseline.",
    Signal.Maturity.RECURRING: "Recurring discussion: raised in more than one week, not yet "
    "measured as a rising trend.",
    Signal.Maturity.WATCH: "Watch item: not yet enough evidence over time to call it a trend.",
}


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
    #: Every source cited, in first-cited order; the key says what kind it is.
    sources: OrderedDict[tuple, object] = OrderedDict()

    body = [_development(organization, d, sources) for d in developments]
    watched = _watched(organization, exclude={d.signal.pk for d in developments})
    watch = _watch_list(watched)
    family_map = _family_map(organization, developments, watched)

    sections = [
        _about(organization, developments, sources),
        _executive_summary(organization, developments, watched, sources),
        *([family_map] if family_map else []),
        *body,
        _next_steps(developments),
        *([watch] if watch else []),
        _method(organization, developments, sources, profiles),
        _sources(sources),
    ]
    return Brief(sections, ", ".join(sorted(profiles)), len(developments))


# ── Sections ────────────────────────────────────────────────────────────────


def _window(sources: OrderedDict) -> tuple:
    dates = []
    for (kind, _), obj in sources.items():
        if kind == "item":
            when = obj.published_at or obj.discovered_at
            dates.append(when.date() if hasattr(when, "date") else when)
        elif obj.published_at and not obj.is_old_material:
            # The day something was checked is not when anyone said it, so an
            # undated product page or fact sheet never stretches the window, and
            # older material is background, not part of it.
            dates.append(obj.published_at)
    dates = [d for d in dates if d]
    return (min(dates), max(dates)) if dates else (None, None)


def _about(organization, developments, sources) -> dict:
    start, end = _window(sources)
    span = (
        f"between {start:%-d %B}{f' {start.year}' if start.year != end.year else ''} and "
        f"{end:%-d %B %Y}" if start and start != end
        else f"on {start:%-d %B %Y}" if start else "in the current window"
    )
    measured = any(d.signal.maturity == Signal.Maturity.MEASURED_TREND for d in developments)
    caution = (
        "Some developments below are measured trends; each says so."
        if measured else
        "These are current conversation themes and editorial priorities. We have not "
        "measured market growth or proven that any topic is accelerating."
    )
    return {
        "heading": "About this brief",
        "text": (
            f"This brief reviews what people discussed in podcasts, YouTube and online "
            f"communities {span}, and where the {organization.name} range fits those "
            f"conversations. {caution} Every claim is linked to a dated source, listed in "
            f"full at the end.\n\n"
            f"Content drafts referred to here are for Scientific Affairs review and have "
            f"not been published. They have not been medically or legally approved."
        ),
    }


def _executive_summary(organization, developments, watched, sources) -> dict:
    lead = developments[0]
    profile = client_services.current_for(organization)
    listings = (
        profile.assets.exclude(kind=ClientAsset.Kind.CATEGORY).count() if profile else 0
    )
    families = (
        profile.assets.exclude(family="").values("family").distinct().count() if profile else 0
    )
    counts = [
        f"{listings} listings reviewed" if listings else "",
        f"{families} listening families" if families else "",
        f"{len(developments)} stor{'y' if len(developments) == 1 else 'ies'}"
        + (f" plus {len(watched)} watch item{'s' if len(watched) != 1 else ''}" if watched else ""),
        f"{len(sources)} dated, linked sources",
    ]
    stories = "\n".join(
        ["| Story | Status | Recommended response |"]
        + [
            f"| {d.signal.title} | {d.signal.get_maturity_display()} | "
            + _cell(client_facing(d.recommendation.actions[0]["action"])
                    if d.recommendation.actions else _first_sentence(
                        client_facing(d.recommendation.why_it_matters)))
            + " |"
            for d in developments
        ]
    )
    return {
        "heading": "Executive summary",
        "text": (
            f"Our recommendation. {lead.signal.title} is the lead story in this review. "
            f"{_first_sentence(client_facing(lead.recommendation.why_it_matters))}\n\n"
            f"{' · '.join(c for c in counts if c)}.\n\n"
            f"{stories}"
        ),
    }


def _cell(text: str) -> str:
    """Text safe inside a table cell: one line, no column separators."""
    return " ".join(str(text).replace("|", "/").split())


def _family_map(organization, developments, watched) -> dict | None:
    """Every listing assigned to its family, and what this brief does with each."""
    profile = client_services.current_for(organization)
    if profile is None:
        return None
    products = list(
        profile.assets.exclude(kind=ClientAsset.Kind.CATEGORY).exclude(family="").order_by("name")
    )
    if not products:
        return None
    by_name = {a.name.lower(): a.family for a in profile.assets.all() if a.family}

    def families_for(signals, extra_names=()):
        names = set(n.lower() for n in extra_names)
        for score in ClientSignalScore.objects.filter(
            organization=organization, signal__in=signals
        ):
            names |= {m["name"].lower() for m in (score.matches or [])}
        return {by_name[n] for n in names if n in by_name}

    featured = families_for(
        [d.signal for d in developments],
        [p for d in developments for p in d.recommendation.products],
    )
    watching = families_for(watched) - featured

    grouped: OrderedDict[str, list[str]] = OrderedDict()
    for asset in sorted(products, key=lambda a: a.family):
        grouped.setdefault(asset.family, []).append(asset.name)
    lines = ["| Family | Listings | This period | Listings include |"]
    for family, names in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        status = (
            "Featured story" if family in featured
            else "Watch item" if family in watching else "Monitoring"
        )
        shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
        lines.append(f"| {_cell(family)} | {len(names)} | {status} | {_cell(shown)} |")
    return {
        "heading": "The full portfolio",
        "text": (
            f"Each of the {len(products)} listings is assigned to one listening family. "
            f"Counts include different strengths, formats and multipacks; they are not "
            f"counts of unique ingredients. Families without a story this period stay in "
            f"the listening scope: product coverage and trend evidence are different "
            f"things.\n\n" + "\n".join(lines)
        ),
    }


def _development(organization, development: Development, sources) -> dict:
    signal, rec = development.signal, development.recommendation
    supporting, dissent = _evidence(signal, sources)

    parts = [
        f"Status. {STATUS.get(signal.maturity, STATUS[Signal.Maturity.WATCH])}",
        f"What changed. {signal.statement}",
    ]
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
    for link in signal.observations.select_related("observation").order_by("added_at"):
        line = observation_line(link.observation, sources)
        (dissent if link.role == SignalEvidence.Role.CONTRADICTS else supporting).append(line)
    if supporting:
        parts.append("The evidence:\n" + "\n".join(f"· {line}" for line in supporting))
    if dissent:
        parts.append(
            "Where sources disagree:\n" + "\n".join(f"· {line}" for line in dissent)
        )
    ai = benchmark.brief_lines(organization, rec.benchmark_questions)
    if ai:
        parts.append("What AI answers show today:\n" + "\n".join(f"· {line}" for line in ai))
    parts.extend(_content_plan(rec))
    return {"heading": signal.title, "text": "\n\n".join(parts)}


#: The content package's pieces, by section heading prefix, as the brief names them.
PACKAGE_PIECES = (
    ("Article draft", "an article"),
    ("Product-page FAQ drafts", "product-page FAQs"),
    ("Short video script", "a short video"),
    ("Email draft", "an email"),
    ("Social posts", "social posts"),
    ("AI questions to win", "answers to the questions AI assistants are asked"),
)


def _content_plan(rec: Recommendation) -> list[str]:
    """The story's content package, if one has been drafted: who should write
    it, the plan, every way to use it, and the FAQ drafts. Says which version."""
    output = (
        Output.objects.filter(recommendation=rec, type=OutputType.CONTENT_BRIEF)
        .order_by("-updated_at").first()
    )
    version = output.versions.order_by("-number").first() if output else None
    if version is None:
        return []
    sections = {s["heading"].split(":")[0]: s["text"] for s in version.body}
    title = re.search(r"Working title: (.+)", sections.get("The brief", ""))
    state = (
        "approved" if version.state in (OutputState.APPROVED, OutputState.PUBLISHED)
        else "a draft, not yet approved"
    )
    parts = [
        f"The content plan. From content package v{version.number} ({state})"
        + (f": “{title.group(1).strip()}”." if title else ".")
    ]
    specialty = sections.get("Suggested Science Advisory Board specialty", "").strip()
    if specialty:
        parts.append(f"Suggested Science Advisory Board specialty. {specialty}")
    outline = [
        re.sub(r"\s*\((draws on [^)]*|needs a source before drafting)\)$", "", line)
        for line in sections.get("Outline", "").splitlines() if line.strip()
    ]
    if outline:
        parts.append("Article plan:\n" + "\n".join(outline))
    pieces = [label for prefix, label in PACKAGE_PIECES if prefix in sections]
    if pieces:
        parts.append(
            "All the ways to use it: " + ", ".join(pieces[:-1])
            + (f" and {pieces[-1]}" if len(pieces) > 1 else pieces[0]) + "."
        )
    if "Product-page FAQ drafts" in sections:
        parts.append(sections["Product-page FAQ drafts"])
    return parts


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


def _watched(organization, *, exclude: set[int]) -> list[Signal]:
    return list(
        Signal.objects.filter(state=Signal.State.WATCHING, is_illustrative=False)
        .exclude(pk__in=exclude)
        .filter(
            pk__in=ClientSignalScore.objects.filter(
                organization=organization, fit_score__gt=0
            ).values("signal_id")
        )
        .order_by("-domain_score", "code")[:6]
    )


def _watch_list(watched: list[Signal]) -> dict | None:
    if not watched:
        return None
    return {
        "heading": "Watch list",
        "text": "Not yet strong enough to act on, but relevant and being followed:\n"
        + "\n".join(f"· {s.title}: {_first_sentence(s.statement)}" for s in watched),
    }


def _method(organization, developments, sources: OrderedDict, profiles: set[str]) -> dict:
    items = [obj for (kind, _), obj in sources.items() if kind == "item"]
    observed = [obj for (kind, _), obj in sources.items() if kind == "obs"]
    creators = {(i.creator or i.source.name).strip().lower() for i in items}
    by_type: OrderedDict[str, int] = OrderedDict()
    for o in observed:
        by_type[o.get_source_type_display()] = by_type.get(o.get_source_type_display(), 0) + 1
    observed_line = (
        "; ".join(f"{n} {kind}" for kind, n in by_type.items()) if by_type else "none"
    )
    return {
        "heading": "Coverage and method",
        "text": (
            (
                f"Collected automatically: {len(items)} podcast and video item(s) from "
                f"{len(creators)} creator(s), transcribed and quoted with the moment each "
                f"line was said. Paid sponsor segments are identified and excluded, and the "
                f"same conversation published twice (a podcast and its video, or a clip) "
                f"counts once.\n\n"
                if items else
                "No automatically collected item is cited in this brief: its stories were "
                "started by an analyst from the examples below.\n\n"
            )
            + f"Entered by hand, with their links, dates and labels: {observed_line}. "
            f"These are targeted examples, not a representative sample. Figures keep "
            f"their kind: a search estimate is a rolling monthly average, Shop counts are "
            f"global lifetime totals, engagement is cumulative.\n\n"
            f"Each story was reviewed and approved by a Pure Play analyst, as was every "
            f"recommendation to {organization.name}, against {_possessive(organization.name)} "
            f"profile {', '.join(sorted(profiles)) or '(unversioned)'}. Expert approval, "
            f"publication and measurement are separate steps that follow."
        ),
    }


def _sources(sources: OrderedDict) -> dict:
    lines = []
    for n, ((kind, _), obj) in enumerate(sources.items(), start=1):
        if kind == "item":
            lines.append(f"· [{n}] {_source_line(obj)}")
        else:
            note = "; ".join(p for p in [", ".join(obj.labels), obj.limitations.strip()] if p)
            lines.append(
                f"· [{n}] "
                + (f"{obj.published_at:%-d %b %Y}" if obj.published_at
                   else f"checked {obj.observed_at:%-d %b %Y}")
                + " — "
                + (f"[{_label(obj.title)}]({obj.url})" if obj.url
                   else f"{_label(obj.title)} (no public link; {obj.provenance})")
                + f" — {obj.get_source_type_display()}"
                + (f". {note}" if note else "")
            )
    return {
        "heading": "Sources",
        "text": "Links are clickable. Dates are publication dates where known; otherwise "
        "the date the page was checked.\n" + "\n".join(lines),
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


def _cite(sources: OrderedDict, key: tuple, obj) -> int:
    sources.setdefault(key, obj)
    return list(sources).index(key) + 1


def quote_line(claim: Claim, sources: OrderedDict | None = None) -> str:
    """One quoted claim. Given a registry, it is cited by its number in it."""
    item = claim.content_item
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
    number = f" [{_cite(sources, ('item', item.pk), item)}]" if sources is not None else ""
    return f"“{quote}” — {who}, {where}{at}.{cited}{number}"


def observation_line(o, sources: OrderedDict | None = None) -> str:
    """A hand-entered observation, readable by a client and fully traceable.

    The figure is always named by its kind — "global lifetime items sold" is
    never allowed to read as recent sales, nor a search estimate as a count of
    buyers — and every label travels with it.
    """
    title = _label(o.title)
    parts = [f"[{title}]({o.url})" if o.url else f"{title} (no public link; {o.provenance})",
             o.get_source_type_display()]
    if o.published_at:
        parts.append(f"published {o.published_at:%-d %b %Y}")
    parts.append(f"observed {o.observed_at:%-d %b %Y}")
    line = "; ".join(parts) + "."
    if o.excerpt:
        line = f"{o.excerpt.strip().rstrip('.')}. " + line
    if o.metric_kind:
        figure = o.metric_label or (f"{o.metric_value:,.0f}" if o.metric_value is not None else "")
        line += f" {figure} ({o.get_metric_kind_display().lower()}"
        if o.query:
            line += f"; query “{o.query}”, {o.provider}, {o.geography}, {o.period}"
        line += ")."
    if o.labels:
        line += f" Labelled: {', '.join(o.labels)}."
    if o.limitations:
        line += f" {o.limitations.strip().rstrip('.')}."
    if sources is not None:
        line += f" [{_cite(sources, ('obs', o.pk), o)}]"
    return line


def _source_line(item: ContentItem) -> str:
    who = item.creator or item.source.name
    when = item.published_at or item.discovered_at
    title = _label(item.title)
    named = f"[{title}]({item.url})" if item.url else title
    return f"{named} — {who}" + (f", {when:%-d %b %Y}" if when else "")


def _strength(signal: Signal) -> str:
    if signal.origin == Signal.Origin.ANALYST:
        observed = [link.observation for link in signal.observations.filter(role="supports")]
        events = len({o.independence_key for o in observed
                      if o.source_type not in ("catalogue", "research")})
        kinds = sorted({o.get_source_type_display() for o in observed})
        return (
            f"Evidence strength: an analyst-led story from {len(observed)} observation(s), "
            f"{events} independent event(s), across {', '.join(kinds) or 'no channel'}. "
            f"Targeted examples, not a measured trend."
        )
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

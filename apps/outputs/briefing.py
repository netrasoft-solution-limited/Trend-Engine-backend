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
from apps.enrichment.models import CORRECTION_NOTE, Claim
from apps.evidence.models import ContentItem, TranscriptArtifact
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


def status_counts(signal: Signal) -> str:
    """The measurement behind a status, with its periods (Mark, 15 Nov).

    The current week is compared with the average of the four weeks before it,
    counted in distinct independent items (promotional ones excluded), from the
    last time the development was scored. Said for every status, so "measured
    rising" is shown, not asserted, and a recurring discussion shows why it is
    not one.
    """
    from datetime import timedelta

    baseline = signal.baseline or {}
    weekly = list(baseline.get("weekly_items") or [])
    scored = signal.scored_at
    if len(weekly) < 5 or scored is None:
        return "No weekly measurement yet."
    end = scored.date() if hasattr(scored, "date") else scored
    current_from = end - timedelta(days=6)
    base_from, base_to = end - timedelta(days=34), end - timedelta(days=7)
    current, base = weekly[0], sum(weekly[1:5]) / 4
    change = (
        f"{(current - base) / base:+.0%}" if base else ("new this week" if current else "none")
    )
    streak = int(baseline.get("covered_streak") or 0)
    text = (
        f"Current week ({current_from:%-d %b}–{end:%-d %b}): {current} item(s). Baseline "
        f"({base_from:%-d %b}–{base_to:%-d %b}): {base:.1f} a week, from "
        f"{', '.join(str(n) for n in reversed(weekly[1:5]))} item(s) in its four weeks. "
        f"Change: {change}."
    )
    if streak < 5 and signal.maturity != Signal.Maturity.MEASURED_TREND:
        text += (
            f" Not measured as a trend: {streak} of the 5 adequately covered weeks a "
            f"baseline needs."
        )
    return text


def status_sentence(signal: Signal) -> str:
    return f"{STATUS.get(signal.maturity, STATUS[Signal.Maturity.WATCH])} {status_counts(signal)}"


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
    # Strongest standing first, then the freshest evidence, then rank: a watch
    # item built from early-October examples no longer leads a recurring
    # discussion with this week's sources (Mark, 30 Oct).
    developments.sort(key=lambda d: (
        STANDING.get(d.signal.maturity, len(STANDING)),
        -(latest_evidence(d.signal).toordinal() if latest_evidence(d.signal) else 0),
        -(d.rank or 0),
        d.signal.code,
    ))
    return developments


#: How a development's status orders it in a brief.
STANDING = {
    Signal.Maturity.MEASURED_TREND: 0,
    Signal.Maturity.RECURRING: 1,
    Signal.Maturity.WATCH: 2,
}


def latest_evidence(signal: Signal):
    """The newest publication date among a development's supporting sources."""
    dates = [
        _item_date(row.claim.content_item)
        for row in SignalEvidence.objects.filter(
            signal=signal, role=SignalEvidence.Role.SUPPORTS, claim__is_sponsored=False
        ).select_related("claim__content_item")
    ] + [
        link.observation.published_at
        for link in signal.observations.filter(role="supports").select_related("observation")
        if link.observation.source_type not in ("catalogue", "research")
    ]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


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

    _refuse_status_conflicts(developments)

    profiles = {d.recommendation.profile.citation for d in developments if d.recommendation.profile}
    #: Every source cited, in first-cited order; the key says what kind it is.
    sources: OrderedDict[tuple, object] = OrderedDict()

    body = [_development(organization, d, sources) for d in developments]
    leads = [section.pop("lead", []) for section in body]
    watched = _watched(organization, exclude={d.signal.pk for d in developments})
    watch = _watch_list(watched)
    family_map = _family_map(organization, developments, watched)

    # Mark, 15 Nov: "a concise front section showing what changed, the
    # strongest evidence, the Jarrow product or portfolio gap, and the
    # recommended action. Keep detailed evidence in an appendix."
    appendix = [
        {
            "heading": "Appendix A: Evidence by story",
            "text": "Each story in full: its status and measurement, what changed, why it "
            "matters, the recommended steps, what the evidence does not prove, and every "
            "quoted source with its date.",
        },
        *[{**section, "heading": f"A{n}. {section['heading']}"}
          for n, section in enumerate(body, start=1)],
        *([{**family_map, "heading": "Appendix B: The full portfolio"}] if family_map else []),
        {**_method(organization, developments, sources, profiles),
         "heading": "Appendix C: Coverage and method"},
        {**_sources(sources), "heading": "Appendix D: Sources"},
    ]
    sections = [
        _about(organization, developments, sources),
        _decision_summary(organization, developments, leads, watched, sources),
        _what_changed(organization, developments),
        _next_steps(developments),
        *([watch] if watch else []),
        *appendix,
    ]
    return Brief(sections, ", ".join(sorted(profiles)), len(developments))


def _refuse_status_conflicts(developments: list[Development]) -> None:
    """A story's words must agree with its status (Mark, 15 Nov: NMN and D3/K2
    had a recurring-discussion status and narrative calling them rising).
    Checked against the status today, since rescoring can change it after the
    text was approved."""
    from apps.scoring.recommendations import status_conflicts

    problems = []
    for d in developments:
        rec = d.recommendation
        content = {
            "why_it_matters": f"{rec.why_it_matters} {d.signal.statement}",
            "limits": rec.limits,
            "actions": rec.actions,
        }
        found = status_conflicts(content, d.signal.maturity)
        if found:
            problems.append(
                f"{d.signal.code} ({d.signal.get_maturity_display().lower()}) says "
                + ", ".join(repr(f) for f in found)
            )
    if problems:
        raise DraftingError(
            "Some approved text calls a development rising that is not measured as a "
            "rising trend. Edit or redraft it, then generate again: " + "; ".join(problems) + "."
        )


def _decision_summary(organization, developments, leads, watched, sources) -> dict:
    """One block per story: what changed, the best evidence, the product or
    gap, the action. Everything here is approved text or a cited source."""
    profile = client_services.current_for(organization)
    listings = (
        profile.assets.exclude(kind=ClientAsset.Kind.CATEGORY).count() if profile else 0
    )
    counts = [
        f"{len(developments)} stor{'y' if len(developments) == 1 else 'ies'}"
        + (f" plus {len(watched)} watch item{'s' if len(watched) != 1 else ''}" if watched else ""),
        f"{listings} listings mapped" if listings else "",
        f"{len(sources)} dated, linked sources",
    ]
    blocks = []
    for n, (d, lead) in enumerate(zip(developments, leads, strict=True), start=1):
        rec = d.recommendation
        action = (
            client_facing(rec.actions[0]["action"]) if rec.actions
            else _first_sentence(client_facing(rec.why_it_matters))
        )
        products = ", ".join(rec.products) or "No current listing named; see Appendix A."
        lines = [
            f"{n}. {d.signal.title}",
            f"· Status: {d.signal.get_maturity_display()}. {status_counts(d.signal)}",
            f"· What changed: {_first_sentence(d.signal.statement)}",
            f"· Strongest evidence: {lead[0] if lead else 'see Appendix A.'}",
            f"· {organization.name} products: {products}",
            f"· Recommended action: {action}",
        ]
        blocks.append("\n".join(lines))
    return {
        "heading": "Decision summary",
        "text": f"{' · '.join(c for c in counts if c)}. Full evidence for each story is in "
        f"Appendix A; sources are in Appendix D.\n\n" + "\n\n".join(blocks),
    }


#: Headings that are not stories, in this and earlier brief layouts.
NOT_STORIES = {
    "About this brief", "Executive summary", "Decision summary", "The full portfolio",
    "Next 14 days", "Watch list", "Coverage and method", "Sources",
    "What changed since the last brief",
}


def last_sent_version(organization, type_):
    """The newest version of this kind that was rendered and sent out: the one
    the reader has, so "what changed" is measured against it."""
    from .models import OutputVersion

    return (
        OutputVersion.objects.filter(
            organization=organization, output__type=type_, exports__isnull=False
        ).order_by("-created_at").first()
    )


def _previous_stories(organization) -> tuple[dict[str, str], str]:
    """Story title → status line from the last trend brief sent, and when."""
    version = last_sent_version(organization, OutputType.TREND_BRIEF)
    if version is None:
        return {}, ""
    stories = {}
    for section in version.body or []:
        heading = re.sub(r"^A\d+\.\s+", "", section.get("heading") or "")
        if heading in NOT_STORIES or heading.startswith("Appendix"):
            continue
        status = re.search(r"Status\. ([^:.]+)", section.get("text") or "")
        stories[heading] = status.group(1).strip() if status else ""
    return stories, f"{version.created_at:%-d %B %Y, %H:%M} UTC"


def _what_changed(organization, developments) -> dict:
    before, when = _previous_stories(organization)
    if not before:
        return {"heading": "What changed since the last brief",
                "text": "This is the first trend brief for this client."}
    now = {d.signal.title: d.signal.get_maturity_display() for d in developments}
    new = [t for t in now if t not in before]
    dropped = [t for t in before if t not in now]
    moved = [f"{t}: {before[t]} → {now[t]}" for t in now
             if t in before and before[t] and before[t] != now[t]]
    lines = [f"Compared with the brief last sent for review ({when}):"]
    lines.append("· New stories: " + ("; ".join(new) if new else "none") + ".")
    lines.append("· Carried over: " + ("; ".join(t for t in now if t in before) or "none") + ".")
    lines.append("· No longer featured: " + ("; ".join(dropped) if dropped else "none") + ".")
    lines.append("· Status changes: " + ("; ".join(moved) if moved else "none") + ".")
    return {"heading": "What changed since the last brief", "text": "\n".join(lines)}


# ── Sections ────────────────────────────────────────────────────────────────


#: The rolling research window every brief states (Mark, 24 Oct). Collected
#: material published before it is labelled older and kept out of the window.
WINDOW_DAYS = 30


def _item_date(item):
    when = item.published_at or item.discovered_at
    return when.date() if hasattr(when, "date") else when


def _today():
    from django.utils import timezone

    return timezone.localdate()


def is_older(item) -> bool:
    from datetime import timedelta

    from django.utils import timezone

    when = _item_date(item)
    return bool(when) and when < timezone.localdate() - timedelta(days=WINDOW_DAYS)


def window_start():
    return research_window()[0]


def observation_is_older(o) -> bool:
    """Older than the window, judged by publication date when the brief is built.

    The stored `is_old_material` flag is set when an observation is entered,
    against the window of that day, and goes stale: 23 of 84 sources in the
    15 Nov opportunity memo predated the window and several carried no label
    (Mark, 15 Nov). The date decides; the flag can only add.
    """
    return bool(o.is_old_material or (o.published_at and o.published_at < window_start()))


def observation_is_promotional(o) -> bool:
    return bool(o.is_ad or o.is_brand_content)


#: Hand-entered kinds that describe a product or a study, not a conversation:
#: undated is normal for them and they are context, not corroboration.
REFERENCE_TYPES = ("catalogue", "research")


def _window(sources: OrderedDict) -> tuple:
    dates = []
    for (kind, _), obj in sources.items():
        if kind == "item":
            if not is_older(obj):
                dates.append(_item_date(obj))
        elif obj.published_at and not observation_is_older(obj):
            # The day something was checked is not when anyone said it, so an
            # undated product page or fact sheet never stretches the window, and
            # older material is background, not part of it.
            dates.append(obj.published_at)
    dates = [d for d in dates if d]
    return (min(dates), max(dates)) if dates else (None, None)


def research_window() -> tuple:
    """The rolling window every brief states, ending today."""
    from datetime import timedelta

    end = _today()
    return end - timedelta(days=WINDOW_DAYS), end


def window_sentence(sources: OrderedDict) -> str:
    """One wording for the research window, used in every place a brief dates
    itself. Brief 7 said "6 to 28 October" in one section and "the rolling 30
    days to 30 October" in another (Mark, 30 Oct): one is the window, the other
    is when the cited sources happened to be published, and both are now said
    together, plainly."""
    start, end = research_window()
    first, last = _window(sources)
    window = (
        f"Research window: {start:%-d %B}{f' {start.year}' if start.year != end.year else ''}"
        f" to {end:%-d %B %Y} (the rolling {WINDOW_DAYS} days to the date of this brief)."
    )
    if first and last:
        published = (
            f"on {first:%-d %B %Y}" if first == last else
            f"between {first:%-d %B}{f' {first.year}' if first.year != last.year else ''} "
            f"and {last:%-d %B %Y}"
        )
        window += f" The sources cited inside it were published {published}."
    return window + " Anything published earlier is labelled older material where it appears."


def _about(organization, developments, sources) -> dict:
    measured = any(d.signal.maturity == Signal.Maturity.MEASURED_TREND for d in developments)
    caution = (
        "Developments marked as measured rising trends are measured; every other "
        "development is a recurring discussion or a watch item, and says so."
        if measured else
        "These are recurring discussions and watch items, not measured growth: we have "
        "not measured market growth or shown that any topic is accelerating."
    )
    return {
        "heading": "About this brief",
        "text": (
            f"This brief reviews what people discussed in podcasts, YouTube, TikTok, "
            f"Instagram and Reddit, and where the {organization.name} range fits those "
            f"conversations. {window_sentence(sources)}\n\n"
            f"{caution} Every claim is linked to a dated source, listed in full at the "
            f"end.\n\n{REVIEW_STATUS}"
        ),
    }


#: Said once, the same way, wherever a brief describes its own review status.
#: True when the draft is generated; a later sign-off is recorded on the
#: output and stated in the email that carries it.
REVIEW_STATUS = (
    "Review status: a draft prepared by Pure Play. No medical, scientific or legal "
    "expert has reviewed it, and nothing in it is an approved claim."
)


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
        f"Status. {status_sentence(signal)}",
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
    brand = []
    for link in signal.observations.select_related("observation").order_by("added_at"):
        line = observation_line(link.observation, sources)
        if observation_is_promotional(link.observation):
            # Competitor and brand marketing: worth knowing, never evidence of
            # independent interest (Mark, 15 Nov).
            brand.append(line)
        else:
            (dissent if link.role == SignalEvidence.Role.CONTRADICTS else supporting).append(line)
    if supporting:
        parts.append("The evidence:\n" + "\n".join(f"· {line}" for line in supporting))
    if dissent:
        parts.append(
            "Where sources disagree:\n" + "\n".join(f"· {line}" for line in dissent)
        )
    if brand:
        parts.append(
            "Brand and competitor activity (not counted as independent evidence):\n"
            + "\n".join(f"· {line}" for line in brand)
        )
    ai = benchmark.brief_lines(organization, rec.benchmark_questions)
    if ai:
        parts.append("What AI answers show today:\n" + "\n".join(f"· {line}" for line in ai))
    parts.extend(_content_plan(rec))
    return {"heading": signal.title, "text": "\n\n".join(parts), "lead": supporting[:1]}


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
            f"{window_sentence(sources)}\n\n"
            + (
                f"Collected automatically: {len(items)} item(s) ({_by_platform(items)}) from "
                f"{len(creators)} creator(s). Podcasts and videos are transcribed and quoted "
                f"with the moment each line was said; Reddit posts are quoted as written. "
                f"Paid sponsor segments and posts marked as promotional are identified and "
                f"excluded, and the same conversation or post found twice counts once.\n\n"
                if items else
                "No automatically collected item is cited in this brief: its stories were "
                "started by an analyst from the examples below.\n\n"
            )
            + f"{podcast_coverage(items)}\n\n"
            + f"{coverage_gaps(items)}\n\n"
            + f"Entered by hand, with their links, dates and labels: {observed_line}. "
            f"These are targeted examples, not a representative sample. Figures keep "
            f"their kind: a search estimate is a rolling monthly average, Shop counts are "
            f"global lifetime totals, engagement is cumulative.\n\n"
            f"{_approvals(organization, developments, profiles)} Expert review, publication "
            f"and measurement are separate steps; see Review status above."
        ),
    }


#: The channels a brief should be able to draw on, in the order they are named.
CHANNELS = ("podcast", "youtube", "tiktok", "instagram", "reddit")


def _by_platform(items) -> str:
    counts: OrderedDict[str, int] = OrderedDict((c, 0) for c in CHANNELS)
    for item in items:
        counts[item.platform] = counts.get(item.platform, 0) + 1
    labels = {"podcast": "podcast", "youtube": "YouTube", "tiktok": "TikTok",
              "instagram": "Instagram", "reddit": "Reddit"}
    return ", ".join(f"{n} {labels.get(p, p)}" for p, n in counts.items() if n)


def podcast_coverage(cited_items) -> str:
    """What podcast collection found in the window and what of it this brief
    uses, from the records (Mark, 15 Nov: show that fresh podcast collection
    feeds detection and the briefs, or report honestly that it did not)."""
    from django.db.models import Count

    start, end = research_window()
    episodes = ContentItem.objects.filter(
        source__route="podcast", published_at__date__gte=start, published_at__date__lte=end
    )
    total = episodes.count()
    if not total:
        return ("Podcasts: no episode published in the window was collected from the "
                "podcast sources followed.")
    shows = episodes.values("source").distinct().count()
    relevant = episodes.filter(gate_decision="relevant").count()
    transcribed = episodes.filter(content_state="transcribed").count()
    claims = Claim.objects.filter(content_item__in=episodes)
    sponsored = claims.filter(is_sponsored=True).count()
    in_signals = (
        claims.filter(is_sponsored=False, signal_evidence__isnull=False)
        .aggregate(n=Count("content_item", distinct=True))["n"]
    )
    cited = [i for i in cited_items if i.platform == "podcast"]
    used = (
        f"{len(cited)} episode(s) are quoted in this brief."
        if cited else
        "None is quoted in this brief: no approved story drew on a current podcast claim."
    )
    return (
        f"Podcasts: {total} episode(s) published in the window from {shows} show(s) were "
        f"collected; {relevant} passed the relevance check and {transcribed} were transcribed. "
        f"They produced {claims.count()} claim(s), of which {sponsored} came from sponsor "
        f"reads or promotion and are excluded; claims from {in_signals} episode(s) were "
        f"grouped into developments. {used}"
    )


def coverage_gaps(items) -> str:
    """What this brief cannot see, stated rather than left to be inferred."""
    present = {item.platform for item in items}
    labels = {"podcast": "podcasts", "youtube": "YouTube", "tiktok": "TikTok",
              "instagram": "Instagram", "reddit": "Reddit"}
    missing = [labels[c] for c in CHANNELS if c not in present]
    lines = []
    if missing:
        names = (
            missing[0] if len(missing) == 1
            else f"{', '.join(missing[:-1])} or {missing[-1]}"
        )
        lines.append(f"Coverage gaps: no collected item from {names} is cited here.")
    if "instagram" in present:
        lines.append("Instagram Reels are read from captions and comments only; their speech "
                     "is not transcribed.")
    if "reddit" in present:
        lines.append("Reddit's public feed carries no score or comment count, so none is shown.")
    lines.append(SEARCH_LIMITS)
    return " ".join(lines)


#: Said wherever a brief describes what search evidence it can and cannot have.
SEARCH_LIMITS = (
    "Monthly keyword volumes need a paid keyword tool, which is not in use; any volume "
    "estimate shown keeps the date it was retrieved and is not fresh research. Google Trends "
    "has no official API and refuses automated requests from our server, so Trends readings "
    "come from Google's own export, with the exact periods compared. TikTok Shop sales are "
    "not collected."
)


def _approvals(organization, developments, profiles) -> str:
    """What was approved inside Pure Play, and when — from the records.

    Dates only: the approver's label is free text typed into the review
    screen ("Codex workflow test for Mark…") and is an internal note, not
    something to print in a client document. The audit trail keeps who.
    """
    lines = []
    for d in developments:
        when = d.recommendation.decided_at
        lines.append(f"{d.signal.title} (approved {when:%-d %B %Y})" if when else d.signal.title)
    profile = ", ".join(sorted(profiles)) or "(unversioned)"
    return (
        f"Each story and its recommendation were approved in Pure Play's internal review "
        f"against {_possessive(organization.name)} profile {profile}: " + "; ".join(lines) + "."
    )


def _sources(sources: OrderedDict) -> dict:
    lines = []
    for n, ((kind, _), obj) in enumerate(sources.items(), start=1):
        if kind == "item":
            lines.append(f"· [{n}] {_source_line(obj)}")
        else:
            labels = list(obj.labels)
            if (obj.published_at and obj.published_at < window_start()
                    and not obj.is_old_material and obj.source_type not in REFERENCE_TYPES):
                labels.append("older material")
            note = "; ".join(p for p in [", ".join(labels), own_words(obj.limitations)] if p)
            lines.append(
                f"· [{n}] "
                + (f"{obj.published_at:%-d %b %Y}" if obj.published_at
                   else f"checked {obj.observed_at:%-d %b %Y}")
                + " — "
                + (f"[{_label(obj.title)}]({obj.url})" if obj.url
                   else f"{_label(obj.title)} (no public link; "
                   f"{own_words(obj.provenance, keep_as='source')})")
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
    dissent = [
        quote_line(row.claim, sources)
        for row in rows if row.role == SignalEvidence.Role.CONTRADICTS
    ][:DISSENT_PER_DEVELOPMENT]
    backing = [row.claim for row in rows if row.role != SignalEvidence.Role.CONTRADICTS]
    # Current material before older; then one quote per channel before a second
    # from any, so a podcast is not crowded out by five newer TikToks (Mark,
    # 15 Nov); and one quote per item, so five quotes are five voices.
    backing.sort(key=lambda c: is_older(c.content_item))
    chosen, seen_items, seen_channels = [], set(), set()
    for claim in backing:
        channel = claim.content_item.platform
        if claim.content_item_id in seen_items or channel in seen_channels:
            continue
        seen_items.add(claim.content_item_id)
        seen_channels.add(channel)
        chosen.append(claim)
    for claim in backing:
        if len(chosen) >= EVIDENCE_PER_DEVELOPMENT:
            break
        if claim.content_item_id not in seen_items:
            seen_items.add(claim.content_item_id)
            chosen.append(claim)
    supporting = [quote_line(c, sources) for c in chosen[:EVIDENCE_PER_DEVELOPMENT]]
    return supporting, dissent


def _cite(sources: OrderedDict, key: tuple, obj) -> int:
    sources.setdefault(key, obj)
    return list(sources).index(key) + 1


#: Text with no timings, so a quote from it carries no timestamp.
UNTIMED = {
    TranscriptArtifact.Method.POST_TEXT,
    TranscriptArtifact.Method.VIRLO,
    TranscriptArtifact.Method.CAPTION_COMMENTS,
}


def quote_line(claim: Claim, sources: OrderedDict | None = None) -> str:
    """One quoted claim. Given a registry, it is cited by its number in it."""
    item = claim.content_item
    seconds = claim.segment.start_seconds if claim.segment_id else None
    if getattr(getattr(item, "transcript", None), "method", "") in UNTIMED:
        # A post, a caption or a Virlo transcript has no timings: "at 0:00"
        # would point the reader at a moment nobody measured.
        seconds = None
    quote = claim.shown_quote.strip()
    if len(quote) > QUOTE_CHARS:
        quote = quote[: QUOTE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    who = item.creator or item.source.name
    title = _label(item.title)
    link = item.link_at(seconds)
    where = f"[{title}]({link})" if link else f"“{title}”"
    at = f" at {_timestamp(seconds)}" if seconds is not None else ""
    cited = " The speaker cites a study." if claim.is_cited else ""
    if claim.quote_corrections:
        cited += f" {CORRECTION_NOTE}"
    older = (
        f" Older material: published {_item_date(item):%-d %b %Y}, before the "
        f"{WINDOW_DAYS}-day window." if is_older(item) else ""
    )
    number = f" [{_cite(sources, ('item', item.pk), item)}]" if sources is not None else ""
    return f"“{quote}” — {who}, {where}{at}.{cited}{older}{number}"


def own_words(text: str, *, keep_as: str = "") -> str:
    """Analyst text with another document's citation numbers taken out.

    Rows imported from Pure Play's October report carried that report's own
    numbers ("[25]", "Opportunity Brief, Oct 2026 [2]"), which then read as
    this brief's sources (Mark, 30 Oct). With `keep_as`, a number is kept in
    words ("source 2") — for provenance, where it locates the original.
    """
    text = text or ""
    if keep_as:
        return re.sub(r"\s*\[(\d+)\]", rf", {keep_as} \1", text)
    return re.sub(r"\s*\[\d+\]", "", text).strip()


def observation_line(o, sources: OrderedDict | None = None) -> str:
    """A hand-entered observation, readable by a client and fully traceable.

    The figure is always named by its kind — "global lifetime items sold" is
    never allowed to read as recent sales, nor a search estimate as a count of
    buyers — and every label travels with it.
    """
    title = _label(o.title)
    parts = [f"[{title}]({o.url})" if o.url
             else f"{title} (no public link; {own_words(o.provenance, keep_as='source')})",
             o.get_source_type_display()]
    if o.published_at:
        parts.append(f"published {o.published_at:%-d %b %Y}")
    parts.append(f"observed {o.observed_at:%-d %b %Y}")
    line = "; ".join(parts) + "."
    if o.excerpt:
        line = f"{own_words(o.excerpt).rstrip('.')}. " + line
    if o.metric_kind:
        figure = o.metric_label or (f"{o.metric_value:,.0f}" if o.metric_value is not None else "")
        line += f" {figure} ({o.get_metric_kind_display().lower()}"
        if o.query:
            line += f"; query “{o.query}”, {o.provider}, {o.geography}, {o.period}"
        line += ")."
    labels = list(o.labels)
    if (
        o.published_at and o.published_at < window_start() and not o.is_old_material
        and o.source_type not in REFERENCE_TYPES
    ):
        labels.append(f"older material (published before {window_start():%-d %b %Y})")
    if labels:
        line += f" Labelled: {', '.join(labels)}."
    if o.limitations:
        line += f" {own_words(o.limitations).rstrip('.')}."
    if sources is not None:
        line += f" [{_cite(sources, ('obs', o.pk), o)}]"
    return line


def _source_line(item: ContentItem) -> str:
    who = item.creator or item.source.name
    when = item.published_at or item.discovered_at
    title = _label(item.title)
    named = f"[{title}]({item.clean_url})" if item.url else title
    line = f"{named} — {who}" + (f", {when:%-d %b %Y}" if when else "")
    if item.platform in ("tiktok", "instagram", "reddit"):
        line += f" — {item.platform_label}"
    if getattr(getattr(item, "transcript", None), "method", "") == "caption_comments":
        # Mark, 31 Oct: never present unseen spoken claims as evidence.
        line += " (caption and comments only; speech not transcribed)"
    return line


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
            signal=signal, role=SignalEvidence.Role.SUPPORTS, claim__is_cited=True,
            claim__is_sponsored=False,
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

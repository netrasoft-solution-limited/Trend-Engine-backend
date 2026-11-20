"""The New Product Opportunity Brief, in the shape Mark delivered (20 Oct).

Built only from APPROVED candidates, in the analyst's priority order. The
sections follow his October brief: about, executive summary, what the evidence
supports, search demand, cross-channel corroboration, one section per
candidate, how to read the evidence, numbered sources.

Figure kinds are never mixed: search estimates, Trends interest, lifetime Shop
counts and cumulative engagement each keep their own column or label.
"""
from __future__ import annotations

import re
from collections import OrderedDict

from apps.evidence.models import Observation
from apps.scoring import opportunities
from apps.scoring.models import Candidate

from .briefing import (
    REFERENCE_TYPES,
    REVIEW_STATUS,
    SEARCH_LIMITS,
    Brief,
    _cell,
    _cite,
    _first_sentence,
    _sources,
    collection_outages,
    last_sent_version,
    observation_is_older,
    observation_is_promotional,
    observation_line,
    research_window,
    window_sentence,
)
from .services import DraftingError

RESEARCH_NOTE = (
    "This is commercial research for {client} product planning, not a launch "
    "recommendation. Claims for any new product would need {client} Scientific "
    "Affairs review."
)

#: Columns of the corroboration table, and the source types under each.
CHANNELS = OrderedDict([
    ("Podcast or video", {"podcast", "youtube"}),
    ("Instagram and TikTok", {"instagram", "tiktok", "tiktok_shop"}),
    ("Reddit and communities", {"reddit"}),
    ("Search", {"search_estimate", "google_trends"}),
])


#: A Trends reading counts as current research only if it was retrieved this
#: recently. The 15 Nov memo carried a comparison retrieved 20 Oct that ended
#: 17 Oct, presented as current (Mark, 15 Nov).
FRESH_SEARCH_DAYS = 14

SEARCH_TYPES = CHANNELS["Search"]

CURRENT, PROMOTIONAL, OLDER, UNDATED, REFERENCE, SEARCH = (
    "current", "promotional", "older", "undated", "reference", "search",
)


def evidence_class(o: Observation) -> str:
    """What an observation can count as, decided by its date when the brief is built."""
    if o.source_type in REFERENCE_TYPES:
        return REFERENCE
    if o.source_type in SEARCH_TYPES:
        return SEARCH
    if observation_is_promotional(o):
        return PROMOTIONAL
    if observation_is_older(o):
        return OLDER
    if not o.published_at:
        return UNDATED
    return CURRENT


def search_is_fresh(o: Observation) -> bool:
    """Only a Trends reading retrieved in the last FRESH_SEARCH_DAYS is current.
    Volume estimates are 12-month averages: background with their date, never
    fresh research."""
    from datetime import timedelta

    if o.metric_kind != Observation.MetricKind.TRENDS:
        return False
    return o.observed_at >= research_window()[1] - timedelta(days=FRESH_SEARCH_DAYS)


def event_key(o: Observation) -> str:
    """One key per independent event: the analyst's event, else the post itself."""
    return o.event_key.strip().lower() or o.media_key or f"observation-{o.pk}"


def channel_counts(c: Candidate) -> OrderedDict:
    """Current, independent events per channel, each event counted once across
    all channels (Mark, 15 Nov: deduplicate repeated stories and creator posts).
    Search counts a fresh Trends reading only."""
    counts: OrderedDict[str, dict] = OrderedDict(
        (ch, {"current": 0, "excluded": 0}) for ch in CHANNELS
    )
    seen: set[str] = set()
    for o in sorted(c.observations.all(), key=lambda o: (o.published_at or o.observed_at, o.pk),
                    reverse=True):
        channel = next((ch for ch, types in CHANNELS.items() if o.source_type in types), None)
        if channel is None:
            continue
        if channel == "Search":
            counts[channel]["current" if search_is_fresh(o) else "excluded"] += 1
            continue
        key = event_key(o)
        if evidence_class(o) != CURRENT:
            counts[channel]["excluded"] += 1
        elif key not in seen:
            seen.add(key)
            counts[channel]["current"] += 1
    return counts


def approved_candidates(organization) -> list[Candidate]:
    return list(
        Candidate.objects.filter(organization=organization, approved_at__isnull=False)
        .exclude(status=Candidate.Status.REJECTED)
        .prefetch_related("observations")
        .order_by("priority", "name")
    )


def sections_for(organization) -> Brief:
    candidates = approved_candidates(organization)
    if not candidates:
        waiting = Candidate.objects.filter(
            organization=organization, approved_at__isnull=True
        ).exclude(status=Candidate.Status.REJECTED).count()
        raise DraftingError(
            f"No candidate is approved for {organization.name}"
            + (f"; {waiting} candidate(s) are waiting for review." if waiting else ".")
        )

    sources: OrderedDict[tuple, object] = OrderedDict()
    note = RESEARCH_NOTE.format(client=organization.name)
    about = _about(organization, candidates, sources, note)
    body = [_candidate(organization, n, c, sources) for n, c in enumerate(candidates, start=1)]
    demand = _search_demand(candidates, sources)
    corroboration = _corroboration(candidates)
    profiles = {c.gap_check.get("profile") for c in candidates if c.gap_check.get("profile")}
    discovered = _discovered(organization)

    # Mark, 15 Nov: what changed, the strongest evidence, the gap and the
    # action up front; the detailed evidence in appendices.
    sections = [
        about,
        _summary(organization, candidates, note, sources),
        _what_changed(organization, candidates),
        *([discovered] if discovered else []),
        {"heading": "Appendix A: Evidence by candidate",
         "text": "Each candidate in full. Current evidence (published inside the research "
         "window, independent of any brand) is listed first and is the only evidence "
         "counted. Brand and promotional activity, older material and undated items "
         "follow, labelled, and are not counted."},
        *[{**section, "heading": f"A{n}. {section['heading']}"}
          for n, section in enumerate(body, start=1)],
        {**corroboration, "heading": "Appendix B: What is corroborated across channels"},
        {**demand, "heading": "Appendix C: Search demand"},
        {**_reading(), "heading": "Appendix D: How to read the evidence"},
        {**_split_sources(sources), "heading": "Appendix E: Sources"},
    ]
    return Brief(sections, ", ".join(sorted(profiles)), len(candidates))


def _cited(text: str, candidate: Candidate, sources) -> str:
    """The analyst's O-numbers become this brief's source numbers. A number the
    candidate does not carry is dropped rather than shown."""
    attached = {o.pk: o for o in candidate.observations.all()}

    def swap(match):
        o = attached.get(int(match.group(1)))
        return f"[{_cite(sources, ('obs', o.pk), o)}]" if o else ""

    text = re.sub(r"\(\s*(O\d+(?:\s*,\s*O\d+)*)\s*\)",
                  lambda m: re.sub(r"\s*,\s*", " ", m.group(1)), text or "")
    text = re.sub(r"\bO(\d+)\b", swap, text)
    return re.sub(r"\s+([.,;])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _names(candidates) -> str:
    names = [c.name for c in candidates]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _listings(candidates) -> int:
    return max((c.gap_check.get("listings", 0) for c in candidates), default=0)


def _about(organization, candidates, sources, note) -> dict:
    observed = [o for c in candidates for o in c.observations.all()]
    for o in observed:
        sources.setdefault(("obs", o.pk), o)
    return {
        "heading": "About this brief",
        "text": (
            f"This brief looks beyond the current {organization.name} range. We reviewed "
            f"{len(candidates)} ingredient(s) {organization.name} does not appear to sell "
            f"({_names(candidates)}) against podcast, video, social, Reddit and search "
            f"signals, and ranked them for commercial testing. {window_sentence(sources)}\n\n"
            f"The ranking is our commercial judgment within this review. It is not a "
            f"market-size ranking. Every observation is linked to a dated source, listed in "
            f"full at the end.\n\n{note}\n\n{REVIEW_STATUS}"
        ),
    }


def _summary(organization, candidates, note, sources) -> dict:
    lead = candidates[0]
    listings = _listings(candidates)
    absent = [c for c in candidates if c.gap_check and not c.gap_check.get("found")]
    lines = ["| # | Candidate | Status | Commercial opening | Recommended next step |"]
    for n, c in enumerate(candidates, start=1):
        lines.append(
            f"| {n} | {_cell(c.name)} | {c.get_status_display()} | "
            f"{_cell(_cited(c.opening, c, sources))} | "
            f"{_cell(c.next_step)} |"
        )
    pending = [c.name for c in candidates if c.internal_check == Candidate.InternalCheck.PENDING]
    gap = (
        f"{'None' if len(absent) == len(candidates) else f'{len(absent)}'} of the "
        f"{len(candidates)} was found in the titles, terms or notes of the {listings} "
        f"{organization.name} listings."
    )
    confirm = (
        f" {organization.name} should confirm retail-only products and its development "
        f"pipeline before treating {'these' if len(pending) > 1 else 'this'} as "
        f"portfolio gaps ({', '.join(pending)})." if pending else ""
    )
    start = (
        f"a {lead.name} {lead.get_test_type_display().lower()}" if lead.test_type else lead.name
    )
    step = lead.next_step.rstrip(".")
    step = step[:1].lower() + step[1:]
    if lead.status == Candidate.Status.MONITOR:
        # Nothing is ready to test, so there is no budget to ask for: "Start
        # with a Spermidine monitor" read as a product (Mark would ask which).
        recommendation = f"Keep {lead.name} on watch first; it is not ready to test yet."
        decision = f"Confirm the portfolio gaps and agree the monitoring step: {step}."
    else:
        recommendation = f"Start with {start}."
        decision = (
            f"Confirm the portfolio gaps and agree to {step}, with a budget and "
            f"success criteria."
        )
    blocks = []
    for n, c in enumerate(candidates, start=1):
        counts = channel_counts(c)
        present = [ch for ch, v in counts.items() if v["current"]]
        missing = [ch for ch, v in counts.items() if not v["current"]]
        best = _strongest(c)
        beside = f" It would sit beside {', '.join(c.adjacent)}." if c.adjacent else ""
        blocks.append("\n".join([
            f"{n}. {c.name}: {c.get_status_display().lower()}",
            "· Current evidence: "
            + (", ".join(f"{ch.lower()} {counts[ch]['current']}" for ch in present
                         if ch != "Search") or "no")
            + " independent event(s) in the window"
            + (f"; none in {', '.join(m.lower() for m in missing if m != 'Search')}"
               if [m for m in missing if m != "Search"] else "")
            + ("; a fresh Google Trends reading" if counts["Search"]["current"]
               else "; no fresh search reading")
            + ".",
            "· Strongest current evidence: "
            + (observation_line(best, sources) if best else "none inside the window."),
            f"· {organization.name} portfolio gap: {opportunities.gap_sentence(c)}{beside}",
            f"· Recommended action: {c.next_step or _first_sentence(c.test_plan)}",
        ]))
    return {
        "heading": "Decision summary",
        "text": (
            f"Our recommendation. {recommendation} "
            f"{_first_sentence(_cited(lead.why_it_matters, lead, sources)) or lead.opening}\n\n"
            f"{len(candidates)} candidate ingredient(s) reviewed · {len(absent)} of "
            f"{len(candidates)} not found in {listings} listings.\n\n"
            + "\n\n".join(blocks)
            + "\n\n" + "\n".join(lines)
            + f"\n\n{gap}{confirm}\n\n"
            f"The decision we need. {decision}\n\n{note}"
        ),
    }


def _strongest(c: Candidate) -> Observation | None:
    """The best current, independent observation: a podcast or video first,
    then the newest."""
    current = [o for o in c.observations.all() if evidence_class(o) == CURRENT]
    if not current:
        return None
    spoken = {"podcast", "youtube"}
    return sorted(
        current, key=lambda o: (o.source_type not in spoken, -o.published_at.toordinal())
    )[0]


def _what_changed(organization, candidates) -> dict:
    """Against the previous memo: new current evidence and status changes."""
    from apps.outputs.models import OutputType

    version = last_sent_version(organization, OutputType.PRODUCT_MEMO)
    if version is None:
        return {"heading": "What changed since the last memo",
                "text": "This is the first opportunity memo for this client."}
    since = version.created_at
    before = {}
    for section in version.body or []:
        found = re.match(r"(?:A\d+\. )?Candidate \d+: (.+?) — (.+)$", section.get("heading") or "")
        if found:
            before[found.group(1)] = found.group(2)
    lines = [f"Compared with the memo last sent for review ({since:%-d %B %Y, %H:%M} UTC):"]
    for c in candidates:
        added = [o for o in c.observations.all() if o.entered_at > since]
        fresh = [o for o in added if evidence_class(o) in (CURRENT, SEARCH)]
        status = c.get_status_display().lower()
        was = before.get(c.name)
        change = (
            "new in this memo" if was is None
            else f"status unchanged ({status})" if was == status
            else f"status {was} → {status}"
        )
        lines.append(
            f"· {c.name}: {change}; {len(fresh)} new current observation(s) or fresh search "
            f"reading(s) added since then."
        )
    dropped = [name for name in before if name not in {c.name for c in candidates}]
    if dropped:
        lines.append(f"· No longer included: {', '.join(dropped)}.")
    return {"heading": "What changed since the last memo", "text": "\n".join(lines)}


def _discovered(organization) -> dict | None:
    """Ingredients surfaced from the window's conversations that the catalogue
    lacks and no candidate covers (Mark, 15 Nov: show discovery beyond a fixed list)."""
    from apps.scoring import discovery

    found = [d for d in discovery.latest(organization) if d.status == d.Status.NEW]
    if not found:
        return None
    lines = ["| Ingredient | Independent items | Creators | Channels | Status |"]
    for d in found:
        lines.append(
            f"| {_cell(d.name)} | {d.items} | {d.creators} | {_cell(', '.join(d.channels))} | "
            f"{_cell(d.get_status_display())} |"
        )
    when = found[0].discovered_at
    return {
        "heading": "Other ingredients surfaced this window",
        "text": (
            f"Found automatically on {when:%-d %B %Y} from the window's collected, "
            f"non-promotional conversations: ingredients discussed that do not appear in the "
            f"{organization.name} catalogue and are not among the candidates above. Counts are "
            f"distinct items and creators. These are not yet assessed; one is added as a "
            f"candidate only after an analyst reviews it.\n\n" + "\n".join(lines)
        ),
    }


def _figure(o: Observation) -> str:
    value = o.metric_label or (f"{o.metric_value:,.0f}" if o.metric_value is not None else "")
    return value


def _search_demand(candidates, sources) -> dict:
    rows, trends = [], []
    for c in candidates:
        ordered = sorted(c.observations.all(), key=lambda o: -(o.metric_value or 0))
        for o in ordered:
            if o.metric_kind == Observation.MetricKind.SEARCH_ESTIMATE:
                n = list(sources).index(("obs", o.pk)) + 1
                rows.append(
                    f"| {_cell(c.name)} | {_cell(o.query)} | {_cell(_figure(o))} | "
                    f"{_cell(o.geography)} | {_cell(o.provider)}, {_cell(o.period)}"
                    + ("" if "retrieved" in o.period.lower()
                       else f"; retrieved {o.observed_at:%-d %b %Y}")
                    + f"; not refreshed [{n}] |"
                )
            elif o.metric_kind == Observation.MetricKind.TRENDS:
                state = (
                    "current" if search_is_fresh(o)
                    else f"not current: retrieved {o.observed_at:%-d %b %Y}"
                )
                trends.append(f"· {c.name} ({state}): {observation_line(o, sources)}")
    parts = []
    if rows:
        parts.append(
            "| Candidate | Query | Estimated searches per month | Geography | Source and period |\n"
            + "\n".join(rows)
        )
        parts.append(
            "These are estimated rolling monthly averages from third-party keyword data, kept "
            "with the date they were retrieved. They are not fresh research, not the past 30 "
            "days and not counts of buyers. Queries overlap and must not be added together."
        )
    if trends:
        parts.append("Relative search interest (Google Trends, not volumes):\n" + "\n".join(trends))
    without = [c.name for c in candidates
               if not any(o.source_type in SEARCH_TYPES for o in c.observations.all())]
    if without:
        parts.append(f"No search data of any kind for: {', '.join(without)}.")
    parts.append(SEARCH_LIMITS)
    parts.extend(collection_outages())
    return {"heading": "Search demand", "text": "\n\n".join(parts)}


def _corroboration(candidates) -> dict:
    """Channels with current, independent evidence, from `channel_counts`.

    "Every channel" includes Search: Urolithin A had no search data at all and
    was still said to have current evidence in every channel (Mark, 15 Nov).
    """
    lines = ["| Candidate | " + " | ".join(CHANNELS) + " |"]
    complete, partial = [], []
    for c in candidates:
        counts = channel_counts(c)
        cells = []
        for channel, v in counts.items():
            if channel == "Search":
                cell = (f"{v['current']} fresh Trends reading(s)" if v["current"]
                        else "No fresh reading")
                cell += f"; {v['excluded']} older or estimate(s)" if v["excluded"] else ""
            else:
                cell = (f"{v['current']} independent current event(s)" if v["current"]
                        else "None current")
                cell += f"; {v['excluded']} paid, brand, older or undated" if v["excluded"] else ""
            cells.append(cell)
        missing = [ch.lower() for ch, v in counts.items() if not v["current"]]
        if missing:
            partial.append(f"{c.name} (none current in {', '.join(missing)})")
        else:
            complete.append(c.name)
        lines.append(f"| {_cell(c.name)} | " + " | ".join(cells) + " |")
    closing = []
    if complete:
        closing.append(
            f"Current, independent evidence in every channel, search included: "
            f"{', '.join(complete)}."
        )
    if partial:
        closing.append("Not in every channel: " + "; ".join(partial) + ".")
    return {
        "heading": "What is corroborated across channels",
        "text": "\n".join(lines) + "\n\n"
        "Counts are independent events published inside the research window: the same story "
        "told in a podcast and its video, or two posts about one study, counts once across "
        "all channels, and so do one creator's repeated posts. Paid, brand, older and undated "
        "material is shown in Appendix A but does not count. Search counts only a Google "
        f"Trends reading retrieved in the last {FRESH_SEARCH_DAYS} days; volume estimates are "
        f"12-month averages and never count as current. " + " ".join(closing),
    }


def _candidate(organization, n: int, c: Candidate, sources) -> dict:
    parts = []
    beside = (
        f" It would sit beside {', '.join(c.adjacent)} in the {organization.name} range."
        if c.adjacent else ""
    )
    parts.append(
        f"The opportunity. {_cited(c.opening, c, sources)}{beside} {opportunities.gap_sentence(c)}"
    )
    if c.internal_check != Candidate.InternalCheck.PENDING:
        parts[-1] += f" {c.get_internal_check_display()}."
    observations = sorted(c.observations.all(), key=lambda o: (o.published_at or o.observed_at))
    start, end = research_window()
    groups = (
        (CURRENT, f"Current evidence ({start:%-d %b}–{end:%-d %b %Y}), counted"),
        (SEARCH, "Search readings (see Appendix C for what is current)"),
        (PROMOTIONAL, "Brand and promotional activity: competitor context, not counted"),
        (OLDER, f"Older context, published before {start:%-d %b %Y}: not counted"),
        (UNDATED, "Undated: not counted"),
        (REFERENCE, "Product and research references"),
    )
    for kind, title in groups:
        chosen = [o for o in observations if evidence_class(o) == kind]
        if chosen:
            parts.append(
                f"{title}:\n"
                + "\n".join(
                    f"· {o.get_source_type_display()}. {observation_line(o, sources)}"
                    for o in chosen
                )
            )
    if c.why_it_matters:
        parts.append(
            f"Why it could matter to {organization.name}. {_cited(c.why_it_matters, c, sources)}"
        )
    if c.test_plan:
        parts.append(f"What to test next. {_cited(c.test_plan, c, sources)}")
    if c.gate:
        parts.append(f"Gate. {_cited(c.gate, c, sources)}")
    if c.limits:
        parts.append(f"What this does not show. {_cited(c.limits, c, sources)}")
    return {"heading": f"Candidate {n}: {c.name} — {c.get_status_display().lower()}",
            "text": "\n\n".join(parts)}


def _reading() -> dict:
    return {
        "heading": "How to read the evidence",
        "text": (
            "· Search estimates are rolling monthly averages for one query, geography and "
            "provider. They are not recent momentum and are never added across queries.\n"
            "· Google Trends figures are relative interest on a 0–100 scale, not volumes.\n"
            "· TikTok Shop counts are global lifetime totals for one listing, not recent "
            "sales speed or category size.\n"
            "· Social engagement is cumulative from posting to the date observed.\n"
            "· Advertisements and brand content show response to marketing, not independent "
            "demand, and are labelled wherever they appear.\n"
            "· Monthly keyword volumes need a paid keyword tool, which is not in use; any "
            "estimate shown is labelled with the date it was retrieved and its source. Search "
            "interest is Google Trends relative interest, from Google's own export.\n"
            "· Instagram Reels are read from captions and comments only, never their speech. "
            "Reddit's public feed carries no score or comment count.\n"
            "· Observations are targeted examples, entered by an analyst or collected "
            "automatically from standing searches. They are not a representative sample."
        ),
    }


def _split_sources(sources: OrderedDict) -> dict:
    """Current sources first, then older context, each keeping its number."""
    full = _sources(sources)
    header, *lines = full["text"].split("\n")
    older = {
        n for n, ((kind, _), obj) in enumerate(sources.items(), start=1)
        if kind == "obs" and evidence_class(obj) in (OLDER, UNDATED)
    }

    def number(line):
        found = re.match(r"· \[(\d+)\]", line)
        return int(found.group(1)) if found else 0

    current = [line for line in lines if number(line) not in older]
    previous = [line for line in lines if number(line) in older]
    text = header + "\n" + "\n".join(current)
    if previous:
        text += (
            "\n\nOlder or undated context (published before the research window or "
            "undated; not counted as current evidence):\n" + "\n".join(previous)
        )
    return {"heading": "Sources", "text": text}

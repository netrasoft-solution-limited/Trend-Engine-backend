"""The New Product Opportunity Brief, in the shape Mark delivered (20 Oct).

Built only from APPROVED candidates, in the analyst's priority order. The
sections follow his October brief: about, executive summary, what the evidence
supports, search demand, cross-channel corroboration, one section per
candidate, how to read the evidence, numbered sources.

Figure kinds are never mixed: search estimates, Trends interest, lifetime Shop
counts and cumulative engagement each keep their own column or label.
"""
from __future__ import annotations

from collections import OrderedDict

from apps.evidence.models import Observation
from apps.scoring import opportunities
from apps.scoring.models import Candidate

from .briefing import Brief, _cell, _first_sentence, _sources, _window, observation_line
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
    body = [_candidate(organization, n, c, sources) for n, c in enumerate(candidates, start=1)]
    demand = _search_demand(candidates, sources)
    corroboration = _corroboration(candidates)
    profiles = {c.gap_check.get("profile") for c in candidates if c.gap_check.get("profile")}

    sections = [
        _about(organization, candidates, sources, note),
        _summary(organization, candidates, note),
        *([demand] if demand else []),
        corroboration,
        *body,
        _reading(),
        _sources(sources),
    ]
    return Brief(sections, ", ".join(sorted(profiles)), len(candidates))


def _names(candidates) -> str:
    names = [c.name for c in candidates]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _listings(candidates) -> int:
    return max((c.gap_check.get("listings", 0) for c in candidates), default=0)


def _about(organization, candidates, sources, note) -> dict:
    observed = [o for c in candidates for o in c.observations.all()]
    for o in observed:
        sources.setdefault(("obs", o.pk), o)
    start, end = _window(sources)
    span = (
        f"Between {start:%-d %B} and {end:%-d %B %Y}" if start and start != end
        else "This period"
    )
    return {
        "heading": "About this brief",
        "text": (
            f"This brief looks beyond the current {organization.name} range. {span} we "
            f"reviewed {len(candidates)} ingredient(s) {organization.name} does not appear to "
            f"sell ({_names(candidates)}) against podcast, video, social, Reddit and search "
            f"signals, and ranked them for commercial testing.\n\n"
            f"The ranking is our commercial judgment within this review. It is not a "
            f"market-size ranking. Every observation is linked to a dated source, listed in "
            f"full at the end.\n\n{note}"
        ),
    }


def _summary(organization, candidates, note) -> dict:
    lead = candidates[0]
    listings = _listings(candidates)
    absent = [c for c in candidates if c.gap_check and not c.gap_check.get("found")]
    lines = ["| # | Candidate | Status | Commercial opening | Recommended next step |"]
    for n, c in enumerate(candidates, start=1):
        lines.append(
            f"| {n} | {_cell(c.name)} | {c.get_status_display()} | {_cell(c.opening)} | "
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
    return {
        "heading": "Executive summary",
        "text": (
            f"Our recommendation. Start with {start}. "
            f"{_first_sentence(lead.why_it_matters) or lead.opening}\n\n"
            f"{len(candidates)} candidate ingredient(s) reviewed · {len(absent)} of "
            f"{len(candidates)} not found in {listings} listings.\n\n"
            + "\n".join(lines)
            + f"\n\n{gap}{confirm}\n\n"
            f"The decision we need. Confirm the portfolio gaps and agree to {step}, "
            f"with a budget and success criteria.\n\n{note}"
        ),
    }


def _figure(o: Observation) -> str:
    value = o.metric_label or (f"{o.metric_value:,.0f}" if o.metric_value is not None else "")
    return value


def _search_demand(candidates, sources) -> dict | None:
    rows, trends = [], []
    for c in candidates:
        for o in c.observations.all():
            if o.metric_kind == Observation.MetricKind.SEARCH_ESTIMATE:
                n = list(sources).index(("obs", o.pk)) + 1
                rows.append(
                    f"| {_cell(c.name)} | {_cell(o.query)} | {_cell(_figure(o))} | "
                    f"{_cell(o.geography)} | {_cell(o.provider)}, {_cell(o.period)} [{n}] |"
                )
            elif o.metric_kind == Observation.MetricKind.TRENDS:
                trends.append(f"· {c.name}: {observation_line(o, sources)}")
    if not rows and not trends:
        return None
    parts = []
    if rows:
        parts.append(
            "| Candidate | Query | Estimated searches per month | Geography | Source and period |\n"
            + "\n".join(rows)
        )
        parts.append(
            "These are estimated rolling monthly averages from third-party keyword data, not "
            "the past 30 days or counts of buyers. Queries overlap and must not be added together."
        )
    if trends:
        parts.append("Relative search interest (Google Trends, not volumes):\n" + "\n".join(trends))
    return {"heading": "Search demand", "text": "\n\n".join(parts)}


def _corroboration(candidates) -> dict:
    lines = ["| Candidate | " + " | ".join(CHANNELS) + " |"]
    complete = []
    for c in candidates:
        cells = []
        for types in CHANNELS.values():
            seen = [o for o in c.observations.all() if o.source_type in types]
            events = len({o.independence_key for o in seen})
            flagged = [o for o in seen if o.is_ad or o.is_brand_content or o.is_old_material]
            cell = f"{events} event(s)" if events else "None found"
            if flagged:
                cell += f"; {len(flagged)} paid, brand or older"
            cells.append(cell)
        if all(not cell.startswith("None") for cell in cells):
            complete.append(c.name)
        lines.append(f"| {_cell(c.name)} | " + " | ".join(cells) + " |")
    closing = (
        f"Evidence in every channel: {', '.join(complete)}."
        if complete else "No candidate has evidence in every channel within this review."
    )
    return {
        "heading": "What is corroborated across channels",
        "text": "\n".join(lines) + "\n\n"
        "Counts are independent events: the same story told in a podcast and its video, or "
        "two posts about one study, counts once. Paid, brand and older material is counted "
        f"but named. {closing}",
    }


def _candidate(organization, n: int, c: Candidate, sources) -> dict:
    parts = []
    beside = (
        f" It would sit beside {', '.join(c.adjacent)} in the {organization.name} range."
        if c.adjacent else ""
    )
    parts.append(f"The opportunity. {c.opening}{beside} {opportunities.gap_sentence(c)}")
    if c.internal_check != Candidate.InternalCheck.PENDING:
        parts[-1] += f" {c.get_internal_check_display()}."
    observations = sorted(c.observations.all(), key=lambda o: (o.published_at or o.observed_at))
    if observations:
        parts.append(
            "What people are discussing:\n"
            + "\n".join(
                f"· {o.get_source_type_display()}. {observation_line(o, sources)}"
                for o in observations
            )
        )
    if c.why_it_matters:
        parts.append(f"Why it could matter to {organization.name}. {c.why_it_matters}")
    if c.test_plan:
        parts.append(f"What to test next. {c.test_plan}")
    if c.gate:
        parts.append(f"Gate. {c.gate}")
    if c.limits:
        parts.append(f"What this does not show. {c.limits}")
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
            "· Observations are targeted examples entered by an analyst, not a representative "
            "sample."
        ),
    }

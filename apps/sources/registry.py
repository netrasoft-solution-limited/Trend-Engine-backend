"""What each route needs before it can collect anything.

The config on a `Source` is a JSON object rather than columns, because each
route needs something different and a column per connector would mean a
migration every time a vendor is added. The cost of that choice is that nothing
stops an operator saving a source with the wrong keys, or none — and the
failure surfaces much later, as a source that is "active" and silently returns
nothing. Two of the four sources in the dev database are in exactly that state.

So this module is the one place that knows what a route needs, written once and
used by both the screen (to render the right fields and refuse a bad save) and
the operator reading it. `apps.evidence.collection` still owns the reading of
those keys at collection time; what is here is the contract, not a second
implementation of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import AcquisitionProvider, Source


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    placeholder: str = ""
    #: A comma-separated input that stores a JSON list. The alternative is
    #: asking an operator to type JSON into a form, which is a way of making
    #: them responsible for your schema.
    is_list: bool = False
    help: str = ""


@dataclass(frozen=True)
class RouteSpec:
    route: str
    label: str
    #: Prose for the screen: what this route actually does.
    blurb: str
    fields: tuple[Field, ...] = ()
    #: At least one of these must be filled. Not "all" — a podcast needs a feed
    #: URL *or* a name, and demanding both would be wrong.
    requires_one_of: tuple[str, ...] = ()
    available: bool = True
    #: Why not, in a sentence the operator can act on.
    unavailable_because: str = ""
    #: Which vendor actually collects this route. The access basis a source
    #: records has to be THAT vendor's — a YouTube source filed under a podcast
    #: provider's policy is a mis-recorded legal basis, which is precisely what
    #: PRD §7.2 exists to prevent, and it would survive every other check here.
    provider_kind: str = ""


SPECS: dict[str, RouteSpec] = {
    Source.Route.PODCAST: RouteSpec(
        route=Source.Route.PODCAST,
        label="Podcast",
        blurb=(
            "Checks a show for new episodes. Transcripts come from the publisher "
            "where they exist, and are paid for only when they do not."
        ),
        fields=(
            Field(
                key="rss_url",
                label="RSS feed address",
                placeholder="https://feeds.example.com/the-show",
                help="Preferred — it matches the exact show, with no ambiguity.",
            ),
            Field(
                key="series_name",
                label="…or the show's name",
                placeholder="The Peter Attia Drive",
                help=(
                    "Matched exactly by the provider: “Huberman Lab” resolves and "
                    "“huberman lab” does not. Use the feed address if you have it."
                ),
            ),
        ),
        requires_one_of=("rss_url", "series_name"),
        provider_kind=AcquisitionProvider.Kind.TADDY,
    ),
    Source.Route.YOUTUBE: RouteSpec(
        route=Source.Route.YOUTUBE,
        label="YouTube",
        blurb=(
            "Either a standing search, which finds creators you have not thought "
            "of, or specific videos by address."
        ),
        fields=(
            Field(
                key="queries",
                label="Search terms",
                placeholder="magnesium sleep, creatine cognition",
                is_list=True,
                help="One search per term, run on the schedule below.",
            ),
            Field(
                key="urls",
                label="…or specific video addresses",
                placeholder="https://www.youtube.com/watch?v=…",
                is_list=True,
            ),
        ),
        requires_one_of=("queries", "urls"),
        provider_kind=AcquisitionProvider.Kind.APIFY,
    ),
    Source.Route.RESEARCH: RouteSpec(
        route=Source.Route.RESEARCH,
        label="Research",
        blurb="Clinical trial registries and journals.",
        available=False,
        unavailable_because=(
            "The research route has no collector yet. PubMed, Crossref and "
            "ClinicalTrials.gov are a planned package, not a configuration."
        ),
    ),
    Source.Route.SOCIAL: RouteSpec(
        route=Source.Route.SOCIAL,
        label="Social",
        blurb="TikTok, Instagram, X, Reddit.",
        available=False,
        unavailable_because=(
            "Social platforms are gated rather than unbuilt. PRD §4.2 requires "
            "each one to have its own recorded access basis, cost line and "
            "approval before any connector work begins, and none is subscribed. "
            "This is a commercial decision before it is a build."
        ),
    ),
}


def spec_for(route: str) -> RouteSpec | None:
    return SPECS.get(route)


def available_specs() -> list[RouteSpec]:
    return [spec for spec in SPECS.values() if spec.available]


def config_problem(route: str, config: dict) -> str:
    """Why this source cannot collect, or "" if it can.

    Returns prose rather than a boolean because the screen shows it verbatim,
    and "invalid" tells an operator nothing they can act on.
    """
    spec = spec_for(route)
    if spec is None:
        return f"“{route}” is not a route this system knows."
    if not spec.available:
        return spec.unavailable_because
    if not spec.requires_one_of:
        return ""

    filled = [key for key in spec.requires_one_of if (config or {}).get(key)]
    if filled:
        return ""

    labels = [f.label.lstrip("…or ").rstrip(":") for f in spec.fields if f.key in spec.requires_one_of]
    return (
        f"Nothing to collect from: this needs {' or '.join(labels).lower()}. "
        f"It will stay quiet rather than fail, which is the harder problem to spot."
    )


def describe_config(route: str, config: dict) -> str:
    """A one-line summary for the list, e.g. `rss_url · 2 queries`."""
    spec = spec_for(route)
    if spec is None or not config:
        return ""
    parts = []
    for field_spec in spec.fields:
        value = config.get(field_spec.key)
        if not value:
            continue
        if field_spec.is_list:
            parts.append(f"{len(value)} {field_spec.label.lower().lstrip('…or ')}")
        else:
            parts.append(str(value)[:48])
    return " · ".join(parts)


def policies_for(route: str, policies) -> list:
    """The access bases that could legitimately cover this route.

    Filtering rather than listing everything: an operator offered one dropdown
    of every live policy will pick the first plausible one, and a source under
    the wrong vendor's basis looks correct in every view afterwards.
    """
    spec = spec_for(route)
    if spec is None or not spec.provider_kind:
        return []
    return [p for p in policies if p.provider.kind == spec.provider_kind]

"""Asking a source what is new.

The mirror of `services/transcripts.py`: that module walks rungs to get one
item's text, this one asks a source for its items. Both are registries keyed by
a string from configuration rather than branches on a type, for the same reason
— Arch §7.2's "declarative data, not branching code" is about the whole
acquisition layer, not only the fallback chain.

WHAT A COLLECTOR MAY DO IS DELIBERATELY NARROW: return normalised rows. It does
not decide relevance, it does not fetch transcripts, and it does not write
anything. Collection is the cheap step that feeds the gate, and the gate exists
precisely so that the expensive steps run on a fraction of what arrives
(Arch §6.2). A collector that helpfully fetched transcripts while it was there
would spend the whole budget before anything had been screened.

This lives in `evidence` rather than `ingestion` because it reads
`Source.config` and returns rows destined for `ContentItem`, and because the
layer stack puts `evidence` above both `ingestion` and `connectors` — this is
the lowest place that can see all three.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from apps.sources.models import Source

logger = logging.getLogger(__name__)


class NotCollectable(RuntimeError):
    """This source cannot be polled, and the message says what to fix.

    Distinct from a failed poll: the source is misconfigured rather than the
    vendor being down, so retrying changes nothing until someone edits it.
    """


def _podcast(source: Source, limit: int) -> list[dict]:
    """Taddy, by feed URL or by exact series name.

    The RSS URL is preferred because Taddy matches `name` exactly — "Huberman
    Lab" resolves and "huberman lab" does not, which is a sharp edge to leave
    an operator standing on.
    """
    from apps.connectors.factory import taddy_for

    config = source.config or {}
    rss_url = (config.get("rss_url") or "").strip()
    series = (config.get("series_name") or "").strip()

    if not rss_url and not series:
        raise NotCollectable(
            f"{source.name} has no rss_url or series_name in its config. "
            f"Taddy needs one of them to find the series."
        )

    connector = taddy_for(source)
    if rss_url:
        return connector.episodes(rss_url=rss_url, limit=limit)
    return connector.episodes(name=series, limit=limit)


def _youtube(source: Source, limit: int) -> list[dict]:
    """Apify, by search query or by explicit video URLs.

    Both are charged per video returned, so `limit` is a spend control rather
    than a page size — Arch §10.2 wants the ceiling applied before the run.
    """
    from apps.connectors.factory import apify_for

    config = source.config or {}
    queries = [q for q in (config.get("queries") or []) if q]
    urls = [u for u in (config.get("urls") or []) if u]

    if not queries and not urls:
        raise NotCollectable(
            f"{source.name} has no queries or urls in its config. "
            f"The YouTube connector needs something to search for or fetch."
        )

    connector = apify_for(source)
    if urls:
        return connector.fetch_by_url(urls[:limit])
    return connector.search(queries, max_results=limit)


def _unbuilt(route: str) -> Callable[[Source, int], list[dict]]:
    def collect(source: Source, limit: int) -> list[dict]:
        raise NotCollectable(
            f"No collector for the '{route}' route yet. {source.name} will not "
            f"be polled until one exists."
        )

    return collect


#: route → how to ask it for items. A route with no collector raises
#: NotCollectable with its name in the message, rather than returning zero
#: items — a source that silently collects nothing looks exactly like a source
#: whose publisher has gone quiet, and the two need different responses.
COLLECTORS: dict[str, Callable[[Source, int], list[dict]]] = {
    Source.Route.PODCAST: _podcast,
    Source.Route.YOUTUBE: _youtube,
    Source.Route.RESEARCH: _unbuilt("research"),
    Source.Route.SOCIAL: _unbuilt("social"),
}

#: How many items to take from one poll. Small on purpose: a source is polled
#: repeatedly, so a low ceiling costs a little latency on a backlog while a
#: high one turns a misconfigured feed into a bill.
DEFAULT_LIMIT = 25


def collect(source: Source, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """The latest items from one source, normalised and unjudged."""
    if not source.can_collect:
        raise NotCollectable(
            f"{source.name} has no live provider policy. PRD §7.2: no connector "
            f"runs without a recorded access basis."
        )

    collector = COLLECTORS.get(source.route)
    if collector is None:
        raise NotCollectable(f"Unknown route {source.route!r} on {source.name}.")

    rows = collector(source, min(limit, int(source.config.get("limit") or limit)))
    logger.info("Collected %s items from %s", len(rows), source.name)
    return rows

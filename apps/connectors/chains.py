"""Fallback chains as configuration — Arch §7.2.

"Chains are declarative data, not branching code." Adding or reordering a route
is a config change, not a deploy. The defaults below seed the database; the
running system reads them from a table, so this module is a fixture, not a
lookup that code branches on.

    podcast:  publisher_transcript → taddy → assemblyai → manual → metadata_only
    youtube:  creator_captions → apify_subtitles → supadata → manual → metadata_only

Each step records whether it was the one that succeeded, so transcript
provenance and cost stay attributable per item.
"""
from __future__ import annotations

#: Seed data for the fallback-chain table. Order is the ladder.
DEFAULT_CHAINS: dict[str, tuple[str, ...]] = {
    "podcast": (
        "publisher_transcript",
        "taddy",
        "assemblyai",
        "manual",
        "metadata_only",
    ),
    "youtube": (
        "creator_captions",
        "apify_subtitles",
        "supadata",
        "manual",
        "metadata_only",
    ),
    "research": (
        "ncbi_eutils",
        "crossref",
        "manual",
    ),
    "web": (
        "allowlist_crawler",
        "manual",
        "metadata_only",
    ),
}

#: The terminal rung. Reaching it is not a failure — it is a documented outcome
#: that must be surfaced honestly on the evidence record (Arch §7.1).
TERMINAL_STEP = "metadata_only"

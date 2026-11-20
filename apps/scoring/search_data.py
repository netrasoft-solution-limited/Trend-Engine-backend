"""Search volumes and recent search interest, refreshed inside the cycle.

Mark, 20 Nov: "Bring fresh Google search volumes and recent search interest
into the workflow, with source dates and uncertainty." Google Trends refuses
automated requests from our server and keyword volumes need a paid source, so
this waits on a provider being approved. Until then `available()` is False and
the research cycle records the stage as skipped, in plain words.
"""
from __future__ import annotations


def available() -> bool:
    return False


def refresh(organization, *, actor_label: str) -> dict:
    raise NotImplementedError("No search-data provider is configured.")


def describe(result: dict) -> str:
    return ""

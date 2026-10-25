"""Template context for the operator plane."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def ops_alerts(request) -> dict:
    """Current problems, for signed-in operators only. Never breaks a page:
    a banner that raises would take the screen down with it."""
    if not getattr(getattr(request, "user", None), "is_authenticated", False):
        return {}
    try:
        from .alerts import problems

        return {"ops_alerts": problems()}
    except Exception:  # noqa: BLE001 — the page matters more than the banner
        logger.exception("Could not compute operator alerts")
        return {}

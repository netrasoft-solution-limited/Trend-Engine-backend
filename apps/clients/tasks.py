"""Celery tasks for clients. Queue routing lives in config/celery.py.

Discovery is a task rather than request work because it makes two slow calls —
an HTTP fetch of someone else's storefront, then a model call over the whole
catalogue. Thirty seconds inside a gunicorn worker is a request that times out
at the proxy and an operator who cannot tell whether it worked.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.clients.tasks.discover_storefront", max_retries=1)
def discover_storefront(*, organization_id: int, store_url: str, actor_label: str = "") -> dict:
    """Read a storefront and leave a DRAFT profile for the operator to review.

    Never activates. A storefront says what a client sells; it cannot say which
    product carries the margin or who they are selling to, and a profile that
    looks considered but contains numbers nobody chose is worse than an empty
    one — see `discovery.draft_from_catalogue`.

    Failures are swallowed into the return value rather than raised: the
    operator is looking at a page, not a traceback, and "that storefront blocks
    us" is a normal answer this task should be able to give.
    """
    from apps.clients import catalogue, discovery
    from apps.tenancy.context import operator_scope, scoped
    from apps.tenancy.models import Organization

    with operator_scope():
        organization = Organization.objects.filter(pk=organization_id).first()
    if organization is None:
        logger.error("discover_storefront: no organisation %s", organization_id)
        return {"ok": False, "error": "organisation not found"}

    try:
        products = catalogue.read(store_url)
    except catalogue.CatalogueError as exc:
        logger.warning("Could not read %s: %s", store_url, exc)
        return {"ok": False, "error": str(exc)}

    with scoped(organization):
        version, found = discovery.draft_from_catalogue(
            organization, products=products, actor_label=actor_label or "discovery"
        )

    return {
        "ok": True,
        "profile_version": version.number,
        "products": len(products),
        "assets": version.assets.count(),
        "category": found.category,
    }

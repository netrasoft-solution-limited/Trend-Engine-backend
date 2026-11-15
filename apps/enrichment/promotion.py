"""One promotional classification, carried to every output.

Found the hard way (Mark, 15 Nov): a Timeline gummies post was labelled an
advertisement in the opportunity memo and still supported the gummy story in
the trend brief. The two outputs read different flags. The memo reads
`Observation.is_ad` / `is_brand_content`, set by hand; the trend brief, detection
and the momentum counts read `Claim.is_sponsored`, set once at extraction from
the item's own title and description and never revisited.

`reclassify()` closes that gap in one direction only: it marks claims sponsored,
never unmarks them, so it is safe to run as often as needed. It applies:

  · the promotion pattern (`promotion_reason`) to every item's title and
    description, so claims extracted before a mark was added are caught up;
  · every hand-marked observation collected from an item (provenance
    "Collected automatically: item N") to that item's claims;
  · a creator marked as brand content to every item from that creator on the
    same platform: a brand account's posts are its own marketing, whichever
    one was looked at.

Detection and both brief generators call it first, so a mark made anywhere is
in force the next time anything is counted.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from apps.evidence.models import ContentItem, Observation, promotion_reason
from apps.operations.models import AuditEvent

from .models import Claim

logger = logging.getLogger(__name__)

COLLECTED_ITEM = re.compile(r"Collected automatically: item (\d+)")


@dataclass
class Reclassified:
    #: item id → why it is promotional
    items: dict[int, str] = field(default_factory=dict)
    claims: int = 0


def collected_item_id(observation) -> int | None:
    found = COLLECTED_ITEM.search(observation.provenance or "")
    return int(found.group(1)) if found else None


def _creator_key(item) -> tuple[str, str]:
    return (item.platform, (item.creator or "").strip().lstrip("@").lower())


def promotional_items() -> dict[int, str]:
    """Every collected item that is promotional, with the reason."""
    found: dict[int, str] = {}

    marked = Observation.objects.filter(is_ad=True) | Observation.objects.filter(
        is_brand_content=True
    )
    brand_creators: dict[tuple[str, str], str] = {}
    for observation in marked:
        item_id = collected_item_id(observation)
        if item_id is None:
            continue
        label = "advertising" if observation.is_ad else "brand content"
        found.setdefault(item_id, f"marked {label} by hand (O{observation.pk})")
        if observation.is_brand_content:
            item = ContentItem.objects.select_related("source").filter(pk=item_id).first()
            if item and item.creator:
                brand_creators.setdefault(
                    _creator_key(item), f"creator marked brand content by hand (O{observation.pk})"
                )

    candidates = ContentItem.objects.filter(claims__is_sponsored=False).distinct()
    for item in candidates.select_related("source").iterator():
        if item.pk in found:
            continue
        reason = promotion_reason(item.title, item.description)
        if not reason and item.creator:
            reason = brand_creators.get(_creator_key(item), "")
        if reason:
            found[item.pk] = reason
    return found


def reclassify(*, actor_label: str = "system") -> Reclassified:
    """Mark the claims of every promotional item sponsored. Idempotent."""
    result = Reclassified(items=promotional_items())
    if not result.items:
        return result
    result.claims = Claim.objects.filter(
        content_item_id__in=list(result.items), is_sponsored=False
    ).update(is_sponsored=True)
    if result.claims:
        AuditEvent.objects.create(
            kind=AuditEvent.Kind.SYSTEM,
            actor_realm=AuditEvent.Realm.SYSTEM,
            actor_label=actor_label,
            message=(
                f"Marked {result.claims} claim(s) promotional across "
                f"{len(result.items)} item(s); they no longer count as independent evidence."
            ),
            context={"items": {str(k): v for k, v in list(result.items.items())[:200]}},
        )
        logger.info("Reclassified %s claim(s) as promotional", result.claims)
    return result

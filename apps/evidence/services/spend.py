"""What Virlo has cost this month, against its monthly cap.

Mark, 2 Nov: a $10 monthly Virlo cap, automatic top-ups off. The balance is
prepaid, so going over would only fail at Virlo; checking here stops the call
before it is made (Arch §10.2) and says why in the run log.

Spend is read from what was recorded: each collection run's `cost_usd`, and
each Virlo transcript or comment page's `cost_usd` on its transcript.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.evidence.models import TranscriptArtifact
from apps.ingestion.models import IngestionRun
from apps.sources.models import AcquisitionProvider

VIRLO_METHODS = (TranscriptArtifact.Method.VIRLO, TranscriptArtifact.Method.CAPTION_COMMENTS)


def virlo_month_spend() -> Decimal:
    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    runs = IngestionRun.objects.filter(
        started_at__gte=start, source__policy__provider__kind=AcquisitionProvider.Kind.VIRLO
    ).aggregate(s=Sum("cost_usd"))["s"] or Decimal("0")
    texts = TranscriptArtifact.objects.filter(
        created_at__gte=start, method__in=VIRLO_METHODS
    ).aggregate(s=Sum("cost_usd"))["s"] or Decimal("0")
    return runs + texts


def virlo_calls_left() -> int | None:
    """Calls the cap still allows this month; None when there is no cap."""
    from apps.connectors.providers.virlo import PRICE_PER_CALL_USD

    provider = AcquisitionProvider.objects.filter(kind=AcquisitionProvider.Kind.VIRLO).first()
    if provider is None or not provider.monthly_cap_usd:
        return None
    left = provider.monthly_cap_usd - virlo_month_spend()
    return max(int(left / PRICE_PER_CALL_USD), 0)

"""Align the app's AI spend ledger with what the provider reports.

    python manage.py reconcile_llm_spend 50.83 --as-of "2026-11-20 16:36" --by "mark@…"

The app costs each call from the tokens in its response. Anything the provider
bills that never produced a ledger row (a call outside the app, a call whose
record was lost) is invisible to the cap and its alerts. This records the
difference as one adjustment CostEvent, so month-to-date spend, the cap and the
80% warning read the provider's figure. The adjustment is never deleted; a
later reconciliation adds another.
"""
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from apps.enrichment.llm import costs


class Command(BaseCommand):
    help = "Record the gap between provider-reported and ledger AI spend for this month."

    def add_arguments(self, parser):
        parser.add_argument("provider_usd", help="Month-to-date spend the provider shows.")
        parser.add_argument("--as-of", required=True, help="When the provider figure was read.")
        parser.add_argument("--by", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, provider_usd, as_of, by, dry_run, **options):
        try:
            provider = Decimal(provider_usd)
        except InvalidOperation as exc:
            raise CommandError(f"{provider_usd!r} is not an amount.") from exc
        ledger = costs.spend_this_month()
        gap = provider - ledger
        self.stdout.write(f"Provider ${provider} · ledger ${ledger:.6f} · gap ${gap:.6f}")
        if dry_run or gap <= 0:
            return
        costs.CostEvent.objects.create(
            provider=costs.LLM_PROVIDER,
            source_id="provider-reconciliation",
            run_id="",
            units={"provider_reported_usd": str(provider), "ledger_usd": str(ledger),
                   "as_of": as_of, "by": by},
            estimated_usd=gap,
        )
        self.stdout.write(f"Recorded an adjustment of ${gap:.6f}; month-to-date now ${provider}.")

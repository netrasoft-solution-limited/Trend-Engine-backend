"""Carry every promotional mark to the claims it covers. Safe to rerun."""
from django.core.management.base import BaseCommand

from apps.enrichment import promotion


class Command(BaseCommand):
    help = "Mark claims from promotional items sponsored (pattern, hand marks, brand creators)."

    def handle(self, *args, **options):
        result = promotion.reclassify(actor_label="reclassify_promotion")
        self.stdout.write(
            f"{result.claims} claim(s) newly marked promotional; "
            f"{len(result.items)} promotional item(s) in all."
        )

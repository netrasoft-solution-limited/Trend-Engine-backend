"""Run signal detection now, rather than waiting for the schedule.

    manage.py detect_signals            detect and score
    manage.py detect_signals --rescore  rescore open signals only; no model call

Detection makes one cost-capped model call over the window's claims; the
summary printed is the same one the scheduled task returns.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.intelligence import detection


class Command(BaseCommand):
    help = "Detect cross-source signals from recent claims, and score them."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--rescore",
            action="store_true",
            help="Recompute scores for open signals without calling the model.",
        )

    def handle(self, *args, **options) -> None:
        if options["rescore"]:
            count = detection.rescore_open(now=timezone.now())
            self.stdout.write(self.style.SUCCESS(f"Rescored {count} open signal(s)."))
            return

        result = detection.detect()
        if result.skipped:
            self.stdout.write(self.style.WARNING(f"Skipped: {result.skipped}"))
        self.stdout.write(
            f"{result.claims_considered} claim(s) considered · "
            f"{len(result.created)} new: {', '.join(result.created) or '—'} · "
            f"{len(result.extended)} extended: {', '.join(result.extended) or '—'}"
        )
        if result.dropped_unknown_ids or result.dropped_single_source:
            self.stdout.write(
                self.style.WARNING(
                    f"Dropped {result.dropped_unknown_ids} invented claim ID(s) and "
                    f"{result.dropped_single_source} single-item proposal(s)."
                )
            )
        self.stdout.write(self.style.SUCCESS(f"Rescored {result.rescored} open signal(s)."))

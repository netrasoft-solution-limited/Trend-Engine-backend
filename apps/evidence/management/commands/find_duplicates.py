"""Find items that are the same conversation as another — report, or record.

    manage.py find_duplicates            report only; writes nothing
    manage.py find_duplicates --apply    record the matches

New items are checked as their transcript arrives. This is for what was
collected before that check existed. Already-extracted duplicates keep their
claims; from now on those claims count under the canonical item, so they add
no item, creator or source type to any score.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.evidence.models import ContentItem
from apps.evidence.services import duplicates


class Command(BaseCommand):
    help = "Find podcast/YouTube re-uploads and clips that are the same conversation."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--apply", action="store_true", help="Record the matches.")

    def handle(self, *args, **options) -> None:
        items = (
            ContentItem.objects.select_related("transcript", "source")
            .order_by("discovered_at", "pk")
        )
        found = 0
        for item in items:
            if item.is_duplicate:
                continue
            match = (duplicates.check(item) if options["apply"] else duplicates.find(item))
            if match is None:
                continue
            found += 1
            other = ContentItem.objects.select_related("source").get(pk=match.other_id)
            self.stdout.write(
                f"{match.status:<9} {match.overlap:>4.0%}  "
                f"[{item.source.name}] {item.title[:60]}\n"
                f"{'':>15}= [{other.source.name}] {other.title[:60]}\n"
                f"{'':>15}{match.reason}"
            )
        verb = "Recorded" if options["apply"] else "Found (not recorded)"
        self.stdout.write(self.style.SUCCESS(f"{verb}: {found} match(es)."))

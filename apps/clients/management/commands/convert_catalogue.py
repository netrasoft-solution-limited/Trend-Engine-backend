"""Map a catalogue inventory CSV into the profile import formats.

    manage.py convert_catalogue inventory.csv --csv import.csv --json profile.json

Writes the product-template CSV (for review and the client-page import) and
the JSON spec `client_profile load` accepts. Loads nothing by itself: load the
JSON as a draft, review it, then make it live.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.clients import catalogue_import


class Command(BaseCommand):
    help = "Convert a catalogue inventory CSV into the profile import CSV and JSON."

    def add_arguments(self, parser) -> None:
        parser.add_argument("inventory")
        parser.add_argument("--csv", required=True, help="Where to write the import CSV.")
        parser.add_argument("--json", required=True, help="Where to write the JSON spec.")

    def handle(self, *args, **options) -> None:
        source = Path(options["inventory"])
        if not source.exists():
            raise CommandError(f"No such file: {source}")
        spec, rows = catalogue_import.spec_from_inventory(source.read_text(encoding="utf-8"))
        Path(options["csv"]).write_text(catalogue_import.import_csv(rows), encoding="utf-8")
        Path(options["json"]).write_text(
            json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        products = sum(1 for r in rows if r["kind"] == "product")
        self.stdout.write(self.style.SUCCESS(
            f"{products} listings and {len(rows) - products} families written."
        ))

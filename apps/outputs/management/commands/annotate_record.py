"""Attach a correction to an output's record without rewriting it.

    manage.py annotate_record 1 --author abubakar@netrasoft --text "..."
    manage.py annotate_record 1 --author abubakar@netrasoft --file note.txt

The note is append-only, audited, shown with the output in the operator app,
and never included in an exported file.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.outputs import services
from apps.outputs.models import Output
from apps.tenancy.context import operator_scope, scoped


class Command(BaseCommand):
    help = "Add an append-only record note to an output."

    def add_arguments(self, parser) -> None:
        parser.add_argument("output_id", type=int)
        parser.add_argument("--author", required=True)
        parser.add_argument("--text")
        parser.add_argument("--file", help="Read the note from a text file.")

    def handle(self, *args, **options) -> None:
        text = options["text"] or ""
        if options["file"]:
            text = Path(options["file"]).read_text()
        if not text.strip():
            raise CommandError("Give the note with --text or --file.")

        with operator_scope():
            output = Output.objects.select_related("organization").filter(
                pk=options["output_id"]
            ).first()
        if output is None:
            raise CommandError(f"No output {options['output_id']}.")

        with scoped(output.organization):
            note = services.add_record_note(output, text=text, author_label=options["author"])
        self.stdout.write(self.style.SUCCESS(f"Note {note.pk} added to “{output.title}”."))

"""Import a Google Trends export and attach each reading to its candidate.

    python manage.py import_trends multiTimeline.csv --by "abubakar@…"

Each term becomes one google_trends observation (current 28 days against the
28 before, both named). A reading whose term is a candidate's name or alias is
attached to that candidate, which withdraws its approval as any new evidence
does, so the case is read again before a brief uses it.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.evidence.services import trends_import
from apps.scoring import opportunities
from apps.scoring.models import Candidate
from apps.tenancy.context import operator_scope


class Command(BaseCommand):
    help = "Import a Google Trends multiTimeline.csv and attach readings to matching candidates."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--by", default="import_trends")

    def handle(self, *args, path, by, **options):
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
            saved = trends_import.save(trends_import.parse(text), entered_by=by)
        except (OSError, trends_import.TrendsImportError) as exc:
            raise CommandError(str(exc)) from exc
        with operator_scope():
            for o in saved:
                matches = []
                for candidate in Candidate.objects.all():
                    if o.query.lower() not in (n.lower() for n in candidate.names):
                        continue
                    attached, _ = opportunities.attach(candidate, [o.pk], actor_label=by)
                    if attached:
                        matches.append(candidate)
                self.stdout.write(
                    f"O{o.pk} {o.query}: {o.metric_label}"
                    + (f" → attached to {', '.join(c.name for c in matches)}" if matches else "")
                )

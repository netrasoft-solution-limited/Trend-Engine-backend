"""Run a backup or a restore test by hand.

The schedule runs both (config/celery.py). This exists for the three times a
human needs one: before a risky migration, to prove a newly configured bucket
actually works, and during the recovery itself — when Celery may well be part
of what is broken.

It calls exactly the same functions the tasks call, so a green run here means
the scheduled one will behave the same way.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.operations import backups
from apps.operations.models import BackupRun


class Command(BaseCommand):
    help = "Take an off-box backup, or restore-test the most recent one."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "action",
            choices=["now", "restore-test", "status"],
            help="now: take a backup. restore-test: prove the latest one restores. "
                 "status: show the last run of each.",
        )

    def handle(self, *args, **options) -> None:
        action = options["action"]

        if action == "status":
            self._status()
            return

        runner = backups.run_backup if action == "now" else backups.run_restore_test
        try:
            run = runner()
        except backups.BackupError as exc:
            raise CommandError(str(exc)) from exc

        if run.outcome == BackupRun.Outcome.SKIPPED:
            self.stdout.write(self.style.WARNING(f"skipped — {run.detail}"))
            return
        self.stdout.write(self.style.SUCCESS(f"{run.get_kind_display()}: {run.detail or 'ok'}"))
        if run.artifact:
            self.stdout.write(f"  artifact {run.artifact}")

    def _status(self) -> None:
        for kind, label in BackupRun.Kind.choices:
            run = BackupRun.latest(kind)
            if run is None:
                self.stdout.write(self.style.ERROR(f"{label}: never run"))
                continue
            age = run.started_at
            style = (
                self.style.SUCCESS
                if run.outcome == BackupRun.Outcome.SUCCEEDED
                else self.style.ERROR
            )
            self.stdout.write(style(f"{label}: {run.outcome} at {age:%Y-%m-%d %H:%M} UTC"))
            if run.detail:
                self.stdout.write(f"  {run.detail}")

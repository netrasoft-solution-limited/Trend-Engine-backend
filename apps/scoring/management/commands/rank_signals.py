"""Rank signals for one client, and draft recommendations for the top ones.

    manage.py rank_signals jarrow                 rank, then draft up to 3
    manage.py rank_signals jarrow --recommend 0   rank only; no model call
    manage.py rank_signals jarrow --signal SIG-3001
                                                  draft for that signal alone

Ranking is arithmetic and free. Each recommendation is one cost-capped model
call that sees this client's profile and nothing else.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.intelligence.models import Signal
from apps.scoring import recommendations, signal_rank
from apps.scoring.tasks import RECOMMEND_TOP, ranked_open, recommend_top
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization


class Command(BaseCommand):
    help = "Rank signals for a client and draft recommendations for the best of them."

    def add_arguments(self, parser) -> None:
        parser.add_argument("slug", help="The client's slug, e.g. jarrow.")
        parser.add_argument(
            "--recommend", type=int, default=RECOMMEND_TOP,
            help=f"How many new recommendations to draft (default {RECOMMEND_TOP}).",
        )
        parser.add_argument("--signal", help="Draft a recommendation for this signal code only.")

    def handle(self, *args, **options) -> None:
        with operator_scope():
            organization = Organization.objects.filter(slug=options["slug"]).first()
        if organization is None:
            raise CommandError(f"No client with slug {options['slug']!r}.")

        with scoped(organization):
            try:
                summary = signal_rank.rank_for(organization)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"Ranked {summary['ranked']} signal(s) against {summary['profile']}.")
            for signal in ranked_open(organization)[:10]:
                self.stdout.write(f"  {signal.rank_score:>3}  {signal.code}  {signal.title}")

            if options["signal"]:
                signal = Signal.objects.filter(code=options["signal"]).first()
                if signal is None:
                    raise CommandError(f"No signal {options['signal']}.")
                try:
                    result = recommendations.draft(organization, signal)
                except recommendations.RecommendationError as exc:
                    raise CommandError(str(exc)) from exc
                if result.recommendation is None:
                    self.stdout.write(self.style.WARNING(f"Declined: {result.declined}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"Drafted for {signal.code}."))
                return

            if options["recommend"] > 0:
                outcome = recommend_top(organization, limit=options["recommend"])
                self.stdout.write(
                    f"Drafted: {', '.join(outcome['drafted']) or '—'} · "
                    f"declined: {', '.join(outcome['declined']) or '—'} · "
                    f"failed: {', '.join(outcome['failed']) or '—'}"
                )

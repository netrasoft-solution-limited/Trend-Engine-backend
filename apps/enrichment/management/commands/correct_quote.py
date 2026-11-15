"""Record a transcription correction on a claim's quote.

    python manage.py correct_quote 6705 "echol polytenique federal deozan" "ETH Zurich"
"""
from django.core.management.base import BaseCommand, CommandError

from apps.enrichment.corrections import CorrectionError, correct_quote
from apps.enrichment.models import Claim


class Command(BaseCommand):
    help = "Correct a transcription error in a quote; shown in brackets in every output."

    def add_arguments(self, parser):
        parser.add_argument("claim_id", type=int)
        parser.add_argument("wrong")
        parser.add_argument("right")
        parser.add_argument("--by", default="correct_quote")

    def handle(self, *args, claim_id, wrong, right, by, **options):
        claim = Claim.objects.filter(pk=claim_id).first()
        if claim is None:
            raise CommandError(f"No claim C{claim_id}.")
        try:
            correct_quote(claim, wrong=wrong, right=right, actor_label=by)
        except CorrectionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"C{claim_id}: {claim.shown_quote}")

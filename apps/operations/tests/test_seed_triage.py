"""`seed_triage` reproduces the ops frontend mock's Triage summary."""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from apps.ingestion.models import IngestionRun
from apps.intelligence.models import Digest, Signal
from apps.scoring.ranking import ranked_candidates
from apps.sources.models import Source
from apps.tenancy.context import scoped
from apps.tenancy.models import Organization

pytestmark = pytest.mark.django_db


def seed():
    call_command("seed_triage", stdout=StringIO())


@pytest.fixture
def seeded():
    seed()


def test_counts_match_the_mock(seeded):
    previous = Digest.objects.order_by("-created_at")[1]

    new_candidates = Signal.objects.filter(
        state=Signal.State.CANDIDATE, first_seen_at__gt=previous.created_at
    ).count()
    blocking = (
        IngestionRun.objects.filter(status=IngestionRun.Status.FAILED, blocking=True).count()
        + Source.objects.filter(status=Source.Status.FAILED).count()
    )

    assert new_candidates == 12
    assert Signal.objects.filter(awaiting_operator_judgment=True).count() == 4
    assert Signal.objects.filter(needs_scientific_routing=True).count() == 1
    assert blocking == 0


def test_sig_2041_ranks_first_for_jarrow(seeded):
    jarrow = Organization.objects.get(slug="jarrow")
    with scoped(jarrow):
        assert ranked_candidates(jarrow).first().code == "SIG-2041"


def test_reseeding_changes_nothing(seeded):
    before = (Signal.objects.count(), Source.objects.count(), IngestionRun.objects.count(),
              Digest.objects.count())

    seed()

    after = (Signal.objects.count(), Source.objects.count(), IngestionRun.objects.count(),
             Digest.objects.count())
    assert after == before

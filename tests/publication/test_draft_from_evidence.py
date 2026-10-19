"""`draft_from_evidence` — the command that produced the 1 October brief.

It drafted from the whole corpus under a fixed profile label, and recorded a
sign-off under whatever name it was given with a note it wrote itself. These
tests hold it to the rules every other path now follows.
"""
from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command

from apps.outputs.models import ExpertReviewer, Output
from apps.tenancy.context import operator_scope
from tests.outputs import test_briefing as briefs

pytestmark = pytest.mark.django_db

evidence = briefs.evidence
jarrow = briefs.jarrow


def test_it_refuses_a_client_with_no_live_profile(org_b, evidence):
    with pytest.raises(CommandError, match="no live client profile"):
        call_command("draft_from_evidence", org=org_b.slug, reviewer="Dr Jane Doe")


def test_it_refuses_without_a_named_reviewer(jarrow):
    from apps.scoring import signal_rank
    from apps.tenancy.context import scoped

    with scoped(jarrow):
        signal_rank.rank_for(jarrow)

    with pytest.raises(CommandError, match="--reviewer"):
        call_command("draft_from_evidence", org=jarrow.slug)


def test_it_cites_the_stored_profile_and_writes_no_note_for_the_reviewer(jarrow):
    from apps.scoring import signal_rank
    from apps.tenancy.context import scoped

    ExpertReviewer.objects.create(name="Dr Jane Doe", qualification="RD", email="jane@r.test")
    with scoped(jarrow):
        signal_rank.rank_for(jarrow)

    call_command("draft_from_evidence", org=jarrow.slug, reviewer="Dr Jane Doe", no_publish=True)

    with operator_scope():
        output = Output.objects.get()
        review = output.expert_reviews.get()
    assert output.client_profile_version == "v1"
    assert review.note == ""

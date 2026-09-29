"""Signal and Digest: the state fields the Triage home counts on."""
from __future__ import annotations

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.intelligence.models import Digest, Signal

pytestmark = pytest.mark.django_db


def make_signal(code="SIG-0001", **fields) -> Signal:
    fields.setdefault("domain_score", 50)
    return Signal.objects.create(code=code, title=f"Signal {code}", **fields)


def test_signal_states_are_the_five_triage_states():
    assert Signal.State.values == ["candidate", "in review", "watching", "approved", "rejected"]


def test_a_new_signal_is_a_candidate_with_no_flags_set():
    signal = make_signal()

    assert signal.state == Signal.State.CANDIDATE
    assert signal.first_seen_at is not None
    assert signal.awaiting_operator_judgment is False
    assert signal.needs_scientific_routing is False


def test_the_two_flags_are_independent_of_state_and_of_each_other():
    make_signal("SIG-0001", state=Signal.State.IN_REVIEW, awaiting_operator_judgment=True)
    make_signal("SIG-0002", state=Signal.State.CANDIDATE, needs_scientific_routing=True)
    make_signal(
        "SIG-0003",
        state=Signal.State.WATCHING,
        awaiting_operator_judgment=True,
        needs_scientific_routing=True,
    )

    assert Signal.objects.filter(awaiting_operator_judgment=True).count() == 2
    assert Signal.objects.filter(needs_scientific_routing=True).count() == 2


def test_an_unknown_state_fails_validation():
    signal = Signal(code="SIG-0001", title="x", domain_score=50, state="in_review")
    with pytest.raises(ValidationError) as exc:
        signal.full_clean()
    assert "state" in exc.value.message_dict


def test_signal_codes_are_unique():
    make_signal("SIG-2041")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_signal("SIG-2041")


def test_domain_score_above_100_is_refused_by_the_database():
    with pytest.raises(IntegrityError), transaction.atomic():
        make_signal(domain_score=101)


def test_digest_window_must_not_end_before_it_starts():
    with pytest.raises(IntegrityError), transaction.atomic():
        Digest.objects.create(window_start=date(2026, 9, 20), window_end=date(2026, 9, 13))


def test_latest_digest_is_the_most_recently_created():
    from datetime import UTC, datetime

    Digest.objects.create(
        window_start=date(2026, 9, 6), window_end=date(2026, 9, 13),
        created_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    newest = Digest.objects.create(
        window_start=date(2026, 9, 13), window_end=date(2026, 9, 20),
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
    )

    assert Digest.objects.latest() == newest

"""Candidate ranking: domain score, then client fit, then confidence."""
from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.intelligence.models import Signal
from apps.scoring.models import ClientSignalScore
from apps.scoring.ranking import ranked_candidates
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.exceptions import TenantScopeError
from apps.tenancy.models import Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def jarrow(db):
    return Organization.objects.create(slug="jarrow", name="Jarrow Formulas")


@pytest.fixture
def other(db):
    return Organization.objects.create(slug="other", name="Other client")


def signal(code, domain, state=Signal.State.CANDIDATE) -> Signal:
    return Signal.objects.create(code=code, title=code, domain_score=domain, state=state)


def score(org, sig, fit, confidence):
    with operator_scope():
        return ClientSignalScore.objects.create(
            organization=org, signal=sig, fit_score=fit, confidence=confidence
        )


def codes(org) -> list[str]:
    with scoped(org):
        return [s.code for s in ranked_candidates(org)]


def test_domain_score_ranks_first(jarrow):
    score(jarrow, signal("SIG-A", 60), fit=99, confidence=99)
    score(jarrow, signal("SIG-B", 82), fit=10, confidence=10)

    assert codes(jarrow) == ["SIG-B", "SIG-A"]


def test_fit_breaks_a_domain_score_tie(jarrow):
    score(jarrow, signal("SIG-A", 70), fit=40, confidence=99)
    score(jarrow, signal("SIG-B", 70), fit=80, confidence=10)

    assert codes(jarrow) == ["SIG-B", "SIG-A"]


def test_confidence_breaks_a_domain_and_fit_tie(jarrow):
    score(jarrow, signal("SIG-A", 70), fit=50, confidence=60)
    score(jarrow, signal("SIG-B", 70), fit=50, confidence=90)

    assert codes(jarrow) == ["SIG-B", "SIG-A"]


def test_code_breaks_a_full_tie_so_the_order_is_deterministic(jarrow):
    score(jarrow, signal("SIG-B", 70), fit=50, confidence=50)
    score(jarrow, signal("SIG-A", 70), fit=50, confidence=50)

    assert codes(jarrow) == ["SIG-A", "SIG-B"]


def test_only_candidates_are_ranked(jarrow):
    score(jarrow, signal("SIG-A", 90, state=Signal.State.IN_REVIEW), fit=90, confidence=90)
    score(jarrow, signal("SIG-B", 50), fit=50, confidence=50)

    assert codes(jarrow) == ["SIG-B"]


def test_an_unscored_candidate_ranks_after_scored_ones_on_the_same_domain_score(jarrow):
    signal("SIG-A", 70)  # no score for Jarrow
    score(jarrow, signal("SIG-B", 70), fit=1, confidence=1)

    assert codes(jarrow) == ["SIG-B", "SIG-A"]


def test_ranking_uses_only_the_given_clients_scores(jarrow, other):
    a, b = signal("SIG-A", 70), signal("SIG-B", 70)
    score(jarrow, a, fit=90, confidence=50)
    score(jarrow, b, fit=20, confidence=50)
    score(other, a, fit=20, confidence=50)
    score(other, b, fit=90, confidence=50)

    assert codes(jarrow) == ["SIG-A", "SIG-B"]
    assert codes(other) == ["SIG-B", "SIG-A"]


def test_ranked_rows_carry_the_clients_fit_and_confidence(jarrow):
    score(jarrow, signal("SIG-A", 70), fit=82, confidence=74)

    with scoped(jarrow):
        top = ranked_candidates(jarrow).first()

    assert (top.fit_score, top.confidence) == (82, 74)


def test_ranking_without_a_bound_tenant_raises(jarrow):
    signal("SIG-A", 70)
    with pytest.raises(TenantScopeError):
        list(ranked_candidates(jarrow))


def test_one_score_per_client_per_signal(jarrow):
    sig = signal("SIG-A", 70)
    score(jarrow, sig, fit=50, confidence=50)
    with pytest.raises(IntegrityError), transaction.atomic():
        score(jarrow, sig, fit=60, confidence=60)

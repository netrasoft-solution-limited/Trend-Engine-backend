"""Fixtures for the publication gate.

Built by hand rather than through the generic `make_row`, because these tests
turn on *states* — approved versus draft, signed off versus not — and a factory
that fills fields with whatever satisfies the constraints would give every
version the same state and quietly make half the suite tautological.

Every fixture runs inside `operator_scope()`: creating a tenant-scoped row goes
through `TenantScopedManager`, which is default-deny, and the operator plane is
where publication actually happens.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.operations.models import AuditEvent
from apps.outputs.models import ExpertReview, Output, OutputState, OutputType, OutputVersion
from apps.tenancy.context import operator_scope

#: The default fixture type is a visibility benchmark, because it is one of the
#: two types that do NOT require expert sign-off (Arch §9.2). Using a health
#: type here would mean every "ordinary publication" test tripped the expert
#: gate, and the suite would be testing that gate over and over instead of the
#: thing it names.
PLAIN = OutputType.VISIBILITY_BENCHMARK


def _output(organization, type_=PLAIN, state=OutputState.DRAFTING) -> Output:
    with operator_scope():
        output = Output.objects.create(
            organization=organization,
            type=type_,
            title="Magnesium glycinate momentum",
            state=state,
        )
    if type_ is PLAIN:
        assert not output.requires_expert_review, (
            "the plain fixture type must not require expert review"
        )
    return output


def _version(output, *, number=1, state=OutputState.DRAFT) -> OutputVersion:
    with operator_scope():
        return OutputVersion.objects.create(
            organization=output.organization,
            output=output,
            number=number,
            summary="Search and podcast mentions both rising.",
            body=[{"heading": "What moved", "text": "Mentions up 40% week on week."}],
            state=state,
        )


@pytest.fixture
def draft_version(organization):
    """Not approved. Publishing this must be refused — PRD §6.9."""
    return _version(_output(organization))


@pytest.fixture
def approved_version(organization):
    output = _output(organization, state=OutputState.APPROVED)
    return _version(output, number=1, state=OutputState.APPROVED)


@pytest.fixture
def later_version(approved_version):
    """A second approved version of the SAME output.

    Same output on purpose: "only one published version visible at a time" is a
    claim about one output, and two unrelated outputs would not test it.
    """
    return _version(approved_version.output, number=2, state=OutputState.APPROVED)


@pytest.fixture
def approved_research_alert(organization):
    """Approved, and of a type that requires expert sign-off — with none recorded.

    Arch §9.2 makes this the one case where approval is not enough.
    """
    output = _output(
        organization, type_=OutputType.RESEARCH_ALERT, state=OutputState.APPROVED
    )
    assert output.requires_expert_review, (
        "this fixture is pointless unless the type genuinely requires review"
    )
    return _version(output, state=OutputState.APPROVED)


@pytest.fixture
def signed_off_research_alert(organization):
    """The same, but with the sign-off recorded — the control case."""
    output = _output(
        organization, type_=OutputType.RESEARCH_ALERT, state=OutputState.APPROVED
    )
    with operator_scope():
        ExpertReview.objects.create(
            organization=organization,
            output=output,
            reviewer_label="Dr Reviewer",
            signed_off_at=timezone.now(),
        )
    return _version(output, state=OutputState.APPROVED)


@pytest.fixture
def published(approved_version, organization, operator):
    from apps.publication import services

    with operator_scope():
        return services.publish(
            version=approved_version,
            organization=organization,
            actor_label=operator.email,
        )


@pytest.fixture
def audit_log(db):
    """The audit table as a queryset, so a test can assert on what was written.

    A manager rather than a list: the events are written inside the service's
    transaction, and a snapshot taken at fixture time would always be empty.
    """
    return AuditEvent.objects

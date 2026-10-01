"""Drafting and approval.

One test here matters more than the rest: that `approve()` creates no
Publication. PRD §6.9 exists because collapsing approval into publication
"removes the operator's last point of control and makes accidental disclosure a
single mis-click" — and the cheapest way to reintroduce that is for someone to
helpfully publish from inside approve() so the happy path has fewer steps.
"""
from __future__ import annotations

import pytest

from apps.operations.models import AuditEvent
from apps.outputs import services
from apps.outputs.models import Approval, ExpertReview, Output, OutputState, OutputType, OutputVersion
from apps.publication import services as gate
from apps.publication.models import Publication
from apps.tenancy.context import operator_scope
from apps.tenancy.models import Organization

pytestmark = pytest.mark.django_db

SECTIONS = [
    {"heading": "What moved", "text": "Magnesium glycinate mentions up 40% week on week."},
    {"heading": "Why", "text": "Three independent creators covered the sleep trials."},
]


@pytest.fixture
def org():
    return Organization.objects.create(
        slug="jarrow", name="Jarrow Formulas", status=Organization.Status.ACTIVE
    )


@pytest.fixture(autouse=True)
def _operator():
    """Every write here is an operator-plane action, so bind the scope once."""
    with operator_scope():
        yield


def drafted(org, **overrides) -> OutputVersion:
    kwargs = dict(
        type=OutputType.VISIBILITY_BENCHMARK,  # no expert review required
        title="Magnesium momentum",
        sections=SECTIONS,
        actor_label="abubakar@netrasoft",
    )
    kwargs.update(overrides)
    return services.draft(org, **kwargs)


# ── Drafting ────────────────────────────────────────────────────────────────


def test_draft_creates_an_output_and_its_first_version(org):
    version = drafted(org)

    assert version.number == 1
    assert version.state == OutputState.DRAFT
    assert version.body == SECTIONS
    assert version.output.state == OutputState.DRAFT
    assert version.organization == org


def test_versions_are_numbered_per_output_and_never_reused(org):
    first = drafted(org)
    second = drafted(org)

    assert (first.number, second.number) == (1, 2)
    assert first.output_id == second.output_id, "same title means the same output"
    assert OutputVersion.objects.filter(output=first.output).count() == 2


def test_an_output_with_no_sections_is_refused(org):
    """An empty body is not a draft, and letting one through means a client
    eventually opens a brief with nothing in it."""
    with pytest.raises(services.DraftingError, match="empty row"):
        drafted(org, sections=[])


def test_the_versions_an_output_was_built_from_are_recorded(org):
    """PRD §6.4: exactly one tenant, one client-profile version and one
    domain-pack version — so an output stays explainable against what it was
    actually built from, not against whatever is current later."""
    version = drafted(
        org, client_profile_version="jarrow@2026-10-01", domain_pack_version="supplements@0.1.0"
    )

    assert version.output.client_profile_version == "jarrow@2026-10-01"
    assert version.output.domain_pack_version == "supplements@0.1.0"


def test_drafting_is_audited(org):
    drafted(org)

    assert AuditEvent.objects.filter(
        kind=AuditEvent.Kind.CONFIG, message__icontains="Drafted"
    ).exists()


# ── Approval, and the line it must not cross ────────────────────────────────


def test_approve_moves_the_version_and_records_who(org):
    version = drafted(org)

    approval = services.approve(version, actor_label="mark@pureplay.example")
    version.refresh_from_db()

    assert version.state == OutputState.APPROVED
    assert version.output.state == OutputState.APPROVED
    assert approval.approver_label == "mark@pureplay.example"
    assert Approval.objects.filter(version=version).count() == 1


def test_approve_creates_no_publication(org):
    """THE test. PRD §6.9: approving is an internal record and must not make
    anything client-visible. A helpful publish inside approve() would remove
    the operator's last point of control."""
    version = drafted(org)

    services.approve(version, actor_label="abubakar@netrasoft")

    assert Publication.objects.count() == 0
    assert not gate.published_for(org).exists()


def test_approval_is_audited_as_an_approval_not_a_publication(org):
    version = drafted(org)
    services.approve(version, actor_label="abubakar@netrasoft")

    event = AuditEvent.objects.filter(kind=AuditEvent.Kind.APPROVAL).latest("at")
    assert "not published" in event.message


def test_a_draft_version_cannot_be_published(org):
    """The other side of the same line: the gate refuses anything approve()
    has not touched."""
    version = drafted(org)

    with pytest.raises(gate.NotApproved):
        gate.publish(version=version, organization=org, actor_label="abubakar@netrasoft")


def test_approve_then_publish_is_the_whole_path(org):
    version = drafted(org)
    services.approve(version, actor_label="abubakar@netrasoft")

    publication = gate.publish(
        version=version, organization=org, actor_label="abubakar@netrasoft"
    )

    assert gate.published_for(org).count() == 1
    assert publication.body == SECTIONS, "the snapshot is frozen onto the publication"


def test_a_published_version_cannot_be_re_approved(org):
    version = drafted(org)
    services.approve(version, actor_label="abubakar@netrasoft")
    gate.publish(version=version, organization=org, actor_label="abubakar@netrasoft")
    version.refresh_from_db()

    with pytest.raises(services.DraftingError, match="already published"):
        services.approve(version, actor_label="abubakar@netrasoft")


# ── Expert sign-off ─────────────────────────────────────────────────────────


def test_a_health_output_needs_a_recorded_signoff_before_publication(org):
    """Arch §9.2: stricter than approval, because publication reaches the
    client."""
    version = drafted(org, type=OutputType.TREND_BRIEF, title="Creatine brief")
    services.approve(version, actor_label="abubakar@netrasoft")

    with pytest.raises(gate.ExpertReviewMissing):
        gate.publish(version=version, organization=org, actor_label="abubakar@netrasoft")


def test_with_the_signoff_recorded_the_same_output_publishes(org):
    version = drafted(org, type=OutputType.TREND_BRIEF, title="Creatine brief")
    services.record_expert_signoff(version.output, reviewer_label="Dr M. Alvarez")
    services.approve(version, actor_label="abubakar@netrasoft")

    gate.publish(version=version, organization=org, actor_label="abubakar@netrasoft")

    assert gate.published_for(org).count() == 1


def test_an_unnamed_reviewer_is_not_a_record(org):
    version = drafted(org)

    with pytest.raises(services.DraftingError, match="named reviewer"):
        services.record_expert_signoff(version.output, reviewer_label="   ")


def test_the_signoff_is_stored_against_the_output(org):
    version = drafted(org)
    services.record_expert_signoff(
        version.output, reviewer_label="Dr M. Alvarez", discipline="Nutrition science"
    )

    review = ExpertReview.objects.get(output=version.output)
    assert review.signed_off_at is not None
    assert review.discipline == "Nutrition science"


# ── Tenancy ─────────────────────────────────────────────────────────────────


def test_everything_written_carries_the_tenant(org):
    """These are L6 models; an unscoped row is the leak Arch §5.2 prevents."""
    version = drafted(org)
    services.approve(version, actor_label="abubakar@netrasoft")
    services.record_expert_signoff(version.output, reviewer_label="Dr M. Alvarez")

    assert Output.objects.get().organization == org
    assert OutputVersion.objects.get().organization == org
    assert Approval.objects.get().organization == org
    assert ExpertReview.objects.get().organization == org

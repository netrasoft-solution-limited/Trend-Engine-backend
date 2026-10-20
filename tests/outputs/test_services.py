"""Drafting and approval.

Lives in `tests/`, not `apps/outputs/tests/`, because it asserts what the
publication gate does with what drafting produces — and `.importlinter` puts
`apps.publication` above `apps.outputs`, so a test inside the app may not
import it. The top-level tree is outside the `apps` root package and can see
both sides.

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
from apps.outputs.models import (
    Approval,
    ExpertReview,
    ExpertReviewer,
    Output,
    OutputState,
    OutputType,
    OutputVersion,
)
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


@pytest.fixture
def reviewer(db):
    return ExpertReviewer.objects.create(
        name="Dr Jane Doe", qualification="RD, PhD nutrition science", email="jane@review.test"
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
    from apps.clients import services as profiles

    profiles.draft(org, label="first", copy_current=False)
    version = drafted(
        org, client_profile_version="v1", domain_pack_version="supplements@0.1.0"
    )

    assert version.output.client_profile_version == "v1"
    assert version.output.domain_pack_version == "supplements@0.1.0"


def test_an_output_cannot_cite_a_profile_that_does_not_exist(org):
    """The 1 October brief cited "jarrow@2026-10-01", which matched no stored
    profile because none existed."""
    with pytest.raises(services.DraftingError, match="no stored client profile"):
        drafted(org, client_profile_version="jarrow@2026-10-01")


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


def test_with_the_signoff_recorded_the_same_output_publishes(org, reviewer):
    version = drafted(org, type=OutputType.TREND_BRIEF, title="Creatine brief")
    services.record_expert_signoff(
        version.output, reviewer_label="Dr Jane Doe", actor_label="abubakar@netrasoft"
    )
    services.approve(version, actor_label="abubakar@netrasoft")

    gate.publish(version=version, organization=org, actor_label="abubakar@netrasoft")

    assert gate.published_for(org).count() == 1


def test_an_unnamed_reviewer_is_not_a_record(org):
    version = drafted(org)

    with pytest.raises(services.DraftingError, match="named reviewer"):
        services.record_expert_signoff(
            version.output, reviewer_label="   ", actor_label="abubakar@netrasoft"
        )


def test_a_reviewer_who_is_not_registered_is_refused(org, reviewer):
    """The name on the 1 October sign-off is the placeholder these tests use."""
    version = drafted(org)

    with pytest.raises(services.DraftingError, match="not a registered expert reviewer"):
        services.record_expert_signoff(
            version.output, reviewer_label="Dr M. Alvarez, nutrition science",
            actor_label="abubakar@netrasoft",
        )


def test_the_operator_cannot_sign_off_their_own_output(org, reviewer):
    version = drafted(org)

    with pytest.raises(services.DraftingError, match="cannot be the reviewer"):
        services.record_expert_signoff(
            version.output, reviewer_label="Dr Jane Doe", actor_label="jane@review.test"
        )


def test_the_signoff_is_stored_against_the_output(org, reviewer):
    version = drafted(org)
    services.record_expert_signoff(
        version.output, reviewer_label="dr jane doe", actor_label="abubakar@netrasoft",
        discipline="Nutrition science",
    )

    review = ExpertReview.objects.get(output=version.output)
    assert review.signed_off_at is not None
    assert review.discipline == "Nutrition science"
    assert review.reviewer_label == "Dr Jane Doe, RD, PhD nutrition science"
    event = AuditEvent.objects.get(kind=AuditEvent.Kind.REVIEW)
    assert event.actor_label == "abubakar@netrasoft"
    assert event.context["reviewer_email"] == "jane@review.test"


# ── Tenancy ─────────────────────────────────────────────────────────────────


def test_everything_written_carries_the_tenant(org, reviewer):
    """These are L6 models; an unscoped row is the leak Arch §5.2 prevents."""
    version = drafted(org)
    services.approve(version, actor_label="abubakar@netrasoft")
    services.record_expert_signoff(
        version.output, reviewer_label="Dr Jane Doe", actor_label="abubakar@netrasoft"
    )

    assert Output.objects.get().organization == org
    assert OutputVersion.objects.get().organization == org
    assert Approval.objects.get().organization == org
    assert ExpertReview.objects.get().organization == org



# ── Correcting a record without rewriting it (Mark, 19 Oct) ─────────────────


def test_a_record_note_is_append_only_and_audited(org):
    from apps.outputs.models import RecordNote

    version = drafted(org)
    note = services.add_record_note(
        version.output, text="No client profile existed for this run.", author_label="abu"
    )

    with pytest.raises(ValueError):
        note.text = "rewritten"
        note.save()
    with pytest.raises(ValueError):
        note.delete()
    assert RecordNote.objects.get().text == "No client profile existed for this run."
    assert AuditEvent.objects.filter(message__startswith="Record note added").exists()


def test_every_export_of_a_corrected_output_opens_with_the_correction(org):
    """Mark, 20 Oct: an app-only note does not protect someone downloading the
    old file. The correction goes first, in every format, under a notice."""
    from apps.outputs import exports
    from apps.outputs.exports.adapters import document_for_version

    version = drafted(org)
    services.add_record_note(
        version.output, text="No expert review supports the recorded sign-off.", author_label="abu"
    )

    document = document_for_version(version)
    assert document.sections[0].heading == "Correction to this record"
    for fmt in ("html", "markdown", "csv"):
        body = exports.render(document, fmt).content.decode()
        assert exports.CORRECTION_NOTICE in body, fmt
        assert "No expert review supports the recorded sign-off." in body, fmt
        assert body.index("No expert review") < body.index("Magnesium glycinate mentions"), fmt


def test_an_uncorrected_output_carries_no_correction_notice(org):
    from apps.outputs import exports
    from apps.outputs.exports.adapters import document_for_version

    html = exports.render(document_for_version(drafted(org)), "html").content.decode()

    assert exports.CORRECTION_NOTICE not in html


def test_audit_events_cannot_be_changed_or_removed(org):
    from apps.operations.models import ImmutableRecord

    drafted(org)
    event = AuditEvent.objects.first()

    with pytest.raises(ImmutableRecord):
        event.message = "rewritten"
        event.save()
    with pytest.raises(ImmutableRecord):
        event.delete()
    with pytest.raises(ImmutableRecord):
        AuditEvent.objects.update(message="rewritten")
    with pytest.raises(ImmutableRecord):
        AuditEvent.objects.all().delete()

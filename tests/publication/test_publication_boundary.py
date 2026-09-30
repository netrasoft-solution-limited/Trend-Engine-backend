"""Arch §9.3 — the gate is the only door.

The import-linter contract in `.importlinter` covers the static half of this.
These cover the behavioural half: that approval genuinely does not publish, and
that a withdrawal genuinely removes access.

The actor is passed as `actor_label`, a string, not as a user object. That is
not a convenience — `operations.AuditEvent` deliberately carries no user
foreign key, so that an audit row outlives the account it names and so that the
operator and org realms can both write to one table. See the note on that model.
"""
from __future__ import annotations

import pytest

from apps.publication import services
from apps.tenancy.context import operator_scope

pytestmark = [pytest.mark.django_db, pytest.mark.publication]


def test_approving_does_not_publish(approved_version, organization):
    """PRD §6.9. The whole reason the two states exist."""
    with operator_scope():
        assert not services.published_for(organization).filter(
            version=approved_version
        ).exists()


def test_publishing_makes_exactly_one_version_visible(approved_version, organization, operator):
    with operator_scope():
        services.publish(
            version=approved_version, organization=organization, actor_label=operator.email
        )
        assert services.published_for(organization).count() == 1


def test_publishing_a_second_version_retires_the_first(
    approved_version, later_version, organization, operator
):
    """Only one published version per output per tenant is visible at a time."""
    with operator_scope():
        services.publish(
            version=approved_version, organization=organization, actor_label=operator.email
        )
        services.publish(
            version=later_version, organization=organization, actor_label=operator.email
        )

        visible = services.published_for(organization)
        assert visible.count() == 1
        assert visible.first().version == later_version


def test_unapproved_versions_cannot_be_published(draft_version, organization, operator):
    with operator_scope(), pytest.raises(services.NotApproved):
        services.publish(
            version=draft_version, organization=organization, actor_label=operator.email
        )


def test_scientific_output_needs_expert_signoff_before_publication(
    approved_research_alert, organization, operator
):
    """Arch §9.2: stricter than approval, because publication reaches the client."""
    with operator_scope(), pytest.raises(services.ExpertReviewMissing):
        services.publish(
            version=approved_research_alert,
            organization=organization,
            actor_label=operator.email,
        )


def test_a_signed_off_scientific_output_can_be_published(
    signed_off_research_alert, organization, operator
):
    """The control case. Without it, the test above would also pass if the
    expert-review gate refused everything."""
    with operator_scope():
        services.publish(
            version=signed_off_research_alert,
            organization=organization,
            actor_label=operator.email,
        )
        assert services.published_for(organization).count() == 1


def test_unpublish_removes_access_and_is_audited(published, organization, operator, audit_log):
    with operator_scope():
        services.unpublish(
            publication=published, actor_label=operator.email, reason="citation error"
        )

        assert not services.published_for(organization).exists()
    assert audit_log.filter(kind="publication", message__icontains="unpublish").exists()


def test_a_withdrawn_publication_resolves_to_nothing(published, operator):
    """PRD §6.9: it must not fall back to an older version, which would show
    the client something they were not reading before."""
    with operator_scope():
        services.unpublish(
            publication=published, actor_label=operator.email, reason="citation error"
        )
        assert services.publication_by_id(published.pk) is None


def test_publishing_writes_an_audit_event(approved_version, organization, operator, audit_log):
    with operator_scope():
        services.publish(
            version=approved_version, organization=organization, actor_label=operator.email
        )

    event = audit_log.filter(kind="publication").latest("at")
    assert event.actor_label == operator.email
    assert event.organization == organization
    assert organization.name in event.message

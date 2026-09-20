"""Arch §9.3 — the gate is the only door.

The import-linter contract in `.importlinter` covers the static half of this.
These cover the behavioural half: that approval genuinely does not publish, and
that a withdrawal genuinely removes access.
"""
from __future__ import annotations

import pytest

from apps.publication import services

pytestmark = pytest.mark.django_db


def test_approving_does_not_publish(approved_version, organization):
    """PRD §6.9. The whole reason the two states exist."""
    assert not services.published_for(organization).filter(
        version=approved_version
    ).exists()


def test_publishing_makes_exactly_one_version_visible(approved_version, organization, operator):
    services.publish(version=approved_version, organization=organization, actor=operator)
    visible = services.published_for(organization)
    assert visible.count() == 1


def test_publishing_a_second_version_retires_the_first(
    approved_version, later_version, organization, operator
):
    """Only one published version per output per tenant is visible at a time."""
    services.publish(version=approved_version, organization=organization, actor=operator)
    services.publish(version=later_version, organization=organization, actor=operator)

    visible = services.published_for(organization)
    assert visible.count() == 1
    assert visible.first().version == later_version


def test_unapproved_versions_cannot_be_published(draft_version, organization, operator):
    with pytest.raises(services.NotApproved):
        services.publish(version=draft_version, organization=organization, actor=operator)


def test_scientific_output_needs_expert_signoff_before_publication(
    approved_research_alert, organization, operator
):
    """Arch §9.2: stricter than approval, because publication reaches the client."""
    with pytest.raises(services.ExpertReviewMissing):
        services.publish(version=approved_research_alert, organization=organization, actor=operator)


def test_unpublish_removes_access_and_is_audited(published, organization, operator, audit_log):
    services.unpublish(publication=published, actor=operator, reason="citation error")

    assert not services.published_for(organization).exists()
    assert audit_log.filter(kind="publication", message__icontains="unpublish").exists()


def test_publishing_writes_an_audit_event(approved_version, organization, operator, audit_log):
    services.publish(version=approved_version, organization=organization, actor=operator)
    event = audit_log.filter(kind="publication").latest("at")
    assert event.actor == operator
    assert str(organization.pk) in event.message

"""The output builder's draft form, and the buttons on Triage and Signal Review.

Mark's 8 October review found that the type list said nothing about which
outputs were built — choosing "Research alert" quietly produced a trend brief
under another name — and that an output drafted from the screen recorded no
profile version, which PRD §8 requires of every output. Since then a trend brief
is built only from approved developments, and a content brief from one approved
recommendation.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.enrichment.domain import DOMAIN_PACK_VERSION
from apps.enrichment.llm.client import LLMClient
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.operations.models import OperatorUser
from apps.outputs import content_brief, exports
from apps.outputs.content_brief import ContentBriefDraft
from apps.outputs.exports.adapters import document_for_version
from apps.outputs.models import Output, OutputType
from apps.tenancy.context import operator_scope, scoped
from tests.outputs import test_briefing as briefs

pytestmark = pytest.mark.django_db

# The two-client corpus, Jarrow's profile, and SIG-3001 approved for Jarrow.
evidence = briefs.evidence
jarrow = briefs.jarrow
sports_brand = briefs.sports_brand
approved = briefs.approved


@pytest.fixture
def real_client(jarrow):
    """`org_a` is created as a fixture organisation; a real client is not one,
    and the difference decides whether its outputs are labelled illustrative."""
    with operator_scope():
        jarrow.is_fixture = False
        jarrow.save(update_fields=["is_fixture"])
    return jarrow


@pytest.fixture
def admin(db):
    return OperatorUser.objects.create_user(
        email="admin@netrasoft.test",
        password="x" * 24,
        name="Platform Admin",
        role=OperatorUser.Role.PLATFORM_ADMIN,
    )


def _draft(client, organization, **extra):
    return client.post(
        reverse("ops-outputs"),
        {"organization": organization.slug, "type": OutputType.TREND_BRIEF, **extra},
        follow=True,
    )


def test_a_type_with_no_generator_is_refused_by_name(client, admin, real_client, approved):
    client.force_login(admin)

    response = _draft(client, real_client, type=OutputType.RESEARCH_ALERT)

    assert "Research alert cannot be drafted yet" in response.content.decode()
    with operator_scope():
        assert not Output.objects.exists()


def test_the_form_marks_the_types_that_are_not_built(client, admin):
    client.force_login(admin)

    page = client.get(reverse("ops-outputs")).content.decode()

    assert "Product / opportunity memo — not built yet" in page
    assert "Trend intelligence brief — not built yet" not in page
    assert "Content brief / draft — not built yet" not in page


def test_a_brief_with_nothing_approved_is_refused_on_screen(client, admin, real_client, evidence):
    client.force_login(admin)

    response = _draft(client, real_client)

    assert "Nothing is approved for" in response.content.decode()
    with operator_scope():
        assert not Output.objects.exists()


def test_a_drafted_brief_records_what_it_was_built_from(client, admin, real_client, approved):
    client.force_login(admin)

    _draft(client, real_client)

    with scoped(real_client):
        output = Output.objects.get()
        version = output.versions.get()
    assert output.title.startswith(f"{real_client.name} — trend brief, "), "a default title"
    assert output.client_profile_version == "v1"
    assert output.domain_pack_version == DOMAIN_PACK_VERSION
    assert version.summary == "1 approved development(s), with recommended next steps."
    assert not output.is_illustrative


def test_an_illustrative_output_is_labelled_on_its_export(client, admin, real_client, approved):
    client.force_login(admin)

    _draft(client, real_client, is_illustrative="1")

    with scoped(real_client):
        output = Output.objects.get()
        document = document_for_version(output.versions.get())
    assert output.is_illustrative
    assert exports.ILLUSTRATIVE_NOTICE in exports.render(document, "html").content.decode()


def test_a_fixture_organisations_output_is_always_illustrative(client, admin, jarrow, approved):
    with operator_scope():
        assert jarrow.is_fixture
    client.force_login(admin)

    _draft(client, jarrow)

    with scoped(jarrow):
        assert Output.objects.get().is_illustrative


def test_a_content_brief_is_drafted_from_its_recommendation(
    client, admin, real_client, approved, monkeypatch
):
    transport = FakeTransport([respond(ContentBriefDraft(
        working_title="Which magnesium, for which goal", audience="Older adults",
        core_question="Which magnesium?", search_intent="Compare", angle="Choose by goal.",
        outline=[],
    ))])
    monkeypatch.setattr(content_brief, "LLMClient", lambda: LLMClient(transport=transport))
    client.force_login(admin)

    client.post(reverse("ops-outputs"), {
        "organization": real_client.slug,
        "type": OutputType.CONTENT_BRIEF,
        "recommendation": approved.pk,
    }, follow=True)

    with scoped(real_client):
        output = Output.objects.get()
    assert output.type == OutputType.CONTENT_BRIEF
    assert output.title == "Which magnesium, for which goal", "the working title by default"
    assert output.requires_expert_review, "content briefs still need expert sign-off to publish"


def test_a_content_brief_without_a_recommendation_says_where_to_start(
    client, admin, real_client, approved
):
    client.force_login(admin)

    response = client.post(reverse("ops-outputs"), {
        "organization": real_client.slug, "type": OutputType.CONTENT_BRIEF,
    }, follow=True)

    assert "Signal Review page" in response.content.decode()


def test_another_clients_recommendation_cannot_be_used(
    client, admin, real_client, sports_brand, approved
):
    """The recommendation is looked up inside the chosen client's scope."""
    client.force_login(admin)

    response = client.post(reverse("ops-outputs"), {
        "organization": sports_brand.slug,
        "type": OutputType.CONTENT_BRIEF,
        "recommendation": approved.pk,
    }, follow=True)

    assert "Choose the approved recommendation" in response.content.decode()
    with operator_scope():
        assert not Output.objects.exists()


# ── Sign-off and record notes on the output page (Mark, 19 Oct) ────────────


def _brief(client, admin, organization):
    client.force_login(admin)
    _draft(client, organization)
    with scoped(organization):
        return Output.objects.get()


def test_an_unregistered_reviewer_is_refused_on_screen(client, admin, real_client, approved):
    output = _brief(client, admin, real_client)

    response = client.post(
        reverse("ops-output-signoff", args=[output.pk]),
        {"reviewer": "Dr M. Alvarez, nutrition science"},
        follow=True,
    )

    assert "not a registered expert reviewer" in response.content.decode()
    with scoped(real_client):
        assert not output.expert_reviews.exists()


def test_the_signoff_form_offers_registered_reviewers_only(client, admin, real_client, approved):
    from apps.outputs.models import ExpertReviewer

    ExpertReviewer.objects.create(name="Dr Jane Doe", qualification="RD", email="jane@r.test")
    output = _brief(client, admin, real_client)

    page = client.get(reverse("ops-output", args=[output.pk])).content.decode()

    assert '<option value="Dr Jane Doe">' in page


def test_record_notes_show_on_the_output_and_in_the_list(client, admin, real_client, approved):
    from apps.outputs import services

    output = _brief(client, admin, real_client)
    with scoped(real_client):
        services.add_record_note(output, text="No client profile existed.", author_label="abu")

    detail = client.get(reverse("ops-output", args=[output.pk])).content.decode()
    listing = client.get(reverse("ops-outputs")).content.decode()

    assert "No client profile existed." in detail
    assert "record note" in listing



def test_a_notification_without_a_delivery_is_not_called_sent(client, admin, real_client, approved):
    """The 1 October page read "Sent to the client" from a notification
    timestamp, with no delivery and nobody at the client receiving anything."""
    from django.utils import timezone

    from apps.outputs import services
    from apps.publication import services as gate
    from apps.publication.models import Publication

    output = _brief(client, admin, real_client)
    with scoped(real_client):
        version = output.versions.get()
        from apps.outputs.models import ExpertReviewer

        ExpertReviewer.objects.create(name="Dr Jane Doe", qualification="RD", email="j@r.test")
        services.record_expert_signoff(output, reviewer_label="Dr Jane Doe", actor_label="abu")
        services.approve(version, actor_label="abu")
        publication = gate.publish(version=version, organization=real_client, actor_label="abu")
        Publication.objects.filter(pk=publication.pk).update(notified_at=timezone.now())

    page = client.get(reverse("ops-output", args=[output.pk])).content.decode()

    assert "Sent to the client" not in page
    assert "Not delivered." in page and "there is no delivery record" in page

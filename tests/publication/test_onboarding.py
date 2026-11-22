"""A second brand set up without leaving the application (Mark, 20 Nov)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.clients import services as client_services
from apps.clients.models import BrandSource, ClientAsset
from apps.enrichment.llm.client import LLMClient
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.publication import onboarding
from apps.sources.models import Source
from apps.tenancy.context import scoped
from tests.publication import test_output_builder as builder

pytestmark = pytest.mark.django_db

admin = builder.admin


@pytest.fixture
def brand(org_a):
    with scoped(org_a):
        draft = client_services.draft(org_a, label="storefront", copy_current=False)
        for name in ("Calcium Plus", "Sleep Gummies", "Kids Probiotic"):
            ClientAsset.objects.create(organization=org_a, profile=draft, name=name)
    return org_a


def proposed():
    return respond(onboarding.Families(families=[
        onboarding.Family(name="Bone health", terms=["Bone Density", "calcium"],
                          query="calcium bone health", listings=[1]),
        onboarding.Family(name="Sleep", terms=["sleep"], query="sleep gummies", listings=[2, 3]),
    ]))


def test_families_are_proposed_for_the_draft_with_terms_and_a_query(brand):
    with scoped(brand):
        done = onboarding.assign_families(
            brand, actor_label="abu", client=LLMClient(transport=FakeTransport([proposed()])))
        draft = onboarding.latest_draft(brand)
        families = onboarding.families_of(draft)

    assert done == {"families": 2, "assigned": 3, "unassigned": 0}
    bone = next(f for f in families if f["name"] == "Bone health")
    assert bone["terms"] == ["bone density", "calcium"] and bone["query"] == "calcium bone health"


def test_sources_are_created_per_family_and_platform_and_linked(brand):
    podcast = Source.objects.create(name="Shared podcast", route=Source.Route.PODCAST)
    with scoped(brand):
        onboarding.assign_families(
            brand, actor_label="abu", client=LLMClient(transport=FakeTransport([proposed()])))
        client_services.activate(onboarding.latest_draft(brand), actor_label="abu")
        done = onboarding.create_sources(brand, actor_label="abu")
        again = onboarding.create_sources(brand, actor_label="abu")
        linked = set(BrandSource.objects.values_list("source_id", flat=True))

    assert done["created"] == 8 and again["created"] == 0, "2 families x 4 platforms, once"
    tiktok = Source.objects.get(name__contains="TikTok — Bone health")
    assert tiktok.config["queries"] == ["calcium bone health"] and tiktok.poll_interval_minutes == 0
    assert podcast.pk in linked and len(linked) == 9


def test_sources_need_a_live_profile_with_families(brand):
    with scoped(brand), pytest.raises(onboarding.OnboardingError, match="live first"):
        onboarding.create_sources(brand, actor_label="abu")


def test_the_setup_page_shows_what_is_left(client, admin, brand):
    client.force_login(admin)

    page = client.get(reverse("ops-cycle-setup", args=[brand.slug])).content.decode()

    assert "3 listing(s) have no family" in page
    assert "Propose families for the draft" in page

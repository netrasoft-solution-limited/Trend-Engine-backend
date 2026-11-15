"""Discovery beyond the fixed candidate list (Mark, 15 Nov)."""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.enrichment.llm.client import LLMClient
from apps.enrichment.models import Claim
from apps.enrichment.tests.fakes import FakeTransport, respond
from apps.evidence.models import ContentItem, ContentSegment
from apps.scoring import discovery
from apps.scoring.models import Candidate, DiscoveredIngredient
from apps.scoring.tests import test_signal_rank as shared
from apps.sources.models import Source
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db

evidence = shared.evidence
jarrow = shared.jarrow


def said(subject, creator, *, n, route=Source.Route.YOUTUBE, sponsored=False):
    source, _ = Source.objects.get_or_create(name=f"{route} source", route=route)
    item = ContentItem.objects.create(
        source=source, external_id=f"{subject}-{n}", title=f"About {subject}", creator=creator,
        content_hash=f"{subject}-{n}", published_at=timezone.now(),
    )
    segment = ContentSegment.objects.create(
        content_item=item, ordinal=1, text=subject, start_char=0, end_char=len(subject)
    )
    return Claim.objects.create(content_item=item, segment=segment, kind="effect",
                                subject=subject, text=subject, quote=subject,
                                is_sponsored=sponsored)


def test_ingredients_the_catalogue_lacks_are_proposed_and_sold_ones_are_not(jarrow):
    for n, creator in enumerate(["Dr A", "Dr B", "Dr C"]):
        said("Creatine monohydrate", creator, n=n, route=Source.Route.PODCAST)
        said("magnesium glycinate", creator, n=10 + n)
    said("Spermidine", "Dr D", n=20)
    said("Spermidine", "Dr E", n=21)
    with scoped(jarrow):
        Candidate.objects.create(organization=jarrow, name="Spermidine")

    subjects = list(discovery.subject_claims(*discovery.window()))
    def number(name):
        return subjects.index(name.lower()) + 1

    transport = FakeTransport([respond(discovery.Naming(ingredients=[
        discovery.Named(ingredient="Creatine monohydrate",
                        subjects=[number("Creatine monohydrate")]),
        discovery.Named(ingredient="Magnesium glycinate", subjects=[number("magnesium glycinate")]),
        discovery.Named(ingredient="Spermidine", subjects=[number("Spermidine")]),
    ]))])

    with scoped(jarrow):
        found = discovery.discover(jarrow, client=LLMClient(transport=transport))

    assert [d.name for d in found] == ["Creatine monohydrate"], "sold and known ones left out"
    creatine = found[0]
    assert (creatine.items, creatine.creators, creatine.channels) == (3, 3, ["podcast"])


def test_promotional_claims_are_not_counted(jarrow):
    for n, creator in enumerate(["Brand A", "Brand B"]):
        said("Shilajit", creator, n=n, sponsored=True)

    assert "shilajit" not in discovery.subject_claims(*discovery.window())


def test_a_proposal_becomes_a_candidate_with_its_evidence(jarrow):
    with scoped(jarrow):
        found = DiscoveredIngredient.objects.create(
            organization=jarrow, name="Creatine monohydrate", items=3, creators=3,
            channels=["podcast"], subjects=["creatine monohydrate"],
            window_start=timezone.localdate(), window_end=timezone.localdate(),
            discovered_at=timezone.now(),
        )
        candidate = discovery.add_as_candidate(found, actor_label="abu")
        found.refresh_from_db()

    assert candidate.name == "Creatine monohydrate"
    assert found.status == DiscoveredIngredient.Status.ADDED and found.candidate == candidate

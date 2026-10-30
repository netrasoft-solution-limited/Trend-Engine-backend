"""Mapping a catalogue inventory into a profile (Mark, 20 Oct).

Keep formula distinctions, keep product URLs, keep the listening families, and
never turn a common word into a matching term.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.clients import catalogue_import, services
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

HEADER = (
    "catalogue_id,product,primary_listening_family,listening_topics,url,listing_status,"
    "variants,available_variants,skus,manufacturer_tags,brief_status,review_note\n"
)
INVENTORY = HEADER + (
    '1,Fem-Dophilus® Ultra - 50 Billion CFU (Refrigerated),Vaginal and urinary health,'
    'Vaginal microbiome; menopause,https://jarrow.com/a,x,1,1,FEMUL30,'
    'ing-probiotics | Women,A separate women’s health story.,Formula-specific review.\n'
    '2,Fem-Dophilus® Ultra - 50 Billion CFU,Vaginal and urinary health,'
    'Vaginal microbiome; menopause,https://jarrow.com/b,x,1,1,FEMSUL30,'
    'ing-probiotics | Women,A separate women’s health story.,Formula-specific review.\n'
    '3,SAMe - 200mg,Stress and relaxation,stress; diet,https://jarrow.com/c,x,2,2,SAM60,'
    'ing-s-adenosyl-methionine,Use the magnesium signal now.,Formula-specific review.\n'
    '4,Biotin,Skin and eyes,biotin; beauty supplements,https://jarrow.com/d,x,1,1,BTN,'
    'ing-biotin | ing-calcium,Keep in monitoring.,Formula-specific review.\n'
)


def test_each_listing_stays_its_own_formula():
    spec, rows = catalogue_import.spec_from_inventory(INVENTORY)
    products = [r for r in rows if r["kind"] == "product"]

    assert [p["name"] for p in products[:2]] == [
        "Fem-Dophilus® Ultra - 50 Billion CFU (Refrigerated)",
        "Fem-Dophilus® Ultra - 50 Billion CFU",
    ]
    assert [p["url"] for p in products[:2]] == ["https://jarrow.com/a", "https://jarrow.com/b"]
    assert "fem-dophilus ultra" in products[0]["terms"]
    assert "probiotic" in products[0]["terms"]


def test_common_words_never_become_terms():
    _, rows = catalogue_import.spec_from_inventory(INVENTORY)
    by_name = {r["name"]: r for r in rows}

    assert by_name["SAMe - 200mg"]["terms"] == ["sam-e", "s-adenosyl methionine"]
    assert "diet" not in by_name["Stress and relaxation"]["terms"]


def test_a_salt_is_not_an_ingredient_outside_its_family():
    _, rows = catalogue_import.spec_from_inventory(INVENTORY)

    assert "calcium" not in {r["name"]: r for r in rows}["Biotin"]["terms"]


def test_families_become_categories_with_their_topics():
    _, rows = catalogue_import.spec_from_inventory(INVENTORY)
    family = {r["name"]: r for r in rows}["Vaginal and urinary health"]

    assert family["kind"] == "category"
    assert family["terms"] == ["vaginal microbiome", "menopause"]


def test_weights_are_neutral_and_the_snapshot_is_labelled():
    spec, rows = catalogue_import.spec_from_inventory(INVENTORY)

    assert {r["weight"] for r in rows} == {50}
    assert all(catalogue_import.SNAPSHOT_NOTE in a["notes"] for a in spec["assets"])


def test_the_import_csv_round_trips_through_the_client_importer():
    _, rows = catalogue_import.spec_from_inventory(INVENTORY)

    spec = services.spec_from_csv(catalogue_import.import_csv(rows))

    assert len(spec["assets"]) == 7  # 4 listings + 3 families
    assert spec["assets"][0]["notes"].startswith("https://jarrow.com/a")


@pytest.mark.django_db
def test_marks_full_inventory_loads_as_a_draft_profile():
    inventory = (
        Path(__file__).resolve().parents[4]
        / "mark-inputs-2026-10-19" / "Jarrow-Full-Portfolio-2026-10-20.csv"
    )
    if not inventory.exists():
        pytest.skip("the inputs folder sits beside the repo, not in it")
    spec, rows = catalogue_import.spec_from_inventory(inventory.read_text(encoding="utf-8"))
    with operator_scope():
        jarrow = Organization.objects.create(name="Jarrow Formulas", slug="jarrow")

    with scoped(jarrow):
        version = services.load_spec(jarrow, spec, actor_label="test")
        assert not version.is_current, "a snapshot is reviewed before it goes live"
        assert version.assets.filter(kind="product").count() == 103
        assert version.assets.filter(kind="category").count() == 14
        gut = version.assets.get(name="Gut & Protein Optimizer")
    assert "https://jarrow.com/products/gut-protein-optimizer" in gut.notes


@pytest.mark.django_db
def test_a_new_profile_version_keeps_each_listings_family():
    """A copied version lost every family, and with it the brief's portfolio
    table (found 30 Oct, after adding manufacturer descriptions)."""
    from apps.clients.models import ClientAsset

    with operator_scope():
        org = Organization.objects.create(name="Brand", slug="brand")
    with scoped(org):
        first = services.draft(org, actor_label="test", copy_current=False)
        ClientAsset.objects.create(organization=org, profile=first, name="Gut Optimizer",
                                   family="Digestion and protein")
        services.activate(first, actor_label="test")
        second = services.draft(org, actor_label="test")
        assert second.assets.get().family == "Digestion and protein"

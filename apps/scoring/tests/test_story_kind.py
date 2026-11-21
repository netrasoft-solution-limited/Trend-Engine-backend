"""Existing product, gap, new format or new audience (Mark, 20 Nov)."""
import pytest

from apps.scoring import story_kind
from apps.scoring.tests import test_signal_rank as shared
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db

evidence = shared.evidence
jarrow = shared.jarrow


def kind(org, text, listings):
    with scoped(org):
        return story_kind.classify(org, text=text, listings=listings)


def test_no_matching_listing_is_a_gap(jarrow):
    assert kind(jarrow, "Spermidine for longevity", []).kind == story_kind.GAP


def test_a_form_the_matched_listings_lack_is_a_new_format(jarrow):
    found = kind(jarrow, "Magnesium gummies for sleep", ["Magnesium Glycinate"])

    assert found.kind == story_kind.FORMAT
    assert "gummy formats (“gummies”)" in found.reason and "Magnesium Glycinate" in found.reason


def test_a_group_no_listing_is_for_is_a_new_audience(jarrow):
    found = kind(jarrow, "Magnesium glycinate for kids' sleep", ["Magnesium Glycinate"])

    assert found.kind == story_kind.AUDIENCE and "children" in found.reason


def test_otherwise_it_supports_an_existing_product(jarrow):
    found = kind(jarrow, "Magnesium glycinate before bed", ["Magnesium Glycinate", "MagMind"])

    assert found.kind == story_kind.EXISTING
    # MagMind (threonate) is matched but not what this story is about.
    assert found.sentence() == "Existing product: supports Magnesium Glycinate."


def test_listings_the_story_does_not_name_are_only_the_nearest(jarrow):
    found = kind(jarrow, "Urolithin A comes from gut bacteria", ["Magnesium Glycinate"])

    assert found.kind == story_kind.GAP
    assert "nearest listings: Magnesium Glycinate" in found.reason


def test_a_group_named_only_in_passing_does_not_make_a_new_audience(jarrow):
    with scoped(jarrow):
        found = story_kind.classify(
            jarrow, text="Magnesium glycinate formats; one post pitched it to kids",
            listings=["Magnesium Glycinate"], title="Magnesium glycinate formats")

    assert found.kind == story_kind.EXISTING

"""Deriving a client profile from their storefront.

The failure this guards against is not "the import crashes". It is a profile
that imports cleanly, looks thorough, and is subtly wrong — because that one
gets activated, and then every brief for that client is quietly built on it.

So the assertions cluster around three things: reading the catalogue correctly,
refusing rather than guessing when it cannot be read, and never producing a
term that would match everything.
"""
from __future__ import annotations

import json

import pytest

from apps.clients import catalogue, discovery
from apps.clients.models import ClientAsset, ClientTerm
from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db


# ── A storefront, faked at the transport ────────────────────────────────────


def transport_for(pages: dict[str, tuple[int, str]]):
    """`conftest.py` blocks real HTTP, so every test supplies its own web."""

    def get(url: str) -> tuple[int, str]:
        return pages.get(url, (404, ""))

    return get


SHOPIFY = {
    "https://brand.example/products.json?limit=250": (
        200,
        json.dumps(
            {
                "products": [
                    {
                        "title": "Magnesium Glycinate 400mg",
                        "handle": "mag-glycinate",
                        "body_html": "<p>Gentle, <b>highly absorbable</b> magnesium.</p>",
                        "product_type": "Minerals",
                        "tags": ["sleep", "magnesium"],
                    },
                    {
                        "title": "Creatine Monohydrate",
                        "handle": "creatine",
                        "body_html": "Micronised creatine.",
                        "product_type": "Performance",
                        "tags": ["strength"],
                    },
                    {"title": "", "handle": "broken"},
                ]
            }
        ),
    )
}


def test_a_shopify_catalogue_is_read_without_scraping():
    """`/products.json` is public, structured and free. Preferring it over a
    crawl is the same cheapest-rung-first discipline the transcript ladder uses."""
    products = catalogue.read("https://brand.example", transport=transport_for(SHOPIFY))

    assert [p.name for p in products] == ["Magnesium Glycinate 400mg", "Creatine Monohydrate"]
    assert products[0].product_type == "Minerals"
    assert products[0].tags == ["sleep", "magnesium"]


def test_product_html_is_stripped_before_it_reaches_a_prompt():
    """Markup in a prompt is tokens paid for and noise reasoned over."""
    products = catalogue.read("https://brand.example", transport=transport_for(SHOPIFY))

    assert "<p>" not in products[0].description
    assert "highly absorbable" in products[0].description


def test_a_bare_domain_is_accepted():
    pages = {k.replace("https://brand.example", "https://brand.example"): v for k, v in SHOPIFY.items()}
    assert catalogue.read("brand.example", transport=transport_for(pages))


# ── The JSON-LD fallback ────────────────────────────────────────────────────


def _ld_page(name: str, description: str = "") -> str:
    return (
        '<html><head><script type="application/ld+json">'
        + json.dumps(
            {"@context": "https://schema.org", "@type": "Product",
             "name": name, "description": description}
        )
        + "</script></head><body></body></html>"
    )


def test_a_non_shopify_store_falls_back_to_structured_markup():
    pages = {
        "https://brand.example/products.json?limit=250": (200, "<html>not json</html>"),
        "https://brand.example/sitemap.xml": (
            200,
            "<urlset><url><loc>https://brand.example/product/zinc</loc></url>"
            "<url><loc>https://brand.example/about</loc></url></urlset>",
        ),
        "https://brand.example/product/zinc": (200, _ld_page("Zinc Picolinate", "30mg")),
    }

    products = catalogue.read("https://brand.example", transport=transport_for(pages))

    assert [p.name for p in products] == ["Zinc Picolinate"]


def test_json_ld_in_a_graph_wrapper_is_still_found():
    """Shopify, WooCommerce and Squarespace each emit a different shape. A
    reader that handles only the flat one works on a third of the web."""
    graph = (
        '<script type="application/ld+json">'
        + json.dumps({"@graph": [{"@type": "WebPage"}, {"@type": "Product", "name": "Iron Bisglycinate"}]})
        + "</script>"
    )
    pages = {
        "https://brand.example/products.json?limit=250": (404, ""),
        "https://brand.example/sitemap.xml": (
            200, "<urlset><url><loc>https://brand.example/product/iron</loc></url></urlset>"
        ),
        "https://brand.example/product/iron": (200, graph),
    }

    assert [p.name for p in catalogue.read("https://brand.example", transport=transport_for(pages))] == [
        "Iron Bisglycinate"
    ]


def test_the_same_product_on_two_urls_is_counted_once():
    """A sitemap crawl sees canonical and collection URLs for one product.
    Listing it twice would weight it twice."""
    pages = {
        "https://brand.example/products.json?limit=250": (404, ""),
        "https://brand.example/sitemap.xml": (
            200,
            "<urlset>"
            "<url><loc>https://brand.example/product/b12</loc></url>"
            "<url><loc>https://brand.example/collections/x/product/b12</loc></url>"
            "</urlset>",
        ),
        "https://brand.example/product/b12": (200, _ld_page("Methyl B-12")),
        "https://brand.example/collections/x/product/b12": (200, _ld_page("Methyl B-12", "longer text")),
    }

    products = catalogue.read("https://brand.example", transport=transport_for(pages))

    assert len(products) == 1
    assert products[0].description == "longer text", "the richer copy is kept"


# ── Refusing beats guessing ─────────────────────────────────────────────────


def test_an_unreadable_storefront_refuses_rather_than_returning_nothing():
    """Silently returning an empty catalogue would produce an empty profile,
    which cannot be activated but also cannot be diagnosed."""
    pages = {
        "https://brand.example/products.json?limit=250": (404, ""),
        "https://brand.example/sitemap.xml": (404, ""),
    }

    with pytest.raises(catalogue.CatalogueError, match="worse than none"):
        catalogue.read("https://brand.example", transport=transport_for(pages))


def test_a_nonsense_address_is_refused_before_any_request():
    with pytest.raises(catalogue.CatalogueError, match="not a usable storefront"):
        catalogue.read("   ", transport=transport_for({}))


def test_one_dead_route_does_not_stop_the_other(monkeypatch):
    """A storefront that errors on products.json must still reach the fallback."""

    def get(url):
        if "products.json" in url:
            raise OSError("connection reset")
        return {
            "https://brand.example/sitemap.xml": (
                200, "<urlset><url><loc>https://brand.example/product/d3</loc></url></urlset>"
            ),
            "https://brand.example/product/d3": (200, _ld_page("Vitamin D3")),
        }.get(url, (404, ""))

    assert [p.name for p in catalogue.read("https://brand.example", transport=get)] == ["Vitamin D3"]


# ── Interpretation: what must never reach a profile ─────────────────────────


class FakeDiscovery:
    """Stands in for the model, so the guardrails can be tested without one."""

    def __init__(self, payload):
        self.payload = payload

    def __call__(self, products, *, transport=None):
        return self.payload


def discovered(**overrides):
    return discovery.DiscoveredProfile(
        **{
            "category": "Dietary supplements",
            "adjacent_categories": ["sports nutrition"],
            "assets": [discovery.Asset(name="Magnesium", kind="ingredient", terms=["magnesium"])],
            "audiences": [],
            "priorities": [],
            **overrides,
        }
    )


@pytest.fixture
def fake_model(monkeypatch):
    def install(payload):
        monkeypatch.setattr(discovery, "interpret", FakeDiscovery(payload))

    return install


def test_a_discovered_profile_is_drafted_and_never_activated(org_a, fake_model):
    """The storefront says what they sell. It does not say what they are
    pushing, and activating on that basis is the operator's call."""
    fake_model(discovered())

    with scoped(org_a):
        version, _ = discovery.draft_from_catalogue(org_a, products=[catalogue.Product(name="x")])

        assert version.is_current is False
        assert version.assets.count() == 1


def test_weights_are_flat_rather_than_invented(org_a, fake_model):
    """A varied set of weights would read as a judgement somebody made."""
    fake_model(
        discovered(
            assets=[
                discovery.Asset(name="A", kind="product", terms=["alpha"]),
                discovery.Asset(name="B", kind="product", terms=["beta"]),
            ]
        )
    )

    with scoped(org_a):
        version, _ = discovery.draft_from_catalogue(org_a, products=[catalogue.Product(name="x")])
        weights = set(version.assets.values_list("weight", flat=True))

    assert weights == {discovery.DEFAULT_WEIGHT}


def test_a_term_too_short_to_be_safe_is_dropped(org_a, fake_model):
    """"d" as a term for vitamin D matches the letter as a word in any sentence.
    Scores built on that are confidently meaningless."""
    fake_model(
        discovered(
            assets=[discovery.Asset(name="Vitamin D", kind="ingredient",
                                    terms=["d", "d3", "vitamin d3", "  ", "!!"])]
        )
    )

    with scoped(org_a):
        version, _ = discovery.draft_from_catalogue(org_a, products=[catalogue.Product(name="x")])
        terms = version.assets.first().terms

    assert terms == ["vitamin d3"], "only terms long enough to be specific survive"


def test_an_asset_with_no_usable_terms_is_dropped_entirely(org_a, fake_model):
    """It could never match. Keeping it pads the profile with rows that look
    like coverage and are not."""
    fake_model(
        discovered(
            assets=[
                discovery.Asset(name="Unusable", kind="product", terms=["a", ""]),
                discovery.Asset(name="Real", kind="product", terms=["creatine"]),
            ]
        )
    )

    with scoped(org_a):
        version, _ = discovery.draft_from_catalogue(org_a, products=[catalogue.Product(name="x")])

        assert list(version.assets.values_list("name", flat=True)) == ["Real"]


def test_duplicate_terms_are_collapsed_case_insensitively(org_a, fake_model):
    fake_model(
        discovered(
            assets=[
                discovery.Asset(name="Mag", kind="ingredient",
                                terms=["Magnesium", "magnesium", "MAGNESIUM", "glycinate"])
            ]
        )
    )

    with scoped(org_a):
        version, _ = discovery.draft_from_catalogue(org_a, products=[catalogue.Product(name="x")])

        assert version.assets.first().terms == ["Magnesium", "glycinate"]


def test_an_unknown_asset_kind_falls_back_rather_than_failing(org_a, fake_model):
    """The model is told the valid kinds; it is not trusted to obey."""
    fake_model(
        discovered(assets=[discovery.Asset(name="Thing", kind="widget", terms=["creatine"])])
    )

    with scoped(org_a):
        version, _ = discovery.draft_from_catalogue(org_a, products=[catalogue.Product(name="x")])

        assert version.assets.first().kind == ClientAsset.Kind.PRODUCT


def test_competitors_are_never_discovered(org_a, fake_model):
    """They are not derivable from a catalogue. A guessed competitor list is
    the kind of thing an operator would assume had been checked."""
    fake_model(
        discovered(
            audiences=[discovery.Term(name="Older adults", terms=["older adults"])],
            priorities=[discovery.Term(name="Sleep", terms=["sleep quality"])],
        )
    )

    with scoped(org_a):
        version, _ = discovery.draft_from_catalogue(org_a, products=[catalogue.Product(name="x")])
        facets = set(version.terms.values_list("facet", flat=True))

    assert ClientTerm.Facet.COMPETITOR not in facets
    assert facets == {ClientTerm.Facet.AUDIENCE, ClientTerm.Facet.PRIORITY}


def test_an_empty_catalogue_is_refused_before_a_model_call(org_a):
    """Paying for a call that can only return nothing."""
    with scoped(org_a), pytest.raises(ValueError, match="empty catalogue"):
        discovery.interpret([])


# ── Sitemap shapes that real storefronts actually use ───────────────────────
#
# Both of these were found against a live client's site after the code looked
# correct in tests built from a sitemap I had written myself.


def test_a_nested_sitemap_with_a_query_string_is_followed():
    """Shopify's index points at `sitemap_products_1.xml?from=…&to=…`, so a
    check of `url.endswith(".xml")` skips the product index and the catalogue
    reads as empty — for one of the most common storefronts there is."""
    pages = {
        "https://brand.example/products.json?limit=250": (429, "rate limited"),
        "https://brand.example/sitemap.xml": (
            200,
            "<sitemapindex><sitemap><loc>"
            "https://brand.example/sitemap_products_1.xml?from=1&amp;to=9"
            "</loc></sitemap></sitemapindex>",
        ),
        "https://brand.example/sitemap_products_1.xml?from=1&to=9": (
            200,
            "<urlset><url><loc>https://brand.example/products/magnesium</loc></url></urlset>",
        ),
        "https://brand.example/products/magnesium": (200, _ld_page("Magnesium Optimizer")),
    }

    products = catalogue.read("https://brand.example", transport=transport_for(pages))

    assert [p.name for p in products] == ["Magnesium Optimizer"]


def test_xml_escaped_urls_are_unescaped_before_being_requested():
    """A sitemap is XML, so `&` arrives as `&amp;`. Requesting it unescaped
    sends a literal "&amp;" and the server answers something else — which
    presents as "this storefront has no products"."""
    asked: list[str] = []

    def get(url):
        asked.append(url)
        return {
            "https://brand.example/products.json?limit=250": (404, ""),
            "https://brand.example/sitemap.xml": (
                200,
                "<urlset><url><loc>"
                "https://brand.example/sitemap_products_1.xml?a=1&amp;b=2"
                "</loc></url></urlset>",
            ),
        }.get(url, (404, ""))

    with pytest.raises(catalogue.CatalogueError):
        catalogue.read("https://brand.example", transport=get)

    assert "https://brand.example/sitemap_products_1.xml?a=1&b=2" in asked
    assert not any("&amp;" in url for url in asked)


def test_a_throttled_crawl_refuses_rather_than_returning_a_fraction():
    """The launch client's storefront answers 429 to most pages. A throttled
    page is not an error — it is a page with no product on it — so without a
    count the import succeeds and builds a confident profile from 8 of 104
    products. That is the failure this whole module keeps producing in
    different forms: something that looks complete and is not."""
    pages = {
        "https://brand.example/products.json?limit=250": (429, ""),
        "https://brand.example/sitemap.xml": (
            200,
            "<urlset>"
            + "".join(
                f"<url><loc>https://brand.example/products/p{n}</loc></url>" for n in range(6)
            )
            + "</urlset>",
        ),
        "https://brand.example/products/p0": (200, _ld_page("The one that got through")),
        # The rest are throttled, which is what the real site does.
        **{f"https://brand.example/products/p{n}": (429, "") for n in range(1, 6)},
    }

    with pytest.raises(catalogue.CatalogueError, match="rate-limiting us"):
        catalogue.read("https://brand.example", transport=transport_for(pages))


def test_a_mostly_successful_crawl_still_returns_what_it_found():
    """One blocked page among many must not throw away a good import."""
    pages = {
        "https://brand.example/products.json?limit=250": (404, ""),
        "https://brand.example/sitemap.xml": (
            200,
            "".join(
                f"<url><loc>https://brand.example/products/p{n}</loc></url>" for n in range(3)
            ),
        ),
        "https://brand.example/products/p0": (200, _ld_page("Alpha")),
        "https://brand.example/products/p1": (200, _ld_page("Beta")),
        "https://brand.example/products/p2": (429, ""),
    }

    products = catalogue.read("https://brand.example", transport=transport_for(pages))

    assert sorted(p.name for p in products) == ["Alpha", "Beta"]

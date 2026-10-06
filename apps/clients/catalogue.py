"""Reading a client's product catalogue from their own storefront.

The expensive, slow, error-prone part of onboarding a client is writing down
what they sell. It is also the part they have already published: a storefront
IS the asset list, kept current by the client themselves, in public.

Two routes, cheapest first — the same discipline the transcript ladder uses:

  1. `/products.json` — Shopify exposes the whole catalogue as structured JSON
     with no key, no scraping and no vendor cost. A large share of DTC brands
     are on Shopify, and for those this is complete and authoritative.
  2. `sitemap.xml` → product pages → schema.org JSON-LD. Slower and partial,
     but Product markup is near-universal on commerce platforms because it is
     what Google requires for rich results.

If neither answers, this returns nothing and says so. It does not fall back to
guessing from page text: a catalogue that is subtly wrong is worse than one
that is absent, because the absent one gets written by hand and the wrong one
gets activated.

NOTHING HERE IS AUTHORITATIVE. The output is a draft for an operator to correct.
A storefront tells you what a client sells; it does not tell you what they are
pushing this quarter, which SKU carries the margin, or who they are trying to
reach. Those are commercial decisions and they stay human — see `discovery.py`.
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

#: A catalogue read should not become a crawl. Brands with thousands of SKUs
#: are real, and the profile that matters is the first page or two of them.
MAX_PRODUCTS = 250
MAX_SITEMAP_PAGES = 140
TIMEOUT_SECONDS = 20

#: Between page fetches. We are reading someone else's shop a hundred times in
#: a row, and the launch client's own site answers 429 to an unpaced crawl —
#: which silently cost us 98 of 104 products, because a rate-limited page is
#: not an error, it is just a page with no product on it. Politeness here is
#: both good manners and the difference between a usable profile and a thin one.
CRAWL_DELAY_SECONDS = 0.4

#: One backoff on 429, then give up on that page. A storefront that is actively
#: throttling will not be talked round by retrying harder.
RATE_LIMIT_BACKOFF_SECONDS = 2.0


class CatalogueError(RuntimeError):
    """The storefront could not be read."""


@dataclass
class Product:
    name: str
    description: str = ""
    product_type: str = ""
    tags: list[str] = field(default_factory=list)
    url: str = ""

    def as_prompt_line(self) -> str:
        """Compact: this is fed to a model, and the catalogue may be long."""
        bits = [self.name]
        if self.product_type:
            bits.append(f"[{self.product_type}]")
        if self.tags:
            bits.append("tags: " + ", ".join(self.tags[:6]))
        if self.description:
            bits.append(self.description[:240])
        return " · ".join(bits)


def _normalise(url: str) -> str:
    """A hostname, or a refusal before any request is made.

    Truthiness is not enough to validate a netloc: `urlparse("https://   ")`
    returns a netloc of three spaces, which is truthy, and the caller then makes
    two doomed requests and reports "could not read a catalogue" — blaming the
    storefront for a typo.
    """
    raw = (url or "").strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc.strip()
    if not host or "." not in host or not re.match(r"^[A-Za-z0-9.\-:]+$", host):
        raise CatalogueError(f"{url!r} is not a usable storefront address.")
    return f"{parsed.scheme}://{host}"


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def _default_transport():
    """Lazy, and replaceable in tests.

    `conftest.py` blocks outbound HTTP at the transport layer, so a test that
    forgets to pass a fake fails loudly instead of quietly reaching the
    internet — which is the behaviour we want from a module whose whole job is
    to fetch other people's websites.

    Pacing lives here rather than in the crawl loop on purpose: it is a property
    of making real network calls, so tests that supply their own transport are
    not slowed by it and do not have to know it exists.
    """
    import time

    import httpx

    client = httpx.Client(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "TrendEngine/1.0 (+catalogue import)"},
    )
    state = {"first": True}

    def get(url: str) -> tuple[int, str]:
        if not state["first"]:
            time.sleep(CRAWL_DELAY_SECONDS)
        state["first"] = False

        response = client.get(url)
        if response.status_code == 429:
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
            response = client.get(url)
        return response.status_code, response.text

    return get


# ── Route 1: Shopify ────────────────────────────────────────────────────────


def _from_shopify(origin: str, get) -> list[Product]:
    """`/products.json` is public on Shopify and paginated 250 at a time."""
    status, body = get(f"{origin}/products.json?limit={MAX_PRODUCTS}")
    if status != 200:
        return []
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # A storefront that 200s with HTML on this path is not Shopify; it is a
        # catch-all route. Not an error, just the wrong route.
        return []

    products = []
    for row in payload.get("products", [])[:MAX_PRODUCTS]:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        products.append(
            Product(
                name=title,
                description=_strip_html(row.get("body_html", ""))[:600],
                product_type=(row.get("product_type") or "").strip(),
                tags=[t for t in (row.get("tags") or []) if t][:10],
                url=urljoin(origin, f"/products/{row.get('handle', '')}"),
            )
        )
    if products:
        logger.info("Read %d products from %s via products.json", len(products), origin)
    return products


# ── Route 2: sitemap + JSON-LD ──────────────────────────────────────────────

_LD_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _product_nodes(payload) -> list[dict]:
    """JSON-LD arrives as an object, a list, or an @graph. All three occur."""
    if isinstance(payload, list):
        found = []
        for item in payload:
            found.extend(_product_nodes(item))
        return found
    if not isinstance(payload, dict):
        return []
    if "@graph" in payload:
        return _product_nodes(payload["@graph"])
    types = payload.get("@type", "")
    types = types if isinstance(types, list) else [types]
    return [payload] if "Product" in types else []


def _locations(body: str) -> list[str]:
    """Every <loc> in a sitemap, unescaped.

    Sitemaps are XML, so a URL with query parameters arrives with `&` written
    as `&amp;`. Requesting it unescaped sends a literal "&amp;" and the server
    returns the wrong page or an error — which presents as "this storefront has
    no products" rather than as a malformed request.
    """
    return [
        html_module.unescape(loc)
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
    ]


def _is_sitemap(url: str) -> bool:
    """Checked on the PATH, not the whole URL.

    Shopify's sitemap index points at `sitemap_products_1.xml?from=…&to=…`, so
    `url.endswith(".xml")` is False and the product index is skipped — the
    catalogue then reads as empty for one of the most common storefronts there
    is. Found against a real client's site, which is the only way this surfaces.
    """
    return urlparse(url).path.lower().endswith(".xml")


def _from_jsonld(origin: str, get) -> list[Product]:
    status, body = get(f"{origin}/sitemap.xml")
    if status != 200:
        return []

    locations = _locations(body)
    # Nested sitemaps: follow only the ones that look like product indexes,
    # rather than every sitemap a large site publishes.
    nested = [u for u in locations if _is_sitemap(u) and "product" in u.lower()]
    for sub in nested[:3]:
        sub_status, sub_body = get(sub)
        if sub_status == 200:
            locations.extend(_locations(sub_body))

    pages = [u for u in locations if "/product" in u.lower() and not _is_sitemap(u)]
    products: list[Product] = []
    blocked = 0
    for url in pages[:MAX_SITEMAP_PAGES]:
        status, html = get(url)
        if status in (403, 429):
            # Counted, not just skipped. A throttled page is not an error — it
            # is a page with no product on it — so without this the import
            # SUCCEEDS and returns a fraction of the range. The launch client's
            # own site answers 429 to 7 pages in 8, which would have produced a
            # confident profile built from 8 of 104 products.
            blocked += 1
            continue
        if status != 200:
            continue
        for block in _LD_BLOCK.findall(html):
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                continue
            for node in _product_nodes(payload):
                name = (node.get("name") or "").strip()
                if not name:
                    continue
                products.append(
                    Product(
                        name=name,
                        description=_strip_html(str(node.get("description", "")))[:600],
                        product_type=str(node.get("category", "")).strip(),
                        url=url,
                    )
                )
    if blocked > len(products):
        raise CatalogueError(
            f"{origin} is rate-limiting us: {blocked} of "
            f"{min(len(pages), MAX_SITEMAP_PAGES)} product pages were refused and only "
            f"{len(products)} could be read. A profile built from that fraction of the "
            f"range would look complete and would not be. Write the profile by hand, or "
            f"ask the client for their product list — they have one."
        )

    if products:
        logger.info(
            "Read %d products from %s via JSON-LD (%d pages refused)",
            len(products), origin, blocked,
        )
    return products


# ── The ladder ──────────────────────────────────────────────────────────────


def read(store_url: str, *, transport=None) -> list[Product]:
    """Everything the storefront will tell us about what this client sells.

    Deduplicated by name: a sitemap crawl routinely sees the same product on a
    canonical URL and a collection URL, and a profile listing one SKU three
    times would weight it three times.
    """
    origin = _normalise(store_url)
    get = transport or _default_transport()

    for route in (_from_shopify, _from_jsonld):
        try:
            found = route(origin, get)
        except CatalogueError:
            # A deliberate refusal, not a route that happened to break — a
            # throttled crawl knows something the generic message below does
            # not, and swallowing it here would replace "they are rate-limiting
            # us, write the profile by hand" with "we could not read anything".
            raise
        except Exception as exc:  # noqa: BLE001 — one dead route is not fatal
            logger.warning("Catalogue route %s failed for %s: %s", route.__name__, origin, exc)
            continue
        if found:
            return _deduplicate(found)

    raise CatalogueError(
        f"Could not read a product catalogue from {origin}. Neither "
        f"/products.json nor sitemap JSON-LD returned products. Write the "
        f"profile by hand instead — a guessed catalogue is worse than none."
    )


def _deduplicate(products: list[Product]) -> list[Product]:
    seen: dict[str, Product] = {}
    for product in products:
        key = product.name.strip().casefold()
        # Keep whichever copy carries the most context for the model.
        if key not in seen or len(product.description) > len(seen[key].description):
            seen[key] = product
    return list(seen.values())[:MAX_PRODUCTS]

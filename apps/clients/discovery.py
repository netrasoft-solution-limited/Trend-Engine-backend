"""Deriving a draft client profile from a storefront.

Onboarding a client previously meant an operator writing out every product,
every synonym a podcast host might use for it, the audiences and the
priorities — by hand, per client, and again whenever the range changed. That is
the cost that makes a second client expensive, and most of it is mechanical.

WHAT IS AUTOMATED: the catalogue (read from the storefront), the grouping of
SKUs into assets worth scoring, the vocabulary for each — the ingredient names,
abbreviations and common forms a creator would actually say — and the category
the client trades in.

WHAT IS NOT, AND WILL NOT BE: which products matter most this quarter, who the
client is trying to reach, and what they are strategically pushing. Those are
not discoverable from a storefront, because they are decisions rather than
facts. A model asked to guess them produces confident, plausible, wrong
answers, and the resulting profile is worse than an empty one because it looks
finished.

So this writes a DRAFT version and stops. `services.activate()` is the human
gate and stays the human gate. Weights come out flat at a deliberate 50 —
undifferentiated and obviously so, rather than invented numbers an operator
would assume someone had thought about.

ONE CALL, NOT ONE PER PRODUCT. Arch §10.3: the model is chosen by task. This is
the gate tier (Haiku) over a whole catalogue, once per client, at onboarding —
a few cents. Per-product calls would scale with the range and recur on every
re-import, and they would buy nothing: grouping SKUs needs to see the range
together anyway.
"""
from __future__ import annotations

import logging

from django.db import transaction
from pydantic import BaseModel, Field

from apps.enrichment.llm import client as llm

from . import catalogue as catalogue_reader
from . import services
from .models import ClientAsset, ClientTerm

logger = logging.getLogger(__name__)

PROMPT_VERSION = "client-profile-discovery-1"

#: Flat, and visibly so. See the module docstring — an invented weight is a
#: commercial judgement nobody made.
DEFAULT_WEIGHT = 50

SYSTEM = """You turn an ecommerce catalogue into a scoring profile for a market
intelligence system.

That system reads podcast and video transcripts and must decide which claims
matter to this brand. It matches literally, on the words people actually say —
so the vocabulary you produce is the whole mechanism. A missed synonym is a
missed claim.

Rules:

· Group the catalogue into ASSETS worth scoring separately. One asset per
  distinct ingredient, product line or category — not one per SKU. Six flavours
  of the same protein are one asset.
· For each asset give the terms a podcast host or YouTuber would SAY. Include
  the ingredient name, common abbreviations, chemical forms and the brand's own
  product name. Prefer what is spoken over what is printed on a label.
· Terms must be specific enough not to collide. "magnesium l-threonate" is a
  term; "supplement" is not. A term that would match half of all health content
  is worse than no term.
· Infer AUDIENCES only where the catalogue genuinely implies them — a
  pregnancy range implies expectant mothers; a plain multivitamin implies
  nothing. Returning none is correct and expected.
· Infer PRIORITIES only as the health or performance outcomes the range is
  built around, not as marketing aims.
· Name the CATEGORY the brand trades in, and the adjacent categories its
  customers care about. This becomes the domain vocabulary for relevance.

Do not invent products that are not in the catalogue. Do not guess competitors;
they are not derivable from a catalogue and an operator will add them."""


class Asset(BaseModel):
    name: str = Field(description="The asset as the operator would name it.")
    kind: str = Field(description="One of: product, ingredient, category, service, content")
    terms: list[str] = Field(description="Spoken vocabulary that means this asset.")


class Term(BaseModel):
    name: str
    terms: list[str]


class DiscoveredProfile(BaseModel):
    category: str = Field(description="The category this brand trades in.")
    adjacent_categories: list[str] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    audiences: list[Term] = Field(default_factory=list)
    priorities: list[Term] = Field(default_factory=list)


VALID_KINDS = {choice for choice, _label in ClientAsset.Kind.choices}


def interpret(products: list[catalogue_reader.Product], *, transport=None) -> DiscoveredProfile:
    """Catalogue in, draft profile shape out. Writes nothing."""
    if not products:
        raise ValueError("An empty catalogue produces an empty profile; nothing to interpret.")

    listing = "\n".join(f"- {product.as_prompt_line()}" for product in products)
    return llm.LLMClient(transport=transport).structured(
        schema=DiscoveredProfile,
        # EXTRACTION tier, not the gate tier. The gate is sized for a yes/no
        # with 512 output tokens; a profile for a 70-product range runs to a
        # thousand or more — ten assets with six terms each, plus audiences and
        # priorities — and would be truncated mid-JSON. The failure would look
        # like a model that returns malformed output rather than a budget set
        # too low. This runs once per client at onboarding, so the better model
        # costs a few cents against a profile every later brief depends on.
        tier=llm.extraction_tier(),
        purpose="client_profile_discovery",
        prompt_version=PROMPT_VERSION,
        # The instructions are stable across every client and the catalogue is
        # not, so the prefix caches and only the listing is paid for in full.
        system=llm.cached_system(SYSTEM),
        user=f"Catalogue ({len(products)} products):\n\n{listing}",
    )


@transaction.atomic
def draft_from_catalogue(
    organization,
    *,
    products: list[catalogue_reader.Product],
    actor_label: str = "",
    transport=None,
) -> tuple:
    """Write a DRAFT profile version from a catalogue. Returns (version, discovered).

    Deliberately not activated. The operator reads it, fixes the weights, adds
    the competitors and the strategy, and activates it — which is the step that
    makes it real, and the step a machine should not take.
    """
    discovered = interpret(products, transport=transport)

    version = services.draft(
        organization,
        label=f"Discovered from catalogue ({len(products)} products)",
        actor_label=actor_label or "catalogue discovery",
        copy_current=False,
    )

    assets = []
    for asset in discovered.assets:
        terms = _usable(asset.terms)
        if not terms:
            # An asset with no terms can never match. Keeping it would pad the
            # profile with rows that look like coverage and provide none.
            logger.warning("Discovered asset %r had no usable terms; dropped", asset.name)
            continue
        assets.append(
            ClientAsset(
                organization=organization,
                profile=version,
                name=asset.name[:200],
                kind=asset.kind if asset.kind in VALID_KINDS else ClientAsset.Kind.PRODUCT,
                terms=terms,
                weight=DEFAULT_WEIGHT,
                notes="Discovered from the storefront. Confirm the weight before activating.",
            )
        )
    ClientAsset.objects.bulk_create(assets)

    terms_rows = []
    for facet, rows in (
        (ClientTerm.Facet.AUDIENCE, discovered.audiences),
        (ClientTerm.Facet.PRIORITY, discovered.priorities),
    ):
        for row in rows:
            usable = _usable(row.terms)
            if not usable:
                continue
            terms_rows.append(
                ClientTerm(
                    organization=organization,
                    profile=version,
                    facet=facet,
                    name=row.name[:200],
                    terms=usable,
                    weight=DEFAULT_WEIGHT,
                    notes="Discovered. Competitors are not derivable and must be added by hand.",
                )
            )
    ClientTerm.objects.bulk_create(terms_rows)

    logger.info(
        "Discovered profile v%s for %s: %d assets, %d terms, category %r",
        version.number,
        organization,
        len(assets),
        len(terms_rows),
        discovered.category,
    )
    return version, discovered


def _usable(terms: list[str]) -> list[str]:
    """Drop what cannot match or would match everything.

    Single characters and very short tokens are the dangerous case: "d" as a
    term for vitamin D matches every sentence containing the letter as a word,
    and the resulting scores are confidently meaningless.
    """
    cleaned = []
    for term in terms or []:
        value = (term or "").strip()
        if len(value) < 3 or not any(ch.isalnum() for ch in value):
            continue
        if value.casefold() not in {t.casefold() for t in cleaned}:
            cleaned.append(value)
    return cleaned[:24]


def discover(organization, *, store_url: str, actor_label: str = "", transport=None, llm_transport=None):
    """Read the storefront, then draft a profile from it. The whole path."""
    products = catalogue_reader.read(store_url, transport=transport)
    return draft_from_catalogue(
        organization,
        products=products,
        actor_label=actor_label,
        transport=llm_transport,
    )

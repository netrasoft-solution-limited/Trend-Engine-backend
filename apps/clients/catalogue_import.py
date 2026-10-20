"""A client's catalogue inventory, mapped into a profile spec.

Built for the 103-listing Jarrow inventory Pure Play supplied on 20 Oct
(`catalogue_id, product, primary_listening_family, listening_topics, url,
skus, manufacturer_tags, ...`), and kept general enough for the next snapshot
in the same shape.

Three rules, from Mark's covering note:

- FORMULA DISTINCTIONS ARE KEPT. Every listing stays its own asset under its
  exact name: Fem-Dophilus Ultra refrigerated and shelf-stable are two rows,
  not one, because their labels, storage and claims differ.
- IT IS A PUBLIC SNAPSHOT. Not approved claims and not commercial weighting:
  every weight is a neutral 50, and the notes say so, until Jarrow supplies
  priorities.
- THE LISTENING FAMILIES COME WITH IT. Each family becomes a category asset
  whose terms are its listening topics, so a conversation about "bloating" or
  "bone density" reaches the family even when no product is named.

Terms are what a speaker would actually say: the product name without marks,
pack sizes and strengths, plus the ingredients in its manufacturer tags. Tags
that name a salt or a trace mineral (calcium in a biotin capsule, copper in a
bone formula) are left out, because they would match conversations about
something else entirely.
"""
from __future__ import annotations

import csv
import io
import re

SNAPSHOT_NOTE = "Public catalogue snapshot; not approved claims or commercial weighting."
NEUTRAL_WEIGHT = 50

#: Tag → the words a speaker uses for it. Unlisted tags fall back to the tag
#: itself with hyphens as spaces.
INGREDIENT_TERMS: dict[str, list[str]] = {
    "probiotics": ["probiotic", "probiotics"],
    "prebiotics": ["prebiotic", "prebiotics"],
    "vit-c": ["vitamin c"],
    "vit-d": ["vitamin d", "vitamin d3"],
    "vit-k": ["vitamin k", "vitamin k2"],
    "vit-b": ["vitamin b12", "b12"],
    "vit-e": ["vitamin e"],
    "ala": ["alpha lipoic acid"],
    "r-ala": ["r-lipoic acid", "alpha lipoic acid"],
    "ps": ["phosphatidylserine"],
    "5htp": ["5-htp"],
    "nac": ["nac", "n-acetyl cysteine", "n-acetylcysteine"],
    "nag": ["n-acetyl glucosamine"],
    "coq10": ["coq10", "coenzyme q10", "ubiquinone"],
    "ubiquinol": ["ubiquinol", "coq10"],
    "omega3": ["omega-3", "fish oil"],
    "l-theanine": ["theanine", "l-theanine"],
    "l-glutamine": ["glutamine", "l-glutamine"],
    "l-tyrosine": ["tyrosine", "l-tyrosine"],
    "l-carnitine": ["carnitine", "l-carnitine"],
    "acetyl-lcarnitine": ["acetyl l-carnitine", "acetyl-l-carnitine"],
    "citrus-bergamia": ["bergamot", "citrus bergamot"],
    "s-adenosyl-methionine": ["s-adenosyl methionine"],
    "diindolylmethane-complex": ["diindolylmethane"],
    "broccoli-seed": ["sulforaphane", "broccoli seed"],
    "saccharomyces-boulardii": ["saccharomyces boulardii", "s. boulardii", "boulardii"],
    "saccharomyces-cerevisiae": ["beta glucan", "beta-glucan"],
    "trans-pterostilbene": ["pterostilbene"],
    "shilajit-fulvic-acid-complex": ["shilajit"],
    "milk-thistle": ["milk thistle", "silymarin"],
    "wild-bitter-melon": ["bitter melon"],
    "alpha-gpc": ["alpha-gpc", "alpha gpc"],
    "red-yeast-rice": ["red yeast rice"],
    "krill-oil": ["krill oil"],
    "mastic-gum": ["mastic gum"],
    "glucosamine-sulfate": ["glucosamine"],
    "chondroitin-sulfate": ["chondroitin"],
    "curcumin-phytosome": ["curcumin"],
    "turmeric": ["curcumin", "turmeric"],
    "hyaluronic-acid": ["hyaluronic acid"],
    "s-acetyl-l-glutathione": ["glutathione"],
    "folate": ["folate", "methylfolate"],
    "pantothenic-acid": ["pantothenic acid", "vitamin b5"],
}

#: Trace minerals and salts that ride along in a formula's tags without being
#: what the product is about.
NOISE = {"sodium", "potassium", "silicon", "copper", "manganese"}

#: Words that are a product name or a listening topic but also ordinary
#: English, and would match conversations about anything ("SAMe" → "same").
#: Matching is literal, so these are never used as terms.
TOO_GENERIC = {"same", "diet", "energy", "deficiency"}

#: Spoken names for products whose catalogue name is a common word.
NAME_ALIASES = {"same": ["sam-e", "s-adenosyl methionine"]}


def _clean_name(product: str) -> str:
    """The name as said aloud: no marks, no pack size, no strength."""
    name = re.sub(r"[®™]", "", product)
    name = re.sub(r"\([^)]*\)", "", name)
    name = name.split(" - ")[0]
    return re.sub(r"\s+", " ", name).strip(" ,").lower()


def _ingredient_terms(tags: list[str], *, family: str, product: str) -> list[str]:
    terms: list[str] = []
    lowered = product.lower()
    for tag in tags:
        if not tag.startswith("ing-"):
            continue
        key = tag[4:]
        if key in NOISE:
            continue
        # Calcium is a salt in a biotin capsule and a B5 tablet; only in the bone
        # family is it what the product is for.
        if key == "calcium" and family != "Bone health":
            continue
        if key == "biotin" and "biotin" not in lowered and "b-right" not in lowered:
            continue
        terms.extend(INGREDIENT_TERMS.get(key, [key.replace("-", " ")]))
    return terms


def spec_from_inventory(text: str) -> tuple[dict, list[dict]]:
    """(profile spec, flat rows for the import CSV) from an inventory CSV."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    assets: list[dict] = []
    families: dict[str, dict] = {}

    for row in reader:
        product = (row.get("product") or "").strip()
        if not product:
            continue
        family = (row.get("primary_listening_family") or "").strip()
        tags = [t.strip() for t in (row.get("manufacturer_tags") or "").split("|")]
        name_term = _clean_name(product)
        terms = [
            t for t in dict.fromkeys([
                name_term,
                *NAME_ALIASES.get(name_term, []),
                *_ingredient_terms(tags, family=family, product=product),
            ])
            if t not in TOO_GENERIC
        ]
        notes = " ".join(part for part in [
            f"Catalogue {row.get('catalogue_id', '').strip()};",
            f"SKUs {row.get('skus', '').strip()};",
            f"{row.get('available_variants', '').strip()} of {row.get('variants', '').strip()} "
            f"variants available;",
            f"family: {family}." if family else "",
            (row.get("review_note") or "").strip(),
            SNAPSHOT_NOTE,
        ] if part)
        assets.append({
            "name": product,
            "kind": "product",
            "weight": NEUTRAL_WEIGHT,
            "terms": terms,
            "url": (row.get("url") or "").strip(),
            "notes": notes,
        })
        if family and family not in families:
            families[family] = {
                "name": family,
                "kind": "category",
                "weight": NEUTRAL_WEIGHT,
                "terms": [
                    t.strip().lower()
                    for t in (row.get("listening_topics") or "").split(";")
                    if t.strip() and t.strip().lower() not in TOO_GENERIC
                ],
                "url": "",
                "notes": " ".join(part for part in [
                    "Listening family.",
                    (row.get("brief_status") or "").strip(),
                    SNAPSHOT_NOTE,
                ] if part),
            }

    rows = assets + list(families.values())
    spec = {
        "label": f"Public catalogue snapshot: {len(assets)} listings, {len(families)} families",
        "assets": [
            {
                "name": r["name"],
                "kind": r["kind"],
                "weight": r["weight"],
                "terms": r["terms"],
                "notes": " · ".join(p for p in (r["url"], r["notes"]) if p),
            }
            for r in rows
        ],
        "audiences": [],
        "priorities": [],
        "competitors": [],
    }
    return spec, rows


def import_csv(rows: list[dict]) -> str:
    """The rows in the product-template format (name,kind,terms,weight,url,notes)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["name", "kind", "terms", "weight", "url", "notes"])
    for r in rows:
        writer.writerow(
            [r["name"], r["kind"], "; ".join(r["terms"]), r["weight"], r["url"], r["notes"]]
        )
    return out.getvalue()

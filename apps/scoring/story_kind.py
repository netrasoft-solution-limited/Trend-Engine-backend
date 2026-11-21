"""What a story or candidate means for the brand's catalogue.

Mark, 20 Nov: "distinguish existing-product stories from genuine gaps, new
formats or new audiences." Decided by rules over the full live catalogue, not
by a model, so every label comes with the reason and the listings behind it:

  gap       no listing in the catalogue matches the story or candidate
  format    listings match, but the story is about a form none of them comes in
  audience  listings match, but the story is about a group no listing is for
  existing  listings match, in the form and for the people discussed

Checked in that order. Format and audience are found by plain words in the
story's title and statement, and in the listings' names and notes, so a wrong
label can be traced to the word that caused it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.clients import services as client_services
from apps.clients.models import ClientAsset

GAP, FORMAT, AUDIENCE, EXISTING = "gap", "format", "audience", "existing"
LABELS = {
    GAP: "Portfolio gap",
    FORMAT: "New format",
    AUDIENCE: "New audience",
    EXISTING: "Existing product",
}

#: Form → the words that name it. Order matters only for reporting.
FORMATS = {
    "gummy": ("gummy", "gummies", "chewable", "chewables"),
    "powder": ("powder", "powders", "stick pack", "drink mix", "scoop"),
    "liquid": ("liquid", "drops", "tincture", "shot", "syrup"),
    "spray": ("spray", "oral spray"),
    "effervescent": ("effervescent", "fizzy tablet"),
    "lozenge": ("lozenge", "lozenges", "melt", "dissolvable"),
}

#: Group → the words that name it.
AUDIENCES = {
    "children": ("children", "kids", "child", "toddler", "toddlers", "infant", "baby", "babies"),
    "pregnancy and new mothers": ("pregnancy", "pregnant", "prenatal", "postpartum",
                                  "breastfeeding"),
    "women in menopause": ("menopause", "perimenopause", "menopausal"),
    "older adults": ("older adults", "over 50", "over 60", "seniors", "elderly", "aging adults"),
    "athletes": ("athletes", "athlete", "lifters", "endurance", "gym-goers"),
    "people on GLP-1 medicines": ("glp-1", "glp1", "ozempic", "wegovy", "semaglutide",
                                  "tirzepatide", "mounjaro"),
    "vegans": ("vegan", "plant-based"),
}


@dataclass
class Kind:
    kind: str
    reason: str
    listings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    def sentence(self) -> str:
        return f"{self.label}: {self.reason}"


def _words(text: str, terms) -> list[str]:
    lowered = (text or "").lower()
    return [t for t in terms if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", lowered)]


def _catalogue(organization) -> list[ClientAsset]:
    profile = client_services.current_for(organization)
    if profile is None:
        return []
    return list(profile.assets.exclude(kind=ClientAsset.Kind.CATEGORY))


def _listing_text(asset: ClientAsset) -> str:
    return f"{asset.name} {' '.join(asset.terms)} {asset.notes}"


def classify(organization, *, text: str, listings: list[str]) -> Kind:
    """`text` is what the story says; `listings` the catalogue names it matched."""
    catalogue = _catalogue(organization)
    by_name = {a.name: a for a in catalogue}
    matched = [by_name[n] for n in listings if n in by_name]
    if not matched:
        return Kind(GAP, "no listing in the current catalogue matches it.")

    # A listing supports the story only if the story names it or one of its
    # terms. Otherwise it is the nearest thing the brand sells, and the story
    # is about something it does not (21 Nov: urolithin A was mapped to the
    # probiotics and labelled an existing product).
    direct = [a for a in matched if _words(text, _key_words(a))]
    if not direct:
        return Kind(
            GAP,
            f"the story is about something the catalogue does not sell; nearest listings: "
            f"{_names([a.name for a in matched])}.",
            [a.name for a in matched],
        )
    matched = direct
    names = [a.name for a in matched]
    for form, words in FORMATS.items():
        said = _words(text, words)
        if said and not any(_words(_listing_text(a), words) for a in matched):
            return Kind(
                FORMAT,
                f"the discussion is about {form} formats (“{said[0]}”); the matching "
                f"listings ({_names(names)}) do not come in that form.",
                names,
            )

    for group, words in AUDIENCES.items():
        said = _words(text, words)
        if said and not any(_words(_listing_text(a), words) for a in catalogue):
            return Kind(
                AUDIENCE,
                f"the discussion is about {group} (“{said[0]}”); no listing in the "
                f"catalogue is described for them.",
                names,
            )

    return Kind(EXISTING, f"supports {_names(names)}.", names)


def for_recommendation(recommendation) -> Kind:
    signal = recommendation.signal
    return classify(
        recommendation.organization,
        text=f"{signal.title} {signal.statement}",
        listings=list(recommendation.products or []),
    )


def for_candidate(candidate) -> Kind:
    found = [f["name"] for f in (candidate.gap_check or {}).get("found", [])]
    if not found:
        listings = (candidate.gap_check or {}).get("listings")
        return Kind(GAP, f"not found in the titles, terms or notes of {listings or 'the'} "
                         f"catalogue listings; to be confirmed against retail-only lines "
                         f"and the pipeline.")
    return classify(
        candidate.organization,
        text=f"{candidate.name} {candidate.opening} {candidate.why_it_matters}",
        listings=found,
    )


#: Words in listing names that say nothing about what the listing is.
GENERIC = {"supplement", "supplements", "capsules", "capsule", "veggie", "tablets", "tablet",
           "formula", "complex", "advanced", "ultra", "sustain", "billion", "refrigerated",
           "refrigeratred", "softgels", "jarrow", "formulas", "health", "support", "pack"}


def _key_words(asset: ClientAsset) -> list[str]:
    """The words that identify a listing: its terms, and the distinctive words
    (five letters or more) of its name and terms."""
    phrases = [asset.name.lower(), *[t.lower() for t in asset.terms]]
    words = {w for p in phrases for w in re.findall(r"[a-z][a-z0-9-]{4,}", p)} - GENERIC
    return [*phrases, *sorted(words)]


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:3])
    return shown + (f" and {len(names) - 3} more" if len(names) > 3 else "")

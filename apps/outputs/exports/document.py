"""What a renderer is given. No models, by design.

`apps.publication` (L7) sits above `apps.outputs` (L6), so a renderer living
here cannot import `Publication` — and a renderer that can only export
unpublished versions is not the renderer we need.

Putting the shared code in a top-level package outside `apps/` would look like
the escape hatch and is the opposite: `.importlinter` sets `root_package = apps`,
so such a module is invisible to CI, and the one module every layer imports
becomes the one that can import upward unchecked.

So a renderer takes this dataclass, and each layer builds its own:

    apps/outputs/exports/adapters.py     from an OutputVersion
    apps/publication/exports.py          from a Publication

This repeats a decision the codebase already made —
`publication.services.delivery_status_for()` exists for the same reason.

NOTHING HERE IS A TIMESTAMP OF THE RENDER. `ExportArtifact` stores a sha256 so
that re-rendering can prove a client received exactly those bytes, and a
"generated at" line would make every render differ and the hash worthless.
`dated` is the publication date — a fact about the document, stable across
renders — not the moment the file was built.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from .text import Block, blocks

#: Bumped whenever a renderer's output changes for the same input. Stored on
#: `ExportArtifact` so that a file regenerated months later can be compared
#: honestly: without it, a mismatch cannot distinguish changed content from a
#: changed template.
RENDERER_VERSION = "2"

#: Shown on anything not yet published. The gate's whole purpose is that
#: approved is not client-visible, and this module can hand an operator a
#: client-ready PDF of an unapproved draft. The watermark is the cheapest
#: restoration of the backstop that the portal used to provide.
DRAFT_NOTICE = "DRAFT — NOT PUBLISHED"

#: Shown on demonstration or test content. Mark's 8 October review asked for
#: illustrative records to be labelled; a label that lived only in the app would
#: be lost the moment the file was forwarded.
ILLUSTRATIVE_NOTICE = "ILLUSTRATIVE — DEMONSTRATION CONTENT, NOT A FINDING"

#: Shown when the record behind the document has been corrected. Mark, 20 Oct:
#: "an app-only note will not protect someone downloading the old file". The
#: correction itself is the first section of every export from then on.
CORRECTION_NOTICE = "CORRECTED RECORD — READ THE CORRECTION BEFORE USING THIS DOCUMENT"
CORRECTION_HEADING = "Correction to this record"

#: Used as document metadata when there is no publication date. Any fixed
#: value; what matters is that it is not the clock.
STABLE_FALLBACK_DATE = date(1980, 1, 1)


#: Logo files shipped with the renderer. `brand.png` is the consultancy's mark;
#: `clients/<organization slug>.png` (and an optional `.svg`, which the PDF
#: prefers for sharpness) is the client's. Word cannot embed SVG, so the PNG is
#: the one every client logo must have.
ASSETS = Path(__file__).resolve().parent / "assets"


@dataclass(frozen=True)
class Logo:
    png: bytes
    svg: bytes = b""

    @property
    def data_uri(self) -> str:
        """Inline, so the HTML and PDF need no file or network access."""
        if self.svg:
            return "data:image/svg+xml;base64," + base64.b64encode(self.svg).decode("ascii")
        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")


@lru_cache(maxsize=32)
def load_logo(stem: str) -> Logo | None:
    """`stem` relative to `ASSETS`, without extension; None when there is no PNG."""
    png = ASSETS / f"{stem}.png"
    if not stem or not png.is_file():
        return None
    svg = ASSETS / f"{stem}.svg"
    return Logo(png=png.read_bytes(), svg=svg.read_bytes() if svg.is_file() else b"")


@dataclass(frozen=True)
class Section:
    heading: str
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Provenance:
    """What this document was built from — PRD §8's auditable triple.

    Every field is a stable property of the version, not of this render.
    """

    version_number: int = 0
    client_profile_version: str = ""
    domain_pack_version: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.client_profile_version or self.domain_pack_version)


@dataclass(frozen=True)
class ExportDocument:
    title: str
    type_label: str
    organization_name: str
    summary: str = ""
    sections: tuple[Section, ...] = ()
    is_draft: bool = True
    dated: date | None = None
    provenance: Provenance = field(default_factory=Provenance)
    is_illustrative: bool = False
    is_corrected: bool = False
    #: The consultancy the document is issued under, from `EXPORT_BRAND`.
    #: Blank leaves every format unbranded.
    brand: str = ""
    #: The consultancy's logo, shown in the masthead when `brand` is set.
    brand_logo: Logo | None = None
    #: The client's logo, shown in the masthead beside "Prepared for".
    client_logo: Logo | None = None

    @property
    def running_header(self) -> str:
        when = f" | {self.dated:%B %Y}" if self.dated else ""
        return f"{self.brand.upper()} · {self.organization_name} | {self.type_label}{when}"

    @property
    def running_footer(self) -> str:
        return f"Prepared by {self.brand} | Confidential. Prepared for {self.organization_name}."

    @property
    def notice(self) -> str:
        """Every warning the document carries, on one line. Renderers print
        this when `has_notice`; none of them decides on its own which apply."""
        parts = [CORRECTION_NOTICE] if self.is_corrected else []
        if self.is_illustrative:
            parts.append(ILLUSTRATIVE_NOTICE)
        if self.is_draft:
            parts.append(DRAFT_NOTICE)
        return " · ".join(parts)

    @property
    def has_notice(self) -> bool:
        return self.is_draft or self.is_illustrative or self.is_corrected

    @property
    def metadata_date(self) -> str:
        """An ISO date for the document's own metadata, never the clock.

        WeasyPrint reads `dcterms.created` out of the HTML and, finding none,
        stamps `/CreationDate` from `datetime.now()` — which alone would make
        every PDF of one publication differ. An unpublished draft has no
        publication date, so it gets a fixed sentinel rather than today.
        """
        return (self.dated or STABLE_FALLBACK_DATE).isoformat()

    def slug(self) -> str:
        """A filename stem that is safe everywhere and still recognisable.

        Deterministic, like everything else here: the same document exports to
        the same filename, so a second download overwrites rather than
        accumulating `brief (3).docx` in someone's downloads folder.
        """
        import re
        import unicodedata

        folded = unicodedata.normalize("NFKD", self.title or "output")
        ascii_only = folded.encode("ascii", "ignore").decode("ascii")
        stem = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
        return stem[:60] or "output"


def document_from_body(
    *,
    title: str,
    type_label: str,
    organization_name: str,
    body: list[dict],
    summary: str = "",
    is_draft: bool = True,
    dated: date | None = None,
    provenance: Provenance | None = None,
    is_illustrative: bool = False,
    corrections: tuple[str, ...] | list[str] = (),
    brand: str | None = None,
    organization_slug: str = "",
) -> ExportDocument:
    """Build a document from the stored `body` shape.

    Both adapters funnel through here, so the two layers cannot drift in how
    they interpret a section. A section with no text still becomes a Section —
    an empty heading in the output is a visible fault, where a silently dropped
    one is not.
    """
    brand = _brand() if brand is None else brand
    return ExportDocument(
        title=title,
        type_label=type_label,
        organization_name=organization_name,
        summary=summary,
        # Corrections come FIRST, before anything the original document says,
        # so a reader cannot reach the content without passing them.
        sections=tuple(
            Section(heading=CORRECTION_HEADING, blocks=blocks(text))
            for text in corrections
            if text and text.strip()
        )
        + tuple(
            Section(
                heading=str(entry.get("heading") or ""),
                blocks=blocks(str(entry.get("text") or "")),
            )
            for entry in (body or [])
        ),
        is_draft=is_draft,
        dated=dated,
        provenance=provenance or Provenance(),
        is_illustrative=is_illustrative,
        is_corrected=any(text and text.strip() for text in corrections),
        brand=brand,
        # Logos only on a branded document: an unbranded export stays plain.
        brand_logo=load_logo("brand") if brand else None,
        client_logo=(
            load_logo(f"clients/{organization_slug}") if brand and organization_slug else None
        ),
    )


def _brand() -> str:
    from django.conf import settings

    return getattr(settings, "EXPORT_BRAND", "")

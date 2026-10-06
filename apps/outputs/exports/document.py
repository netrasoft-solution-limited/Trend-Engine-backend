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

from dataclasses import dataclass, field
from datetime import date

from .text import Block, blocks

#: Bumped whenever a renderer's output changes for the same input. Stored on
#: `ExportArtifact` so that a file regenerated months later can be compared
#: honestly: without it, a mismatch cannot distinguish changed content from a
#: changed template.
RENDERER_VERSION = "1"

#: Shown on anything not yet published. The gate's whole purpose is that
#: approved is not client-visible, and this module can hand an operator a
#: client-ready PDF of an unapproved draft. The watermark is the cheapest
#: restoration of the backstop that the portal used to provide.
DRAFT_NOTICE = "DRAFT — NOT PUBLISHED"

#: Used as document metadata when there is no publication date. Any fixed
#: value; what matters is that it is not the clock.
STABLE_FALLBACK_DATE = date(1980, 1, 1)


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

    @property
    def notice(self) -> str:
        return DRAFT_NOTICE if self.is_draft else ""

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
) -> ExportDocument:
    """Build a document from the stored `body` shape.

    Both adapters funnel through here, so the two layers cannot drift in how
    they interpret a section. A section with no text still becomes a Section —
    an empty heading in the output is a visible fault, where a silently dropped
    one is not.
    """
    return ExportDocument(
        title=title,
        type_label=type_label,
        organization_name=organization_name,
        summary=summary,
        sections=tuple(
            Section(heading=str(entry.get("heading") or ""), blocks=blocks(str(entry.get("text") or "")))
            for entry in (body or [])
        ),
        is_draft=is_draft,
        dated=dated,
        provenance=provenance or Provenance(),
    )

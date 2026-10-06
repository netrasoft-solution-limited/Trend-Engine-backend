"""Rendering an output into something a client can be sent.

With no client portal, this IS the delivery mechanism — an output reaches a
client as a file attached to an email. So these renderers are not a convenience
feature; they are the product's last mile.

    from apps.outputs import exports
    rendered = exports.render(document, "docx")
    rendered.content, rendered.filename, rendered.mimetype

Every renderer is BYTE-DETERMINISTIC for the same document, and
`tests/outputs/test_exports.py` asserts it. That is not neatness: `ExportArtifact`
stores a sha256 so a file can be re-rendered later and proved identical to what
a client received. Without determinism the hash identifies a blob we no longer
have, which is worse than storing nothing — it looks like evidence.

The formats that need no third-party package (markdown, html, csv) are always
available. DOCX and PDF need `python-docx` and `weasyprint`, and are reported as
unavailable rather than raising an ImportError from somewhere deep in a Celery
task when a delivery is already half-recorded.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from .document import (  # noqa: F401 — the public surface of this package
    DRAFT_NOTICE,
    RENDERER_VERSION,
    ExportDocument,
    Provenance,
    Section,
    document_from_body,
)
from .text import Block, Bullets, Paragraph, blocks  # noqa: F401

logger = logging.getLogger(__name__)

#: 1980-01-01 — the same instant `docx.py` pins its zip entries to. Any fixed
#: value works; matching the two means one answer to "why is this date here?".
FIXED_EPOCH = "315532800"

# PDF determinism rests on this, and the reason is not guessable from the
# symptom. WeasyPrint subsets every font it embeds, and the subsetter stamps the
# TrueType `head` table — which carries the font's own created/modified dates —
# from the clock. Two PDFs of identical content therefore differ by a few bytes
# deep inside the embedded font, while every piece of PDF metadata matches.
# fontTools honours SOURCE_DATE_EPOCH for exactly this (the reproducible-builds
# convention), and it is what makes two renders byte-identical.
#
# Set here rather than in `pdf.py` because that module does not import without
# its system libraries — a setting living there would be missing on precisely
# the hosts where something is already going wrong. Set process-wide rather than
# around each call because `os.environ` is shared between threads, and a
# concurrent render could have it removed underneath it. `setdefault`, so an
# operator who chooses a value deliberately is not overridden.
os.environ.setdefault("SOURCE_DATE_EPOCH", FIXED_EPOCH)


class ExportError(RuntimeError):
    """The document could not be rendered in the format asked for."""


@dataclass(frozen=True)
class Rendered:
    content: bytes
    filename: str
    mimetype: str
    format: str

    @property
    def byte_size(self) -> int:
        return len(self.content)

    def sha256(self) -> str:
        import hashlib

        return hashlib.sha256(self.content).hexdigest()


@lru_cache(maxsize=1)
def _renderers() -> dict:
    """Imported lazily and one at a time, so a missing optional dependency
    disables one format rather than the module.

    Cached: probing is cheap but not free, and without it the "not installed"
    notice is logged on every single render — which trains everyone to ignore
    the log that is supposed to tell them a format is missing.
    """
    from . import csv as csv_renderer
    from . import html as html_renderer
    from . import markdown as markdown_renderer

    found = {
        "markdown": markdown_renderer,
        "html": html_renderer,
        "csv": csv_renderer,
    }

    try:
        from . import docx as docx_renderer
    except ImportError:  # pragma: no cover — exercised by the container build
        logger.info("python-docx is not installed; DOCX export is unavailable")
    else:
        found["docx"] = docx_renderer

    # OSError as well as ImportError, and that is not defensive padding: the
    # weasyprint PACKAGE imports fine and then raises OSError("cannot load
    # library 'libpango-1.0-0'") when its system libraries are absent. Catching
    # only ImportError means a host missing one apt package takes out every
    # export format instead of one — including the Markdown that needs nothing.
    try:
        from . import pdf as pdf_renderer
    except (ImportError, OSError) as exc:  # pragma: no cover
        logger.info("PDF export is unavailable: %s", exc)
    else:
        found["pdf"] = pdf_renderer

    return found


#: Ordered as an operator would choose. PDF first because it is what a client
#: forwards; Markdown last because it is for us.
FORMAT_ORDER = ("pdf", "docx", "html", "markdown", "csv")

LABELS = {
    "pdf": "PDF",
    "docx": "Word",
    "html": "Web page",
    "markdown": "Markdown",
    "csv": "Spreadsheet",
}


def available() -> list[str]:
    found = _renderers()
    return [fmt for fmt in FORMAT_ORDER if fmt in found]


def render(document: ExportDocument, fmt: str) -> Rendered:
    renderer = _renderers().get(fmt)
    if renderer is None:
        raise ExportError(
            f"“{fmt}” is not an available export format. "
            f"Available: {', '.join(available())}."
        )

    content = renderer.render(document)
    return Rendered(
        content=content,
        filename=f"{document.slug()}.{renderer.EXTENSION}",
        mimetype=renderer.MIMETYPE,
        format=fmt,
    )

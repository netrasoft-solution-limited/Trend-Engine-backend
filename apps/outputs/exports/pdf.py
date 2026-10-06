"""PDF — printed from exactly the same HTML a browser would show.

One template for both formats, so they cannot drift: a layout problem is visible
in a browser without building the container, and fixing it fixes both.

WHERE THIS RUNS MATTERS AS MUCH AS WHICH LIBRARY. Rendering a PDF takes a second
or two, which is fine for an operator pressing Download and wrong for a
delivery: `config/celery.py`'s `default` queue exists precisely so that "short
tasks (exports, email)" do not queue behind a transcription. Delivery renders in
the worker; the operator's own download is synchronous because they are waiting
for it. Do not "fix" one into the other.

DETERMINISM TOOK THREE GOES TO GET RIGHT, and the answer was not where it
looked. A PDF carries `/CreationDate`, `/ModDate` and a document `/ID`, so those
are the obvious suspects — the identifier is pinned below, derived from the
content itself. But pinning all three still produced different bytes on every
render, and diffing two uncompressed PDFs showed why: the difference was inside
the EMBEDDED FONT PROGRAM, in the TrueType `head` table, which carries the
font's own created/modified timestamps. WeasyPrint subsets each font it embeds,
and the subsetter stamps those fields from the clock.

`SOURCE_DATE_EPOCH` is the reproducible-builds convention that fontTools honours
for exactly this, and setting it is what finally makes two renders identical.

Worth knowing because the symptom is so misleading: every `ExportArtifact.sha256`
would differ for content that had not changed, and anyone investigating would
reasonably spend the day on PDF metadata.

SYSTEM LIBRARIES ARE REQUIRED, including a font package — see `deploy/Dockerfile`.
Without `fonts-dejavu-core` a slim container has no font with the curly quotes
and `·` bullets this content is full of, and they render as empty boxes. The
document looks broken rather than failing, which is the harder fault to notice.
"""
from __future__ import annotations

import hashlib

from weasyprint import HTML

from . import html as html_renderer
from .document import ExportDocument

MIMETYPE = "application/pdf"
EXTENSION = "pdf"

# SOURCE_DATE_EPOCH is set in this package's __init__, not here: this module
# does not import at all without its system libraries, so a setting that lives
# here would be absent on exactly the machines where something is already wrong.


def render(document: ExportDocument) -> bytes:
    source = html_renderer.render(document)

    # Derived from the content, so it is stable across renders of the same
    # document and different between different ones — which is what a PDF
    # identifier is for. WeasyPrint otherwise generates a fresh one each time.
    identifier = hashlib.sha256(source).hexdigest()[:32].encode("ascii")

    return HTML(string=source.decode("utf-8")).write_pdf(pdf_identifier=identifier)

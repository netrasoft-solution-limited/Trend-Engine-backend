"""CSV — one row per paragraph.

A CSV of prose is of limited use and this does not pretend otherwise: it is
`section,paragraph` and nothing more, for someone who wants the text in a
spreadsheet.

The CSV that will actually be wanted is the claim/evidence table with scores and
sources — a different export, from a different query, and a later package. The
two are kept apart deliberately, because naming this one "the data export" is
how it would quietly become the thing nobody can replace.
"""
from __future__ import annotations

import csv as _csv
import io

from .document import ExportDocument

MIMETYPE = "text/csv; charset=utf-8"
EXTENSION = "csv"


def render(document: ExportDocument) -> bytes:
    buffer = io.StringIO(newline="")
    # \r\n, which is what RFC 4180 specifies and what Excel expects. Fixed
    # rather than platform-dependent, so the bytes are the same everywhere —
    # the sha256 on ExportArtifact is worthless if the line ending follows the
    # machine that rendered it.
    writer = _csv.writer(buffer, lineterminator="\r\n", quoting=_csv.QUOTE_MINIMAL)

    writer.writerow(["section", "paragraph"])
    if document.is_draft:
        writer.writerow([document.notice, ""])

    for section in document.sections:
        for block in section.blocks:
            if block.is_bullets:
                for item in block.items:
                    writer.writerow([section.heading, item])
            else:
                writer.writerow([section.heading, block.text])

    return buffer.getvalue().encode("utf-8")

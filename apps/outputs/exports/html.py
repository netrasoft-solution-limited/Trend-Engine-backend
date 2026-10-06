"""HTML — readable on its own, and the source the PDF is printed from.

One template for both, so the two cannot drift. WeasyPrint consumes exactly
these bytes (`pdf.py`), which means a layout problem is visible in a browser
without building the container.

Rendered through Django's template engine rather than string-building because
the content is client-facing prose: `{{ }}` escapes, and a claim quoting someone
who said "<" would otherwise produce broken markup — or worse, markup.
"""
from __future__ import annotations

from django.template.loader import render_to_string

from .document import ExportDocument

MIMETYPE = "text/html; charset=utf-8"
EXTENSION = "html"

TEMPLATE = "exports/document.html"


def render(document: ExportDocument) -> bytes:
    return render_to_string(TEMPLATE, {"document": document}).encode("utf-8")

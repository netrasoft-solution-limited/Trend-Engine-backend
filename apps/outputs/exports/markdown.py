"""Markdown. PRD §14 names it as one of the two required export formats.

Deterministic by construction: no timestamps, no ordering that depends on
anything but the document, and a trailing newline so the file is well-formed for
every tool that reads it.
"""
from __future__ import annotations

from .document import ExportDocument

MIMETYPE = "text/markdown; charset=utf-8"
EXTENSION = "md"


def render(document: ExportDocument) -> bytes:
    lines: list[str] = []

    if document.is_draft:
        # First line, not a footer. Someone who forwards the first screenful
        # has still forwarded the warning.
        lines += [f"> **{document.notice}**", ""]

    lines += [f"# {document.title}", ""]

    meta = [document.type_label, f"Prepared for {document.organization_name}"]
    if document.dated:
        meta.append(document.dated.strftime("%-d %B %Y"))
    lines += [" · ".join(part for part in meta if part), ""]

    if document.summary:
        lines += [document.summary, ""]

    for section in document.sections:
        lines += [f"## {section.heading}", ""]
        for block in section.blocks:
            if block.is_bullets:
                lines += [f"- {item}" for item in block.items]
                lines.append("")
            else:
                lines += [block.text, ""]

    if not document.provenance.is_empty:
        parts = []
        if document.provenance.client_profile_version:
            parts.append(f"client profile {document.provenance.client_profile_version}")
        if document.provenance.domain_pack_version:
            parts.append(f"domain pack {document.provenance.domain_pack_version}")
        lines += ["---", "", f"*Built from {', '.join(parts)}.*", ""]

    # One trailing newline exactly: `\n`.join plus a final newline, rather than
    # letting a blank last entry decide, so the byte count is predictable.
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")

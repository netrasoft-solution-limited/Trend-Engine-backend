"""Turning a section's text into blocks, once, for every format.

`OutputVersion.body` and `Publication.body` are a list of `{"heading", "text"}`,
and that `text` carries three conventions produced by `apps/outputs/drafting.py`:

    paragraphs   separated by a blank line (\\n\\n)
    bullets      lines beginning with "·", single-newline separated
    quotes       curly, inside the prose

Nothing declares those conventions anywhere. If each of five renderers reads
them independently, one body produces five different documents, and the binary
formats go wrong in ways nobody notices until a client reads one — a bullet list
rendered as a single run-on paragraph in DOCX reads as sloppy writing rather
than as a bug.

So the parse happens exactly once, here, with no knowledge of any format and no
model imports. Every renderer consumes blocks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: The bullet `drafting._methodology` emits. Matched explicitly rather than by a
#: general "does this look like a list?" rule — guessing would reformat a
#: sentence that happens to start with a dash.
BULLET = "·"

_WHITESPACE_ONLY = re.compile(r"^\s*$")


@dataclass(frozen=True)
class Paragraph:
    text: str

    @property
    def is_bullets(self) -> bool:
        return False


@dataclass(frozen=True)
class Bullets:
    items: tuple[str, ...]

    @property
    def is_bullets(self) -> bool:
        return True


Block = Paragraph | Bullets


def blocks(text: str) -> tuple[Block, ...]:
    """Parse one section's text into paragraphs and bullet lists.

    Deliberately total: any string parses, and anything unrecognised stays a
    paragraph. A renderer must never be handed something it has to guess about,
    and a parse that can fail would make a malformed body an export outage
    rather than an ugly page.
    """
    if not text:
        return ()

    out: list[Block] = []
    # Blank-line separated groups. A group may still contain single newlines,
    # which is how the methodology section carries its source list.
    for group in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        if _WHITESPACE_ONLY.match(group):
            continue
        out.extend(_parse_group(group))
    return tuple(out)


def _parse_group(group: str) -> list[Block]:
    """One blank-line-delimited group, which may mix a lead-in with a list."""
    blocks_out: list[Block] = []
    pending_items: list[str] = []
    pending_lines: list[str] = []

    def flush_lines() -> None:
        if pending_lines:
            blocks_out.append(Paragraph(" ".join(pending_lines).strip()))
            pending_lines.clear()

    def flush_items() -> None:
        if pending_items:
            blocks_out.append(Bullets(tuple(pending_items)))
            pending_items.clear()

    for line in group.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(BULLET):
            # A list interrupts prose, so the lead-in ("Sources (3):") becomes
            # its own paragraph rather than being swallowed into the first item.
            flush_lines()
            item = stripped[len(BULLET):].strip()
            if item:
                pending_items.append(item)
        else:
            flush_items()
            pending_lines.append(stripped)

    flush_lines()
    flush_items()
    return blocks_out


def plain(text: str) -> str:
    """The same content as one flat string, for formats with no structure.

    Used by the CSV exporter, which has one cell per paragraph and no way to
    represent a list.
    """
    parts = []
    for block in blocks(text):
        if block.is_bullets:
            parts.extend(block.items)
        else:
            parts.append(block.text)
    return "\n".join(parts)

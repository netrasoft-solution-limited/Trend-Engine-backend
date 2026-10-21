"""Turning a section's text into blocks, once, for every format.

`OutputVersion.body` and `Publication.body` are a list of `{"heading", "text"}`,
and that `text` carries four conventions produced by `apps/outputs/drafting.py`
and `apps/outputs/briefing.py`:

    paragraphs   separated by a blank line (\\n\\n)
    bullets      lines beginning with "·", single-newline separated
    quotes       curly, inside the prose
    tables       lines beginning with "|", cells separated by "|"; the first
                 row is the header and a |---| row under it is optional
    links        [label](https://…), inline — Markdown's own syntax, so the
                 Markdown export needs no translation and every other format
                 reads it through `spans()`

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

#: Only http(s). A `javascript:` or `file:` target in client-facing content is
#: either a mistake or an attack, and either way it stays plain text.
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


TABLE = "|"
_RULE = re.compile(r"^\|?[\s:|-]+\|?$")


@dataclass(frozen=True)
class Paragraph:
    text: str
    is_bullets = False
    is_table = False


@dataclass(frozen=True)
class Bullets:
    items: tuple[str, ...]
    is_bullets = True
    is_table = False


@dataclass(frozen=True)
class Table:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    is_bullets = False
    is_table = True


Block = Paragraph | Bullets | Table


def _cells(line: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in line.strip().strip("|").split("|"))


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
    pending_rows: list[tuple[str, ...]] = []

    def flush_lines() -> None:
        if pending_lines:
            blocks_out.append(Paragraph(" ".join(pending_lines).strip()))
            pending_lines.clear()

    def flush_items() -> None:
        if pending_items:
            blocks_out.append(Bullets(tuple(pending_items)))
            pending_items.clear()

    def flush_rows() -> None:
        if pending_rows:
            width = len(pending_rows[0])
            rows = tuple((r + ("",) * width)[:width] for r in pending_rows[1:])
            blocks_out.append(Table(pending_rows[0], rows))
            pending_rows.clear()

    for line in group.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(TABLE):
            flush_lines()
            flush_items()
            if not (pending_rows and _RULE.match(stripped)):
                pending_rows.append(_cells(stripped))
            continue
        flush_rows()
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
    flush_rows()
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
        elif block.is_table:
            parts.extend(" · ".join(r) for r in (block.header, *block.rows))
        else:
            parts.append(block.text)
    return unlinked("\n".join(parts))


@dataclass(frozen=True)
class Span:
    text: str
    href: str = ""


def spans(text: str) -> tuple[Span, ...]:
    """A line of prose as text and link runs, for formats that render links."""
    out: list[Span] = []
    position = 0
    for match in _LINK.finditer(text or ""):
        if match.start() > position:
            out.append(Span(text[position:match.start()]))
        out.append(Span(match.group(1), match.group(2)))
        position = match.end()
    if position < len(text or ""):
        out.append(Span(text[position:]))
    return tuple(out)


def unlinked(text: str) -> str:
    """Links written out as "label (url)", for formats with no hyperlinks."""
    return _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text or "")

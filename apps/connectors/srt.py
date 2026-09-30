"""Turn subtitles into something extraction can quote from.

Auto-generated YouTube captions are not prose. They arrive as thousands of
overlapping two-second cues, each repeating most of the previous one as the
rolling caption fills in:

    00:00:04,880 --> 00:00:09,519   your health with Dr Christie my name is
    00:00:07,230 --> 00:00:09,519
    00:00:07,240 --> 00:00:10,440   Dr Christy ringer and today I'm going to

Concatenating those naively produces text where every phrase appears two or
three times. That matters here more than it would elsewhere: extraction anchors
each claim to a verbatim quote inside a segment, so duplicated text means the
quote a model returns may match in several places, and the character offsets
stop meaning anything.

So this deduplicates on the way through, and keeps the timestamp of the cue
that first introduced each line — which is what makes `ContentSegment`'s
`start_seconds` a real position in the audio rather than a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: `00:01:02,345` and the sloppier `00:00:4,880` that auto-captions emit.
TIMESTAMP = re.compile(
    r"(?P<h>\d+):(?P<m>\d{1,2}):(?P<s>\d{1,2})[,.](?P<ms>\d{1,3})\s*-->\s*"
    r"(?P<eh>\d+):(?P<em>\d{1,2}):(?P<es>\d{1,2})[,.](?P<ems>\d{1,3})"
)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse(srt: str) -> list[Cue]:
    """Parse SRT into cues, dropping empties and music/sound markers."""
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", srt.strip()):
        match = TIMESTAMP.search(block)
        if not match:
            continue

        body_lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not TIMESTAMP.search(line) and not line.strip().isdigit()
        ]
        text = " ".join(body_lines).strip()

        # `[Music]`, `[Applause]` and bare whitespace cues carry no claim and
        # would only dilute the segments.
        if not text or re.fullmatch(r"\[.*?\]", text):
            continue

        cues.append(
            Cue(
                start=_seconds(match["h"], match["m"], match["s"], match["ms"]),
                end=_seconds(match["eh"], match["em"], match["es"], match["ems"]),
                text=text,
            )
        )
    return cues


def to_text(srt: str) -> tuple[str, list[tuple[float, int]]]:
    """Flatten SRT to prose, plus a map from time to character offset.

    Returns `(text, anchors)` where each anchor is `(start_seconds, char_index)`
    for the point each new piece of speech begins. That is what lets a segment
    built on character offsets also report where in the audio it came from.

    Deduplication is by suffix overlap rather than by exact line match: a
    rolling caption repeats a *prefix* of the next cue, so the useful question
    is "how much of this cue is already at the end of what I have".
    """
    cues = parse(srt)
    parts: list[str] = []
    anchors: list[tuple[float, int]] = []
    running = ""

    for cue in cues:
        addition = _new_suffix(running, cue.text)
        if not addition:
            continue
        anchors.append((cue.start, len(running) + (1 if running else 0)))
        parts.append(addition)
        running = " ".join(parts)

    return running, anchors


def _new_suffix(existing: str, incoming: str) -> str:
    """The part of `incoming` not already at the tail of `existing`.

    Compares on the last ~300 characters: a rolling caption never repeats more
    than a few seconds of speech, and bounding the window keeps this linear
    rather than quadratic over a 17,000-character transcript.
    """
    if not existing:
        return incoming

    tail = existing[-300:].casefold()
    candidate = incoming.casefold()

    # Longest prefix of `incoming` that is already a suffix of `existing`.
    for size in range(min(len(candidate), len(tail)), 0, -1):
        if tail.endswith(candidate[:size]):
            remainder = incoming[size:].strip()
            return remainder
    return incoming

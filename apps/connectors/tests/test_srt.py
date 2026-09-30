"""Subtitle flattening.

The fixtures here are shaped like the real thing: the rolling-caption pattern
below is copied from a YouTube auto-generated track, where each cue repeats most
of the previous one. Getting this wrong does not crash anything — it produces a
transcript where every phrase appears three times, which silently breaks quote
anchoring downstream. So it is tested on the property that matters: a phrase
spoken once appears once.
"""
from __future__ import annotations

import pytest

from apps.connectors.srt import parse, to_text

ROLLING = """1
00:00:04,880 --> 00:00:09,519
your health with Dr Christie my name is

2
00:00:07,230 --> 00:00:09,519
your health with Dr Christie my name is
Dr Christy Risinger and today

3
00:00:09,519 --> 00:00:12,440
Dr Christy Risinger and today I'm going
to talk about creatine
"""

CLEAN = """1
00:00:01,000 --> 00:00:03,000
Five grams a day is enough.

2
00:00:03,000 --> 00:00:06,500
You do not need a loading phase.
"""


def test_rolling_captions_say_each_thing_once():
    text, _ = to_text(ROLLING)

    assert text.count("my name is") == 1
    assert text.count("Dr Christy Risinger and today") == 1
    # And nothing was lost to the deduplication.
    assert "creatine" in text
    assert text.startswith("your health with Dr Christie")


def test_anchors_point_into_the_flattened_text():
    text, anchors = to_text(ROLLING)

    assert anchors, "timed captions must produce anchors"
    for seconds, char_index in anchors:
        assert 0 <= char_index <= len(text)
        assert seconds >= 0
    # Monotonic in both dimensions — a later cue is later in the audio and
    # later in the text, which is what makes the binary search in
    # `extraction._seconds_at` valid.
    assert anchors == sorted(anchors)


def test_music_and_sound_markers_are_dropped():
    srt = """1
00:00:01,000 --> 00:00:02,000
[Music]

2
00:00:02,000 --> 00:00:04,000
Magnesium glycinate.
"""
    text, _ = to_text(srt)
    assert text == "Magnesium glycinate."


def test_sloppy_timestamps_parse():
    """Auto-captions emit `00:00:4,880` — single-digit seconds, short millis."""
    srt = "1\n00:00:4,88 --> 00:00:9,5\nCreatine monohydrate.\n"
    cues = parse(srt)

    assert len(cues) == 1
    assert cues[0].start == pytest.approx(4.880)
    assert cues[0].end == pytest.approx(9.500)


def test_empty_input_is_not_an_error():
    assert to_text("") == ("", [])
    assert parse("   ") == []


def test_untimed_prose_is_left_alone():
    text, anchors = to_text(CLEAN)
    assert text == "Five grams a day is enough. You do not need a loading phase."
    assert [a[0] for a in anchors] == [1.0, 3.0]

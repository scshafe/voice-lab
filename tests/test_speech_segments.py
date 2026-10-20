"""Sentences, seeds and joining (speech/segments.py)."""

from __future__ import annotations

import random
import re
import wave
from pathlib import Path

import pytest
from speech_fixtures import write_tone

from cole_voice_lab.speech.f5 import MIN_SEGMENT_BYTES, max_segment_bytes
from cole_voice_lab.speech.segments import (
    MIN_SEGMENT_CHARACTERS,
    Segment,
    job_seed,
    join_wavs,
    segment_seed,
    split_segments,
    split_sentences,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One sentence only", ["One sentence only"]),
        (
            "The first one ends here. Then a question? Yes, and an exclamation!",
            ["The first one ends here.", "Then a question?", "Yes, and an exclamation!"],
        ),
        ("A line break\nstarts another line.", ["A line break", "starts another line."]),
        ("He said “stop.” Then he left the room.", ["He said “stop.”", "Then he left the room."]),
        ("3.14 is not the end of a sentence here.", ["3.14 is not the end of a sentence here."]),
        ("Wait… there is more to come after this.", ["Wait… there is more to come after this."]),
        (
            "「こんにちは。」 そして、また会いましょう。",
            ["「こんにちは。」 そして、また会いましょう。"],
        ),
        ("These are long enough. OK.", ["These are long enough. OK."]),
        ("No. That is not right at all.", ["No. That is not right at all."]),
        ("Hi.", ["Hi."]),
        ("  \n  Spaced out sentence here.  \n\n ", ["Spaced out sentence here."]),
    ],
)
def test_text_is_cut_into_sentences(text: str, expected: list[str]) -> None:
    assert split_sentences(text, max_bytes=500) == expected


def test_a_long_sentence_is_cut_at_a_comma_then_a_space_then_anywhere() -> None:
    sentence = "alpha beta gamma, delta epsilon zeta, eta theta iota kappa lambda mu."
    pieces = split_sentences(sentence, max_bytes=40)
    assert pieces == ["alpha beta gamma, delta epsilon zeta,", "eta theta iota kappa lambda mu."]
    assert all(len(piece.encode()) <= 40 for piece in pieces)
    words = split_sentences("word " * 30, max_bytes=50)
    assert all(len(piece.encode()) <= 50 for piece in words) and len(words) > 1
    solid = split_sentences("x" * 120, max_bytes=50)
    assert solid == ["x" * 50, "x" * 50, "x" * 20]
    assert "".join(split_sentences("é" * 40, max_bytes=50)) == "é" * 40


def _ranges_hold(text: str, segments: list[Segment]) -> None:
    """The ranges follow one another over ``text`` with only white space between them, and
    each holds its segment's words, starting and ending with its own characters."""
    assert segments, text
    stripped = text.strip()
    lead = len(text) - len(text.lstrip())
    assert segments[0].start == lead and segments[-1].end == lead + len(stripped)
    previous = 0
    for segment in segments:
        assert previous <= segment.start < segment.end <= len(text)
        assert not text[previous : segment.start].strip()
        spoken = text[segment.start : segment.end]
        assert spoken.split() == segment.text.split()
        assert (spoken[0], spoken[-1]) == (segment.text[0], segment.text[-1])
        previous = segment.end


def test_each_segment_names_the_range_it_speaks() -> None:
    text = "  The first one ends here.  Then a question?\nYes, and an exclamation!  "
    segments = split_segments(text, max_bytes=500)
    assert segments == [
        Segment("The first one ends here.", 2, 26),
        Segment("Then a question?", 28, 44),
        Segment("Yes, and an exclamation!", 45, 69),
    ]
    assert [text[s.start : s.end] for s in segments] == [s.text for s in segments]
    _ranges_hold(text, segments)


def test_a_joined_short_sentence_spans_both_sentences() -> None:
    # Under 12 characters joins the next sentence, with one space in the segment's text; the
    # range keeps the request's own white space between them.
    text = "No.\n\nThat is not right at all. These are long enough.   OK."
    segments = split_segments(text, max_bytes=500)
    assert [s.text for s in segments] == [
        "No. That is not right at all.",
        "These are long enough. OK.",
    ]
    assert [(s.start, s.end) for s in segments] == [(0, 30), (31, 59)]
    assert text[0:30] == "No.\n\nThat is not right at all."
    assert text[31:59] == "These are long enough.   OK."
    assert len("No.") < MIN_SEGMENT_CHARACTERS
    _ranges_hold(text, segments)
    only = split_segments("Hi.", max_bytes=500)
    assert only == [Segment("Hi.", 0, 3)]


def test_a_long_sentences_pieces_name_their_ranges() -> None:
    sentence = "alpha beta gamma, delta epsilon zeta, eta theta iota kappa lambda mu."
    by_comma = split_segments(sentence, max_bytes=40)
    assert [(s.start, s.end) for s in by_comma] == [(0, 37), (38, 69)]
    assert [sentence[s.start : s.end] for s in by_comma] == [s.text for s in by_comma]
    text = "word " * 30
    by_space = split_segments(text, max_bytes=50)
    _ranges_hold(text, by_space)
    assert all(text[s.start : s.end] == s.text for s in by_space)
    hard = split_segments("x" * 120, max_bytes=50)
    assert [(s.start, s.end) for s in hard] == [(0, 50), (50, 100), (100, 120)]
    # Offsets count code points, not bytes.
    wide = split_segments("é" * 40, max_bytes=50)
    assert [(s.start, s.end) for s in wide] == [(0, 25), (25, 40)]
    # A short sentence joined to a long one is cut with it.
    joined = "Hi.  " + "alpha beta gamma, delta epsilon zeta, eta theta iota kappa."
    pieces = split_segments(joined, max_bytes=40)
    assert pieces[0].text.startswith("Hi. alpha") and pieces[0].start == 0
    _ranges_hold(joined, pieces)


def _old_split_long(sentence: str, limit: int) -> list[str]:
    # speech/segments.py before #78, verbatim: the texts must not change (rendering, seeds).
    pieces: list[str] = []
    rest = sentence
    while len(rest.encode("utf-8")) > limit:
        window = rest
        while len(window.encode("utf-8")) > limit:
            window = window[:-1]
        soft = [match.end() for match in re.finditer(r"[,;:，；：]\s", window)]
        cut = soft[-1] if soft else window.rfind(" ") + 1
        if cut <= 0:
            cut = len(window) or 1
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return [piece for piece in pieces if piece]


def _old_split_sentences(text: str, *, max_bytes: int) -> list[str]:
    terminal, closer = "[.!?…。！？]", "[\"'”’)\\]]"
    end = rf"(?<={terminal})\s+|(?<={terminal}{closer})\s+|(?<={terminal}{closer}{closer})\s+|\n+"
    sentences = [part.strip() for part in re.split(end, text) if part and part.strip()]
    merged: list[str] = []
    carry = ""
    for sentence in sentences:
        sentence = f"{carry} {sentence}".strip() if carry else sentence
        if len(sentence) < 12:
            carry = sentence
            continue
        carry = ""
        merged.append(sentence)
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]} {carry}"
        else:
            merged.append(carry)
    segments: list[str] = []
    for sentence in merged:
        segments.extend(_old_split_long(sentence, max_bytes))
    return segments


_PIECES = (
    "word", "ab", "No", "Yes", "x" * 30, "é" * 9, "こんにちは", "“stop", "3.14", "(aside",
    ".", ". ", "! ", "? ", "… ", "。", "！", ",", ", ", "; ", ": ", "，", "”", "’", ")", "]",
    " ", "  ", "\n", "\n\n", " \n ", "\t",
)  # fmt: skip


def test_the_ranges_hold_and_the_texts_are_unchanged_for_any_text() -> None:
    rng = random.Random(78)
    for _ in range(4_000):
        text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 40)))
        if not text.strip():
            continue
        for max_bytes in (16, 40, 120, 500):
            segments = split_segments(text, max_bytes=max_bytes)
            assert [s.text for s in segments] == _old_split_sentences(text, max_bytes=max_bytes)
            assert split_sentences(text, max_bytes=max_bytes) == [s.text for s in segments]
            _ranges_hold(text, segments)


def test_f5s_per_pass_limit_follows_upstreams_formula() -> None:
    # 50 bytes over 5 s of reference: 10 bytes per second, times (22 - 5) s.
    assert max_segment_bytes("x" * 50, 5 * 24_000) == 170
    assert max_segment_bytes("x" * 50, 30 * 24_000) == MIN_SEGMENT_BYTES


def test_a_tiny_limit_still_cuts_every_character_off() -> None:
    # A family's limit below one character's bytes never loops: each cut takes one character.
    assert split_sentences("ééé", max_bytes=1) == ["é", "é", "é"]


def test_seeds_are_derived_and_stable() -> None:
    seed = job_seed("a" * 64, "b" * 64, "d" * 64)
    assert seed == job_seed("a" * 64, "b" * 64, "d" * 64)
    assert seed != job_seed("a" * 64, "b" * 64, "e" * 64) != job_seed("c" * 64, "b" * 64, "d" * 64)
    assert 0 <= seed <= 0x7FFFFFFF
    seeds = [segment_seed(seed, index) for index in range(1, 6)]
    assert len(set(seeds)) == 5 and seeds == [segment_seed(seed, index) for index in range(1, 6)]


def test_segments_are_joined_in_order(tmp_path: Path) -> None:
    parts = []
    for index, frames in enumerate((100, 250, 7), start=1):
        part = tmp_path / f"{index}.wav"
        write_tone(part, frames=frames, period=10 + index)
        parts.append(part)
    output = tmp_path / "joined.wav"
    assert join_wavs(parts, output) == 357
    with wave.open(str(output), "rb") as joined:
        data = joined.readframes(joined.getnframes())
    expected = b""
    for part in parts:
        with wave.open(str(part), "rb") as handle:
            expected += handle.readframes(handle.getnframes())
    assert data == expected and output.stat().st_mode & 0o777 == 0o600
    stereo = tmp_path / "stereo.wav"
    write_tone(stereo, channels=2)
    with pytest.raises(ValueError, match="not 24 kHz mono"):
        join_wavs([stereo], tmp_path / "bad.wav")

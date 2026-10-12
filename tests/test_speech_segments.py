"""Sentences, seeds and joining (speech/segments.py)."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from speech_fixtures import write_tone

from cole_voice_lab.speech.segments import (
    MIN_MAX_CHARACTERS,
    job_seed,
    join_wavs,
    max_segment_characters,
    segment_seed,
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
    assert split_sentences(text, max_characters=500) == expected


def test_a_long_sentence_is_cut_at_a_comma_then_a_space_then_anywhere() -> None:
    sentence = "alpha beta gamma, delta epsilon zeta, eta theta iota kappa lambda mu."
    pieces = split_sentences(sentence, max_characters=40)
    assert pieces == ["alpha beta gamma, delta epsilon zeta,", "eta theta iota kappa lambda mu."]
    assert all(len(piece.encode()) <= 40 for piece in pieces)
    words = split_sentences("word " * 30, max_characters=50)
    assert all(len(piece.encode()) <= 50 for piece in words) and len(words) > 1
    solid = split_sentences("x" * 120, max_characters=50)
    assert solid == ["x" * 50, "x" * 50, "x" * 20]
    assert "".join(split_sentences("é" * 40, max_characters=50)) == "é" * 40


def test_the_per_pass_limit_follows_upstreams_formula() -> None:
    # 50 bytes over 5 s of reference: 10 bytes per second, times (22 - 5) s.
    assert max_segment_characters("x" * 50, 5 * 24_000) == 170
    assert max_segment_characters("x" * 50, 30 * 24_000) == MIN_MAX_CHARACTERS


def test_seeds_are_derived_and_stable() -> None:
    seed = job_seed("a" * 64, "b" * 64, 32)
    assert seed == job_seed("a" * 64, "b" * 64, 32)
    assert seed != job_seed("a" * 64, "b" * 64, 16) != job_seed("c" * 64, "b" * 64, 32)
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

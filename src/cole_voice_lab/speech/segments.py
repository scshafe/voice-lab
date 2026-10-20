"""Sentences, seeds and the joined audio of a speech job (D-046, docs/SPEECH_API.md §7).

@canonical speech.segments -- pure functions. ``split_segments`` cuts a text into the
segments the job runner renders one after another, none longer than the release's family
allows in one pass (``release.VerifiedRelease.max_segment_bytes``, D-048), each with the range
of the text it speaks (``split_sentences`` is the same cut, the texts alone); ``job_seed`` and
``segment_seed`` make every render reproducible (the same text, release and render settings
give the same seeds); and ``join_wavs`` concatenates the finished segments' PCM into the
whole reply.
"""

from __future__ import annotations

import hashlib
import re
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .contracts import API_SAMPLE_RATE_HZ

SAMPLE_RATE_HZ: Final = API_SAMPLE_RATE_HZ
# A fragment shorter than this ("Yes.", "OK.") joins the sentence after it.
MIN_SEGMENT_CHARACTERS: Final = 12
_TERMINAL: Final = "[.!?…。！？]"
_CLOSER: Final = "[\"'”’)\\]]"
_SENTENCE_END: Final = re.compile(
    rf"(?<={_TERMINAL})\s+|(?<={_TERMINAL}{_CLOSER})\s+|(?<={_TERMINAL}{_CLOSER}{_CLOSER})\s+|\n+"
)
_SOFT_BREAK: Final = re.compile(r"[,;:，；：]\s")
_SEED_MASK: Final = 0x7FFFFFFF


@dataclass(frozen=True, slots=True)
class Segment:
    """One segment the runner renders: its text, and the range of the request's text it speaks.

    ``start`` and ``end`` are 0-based offsets in code points into the text as received, ``end``
    exclusive. ``text[start:end]`` holds the same words as ``text`` (a short sentence joined to
    the next keeps the request's white space between them there, where ``text`` has one
    space); its first and last characters are the segment's own."""

    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Span:
    """A run of segment text and, for each of its characters, its offset in the request's
    text (a space that joins two sentences stands at the white space between them)."""

    text: str
    origins: tuple[int, ...]

    def strip(self) -> _Span:
        stripped = self.text.strip()
        lead = len(self.text) - len(self.text.lstrip())
        return _Span(stripped, self.origins[lead : lead + len(stripped)])

    def joined(self, other: _Span) -> _Span:
        return _Span(
            f"{self.text} {other.text}", (*self.origins, self.origins[-1] + 1, *other.origins)
        )

    def segment(self) -> Segment:
        return Segment(self.text, self.origins[0], self.origins[-1] + 1)


def _sentences(text: str) -> list[_Span]:
    spans: list[_Span] = []
    begin = 0
    bounds = [(match.start(), match.end()) for match in _SENTENCE_END.finditer(text)]
    for stop, after in [*bounds, (len(text), len(text))]:
        part = _Span(text[begin:stop], tuple(range(begin, stop))).strip()
        if part.text:
            spans.append(part)
        begin = after
    return spans


def _split_long(sentence: _Span, limit: int) -> list[_Span]:
    pieces: list[_Span] = []
    rest = sentence
    while len(rest.text.encode("utf-8")) > limit:
        window = rest.text
        while len(window.encode("utf-8")) > limit:
            window = window[:-1]
        soft = [match.end() for match in _SOFT_BREAK.finditer(window)]
        cut = soft[-1] if soft else window.rfind(" ") + 1
        if cut <= 0:
            cut = len(window) or 1  # a hard cut, never an empty one
        pieces.append(_Span(rest.text[:cut], rest.origins[:cut]).strip())
        rest = _Span(rest.text[cut:], rest.origins[cut:]).strip()
    if rest.text:
        pieces.append(rest)
    return [piece for piece in pieces if piece.text]


def split_segments(text: str, *, max_bytes: int) -> list[Segment]:
    """``text`` as the segments the job runner renders, in order, none empty, each with the
    range of ``text`` it speaks (``Segment``).

    Sentences end after ``.``, ``!``, ``?``, ``…`` (and their CJK forms) followed by white
    space, and at line breaks. A fragment under ``MIN_SEGMENT_CHARACTERS`` joins the next
    sentence, with one space between them; a sentence longer than ``max_bytes`` UTF-8 bytes
    is cut at its last comma, semicolon or colon within the limit, else at its last space,
    else hard. The ranges follow one another; only white space lies between them.
    """
    merged: list[_Span] = []
    carry: _Span | None = None
    for sentence in _sentences(text):
        sentence = carry.joined(sentence) if carry is not None else sentence
        if len(sentence.text) < MIN_SEGMENT_CHARACTERS:
            carry = sentence
            continue
        carry = None
        merged.append(sentence)
    if carry is not None:
        if merged:
            merged[-1] = merged[-1].joined(carry)
        else:
            merged.append(carry)
    return [piece.segment() for sentence in merged for piece in _split_long(sentence, max_bytes)]


def split_sentences(text: str, *, max_bytes: int) -> list[str]:
    """The texts of ``split_segments(text, max_bytes=max_bytes)``."""
    return [segment.text for segment in split_segments(text, max_bytes=max_bytes)]


def _seed(material: str) -> int:
    return int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:8], 16) & _SEED_MASK


def job_seed(text_digest: str, release_sha256: str, settings_sha256: str) -> int:
    """The job's seed: the same text (its keyed digest), release and render settings (the
    canonical digest of the family's ``RenderSettings``: F5-TTS's flow steps) always give the
    same one."""
    return _seed(f"cole-voice-lab.speech-seed/v2:{text_digest}:{release_sha256}:{settings_sha256}")


def segment_seed(seed: int, index: int) -> int:
    """The seed of segment ``index`` (from 1) of a job seeded ``seed``."""
    return _seed(f"cole-voice-lab.speech-segment-seed/v1:{seed}:{index}")


def join_wavs(parts: Sequence[Path], output: Path) -> int:
    """Concatenate 24 kHz mono 16-bit WAV ``parts`` into ``output`` (0600); return its frames.

    Raises: ``ValueError`` for a part of another shape; ``wave.Error`` for one that is not WAV.
    """
    frames = 0
    with wave.open(str(output), "wb") as joined:
        joined.setnchannels(1)
        joined.setsampwidth(2)
        joined.setframerate(SAMPLE_RATE_HZ)
        for part in parts:
            with wave.open(str(part), "rb") as handle:
                shape = (handle.getframerate(), handle.getnchannels(), handle.getsampwidth())
                if shape != (SAMPLE_RATE_HZ, 1, 2):
                    raise ValueError(f"{part.name} is not 24 kHz mono 16-bit")
                count = handle.getnframes()
                joined.writeframes(handle.readframes(count))
                frames += count
    output.chmod(0o600)
    return frames

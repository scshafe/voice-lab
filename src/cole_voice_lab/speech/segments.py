"""Sentences, seeds and the joined audio of a speech job (D-046, docs/SPEECH_API.md §7).

@canonical speech.segments -- pure functions. ``split_sentences`` cuts a text into the
segments the job runner renders one after another; ``max_segment_characters`` is upstream
F5-TTS's one-pass limit for a reference clip; ``job_seed`` and ``segment_seed`` make every
render reproducible (the same text, release and flow steps give the same seeds); and
``join_wavs`` concatenates the finished segments' PCM into the whole reply.
"""

from __future__ import annotations

import hashlib
import re
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Final

SAMPLE_RATE_HZ: Final = 24_000
# A fragment shorter than this ("Yes.", "OK.") joins the sentence after it.
MIN_SEGMENT_CHARACTERS: Final = 12
# Upstream's floor for a reference clip so long that its own formula gives almost nothing.
MIN_MAX_CHARACTERS: Final = 50
_TERMINAL: Final = "[.!?…。！？]"
_CLOSER: Final = "[\"'”’)\\]]"
_SENTENCE_END: Final = re.compile(
    rf"(?<={_TERMINAL})\s+|(?<={_TERMINAL}{_CLOSER})\s+|(?<={_TERMINAL}{_CLOSER}{_CLOSER})\s+|\n+"
)
_SOFT_BREAK: Final = re.compile(r"[,;:，；：]\s")
_SEED_MASK: Final = 0x7FFFFFFF


def max_segment_characters(reference_text: str, reference_frames: int) -> int:
    """F5-TTS 1.1.22's per-pass text limit for a reference clip (``utils_infer``'s formula:
    the reference text's UTF-8 bytes per second times 22 s less the clip's length)."""
    seconds = max(reference_frames / SAMPLE_RATE_HZ, 0.1)
    limit = int(len(reference_text.encode("utf-8")) / seconds * (22 - seconds))
    return max(limit, MIN_MAX_CHARACTERS)


def _split_long(sentence: str, limit: int) -> list[str]:
    pieces: list[str] = []
    rest = sentence
    while len(rest.encode("utf-8")) > limit:
        window = rest
        while len(window.encode("utf-8")) > limit:
            window = window[:-1]
        soft = [match.end() for match in _SOFT_BREAK.finditer(window)]
        cut = soft[-1] if soft else window.rfind(" ") + 1
        if cut <= 0:
            cut = len(window)
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return [piece for piece in pieces if piece]


def split_sentences(text: str, *, max_characters: int) -> list[str]:
    """``text`` as the segments the job runner renders, in order, none empty.

    Sentences end after ``.``, ``!``, ``?``, ``…`` (and their CJK forms) followed by white
    space, and at line breaks. A fragment under ``MIN_SEGMENT_CHARACTERS`` joins the next
    sentence; a sentence longer than ``max_characters`` UTF-8 bytes is cut at its last comma,
    semicolon or colon within the limit, else at its last space, else hard.
    """
    sentences = [part.strip() for part in _SENTENCE_END.split(text) if part and part.strip()]
    merged: list[str] = []
    carry = ""
    for sentence in sentences:
        sentence = f"{carry} {sentence}".strip() if carry else sentence
        if len(sentence) < MIN_SEGMENT_CHARACTERS:
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
        segments.extend(_split_long(sentence, max_characters))
    return segments


def _seed(material: str) -> int:
    return int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:8], 16) & _SEED_MASK


def job_seed(text_sha256: str, release_sha256: str, nfe_steps: int) -> int:
    """The job's seed: the same text, release and flow steps always give the same one."""
    return _seed(f"cole-voice-lab.speech-seed/v1:{text_sha256}:{release_sha256}:{nfe_steps}")


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

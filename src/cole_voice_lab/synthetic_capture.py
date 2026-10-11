"""Synthetic capture packages for tests and intake acceptance runs: no human audio at all.

@canonical synthetic_capture -- ``build_synthetic_session`` writes a complete, finalized
capture package (room tone plus prompts, 48 kHz 24-bit mono PCM) whose audio is generated
sine tones and low-level noise, through the real ``finalize_capture_session``. The subject
is ``synthetic-test`` and the suite ``synthetic-acceptance``, so such a package can never be
mistaken for a recording of a person. It exists so that the home-host intake can be
exercised end to end on macOS and Linux (design phase 2 acceptance) with no recording.
"""

from __future__ import annotations

import math
import random
import wave
from datetime import datetime
from pathlib import Path
from typing import Final

from .capture import expected_recording_paths, finalize_capture_session
from .models import CaptureDevice, Prompt, PromptSuite, utc_now

SYNTHETIC_SUBJECT: Final = "synthetic-test"
SYNTHETIC_SUITE: Final = "synthetic-acceptance"
SAMPLE_RATE: Final = 48_000


def synthetic_suite(prompts: int = 2) -> PromptSuite:
    """Room tone and ``prompts`` neutral sentences."""
    return PromptSuite(
        suite_id=SYNTHETIC_SUITE,
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="room-tone-01",
                text="Remain silent.",
                category="room-tone",
                style="Silent",
                expected_seconds=10.0,
            ),
            *(
                Prompt(
                    prompt_id=f"synthetic-{index:02d}",
                    text=f"This is synthetic acceptance sentence number {index}.",
                    category="neutral",
                    style="Neutral",
                    expected_seconds=10.0,
                )
                for index in range(1, prompts + 1)
            ),
        ),
    )


def _write_tone(path: Path, *, seconds: float, frequency: float | None, seed: int) -> None:
    """A 24-bit mono WAV: a sine at ``frequency`` (with faint noise), or faint noise alone."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    noise = random.Random(seed)
    frames = bytearray()
    for index in range(int(seconds * SAMPLE_RATE)):
        sample = noise.uniform(-200.0, 200.0)
        if frequency is not None:
            fade = min(1.0, index / 2400, (seconds * SAMPLE_RATE - index) / 2400)
            sample += 1_000_000.0 * fade * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)
        frames += int(sample).to_bytes(3, "little", signed=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(3)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(bytes(frames))


def build_synthetic_session(
    directory: Path,
    *,
    session_id: str,
    prompts: int = 2,
    seconds: float = 2.0,
    capture_host: str = "synthetic-host",
    now: datetime | None = None,
    device: CaptureDevice | None = None,
) -> Path:
    """Write a finalized synthetic package into the new directory ``directory``."""
    suite = synthetic_suite(prompts)
    directory.mkdir(mode=0o700)
    for index, relative in enumerate(expected_recording_paths(suite)):
        frequency = None if "room-tone" in relative else 180.0 + 40.0 * index
        _write_tone(directory / relative, seconds=seconds, frequency=frequency, seed=index)
    moment = now or utc_now()
    finalize_capture_session(
        directory,
        session_id=session_id,
        subject_id=SYNTHETIC_SUBJECT,
        prompt_suite=suite,
        device=device
        or CaptureDevice(device_id="synthetic", display_name="synthetic UMIK-1 (generated)"),
        captured_at=moment,
        finalized_at=moment,
        capture_host=capture_host,
    )
    return directory

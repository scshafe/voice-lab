"""Synthetic takes for the capture tests: a canonical partial WAV and its evidence."""

from __future__ import annotations

import hashlib
import math
import os
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from voice_lab_desktop.capture.evidence import PortAudioContinuityEvidence, write_evidence
from voice_lab_desktop.capture.types import EVIDENCE_NAME, PARTIAL_NAME

STARTED = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def write_wav(path: Path, frames: int) -> None:
    """A mono 48 kHz 24-bit sine (440 Hz, about -12 dBFS), private."""
    amplitude = 0.25 * (2**23 - 1)
    pcm = b"".join(
        int(amplitude * math.sin(2 * math.pi * 440 * index / 48_000)).to_bytes(
            3, "little", signed=True
        )
        for index in range(frames)
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream, wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(3)
        writer.setframerate(48_000)
        writer.writeframes(pcm)


def evidence_for(path: Path, frames: int, **changes: Any) -> PortAudioContinuityEvidence:
    payload = path.read_bytes()
    fields: dict[str, Any] = {
        "host_api": "ALSA",
        "simulated": True,
        "device_name": "Umik-1  Gain: 18dB: USB Audio (hw:2,0)",
        "device_usb_id": "2752:0007",
        "output_name": PARTIAL_NAME,
        "wav_sha256": hashlib.sha256(payload).hexdigest(),
        "wav_size_bytes": len(payload),
        "frames": frames,
        "received_frames": frames,
        "callback_count": 5,
        "block_frames": 1024,
        "first_adc_time": 10.0,
        "last_adc_end": 10.0 + frames / 48_000,
        "adc_time_valid": True,
        "tolerance_frames": 64.0,
        "max_abs_deviation_frames": 0.5,
        "input_overflows": 0,
        "input_underflows": 0,
        "queue_capacity": 64,
        "queue_high_water": 2,
        "success": True,
        "continuity_checked": True,
        "stop_reason": "requested",
        "error": None,
        "started_at": STARTED,
        **changes,
    }
    return PortAudioContinuityEvidence(**fields)


def make_take(take_dir: Path, frames: int = 4800, **changes: Any) -> PortAudioContinuityEvidence:
    """A finished take as the worker leaves it: the partial and its evidence."""
    take_dir.mkdir(mode=0o700, exist_ok=True)
    write_wav(take_dir / PARTIAL_NAME, frames)
    evidence = evidence_for(take_dir / PARTIAL_NAME, frames, **changes)
    write_evidence(take_dir / EVIDENCE_NAME, evidence)
    return evidence

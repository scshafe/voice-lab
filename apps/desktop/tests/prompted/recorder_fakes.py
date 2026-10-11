"""A stand-in for the capture worker (``capture.types.TakeRecorder``): no device, no
process. A take's frames are confirmed when the test says so (``advance``); stopping writes
a canonical mono 48 kHz 24-bit WAV of the frames asked for, and a small continuity record,
into the take directory, as the real recorder publishes them. A take can be made to fail."""

from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from voice_lab_desktop.capture.types import (
    EVIDENCE_NAME,
    ORIGINAL_NAME,
    PARTIAL_NAME,
    SAMPLE_RATE,
    CapturedTake,
    CaptureFailed,
)
from voice_lab_desktop.measurement.devices import DeviceRefused

from cole_voice_lab.models import utc_now

DEVICE = "Umik-1  Gain: 18dB [Core Audio]"


def write_take_wav(path: Path, frames: int, *, hz: float = 220.0, amplitude: float = 0.4) -> None:
    """Canonical s24le mono 48 kHz PCM: a sine whose sample ``n`` is predictable."""
    out = bytearray(frames * 3)
    scale = amplitude * (2**23 - 1)
    for n in range(frames):
        value = int(round(scale * math.sin(2 * math.pi * hz * n / SAMPLE_RATE)))
        out[3 * n : 3 * n + 3] = value.to_bytes(3, "little", signed=True)
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        with wave.open(stream, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(3)
            writer.setframerate(SAMPLE_RATE)
            writer.writeframes(bytes(out))


@dataclass
class FakeRunningTake:
    take_dir: Path
    frames: int
    requested_at: datetime
    device: str = DEVICE
    fail: str | None = None
    _confirmed: int = 0
    _done: bool = False
    stops: int = 0

    @property
    def confirmed_frames(self) -> int:
        return self._confirmed

    @property
    def done(self) -> bool:
        return self._done

    def advance(self, frames: int) -> None:
        """The worker confirmed ``frames`` more frames."""
        self._confirmed = min(self.frames, self._confirmed + frames)

    def stop(self) -> None:
        self.stops += 1
        if self._done:
            return
        if self.fail is None:
            write_take_wav(self.take_dir / ORIGINAL_NAME, self.frames)
            evidence = {"schema": "fake-continuity", "success": True, "frames": self.frames}
        else:
            write_take_wav(self.take_dir / PARTIAL_NAME, max(1, self._confirmed or 480))
            evidence = {"schema": "fake-continuity", "success": False, "error": self.fail}
        target = self.take_dir / EVIDENCE_NAME
        target.write_text(json.dumps(evidence))
        target.chmod(0o600)
        self._confirmed = self.frames if self.fail is None else self._confirmed
        self._done = True

    def finish_by_itself(self) -> None:
        """The worker stopped on its own (its maximum duration)."""
        self.stop()

    def wait(self, timeout: float | None = None) -> CapturedTake:
        if not self._done:
            raise TimeoutError("the fake take is still running")
        if self.fail is not None:
            raise CaptureFailed(
                self.fail,
                partial=self.take_dir / PARTIAL_NAME,
                evidence=self.take_dir / EVIDENCE_NAME,
            )
        path = self.take_dir / ORIGINAL_NAME
        data = path.read_bytes()
        return CapturedTake(
            path=path,
            evidence_path=self.take_dir / EVIDENCE_NAME,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            frames=self.frames,
            requested_at=self.requested_at,
            device=self.device,
        )


@dataclass
class FakeRecorder:
    """Takes of ``frames`` frames each; ``fail_next`` makes the next take fail with that
    message; ``start_error`` makes ``start`` itself fail; ``refused`` makes the device check
    refuse (no UMIK-1)."""

    frames: int = 24_000
    device: str = DEVICE
    fail_next: str | None = None
    start_error: str | None = None
    refused: str | None = None
    takes: list[FakeRunningTake] = field(default_factory=list)

    def device_label(self) -> str:
        if self.refused:
            raise DeviceRefused(self.refused)
        return self.device

    def start(self, take_dir: Path) -> FakeRunningTake:
        if self.start_error:
            raise CaptureFailed(self.start_error)
        assert take_dir.is_dir() and not any(take_dir.iterdir())
        assert (take_dir.stat().st_mode & 0o777) == 0o700
        running = FakeRunningTake(
            take_dir, self.frames, utc_now(), device=self.device, fail=self.fail_next
        )
        self.fail_next = None
        self.takes.append(running)
        return running

    @property
    def last(self) -> FakeRunningTake:
        return self.takes[-1]

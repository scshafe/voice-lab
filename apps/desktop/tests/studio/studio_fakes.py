"""A stand-in for the capture worker that leaves what the real one leaves: a generated tone as
``original.wav`` and the PortAudio worker's continuity evidence bound to it
(``corpus_rig.capture``), so the corpus client saves it as it would a real take. No device,
no process, no person's voice. Each take's tone is its own (``seed``), so each has its own
take id. ``simulated`` (default) marks it as the simulated device's: it stays on this machine;
``simulated=False`` makes a take the PC accepts. ``hold_seal`` keeps each take's check (the
worker's wait) running until the test calls ``seal``, as a slow stop would."""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from corpus_rig import DEVICE, capture
from voice_lab_desktop.capture.types import PARTIAL_NAME, CapturedTake, CaptureFailed

_SEEDS = itertools.count(100)


@dataclass
class StudioTake:
    take_dir: Path
    seconds: float
    simulated: bool
    fail: str | None = None
    seed: int = field(default_factory=lambda: next(_SEEDS))
    _confirmed: int = 0
    _done: bool = False
    _captured: CapturedTake | None = None
    stops: int = 0
    #: Set: the worker's check is done (``None``: at once, when stopped).
    sealed: threading.Event | None = None

    @property
    def confirmed_frames(self) -> int:
        return self._confirmed

    @property
    def done(self) -> bool:
        return self._done

    def advance(self, frames: int, amplitude: float = 0.25) -> None:
        """The worker wrote and confirmed ``frames`` more frames (a tone at ``amplitude``) in
        its partial WAV, as the real worker does block by block."""
        partial = self.take_dir / PARTIAL_NAME
        if not partial.exists():
            partial.write_bytes(b"RIFF" + bytes(40))  # the header, rewritten at the end
        start = self._confirmed
        phases = (np.arange(start, start + frames) * 2 * np.pi * 220.0 / 48_000).astype(np.float64)
        values = np.round(np.sin(phases) * amplitude * (2**23 - 1)).astype(np.int32)
        raw = np.stack([values & 0xFF, (values >> 8) & 0xFF, (values >> 16) & 0xFF], axis=1)
        with partial.open("ab") as stream:
            stream.write(raw.astype(np.uint8).tobytes())
        self._confirmed += frames

    def stop(self) -> None:
        self.stops += 1
        if self._done:
            return
        if self.fail is None:
            (self.take_dir / PARTIAL_NAME).unlink(missing_ok=True)  # published as original.wav
            self._captured = capture(
                self.take_dir, seed=self.seed, seconds=self.seconds, simulated=self.simulated
            )
            self._confirmed = self._captured.frames
        self._done = True

    def wait(self, timeout: float | None = None) -> CapturedTake:
        if not self._done:
            raise TimeoutError("the fake take is still running")
        if self.sealed is not None and not self.sealed.wait(timeout):
            raise TimeoutError("the fake take's check did not finish")
        if self.fail is not None:
            raise CaptureFailed(self.fail)
        assert self._captured is not None
        return self._captured


@dataclass
class StudioRecorder:
    """Takes of ``seconds`` each; ``fail_next`` fails the next take with that message;
    ``start_error`` makes ``start`` fail; ``refused`` makes the device check refuse;
    ``hold_seal`` holds each take's check until ``seal``."""

    seconds: float = 0.25
    simulated: bool = True
    fail_next: str | None = None
    start_error: str | None = None
    refused: str | None = None
    hold_seal: bool = False
    takes: list[StudioTake] = field(default_factory=list)

    def device_label(self) -> str:
        if self.refused:
            from voice_lab_desktop.measurement.devices import DeviceRefused

            raise DeviceRefused(self.refused)
        return DEVICE

    def start(self, take_dir: Path) -> StudioTake:
        if self.start_error:
            raise CaptureFailed(self.start_error)
        assert take_dir.is_dir() and not any(take_dir.iterdir())
        assert (take_dir.stat().st_mode & 0o777) == 0o700
        take = StudioTake(take_dir, self.seconds, self.simulated, fail=self.fail_next)
        if self.hold_seal:
            take.sealed = threading.Event()
        self.fail_next = None
        self.takes.append(take)
        return take

    @property
    def last(self) -> StudioTake:
        return self.takes[-1]

    def seal(self) -> None:
        """The last take's check is done (with ``hold_seal``)."""
        assert self.last.sealed is not None
        self.last.sealed.set()

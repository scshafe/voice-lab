"""A stand-in for the capture worker that leaves what the real one leaves: a generated tone as
``original.wav`` and the PortAudio worker's continuity evidence bound to it
(``corpus_rig.capture``), so the corpus client saves it as it would a real take. No device,
no process, no person's voice. Each take's tone is its own (``seed``), so each has its own
take id. ``simulated`` (default) marks it as the simulated device's: it stays on this machine;
``simulated=False`` makes a take the PC accepts."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path

from corpus_rig import DEVICE, capture
from voice_lab_desktop.capture.types import CapturedTake, CaptureFailed

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

    @property
    def confirmed_frames(self) -> int:
        return self._confirmed

    @property
    def done(self) -> bool:
        return self._done

    def advance(self, frames: int) -> None:
        """The worker confirmed ``frames`` more frames."""
        self._confirmed += frames

    def stop(self) -> None:
        self.stops += 1
        if self._done:
            return
        if self.fail is None:
            self._captured = capture(
                self.take_dir, seed=self.seed, seconds=self.seconds, simulated=self.simulated
            )
            self._confirmed = self._captured.frames
        self._done = True

    def wait(self, timeout: float | None = None) -> CapturedTake:
        if not self._done:
            raise TimeoutError("the fake take is still running")
        if self.fail is not None:
            raise CaptureFailed(self.fail)
        assert self._captured is not None
        return self._captured


@dataclass
class StudioRecorder:
    """Takes of ``seconds`` each; ``fail_next`` fails the next take with that message;
    ``start_error`` makes ``start`` fail; ``refused`` makes the device check refuse."""

    seconds: float = 0.25
    simulated: bool = True
    fail_next: str | None = None
    start_error: str | None = None
    refused: str | None = None
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
        self.fail_next = None
        self.takes.append(take)
        return take

    @property
    def last(self) -> StudioTake:
        return self.takes[-1]

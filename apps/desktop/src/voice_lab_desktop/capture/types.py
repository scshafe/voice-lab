"""What the Record: prompts screen needs from a capture: one take at a time (phase 4c).

The real recorder runs the PortAudio capture worker in its own process (``worker.py``,
D-039): it writes a take's PCM and its continuity evidence, and the parent validates both
before the take exists (fail closed, as D-015 asks of the Swift app's worker). The screen
sees only these types, so tests can run it over the simulated device or a stand-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

SAMPLE_RATE: Final = 48_000
FRAME_BYTES: Final = 3
#: The take's PCM and its evidence inside its (new, private) take directory.
ORIGINAL_NAME: Final = "original.wav"
PARTIAL_NAME: Final = "original.partial.wav"
EVIDENCE_NAME: Final = "continuity.json"


@dataclass(frozen=True)
class CapturedTake:
    """A take whose PCM and continuity evidence were validated and published."""

    path: Path
    evidence_path: Path
    sha256: str
    #: Exactly ``44 + frames * 3``.
    size_bytes: int
    frames: int
    #: UTC, when the parent asked the worker to start (D-010: never a file time).
    requested_at: datetime
    #: The device the worker opened, e.g. ``Umik-1  Gain: 18dB [Core Audio]``.
    device: str


class CaptureFailed(Exception):
    """The take did not complete; the message says why. What was written is kept, never
    published: the partial PCM and the evidence (when there is any) stay in the take
    directory for inspection."""

    def __init__(
        self, message: str, *, partial: Path | None = None, evidence: Path | None = None
    ) -> None:
        super().__init__(message)
        self.partial = partial
        self.evidence = evidence


class RunningTake(Protocol):
    """A take being recorded."""

    @property
    def confirmed_frames(self) -> int:
        """Frames the worker has written so far (D-014: the elapsed time is this / 48 kHz,
        never a wall clock)."""
        ...

    @property
    def done(self) -> bool:
        """The worker has exited (stopped, failed, or reached its maximum duration)."""
        ...

    def stop(self) -> None:
        """Ask the worker to stop; idempotent."""
        ...

    def wait(self, timeout: float | None = None) -> CapturedTake:
        """The validated take once the worker has exited.

        Raises: ``CaptureFailed``; ``TimeoutError`` when ``timeout`` passes first."""
        ...


class TakeRecorder(Protocol):
    """Starts takes on this machine's capture device."""

    def device_label(self) -> str:
        """The device a take would open. Raises: ``measurement.devices.DeviceError``
        (``DeviceRefused`` for anything that is not a UMIK-1)."""
        ...

    def start(self, take_dir: Path) -> RunningTake:
        """Start a take writing into ``take_dir`` (new, empty, mode 0700).

        Raises: ``CaptureFailed`` when the worker cannot start."""
        ...

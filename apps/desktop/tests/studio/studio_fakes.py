"""A stand-in for the capture worker that leaves what the real one leaves: a generated tone as
``original.wav`` and the PortAudio worker's continuity evidence bound to it
(``corpus_rig.capture``), so the corpus client saves it as it would a real take. No device,
no process, no person's voice. Each take's tone is its own (``seed``), so each has its own
take id. ``simulated`` (default) marks it as the simulated device's: it stays on this machine;
``simulated=False`` makes a take the PC accepts. ``hold_seal`` keeps each take's check (the
worker's wait) running until the test calls ``seal``, as a slow stop would. ``voice`` makes
each take speech-like audio instead of the tone (``voice_samples``: a room and a voice), for
the quality flags."""

from __future__ import annotations

import itertools
import os
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import numpy as np
import numpy.typing as npt
from corpus_rig import DEVICE, capture
from corpus_take_fixtures import NOW, evidence_for
from voice_lab_desktop.capture.evidence import write_evidence
from voice_lab_desktop.capture.types import (
    EVIDENCE_NAME,
    ORIGINAL_NAME,
    PARTIAL_NAME,
    CapturedTake,
    CaptureFailed,
)

from cole_voice_lab.canonical import sha256_file

RATE = 48_000
type Voice = Callable[[int], npt.NDArray[np.float64]]


def voice_samples(
    seconds: float,
    *,
    speech_dbfs: float = -40.0,
    floor_dbfs: float = -80.0,
    lead: float = 0.4,
    tail: float = 0.4,
    seed: int = 0,
) -> npt.NDArray[np.float64]:
    """Speech-like audio in [-1, 1]: the room's noise at ``floor_dbfs`` RMS throughout and,
    from ``lead`` seconds to ``tail`` seconds before the end, a voice: a 150 Hz tone with two
    harmonics, swelling and falling four times a second (syllables), at ``speech_dbfs`` RMS
    (its peak about 8 dB over that). No person's voice."""
    frames = round(seconds * RATE)
    rng = np.random.default_rng(seed)
    room = rng.normal(0.0, 10 ** (floor_dbfs / 20), frames)
    start, end = round(lead * RATE), frames - round(tail * RATE)
    voiced = np.zeros(frames)
    if end > start:
        times = np.arange(end - start) / RATE
        tone = (
            np.sin(2 * np.pi * 150 * times)
            + 0.5 * np.sin(2 * np.pi * 300 * times)
            + 0.25 * np.sin(2 * np.pi * 450 * times)
        )
        swell = 0.55 + 0.45 * np.sin(2 * np.pi * 4 * times) ** 2
        segment = tone * swell
        segment *= 10 ** (speech_dbfs / 20) / np.sqrt(np.mean(segment**2))
        voiced[start:end] = segment
    return np.clip(room + voiced, -1.0, (2**23 - 1) / 2**23)


def write_wav(path: Path, samples: npt.NDArray[np.float64]) -> None:
    """``samples`` as a canonical WAV (mono 48 kHz 24-bit, 0600)."""
    values = np.clip(np.round(samples * 2**23), -(2**23), 2**23 - 1).astype(np.int32)
    raw = np.stack([values & 0xFF, (values >> 8) & 0xFF, (values >> 16) & 0xFF], axis=1)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(3)
        stream.setframerate(RATE)
        stream.writeframes(raw.astype(np.uint8).tobytes())
    os.chmod(path, 0o600)


def capture_voice(
    directory: Path, samples: npt.NDArray[np.float64], *, seed: int, simulated: bool
) -> CapturedTake:
    """What the worker leaves for ``samples``: ``original.wav`` and its evidence, checked as
    the parent checks them (``corpus_rig.capture``, with this audio instead of the tone)."""
    wav = directory / ORIGINAL_NAME
    write_wav(wav, samples)
    requested_at = NOW + timedelta(seconds=seed)
    evidence = evidence_for(wav, simulated=simulated, started_at=requested_at, device_name=DEVICE)
    write_evidence(directory / EVIDENCE_NAME, evidence)
    sha256, size = sha256_file(wav)
    return CapturedTake(
        path=wav,
        evidence_path=directory / EVIDENCE_NAME,
        sha256=sha256,
        size_bytes=size,
        frames=evidence.frames,
        requested_at=requested_at,
        device=DEVICE,
    )


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
    #: The take's audio (its seed given), instead of the tone.
    voice: Voice | None = None

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
            if self.voice is not None:
                self._captured = capture_voice(
                    self.take_dir, self.voice(self.seed), seed=self.seed, simulated=self.simulated
                )
            else:
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
    ``hold_seal`` holds each take's check until ``seal``; ``voice`` gives each take that audio
    (from its seed) instead of the tone."""

    seconds: float = 0.25
    simulated: bool = True
    fail_next: str | None = None
    start_error: str | None = None
    refused: str | None = None
    hold_seal: bool = False
    voice: Voice | None = None
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
        take = StudioTake(
            take_dir, self.seconds, self.simulated, fail=self.fail_next, voice=self.voice
        )
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

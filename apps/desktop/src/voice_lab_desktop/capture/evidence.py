"""The PortAudio capture worker's continuity evidence, and the parent's check of a take.

D-039: a take of the PortAudio worker is published only with its own evidence,
``voice-lab-desktop.portaudio-continuity/v1``: private (0600, one link, at most 16 KiB),
written by the worker on success and on failure, and bound to the exact partial WAV it
describes (name, SHA-256, size, frames). The parent validates it and inspects the WAV
before the partial becomes ``original.wav``; anything missing, malformed, unsuccessful or
mismatched fails closed and keeps both files for inspection. This is not D-015's
``capture-continuity/v1`` (Audio Queue sample times, exact to 1e-6 frames), which the
PortAudio worker never writes: its continuity rule is ``timing.py``'s.

The evidence stays on the capturing machine, like D-015's: a capture package carries
audio and its manifest, never evidence.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, model_validator

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import ContractModel, Sha256

from .types import (
    EVIDENCE_NAME,
    FRAME_BYTES,
    ORIGINAL_NAME,
    PARTIAL_NAME,
    SAMPLE_RATE,
    CapturedTake,
    CaptureFailed,
)

SCHEMA_VERSION: Final = "voice-lab-desktop.portaudio-continuity/v1"
MAX_EVIDENCE_BYTES: Final = 16 * 1024
WAV_HEADER_BYTES: Final = 44

type Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type FileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")]
type Seconds = Annotated[float, Field(allow_inf_nan=False)]


class PortAudioContinuityEvidence(ContractModel):
    """What the worker knows about one take, successful or not."""

    schema_version: Literal["voice-lab-desktop.portaudio-continuity/v1"] = SCHEMA_VERSION
    backend: Literal["portaudio"] = "portaudio"
    #: PortAudio's host API ("Core Audio", "ALSA", "JACK Audio Connection Kit"), or
    #: "Core Audio"/"ALSA" as the simulated device reports it.
    host_api: Text
    #: True when the simulated device produced the take: never hardware evidence.
    simulated: bool
    device_name: Text
    device_usb_id: Text | None
    output_name: FileName
    wav_sha256: Sha256 | None
    wav_size_bytes: int | None = Field(ge=0)
    #: Frames written to the WAV.
    frames: int = Field(ge=0)
    #: Frames accepted from PortAudio (a refused block is never written).
    received_frames: int = Field(ge=0)
    callback_count: int = Field(ge=0)
    block_frames: int = Field(gt=0, le=65536)
    sample_rate: Literal[48000] = SAMPLE_RATE
    first_adc_time: Seconds | None
    last_adc_end: Seconds | None
    adc_time_valid: bool
    tolerance_frames: float = Field(gt=0, allow_inf_nan=False)
    max_abs_deviation_frames: float | None = Field(ge=0, allow_inf_nan=False)
    input_overflows: int = Field(ge=0)
    input_underflows: int = Field(ge=0)
    queue_capacity: int = Field(gt=0)
    queue_high_water: int = Field(ge=0)
    success: bool
    #: Every accepted block's ADC time was checked against the block before it.
    continuity_checked: bool
    stop_reason: Literal["requested", "maximum-duration"] | None
    error: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None
    #: When the stream started (UTC, the worker's clock).
    started_at: AwareDatetime | None

    @model_validator(mode="after")
    def _success_is_complete(self) -> Self:
        if self.queue_high_water > self.queue_capacity:
            raise ValueError("the queue cannot hold more blocks than its capacity")
        if not self.success:
            return self
        problems = [
            problem
            for problem, broken in (
                ("an error", self.error is not None),
                ("no continuity check", not self.continuity_checked),
                ("an invalid ADC time", not self.adc_time_valid),
                ("no frames", self.frames <= 0),
                ("unwritten frames", self.frames != self.received_frames),
                ("an overflow", self.input_overflows > 0),
                ("an underflow", self.input_underflows > 0),
                ("no stop reason", self.stop_reason is None),
                ("no start time", self.started_at is None),
                ("no WAV digest", self.wav_sha256 is None),
                (
                    "a WAV size that is not 44 + 3 x frames",
                    self.wav_size_bytes != WAV_HEADER_BYTES + self.frames * FRAME_BYTES,
                ),
                ("no ADC span", self.first_adc_time is None or self.last_adc_end is None),
                (
                    "a deviation beyond the tolerance",
                    self.max_abs_deviation_frames is not None
                    and self.max_abs_deviation_frames > self.tolerance_frames,
                ),
                (
                    "no deviation measured over several blocks",
                    self.callback_count > 1 and self.max_abs_deviation_frames is None,
                ),
            )
            if broken
        ]
        if problems:
            raise ValueError("successful evidence with " + ", ".join(problems))
        return self

    @property
    def device(self) -> str:
        return f"{self.device_name} [{self.host_api}]"


def write_evidence(path: Path, evidence: PortAudioContinuityEvidence) -> None:
    """Write the evidence once: a new private file, synced. Raises: ``OSError``."""
    payload = (evidence.model_dump_json() + "\n").encode("utf-8")
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise ContractError("continuity evidence is larger than 16 KiB")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _read_private(path: Path, limit: int, *, read: bool = True) -> bytes:
    """The file's bytes (or nothing, with ``read=False``) once it is shown to be a private
    regular file with one link and at most ``limit`` bytes. Raises: ``SecurityError``."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise SecurityError(f"{path.name} is missing") from exc
    except OSError as exc:
        raise SecurityError(f"{path.name} cannot be opened safely: {exc}") from exc
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise SecurityError(f"{path.name} is not a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise SecurityError(f"{path.name} is not private (mode {stat.S_IMODE(info.st_mode):o})")
        if info.st_nlink != 1:
            raise SecurityError(f"{path.name} has another hard link")
        if info.st_size > limit:
            raise SecurityError(f"{path.name} is larger than {limit} bytes")
        return stream.read(limit + 1) if read else b""


def read_evidence(path: Path) -> PortAudioContinuityEvidence:
    """The evidence file, checked as a file and as a contract.

    Raises: ``SecurityError`` (the file), ``ContractError`` (its contents)."""
    payload = _read_private(path, MAX_EVIDENCE_BYTES)
    try:
        return PortAudioContinuityEvidence.model_validate_json(payload)
    except ValidationError as exc:
        raise ContractError(
            f"continuity evidence is not valid: {exc.error_count()} error(s)"
        ) from exc


def check_binding(take_dir: Path, *, progress_frames: int | None) -> PortAudioContinuityEvidence:
    """The take's evidence, successful and bound to the partial WAV beside it.

    Raises: ``SecurityError``/``ContractError`` naming what does not hold."""
    evidence = read_evidence(take_dir / EVIDENCE_NAME)
    if not evidence.success:
        raise ContractError(f"the capture failed: {evidence.error or 'no reason given'}")
    if evidence.output_name != PARTIAL_NAME:
        raise ContractError(f"the evidence describes {evidence.output_name}, not {PARTIAL_NAME}")
    partial = take_dir / PARTIAL_NAME
    _read_private(partial, MAX_WAV_BYTES, read=False)
    inspection = inspect_canonical_wav(partial)
    if inspection.sha256 != evidence.wav_sha256:
        raise ContractError("the WAV's SHA-256 is not the one the evidence names")
    if inspection.size_bytes != evidence.wav_size_bytes:
        raise ContractError("the WAV's size is not the one the evidence names")
    if inspection.audio.frames != evidence.frames:
        raise ContractError(
            f"the WAV holds {inspection.audio.frames} frames; the evidence names {evidence.frames}"
        )
    if progress_frames is not None and progress_frames != evidence.frames:
        raise ContractError(
            f"the worker reported {progress_frames} frames written; the WAV holds {evidence.frames}"
        )
    return evidence


#: Ten minutes of mono 24-bit PCM at 48 kHz and its header, with room to spare.
MAX_WAV_BYTES: Final = WAV_HEADER_BYTES + 48_000 * 3 * 660


def validate_take(
    take_dir: Path, *, progress_frames: int, requested_at: datetime, device: str | None = None
) -> CapturedTake:
    """Publish a finished take: check its evidence and WAV, then rename the partial to
    ``original.wav`` (never over an existing file).

    Raises: ``CaptureFailed`` keeping the partial and the evidence."""
    partial = take_dir / PARTIAL_NAME
    evidence_path = take_dir / EVIDENCE_NAME
    original = take_dir / ORIGINAL_NAME
    kept = {
        "partial": partial if os.path.lexists(partial) else None,
        "evidence": evidence_path if os.path.lexists(evidence_path) else None,
    }
    try:
        evidence = check_binding(take_dir, progress_frames=progress_frames)
        os.link(partial, original, follow_symlinks=False)
        os.unlink(partial)
    except (SecurityError, ContractError, OSError) as exc:
        raise CaptureFailed(f"the take was not kept: {exc}", **kept) from exc
    return CapturedTake(
        path=original,
        evidence_path=evidence_path,
        sha256=evidence.wav_sha256 or "",
        size_bytes=evidence.wav_size_bytes or 0,
        frames=evidence.frames,
        requested_at=requested_at,
        device=device or evidence.device,
    )

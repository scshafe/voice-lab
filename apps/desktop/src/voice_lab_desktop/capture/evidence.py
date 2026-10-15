"""The PortAudio capture worker's continuity evidence, and the parent's check of a take.

D-039: a take of the PortAudio worker is published only with its own evidence,
``voice-lab-desktop.portaudio-continuity/v1``: private (0600, one link, at most 16 KiB),
written by the worker on success and on failure, and bound to the exact partial WAV it
describes (name, SHA-256, size, frames). The parent validates it and inspects the WAV
before the partial becomes ``original.wav``; anything missing, malformed, unsuccessful or
mismatched fails closed and keeps both files for inspection. This is not D-015's
``capture-continuity/v1`` (Audio Queue sample times, exact to 1e-6 frames), which the
PortAudio worker never writes: its continuity rule is ``timing.py``'s.

A measurement package carries audio and its manifest, never evidence. A prompted take's
package (``cole_voice_lab.corpus_takes``, D-052) carries its evidence to the PC, which checks
it again.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from cole_voice_lab.audio import identify_canonical_wav
from cole_voice_lab.errors import ContractError, SecurityError

# The contract is the backend's since a take package carries it to the PC (D-052); it is
# re-exported here for the app's callers.
from cole_voice_lab.portaudio_evidence import (
    MAX_EVIDENCE_BYTES,
    SCHEMA_VERSION,
    WAV_HEADER_BYTES,
    PortAudioContinuityEvidence,
)

from .types import (
    EVIDENCE_NAME,
    ORIGINAL_NAME,
    PARTIAL_NAME,
    CapturedTake,
    CaptureFailed,
)

__all__ = [
    "MAX_EVIDENCE_BYTES",
    "MAX_WAV_BYTES",
    "SCHEMA_VERSION",
    "WAV_HEADER_BYTES",
    "PortAudioContinuityEvidence",
    "check_binding",
    "read_evidence",
    "validate_take",
    "write_evidence",
]


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
    # The digest, size and format, not the samples' levels: measuring every sample in
    # Python (about 19 ms per second of audio) was a quarter of the time from Stop to the
    # sealed take of 3 s, and would be most of it for one of 30 s (D-040).
    identity = identify_canonical_wav(partial)
    if identity.sha256 != evidence.wav_sha256:
        raise ContractError("the WAV's SHA-256 is not the one the evidence names")
    if identity.size_bytes != evidence.wav_size_bytes:
        raise ContractError("the WAV's size is not the one the evidence names")
    if identity.audio.frames != evidence.frames:
        raise ContractError(
            f"the WAV holds {identity.audio.frames} frames; the evidence names {evidence.frames}"
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

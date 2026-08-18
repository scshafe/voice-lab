"""Portable inspection and quality checks for canonical PCM WAV recordings."""

from __future__ import annotations

import hashlib
import math
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, cast

from .canonical import open_regular_no_follow
from .errors import ContractError, SecurityError
from .models import AudioQcMetrics, AudioSpec

CANONICAL_SAMPLE_RATE_HZ = 48_000
CANONICAL_CHANNELS = 1
CANONICAL_SAMPLE_WIDTH_BYTES = 3
CANONICAL_ENCODING = "pcm_s24le"

_READ_FRAMES = 65_536
_SILENCE_FLOOR_DBFS = -300.0
_QUIET_PEAK_DBFS = -35.0
_MAX_ABS_DC_OFFSET = 0.02
_MAX_ZERO_RUN_MS = 250.0


@dataclass(frozen=True, slots=True)
class WavInspection:
    """Evidence computed from one stable, descriptor-opened WAV file."""

    sha256: str
    size_bytes: int
    audio: AudioSpec
    peak_dbfs: float
    rms_dbfs: float
    dc_offset: float
    clipped_samples: int
    longest_zero_run_ms: float

    def qc_metrics(
        self,
        path: str,
        *,
        noise_rms_dbfs: float | None = None,
        allow_quiet: bool = False,
    ) -> AudioQcMetrics:
        findings: list[str] = []
        if self.clipped_samples:
            findings.append("clipped-samples")
        if not allow_quiet and self.peak_dbfs < _QUIET_PEAK_DBFS:
            findings.append("peak-too-quiet")
        if abs(self.dc_offset) > _MAX_ABS_DC_OFFSET:
            findings.append("dc-offset")
        if self.longest_zero_run_ms >= _MAX_ZERO_RUN_MS:
            findings.append("digital-silence-or-dropout")
        signal_to_noise_db = self.rms_dbfs - noise_rms_dbfs if noise_rms_dbfs is not None else None
        return AudioQcMetrics(
            path=path,
            duration_seconds=self.audio.duration_seconds,
            peak_dbfs=self.peak_dbfs,
            rms_dbfs=self.rms_dbfs,
            dc_offset=self.dc_offset,
            clipped_samples=self.clipped_samples,
            longest_zero_run_ms=self.longest_zero_run_ms,
            signal_to_noise_db=signal_to_noise_db,
            passed=not findings,
            findings=tuple(findings),
        )


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0.0:
        return _SILENCE_FLOOR_DBFS
    return min(0.0, 20.0 * math.log10(amplitude))


def _sample_values(payload: bytes, sample_width: int) -> list[int]:
    if len(payload) % sample_width:
        raise ContractError("PCM payload is not aligned to its declared sample width")
    values: list[int] = []
    if sample_width == 3:
        for offset in range(0, len(payload), 3):
            value = payload[offset] | payload[offset + 1] << 8 | payload[offset + 2] << 16
            if value & 0x800000:
                value -= 1 << 24
            values.append(value)
        return values
    for offset in range(0, len(payload), sample_width):
        values.append(
            int.from_bytes(payload[offset : offset + sample_width], "little", signed=True)
        )
    return values


def _inspect_open_wav(
    stream: BinaryIO, *, require_canonical: bool
) -> tuple[AudioSpec, dict[str, float | int]]:
    try:
        with wave.open(stream, "rb") as reader:
            channels = reader.getnchannels()
            sample_rate = reader.getframerate()
            sample_width = reader.getsampwidth()
            frames = reader.getnframes()
            if reader.getcomptype() != "NONE":
                raise ContractError("canonical recordings must contain uncompressed PCM")
            if sample_width not in {2, 3, 4}:
                raise ContractError(f"unsupported PCM sample width: {sample_width}")
            typed_sample_width = cast(Literal[2, 3, 4], sample_width)
            if channels < 1 or sample_rate < 1 or frames < 1:
                raise ContractError("WAV header contains invalid channel, rate, or frame evidence")
            if require_canonical and (
                channels != CANONICAL_CHANNELS
                or sample_rate != CANONICAL_SAMPLE_RATE_HZ
                or sample_width != CANONICAL_SAMPLE_WIDTH_BYTES
            ):
                raise ContractError(
                    "raw recording must be mono 48000 Hz 24-bit little-endian PCM WAV"
                )

            count = 0
            total = 0
            squares = 0
            absolute_peak = 0
            clipped = 0
            current_zero_run = 0
            longest_zero_run = 0
            minimum = -(1 << (sample_width * 8 - 1))
            maximum = (1 << (sample_width * 8 - 1)) - 1

            while payload := reader.readframes(_READ_FRAMES):
                if len(payload) % (channels * sample_width):
                    raise ContractError("WAV data chunk ends inside a PCM frame")
                for value in _sample_values(payload, sample_width):
                    count += 1
                    total += value
                    squares += value * value
                    absolute_peak = max(absolute_peak, abs(value))
                    if value in {minimum, maximum}:
                        clipped += 1
                    if value == 0:
                        current_zero_run += 1
                        longest_zero_run = max(longest_zero_run, current_zero_run)
                    else:
                        current_zero_run = 0

            expected_samples = frames * channels
            if count != expected_samples:
                raise ContractError(
                    f"WAV data length disagrees with header: {count} != {expected_samples} samples"
                )
    except (EOFError, wave.Error) as error:
        raise ContractError(f"invalid PCM WAV: {error}") from error

    full_scale = float(1 << (sample_width * 8 - 1))
    rms = math.sqrt(squares / count) if count else 0.0
    encoding = cast(
        Literal["pcm_s16le", "pcm_s24le", "pcm_s32le"],
        {2: "pcm_s16le", 3: "pcm_s24le", 4: "pcm_s32le"}[typed_sample_width],
    )
    spec = AudioSpec(
        sample_rate_hz=sample_rate,
        channels=channels,
        sample_width_bytes=typed_sample_width,
        encoding=encoding,
        frames=frames,
        duration_seconds=frames / sample_rate,
    )
    metrics: dict[str, float | int] = {
        "peak_dbfs": _dbfs(absolute_peak / full_scale),
        "rms_dbfs": _dbfs(rms / full_scale),
        "dc_offset": max(-1.0, min(1.0, (total / count) / full_scale)),
        "clipped_samples": clipped,
        "longest_zero_run_ms": longest_zero_run / (sample_rate * channels) * 1_000.0,
    }
    return spec, metrics


def inspect_pcm_wav(path: Path, *, require_canonical: bool = False) -> WavInspection:
    """Hash and inspect a WAV through one no-follow descriptor.

    The before/after descriptor metadata check makes a concurrent replacement or
    mutation a hard failure instead of accepting evidence about mixed file states.
    """

    try:
        with open_regular_no_follow(path) as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            stream.seek(0)
            audio, values = _inspect_open_wav(stream, require_canonical=require_canonical)
            after = os.fstat(stream.fileno())
    except SecurityError:
        raise
    except OSError as error:
        raise ContractError(f"cannot inspect WAV {path}: {error}") from error

    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or size != after.st_size:
        raise SecurityError(f"WAV changed while it was being inspected: {path}")
    return WavInspection(
        sha256=digest.hexdigest(),
        size_bytes=size,
        audio=audio,
        peak_dbfs=float(values["peak_dbfs"]),
        rms_dbfs=float(values["rms_dbfs"]),
        dc_offset=float(values["dc_offset"]),
        clipped_samples=int(values["clipped_samples"]),
        longest_zero_run_ms=float(values["longest_zero_run_ms"]),
    )


def inspect_canonical_wav(path: Path) -> WavInspection:
    """Inspect one canonical raw recording and fail on any format deviation."""

    return inspect_pcm_wav(path, require_canonical=True)


def analyze_pcm_wav(
    path: Path,
    *,
    relative_path: str | None = None,
    noise_rms_dbfs: float | None = None,
    require_canonical: bool = True,
    allow_quiet: bool = False,
) -> AudioQcMetrics:
    """Return portable QC metrics without optional numerical dependencies."""

    inspection = inspect_pcm_wav(path, require_canonical=require_canonical)
    return inspection.qc_metrics(
        relative_path or path.name,
        noise_rms_dbfs=noise_rms_dbfs,
        allow_quiet=allow_quiet,
    )

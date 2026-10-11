"""Raw mono 24-bit PCM WAV, and the meter's arithmetic.

Frames arrive as signed 32-bit little-endian integers (a 24-bit converter
fills the top 24 bits); the file keeps those top 24 bits, unchanged: no gain,
no dither, no DSP (the cal file is applied at analysis time, never here). The
header is the canonical 44-byte ``WAVE_FORMAT_PCM`` one, sizes patched on close.
"""

from __future__ import annotations

import math
import os
import struct
from array import array
from dataclasses import dataclass
from pathlib import Path

HEADER_BYTES = 44
BYTES_PER_SAMPLE = 3
FULL_SCALE = 2**31
#: A sample at or beyond this magnitude counts as clipped (the top 24-bit code, either sign).
CLIP_LEVEL = 2**31 - 2**8


def int32_to_s24le(raw: bytes) -> bytes:
    """Drop the low byte of each little-endian int32: the 24-bit sample, little-endian."""
    if len(raw) % 4:
        raise ValueError("a partial 32-bit sample")
    src = memoryview(raw)
    out = bytearray(len(raw) // 4 * 3)
    out[0::3] = src[1::4]
    out[1::3] = src[2::4]
    out[2::3] = src[3::4]
    return bytes(out)


def header(sample_rate: int, channels: int, data_bytes: int) -> bytes:
    block_align = channels * BYTES_PER_SAMPLE
    return b"".join([
        b"RIFF", struct.pack("<I", 36 + data_bytes), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * block_align, block_align, 8 * BYTES_PER_SAMPLE),
        b"data", struct.pack("<I", data_bytes),
    ])


class WavWriter:
    def __init__(self, path: Path, sample_rate: int, channels: int = 1) -> None:
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.channels = channels
        self.frames = 0
        self._file = open(self.path, "wb")  # noqa: SIM115 - closed in close()
        self._file.write(header(sample_rate, channels, 0))

    def write_int32(self, raw: bytes) -> None:
        self._file.write(int32_to_s24le(raw))
        self.frames += len(raw) // 4 // self.channels

    def close(self) -> int:
        """Patch the sizes, flush to disk and return the file size."""
        data_bytes = self.frames * self.channels * BYTES_PER_SAMPLE
        if data_bytes % 2:
            self._file.write(b"\x00")  # RIFF pad byte (not counted in the data chunk)
        self._file.seek(0)
        self._file.write(header(self.sample_rate, self.channels, data_bytes))
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        return self.path.stat().st_size


@dataclass
class BlockStats:
    frames: int = 0
    peak: int = 0
    sum_squares: float = 0.0
    clipped: int = 0
    nonzero: int = 0  # blocks holding a non-zero sample

    def add(self, raw: bytes) -> BlockStats:
        samples = array("i")
        samples.frombytes(raw)
        if not samples:
            return self
        top = max(samples)
        bottom = min(samples)
        self.frames += len(samples)
        self.peak = max(self.peak, top, -bottom)
        # Scale to +-1 before squaring: exact enough and no overflow worries.
        scaled = [s / FULL_SCALE for s in samples]
        self.sum_squares += math.sumprod(scaled, scaled)
        if top >= CLIP_LEVEL or bottom <= -CLIP_LEVEL:
            self.clipped += sum(1 for s in samples if s >= CLIP_LEVEL or s <= -CLIP_LEVEL)
        if top != 0 or bottom != 0:
            self.nonzero += 1  # blocks with any non-zero sample
        return self

    @property
    def peak_dbfs(self) -> float | None:
        return None if self.peak <= 0 else round(20 * math.log10(self.peak / FULL_SCALE), 2)

    @property
    def rms_dbfs(self) -> float | None:
        if self.frames == 0 or self.sum_squares <= 0:
            return None
        return round(10 * math.log10(self.sum_squares / self.frames), 2)

    @property
    def digital_silence(self) -> bool:
        """Every sample exactly zero: a real capsule never does this (macOS sends zeros when mic access is denied)."""
        return self.frames > 0 and self.nonzero == 0


def meter_fraction(dbfs: float | None, floor: float = -72.0) -> float:
    """dBFS → 0..1 for a meter bar (floor .. 0 dBFS)."""
    if dbfs is None:
        return 0.0
    return max(0.0, min(1.0, (dbfs - floor) / -floor))

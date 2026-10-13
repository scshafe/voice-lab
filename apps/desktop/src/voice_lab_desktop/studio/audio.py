"""A take's audio in the studio (RECORDING-V1.md §5, step 6b): samples in memory, waveform bins
on the dB display scale, exact clips, and the live waveform while a take is recorded.

- A take here is read from its ``original.wav``; a take on the PC arrives as bytes
  (``GET /api/v1/corpus/takes/{id}/audio``, behind the PC's playback approval) and is read in
  memory, never written to disk (D-038). Both are canonical mono 48 kHz 24-bit WAV.
- Bins are ``[min0, max0, min1, max1, ...]`` over a view ``[start, end)`` of the take, mapped
  onto the Swift app's fixed **dB display scale** (NATIVE_RECORDING_UX.md §4, amended
  2026-09-14): 0 dBFS draws at the full half-height, −30 dBFS at half of it, and a nonzero
  level at or below −60 dBFS as a hairline; exact silence stays on the baseline. The mapping
  is fixed, symmetric and monotonic, so it never normalizes and stays stable while recording.
- A preview is exactly the frames ``[start, end)`` of the take (D-012), cut from the samples.
- ``LiveWaveform`` follows the capture worker's partial WAV (``original.partial.wav``) while
  it grows: the worker writes the PCM it has checked there, block by block (D-039), and
  exposes no other level stream. The studio reads only what the worker wrote, never the
  device, and draws the last few seconds (a rolling viewport) and the peak level.
"""

from __future__ import annotations

import io
import os
import wave
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt

from ..capture.types import FRAME_BYTES, SAMPLE_RATE
from ..playback import Clip, PlaybackError
from ..prompted.audio import read_frames

type Samples = npt.NDArray[np.float32]

#: The quietest level the display scale draws above its hairline.
FLOOR_DB: Final = -60.0
HAIRLINE: Final = 0.01
#: Bins drawn for a view (about one per pixel of a wide waveform).
VIEW_BINS: Final = 1024
#: The live waveform: one bin per 10 ms, the last 5 seconds.
LIVE_BIN_FRAMES: Final = 480
LIVE_SLOTS: Final = 500
#: The live peak level covers the last 50 ms.
PEAK_FRAMES: Final = 2400
_HEADER_BYTES: Final = 44
_FULL_SCALE: Final = float(2**23)


def display(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Signed sample values in [-1, 1] on the dB display scale (also signed, in [-1, 1])."""
    magnitude = np.abs(values)
    with np.errstate(divide="ignore"):
        decibels = 20.0 * np.log10(np.maximum(magnitude, 1e-12))
    scaled = np.clip((decibels - FLOOR_DB) / -FLOOR_DB, 0.0, 1.0)
    scaled = np.where(magnitude > 0, np.maximum(scaled, HAIRLINE), 0.0)
    return np.sign(values) * scaled


def bins(samples: Samples, start: int, end: int, count: int = VIEW_BINS) -> list[float]:
    """The display bins of frames ``[start, end)`` (at most ``count`` pairs)."""
    frames = int(samples.shape[0])
    start, end = max(0, start), min(frames, end)
    if end <= start:
        return []
    view = samples[start:end]
    pairs = max(1, min(count, end - start))
    edges = np.linspace(0, end - start, pairs + 1).astype(np.int64)[:-1]
    flat = np.empty(pairs * 2, dtype=np.float64)
    flat[0::2] = np.minimum.reduceat(view, edges)
    flat[1::2] = np.maximum.reduceat(view, edges)
    return [round(float(v), 4) for v in display(flat)]


def read_take(path: Path) -> Samples:
    """A take here, whole. Raises: ``PlaybackError``."""
    return read_frames(path)


def read_take_bytes(data: bytes) -> Samples:
    """A take the PC served, whole, from memory. Raises: ``PlaybackError``."""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            if (
                reader.getnchannels() != 1
                or reader.getsampwidth() != FRAME_BYTES
                or reader.getframerate() != SAMPLE_RATE
            ):
                raise PlaybackError("the take is not mono 48 kHz 24-bit")
            raw = reader.readframes(reader.getnframes())
    except (OSError, wave.Error, EOFError) as exc:
        raise PlaybackError(f"the take cannot be read ({type(exc).__name__})") from exc
    return _decode(raw)


def _decode(raw: bytes) -> Samples:
    usable = len(raw) - len(raw) % FRAME_BYTES
    data = np.frombuffer(raw[:usable], dtype=np.uint8).reshape(-1, FRAME_BYTES).astype(np.int32)
    values = data[:, 0] | (data[:, 1] << 8) | (data[:, 2] << 16)
    values = (values << 8) >> 8  # sign-extend the 24-bit samples
    return (values.astype(np.float32) / _FULL_SCALE).astype(np.float32, copy=False)


def clip(samples: Samples, start: int, end: int) -> Clip:
    """Exactly frames ``[start, end)``, ready to play. Raises: ``PlaybackError``."""
    if not 0 <= start < end <= samples.shape[0]:
        raise PlaybackError(f"frames {start} to {end} are outside the take")
    return Clip(np.ascontiguousarray(samples[start:end]).reshape(-1, 1), SAMPLE_RATE)


def peak_dbfs(samples: Samples) -> float:
    """The peak of ``samples`` in dBFS (``FLOOR_DB`` for silence)."""
    if samples.size == 0:
        return FLOOR_DB
    peak = float(np.max(np.abs(samples)))
    return max(FLOOR_DB, 20.0 * float(np.log10(peak))) if peak > 0 else FLOOR_DB


class LiveWaveform:
    """The worker's partial WAV at ``path``, read as it grows (``poll``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = _HEADER_BYTES
        self._carry = b""
        self._pending: list[Samples] = []
        self._pairs: list[tuple[float, float]] = []
        self._recent: Samples = np.zeros(0, dtype=np.float32)
        self.frames = 0
        self.error = ""

    def poll(self) -> bool:
        """Read what the worker wrote since the last poll; ``True`` when there was some."""
        try:
            with self.path.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                if size <= self._offset:
                    return False
                stream.seek(self._offset)
                raw = stream.read(size - self._offset)
        except FileNotFoundError:
            return False
        except OSError as exc:
            self.error = f"the take's audio cannot be read while it is recorded ({exc})"
            return False
        self._offset += len(raw)
        raw = self._carry + raw
        usable = len(raw) - len(raw) % FRAME_BYTES
        self._carry = raw[usable:]
        samples = _decode(raw[:usable])
        if samples.size == 0:
            return False
        self.frames += int(samples.size)
        self._recent = np.concatenate([self._recent, samples])[-PEAK_FRAMES:]
        self._pending.append(samples)
        joined = np.concatenate(self._pending)
        whole = joined.size - joined.size % LIVE_BIN_FRAMES
        if whole:
            blocks = joined[:whole].reshape(-1, LIVE_BIN_FRAMES)
            self._pairs.extend(
                zip(blocks.min(axis=1).tolist(), blocks.max(axis=1).tolist(), strict=True)
            )
            self._pairs = self._pairs[-LIVE_SLOTS:]
        self._pending = [joined[whole:]] if whole < joined.size else []
        return True

    def bins(self) -> list[float]:
        """The last ``LIVE_SLOTS`` bins on the display scale (fewer while the take is young:
        they fill from the left)."""
        if not self._pairs:
            return []
        flat = np.asarray(self._pairs, dtype=np.float64).reshape(-1)
        return [round(float(v), 4) for v in display(flat)]

    @property
    def peak_dbfs(self) -> float:
        return peak_dbfs(self._recent)

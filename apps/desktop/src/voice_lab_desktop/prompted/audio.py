"""A take's PCM on this machine: its waveform and its exact frames (phase 4c).

Takes are canonical mono 48 kHz signed 24-bit WAV (the capture worker's, validated). The
waveform is ``bins`` (min, max) pairs over the whole original, in [-1, 1], computed with
NumPy off the GUI thread (``desktop_waveform.py``'s WaveformView shows the same thing); a
preview is exactly the trimmed frames ``[start, end)`` as a :class:`~voice_lab_desktop.
playback.Clip` the one player plays (D-012: preview plays exactly the selected frames).
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt

from ..capture.types import FRAME_BYTES, SAMPLE_RATE
from ..playback import Clip, PlaybackError

#: About one bin per pixel of a wide waveform (the WaveformView's 2,048).
BINS: Final = 2048
_FULL_SCALE: Final = float(2**23)


def read_frames(path: Path, start: int = 0, end: int | None = None) -> npt.NDArray[np.float32]:
    """Frames ``[start, end)`` of a canonical take, as float32 in [-1, 1).

    Raises: ``PlaybackError`` for a WAV that is not mono 48 kHz 24-bit or a bad range."""
    try:
        with wave.open(str(path), "rb") as reader:
            if (
                reader.getnchannels() != 1
                or reader.getsampwidth() != FRAME_BYTES
                or reader.getframerate() != SAMPLE_RATE
            ):
                raise PlaybackError("the take is not mono 48 kHz 24-bit")
            total = reader.getnframes()
            stop = total if end is None else end
            if not 0 <= start < stop <= total:
                raise PlaybackError(f"frames {start} to {stop} are outside the take ({total})")
            reader.setpos(start)
            raw = reader.readframes(stop - start)
    except (OSError, wave.Error, EOFError) as exc:
        raise PlaybackError(f"the take cannot be read ({type(exc).__name__})") from exc
    data = np.frombuffer(raw, dtype=np.uint8).reshape(-1, FRAME_BYTES).astype(np.int32)
    values = data[:, 0] | (data[:, 1] << 8) | (data[:, 2] << 16)
    values = (values << 8) >> 8  # sign-extend the 24-bit samples
    return (values.astype(np.float32) / _FULL_SCALE).astype(np.float32, copy=False)


def waveform(path: Path, bins: int = BINS) -> tuple[list[float], int]:
    """The take's waveform as ``[min0, max0, min1, max1, ...]`` (at most ``bins`` pairs)
    and its frame count."""
    samples = read_frames(path)
    frames = int(samples.shape[0])
    count = max(1, min(bins, frames))
    edges = np.linspace(0, frames, count + 1).astype(np.int64)[:-1]
    lows = np.minimum.reduceat(samples, edges)
    highs = np.maximum.reduceat(samples, edges)
    flat = np.empty(count * 2, dtype=np.float64)
    flat[0::2] = lows
    flat[1::2] = highs
    return [round(float(v), 5) for v in flat], frames


def clip(path: Path, start: int, end: int) -> Clip:
    """Exactly frames ``[start, end)`` of the take, ready to play."""
    return Clip(read_frames(path, start, end).reshape(-1, 1), SAMPLE_RATE)

"""Bounded PCM previews and a seekable macOS audio clock, without optional audio packages.

@canonical desktop_audio -- immutable preview PCM, complete waveform envelopes and
AVAudioPlayer transport. Files are read on the caller's worker; no audio device is
opened until NativeAudioPlayer.play(). There are no Qt imports or implicit writes.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import math
import os
import re
import sys
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .audio import _sample_values
from .canonical import open_regular_no_follow
from .errors import ContractError, ExternalToolError, SecurityError
from .paths import reject_symlink_components

MAX_PREVIEW_BYTES = 128 * 1024 * 1024
MAX_WAVEFORM_BINS = 16_384
DEFAULT_WAVEFORM_BINS = 2048
WAVEFORM_DECODE_FRAMES = 65_536


def _pcm_fields(payload: bytes) -> tuple[int, int, int, int, bytes]:
    if not isinstance(payload, bytes) or not 44 <= len(payload) <= MAX_PREVIEW_BYTES:
        raise ContractError("audio preview must be a nonempty, bounded PCM WAV")
    if payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise ContractError("audio preview must use a RIFF WAVE container")
    if int.from_bytes(payload[4:8], "little") + 8 != len(payload):
        raise ContractError("audio preview RIFF byte count does not match its contents")
    try:
        with wave.open(io.BytesIO(payload), "rb") as reader:
            rate, frames = reader.getframerate(), reader.getnframes()
            width, channels = reader.getsampwidth(), reader.getnchannels()
            if reader.getcomptype() != "NONE" or width not in {2, 3} or channels != 1:
                raise ContractError("audio preview supports only mono 16-bit or 24-bit PCM WAV")
            if not 8000 <= rate <= 384_000 or frames <= 0:
                raise ContractError("audio preview has an invalid sample rate or empty frame count")
            if frames * width > MAX_PREVIEW_BYTES:
                raise ContractError("audio preview exceeds the bounded PCM frame budget")
            pcm = reader.readframes(frames)
            if len(pcm) != frames * width or reader.readframes(1):
                raise ContractError("audio preview PCM byte count does not match complete frames")
    except (wave.Error, EOFError, OSError) as error:
        raise ContractError(f"cannot decode audio preview WAV: {error}") from error
    return rate, frames, width, channels, pcm


def _waveform_bins(
    pcm: bytes, frame_count: int, sample_width: int, bin_count: int
) -> tuple[tuple[float, float], ...]:
    count = min(bin_count, frame_count)
    scale = float(1 << (sample_width * 8 - 1))
    bins: list[tuple[float, float]] = []
    for index in range(count):
        start = index * frame_count // count
        end = (index + 1) * frame_count // count
        minimum, maximum = scale, -scale
        # Bound the integer working set even if the caller requests one wide bin.
        for offset in range(start, end, WAVEFORM_DECODE_FRAMES):
            until = min(offset + WAVEFORM_DECODE_FRAMES, end)
            values = _sample_values(pcm[offset * sample_width : until * sample_width], sample_width)
            minimum = min(minimum, min(values))
            maximum = max(maximum, max(values))
        bins.append((minimum / scale, maximum / scale))
    return tuple(bins)


@dataclass(frozen=True, slots=True)
class PcmAudioClip:
    """A complete immutable mono preview and a full-scale min/max envelope.

    Frame coordinates always refer to the original PCM; an end frame is exclusive.
    source_sha256 binds the original WAV bytes, including their container metadata.
    """

    sample_rate: int
    frame_count: int
    sample_width: int
    channels: int
    pcm: bytes
    waveform_bins: tuple[tuple[float, float], ...]
    source_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.sample_rate) is not int
            or not 8000 <= self.sample_rate <= 384_000
            or type(self.frame_count) is not int
            or self.frame_count <= 0
            or type(self.sample_width) is not int
            or self.sample_width not in {2, 3}
            or type(self.channels) is not int
            or self.channels != 1
            or not isinstance(self.pcm, bytes)
            or len(self.pcm) != self.frame_count * self.sample_width
            or len(self.pcm) > MAX_PREVIEW_BYTES
        ):
            raise ContractError("invalid or unbounded immutable PCM preview")
        if (
            not isinstance(self.waveform_bins, tuple)
            or not 1 <= len(self.waveform_bins) <= min(MAX_WAVEFORM_BINS, self.frame_count)
            or any(
                not isinstance(pair, tuple)
                or len(pair) != 2
                or any(
                    not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in pair
                )
                or not -1.0 <= pair[0] <= pair[1] <= 1.0
                for pair in self.waveform_bins
            )
            or not isinstance(self.source_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256)
        ):
            raise ContractError("invalid waveform envelope or source digest")

    @property
    def duration_seconds(self) -> float:
        """Exact PCM duration, computed from frame count and sample rate."""
        return self.frame_count / self.sample_rate

    def wav_bytes(self, start_frame: int = 0, end_frame: int | None = None) -> bytes:
        """Return a lossless in-memory WAV of [start_frame, end_frame); never edit the source."""
        end = self.frame_count if end_frame is None else end_frame
        if (
            type(start_frame) is not int
            or type(end) is not int
            or not 0 <= start_frame < end <= self.frame_count
        ):
            raise ContractError("preview range must contain complete frames within the source")
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(self.channels)
            writer.setsampwidth(self.sample_width)
            writer.setframerate(self.sample_rate)
            writer.writeframes(self.pcm[start_frame * self.sample_width : end * self.sample_width])
        return output.getvalue()


def load_pcm_audio(
    path: Path, *, expected_sha256: str | None = None, bin_count: int = DEFAULT_WAVEFORM_BINS
) -> PcmAudioClip:
    """Read one stable, bounded, no-symlink WAV and scan all frames for its waveform.

    Call from the serialized worker after library validation. Optional expected_sha256
    binds the exact original file. Raises ContractError for unsupported audio and
    SecurityError for path, identity or digest failures; no partial result is returned.
    """
    if type(bin_count) is not int or not 1 <= bin_count <= MAX_WAVEFORM_BINS:
        raise ContractError("waveform bin count must be an integer from 1 to 16384")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise ContractError("expected preview SHA-256 must be 64 lowercase hexadecimal characters")
    absolute = Path(os.path.abspath(path))
    reject_symlink_components(Path(absolute.anchor), absolute)
    try:
        with open_regular_no_follow(absolute) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_PREVIEW_BYTES:
                raise ContractError("audio preview exceeds the 128 MiB file limit")
            payload = stream.read(MAX_PREVIEW_BYTES + 1)
            after = os.fstat(stream.fileno())
            reject_symlink_components(Path(absolute.anchor), absolute)
            path_after = absolute.lstat()
    except OSError as error:
        raise SecurityError(f"cannot read stable audio preview {absolute}: {error}") from error
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        for item in (before, after, path_after)
    }
    if len(identities) != 1 or len(payload) != after.st_size:
        raise SecurityError("audio preview changed while its bytes were read")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise SecurityError("audio preview SHA-256 does not match the selected recording")
    rate, frames, width, channels, pcm = _pcm_fields(payload)
    return PcmAudioClip(
        rate, frames, width, channels, pcm, _waveform_bins(pcm, frames, width, bin_count), digest
    )


class AudioPlayerBridge(Protocol):
    """Injectable native transport boundary; tests supply a non-audible implementation."""

    def load(self, wav_bytes: bytes) -> None: ...
    def play(self) -> bool: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def seek(self, seconds: float) -> None: ...
    def close(self) -> None: ...
    @property
    def duration_seconds(self) -> float: ...
    @property
    def position_seconds(self) -> float: ...
    @property
    def is_playing(self) -> bool: ...


class _AVAudioPlayerBridge:
    """Narrow typed objc_msgSend signatures for macOS AVAudioPlayer.

    currentTime is the native media clock. No synthetic completion is inferred from
    wall time: isPlaying=False means the native player stopped, including interruption.
    Related: developer.apple.com/documentation/avfaudio/avaudioplayer/currenttime
    """

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise ExternalToolError("native desktop audio playback requires macOS")
        try:
            self._foundation = ctypes.CDLL(
                "/System/Library/Frameworks/Foundation.framework/Foundation"
            )
            self._audio = ctypes.CDLL("/System/Library/Frameworks/AVFAudio.framework/AVFAudio")
            self._objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        except OSError as error:
            raise ExternalToolError(
                f"cannot load native macOS audio frameworks: {error}"
            ) from error
        self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self._objc.objc_getClass.restype = ctypes.c_void_p
        self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self._objc.sel_registerName.restype = ctypes.c_void_p
        self._get_class = cast(Callable[[bytes], int | None], self._objc.objc_getClass)
        self._selector = cast(Callable[[bytes], int], self._objc.sel_registerName)
        pointer = ctypes.c_void_p
        self._object = cast(
            Callable[[int, int], int | None],
            ctypes.CFUNCTYPE(pointer, pointer, pointer)(("objc_msgSend", self._objc)),
        )
        self._void = cast(
            Callable[[int, int], None],
            ctypes.CFUNCTYPE(None, pointer, pointer)(("objc_msgSend", self._objc)),
        )
        self._bool = cast(
            Callable[[int, int], bool],
            ctypes.CFUNCTYPE(ctypes.c_bool, pointer, pointer)(("objc_msgSend", self._objc)),
        )
        self._double = cast(
            Callable[[int, int], float],
            ctypes.CFUNCTYPE(ctypes.c_double, pointer, pointer)(("objc_msgSend", self._objc)),
        )
        self._set_double = cast(
            Callable[[int, int, float], None],
            ctypes.CFUNCTYPE(None, pointer, pointer, ctypes.c_double)(("objc_msgSend", self._objc)),
        )
        self._data_init = cast(
            Callable[[int, int, bytes, int], int | None],
            ctypes.CFUNCTYPE(pointer, pointer, pointer, ctypes.c_char_p, ctypes.c_size_t)(
                ("objc_msgSend", self._objc)
            ),
        )
        self._player_init = cast(
            Callable[[int, int, int, object], int | None],
            ctypes.CFUNCTYPE(pointer, pointer, pointer, pointer, ctypes.POINTER(pointer))(
                ("objc_msgSend", self._objc)
            ),
        )
        self._string = cast(
            Callable[[int, int], bytes | None],
            ctypes.CFUNCTYPE(ctypes.c_char_p, pointer, pointer)(("objc_msgSend", self._objc)),
        )
        self._player = 0
        self._data = 0

    def _allocate(self, class_name: bytes) -> int:
        cls = self._get_class(class_name)
        instance = self._object(cls, self._selector(b"alloc")) if cls else None
        if not instance:
            raise ExternalToolError(f"native audio class unavailable: {class_name.decode('ascii')}")
        return instance

    def load(self, wav_bytes: bytes) -> None:
        self.close()
        pool_alloc = self._allocate(b"NSAutoreleasePool")
        pool = self._object(pool_alloc, self._selector(b"init"))
        if not pool:
            raise ExternalToolError("cannot initialize native audio autorelease pool")
        try:
            data = self._data_init(
                self._allocate(b"NSData"),
                self._selector(b"initWithBytes:length:"),
                wav_bytes,
                len(wav_bytes),
            )
            if not data:
                raise ExternalToolError("cannot allocate native audio preview data")
            self._data = data
            error = ctypes.c_void_p()
            player = self._player_init(
                self._allocate(b"AVAudioPlayer"),
                self._selector(b"initWithData:error:"),
                data,
                ctypes.byref(error),
            )
            if not player:
                detail = "AVAudioPlayer rejected the PCM preview"
                if error.value:
                    description = self._object(error.value, self._selector(b"localizedDescription"))
                    message = (
                        self._string(description, self._selector(b"UTF8String"))
                        if description
                        else None
                    )
                    if message:
                        detail = message.decode("utf-8", errors="replace")
                raise ExternalToolError(f"cannot load native audio preview: {detail}")
            self._player = player
        except BaseException:
            self.close()
            raise
        finally:
            self._void(pool, self._selector(b"drain"))

    def play(self) -> bool:
        return self._bool(self._player, self._selector(b"play"))

    def pause(self) -> None:
        self._void(self._player, self._selector(b"pause"))

    def stop(self) -> None:
        self._void(self._player, self._selector(b"stop"))

    def seek(self, seconds: float) -> None:
        self._set_double(self._player, self._selector(b"setCurrentTime:"), seconds)

    @property
    def duration_seconds(self) -> float:
        return self._double(self._player, self._selector(b"duration"))

    @property
    def position_seconds(self) -> float:
        return self._double(self._player, self._selector(b"currentTime"))

    @property
    def is_playing(self) -> bool:
        return self._bool(self._player, self._selector(b"isPlaying"))

    def close(self) -> None:
        if self._player:
            self.stop()
            self._void(self._player, self._selector(b"release"))
            self._player = 0
        if self._data:
            self._void(self._data, self._selector(b"release"))
            self._data = 0


class NativeAudioPlayer:
    """Seekable, pausable native playback of validated in-memory PCM WAVs.

    Constructing this adapter is inert. load() allocates a native player lazily;
    only play() requests output hardware. Position and playing state come directly
    from AVAudioPlayer. False is_playing is an observation, not a success claim.
    """

    def __init__(self, *, bridge: AudioPlayerBridge | None = None) -> None:
        self._bridge = bridge
        self._loaded = False
        self._closed = False

    def _require_loaded(self) -> AudioPlayerBridge:
        if self._closed or not self._loaded or self._bridge is None:
            raise ContractError("load an audio preview before controlling playback")
        return self._bridge

    def load(self, wav_bytes: bytes) -> None:
        """Validate and load a bounded mono PCM preview without starting playback."""
        if self._closed:
            raise ContractError("native audio player is closed")
        self.stop()
        self._loaded = False
        rate, frames, _width, _channels, _pcm = _pcm_fields(wav_bytes)
        if self._bridge is None:
            self._bridge = _AVAudioPlayerBridge()
        self._bridge.load(wav_bytes)
        duration = self._bridge.duration_seconds
        if not math.isfinite(duration) or abs(duration - frames / rate) > 1.0 / rate:
            self._bridge.close()
            raise ExternalToolError("native audio duration does not match the exact preview frames")
        self._loaded = True

    def play(self) -> None:
        """Begin or resume playback, failing visibly if native hardware rejects it."""
        if not self._require_loaded().play():
            raise ExternalToolError("native audio player could not start playback")

    def pause(self) -> None:
        """Pause at the native audio clock position, retaining a resumable preview."""
        self._require_loaded().pause()

    def stop(self) -> None:
        """Stop audio hardware and reset the preview position; safe before loading."""
        if self._loaded:
            bridge = self._require_loaded()
            bridge.stop()
            bridge.seek(0.0)

    def seek(self, seconds: float) -> None:
        """Seek within the loaded preview using AVAudioPlayer's real media timeline."""
        bridge = self._require_loaded()
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0.0 <= seconds <= self.duration_seconds
        ):
            raise ContractError("audio seek must be a finite time inside the loaded preview")
        bridge.seek(float(seconds))

    @property
    def duration_seconds(self) -> float:
        """Native duration, or zero when no preview has been loaded."""
        return self._require_loaded().duration_seconds if self._loaded else 0.0

    @property
    def position_seconds(self) -> float:
        """Read the real native media position; never estimate it from wall time."""
        position = self._require_loaded().position_seconds if self._loaded else 0.0
        if not math.isfinite(position) or position < 0.0:
            raise ExternalToolError("native audio player reported an invalid position")
        return position

    @property
    def is_playing(self) -> bool:
        """Report native playback state, including natural stop or interruption."""
        return self._require_loaded().is_playing if self._loaded else False

    def close(self) -> None:
        """Stop and release native resources; safe to call repeatedly."""
        if not self._closed and self._bridge is not None:
            self._bridge.close()
        self._loaded = False
        self._closed = True

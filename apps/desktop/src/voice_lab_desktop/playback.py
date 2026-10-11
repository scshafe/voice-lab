"""Playback of the server's recordings: one player for the whole app (D-038).

The server streams a recording's bytes (``GET /api/v1/recordings/{id}/audio``, only when
the operator recorded a playback approval on the server). The app keeps them in memory,
never on disk (as D-022 has it for synthesized clips), decodes them with PyAV (AAC memos,
WAV measurement sessions), shapes them with NumPy and plays them through PortAudio
(``sounddevice``), the library the measurement capture already uses. QtMultimedia is not
in PySide6-Essentials.

Shaping is what the SPA did with WebAudio for the blind referee: a per-side gain from the
server (``gain_db``, loudness matching) and a low-pass at 8 kHz, so that a 2019 phone take
and a 2026 take stop telegraphing their year through the recording chain. The low-pass
here removes everything above the cut-off (an FFT brick wall), a stricter filter than the
browser's biquad; the served audio is untouched.

There is one player: starting a take stops the one playing (the SPA's single ``<audio>``
element). :class:`Player` is the Qt face of it; the device behind it is an
:class:`AudioOutput`: :class:`PortAudioOutput` in the app, a recording stand-in in tests,
which never open an audio device.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import numpy as np
import numpy.typing as npt
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from .tasks import Tasks

type Samples = npt.NDArray[np.float32]

#: The referee's band limit (the SPA's WebAudio low-pass).
REFEREE_LOWPASS_HZ: Final = 8000.0
#: The longest take the player decodes (a measurement session's free practice is long).
MAX_SECONDS: Final = 60 * 60
POSITION_MS: Final = 100


class PlaybackError(Exception):
    """The audio could not be decoded or played; the message is safe to show."""


@dataclass(frozen=True)
class Clip:
    """Decoded audio: ``samples`` is (frames, channels) float32 in [-1, 1]."""

    samples: Samples
    rate: int

    @property
    def seconds(self) -> float:
        return float(self.samples.shape[0]) / self.rate if self.rate else 0.0


def decode(data: bytes) -> Clip:
    """Decode a whole recording (any container PyAV reads) into float32 frames."""
    import av

    if not data:
        raise PlaybackError("the recording is empty")
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.audio:
                raise PlaybackError("the recording has no audio stream")
            stream = container.streams.audio[0]
            rate = int(stream.rate or 0)
            channels = 1 if (stream.channels or 1) < 2 else 2
            if rate <= 0:
                raise PlaybackError("the recording has no sample rate")
            resampler = av.AudioResampler(
                format="flt", layout="mono" if channels == 1 else "stereo", rate=rate
            )
            chunks: list[Samples] = []
            total = 0
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    array = out.to_ndarray().reshape(-1, channels).astype(np.float32, copy=False)
                    chunks.append(array)
                    total += array.shape[0]
                    if total > MAX_SECONDS * rate:
                        raise PlaybackError("the recording is too long to play here")
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1, channels).astype(np.float32))
    except PlaybackError:
        raise
    except Exception as exc:  # noqa: BLE001 - PyAV raises its own error types
        raise PlaybackError(f"the recording could not be decoded ({type(exc).__name__})") from exc
    if not chunks:
        raise PlaybackError("the recording holds no audio")
    return Clip(np.concatenate(chunks, axis=0), rate)


def shape(clip: Clip, *, gain_db: float = 0.0, lowpass_hz: float | None = None) -> Clip:
    """``clip`` with ``gain_db`` applied and, when ``lowpass_hz`` is set, nothing above it.

    The result is clipped to [-1, 1] (a gain above 0 dB cannot overdrive the device)."""
    samples = clip.samples
    if lowpass_hz is not None and 0 < lowpass_hz < clip.rate / 2:
        spectrum = np.fft.rfft(samples, axis=0)
        freqs = np.fft.rfftfreq(samples.shape[0], d=1.0 / clip.rate)
        spectrum[freqs > lowpass_hz, :] = 0
        samples = np.fft.irfft(spectrum, n=samples.shape[0], axis=0).astype(np.float32)
    if gain_db:
        samples = samples * np.float32(10 ** (gain_db / 20))
    return Clip(np.clip(samples, -1.0, 1.0).astype(np.float32, copy=False), clip.rate)


class AudioOutput(Protocol):
    """Where decoded audio goes. ``start`` plays ``clip`` from ``frame``; ``on_done`` is
    called (on any thread) when it played to its end."""

    def start(self, clip: Clip, frame: int, on_done: Callable[[], None]) -> None: ...
    def stop(self) -> None: ...
    @property
    def frame(self) -> int: ...


class PortAudioOutput:
    """The device: a PortAudio output stream fed from the clip in memory."""

    def __init__(self) -> None:
        self._stream: Any = None
        self._clip: Clip | None = None
        self._frame = 0
        self._lock = threading.Lock()

    @property
    def frame(self) -> int:
        return self._frame

    def start(self, clip: Clip, frame: int, on_done: Callable[[], None]) -> None:
        import sounddevice

        self.stop()
        self._clip, self._frame = clip, max(0, min(frame, clip.samples.shape[0]))
        source = clip.samples

        def callback(outdata: Any, frames: int, _time: Any, _status: Any) -> None:
            with self._lock:
                start = self._frame
                chunk = source[start : start + frames]
                outdata[: len(chunk)] = chunk
                outdata[len(chunk) :] = 0
                self._frame = start + len(chunk)
                if len(chunk) < frames:
                    raise sounddevice.CallbackStop

        try:
            self._stream = sounddevice.OutputStream(
                samplerate=clip.rate,
                channels=source.shape[1],
                dtype="float32",
                callback=callback,
                finished_callback=on_done,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudio's errors vary by platform
            self._stream = None
            raise PlaybackError(f"the audio device could not play ({exc})") from exc

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.abort()
            stream.close()


class Player(QObject):
    """The app's one player.

    ``key`` names who plays: ``"corpus:<id>"``, ``"inspector:<id>"``, ``"era:<n>"``,
    ``"referee:a"``. A new ``play`` stops the current one. States: ``idle``, ``loading``,
    ``playing``, ``paused``, ``failed`` (``error`` says why)."""

    changed = Signal()
    positionChanged = Signal()
    failed = Signal(str, str)  # key, message
    finished = Signal(str)  # key

    def __init__(
        self,
        tasks: Tasks,
        fetch: Callable[[str], bytes],
        output: AudioOutput | None = None,
    ) -> None:
        super().__init__()
        self._tasks = tasks
        self._fetch = fetch
        self._output: AudioOutput = output if output is not None else PortAudioOutput()
        self._key = ""
        self._state = "idle"
        self._error = ""
        self._clip: Clip | None = None
        self._held = 0  # the frame a paused or stopped take resumes from
        self._queue: list[tuple[str, str]] = []
        self._generation = 0
        self._started = 0  # counts output starts: a stopped stream's late "done" is ignored
        self._timer = QTimer(self)
        self._timer.setInterval(POSITION_MS)
        self._timer.timeout.connect(self.positionChanged)

    # -- properties -------------------------------------------------------------------------

    def _get_key(self) -> str:
        return self._key

    def _get_state(self) -> str:
        return self._state

    def _get_error(self) -> str:
        return self._error

    def _get_duration(self) -> float:
        return self._clip.seconds if self._clip else 0.0

    def _get_position(self) -> float:
        if self._clip is None:
            return 0.0
        frame = self._output.frame if self._state == "playing" else self._held
        return float(frame) / self._clip.rate

    activeKey = Property(str, _get_key, notify=changed)
    state = Property(str, _get_state, notify=changed)
    error = Property(str, _get_error, notify=changed)
    duration = Property(float, _get_duration, notify=changed)
    position = Property(float, _get_position, notify=positionChanged)

    def _set(
        self, *, key: str | None = None, state: str | None = None, error: str | None = None
    ) -> None:
        if key is not None:
            self._key = key
        if state is not None:
            self._state = state
            if state == "playing":
                self._timer.start()
            else:
                self._timer.stop()
        if error is not None:
            self._error = error
        self.changed.emit()
        self.positionChanged.emit()

    # -- actions ----------------------------------------------------------------------------

    def play_source(
        self, key: str, source: str, *, gain_db: float = 0.0, lowpass_hz: float | None = None
    ) -> None:
        """Fetch ``source`` (a recording id, or an ``/api/v1/...`` audio path), decode,
        shape and play it, as ``key``."""

        def work() -> Clip:
            return shape(decode(self._fetch(source)), gain_db=gain_db, lowpass_hz=lowpass_hz)

        self.play_clip(key, work)

    def play_clip(self, key: str, load: Callable[[], Clip]) -> None:
        """Play the audio ``load`` returns (run off the GUI thread), as ``key``: a take on
        this machine, exactly the frames it was asked for (Record: prompts' preview)."""
        self._stop_output()
        self._generation += 1
        generation = self._generation
        self._clip = None
        self._set(key=key, state="loading", error="")

        def work() -> Clip:
            return load()

        def done(clip: Clip | None, error: BaseException | None) -> None:
            if generation != self._generation:
                return  # another take started meanwhile
            if error is not None or clip is None:
                message = str(error) if error is not None else "no audio"
                self._queue.clear()
                self._set(state="failed", error=message)
                self.failed.emit(key, message)
                return
            self._clip = clip
            self._start_at(0)

        self._tasks.submit(work, done)

    @Slot(str, str)
    def play(self, key: str, recording_id: str) -> None:
        """Play a recording (``key`` names the caller)."""
        self._queue.clear()
        self.play_source(key, recording_id)

    @Slot(str, str, float, float)
    def playShaped(self, key: str, source: str, gain_db: float, lowpass_hz: float) -> None:  # noqa: N802
        """Play ``source`` with a gain and, when ``lowpass_hz`` > 0, a low-pass (the referee)."""
        self._queue.clear()
        self.play_source(key, source, gain_db=gain_db, lowpass_hz=lowpass_hz or None)

    def play_queue(self, items: Sequence[tuple[str, str]]) -> None:
        """Play ``(key, recording id)`` items one after another (an era's sample takes)."""
        self._queue = list(items)
        if self._queue:
            key, source = self._queue.pop(0)
            self.play_source(key, source)

    def _start_at(self, frame: int) -> None:
        assert self._clip is not None
        self._held = frame
        self._started += 1
        started = self._started

        def on_done() -> None:
            self._tasks.on_gui_thread(lambda: self._ended(started))

        try:
            self._output.start(self._clip, frame, on_done)
        except PlaybackError as exc:
            self._set(state="failed", error=str(exc))
            self.failed.emit(self._key, str(exc))
            return
        self._set(state="playing")

    def _ended(self, started: int) -> None:
        if started != self._started or self._state != "playing":
            return
        key = self._key
        self._set(state="idle")
        self.finished.emit(key)
        if self._queue:
            next_key, source = self._queue.pop(0)
            self.play_source(next_key, source)

    def _stop_output(self) -> None:
        self._started += 1
        self._output.stop()

    @Slot()
    def pause(self) -> None:
        if self._state == "playing":
            self._held = self._output.frame
            self._stop_output()
            self._set(state="paused")

    @Slot()
    def resume(self) -> None:
        if self._state == "paused" and self._clip is not None:
            self._start_at(self._held)

    @Slot(str)
    def toggle(self, key: str) -> None:
        """Pause or resume ``key`` when it is the active take."""
        if key != self._key:
            return
        if self._state == "playing":
            self.pause()
        elif self._state == "paused":
            self.resume()

    @Slot(float)
    def seek(self, seconds: float) -> None:
        if self._clip is None:
            return
        frame = int(max(0.0, min(seconds, self._clip.seconds)) * self._clip.rate)
        if self._state == "playing":
            self._stop_output()
            self._start_at(frame)
        else:
            self._held = frame
            self.positionChanged.emit()

    @Slot()
    def stop(self) -> None:
        """Stop and forget the take (its samples are dropped)."""
        self._generation += 1
        self._queue.clear()
        self._stop_output()
        self._clip = None
        self._set(key="", state="idle", error="")

    def shutdown(self) -> None:
        self.stop()

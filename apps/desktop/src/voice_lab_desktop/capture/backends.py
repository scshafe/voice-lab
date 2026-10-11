"""Timed input streams for the capture worker: PortAudio, and a simulated UMIK-1.

A timed stream delivers mono 48 kHz **packed 24-bit** little-endian PCM (PortAudio's
``int24``: exactly the canonical WAV's sample bytes, nothing to convert) in blocks of a
fixed size, each with its ``BlockTiming`` (the callback's ADC and current time, and its
overflow/underflow flags). Device listing and the UMIK-1 rule are the measurement
capture's (``measurement.devices``): anything that is not a UMIK-1 is refused.

The simulated backend never touches audio hardware. It is for tests and for the S-1
harness's ``--simulated`` dry run only; evidence it produces says ``simulated: true``.
"""

from __future__ import annotations

import math
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Protocol

from ..measurement.devices import DeviceError, InputDevice, PortAudioBackend
from ..measurement.simulated import BUILT_IN, UMIK
from .timing import BlockTiming
from .types import FRAME_BYTES, SAMPLE_RATE

#: ``callback(pcm_s24le_bytes, timing)``, on the audio thread: copy and enqueue only.
TimedCallback = Callable[[bytes, BlockTiming], None]


class TimedStream(Protocol):
    @property
    def active(self) -> bool:
        """False once the stream stopped by itself (a removed device, an aborted stream)."""
        ...

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class TimedBackend(Protocol):
    #: True for the simulated backend: its takes are never hardware evidence.
    simulated: bool

    def list_inputs(self) -> list[InputDevice]: ...

    def open_timed_input(
        self, device: InputDevice, *, block_frames: int, callback: TimedCallback
    ) -> TimedStream: ...


class PortAudioTimedBackend:
    """``sounddevice.RawInputStream`` with ``dtype="int24"`` and a fixed block size."""

    simulated = False

    def __init__(self, base: PortAudioBackend | None = None) -> None:
        self.base = base if base is not None else PortAudioBackend()

    def list_inputs(self) -> list[InputDevice]:
        return self.base.list_inputs()

    def open_timed_input(
        self, device: InputDevice, *, block_frames: int, callback: TimedCallback
    ) -> TimedStream:
        sd = self.base.sd
        extra = None
        if sys.platform == "darwin":
            # 48 kHz on the device itself, and an error rather than a sample-rate converter.
            extra = sd.CoreAudioSettings(
                change_device_parameters=True, fail_if_conversion_required=True
            )

        def on_block(indata: Any, frames: int, stamp: Any, status: Any) -> None:
            callback(
                bytes(indata),
                BlockTiming(
                    frames=int(frames),
                    adc_time=float(stamp.inputBufferAdcTime),
                    current_time=float(stamp.currentTime),
                    input_overflow=bool(status.input_overflow),
                    input_underflow=bool(status.input_underflow),
                ),
            )

        try:
            stream: TimedStream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                device=device.index,
                channels=1,
                dtype="int24",
                blocksize=block_frames,
                latency="high",
                callback=on_block,
                extra_settings=extra,
            )
            return stream
        except sd.PortAudioError as exc:
            hint = ""
            if device.host_api.startswith("JACK"):
                hint = (
                    " (PipeWire's graph must run at 48 kHz: "
                    "pw-metadata -n settings 0 clock.force-rate 48000)"
                )
            raise DeviceError(
                f"cannot open {device.describe()} at {SAMPLE_RATE} Hz, 24-bit mono: {exc}{hint}"
            ) from exc


UMIK_2 = InputDevice(
    index=4, name="UMIK-2", host_api="Core Audio", channels=1, default_rate=48000.0
)
_DEVICES = {"umik-1": UMIK, "umik-2": UMIK_2, "built-in": BUILT_IN}
#: One period of a 240 Hz tone at 48 kHz (200 frames), -24 dBFS, packed s24le.
_PERIOD_FRAMES = 200


def _tone_period() -> bytes:
    amplitude = 10 ** (-24 / 20) * (2**23 - 1)
    return b"".join(
        int(amplitude * math.sin(2 * math.pi * index / _PERIOD_FRAMES)).to_bytes(
            3, "little", signed=True
        )
        for index in range(_PERIOD_FRAMES)
    )


@dataclass(frozen=True)
class SimulatedSpec:
    """A simulated UMIK-1 and the faults it injects, from JSON (``--simulated``).

    Block numbers count from 0. ``speed`` is a real-time multiple (0: as fast as the
    worker takes them). Jitter moves each block's ADC time by ±``jitter_frames``,
    alternating, so it never accumulates."""

    speed: float = 50.0
    gap_at_block: int | None = None
    gap_frames: int = 4800
    overflow_at_block: int | None = None
    underflow_at_block: int | None = None
    zero_adc_at_block: int | None = None
    jitter_frames: float = 0.0
    silent: bool = False
    #: The stream stops by itself after this many blocks (an unplugged device).
    stop_after_blocks: int | None = None
    #: Deliver nothing at all (a device that never starts).
    stall: bool = False
    #: Seconds the worker's writer waits per block (to fill its queue); simulated only.
    writer_delay: float = 0.0
    devices: tuple[str, ...] = ("built-in", "umik-1")

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> SimulatedSpec:
        known = {item.name for item in fields(cls)}
        unknown = set(value) - known
        if unknown:
            raise ValueError(f"unknown simulated settings: {', '.join(sorted(unknown))}")
        data = dict(value)
        if "devices" in data:
            devices = tuple(data["devices"])
            if any(name not in _DEVICES for name in devices):
                raise ValueError(f"simulated devices are {', '.join(sorted(_DEVICES))}")
            data["devices"] = devices
        spec = cls(**data)
        if spec.speed < 0 or not math.isfinite(spec.speed):
            raise ValueError("speed must be a finite number >= 0")
        return spec

    def to_json(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)} | {
            "devices": list(self.devices)
        }


@dataclass
class SimulatedTimedStream:
    spec: SimulatedSpec
    block_frames: int
    callback: TimedCallback
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _ended: bool = False

    @property
    def active(self) -> bool:
        return not self._ended

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="simulated-timed", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        spec = self.spec
        if spec.stall:
            self._stop.wait()
            return
        block = self.block_frames
        tone = _tone_period() * (block // _PERIOD_FRAMES + 2)
        silence = bytes(block * FRAME_BYTES)
        base = 1000.0
        frame = 0
        index = 0
        offset_frames = 0.0
        while not self._stop.is_set():
            if spec.stop_after_blocks is not None and index >= spec.stop_after_blocks:
                self._ended = True
                return
            if spec.gap_at_block is not None and index == spec.gap_at_block:
                offset_frames += spec.gap_frames
            jitter = spec.jitter_frames * (1 if index % 2 else -1)
            adc = base + (frame + offset_frames + jitter) / SAMPLE_RATE
            if spec.zero_adc_at_block is not None and index == spec.zero_adc_at_block:
                adc = 0.0
            start = (frame % _PERIOD_FRAMES) * FRAME_BYTES
            data = silence if spec.silent else tone[start : start + block * FRAME_BYTES]
            self.callback(
                data,
                BlockTiming(
                    frames=block,
                    adc_time=adc,
                    current_time=adc + 0.01 if adc else 0.0,
                    input_overflow=index == spec.overflow_at_block,
                    input_underflow=index == spec.underflow_at_block,
                ),
            )
            frame += block
            index += 1
            if spec.speed:
                time.sleep(block / SAMPLE_RATE / spec.speed)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def close(self) -> None:
        self.stop()


class SimulatedTimedBackend:
    simulated = True

    def __init__(self, spec: SimulatedSpec | None = None) -> None:
        self.spec = spec or SimulatedSpec()
        self.opened: list[InputDevice] = []

    def list_inputs(self) -> list[InputDevice]:
        return [_DEVICES[name] for name in self.spec.devices]

    def open_timed_input(
        self, device: InputDevice, *, block_frames: int, callback: TimedCallback
    ) -> SimulatedTimedStream:
        self.opened.append(device)
        return SimulatedTimedStream(self.spec, block_frames, callback)

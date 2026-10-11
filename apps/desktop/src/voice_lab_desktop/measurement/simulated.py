"""A simulated UMIK-1 and level, for tests and ``--smoke`` only.

Nothing here touches audio hardware. Sessions recorded with it are synthetic:
the window never offers it, and ``--smoke`` writes only into a temporary dir.
"""

from __future__ import annotations

import math
import random
import struct
import threading
import time
from dataclasses import dataclass, field

from .devices import BlockCallback, DeviceError, InputDevice
from .levels import InputLevel, LevelUnreadable

UMIK = InputDevice(
    index=3, name="Umik-1  Gain: 18dB", host_api="Core Audio", channels=1, default_rate=48000.0
)
BUILT_IN = InputDevice(
    index=1, name="MacBook Air Microphone", host_api="Core Audio", channels=1, default_rate=48000.0
)


def tone_block(
    start_frame: int,
    frames: int,
    rate: int,
    dbfs: float = -24.0,
    hz: float = 220.0,
    noise_dbfs: float = -90.0,
    rng: random.Random | None = None,
) -> bytes:
    """A sine plus a little noise, as 24-bit samples in the top bits of int32 (what a 24-bit
    device delivers)."""
    amp = 10 ** (dbfs / 20) * (2**23 - 1)
    noise = 10 ** (noise_dbfs / 20) * (2**23 - 1)
    rng = rng or random.Random(start_frame)
    out = bytearray(frames * 4)
    for i in range(frames):
        value = int(
            amp * math.sin(2 * math.pi * hz * (start_frame + i) / rate) + rng.gauss(0, noise)
        )
        value = max(-(2**23), min(2**23 - 1, value))
        struct.pack_into("<i", out, i * 4, value << 8)
    return bytes(out)


@dataclass
class SimulatedStream:
    backend: SimulatedBackend
    rate: int
    callback: BlockCallback
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="simulated-audio", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        backend = self.backend
        frame = 0
        block = backend.block_frames
        # 240 Hz has a whole number of periods in a 2400-frame block at 48 kHz: one block, reused.
        tone = tone_block(0, block, self.rate, backend.dbfs, hz=240.0, rng=random.Random(7))
        silence = bytes(block * 4)
        while not self._stop.is_set():
            data = silence if backend.zeros else tone
            overflow = (
                backend.overflow_every > 0
                and (frame // block) % backend.overflow_every == backend.overflow_every - 1
            )
            self.callback(data, block, overflow)
            frame += block
            backend.frames_delivered += block
            time.sleep(block / self.rate / backend.speed)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def close(self) -> None:
        self.stop()


@dataclass
class SimulatedBackend:
    devices: list[InputDevice] = field(default_factory=lambda: [BUILT_IN, UMIK])
    #: Real-time multiple (20 = one second of audio every 50 ms).
    speed: float = 20.0
    block_frames: int = 2400
    dbfs: float = -24.0
    zeros: bool = False
    overflow_every: int = 0
    fail_open: str | None = None
    frames_delivered: int = 0
    opened: list[InputDevice] = field(default_factory=list)

    def list_inputs(self) -> list[InputDevice]:
        return list(self.devices)

    def open_input(
        self, device: InputDevice, sample_rate: int, callback: BlockCallback
    ) -> SimulatedStream:
        if self.fail_open:
            raise DeviceError(self.fail_open)
        if device not in self.devices:
            raise DeviceError(f"{device.name!r} is gone")
        self.opened.append(device)
        return SimulatedStream(self, sample_rate, callback)


@dataclass
class SimulatedLevelReader:
    level: InputLevel = field(
        default_factory=lambda: InputLevel(
            63, (0.63,), False, "coreaudio", "VolumeScalar input element 0"
        )
    )
    unreadable: str | None = None
    reads: int = 0

    def read(self, device: InputDevice) -> InputLevel:
        self.reads += 1
        if self.unreadable:
            raise LevelUnreadable(self.unreadable)
        return self.level

    def move_to(self, volume: int) -> None:
        self.level = InputLevel(
            volume,
            (round(volume / 100, 6),),
            self.level.muted,
            self.level.source,
            self.level.control,
        )

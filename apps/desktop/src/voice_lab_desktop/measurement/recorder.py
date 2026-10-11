"""Recording one segment: the audio thread queues blocks, a writer thread writes and meters them.

A timed segment ends after exactly ``seconds * sample_rate`` frames (the
writer truncates the last block), so durations do not depend on wall-clock
jitter. A manual segment ends on :meth:`SegmentRecording.stop`. Input
overflows (dropped frames) are counted and reported with the segment.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .devices import AudioBackend, DeviceError, InputDevice, InputStream
from .wav import BlockStats, WavWriter


@dataclass(frozen=True)
class SegmentResult:
    frames: int
    size_bytes: int
    peak_dbfs: float | None
    rms_dbfs: float | None
    clipped_samples: int
    input_overflows: int
    digital_silence: bool


class SegmentRecording:
    def __init__(
        self,
        backend: AudioBackend,
        device: InputDevice,
        path: Path,
        sample_rate: int,
        max_frames: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.max_frames = max_frames
        self.stats = BlockStats()
        #: The latest block's stats, for the live meter (replaced, never mutated).
        self.last_block = BlockStats()
        self.overflows = 0
        self.error: BaseException | None = None
        self.finished = threading.Event()
        self._queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._stopping = False
        self._result: SegmentResult | None = None
        self._writer = WavWriter(self.path, sample_rate)
        try:
            self._stream: InputStream = backend.open_input(device, sample_rate, self._on_block)
        except BaseException:
            self._writer.close()
            self.path.unlink(missing_ok=True)
            raise
        self._thread = threading.Thread(target=self._write_loop, name="segment-writer", daemon=True)

    @property
    def frames(self) -> int:
        return self._writer.frames

    @property
    def seconds(self) -> float:
        return self._writer.frames / self.sample_rate

    def start(self) -> None:
        self._thread.start()
        try:
            self._stream.start()
        except Exception as exc:
            self._queue.put(None)
            self._thread.join()
            raise DeviceError(f"the input stream did not start: {exc}") from exc

    # Audio thread: never block, never allocate more than the copy.
    def _on_block(self, data: bytes, frames: int, overflowed: bool) -> None:
        if overflowed:
            self.overflows += 1
        if not self._stopping:
            self._queue.put(data)

    def _write_loop(self) -> None:
        try:
            while True:
                data = self._queue.get()
                if data is None:
                    break
                if self.max_frames is not None:
                    room = self.max_frames - self._writer.frames
                    if room <= 0:
                        continue
                    data = data[: room * 4]
                self._writer.write_int32(data)
                self.stats.add(data)
                self.last_block = BlockStats().add(data)
                if self.max_frames is not None and self._writer.frames >= self.max_frames:
                    self.finished.set()
        except BaseException as exc:  # noqa: BLE001 - reported by stop()
            self.error = exc
            self.finished.set()

    def stop(self) -> SegmentResult:
        """Stop the stream, drain the queue, close the file. Idempotent."""
        if self._result is not None:
            return self._result
        self._stopping = True
        try:
            self._stream.stop()
        finally:
            self._stream.close()
            self._queue.put(None)
            self._thread.join(timeout=30)
        size = self._writer.close()
        self.finished.set()
        if self.error is not None:
            raise DeviceError(f"writing {self.path.name} failed: {self.error}")
        self._result = SegmentResult(
            frames=self._writer.frames,
            size_bytes=size,
            peak_dbfs=self.stats.peak_dbfs,
            rms_dbfs=self.stats.rms_dbfs,
            clipped_samples=self.stats.clipped,
            input_overflows=self.overflows,
            digital_silence=self.stats.digital_silence,
        )
        return self._result

    def abandon(self) -> None:
        """Stop and delete the file (an aborted or redone segment)."""
        try:
            self.stop()
        except DeviceError:
            pass
        self.path.unlink(missing_ok=True)


def probe(
    backend: AudioBackend,
    device: InputDevice,
    sample_rate: int,
    seconds: float = 0.5,
    timeout: float = 5.0,
) -> BlockStats:
    """Open the device at the chain's rate and listen briefly: proves the stream works and the
    signal is not digital zeros."""
    stats = BlockStats()
    lock = threading.Lock()
    target = int(seconds * sample_rate)

    def on_block(data: bytes, frames: int, overflowed: bool) -> None:
        with lock:
            if stats.frames < target:
                stats.add(data)

    stream = backend.open_input(device, sample_rate, on_block)
    try:
        stream.start()
        deadline = time.monotonic() + timeout
        while True:
            with lock:
                if stats.frames >= target:
                    break
            if time.monotonic() > deadline:
                raise DeviceError(f"{device.name!r} delivered no audio within {timeout:.0f} s")
            time.sleep(0.02)
    finally:
        try:
            stream.stop()
        finally:
            stream.close()
    return stats

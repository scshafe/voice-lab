"""The PortAudio capture worker: one take, in its own process (D-039; spike S-1).

    voice-lab-desktop --capture-worker --output DIR/original.partial.wav \\
        --evidence DIR/continuity.json [--trace DIR/trace.jsonl] [--max-seconds 600] \\
        [--block-frames 1024] [--tolerance-frames 64] [--simulated JSON]

Modelled on the Swift app's Audio Queue worker (``cole_voice_lab.capture_native``,
D-015): the UMIK-1 only (anything else is refused before a file exists); an exclusive,
private partial; the audio callback copies each block into a bounded queue and does no
IO (a full queue fails the take: nothing is ever dropped); a writer thread writes the
canonical WAV (mono 48 kHz s24le) and reports ``<bytes>\\n`` on stdout for every block it
wrote (D-014's confirmed frames); ``q`` on stdin stops, and EOF on stdin, SIGINT or SIGTERM
fail closed; no first block within the startup timeout fails; ``--max-seconds`` of audio
stops with ``maximum-duration``. Every block is checked by ``timing.ContinuityTracker``;
the first failure stops the take and keeps the partial. The evidence
(``evidence.PortAudioContinuityEvidence``) is written on success and on failure; the last
line on stderr is ``VOICE_LAB_CAPTURE_SUMMARY {json}``; the exit status is 0 only for a
successful take.

``--trace`` writes one JSON line per accepted block (written by the writer thread, never
the callback): its index, frames, ADC and current time, flags and the worker's monotonic
receive time; the S-1 harness keeps it beside each take.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import resource
import signal
import stat
import sys
import threading
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal

from ..measurement.devices import DeviceError, InputDevice, choose_umik
from .backends import PortAudioTimedBackend, SimulatedSpec, SimulatedTimedBackend, TimedBackend
from .timing import DEFAULT_TOLERANCE_FRAMES, BlockTiming, ContinuityError, ContinuityTracker
from .types import FRAME_BYTES, SAMPLE_RATE

SUMMARY_PREFIX: Final = "VOICE_LAB_CAPTURE_SUMMARY "
DEFAULT_BLOCK_FRAMES: Final = 1024
DEFAULT_QUEUE_BLOCKS: Final = 64
MAX_CAPTURE_SECONDS: Final = 600.0
STARTUP_TIMEOUT_SECONDS: Final = 10.0
POLL_SECONDS: Final = 0.02


class CaptureError(Exception):
    """The take failed; the partial (if any) is kept. ``summary`` is what the worker
    reports for it (the evidence's fields and its times) when there is one."""

    def __init__(self, message: str, summary: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.summary = summary


def max_rss_bytes() -> int:
    """This process's peak resident set (``ru_maxrss`` is KiB on Linux, bytes on macOS)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def current_rss_bytes() -> int | None:
    """The resident set now (Linux ``/proc``), else ``None``."""
    try:
        fields_ = Path("/proc/self/statm").read_text().split()
        return int(fields_[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


@dataclass
class _Block:
    payload: bytes
    timing: BlockTiming
    index: int
    received_at: float


@dataclass
class CaptureBuffers:
    """The callback's side (``receive``: check, copy, enqueue) and the writer's side."""

    tracker: ContinuityTracker
    capacity: int = DEFAULT_QUEUE_BLOCKS
    monotonic: Callable[[], float] = time.monotonic
    writer_delay: float = 0.0
    pending: queue.Queue[_Block] = field(init=False)
    failed: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    failure: BaseException | None = None
    high_water: int = 0
    written_frames: int = 0
    first_block_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.pending = queue.Queue(maxsize=self.capacity)

    def fail(self, error: BaseException) -> None:
        with self.lock:
            if self.failure is None:
                self.failure = error
        self.failed.set()

    def receive(self, payload: bytes, timing: BlockTiming) -> None:
        """The audio callback: never raises, never does IO."""
        if self.failed.is_set():
            return
        try:
            if len(payload) != timing.frames * FRAME_BYTES:
                raise ContinuityError(
                    f"block {self.tracker.callback_count}: {len(payload)} bytes for "
                    f"{timing.frames} frames of 24-bit mono"
                )
            index = self.tracker.callback_count
            self.tracker.receive(timing)
            now = self.monotonic()
            if self.first_block_at is None:
                self.first_block_at = now
            try:
                self.pending.put_nowait(_Block(payload, timing, index, now))
            except queue.Full as exc:
                raise CaptureError(
                    f"the writer fell {self.capacity} blocks behind; the take stops rather "
                    "than drop audio"
                ) from exc
            self.high_water = max(self.high_water, self.pending.qsize())
        except BaseException as error:  # noqa: BLE001 - reported, never raised into PortAudio
            self.fail(error)

    def write(self, stream: BinaryIO, progress: BinaryIO, trace: BinaryIO | None) -> None:
        try:
            with wave.open(stream, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(FRAME_BYTES)
                writer.setframerate(SAMPLE_RATE)
                while not self.finished.is_set() or not self.pending.empty():
                    try:
                        block = self.pending.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if self.writer_delay:
                        time.sleep(self.writer_delay)
                    writer.writeframesraw(block.payload)
                    self.written_frames += block.timing.frames
                    if trace is not None:
                        trace.write(_trace_line(block))
                    line = f"{len(block.payload)}\n".encode("ascii")
                    if progress.write(line) != len(line):
                        raise CaptureError("the progress pipe took an incomplete line")
                    progress.flush()
            stream.flush()
            os.fsync(stream.fileno())
            if trace is not None:
                trace.flush()
        except BaseException as error:  # noqa: BLE001 - reported by the controller
            self.fail(error)
            # Keep draining, so the callback never blocks on a full queue after a failure.
            while not self.finished.is_set():
                try:
                    self.pending.get(timeout=0.05)
                except queue.Empty:
                    continue


def _trace_line(block: _Block) -> bytes:
    timing = block.timing
    record = {
        "index": block.index,
        "frames": timing.frames,
        "adc_time": timing.adc_time,
        "current_time": timing.current_time,
        "input_overflow": timing.input_overflow,
        "input_underflow": timing.input_underflow,
        "received_monotonic": block.received_at,
    }
    return (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")


@dataclass
class Settings:
    output: Path
    evidence: Path
    trace: Path | None = None
    max_seconds: float = MAX_CAPTURE_SECONDS
    block_frames: int = DEFAULT_BLOCK_FRAMES
    tolerance_frames: float = DEFAULT_TOLERANCE_FRAMES
    queue_blocks: int = DEFAULT_QUEUE_BLOCKS
    startup_timeout: float = STARTUP_TIMEOUT_SECONDS
    writer_delay: float = 0.0


def _create_private(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    stream = os.fdopen(descriptor, "wb")
    os.fchmod(stream.fileno(), 0o600)
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise CaptureError(f"{path.name} is not a regular file")
    return stream


def _wav_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        while payload := stream.read(1024 * 1024):
            digest.update(payload)
            size += len(payload)
    return digest.hexdigest(), size


def run_capture(
    settings: Settings,
    *,
    backend: TimedBackend,
    stop_requested: threading.Event,
    progress: BinaryIO,
    control_error: Callable[[], BaseException | None] = lambda: None,
    control_times: dict[str, float] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Record one take; returns the summary (the evidence's fields and the worker's times).

    ``control_times`` is filled by whoever reads the stop request (``stop_read``: when
    the ``q`` arrived), and is copied into the summary's ``monotonic`` times.

    Raises: ``DeviceError`` before any file exists when there is no UMIK-1 (or it cannot
    be listed); ``CaptureError`` with the summary when the take failed after the partial
    was created (the evidence, with ``success`` false, is written first)."""
    device: InputDevice = choose_umik(backend.list_inputs())
    tracker = ContinuityTracker(SAMPLE_RATE, settings.tolerance_frames)
    buffers = CaptureBuffers(
        tracker,
        capacity=settings.queue_blocks,
        monotonic=monotonic,
        writer_delay=settings.writer_delay,
    )
    # The baseline: the resident set now (Linux), else the peak so far (macOS).
    rss_before = current_rss_bytes() or max_rss_bytes()
    failures: list[BaseException] = []
    stop_reason: Literal["requested", "maximum-duration"] | None = None
    started_at: datetime | None = None
    times: dict[str, float | None] = {"started": monotonic()}
    output = _create_private(settings.output)
    trace = _create_private(settings.trace) if settings.trace is not None else None
    stream = None
    writer: threading.Thread | None = None
    try:
        writer = threading.Thread(
            target=buffers.write, args=(output, progress, trace), name="capture-writer", daemon=True
        )
        writer.start()
        stream = backend.open_timed_input(
            device, block_frames=settings.block_frames, callback=buffers.receive
        )
        if stop_requested.is_set():
            raise CaptureError("stopped before the microphone opened")
        stream.start()
        started_at = wall_clock()
        opened = monotonic()
        limit = round(settings.max_seconds * SAMPLE_RATE)
        while not stop_requested.is_set() and not buffers.failed.is_set():
            if tracker.received_frames >= limit:
                stop_reason = "maximum-duration"
                break
            if not tracker.callback_count and monotonic() - opened >= settings.startup_timeout:
                raise CaptureError(
                    f"the UMIK-1 delivered no audio within {settings.startup_timeout:g} s"
                )
            if not stream.active:
                raise CaptureError("the input stream stopped by itself (was the UMIK-1 unplugged?)")
            stop_requested.wait(POLL_SECONDS)
        if stop_reason is None and not buffers.failed.is_set():
            stop_reason = "requested"
        if interrupted := control_error():
            raise interrupted
    except BaseException as error:  # noqa: BLE001 - every failure is reported in the evidence
        failures.append(error)
    finally:
        times["stop_seen"] = monotonic()
        if stream is not None:
            for cleanup in (stream.stop, stream.close):
                try:
                    cleanup()
                except BaseException as error:  # noqa: BLE001
                    failures.append(error)
        buffers.finished.set()
        if writer is not None:
            writer.join(timeout=10.0)
            if writer.is_alive():
                failures.append(CaptureError("the writer did not finish after the stop"))
        output.close()
        if trace is not None:
            trace.close()
        if buffers.failure is not None:
            failures.insert(0, buffers.failure)
        if interrupted := control_error():
            if interrupted not in failures:
                failures.append(interrupted)
    if not failures and (
        buffers.written_frames == 0 or buffers.written_frames != tracker.received_frames
    ):
        failures.append(
            CaptureError(
                f"{tracker.received_frames} frames received but {buffers.written_frames} written"
            )
        )
    error_text = "; ".join(str(error) or type(error).__name__ for error in failures) or None
    sha256: str | None = None
    size: int | None = None
    try:
        sha256, size = _wav_digest(settings.output)
    except OSError as exc:
        error_text = f"{error_text + '; ' if error_text else ''}cannot read the WAV back: {exc}"
    times["first_block"] = buffers.first_block_at
    times.update(control_times or {})
    times["finished"] = monotonic()
    # Imported only now: the contract models (pydantic) are most of the worker's start-up
    # time, and the first block should not wait for them.
    from .evidence import PortAudioContinuityEvidence, write_evidence

    evidence = PortAudioContinuityEvidence(
        host_api=device.host_api,
        simulated=backend.simulated,
        device_name=device.name,
        device_usb_id=device.usb_id,
        output_name=settings.output.name,
        wav_sha256=sha256,
        wav_size_bytes=size,
        frames=buffers.written_frames,
        received_frames=tracker.received_frames,
        callback_count=tracker.callback_count,
        block_frames=settings.block_frames,
        first_adc_time=tracker.first_adc_time,
        last_adc_end=tracker.last_adc_end,
        adc_time_valid=tracker.adc_time_valid,
        tolerance_frames=tracker.tolerance_frames,
        max_abs_deviation_frames=tracker.max_abs_deviation_frames,
        input_overflows=tracker.input_overflows,
        input_underflows=tracker.input_underflows,
        queue_capacity=buffers.capacity,
        queue_high_water=buffers.high_water,
        success=error_text is None,
        continuity_checked=tracker.callback_count > 0,
        stop_reason=None if error_text else stop_reason,
        error=error_text[:2000] if error_text else None,
        started_at=started_at,
    )
    write_evidence(settings.evidence, evidence)
    summary: dict[str, Any] = {
        **json.loads(evidence.model_dump_json()),
        "device": evidence.device,
        "monotonic": times,
        "rss_before_bytes": rss_before,
        "max_rss_bytes": max_rss_bytes(),
    }
    if error_text:
        raise CaptureError(error_text, summary) from (failures[0] if failures else None)
    return summary


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="voice-lab-desktop --capture-worker",
        description="Record one take from the UMIK-1 with continuity evidence (D-039).",
    )
    ap.add_argument("--output", type=Path, required=True, help="the partial WAV (new)")
    ap.add_argument("--evidence", type=Path, required=True, help="the evidence JSON (new)")
    ap.add_argument("--trace", type=Path, help="per-block timing, JSON lines (new)")
    ap.add_argument("--max-seconds", type=float, default=MAX_CAPTURE_SECONDS)
    ap.add_argument("--block-frames", type=int, default=DEFAULT_BLOCK_FRAMES)
    ap.add_argument("--tolerance-frames", type=float, default=DEFAULT_TOLERANCE_FRAMES)
    ap.add_argument("--queue-blocks", type=int, default=DEFAULT_QUEUE_BLOCKS)
    ap.add_argument("--startup-timeout", type=float, default=STARTUP_TIMEOUT_SECONDS)
    ap.add_argument(
        "--simulated",
        metavar="JSON",
        help="a simulated UMIK-1 (tests and the S-1 harness's dry run; never hardware)",
    )
    return ap


def parse_settings(argv: Sequence[str]) -> tuple[Settings, SimulatedSpec | None]:
    """Raises: ``SystemExit`` (argparse) for bad arguments."""
    ap = build_parser()
    options = ap.parse_args(argv)
    if not 0 < options.max_seconds <= MAX_CAPTURE_SECONDS:
        ap.error(f"--max-seconds must be in (0, {MAX_CAPTURE_SECONDS:g}]")
    if not 16 <= options.block_frames <= 16384:
        ap.error("--block-frames must be in [16, 16384]")
    if not 0 < options.tolerance_frames <= 4800:
        ap.error("--tolerance-frames must be in (0, 4800]")
    if not 1 <= options.queue_blocks <= 4096:
        ap.error("--queue-blocks must be in [1, 4096]")
    if not 0 < options.startup_timeout <= 60:
        ap.error("--startup-timeout must be in (0, 60]")
    spec = None
    if options.simulated is not None:
        try:
            spec = SimulatedSpec.from_json(json.loads(options.simulated))
        except (ValueError, TypeError) as exc:
            ap.error(f"--simulated: {exc}")
    settings = Settings(
        output=options.output,
        evidence=options.evidence,
        trace=options.trace,
        max_seconds=options.max_seconds,
        block_frames=options.block_frames,
        tolerance_frames=options.tolerance_frames,
        queue_blocks=options.queue_blocks,
        startup_timeout=options.startup_timeout,
        writer_delay=spec.writer_delay if spec is not None else 0.0,
    )
    return settings, spec


def make_backend(spec: SimulatedSpec | None) -> TimedBackend:
    return SimulatedTimedBackend(spec) if spec is not None else PortAudioTimedBackend()


def main(argv: Sequence[str] | None = None) -> int:
    """The worker process. stdin ``q`` stops; EOF, SIGINT or SIGTERM fail closed."""
    settings, spec = parse_settings(list(sys.argv[1:] if argv is None else argv))
    stop_requested = threading.Event()
    errors: list[BaseException] = []
    control_times: dict[str, float] = {}
    control_fd = sys.stdin.fileno()

    def interrupted(number: int, _frame: object) -> None:
        errors.append(CaptureError(f"interrupted by signal {number}"))
        stop_requested.set()

    def read_control() -> None:
        try:
            while not stop_requested.is_set():
                # The raw descriptor, not sys.stdin's buffer: a daemon thread blocked in a
                # buffered read makes the interpreter abort at exit (a take that stops at
                # its maximum duration exits while this thread still waits).
                value = os.read(control_fd, 1)
                if value == b"q":
                    control_times["stop_read"] = time.monotonic()
                    stop_requested.set()
                elif not value:
                    raise CaptureError("the control pipe closed before a stop was asked for")
        except BaseException as error:  # noqa: BLE001 - fails the take
            errors.append(error)
            stop_requested.set()

    previous = {
        number: signal.signal(number, interrupted) for number in (signal.SIGINT, signal.SIGTERM)
    }
    summary: dict[str, Any] = {}
    try:
        threading.Thread(target=read_control, name="capture-control", daemon=True).start()
        summary = run_capture(
            settings,
            backend=make_backend(spec),
            stop_requested=stop_requested,
            progress=sys.stdout.buffer,
            control_error=lambda: errors[0] if errors else None,
            control_times=control_times,
        )
        return 0
    except CaptureError as error:
        summary = error.summary or {"success": False, "error": str(error)}
        return 1
    except (DeviceError, OSError) as error:
        summary = {"success": False, "error": str(error) or type(error).__name__}
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        print(SUMMARY_PREFIX + json.dumps(summary, sort_keys=True), file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

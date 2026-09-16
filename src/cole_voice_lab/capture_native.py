"""Native macOS buffered PCM capture worker with verified sample continuity.

@canonical native_capture -- AudioQueue callbacks never perform file or pipe IO.
The subprocess boundary isolates capture callbacks from desktop Python workloads.
Related: AudioToolbox/AudioQueue.h and CoreAudioTypes/CoreAudioBaseTypes.h.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import queue
import signal
import stat
import sys
import threading
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

from .canonical import open_regular_no_follow
from .errors import ContractError, ExternalToolError, SecurityError
from .models import CaptureDevice
from .paths import _reject_existing_symlink_components

SAMPLE_RATE = 48_000
FRAME_BYTES = 3
BUFFER_FRAMES = 4096
BUFFER_COUNT = 8
WRITER_QUEUE_BUFFERS = 64
MAX_CAPTURE_SECONDS = 600.0
STARTUP_TIMEOUT_SECONDS = 10.0
SUMMARY_PREFIX = "COLE_VOICE_NATIVE_CAPTURE_SUMMARY "
_SAMPLE_TIME_VALID = 1
type CaptureSummary = dict[str, int | float | bool | str | None]


class NativeCaptureError(ExternalToolError):
    """Native capture failed; partial PCM is not a successfully completed take."""


def _fourcc(value: str) -> int:
    return int.from_bytes(value.encode("ascii"), "big")


class AudioStreamBasicDescription(ctypes.Structure):
    _fields_ = [
        ("sample_rate", ctypes.c_double),
        ("format_id", ctypes.c_uint32),
        ("format_flags", ctypes.c_uint32),
        ("bytes_per_packet", ctypes.c_uint32),
        ("frames_per_packet", ctypes.c_uint32),
        ("bytes_per_frame", ctypes.c_uint32),
        ("channels", ctypes.c_uint32),
        ("bits_per_channel", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class SMPTETime(ctypes.Structure):
    _fields_ = [
        ("subframes", ctypes.c_int16),
        ("subframe_divisor", ctypes.c_int16),
        ("counter", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("hours", ctypes.c_int16),
        ("minutes", ctypes.c_int16),
        ("seconds", ctypes.c_int16),
        ("frames", ctypes.c_int16),
    ]


class AudioTimeStamp(ctypes.Structure):
    _fields_ = [
        ("sample_time", ctypes.c_double),
        ("host_time", ctypes.c_uint64),
        ("rate_scalar", ctypes.c_double),
        ("word_clock_time", ctypes.c_uint64),
        ("smpte_time", SMPTETime),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class AudioQueueBuffer(ctypes.Structure):
    _fields_ = [
        ("capacity", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
        ("byte_size", ctypes.c_uint32),
        ("user_data", ctypes.c_void_p),
        ("packet_capacity", ctypes.c_uint32),
        ("packet_descriptions", ctypes.c_void_p),
        ("packet_count", ctypes.c_uint32),
    ]


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


_BufferPointer = ctypes.POINTER(AudioQueueBuffer)
_TimestampPointer = ctypes.POINTER(AudioTimeStamp)
_InputCallback = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    _BufferPointer,
    _TimestampPointer,
    ctypes.c_uint32,
    ctypes.c_void_p,
)
type ReceiveBuffer = Callable[[bytes, float, int], None]
type ReportFailure = Callable[[BaseException], None]


def _bind(
    library: ctypes.CDLL, name: str, arguments: Sequence[object], result: object
) -> Callable[..., Any]:
    function = getattr(library, name)
    function.argtypes = list(arguments)
    function.restype = result
    return cast(Callable[..., Any], function)


def _check(status: int, operation: str) -> None:
    if status != 0:
        raise NativeCaptureError(f"Core Audio {operation} failed (OSStatus {status})")


class _CoreAudio:
    """Typed HAL discovery and CFString ownership; discovery opens no microphone."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise NativeCaptureError("native microphone capture requires macOS")
        try:
            self.core = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            self.cf = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError as error:
            raise NativeCaptureError(f"Cannot load Core Audio frameworks: {error}") from error
        pointer = ctypes.c_void_p
        uint = ctypes.c_uint32
        address = ctypes.POINTER(AudioObjectPropertyAddress)
        self.data_size = _bind(
            self.core,
            "AudioObjectGetPropertyDataSize",
            [uint, address, uint, pointer, ctypes.POINTER(uint)],
            ctypes.c_int32,
        )
        self.data = _bind(
            self.core,
            "AudioObjectGetPropertyData",
            [uint, address, uint, pointer, ctypes.POINTER(uint), pointer],
            ctypes.c_int32,
        )
        self.release = _bind(self.cf, "CFRelease", [pointer], None)
        self.string_length = _bind(self.cf, "CFStringGetLength", [pointer], ctypes.c_long)
        self.string_get = _bind(
            self.cf,
            "CFStringGetCString",
            [pointer, pointer, ctypes.c_long, uint],
            ctypes.c_uint8,
        )
        self.string_create = _bind(
            self.cf,
            "CFStringCreateWithCString",
            [pointer, ctypes.c_char_p, uint],
            pointer,
        )

    def property(self, device: int, selector: str, scope: str = "glob") -> bytes:
        address = AudioObjectPropertyAddress(_fourcc(selector), _fourcc(scope), 0)
        size = ctypes.c_uint32()
        _check(self.data_size(device, ctypes.byref(address), 0, None, ctypes.byref(size)), selector)
        if size.value > 1024 * 1024:
            raise NativeCaptureError(f"Core Audio property {selector} exceeds bounded size")
        payload = ctypes.create_string_buffer(size.value)
        expected = size.value
        _check(
            self.data(device, ctypes.byref(address), 0, None, ctypes.byref(size), payload),
            selector,
        )
        if size.value != expected:
            raise NativeCaptureError(f"Core Audio property {selector} changed during discovery")
        return payload.raw

    def string(self, reference: int) -> str:
        if not reference:
            raise NativeCaptureError("Core Audio returned a null string")
        length = int(self.string_length(reference))
        if not 0 <= length <= 4096:
            raise NativeCaptureError("Core Audio string exceeds bounded size")
        payload = ctypes.create_string_buffer(length * 4 + 1)
        if not self.string_get(reference, payload, len(payload), 0x08000100):
            raise NativeCaptureError("Core Audio string is not valid UTF-8")
        return payload.value.decode("utf-8")

    def string_property(self, device: int, selector: str) -> str:
        payload = self.property(device, selector)
        if len(payload) != ctypes.sizeof(ctypes.c_void_p):
            raise NativeCaptureError(f"Core Audio property {selector} has invalid pointer size")
        reference = ctypes.c_void_p.from_buffer_copy(payload).value
        if not reference:
            raise NativeCaptureError(f"Core Audio property {selector} is null")
        try:
            return self.string(reference)
        finally:
            self.release(reference)

    def devices(self) -> tuple[CaptureDevice, ...]:
        payload = self.property(1, "dev#")
        if len(payload) % ctypes.sizeof(ctypes.c_uint32):
            raise NativeCaptureError("Core Audio device list has invalid byte count")
        devices: list[CaptureDevice] = []
        identifiers = (ctypes.c_uint32 * (len(payload) // 4)).from_buffer_copy(payload)
        for identifier in identifiers:
            if not self.property(identifier, "stm#", "inpt"):
                continue
            devices.append(
                CaptureDevice(
                    backend="coreaudio",
                    device_id=self.string_property(identifier, "uid "),
                    display_name=self.string_property(identifier, "lnam"),
                )
            )
        if len({item.device_id for item in devices}) != len(devices):
            raise NativeCaptureError("Core Audio reported duplicate input device UIDs")
        return tuple(devices)


def list_coreaudio_input_devices() -> tuple[CaptureDevice, ...]:
    """Return native input names and stable UIDs without opening a recording stream."""
    return _CoreAudio().devices()


def _validate_uid(device_id: str) -> None:
    if not device_id.strip() or len(device_id) > 500 or any(ord(c) < 32 for c in device_id):
        raise ContractError("native capture device UID is invalid")


def native_capture_command(
    *, output_path: Path, device_id: str, diagnostics_path: Path | None = None
) -> tuple[str, ...]:
    """Build the local isolated worker command; it does not open a device or file."""
    _validate_uid(device_id)
    diagnostics = (
        ("--diagnostics-path", str(diagnostics_path)) if diagnostics_path is not None else ()
    )
    return (
        sys.executable,
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.capture_native",
        "--device-uid",
        device_id,
        *diagnostics,
        "--output",
        str(output_path),
    )


class CaptureBackend(Protocol):
    def open(self, device_uid: str, receive: ReceiveBuffer, fail: ReportFailure) -> None: ...
    def start(self) -> None: ...
    def health(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class _AudioQueueBackend:
    """One AudioQueue with multiple native buffers, explicit UID, and short callbacks."""

    def __init__(self) -> None:
        self.core = _CoreAudio()
        try:
            library = ctypes.CDLL("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
        except OSError as error:
            raise NativeCaptureError(f"Cannot load AudioQueue framework: {error}") from error
        self.library = library
        p = ctypes.c_void_p
        u = ctypes.c_uint32
        status = ctypes.c_int32
        self.new_input = _bind(
            library,
            "AudioQueueNewInput",
            [
                ctypes.POINTER(AudioStreamBasicDescription),
                _InputCallback,
                p,
                p,
                p,
                u,
                ctypes.POINTER(p),
            ],
            status,
        )
        self.set_property = _bind(library, "AudioQueueSetProperty", [p, u, p, u], status)
        self.get_property = _bind(
            library, "AudioQueueGetProperty", [p, u, p, ctypes.POINTER(u)], status
        )
        self.allocate = _bind(
            library, "AudioQueueAllocateBuffer", [p, u, ctypes.POINTER(_BufferPointer)], status
        )
        self.enqueue = _bind(library, "AudioQueueEnqueueBuffer", [p, _BufferPointer, u, p], status)
        self.start_queue = _bind(library, "AudioQueueStart", [p, _TimestampPointer], status)
        self.stop_queue = _bind(library, "AudioQueueStop", [p, ctypes.c_uint8], status)
        self.dispose = _bind(library, "AudioQueueDispose", [p, ctypes.c_uint8], status)
        self.reference = ctypes.c_void_p()
        self.buffers: list[object] = []
        self.callback: object | None = None
        self.stopping = False
        self.started = False
        self.has_samples = False
        self.device_uid = ""

    def _get(self, selector: str, destination: Any) -> None:
        size = ctypes.c_uint32(ctypes.sizeof(destination))
        _check(
            self.get_property(
                self.reference, _fourcc(selector), ctypes.byref(destination), ctypes.byref(size)
            ),
            f"get {selector}",
        )
        if size.value != ctypes.sizeof(destination):
            raise NativeCaptureError(f"AudioQueue property {selector} has unexpected size")

    def open(self, device_uid: str, receive: ReceiveBuffer, fail: ReportFailure) -> None:
        selected = [item for item in self.core.devices() if item.device_id == device_uid]
        if len(selected) != 1:
            raise NativeCaptureError("Selected native microphone UID is unavailable or ambiguous")
        if "umik-1" not in selected[0].display_name.casefold():
            raise NativeCaptureError("Native production capture requires the selected UMIK-1")
        self.device_uid = device_uid

        def callback(
            _user: object, _queue: object, buffer: Any, timestamp: Any, _count: int, _descs: object
        ) -> None:
            try:
                if not buffer or not timestamp:
                    raise NativeCaptureError("AudioQueue callback has null buffer or timestamp")
                item = buffer.contents
                size = int(item.byte_size)
                if size:
                    if not item.data or size > item.capacity or size > BUFFER_FRAMES * FRAME_BYTES:
                        raise NativeCaptureError("AudioQueue callback exceeds its native buffer")
                    receive(
                        ctypes.string_at(item.data, size),
                        float(timestamp.contents.sample_time),
                        int(timestamp.contents.flags),
                    )
                    self.has_samples = True
                if not self.stopping:
                    _check(self.enqueue(self.reference, buffer, 0, None), "re-enqueue input buffer")
            except BaseException as error:
                self.stopping = True
                fail(error)

        self.callback = _InputCallback(callback)
        description = AudioStreamBasicDescription(
            SAMPLE_RATE, _fourcc("lpcm"), 12, FRAME_BYTES, 1, FRAME_BYTES, 1, 24, 0
        )
        _check(
            self.new_input(
                ctypes.byref(description),
                self.callback,
                None,
                None,
                None,
                0,
                ctypes.byref(self.reference),
            ),
            "create input queue",
        )
        if not self.reference.value:
            raise NativeCaptureError("AudioQueue created a null input queue")
        uid = self.core.string_create(None, device_uid.encode("utf-8"), 0x08000100)
        if not uid:
            raise NativeCaptureError("Cannot create selected microphone UID string")
        try:
            pointer = ctypes.c_void_p(uid)
            _check(
                self.set_property(
                    self.reference, _fourcc("aqcd"), ctypes.byref(pointer), ctypes.sizeof(pointer)
                ),
                "select microphone UID",
            )
        finally:
            self.core.release(uid)
        actual = AudioStreamBasicDescription()
        self._get("aqft", actual)
        if bytes(actual) != bytes(description):
            raise NativeCaptureError("AudioQueue did not retain canonical mono 48 kHz 24-bit PCM")
        self._check_device()
        rate = ctypes.c_double()
        self._get("aqsr", rate)
        if rate.value != SAMPLE_RATE:
            raise NativeCaptureError(
                "Selected microphone must use 48000 Hz; resampling is disabled"
            )
        for _ in range(BUFFER_COUNT):
            buffer = _BufferPointer()
            _check(
                self.allocate(self.reference, BUFFER_FRAMES * FRAME_BYTES, ctypes.byref(buffer)),
                "allocate input buffer",
            )
            if not buffer:
                raise NativeCaptureError("AudioQueue allocated a null input buffer")
            self.buffers.append(buffer)
            _check(self.enqueue(self.reference, buffer, 0, None), "enqueue input buffer")

    def _check_device(self) -> None:
        uid = ctypes.c_void_p()
        self._get("aqcd", uid)
        if not uid.value:
            raise NativeCaptureError("AudioQueue lost its selected microphone UID")
        try:
            if self.core.string(uid.value) != self.device_uid:
                raise NativeCaptureError("AudioQueue unexpectedly changed microphone")
        finally:
            # AudioQueueGetProperty explicitly duplicates CF values despite its
            # Get name; Apple documents caller ownership and mandatory release.
            self.core.release(uid.value)

    def start(self) -> None:
        _check(self.start_queue(self.reference, None), "start microphone")
        self.started = True

    def health(self) -> None:
        self._check_device()
        sample_rate = ctypes.c_double()
        self._get("aqsr", sample_rate)
        if sample_rate.value != SAMPLE_RATE:
            raise NativeCaptureError("Native microphone sample rate changed during capture")
        converter_error = ctypes.c_uint32()
        self._get("qcve", converter_error)
        if converter_error.value:
            raise NativeCaptureError(f"AudioQueue converter failed ({converter_error.value})")
        running = ctypes.c_uint32()
        self._get("aqrn", running)
        if self.has_samples and not running.value and not self.stopping:
            raise NativeCaptureError("AudioQueue microphone stopped unexpectedly")

    def stop(self) -> None:
        self.stopping = True
        if self.reference.value and self.started:
            _check(self.stop_queue(self.reference, True), "stop microphone")
            self.started = False

    def close(self) -> None:
        self.stopping = True
        if self.reference.value:
            _check(self.dispose(self.reference, True), "dispose microphone queue")
            self.reference = ctypes.c_void_p()
        self.callback = None
        self.buffers.clear()


class _CaptureBuffers:
    """Continuity validation and bounded handoff; never silently discard a buffer."""

    def __init__(self, capacity: int = WRITER_QUEUE_BUFFERS) -> None:
        self.pending: queue.Queue[bytes] = queue.Queue(maxsize=capacity)
        self.finished = threading.Event()
        self.failed = threading.Event()
        self.failure: BaseException | None = None
        self.lock = threading.Lock()
        self.expected_sample: float | None = None
        self.first_sample: float | None = None
        self.callback_count = 0
        self.received_frames = 0
        self.written_frames = 0
        self.output_identity: tuple[int, int] | None = None

    def fail(self, error: BaseException) -> None:
        with self.lock:
            if self.failure is None:
                self.failure = error
            self.failed.set()

    def receive(self, payload: bytes, sample_time: float, flags: int) -> None:
        if self.failed.is_set():
            return
        if not payload or len(payload) % FRAME_BYTES:
            raise NativeCaptureError("Native input buffer does not contain complete PCM frames")
        if not flags & _SAMPLE_TIME_VALID or not math.isfinite(sample_time):
            raise NativeCaptureError("Native input buffer lacks a valid sample timestamp")
        if self.expected_sample is not None and abs(sample_time - self.expected_sample) > 0.000001:
            raise NativeCaptureError(
                "Native capture sample discontinuity: "
                f"expected={self.expected_sample:.6f}, actual={sample_time:.6f}, "
                f"gap_frames={sample_time - self.expected_sample:.6f}"
            )
        frames = len(payload) // FRAME_BYTES
        try:
            self.pending.put_nowait(payload)
        except queue.Full as error:
            raise NativeCaptureError(
                "Native capture writer queue overflow; no silent buffer loss"
            ) from error
        self.expected_sample = sample_time + frames
        if self.first_sample is None:
            self.first_sample = sample_time
        self.received_frames += frames
        self.callback_count += 1

    def write(self, stream: BinaryIO, progress: BinaryIO) -> None:
        try:
            with wave.open(stream, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(FRAME_BYTES)
                writer.setframerate(SAMPLE_RATE)
                while not self.finished.is_set() or not self.pending.empty():
                    try:
                        payload = self.pending.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    writer.writeframesraw(payload)
                    self.written_frames += len(payload) // FRAME_BYTES
                    line = f"{len(payload)}\n".encode("ascii")
                    if progress.write(line) != len(line):
                        raise NativeCaptureError(
                            "Native capture progress pipe write was incomplete"
                        )
                    progress.flush()
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException as error:
            self.fail(error)


def _run_native_capture(
    *,
    output_path: Path,
    device_uid: str,
    stop_requested: threading.Event,
    progress: BinaryIO,
    buffers: _CaptureBuffers,
    backend_factory: Callable[[], CaptureBackend] = _AudioQueueBackend,
    monotonic: Callable[[], float] = time.monotonic,
    control_error: Callable[[], BaseException | None] = lambda: None,
) -> CaptureSummary:
    """Capture one private partial; failures stop hardware and leave recoverable PCM.

    The controller owns Stop/Dispose; neither is called under a callback lock.
    """
    _validate_uid(device_uid)
    output_path = _reject_existing_symlink_components(output_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(output_path, flags, 0o600)
    except OSError as error:
        raise SecurityError(
            f"Cannot exclusively create private capture partial: {error}"
        ) from error
    backend: CaptureBackend | None = None
    writer: threading.Thread | None = None
    failures: list[BaseException] = []
    stop_reason = "requested"
    with os.fdopen(descriptor, "wb") as stream:
        try:
            os.fchmod(stream.fileno(), 0o600)
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise SecurityError("Native capture output is not a regular file")
            buffers.output_identity = (metadata.st_dev, metadata.st_ino)
            writer = threading.Thread(
                target=buffers.write, args=(stream, progress), name="native-pcm-writer", daemon=True
            )
            writer.start()
            backend = backend_factory()
            if stop_requested.is_set():
                raise NativeCaptureError("Native capture stopped before opening the microphone")
            backend.open(device_uid, buffers.receive, buffers.fail)
            started = monotonic()
            backend.start()
            while not stop_requested.is_set() and not buffers.failed.is_set():
                elapsed = monotonic() - started
                if elapsed >= MAX_CAPTURE_SECONDS:
                    stop_reason = "maximum-duration"
                    break
                if not buffers.callback_count and elapsed >= STARTUP_TIMEOUT_SECONDS:
                    raise NativeCaptureError(
                        "Native microphone produced no samples before startup timeout"
                    )
                backend.health()
                stop_requested.wait(0.05)
            if interrupted := control_error():
                raise interrupted
        except BaseException as error:
            failures.append(error)
        finally:
            if backend is not None:
                for cleanup in (backend.stop, backend.close):
                    try:
                        cleanup()
                    except BaseException as error:
                        failures.append(error)
            buffers.finished.set()
            if writer is not None and writer.ident is not None:
                writer.join(timeout=5.0)
                if writer.is_alive():
                    failures.append(
                        NativeCaptureError("Native PCM writer did not finish after stop")
                    )
            if buffers.failure is not None:
                failures.append(buffers.failure)
            if interrupted := control_error():
                if interrupted not in failures:
                    failures.append(interrupted)
        if failures:
            detail = "; ".join(str(error) for error in failures)
            raise NativeCaptureError(
                f"Native capture failed; partial preserved: {detail}"
            ) from failures[0]
        if buffers.written_frames == 0 or buffers.written_frames != buffers.received_frames:
            raise NativeCaptureError(
                "Native capture ended without complete written sample evidence"
            )
    return {
        "backend": "coreaudio-audioqueue",
        "frames": buffers.written_frames,
        "callback_count": buffers.callback_count,
        "continuity_checked": True,
        "stop_reason": stop_reason,
    }


def _write_diagnostics(path: Path, summary: CaptureSummary) -> None:
    path = _reject_existing_symlink_components(path)
    payload = (json.dumps(summary, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _wav_evidence(path: Path, expected_identity: tuple[int, int] | None = None) -> tuple[str, int]:
    path = _reject_existing_symlink_components(path)
    with open_regular_no_follow(path) as stream:
        before = os.fstat(stream.fileno())
        if expected_identity is not None and (before.st_dev, before.st_ino) != expected_identity:
            raise SecurityError("Native capture output was replaced before continuity binding")
        if before.st_nlink != 1:
            raise SecurityError("Native capture output has unexpected hard links")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise SecurityError("Native capture partial is no longer private")
        digest = hashlib.sha256()
        size = 0
        while payload := stream.read(1024 * 1024):
            size += len(payload)
            digest.update(payload)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, after, current)
    }
    if len(identities) != 1 or size != after.st_size:
        raise SecurityError("Native capture partial changed while binding continuity evidence")
    return digest.hexdigest(), size


def run_native_capture(
    *,
    output_path: Path,
    device_uid: str,
    stop_requested: threading.Event,
    progress: BinaryIO,
    backend_factory: Callable[[], CaptureBackend] = _AudioQueueBackend,
    monotonic: Callable[[], float] = time.monotonic,
    control_error: Callable[[], BaseException | None] = lambda: None,
    diagnostics_path: Path | None = None,
) -> CaptureSummary:
    """Record one take, preserving success or failure continuity evidence separately."""
    buffers = _CaptureBuffers()
    summary: CaptureSummary = {}
    try:
        summary = _run_native_capture(
            output_path=output_path,
            device_uid=device_uid,
            stop_requested=stop_requested,
            progress=progress,
            buffers=buffers,
            backend_factory=backend_factory,
            monotonic=monotonic,
            control_error=control_error,
        )
    except BaseException as error:
        summary["error"] = str(error)
        raise
    finally:
        summary.update(
            {
                "schema_version": "cole-voice-lab.capture-continuity/v1",
                "backend": "coreaudio-audioqueue",
                "device_uid": device_uid,
                "frames": buffers.written_frames,
                "received_frames": buffers.received_frames,
                "callback_count": buffers.callback_count,
                "continuity_checked": buffers.callback_count > 0,
                "first_sample_time": buffers.first_sample,
                "last_sample_end": buffers.expected_sample,
                "success": "error" not in summary,
                "output_name": output_path.name,
            }
        )
        try:
            digest, size = _wav_evidence(output_path, buffers.output_identity)
            summary.update({"wav_sha256": digest, "wav_size_bytes": size})
        except Exception as binding_error:
            if "error" not in summary:
                summary["success"] = False
                summary["error"] = f"Cannot bind captured WAV: {binding_error}"
                if diagnostics_path is not None:
                    _write_diagnostics(diagnostics_path, summary)
                raise NativeCaptureError(str(summary["error"])) from binding_error
            summary["wav_binding_error"] = str(binding_error)
        if diagnostics_path is not None:
            try:
                _write_diagnostics(diagnostics_path, summary)
            except Exception as evidence_error:
                detail = str(summary.get("error", "capture finished"))
                raise NativeCaptureError(
                    f"Cannot persist capture continuity evidence after {detail}: {evidence_error}"
                ) from evidence_error
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Controlled local worker: stdin q stops; EOF or interruption fails closed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-uid", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostics-path", type=Path)
    options = parser.parse_args(argv)
    stop_requested = threading.Event()
    errors: list[BaseException] = []

    def interrupted(number: int, _frame: object) -> None:
        errors.append(NativeCaptureError(f"Native capture interrupted by signal {number}"))
        stop_requested.set()

    def read_control() -> None:
        try:
            while not stop_requested.is_set():
                value = sys.stdin.buffer.read(1)
                if value == b"q":
                    stop_requested.set()
                elif not value:
                    raise NativeCaptureError("Native capture control pipe closed unexpectedly")
        except BaseException as error:
            errors.append(error)
            stop_requested.set()

    previous = {
        number: signal.signal(number, interrupted) for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        control = threading.Thread(target=read_control, name="native-capture-control", daemon=True)
        control.start()
        summary = run_native_capture(
            output_path=options.output,
            device_uid=options.device_uid,
            stop_requested=stop_requested,
            progress=sys.stdout.buffer,
            control_error=lambda: errors[0] if errors else None,
            diagnostics_path=options.diagnostics_path,
        )
        if errors:
            raise errors[0]
        print(SUMMARY_PREFIX + json.dumps(summary, sort_keys=True), file=sys.stderr, flush=True)
        return 0
    except Exception as error:
        print(f"Native capture error: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())

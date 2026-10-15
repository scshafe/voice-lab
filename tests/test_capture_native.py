"""Synthetic AudioQueue ABI, buffering, lifecycle and evidence tests; no hardware."""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import math
import os
import signal
import stat
import sys
import threading
import wave
from types import SimpleNamespace

import pytest

import cole_voice_lab.capture_native as native
from cole_voice_lab.errors import ContractError, SecurityError

UID = "synthetic-microphone-uid"
PCM = b"\x01\x02\x03" * 512


class FakeBackend:
    def __init__(self, stop, *, failure=None, gap=0, empty=False, tail=True):
        self.request = stop
        self.failure = failure
        self.gap = gap
        self.empty = empty
        self.tail = tail
        self.calls = []

    def open(self, uid, receive, fail):
        self.calls.append(("open", uid))
        self.receive = receive
        self.fail = fail
        if self.failure == "open":
            raise native.NativeCaptureError("synthetic open failure")

    def start(self):
        self.calls.append("start")
        if self.failure == "start":
            raise native.NativeCaptureError("synthetic start failure")
        if not self.empty:
            self.receive(PCM, 1000.0, 1)

    def health(self):
        self.calls.append("health")
        if self.failure == "health":
            raise native.NativeCaptureError("synthetic health failure")
        if not self.empty:
            try:
                self.receive(PCM, 1512.0 + self.gap, 1)
            except BaseException as error:
                self.fail(error)
        self.request.set()

    def stop(self):
        self.calls.append("stop")
        if self.failure == "stop":
            raise native.NativeCaptureError("synthetic stop failure")
        if self.tail and not self.empty and self.failure not in {"open", "start", "health"}:
            self.receive(PCM[:30], 2024.0, 1)

    def close(self):
        self.calls.append("close")
        if self.failure == "close":
            raise native.NativeCaptureError("synthetic close failure")


def run(tmp_path, *, fake=None, stop=None, progress=None, **kwargs):
    stop = stop or threading.Event()
    fake = fake or FakeBackend(stop)
    progress = progress or io.BytesIO()
    output = tmp_path / "synthetic.partial.wav"
    diagnostics = tmp_path / "continuity.json"
    summary = native.run_native_capture(
        output_path=output,
        device_uid=UID,
        stop_requested=stop,
        progress=progress,
        backend_factory=lambda: fake,
        diagnostics_path=diagnostics,
        **kwargs,
    )
    return summary, output, diagnostics, progress, fake


def test_native_command_preserves_uid_and_final_output_without_opening_anything(tmp_path):
    output = tmp_path / "capture.partial.wav"
    diagnostic = tmp_path / "continuity.json"
    command = native.native_capture_command(
        output_path=output, device_id=UID, diagnostics_path=diagnostic
    )
    assert command == (
        sys.executable,
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.capture_native",
        "--device-uid",
        UID,
        "--diagnostics-path",
        str(diagnostic),
        "--output",
        str(output),
    )
    assert not output.exists()
    assert "--diagnostics-path" not in native.native_capture_command(
        output_path=output, device_id=UID
    )


@pytest.mark.parametrize("uid", ["", " ", "a\n", "a\0b", "x" * 501])
def test_invalid_native_uid_is_rejected(uid, tmp_path):
    with pytest.raises(ContractError, match="UID"):
        native.native_capture_command(output_path=tmp_path / "a.wav", device_id=uid)


def test_native_abi_layout_matches_64_bit_apple_sdk():
    assert ctypes.sizeof(native.AudioStreamBasicDescription) == 40
    assert ctypes.sizeof(native.SMPTETime) == 24
    assert ctypes.sizeof(native.AudioTimeStamp) == 64
    assert ctypes.sizeof(native.AudioQueueBuffer) == 56
    assert ctypes.sizeof(native.AudioObjectPropertyAddress) == 12
    assert native.AudioQueueBuffer.data.offset == 8
    assert native.AudioQueueBuffer.byte_size.offset == 16
    assert native.AudioTimeStamp.flags.offset == 56


def test_buffered_capture_writes_exact_pcm_and_stop_tail_before_summary(tmp_path):
    summary, output, diagnostic, progress, fake = run(tmp_path)
    with wave.open(str(output), "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (
            1,
            3,
            48000,
        )
        assert reader.getnframes() == 1034
        assert reader.readframes(1034) == PCM + PCM + PCM[:30]
    assert progress.getvalue() == b"1536\n1536\n30\n"
    assert summary["frames"] == summary["received_frames"] == 1034
    assert summary["callback_count"] == 3
    assert summary["continuity_checked"] is summary["success"] is True
    assert summary["first_sample_time"] == 1000.0
    assert summary["last_sample_end"] == 2034.0
    assert summary["schema_version"] == "cole-voice-lab.capture-continuity/v1"
    assert summary["wav_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert summary["wav_size_bytes"] == output.stat().st_size
    assert summary["output_name"] == output.name
    assert json.loads(diagnostic.read_bytes()) == summary
    assert stat.S_IMODE(output.stat().st_mode) == stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    assert fake.calls[-2:] == ["stop", "close"]


@pytest.mark.parametrize("gap", [512, -512, 1, -1, 0.01])
def test_timestamp_gap_preserves_partial_and_reports_expected_actual(tmp_path, gap):
    stop = threading.Event()
    fake = FakeBackend(stop, gap=gap)
    with pytest.raises(native.NativeCaptureError, match="gap_frames="):
        run(tmp_path, fake=fake, stop=stop)
    evidence = json.loads((tmp_path / "continuity.json").read_bytes())
    assert evidence["success"] is False
    assert "expected=1512.000000" in evidence["error"]
    assert evidence["frames"] == 512
    assert fake.calls[-2:] == ["stop", "close"]


@pytest.mark.parametrize("stage", ["open", "start", "health", "stop", "close"])
def test_every_native_lifecycle_failure_still_stops_and_disposes(tmp_path, stage):
    stop = threading.Event()
    fake = FakeBackend(stop, failure=stage)
    with pytest.raises(native.NativeCaptureError, match=f"synthetic {stage} failure"):
        run(tmp_path, stop=stop, fake=fake)
    assert fake.calls[-2:] == ["stop", "close"]
    assert (tmp_path / "synthetic.partial.wav").exists()


@pytest.mark.parametrize(
    "payload,timestamp,flags",
    [(b"", 0, 1), (b"\0", 0, 1), (PCM, math.nan, 1), (PCM, math.inf, 1), (PCM, 0, 0)],
)
def test_invalid_native_buffer_evidence_fails_closed(payload, timestamp, flags):
    buffers = native._CaptureBuffers()
    with pytest.raises(native.NativeCaptureError):
        buffers.receive(payload, timestamp, flags)
    assert buffers.received_frames == 0


def test_buffer_handoff_is_bounded_and_failure_keeps_first_cause():
    buffers = native._CaptureBuffers(capacity=1)
    buffers.receive(PCM, 0, 1)
    with pytest.raises(native.NativeCaptureError, match="queue overflow") as caught:
        buffers.receive(PCM, 512, 1)
    buffers.fail(caught.value)
    buffers.fail(RuntimeError("secondary"))
    buffers.receive(PCM, 999, 1)
    assert buffers.failure is caught.value
    assert buffers.received_frames == 512


def test_existing_output_and_symlink_are_never_overwritten(tmp_path):
    output = tmp_path / "synthetic.partial.wav"
    output.write_bytes(b"original")
    with pytest.raises(SecurityError, match="exclusively"):
        run(tmp_path)
    assert output.read_bytes() == b"original"
    output.unlink()
    (tmp_path / "continuity.json").unlink()
    target = tmp_path / "target.wav"
    target.write_bytes(b"target")
    output.symlink_to(target)
    with pytest.raises(SecurityError):
        run(tmp_path)
    assert target.read_bytes() == b"target"


def test_diagnostic_collision_is_failure_and_preserves_existing_evidence(tmp_path):
    path = tmp_path / "continuity.json"
    path.write_bytes(b"original")
    with pytest.raises(native.NativeCaptureError, match="Cannot persist"):
        run(tmp_path)
    assert path.read_bytes() == b"original"


def test_control_pipe_error_is_reported_after_microphone_cleanup(tmp_path):
    with pytest.raises(native.NativeCaptureError, match="control EOF"):
        run(tmp_path, control_error=lambda: RuntimeError("control EOF"))


def test_interruption_during_native_stop_cannot_become_success(tmp_path):
    stop = threading.Event()
    errors = []

    class InterruptedStop(FakeBackend):
        def stop(self):
            super().stop()
            errors.append(RuntimeError("interrupt during stop"))

    fake = InterruptedStop(stop)
    with pytest.raises(native.NativeCaptureError, match="interrupt during stop"):
        run(tmp_path, stop=stop, fake=fake, control_error=lambda: errors[0] if errors else None)
    assert json.loads((tmp_path / "continuity.json").read_bytes())["success"] is False


def test_cancel_before_microphone_open_does_not_open_hardware(tmp_path):
    stop = threading.Event()
    stop.set()
    fake = FakeBackend(stop, tail=False)
    with pytest.raises(native.NativeCaptureError, match="before opening"):
        run(tmp_path, stop=stop, fake=fake)
    assert not any(isinstance(call, tuple) for call in fake.calls)


def test_native_output_parent_symlink_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(SecurityError, match="symlink component"):
        native.run_native_capture(
            output_path=alias / "a.partial.wav",
            device_uid=UID,
            stop_requested=threading.Event(),
            progress=io.BytesIO(),
            backend_factory=lambda: pytest.fail("must not open backend"),
        )
    assert not list(real.iterdir())


def test_no_samples_is_not_success(tmp_path):
    stop = threading.Event()
    fake = FakeBackend(stop, empty=True)
    with pytest.raises(native.NativeCaptureError, match="without complete"):
        run(tmp_path, stop=stop, fake=fake)


def test_startup_timeout_and_maximum_duration_are_independent_of_callback_clock(tmp_path):
    stop = threading.Event()
    fake = FakeBackend(stop, empty=True)
    clock = iter((0.0, 11.0))
    with pytest.raises(native.NativeCaptureError, match="startup timeout"):
        run(tmp_path, stop=stop, fake=fake, monotonic=lambda: next(clock))
    folder = tmp_path / "second"
    folder.mkdir()
    fake = FakeBackend(threading.Event(), tail=False)
    clock = iter((0.0, 601.0))
    summary, *_ = run(folder, fake=fake, monotonic=lambda: next(clock))
    assert summary["stop_reason"] == "maximum-duration"


@pytest.mark.parametrize("short", [False, True])
def test_progress_pipe_failure_never_publishes_success(tmp_path, short):
    class FailingProgress(io.BytesIO):
        def write(self, payload):
            if short:
                return 0
            raise BrokenPipeError("synthetic progress closed")

    with pytest.raises(native.NativeCaptureError, match="progress"):
        run(tmp_path, progress=FailingProgress())


class Function:
    def __init__(self, action):
        self.action = action

    def __call__(self, *arguments):
        return self.action(*arguments)


class FakeLibraries:
    """In-memory ABI implementation; pointers are genuine ctypes allocations."""

    def __init__(self):
        self.references = {100: UID, 101: "UMIK-1"}
        self.released = []
        self.allocations = []
        self.callback = None
        self.description = None
        self.running = False
        self.errors = {}
        self.property_overrides = {}
        self.empty_inputs = False
        self.core_properties = {
            (1, "dev#", "glob"): bytes((ctypes.c_uint32 * 2)(21, 22)),
            (21, "stm#", "inpt"): bytes(ctypes.c_uint32(90)),
            (22, "stm#", "inpt"): b"",
            (21, "uid ", "glob"): bytes(ctypes.c_void_p(100)),
            (21, "lnam", "glob"): bytes(ctypes.c_void_p(101)),
        }

    @staticmethod
    def selector(value):
        return value.to_bytes(4, "big").decode("ascii")

    def load(self, path):
        return self

    def __getattr__(self, name):
        return Function(lambda *args: self.dispatch(name, *args))

    def dispatch(self, name, *args):
        if name in self.errors:
            return self.errors[name]
        if name.startswith("AudioObjectGetProperty"):
            device, address, _, _, size, *destination = args
            address = address._obj
            key = (device, self.selector(address.selector), self.selector(address.scope))
            payload = self.core_properties[key]
            size._obj.value = len(payload)
            if destination:
                ctypes.memmove(destination[0], payload, len(payload))
        elif name == "CFRelease":
            self.released.append(args[0])
        elif name == "CFStringGetLength":
            return len(self.references[args[0]])
        elif name == "CFStringGetCString":
            payload = self.references[args[0]].encode() + b"\0"
            ctypes.memmove(args[1], payload, len(payload))
            return 1
        elif name == "CFStringCreateWithCString":
            self.references[102] = args[1].decode()
            return 102
        elif name == "AudioQueueNewInput":
            self.description = bytes(args[0]._obj)
            self.callback = args[1]
            args[-1]._obj.value = 77
        elif name == "AudioQueueGetProperty":
            _, selector, target, size = args
            key = self.selector(selector)
            values = {
                "aqcd": bytes(ctypes.c_void_p(100)),
                "aqft": self.description,
                "aqsr": bytes(ctypes.c_double(48000)),
                "qcve": bytes(ctypes.c_uint32(0)),
                "aqrn": bytes(ctypes.c_uint32(int(self.running))),
            }
            payload = self.property_overrides.get(key, values[key])
            ctypes.memmove(target, payload, len(payload))
            size._obj.value = len(payload)
        elif name == "AudioQueueAllocateBuffer":
            data = ctypes.create_string_buffer(args[1])
            buffer = native.AudioQueueBuffer(args[1], ctypes.addressof(data), 0, None, 0, None, 0)
            pointer = ctypes.pointer(buffer)
            ctypes.cast(args[2], ctypes.POINTER(native._BufferPointer))[0] = pointer
            self.allocations.append((buffer, data, pointer))
        elif name == "AudioQueueStart":
            self.running = True
        elif name in {"AudioQueueStop", "AudioQueueDispose"}:
            self.running = False
        return 0

    def emit(self, payload=PCM, timestamp=10.0, flags=1, *, capacity=None):
        buffer, data, pointer = self.allocations[0]
        ctypes.memmove(data, payload, min(len(payload), len(data)))
        buffer.byte_size = len(payload)
        if capacity is not None:
            buffer.capacity = capacity
        stamp = native.AudioTimeStamp()
        stamp.sample_time = timestamp
        stamp.flags = flags
        self.callback(None, 77, pointer, ctypes.pointer(stamp), 0, None)


@pytest.fixture
def libraries(monkeypatch):
    fake = FakeLibraries()
    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native.ctypes, "CDLL", fake.load)
    return fake


def test_hal_discovery_returns_only_input_devices_with_owned_strings(libraries):
    devices = native.list_coreaudio_input_devices()
    assert len(devices) == 1
    assert (devices[0].backend, devices[0].device_id, devices[0].display_name) == (
        "coreaudio",
        UID,
        "UMIK-1",
    )
    assert libraries.released == [100, 101]


@pytest.mark.serial
def test_real_bridge_abi_allocates_multiple_buffers_and_delivers_continuous_samples(libraries):
    backend = native._AudioQueueBackend()
    buffers = native._CaptureBuffers()
    backend.open(UID, buffers.receive, buffers.fail)
    assert len(libraries.allocations) == native.BUFFER_COUNT
    assert all(buffer.capacity == 4096 * 3 for buffer, *_ in libraries.allocations)
    backend.start()
    libraries.emit()
    libraries.emit(timestamp=522.0)
    backend.health()
    backend.stop()
    libraries.emit(payload=PCM[:30], timestamp=1034.0)
    backend.close()
    backend.close()
    assert buffers.received_frames == 1034
    assert backend.callback is None
    assert backend.reference.value is None
    assert buffers.failure is None


@pytest.mark.parametrize("scenario", ["timestamp", "capacity", "empty", "null"])
def test_native_callback_never_raises_through_c_boundary(libraries, scenario):
    backend = native._AudioQueueBackend()
    buffers = native._CaptureBuffers()
    backend.open(UID, buffers.receive, buffers.fail)
    if scenario == "timestamp":
        libraries.emit(flags=0)
    elif scenario == "capacity":
        libraries.emit(capacity=1)
    elif scenario == "null":
        libraries.callback(None, 77, None, None, 0, None)
    else:
        libraries.emit(payload=b"")
        assert buffers.failure is None
        backend.close()
        return
    assert isinstance(buffers.failure, native.NativeCaptureError)
    assert backend.stopping
    backend.close()


@pytest.mark.parametrize(
    "property_name,payload,match",
    [
        ("aqsr", bytes(ctypes.c_double(44100)), "48000"),
        ("aqft", bytes(native.AudioStreamBasicDescription()), "canonical"),
        ("aqcd", bytes(ctypes.c_void_p()), "lost"),
    ],
)
def test_open_rejects_wrong_native_format_or_device(libraries, property_name, payload, match):
    libraries.property_overrides[property_name] = payload
    backend = native._AudioQueueBackend()
    with pytest.raises(native.NativeCaptureError, match=match):
        backend.open(UID, lambda *_: None, lambda _: None)
    backend.close()


def test_health_detects_converter_failure_and_unexpected_stop(libraries):
    backend = native._AudioQueueBackend()
    backend.open(UID, lambda *_: None, lambda _: None)
    backend.start()
    libraries.emit()
    libraries.property_overrides["qcve"] = bytes(ctypes.c_uint32(123))
    with pytest.raises(native.NativeCaptureError, match="converter"):
        backend.health()
    libraries.property_overrides.pop("qcve")
    libraries.running = False
    with pytest.raises(native.NativeCaptureError, match="unexpectedly"):
        backend.health()
    backend.close()


def test_health_detects_mid_take_rate_change(libraries):
    backend = native._AudioQueueBackend()
    backend.open(UID, lambda *_: None, lambda _: None)
    backend.start()
    libraries.property_overrides["aqsr"] = bytes(ctypes.c_double(44100))
    with pytest.raises(native.NativeCaptureError, match="rate changed"):
        backend.health()
    backend.close()


def test_direct_worker_rejects_non_umik_uid(libraries):
    libraries.references[101] = "Built-in microphone"
    backend = native._AudioQueueBackend()
    with pytest.raises(native.NativeCaptureError, match="UMIK-1"):
        backend.open(UID, lambda *_: None, lambda _: None)


@pytest.mark.parametrize("payload", [b"bad", bytes(ctypes.c_void_p())])
def test_coreaudio_string_pointer_shape_is_checked(libraries, payload):
    libraries.core_properties[(21, "uid ", "glob")] = payload
    with pytest.raises(native.NativeCaptureError):
        native.list_coreaudio_input_devices()


def test_coreaudio_string_bounds_encoding_and_null_are_checked(libraries):
    core = native._CoreAudio()
    with pytest.raises(native.NativeCaptureError, match="null"):
        core.string(0)
    libraries.references[100] = "x" * 4097
    with pytest.raises(native.NativeCaptureError, match="bounded"):
        core.string(100)
    libraries.references[100] = UID
    libraries.errors["CFStringGetCString"] = 0
    with pytest.raises(native.NativeCaptureError, match="UTF-8"):
        core.string(100)


def test_duplicate_and_malformed_hal_device_lists_are_rejected(libraries):
    libraries.core_properties[(1, "dev#", "glob")] = bytes((ctypes.c_uint32 * 2)(21, 21))
    with pytest.raises(native.NativeCaptureError, match="duplicate"):
        native.list_coreaudio_input_devices()
    libraries.core_properties[(1, "dev#", "glob")] = b"x"
    with pytest.raises(native.NativeCaptureError, match="byte count"):
        native.list_coreaudio_input_devices()


def test_native_queue_null_allocations_are_explicit(libraries):
    backend = native._AudioQueueBackend()
    libraries.errors["AudioQueueAllocateBuffer"] = 0
    with pytest.raises(native.NativeCaptureError, match="null input buffer"):
        backend.open(UID, lambda *_: None, lambda _: None)
    backend.close()


def test_native_queue_null_handle_and_cf_allocation_failures_are_explicit(libraries):
    backend = native._AudioQueueBackend()
    libraries.errors["AudioQueueNewInput"] = 0
    with pytest.raises(native.NativeCaptureError, match="null input queue"):
        backend.open(UID, lambda *_: None, lambda _: None)
    libraries.errors.pop("AudioQueueNewInput")
    libraries.errors["CFStringCreateWithCString"] = 0
    with pytest.raises(native.NativeCaptureError, match="UID string"):
        backend.open(UID, lambda *_: None, lambda _: None)
    backend.close()


def test_queue_uid_switch_and_property_size_change_are_not_hidden(libraries):
    backend = native._AudioQueueBackend()
    backend.open(UID, lambda *_: None, lambda _: None)
    libraries.references[100] = "changed"
    with pytest.raises(native.NativeCaptureError, match="changed microphone"):
        backend.health()
    libraries.references[100] = UID
    libraries.property_overrides["aqcd"] = b""
    with pytest.raises(native.NativeCaptureError, match="unexpected size"):
        backend.health()
    backend.close()


def test_wav_binding_rejects_replacement_permissions_and_hardlinks(tmp_path):
    path = tmp_path / "partial.wav"
    path.write_bytes(PCM)
    path.chmod(0o600)
    metadata = path.stat()
    with pytest.raises(SecurityError, match="replaced"):
        native._wav_evidence(path, (metadata.st_dev, metadata.st_ino + 1))
    path.chmod(0o644)
    with pytest.raises(SecurityError, match="private"):
        native._wav_evidence(path)
    path.chmod(0o600)
    os.link(path, tmp_path / "alias.wav")
    with pytest.raises(SecurityError, match="hard links"):
        native._wav_evidence(path)


def test_successful_capture_becomes_failure_if_digest_binding_fails(tmp_path, monkeypatch):
    def fail_binding(*_args):
        raise SecurityError("synthetic changed output")

    monkeypatch.setattr(native, "_wav_evidence", fail_binding)
    with pytest.raises(native.NativeCaptureError, match="Cannot bind captured WAV"):
        run(tmp_path)
    summary = json.loads((tmp_path / "continuity.json").read_bytes())
    assert summary["success"] is False
    assert "synthetic changed output" in summary["error"]


def test_worker_reports_signals_received_during_evidence_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(native.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"q")))
    monkeypatch.setattr(native.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))

    def interrupted_run(**kwargs):
        kwargs["stop_requested"].wait(1)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return {"frames": 10}

    monkeypatch.setattr(native, "run_native_capture", interrupted_run)
    assert native.main(["--device-uid", UID, "--output", str(tmp_path / "partial.wav")]) == 1
    assert "signal" in capsys.readouterr().err


def test_missing_device_and_native_osstatus_are_explicit(libraries):
    backend = native._AudioQueueBackend()
    with pytest.raises(native.NativeCaptureError, match="unavailable"):
        backend.open("absent", lambda *_: None, lambda _: None)
    libraries.errors["AudioObjectGetPropertyDataSize"] = -50
    with pytest.raises(native.NativeCaptureError, match="OSStatus -50"):
        native.list_coreaudio_input_devices()


def test_native_frameworks_are_lazy_and_platform_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")
    with pytest.raises(native.NativeCaptureError, match="macOS"):
        native.list_coreaudio_input_devices()
    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native.ctypes, "CDLL", lambda _: (_ for _ in ()).throw(OSError("missing")))
    with pytest.raises(native.NativeCaptureError, match="frameworks"):
        native.list_coreaudio_input_devices()


@pytest.mark.parametrize("control", [b"q", b"", b"xq"])
def test_worker_cli_control_and_summary_stay_separate_from_pcm(
    tmp_path, monkeypatch, capsys, control
):
    monkeypatch.setattr(native.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(control)))
    monkeypatch.setattr(native.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))

    def fake_run(**kwargs):
        kwargs["stop_requested"].wait(1)
        error = kwargs["control_error"]()
        if error:
            raise error
        return {"frames": 10, "continuity_checked": True}

    monkeypatch.setattr(native, "run_native_capture", fake_run)
    previous = signal.getsignal(signal.SIGINT)
    result = native.main(["--device-uid", UID, "--output", str(tmp_path / "a.wav")])
    assert result == (0 if control else 1)
    stderr = capsys.readouterr().err
    assert (native.SUMMARY_PREFIX in stderr) == bool(control)
    assert signal.getsignal(signal.SIGINT) is previous

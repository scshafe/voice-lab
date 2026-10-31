"""The PortAudio capture worker on the simulated UMIK-1: in process, and as the real
subprocess the app starts (D-039). No audio device is ever opened."""

from __future__ import annotations

import io
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from voice_lab_desktop.capture import process as process_module
from voice_lab_desktop.capture import worker as worker_module
from voice_lab_desktop.capture.backends import SimulatedSpec, SimulatedTimedBackend
from voice_lab_desktop.capture.evidence import read_evidence
from voice_lab_desktop.capture.process import WorkerRecorder, worker_command
from voice_lab_desktop.capture.timing import BlockTiming, ContinuityTracker
from voice_lab_desktop.capture.types import (
    EVIDENCE_NAME,
    ORIGINAL_NAME,
    PARTIAL_NAME,
    CaptureFailed,
)
from voice_lab_desktop.capture.worker import (
    SUMMARY_PREFIX,
    CaptureBuffers,
    CaptureError,
    Settings,
    parse_settings,
    run_capture,
)
from voice_lab_desktop.measurement.devices import DeviceRefused

from cole_voice_lab.audio import inspect_canonical_wav

FAST = {"speed": 100}


def settings(take: Path, **changes) -> Settings:
    take.mkdir(mode=0o700, exist_ok=True)
    return Settings(
        output=take / PARTIAL_NAME, evidence=take / EVIDENCE_NAME, **{"max_seconds": 0.2, **changes}
    )


class StopAtBlock(SimulatedTimedBackend):
    """Requests the stop from the stream's callback once ``block`` blocks arrived: never before
    the microphone opened, however loaded the host (a timer started first can fire before)."""

    def __init__(self, spec: SimulatedSpec, *, block: int, stop: threading.Event) -> None:
        super().__init__(spec)
        self.block, self.stop = block, stop

    def open_timed_input(self, device, *, block_frames, callback):
        received = itertools.count(1)

        def receive(data, timing):
            callback(data, timing)
            if next(received) == self.block:
                self.stop.set()

        return super().open_timed_input(device, block_frames=block_frames, callback=receive)


def run(take: Path, spec: dict | None = None, *, stop_at_block: int | None = None, **changes):
    """``run_capture`` in this process; returns (summary or CaptureError, progress bytes)."""
    stop = threading.Event()
    progress = io.BytesIO()
    simulated = SimulatedSpec.from_json({**FAST, **(spec or {})})
    if stop_at_block is None:
        backend = SimulatedTimedBackend(simulated)
    else:
        backend = StopAtBlock(simulated, block=stop_at_block, stop=stop)
    try:
        result = run_capture(
            settings(take, **changes), backend=backend, stop_requested=stop, progress=progress
        )
    except CaptureError as error:
        result = error
    return result, progress.getvalue()


def confirmed(progress: bytes) -> int:
    return sum(int(line) for line in progress.splitlines()) // 3


def test_a_take_stops_at_its_maximum_duration_with_evidence(tmp_path):
    take = tmp_path / "take"
    summary, progress = run(take, max_seconds=0.2, trace=take / "trace.jsonl")
    assert isinstance(summary, dict) and summary["success"] is True
    assert summary["stop_reason"] == "maximum-duration"
    frames = summary["frames"]
    assert frames >= 0.2 * 48_000 and frames % 1024 == 0
    assert confirmed(progress) == frames
    inspection = inspect_canonical_wav(take / PARTIAL_NAME)
    assert inspection.audio.frames == frames and inspection.size_bytes == 44 + frames * 3
    evidence = read_evidence(take / EVIDENCE_NAME)
    assert evidence.success and evidence.simulated and evidence.wav_sha256 == inspection.sha256
    assert evidence.max_abs_deviation_frames == pytest.approx(0.0, abs=1e-3)
    assert evidence.device == "Umik-1  Gain: 18dB [Core Audio]"
    trace = [json.loads(line) for line in (take / "trace.jsonl").read_text().splitlines()]
    assert [record["index"] for record in trace] == list(range(len(trace)))
    assert sum(record["frames"] for record in trace) == frames
    times = summary["monotonic"]
    assert times["started"] <= times["first_block"] <= times["finished"]
    # Two kernel counters on Linux (/proc/self/statm before, ru_maxrss after), each
    # approximate and read at different moments: the peak can read a few pages below the
    # sample (a hosted runner's kernel showed 311 KB), never far below it.
    assert summary["rss_before_bytes"] > 0
    assert summary["max_rss_bytes"] >= summary["rss_before_bytes"] - 4 * 1024 * 1024


def test_a_requested_stop_keeps_every_block_received(tmp_path):
    summary, progress = run(tmp_path / "take", stop_at_block=8, max_seconds=60)
    assert isinstance(summary, dict) and summary["stop_reason"] == "requested"
    assert summary["frames"] == summary["received_frames"] == confirmed(progress) > 0


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"gap_at_block": 3}, "block 3: discontinuity"),
        ({"overflow_at_block": 2}, "block 2: PortAudio reported an input overflow"),
        ({"underflow_at_block": 2}, "input underflow"),
        ({"zero_adc_at_block": 1}, "block 1: PortAudio gave no ADC time"),
        ({"jitter_frames": 40}, "tolerance ±64 frames"),
        ({"stop_after_blocks": 4}, "stopped by itself"),
    ],
)
def test_faults_fail_the_take_and_keep_the_partial(tmp_path, spec, message):
    take = tmp_path / "take"
    error, progress = run(take, spec, max_seconds=5)
    assert isinstance(error, CaptureError) and message in str(error)
    evidence = read_evidence(take / EVIDENCE_NAME)
    assert not evidence.success and evidence.stop_reason is None and message in evidence.error
    assert (take / PARTIAL_NAME).exists()
    # What was written before the fault is a complete WAV of exactly those frames.
    assert inspect_canonical_wav(take / PARTIAL_NAME).audio.frames == evidence.frames
    assert confirmed(progress) == evidence.frames
    assert error.summary is not None and error.summary["success"] is False


def test_jitter_within_the_tolerance_is_a_good_take(tmp_path):
    summary, _ = run(tmp_path / "take", {"jitter_frames": 20}, max_seconds=0.1)
    assert isinstance(summary, dict) and summary["success"]
    assert summary["max_abs_deviation_frames"] == pytest.approx(40.0, abs=1e-3)


def test_a_full_queue_fails_rather_than_drop_audio(tmp_path):
    take = tmp_path / "take"
    error, _ = run(take, {"speed": 0, "writer_delay": 0.05}, queue_blocks=1, writer_delay=0.05)
    assert isinstance(error, CaptureError) and "fell 1 blocks behind" in str(error)
    evidence = read_evidence(take / EVIDENCE_NAME)
    assert evidence.queue_capacity == 1 and not evidence.success


class _Watched(queue.Queue):
    """A queue that says when the writer first asks it for a block."""

    def __init__(self, capacity: int) -> None:
        super().__init__(capacity)
        self.asked = threading.Event()

    def get(self, block=True, timeout=None):
        self.asked.set()
        return super().get(block, timeout)


def _write_in_thread(buffers: CaptureBuffers, path: Path) -> tuple[threading.Thread, io.BytesIO]:
    progress = io.BytesIO()
    stream = path.open("wb")
    writer = threading.Thread(
        target=lambda: (buffers.write(stream, progress, None), stream.close()), daemon=True
    )
    writer.start()
    return writer, progress


@pytest.mark.parametrize("queued", [0, 1])
def test_the_writer_ends_at_the_stop_not_at_its_next_poll(tmp_path, monkeypatch, queued):
    """``finish`` wakes a writer waiting for a block (its poll would add up to 50 ms to every
    stop); with a full queue there is no room to wake it, and it drains the queue first."""
    monkeypatch.setattr(worker_module, "WRITER_POLL_SECONDS", 60.0)
    buffers = CaptureBuffers(ContinuityTracker(), capacity=1)
    buffers.pending = _Watched(1)
    for _ in range(queued):
        buffers.receive(bytes(16 * 3), BlockTiming(frames=16, adc_time=1.0, current_time=1.01))
    if queued:
        buffers.finish()  # the queue is full: no wake-up fits
        writer, progress = _write_in_thread(buffers, tmp_path / "take.wav")
    else:
        writer, progress = _write_in_thread(buffers, tmp_path / "take.wav")
        assert buffers.pending.asked.wait(10)
        buffers.finish()
    writer.join(timeout=10)
    assert not writer.is_alive()
    assert buffers.failure is None and buffers.written_frames == 16 * queued
    assert progress.getvalue() == b"48\n" * queued


def test_a_device_that_never_delivers_times_out(tmp_path):
    error, _ = run(tmp_path / "take", {"stall": True}, startup_timeout=0.1, max_seconds=5)
    assert isinstance(error, CaptureError) and "delivered no audio within 0.1 s" in str(error)


@pytest.mark.parametrize(
    ("devices", "match"),
    [
        (["built-in"], "No UMIK-1 found"),
        (["umik-2", "built-in"], "'UMIK-2' is not a UMIK-1"),
    ],
)
def test_anything_but_a_umik_1_is_refused_before_a_file_exists(tmp_path, devices, match):
    take = tmp_path / "take"
    with pytest.raises(DeviceRefused, match=match):
        run(take, {"devices": devices})
    assert list(take.iterdir()) == []


def test_a_stop_before_the_stream_opens_and_a_control_failure_fail(tmp_path):
    take = tmp_path / "early"
    stop = threading.Event()
    stop.set()
    backend = SimulatedTimedBackend(SimulatedSpec.from_json(FAST))
    with pytest.raises(CaptureError, match="stopped before the microphone opened"):
        run_capture(settings(take), backend=backend, stop_requested=stop, progress=io.BytesIO())

    take = tmp_path / "control"
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    with pytest.raises(CaptureError, match="the control pipe closed"):
        run_capture(
            settings(take, max_seconds=30),
            backend=SimulatedTimedBackend(SimulatedSpec.from_json(FAST)),
            stop_requested=stop,
            progress=io.BytesIO(),
            control_error=lambda: (
                CaptureError("the control pipe closed") if stop.is_set() else None
            ),
        )


def test_an_existing_partial_is_never_overwritten(tmp_path):
    take = tmp_path / "take"
    take.mkdir()
    (take / PARTIAL_NAME).write_bytes(b"older")
    with pytest.raises(FileExistsError):
        run(take)
    assert (take / PARTIAL_NAME).read_bytes() == b"older"


def test_the_worker_arguments_are_checked():
    good, spec = parse_settings(
        ["--output", "a.wav", "--evidence", "e.json", "--simulated", '{"speed": 0}']
    )
    assert good.block_frames == 1024 and spec is not None and spec.speed == 0
    for bad in (
        ["--max-seconds", "601"],
        ["--block-frames", "8"],
        ["--tolerance-frames", "0"],
        ["--queue-blocks", "0"],
        ["--startup-timeout", "0"],
        ["--simulated", '{"nope": 1}'],
        ["--simulated", "[1]"],
        ["--simulated", '{"speed": -1}'],
        ["--simulated", '{"devices": ["mystery"]}'],
    ):
        with pytest.raises(SystemExit):
            parse_settings(["--output", "a.wav", "--evidence", "e.json", *bad])


# --- the real subprocess ------------------------------------------------------------------


def private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def wait_for(take, frames: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while take.confirmed_frames < frames and not take.done:
        assert time.monotonic() < deadline, "the worker produced no audio"
        time.sleep(0.005)


def test_a_take_through_the_worker_process(tmp_path):
    recorder = WorkerRecorder(simulated=FAST, trace=True)
    assert recorder.device_label() == "Umik-1  Gain: 18dB [Core Audio]"
    take_dir = private(tmp_path / "take")
    take = recorder.start(take_dir)
    wait_for(take, 9600)
    take.stop()
    take.stop()  # idempotent
    captured = take.wait(timeout=30)
    assert captured.path == take_dir / ORIGINAL_NAME and captured.frames >= 9600
    assert captured.frames == take.confirmed_frames
    assert captured.device == "Umik-1  Gain: 18dB [Core Audio]"
    assert take.wait() is captured  # the result is kept
    assert sorted(path.name for path in take_dir.iterdir()) == [
        EVIDENCE_NAME,
        ORIGINAL_NAME,
        "trace.jsonl",
    ]
    times = take.times
    assert times["requested"] < times["first_frames"] < times["stop_sent"] < times["validated"]
    worker_times = take.summary["monotonic"]
    assert worker_times["stop_read"] >= times["stop_sent"]


def test_a_worker_take_that_reaches_its_maximum_duration(tmp_path):
    take = WorkerRecorder(simulated=FAST, max_seconds=0.1).start(private(tmp_path / "take"))
    captured = take.wait(timeout=30)
    assert captured.frames >= 4800 and take.summary["stop_reason"] == "maximum-duration"


@pytest.mark.parametrize(
    ("spec", "queue_blocks", "match"),
    [
        # The device's defects, with the writer's usual queue: a loaded host's late writer
        # must not fail the take first (one block of queue at 100x speed did, once).
        ({"gap_at_block": 2}, None, "block 2: discontinuity"),
        ({"overflow_at_block": 1}, None, "input overflow"),
        ({"zero_adc_at_block": 1}, None, "no ADC time"),
        # The writer's own lag, with a queue of one block.
        ({"speed": 0, "writer_delay": 0.05}, 1, "fell 1 blocks behind"),
    ],
)
def test_a_failed_worker_take_is_kept_not_published(tmp_path, spec, queue_blocks, match):
    take_dir = private(tmp_path / "take")
    recorder = WorkerRecorder(simulated={**FAST, **spec}, queue_blocks=queue_blocks, max_seconds=5)
    take = recorder.start(take_dir)
    with pytest.raises(CaptureFailed, match=match) as caught:
        take.wait(timeout=30)
    assert caught.value.partial == take_dir / PARTIAL_NAME
    assert caught.value.evidence == take_dir / EVIDENCE_NAME
    assert not (take_dir / ORIGINAL_NAME).exists()
    with pytest.raises(CaptureFailed):
        take.wait()  # the failure is kept


def test_eof_on_the_control_pipe_fails_closed(tmp_path):
    take_dir = private(tmp_path / "take")
    take = WorkerRecorder(simulated=FAST, max_seconds=30).start(take_dir)
    wait_for(take, 1024)
    take.process.stdin.close()
    with pytest.raises(CaptureFailed, match="control pipe closed before a stop"):
        take.wait(timeout=30)
    assert not read_evidence(take_dir / EVIDENCE_NAME).success


def test_a_worker_without_a_umik_1_fails_before_any_file(tmp_path):
    recorder = WorkerRecorder(simulated={"devices": ["built-in"]})
    with pytest.raises(DeviceRefused):
        recorder.device_label()
    take_dir = private(tmp_path / "take")
    with pytest.raises(CaptureFailed, match="No UMIK-1 found") as caught:
        recorder.start(take_dir).wait(timeout=30)
    assert caught.value.partial is None and caught.value.evidence is None
    assert list(take_dir.iterdir()) == []


def test_a_killed_worker_and_a_slow_one(tmp_path):
    take = WorkerRecorder(simulated=FAST, max_seconds=30).start(private(tmp_path / "killed"))
    wait_for(take, 1024)
    with pytest.raises(TimeoutError):
        take.wait(timeout=0.01)
    assert not take.done
    take.kill()
    with pytest.raises(CaptureFailed, match="the take was not kept"):
        take.wait(timeout=30)
    assert take.done
    take.stop()  # a stop after the end is harmless


def _fake_worker(monkeypatch, script: str) -> None:
    monkeypatch.setattr(
        process_module,
        "worker_command",
        lambda arguments: [sys.executable, "-I", "-B", "-c", script, *arguments],
    )


@pytest.mark.parametrize(
    ("script", "match"),
    [
        (
            "import sys; print('hello', flush=True); sys.stdin.read()",
            "wrote b'hello\\\\n'",
        ),
        (
            "import sys; print('4', flush=True); sys.stdin.read()",
            "wrote b'4\\\\n' as progress",
        ),
        ("raise SystemExit(0)", "the worker reported no result"),
        ("import sys; print('broken', file=sys.stderr); raise SystemExit(3)", "broken"),
        ("raise SystemExit(5)", "exit status 5"),
        (
            "import sys; print('" + SUMMARY_PREFIX + "{not json', file=sys.stderr); "
            "raise SystemExit(0)",
            "reported no result",
        ),
    ],
)
def test_a_worker_that_breaks_the_protocol_fails(tmp_path, monkeypatch, script, match):
    _fake_worker(monkeypatch, script)
    take = WorkerRecorder(simulated=FAST).start(private(tmp_path / "take"))
    with pytest.raises(CaptureFailed, match=match):
        take.wait(timeout=30)


def test_a_worker_claiming_success_without_evidence_fails(tmp_path, monkeypatch):
    summary = SUMMARY_PREFIX + json.dumps({"success": True})
    _fake_worker(monkeypatch, f"import sys; print({summary!r}, file=sys.stderr)")
    take = WorkerRecorder(simulated=FAST).start(private(tmp_path / "take"))
    with pytest.raises(CaptureFailed, match="continuity.json is missing"):
        take.wait(timeout=30)


def test_a_worker_that_cannot_start(tmp_path, monkeypatch):
    monkeypatch.setattr(process_module, "worker_command", lambda _a: [str(tmp_path / "nothing")])
    with pytest.raises(CaptureFailed, match="did not start"):
        WorkerRecorder(simulated=FAST).start(private(tmp_path / "take"))


def test_the_command_runs_this_package_or_the_frozen_app(monkeypatch):
    command = worker_command(["--output", "x"])
    assert command[:4] == [sys.executable, "-I", "-B", "-c"]
    assert Path(command[5], "voice_lab_desktop", "capture", "worker.py").is_file()
    assert command[-2:] == ["--output", "x"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen = worker_command(["--output", "x"])
    assert frozen == [sys.executable, "--capture-worker", "--output", "x"]


def test_the_app_entry_point_runs_the_worker(tmp_path):
    """``voice-lab-desktop --capture-worker`` (the frozen bundle's route) is the worker."""
    take_dir = private(tmp_path / "take")
    package_root = Path(process_module.__file__).resolve().parents[2]
    script = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from voice_lab_desktop.cli import main; raise SystemExit(main(sys.argv[1:]))"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            str(package_root),
            "--capture-worker",
            *WorkerRecorder(simulated=FAST, max_seconds=0.05).arguments(take_dir),
        ],
        input=b"",
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    # stdin is at EOF from the start: the take fails closed, with its evidence.
    assert completed.returncode == 1
    assert SUMMARY_PREFIX in completed.stderr.decode()
    assert (take_dir / EVIDENCE_NAME).exists()

from __future__ import annotations

import io
import json
import signal
import stat
import subprocess
import wave
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import pytest

import cole_voice_lab.capture as capture_module
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.capture import (
    READY_NAME,
    AvfoundationDevice,
    CaptureInterrupted,
    CaptureProgressError,
    ReadyMarker,
    RejectedTakeEntry,
    RejectedTakeLog,
    TakeEngine,
    capture_prompt_suite,
    expected_recording_paths,
    ffmpeg_capture_command,
    finalize_capture_session,
    list_avfoundation_audio_devices,
    open_session,
    select_umik1_device,
)
from cole_voice_lab.errors import ContractError, ExternalToolError, SecurityError
from cole_voice_lab.models import CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout

NOW = datetime(2026, 8, 17, 19, 0, tzinfo=UTC)


class _ManualTimer:
    def __init__(self, interval: float, callback: Callable[[], None]) -> None:
        self.interval = interval
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        assert self.started
        assert not self.cancelled
        self.callback()


class _ManualTimerFactory:
    def __init__(self) -> None:
        self.timers: list[_ManualTimer] = []

    def __call__(self, interval: float, callback: Callable[[], None]) -> _ManualTimer:
        timer = _ManualTimer(interval, callback)
        self.timers.append(timer)
        return timer


class _ControlledProcess:
    def __init__(
        self,
        output_path: Path,
        *,
        on_quit: Callable[[Path], None],
        wait_results: Sequence[int | BaseException] = (0,),
    ) -> None:
        self.output_path = output_path
        self.on_quit = on_quit
        self.wait_results = list(wait_results)
        self.quit_signals = 0
        self.signals: list[int] = []
        self.wait_timeouts: list[float | None] = []
        self.terminated = False
        self.killed = False
        self.return_code: int | None = None
        self.captured_frames: int | None = None

    def signal_quit(self) -> None:
        self.quit_signals += 1
        self.on_quit(self.output_path)

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        result = self.wait_results.pop(0) if self.wait_results else 0
        if isinstance(result, BaseException):
            raise result
        self.return_code = result
        return result

    def poll(self) -> int | None:
        return self.return_code

    def send_signal(self, signal_number: int) -> None:
        self.signals.append(signal_number)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _write_wav(path: Path, *, quiet: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    samples = [100, -100] * 480 if quiet else [2_000_000, -2_000_000] * 480
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(3)
        writer.setframerate(48_000)
        writer.writeframes(b"".join(value.to_bytes(3, "little", signed=True) for value in samples))


def _suite(*, text: str = "Cole reads this exact sentence.") -> PromptSuite:
    return PromptSuite(
        suite_id="pilot-v1",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="room-tone-01",
                text="Remain silent for room tone.",
                category="room-tone",
                style="Still and silent",
                expected_seconds=10.0,
            ),
            Prompt(
                prompt_id="neutral-01",
                text=text,
                category="neutral",
                style="Natural speaking voice",
                expected_seconds=10.0,
            ),
        ),
    )


def _device() -> CaptureDevice:
    return CaptureDevice(
        device_id="4",
        display_name="miniDSP UMIK-1",
        microphone_serial="1234567",
    )


def _engine(
    layout: AssetLayout,
    *,
    session_id: str,
    process_factory: Callable[[Sequence[str]], _ControlledProcess],
    timers: _ManualTimerFactory | None = None,
    resume: bool = False,
) -> TakeEngine:
    return TakeEngine(
        layout=layout,
        prompt_suite=_suite(),
        subject_id="cole",
        device=_device(),
        session_id=session_id,
        ffmpeg="/test/ffmpeg",
        process_factory=process_factory,
        clock=lambda: NOW,
        capture_host="coles-macbook-pro",
        timer_factory=timers or _ManualTimerFactory(),
        resume=resume,
    )


def _make_unfinalized_session(path: Path, suite: PromptSuite) -> None:
    path.mkdir(mode=0o700, parents=True)
    for index, relative in enumerate(expected_recording_paths(suite)):
        _write_wav(path / relative, quiet=index == 0)


def test_avfoundation_device_discovery_and_umik_selection() -> None:
    stderr = """
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x1] [4] miniDSP UMIK-1
"""

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=stderr)

    devices = list_avfoundation_audio_devices(runner=runner)

    assert devices == (
        AvfoundationDevice(0, "MacBook Pro Microphone"),
        AvfoundationDevice(4, "miniDSP UMIK-1"),
    )
    assert select_umik1_device(devices).device_id == "4"
    assert select_umik1_device(devices, requested="miniDSP UMIK-1").index == 4


def test_capture_command_is_unprocessed_48k_24bit_pcm(tmp_path: Path) -> None:
    command = ffmpeg_capture_command(
        output_path=tmp_path / "utterance.partial.wav",
        device_id="4",
    )

    assert ("-ac", "1") == command[command.index("-ac") : command.index("-ac") + 2]
    assert ("-ar", "48000") == command[command.index("-ar") : command.index("-ar") + 2]
    assert ("-c:a", "pcm_s24le") == command[command.index("-c:a") : command.index("-c:a") + 2]
    assert "-af" not in command
    assert "-filter:a" not in command
    assert command[-2] == "-n"


def test_foreground_capture_prints_exact_prompt_and_finalizes_ready(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    suite = _suite()
    emitted: list[str] = []

    class FinishedProcess:
        def wait(self, timeout: float | None = None) -> int:
            return 0

        def poll(self) -> int | None:
            return 0

        def send_signal(self, signal_number: int) -> None:
            raise AssertionError(signal_number)

        def terminate(self) -> None:
            raise AssertionError

        def kill(self) -> None:
            raise AssertionError

    def factory(command: Sequence[str]) -> FinishedProcess:
        _write_wav(Path(command[-1]), quiet="room-tone" in command[-1])
        return FinishedProcess()

    result = capture_prompt_suite(
        layout=layout,
        prompt_suite=suite,
        subject_id="cole",
        device=_device(),
        session_id="pilot-001",
        read_line=lambda _: "",
        emit=emitted.append,
        process_factory=factory,
        clock=lambda: NOW,
        capture_host="coles-macbook-pro",
    )

    assert suite.prompts[1].text in emitted
    assert result.manifest.files[1].transcript == suite.prompts[1].text
    assert result.manifest.processing_applied == ()
    assert (result.session_directory / READY_NAME).is_file()
    assert (result.session_directory / "manifest.json").read_bytes() == (
        canonical_json_bytes(result.manifest) + b"\n"
    )


def test_incomplete_session_never_gets_ready(tmp_path: Path) -> None:
    suite = _suite()
    session = tmp_path / "private" / "sessions" / "pilot-002"
    session.mkdir(mode=0o700, parents=True)
    _write_wav(session / expected_recording_paths(suite)[0], quiet=True)

    with pytest.raises(ContractError, match="incomplete"):
        finalize_capture_session(
            session,
            session_id="pilot-002",
            subject_id="cole",
            prompt_suite=suite,
            device=_device(),
            captured_at=NOW,
            finalized_at=NOW,
            capture_host="coles-macbook-pro",
        )

    assert not (session / READY_NAME).exists()


def test_sigint_preserves_partial_and_never_publishes_ready(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")

    class InterruptedProcess:
        def __init__(self) -> None:
            self.signals: list[int] = []
            self.waits = 0

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 130

        def poll(self) -> int | None:
            return None

        def send_signal(self, signal_number: int) -> None:
            self.signals.append(signal_number)

        def terminate(self) -> None:
            raise AssertionError

        def kill(self) -> None:
            raise AssertionError

    process = InterruptedProcess()

    def factory(command: Sequence[str]) -> InterruptedProcess:
        _write_wav(Path(command[-1]), quiet=True)
        return process

    with pytest.raises(CaptureInterrupted) as caught:
        capture_prompt_suite(
            layout=layout,
            prompt_suite=_suite(),
            subject_id="cole",
            device=_device(),
            session_id="pilot-003",
            read_line=lambda _: "",
            emit=lambda _: None,
            process_factory=factory,
            clock=lambda: NOW,
        )

    assert process.signals == [signal.SIGINT]
    assert caught.value.partial_path.exists()
    assert not (caught.value.session_directory / READY_NAME).exists()


def test_umik_is_required_for_production_capture(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    wrong_device = CaptureDevice(device_id="0", display_name="MacBook Pro Microphone")

    with pytest.raises(ContractError, match="UMIK-1"):
        capture_prompt_suite(
            layout=layout,
            prompt_suite=_suite(),
            subject_id="cole",
            device=wrong_device,
            session_id="pilot-004",
        )


def test_device_discovery_and_selection_fail_closed() -> None:
    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="no devices")

    with pytest.raises(Exception, match="did not report"):
        list_avfoundation_audio_devices(runner=runner)
    with pytest.raises(ContractError, match="exactly one"):
        select_umik1_device((AvfoundationDevice(0, "MacBook Pro Microphone"),))
    with pytest.raises(ContractError, match="exactly one"):
        select_umik1_device((AvfoundationDevice(2, "UMIK-1"), AvfoundationDevice(3, "UMIK-1")))


def test_invalid_device_and_suite_without_room_tone_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="device ID"):
        ffmpeg_capture_command(output_path=tmp_path / "x.wav", device_id="bad\nvalue")
    suite = PromptSuite(
        suite_id="no-room",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="neutral-01",
                text="A prompt without room tone.",
                category="neutral",
                style="Neutral",
                expected_seconds=5.0,
            ),
        ),
    )
    with pytest.raises(ContractError, match="room tone"):
        expected_recording_paths(suite)


def _v2_suite(*, physical_test: bool = False, procedure: bool = False) -> PromptSuite:
    labels = ("test",) if physical_test else ()
    prompts = [
        Prompt(
            prompt_id="room-tone-01",
            instructions="Remain silent for room tone.",
            category="room-tone",
            style="Still and silent",
            expected_seconds=10.0,
            labels=labels,
        ),
        Prompt(
            prompt_id="neutral-01",
            text="Cole reads this exact sentence.",
            category="neutral",
            style="Natural speaking voice",
            expected_seconds=10.0,
            labels=labels,
        ),
    ]
    if procedure:
        prompts.append(
            Prompt(
                prompt_id="step-01",
                instructions="Follow the synthetic step.",
                category="procedure",
                style="Follow the steps",
                expected_seconds=10.0,
                labels=labels,
            )
        )
    return PromptSuite(
        schema_version="cole-voice-lab.prompt-suite/v2",
        suite_id="cole-physical-test-synthetic" if physical_test else "synthetic-pilot-v2",
        purpose="physical-test" if physical_test else "capture-pilot",
        prompts=tuple(prompts),
    )


def test_legacy_capture_engines_refuse_v2_suites_before_any_session_write(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    with pytest.raises(ContractError, match="native recording application"):
        capture_prompt_suite(
            layout=layout,
            prompt_suite=_v2_suite(),
            subject_id="cole",
            device=_device(),
            session_id="pilot-v2-001",
            read_line=lambda _: pytest.fail("v2 capture must not prompt"),
            emit=lambda _: pytest.fail("v2 capture must not print prompts"),
            process_factory=lambda _: pytest.fail("v2 capture must not start FFmpeg"),
            clock=lambda: NOW,
        )
    with pytest.raises(ContractError, match="native recording application"):
        TakeEngine(
            layout=layout,
            prompt_suite=_v2_suite(),
            subject_id="cole",
            device=_device(),
            session_id="pilot-v2-002",
            ffmpeg="/test/ffmpeg",
            process_factory=lambda _: pytest.fail("v2 capture must not start FFmpeg"),
            clock=lambda: NOW,
            timer_factory=_ManualTimerFactory(),
        )
    assert not any(layout.directory("tmp").iterdir())


def test_finalize_binds_suite_purpose_prefix_native_v2_and_refuses_procedures(
    tmp_path: Path,
) -> None:
    def finalize(suite: PromptSuite, session_id: str) -> Path:
        session = tmp_path / "private" / "sessions" / session_id
        _make_unfinalized_session(session, suite)
        finalize_capture_session(
            session,
            session_id=session_id,
            subject_id="cole",
            prompt_suite=suite,
            device=_device(),
            captured_at=NOW,
            finalized_at=NOW,
            capture_host="coles-macbook-pro",
        )
        return session

    legacy_prefix = _suite().model_copy(update={"suite_id": "cole-physical-test-legacy"})
    refusals = (
        (legacy_prefix, "pilot-prefix-001", "prefix and purpose"),
        (_v2_suite(), "pilot-v2-003", "native session"),
        (_v2_suite(physical_test=True, procedure=True), "native-root-004", "test steps"),
    )
    for suite, session_id, message in refusals:
        with pytest.raises(ContractError, match=message):
            finalize(suite, session_id)
        session = tmp_path / "private" / "sessions" / session_id
        assert {path.name for path in session.iterdir()} == {"audio"}
    session = finalize(_v2_suite(), "native-root-005")
    manifest = json.loads((session / "manifest.json").read_bytes())
    assert manifest["prompt_suite_id"] == "synthetic-pilot-v2"
    assert [entry.get("transcript") for entry in manifest["files"]] == [
        None,
        "Cole reads this exact sentence.",
    ]


def test_nonzero_ffmpeg_exit_leaves_session_unready(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")

    class FailedProcess:
        def wait(self, timeout: float | None = None) -> int:
            return 2

        def poll(self) -> int | None:
            return 2

        def send_signal(self, signal_number: int) -> None:
            raise AssertionError

        def terminate(self) -> None:
            raise AssertionError

        def kill(self) -> None:
            raise AssertionError

    with pytest.raises(Exception, match="exit code 2"):
        capture_prompt_suite(
            layout=layout,
            prompt_suite=_suite(),
            subject_id="cole",
            device=_device(),
            session_id="pilot-005",
            read_line=lambda _: "",
            emit=lambda _: None,
            process_factory=lambda _: FailedProcess(),
            clock=lambda: NOW,
        )
    assert not (layout.directory("tmp") / "capture-pilot-005" / READY_NAME).exists()


def test_finalize_is_idempotent_and_rejects_session_symlink(tmp_path: Path) -> None:
    suite = _suite()
    session = tmp_path / "private" / "sessions" / "pilot-006"
    _make_unfinalized_session(session, suite)
    first = finalize_capture_session(
        session,
        session_id="pilot-006",
        subject_id="cole",
        prompt_suite=suite,
        device=_device(),
        captured_at=NOW,
        finalized_at=NOW,
        capture_host="coles-macbook-pro",
    )
    second = finalize_capture_session(
        session,
        session_id="pilot-006",
        subject_id="cole",
        prompt_suite=suite,
        device=_device(),
        captured_at=NOW,
        finalized_at=NOW,
        capture_host="coles-macbook-pro",
    )
    assert second == first

    link = tmp_path / "private" / "sessions" / "linked-session"
    link.symlink_to(session, target_is_directory=True)
    with pytest.raises(Exception, match="real directory"):
        finalize_capture_session(
            link,
            session_id="pilot-006",
            subject_id="cole",
            prompt_suite=suite,
            device=_device(),
            captured_at=NOW,
        )


def test_ready_marker_requires_timezone_and_session_id_is_portable(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="timezone"):
        ReadyMarker(
            session_id="pilot",
            manifest_sha256="0" * 64,
            qc_report_sha256="1" * 64,
            finalized_at=datetime(2026, 8, 17),
        )
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    with pytest.raises(ContractError, match="portable stable ID"):
        capture_prompt_suite(
            layout=layout,
            prompt_suite=_suite(),
            subject_id="cole",
            device=_device(),
            session_id="../escape",
        )


def test_sigint_escalates_if_ffmpeg_does_not_exit(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")

    class StubbornProcess:
        def __init__(self) -> None:
            self.waits = 0
            self.terminated = False
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            if self.waits in {2, 3}:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return 137

        def poll(self) -> int | None:
            return None

        def send_signal(self, signal_number: int) -> None:
            assert signal_number == signal.SIGINT

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    process = StubbornProcess()

    def factory(command: Sequence[str]) -> StubbornProcess:
        _write_wav(Path(command[-1]))
        return process

    with pytest.raises(CaptureInterrupted):
        capture_prompt_suite(
            layout=layout,
            prompt_suite=_suite(),
            subject_id="cole",
            device=_device(),
            session_id="pilot-007",
            read_line=lambda _: "",
            emit=lambda _: None,
            process_factory=factory,
            clock=lambda: NOW,
        )
    assert process.terminated and process.killed


def test_take_engine_records_reviews_accepts_and_finalizes_only_when_complete(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    timers = _ManualTimerFactory()
    processes: list[_ControlledProcess] = []

    def factory(command: Sequence[str]) -> _ControlledProcess:
        output = Path(command[-1])
        process = _ControlledProcess(
            output,
            on_quit=lambda path: _write_wav(path, quiet="room-tone" in path.name),
        )
        processes.append(process)
        return process

    engine = _engine(
        layout,
        session_id="engine-complete",
        process_factory=factory,
        timers=timers,
    )
    session = layout.directory("tmp") / "capture-engine-complete"
    expected = tuple(session / relative for relative in expected_recording_paths(_suite()))

    initial = engine.current()
    assert initial.status == "ready"
    assert initial.index == 0
    assert initial.relative == "audio/000-room-tone.wav"
    assert stat.S_IMODE(session.stat().st_mode) == 0o700
    assert stat.S_IMODE((session / "audio").stat().st_mode) == 0o700

    recording = engine.start_take()
    assert recording.status == "recording"
    assert timers.timers[0].interval == 600.0
    assert timers.timers[0].started
    review = engine.stop_take()
    assert review.status == "review"
    assert review.index == 0
    assert review.metrics == engine.metrics(0)
    assert review.metrics is not None
    assert review.metrics.signal_to_noise_db is None
    assert processes[0].quit_signals == 1
    assert timers.timers[0].cancelled
    assert expected[0].is_file()
    assert not expected[0].with_suffix(".partial.wav").exists()
    assert stat.S_IMODE(expected[0].stat().st_mode) == 0o600
    assert not (session / "manifest.json").exists()
    assert not (session / "qc.json").exists()
    assert not (session / READY_NAME).exists()

    next_take = engine.accept_take()
    assert next_take.status == "ready"
    assert next_take.index == 1
    engine.start_take()
    utterance_review = engine.stop_take()
    assert utterance_review.status == "review"
    assert utterance_review.metrics == engine.metrics(1)
    assert utterance_review.metrics is not None
    assert utterance_review.metrics.signal_to_noise_db is not None
    assert utterance_review.metrics.signal_to_noise_db > 0.0
    complete = engine.accept_take()
    assert complete.status == "complete"
    assert complete.index is None
    assert not (session / "manifest.json").exists()
    assert not (session / "qc.json").exists()
    assert not (session / READY_NAME).exists()

    result = engine.finalize()

    assert result.session_directory == session
    assert result.manifest.captured_at == NOW
    assert result.manifest.finalized_at == NOW
    assert result.qc_report.metrics[0] == engine.metrics(0)
    assert result.qc_report.metrics[1] == engine.metrics(1)
    assert engine.current().status == "finalized"
    assert (session / "manifest.json").is_file()
    assert (session / "qc.json").is_file()
    assert (session / READY_NAME).is_file()


def test_rejected_takes_are_private_canonical_and_outside_the_session(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")

    def factory(command: Sequence[str]) -> _ControlledProcess:
        return _ControlledProcess(
            Path(command[-1]),
            on_quit=lambda path: _write_wav(path, quiet=True),
        )

    engine = _engine(layout, session_id="engine-retakes", process_factory=factory)
    session = layout.directory("tmp") / "capture-engine-retakes"
    rejected = layout.directory("tmp") / "capture-engine-retakes.rejected"

    engine.start_take()
    engine.stop_take()
    state = engine.reject_take("pilot requested a retake")

    assert state.status == "ready"
    assert state.index == 0
    assert rejected.parent == session.parent
    assert rejected != session
    assert stat.S_IMODE(rejected.stat().st_mode) == 0o700
    first_audio = rejected / "000-room-tone.take-1.wav"
    assert first_audio.is_file()
    assert stat.S_IMODE(first_audio.stat().st_mode) == 0o600
    assert not (session / "audio" / "000-room-tone.wav").exists()

    engine.start_take()
    engine.stop_take()
    engine.reject_take("second room-tone attempt")

    second_audio = rejected / "000-room-tone.take-2.wav"
    log_path = rejected / "rejected-takes.json"
    log = RejectedTakeLog.model_validate_json(log_path.read_bytes())
    assert second_audio.is_file()
    assert stat.S_IMODE(second_audio.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert log.session_id == "engine-retakes"
    assert [entry.relative for entry in log.entries] == [
        "000-room-tone.take-1.wav",
        "000-room-tone.take-2.wav",
    ]
    assert [entry.take_number for entry in log.entries] == [1, 2]
    assert [entry.reason for entry in log.entries] == [
        "pilot requested a retake",
        "second room-tone attempt",
    ]
    assert all(entry.rejected_at == NOW for entry in log.entries)
    assert log_path.read_bytes() == canonical_json_bytes(log) + b"\n"
    assert not (session / "rejected-takes.json").exists()
    assert not any(path.name.endswith(".take-1.wav") for path in session.rglob("*"))


def test_resume_reinspects_canonical_prefix_and_rejects_declared_partial(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    suite = _suite()
    session = layout.directory("tmp") / "capture-engine-resume"
    audio = session / "audio"
    session.mkdir(mode=0o700)
    audio.mkdir(mode=0o700)
    relatives = expected_recording_paths(suite)
    accepted = session / relatives[0]
    partial = (session / relatives[1]).with_suffix(".partial.wav")
    _write_wav(accepted, quiet=True)
    _write_wav(partial)
    accepted.chmod(0o600)
    partial.chmod(0o600)

    opened = open_session(
        layout,
        suite,
        "engine-resume",
        resume=True,
        clock=lambda: NOW,
    )

    assert opened == session
    assert accepted.is_file()
    assert not partial.exists()
    rejected = layout.directory("tmp") / "capture-engine-resume.rejected"
    rejected_partial = rejected / "001-neutral-01.take-1.wav"
    assert rejected_partial.is_file()
    assert stat.S_IMODE(rejected_partial.stat().st_mode) == 0o600
    log = RejectedTakeLog.model_validate_json((rejected / "rejected-takes.json").read_bytes())
    assert len(log.entries) == 1
    assert log.entries[0].relative == rejected_partial.name
    assert log.entries[0].reason == "interrupted"
    assert log.entries[0].rejected_at == NOW

    engine = _engine(
        layout,
        session_id="engine-resume",
        process_factory=lambda command: _ControlledProcess(
            Path(command[-1]), on_quit=lambda path: _write_wav(path)
        ),
        resume=True,
    )
    state = engine.current()
    assert state.status == "ready"
    assert state.index == 1
    assert state.relative == relatives[1]


def test_open_session_is_fresh_by_default_and_resume_fails_closed(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    suite = _suite()

    fresh = open_session(layout, suite, "fresh-session", clock=lambda: NOW)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700
    assert stat.S_IMODE((fresh / "audio").stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        open_session(layout, suite, "fresh-session", clock=lambda: NOW)

    wrong_mode = layout.directory("tmp") / "capture-wrong-mode"
    wrong_mode.mkdir(mode=0o700)
    (wrong_mode / "audio").mkdir(mode=0o700)
    wrong_mode.chmod(0o755)
    with pytest.raises(SecurityError, match="0700"):
        open_session(layout, suite, "wrong-mode", resume=True, clock=lambda: NOW)

    unexpected = layout.directory("tmp") / "capture-unexpected"
    unexpected.mkdir(mode=0o700)
    (unexpected / "audio").mkdir(mode=0o700)
    extra = unexpected / "notes.txt"
    extra.write_text("undeclared", encoding="utf-8")
    extra.chmod(0o600)
    with pytest.raises(ContractError, match="unexpected"):
        open_session(layout, suite, "unexpected", resume=True, clock=lambda: NOW)

    ready = layout.directory("tmp") / "capture-ready-present"
    ready.mkdir(mode=0o700)
    (ready / "audio").mkdir(mode=0o700)
    ready_marker = ready / READY_NAME
    ready_marker.write_text("not resumable", encoding="utf-8")
    ready_marker.chmod(0o600)
    with pytest.raises(ContractError):
        open_session(layout, suite, "ready-present", resume=True, clock=lambda: NOW)

    actual = layout.directory("tmp") / "linked-session-target"
    actual.mkdir(mode=0o700)
    (actual / "audio").mkdir(mode=0o700)
    linked = layout.directory("tmp") / "capture-linked-session"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(SecurityError, match="real directory"):
        open_session(layout, suite, "linked-session", resume=True, clock=lambda: NOW)


def test_resume_rejects_noncanonical_hardlinked_and_noncontiguous_takes(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    suite = _suite()
    relatives = expected_recording_paths(suite)

    invalid = layout.directory("tmp") / "capture-invalid-wav"
    invalid.mkdir(mode=0o700)
    (invalid / "audio").mkdir(mode=0o700)
    invalid_audio = invalid / relatives[0]
    invalid_audio.write_bytes(b"not a WAV")
    invalid_audio.chmod(0o600)
    with pytest.raises(ContractError, match="WAV"):
        open_session(layout, suite, "invalid-wav", resume=True, clock=lambda: NOW)

    hardlinked = layout.directory("tmp") / "capture-hardlinked"
    hardlinked.mkdir(mode=0o700)
    (hardlinked / "audio").mkdir(mode=0o700)
    hardlinked_audio = hardlinked / relatives[0]
    _write_wav(hardlinked_audio, quiet=True)
    hardlinked_audio.chmod(0o600)
    hardlinked_alias = hardlinked / "audio" / "alias.wav"
    hardlinked_alias.hardlink_to(hardlinked_audio)
    with pytest.raises(SecurityError, match="unlinked regular file"):
        open_session(layout, suite, "hardlinked", resume=True, clock=lambda: NOW)

    gap = layout.directory("tmp") / "capture-noncontiguous"
    gap.mkdir(mode=0o700)
    (gap / "audio").mkdir(mode=0o700)
    later = gap / relatives[1]
    _write_wav(later)
    later.chmod(0o600)
    with pytest.raises(ContractError, match="contiguous"):
        open_session(layout, suite, "noncontiguous", resume=True, clock=lambda: NOW)


def test_take_engine_abort_preserves_partial_and_failed_stop_never_renames(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    abort_timers = _ManualTimerFactory()
    aborted_processes: list[_ControlledProcess] = []

    def abort_factory(command: Sequence[str]) -> _ControlledProcess:
        output = Path(command[-1])
        _write_wav(output, quiet=True)
        output.chmod(0o600)
        process = _ControlledProcess(output, on_quit=lambda _path: None)
        aborted_processes.append(process)
        return process

    interrupted = _engine(
        layout,
        session_id="engine-abort",
        process_factory=abort_factory,
        timers=abort_timers,
    )
    interrupted.start_take()
    with pytest.raises(CaptureInterrupted) as caught:
        interrupted.abort()
    expected = expected_recording_paths(_suite())[0]
    interrupted_partial = (caught.value.session_directory / expected).with_suffix(".partial.wav")
    assert caught.value.partial_path == interrupted_partial
    assert interrupted_partial.is_file()
    assert not (caught.value.session_directory / expected).exists()
    assert aborted_processes[0].signals == [signal.SIGINT]
    assert abort_timers.timers[0].cancelled
    assert not (caught.value.session_directory / READY_NAME).exists()

    failed_processes: list[_ControlledProcess] = []

    def failed_factory(command: Sequence[str]) -> _ControlledProcess:
        process = _ControlledProcess(
            Path(command[-1]),
            on_quit=lambda path: _write_wav(path, quiet=True),
            wait_results=(2,),
        )
        failed_processes.append(process)
        return process

    failed = _engine(layout, session_id="engine-failed-stop", process_factory=failed_factory)
    failed.start_take()
    with pytest.raises(ExternalToolError, match="exit code 2"):
        failed.stop_take()
    failed_session = layout.directory("tmp") / "capture-engine-failed-stop"
    failed_destination = failed_session / expected
    assert failed_destination.with_suffix(".partial.wav").is_file()
    assert not failed_destination.exists()
    assert failed_processes[0].quit_signals == 1
    assert not (failed_session / READY_NAME).exists()

    malformed = _engine(
        layout,
        session_id="engine-malformed-stop",
        process_factory=lambda command: _ControlledProcess(
            Path(command[-1]),
            on_quit=lambda path: (path.write_bytes(b"not a WAV"), path.chmod(0o600)),
        ),
    )
    malformed.start_take()
    with pytest.raises(ContractError, match="WAV"):
        malformed.stop_take()
    malformed_session = layout.directory("tmp") / "capture-engine-malformed-stop"
    malformed_destination = malformed_session / expected
    assert malformed_destination.with_suffix(".partial.wav").is_file()
    assert not malformed_destination.exists()


def test_take_engine_stop_escalates_after_quit_timeout_without_accepting_partial(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    processes: list[_ControlledProcess] = []

    def factory(command: Sequence[str]) -> _ControlledProcess:
        process = _ControlledProcess(
            Path(command[-1]),
            on_quit=lambda path: _write_wav(path, quiet=True),
            wait_results=(
                subprocess.TimeoutExpired("ffmpeg", 10.0),
                subprocess.TimeoutExpired("ffmpeg", 10.0),
                subprocess.TimeoutExpired("ffmpeg", 3.0),
                137,
            ),
        )
        processes.append(process)
        return process

    engine = _engine(layout, session_id="engine-stop-timeout", process_factory=factory)
    engine.start_take()
    with pytest.raises(ExternalToolError, match="exit code 137"):
        engine.stop_take()

    process = processes[0]
    assert process.signals == [signal.SIGINT]
    assert process.terminated
    assert process.killed
    assert process.wait_timeouts == [10.0, 10.0, 3.0, None]
    relative = expected_recording_paths(_suite())[0]
    destination = layout.directory("tmp") / "capture-engine-stop-timeout" / relative
    assert destination.with_suffix(".partial.wav").is_file()
    assert not destination.exists()


def test_take_engine_cap_auto_stops_and_flags_review_state(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    timers = _ManualTimerFactory()
    processes: list[_ControlledProcess] = []

    def factory(command: Sequence[str]) -> _ControlledProcess:
        process = _ControlledProcess(
            Path(command[-1]),
            on_quit=lambda path: _write_wav(path, quiet=True),
        )
        processes.append(process)
        return process

    engine = _engine(
        layout,
        session_id="engine-auto-stop",
        process_factory=factory,
        timers=timers,
    )
    engine.start_take()

    timers.timers[0].fire()

    state = engine.current()
    assert state.status == "review"
    assert state.index == 0
    assert "auto-stopped" in state.findings
    assert processes[0].quit_signals == 1
    destination = (
        layout.directory("tmp") / "capture-engine-auto-stop" / expected_recording_paths(_suite())[0]
    )
    assert destination.is_file()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_take_engine_recomputes_disk_state_and_refuses_existing_partial(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")

    def factory(command: Sequence[str]) -> _ControlledProcess:
        return _ControlledProcess(
            Path(command[-1]),
            on_quit=lambda path: _write_wav(path, quiet=True),
        )

    engine = _engine(layout, session_id="engine-disk-state", process_factory=factory)
    session = layout.directory("tmp") / "capture-engine-disk-state"
    first = session / expected_recording_paths(_suite())[0]
    partial = first.with_suffix(".partial.wav")
    _write_wav(partial, quiet=True)
    partial.chmod(0o600)
    with pytest.raises(SecurityError):
        engine.start_take()

    partial.unlink()
    engine.start_take()
    engine.stop_take()
    engine.accept_take()
    assert engine.current().index == 1
    first.unlink()
    recomputed = engine.current()
    assert recomputed.status == "ready"
    assert recomputed.index == 0


def test_engine_finalization_rejects_an_unexpected_file_and_never_writes_ready(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")

    def factory(command: Sequence[str]) -> _ControlledProcess:
        output = Path(command[-1])
        return _ControlledProcess(
            output,
            on_quit=lambda path: _write_wav(path, quiet="room-tone" in path.name),
        )

    engine = _engine(layout, session_id="engine-unexpected", process_factory=factory)
    for _ in _suite().prompts:
        engine.start_take()
        engine.stop_take()
        engine.accept_take()
    session = layout.directory("tmp") / "capture-engine-unexpected"
    unexpected = session / "undeclared.txt"
    unexpected.write_text("not declared", encoding="utf-8")
    unexpected.chmod(0o600)

    with pytest.raises(ContractError, match="undeclared"):
        engine.finalize()

    assert not (session / "manifest.json").exists()
    assert not (session / "qc.json").exists()
    assert not (session / READY_NAME).exists()


def test_capture_start_rejects_dangling_paths_for_engine_and_foreground(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    engine = _engine(
        layout,
        session_id="engine-dangling",
        process_factory=lambda command: _ControlledProcess(
            Path(command[-1]), on_quit=lambda path: _write_wav(path)
        ),
    )
    engine_partial = (engine.session_directory / expected_recording_paths(_suite())[0]).with_suffix(
        ".partial.wav"
    )
    engine_partial.symlink_to(tmp_path / "missing-engine-target.wav")

    with pytest.raises(SecurityError):
        engine.start_take()

    foreground_session = layout.directory("tmp") / "capture-foreground-dangling"

    def create_dangling_partial(_prompt: str) -> str:
        partial = (foreground_session / expected_recording_paths(_suite())[0]).with_suffix(
            ".partial.wav"
        )
        partial.symlink_to(tmp_path / "missing-foreground-target.wav")
        return ""

    with pytest.raises(SecurityError, match="existing recording"):
        capture_prompt_suite(
            layout=layout,
            prompt_suite=_suite(),
            subject_id="cole",
            device=_device(),
            session_id="foreground-dangling",
            read_line=create_dangling_partial,
            emit=lambda _message: None,
            process_factory=lambda _command: pytest.fail("process must not start"),
            clock=lambda: NOW,
        )


def test_invalid_rejection_reason_does_not_move_or_log_the_reviewed_take(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    engine = _engine(
        layout,
        session_id="engine-invalid-reason",
        process_factory=lambda command: _ControlledProcess(
            Path(command[-1]), on_quit=lambda path: _write_wav(path, quiet=True)
        ),
    )
    engine.start_take()
    engine.stop_take()
    canonical = engine.session_directory / expected_recording_paths(_suite())[0]
    rejected = layout.directory("tmp") / "capture-engine-invalid-reason.rejected"

    for reason in ("x" * 161, "line one\nline two"):
        with pytest.raises(ContractError, match="reason"):
            engine.reject_take(reason)
        assert engine.current().status == "review"
        assert canonical.is_file()
        assert not rejected.exists()


def test_publication_detects_path_substitution_and_restores_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    engine = _engine(
        layout,
        session_id="engine-path-swap",
        process_factory=lambda command: _ControlledProcess(
            Path(command[-1]), on_quit=lambda path: _write_wav(path, quiet=True)
        ),
    )
    real_inspect = capture_module.inspect_canonical_wav
    replaced = False

    def replace_after_inspection(path: Path) -> object:
        nonlocal replaced
        inspection = real_inspect(path)
        if not replaced and path.name.endswith(".partial.wav"):
            replacement = path.with_name("replacement.wav")
            _write_wav(replacement)
            replacement.chmod(0o600)
            replacement.replace(path)
            replaced = True
        return inspection

    monkeypatch.setattr(capture_module, "inspect_canonical_wav", replace_after_inspection)
    engine.start_take()

    with pytest.raises(SecurityError, match="changed after inspection"):
        engine.stop_take()

    canonical = engine.session_directory / expected_recording_paths(_suite())[0]
    assert canonical.with_suffix(".partial.wav").is_file()
    assert not canonical.exists()


def test_publication_failure_after_rename_rolls_back_to_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    engine = _engine(
        layout,
        session_id="engine-publish-rollback",
        process_factory=lambda command: _ControlledProcess(
            Path(command[-1]), on_quit=lambda path: _write_wav(path, quiet=True)
        ),
    )
    canonical = engine.session_directory / expected_recording_paths(_suite())[0]
    real_fsync_directory = capture_module._fsync_directory
    failed = False

    def fail_first_published_fsync(path: Path) -> None:
        nonlocal failed
        if path == canonical.parent and canonical.exists() and not failed:
            failed = True
            raise OSError("synthetic directory fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(capture_module, "_fsync_directory", fail_first_published_fsync)
    engine.start_take()

    with pytest.raises(OSError, match="synthetic directory fsync failure"):
        engine.stop_take()

    assert failed
    assert canonical.with_suffix(".partial.wav").is_file()
    assert not canonical.exists()


def test_finalized_engine_revalidates_ready_instead_of_trusting_memory(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    engine = _engine(
        layout,
        session_id="engine-final-tamper",
        process_factory=lambda command: _ControlledProcess(
            Path(command[-1]),
            on_quit=lambda path: _write_wav(path, quiet="room-tone" in path.name),
        ),
    )
    for _prompt in _suite().prompts:
        engine.start_take()
        engine.stop_take()
        engine.accept_take()
    engine.finalize()
    (engine.session_directory / READY_NAME).unlink()

    with pytest.raises(SecurityError, match="READY"):
        engine.current()
    with pytest.raises(SecurityError, match="READY"):
        engine.finalize()


def test_rejected_take_contract_rejects_ambiguous_forensic_evidence() -> None:
    valid = RejectedTakeEntry(
        relative="000-room-tone.take-1.wav",
        take_number=1,
        sha256="0" * 64,
        size_bytes=1,
        reason="retake",
        rejected_at=NOW,
    )
    with pytest.raises(Exception, match="direct children"):
        RejectedTakeEntry(**{**valid.model_dump(), "relative": "nested/000-room-tone.take-1.wav"})
    with pytest.raises(Exception, match="whitespace"):
        RejectedTakeEntry(**{**valid.model_dump(), "reason": "   "})
    with pytest.raises(Exception, match="control"):
        RejectedTakeEntry(**{**valid.model_dump(), "reason": "bad\treason"})
    with pytest.raises(Exception, match="timezone"):
        RejectedTakeEntry(**{**valid.model_dump(), "rejected_at": datetime(2026, 8, 17, 19, 0)})
    with pytest.raises(Exception, match="unique"):
        RejectedTakeLog(session_id="contract-log", entries=(valid, valid))
    with pytest.raises(Exception, match="filename"):
        RejectedTakeLog(
            session_id="contract-log",
            entries=(valid.model_copy(update={"relative": "not-a-take.wav"}),),
        )
    third = valid.model_copy(update={"relative": "000-room-tone.take-3.wav", "take_number": 3})
    with pytest.raises(Exception, match="contiguous"):
        RejectedTakeLog(session_id="contract-log", entries=(valid, third))


def test_popen_adapter_controls_pipe_and_preserves_foreground_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[object] = []

    class FakePopen:
        def __init__(
            self, command: tuple[str, ...], *, stdin: int | None, stdout: int | None
        ) -> None:
            self.command = command
            self.stdin = io.BytesIO() if stdin == subprocess.PIPE else None
            self.stdout = io.BytesIO(b"3072\n192\n") if stdout == subprocess.PIPE else None
            self.signals: list[int] = []
            self.terminated = False
            self.killed = False
            processes.append(self)

        def wait(self, timeout: float | None = None) -> int:
            assert timeout in {None, 2.0}
            return 0

        def poll(self) -> int:
            return 0

        def send_signal(self, signal_number: int) -> None:
            self.signals.append(signal_number)

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    monkeypatch.setattr(capture_module.subprocess, "Popen", FakePopen)
    command = ffmpeg_capture_command(output_path=Path("synthetic.partial.wav"), device_id="4")
    controlled = capture_module._default_process_factory(command)
    assert controlled.wait(timeout=2.0) == 0
    assert controlled.poll() == 0
    controlled.signal_quit()
    controlled.send_signal(signal.SIGINT)
    controlled.terminate()
    controlled.kill()
    controlled_process = processes[0]
    assert isinstance(controlled_process, FakePopen)
    assert controlled_process.stdin is not None
    assert controlled.captured_frames == 1088
    assert controlled_process.command == (
        *command[:-1],
        "-stats_mux_pre:a:0",
        "pipe:1",
        "-stats_mux_pre_fmt:a:0",
        "{size}",
        command[-1],
    )
    assert controlled_process.command.index("-i") < controlled_process.command.index(
        "-stats_mux_pre:a:0"
    )
    assert controlled_process.stdout is not None and controlled_process.stdout.closed
    assert controlled_process.stdin.getvalue() == b"q"
    assert controlled_process.signals == [signal.SIGINT]
    assert controlled_process.terminated and controlled_process.killed

    foreground = capture_module._foreground_process_factory(("ffmpeg", "-version"))
    with pytest.raises(ExternalToolError, match="pipe"):
        foreground.signal_quit()
    foreground_process = processes[1]
    assert isinstance(foreground_process, FakePopen)
    assert foreground_process.stdin is None
    assert foreground_process.stdout is None
    assert foreground_process.command == ("ffmpeg", "-version")
    assert foreground.captured_frames is None
    assert foreground.wait() == 0

    timer = capture_module._default_timer_factory(600.0, lambda: None)
    assert isinstance(timer, capture_module.threading.Timer)
    assert timer.daemon
    timer.cancel()


def _progress_adapter(
    monkeypatch: pytest.MonkeyPatch,
    stream: BinaryIO | None,
    *,
    exit_code: int = 0,
) -> capture_module._PopenCaptureProcess:
    class ProgressPopen:
        def __init__(
            self, command: tuple[str, ...], *, stdin: int | None, stdout: int | None
        ) -> None:
            assert stdin == subprocess.PIPE and stdout == subprocess.PIPE
            self.stdout = stream

        def wait(self, timeout: float | None = None) -> int:
            return exit_code

        def poll(self) -> int:
            return exit_code

    monkeypatch.setattr(capture_module.subprocess, "Popen", ProgressPopen)
    return capture_module._PopenCaptureProcess(("ffmpeg", "synthetic.wav"), controlled=True)


def test_progress_clock_counts_pcm_frames_and_drains_before_wait_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.BytesIO(b"3072\n" * 10_000 + b"192\n")
    adapter = _progress_adapter(monkeypatch, stream)
    assert adapter.wait() == 0
    assert adapter.captured_frames == 10_240_064
    assert stream.closed
    assert adapter._progress_thread is not None and not adapter._progress_thread.is_alive()
    assert adapter.wait() == 0


@pytest.mark.parametrize("failure_stage", ["construct", "start"])
@pytest.mark.parametrize("cleanup_failure", [None, "kill", "wait", "close"])
@pytest.mark.parametrize("startup_error_type", [RuntimeError, KeyboardInterrupt])
def test_reader_startup_failure_stops_and_reaps_capture_preserving_original_error(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    cleanup_failure: str | None,
    startup_error_type: type[BaseException],
) -> None:
    startup_error = startup_error_type("synthetic thread startup failure")
    operations: list[str] = []

    class ControlStream(io.BytesIO):
        def close(self) -> None:
            super().close()
            if cleanup_failure == "close":
                raise OSError("synthetic pipe close failure")

    control = ControlStream()
    progress = io.BytesIO()

    class StartupPopen:
        def __init__(
            self, command: tuple[str, ...], *, stdin: int | None, stdout: int | None
        ) -> None:
            self.stdin = control
            self.stdout = progress

        def kill(self) -> None:
            operations.append("kill")
            if cleanup_failure == "kill":
                raise OSError("synthetic kill failure")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 3.0
            operations.append("wait")
            if cleanup_failure == "wait":
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return -signal.SIGKILL

    class FailedThread:
        def __init__(self, *, target: Callable[[], None], name: str, daemon: bool) -> None:
            if failure_stage == "construct":
                raise startup_error

        def start(self) -> None:
            raise startup_error

    monkeypatch.setattr(capture_module.subprocess, "Popen", StartupPopen)
    monkeypatch.setattr(capture_module.threading, "Thread", FailedThread)
    expected_error = (
        CaptureProgressError if isinstance(startup_error, Exception) else KeyboardInterrupt
    )
    with pytest.raises(expected_error) as caught:
        capture_module._PopenCaptureProcess(("ffmpeg", "synthetic.wav"), controlled=True)
    if isinstance(startup_error, Exception):
        assert caught.value.__cause__ is startup_error
        assert ("cleanup also failed" in str(caught.value)) is (cleanup_failure is not None)
    else:
        assert caught.value is startup_error
    assert bool(getattr(startup_error, "__notes__", [])) is (cleanup_failure is not None)
    assert operations == ["kill", "wait"]
    assert control.closed and progress.closed


@pytest.mark.parametrize(
    "payload",
    [b"N/A\n", b"0\n", b"-3\n", b"3.0\n", b"3\r\n", b"\n", b"3", b"3" * 33 + b"\n"],
)
def test_progress_rejects_malformed_metadata_without_wall_fallback(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    adapter = _progress_adapter(monkeypatch, io.BytesIO(b"3072\n" + payload))
    with pytest.raises(CaptureProgressError, match="malformed"):
        adapter.wait()
    with pytest.raises(CaptureProgressError, match="malformed"):
        _ = adapter.captured_frames


@pytest.mark.parametrize("payload", [b"1\n", b"4\n", b"3071\n"])
def test_progress_rejects_incomplete_canonical_mono24_frame(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    adapter = _progress_adapter(monkeypatch, io.BytesIO(payload))
    with pytest.raises(CaptureProgressError, match="incomplete PCM frame"):
        adapter.wait()


def test_progress_missing_or_failed_reader_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = _progress_adapter(monkeypatch, io.BytesIO())
    with pytest.raises(CaptureProgressError, match="without reporting"):
        missing.wait()
    assert missing.captured_frames is None

    failed_capture = _progress_adapter(monkeypatch, io.BytesIO(), exit_code=234)
    assert failed_capture.wait() == 234
    assert failed_capture.captured_frames is None

    unavailable = _progress_adapter(monkeypatch, None)
    with pytest.raises(CaptureProgressError, match="unavailable"):
        unavailable.wait()

    class FailedStream(io.BytesIO):
        def readline(self, size: int | None = -1) -> bytes:
            raise OSError("synthetic metadata pipe failure")

    failed_stream = FailedStream()
    failed_reader = _progress_adapter(monkeypatch, failed_stream)
    with pytest.raises(CaptureProgressError, match="Cannot read.*synthetic"):
        failed_reader.wait()
    assert failed_stream.closed


def test_progress_wait_requires_reader_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    class StalledThread:
        def __init__(self, *, target: Callable[[], None], name: str, daemon: bool) -> None:
            assert name == "capture-pcm-progress" and daemon

        def start(self) -> None:
            pass

        def join(self, timeout: float | None = None) -> None:
            assert timeout == capture_module.PROGRESS_READER_JOIN_SECONDS

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(capture_module.threading, "Thread", StalledThread)
    stream = io.BytesIO()
    adapter = _progress_adapter(monkeypatch, stream)
    assert adapter.captured_frames is None
    with pytest.raises(CaptureProgressError, match="reader did not finish"):
        adapter.wait()
    stream.close()


def test_progress_wait_timeout_does_not_drain_before_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _progress_adapter(monkeypatch, io.BytesIO(b"3072\n"))
    assert adapter._progress_thread is not None
    thread = adapter._progress_thread
    real_join = thread.join
    joins: list[float | None] = []
    waits = 0

    def wait(timeout: float | None = None) -> int:
        nonlocal waits
        waits += 1
        if waits == 1:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return 0

    def join(timeout: float | None = None) -> None:
        joins.append(timeout)
        real_join(timeout=timeout)

    monkeypatch.setattr(adapter._process, "wait", wait)
    monkeypatch.setattr(thread, "join", join)
    with pytest.raises(subprocess.TimeoutExpired):
        adapter.wait(timeout=0.01)
    assert joins == []
    assert adapter.wait() == 0
    assert joins == [capture_module.PROGRESS_READER_JOIN_SECONDS]
    assert adapter.captured_frames == 1024


def test_interrupt_reaps_exited_capture_and_surfaces_progress_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _progress_adapter(monkeypatch, io.BytesIO(b"bad\n"))
    with pytest.raises(CaptureProgressError, match="malformed"):
        capture_module._interrupt_process(adapter)


def test_take_timer_uses_only_confirmed_pcm_frames_and_resets_between_takes(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    processes: list[_ControlledProcess] = []
    timers = _ManualTimerFactory()

    def factory(command: Sequence[str]) -> _ControlledProcess:
        process = _ControlledProcess(
            Path(command[-1]), on_quit=lambda path: _write_wav(path, quiet=True)
        )
        processes.append(process)
        return process

    engine = _engine(
        layout, session_id="engine-media-clock", process_factory=factory, timers=timers
    )
    wall = 100.0
    engine.monotonic = lambda: wall
    started = engine.start_take()
    assert started.status == "recording" and started.elapsed_seconds is None
    wall += 2.0
    assert engine.current().elapsed_seconds is None
    processes[0].captured_frames = 480
    assert engine.current().elapsed_seconds == 0.01
    wall += 30.0
    assert engine.current().elapsed_seconds == 0.01
    processes[0].captured_frames = 960
    assert engine.current().elapsed_seconds == 0.02
    assert timers.timers[0].interval == capture_module.MAX_TAKE_SECONDS
    stopped = engine.stop_take()
    assert stopped.status == "review" and stopped.elapsed_seconds is None
    assert stopped.metrics is not None and stopped.metrics.duration_seconds == 0.02
    engine.accept_take()
    restarted = engine.start_take()
    assert restarted.prompt == _suite().prompts[1]
    assert restarted.elapsed_seconds is None
    engine.stop_take()


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_initial_progress_failure_aborts_capture_and_preserves_original_error(
    tmp_path: Path, cleanup_failure: bool
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    startup_error = CaptureProgressError("synthetic initial frame read failed")
    processes: list[_ControlledProcess] = []
    timers = _ManualTimerFactory()

    class FailedInitialProgress(_ControlledProcess):
        @property
        def captured_frames(self) -> int | None:
            raise startup_error

        @captured_frames.setter
        def captured_frames(self, value: int | None) -> None:
            self._unused_frames = value

        def wait(self, timeout: float | None = None) -> int:
            result = super().wait(timeout=timeout)
            if cleanup_failure:
                raise CaptureProgressError("synthetic reader drain also failed")
            return result

    def factory(command: Sequence[str]) -> _ControlledProcess:
        partial = Path(command[-1])
        _write_wav(partial, quiet=True)
        partial.chmod(0o600)
        process = FailedInitialProgress(partial, on_quit=lambda _: None)
        processes.append(process)
        return process

    engine = _engine(
        layout, session_id="engine-initial-progress-failure", process_factory=factory, timers=timers
    )
    with pytest.raises(CaptureProgressError, match="initial frame read") as caught:
        engine.start_take()
    if cleanup_failure:
        assert caught.value.__cause__ is startup_error
        assert "cleanup also failed: synthetic reader drain also failed" in str(caught.value)
    else:
        assert caught.value is startup_error
        assert "Failed capture start was aborted" in " ".join(startup_error.__notes__)
    assert processes[0].signals == [signal.SIGINT]
    assert processes[0].poll() == 0
    assert timers.timers[0].cancelled
    assert engine._live_process is None and engine._live_index is None and engine._timer is None
    assert processes[0].output_path.is_file()
    assert stat.S_IMODE(processes[0].output_path.stat().st_mode) == 0o600
    assert not (engine.session_directory / expected_recording_paths(_suite())[0]).exists()
    assert not (engine.session_directory / READY_NAME).exists()


def test_watchdog_start_failure_aborts_before_initial_recording_state(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    processes: list[_ControlledProcess] = []
    startup_error = RuntimeError("synthetic watchdog start failure")

    class FailedTimer(_ManualTimer):
        def start(self) -> None:
            raise startup_error

    timers: list[FailedTimer] = []

    def timer_factory(interval: float, callback: Callable[[], None]) -> FailedTimer:
        timer = FailedTimer(interval, callback)
        timers.append(timer)
        return timer

    def factory(command: Sequence[str]) -> _ControlledProcess:
        process = _ControlledProcess(Path(command[-1]), on_quit=lambda _: None)
        processes.append(process)
        return process

    engine = _engine(layout, session_id="engine-watchdog-start-failure", process_factory=factory)
    engine.timer_factory = timer_factory
    with pytest.raises(RuntimeError, match="watchdog start") as caught:
        engine.start_take()
    assert caught.value is startup_error
    assert processes[0].signals == [signal.SIGINT] and processes[0].poll() == 0
    assert timers[0].cancelled
    assert engine._live_process is None and engine._timer is None
    assert engine.current().status == "ready"


@pytest.mark.parametrize("mismatched_frames", [None, 959, 961])
def test_progress_failure_or_wav_mismatch_preserves_partial_and_never_publishes(
    tmp_path: Path, mismatched_frames: int | None
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    processes: list[_ControlledProcess] = []

    def factory(command: Sequence[str]) -> _ControlledProcess:
        process = _ControlledProcess(
            Path(command[-1]),
            on_quit=lambda path: _write_wav(path, quiet=True),
            wait_results=(CaptureProgressError("synthetic progress failure"),)
            if mismatched_frames is None
            else (0,),
        )
        process.captured_frames = mismatched_frames
        processes.append(process)
        return process

    engine = _engine(layout, session_id="engine-bad-progress", process_factory=factory)
    engine.start_take()
    with pytest.raises(CaptureProgressError, match="progress"):
        engine.stop_take()
    process = processes[0]
    assert process.quit_signals == 1 and process.signals == []
    assert process.output_path.is_file()
    assert stat.S_IMODE(process.output_path.stat().st_mode) == 0o600
    assert not process.output_path.with_name(
        process.output_path.name.replace(".partial", "")
    ).exists()
    assert not (engine.session_directory / READY_NAME).exists()


def test_failed_progress_after_quit_pipe_error_still_secures_partial(tmp_path: Path) -> None:
    destination = tmp_path / "synthetic.wav"
    partial = destination.with_suffix(".partial.wav")
    _write_wav(partial)
    partial.chmod(0o644)

    class ClosedPipeProcess(_ControlledProcess):
        def signal_quit(self) -> None:
            raise BrokenPipeError("synthetic closed control pipe")

    process = ClosedPipeProcess(
        partial,
        on_quit=lambda _: None,
        wait_results=(CaptureProgressError("synthetic drain failure"),),
    )
    process.return_code = 0
    with pytest.raises(CaptureProgressError, match="drain failure"):
        capture_module._stop_capture(process=process, destination=destination)
    assert process.wait_timeouts == [None]
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600
    assert not destination.exists()


def test_take_engine_preview_and_invalid_transitions_are_explicit(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    engine = _engine(
        layout,
        session_id="engine-transitions",
        process_factory=lambda command: _ControlledProcess(
            Path(command[-1]), on_quit=lambda path: _write_wav(path, quiet=True)
        ),
    )
    with pytest.raises(ContractError, match="preview"):
        engine.preview_path()
    with pytest.raises(ContractError, match="selection is not enabled"):
        engine.select_take(1)
    with pytest.raises(ContractError, match="recording"):
        engine.stop_take()
    with pytest.raises(ContractError, match="accept"):
        engine.accept_take()
    with pytest.raises(ContractError, match="reject"):
        engine.reject_take("retake")
    with pytest.raises(ContractError, match="finalize"):
        engine.finalize()
    engine.abort()

    engine.start_take()
    with pytest.raises(ContractError, match="cannot start"):
        engine.start_take()
    engine.stop_take()
    assert engine.preview_path() == (
        engine.session_directory / expected_recording_paths(_suite())[0]
    )
    with pytest.raises(ContractError, match="preview"):
        engine.preview_path(1)


_SYNTHETIC_NATIVE_COMMAND = (
    "/synthetic/python",
    "-I",
    "-B",
    "-m",
    "cole_voice_lab.capture_native",
    "--device-uid",
    "synthetic-umik-uid",
    "--output",
    "synthetic.partial.wav",
)


def _native_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    diagnostics: BinaryIO | None,
    progress: bytes = b"3072\n192\n",
    exit_code: int = 0,
    command: tuple[str, ...] = _SYNTHETIC_NATIVE_COMMAND,
) -> capture_module._PopenCaptureProcess:
    class NativePopen:
        def __init__(
            self, command: tuple[str, ...], *, stdin: int, stdout: int, stderr: int
        ) -> None:
            assert stdin == stdout == stderr == subprocess.PIPE
            self.command = command
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(progress)
            self.stderr = diagnostics
            self.return_code: int | None = None
            self.waits: list[float | None] = []
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            self.waits.append(timeout)
            self.return_code = exit_code
            return exit_code

        def poll(self) -> int | None:
            return self.return_code

        def kill(self) -> None:
            self.killed = True

    monkeypatch.setattr(capture_module.subprocess, "Popen", NativePopen)
    adapter = capture_module._default_process_factory(command)
    assert isinstance(adapter, capture_module._PopenCaptureProcess)
    return adapter


def test_native_adapter_dispatch_keeps_exact_command_and_counts_pcm_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic_stream = io.BytesIO(b"synthetic native summary\n")
    adapter = _native_adapter(monkeypatch, diagnostics=diagnostic_stream)

    assert adapter.wait(timeout=2.0) == 0
    assert adapter._process.command == _SYNTHETIC_NATIVE_COMMAND
    assert "-stats_mux_pre:a:0" not in adapter._process.command
    assert adapter.captured_frames == 1088
    assert adapter._progress_thread is not None and not adapter._progress_thread.is_alive()
    assert adapter._diagnostic_thread is not None and not adapter._diagnostic_thread.is_alive()
    assert diagnostic_stream.closed and adapter._process.stdout.closed
    adapter.signal_quit()
    assert adapter._process.stdin.getvalue() == b"q"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_native_stderr_is_bounded_and_nonzero_exit_reports_tail_diagnostics(
    monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    payload = b"synthetic discarded prefix\n" * 10_000 + b"\xff native input disconnected\n"
    adapter = _native_adapter(monkeypatch, diagnostics=io.BytesIO(payload), exit_code=exit_code)

    if exit_code:
        with pytest.raises(CaptureProgressError, match="exit 7") as caught:
            adapter.wait()
        assert str(caught.value).endswith("native input disconnected")
    else:
        assert adapter.wait() == 0
    assert (
        bytes(adapter._diagnostic_bytes) == payload[-capture_module.MAX_CAPTURE_DIAGNOSTIC_BYTES :]
    )
    assert len(adapter._diagnostic_bytes) == capture_module.MAX_CAPTURE_DIAGNOSTIC_BYTES
    assert adapter._diagnostic_thread is not None and not adapter._diagnostic_thread.is_alive()


@pytest.mark.parametrize("failure", ["missing", "oserror", "valueerror"])
def test_native_diagnostic_reader_failure_cannot_report_success(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class FailedDiagnosticStream(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            error_type = OSError if failure == "oserror" else ValueError
            raise error_type("synthetic diagnostics read failure")

    stream = None if failure == "missing" else FailedDiagnosticStream()
    adapter = _native_adapter(monkeypatch, diagnostics=stream)

    with pytest.raises(CaptureProgressError, match="diagnostics.*missing|diagnostics.*synthetic"):
        adapter.wait()
    with pytest.raises(CaptureProgressError):
        _ = adapter.captured_frames
    assert stream is None or stream.closed


def test_native_adapter_rejects_success_without_pcm_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _native_adapter(monkeypatch, diagnostics=io.BytesIO(), progress=b"")

    with pytest.raises(CaptureProgressError, match="without reporting"):
        adapter.wait()


def test_native_diagnostic_reader_must_finish_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SelectivelyStalledThread:
        def __init__(self, *, target: Callable[[], None], name: str, daemon: bool) -> None:
            self.target, self.name = target, name
            assert daemon

        def start(self) -> None:
            if self.name == "capture-pcm-progress":
                self.target()

        def join(self, timeout: float | None = None) -> None:
            assert timeout == capture_module.PROGRESS_READER_JOIN_SECONDS

        def is_alive(self) -> bool:
            return self.name == "capture-diagnostics"

    monkeypatch.setattr(capture_module.threading, "Thread", SelectivelyStalledThread)
    stream = io.BytesIO()
    adapter = _native_adapter(monkeypatch, diagnostics=stream)
    try:
        with pytest.raises(CaptureProgressError, match="diagnostics reader did not finish"):
            adapter.wait()
    finally:
        stream.close()


@pytest.mark.parametrize("reader_name", ["capture-pcm-progress", "capture-diagnostics"])
@pytest.mark.parametrize("failure_stage", ["construct", "start"])
def test_native_reader_startup_failure_reaps_child_and_closes_all_three_pipes(
    monkeypatch: pytest.MonkeyPatch, reader_name: str, failure_stage: str
) -> None:
    startup_error = RuntimeError("synthetic native reader startup failure")
    processes: list[object] = []

    class StartupNativePopen:
        def __init__(
            self, command: tuple[str, ...], *, stdin: int, stdout: int, stderr: int
        ) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"3072\n")
            self.stderr = io.BytesIO()
            self.killed = self.waited = False
            processes.append(self)

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 3.0
            self.waited = True
            return -signal.SIGKILL

    class FailedNativeReader:
        def __init__(self, *, target: Callable[[], None], name: str, daemon: bool) -> None:
            self.target, self.name = target, name
            if name == reader_name and failure_stage == "construct":
                raise startup_error

        def start(self) -> None:
            if self.name == reader_name and failure_stage == "start":
                raise startup_error
            self.target()

    monkeypatch.setattr(capture_module.subprocess, "Popen", StartupNativePopen)
    monkeypatch.setattr(capture_module.threading, "Thread", FailedNativeReader)

    with pytest.raises(CaptureProgressError) as caught:
        capture_module._default_process_factory(_SYNTHETIC_NATIVE_COMMAND)
    process = processes[0]
    assert isinstance(process, StartupNativePopen)
    assert caught.value.__cause__ is startup_error
    assert process.killed and process.waited
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed


def test_native_capture_exit_failure_preserves_partial_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "synthetic.wav"
    partial = destination.with_suffix(".partial.wav")
    _write_wav(partial)
    partial.chmod(0o644)
    adapter = _native_adapter(
        monkeypatch,
        diagnostics=io.BytesIO(b"synthetic capture timestamp discontinuity\n"),
        exit_code=7,
    )

    with pytest.raises(CaptureProgressError, match="timestamp discontinuity"):
        capture_module._stop_capture(process=adapter, destination=destination)
    assert partial.is_file() and stat.S_IMODE(partial.stat().st_mode) == 0o600
    assert not destination.exists()
    assert not (tmp_path / READY_NAME).exists()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_unexpected_capture_exit_is_sticky_and_preserves_partial_without_ready(
    tmp_path: Path, exit_code: int
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    processes: list[_ControlledProcess] = []
    timers = _ManualTimerFactory()

    def factory(command: Sequence[str]) -> _ControlledProcess:
        output = Path(command[-1])
        _write_wav(output)
        output.chmod(0o644)
        process = _ControlledProcess(output, on_quit=lambda _: None, wait_results=(exit_code,))
        processes.append(process)
        return process

    engine = _engine(
        layout, session_id="engine-unexpected-exit", process_factory=factory, timers=timers
    )
    assert engine.start_take().status == "recording"
    processes[0].return_code = exit_code

    with pytest.raises(CaptureProgressError, match="unexpected") as caught:
        engine.current()
    with pytest.raises(CaptureProgressError) as repeated:
        engine.current()
    assert repeated.value is caught.value
    assert engine._live_process is None and engine._live_index is None
    assert timers.timers[0].cancelled and engine._timer is None
    assert processes[0].wait_timeouts
    assert processes[0].output_path.is_file()
    assert stat.S_IMODE(processes[0].output_path.stat().st_mode) == 0o600
    assert not (engine.session_directory / expected_recording_paths(_suite())[0]).exists()
    assert not (engine.session_directory / READY_NAME).exists()


@pytest.mark.parametrize("with_diagnostics", [False, True])
def test_coreaudio_start_routes_native_uid_output_and_optional_diagnostics(
    tmp_path: Path, with_diagnostics: bool
) -> None:
    from cole_voice_lab.capture_native import native_capture_command

    commands: list[tuple[str, ...]] = []
    destination = tmp_path / "synthetic.wav"
    diagnostics = tmp_path / "synthetic-diagnostics.json" if with_diagnostics else None

    def factory(command: Sequence[str]) -> _ControlledProcess:
        commands.append(tuple(command))
        return _ControlledProcess(Path(command[-1]), on_quit=lambda _: None)

    process = capture_module._start_capture(
        destination=destination,
        device_id="synthetic-coreaudio-uid",
        ffmpeg="/must-not-run-ffmpeg",
        process_factory=factory,
        backend="coreaudio",
        diagnostics_path=diagnostics,
    )

    assert commands == [
        native_capture_command(
            output_path=destination.with_suffix(".partial.wav"),
            device_id="synthetic-coreaudio-uid",
            diagnostics_path=diagnostics,
        )
    ]
    assert isinstance(process, _ControlledProcess)
    assert process.output_path == destination.with_suffix(".partial.wav")
    assert ("--diagnostics-path" in commands[0]) is with_diagnostics
    assert "/must-not-run-ffmpeg" not in commands[0]
    assert not destination.exists()


def test_native_engine_diagnostics_are_private_external_to_transfer_and_unique_per_take(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    commands: list[tuple[str, ...]] = []

    def factory(command: Sequence[str]) -> _ControlledProcess:
        commands.append(tuple(command))
        process = _ControlledProcess(
            Path(command[-1]), on_quit=lambda path: _write_wav(path, quiet=True)
        )
        process.captured_frames = 960
        return process

    device = _device().model_copy(update={"backend": "coreaudio", "device_id": "native-umik-uid"})
    engine = TakeEngine(
        layout=layout,
        prompt_suite=_suite(),
        subject_id="cole",
        device=device,
        session_id="engine-native-diagnostics",
        process_factory=factory,
        timer_factory=_ManualTimerFactory(),
        clock=lambda: NOW,
    )

    engine.start_take()
    engine.stop_take()
    engine.reject_take("synthetic-retake")
    engine.start_take()
    engine.stop_take()

    diagnostic_paths = [
        Path(command[command.index("--diagnostics-path") + 1]) for command in commands
    ]
    expected_directory = layout.root / "provenance/capture-continuity/engine-native-diagnostics"
    assert len(diagnostic_paths) == 2 and diagnostic_paths[0] != diagnostic_paths[1]
    assert all(path.parent == expected_directory for path in diagnostic_paths)
    assert stat.S_IMODE(expected_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(expected_directory.parent.stat().st_mode) == 0o700
    assert all(not path.is_relative_to(engine.session_directory) for path in diagnostic_paths)
    assert all(
        command[command.index("--device-uid") + 1] == "native-umik-uid" for command in commands
    )
    assert all(command[-1].endswith(".partial.wav") for command in commands)
    assert not (engine.session_directory / READY_NAME).exists()


def test_unexpected_capture_exit_preserves_native_diagnostic_failure(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    failure = CaptureProgressError("synthetic native timestamp discontinuity")
    processes: list[_ControlledProcess] = []

    def factory(command: Sequence[str]) -> _ControlledProcess:
        output = Path(command[-1])
        _write_wav(output)
        process = _ControlledProcess(output, on_quit=lambda _: None, wait_results=(failure,))
        processes.append(process)
        return process

    engine = _engine(layout, session_id="engine-native-exit-detail", process_factory=factory)
    engine.start_take()
    processes[0].return_code = 7

    with pytest.raises(CaptureProgressError) as caught:
        engine.current()
    assert caught.value is failure
    assert engine._live_process is None
    assert processes[0].output_path.is_file()
    assert not (engine.session_directory / READY_NAME).exists()


def _native_completed_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[capture_module._PopenCaptureProcess, Path, Path, dict[str, object]]:
    """Create only synthetic PCM/proof and inert process pipes; never invoke a device."""
    from cole_voice_lab.capture_native import native_capture_command

    destination = tmp_path / "synthetic-proof.wav"
    partial = destination.with_suffix(".partial.wav")
    _write_wav(partial)
    partial.chmod(0o644)
    inspection = capture_module.inspect_canonical_wav(partial)
    proof_directory = tmp_path / "private-proof"
    proof_directory.mkdir(mode=0o700)
    proof_path = proof_directory / "continuity.json"
    evidence = capture_module.CaptureContinuityEvidence(
        schema_version="cole-voice-lab.capture-continuity/v1",
        backend="coreaudio-audioqueue",
        device_uid="synthetic-umik-uid",
        output_name=partial.name,
        wav_sha256=inspection.sha256,
        wav_size_bytes=inspection.size_bytes,
        frames=inspection.audio.frames,
        received_frames=inspection.audio.frames,
        callback_count=1,
        first_sample_time=1_000.0,
        last_sample_end=1_000.0 + inspection.audio.frames,
        success=True,
        continuity_checked=True,
        stop_reason="requested",
    )
    proof_path.write_bytes(canonical_json_bytes(evidence))
    proof_path.chmod(0o600)
    adapter = _native_adapter(
        monkeypatch,
        diagnostics=io.BytesIO(),
        progress=f"{inspection.audio.frames * 3}\n".encode("ascii"),
        command=native_capture_command(
            output_path=partial,
            device_id="synthetic-umik-uid",
            diagnostics_path=proof_path,
        ),
    )
    return adapter, destination, proof_path, evidence.model_dump(mode="json")


def test_native_publication_requires_and_accepts_checksum_bound_continuity_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, destination, proof_path, evidence = _native_completed_proof(tmp_path, monkeypatch)
    original = destination.with_suffix(".partial.wav").read_bytes()

    inspection = capture_module._stop_capture(process=adapter, destination=destination)

    assert destination.read_bytes() == original
    assert not destination.with_suffix(".partial.wav").exists()
    assert inspection.sha256 == evidence["wav_sha256"]
    assert inspection.audio.frames == evidence["frames"] == adapter.captured_frames
    assert proof_path.is_file()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not (tmp_path / READY_NAME).exists()


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"device_uid": "different-device"},
        {"output_name": "different-output.partial.wav"},
        {"output_name": "../synthetic-proof.partial.wav"},
        {"wav_sha256": "0" * 64},
        {"wav_size_bytes": 1},
        {"frames": 961, "received_frames": 961, "last_sample_end": 1_961.0},
        {"received_frames": 961},
        {"last_sample_end": 1_962.0},
        {"first_sample_time": float("nan")},
        {"last_sample_end": float("nan")},
        {"first_sample_time": float("inf"), "last_sample_end": float("inf")},
        {"success": False},
        {"continuity_checked": False},
        {"callback_count": 0},
        {"frames": True},
        {"schema_version": "cole-voice-lab.capture-continuity/v999"},
        {"backend": "avfoundation"},
        {"unexpected_field": "not permitted"},
    ],
    ids=[
        "uid",
        "output",
        "traversal",
        "digest",
        "size",
        "wav-frames",
        "received-frames",
        "timestamp-span",
        "nan-first",
        "nan-last",
        "infinite-span",
        "unsuccessful",
        "unchecked",
        "no-callbacks",
        "coerced-frames",
        "unknown-version",
        "backend",
        "extra",
    ],
)
def test_invalid_native_continuity_proof_never_publishes_or_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_fields: dict[str, object]
) -> None:
    adapter, destination, proof_path, evidence = _native_completed_proof(tmp_path, monkeypatch)
    partial = destination.with_suffix(".partial.wav")
    original = partial.read_bytes()
    evidence.update(invalid_fields)
    # Adversarial JSON intentionally includes nonfinite tokens in dedicated cases;
    # valid evidence above always uses the canonical, finite JSON encoder.
    proof_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(SecurityError, match="continuity evidence"):
        capture_module._stop_capture(process=adapter, destination=destination)
    assert partial.read_bytes() == original
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600
    assert not destination.exists()
    assert not (tmp_path / READY_NAME).exists()


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "oversize",
        "public-mode",
        "hardlink",
        "symlink",
        "not-json",
        "unbound",
        "no-output",
    ],
)
def test_untrusted_native_proof_file_cannot_authorize_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    adapter, destination, proof_path, _ = _native_completed_proof(tmp_path, monkeypatch)
    partial = destination.with_suffix(".partial.wav")
    original = partial.read_bytes()
    if failure == "missing":
        proof_path.unlink()
    elif failure == "oversize":
        proof_path.write_bytes(b" " * (capture_module.MAX_CAPTURE_DIAGNOSTIC_BYTES + 1))
    elif failure == "public-mode":
        proof_path.chmod(0o644)
    elif failure == "hardlink":
        proof_path.with_name("other-proof.json").hardlink_to(proof_path)
    elif failure == "symlink":
        target = proof_path.with_name("actual-proof.json")
        proof_path.rename(target)
        proof_path.symlink_to(target)
    elif failure == "not-json":
        proof_path.write_bytes(b"not JSON")
    elif failure == "unbound":
        adapter._native_diagnostics_path = None
    else:
        adapter._native_output_path = None

    with pytest.raises((SecurityError, FileNotFoundError)):
        capture_module._stop_capture(process=adapter, destination=destination)
    assert partial.read_bytes() == original
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600
    assert not destination.exists()
    assert not (tmp_path / READY_NAME).exists()

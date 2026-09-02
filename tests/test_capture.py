from __future__ import annotations

import io
import signal
import stat
import subprocess
import wave
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cole_voice_lab.capture as capture_module
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.capture import (
    READY_NAME,
    AvfoundationDevice,
    CaptureInterrupted,
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
        def __init__(self, command: tuple[str, ...], *, stdin: int | None) -> None:
            self.command = command
            self.stdin = io.BytesIO() if stdin == subprocess.PIPE else None
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
    controlled = capture_module._default_process_factory(("ffmpeg", "-version"))
    assert controlled.wait(timeout=2.0) == 0
    assert controlled.poll() == 0
    controlled.signal_quit()
    controlled.send_signal(signal.SIGINT)
    controlled.terminate()
    controlled.kill()
    controlled_process = processes[0]
    assert isinstance(controlled_process, FakePopen)
    assert controlled_process.stdin is not None
    assert controlled_process.stdin.getvalue() == b"q"
    assert controlled_process.signals == [signal.SIGINT]
    assert controlled_process.terminated and controlled_process.killed

    foreground = capture_module._foreground_process_factory(("ffmpeg", "-version"))
    with pytest.raises(ExternalToolError, match="pipe"):
        foreground.signal_quit()
    foreground_process = processes[1]
    assert isinstance(foreground_process, FakePopen)
    assert foreground_process.stdin is None

    timer = capture_module._default_timer_factory(600.0, lambda: None)
    assert isinstance(timer, capture_module.threading.Timer)
    assert timer.daemon
    timer.cancel()


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

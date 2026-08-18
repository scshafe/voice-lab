from __future__ import annotations

import signal
import subprocess
import wave
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.capture import (
    READY_NAME,
    AvfoundationDevice,
    CaptureInterrupted,
    ReadyMarker,
    capture_prompt_suite,
    expected_recording_paths,
    ffmpeg_capture_command,
    finalize_capture_session,
    list_avfoundation_audio_devices,
    select_umik1_device,
)
from cole_voice_lab.errors import ContractError
from cole_voice_lab.models import CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout

NOW = datetime(2026, 8, 17, 19, 0, tzinfo=UTC)


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

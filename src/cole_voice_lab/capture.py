"""Foreground macOS capture and fail-closed session finalization."""

from __future__ import annotations

import os
import re
import signal
import socket
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError, field_validator

from .audio import WavInspection, inspect_canonical_wav
from .canonical import (
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .errors import ContractError, ExternalToolError, SecurityError, VoiceLabError
from .models import (
    AudioFileEvidence,
    CaptureDevice,
    CaptureManifest,
    ContractModel,
    Prompt,
    PromptSuite,
    QcReport,
    Sha256,
    StableId,
    utc_now,
)
from .paths import AssetLayout, confined_path, ensure_private_directory, validate_relative_path

DEFAULT_FFMPEG = "/opt/homebrew/bin/ffmpeg"
MANIFEST_NAME = "manifest.json"
QC_REPORT_NAME = "qc.json"
READY_NAME = "READY"

_STABLE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


class ReadyMarker(ContractModel):
    """Last-written commit marker for a complete capture package."""

    schema_version: Literal["cole-voice-lab.ready/v1"] = "cole-voice-lab.ready/v1"
    session_id: StableId
    manifest_path: Literal["manifest.json"] = "manifest.json"
    manifest_sha256: Sha256
    qc_report_path: Literal["qc.json"] = "qc.json"
    qc_report_sha256: Sha256
    finalized_at: datetime

    @field_validator("finalized_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("finalization time requires an explicit timezone")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AvfoundationDevice:
    index: int
    display_name: str

    @property
    def device_id(self) -> str:
        return str(self.index)


@dataclass(frozen=True, slots=True)
class CaptureResult:
    session_directory: Path
    manifest: CaptureManifest
    manifest_sha256: str
    qc_report: QcReport
    qc_report_sha256: str
    ready: ReadyMarker


class CaptureProcess(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def send_signal(self, signal_number: int) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


type ProcessFactory = Callable[[Sequence[str]], CaptureProcess]
type RecordingKind = Literal["utterance", "room-tone"]


class CaptureInterrupted(VoiceLabError):
    """Capture was interrupted; no READY marker was published."""

    def __init__(self, session_directory: Path, partial_path: Path) -> None:
        super().__init__(
            f"capture interrupted; recoverable partial remains at {partial_path} "
            f"and session is not ready: {session_directory}"
        )
        self.session_directory = session_directory
        self.partial_path = partial_path


def _default_process_factory(command: Sequence[str]) -> CaptureProcess:
    return subprocess.Popen(tuple(command))


def _require_stable_id(value: str, label: str) -> str:
    if not _STABLE_ID.fullmatch(value):
        raise ContractError(f"{label} is not a portable stable ID: {value!r}")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_if_regular(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    except FileNotFoundError:
        return


def list_avfoundation_audio_devices(
    *,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[AvfoundationDevice, ...]:
    """List AVFoundation audio inputs without opening a recording stream."""

    process = runner(
        (ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""),
        text=True,
        capture_output=True,
        check=False,
    )
    output = f"{process.stdout}\n{process.stderr}"
    in_audio_section = False
    devices: list[AvfoundationDevice] = []
    for line in output.splitlines():
        lowered = line.lower()
        if "avfoundation audio devices" in lowered:
            in_audio_section = True
            continue
        if "avfoundation video devices" in lowered:
            in_audio_section = False
            continue
        if not in_audio_section:
            continue
        match = re.search(r"\[(\d+)]\s+(.+?)\s*$", line)
        if match:
            devices.append(
                AvfoundationDevice(index=int(match.group(1)), display_name=match.group(2))
            )
    if not devices:
        raise ExternalToolError("FFmpeg did not report any AVFoundation audio inputs")
    return tuple(devices)


def select_umik1_device(
    devices: Sequence[AvfoundationDevice],
    *,
    requested: str | None = None,
) -> AvfoundationDevice:
    """Select the explicitly requested UMIK-1 and reject ambiguous discovery."""

    candidates = [
        device
        for device in devices
        if "umik-1" in device.display_name.casefold()
        and (requested is None or requested in {device.device_id, device.display_name})
    ]
    if len(candidates) != 1:
        detail = ", ".join(f"{item.device_id}:{item.display_name}" for item in devices)
        raise ContractError(
            f"expected exactly one selected UMIK-1 input; discovered/selected: {detail or 'none'}"
        )
    return candidates[0]


def ffmpeg_capture_command(
    *,
    output_path: Path,
    device_id: str,
    ffmpeg: str = DEFAULT_FFMPEG,
) -> tuple[str, ...]:
    """Build the unprocessed, canonical UMIK-1 capture command."""

    if not device_id or any(character in device_id for character in {"\x00", "\n", "\r"}):
        raise ContractError("AVFoundation device ID is invalid")
    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "avfoundation",
        "-i",
        f":{device_id}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s24le",
        "-f",
        "wav",
        "-n",
        str(output_path),
    )


def _recording_entries(
    prompt_suite: PromptSuite,
) -> tuple[tuple[Prompt, RecordingKind, str], ...]:
    if not any(prompt.category == "room-tone" for prompt in prompt_suite.prompts):
        raise ContractError("capture prompt suite must include room tone")
    entries: list[tuple[Prompt, RecordingKind, str]] = []
    for index, prompt in enumerate(prompt_suite.prompts):
        kind: RecordingKind = "room-tone" if prompt.category == "room-tone" else "utterance"
        label = "room-tone" if kind == "room-tone" else prompt.prompt_id
        relative = validate_relative_path(f"audio/{index:03d}-{label}.wav")
        entries.append((prompt, kind, relative))
    return tuple(entries)


def expected_recording_paths(prompt_suite: PromptSuite) -> tuple[str, ...]:
    return tuple(relative for _, _, relative in _recording_entries(prompt_suite))


def _capture_one(
    *,
    session_directory: Path,
    destination: Path,
    device_id: str,
    ffmpeg: str,
    process_factory: ProcessFactory,
) -> None:
    partial = destination.with_suffix(".partial.wav")
    if partial.exists() or destination.exists():
        raise SecurityError(f"refusing to overwrite an existing recording: {destination}")
    process = process_factory(
        ffmpeg_capture_command(output_path=partial, device_id=device_id, ffmpeg=ffmpeg)
    )
    try:
        return_code = process.wait()
    except KeyboardInterrupt as error:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        _fsync_if_regular(partial)
        _fsync_directory(partial.parent)
        raise CaptureInterrupted(session_directory, partial) from error
    if return_code != 0:
        _fsync_if_regular(partial)
        raise ExternalToolError(f"FFmpeg capture failed with exit code {return_code}: {partial}")
    inspect_canonical_wav(partial)
    os.replace(partial, destination)
    os.chmod(destination, 0o600)
    _fsync_directory(destination.parent)


def capture_prompt_suite(
    *,
    layout: AssetLayout,
    prompt_suite: PromptSuite,
    subject_id: str,
    device: CaptureDevice,
    session_id: str,
    ffmpeg: str = DEFAULT_FFMPEG,
    read_line: Callable[[str], str] = input,
    emit: Callable[[str], None] = print,
    process_factory: ProcessFactory = _default_process_factory,
    clock: Callable[[], datetime] = utc_now,
    capture_host: str | None = None,
) -> CaptureResult:
    """Interactively record every exact prompt and finalize only the complete suite."""

    _require_stable_id(session_id, "session ID")
    _require_stable_id(subject_id, "subject ID")
    if "umik-1" not in device.display_name.casefold():
        raise ContractError("production capture requires the selected UMIK-1 microphone")
    captured_at = clock()
    session_directory = layout.directory("tmp") / f"capture-{session_id}"
    session_directory.mkdir(mode=0o700, exist_ok=False)
    audio_directory = session_directory / "audio"
    audio_directory.mkdir(mode=0o700)
    for index, (prompt, kind, relative) in enumerate(_recording_entries(prompt_suite), start=1):
        emit(f"\n[{index}/{len(prompt_suite.prompts)}] {prompt.category}: {prompt.style}")
        emit(prompt.text)
        if kind == "room-tone":
            emit("Remain silent and still for the complete room-tone recording.")
        else:
            emit("Read the prompt exactly as printed; do not improvise or correct it.")
        read_line("Press Return to start recording. ")
        emit("Recording in the foreground. Press q in FFmpeg when finished; Ctrl-C aborts.")
        _capture_one(
            session_directory=session_directory,
            destination=confined_path(session_directory, relative),
            device_id=device.device_id,
            ffmpeg=ffmpeg,
            process_factory=process_factory,
        )
    return finalize_capture_session(
        session_directory,
        session_id=session_id,
        subject_id=subject_id,
        prompt_suite=prompt_suite,
        device=device,
        captured_at=captured_at,
        finalized_at=clock(),
        capture_host=capture_host or socket.gethostname(),
    )


def _regular_file_paths(session_directory: Path) -> set[str]:
    paths: set[str] = set()
    for root, directories, files in os.walk(session_directory, followlinks=False):
        root_path = Path(root)
        for name in directories:
            child = root_path / name
            if child.is_symlink():
                raise SecurityError(f"symlink directory is forbidden in capture: {child}")
        for name in files:
            child = root_path / name
            metadata = child.lstat()
            if child.is_symlink() or not child.is_file() or metadata.st_nlink != 1:
                raise SecurityError(f"capture member must be one unlinked regular file: {child}")
            paths.add(child.relative_to(session_directory).as_posix())
    return paths


def _read_existing_result(session_directory: Path) -> CaptureResult:
    try:
        with open_regular_no_follow(session_directory / READY_NAME) as stream:
            marker = ReadyMarker.model_validate_json(stream.read())
        with open_regular_no_follow(session_directory / marker.manifest_path) as stream:
            manifest = CaptureManifest.model_validate_json(stream.read())
        with open_regular_no_follow(session_directory / marker.qc_report_path) as stream:
            report = QcReport.model_validate_json(stream.read())
    except (ValidationError, ValueError) as error:
        raise ContractError(f"existing finalized capture is invalid: {error}") from error
    manifest_sha256, _ = sha256_file(session_directory / marker.manifest_path)
    report_sha256, _ = sha256_file(session_directory / marker.qc_report_path)
    if manifest_sha256 != marker.manifest_sha256 or report_sha256 != marker.qc_report_sha256:
        raise SecurityError("existing READY marker does not match capture evidence")
    return CaptureResult(
        session_directory=session_directory,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        qc_report=report,
        qc_report_sha256=report_sha256,
        ready=marker,
    )


def finalize_capture_session(
    session_directory: Path,
    *,
    session_id: str,
    subject_id: str,
    prompt_suite: PromptSuite,
    device: CaptureDevice,
    captured_at: datetime,
    finalized_at: datetime | None = None,
    capture_host: str | None = None,
) -> CaptureResult:
    """Validate every recording and publish canonical evidence, with READY last."""

    original_metadata = session_directory.expanduser().lstat()
    if stat.S_ISLNK(original_metadata.st_mode) or not stat.S_ISDIR(original_metadata.st_mode):
        raise SecurityError(f"capture session must be a real directory: {session_directory}")
    _require_stable_id(session_id, "session ID")
    _require_stable_id(subject_id, "subject ID")
    session_directory = ensure_private_directory(session_directory)
    if (session_directory / READY_NAME).exists():
        return _read_existing_result(session_directory)

    entries = _recording_entries(prompt_suite)
    expected_audio = {relative for _, _, relative in entries}
    allowed_before_ready = expected_audio | {MANIFEST_NAME, QC_REPORT_NAME}
    actual = _regular_file_paths(session_directory)
    unexpected = actual - allowed_before_ready
    missing = expected_audio - actual
    if unexpected or missing:
        raise ContractError(
            f"capture file set is incomplete or undeclared; missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )

    inspected: dict[str, WavInspection] = {}
    for relative in sorted(expected_audio):
        inspected[relative] = inspect_canonical_wav(
            confined_path(session_directory, relative, must_exist=True)
        )

    finalized = finalized_at or utc_now()
    evidence: list[AudioFileEvidence] = []
    room_noise: float | None = None
    for prompt, kind, relative in entries:
        inspection = inspected[relative]
        if kind == "room-tone" and room_noise is None:
            room_noise = inspection.rms_dbfs
        evidence.append(
            AudioFileEvidence(
                path=relative,
                kind=kind,
                sha256=inspection.sha256,
                size_bytes=inspection.size_bytes,
                audio=inspection.audio,
                prompt_id=prompt.prompt_id if kind == "utterance" else None,
                transcript=prompt.text if kind == "utterance" else None,
            )
        )

    manifest = CaptureManifest(
        session_id=session_id,
        subject_id=subject_id,
        prompt_suite_id=prompt_suite.suite_id,
        capture_host=capture_host or socket.gethostname(),
        captured_at=captured_at,
        finalized_at=finalized,
        capture_device=device,
        files=tuple(evidence),
    )
    manifest_path = session_directory / MANIFEST_NAME
    manifest_sha256 = write_canonical_json(manifest_path, manifest)

    metrics = tuple(
        inspected[relative].qc_metrics(
            relative,
            noise_rms_dbfs=(room_noise if kind == "utterance" else None),
            allow_quiet=kind == "room-tone",
        )
        for _, kind, relative in entries
    )
    report = QcReport(
        session_id=session_id,
        manifest_sha256=manifest_sha256,
        generated_at=finalized,
        status="pass" if all(metric.passed for metric in metrics) else "review",
        metrics=metrics,
    )
    report_path = session_directory / QC_REPORT_NAME
    report_sha256 = write_canonical_json(report_path, report)
    marker = ReadyMarker(
        session_id=session_id,
        manifest_sha256=manifest_sha256,
        qc_report_sha256=report_sha256,
        finalized_at=finalized,
    )
    write_canonical_json(session_directory / READY_NAME, marker)
    _fsync_directory(session_directory)
    return CaptureResult(
        session_directory=session_directory,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        qc_report=report,
        qc_report_sha256=report_sha256,
        ready=marker,
    )


# Stable, short aliases for CLI and library callers.
capture_session = capture_prompt_suite
finalize_capture = finalize_capture_session

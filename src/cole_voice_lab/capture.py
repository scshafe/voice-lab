"""Foreground macOS capture and fail-closed session finalization."""

from __future__ import annotations

import os
import re
import signal
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, ValidationError, field_validator, model_validator

from .audio import WavInspection, inspect_canonical_wav
from .canonical import (
    canonical_json_bytes,
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .errors import ContractError, ExternalToolError, SecurityError, VoiceLabError
from .models import (
    AudioFileEvidence,
    AudioQcMetrics,
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
REJECTED_LOG_NAME = "rejected-takes.json"
MAX_TAKE_SECONDS = 600.0

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


class RejectedTakeEntry(ContractModel):
    """One rejected recording retained outside the transferable session."""

    relative: str = Field(min_length=1, max_length=1_024)
    take_number: int = Field(ge=1)
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=160)
    rejected_at: datetime

    @field_validator("relative")
    @classmethod
    def _portable_direct_child(cls, value: str) -> str:
        validated = validate_relative_path(value)
        if Path(validated).name != validated:
            raise ValueError("rejected take paths must be direct children")
        return validated

    @field_validator("reason")
    @classmethod
    def _reason_is_not_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rejection reason must not be whitespace")
        if any(ord(character) < 32 for character in value):
            raise ValueError("rejection reason cannot contain control characters")
        return value

    @field_validator("rejected_at")
    @classmethod
    def _rejected_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("rejection time requires an explicit timezone")
        return value.astimezone(UTC)


class RejectedTakeLog(ContractModel):
    """Canonical ledger for forensic recordings rejected from one session."""

    schema_version: Literal["cole-voice-lab.capture-rejected-takes/v1"] = (
        "cole-voice-lab.capture-rejected-takes/v1"
    )
    session_id: StableId
    entries: tuple[RejectedTakeEntry, ...] = ()

    @model_validator(mode="after")
    def _entries_are_unique(self) -> RejectedTakeLog:
        relatives = [entry.relative for entry in self.entries]
        if len(relatives) != len(set(relatives)):
            raise ValueError("rejected take paths must be unique")
        numbers: dict[str, list[int]] = {}
        for entry in self.entries:
            match = re.fullmatch(r"(.+)\.take-([1-9][0-9]*)\.wav", entry.relative)
            if match is None or int(match.group(2)) != entry.take_number:
                raise ValueError("rejected take filename must bind its take number")
            numbers.setdefault(match.group(1), []).append(entry.take_number)
        if any(sorted(values) != list(range(1, len(values) + 1)) for values in numbers.values()):
            raise ValueError("rejected take numbers must be contiguous per recording")
        return self


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


@dataclass(frozen=True, slots=True)
class TakeView:
    """One prompt's disk-derived capture and QC state."""

    index: int
    total: int
    prompt: Prompt
    kind: RecordingKind
    relative: str
    status: TakePhase
    metrics: AudioQcMetrics | None
    findings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaptureState:
    """Serializable state exposed to the console driver."""

    session_directory: Path
    session_id: str
    status: CapturePhase
    index: int | None
    total: int
    prompt: Prompt | None
    kind: RecordingKind | None
    relative: str | None
    metrics: AudioQcMetrics | None
    findings: tuple[str, ...]
    elapsed_seconds: float | None
    takes: tuple[TakeView, ...]
    manifest_sha256: str | None = None
    qc_report_sha256: str | None = None
    ready: bool = False


class CaptureProcess(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def signal_quit(self) -> None: ...

    def send_signal(self, signal_number: int) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class CaptureTimer(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


type ProcessFactory = Callable[[Sequence[str]], CaptureProcess]
type TimerFactory = Callable[[float, Callable[[], None]], CaptureTimer]
type RecordingKind = Literal["utterance", "room-tone"]
type CapturePhase = Literal["ready", "recording", "review", "complete", "finalized"]
type TakePhase = Literal["pending", "recording", "review", "accepted"]


class CaptureInterrupted(VoiceLabError):
    """Capture was interrupted; no READY marker was published."""

    def __init__(self, session_directory: Path, partial_path: Path) -> None:
        super().__init__(
            f"capture interrupted; recoverable partial remains at {partial_path} "
            f"and session is not ready: {session_directory}"
        )
        self.session_directory = session_directory
        self.partial_path = partial_path


class _PopenCaptureProcess:
    """Small typed adapter around Popen, with an optional FFmpeg control pipe."""

    def __init__(self, command: Sequence[str], *, controlled: bool) -> None:
        self._process = subprocess.Popen(
            tuple(command),
            stdin=subprocess.PIPE if controlled else None,
        )

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def poll(self) -> int | None:
        return self._process.poll()

    def signal_quit(self) -> None:
        if self._process.stdin is None:
            raise ExternalToolError("FFmpeg control pipe is unavailable")
        self._process.stdin.write(b"q")
        self._process.stdin.flush()

    def send_signal(self, signal_number: int) -> None:
        self._process.send_signal(signal_number)

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()


def _default_process_factory(command: Sequence[str]) -> CaptureProcess:
    return _PopenCaptureProcess(command, controlled=True)


def _foreground_process_factory(command: Sequence[str]) -> CaptureProcess:
    return _PopenCaptureProcess(command, controlled=False)


def _default_timer_factory(seconds: float, callback: Callable[[], None]) -> CaptureTimer:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    return timer


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


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _secure_capture_file(path: Path) -> tuple[int, int, int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise SecurityError(f"capture member must be one regular file: {path}")
    with open_regular_no_follow(path) as stream:
        opened = os.fstat(stream.fileno())
        if _file_identity(opened) != _file_identity(metadata):
            raise SecurityError(f"capture member changed while it was opened: {path}")
        os.fchmod(stream.fileno(), 0o600)
        try:
            os.fsync(stream.fileno())
        except OSError as error:
            raise SecurityError(f"cannot fsync capture member {path}: {error}") from error
    after = path.lstat()
    if _file_identity(after) != _file_identity(opened):
        raise SecurityError(f"capture member changed after it was secured: {path}")
    return _file_identity(opened)


def _stable_sha256_file(path: Path) -> tuple[str, int]:
    before = path.lstat()
    digest, size = sha256_file(path)
    after = path.lstat()
    if _file_identity(before) != _file_identity(after):
        raise SecurityError(f"file changed while it was hashed: {path}")
    return digest, size


def _require_canonical_private_json(path: Path, value: object) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise SecurityError(f"capture evidence is not one private regular file: {path}")
    with open_regular_no_follow(path) as stream:
        opened = os.fstat(stream.fileno())
        if _file_identity(opened) != _file_identity(metadata):
            raise SecurityError(f"capture evidence changed while it was opened: {path}")
        payload = stream.read()
    if _file_identity(path.lstat()) != _file_identity(opened):
        raise SecurityError(f"capture evidence changed while it was read: {path}")
    if payload != canonical_json_bytes(value) + b"\n":
        raise ContractError(f"capture evidence is not canonical JSON: {path}")


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


def _start_capture(
    *,
    destination: Path,
    device_id: str,
    ffmpeg: str,
    process_factory: ProcessFactory,
) -> CaptureProcess:
    partial = destination.with_suffix(".partial.wav")
    if os.path.lexists(partial) or os.path.lexists(destination):
        raise SecurityError(f"refusing to overwrite an existing recording: {destination}")
    return process_factory(
        ffmpeg_capture_command(output_path=partial, device_id=device_id, ffmpeg=ffmpeg)
    )


def _interrupt_process(process: CaptureProcess) -> int:
    return_code = process.poll()
    if return_code is not None:
        return return_code
    process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            return process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


def _publish_completed_capture(*, partial: Path, destination: Path) -> WavInspection:
    identity = _secure_capture_file(partial)
    if identity is None:
        raise ContractError(f"FFmpeg did not produce a capture partial: {partial}")
    inspection = inspect_canonical_wav(partial)
    if _file_identity(partial.lstat()) != identity:
        raise SecurityError(f"capture partial changed after inspection: {partial}")
    if os.path.lexists(destination):
        raise SecurityError(f"refusing to overwrite an existing recording: {destination}")
    os.replace(partial, destination)
    try:
        if _file_identity(destination.lstat()) != identity:
            raise SecurityError("published capture is not the inspected partial")
        published_identity = _secure_capture_file(destination)
        if published_identity != identity:
            raise SecurityError("published capture identity changed while it was secured")
        if inspect_canonical_wav(destination) != inspection:
            raise SecurityError("published capture bytes differ from the inspected partial")
        _fsync_directory(destination.parent)
        return inspection
    except Exception:
        try:
            if os.path.lexists(destination) and not os.path.lexists(partial):
                os.replace(destination, partial)
                _secure_capture_file(partial)
                _fsync_directory(partial.parent)
        except Exception as rollback_error:
            raise SecurityError(
                f"capture publication failed and could not restore the partial: {partial}"
            ) from rollback_error
        raise


def _stop_capture(*, process: CaptureProcess, destination: Path) -> WavInspection:
    partial = destination.with_suffix(".partial.wav")
    try:
        process.signal_quit()
        return_code = process.wait(timeout=10.0)
    except (BrokenPipeError, ExternalToolError, OSError, ValueError, subprocess.TimeoutExpired):
        return_code = _interrupt_process(process)
    if return_code != 0:
        _secure_capture_file(partial)
        raise ExternalToolError(f"FFmpeg capture failed with exit code {return_code}: {partial}")
    return _publish_completed_capture(partial=partial, destination=destination)


def _capture_one(
    *,
    session_directory: Path,
    destination: Path,
    device_id: str,
    ffmpeg: str,
    process_factory: ProcessFactory,
) -> None:
    partial = destination.with_suffix(".partial.wav")
    process = _start_capture(
        destination=destination,
        device_id=device_id,
        ffmpeg=ffmpeg,
        process_factory=process_factory,
    )
    try:
        return_code = process.wait()
    except KeyboardInterrupt as error:
        _interrupt_process(process)
        _secure_capture_file(partial)
        _fsync_directory(partial.parent)
        raise CaptureInterrupted(session_directory, partial) from error
    if return_code != 0:
        _secure_capture_file(partial)
        raise ExternalToolError(f"FFmpeg capture failed with exit code {return_code}: {partial}")
    _publish_completed_capture(partial=partial, destination=destination)


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
    process_factory: ProcessFactory = _foreground_process_factory,
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


def _require_private_real_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ContractError(f"{label} does not exist: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"{label} must be a real directory: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise SecurityError(f"{label} must have mode 0700: {path}")


def _require_private_recording(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise SecurityError(f"capture recording must be one regular file: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SecurityError(f"accepted capture recording must have mode 0600: {path}")


def _validate_engine_directories(session_directory: Path) -> None:
    _require_private_real_directory(session_directory, label="capture session")
    audio_directory = session_directory / "audio"
    _require_private_real_directory(audio_directory, label="capture audio directory")
    for child in session_directory.iterdir():
        if child.name == "audio":
            continue
        metadata = child.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            raise ContractError(f"unexpected directory in capture session: {child}")
    for child in audio_directory.iterdir():
        metadata = child.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            raise ContractError(f"unexpected directory in capture audio: {child}")


def _rejected_directory(layout: AssetLayout, session_id: str) -> Path:
    return layout.directory("tmp") / f"capture-{session_id}.rejected"


def _read_rejected_log(layout: AssetLayout, session_id: str) -> RejectedTakeLog:
    directory = _rejected_directory(layout, session_id)
    if not os.path.lexists(directory):
        return RejectedTakeLog(session_id=session_id)
    _require_private_real_directory(directory, label="rejected-takes directory")
    children = {child.name: child for child in directory.iterdir()}
    log_path = directory / REJECTED_LOG_NAME
    if REJECTED_LOG_NAME not in children:
        if children:
            raise ContractError(f"rejected-takes directory has no canonical log: {directory}")
        return RejectedTakeLog(session_id=session_id)
    metadata = log_path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise SecurityError(f"rejected-takes log is not one private regular file: {log_path}")
    with open_regular_no_follow(log_path) as stream:
        opened_metadata = os.fstat(stream.fileno())
        if _file_identity(opened_metadata) != _file_identity(metadata):
            raise SecurityError(f"rejected-takes log changed while it was opened: {log_path}")
        payload = stream.read()
    if _file_identity(log_path.lstat()) != _file_identity(opened_metadata):
        raise SecurityError(f"rejected-takes log changed while it was read: {log_path}")
    try:
        log = RejectedTakeLog.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"invalid rejected-takes log: {error}") from error
    if payload != canonical_json_bytes(log) + b"\n":
        raise ContractError(f"rejected-takes log is not canonical JSON: {log_path}")
    if log.session_id != session_id:
        raise ContractError("rejected-takes log session ID does not match its directory")
    expected = {REJECTED_LOG_NAME, *(entry.relative for entry in log.entries)}
    if set(children) != expected:
        raise ContractError(
            "rejected-takes file set disagrees with its log; "
            f"missing={sorted(expected - set(children))}, "
            f"unexpected={sorted(set(children) - expected)}"
        )
    for entry in log.entries:
        path = directory / entry.relative
        item_metadata = path.lstat()
        if (
            stat.S_ISLNK(item_metadata.st_mode)
            or not stat.S_ISREG(item_metadata.st_mode)
            or item_metadata.st_nlink != 1
            or stat.S_IMODE(item_metadata.st_mode) != 0o600
        ):
            raise SecurityError(f"rejected take is not one private regular file: {path}")
        digest, size = _stable_sha256_file(path)
        if digest != entry.sha256 or size != entry.size_bytes:
            raise SecurityError(f"rejected take does not match its log: {path}")
    return log


def _reject_recording(
    *,
    layout: AssetLayout,
    session_id: str,
    source: Path,
    canonical_relative: str,
    reason: str,
    rejected_at: datetime,
) -> RejectedTakeEntry:
    if not reason.strip():
        raise ContractError("rejection reason must not be empty")
    if len(reason) > 160 or any(ord(character) < 32 for character in reason):
        raise ContractError("rejection reason is not a valid durable description")
    if rejected_at.tzinfo is None or rejected_at.utcoffset() is None:
        raise ContractError("rejection time requires an explicit timezone")
    source_metadata = source.lstat()
    if (
        stat.S_ISLNK(source_metadata.st_mode)
        or not stat.S_ISREG(source_metadata.st_mode)
        or source_metadata.st_nlink != 1
    ):
        raise SecurityError(f"rejected source must be one regular file: {source}")
    directory = _rejected_directory(layout, session_id)
    if not os.path.lexists(directory):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        _fsync_directory(directory.parent)
    log = _read_rejected_log(layout, session_id)
    stem = Path(canonical_relative).stem
    prefix = f"{stem}.take-"
    prior_numbers = [
        entry.take_number
        for entry in log.entries
        if entry.relative.startswith(prefix) and entry.relative.endswith(".wav")
    ]
    take_number = max(prior_numbers, default=0) + 1
    relative = validate_relative_path(f"{stem}.take-{take_number}.wav")
    destination = directory / relative
    if os.path.lexists(destination):
        raise SecurityError(f"refusing to overwrite a rejected take: {destination}")
    source_identity = _file_identity(source_metadata)
    os.replace(source, destination)
    try:
        if _file_identity(destination.lstat()) != source_identity:
            raise SecurityError("rejected take is not the source recording that was validated")
        if _secure_capture_file(destination) != source_identity:
            raise SecurityError("rejected take identity changed while it was secured")
        _fsync_directory(source.parent)
        _fsync_directory(directory)
        digest, size = _stable_sha256_file(destination)
        entry = RejectedTakeEntry(
            relative=relative,
            take_number=take_number,
            sha256=digest,
            size_bytes=size,
            reason=reason,
            rejected_at=rejected_at,
        )
        write_canonical_json(
            directory / REJECTED_LOG_NAME,
            RejectedTakeLog(session_id=session_id, entries=(*log.entries, entry)),
        )
        return entry
    except Exception:
        try:
            if os.path.lexists(destination) and not os.path.lexists(source):
                os.replace(destination, source)
                _secure_capture_file(source)
                _fsync_directory(source.parent)
                _fsync_directory(directory)
        except Exception as rollback_error:
            raise SecurityError(
                f"take rejection failed and could not restore the canonical take: {source}"
            ) from rollback_error
        raise


def open_session(
    layout: AssetLayout,
    prompt_suite: PromptSuite,
    session_id: str,
    *,
    resume: bool = False,
    clock: Callable[[], datetime] = utc_now,
) -> Path:
    """Create a fresh capture session or fail-closed while reopening one."""

    _require_stable_id(session_id, "session ID")
    AssetLayout.validate(layout.root)
    session_directory = layout.directory("tmp") / f"capture-{session_id}"
    rejected_directory = _rejected_directory(layout, session_id)
    if not resume:
        if os.path.lexists(rejected_directory):
            raise SecurityError(
                f"refusing a fresh session with existing rejected evidence: {rejected_directory}"
            )
        session_directory.mkdir(mode=0o700, exist_ok=False)
        os.chmod(session_directory, 0o700)
        audio_directory = session_directory / "audio"
        audio_directory.mkdir(mode=0o700)
        os.chmod(audio_directory, 0o700)
        _fsync_directory(session_directory)
        _fsync_directory(session_directory.parent)
        return session_directory

    _validate_engine_directories(session_directory)
    if os.path.lexists(session_directory / READY_NAME):
        raise ContractError(f"a finalized capture session cannot be resumed: {session_directory}")
    entries = _recording_entries(prompt_suite)
    expected = {relative for _, _, relative in entries}
    partials = {str(Path(relative).with_suffix(".partial.wav")) for relative in expected}
    actual = _regular_file_paths(session_directory)
    unexpected = actual - expected - partials
    if unexpected:
        raise ContractError(f"capture session contains unexpected files: {sorted(unexpected)}")
    canonical_indices: list[int] = []
    for index, (_, _, relative) in enumerate(entries):
        canonical = confined_path(session_directory, relative)
        partial = canonical.with_suffix(".partial.wav")
        if canonical.exists() and partial.exists():
            raise ContractError(f"canonical and partial take both exist: {canonical}")
        if canonical.exists():
            _require_private_recording(canonical)
            inspect_canonical_wav(canonical)
            canonical_indices.append(index)
    if canonical_indices != list(range(len(canonical_indices))):
        raise ContractError("completed resumed takes must form a contiguous prompt prefix")
    for _, _, relative in entries:
        canonical = confined_path(session_directory, relative)
        partial = canonical.with_suffix(".partial.wav")
        if partial.exists():
            _reject_recording(
                layout=layout,
                session_id=session_id,
                source=partial,
                canonical_relative=relative,
                reason="interrupted",
                rejected_at=clock(),
            )
    _read_rejected_log(layout, session_id)
    return session_directory


class TakeEngine:
    """Disk-derived, serialized state machine for one foreground capture session."""

    def __init__(
        self,
        *,
        layout: AssetLayout,
        prompt_suite: PromptSuite,
        subject_id: str,
        device: CaptureDevice,
        session_id: str,
        ffmpeg: str = DEFAULT_FFMPEG,
        process_factory: ProcessFactory = _default_process_factory,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        timer_factory: TimerFactory = _default_timer_factory,
        capture_host: str | None = None,
        resume: bool = False,
    ) -> None:
        _require_stable_id(subject_id, "subject ID")
        if "umik-1" not in device.display_name.casefold():
            raise ContractError("production capture requires the selected UMIK-1 microphone")
        self._lock = threading.RLock()
        self.layout = layout
        self.prompt_suite = prompt_suite
        self.subject_id = subject_id
        self.device = device
        self.session_id = _require_stable_id(session_id, "session ID")
        self.ffmpeg = ffmpeg
        self.process_factory = process_factory
        self.clock = clock
        self.monotonic = monotonic
        self.timer_factory = timer_factory
        self.capture_host = capture_host or socket.gethostname()
        self.entries = _recording_entries(prompt_suite)
        self.session_directory = open_session(
            layout,
            prompt_suite,
            session_id,
            resume=resume,
            clock=clock,
        )
        self.captured_at = clock()
        self._live_process: CaptureProcess | None = None
        self._live_index: int | None = None
        self._started_monotonic: float | None = None
        self._timer: CaptureTimer | None = None
        self._review_index: int | None = None
        self._take_findings: dict[int, tuple[str, ...]] = {}
        self._background_error: VoiceLabError | None = None
        self._final_result: CaptureResult | None = None
        self._state()

    def _recording_path(self, index: int) -> Path:
        try:
            relative = self.entries[index][2]
        except IndexError as error:
            raise ContractError(f"take index is out of range: {index}") from error
        return confined_path(self.session_directory, relative)

    def _inspections(self) -> dict[int, WavInspection]:
        _validate_engine_directories(self.session_directory)
        expected = {relative for _, _, relative in self.entries}
        expected_partials = {
            str(Path(relative).with_suffix(".partial.wav")) for relative in expected
        }
        actual = _regular_file_paths(self.session_directory)
        evidence = (
            {MANIFEST_NAME, QC_REPORT_NAME, READY_NAME} if self._final_result is not None else set()
        )
        unexpected = actual - expected - expected_partials - evidence
        if unexpected:
            raise ContractError(f"capture session contains undeclared files: {sorted(unexpected)}")
        inspections: dict[int, WavInspection] = {}
        partial_indices: list[int] = []
        for index, _entry in enumerate(self.entries):
            canonical = self._recording_path(index)
            partial = canonical.with_suffix(".partial.wav")
            if canonical.exists() and partial.exists():
                raise ContractError(f"canonical and partial take both exist: {canonical}")
            if canonical.exists():
                _require_private_recording(canonical)
                inspections[index] = inspect_canonical_wav(canonical)
            if partial.exists():
                partial_indices.append(index)
        if sorted(inspections) != list(range(len(inspections))):
            raise ContractError("completed takes must form a contiguous prompt prefix")
        allowed_partial = {self._live_index} if self._live_index is not None else set()
        if set(partial_indices) - allowed_partial:
            raise SecurityError(
                "refusing to overwrite an existing recording; "
                f"interrupted partial indices={partial_indices}"
            )
        return inspections

    def _metrics_from(self, inspections: dict[int, WavInspection], index: int) -> AudioQcMetrics:
        try:
            _, kind, relative = self.entries[index]
            inspection = inspections[index]
        except (IndexError, KeyError) as error:
            raise ContractError(f"take is not available for QC: {index}") from error
        room_noise: float | None = None
        if kind == "utterance":
            for room_index, (_, room_kind, _) in enumerate(self.entries):
                if room_kind == "room-tone" and room_index in inspections:
                    if room_index != self._review_index:
                        room_noise = inspections[room_index].rms_dbfs
                        break
        return inspection.qc_metrics(
            relative,
            noise_rms_dbfs=room_noise,
            allow_quiet=kind == "room-tone",
        )

    def _validated_final_result(self) -> CaptureResult:
        cached = self._final_result
        if cached is None:
            raise ContractError("capture has not been finalized")
        on_disk = _read_existing_result(self.session_directory)
        if on_disk != cached:
            raise SecurityError("finalized capture evidence changed after publication")
        expected_audio = {relative for _, _, relative in self.entries}
        expected_files = expected_audio | {MANIFEST_NAME, QC_REPORT_NAME, READY_NAME}
        actual = _regular_file_paths(self.session_directory)
        if actual != expected_files:
            raise ContractError(
                "finalized capture file set changed; "
                f"missing={sorted(expected_files - actual)}, "
                f"unexpected={sorted(actual - expected_files)}"
            )
        _require_canonical_private_json(
            self.session_directory / MANIFEST_NAME,
            on_disk.manifest,
        )
        _require_canonical_private_json(
            self.session_directory / QC_REPORT_NAME,
            on_disk.qc_report,
        )
        _require_canonical_private_json(
            self.session_directory / READY_NAME,
            on_disk.ready,
        )
        manifest_digest, _ = _stable_sha256_file(self.session_directory / MANIFEST_NAME)
        report_digest, _ = _stable_sha256_file(self.session_directory / QC_REPORT_NAME)
        if manifest_digest != on_disk.manifest_sha256 or report_digest != on_disk.qc_report_sha256:
            raise SecurityError("finalized capture digests changed during validation")
        if (
            on_disk.ready.session_id != self.session_id
            or on_disk.manifest.session_id != self.session_id
            or on_disk.qc_report.session_id != self.session_id
            or on_disk.manifest.subject_id != self.subject_id
            or on_disk.manifest.prompt_suite_id != self.prompt_suite.suite_id
            or on_disk.manifest.capture_device != self.device
            or on_disk.manifest.capture_host != self.capture_host
            or on_disk.qc_report.manifest_sha256 != manifest_digest
            or on_disk.manifest.finalized_at != on_disk.qc_report.generated_at
            or on_disk.manifest.finalized_at != on_disk.ready.finalized_at
        ):
            raise ContractError("finalized capture evidence bindings are inconsistent")
        expected_order = tuple(relative for _, _, relative in self.entries)
        if tuple(item.path for item in on_disk.manifest.files) != expected_order:
            raise ContractError("finalized capture manifest does not match the prompt suite")
        inspections = self._inspections()
        for index, evidence in enumerate(on_disk.manifest.files):
            inspection = inspections[index]
            if (
                evidence.sha256 != inspection.sha256
                or evidence.size_bytes != inspection.size_bytes
                or evidence.audio != inspection.audio
            ):
                raise SecurityError(f"finalized recording changed: {evidence.path}")
        recomputed_metrics = tuple(
            self._metrics_from(inspections, index) for index in range(len(self.entries))
        )
        expected_status = (
            "pass" if all(metric.passed for metric in recomputed_metrics) else "review"
        )
        if (
            on_disk.qc_report.metrics != recomputed_metrics
            or on_disk.qc_report.status != expected_status
        ):
            raise SecurityError("finalized capture QC no longer matches the recordings")
        return on_disk

    def _finalized_state(self, result: CaptureResult) -> CaptureState:
        inspections = {
            index: inspect_canonical_wav(self._recording_path(index))
            for index in range(len(self.entries))
        }
        takes = tuple(
            TakeView(
                index=index,
                total=len(self.entries),
                prompt=prompt,
                kind=kind,
                relative=relative,
                status="accepted",
                metrics=self._metrics_from(inspections, index),
                findings=(
                    *self._metrics_from(inspections, index).findings,
                    *self._take_findings.get(index, ()),
                ),
            )
            for index, (prompt, kind, relative) in enumerate(self.entries)
        )
        return CaptureState(
            session_directory=self.session_directory,
            session_id=self.session_id,
            status="finalized",
            index=None,
            total=len(self.entries),
            prompt=None,
            kind=None,
            relative=None,
            metrics=None,
            findings=(),
            elapsed_seconds=None,
            takes=takes,
            manifest_sha256=result.manifest_sha256,
            qc_report_sha256=result.qc_report_sha256,
            ready=True,
        )

    def _state(self) -> CaptureState:
        if self._background_error is not None:
            raise self._background_error
        if self._final_result is not None:
            return self._finalized_state(self._validated_final_result())
        inspections = self._inspections()
        if self._live_index is not None:
            status: CapturePhase = "recording"
            index: int | None = self._live_index
        elif self._review_index is not None:
            if self._review_index not in inspections:
                raise SecurityError("reviewed take disappeared from the capture session")
            status = "review"
            index = self._review_index
        elif len(inspections) == len(self.entries):
            status = "complete"
            index = None
        else:
            status = "ready"
            index = len(inspections)

        views: list[TakeView] = []
        for take_index, (prompt, kind, relative) in enumerate(self.entries):
            metrics = (
                self._metrics_from(inspections, take_index) if take_index in inspections else None
            )
            if take_index == self._live_index:
                take_status: TakePhase = "recording"
            elif take_index == self._review_index:
                take_status = "review"
            elif take_index in inspections:
                take_status = "accepted"
            else:
                take_status = "pending"
            views.append(
                TakeView(
                    index=take_index,
                    total=len(self.entries),
                    prompt=prompt,
                    kind=kind,
                    relative=relative,
                    status=take_status,
                    metrics=metrics,
                    findings=(
                        *(metrics.findings if metrics is not None else ()),
                        *self._take_findings.get(take_index, ()),
                    ),
                )
            )

        current_prompt: Prompt | None = None
        current_kind: RecordingKind | None = None
        current_relative: str | None = None
        current_metrics: AudioQcMetrics | None = None
        current_findings: tuple[str, ...] = ()
        if index is not None:
            current_prompt, current_kind, current_relative = self.entries[index]
            current_metrics = views[index].metrics
            current_findings = views[index].findings
        elapsed: float | None = None
        if status == "recording" and self._started_monotonic is not None:
            elapsed = max(0.0, self.monotonic() - self._started_monotonic)
        return CaptureState(
            session_directory=self.session_directory,
            session_id=self.session_id,
            status=status,
            index=index,
            total=len(self.entries),
            prompt=current_prompt,
            kind=current_kind,
            relative=current_relative,
            metrics=current_metrics,
            findings=current_findings,
            elapsed_seconds=elapsed,
            takes=tuple(views),
        )

    def current(self) -> CaptureState:
        with self._lock:
            return self._state()

    def metrics(self, index: int) -> AudioQcMetrics:
        with self._lock:
            return self._metrics_from(self._inspections(), index)

    def preview_path(self, index: int | None = None) -> Path:
        """Return a re-inspected canonical path for in-memory loopback preview."""

        with self._lock:
            selected = self._review_index if index is None else index
            if selected is None:
                raise ContractError("no reviewed take is available for preview")
            inspections = self._inspections()
            if selected not in inspections:
                raise ContractError(f"take is not available for preview: {selected}")
            return self._recording_path(selected)

    def start_take(self) -> CaptureState:
        with self._lock:
            state = self._state()
            if state.status != "ready" or state.index is None:
                raise ContractError(f"cannot start a take while capture is {state.status}")
            index = state.index
            process = _start_capture(
                destination=self._recording_path(index),
                device_id=self.device.device_id,
                ffmpeg=self.ffmpeg,
                process_factory=self.process_factory,
            )
            self._live_process = process
            self._live_index = index
            self._started_monotonic = self.monotonic()
            self._take_findings.pop(index, None)
            timer = self.timer_factory(
                MAX_TAKE_SECONDS,
                lambda: self._auto_stop(index, process),
            )
            self._timer = timer
            timer.start()
            return self._state()

    def _clear_live(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._live_process = None
        self._live_index = None
        self._started_monotonic = None

    def _stop_take_locked(self, *, automatic: bool) -> CaptureState:
        process = self._live_process
        index = self._live_index
        if process is None or index is None:
            raise ContractError("no recording is in progress")
        try:
            _stop_capture(process=process, destination=self._recording_path(index))
        finally:
            self._clear_live()
        self._review_index = index
        if automatic:
            self._take_findings[index] = ("auto-stopped",)
        return self._state()

    def _auto_stop(self, index: int, process: CaptureProcess) -> None:
        with self._lock:
            if self._live_index != index or self._live_process is not process:
                return
            try:
                self._stop_take_locked(automatic=True)
            except (OSError, VoiceLabError) as error:
                self._background_error = ExternalToolError(f"automatic stop failed: {error}")

    def stop_take(self) -> CaptureState:
        with self._lock:
            return self._stop_take_locked(automatic=False)

    def accept_take(self) -> CaptureState:
        with self._lock:
            if self._review_index is None:
                raise ContractError("no reviewed take is available to accept")
            index = self._review_index
            self._metrics_from(self._inspections(), index)
            self._review_index = None
            return self._state()

    def reject_take(self, reason: str) -> CaptureState:
        with self._lock:
            if self._review_index is None:
                raise ContractError("no reviewed take is available to reject")
            index = self._review_index
            _, _, relative = self.entries[index]
            self._inspections()
            _reject_recording(
                layout=self.layout,
                session_id=self.session_id,
                source=self._recording_path(index),
                canonical_relative=relative,
                reason=reason,
                rejected_at=self.clock(),
            )
            self._review_index = None
            self._take_findings.pop(index, None)
            return self._state()

    def finalize(self) -> CaptureResult:
        with self._lock:
            if self._final_result is not None:
                return self._validated_final_result()
            state = self._state()
            if state.status != "complete":
                raise ContractError(f"cannot finalize while capture is {state.status}")
            self._final_result = finalize_capture_session(
                self.session_directory,
                session_id=self.session_id,
                subject_id=self.subject_id,
                prompt_suite=self.prompt_suite,
                device=self.device,
                captured_at=self.captured_at,
                finalized_at=self.clock(),
                capture_host=self.capture_host,
            )
            return self._final_result

    def abort(self) -> None:
        with self._lock:
            process = self._live_process
            index = self._live_index
            if process is None or index is None:
                return
            partial = self._recording_path(index).with_suffix(".partial.wav")
            try:
                _interrupt_process(process)
            finally:
                self._clear_live()
                _secure_capture_file(partial)
                _fsync_directory(partial.parent)
            raise CaptureInterrupted(self.session_directory, partial)


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

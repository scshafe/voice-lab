"""Catalog-wide recording history and durable desktop capture orchestration.

@canonical recording_library -- desktop history uses validated capture evidence;
all microphone operations remain in capture.TakeEngine.
"""

from __future__ import annotations

import os
import re
import socket
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import Field, ValidationError, field_validator, model_validator

from .audio import inspect_canonical_wav
from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    write_canonical_json,
)
from .capture import (
    DEFAULT_FFMPEG,
    CaptureInterrupted,
    CaptureResult,
    CaptureState,
    ProcessFactory,
    TakeEngine,
    TimerFactory,
    expected_recording_paths,
    validate_rejected_takes,
)
from .capture_native import list_coreaudio_input_devices
from .corpus_takes import is_corpus_take_package
from .datasets import load_pilot_gate
from .errors import ContractError, ExternalToolError, SecurityError
from .intake import ValidatedPackage, validate_capture_package
from .models import (
    CaptureDevice,
    ContractModel,
    Prompt,
    PromptSuite,
    Sha256,
    StableId,
    utc_now,
)
from .paths import AssetLayout, confined_path, ensure_private_directory
from .prompt_catalogs import legacy_pilot_v1, legacy_pilot_v1_suite
from .recording_contracts import CatalogSnapshot
from .recording_edits import (
    RecordingEdit,
    effective_recording_path,
    read_recording_edit,
    source_edit_state,
    update_recording_edit,
)
from .recording_ownership import acquire_capture_ownership
from .recording_takes import RecordingSelectionError

if TYPE_CHECKING:
    from .recording_contracts import (
        NavigateCommand,
        NavigationEvent,
        RecordingHistoryPage,
        SaveEvent,
        SaveTakeCommand,
    )
    from .recording_index import RecordingIndex
    from .recording_takes import RecordingTakeStore


class DesktopTakeEvidence(ContractModel):
    """Take-start event, committed before opening the microphone."""

    prompt_id: StableId
    take_started_at: datetime
    capture_device: CaptureDevice
    audio_sha256: Sha256 | None = None
    accepted: bool = False
    rejected: bool = False

    @field_validator("take_started_at")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("take time must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _accepted_has_audio(self) -> DesktopTakeEvidence:
        if self.accepted and (self.audio_sha256 is None or self.rejected):
            raise ValueError("accepted takes require an audio digest and cannot be rejected")
        return self


class DesktopSessionEvidence(ContractModel):
    """Private provenance sidecar, deliberately outside transferable packages."""

    schema_version: Literal["cole-voice-lab.capture-desktop-session/v1"] = (
        "cole-voice-lab.capture-desktop-session/v1"
    )
    session_id: StableId
    subject_id: StableId
    catalog_suite: PromptSuite
    catalog_sha256: Sha256
    selected_suite: PromptSuite
    session_started_at: datetime
    capture_device: CaptureDevice
    capture_host: str = Field(min_length=1, max_length=255)
    takes: tuple[DesktopTakeEvidence, ...] = ()
    manifest_sha256: Sha256 | None = None

    @field_validator("session_started_at")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("session time must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _catalog_bindings(self) -> DesktopSessionEvidence:
        if canonical_sha256(self.catalog_suite) != self.catalog_sha256:
            raise ValueError("catalog digest does not match the exact prompt suite")
        selected = {prompt.prompt_id for prompt in self.selected_suite.prompts}
        expected = tuple(p for p in self.catalog_suite.prompts if p.prompt_id in selected)
        if expected != self.selected_suite.prompts:
            raise ValueError("selected prompts must retain exact catalog content and order")
        if self.selected_suite.purpose != self.catalog_suite.purpose:
            raise ValueError("selected suite must retain catalog purpose")
        if self.selected_suite.prompts == self.catalog_suite.prompts:
            if self.selected_suite != self.catalog_suite:
                raise ValueError("a complete selected suite must retain the exact catalog")
        else:
            digest = canonical_sha256(
                [prompt.model_dump(mode="json") for prompt in self.selected_suite.prompts]
            )[:16]
            if self.selected_suite.suite_id != f"desktop-subset-{digest}":
                raise ValueError("subset suite ID must bind the exact selected prompts")
        if any(
            p.category == "room-tone" and p.prompt_id not in selected
            for p in self.catalog_suite.prompts
        ):
            raise ValueError("selected session must include every catalog room-tone prompt")
        live_ids = [take.prompt_id for take in self.takes if not take.rejected]
        if len(live_ids) != len(set(live_ids)):
            raise ValueError("only one current take per prompt is allowed")
        if any(take.prompt_id not in selected for take in self.takes):
            raise ValueError("take evidence must belong to the selected suite")
        if any(take.take_started_at < self.session_started_at for take in self.takes):
            raise ValueError("a take cannot precede its session")
        return self


@dataclass(frozen=True, slots=True)
class RecordingHistory:
    """A byte-validated take, with explicitly qualified date precision.

    ``recorded_prompt`` is the exact prompt the audio was captured against: the library
    catalog's prompt ``prompt_id`` itself, or an earlier revision of it that the catalog's
    prompt aliases still accept (keep-recordings edits).
    """

    session_id: str
    prompt_id: str
    recorded_at: datetime
    date_precision: Literal["take", "session"]
    status: str
    relative_path: str
    sha256: str
    session_directory: Path
    recorded_prompt: Prompt
    native_take_id: str | None = None

    def __post_init__(self) -> None:
        if self.recorded_prompt.prompt_id != self.prompt_id:
            raise ValueError("recording history must name its recorded prompt's id")


@dataclass(frozen=True, slots=True)
class RecordingRow:
    """One catalog prompt, including never-recorded prompts."""

    prompt: Prompt
    status: str
    latest_recorded_at: datetime | None
    take_count: int
    history: tuple[RecordingHistory, ...]


@dataclass(frozen=True, slots=True)
class RecordingSessionInfo:
    """Session discovery result; unknown legacy dates stay unknown."""

    session_id: str
    session_started_at: datetime | None
    prompt_count: int
    recorded_count: int
    status: str
    resumable: bool
    catalog_known: bool = True


def _read_sidecar(path: Path) -> DesktopSessionEvidence:
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecurityError(f"desktop provenance must be one private regular file: {path}")
        payload = stream.read()
    try:
        evidence = DesktopSessionEvidence.model_validate_json(payload)
    except ValidationError as error:
        raise ContractError(f"invalid desktop session provenance {path}: {error}") from error
    if payload != canonical_json_bytes(evidence) + b"\n":
        raise SecurityError(f"desktop session provenance is not canonical: {path}")
    if path.name != f"{evidence.session_id}.json":
        raise SecurityError(f"desktop provenance filename does not bind session ID: {path}")
    return evidence


class RecordingLibrary:
    """Persistent catalog and session facade for the standalone desktop window."""

    def __init__(
        self,
        layout: AssetLayout,
        prompt_suite: PromptSuite | CatalogSnapshot,
        subject_id: str = "cole-self",
        *,
        ffmpeg: str = DEFAULT_FFMPEG,
        process_factory: ProcessFactory | None = None,
        clock: Callable[[], datetime] = utc_now,
        timer_factory: TimerFactory | None = None,
        pilot_gate_path: str | None = None,
        pilot_gate_sha256: str | None = None,
        device_resolver: Callable[[CaptureDevice], CaptureDevice] | None = None,
    ) -> None:
        """Bind one asset root, exact catalog and subject.

        Params: ``prompt_suite`` is the exact catalog, either a plain ``PromptSuite`` (a
        built-in or embedded catalog; nothing but its own prompts counts) or a
        ``CatalogSnapshot`` whose prompt aliases also admit legacy takes recorded against an
        accepted earlier revision of a prompt (same prompt id). The attributes
        ``prompt_suite`` and ``catalog_sha256`` always describe the suite itself, and
        ``catalog`` is the snapshot (with no aliases for a plain suite).
        Raises: ``SecurityError`` / ``OSError`` when the asset root is not a valid private
        layout.
        """
        self.layout = AssetLayout.validate(layout.root)
        self.catalog = (
            prompt_suite
            if isinstance(prompt_suite, CatalogSnapshot)
            else CatalogSnapshot.embedded(prompt_suite)
        )
        self.prompt_suite = self.catalog.suite
        self.catalog_sha256 = self.catalog.catalog_sha256
        self.subject_id = subject_id
        self.ffmpeg = ffmpeg
        self.process_factory = process_factory
        self.clock = clock
        self.timer_factory = timer_factory
        self.pilot_gate_path = pilot_gate_path
        self.pilot_gate_sha256 = pilot_gate_sha256
        self.device_resolver = device_resolver or self._resolve_device
        self._active: DesktopRecordingSession | None = None

    def _resolve_device(self, stored: CaptureDevice) -> CaptureDevice:
        if stored.backend != "coreaudio":
            raise ContractError(
                "This session uses the older microphone recorder. End this session and "
                "start a new recording session to use continuity-checked native capture. "
                "Earlier takes remain available."
            )
        matches = [
            device
            for device in list_coreaudio_input_devices()
            if device.device_id == stored.device_id and device.display_name == stored.display_name
        ]
        if len(matches) != 1:
            raise ContractError(
                "the selected UMIK-1 is absent or ambiguous; reconnect it "
                "and refresh microphones before recording"
            )
        # Discovery proves physical identity; serial/calibration evidence belongs
        # to the saved selection and must not be erased by a fresh enumeration.
        return stored

    def counts(self, recorded: Prompt) -> bool:
        """Whether a take recorded against ``recorded`` belongs to this catalog's history.

        True when ``recorded`` is exactly one of the catalog's prompts, or an earlier revision
        of one (same prompt id) that the catalog's prompt aliases accept
        (``CatalogSnapshot.current_prompt_for``). With a plain suite only exact prompts count.
        Raises: nothing.
        """
        return self.catalog.current_prompt_for(recorded) is not None

    def _require_capture_gate(self) -> None:
        if self.prompt_suite.purpose != "capture-corpus":
            return
        if self.pilot_gate_path is None or self.pilot_gate_sha256 is None:
            raise ContractError("full corpus capture requires a checksum-bound pilot gate approval")
        gate = load_pilot_gate(
            self.layout, relative_path=self.pilot_gate_path, expected_sha256=self.pilot_gate_sha256
        )
        if gate.record.approved_at > self.clock():
            raise ContractError("pilot-gate approval cannot be in the future")
        for digest in gate.record.pilot_manifest_sha256s:
            package = validate_capture_package(
                confined_path(self.layout.root, f"raw/{digest}", must_exist=True),
                allow_intake_receipt=True,
            )
            if package.manifest.subject_id != self.subject_id:
                raise ContractError("pilot-gate capture belongs to a different subject")

    def _provenance_directory(self, *, create: bool = False) -> Path:
        path = confined_path(self.layout.root, "provenance/capture-desktop")
        if create:
            return ensure_private_directory(path)
        if path.exists():
            metadata = path.stat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise SecurityError(f"desktop provenance directory must be private: {path}")
        return path

    def _sidecar_path(self, session_id: str) -> Path:
        return confined_path(self._provenance_directory(), f"{session_id}.json")

    def _lock(self) -> int:
        return acquire_capture_ownership(self.layout)

    def _engine(
        self, evidence: DesktopSessionEvidence, *, resume: bool, ownership_descriptor: int
    ) -> TakeEngine:
        # Keep optional dependency injection typed without replacing the canonical engine.
        engine = TakeEngine(
            layout=self.layout,
            prompt_suite=evidence.selected_suite,
            subject_id=evidence.subject_id,
            device=evidence.capture_device,
            session_id=evidence.session_id,
            ffmpeg=self.ffmpeg,
            clock=self.clock,
            capture_host=evidence.capture_host,
            resume=resume,
            allow_prompt_selection=True,
            capture_ownership_descriptor=ownership_descriptor,
        )
        if self.process_factory is not None:
            engine.process_factory = self.process_factory
        if self.timer_factory is not None:
            engine.timer_factory = self.timer_factory
        engine.captured_at = evidence.session_started_at
        return engine

    def begin_session(
        self, prompt_ids: Sequence[str], device: CaptureDevice
    ) -> DesktopRecordingSession:
        """Create a unique session containing selected prompts plus all room tones."""
        self._require_capture_gate()
        selected = set(prompt_ids)
        known = {prompt.prompt_id for prompt in self.prompt_suite.prompts}
        if not selected or selected - known:
            raise ContractError("select at least one known catalog prompt to record")
        if self.prompt_suite.purpose == "held-out-evaluation":
            raise ContractError("held-out evaluation prompts cannot be recorded as training audio")
        selected.update(p.prompt_id for p in self.prompt_suite.prompts if p.category == "room-tone")
        prompts = tuple(p for p in self.prompt_suite.prompts if p.prompt_id in selected)
        if not any(p.category == "room-tone" for p in prompts):
            raise ContractError("a recording catalog must include room tone")
        if prompts == self.prompt_suite.prompts:
            suite = self.prompt_suite
        else:
            subset_hash = canonical_sha256([p.model_dump(mode="json") for p in prompts])[:16]
            suite = PromptSuite(
                suite_id=f"desktop-subset-{subset_hash}",
                purpose=self.prompt_suite.purpose,
                prompts=prompts,
            )
        evidence = DesktopSessionEvidence(
            session_id=f"desktop-{uuid4().hex}",
            subject_id=self.subject_id,
            catalog_suite=self.prompt_suite,
            catalog_sha256=self.catalog_sha256,
            selected_suite=suite,
            session_started_at=self.clock(),
            capture_device=device,
            capture_host=socket.gethostname(),
        )
        descriptor = self._lock()
        try:
            engine = self._engine(evidence, resume=False, ownership_descriptor=descriptor)
            write_canonical_json(self._sidecar_path(evidence.session_id), evidence)
            session = DesktopRecordingSession(self, evidence, engine, descriptor)
            self._active = session
            return session
        except BaseException:
            os.close(descriptor)
            raise

    def resume_session(self, session_id: str) -> DesktopRecordingSession:
        """Resume private desktop provenance, retaining original take and session dates."""
        self._require_capture_gate()
        evidence = _read_sidecar(self._sidecar_path(session_id))
        if evidence.subject_id != self.subject_id or evidence.catalog_suite != self.prompt_suite:
            raise ContractError("session belongs to a different subject or exact prompt catalog")
        descriptor = self._lock()
        try:
            session = DesktopRecordingSession(
                self,
                evidence,
                self._engine(evidence, resume=True, ownership_descriptor=descriptor),
                descriptor,
            )
            session._restore_review()
            self._active = session
            return session
        except BaseException:
            os.close(descriptor)
            raise

    def _sidecars(self) -> dict[str, DesktopSessionEvidence]:
        directory = self._provenance_directory()
        if not directory.exists():
            return {}
        result: dict[str, DesktopSessionEvidence] = {}
        for path in sorted(directory.iterdir()):
            if path.name == ".capture.lock":
                confined_path(directory, path.name, must_exist=True)
                continue
            path = confined_path(directory, path.name, must_exist=True)
            if re.fullmatch(r"\.[a-z0-9][a-z0-9._-]*\.json\.tmp-[0-9a-f]{16}", path.name):
                metadata = path.stat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise SecurityError(f"unsafe interrupted provenance write: {path}")
                continue
            if path.suffix != ".json":
                raise SecurityError(f"unexpected desktop provenance member: {path}")
            evidence = _read_sidecar(path)
            result[evidence.session_id] = evidence
        return result

    def _recover_rejections(
        self, evidence: DesktopSessionEvidence
    ) -> tuple[DesktopSessionEvidence, tuple[RecordingHistory, ...]]:
        """Reconcile a completed canonical rejection after a sidecar-write interruption."""
        log = validate_rejected_takes(self.layout, evidence.session_id)
        paths = dict(
            zip(
                (p.prompt_id for p in evidence.selected_suite.prompts),
                expected_recording_paths(evidence.selected_suite),
                strict=True,
            )
        )
        canonical_directory = confined_path(self.layout.root, f"tmp/capture-{evidence.session_id}")
        rejected_directory = confined_path(
            self.layout.root, f"tmp/capture-{evidence.session_id}.rejected"
        )
        selected = {prompt.prompt_id: prompt for prompt in evidence.selected_suite.prompts}
        consumed: set[str] = set()
        takes = list(evidence.takes)
        history: list[RecordingHistory] = []
        for index, take in enumerate(takes):
            if take.audio_sha256 is None:
                continue
            if not take.rejected and (
                evidence.manifest_sha256 is not None
                or confined_path(canonical_directory, paths[take.prompt_id]).exists()
            ):
                continue
            prefix = f"{Path(paths[take.prompt_id]).stem}.take-"
            entry = next(
                (
                    entry
                    for entry in log.entries
                    if entry.relative.startswith(prefix)
                    and entry.sha256 == take.audio_sha256
                    and entry.relative not in consumed
                ),
                None,
            )
            if entry is None:
                raise SecurityError(
                    f"desktop rejected take lacks forensic evidence: {take.prompt_id}"
                )
            if take.accepted:
                raise SecurityError("an accepted take cannot be silently rejected")
            consumed.add(entry.relative)
            takes[index] = take.model_copy(update={"rejected": True})
            history.append(
                RecordingHistory(
                    session_id=evidence.session_id,
                    prompt_id=take.prompt_id,
                    recorded_at=take.take_started_at,
                    date_precision="take",
                    status="Rejected",
                    relative_path=entry.relative,
                    sha256=entry.sha256,
                    session_directory=rejected_directory,
                    recorded_prompt=selected[take.prompt_id],
                )
            )
        return evidence.model_copy(update={"takes": tuple(takes)}), tuple(history)

    def _partial_history(
        self, evidence: DesktopSessionEvidence, directory: Path
    ) -> tuple[tuple[RecordingHistory, ...], int]:
        expected = expected_recording_paths(evidence.selected_suite)
        allowed = set(expected) | {str(Path(p).with_suffix(".partial.wav")) for p in expected}
        files: set[str] = set()
        for path in directory.rglob("*"):
            relative = path.relative_to(directory).as_posix()
            safe = confined_path(directory, relative, must_exist=True)
            metadata = safe.stat()
            if stat.S_ISDIR(metadata.st_mode):
                if relative != "audio":
                    raise SecurityError(f"unexpected capture directory: {path}")
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise SecurityError(f"capture recording must be private: {path}")
                files.add(relative)
            else:
                raise SecurityError(f"capture member must be one regular file: {path}")
        if files - allowed:
            raise SecurityError(f"unexpected capture members: {sorted(files - allowed)}")
        history: list[RecordingHistory] = []
        recorded_indices: list[int] = []
        for index, (prompt, relative) in enumerate(
            zip(evidence.selected_suite.prompts, expected, strict=True)
        ):
            take = next(
                (t for t in evidence.takes if t.prompt_id == prompt.prompt_id and not t.rejected),
                None,
            )
            if relative not in files:
                if take is not None and take.audio_sha256 is not None:
                    raise SecurityError(f"recorded take disappeared: {directory / relative}")
                continue
            if str(Path(relative).with_suffix(".partial.wav")) in files:
                raise SecurityError(f"canonical and partial take both exist: {relative}")
            recorded_indices.append(index)
            if take is None:
                raise SecurityError(f"recording has no durable take-start evidence: {relative}")
            inspection = inspect_canonical_wav(confined_path(directory, relative, must_exist=True))
            if take.audio_sha256 is not None and inspection.sha256 != take.audio_sha256:
                raise SecurityError(f"desktop recording hash changed: {relative}")
            metrics = inspection.qc_metrics(relative, allow_quiet=prompt.allows_quiet)
            history.append(
                RecordingHistory(
                    session_id=evidence.session_id,
                    prompt_id=prompt.prompt_id,
                    recorded_at=take.take_started_at,
                    date_precision="take",
                    status="Recorded" if take.accepted and metrics.passed else "Needs review",
                    relative_path=relative,
                    sha256=inspection.sha256,
                    session_directory=directory,
                    recorded_prompt=prompt,
                )
            )
        return tuple(history), len(recorded_indices)

    def _package_history(
        self,
        package: ValidatedPackage,
        directory: Path,
        evidence: DesktopSessionEvidence | None,
        *,
        raw: bool,
    ) -> tuple[RecordingHistory, ...]:
        manifest = package.manifest
        if manifest.subject_id != self.subject_id:
            return ()
        from .recording_packages import load_recording_package_map, resolve_native_package_alias

        native_map = load_recording_package_map(self.layout, manifest.session_id, package=package)
        if native_map is not None:
            native_history: list[RecordingHistory] = []
            for entry in native_map.entries:
                if not self.counts(entry.prompt):
                    continue
                native_take = resolve_native_package_alias(
                    self.layout,
                    subject_id=self.subject_id,
                    session_id=manifest.session_id,
                    prompt_id=entry.prompt.prompt_id,
                    source_sha256=entry.original_sha256,
                    manifest_sha256=package.manifest_sha256,
                )
                if native_take is None:
                    raise SecurityError("native package map lost its original take")
                native_history.append(
                    RecordingHistory(
                        session_id=manifest.session_id,
                        prompt_id=entry.prompt.prompt_id,
                        recorded_at=entry.recorded_at,
                        date_precision="take",
                        status="Removed"
                        if native_take.decision == "removed"
                        else "Rejected"
                        if native_take.decision == "rejected"
                        else "Accepted raw"
                        if raw
                        else "Finalized",
                        relative_path=entry.package_relative_path,
                        sha256=entry.original_sha256,
                        session_directory=directory,
                        recorded_prompt=entry.prompt,
                        native_take_id=entry.take_id,
                    )
                )
            return tuple(native_history)
        if evidence is None:
            if manifest.prompt_suite_id == "cole-pilot-v1" and legacy_pilot_v1(
                self.prompt_suite,
                aliases=(alias.accepted_prompt_sha256 for alias in self.catalog.prompt_aliases),
            ):
                # Legacy manifests contain transcripts but no style metadata. Only the
                # pinned pilot-v1 identifies the entire old suite; the exact filter below
                # admits just the prompts this catalog counts (its own or an accepted
                # earlier revision), so rows never depend on it.
                suite = legacy_pilot_v1_suite(self.prompt_suite)
            elif manifest.session_id.startswith("native-"):
                return ()  # Foreign v1 carries no exact style/catalog or individual take dates.
            elif manifest.prompt_suite_id == self.prompt_suite.suite_id:
                raise ContractError(
                    "legacy capture lacks exact style/catalog provenance; "
                    f"cannot assign its takes to this catalog: {directory}"
                )
            else:
                return ()
        else:
            suite = evidence.selected_suite
            if (
                manifest.subject_id != evidence.subject_id
                or manifest.prompt_suite_id != suite.suite_id
                or manifest.captured_at != evidence.session_started_at
                or manifest.capture_device != evidence.capture_device
                or manifest.capture_host != evidence.capture_host
                or (
                    evidence.manifest_sha256 is not None
                    and package.manifest_sha256 != evidence.manifest_sha256
                )
            ):
                raise SecurityError(
                    f"desktop provenance and finalized manifest disagree: {directory}"
                )
        expected = expected_recording_paths(suite)
        if tuple(file.path for file in manifest.files) != expected:
            raise SecurityError(
                f"finalized recording paths disagree with prompt suite: {directory}"
            )
        result: list[RecordingHistory] = []
        for prompt, file in zip(suite.prompts, manifest.files, strict=True):
            if prompt.category != "room-tone" and (
                file.prompt_id != prompt.prompt_id or file.transcript != prompt.text
            ):
                raise SecurityError(
                    f"finalized transcript disagrees with exact prompt: {file.path}"
                )
            if not self.counts(prompt):
                continue
            take = (
                None
                if evidence is None
                else next(
                    (
                        t
                        for t in evidence.takes
                        if t.prompt_id == prompt.prompt_id and not t.rejected
                    ),
                    None,
                )
            )
            if evidence is not None and (
                take is None or not take.accepted or take.audio_sha256 != file.sha256
            ):
                raise SecurityError(
                    f"finalized take lacks matching accepted provenance: {file.path}"
                )
            result.append(
                RecordingHistory(
                    session_id=manifest.session_id,
                    prompt_id=prompt.prompt_id,
                    recorded_at=manifest.captured_at if take is None else take.take_started_at,
                    date_precision="session" if take is None else "take",
                    status="Accepted raw" if raw else "Finalized",
                    relative_path=file.path,
                    sha256=file.sha256,
                    session_directory=directory,
                    recorded_prompt=prompt,
                )
            )
        return tuple(result)

    def _scan(self) -> tuple[tuple[RecordingHistory, ...], tuple[RecordingSessionInfo, ...]]:
        AssetLayout.validate(self.layout.root)
        if self._active is not None:
            self._active.current()
        sidecars = self._sidecars()
        history: list[RecordingHistory] = []
        for session_id, sidecar_evidence in sidecars.items():
            recovered, rejected = self._recover_rejections(sidecar_evidence)
            sidecars[session_id] = recovered
            matching = {
                p.prompt_id for p in sidecar_evidence.selected_suite.prompts if self.counts(p)
            }
            if sidecar_evidence.subject_id == self.subject_id:
                history.extend(entry for entry in rejected if entry.prompt_id in matching)
        sessions: list[RecordingSessionInfo] = []
        seen_manifests: set[str] = set()
        seen_sessions: set[str] = set()
        # Raw first makes deduplication prefer content-addressed accepted evidence.
        for name in ("raw", "tmp"):
            root = self.layout.directory(name)
            for child in sorted(root.iterdir()):
                if name == "tmp" and not child.name.startswith("capture-"):
                    continue
                if name == "tmp" and child.name.endswith(".rejected"):
                    validate_rejected_takes(
                        self.layout, child.name.removeprefix("capture-").removesuffix(".rejected")
                    )
                    continue
                directory = confined_path(root, child.name, must_exist=True)
                if not directory.is_dir():
                    raise SecurityError(f"capture package must be a real directory: {directory}")
                if name == "raw" and is_corpus_take_package(directory):
                    # A take of the PC's corpus (D-052), or a Swift take imported into it
                    # (D-053): the PC's corpus API serves it.
                    continue
                ready = confined_path(directory, "READY")
                if ready.exists() or name == "raw":
                    package = validate_capture_package(
                        directory, allow_intake_receipt=name == "raw"
                    )
                    if name == "raw" and directory.name != package.manifest_sha256:
                        raise SecurityError(f"raw directory is not content-addressed: {directory}")
                    evidence = sidecars.get(package.manifest.session_id)
                    if evidence is None and package.manifest.session_id.startswith("desktop-"):
                        raise SecurityError("desktop capture provenance disappeared")
                    entries = self._package_history(package, directory, evidence, raw=name == "raw")
                    seen_sessions.add(package.manifest.session_id)
                    if package.manifest_sha256 in seen_manifests:
                        continue
                    seen_manifests.add(package.manifest_sha256)
                    history.extend(entries)
                    if entries or (
                        package.manifest.session_id.startswith("native-")
                        and package.manifest.subject_id == self.subject_id
                    ):
                        sessions.append(
                            RecordingSessionInfo(
                                session_id=package.manifest.session_id,
                                session_started_at=package.manifest.captured_at,
                                prompt_count=len(package.manifest.files),
                                recorded_count=len(package.manifest.files),
                                status=("Accepted raw" if name == "raw" else "Finalized")
                                + (" (catalog unknown)" if not entries else ""),
                                resumable=False,
                                catalog_known=bool(entries),
                            )
                        )
                    continue
                session_id = directory.name.removeprefix("capture-")
                seen_sessions.add(session_id)
                evidence = sidecars.get(session_id)
                if evidence is None:
                    if session_id.startswith("desktop-"):
                        raise SecurityError("desktop capture provenance disappeared")
                    sessions.append(
                        RecordingSessionInfo(
                            session_id=session_id,
                            session_started_at=None,
                            prompt_count=0,
                            recorded_count=0,
                            status="Legacy unfinished (original capture tools)",
                            resumable=False,
                        )
                    )
                    continue
                entries, count = self._partial_history(evidence, directory)
                if evidence.subject_id != self.subject_id:
                    continue
                exact = evidence.catalog_suite == self.prompt_suite
                matching = {p.prompt_id for p in evidence.selected_suite.prompts if self.counts(p)}
                history.extend(entry for entry in entries if entry.prompt_id in matching)
                sessions.append(
                    RecordingSessionInfo(
                        session_id=session_id,
                        session_started_at=evidence.session_started_at,
                        prompt_count=len(evidence.selected_suite.prompts),
                        recorded_count=count,
                        status="Ready to finalize"
                        if count == len(evidence.selected_suite.prompts)
                        else "In progress",
                        resumable=exact,
                    )
                )
        missing = set(sidecars) - seen_sessions
        if missing:
            raise SecurityError(f"desktop session directories disappeared: {sorted(missing)}")
        effective_history: list[RecordingHistory] = []
        for entry in history:
            edit = read_recording_edit(
                self.layout,
                subject_id=self.subject_id,
                session_id=entry.session_id,
                prompt_id=entry.prompt_id,
                source_sha256=entry.sha256,
            )
            effective_history.append(
                replace(entry, status="Removed") if edit is not None and edit.removed else entry
            )
        return tuple(effective_history), tuple(sessions)

    def verified_history_snapshot(
        self,
    ) -> tuple[
        tuple[RecordingHistory, ...],
        tuple[RecordingSessionInfo, ...],
        tuple[DesktopSessionEvidence, ...],
    ]:
        """Explicit strict legacy backfill boundary; never used by indexed UI queries.

        Preserves the existing canonical package/rejection/edit validations. This
        may inspect historical WAVs and belongs on the background backfill executor.
        """
        history, sessions = self._scan()
        return history, sessions, tuple(self._sidecars().values())

    def bind_native_store(self, store: RecordingTakeStore, index: RecordingIndex) -> None:
        """Attach one authoritative take store to this library's disposable projection."""
        if (
            store.layout != self.layout
            or index.layout != self.layout
            or index.subject_id != self.subject_id
        ):
            raise ContractError("native library context does not match its asset root/subject")
        store.projection_hook = index.update_take
        store.navigation_resolver = index.resolve_navigation
        store.index_revision_resolver = lambda: index.revision
        store.missing_resolver = lambda digest, prompt: index.next_missing(
            digest, after_prompt_id=prompt
        )
        store.retained_take_resolver = index.latest_retained_take

    def save_native_take(
        self,
        store: RecordingTakeStore,
        command: SaveTakeCommand,
        *,
        commit_barrier: Callable[[], None] | None = None,
    ) -> SaveEvent:
        """Keep this exact draft and its final navigation using the shared durable operation."""
        if command.selection.catalog_sha256 != self.catalog_sha256:
            raise RecordingSelectionError("Save belongs to another exact library catalog")
        return store.save(command, commit_barrier=commit_barrier, subject_id=self.subject_id)

    def navigate_native_recording(
        self,
        store: RecordingTakeStore,
        command: NavigateCommand,
        *,
        commit_barrier: Callable[[], None] | None = None,
    ) -> NavigationEvent:
        """Select a retake/one-off or continue canonical missing coverage; never start audio."""
        if command.selection.catalog_sha256 != self.catalog_sha256:
            raise RecordingSelectionError("navigation belongs to another exact library catalog")
        return store.navigate(command, subject_id=self.subject_id, commit_barrier=commit_barrier)

    def indexed_history(
        self,
        index: RecordingIndex,
        *,
        prompt_id: str | None = None,
        cursor: str | None = None,
        prompt_cursor: str | None = None,
        group_cursor: str | None = None,
        page_size: int = 50,
    ) -> RecordingHistoryPage:
        """Return one bounded metadata snapshot without opening historical audio."""
        if index.layout != self.layout or index.subject_id != self.subject_id:
            raise ContractError("indexed library context does not match this asset root/subject")
        return index.history(
            self.catalog_sha256,
            prompt_id=prompt_id,
            cursor=cursor,
            prompt_cursor=prompt_cursor,
            group_cursor=group_cursor,
            page_size=page_size,
        )

    def rows(self) -> tuple[RecordingRow, ...]:
        """Return the entire catalog; broken evidence raises instead of looking missing."""
        history, _ = self._scan()
        result: list[RecordingRow] = []
        for prompt in self.prompt_suite.prompts:
            takes = tuple(
                sorted(
                    (h for h in history if h.prompt_id == prompt.prompt_id),
                    key=lambda h: (h.recorded_at, h.session_id),
                    reverse=True,
                )
            )
            retained = tuple(take for take in takes if take.status not in {"Rejected", "Removed"})
            result.append(
                RecordingRow(
                    prompt=prompt,
                    status=retained[0].status if retained else "Missing",
                    latest_recorded_at=retained[0].recorded_at if retained else None,
                    take_count=len(takes),
                    history=takes,
                )
            )
        return tuple(result)

    def sessions(self) -> tuple[RecordingSessionInfo, ...]:
        """List completed and resumable sessions, preserving unknown legacy dates."""
        return self._scan()[1]

    def preview_path(self, history: RecordingHistory) -> Path:
        """Return the validated effective take; trims never replace original capture bytes."""
        source = self.original_preview_path(history)
        edit = self._edit_state(history, source)
        return effective_recording_path(self.layout, source, edit)

    def original_preview_path(self, history: RecordingHistory) -> Path:
        """Validate the selected history entry again before original waveform access."""
        available, _ = self._scan()
        if history not in available:
            raise SecurityError("selected recording history changed; refresh before playback")
        path = confined_path(history.session_directory, history.relative_path, must_exist=True)
        if inspect_canonical_wav(path).sha256 != history.sha256:
            raise SecurityError("selected preview recording hash changed")
        return path

    def _edit_state(self, history: RecordingHistory, source: Path) -> RecordingEdit:
        return source_edit_state(
            self.layout,
            source,
            subject_id=self.subject_id,
            session_id=history.session_id,
            prompt_id=history.prompt_id,
            source_sha256=history.sha256,
            updated_at=self.clock(),
        )

    def edit_state(self, history: RecordingHistory) -> RecordingEdit:
        """Read a take's frame bounds and recoverable removal state without writing assets."""
        return self._edit_state(history, self.original_preview_path(history))

    def save_trim(
        self, history: RecordingHistory, start_frame: int, end_frame: int
    ) -> RecordingEdit:
        """Save a frame-exact derivative; retain the unmodified original and earlier derivatives."""
        source = self.original_preview_path(history)
        return update_recording_edit(
            self.layout,
            source,
            self._edit_state(history, source),
            start_frame=start_frame,
            end_frame=end_frame,
            updated_at=self.clock(),
        )

    def clear_trim(self, history: RecordingHistory) -> RecordingEdit:
        """Restore original playback without deleting any recorded or derived audio."""
        source = self.original_preview_path(history)
        state = self._edit_state(history, source)
        return update_recording_edit(
            self.layout,
            source,
            state,
            start_frame=0,
            end_frame=state.source_frames,
            updated_at=self.clock(),
        )

    def remove_take(self, history: RecordingHistory, reason: str) -> RecordingEdit:
        """Recoverably exclude one hash-bound take from usable coverage and downstream use."""
        source = self.original_preview_path(history)
        return update_recording_edit(
            self.layout,
            source,
            self._edit_state(history, source),
            removed=True,
            reason=reason,
            updated_at=self.clock(),
        )

    def restore_take(self, history: RecordingHistory) -> RecordingEdit:
        """Restore an excluded take without changing capture dates, raw bytes, or trim choices."""
        source = self.original_preview_path(history)
        return update_recording_edit(
            self.layout,
            source,
            self._edit_state(history, source),
            removed=False,
            updated_at=self.clock(),
        )


class DesktopRecordingSession:
    """Durable event evidence around the canonical selectable take engine."""

    def __init__(
        self,
        library: RecordingLibrary,
        evidence: DesktopSessionEvidence,
        engine: TakeEngine,
        descriptor: int,
    ) -> None:
        self.library = library
        self.evidence = evidence
        self.engine = engine
        self._descriptor: int | None = descriptor

    def _persist(self, evidence: DesktopSessionEvidence) -> None:
        self._require_open()
        evidence = DesktopSessionEvidence.model_validate(evidence.model_dump())
        if _read_sidecar(self.library._sidecar_path(evidence.session_id)) != self.evidence:
            raise SecurityError("desktop session provenance changed during capture")
        write_canonical_json(self.library._sidecar_path(evidence.session_id), evidence)
        self.evidence = evidence

    def _require_open(self) -> None:
        if self._descriptor is None:
            raise ContractError("desktop session is closed; resume it before recording")

    def _update_take(self, index: int, **updates: object) -> None:
        takes = list(self.evidence.takes)
        data = takes[index].model_dump()
        data.update(updates)
        takes[index] = DesktopTakeEvidence.model_validate(data)
        self._persist(self.evidence.model_copy(update={"takes": tuple(takes)}))

    def _current_take_index(self, prompt_id: str) -> int:
        indices = [
            index
            for index, take in enumerate(self.evidence.takes)
            if take.prompt_id == prompt_id and not take.rejected
        ]
        if len(indices) != 1:
            raise SecurityError(f"expected exactly one live desktop take for {prompt_id}")
        return indices[0]

    def _restore_review(self) -> None:
        recovered, _ = self.library._recover_rejections(self.evidence)
        if recovered != self.evidence:
            self._persist(recovered)
        state = self.current()
        restored_review = False
        for view in state.takes:
            if view.metrics is not None:
                index = self._current_take_index(view.prompt.prompt_id)
                if not self.evidence.takes[index].accepted and not restored_review:
                    self.engine.select_take(view.index)
                    restored_review = True
            else:
                for index, take in enumerate(self.evidence.takes):
                    if take.prompt_id == view.prompt.prompt_id and not take.rejected:
                        if take.audio_sha256 is not None:
                            raise SecurityError("a persisted desktop take disappeared")
                        self._update_take(index, rejected=True)

    def current(self) -> CaptureState:
        """Poll engine state and finish a pending start-event digest after automatic stop."""
        if _read_sidecar(self.library._sidecar_path(self.evidence.session_id)) != self.evidence:
            raise SecurityError("desktop session provenance changed during capture")
        state = self.engine.current()
        for view in state.takes:
            if view.metrics is None:
                continue
            index = self._current_take_index(view.prompt.prompt_id)
            take = self.evidence.takes[index]
            path = confined_path(state.session_directory, view.relative, must_exist=True)
            inspection = inspect_canonical_wav(path)
            if take.audio_sha256 is None:
                self._update_take(index, audio_sha256=inspection.sha256)
            elif take.audio_sha256 != inspection.sha256:
                raise SecurityError(f"desktop take hash changed: {view.relative}")
        # Captured drafts remain unaccepted when the operator selects another
        # prompt. File presence alone must never turn those drafts into Keep.
        unaccepted = {
            take.prompt_id
            for take in self.evidence.takes
            if not take.accepted and not take.rejected and take.audio_sha256 is not None
        }
        if state.status == "complete" and unaccepted:
            index = next(v.index for v in state.takes if v.prompt.prompt_id in unaccepted)
            state = self.engine.select_take(index)
        return replace(
            state,
            takes=tuple(
                replace(view, status="review") if view.prompt.prompt_id in unaccepted else view
                for view in state.takes
            ),
        )

    def select_prompt(self, prompt_id: str) -> CaptureState:
        """Orient capture on one selected-suite prompt, retaining existing drafts."""
        self._require_open()
        self.current()
        indices = [
            index
            for index, prompt in enumerate(self.evidence.selected_suite.prompts)
            if prompt.prompt_id == prompt_id
        ]
        if not indices:
            raise ContractError("selected prompt does not belong to the open recording batch")
        self.engine.select_take(indices[0])
        return self.current()

    def start_take(self) -> CaptureState:
        """Commit an exact UTC start time before opening the selected microphone."""
        self._require_open()
        state = self.current()
        if state.status != "ready" or state.prompt is None:
            raise ContractError(f"cannot start a desktop take while capture is {state.status}")
        resolved = self.library.device_resolver(self.evidence.capture_device)
        if (
            resolved.model_copy(update={"device_id": self.evidence.capture_device.device_id})
            != self.evidence.capture_device
        ):
            raise ContractError("microphone resolution changed the selected physical device")
        if resolved != self.evidence.capture_device:
            self._persist(self.evidence.model_copy(update={"capture_device": resolved}))
            self.engine.device = resolved
        take = DesktopTakeEvidence(
            prompt_id=state.prompt.prompt_id,
            take_started_at=self.library.clock(),
            capture_device=resolved,
        )
        self._persist(self.evidence.model_copy(update={"takes": (*self.evidence.takes, take)}))
        try:
            self.engine.start_take()
            # Startup is not acknowledged until the final checked state reaches
            # the caller; a late progress/provenance failure must stop the mic too.
            return self.current()
        except BaseException as startup_error:
            cleanup_failures: list[str] = []
            try:
                self.engine.abort()
            except CaptureInterrupted as interrupted:
                startup_error.add_note(str(interrupted))
            except Exception as cleanup_error:
                cleanup_failures.append(f"stop capture: {cleanup_error}")
            try:
                self._update_take(len(self.evidence.takes) - 1, rejected=True)
            except Exception as persistence_error:
                cleanup_failures.append(f"record rejected start: {persistence_error}")
            if cleanup_failures:
                message = (
                    f"Desktop capture startup failed: {startup_error}; "
                    "cleanup also failed: " + "; ".join(cleanup_failures)
                )
                if isinstance(startup_error, Exception):
                    raise ExternalToolError(message) from startup_error
                startup_error.add_note(message)
            raise

    def stop_take(self) -> CaptureState:
        """Stop through TakeEngine and bind the persisted timestamp to the audio digest."""
        self._require_open()
        self.engine.stop_take()
        return self.current()

    def accept_take(self) -> CaptureState:
        """Commit the explicit Keep decision before advancing ephemeral engine state."""
        self._require_open()
        state = self.current()
        if state.prompt is None or state.status != "review":
            raise ContractError("no desktop take is available to accept")
        index = self._current_take_index(state.prompt.prompt_id)
        self._update_take(index, accepted=True)
        self.engine.accept_take()
        return self.current()

    def reject_take(self, reason: str) -> CaptureState:
        """Retain rejected audio in TakeEngine's forensic directory and permit a retake."""
        self._require_open()
        state = self.current()
        if state.prompt is None or state.status != "review":
            raise ContractError("no desktop take is available to reject")
        index = self._current_take_index(state.prompt.prompt_id)
        if self.evidence.takes[index].accepted:
            # Reopening an earlier kept take is an explicit operator action;
            # commit that decision before the canonical recoverable rejection.
            self._update_take(index, accepted=False)
        self.engine.reject_take(reason)
        self._update_take(index, rejected=True)
        return self.current()

    def finalize(self) -> CaptureResult:
        """Publish only a complete, explicitly accepted selected batch."""
        self._require_open()
        state = self.current()
        accepted = {t.prompt_id for t in self.evidence.takes if t.accepted and not t.rejected}
        if state.status != "complete" or accepted != {
            p.prompt_id for p in self.evidence.selected_suite.prompts
        }:
            raise ContractError("every selected prompt must be recorded and accepted to finalize")
        result = self.engine.finalize()
        self._persist(self.evidence.model_copy(update={"manifest_sha256": result.manifest_sha256}))
        self._release()
        return result

    def preview_path(self, index: int | None = None) -> Path:
        """Return effective preview after validating original capture and edit evidence."""
        state = self.current()
        source = self.engine.preview_path(index)
        relative = source.relative_to(state.session_directory).as_posix()
        prompt_id = next(view.prompt.prompt_id for view in state.takes if view.relative == relative)
        edit = source_edit_state(
            self.library.layout,
            source,
            subject_id=self.evidence.subject_id,
            session_id=self.evidence.session_id,
            prompt_id=prompt_id,
            source_sha256=inspect_canonical_wav(source).sha256,
            updated_at=self.library.clock(),
        )
        return effective_recording_path(self.library.layout, source, edit)

    def _release(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self.library._active is self:
            self.library._active = None

    def abort(self) -> None:
        """Close the session and release its writer lock, preserving all recorded audio."""
        try:
            self.engine.abort()
        finally:
            self._release()

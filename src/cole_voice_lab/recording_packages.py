"""Strict v1 lab packages from independently saved native takes.

@canonical recording_packages -- copies original PCM into new inodes, reuses
capture finalization/intake, and commits local alias evidence outside the strict
package. Rich take dates/edit decisions are local provenance, never v1 payload.
"""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
import stat
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter, model_validator

from .canonical import canonical_json_bytes, canonical_sha256, open_regular_no_follow, sha256_stream
from .capture import expected_recording_paths, finalize_capture_session
from .diagnostic_roots import require_catalog_root
from .errors import ContractError, SecurityError
from .intake import ValidatedPackage, validate_capture_package
from .models import (
    PHYSICAL_TEST_SUITE_PREFIX,
    ContractModel,
    PromptSuite,
    Sha256,
    StableId,
    utc_now,
)
from .paths import AssetLayout, confined_path
from .recording_analysis import ALGORITHM_VERSION, analysis_build_identity
from .recording_contracts import (
    MAX_CONTROL_FRAME_BYTES,
    ErrorCode,
    PreparePackageCommand,
    RecordingBuildIdentity,
    RecordingError,
    RecordingFrameRange,
    RecordingGroup,
    RecordingPackageEntry,
    RecordingPackageMap,
    RecordingTake,
    RelativePath,
    Revision,
    UtcTimestamp,
)
from .recording_index import RecordingIndex
from .recording_takes import (
    PendingRecordingOperation,
    RecordingFileIdentity,
    RecordingTakeOperation,
    RecordingTakeStore,
    StaleRecordingAnalysisError,
    read_recording_evidence,
    recording_analysis_job_sha256,
    recording_evidence_directory,
    write_recording_evidence,
)

_PACKAGE_ROOT = "provenance/recording-packages"
_COPY_BYTES = 256 * 1024
_PACKAGE_MAX_BYTES = MAX_CONTROL_FRAME_BYTES


class RecordingPackageError(ContractError):
    """An explicit prerequisite or preparation failure, never an instruction to change Save."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: ErrorCode = "RECORDING.GATE_NOT_READY",
    ) -> None:
        self.recording_error = RecordingError(
            code=code, message=message[:1024], retryable=retryable
        )
        super().__init__(message)


class RecordingPackageOrigin(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-origin/v1"] = (
        "cole-voice-lab.recording-package-origin/v1"
    )
    root_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: UtcTimestamp


class RecordingPackageSource(ContractModel):
    take_id: StableId
    original_path: RelativePath
    original_sha256: Sha256
    original_size_bytes: int = Field(gt=0)
    seal_sha256: Sha256
    prompt_id: StableId
    recorded_at: UtcTimestamp
    source_identity: RecordingFileIdentity
    decision_revision: Revision
    edit_revision: Revision
    frame_range: RecordingFrameRange
    edit_identity_sha256: Sha256
    analysis_job_sha256: Sha256
    analysis_sha256: Sha256


class RecordingPackagePlan(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-plan/v1"] = (
        "cole-voice-lab.recording-package-plan/v1"
    )
    command: PreparePackageCommand
    request_sha256: Sha256
    session_id: StableId
    group: RecordingGroup
    selected_suite: PromptSuite
    sources: tuple[RecordingPackageSource, ...] = Field(min_length=1, max_length=10_000)
    analyzer_build: RecordingBuildIdentity
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def _selection_bound(self) -> RecordingPackagePlan:
        if self.request_sha256 != canonical_sha256(self.command):
            raise ValueError("preparation request digest changed")
        if self.group.group_id != self.command.group_id:
            raise ValueError("preparation group changed")
        if {source.take_id for source in self.sources} != set(self.command.take_ids):
            raise ValueError("preparation source IDs differ from the explicit selection")
        if tuple(source.prompt_id for source in self.sources) != tuple(
            prompt.prompt_id for prompt in self.selected_suite.prompts
        ):
            raise ValueError("preparation sources must preserve exact declared suite order")
        if self.selected_suite.purpose != self.group.catalog_purpose:
            raise ValueError("preparation cannot change catalog purpose")
        return self


class RecordingPackageCommit(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-commit/v1"] = (
        "cole-voice-lab.recording-package-commit/v1"
    )
    plan: RecordingPackagePlan
    package: RecordingPackageMap

    @model_validator(mode="after")
    def _map_binding(self) -> RecordingPackageCommit:
        if (
            self.package.session_id != self.plan.session_id
            or self.package.group != self.plan.group
            or self.package.selected_suite != self.plan.selected_suite
            or tuple(entry.take_id for entry in self.package.entries)
            != tuple(source.take_id for source in self.plan.sources)
        ):
            raise ValueError("package commit must bind its exact preparation plan")
        paths = expected_recording_paths(self.plan.selected_suite)
        for entry, source, prompt, path in zip(
            self.package.entries,
            self.plan.sources,
            self.plan.selected_suite.prompts,
            paths,
            strict=True,
        ):
            if (
                entry.prompt != prompt
                or entry.package_relative_path != path
                or entry.original_sha256 != source.original_sha256
                or entry.recorded_at != source.recorded_at
                or entry.edit_identity_sha256 != source.edit_identity_sha256
                or entry.decision_revision != source.decision_revision
                or entry.edit_revision != source.edit_revision
                or entry.frame_range != source.frame_range
                or entry.analysis_sha256 != source.analysis_sha256
            ):
                raise ValueError("package entry must bind exact source, edit and verification")
        return self


class RecordingPackageFinalization(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-finalization/v1"] = (
        "cole-voice-lab.recording-package-finalization/v1"
    )
    operation_id: StableId
    finalized_at: UtcTimestamp


def _publish_without_replace(source: Path, destination: Path) -> None:
    """Atomically publish a private completed inode/directory without replacing any entry.

    macOS sys/stdio.h defines renamex_np and RENAME_EXCL (0x4). Linux exposes
    the equivalent renameat2 RENAME_NOREPLACE operation. Never emulate this with
    an existence check followed by a replacing rename.
    """
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(destination), 0x4)
    elif sys.platform.startswith("linux"):
        rename = library.renameat2
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        raise RecordingPackageError("atomic native package publication is unavailable on this OS")
    if result != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def _original_identity(relative: str, metadata: os.stat_result) -> RecordingFileIdentity:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise SecurityError("package source must remain one owned private original")
    return RecordingFileIdentity(
        relative_path=relative,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size_bytes=metadata.st_size,
        mode=metadata.st_mode,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _edit_identity(layout: AssetLayout, take: RecordingTake) -> str:
    from .recording_edits import native_edit_identity

    return native_edit_identity(layout, take.reservation.take_id, take=take)


def _selected_suite(catalog: PromptSuite, sources: Sequence[RecordingPackageSource]) -> PromptSuite:
    """The package suite: the group's resolved catalog restricted to the selected prompts.

    Prompts keep catalog order; ``catalog`` itself is returned when every prompt is selected.
    Raises: ``RecordingPackageError`` when a source names a prompt the catalog lacks or two
    sources name one prompt.
    """
    selected = {source.prompt_id for source in sources}
    prompts = tuple(prompt for prompt in catalog.prompts if prompt.prompt_id in selected)
    if len(prompts) != len(sources):
        raise RecordingPackageError("choose one take per exact prompt for each package")
    if prompts == catalog.prompts:
        return catalog
    subset_sha = canonical_sha256([prompt.model_dump(mode="json") for prompt in prompts])
    # A manifest names its suite only by ID, so a physical-test subset keeps the reserved prefix.
    prefix = (
        f"{PHYSICAL_TEST_SUITE_PREFIX}subset-"
        if catalog.purpose == "physical-test"
        else "native-subset-"
    )
    return PromptSuite(
        schema_version=catalog.schema_version,
        suite_id=f"{prefix}{subset_sha[:24]}",
        purpose=catalog.purpose,
        prompts=prompts,
    )


def partition_recording_package_selection(
    takes: Sequence[RecordingTake],
) -> tuple[tuple[str, ...], ...]:
    """Split incompatible capture contexts before constructing explicit per-group commands."""
    groups: dict[str, list[str]] = {}
    for take in takes:
        group = take.reservation.group
        key = canonical_sha256(
            {
                "group_id": group.group_id,
                "subject_id": group.subject_id,
                "catalog_sha256": group.catalog_sha256,
                "device": group.capture_device.model_dump(mode="json"),
                "host": group.capture_host,
                "context_revision": group.context_revision,
            }
        )
        groups.setdefault(key, []).append(take.reservation.take_id)
    return tuple(tuple(ids) for ids in groups.values())


def validate_recording_package_map(
    package_map: RecordingPackageMap, validated: ValidatedPackage
) -> None:
    """Bind a local rich map to an independently validated unchanged v1 package.

    The package suite is the map's ``selected_suite``. Only a map of a recording-group/v1
    may omit it (the complete embedded catalog was packaged); a map of a recording-group/v2
    always carries it, and its group's catalog is never consulted here.
    Raises: ``SecurityError`` when the map and the package evidence disagree;
    ``RecordingPackageError`` (``RECORDING.PACKAGE_INCOMPLETE``) for a map that names no
    package suite (the map contract refuses that for v2 groups before this point).
    """
    manifest = validated.manifest
    suite = package_map.selected_suite
    if suite is None:
        # recording-package-map/v1 of an embedded catalog; resolved groups never get here.
        suite = package_map.group.catalog
    if suite is None:
        raise RecordingPackageError(
            "a package of a catalog-reference group requires its selected suite",
            code="RECORDING.PACKAGE_INCOMPLETE",
        )
    if (
        validated.manifest_sha256 != package_map.manifest_sha256
        or manifest.session_id != package_map.session_id
        or manifest.subject_id != package_map.group.subject_id
        or manifest.capture_device != package_map.group.capture_device
        or manifest.capture_host != package_map.group.capture_host
        or manifest.prompt_suite_id != suite.suite_id
        or tuple(file.path for file in manifest.files) != expected_recording_paths(suite)
        or len(manifest.files) != len(package_map.entries)
    ):
        raise SecurityError("native package map differs from strict v1 package evidence")
    for entry, source in zip(package_map.entries, manifest.files, strict=True):
        if entry.package_relative_path != source.path or entry.original_sha256 != source.sha256:
            raise SecurityError("native package map names different original bytes")
        if source.kind == "utterance" and (
            source.prompt_id != entry.prompt.prompt_id or source.transcript != entry.prompt.text
        ):
            raise SecurityError("native package map changed exact transcript binding")


class RecordingPackageStore:
    """Metadata planning and explicit expensive preparation; no accepted-raw mutation."""

    def __init__(
        self,
        layout: AssetLayout,
        *,
        take_store: RecordingTakeStore,
        index: RecordingIndex | None = None,
        analyzer_build: RecordingBuildIdentity | None = None,
        clock: Callable[[], datetime] = utc_now,
        fault_hook: Callable[[str], None] | None = None,
        defer_projection: bool = False,
    ) -> None:
        self.layout = AssetLayout.validate(layout.root)
        self.takes = take_store
        self.index, self.clock, self.fault_hook = index, clock, fault_hook
        self.defer_projection = defer_projection
        if defer_projection and index is not None:
            raise ContractError("a deferred package producer cannot own an index writer")
        self.analyzer_build = analyzer_build or analysis_build_identity()
        self.directory = self._directory(_PACKAGE_ROOT)
        for relative in ("plans", "operations", "sessions", "work"):
            self._directory(f"{_PACKAGE_ROOT}/{relative}")
        with self._lock():
            path = self.takes._path("provenance/recording-package-origin.json")
            if path.exists():
                self.origin = self.takes._read(path, RecordingPackageOrigin)
            else:
                if any(self._path("plans").iterdir()) or any(self._path("operations").iterdir()):
                    raise SecurityError(
                        "local package origin disappeared despite committed provenance"
                    )
                self.origin = RecordingPackageOrigin(root_id=uuid4().hex, created_at=self.clock())
                self._write_immutable(path, self.origin)

    def _directory(self, relative: str) -> Path:
        return recording_evidence_directory(self.layout, relative)

    def _write_immutable(self, path: Path, model: ContractModel) -> None:
        write_recording_evidence(path, model, immutable=True)

    def _path(self, relative: str) -> Path:
        return self.takes._path(f"{_PACKAGE_ROOT}/{relative}")

    def _fault(self, boundary: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(boundary)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor = os.open(
            self._path(".metadata.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            _original_identity(f"{_PACKAGE_ROOT}/.metadata.lock", os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _plan_path(self, operation_id: str) -> Path:
        identifier: str = TypeAdapter(StableId).validate_python(operation_id)
        return self._path(f"plans/{identifier}.json")

    def _operation_path(self, operation_id: str) -> Path:
        identifier: str = TypeAdapter(StableId).validate_python(operation_id)
        return self._path(f"operations/{identifier}.json")

    def _session_path(self, session_id: str) -> Path:
        identifier: str = TypeAdapter(StableId).validate_python(session_id)
        return self._path(f"sessions/{identifier}.json")

    def _source(self, take: RecordingTake) -> RecordingPackageSource:
        seal = take.seal
        if (
            take.decision != "kept"
            or take.analysis != "verified"
            or seal is None
            or take.frame_range is None
        ):
            raise RecordingPackageError(
                "Prepare for lab requires saved, independently verified takes"
            )
        group = take.reservation.group
        job_sha = recording_analysis_job_sha256(
            seal=seal,
            analyzer_build=self.analyzer_build,
            algorithm_version=ALGORITHM_VERSION,
            edit_revision=take.edit_revision,
            context_revision=group.context_revision,
            allow_quiet=take.reservation.prompt.allows_quiet,
        )
        publication = self.takes.analysis_result(job_sha)
        if (
            publication is None
            or publication.result.state != "verified"
            or publication.source_identity is None
        ):
            raise RecordingPackageError(
                "selected take is awaiting current independent verification"
            )
        path = confined_path(self.layout.root, seal.original_path, must_exist=True)
        with open_regular_no_follow(path) as source:
            identity = _original_identity(seal.original_path, os.fstat(source.fileno()))
        if identity != publication.source_identity:
            raise SecurityError("selected original changed since independent verification")
        return RecordingPackageSource(
            take_id=seal.take_id,
            original_path=seal.original_path,
            original_sha256=seal.original_sha256,
            original_size_bytes=seal.original_size_bytes,
            seal_sha256=canonical_sha256(seal),
            prompt_id=take.reservation.prompt.prompt_id,
            recorded_at=seal.first_sample_at,
            source_identity=identity,
            decision_revision=take.decision_revision,
            edit_revision=take.edit_revision,
            frame_range=take.frame_range,
            edit_identity_sha256=_edit_identity(self.layout, take),
            analysis_job_sha256=job_sha,
            analysis_sha256=canonical_sha256(publication),
        )

    def plan(self, command: PreparePackageCommand) -> RecordingPackagePlan:
        """Durable metadata-only prerequisite check. Save and recording never depend on this."""
        with self._lock():
            path = self._plan_path(command.operation_id)
            if path.exists():
                existing = self.takes._read(path, RecordingPackagePlan)
                if existing.command != command:
                    raise RecordingPackageError(
                        "package operation ID already names another selection"
                    )
                return existing
            group = self.takes.get_group(command.group_id)
            catalog = self.takes.group_catalog(group).suite
            try:
                require_catalog_root(self.layout, catalog, group.subject_id)
            except ContractError as error:
                raise RecordingPackageError(
                    str(error), code="RECORDING.PACKAGE_INCOMPLETE"
                ) from error
            if group.catalog_purpose == "held-out-evaluation":
                raise RecordingPackageError(
                    "held-out material cannot be packaged as training recordings"
                )
            if "umik-1" not in group.capture_device.display_name.casefold():
                raise RecordingPackageError(
                    "strict lab packages require the configured UMIK-1 microphone"
                )
            # A take carries its catalog. Retain only compact source records so a
            # large selection cannot multiply that catalog before the size gate.
            metadata_bytes = len(canonical_json_bytes(group)) + len(canonical_json_bytes(command))
            selected: dict[str, RecordingPackageSource] = {}
            tones: set[str] = set()
            for take_id in command.take_ids:
                if metadata_bytes > _PACKAGE_MAX_BYTES:
                    raise RecordingPackageError(
                        "split this package selection to fit its bounded metadata contract"
                    )
                take = self.takes.get_take(take_id)
                if take.reservation.group != group:
                    raise RecordingPackageError(
                        "split selections by exact group, subject, suite, device and host"
                    )
                prompt = take.reservation.prompt
                if prompt.category == "procedure":
                    raise RecordingPackageError(
                        "Test steps without spoken text or room tone cannot be packaged.",
                        code="RECORDING.PACKAGE_INCOMPLETE",
                    )
                if prompt.prompt_id in selected:
                    raise RecordingPackageError("choose one take per exact prompt for each package")
                source = self._source(take)
                metadata_bytes += len(canonical_json_bytes(source))
                selected[prompt.prompt_id] = source
                if prompt.category == "room-tone":
                    tones.add(take_id)
            suite = _selected_suite(catalog, tuple(selected.values()))
            if not tones or tones != set(command.room_tone_take_ids):
                raise RecordingPackageError(
                    "select explicit compatible room-tone takes for this package"
                )
            if len(tones) == len(selected):
                raise RecordingPackageError("select at least one utterance as well as room tone")
            sources = tuple(selected[prompt.prompt_id] for prompt in suite.prompts)
            bound_group = group.model_copy(
                update={
                    "room_tone_take_ids": tuple(
                        source.take_id for source in sources if source.take_id in tones
                    )
                }
            )
            plan = RecordingPackagePlan(
                command=command,
                request_sha256=canonical_sha256(command),
                session_id=f"native-{self.origin.root_id}-{canonical_sha256(command)[:24]}",
                group=bound_group,
                selected_suite=suite,
                sources=sources,
                analyzer_build=self.analyzer_build,
                created_at=self.clock(),
            )
            if len(canonical_json_bytes(plan)) > _PACKAGE_MAX_BYTES:
                raise RecordingPackageError(
                    "split this package selection to fit its bounded metadata contract"
                )
            self._fault("before-plan")
            self._write_immutable(path, plan)
            self._fault("after-plan")
            return plan

    def _copy_original(self, source: RecordingPackageSource, destination: Path, work: Path) -> None:
        original = confined_path(self.layout.root, source.original_path, must_exist=True)
        if destination.exists():
            with open_regular_no_follow(destination) as existing:
                _original_identity(source.original_path, os.fstat(existing.fileno()))
                existing_digest, _ = sha256_stream(existing)
            if existing_digest != source.original_sha256:
                raise SecurityError("interrupted package contains a conflicting original")
            return
        partial = work / f"copy-{source.take_id}-{uuid4().hex}.partial"
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        digest = hashlib.sha256()
        copied = 0
        try:
            with open_regular_no_follow(original) as stream:
                before = _original_identity(source.original_path, os.fstat(stream.fileno()))
                if before != source.source_identity:
                    raise SecurityError("original changed before package copy")
                while payload := stream.read(_COPY_BYTES):
                    digest.update(payload)
                    copied += len(payload)
                    if copied > source.original_size_bytes:
                        raise SecurityError("original grew beyond its sealed size")
                    offset = 0
                    while offset < len(payload):
                        written = os.write(descriptor, payload[offset:])
                        if written <= 0:
                            raise OSError("package copy made no write progress")
                        offset += written
                after = _original_identity(source.original_path, os.fstat(stream.fileno()))
                named = _original_identity(source.original_path, original.lstat())
                if (
                    before != after
                    or before != named
                    or copied != source.original_size_bytes
                    or digest.hexdigest() != source.original_sha256
                ):
                    raise SecurityError("original changed during package copy")
            self._fault("before-copy-sync")
            os.fsync(descriptor)
            self._fault("after-copy-sync")
        finally:
            os.close(descriptor)
        self._fault("before-copy-publish")
        _publish_without_replace(partial, destination)
        self.takes._sync_directory(destination.parent)
        self.takes._sync_directory(work)
        self._fault("after-copy-publish")

    def _project(
        self,
        package_map: RecordingPackageMap,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        for entry in package_map.entries:
            if cancelled():
                raise RecordingPackageError("package publication was stopped", retryable=True)
            operation_id = (
                "package-"
                + canonical_sha256(
                    {
                        "session_id": package_map.session_id,
                        "take_id": entry.take_id,
                    }
                )[:56]
            )
            try:
                take = self.takes.publish_package(
                    operation_id=operation_id,
                    session_id=package_map.session_id,
                    take_id=entry.take_id,
                )
            except StaleRecordingAnalysisError:
                # A later Remove/Edit remains authoritative. The historical package
                # alias still resolves to that current decision at every use boundary.
                take = self.takes.get_take_for_projection(entry.take_id)
            if self.index is not None:
                aliases: tuple[tuple[str, str, str, Literal["package-tmp", "package-raw"]], ...] = (
                    (
                        entry.take_id,
                        f"{package_map.package_path}/{entry.package_relative_path}",
                        entry.original_sha256,
                        "package-tmp",
                    ),
                    (
                        entry.take_id,
                        f"raw/{package_map.manifest_sha256}/{entry.package_relative_path}",
                        entry.original_sha256,
                        "package-raw",
                    ),
                )
                # Full takes contain their catalog. Retain only the current take,
                # and keep each same-owner index publication short and replayable.
                self.index.register_package_aliases((take,), aliases)

    def prepare(self, command: PreparePackageCommand) -> RecordingPackageMap:
        """Expensive worker boundary: revalidate originals and finalize a new strict package."""
        plan = self.plan(command)
        work = self._directory(f"{_PACKAGE_ROOT}/work/{command.operation_id}")
        descriptor = os.open(work / ".prepare.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            _original_identity(
                f"{_PACKAGE_ROOT}/work/{command.operation_id}/.prepare.lock", os.fstat(descriptor)
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RecordingPackageError(
                    "this package is already preparing", retryable=True
                ) from error
            return self._prepare_owned(plan)
        finally:
            os.close(descriptor)

    def _prepare_owned(self, plan: RecordingPackagePlan) -> RecordingPackageMap:
        command = plan.command
        final_relative = f"tmp/capture-{plan.session_id}"
        final = confined_path(self.layout.root, final_relative)
        stage_relative = f"tmp/.native-package-stage-{plan.session_id}"
        stage = self._directory(stage_relative)
        work = self._directory(f"{_PACKAGE_ROOT}/work/{command.operation_id}")
        with self._lock():
            operation_path = self._operation_path(command.operation_id)
            if operation_path.exists():
                committed = self.takes._read(operation_path, RecordingPackageCommit)
                if committed.plan != plan:
                    raise SecurityError("package operation plan changed")
            else:
                committed = None
        if committed is not None:
            return self._publish(committed, stage, final)
        if plan.analyzer_build != self.analyzer_build:
            raise RecordingPackageError(
                "package plan belongs to a different verified analyzer build"
            )
        for source in plan.sources:
            if self._source(self.takes.get_take(source.take_id)) != source:
                raise RecordingPackageError(
                    "durable package plan no longer binds its selected take"
                )
        paths = expected_recording_paths(plan.selected_suite)
        self._directory(f"{stage_relative}/audio")
        for source, relative in zip(plan.sources, paths, strict=True):
            self._copy_original(source, confined_path(stage, relative), work)
        finalization_path = work / "finalization.json"
        if finalization_path.exists():
            finalization = self.takes._read(finalization_path, RecordingPackageFinalization)
        else:
            finalization = RecordingPackageFinalization(
                operation_id=command.operation_id, finalized_at=self.clock()
            )
            self._write_immutable(finalization_path, finalization)
        self._fault("before-finalize")
        result = finalize_capture_session(
            stage,
            session_id=plan.session_id,
            subject_id=plan.group.subject_id,
            prompt_suite=plan.selected_suite,
            device=plan.group.capture_device,
            capture_host=plan.group.capture_host,
            captured_at=min(source.recorded_at for source in plan.sources),
            finalized_at=finalization.finalized_at,
        )
        self._fault("after-ready")
        validated = validate_capture_package(stage)
        if validated.qc_report.status != "pass":
            raise RecordingPackageError(
                "strict package verification needs review before lab preparation"
            )
        package_map = RecordingPackageMap(
            session_id=plan.session_id,
            manifest_sha256=result.manifest_sha256,
            package_path=final_relative,
            group=plan.group,
            selected_suite=plan.selected_suite,
            entries=tuple(
                RecordingPackageEntry(
                    take_id=source.take_id,
                    group_id=plan.group.group_id,
                    subject_id=plan.group.subject_id,
                    catalog_sha256=plan.group.catalog_sha256,
                    prompt=prompt,
                    package_relative_path=relative,
                    original_sha256=source.original_sha256,
                    recorded_at=source.recorded_at,
                    edit_identity_sha256=source.edit_identity_sha256,
                    decision_revision=source.decision_revision,
                    edit_revision=source.edit_revision,
                    frame_range=source.frame_range,
                    analysis_sha256=source.analysis_sha256,
                )
                for source, prompt, relative in zip(
                    plan.sources, plan.selected_suite.prompts, paths, strict=True
                )
            ),
            registered_at=self.clock(),
        )
        validate_recording_package_map(package_map, validated)
        committed = RecordingPackageCommit(plan=plan, package=package_map)
        # An owned producer writes only immutable package evidence. Current take
        # publication belongs to the service after its verified worker reply;
        # direct preparation retains its serialized decision check.
        with (
            self._lock(),
            nullcontext() if self.defer_projection else self.takes._metadata_lock(),
        ):
            if not self.defer_projection:
                self.takes._repair_pending()
            for source in plan.sources:
                identity = _original_identity(
                    source.original_path,
                    confined_path(self.layout.root, source.original_path, must_exist=True).lstat(),
                )
                if identity != source.source_identity:
                    raise SecurityError("original changed before package registration")
                take = (
                    self.takes.get_take_for_projection(source.take_id)
                    if self.defer_projection
                    else self.takes._get_take(source.take_id)
                )
                # Avoid nested TakeStore locks while validating its canonical analysis head.
                if (
                    take.decision != "kept"
                    or take.analysis != "verified"
                    or take.decision_revision != source.decision_revision
                    or take.edit_revision != source.edit_revision
                    or take.frame_range != source.frame_range
                    or take.seal is None
                    or canonical_sha256(take.seal) != source.seal_sha256
                ):
                    raise RecordingPackageError(
                        "selected take decision, edit or verification changed during preparation"
                    )
            self._fault("before-operation")
            self._write_immutable(operation_path, committed)
            self._fault("after-operation")
        return self._publish(committed, stage, final)

    def _publish(
        self, committed: RecordingPackageCommit, stage: Path, final: Path
    ) -> RecordingPackageMap:
        package_map = committed.package
        candidate = final if final.exists() else stage
        validated = validate_capture_package(candidate)
        validate_recording_package_map(package_map, validated)
        with self._lock():
            self._fault("before-map")
            self._write_immutable(self._session_path(package_map.session_id), committed)
            self._fault("after-map")
            if not final.exists():
                self._fault("before-directory-publish")
                _publish_without_replace(stage, final)
                self.takes._sync_directory(final.parent)
                self._fault("after-directory-publish")
        if not self.defer_projection:
            self._fault("before-alias-projection")
            self._project(package_map)
            self._fault("after-alias-projection")
        return package_map


def load_recording_package_map(
    layout: AssetLayout,
    session_id: str,
    *,
    package: ValidatedPackage | None = None,
) -> RecordingPackageMap | None:
    """Read-only local provenance lookup; foreign v1 payloads never invent rich metadata.

    The persistent root UUID in locally prepared session IDs distinguishes a
    missing required local map from a transferred v1 package whose rich sidecars
    were never part of its payload.
    """
    layout = AssetLayout.validate(layout.root)
    identifier: str = TypeAdapter(StableId).validate_python(session_id)
    directory = confined_path(layout.root, _PACKAGE_ROOT)
    origin_path = confined_path(layout.root, "provenance/recording-package-origin.json")
    if not origin_path.exists():
        if directory.exists():
            raise SecurityError("native package origin evidence disappeared")
        return None
    origin = read_recording_evidence(origin_path, RecordingPackageOrigin)
    local = identifier.startswith(f"native-{origin.root_id}-")
    path = confined_path(layout.root, f"{_PACKAGE_ROOT}/sessions/{identifier}.json")
    if not path.exists():
        if local:
            raise SecurityError("locally prepared native package is missing its required take map")
        return None
    committed = read_recording_evidence(path, RecordingPackageCommit)
    if committed.package.session_id != identifier:
        raise SecurityError("native package map filename differs from its identity")
    operation = confined_path(
        directory, f"operations/{committed.plan.command.operation_id}.json", must_exist=True
    )
    if read_recording_evidence(operation, RecordingPackageCommit) != committed:
        raise SecurityError("native package map differs from its immutable registration operation")
    if package is not None:
        validate_recording_package_map(committed.package, package)
    return committed.package


def _read_native_take_snapshot(layout: AssetLayout, take_id: str) -> RecordingTake:
    """Observe committed pending decisions without mutating journals or clearing projections."""
    root = confined_path(layout.root, "provenance/recording-takes", must_exist=True)
    descriptor = os.open(
        confined_path(root, ".metadata.lock", must_exist=True), os.O_RDONLY | os.O_NOFOLLOW
    )
    try:
        _original_identity("provenance/recording-takes/.metadata.lock", os.fstat(descriptor))
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        head = confined_path(root, f"{take_id}/head.json")
        take = read_recording_evidence(head, RecordingTake) if head.exists() else None
        pending_path = confined_path(root, ".pending.json")
        if pending_path.exists():
            pending = read_recording_evidence(pending_path, PendingRecordingOperation)
            operation_path = confined_path(root, f".operations/{pending.operation_id}.json")
            if operation_path.exists():
                operation = read_recording_evidence(operation_path, RecordingTakeOperation)
                if (
                    operation.operation_id != pending.operation_id
                    or operation.request_sha256 != pending.request_sha256
                ):
                    raise SecurityError("native take journal binding changed")
                updated = operation.take
                if (
                    updated is not None
                    and updated.reservation.take_id == take_id
                    and (take is None or updated.state_revision > take.state_revision)
                ):
                    take = updated
        if take is None or take.reservation.take_id != take_id:
            raise SecurityError("native package alias lost its authoritative take identity")
        return take
    finally:
        os.close(descriptor)


def resolve_native_package_alias(
    layout: AssetLayout,
    *,
    subject_id: str,
    session_id: str,
    prompt_id: str,
    source_sha256: str,
    manifest_sha256: str | None = None,
    take_store: RecordingTakeStore | None = None,
) -> RecordingTake | None:
    """Resolve aliases; background projections may supply their nonrepairing take reader.

    Reference and intake callers retain the pending-journal-aware snapshot. A
    background reader retries an overlapping commit without sharing its flock.
    """
    if take_store is not None and (
        not take_store.read_only or take_store.layout.root != layout.root
    ):
        raise ContractError("package projection requires a read-only store for this asset root")
    package_map = load_recording_package_map(layout, session_id)
    if package_map is None:
        return None
    if manifest_sha256 is not None and package_map.manifest_sha256 != manifest_sha256:
        raise SecurityError("native package alias manifest digest changed")
    matches = tuple(
        entry
        for entry in package_map.entries
        if entry.prompt.prompt_id == prompt_id
        and entry.subject_id == subject_id
        and entry.original_sha256 == source_sha256
    )
    if len(matches) != 1:
        raise SecurityError("native package alias has no unique matching original take")
    entry = matches[0]
    take = (
        _read_native_take_snapshot(layout, entry.take_id)
        if take_store is None
        else take_store.get_take(entry.take_id)
    )
    if (
        take.reservation.group.subject_id != subject_id
        or take.reservation.group.group_id != entry.group_id
        or take.reservation.group.catalog_sha256 != entry.catalog_sha256
        or take.reservation.prompt != entry.prompt
        or take.seal is None
        or take.seal.original_sha256 != source_sha256
    ):
        raise SecurityError("native package alias differs from its original take binding")
    return take


RECORDING_PACKAGE_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "recording-package-origin": RecordingPackageOrigin,
    "recording-package-source": RecordingPackageSource,
    "recording-package-plan": RecordingPackagePlan,
    "recording-package-commit": RecordingPackageCommit,
    "recording-package-finalization": RecordingPackageFinalization,
}

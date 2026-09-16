"""Durable independent recording takes and replayable operator decisions.

@canonical recording_takes -- immutable operation evidence is authoritative; take
heads and the library projection are repaired after interrupted commits. No read,
reservation or Save inspects historical WAV samples or grants accepted-raw status.
"""

from __future__ import annotations

import fcntl
import heapq
import itertools
import os
import stat
import struct
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .datasets import load_pilot_gate
from .diagnostic_roots import require_catalog_root
from .errors import ContractError, SecurityError
from .models import (
    CaptureDevice,
    CaptureManifest,
    ContractModel,
    Prompt,
    PromptSuite,
    Sha256,
    StableId,
    utc_now,
)
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    MAX_CONTROL_FRAME_BYTES,
    MAX_HISTORY_PAGE_SIZE,
    CaptureReservation,
    CaptureSealReceipt,
    ContextEvent,
    DecisionCommand,
    DecisionEvent,
    EditEvent,
    EditTakeCommand,
    NavigateCommand,
    NavigationEvent,
    OpenContextCommand,
    PositiveRevision,
    PreparedCaptureGate,
    RecordingAnalysisResult,
    RecordingBuildIdentity,
    RecordingCursor,
    RecordingDecisionEvidence,
    RecordingFrameRange,
    RecordingGroup,
    RecordingNavigationResult,
    RecordingPackageEntry,
    RecordingReviewSource,
    RecordingSaveOperation,
    RecordingSelection,
    RecordingTake,
    RetakeCommand,
    ReviewEvent,
    ReviewTakeCommand,
    Revision,
    SaveEvent,
    SaveTakeCommand,
    UtcTimestamp,
)
from .recording_contracts import (
    RecordingFileIdentity as RecordingFileIdentity,
)
from .recording_edits import (
    RecordingEditRecipe,
    current_native_edit,
    publish_native_edit_recipe,
)

TAKE_PROVENANCE_ROOT = "provenance/recording-takes"
GROUP_PROVENANCE_ROOT = "provenance/recording-groups"
MAX_TAKE_EVIDENCE_BYTES = MAX_CONTROL_FRAME_BYTES
_STABLE_ID: TypeAdapter[StableId] = TypeAdapter(StableId)


def read_recording_evidence[ModelT: ContractModel](path: Path, model: type[ModelT]) -> ModelT:
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecurityError("recording evidence must be one private regular file")
        if metadata.st_size > MAX_TAKE_EVIDENCE_BYTES:
            raise SecurityError("recording evidence exceeds its byte limit")
        payload = stream.read(MAX_TAKE_EVIDENCE_BYTES + 1)
    try:
        result = model.model_validate_json(payload)
    except ValidationError as error:
        raise ContractError("invalid versioned recording evidence") from error
    if payload != canonical_json_bytes(result) + b"\n":
        raise SecurityError("recording evidence is not canonical")
    return result


def sync_recording_evidence_directory(directory: Path) -> None:
    """Sync an already confined real evidence directory without following its final link."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recording_evidence_directory(
    layout: AssetLayout, relative: str, *, create: bool = True
) -> Path:
    """Create private evidence parents, or require existing parents for read-only use.

    Existing permissions and file types fail closed; this never normalizes them.
    Every new directory entry is synced before returning its confined path.
    """
    directory = confined_path(layout.root, relative)
    current = layout.root
    for part in Path(relative).parts:
        parent = current
        current /= part
        if not current.exists():
            if not create:
                raise RecordingMetadataBusyError(
                    "recording evidence directories await the service owner"
                )
            current.mkdir(mode=0o700)
            sync_recording_evidence_directory(parent)
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecurityError("recording evidence directories must be private real directories")
    return directory


def write_recording_evidence(path: Path, model: ContractModel, *, immutable: bool = False) -> None:
    """Write bounded canonical evidence; immutable replay verifies bytes and syncs them.

    Callers own confinement and the namespace's publication lock. A replay cannot
    overwrite evidence bound to different content or relax its file validation.
    """
    if immutable and path.exists():
        if read_recording_evidence(path, type(model)) != model:
            raise SecurityError("immutable recording evidence already binds different content")
        with open_regular_no_follow(path) as stream:
            os.fsync(stream.fileno())
        sync_recording_evidence_directory(path.parent)
    else:
        if len(canonical_json_bytes(model)) + 1 > MAX_TAKE_EVIDENCE_BYTES:
            raise ContractError("recording evidence exceeds its byte limit")
        write_canonical_json(path, model)


class RecordingMetadataBusyError(ContractError):
    """A short canonical commit overlaps background metadata discovery; retry later."""


class StaleRecordingAnalysisError(ContractError):
    """A worker result belongs to a superseded source/edit/context or attempt."""


class AnalysisSourceChangedError(SecurityError):
    """The selected original identity changed after worker verification."""


def recording_analysis_job_sha256(
    *,
    seal: CaptureSealReceipt,
    analyzer_build: RecordingBuildIdentity,
    algorithm_version: str,
    edit_revision: int,
    context_revision: int,
    allow_quiet: bool,
) -> str:
    """Canonical source/version/revision binding shared by job and journal validation."""
    return canonical_sha256(
        {
            "schema_version": "cole-voice-lab.recording-analysis-job/v1",
            "seal": seal.model_dump(mode="json"),
            "analyzer_build": analyzer_build.model_dump(mode="json"),
            "algorithm_version": algorithm_version,
            "edit_revision": edit_revision,
            "context_revision": context_revision,
            "allow_quiet": allow_quiet,
        }
    )


class RecordingAnalysisPublication(ContractModel):
    """Digest-bound worker result; the take journal owns its durable publication."""

    job_sha256: Sha256
    attempt: int = Field(ge=0, le=1000)
    result: RecordingAnalysisResult
    source_identity: RecordingFileIdentity | None = None

    @model_validator(mode="after")
    def _verified_identity(self) -> RecordingAnalysisPublication:
        if self.result.state in {"verified", "needs-review"} and self.source_identity is None:
            raise ValueError("completed PCM verification requires a stable source identity")
        return self


class RecordingPackagePublication(ContractModel):
    """Committed external package provenance projected into one current take."""

    session_id: StableId
    manifest_sha256: Sha256
    package_map_sha256: Sha256
    entry: RecordingPackageEntry


class PreparedRecordingGateEvidence(ContractModel):
    schema_version: Literal["cole-voice-lab.prepared-recording-gate/v1"] = (
        "cole-voice-lab.prepared-recording-gate/v1"
    )
    subject_id: StableId
    catalog_sha256: Sha256
    gate: PreparedCaptureGate
    sources: tuple[RecordingFileIdentity, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def _bound_source_identities(self) -> PreparedRecordingGateEvidence:
        identities = [source.model_dump(mode="json") for source in self.sources]
        if self.gate.state != "ready" or canonical_sha256(identities) != self.gate.evidence_sha256:
            raise ValueError("prepared gate must bind its verified source identity snapshot")
        paths = [source.relative_path for source in self.sources]
        if len(paths) != len(set(paths)):
            raise ValueError("prepared gate source paths must be unique")
        return self


class RecordingTakeOperation(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-take-operation/v1"] = (
        "cole-voice-lab.recording-take-operation/v1"
    )
    operation_id: StableId
    request_sha256: Sha256
    kind: Literal[
        "reserve",
        "register-seal",
        "save",
        "navigate",
        "open-context",
        "analysis",
        "edit",
        "decision",
        "review",
        "retake",
        "package",
    ]
    store_revision: PositiveRevision
    take: RecordingTake | None = None
    cursor: RecordingCursor | None = Field(default=None, exclude_if=lambda value: value is None)
    navigation: RecordingNavigationResult | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    save: RecordingSaveOperation | None = None
    analysis: RecordingAnalysisPublication | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    edit_recipe: RecordingEditRecipe | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    review_source: RecordingReviewSource | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    package: RecordingPackagePublication | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    committed_at: UtcTimestamp

    @model_validator(mode="after")
    def _operation_result(self) -> RecordingTakeOperation:
        if self.kind in {"navigate", "open-context", "review", "retake"}:
            if self.take is not None or self.save is not None or self.cursor is None:
                raise ValueError("cursor operations contain a cursor and no take decision")
            if (self.kind in {"navigate", "retake"}) != (self.navigation is not None):
                raise ValueError("only navigation operations contain their resolved navigation")
        else:
            if self.take is None or self.navigation is not None:
                raise ValueError("take operations require their durable take result")
            if self.committed_at < self.take.reservation.requested_at:
                raise ValueError("recording operation cannot precede its capture request")
            if self.take.seal is not None and self.committed_at < self.take.seal.sealed_at:
                raise ValueError("recording operation cannot precede its capture seal")
            if (self.kind == "save") != (self.save is not None):
                raise ValueError("only Save operations contain a keep/navigation decision")
            if self.save is not None:
                if (
                    self.save.operation_id != self.operation_id
                    or self.save.take_id != self.take.reservation.take_id
                    or self.save.committed_at != self.committed_at
                    or self.take.decision != "kept"
                    or self.save.decision_revision != self.take.decision_revision
                    or self.save.edit_revision != self.take.edit_revision
                    or self.save.frame_range != self.take.frame_range
                    or self.save.receipt_sha256 != canonical_sha256(self.take.seal)
                    or self.save.selection.catalog_sha256
                    != self.take.reservation.group.catalog_sha256
                    or self.save.selection.prompt_id != self.take.reservation.prompt.prompt_id
                ):
                    raise ValueError("immutable Save operation and returned take must agree")
                SaveEvent(operation=self.save, take=self.take, cursor=self.cursor)
            elif self.kind in {"reserve", "register-seal"} and self.take.decision != "draft":
                raise ValueError("reservation and sealing cannot keep a take")
        if (self.kind == "review") != (self.review_source is not None):
            raise ValueError("only review operations contain selected-source authorization")
        if self.review_source is not None:
            if self.cursor is None:
                raise ValueError("review operation requires its committed cursor")
            ReviewEvent(source=self.review_source, cursor=self.cursor)
        if self.kind == "decision" and (
            self.take is None
            or self.take.decision_evidence is None
            or self.take.decision_evidence.operation_id != self.operation_id
        ):
            raise ValueError("decision operation requires its exact operator evidence")
        if self.kind == "edit" and self.edit_recipe is None:
            raise ValueError("edit operation requires its durable frame recipe")
        if self.edit_recipe is not None:
            if (
                self.kind not in {"save", "edit"}
                or self.take is None
                or self.take.seal is None
                or self.edit_recipe.operation_id != self.operation_id
                or self.edit_recipe.take_id != self.take.reservation.take_id
                or self.edit_recipe.subject_id != self.take.reservation.group.subject_id
                or self.edit_recipe.prompt_id != self.take.reservation.prompt.prompt_id
                or self.edit_recipe.source_sha256 != self.take.seal.original_sha256
                or self.edit_recipe.edit_revision != self.take.edit_revision
                or self.edit_recipe.frame_range != self.take.frame_range
            ):
                raise ValueError("edit recipe must bind the exact committed source and revision")
        if self.cursor is not None and self.cursor.operation_id != self.operation_id:
            raise ValueError("cursor must bind its committing operation")
        if self.navigation is not None and self.cursor is not None:
            if self.navigation.selected_take_id is not None and self.kind != "navigate":
                raise ValueError("Retake clears its slot; only prompt selection selects a take")
            NavigationEvent(result=self.navigation, cursor=self.cursor)
        if (self.kind == "analysis") != (self.analysis is not None):
            raise ValueError("only analysis operations contain analysis evidence")
        if self.analysis is not None:
            if self.take is None or self.cursor is not None:
                raise ValueError("analysis publication requires one take and cannot navigate")
            result = self.analysis.result
            if (
                self.take.seal is None
                or result.take_id != self.take.reservation.take_id
                or self.analysis.job_sha256
                != recording_analysis_job_sha256(
                    seal=self.take.seal,
                    analyzer_build=result.analyzer_build,
                    algorithm_version=result.algorithm_version,
                    edit_revision=result.edit_revision,
                    context_revision=result.context_revision,
                    allow_quiet=self.take.reservation.prompt.allows_quiet,
                )
                or result.source_sha256 != self.take.seal.original_sha256
                or result.edit_revision != self.take.edit_revision
                or result.context_revision != self.take.reservation.group.context_revision
                or result.state != self.take.analysis
                or (result.completed_at is not None and result.completed_at > self.committed_at)
                or (
                    self.analysis.source_identity is not None
                    and self.analysis.source_identity.relative_path != self.take.seal.original_path
                )
            ):
                raise ValueError("analysis operation must bind its exact take/source/revisions")
        if (self.kind == "package") != (self.package is not None):
            raise ValueError("only package operations contain package publication evidence")
        if self.package is not None:
            entry = self.package.entry
            if (
                self.take is None
                or self.take.seal is None
                or self.cursor is not None
                or self.take.export != "prepared"
                or entry.take_id != self.take.reservation.take_id
                or entry.group_id != self.take.reservation.group.group_id
                or entry.subject_id != self.take.reservation.group.subject_id
                or entry.catalog_sha256 != self.take.reservation.group.catalog_sha256
                or entry.prompt != self.take.reservation.prompt
                or entry.original_sha256 != self.take.seal.original_sha256
                or entry.decision_revision != self.take.decision_revision
                or entry.edit_revision != self.take.edit_revision
                or entry.frame_range != self.take.frame_range
            ):
                raise ValueError("package publication must bind the exact prepared take revision")
        return self


class PendingRecordingOperation(ContractModel):
    schema_version: Literal["cole-voice-lab.pending-recording-operation/v1"] = (
        "cole-voice-lab.pending-recording-operation/v1"
    )
    operation_id: StableId
    request_sha256: Sha256


class RecordingStoreHead(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-store-head/v1"] = (
        "cole-voice-lab.recording-store-head/v1"
    )
    revision: Revision
    operation_id: StableId | None = None


type TakeProjectionHook = Callable[[RecordingTake], object]
type TakeNavigationResolver = Callable[
    [RecordingTake, RecordingSelection], RecordingNavigationResult
]
# Latest retained take ID for one exact catalog prompt, or None without complete coverage.
type RetainedTakeResolver = Callable[[str, Prompt], str | None]
type CommitFaultHook = Callable[[str], None]


class RecordingSelectionError(ContractError):
    """Displayed workspace identity changed; retry from its authoritative cursor."""


class RecordingTakeStore:
    """Private take authority with short serialized commits and no audio-analysis executor."""

    def __init__(
        self,
        layout: AssetLayout,
        *,
        clock: Callable[[], datetime] = utc_now,
        projection_hook: TakeProjectionHook | None = None,
        reservation_projection_hook: TakeProjectionHook | None = None,
        navigation_resolver: TakeNavigationResolver | None = None,
        fault_hook: CommitFaultHook | None = None,
        index_revision_resolver: Callable[[], int] | None = None,
        missing_resolver: Callable[[str, str], RecordingNavigationResult] | None = None,
        retained_take_resolver: RetainedTakeResolver | None = None,
        read_only: bool = False,
    ) -> None:
        self.layout = AssetLayout.validate(layout.root)
        self.clock = clock
        self.projection_hook = projection_hook
        self.reservation_projection_hook = reservation_projection_hook
        self.navigation_resolver = navigation_resolver
        self.fault_hook = fault_hook
        self.index_revision_resolver = index_revision_resolver
        self.missing_resolver = missing_resolver
        self.retained_take_resolver = retained_take_resolver
        # Background evidence producers cannot repair the service owner's pending
        # projection journal or acquire an interactive metadata lock for head reads.
        self.read_only = read_only
        if read_only and (projection_hook is not None or reservation_projection_hook is not None):
            raise ContractError("a read-only recording store cannot own a projection hook")
        self._thread_lock = threading.RLock()
        self._current_group: RecordingGroup | None = None
        self._directory(TAKE_PROVENANCE_ROOT)
        self._directory(f"{TAKE_PROVENANCE_ROOT}/.operations")
        self._directory(f"{TAKE_PROVENANCE_ROOT}/.cursors")
        self._directory(f"{TAKE_PROVENANCE_ROOT}/.analysis")
        self._directory(GROUP_PROVENANCE_ROOT)
        self._directory(f"{GROUP_PROVENANCE_ROOT}/.prepared-gates")
        if read_only:
            self._require_projection_quiet()
        else:
            with self._metadata_lock():
                self._repair_pending()

    def _fault(self, boundary: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(boundary)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        sync_recording_evidence_directory(directory)

    def _directory(self, relative: str) -> Path:
        return recording_evidence_directory(self.layout, relative, create=not self.read_only)

    def _path(self, relative: str) -> Path:
        return confined_path(self.layout.root, relative)

    def _take_path(self, take_id: str, filename: str) -> Path:
        identifier = _STABLE_ID.validate_python(take_id)
        return self._path(f"{TAKE_PROVENANCE_ROOT}/{identifier}/{filename}")

    def _operation_path(self, operation_id: str) -> Path:
        identifier = _STABLE_ID.validate_python(operation_id)
        return self._path(f"{TAKE_PROVENANCE_ROOT}/.operations/{identifier}.json")

    def _read[ModelT: ContractModel](self, path: Path, model: type[ModelT]) -> ModelT:
        return read_recording_evidence(path, model)

    def _sync_file_and_parent(self, path: Path) -> None:
        with open_regular_no_follow(path) as stream:
            os.fsync(stream.fileno())
        self._sync_directory(path.parent)

    def _write(self, path: Path, model: ContractModel) -> None:
        self._require_writable()
        write_recording_evidence(path, model)

    def _write_immutable(self, path: Path, model: ContractModel) -> None:
        self._require_writable()
        write_recording_evidence(path, model, immutable=True)

    @contextmanager
    def _metadata_lock(self) -> Iterator[None]:
        self._require_writable()
        with self._thread_lock:
            path = self._path(f"{TAKE_PROVENANCE_ROOT}/.metadata.lock")
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise SecurityError("recording metadata lock must be one private regular file")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RecordingMetadataBusyError(
                        "another recording metadata operation is committing"
                    ) from error
                yield
            finally:
                os.close(descriptor)

    def _require_writable(self) -> None:
        if self.read_only:
            raise ContractError("a read-only recording store cannot commit or repair metadata")

    def _store_head(self, *, initial_operation_id: str | None = None) -> RecordingStoreHead:
        path = self._path(f"{TAKE_PROVENANCE_ROOT}/.store-head.json")
        if path.exists():
            return self._read(path, RecordingStoreHead)
        entries = tuple(
            itertools.islice(self._path(f"{TAKE_PROVENANCE_ROOT}/.operations").iterdir(), 2)
        )
        if entries and (
            len(entries) != 1
            or initial_operation_id is None
            or entries[0] != self._operation_path(initial_operation_id)
        ):
            raise SecurityError(
                "recording store head is missing despite committed operation evidence"
            )
        return RecordingStoreHead(revision=0)

    @property
    def state_revision(self) -> int:
        """Read the durable metadata revision without scanning takes or opening audio."""
        with self._metadata_lock():
            self._repair_pending()
            return self._store_head().revision

    def _repair_pending(self) -> None:
        self._require_writable()
        pending_path = self._path(f"{TAKE_PROVENANCE_ROOT}/.pending.json")
        if not pending_path.exists():
            return
        pending = self._read(pending_path, PendingRecordingOperation)
        operation_path = self._operation_path(pending.operation_id)
        if not operation_path.exists():
            return  # An uncommitted intent cannot become a kept decision.
        operation = self._read(operation_path, RecordingTakeOperation)
        if (
            operation.operation_id != pending.operation_id
            or operation.request_sha256 != pending.request_sha256
        ):
            raise SecurityError("recording operation journal identity changed")
        self._sync_file_and_parent(operation_path)
        self._apply_operation(operation)

    def _apply_operation(
        self,
        operation: RecordingTakeOperation,
        *,
        current_session_reservation: bool = False,
    ) -> RecordingTake | None:
        self._require_writable()
        existing = None
        if operation.edit_recipe is not None:
            self._fault("before-edit-recipe")
            publish_native_edit_recipe(self.layout, operation.edit_recipe)
            self._fault("after-edit-recipe")
        if operation.take is not None:
            take_id = operation.take.reservation.take_id
            path = self._take_path(take_id, "head.json")
            existing = self._read(path, RecordingTake) if path.exists() else None
            if existing is None or existing.state_revision < operation.take.state_revision:
                self._fault("before-take-head")
                self._write(path, operation.take)
                self._fault("after-take-head")
                existing = operation.take
            elif (
                existing.state_revision == operation.take.state_revision
                and existing != operation.take
            ):
                raise SecurityError("recording take revision binds conflicting evidence")
            else:
                self._sync_file_and_parent(path)
        if operation.cursor is not None:
            path = self._cursor_path(operation.cursor.subject_id)
            cursor = self._read(path, RecordingCursor) if path.exists() else None
            if (
                cursor is None
                or cursor.selection.selection_revision
                < operation.cursor.selection.selection_revision
            ):
                self._fault("before-cursor-head")
                self._write(path, operation.cursor)
                self._fault("after-cursor-head")
            elif (
                cursor.selection.selection_revision == operation.cursor.selection.selection_revision
                and cursor != operation.cursor
            ):
                raise SecurityError("workspace cursor revision binds conflicting evidence")
            else:
                self._sync_file_and_parent(path)
        if operation.analysis is not None:
            analysis_path = self._analysis_path(operation.analysis.job_sha256)
            prior = (
                self._read(analysis_path, RecordingTakeOperation)
                if analysis_path.exists()
                else None
            )
            if prior is None or prior.store_revision < operation.store_revision:
                self._fault("before-analysis-head")
                self._write(analysis_path, operation)
                self._fault("after-analysis-head")
            elif prior.store_revision == operation.store_revision and prior != operation:
                raise SecurityError("analysis revision binds conflicting evidence")
        head = self._store_head(initial_operation_id=operation.operation_id)
        if head.revision == 0 and operation.store_revision != 1:
            raise SecurityError(
                "recording store head is missing during noninitial operation recovery"
            )
        if head.revision < operation.store_revision:
            self._fault("before-store-head")
            self._write(
                self._path(f"{TAKE_PROVENANCE_ROOT}/.store-head.json"),
                RecordingStoreHead(
                    revision=operation.store_revision,
                    operation_id=operation.operation_id,
                ),
            )
            self._fault("after-store-head")
        elif head.operation_id is not None:
            self._sync_file_and_parent(self._path(f"{TAKE_PROVENANCE_ROOT}/.store-head.json"))
        self._fault("before-projection")
        projection = (
            self.reservation_projection_hook
            if current_session_reservation
            else self.projection_hook
        )
        if projection is not None and existing is not None:
            projection(existing)
        self._fault("after-projection")
        pending_path = self._path(f"{TAKE_PROVENANCE_ROOT}/.pending.json")
        if pending_path.exists():
            pending = self._read(pending_path, PendingRecordingOperation)
            if pending.operation_id == operation.operation_id:
                self._fault("before-completion")
                pending_path.unlink()
                self._sync_directory(pending_path.parent)
                self._fault("after-completion")
        return existing

    def _replay(self, operation_id: str, request_sha256: str) -> RecordingTakeOperation | None:
        path = self._operation_path(operation_id)
        if not path.exists():
            return None
        operation = self._read(path, RecordingTakeOperation)
        if operation.operation_id != operation_id or operation.request_sha256 != request_sha256:
            raise ContractError("operation ID is already bound to a different recording command")
        self._sync_file_and_parent(path)
        self._apply_operation(operation)
        self._fault("before-ack")
        return operation

    def _commit(
        self,
        operation: RecordingTakeOperation,
        commit_barrier: Callable[[], None] | None,
        *,
        current_session_reservation: bool = False,
    ) -> None:
        if current_session_reservation and (
            operation.kind != "reserve" or self.reservation_projection_hook is None
        ):
            raise ContractError("current-session reservation requires its owned projection hook")
        if commit_barrier is not None:
            commit_barrier()
        self._fault("before-intent")
        self._write(
            self._path(f"{TAKE_PROVENANCE_ROOT}/.pending.json"),
            PendingRecordingOperation(
                operation_id=operation.operation_id,
                request_sha256=operation.request_sha256,
            ),
        )
        self._fault("after-intent")
        self._fault("before-operation")
        self._write_immutable(self._operation_path(operation.operation_id), operation)
        self._fault("after-operation")
        self._apply_operation(operation, current_session_reservation=current_session_reservation)
        self._fault("before-ack")

    def get_group(self, group_id: str) -> RecordingGroup:
        identifier = _STABLE_ID.validate_python(group_id)
        group = self._read(self._path(f"{GROUP_PROVENANCE_ROOT}/{identifier}.json"), RecordingGroup)
        if group.group_id != identifier:
            raise SecurityError("recording group filename does not match its identity")
        return group

    def _source_identity(self, relative: str) -> RecordingFileIdentity:
        metadata = self._path(relative).lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
        ):
            raise SecurityError("prepared capture gate contains unsafe source evidence")
        return RecordingFileIdentity(
            relative_path=relative,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size_bytes=metadata.st_size,
            mode=metadata.st_mode,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )

    def prepare_corpus_gate(
        self, catalog: PromptSuite, subject_id: str, *, approval_path: str, approval_sha256: str
    ) -> PreparedCaptureGate:
        """Perform expensive pilot validation once, outside the interactive metadata executor."""
        if catalog.purpose != "capture-corpus":
            raise ContractError("only a corpus catalog requires prepared pilot authorization")
        bound = load_pilot_gate(
            self.layout, relative_path=approval_path, expected_sha256=approval_sha256
        )
        now = self.clock()
        if bound.record.approved_at > now:
            raise ContractError("pilot approval cannot be in the future")
        identities: list[RecordingFileIdentity] = []
        for digest in bound.record.pilot_manifest_sha256s:
            root = self._path(f"raw/{digest}")
            manifest = self._read(root / "manifest.json", CaptureManifest)
            if manifest.subject_id != subject_id:
                raise ContractError("pilot approval belongs to another subject")
            for path in (root, *sorted(root.rglob("*"))):
                identities.append(
                    self._source_identity(path.relative_to(self.layout.root).as_posix())
                )
        sources = tuple(identities)
        gate = PreparedCaptureGate(
            state="ready",
            approval_path=approval_path,
            approval_sha256=approval_sha256,
            verified_at=now,
            evidence_sha256=canonical_sha256(
                [source.model_dump(mode="json") for source in sources]
            ),
        )
        evidence = PreparedRecordingGateEvidence(
            subject_id=subject_id,
            catalog_sha256=canonical_sha256(catalog),
            gate=gate,
            sources=sources,
        )
        self._write_immutable(
            self._gate_evidence_path(subject_id, canonical_sha256(catalog), gate), evidence
        )
        return gate

    def _gate_evidence_path(
        self, subject_id: str, catalog_sha256: str, gate: PreparedCaptureGate
    ) -> Path:
        key = canonical_sha256([subject_id, catalog_sha256, gate.model_dump(mode="json")])
        return self._path(f"{GROUP_PROVENANCE_ROOT}/.prepared-gates/{key}.json")

    def _validate_gate(self, group: RecordingGroup) -> None:
        if group.catalog.purpose != "capture-corpus":
            return
        gate = group.gate
        if (
            gate.state != "ready"
            or gate.approval_path is None
            or gate.approval_sha256 is None
            or gate.evidence_sha256 is None
            or gate.verified_at is None
            or gate.verified_at > self.clock()
        ):
            raise ContractError("corpus capture authorization is not prepared")
        evidence = self._read(
            self._gate_evidence_path(group.subject_id, group.catalog_sha256, gate),
            PreparedRecordingGateEvidence,
        )
        if (
            evidence.subject_id != group.subject_id
            or evidence.catalog_sha256 != group.catalog_sha256
            or evidence.gate != gate
        ):
            raise SecurityError("prepared corpus gate does not match this exact capture context")
        digest, _ = sha256_file(self._path(gate.approval_path))
        current = tuple(self._source_identity(source.relative_path) for source in evidence.sources)
        if digest != gate.approval_sha256 or current != evidence.sources:
            raise SecurityError(
                "prepared capture authorization changed; revalidate before recording"
            )

    def open_group(
        self,
        catalog: PromptSuite,
        subject_id: str,
        capture_device: CaptureDevice,
        capture_host: str,
        *,
        prepared_gate: PreparedCaptureGate | None = None,
        new_setup: bool = False,
        setup_description: str = "",
        _locked: bool = False,
        _operation_id: str | None = None,
    ) -> RecordingGroup:
        """Automatically reuse this process's compatible context, or durably open a new group."""
        # Every group is minted here, so no caller can bypass the service refusal.
        require_catalog_root(self.layout, catalog, subject_id)
        gate = prepared_gate or PreparedCaptureGate(
            state="unprepared" if catalog.purpose == "capture-corpus" else "not-required"
        )
        current = self._current_group
        compatible = current is not None and (
            current.catalog == catalog
            and current.subject_id == subject_id
            and current.capture_device == capture_device
            and current.capture_host == capture_host
            and current.gate == gate
            and current.setup_description == setup_description
        )
        if compatible and not new_setup:
            assert current is not None
            if current.gate.state == "ready":
                self._validate_gate(current)
            return current
        reason: Literal["app-start", "device-change", "configuration-change", "new-setup"] = (
            "app-start"
        )
        if new_setup:
            reason = "new-setup"
        elif current is not None:
            reason = (
                "device-change"
                if current.capture_device != capture_device
                else "configuration-change"
            )
        group_token = uuid4().hex if _operation_id is None else canonical_sha256(_operation_id)
        group = RecordingGroup(
            group_id=f"group-{group_token}",
            context_revision=0,
            subject_id=subject_id,
            catalog=catalog,
            catalog_sha256=canonical_sha256(catalog),
            capture_device=capture_device,
            capture_host=capture_host,
            opened_at=self.clock(),
            reason=reason,
            setup_description=setup_description,
            gate=gate,
        )
        group_path = self._path(f"{GROUP_PROVENANCE_ROOT}/{group.group_id}.json")
        if group_path.exists():
            existing = self.get_group(group.group_id)
            if (
                any(
                    getattr(existing, key) != getattr(group, key)
                    for key in (
                        "subject_id",
                        "catalog",
                        "capture_device",
                        "capture_host",
                        "setup_description",
                    )
                )
                or existing.gate.approval_sha256 != group.gate.approval_sha256
            ):
                raise ContractError("context operation is bound to another automatic group")
            group = existing
        if group.gate.state == "ready":
            self._validate_gate(group)
        with nullcontext() if _locked else self._metadata_lock():
            self._write_immutable(
                self._path(f"{GROUP_PROVENANCE_ROOT}/{group.group_id}.json"), group
            )
        self._current_group = group
        return group

    def reserve(
        self,
        *,
        operation_id: str,
        group_id: str,
        prompt_id: str,
        process_generation: str,
        selection: RecordingSelection | None = None,
        commit_barrier: Callable[[], None] | None = None,
        current_session_reservation: bool = False,
    ) -> CaptureReservation:
        """Sync exact prompt/context/intent and destinations before any microphone command."""
        if current_session_reservation and self.reservation_projection_hook is None:
            raise ContractError("current-session reservation requires its owned projection hook")
        request_sha = canonical_sha256(
            [
                "reserve",
                operation_id,
                group_id,
                prompt_id,
                process_generation,
            ]
        )
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(operation_id, request_sha)
            if replay is not None:
                assert replay.take is not None
                if current_session_reservation:
                    current = self._get_take(replay.take.reservation.take_id)
                    if current.seal is None:
                        assert self.reservation_projection_hook is not None
                        self.reservation_projection_hook(current)
                return replay.take.reservation
            group = self.get_group(group_id)
            cursor = (
                self._check_selection(group.subject_id, selection)
                if selection is not None
                else None
            )
            if cursor is not None and cursor.group_id != group_id:
                raise RecordingSelectionError("recording group changed")
            self._validate_gate(group)
            prompt = next(
                (item for item in group.catalog.prompts if item.prompt_id == prompt_id), None
            )
            if prompt is None:
                raise ContractError("capture prompt is not in the exact group catalog")
            take_id = f"take-{canonical_sha256(operation_id)}"
            self._directory(f"tmp/recording-takes/{take_id}")
            self._directory(f"{TAKE_PROVENANCE_ROOT}/{take_id}")
            path = self._take_path(take_id, "reservation.json")
            if path.exists():
                reservation = self._read(path, CaptureReservation)
                if (
                    reservation.operation_id != operation_id
                    or reservation.group != group
                    or reservation.prompt != prompt
                    or reservation.process_generation != process_generation
                ):
                    raise ContractError("interrupted reservation binds a different capture request")
                self._sync_file_and_parent(path)
            else:
                reservation = CaptureReservation(
                    take_id=take_id,
                    operation_id=operation_id,
                    process_generation=process_generation,
                    group=group,
                    prompt=prompt,
                    requested_at=self.clock(),
                    partial_path=f"tmp/recording-takes/{take_id}/capture.partial.wav",
                    original_path=f"tmp/recording-takes/{take_id}/original.wav",
                )
                self._fault("before-reservation")
                self._write_immutable(path, reservation)
                self._fault("after-reservation")
            take = RecordingTake(
                reservation=reservation,
                transport="ready",
                decision="draft",
                decision_revision=0,
                analysis="pending",
                export="unprepared",
                edit_revision=0,
                state_revision=0,
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=operation_id,
                    request_sha256=request_sha,
                    kind="reserve",
                    store_revision=self._store_head().revision + 1,
                    take=take,
                    cursor=self._next_cursor(cursor, operation_id, take_id=take_id)
                    if cursor is not None
                    else None,
                    committed_at=self.clock(),
                ),
                commit_barrier,
                current_session_reservation=current_session_reservation,
            )
            return reservation

    def operation_cursor(self, operation_id: str) -> RecordingCursor | None:
        with self._metadata_lock():
            self._repair_pending()
            operation = self._read(self._operation_path(operation_id), RecordingTakeOperation)
            return operation.cursor

    def reservation_result(self, operation_id: str) -> RecordingTake:
        """Return the original reserved state even after later seal/Save operations."""
        with self._metadata_lock():
            self._repair_pending()
            operation = self._read(self._operation_path(operation_id), RecordingTakeOperation)
            if operation.kind != "reserve" or operation.operation_id != operation_id:
                raise ContractError("operation does not identify a durable reservation")
            assert operation.take is not None
            return operation.take

    def _get_take(self, take_id: str) -> RecordingTake:
        path = self._take_path(take_id, "head.json")
        take = self._read(path, RecordingTake)
        if take.reservation.take_id != take_id:
            raise SecurityError("take head filename does not match its identity")
        reservation = self._read(self._take_path(take_id, "reservation.json"), CaptureReservation)
        if take.reservation != reservation:
            raise SecurityError("take head does not match its immutable reservation")
        if take.seal is not None and take.seal != self._read(
            self._take_path(take_id, "seal.json"), CaptureSealReceipt
        ):
            raise SecurityError("take head does not match its immutable capture receipt")
        if take.edit_revision > 0:
            recipe = current_native_edit(self.layout, take_id)
            if (
                recipe is None
                or take.seal is None
                or recipe.edit_revision != take.edit_revision
                or recipe.source_sha256 != take.seal.original_sha256
                or recipe.frame_range != take.frame_range
            ):
                raise SecurityError("take head differs from its current immutable frame recipe")
        return take

    def get_take(self, take_id: str) -> RecordingTake:
        """Read one coherent metadata head, repairing an interrupted committed operation first."""
        if self.read_only:
            return self.get_take_for_projection(take_id)
        with self._metadata_lock():
            self._repair_pending()
            return self._get_take(take_id)

    def _check_sealed_source(self, receipt: CaptureSealReceipt) -> None:
        path = self._path(receipt.original_path)
        expected = (
            b"RIFF",
            receipt.original_size_bytes - 8,
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            48_000,
            144_000,
            3,
            24,
            b"data",
            receipt.audio.frames * 3,
        )
        with open_regular_no_follow(path) as stream:
            before = os.fstat(stream.fileno())
            if (
                before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size != receipt.original_size_bytes
            ):
                raise SecurityError(
                    "sealed source must be one private file with the exact declared size"
                )
            header = stream.read(44)
            if len(header) != 44 or struct.unpack("<4sI4s4sIHHIIHH4sI", header) != expected:
                raise ContractError("sealed native WAV header disagrees with its capture receipt")
            after = os.fstat(stream.fileno())
            current = path.lstat()

            def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
                return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)

            if identity(before) != identity(after) or identity(after) != identity(current):
                raise SecurityError("sealed source changed during structural registration")

    def register_seal(
        self,
        *,
        operation_id: str,
        receipt: CaptureSealReceipt,
        commit_barrier: Callable[[], None] | None = None,
    ) -> RecordingTake:
        """Persist a structurally bound native draft; independent hash/QC remains pending."""
        request_sha = canonical_sha256(
            ["register-seal", operation_id, receipt.model_dump(mode="json")]
        )
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(operation_id, request_sha)
            if replay is not None:
                assert replay.take is not None
                return replay.take
            current = self._get_take(receipt.take_id)
            if current.seal is not None:
                if current.seal != receipt:
                    raise SecurityError("take already has a different immutable seal")
                raise ContractError("seal already registered; replay its original operation ID")
            take = RecordingTake.model_validate(
                current.model_dump()
                | {
                    "seal": receipt,
                    "transport": "draft",
                    "state_revision": current.state_revision + 1,
                    "frame_range": RecordingFrameRange(
                        source_frames=receipt.audio.frames,
                        start_frame=0,
                        end_frame=receipt.audio.frames,
                    ),
                }
            )
            self._check_sealed_source(receipt)
            if self._path(current.reservation.partial_path).exists():
                raise ContractError("sealed source still has an ambiguous active partial")
            self._fault("before-seal")
            self._write_immutable(self._take_path(receipt.take_id, "seal.json"), receipt)
            self._fault("after-seal")
            self._commit(
                RecordingTakeOperation(
                    operation_id=operation_id,
                    request_sha256=request_sha,
                    kind="register-seal",
                    store_revision=self._store_head().revision + 1,
                    take=take,
                    committed_at=self.clock(),
                ),
                commit_barrier,
            )
            return take

    def save(
        self,
        command: SaveTakeCommand,
        *,
        commit_barrier: Callable[[], None] | None = None,
        subject_id: str | None = None,
    ) -> SaveEvent:
        """Sync an explicit keep decision and resolved next target before acknowledgment."""
        request_sha = canonical_sha256(command)
        take_id = command.selection.take_id
        if take_id is None:
            raise ContractError("Save requires the displayed current draft")
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(command.operation_id, request_sha)
            if replay is not None:
                assert replay.save is not None and replay.take is not None
                return SaveEvent(operation=replay.save, take=replay.take, cursor=replay.cursor)
            if subject_id is not None:
                self._check_selection(subject_id, command.selection)
            current = self._get_take(take_id)
            cursor = self._check_selection(current.reservation.group.subject_id, command.selection)
            if (
                current.decision != "draft"
                or current.seal is None
                or current.transport != "draft"
                or current.analysis == "invalid"
            ):
                raise ContractError("Save requires a valid sealed current draft")
            if (
                current.reservation.group.catalog_sha256 != command.selection.catalog_sha256
                or current.reservation.prompt.prompt_id != command.selection.prompt_id
            ):
                raise ContractError("Save selection changed from its exact prompt and catalog")
            if (
                current.decision_revision != command.expected_decision_revision
                or current.edit_revision != command.expected_edit_revision
                or command.frame_range.source_frames != current.seal.audio.frames
            ):
                raise ContractError("Save decision/edit/source revision changed")
            with open_regular_no_follow(self._path(current.seal.original_path)) as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                    or metadata.st_size != current.seal.original_size_bytes
                ):
                    raise SecurityError("Save source no longer has its private sealed identity")
            edited = command.frame_range != current.frame_range
            proposed = RecordingTake.model_validate(
                current.model_dump()
                | {
                    "decision": "kept",
                    "decision_revision": current.decision_revision + 1,
                    "decision_evidence": None,
                    "analysis": "pending" if edited else current.analysis,
                    "export": "unprepared" if edited else current.export,
                    "transport": "ready",
                    "frame_range": command.frame_range,
                    "edit_revision": current.edit_revision + int(edited),
                    "state_revision": current.state_revision + 1,
                }
            )
            navigation = None
            if command.advance:
                if self.navigation_resolver is None:
                    raise ContractError("Save and Next requires the complete indexed catalog")
                navigation = self.navigation_resolver(proposed, command.selection)
            cursor_result = (
                self._next_cursor(
                    cursor, command.operation_id, take_id=take_id, navigation=navigation
                )
                if cursor is not None
                else None
            )
            now = self.clock()
            operation = RecordingSaveOperation(
                operation_id=command.operation_id,
                take_id=take_id,
                receipt_sha256=canonical_sha256(current.seal),
                selection=command.selection,
                decision_revision=proposed.decision_revision,
                edit_revision=proposed.edit_revision,
                frame_range=command.frame_range,
                advance=command.advance,
                navigation=navigation,
                committed_at=now,
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=command.operation_id,
                    request_sha256=request_sha,
                    kind="save",
                    store_revision=self._store_head().revision + 1,
                    take=proposed,
                    cursor=cursor_result,
                    save=operation,
                    edit_recipe=self._recipe(proposed, command.operation_id, now)
                    if edited
                    else None,
                    committed_at=now,
                ),
                commit_barrier,
            )
            return SaveEvent(operation=operation, take=proposed, cursor=cursor_result)

    def _cursor_path(self, subject_id: str) -> Path:
        subject = _STABLE_ID.validate_python(subject_id)
        return self._path(f"{TAKE_PROVENANCE_ROOT}/.cursors/{subject}.json")

    def _cursor(self, subject_id: str) -> RecordingCursor | None:
        path = self._cursor_path(subject_id)
        if not path.exists():
            return None
        cursor = self._read(path, RecordingCursor)
        if cursor.subject_id != subject_id:
            raise SecurityError("workspace cursor filename changed subject identity")
        return cursor

    def get_cursor(self, subject_id: str) -> RecordingCursor | None:
        with self._metadata_lock():
            self._repair_pending()
            return self._cursor(subject_id)

    def _check_selection(
        self, subject_id: str, selection: RecordingSelection
    ) -> RecordingCursor | None:
        cursor = self._cursor(subject_id)
        if cursor is not None and any(
            getattr(cursor.selection, key) != getattr(selection, key)
            for key in ("catalog_sha256", "prompt_id", "take_id", "selection_revision")
        ):
            raise RecordingSelectionError(
                "displayed prompt, take, catalog or selection revision changed"
            )
        if (
            self.index_revision_resolver is not None
            and selection.index_revision > self.index_revision_resolver()
        ):
            raise RecordingSelectionError("displayed index revision is from an unavailable future")
        return cursor

    def _next_cursor(
        self,
        cursor: RecordingCursor,
        operation_id: str,
        *,
        take_id: str | None,
        navigation: RecordingNavigationResult | None = None,
        mode: Literal["selected-prompt", "continue-missing"] | None = None,
    ) -> RecordingCursor:
        prompt_id = cursor.selection.prompt_id
        if navigation is not None and navigation.next_prompt is not None:
            prompt_id = navigation.next_prompt.prompt_id
            take_id = navigation.selected_take_id
        revision = (
            navigation.index_revision
            if navigation is not None
            else self.index_revision_resolver()
            if self.index_revision_resolver is not None
            else cursor.selection.index_revision
        )
        return RecordingCursor(
            subject_id=cursor.subject_id,
            group_id=cursor.group_id,
            operation_id=operation_id,
            selection=RecordingSelection(
                catalog_sha256=cursor.selection.catalog_sha256,
                prompt_id=prompt_id,
                take_id=take_id,
                selection_revision=cursor.selection.selection_revision + 1,
                index_revision=revision,
            ),
            mode=mode or cursor.mode,
            complete=navigation.complete if navigation is not None else False,
        )

    def context_replay(self, command: OpenContextCommand) -> ContextEvent | None:
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(command.operation_id, canonical_sha256(command))
            if replay is None:
                return None
            if replay.kind != "open-context" or replay.cursor is None:
                raise ContractError("operation does not identify a context")
            return ContextEvent(group=self.get_group(replay.cursor.group_id), cursor=replay.cursor)

    def open_context(
        self,
        command: OpenContextCommand,
        *,
        prepared_gate: PreparedCaptureGate | None = None,
        commit_barrier: Callable[[], None] | None = None,
    ) -> ContextEvent:
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(command.operation_id, canonical_sha256(command))
            if replay is not None:
                assert replay.cursor is not None
                return ContextEvent(
                    group=self.get_group(replay.cursor.group_id), cursor=replay.cursor
                )
            previous = self._cursor(command.subject_id)
            if (
                previous.selection.selection_revision if previous is not None else None
            ) != command.expected_selection_revision:
                raise RecordingSelectionError("workspace changed while context was being prepared")
            group = self.open_group(
                command.catalog,
                command.subject_id,
                command.capture_device,
                command.capture_host,
                prepared_gate=prepared_gate,
                new_setup=command.new_setup,
                setup_description=command.setup_description,
                _locked=True,
                _operation_id=command.operation_id,
            )
            keep_selection = (
                previous is not None and previous.selection.catalog_sha256 == group.catalog_sha256
            )
            cursor = RecordingCursor(
                subject_id=group.subject_id,
                group_id=group.group_id,
                selection=RecordingSelection(
                    catalog_sha256=group.catalog_sha256,
                    prompt_id=previous.selection.prompt_id
                    if keep_selection and previous is not None
                    else group.catalog.prompts[0].prompt_id,
                    take_id=previous.selection.take_id
                    if keep_selection and previous is not None
                    else None,
                    selection_revision=previous.selection.selection_revision + 1
                    if previous is not None
                    else 0,
                    index_revision=self.index_revision_resolver()
                    if self.index_revision_resolver is not None
                    else 0,
                ),
                mode=previous.mode
                if keep_selection and previous is not None
                else "selected-prompt",
                complete=previous.complete if keep_selection and previous is not None else False,
                operation_id=command.operation_id,
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=command.operation_id,
                    request_sha256=canonical_sha256(command),
                    kind="open-context",
                    store_revision=self._store_head().revision + 1,
                    cursor=cursor,
                    committed_at=self.clock(),
                ),
                commit_barrier,
            )
            return ContextEvent(group=group, cursor=cursor)

    def _latest_retained_take(self, group: RecordingGroup, prompt: Prompt) -> str | None:
        """Show an explicitly selected prompt's latest retained take; never read its audio.

        Runs under the metadata lock. A native index row must still match its take head, so
        a stale projection selects nothing rather than a removed, rejected or invalid take.
        Legacy rows have no store head and rely on the complete index alone.
        """
        if self.retained_take_resolver is None:
            return None
        take_id = self.retained_take_resolver(group.catalog_sha256, prompt)
        if take_id is None or not take_id.startswith("take-"):
            return take_id
        if not self._take_path(take_id, "head.json").exists():
            return None
        take = self._get_take(take_id)
        if (
            take.decision != "kept"
            or take.analysis == "invalid"
            or take.reservation.group.subject_id != group.subject_id
            or take.reservation.prompt != prompt
        ):
            return None
        return take_id

    def navigate(
        self,
        command: NavigateCommand,
        *,
        subject_id: str,
        commit_barrier: Callable[[], None] | None = None,
    ) -> NavigationEvent:
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(command.operation_id, canonical_sha256(command))
            if replay is not None:
                assert replay.navigation is not None and replay.cursor is not None
                return NavigationEvent(result=replay.navigation, cursor=replay.cursor)
            cursor = self._check_selection(subject_id, command.selection)
            if cursor is None:
                raise RecordingSelectionError("open the recording context before navigating")
            group = self.get_group(cursor.group_id)
            if command.mode == "selected-prompt":
                prompt = next(
                    (p for p in group.catalog.prompts if p.prompt_id == command.target_prompt_id),
                    None,
                )
                if prompt is None:
                    raise RecordingSelectionError(
                        "selected prompt is absent from the exact catalog"
                    )
                # Resolve before reading the revision, so the cursor never claims an older
                # index than the evidence behind its selected take.
                selected_take_id = self._latest_retained_take(group, prompt)
                result = RecordingNavigationResult(
                    catalog_sha256=group.catalog_sha256,
                    index_revision=self.index_revision_resolver()
                    if self.index_revision_resolver is not None
                    else cursor.selection.index_revision,
                    next_prompt=prompt,
                    complete=False,
                    selected_take_id=selected_take_id,
                )
            else:
                if self.missing_resolver is None:
                    raise ContractError("Continue Missing requires the complete metadata index")
                result = self.missing_resolver(group.catalog_sha256, cursor.selection.prompt_id)
            resolved = self._next_cursor(
                cursor,
                command.operation_id,
                take_id=cursor.selection.take_id,
                navigation=result,
                mode=command.mode,
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=command.operation_id,
                    request_sha256=canonical_sha256(command),
                    kind="navigate",
                    navigation=result,
                    store_revision=self._store_head().revision + 1,
                    cursor=resolved,
                    committed_at=self.clock(),
                ),
                commit_barrier,
            )
            return NavigationEvent(result=result, cursor=resolved)

    @staticmethod
    def _recipe(
        take: RecordingTake, operation_id: str, updated_at: datetime
    ) -> RecordingEditRecipe:
        assert take.seal is not None and take.frame_range is not None
        return RecordingEditRecipe(
            subject_id=take.reservation.group.subject_id,
            take_id=take.reservation.take_id,
            prompt_id=take.reservation.prompt.prompt_id,
            source_sha256=take.seal.original_sha256,
            frame_range=take.frame_range,
            edit_revision=take.edit_revision,
            operation_id=operation_id,
            updated_at=updated_at,
        )

    def edit(
        self, command: EditTakeCommand, *, commit_barrier: Callable[[], None] | None = None
    ) -> EditEvent:
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(command.operation_id, canonical_sha256(command))
            if replay is not None:
                assert replay.take is not None and replay.edit_recipe is not None
                return EditEvent(
                    operation_id=command.operation_id,
                    take=replay.take,
                    recipe_sha256=canonical_sha256(replay.edit_recipe),
                    derivative_state="pending" if replay.edit_recipe.trimmed else "not-required",
                )
            current = self._get_take(command.take_id)
            self._check_selection(current.reservation.group.subject_id, command.selection)
            if (
                command.selection.take_id != command.take_id
                or command.selection.prompt_id != current.reservation.prompt.prompt_id
            ):
                raise RecordingSelectionError("edit no longer identifies the displayed source")
            if (
                current.seal is None
                or current.frame_range is None
                or current.seal.original_sha256 != command.source_sha256
                or current.edit_revision != command.expected_edit_revision
                or command.frame_range.source_frames != current.seal.audio.frames
            ):
                raise RecordingSelectionError("edit source digest, frames or revision changed")
            self._check_sealed_source(current.seal)
            now = self.clock()
            take = RecordingTake.model_validate(
                current.model_dump()
                | {
                    "frame_range": command.frame_range,
                    "edit_revision": current.edit_revision + 1,
                    "analysis": "pending",
                    "export": "unprepared",
                    "state_revision": current.state_revision + 1,
                }
            )
            recipe = self._recipe(take, command.operation_id, now)
            self._commit(
                RecordingTakeOperation(
                    operation_id=command.operation_id,
                    request_sha256=canonical_sha256(command),
                    kind="edit",
                    store_revision=self._store_head().revision + 1,
                    take=take,
                    edit_recipe=recipe,
                    committed_at=now,
                ),
                commit_barrier,
            )
            return EditEvent(
                operation_id=command.operation_id,
                take=take,
                recipe_sha256=canonical_sha256(recipe),
                derivative_state="pending" if recipe.trimmed else "not-required",
            )

    def decide(
        self, command: DecisionCommand, *, commit_barrier: Callable[[], None] | None = None
    ) -> DecisionEvent:
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(command.operation_id, canonical_sha256(command))
            if replay is not None:
                assert replay.take is not None and replay.take.decision_evidence is not None
                return DecisionEvent(take=replay.take, decision=replay.take.decision_evidence)
            current = self._get_take(command.take_id)
            self._check_selection(current.reservation.group.subject_id, command.selection)
            if (
                command.selection.take_id != command.take_id
                or current.decision_revision != command.expected_decision_revision
            ):
                raise RecordingSelectionError(
                    "decision no longer identifies the displayed take/revision"
                )
            if current.seal is None:
                raise ContractError("decision requires a stopped sealed take")
            prior = current.decision_evidence
            restore: Literal["draft", "kept"] | None
            if current.decision == "draft":
                restore = "draft"
            elif current.decision == "kept":
                restore = "kept"
            else:
                restore = None if prior is None else prior.restore_decision
            decision: Literal["draft", "kept", "rejected", "removed"]
            if command.action == "restore":
                if current.decision not in {"removed", "rejected"} or restore is None:
                    raise ContractError("take has no recoverable prior decision")
                decision = restore
            else:
                if restore is None:
                    raise ContractError("take exclusion is missing its recoverable prior decision")
                decision = "rejected" if command.action == "reject" else "removed"
            now = self.clock()
            evidence = RecordingDecisionEvidence(
                operation_id=command.operation_id,
                revision=current.decision_revision + 1,
                action=command.action,
                previous=current.decision,
                current=decision,
                restore_decision=None if command.action == "restore" else restore,
                reason=command.reason,
                committed_at=now,
            )
            take = RecordingTake.model_validate(
                current.model_dump()
                | {
                    "decision": decision,
                    "decision_revision": evidence.revision,
                    "decision_evidence": evidence,
                    "export": "unprepared",
                    "transport": "draft" if decision == "draft" else "ready",
                    "state_revision": current.state_revision + 1,
                }
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=command.operation_id,
                    request_sha256=canonical_sha256(command),
                    kind="decision",
                    store_revision=self._store_head().revision + 1,
                    take=take,
                    committed_at=now,
                ),
                commit_barrier,
            )
            return DecisionEvent(take=take, decision=evidence)

    def select_review(
        self,
        command: ReviewTakeCommand,
        source: RecordingReviewSource,
        *,
        commit_barrier: Callable[[], None] | None = None,
    ) -> ReviewEvent:
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(command.operation_id, canonical_sha256(command))
            if replay is not None:
                assert replay.review_source is not None and replay.cursor is not None
                return ReviewEvent(source=replay.review_source, cursor=replay.cursor)
            cursor = self._check_selection(source.subject_id, command.selection)
            if cursor is None or command.take_id != source.take_id:
                raise RecordingSelectionError("review target or workspace changed")
            group = self.get_group(cursor.group_id)
            if (
                source.catalog_sha256 != group.catalog_sha256
                or source.prompt not in group.catalog.prompts
            ):
                raise RecordingSelectionError(
                    "review source does not belong to the exact current catalog"
                )
            if source.original_kind == "native-original":
                current = self._get_take(source.take_id)
                if (
                    current.seal is None
                    or current.seal.original_sha256 != source.source_sha256
                    or current.decision_revision != source.decision_revision
                    or current.edit_revision != source.edit_revision
                    or current.frame_range != source.frame_range
                ):
                    raise RecordingSelectionError(
                        "selected source changed during review preparation"
                    )
            if (
                cursor.selection.take_id == source.take_id
                and cursor.selection.prompt_id == source.prompt.prompt_id
            ):
                return ReviewEvent(source=source, cursor=cursor)
            selection = RecordingSelection(
                catalog_sha256=group.catalog_sha256,
                prompt_id=source.prompt.prompt_id,
                take_id=source.take_id,
                selection_revision=cursor.selection.selection_revision + 1,
                index_revision=self.index_revision_resolver()
                if self.index_revision_resolver is not None
                else cursor.selection.index_revision,
            )
            selected = RecordingCursor(
                subject_id=source.subject_id,
                group_id=group.group_id,
                selection=selection,
                mode="selected-prompt",
                complete=False,
                operation_id=command.operation_id,
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=command.operation_id,
                    request_sha256=canonical_sha256(command),
                    kind="review",
                    store_revision=self._store_head().revision + 1,
                    cursor=selected,
                    review_source=source,
                    committed_at=self.clock(),
                ),
                commit_barrier,
            )
            return ReviewEvent(source=source, cursor=selected)

    def retake(
        self,
        command: RetakeCommand,
        *,
        subject_id: str,
        commit_barrier: Callable[[], None] | None = None,
    ) -> NavigationEvent:
        """Clear the selected slot; the previous independent take and decision stay recoverable."""
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(command.operation_id, canonical_sha256(command))
            if replay is not None:
                assert replay.navigation is not None and replay.cursor is not None
                return NavigationEvent(result=replay.navigation, cursor=replay.cursor)
            cursor = self._check_selection(subject_id, command.selection)
            if cursor is None:
                raise RecordingSelectionError("retake requires its displayed context")
            group = self.get_group(cursor.group_id)
            prompt = next(
                item
                for item in group.catalog.prompts
                if item.prompt_id == cursor.selection.prompt_id
            )
            navigation = RecordingNavigationResult(
                catalog_sha256=group.catalog_sha256,
                index_revision=self.index_revision_resolver()
                if self.index_revision_resolver is not None
                else cursor.selection.index_revision,
                next_prompt=prompt,
                complete=False,
            )
            selected = self._next_cursor(
                cursor,
                command.operation_id,
                take_id=None,
                navigation=navigation,
                mode="selected-prompt",
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=command.operation_id,
                    request_sha256=canonical_sha256(command),
                    kind="retake",
                    store_revision=self._store_head().revision + 1,
                    cursor=selected,
                    navigation=navigation,
                    committed_at=self.clock(),
                ),
                commit_barrier,
            )
            return NavigationEvent(result=navigation, cursor=selected)

    def _analysis_path(self, job_sha256: str) -> Path:
        digest: str = TypeAdapter(Sha256).validate_python(job_sha256)
        return self._path(f"{TAKE_PROVENANCE_ROOT}/.analysis/{digest}.json")

    def analysis_result(self, job_sha256: str) -> RecordingAnalysisPublication | None:
        """Read the repaired journal projection; no sample or historical file inspection."""
        if self.read_only:
            return self.analysis_result_for_projection(job_sha256)
        with self._metadata_lock():
            self._repair_pending()
            return self._analysis_result(job_sha256)

    def _analysis_result(self, job_sha256: str) -> RecordingAnalysisPublication | None:
        path = self._analysis_path(job_sha256)
        if not path.exists():
            return None
        operation = self._read(path, RecordingTakeOperation)
        if operation.analysis is None or operation.analysis.job_sha256 != job_sha256:
            raise SecurityError("analysis evidence filename changed")
        return operation.analysis

    def analysis_result_for_projection(
        self, job_sha256: str
    ) -> RecordingAnalysisPublication | None:
        """Read one atomic analysis publication without a metadata lock or journal repair.

        A concurrent pending operation raises RecordingMetadataBusyError; the owned
        background scheduler retries. Filename and publication bindings remain checked.
        """
        self._require_projection_quiet()
        try:
            return self._analysis_result(job_sha256)
        finally:
            self._require_projection_quiet()

    def publish_analysis(
        self, *, operation_id: str, publication: RecordingAnalysisPublication
    ) -> RecordingTake:
        """Commit an exact worker revision without moving selection or changing operator intent."""
        request_sha = canonical_sha256(publication)
        result = publication.result
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(operation_id, request_sha)
            if replay is not None:
                assert replay.take is not None
                return replay.take
            current = self._get_take(result.take_id)
            if (
                current.seal is None
                or current.seal.original_sha256 != result.source_sha256
                or current.edit_revision != result.edit_revision
                or current.reservation.group.context_revision != result.context_revision
            ):
                raise StaleRecordingAnalysisError(
                    "analysis result is stale for this source/edit/context"
                )
            previous_path = self._analysis_path(publication.job_sha256)
            previous_operation = (
                self._read(previous_path, RecordingTakeOperation)
                if previous_path.exists()
                else None
            )
            previous = None if previous_operation is None else previous_operation.analysis
            if previous is not None and (
                previous.result.algorithm_version != result.algorithm_version
                or previous.result.analyzer_build != result.analyzer_build
                or previous.result.queued_at != result.queued_at
                or publication.attempt < previous.attempt
                or (
                    publication.attempt == previous.attempt
                    and previous.result.completed_at is not None
                )
            ):
                raise StaleRecordingAnalysisError(
                    "analysis attempt is stale or its job binding changed"
                )
            if publication.source_identity is not None:
                if publication.source_identity.relative_path != current.seal.original_path:
                    raise SecurityError("analysis source path differs from immutable seal")
                try:
                    identity = self._source_identity(current.seal.original_path)
                except (FileNotFoundError, SecurityError) as error:
                    raise AnalysisSourceChangedError(
                        "analysis source disappeared or became unsafe"
                    ) from error
                if identity != publication.source_identity:
                    raise AnalysisSourceChangedError(
                        "analysis source changed before durable publication"
                    )
            take = RecordingTake.model_validate(
                current.model_dump()
                | {
                    "analysis": result.state,
                    "export": "unprepared" if result.state != "verified" else current.export,
                    "state_revision": current.state_revision + 1,
                }
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=operation_id,
                    request_sha256=request_sha,
                    kind="analysis",
                    store_revision=self._store_head().revision + 1,
                    take=take,
                    analysis=publication,
                    committed_at=self.clock(),
                ),
                None,
            )
            return take

    def publish_package(self, *, operation_id: str, session_id: str, take_id: str) -> RecordingTake:
        """Project a committed package map through the canonical journal; never navigate."""
        from .recording_packages import load_recording_package_map

        package_map = load_recording_package_map(self.layout, session_id)
        if package_map is None:
            raise ContractError("prepared package lacks its required local registration")
        entries = [entry for entry in package_map.entries if entry.take_id == take_id]
        if len(entries) != 1:
            raise ContractError("package does not identify this original take exactly once")
        publication = RecordingPackagePublication(
            session_id=session_id,
            manifest_sha256=package_map.manifest_sha256,
            package_map_sha256=canonical_sha256(package_map),
            entry=entries[0],
        )
        request_sha = canonical_sha256(publication)
        with self._metadata_lock():
            self._repair_pending()
            replay = self._replay(operation_id, request_sha)
            if replay is not None:
                assert replay.take is not None
                return replay.take
            current = self._get_take(take_id)
            entry = publication.entry
            if (
                current.decision != "kept"
                or current.analysis != "verified"
                or current.seal is None
                or current.seal.original_sha256 != entry.original_sha256
                or current.decision_revision != entry.decision_revision
                or current.edit_revision != entry.edit_revision
                or current.frame_range != entry.frame_range
            ):
                raise StaleRecordingAnalysisError(
                    "package belongs to a superseded take decision/edit"
                )
            take = RecordingTake.model_validate(
                current.model_dump()
                | {
                    "export": "prepared",
                    "state_revision": current.state_revision + 1,
                }
            )
            self._commit(
                RecordingTakeOperation(
                    operation_id=operation_id,
                    request_sha256=request_sha,
                    kind="package",
                    store_revision=self._store_head().revision + 1,
                    take=take,
                    package=publication,
                    committed_at=self.clock(),
                ),
                None,
            )
            return take

    def _take_ids(self, after_take_id: str | None, limit: int) -> tuple[str, ...]:
        if not 1 <= limit <= MAX_HISTORY_PAGE_SIZE:
            raise ValueError("take metadata page size is outside its bounds")
        if after_take_id is not None:
            _STABLE_ID.validate_python(after_take_id)
        # Path.iterdir materializes os.listdir on Python 3.12. Keep discovery
        # memory bounded to the requested heap size even for a large history.
        with os.scandir(self._path(TAKE_PROVENANCE_ROOT)) as entries:
            return tuple(
                heapq.nsmallest(
                    limit,
                    (
                        _STABLE_ID.validate_python(entry.name)
                        for entry in entries
                        if entry.name.startswith("take-")
                        and (after_take_id is None or entry.name > after_take_id)
                    ),
                )
            )

    def iter_take_ids(
        self, *, after_take_id: str | None = None, limit: int = MAX_HISTORY_PAGE_SIZE
    ) -> tuple[str, ...]:
        """Page evidence identities so recovery can isolate one unreadable take."""
        with self._metadata_lock():
            self._repair_pending()
            return self._take_ids(after_take_id, limit)

    def iter_takes(
        self, *, after_take_id: str | None = None, limit: int = MAX_HISTORY_PAGE_SIZE
    ) -> tuple[RecordingTake, ...]:
        """Page canonical take metadata for explicit index import; never open historical audio."""
        with self._metadata_lock():
            self._repair_pending()
            return tuple(
                self._get_take(take_id) for take_id in self._take_ids(after_take_id, limit)
            )

    def _require_projection_quiet(self) -> None:
        if self._path(f"{TAKE_PROVENANCE_ROOT}/.pending.json").exists():
            raise RecordingMetadataBusyError("recording projection awaits a metadata commit")

    def get_take_for_projection(self, take_id: str) -> RecordingTake:
        """Read an atomic head and immutable bindings without contending with controls.

        No replay or writes occur. If a journal commit overlaps the read, discovery
        retries; completed older heads cannot replace newer index revisions.
        """
        self._require_projection_quiet()
        try:
            return self._get_take(take_id)
        finally:
            self._require_projection_quiet()

    def iter_take_ids_for_projection(
        self, *, after_take_id: str | None = None, limit: int = 200
    ) -> tuple[str, ...]:
        """Page identities without locking or decoding potentially damaged heads."""
        self._require_projection_quiet()
        try:
            return self._take_ids(after_take_id=after_take_id, limit=limit)
        finally:
            self._require_projection_quiet()

    def iter_takes_for_projection(
        self, *, after_take_id: str | None = None, limit: int = MAX_HISTORY_PAGE_SIZE
    ) -> tuple[RecordingTake, ...]:
        """Bounded background head reads never hold the interactive metadata flock."""
        self._require_projection_quiet()
        try:
            return tuple(
                self._get_take(take_id) for take_id in self._take_ids(after_take_id, limit)
            )
        finally:
            self._require_projection_quiet()

    def recover(self) -> int:
        """Repair only the pending metadata commit; never reopen audio or keep an unsaved draft."""
        with self._metadata_lock():
            self._repair_pending()
            return self._store_head().revision

    @contextmanager
    def recovery_projection_barrier(self) -> Iterator[int]:
        """Bind inventory completion to the current committed revision under the store lock.

        Repairs only the pending operation. Callers may publish their short index
        completion transaction while held; historical enumeration/reads are forbidden.
        """
        with self._metadata_lock():
            self._repair_pending()
            yield self._store_head().revision


RECORDING_TAKE_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "recording-package-publication": RecordingPackagePublication,
    "recording-analysis-publication": RecordingAnalysisPublication,
    "prepared-recording-gate": PreparedRecordingGateEvidence,
    "recording-take-operation": RecordingTakeOperation,
    "pending-recording-operation": PendingRecordingOperation,
    "recording-store-head": RecordingStoreHead,
    "recording-cursor": RecordingCursor,
}

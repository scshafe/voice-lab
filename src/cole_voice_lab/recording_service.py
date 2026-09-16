"""Bounded inherited-pipe control for the persistent Python recording service.

@canonical recording_service -- metadata runs on one bounded executor; cancellation,
EOF and protocol control remain reachable. Native Start/Stop use the recorder pipe
directly. Analysis belongs in its own process, never in this executor.

Durable take/save/index capabilities implement RecordingServiceBackend. The default
backend explicitly reports unknown history and rejects unavailable mutations.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import selectors
import stat
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from .canonical import canonical_json_bytes, canonical_sha256, open_regular_no_follow, sha256_file
from .corpus_store import (
    CorpusSnapshotSecurityError,
    CorpusStoreError,
    corpus_store_migration_resources,
    open_corpus_store,
)
from .diagnostic_roots import require_catalog_root
from .errors import ContractError, SecurityError, VoiceLabError
from .models import PromptSuite, Sha256
from .paths import AssetLayout, _reject_existing_symlink_components
from .recording_contracts import (
    MAX_CATALOG_SUITE_BYTES,
    MAX_CONTROL_FRAME_BYTES,
    MAX_RECORDING_REVISION,
    MAX_TELEMETRY_FRAME_BYTES,
    PROTOCOL_VERSION,
    AcknowledgedEvent,
    AnalysisEvent,
    CancelCommand,
    CatalogCommand,
    CatalogEvent,
    CatalogReference,
    CatalogSnapshot,
    ContextEvent,
    ContinueLegacyRecordingCommand,
    DecisionCommand,
    DecisionEvent,
    DerivativeEvent,
    EditEvent,
    EditTakeCommand,
    ErrorCode,
    HandshakeCommand,
    HandshakeEvent,
    HistoryCommand,
    NavigateCommand,
    NavigationEvent,
    OpenContextCommand,
    PackagePreparationEvent,
    PreparePackageCommand,
    RecorderStateEvent,
    RecordingAnalysisResult,
    RecordingBuildIdentity,
    RecordingError,
    RecordingHistoryPage,
    RecordingMessageMeta,
    RecordingPromptNotCountedError,
    RecordingRecoveryPage,
    RecordingResponse,
    RecordingTake,
    RecordingTelemetry,
    RecordingTransition,
    RecoverCommand,
    RecoveryEvent,
    RecoveryInventoryEvent,
    RecoverySnapshotCommand,
    RegisterSealCommand,
    ReliableEvent,
    ReservationEvent,
    ReserveTakeCommand,
    RetakeCommand,
    ReviewActionEvent,
    ReviewEvent,
    ReviewTakeCommand,
    SaveEvent,
    SaveTakeCommand,
    ServiceRequest,
    ShutdownCommand,
    SnapshotCommand,
    TakeEvent,
    recording_frame_bytes,
)
from .recording_discovery import RecordingDiscoveryCoordinator
from .recording_edits import (
    _legacy_key,
    commit_legacy_edit,
    legacy_action_event,
    legacy_edit_result,
)
from .recording_index import RecordingIndex, RecordingIndexError
from .recording_library import RecordingLibrary
from .recording_recovery import RecordingRecovery, RecordingRecoverySupervisor
from .recording_review import RecordingReviewResolver
from .recording_takes import RecordingSelectionError, RecordingTakeStore

if TYPE_CHECKING:
    from .recording_analysis import RecordingAnalysisQueue
    from .recording_edit_jobs import RecordingEditCoordinator
    from .recording_package_jobs import RecordingPackageQueue

CONTROL_READ_BYTES = 16_384
MAX_PENDING_REQUESTS = 16
MAX_REPLAY_RESPONSES = 128
MAX_REPLAY_BYTES = 8 * MAX_CONTROL_FRAME_BYTES
MAX_OUTPUT_BYTES = 4 * MAX_CONTROL_FRAME_BYTES
CONTROL_POLL_SECONDS = 0.02
# Safety net only: every completed reply and every closure wakes the pipe server through
# its self-pipe, so an idle service sleeps in select instead of polling for replies.
CONTROL_IDLE_WAKE_SECONDS = 1.0
CONTROL_OUTPUT_DEADLINE_SECONDS = 2.0
CONTROL_SHUTDOWN_DEADLINE_SECONDS = 1.0
_SHA256: TypeAdapter[str] = TypeAdapter(Sha256)


class RecordingServiceError(VoiceLabError):
    """A safe protocol/domain failure with no raw payload, transcript or device details."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.recording_error = RecordingError(code=code, message=message, retryable=retryable)


def _snapshot_failure(
    error: CorpusSnapshotSecurityError,
    subject: str = "the catalog snapshot this recording context references",
) -> RecordingServiceError:
    """The wire error for a catalog snapshot that is missing or altered.

    Both store codes surface as ``CORPUS.SNAPSHOT_MISMATCH``; the fixed message names
    ``subject`` and says which, and carries no snapshot content.
    """
    state = "missing" if error.code == "CORPUS.SNAPSHOT_MISSING" else "altered or unsafe"
    return RecordingServiceError("CORPUS.SNAPSHOT_MISMATCH", f"{subject} is {state}")


def _bounded_message(message: str) -> str:
    # RecordingError messages are at most 1024 characters; store messages can name paths.
    return message if len(message) <= 1024 else message[:1021] + "..."


def _active_catalog_failure(error: CorpusStoreError, subject_id: str) -> RecordingServiceError:
    """The wire error for a subject whose active catalog cannot be resolved.

    ``CORPUS.STORE_MISSING`` and ``CORPUS.NO_ACTIVE_CATALOG`` keep their codes and the store's
    message (it names ``voice-lab catalog init`` / ``voice-lab catalog activate``); a missing
    or altered snapshot is ``CORPUS.SNAPSHOT_MISMATCH``; any other store failure (unsafe or
    unavailable store) is ``RECORDING.GATE_NOT_READY`` with the store's message.
    """
    if isinstance(error, CorpusSnapshotSecurityError):
        return _snapshot_failure(error, f"the active catalog snapshot of subject {subject_id}")
    if error.code == "CORPUS.STORE_MISSING":
        return RecordingServiceError("CORPUS.STORE_MISSING", _bounded_message(str(error)))
    if error.code == "CORPUS.NO_ACTIVE_CATALOG":
        return RecordingServiceError("CORPUS.NO_ACTIVE_CATALOG", _bounded_message(str(error)))
    return RecordingServiceError(
        "RECORDING.GATE_NOT_READY",
        _bounded_message(f"the corpus store cannot serve the active catalog: {error}"),
    )


class RecordingFrameDecoder:
    """Incrementally consume bounded network-order frames; malformed streams remain failed."""

    def __init__(self, *, telemetry: bool = False) -> None:
        self.maximum = MAX_TELEMETRY_FRAME_BYTES if telemetry else MAX_CONTROL_FRAME_BYTES
        self._pending = bytearray()
        self._expected: int | None = None
        self._failed = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._pending)

    def _failure(self, message: str) -> RecordingServiceError:
        self._failed = True
        self._pending.clear()
        return RecordingServiceError("RECORDING.INVALID_MESSAGE", message)

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        """Yield complete payloads without retaining the input chunk or an unbounded result list."""
        if self._failed:
            raise self._failure("recording stream has already failed")
        offset = 0
        while offset < len(chunk):
            target = self._expected if self._expected is not None else 4
            copied = min(target - len(self._pending), len(chunk) - offset)
            self._pending.extend(memoryview(chunk)[offset : offset + copied])
            offset += copied
            if len(self._pending) != target:
                continue
            if self._expected is None:
                self._expected = int.from_bytes(self._pending, "big")
                self._pending.clear()
                if not 1 <= self._expected <= self.maximum:
                    raise self._failure("recording frame length is outside its allowed bounds")
            else:
                payload = bytes(self._pending)
                self._pending.clear()
                self._expected = None
                yield payload

    def finish(self) -> None:
        """Accept only EOF between complete frames; partial prefixes/payloads fail closed."""
        if self._failed or self._pending or self._expected is not None:
            raise self._failure("recording control pipe ended inside an incomplete frame")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON object key")
        result[name] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON numbers are forbidden")


def decode_service_request(payload: bytes) -> ServiceRequest:
    """Validate JSON syntax, unique keys, version and strict command identity before dispatch."""
    if not 1 <= len(payload) <= MAX_CONTROL_FRAME_BYTES:
        raise RecordingServiceError("RECORDING.INVALID_MESSAGE", "request exceeds its byte limit")
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_invalid_constant,
        )
        if not isinstance(raw, dict):
            raise ValueError("request must be an object")
        meta = raw.get("meta")
        if isinstance(meta, dict) and meta.get("protocol_version") != PROTOCOL_VERSION:
            raise RecordingServiceError(
                "RECORDING.VERSION_MISMATCH", "unsupported recording protocol"
            )
        return ServiceRequest.model_validate_json(payload)
    except RecordingServiceError:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise RecordingServiceError(
            "RECORDING.INVALID_MESSAGE", "invalid recording request"
        ) from error


class RecordingCancellation:
    """Cooperative cancellation that cannot relabel an operation already entering durable commit."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._committing = False

    def cancel(self) -> bool:
        with self._lock:
            if self._committing:
                return False
            self._event.set()
            return True

    def check(self) -> None:
        if self._event.is_set():
            raise RecordingServiceError("RECORDING.CANCELLED", "recording operation was cancelled")

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)

    def begin_commit(self) -> None:
        """Backend calls immediately before its short authoritative commit; afterwards reconcile."""
        with self._lock:
            self.check()
            self._committing = True


class RecordingServiceBackend(Protocol):
    """Authoritative metadata capability; durable operation-ID replay lives in its evidence store.

    validate_request must check the command's selection/decision/edit identity, not
    reject Save merely because unrelated analysis changed the aggregate index.
    execute must be bounded metadata work; submit independent analysis/export jobs.
    """

    @property
    def state_revision(self) -> int: ...

    def validate_request(self, request: ServiceRequest) -> None: ...

    def execute(
        self, request: ServiceRequest, cancellation: RecordingCancellation
    ) -> ReliableEvent: ...


class UnpreparedRecordingBackend:
    """Honest bridge baseline before durable take/index capabilities are attached."""

    def __init__(self, catalog_sha256: str) -> None:
        # Validate using the actual paginated contract, never a second digest parser.
        self._snapshot = RecordingHistoryPage(
            catalog_sha256=catalog_sha256,
            index_revision=0,
            completeness="unknown",
            has_more=False,
        )

    @property
    def state_revision(self) -> int:
        return 0

    def validate_request(self, request: ServiceRequest) -> None:
        if request.meta.state_revision != self.state_revision:
            raise RecordingServiceError("RECORDING.STALE_REVISION", "metadata revision changed")

    def execute(
        self, request: ServiceRequest, cancellation: RecordingCancellation
    ) -> ReliableEvent:
        cancellation.check()
        if isinstance(request.data, (SnapshotCommand, HistoryCommand)):
            if isinstance(request.data, HistoryCommand):
                if request.data.catalog_sha256 != self._snapshot.catalog_sha256:
                    raise RecordingServiceError(
                        "RECORDING.HISTORY_UNKNOWN", "catalog history is unavailable"
                    )
                if request.data.cursor is not None:
                    raise RecordingServiceError(
                        "RECORDING.HISTORY_UNKNOWN", "history index is not ready"
                    )
            return self._snapshot
        raise RecordingServiceError(
            "RECORDING.GATE_NOT_READY",
            "requested recording capability is not prepared",
        )


class RecordingTakesBackend:
    """Durable reservation/draft/Save capability; indexed history attaches separately."""

    def __init__(
        self,
        store: RecordingTakeStore,
        group_id: str,
        *,
        analysis_queue: RecordingAnalysisQueue | None = None,
        recovery_index: RecordingIndex | None = None,
    ) -> None:
        self.store = store
        self.group = store.get_group(group_id)
        self.analysis_queue = analysis_queue
        self.recovery_index = recovery_index
        self.package_queue: RecordingPackageQueue | None = None

    @property
    def state_revision(self) -> int:
        return self.store.state_revision

    def validate_request(self, request: ServiceRequest) -> None:
        # Each mutation checks its exact durable operation, take, decision and edit.
        # Aggregate revisions also change for unrelated takes/QC and cannot stale Save.
        if isinstance(request.data, ReserveTakeCommand) and (
            request.data.group_id != self.group.group_id
            or request.data.selection.catalog_sha256 != self.group.catalog_sha256
        ):
            raise RecordingServiceError("RECORDING.STALE_REVISION", "capture context changed")

    def execute(
        self, request: ServiceRequest, cancellation: RecordingCancellation
    ) -> ReliableEvent:
        cancellation.check()
        command = request.data
        try:
            if isinstance(command, PreparePackageCommand):
                if self.package_queue is None:
                    raise RecordingServiceError(
                        "RECORDING.GATE_NOT_READY", "package preparation is unavailable"
                    )
                from .recording_packages import RecordingPackageError

                cancellation.begin_commit()
                try:
                    return self.package_queue.submit(command)
                except RecordingPackageError as error:
                    return PackagePreparationEvent(
                        operation_id=command.operation_id,
                        group_id=command.group_id,
                        state="blocked",
                        error=error.recording_error,
                    )
            if isinstance(command, ReserveTakeCommand):
                reservation = self.store.reserve(
                    operation_id=command.operation_id,
                    group_id=command.group_id,
                    prompt_id=command.selection.prompt_id,
                    process_generation=command.recorder_generation,
                    selection=command.selection,
                    commit_barrier=cancellation.begin_commit,
                    current_session_reservation=self.recovery_index is not None,
                )
                return ReservationEvent(
                    take=self.store.reservation_result(command.operation_id),
                    reservation_sha256=canonical_sha256(reservation),
                    cursor=self.store.operation_cursor(command.operation_id),
                )
            if isinstance(command, RegisterSealCommand):
                take = self.store.register_seal(
                    operation_id=command.operation_id,
                    receipt=command.receipt,
                    commit_barrier=cancellation.begin_commit,
                )
                if self.analysis_queue is not None:
                    self.analysis_queue.submit(take.reservation.take_id)
                return TakeEvent(take=take)
            if isinstance(command, SaveTakeCommand):
                saved = self.store.save(command, commit_barrier=cancellation.begin_commit)
                if self.analysis_queue is not None:
                    self.analysis_queue.submit(saved.take.reservation.take_id)
                return saved
            if isinstance(command, RecoverCommand):
                recovery = RecordingRecovery(
                    self.store,
                    index=self.recovery_index,
                    subject_id=self.group.subject_id,
                    submit_analysis=None
                    if self.analysis_queue is None
                    else self.analysis_queue.submit,
                )
                page = (
                    RecordingRecoveryPage(
                        entries=(
                            recovery.recover_take(
                                command.take_id, commit_barrier=cancellation.begin_commit
                            ),
                        )
                    )
                    if command.take_id is not None
                    else recovery.page(
                        after_take_id=command.after_take_id,
                        limit=command.page_size,
                        commit_barrier=cancellation.begin_commit,
                    )
                )
                return RecoveryEvent(
                    operation_id=command.operation_id, take_id=command.take_id, page=page
                )
            if isinstance(command, (SnapshotCommand, HistoryCommand)):
                if isinstance(command, HistoryCommand) and (
                    command.catalog_sha256 != self.group.catalog_sha256
                    or command.cursor is not None
                ):
                    raise RecordingServiceError(
                        "RECORDING.HISTORY_UNKNOWN", "catalog index is not prepared"
                    )
                return RecordingHistoryPage(
                    catalog_sha256=self.group.catalog_sha256,
                    index_revision=self.state_revision,
                    completeness="unknown",
                    has_more=False,
                )
        except CorpusSnapshotSecurityError as error:
            raise _snapshot_failure(error) from error
        except SecurityError as error:
            raise RecordingServiceError(
                "RECORDING.SOURCE_CHANGED", "capture evidence changed"
            ) from error
        except ContractError as error:
            raise RecordingServiceError("RECORDING.INVALID_TRANSITION", str(error)) from error
        except OSError as error:
            raise RecordingServiceError(
                "RECORDING.WRITE_FAILED", "recording metadata could not be synced", retryable=True
            ) from error
        raise RecordingServiceError(
            "RECORDING.GATE_NOT_READY", "requested recording capability is not prepared"
        )


@dataclass(frozen=True, slots=True)
class PinnedCatalog:
    """The subject's active catalog as this service process resolved it at first use."""

    snapshot: CatalogSnapshot
    reference: CatalogReference

    @property
    def suite(self) -> PromptSuite:
        return self.snapshot.suite


class RecordingWorkspaceBackend:
    """Bootstrapped native context, indexed library and durable queue behind one service.

    The subject's catalog comes from the corpus store: the first ``CatalogCommand``,
    open-context or pre-context snapshot that needs it resolves the subject's active catalog
    (the store is opened, read and closed again, so the CLI can edit it while the app runs)
    and pins it for the process lifetime; a later ``voice-lab catalog activate`` takes effect
    at the next launch. Only a successful resolution is pinned.
    """

    def __init__(
        self,
        layout: AssetLayout,
        subject_id: str,
        catalog_sha256: str | None = None,
        *,
        store: RecordingTakeStore | None = None,
        index: RecordingIndex | None = None,
        discover: bool = True,
        analysis_queue: RecordingAnalysisQueue | None = None,
    ) -> None:
        """Params: ``catalog_sha256`` is an explicit digest for the pre-context snapshot
        (``--catalog-sha256``); ``None`` reports the pinned catalog's digest instead.
        Raises: ``RecordingIndexError``, ``SecurityError`` or ``OSError`` when the index or
        take store cannot be opened or recovered.
        """
        self.layout = layout
        self.subject_id = subject_id
        self.initial_catalog_sha256 = catalog_sha256
        self._catalog_lock = threading.Lock()
        self._pinned_catalog: PinnedCatalog | None = None
        self.analysis_queue = analysis_queue
        self.package_queue: RecordingPackageQueue | None = None
        self.index = index or RecordingIndex(layout, subject_id=subject_id)
        self.index.begin_recovery_scan(scan_generation="recovery-" + uuid4().hex)
        self.store = store or RecordingTakeStore(
            layout,
            projection_hook=self.index.update_take,
            index_revision_resolver=lambda: self.index.revision,
        )
        self.store.projection_hook = self.index.update_take
        self.store.reservation_projection_hook = lambda take: self.index.update_take(
            take, current_session_reservation=True
        )
        self.store.index_revision_resolver = lambda: self.index.revision
        self.store.recover()
        self.review_resolver = RecordingReviewResolver(layout, self.store, self.index)
        self.edit_coordinator: RecordingEditCoordinator | None = None
        self.library: RecordingLibrary | None = None
        self._event_publisher: Callable[[str, ReliableEvent], None] | None = None
        self.discovery = (
            RecordingDiscoveryCoordinator(
                layout,
                index=self.index,
                store=self.store,
                completed=self._discovery_completed,
                progress=self._discovery_completed,
            )
            if discover
            else None
        )
        cursor = self.store.get_cursor(subject_id)
        if cursor is not None and self.discovery is not None:
            self.discovery.schedule(cursor.group_id)

    def set_event_publisher(self, publisher: Callable[[str, ReliableEvent], None]) -> None:
        self._event_publisher = publisher

    def _discovery_completed(self, group_id: str) -> None:
        publisher = self._event_publisher
        if publisher is None:
            return
        cursor = self.store.get_cursor(self.subject_id)
        if cursor is None or cursor.group_id != group_id:
            return
        page = self.index.history(cursor.selection.catalog_sha256)
        publisher("history-" + canonical_sha256([group_id, page.index_revision]), page)

    @property
    def state_revision(self) -> int:
        return self.store.state_revision

    def pinned_catalog(self) -> PinnedCatalog:
        """The subject's active catalog for this process, resolved on first use and pinned.

        Opens the corpus store read-only in effect (``create=False``), reads the active
        version and its snapshot (aliases included) and closes the store before returning.
        Returns: the pinned snapshot and reference.
        Raises: ``RecordingServiceError`` with ``CORPUS.STORE_MISSING`` (no store; the message
        names ``voice-lab catalog init``), ``CORPUS.NO_ACTIVE_CATALOG`` (the message names
        ``voice-lab catalog init`` / ``voice-lab catalog activate``),
        ``CORPUS.SNAPSHOT_MISMATCH`` (the active snapshot is missing or altered) or
        ``RECORDING.GATE_NOT_READY`` (any other store failure, with the store's message, or
        an active suite larger than ``MAX_CATALOG_SUITE_BYTES``, which no CatalogEvent frame
        can carry). A failure pins nothing, so a later request resolves again.
        """
        with self._catalog_lock:
            if self._pinned_catalog is None:
                try:
                    with open_corpus_store(self.layout, create=False) as corpus:
                        _, reference = corpus.active_catalog(self.subject_id)
                        snapshot = corpus.materialize(reference)
                except CorpusStoreError as error:
                    raise _active_catalog_failure(error, self.subject_id) from error
                size = len(canonical_json_bytes(snapshot.suite))
                if size > MAX_CATALOG_SUITE_BYTES:
                    # Refused here, by name: an oversized reply would close the whole session.
                    raise RecordingServiceError(
                        "RECORDING.GATE_NOT_READY",
                        f"active catalog {reference.catalog_id} version "
                        f"{reference.catalog_version} is {size} bytes with "
                        f"{len(snapshot.suite.prompts)} prompts, more than the "
                        f"{MAX_CATALOG_SUITE_BYTES} bytes the app receives in one message; "
                        "publish a smaller version and run `voice-lab catalog activate`",
                    )
                self._pinned_catalog = PinnedCatalog(snapshot=snapshot, reference=reference)
            return self._pinned_catalog

    def _served_catalog(self) -> PinnedCatalog:
        """The pinned catalog, refused on a root that does not allow its purpose.

        Its content (CatalogCommand) and identity (the pre-context snapshot's digest) are only
        shown where open-context could open it.
        Raises: the ``pinned_catalog`` errors and the ``_require_catalog_root`` refusal.
        """
        pinned = self.pinned_catalog()
        self._require_catalog_root(pinned.suite)
        return pinned

    def _context_catalog(self, command: OpenContextCommand) -> CatalogSnapshot:
        """The pinned catalog a top-level open-context must name, as its snapshot.

        Params: ``command`` is a top-level (not legacy-continuation) open-context.
        Returns: the pinned snapshot, whose reference equals ``command.catalog_reference``.
        Raises: ``RecordingServiceError`` ``RECORDING.INVALID_MESSAGE`` for an embedded
        catalog (accepted only inside continue-legacy-recording),
        ``RECORDING.STALE_REVISION`` when the reference differs from the pinned one, and the
        ``pinned_catalog`` errors when no active catalog can be resolved.
        """
        named = command.catalog_reference
        if named is None:
            raise RecordingServiceError(
                "RECORDING.INVALID_MESSAGE",
                "open-context must name the catalog this service delivered "
                "(catalog_reference); an embedded catalog is accepted only inside "
                "continue-legacy-recording",
            )
        pinned = self.pinned_catalog()
        if named != pinned.reference:
            served = pinned.reference
            raise RecordingServiceError(
                "RECORDING.STALE_REVISION",
                f"open-context names catalog {named.catalog_id} version "
                f"{named.catalog_version} ({named.catalog_sha256[:12]}), but this service "
                f"serves {served.catalog_id} version {served.catalog_version} "
                f"({served.catalog_sha256[:12]}); request the catalog again",
            )
        return pinned.snapshot

    def _require_catalog_root(self, suite: PromptSuite) -> None:
        """Refuse a catalog whose purpose this root does not allow, before any evidence is read.

        Raises: ``RecordingServiceError`` ``RECORDING.GATE_NOT_READY`` with the
        ``require_catalog_root`` refusal text, or naming an unsafe diagnostic root marker.
        """
        try:
            require_catalog_root(self.layout, suite, self.subject_id)
        except ContractError as error:
            raise RecordingServiceError("RECORDING.GATE_NOT_READY", str(error)) from error
        except SecurityError as error:
            raise RecordingServiceError(
                "RECORDING.GATE_NOT_READY",
                "the diagnostic root marker is unsafe; this asset root is refused",
            ) from error

    def validate_request(self, request: ServiceRequest) -> None:
        context = (
            request.data.context
            if isinstance(request.data, ContinueLegacyRecordingCommand)
            else request.data
        )
        if isinstance(context, OpenContextCommand) and context.subject_id != self.subject_id:
            raise RecordingServiceError("RECORDING.STALE_REVISION", "workspace subject changed")

    def execute(
        self, request: ServiceRequest, cancellation: RecordingCancellation
    ) -> ReliableEvent:
        cancellation.check()
        command = request.data
        try:
            if isinstance(command, RecoverySnapshotCommand):
                return RecoveryInventoryEvent(
                    page=self.index.recovery_snapshot(
                        cursor=command.cursor, page_size=command.page_size
                    )
                )
            if isinstance(command, PreparePackageCommand):
                if self.package_queue is None:
                    raise RecordingServiceError(
                        "RECORDING.GATE_NOT_READY", "package preparation is unavailable"
                    )
                from .recording_packages import RecordingPackageError

                cancellation.begin_commit()
                try:
                    return self.package_queue.submit(command)
                except RecordingPackageError as error:
                    return PackagePreparationEvent(
                        operation_id=command.operation_id,
                        group_id=command.group_id,
                        state="blocked",
                        error=error.recording_error,
                    )
            if isinstance(command, CatalogCommand):
                # A catalog this root refuses is never shown, exactly as open-context refuses it.
                pinned = self._served_catalog()
                return CatalogEvent(
                    operation_id=command.operation_id,
                    catalog=pinned.suite,
                    reference=pinned.reference,
                )
            if isinstance(command, (OpenContextCommand, ContinueLegacyRecordingCommand)):
                if isinstance(command, ContinueLegacyRecordingCommand):
                    # Legacy continuation binds the session's frozen embedded catalog.
                    context = command.context
                    catalog = CatalogSnapshot.embedded(command.legacy_catalog)
                else:
                    context = command
                    catalog = self._context_catalog(command)
                # Refuse before replay or gate preparation can read or bind any evidence.
                self._require_catalog_root(catalog.suite)
                replay = self.store.context_replay(context)
                if replay is not None and not isinstance(command, ContinueLegacyRecordingCommand):
                    return replay
                gate = None
                if (
                    context.pilot_approval_path is not None
                    and context.pilot_approval_sha256 is not None
                ):
                    gate = self.store.prepare_corpus_gate(
                        catalog.suite,
                        self.subject_id,
                        approval_path=context.pilot_approval_path,
                        approval_sha256=context.pilot_approval_sha256,
                    )
                cancellation.check()
                self.index.register_catalog(catalog)
                library = RecordingLibrary(self.layout, catalog, self.subject_id)
                library.bind_native_store(self.store, self.index)
                if isinstance(command, ContinueLegacyRecordingCommand):
                    event = RecordingRecovery(
                        self.store, subject_id=self.subject_id
                    ).continue_legacy(
                        command, prepared_gate=gate, commit_barrier=cancellation.begin_commit
                    )
                else:
                    event = self.store.open_context(
                        context, prepared_gate=gate, commit_barrier=cancellation.begin_commit
                    )
                if self.discovery is not None:
                    self.discovery.schedule(event.group.group_id)
                return event
            cursor = self.store.get_cursor(self.subject_id)
            if cursor is None:
                if isinstance(command, SnapshotCommand):
                    # An explicit launch digest names itself; otherwise the page reports the
                    # pinned catalog's identity, which a root that refuses it never shows.
                    return RecordingHistoryPage(
                        catalog_sha256=self.initial_catalog_sha256
                        if self.initial_catalog_sha256 is not None
                        else self._served_catalog().reference.catalog_sha256,
                        index_revision=self.index.revision,
                        completeness="unknown",
                        has_more=False,
                    )
                raise RecordingServiceError(
                    "RECORDING.GATE_NOT_READY", "recording context is not open"
                )
            group = self.store.get_group(cursor.group_id)
            if self.discovery is not None and isinstance(
                command, (SnapshotCommand, HistoryCommand)
            ):
                self.discovery.schedule(group.group_id)
            if self.library is None or self.library.catalog_sha256 != group.catalog_sha256:
                catalog = self.store.group_catalog(group)
                self.index.register_catalog(catalog)
                self.library = RecordingLibrary(self.layout, catalog, self.subject_id)
                self.library.bind_native_store(self.store, self.index)
            library = self.library
            if isinstance(command, ReviewTakeCommand):
                self.store._check_selection(self.subject_id, command.selection)
                source = self.review_resolver.resolve(group.catalog_sha256, command.take_id)
                return self.store.select_review(
                    command, source, commit_barrier=cancellation.begin_commit
                )
            if isinstance(command, RetakeCommand):
                return self.store.retake(
                    command, subject_id=self.subject_id, commit_barrier=cancellation.begin_commit
                )
            if isinstance(
                command, (EditTakeCommand, DecisionCommand)
            ) and not command.take_id.startswith("take-"):
                self.store._check_selection(self.subject_id, command.selection)
                source = self.review_resolver.resolve(
                    group.catalog_sha256, command.take_id, require_verified=True
                )
                entry, _ = self.index.review_aliases(group.catalog_sha256, command.take_id)
                with self.store._metadata_lock():
                    self.store._repair_pending()
                    self.store._check_selection(self.subject_id, command.selection)
                    intent = commit_legacy_edit(
                        self.layout,
                        request=command,
                        source=source,
                        cursor=cursor,
                        entry=entry,
                        committed_at=self.store.clock(),
                        commit_barrier=cancellation.begin_commit,
                    )
                    self.index.update_legacy_review(intent.projection)
                if self.edit_coordinator is not None:
                    self.edit_coordinator.submit("legacy:" + _legacy_key(intent))
                return legacy_action_event(intent, legacy_edit_result(self.layout, intent))
            if isinstance(command, EditTakeCommand):
                edited = self.store.edit(command, commit_barrier=cancellation.begin_commit)
                if self.analysis_queue is not None:
                    self.analysis_queue.submit(command.take_id)
                if self.edit_coordinator is not None:
                    self.edit_coordinator.submit(command.take_id)
                return edited
            if isinstance(command, DecisionCommand):
                return self.store.decide(command, commit_barrier=cancellation.begin_commit)
            if isinstance(command, NavigateCommand):
                return library.navigate_native_recording(
                    self.store, command, commit_barrier=cancellation.begin_commit
                )
            if isinstance(command, SaveTakeCommand):
                saved = library.save_native_take(
                    self.store, command, commit_barrier=cancellation.begin_commit
                )
                if self.analysis_queue is not None:
                    self.analysis_queue.submit(saved.take.reservation.take_id)
                if self.edit_coordinator is not None and saved.take.edit_revision > 0:
                    self.edit_coordinator.submit(saved.take.reservation.take_id)
                return saved
            if isinstance(command, SnapshotCommand):
                return ContextEvent(
                    group=group, cursor=cursor, history=library.indexed_history(self.index)
                )
            if isinstance(command, HistoryCommand):
                if command.catalog_sha256 != group.catalog_sha256:
                    raise RecordingServiceError(
                        "RECORDING.HISTORY_UNKNOWN",
                        "requested catalog differs from the open context",
                    )
                return library.indexed_history(
                    self.index,
                    prompt_id=command.prompt_id,
                    cursor=command.cursor,
                    prompt_cursor=command.prompt_cursor,
                    group_cursor=command.group_cursor,
                    page_size=command.page_size,
                )
            backend = RecordingTakesBackend(
                self.store,
                group.group_id,
                analysis_queue=self.analysis_queue,
                recovery_index=self.index,
            )
            backend.validate_request(request)
            return backend.execute(request, cancellation)
        except RecordingIndexError as error:
            raise RecordingServiceError(
                error.recording_error.code, error.recording_error.message
            ) from error
        except (RecordingSelectionError, RecordingPromptNotCountedError) as error:
            # A take whose prompt changed without keeping recordings is stale history, not
            # changed evidence: the same code for native and legacy takes.
            raise RecordingServiceError("RECORDING.STALE_REVISION", str(error)) from error
        except CorpusSnapshotSecurityError as error:
            raise _snapshot_failure(error) from error
        except SecurityError as error:
            raise RecordingServiceError(
                "RECORDING.SOURCE_CHANGED", "workspace evidence changed"
            ) from error
        except ContractError as error:
            raise RecordingServiceError("RECORDING.INVALID_TRANSITION", str(error)) from error
        except OSError as error:
            raise RecordingServiceError(
                "RECORDING.WRITE_FAILED", "workspace metadata could not be synced", retryable=True
            ) from error


@dataclass(frozen=True, slots=True)
class _PendingRequest:
    request: ServiceRequest
    digest: str
    cancellation: RecordingCancellation


def _operation_id(request: ServiceRequest) -> str | None:
    value = getattr(request.data, "operation_id", None)
    return value if isinstance(value, str) else None


def _event_take_id(event: ReliableEvent) -> str | None:
    if isinstance(event, (RecorderStateEvent, RecordingTransition, RecoveryEvent)):
        return event.take_id
    if isinstance(event, (ReservationEvent, TakeEvent, SaveEvent, EditEvent, DecisionEvent)):
        return event.take.reservation.take_id
    if isinstance(event, DerivativeEvent):
        return event.take_id
    if isinstance(event, (ReviewEvent, ReviewActionEvent)):
        return event.source.take_id
    if isinstance(event, ContextEvent):
        return event.cursor.selection.take_id
    if isinstance(event, NavigationEvent) and event.cursor is not None:
        return event.cursor.selection.take_id
    if isinstance(event, AnalysisEvent):
        return event.result.take_id
    return None


class RecordingServiceSession:
    """Thread-safe protocol session with one bounded metadata queue and independent controls."""

    def __init__(
        self,
        *,
        build: RecordingBuildIdentity,
        expected_client: RecordingBuildIdentity,
        process_generation: str,
        backend: RecordingServiceBackend,
    ) -> None:
        self.build = build
        self.expected_client = expected_client
        self.generation = RecordingMessageMeta(
            request_id="startup",
            process_generation=process_generation,
            state_revision=0,
            event_sequence=0,
        ).process_generation
        self.backend = backend
        self._lock = threading.RLock()
        self._handshaken = False
        self._closed = threading.Event()
        self._sequence = 0
        self._pending: dict[str, _PendingRequest] = {}
        self._queue: queue.Queue[_PendingRequest] = queue.Queue(MAX_PENDING_REQUESTS)
        self._preparation_queue: queue.Queue[_PendingRequest] = queue.Queue(MAX_PENDING_REQUESTS)
        self._completed: queue.Queue[tuple[RecordingResponse, int]] = queue.Queue(
            MAX_PENDING_REQUESTS + MAX_REPLAY_RESPONSES
        )
        self._completed_bytes = 0
        self.terminal_error: RecordingServiceError | None = None
        self._replay: OrderedDict[str, tuple[str, RecordingResponse, int]] = OrderedDict()
        self._replay_bytes = 0
        self._control_wakeup: Callable[[], None] | None = None
        self._worker = threading.Thread(
            target=self._metadata_loop, daemon=True, name="recording-service-metadata"
        )
        self._worker.start()
        self._preparation_worker = threading.Thread(
            target=self._metadata_loop,
            args=(self._preparation_queue,),
            daemon=True,
            name="recording-service-context-preparation",
        )
        self._preparation_worker.start()
        if isinstance(backend, RecordingWorkspaceBackend):
            backend.set_event_publisher(self.publish_event)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def has_responses(self) -> bool:
        return not self._completed.empty()

    def attach_control_wakeup(self, wakeup: Callable[[], None] | None) -> None:
        """Let the pipe server learn at once about a completed reply or a closure.

        Every publication runs under the session lock, so detaching here (also under
        it) guarantees no later reply touches a descriptor the server has closed.
        """
        with self._lock:
            self._control_wakeup = wakeup

    def _wake_control(self) -> None:
        # Called under self._lock by every path that completes a reply or closes.
        if self._control_wakeup is not None:
            self._control_wakeup()

    def _publish_response(self, response: RecordingResponse) -> None:
        size = len(recording_frame_bytes(response))
        if self._completed.full() or self._completed_bytes + size > MAX_OUTPUT_BYTES:
            self.terminal_error = RecordingServiceError(
                "RECORDING.PARENT_LOST", "recording reply backlog exceeded its bound"
            )
            self._closed.set()
            self._cancel_all()
            self._wake_control()
            raise self.terminal_error
        self._completed.put_nowait((response, size))
        self._completed_bytes += size
        self._wake_control()

    def _response(
        self,
        request: ServiceRequest,
        *,
        event: ReliableEvent | None = None,
        error: RecordingError | None = None,
    ) -> RecordingResponse:
        with self._lock:
            if self._sequence >= MAX_RECORDING_REVISION:
                raise RecordingServiceError("RECORDING.INVALID_MESSAGE", "event sequence exhausted")
            self._sequence += 1
            response = RecordingResponse(
                data=event,
                error=error,
                meta=RecordingMessageMeta(
                    request_id=request.meta.request_id,
                    process_generation=self.generation,
                    state_revision=self.backend.state_revision,
                    event_sequence=self._sequence,
                    take_id=_event_take_id(event) if event is not None else request.meta.take_id,
                ),
            )
            self._publish_response(response)
            return response

    def publish_event(self, request_id: str, event: ReliableEvent) -> None:
        """Reliable unsolicited update shares the same bounded ordered response lane."""
        self._publish_background(request_id, event=event)

    def publish_error(self, request_id: str, error: RecordingError) -> None:
        self._publish_background(request_id, error=error)

    def _publish_background(
        self,
        request_id: str,
        *,
        event: ReliableEvent | None = None,
        error: RecordingError | None = None,
    ) -> None:
        with self._lock:
            if self.closed or not self._handshaken:
                return  # Evidence is durable; bootstrap/snapshot recovers pre-handshake state.
            if self._sequence >= MAX_RECORDING_REVISION:
                raise RecordingServiceError("RECORDING.INVALID_MESSAGE", "event sequence exhausted")
            self._sequence += 1
            self._publish_response(
                RecordingResponse(
                    data=event,
                    error=error,
                    meta=RecordingMessageMeta(
                        request_id=request_id,
                        process_generation=self.generation,
                        state_revision=self.backend.state_revision,
                        event_sequence=self._sequence,
                        take_id=None if event is None else _event_take_id(event),
                    ),
                )
            )

    def publish_analysis(self, operation_id: str, result: RecordingAnalysisResult) -> None:
        self.publish_event(operation_id, AnalysisEvent(result=result))

    def _cache(self, request_id: str, digest: str, response: RecordingResponse) -> None:
        size = len(recording_frame_bytes(response))
        previous = self._replay.pop(request_id, None)
        if previous is not None:
            self._replay_bytes -= previous[2]
        self._replay[request_id] = (digest, response, size)
        self._replay_bytes += size
        while len(self._replay) > MAX_REPLAY_RESPONSES or self._replay_bytes > MAX_REPLAY_BYTES:
            _, (_, _, dropped_size) = self._replay.popitem(last=False)
            self._replay_bytes -= dropped_size

    def submit(self, request: ServiceRequest) -> RecordingResponse | None:
        """Return immediate control/replay response, or enqueue metadata exactly once.

        Repeated pending requests coalesce. Process-local response replay is an
        optimization; backend immutable operation records own lost-reply durability.
        """
        digest = canonical_sha256(
            {
                "data": request.data.model_dump(mode="json"),
                "state_revision": request.meta.state_revision,
            }
        )
        with self._lock:
            try:
                if self.closed:
                    raise RecordingServiceError(
                        "RECORDING.PARENT_LOST", "recording service is closing"
                    )
                if request.meta.process_generation != self.generation:
                    raise RecordingServiceError(
                        "RECORDING.STALE_GENERATION", "recording process generation changed"
                    )
                if request.meta.event_sequence > self._sequence:
                    raise RecordingServiceError(
                        "RECORDING.INVALID_MESSAGE", "request acknowledges a future event"
                    )
                cached = self._replay.get(request.meta.request_id)
                if cached is not None:
                    if cached[0] != digest:
                        raise RecordingServiceError(
                            "RECORDING.INVALID_MESSAGE", "request ID was reused with different data"
                        )
                    self._publish_response(cached[1])
                    return cached[1]
                pending = self._pending.get(request.meta.request_id)
                if pending is not None:
                    if pending.digest != digest:
                        raise RecordingServiceError(
                            "RECORDING.INVALID_MESSAGE", "pending request ID was reused"
                        )
                    return None
                if isinstance(request.data, HandshakeCommand):
                    response = self._handshake(request)
                elif not self._handshaken:
                    raise RecordingServiceError(
                        "RECORDING.VERSION_MISMATCH", "recording handshake is required"
                    )
                elif isinstance(request.data, ShutdownCommand):
                    self._cancel_all()
                    self._closed.set()
                    response = self._response(
                        request,
                        event=AcknowledgedEvent(
                            operation_id=request.meta.request_id,
                            action="shutdown",
                        ),
                    )
                elif isinstance(request.data, CancelCommand):
                    response = self._cancel(request)
                else:
                    if len(self._pending) >= MAX_PENDING_REQUESTS:
                        raise RecordingServiceError(
                            "RECORDING.INVALID_TRANSITION",
                            "recording metadata queue is full",
                            retryable=True,
                        )
                    work = _PendingRequest(request, digest, RecordingCancellation())
                    self._pending[request.meta.request_id] = work
                    target_queue = (
                        self._preparation_queue
                        if isinstance(
                            request.data,
                            (
                                OpenContextCommand,
                                ReviewTakeCommand,
                                ContinueLegacyRecordingCommand,
                                RecoverCommand,
                            ),
                        )
                        else self._queue
                    )
                    target_queue.put_nowait(work)
                    return None
            except RecordingServiceError as failure:
                if self.terminal_error is not None:
                    raise self.terminal_error from failure
                return self._response(request, error=failure.recording_error)
            self._cache(request.meta.request_id, digest, response)
            return response

    def _handshake(self, request: ServiceRequest) -> RecordingResponse:
        command = request.data
        assert isinstance(command, HandshakeCommand)
        if command.role != "ui":
            raise RecordingServiceError(
                "RECORDING.INVALID_MESSAGE", "service requires a UI control peer"
            )
        if command.build != self.expected_client or command.expected_peer_build != self.build:
            raise RecordingServiceError(
                "RECORDING.BUILD_MISMATCH", "recording executable identities do not match"
            )
        self._handshaken = True
        return self._response(request, event=HandshakeEvent(role="service", build=self.build))

    def _cancel(self, request: ServiceRequest) -> RecordingResponse:
        command = request.data
        assert isinstance(command, CancelCommand)
        matches = [
            work
            for work in self._pending.values()
            if _operation_id(work.request) == command.cancelled_operation_id
        ]
        if not matches or not all(work.cancellation.cancel() for work in matches):
            raise RecordingServiceError(
                "RECORDING.INVALID_TRANSITION",
                "operation is not cancellable; reconcile its durable result",
            )
        return self._response(
            request,
            event=AcknowledgedEvent(
                operation_id=command.operation_id,
                action="cancelled",
            ),
        )

    def _cancel_all(self) -> None:
        for work in self._pending.values():
            work.cancellation.cancel()

    def _metadata_loop(self, work_queue: queue.Queue[_PendingRequest] | None = None) -> None:
        work_queue = self._queue if work_queue is None else work_queue
        while not self.closed or not work_queue.empty():
            try:
                work = work_queue.get(timeout=CONTROL_POLL_SECONDS)
            except queue.Empty:
                continue
            event: ReliableEvent | None = None
            operation_error: RecordingError | None = None
            try:
                work.cancellation.check()
                self.backend.validate_request(work.request)
                event = self.backend.execute(work.request, work.cancellation)
            except RecordingServiceError as failure:
                operation_error = failure.recording_error
            except Exception:
                # Preserve a named per-request failure; never dump private command input.
                operation_error = RecordingError(
                    code="RECORDING.WRITE_FAILED",
                    message="recording metadata operation failed",
                    retryable=False,
                )
            with self._lock:
                self._pending.pop(work.request.meta.request_id, None)
                try:
                    response = self._response(work.request, event=event, error=operation_error)
                except (RecordingServiceError, ValidationError, ValueError) as failure:
                    if self.terminal_error is None:
                        self.terminal_error = RecordingServiceError(
                            "RECORDING.INVALID_MESSAGE",
                            "recording backend produced an invalid response",
                        )
                        self.terminal_error.__cause__ = failure
                    self._closed.set()
                    self._cancel_all()
                    self._wake_control()
                    work_queue.task_done()
                    return
                self._cache(work.request.meta.request_id, work.digest, response)
            work_queue.task_done()

    def completed(self) -> Iterator[RecordingResponse]:
        """Drain reliable responses in sequence, including independent controls and replay."""
        while True:
            with self._lock:
                try:
                    response, size = self._completed.get_nowait()
                except queue.Empty:
                    return
                self._completed_bytes -= size
            yield response

    def close(self) -> None:
        """Cancel pre-commit work on parent loss and bound metadata teardown."""
        with self._lock:
            self._closed.set()
            self._cancel_all()
            self._wake_control()
        self._worker.join(CONTROL_SHUTDOWN_DEADLINE_SECONDS)
        self._preparation_worker.join(CONTROL_SHUTDOWN_DEADLINE_SECONDS)
        if self._worker.is_alive() or self._preparation_worker.is_alive():
            raise RecordingServiceError(
                "RECORDING.PARENT_LOST", "recording metadata did not stop within its deadline"
            )


class RecordingResponseTracker:
    """Detect stale generations and gaps; explicit snapshots reestablish sequence."""

    def __init__(self, process_generation: str) -> None:
        self.generation = process_generation
        self.sequence = 0
        self.state_revision = 0
        self.needs_snapshot = False
        self._last_digest: str | None = None

    def accept(self, response: RecordingResponse, *, snapshot: bool = False) -> bool:
        if response.meta.process_generation != self.generation:
            raise RecordingServiceError(
                "RECORDING.STALE_GENERATION", "response belongs to a retired process"
            )
        digest = canonical_sha256(response)
        if response.meta.event_sequence == self.sequence and digest == self._last_digest:
            return False
        if response.meta.event_sequence <= self.sequence:
            return False
        if response.meta.state_revision < self.state_revision:
            self.needs_snapshot = True
            raise RecordingServiceError(
                "RECORDING.STALE_REVISION", "response metadata revision moved backwards"
            )
        if not snapshot and (
            self.needs_snapshot or response.meta.event_sequence != self.sequence + 1
        ):
            self.needs_snapshot = True
            raise RecordingServiceError(
                "RECORDING.INVALID_MESSAGE", "reliable event gap requires a current snapshot"
            )
        if snapshot and not isinstance(
            response.data, (RecordingHistoryPage, RecorderStateEvent, ContextEvent)
        ):
            raise RecordingServiceError(
                "RECORDING.INVALID_MESSAGE", "resynchronization requires a state snapshot"
            )
        self.sequence = response.meta.event_sequence
        self.state_revision = response.meta.state_revision
        self._last_digest = digest
        self.needs_snapshot = False
        return True


class CoalescingRecordingTelemetry:
    """One visual slot coalesces old snapshots without blocking reliable control."""

    def __init__(self, *, take_id: str, process_generation: str) -> None:
        self._lock = threading.Lock()
        self._latest: RecordingTelemetry | None = None
        self._identity = RecordingMessageMeta(
            request_id="telemetry",
            process_generation=process_generation,
            take_id=take_id,
            state_revision=0,
            event_sequence=0,
        )
        self._sequence = -1
        self._written_frames = 0

    def publish(self, telemetry: RecordingTelemetry) -> bool:
        recording_frame_bytes(telemetry, telemetry=True)
        with self._lock:
            if (
                telemetry.meta.take_id != self._identity.take_id
                or telemetry.meta.process_generation != self._identity.process_generation
                or telemetry.meta.event_sequence <= self._sequence
                or telemetry.data.written_frames < self._written_frames
            ):
                return False
            self._sequence = telemetry.meta.event_sequence
            self._written_frames = telemetry.data.written_frames
            self._latest = telemetry
            return True

    def take(self) -> RecordingTelemetry | None:
        with self._lock:
            latest, self._latest = self._latest, None
            return latest


def _wake_pipe_server(descriptor: int) -> None:
    """One byte wakes the selector; a full pipe already holds an undelivered wake-up."""
    try:
        os.write(descriptor, b"\0")
    except BlockingIOError:
        pass


def _drain_pipe_server_wakeups(descriptor: int) -> None:
    try:
        while os.read(descriptor, CONTROL_READ_BYTES):
            pass
    except BlockingIOError:
        pass


def serve_recording_pipes(session: RecordingServiceSession, input_fd: int, output_fd: int) -> None:
    """Serve inherited local pipes with bounded output and prompt EOF/cancellation handling.

    Replies complete on the metadata threads; a self-pipe in the same selector delivers
    each one as soon as it is published instead of at the next timed poll.
    """
    if not all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (input_fd, output_fd)):
        session.close()
        raise RecordingServiceError(
            "RECORDING.INVALID_MESSAGE", "recording service requires inherited local pipes"
        )
    decoder = RecordingFrameDecoder()
    output: deque[memoryview] = deque()
    queued_bytes = 0
    last_output_progress = time.monotonic()
    original_input_blocking = os.get_blocking(input_fd)
    original_output_blocking = os.get_blocking(output_fd)
    os.set_blocking(input_fd, False)
    os.set_blocking(output_fd, False)
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    session.attach_control_wakeup(lambda: _wake_pipe_server(wake_write))
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(input_fd, selectors.EVENT_READ)
            selector.register(wake_read, selectors.EVENT_READ)
            output_registered = False
            while not session.closed or output or session.has_responses:
                if session.terminal_error is not None:
                    raise session.terminal_error
                for response in session.completed():
                    payload = recording_frame_bytes(response)
                    output.append(memoryview(payload))
                    queued_bytes += len(payload)
                if queued_bytes > MAX_OUTPUT_BYTES:
                    raise RecordingServiceError(
                        "RECORDING.PARENT_LOST", "recording reply backlog exceeded its bound"
                    )
                if output and not output_registered:
                    selector.register(output_fd, selectors.EVENT_WRITE)
                    output_registered = True
                    last_output_progress = time.monotonic()
                elif not output and output_registered:
                    selector.unregister(output_fd)
                    output_registered = False
                # Blocked output keeps its deadline; otherwise only input, a wake-up or the
                # idle safety net ends the wait, so replies never wait on a poll interval.
                timeout = (
                    max(
                        0.0,
                        CONTROL_OUTPUT_DEADLINE_SECONDS - (time.monotonic() - last_output_progress),
                    )
                    if output
                    else CONTROL_IDLE_WAKE_SECONDS
                )
                for selected, mask in selector.select(timeout):
                    if selected.fd == wake_read and mask & selectors.EVENT_READ:
                        _drain_pipe_server_wakeups(wake_read)
                    elif selected.fd == input_fd and mask & selectors.EVENT_READ:
                        chunk = os.read(input_fd, CONTROL_READ_BYTES)
                        if not chunk:
                            decoder.finish()
                            return
                        for payload in decoder.feed(chunk):
                            session.submit(decode_service_request(payload))
                    elif selected.fd == output_fd and mask & selectors.EVENT_WRITE and output:
                        written = os.write(output_fd, output[0])
                        if written == 0:
                            raise RecordingServiceError(
                                "RECORDING.PARENT_LOST", "recording reply pipe made no progress"
                            )
                        queued_bytes -= written
                        last_output_progress = time.monotonic()
                        if written == len(output[0]):
                            output.popleft()
                        else:
                            output[0] = output[0][written:]
                if (
                    output
                    and time.monotonic() - last_output_progress > CONTROL_OUTPUT_DEADLINE_SECONDS
                ):
                    raise RecordingServiceError(
                        "RECORDING.PARENT_LOST", "recording reply pipe remained blocked"
                    )
    except (BrokenPipeError, ConnectionResetError) as error:
        raise RecordingServiceError(
            "RECORDING.PARENT_LOST", "recording control peer disconnected"
        ) from error
    finally:
        try:
            session.close()
        finally:
            # Detach under the session lock first: a late reply must never write into a
            # descriptor number these closes hand back to the process.
            session.attach_control_wakeup(None)
            os.close(wake_read)
            os.close(wake_write)
            os.set_blocking(input_fd, original_input_blocking)
            os.set_blocking(output_fd, original_output_blocking)


def recording_service_build_identity() -> RecordingBuildIdentity:
    """Fingerprint Python code/interpreter/runtime without loading optional model stacks.

    The source digest covers every package ``.py`` file plus every SQL migration the service
    applies: the recording-index projection (including the prompt-alias projection) and the
    corpus store it reads the active catalog from.
    Returns: the service build identity.
    Raises: ``OSError`` when a package file cannot be read; ``RecordingIndexError`` or
    ``SqliteMigrationError`` (``SQLITE_MIGRATIONS.RESOURCE_MISSING``) when a migration
    resource is missing; ``SecurityError`` when one is a symlink.
    """
    package = Path(__file__).resolve().parent
    files: list[tuple[str, str, int]] = []
    for path in sorted(package.rglob("*.py")):
        digest, size = sha256_file(path)
        files.append((path.relative_to(package).as_posix(), digest, size))
    from .recording_index import recording_index_migration_resources

    files.extend(recording_index_migration_resources())
    files.extend(corpus_store_migration_resources())
    files.sort()
    executable_sha256, _ = sha256_file(Path(sys.executable).resolve())
    from .runtime import recording_python_environment_identity

    environment = recording_python_environment_identity()
    return RecordingBuildIdentity(
        build_id="python-recording-service-v1",
        source_sha256=canonical_sha256(files),
        executable_sha256=executable_sha256,
        environment_sha256=canonical_sha256(environment),
    )


def _read_private_build_identity(path: Path) -> RecordingBuildIdentity:
    path = _reject_existing_symlink_components(path)
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RecordingServiceError(
                "RECORDING.BUILD_MISMATCH", "build identity manifest must be private"
            )
        if metadata.st_size > 4096:
            raise RecordingServiceError(
                "RECORDING.BUILD_MISMATCH", "build identity manifest is too large"
            )
        return RecordingBuildIdentity.model_validate_json(stream.read(4097))


def main(argv: Sequence[str] | None = None) -> int:
    """Launch only on inherited stdin/stdout pipes; no socket listener or audio input is opened.

    ``--native-workspace`` serves the subject's active corpus-store catalog (pinned at first
    use); ``--catalog-sha256`` is optional there and, when given, only names the pre-context
    snapshot's digest. ``--recording-group-id`` and the unprepared default require
    ``--catalog-sha256`` (the group's digest, or the unprepared snapshot's).
    Returns: 0 after a clean shutdown, 2 after a failure (one JSON line on stderr naming the
    error code, e.g. ``RECORDING.INVALID_MESSAGE`` for a missing ``--catalog-sha256``).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--catalog-sha256")
    parser.add_argument("--recording-group-id")
    parser.add_argument("--native-workspace", action="store_true")
    parser.add_argument("--subject-id", default="cole-self")
    parser.add_argument("--process-generation", required=True)
    parser.add_argument("--expected-client-build", type=Path, required=True)
    parser.add_argument("--build-identity", type=Path, required=True)
    parser.add_argument("--analysis-test-block-fd", type=int)
    parser.add_argument("--package-test-block-fd", type=int)
    options = parser.parse_args(argv)
    from .recording_analysis import RecordingAnalysisQueue, RecordingAnalysisSupervisor
    from .recording_edit_jobs import RecordingEditCoordinator
    from .recording_index import RecordingIndex
    from .recording_package_jobs import RecordingPackageQueue, RecordingPackageSupervisor
    from .recording_packages import RecordingPackageStore

    package_queue = None
    package_supervisor = None
    analysis_queue = None
    analysis_supervisor = None
    recovery_supervisor = None
    edit_coordinator = None
    index = None
    workspace = None
    try:
        layout = AssetLayout.validate(options.asset_root)
        build = recording_service_build_identity()
        if build != _read_private_build_identity(options.build_identity):
            raise RecordingServiceError(
                "RECORDING.BUILD_MISMATCH", "service runtime differs from the expected build"
            )
        if options.native_workspace and options.recording_group_id is not None:
            raise ValueError("native workspace bootstrap owns its automatic context")
        if options.catalog_sha256 is None:
            if not options.native_workspace:
                raise ValueError(
                    "a group or unprepared recording service requires --catalog-sha256"
                )
        else:
            _SHA256.validate_python(options.catalog_sha256)
        backend: RecordingServiceBackend | None = None
        if options.recording_group_id is not None:
            store = RecordingTakeStore(layout)
            group = store.get_group(options.recording_group_id)
            index = RecordingIndex(layout, subject_id=group.subject_id)
            index.register_catalog(store.group_catalog(group))

            def project_take(take: RecordingTake) -> None:
                assert index is not None
                index.update_take(take)

            store.projection_hook = project_take
            analysis_queue = RecordingAnalysisQueue(
                store,
                index,
                build=build.model_copy(update={"build_id": "python-recording-analysis-v1"}),
                recover=False,
            )
            prepared = RecordingTakesBackend(
                store, options.recording_group_id, analysis_queue=analysis_queue
            )
            if prepared.group.catalog_sha256 != options.catalog_sha256:
                raise RecordingServiceError(
                    "RECORDING.STALE_REVISION", "configured catalog differs from durable group"
                )
            backend = prepared
        if options.native_workspace:
            workspace = RecordingWorkspaceBackend(
                layout, options.subject_id, options.catalog_sha256
            )
            index = workspace.index
            analysis_queue = RecordingAnalysisQueue(
                workspace.store,
                index,
                build=build.model_copy(update={"build_id": "python-recording-analysis-v1"}),
                recover=False,
            )
            workspace.analysis_queue = analysis_queue
            backend = workspace
        if backend is None:
            backend = UnpreparedRecordingBackend(options.catalog_sha256)
        if isinstance(backend, (RecordingWorkspaceBackend, RecordingTakesBackend)):
            package_queue = RecordingPackageQueue(
                RecordingPackageStore(
                    layout,
                    take_store=backend.store,
                    index=index,
                    analyzer_build=build.model_copy(
                        update={"build_id": "python-recording-analysis-v1"}
                    ),
                )
            )
            backend.package_queue = package_queue
        session = RecordingServiceSession(
            build=build,
            expected_client=_read_private_build_identity(options.expected_client_build),
            process_generation=options.process_generation,
            backend=backend,
        )
        if workspace is not None:
            edit_coordinator = RecordingEditCoordinator(
                workspace.store,
                published=lambda event: session.publish_event(
                    "derivative-" + event.recipe_sha256
                    if isinstance(event, DerivativeEvent)
                    else event.operation_id,
                    event,
                ),
                failed=lambda error: session.publish_error("edit-supervisor", error),
            )
            workspace.edit_coordinator = edit_coordinator
        if analysis_queue is not None:
            analysis_supervisor = RecordingAnalysisSupervisor(
                analysis_queue,
                on_result=session.publish_analysis,
                on_failure=lambda error: session.publish_error("analysis-supervisor", error),
                block_fd=options.analysis_test_block_fd,
            )
            analysis_supervisor.start()
        if analysis_queue is not None and workspace is not None:
            # Startup recovery belongs to the interactive native session. A group-scoped
            # takes service owns its own registration lifecycle, and recovering unsealed
            # takes underneath it would change results its caller is still waiting for.
            recovery_supervisor = RecordingRecoverySupervisor(
                RecordingRecovery(
                    analysis_queue.store,
                    submit_analysis=analysis_queue.submit,
                    subject_id=options.subject_id,
                    index=index,
                ),
                index=workspace.index,
                selected_take_id=(
                    cursor.selection.take_id
                    if (cursor := workspace.store.get_cursor(options.subject_id)) is not None
                    else None
                ),
                on_changed=lambda event: session.publish_event(
                    "inventory-" + canonical_sha256(event)[:48], event
                ),
                on_failure=lambda error: session.publish_error("recovery-supervisor", error),
            )
            recovery_supervisor.start()
        if package_queue is not None:
            package_supervisor = RecordingPackageSupervisor(
                package_queue,
                on_event=session.publish_event,
                on_failure=lambda error: session.publish_error("package-supervisor", error),
                block_fd=options.package_test_block_fd,
            )
            package_supervisor.start()
        serve_recording_pipes(session, sys.stdin.fileno(), sys.stdout.fileno())
        return 0
    except (VoiceLabError, ValidationError, OSError, ValueError) as error:
        code = (
            error.recording_error.code
            if isinstance(error, RecordingServiceError)
            else "RECORDING.INVALID_MESSAGE"
        )
        print(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": "error",
                    "trace_id": "recording-service-control",
                    "error_code": code,
                    "service": "recording",
                    "message": "recording service stopped",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    finally:
        if package_supervisor is not None:
            package_supervisor.close()
        if package_queue is not None:
            package_queue.close()
        if edit_coordinator is not None:
            edit_coordinator.close()
        if workspace is not None and workspace.discovery is not None:
            workspace.discovery.close()
        if recovery_supervisor is not None:
            recovery_supervisor.close()
        if analysis_supervisor is not None:
            analysis_supervisor.close()
        if analysis_queue is not None:
            analysis_queue.close()
        if index is not None:
            index.close()


if __name__ == "__main__":
    raise SystemExit(main())

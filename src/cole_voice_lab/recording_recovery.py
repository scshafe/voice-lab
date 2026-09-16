"""Bounded restart recovery; native receipts never become a keep or QC decision.

@canonical recording_recovery -- background or explicit recovery only. Capture
ownership excludes a live writer. Original and partial PCM are never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError, model_validator

from .canonical import canonical_json_bytes, canonical_sha256, open_regular_no_follow, sha256_file
from .errors import ContractError, SecurityError
from .models import ContractModel
from .paths import confined_path
from .recording_contracts import (
    ContextEvent,
    NativeRecoveryReceipt,
    PreparedCaptureGate,
    RecordingError,
    RecordingRecoveryObservation,
    RecordingRecoveryWork,
    RecordingTake,
    RecoveryInventoryChangedEvent,
)
from .recording_contracts import (
    ContinueLegacyRecordingCommand as ContinueLegacyRecordingCommand,
)
from .recording_contracts import (
    RecordingRecoveryPage as RecordingRecoveryPage,
)
from .recording_contracts import (
    RecordingRecoveryStatus as RecordingRecoveryStatus,
)
from .recording_library import DesktopSessionEvidence
from .recording_ownership import RecordingOwnershipConflict, capture_ownership
from .recording_takes import RecordingMetadataBusyError, RecordingTakeStore

if TYPE_CHECKING:
    from .recording_index import RecordingIndex

MAX_RECOVERY_PAGE = 50
MAX_RECEIPT_BYTES = 1_048_576
RECOVERY_BUSY_RETRY_SECONDS = 0.5
RECOVERY_BUSY_RETRY_INTERVAL = 0.01
RECOVERY_IDLE_POLL_SECONDS = 0.1


class _RecoveryStopped(Exception):
    """Internal cancellation before an owned startup recovery commit."""


class RecordingLegacyContinuation(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-legacy-continuation/v1"] = (
        "cole-voice-lab.recording-legacy-continuation/v1"
    )
    command: ContinueLegacyRecordingCommand
    original_evidence: DesktopSessionEvidence

    @model_validator(mode="after")
    def _exact_subject_and_catalog(self) -> RecordingLegacyContinuation:
        evidence = self.original_evidence
        context = self.command.context
        if (
            evidence.session_id != self.command.session_id
            or evidence.subject_id != context.subject_id
            or evidence.catalog_suite != context.catalog
            or evidence.catalog_sha256 != context.catalog_sha256
            or evidence.manifest_sha256 is not None
            or hashlib.sha256(canonical_json_bytes(evidence) + b"\n").hexdigest()
            != self.command.session_evidence_sha256
        ):
            raise ValueError("continuation must bind an unfinished exact subject/catalog session")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate native recovery field")
        value[key] = item
    return value


def _read_native_receipt(path: Path) -> NativeRecoveryReceipt:
    with open_regular_no_follow(path) as stream:
        before = os.fstat(stream.fileno())
        if (
            before.st_nlink != 1
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > MAX_RECEIPT_BYTES
        ):
            raise SecurityError("native recovery receipt must be bounded private evidence")
        payload = stream.read(MAX_RECEIPT_BYTES + 1)
        after = os.fstat(stream.fileno())
        current = path.lstat()

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_mode,
            value.st_nlink,
            value.st_uid,
        )

    if (
        identity(before) != identity(after)
        or identity(after) != identity(current)
        or len(payload) > MAX_RECEIPT_BYTES
    ):
        raise SecurityError("native recovery receipt changed while reading")
    # Swift receipt key order is not canonical Python order. Still reject duplicate
    # fields before the canonical strict model validates JSON scalar types.
    json.loads(payload, object_pairs_hook=_unique_object)
    return NativeRecoveryReceipt.model_validate_json(payload)


class RecordingRecovery:
    def __init__(
        self,
        store: RecordingTakeStore,
        *,
        submit_analysis: Callable[[str], object] | None = None,
        subject_id: str | None = None,
        index: RecordingIndex | None = None,
    ) -> None:
        self.store = store
        self.submit_analysis = submit_analysis
        self.subject_id = subject_id
        self.index = index

    def continue_legacy(
        self,
        command: ContinueLegacyRecordingCommand,
        *,
        prepared_gate: PreparedCaptureGate | None = None,
        commit_barrier: Callable[[], None] | None = None,
    ) -> ContextEvent:
        """Bind preserved legacy provenance to a new context; never reopen its engine.

        Intent precedes context commit. Replaying the exact operation repairs that
        gap, and frozen evidence retains old dates, devices and individual takes.
        Historical coverage still requires the existing independent discovery.
        The context embeds the session's frozen catalog (``ContinueLegacyRecordingCommand``
        requires that form), so the new group is an embedded recording-group/v1: legacy
        continuation is the one caller of ``open_group``'s embedded form.
        Returns: the context event of the new group.
        Raises: ``ContractError`` when the operation already binds other evidence, the session
        is finalized or its sidecar no longer matches the command; ``SecurityError`` when the
        session evidence changed or is unsafe; ``RecordingOwnershipConflict`` while another
        capture owns the root; the ``RecordingTakeStore.open_context`` errors.
        """
        directory = self.store._directory("provenance/recording-legacy-continuations")
        intent_path = directory / f"{command.operation_id}.json"
        with capture_ownership(self.store.layout):
            with self.store._metadata_lock():
                if intent_path.exists():
                    intent = self.store._read(intent_path, RecordingLegacyContinuation)
                    if intent.command != command:
                        raise ContractError(
                            "continuation operation already binds different evidence"
                        )
                    if commit_barrier is not None:
                        commit_barrier()
                else:
                    path = confined_path(
                        self.store.layout.root,
                        f"provenance/capture-desktop/{command.session_id}.json",
                        must_exist=True,
                    )
                    evidence = self.store._read(path, DesktopSessionEvidence)
                    if sha256_file(path)[0] != command.session_evidence_sha256:
                        raise SecurityError("legacy session evidence changed before continuation")
                    intent = RecordingLegacyContinuation(
                        command=command, original_evidence=evidence
                    )
                    ready = confined_path(
                        self.store.layout.root, f"tmp/capture-{command.session_id}/READY"
                    )
                    if ready.exists():
                        raise ContractError(
                            "finalized legacy capture is history, not an unfinished session"
                        )
                    # This immutable intent is the first durable operation evidence.
                    # Cancellation must be fenced before it, not at the later context.
                    if commit_barrier is not None:
                        commit_barrier()
                    self.store._write_immutable(intent_path, intent)
            # Keep legacy/native capture exclusion across the intent/context gap.
            return self.store.open_context(command.context, prepared_gate=prepared_gate)

    def recover_take(
        self,
        take_id: str,
        *,
        commit_barrier: Callable[[], None] | None = None,
        raise_metadata_busy: bool = False,
        selected: bool = True,
    ) -> RecordingRecoveryStatus:
        """Recover one owned reservation, isolating failure from other takes."""
        work = (
            None
            if self.index is None
            else self.index.claim_recovery_work(
                take_id, explicit=True, allow_current_session_reservation=selected
            )
        )
        if self.index is not None and work is None:
            return RecordingRecoveryStatus(take_id=take_id, status="busy")
        try:
            observed = self.observe_take(
                take_id,
                commit_barrier=commit_barrier,
                raise_metadata_busy=raise_metadata_busy,
                defer_analysis=self.index is not None,
            )
            if work is not None and self.index is not None:
                deadline = time.monotonic() + RECOVERY_BUSY_RETRY_SECONDS
                while True:
                    try:
                        with self.store.recovery_projection_barrier():
                            self.index.publish_recovery_work(work, observed)
                        break
                    except RecordingMetadataBusyError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(RECOVERY_BUSY_RETRY_INTERVAL)
            if (
                self.index is not None
                and observed.result.status in {"draft", "kept", "rejected", "removed"}
                and self.submit_analysis is not None
            ):
                # The recovered finding must precede queue projections that advance
                # the take revision. Existing standalone recovery still submits inline.
                self.submit_analysis(take_id)
            return observed.result
        finally:
            if work is not None and self.index is not None:
                # Successful/stale work is already unclaimed; failed or cancelled work
                # remains retryable without discarding its last classified finding.
                self.index.release_recovery_work(work)

    def observe_take(
        self,
        take_id: str,
        *,
        commit_barrier: Callable[[], None] | None = None,
        raise_metadata_busy: bool = False,
        submit_existing_analysis: bool = True,
        nonlocking_history: bool = False,
        defer_analysis: bool = False,
    ) -> RecordingRecoveryObservation:
        """Classify/recover one take and retain the actual head used for that result."""
        take: RecordingTake | None = None
        read_take = (
            self.store.get_take_for_projection if nonlocking_history else self.store.get_take
        )
        try:
            take = read_take(take_id)
            if self.subject_id is not None and take.reservation.group.subject_id != self.subject_id:
                raise SecurityError("recovery take does not belong to this workspace subject")
            if take.seal is not None:
                return self._sealed(
                    take, recovered=False, submit=submit_existing_analysis and not defer_analysis
                )
            try:
                with capture_ownership(self.store.layout):
                    # A writer may have finished since the first metadata read.
                    take = read_take(take_id)
                    if take.seal is not None:
                        return self._sealed(
                            take,
                            recovered=False,
                            submit=submit_existing_analysis and not defer_analysis,
                        )
                    return self._recover_owned(
                        take, commit_barrier=commit_barrier, submit_analysis=not defer_analysis
                    )
            except RecordingOwnershipConflict:
                result = RecordingRecoveryStatus(take_id=take_id, status="busy")
        except RecordingMetadataBusyError:
            if raise_metadata_busy:
                raise
            result = RecordingRecoveryStatus(take_id=take_id, status="busy")
        except OSError:
            result = RecordingRecoveryStatus(
                take_id=take_id,
                status="unavailable",
                error=RecordingError(
                    code="RECORDING.WRITE_FAILED",
                    message="Recovery metadata could not be synced; capture remains preserved",
                    retryable=True,
                ),
            )
        except (ContractError, SecurityError, ValidationError, ValueError):
            result = RecordingRecoveryStatus(
                take_id=take_id,
                status="unavailable",
                error=RecordingError(
                    code="RECORDING.SOURCE_CHANGED",
                    message="Preserved capture evidence could not be recovered",
                    retryable=False,
                ),
            )
        return RecordingRecoveryObservation(
            result=result,
            observed_take_revision=None if take is None else take.state_revision,
            subject_id=None if take is None else take.reservation.group.subject_id,
        )

    def _sealed(
        self, take: RecordingTake, *, recovered: bool, submit: bool = True
    ) -> RecordingRecoveryObservation:
        if submit and self.submit_analysis is not None:
            self.submit_analysis(take.reservation.take_id)
        return RecordingRecoveryObservation(
            result=RecordingRecoveryStatus(
                take_id=take.reservation.take_id,
                status=take.decision,
                recovered_seal=recovered,
            ),
            observed_take_revision=take.state_revision,
            subject_id=take.reservation.group.subject_id,
        )

    def _recover_owned(
        self,
        take: RecordingTake,
        *,
        commit_barrier: Callable[[], None] | None = None,
        submit_analysis: bool = True,
    ) -> RecordingRecoveryObservation:
        reservation = take.reservation
        directory = confined_path(
            self.store.layout.root, f"tmp/recording-takes/{reservation.take_id}", must_exist=True
        )
        receipt_path = confined_path(directory, "native-recovery.json")
        if not receipt_path.exists():
            partial = confined_path(self.store.layout.root, reservation.partial_path)
            original = confined_path(self.store.layout.root, reservation.original_path)
            material = partial.exists() or original.exists()
            return RecordingRecoveryObservation(
                result=RecordingRecoveryStatus(
                    take_id=reservation.take_id,
                    status="partial" if material else "empty",
                    error=RecordingError(
                        code="RECORDING.WRITE_FAILED",
                        message="Capture ended without a seal; preserved material is incomplete",
                        retryable=False,
                    ),
                ),
                observed_take_revision=take.state_revision,
                subject_id=take.reservation.group.subject_id,
            )
        receipt = _read_native_receipt(receipt_path)
        if (
            receipt.take_id != reservation.take_id
            or receipt.reservation_sha256 != canonical_sha256(reservation)
            or receipt.process_generation != reservation.process_generation
            or receipt.partial_path != reservation.partial_path
            or receipt.recorded_at < reservation.requested_at
            or receipt.received_frames > reservation.maximum_frames
        ):
            raise SecurityError("native recovery receipt does not bind its reservation")
        if receipt.seal is None:
            return RecordingRecoveryObservation(
                result=RecordingRecoveryStatus(
                    take_id=reservation.take_id, status="partial", error=receipt.error
                ),
                observed_take_revision=take.state_revision,
                subject_id=take.reservation.group.subject_id,
            )
        # register_seal publishes immutable seal evidence before its operation
        # journal; fence before entering that first durable publication.
        if commit_barrier is not None:
            commit_barrier()
        recovered = self.store.register_seal(
            operation_id="recover-seal-" + canonical_sha256(receipt), receipt=receipt.seal
        )
        return self._sealed(recovered, recovered=True, submit=submit_analysis)

    def _page_status(
        self, take_id: str, *, commit_barrier: Callable[[], None] | None = None
    ) -> RecordingRecoveryStatus | None:
        if self.subject_id is not None:
            try:
                take = self.store.get_take_for_projection(take_id)
                if take.reservation.group.subject_id != self.subject_id:
                    return None
            except (ContractError, SecurityError, OSError, ValidationError, ValueError):
                # recover_take owns the per-entry named diagnostic; a broken head
                # cannot hide the rest of this bounded page.
                pass
        return self.recover_take(take_id, commit_barrier=commit_barrier, selected=False)

    def page(
        self,
        *,
        after_take_id: str | None = None,
        limit: int = MAX_RECOVERY_PAGE,
        commit_barrier: Callable[[], None] | None = None,
    ) -> RecordingRecoveryPage:
        if not 1 <= limit <= MAX_RECOVERY_PAGE:
            raise ValueError("recovery page limit is outside its bounds")
        take_ids = self.store.iter_take_ids_for_projection(after_take_id=after_take_id, limit=limit)
        return RecordingRecoveryPage(
            entries=tuple(
                result
                for take_id in take_ids
                if (result := self._page_status(take_id, commit_barrier=commit_barrier)) is not None
            ),
            next_take_id=take_ids[-1] if len(take_ids) == limit else None,
        )


class RecordingRecoverySupervisor:
    """Owned background inventory; interactive snapshots never enumerate or recover takes."""

    def __init__(
        self,
        recovery: RecordingRecovery,
        *,
        index: RecordingIndex,
        on_changed: Callable[[RecoveryInventoryChangedEvent], None],
        on_failure: Callable[[RecordingError], None],
        selected_take_id: str | None = None,
    ) -> None:
        self.recovery, self.index = recovery, index
        self.on_changed, self.on_failure = on_changed, on_failure
        self.selected_take_id = selected_take_id
        self._stop = threading.Event()
        self._last_notification: tuple[str, int] | None = None
        self._thread = threading.Thread(
            target=self._run, name="recording-recovery-supervisor", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _before_commit(self) -> None:
        if self._stop.is_set():
            raise _RecoveryStopped

    def _recover_with_retry(
        self, work: RecordingRecoveryWork
    ) -> RecordingRecoveryObservation | None:
        deadline = time.monotonic() + RECOVERY_BUSY_RETRY_SECONDS
        while True:
            self._before_commit()
            try:
                return self.recovery.observe_take(
                    work.take_id,
                    commit_barrier=self._before_commit,
                    raise_metadata_busy=True,
                    submit_existing_analysis=False,
                    nonlocking_history=True,
                    defer_analysis=True,
                )
            except RecordingMetadataBusyError:
                if time.monotonic() >= deadline:
                    # Unread metadata remains pending, not an invented subject-bound finding.
                    return None
                if self._stop.wait(RECOVERY_BUSY_RETRY_INTERVAL):
                    raise _RecoveryStopped from None

    def _process_take(self, take_id: str) -> None:
        self._before_commit()
        work = self.index.claim_recovery_work(take_id)
        if work is None:
            return
        observation = self._recover_with_retry(work)
        if observation is None:
            self.index.release_recovery_work(work)
            return
        self._before_commit()
        try:
            # Reserve creates its directory before its pending intent. Finish its short
            # commit/protection before publishing an earlier unreadable observation.
            with self.recovery.store.recovery_projection_barrier():
                if (
                    observation.subject_id is not None
                    and observation.subject_id != self.index.subject_id
                ):
                    self.index.discard_recovery_work(work)
                else:
                    if not self.index.publish_recovery_work(work, observation):
                        self.index.release_recovery_work(work)
        except RecordingMetadataBusyError:
            self.index.release_recovery_work(work)
            return  # Pending work is retried; metadata contention is not corruption.
        if observation.result.recovered_seal and self.recovery.submit_analysis is not None:
            # Publication precedes the queue's pending/completed head changes. Those
            # ordinary projections then retain and revise the recovered-seal finding.
            while True:
                self._before_commit()
                try:
                    self.recovery.submit_analysis(take_id)
                    break
                except RecordingMetadataBusyError:
                    # Submission is still owned background work. A transient pending
                    # metadata commit must not turn the completed recovery into scan failure.
                    self._stop.wait(RECOVERY_BUSY_RETRY_INTERVAL)

    def _notify(self) -> None:
        page = self.index.recovery_snapshot(page_size=1)
        identity = (page.scan_generation, page.inventory_revision)
        if identity == self._last_notification:
            return
        self._last_notification = identity
        self.on_changed(
            RecoveryInventoryChangedEvent(
                scan_generation=page.scan_generation,
                inventory_revision=page.inventory_revision,
            )
        )

    def _run(self) -> None:
        after = None
        try:
            if self.selected_take_id is not None:
                self._process_take(self.selected_take_id)
                self._notify()
            while not self._stop.is_set():
                try:
                    take_ids = self.recovery.store.iter_take_ids_for_projection(
                        after_take_id=after, limit=MAX_RECOVERY_PAGE
                    )
                except RecordingMetadataBusyError:
                    if self._stop.wait(RECOVERY_BUSY_RETRY_INTERVAL):
                        return
                    continue
                if not take_ids:
                    break
                for take_id in take_ids:
                    self._process_take(take_id)
                self._notify()
                after = take_ids[-1]
            # Projection hooks enqueue reservations/changes even behind the directory cursor.
            # Keep this one supervisor alive to classify them after the initial scan.
            while not self._stop.is_set():
                self.index.recovery_wake.clear()
                pending = self.index.pending_recovery_take_ids(limit=MAX_RECOVERY_PAGE)
                for take_id in pending:
                    self._process_take(take_id)
                self._before_commit()
                try:
                    with self.recovery.store.recovery_projection_barrier() as revision:
                        self.index.finish_recovery_scan(covered_store_revision=revision)
                except RecordingMetadataBusyError:
                    pass
                self._notify()
                self.index.recovery_wake.wait(RECOVERY_IDLE_POLL_SECONDS)
        except _RecoveryStopped:
            return
        except (ContractError, SecurityError, OSError, ValidationError, ValueError):
            if not self._stop.is_set():
                error = RecordingError(
                    code="RECORDING.SOURCE_CHANGED",
                    message="Some preserved captures need explicit recovery review",
                    retryable=True,
                )
                try:
                    self.index.fail_recovery_scan(error)
                    self._notify()
                finally:
                    self.on_failure(error)

    def close(self) -> None:
        self._stop.set()
        self.index.recovery_wake.set()
        self._thread.join(1.0)
        if self._thread.is_alive():
            raise ContractError("recording recovery did not stop within its owned deadline")

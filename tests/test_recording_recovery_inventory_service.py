"""Read-only inventory requests remain independent of held historical observation."""

from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from test_recording_service import await_responses, build_identity, prepared, request
from test_recording_takes import Rig
from test_recording_takes import rig as rig

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.recording_contracts import (
    NativeRecoveryReceipt,
    RecordingError,
    RecordingRecoveryInventoryPage,
    RecordingRecoveryObservation,
    RecoverCommand,
    RecoveryEvent,
    RecoverySnapshotCommand,
)
from cole_voice_lab.recording_recovery import RecordingRecovery, RecordingRecoverySupervisor
from cole_voice_lab.recording_service import (
    RecordingServiceSession,
    RecordingTakesBackend,
    RecordingWorkspaceBackend,
)


def test_snapshot_replies_while_historical_recovery_is_held(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = RecordingWorkspaceBackend(
        rig.layout, "synthetic-subject", store=rig.store, discover=False
    )
    reservation = rig.reserve()
    entered, release = threading.Event(), threading.Event()
    failures: list[Any] = []
    original = RecordingRecovery.observe_take

    def held(self: RecordingRecovery, take_id: str, **kwargs: Any) -> RecordingRecoveryObservation:
        entered.set()
        assert release.wait(5)
        return original(self, take_id, **kwargs)

    monkeypatch.setattr(RecordingRecovery, "observe_take", held)
    supervisor = RecordingRecoverySupervisor(
        RecordingRecovery(rig.store, index=workspace.index, subject_id="synthetic-subject"),
        index=workspace.index,
        on_changed=lambda event: None,
        on_failure=failures.append,
    )
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=workspace,
    )
    try:
        prepared(session)
        supervisor.start()
        assert entered.wait(2)

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("interactive inventory request read canonical history")

        # The held background observer already claimed its token. The request may read
        # the bounded current store-head revision for the existing response envelope.
        with monkeypatch.context() as guarded:
            guarded.setattr(rig.store, "get_cursor", forbidden)
            guarded.setattr(rig.store, "get_group", forbidden)
            guarded.setattr(rig.store, "get_take", forbidden)
            guarded.setattr(rig.store, "get_take_for_projection", forbidden)
            guarded.setattr(rig.store, "iter_take_ids_for_projection", forbidden)
            session.submit(request(RecoverySnapshotCommand(), request_id="inventory-held"))
            response = await_responses(session)[0]
            assert response.error is None and response.data is not None
            assert response.data.event == "recovery-inventory"
            assert response.data.page.scan_state == "scanning"
            assert response.data.page.entries[0].take_id == reservation.take_id
            assert response.data.page.entries[0].pending
    finally:
        release.set()
        supervisor.close()
        session.close()
        workspace.index.close()
    assert not failures


def test_pre_intent_reservation_cannot_be_classified_before_its_protection_commits(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = RecordingWorkspaceBackend(
        rig.layout, "synthetic-subject", store=rig.store, discover=False
    )
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    failures: list[Any] = []
    reserved: list[Any] = []

    def pause_before_intent(boundary: str) -> None:
        if boundary == "before-reservation":
            entered.set()
            assert release.wait(5)

    def reserve() -> None:
        try:
            reserved.append(
                rig.store.reserve(
                    operation_id="reserve-before-intent",
                    group_id=rig.group_id,
                    prompt_id="synthetic-0000",
                    process_generation="helper-generation",
                    current_session_reservation=True,
                )
            )
        except BaseException as error:
            failures.append(error)

    original = RecordingRecovery.observe_take

    def observe(
        self: RecordingRecovery, take_id: str, **kwargs: Any
    ) -> RecordingRecoveryObservation:
        result = original(self, take_id, **kwargs)
        observed.set()
        return result

    rig.store.fault_hook = pause_before_intent
    monkeypatch.setattr(RecordingRecovery, "observe_take", observe)
    writer = threading.Thread(target=reserve)
    supervisor = RecordingRecoverySupervisor(
        RecordingRecovery(rig.store, index=workspace.index, subject_id="synthetic-subject"),
        index=workspace.index,
        on_changed=lambda event: None,
        on_failure=failures.append,
    )
    writer.start()
    try:
        assert entered.wait(2)
        supervisor.start()
        assert observed.wait(2)
        page = workspace.index.recovery_snapshot()
        assert page.entries[0].pending and page.entries[0].result is None
        release.set()
        writer.join(2)
        assert not writer.is_alive()
        deadline = time.monotonic() + 2
        while not workspace.index.recovery_snapshot().enumeration_complete:
            assert time.monotonic() < deadline
            threading.Event().wait(0.01)
        page = workspace.index.recovery_snapshot()
        assert page.scan_state == "scanning" and page.error is None
        assert page.entries[0].take_id == reserved[0].take_id
        assert page.entries[0].pending and page.entries[0].result is None
        assert workspace.index.claim_recovery_work(reserved[0].take_id) is None
    finally:
        release.set()
        writer.join(2)
        supervisor.close()
        workspace.index.close()
        rig.store.fault_hook = None
    assert not failures


def test_bulk_recovery_preserves_current_session_reservation_without_inspection(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = RecordingWorkspaceBackend(
        rig.layout, "synthetic-subject", store=rig.store, discover=False
    )
    try:
        reservation = rig.store.reserve(
            operation_id="reserve-protected",
            group_id=rig.group_id,
            prompt_id="synthetic-0000",
            process_generation="helper-generation",
            current_session_reservation=True,
        )

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("bulk recovery inspected the current-session reservation")

        monkeypatch.setattr(RecordingRecovery, "observe_take", forbidden)
        page = RecordingRecovery(rig.store, index=workspace.index).page()
        assert page.entries[0].take_id == reservation.take_id
        assert page.entries[0].status == "busy"
        assert workspace.index.claim_recovery_work(reservation.take_id) is None
    finally:
        workspace.index.close()


def _file_identity(path: Any) -> tuple[bytes, int, int, int, int]:
    status = path.stat()
    return path.read_bytes(), status.st_size, status.st_mtime_ns, status.st_ctime_ns, status.st_ino


def _await_inventory(
    workspace: RecordingWorkspaceBackend,
    ready: Callable[[RecordingRecoveryInventoryPage], bool],
) -> RecordingRecoveryInventoryPage:
    deadline = time.monotonic() + 2
    while not ready(page := workspace.index.recovery_snapshot()):
        assert time.monotonic() < deadline, page
        threading.Event().wait(0.01)
    return page


def test_zero_frame_current_session_terminal_resolves_only_through_explicit_recheck(
    rig: Rig,
) -> None:
    """Physical P09: Stop before the first buffer left a protected reservation scanning."""
    workspace = RecordingWorkspaceBackend(
        rig.layout, "synthetic-subject", store=rig.store, discover=False
    )
    failures: list[Any] = []
    supervisor = RecordingRecoverySupervisor(
        RecordingRecovery(rig.store, index=workspace.index, subject_id="synthetic-subject"),
        index=workspace.index,
        on_changed=lambda event: None,
        on_failure=failures.append,
    )
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=RecordingTakesBackend(rig.store, rig.group_id, recovery_index=workspace.index),
    )
    try:
        reservation = rig.store.reserve(
            operation_id="reserve-zero-frame",
            group_id=rig.group_id,
            prompt_id="synthetic-0000",
            process_generation="helper-generation",
            current_session_reservation=True,
        )
        take_id = reservation.take_id
        # The incident helper evidence: a header-only partial and a zero-frame receipt.
        partial = rig.layout.root / reservation.partial_path
        partial.write_bytes(
            b"RIFF"
            + struct.pack("<I", 36)
            + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 48_000, 144_000, 3, 24)
            + b"data"
            + struct.pack("<I", 0)
        )
        partial.chmod(0o600)
        receipt = partial.parent / "native-recovery.json"
        receipt.write_text(
            NativeRecoveryReceipt(
                take_id=take_id,
                reservation_sha256=canonical_sha256(reservation),
                process_generation=reservation.process_generation,
                recorded_at=rig.now,
                partial_path=reservation.partial_path,
                received_frames=0,
                written_frames=0,
                state="partial",
                error=RecordingError(
                    code="RECORDING.CANCELLED",
                    message="Capture stopped before any complete PCM frames arrived",
                    retryable=False,
                ),
            ).model_dump_json(indent=2)
        )
        receipt.chmod(0o600)
        assert partial.stat().st_size == 44
        before = {path.name: _file_identity(path) for path in (partial, receipt)}
        listing = sorted(path.name for path in partial.parent.iterdir())

        supervisor.start()
        page = _await_inventory(workspace, lambda page: page.enumeration_complete)
        # Bulk inventory never inspects the protected reservation, so it cannot finish.
        assert page.scan_state == "scanning" and page.pending_count == 1
        assert page.entries[0].take_id == take_id and page.entries[0].pending
        assert workspace.index.claim_recovery_work(take_id) is None

        prepared(session)
        command = RecoverCommand(operation_id="recover-zero-frame", take_id=take_id)
        assert session.submit(request(command, take_id=take_id)) is None
        response = await_responses(session)[0]
        assert response.error is None and isinstance(response.data, RecoveryEvent)
        (entry,) = response.data.page.entries
        assert entry.status == "partial" and not entry.recovered_seal
        assert entry.error is not None and entry.error.code == "RECORDING.CANCELLED"

        page = _await_inventory(workspace, lambda page: page.scan_state == "complete")
        assert page.pending_count == 0 and page.finding_count == 1 and page.error is None
        (finding,) = page.entries
        assert finding.take_id == take_id and not finding.pending and finding.result is not None
        assert finding.result.status == "partial"
        assert finding.result.error is not None
        assert finding.result.error.code == "RECORDING.CANCELLED"

        # Classification is metadata only: the preserved evidence is byte-for-byte untouched.
        assert {path.name: _file_identity(path) for path in (partial, receipt)} == before
        assert sorted(path.name for path in partial.parent.iterdir()) == listing
        assert rig.store.get_take(take_id).seal is None
    finally:
        supervisor.close()
        session.close()
        workspace.index.close()
    assert not failures

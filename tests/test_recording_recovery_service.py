from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import macos_only
import pytest
from pydantic import ValidationError
from test_recording_recovery import emergency, legacy_command
from test_recording_service import (
    activate_catalog,
    await_responses,
    build_identity,
    prepared,
    request,
)
from test_recording_takes import Rig, store_suite
from test_recording_takes import rig as rig

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.recording_contracts import (
    CatalogSnapshot,
    RecordingError,
    RecordingRecoveryInventoryPage,
    RecordingRecoveryPage,
    RecordingRecoveryStatus,
    RecordingRecoveryWork,
    RecoverCommand,
    RecoveryEvent,
    RecoveryInventoryChangedEvent,
    SnapshotCommand,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_recovery import RecordingRecovery, RecordingRecoverySupervisor
from cole_voice_lab.recording_service import (
    RecordingCancellation,
    RecordingServiceSession,
    RecordingTakesBackend,
    RecordingWorkspaceBackend,
)


@pytest.fixture
def recovery_index(rig: Rig) -> Iterator[RecordingIndex]:
    with RecordingIndex(rig.layout, subject_id="synthetic-subject") as index:
        index.register_catalog(CatalogSnapshot.embedded(rig.store.get_group(rig.group_id).catalog))
        index.begin_recovery_scan(scan_generation="synthetic-recovery-scan")
        previous = rig.store.projection_hook
        rig.store.projection_hook = index.update_take
        try:
            yield index
        finally:
            rig.store.projection_hook = previous


def inventory_supervisor(
    recovery: RecordingRecovery, index: RecordingIndex
) -> tuple[
    RecordingRecoverySupervisor,
    list[RecordingRecoveryInventoryPage],
    list[RecordingError],
    threading.Event,
]:
    pages: list[RecordingRecoveryInventoryPage] = []
    failures: list[RecordingError] = []
    enumerated = threading.Event()

    def changed(event: RecoveryInventoryChangedEvent) -> None:
        page = index.recovery_snapshot()
        assert page.scan_generation == event.scan_generation
        assert page.inventory_revision >= event.inventory_revision
        pages.append(page)
        if page.enumeration_complete or page.scan_state == "error":
            enumerated.set()

    return (
        RecordingRecoverySupervisor(
            recovery, index=index, on_changed=changed, on_failure=failures.append
        ),
        pages,
        failures,
        enumerated,
    )


def test_recovery_command_has_independent_preparation_lane(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    take_id, _ = emergency(rig)
    entered, release = threading.Event(), threading.Event()
    original = RecordingRecovery.recover_take

    def paused(
        self: RecordingRecovery,
        identifier: str,
        *,
        commit_barrier: Callable[[], None] | None = None,
    ) -> RecordingRecoveryStatus:
        entered.set()
        assert release.wait(5)
        return original(self, identifier, commit_barrier=commit_barrier)

    monkeypatch.setattr(RecordingRecovery, "recover_take", paused)
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=RecordingTakesBackend(rig.store, rig.group_id),
    )
    try:
        prepared(session)
        assert (
            session.submit(
                request(
                    RecoverCommand(operation_id="recover-selected", take_id=take_id),
                    take_id=take_id,
                )
            )
            is None
        )
        assert entered.wait(2)
        assert (
            session.submit(request(SnapshotCommand(), request_id="snapshot-during-recovery"))
            is None
        )
        response = await_responses(session)[0]
        assert response.error is None and response.data.event == "history"
        release.set()
        response = await_responses(session)[0]
        assert isinstance(response.data, RecoveryEvent)
        assert response.meta.take_id == take_id and response.data.page.entries[0].recovered_seal
        assert rig.store.get_take(take_id).decision == "draft"
    finally:
        release.set()
        session.close()


def test_subject_filtered_page_isolates_corrupt_head(rig: Rig) -> None:
    good_id, _ = emergency(rig)
    bad = rig.reserve("bad-head-reservation")
    head = rig.layout.root / f"provenance/recording-takes/{bad.take_id}/head.json"
    head.write_bytes(b"{broken\n")
    page = RecordingRecovery(rig.store, subject_id="synthetic-subject").page()
    assert {entry.take_id: entry.status for entry in page.entries} == {
        good_id: "draft",
        bad.take_id: "unavailable",
    }


def test_startup_supervisor_isolates_broken_head_and_submits_recovered_seal(
    rig: Rig, recovery_index: RecordingIndex
) -> None:
    good_id, receipt = emergency(rig)
    original_digest = sha256_file(receipt.parent / "original.wav")
    bad = rig.reserve("bad-head-reservation")
    head = rig.layout.root / f"provenance/recording-takes/{bad.take_id}/head.json"
    head.write_bytes(b"{broken\n")
    submitted: list[str] = []
    supervisor, pages, failures, enumerated = inventory_supervisor(
        RecordingRecovery(
            rig.store, subject_id="synthetic-subject", submit_analysis=submitted.append
        ),
        recovery_index,
    )
    supervisor.start()
    try:
        assert enumerated.wait(5)
    finally:
        supervisor.close()
    assert not failures and submitted == [good_id]
    statuses = {entry.take_id: entry.result.status for entry in pages[-1].entries if entry.result}
    assert statuses == {good_id: "draft", bad.take_id: "unavailable"}
    assert sha256_file(receipt.parent / "original.wav") == original_digest
    assert rig.store.get_take(good_id).analysis == "pending"


def test_startup_recovery_stops_while_projection_commit_is_pending(
    rig: Rig, recovery_index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.reserve()
    pending = rig.layout.root / "provenance/recording-takes/.pending.json"
    pending.write_bytes(b"{}\n")
    pending.chmod(0o600)
    supervisor, pages, failures, _ = inventory_supervisor(
        RecordingRecovery(rig.store), recovery_index
    )
    entered = threading.Event()
    original = rig.store.iter_take_ids_for_projection

    def observed(*, after_take_id: str | None = None, limit: int = 200) -> tuple[str, ...]:
        entered.set()
        return original(after_take_id=after_take_id, limit=limit)

    monkeypatch.setattr(rig.store, "iter_take_ids_for_projection", observed)
    supervisor.start()
    assert entered.wait(2)
    supervisor.close()
    assert not pages and not failures


def test_continue_legacy_service_retains_checksum_bound_prior_evidence(rig: Rig) -> None:
    command, path = legacy_command(rig)
    before = path.read_bytes()
    backend = RecordingWorkspaceBackend(
        rig.layout,
        "synthetic-subject",
        store=rig.store,
        discover=False,
    )
    try:
        message = request(command)
        backend.validate_request(message)
        event = backend.execute(message, RecordingCancellation())
        assert event.event == "context"
        assert backend.execute(message, RecordingCancellation()) == event
        assert path.read_bytes() == before
        assert event.group.group_id != rig.group_id
    finally:
        backend.index.close()


def test_recovery_wire_rejects_cross_take_scope_and_mixed_pagination() -> None:
    with pytest.raises(ValidationError, match="page cursor"):
        RecoverCommand(operation_id="recover-one", take_id="take-a", after_take_id="take-b")
    with pytest.raises(ValidationError, match="requested identity"):
        RecoveryEvent(
            operation_id="recover-one",
            take_id="take-a",
            page=RecordingRecoveryPage(
                entries=(RecordingRecoveryStatus(take_id="take-b", status="draft"),)
            ),
        )


@macos_only.REAL_RECORDING_SERVICE
def test_actual_service_startup_recovers_then_independently_verifies(
    rig: Rig, tmp_path: Path
) -> None:
    from test_recording_navigation import ServiceProcess

    from cole_voice_lab.recording_contracts import OpenContextCommand
    from cole_voice_lab.recording_takes import RecordingMetadataBusyError

    take_id, receipt = emergency(rig)
    source_before = sha256_file(receipt.parent / "original.wav")
    group = rig.store.get_group(rig.group_id)
    reference, _ = activate_catalog(rig.layout, store_suite(2))
    service = ServiceProcess(tmp_path, rig.layout, "recovery-generation")
    try:
        opened = service.request(
            OpenContextCommand(
                operation_id="recovery-open-context",
                subject_id=group.subject_id,
                catalog_reference=reference,
                capture_device=group.capture_device,
                capture_host=group.capture_host,
            ).model_dump()
        )
        assert opened is not None and opened.error is None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = service.request({"command": "snapshot"})
            assert response is not None and response.error is None
            try:
                take = rig.store.get_take_for_projection(take_id)
            except RecordingMetadataBusyError:
                time.sleep(0.01)
                continue
            if take.analysis in {"verified", "needs-review", "invalid", "failed"}:
                break
            time.sleep(0.01)
        assert take.seal is not None and take.analysis in {"verified", "needs-review"}
        assert take.decision == "draft" and take.export == "unprepared"
        response = service.request(
            {"command": "recover", "operation_id": "recover-explicit", "take_id": take_id}, take_id
        )
        assert response is not None and isinstance(response.data, RecoveryEvent)
        assert response.data.page.entries[0].status == "draft"
        service.request({"command": "shutdown"})
        assert service.process.wait(timeout=5) == 0
    finally:
        service.close()
    assert sha256_file(receipt.parent / "original.wav") == source_before


@pytest.mark.parametrize("paged", [False, True])
def test_cancelled_recovery_cannot_publish_immutable_seal(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, paged: bool
) -> None:
    from cole_voice_lab import recording_recovery
    from cole_voice_lab.recording_service import RecordingServiceError

    take_id, receipt = emergency(rig)
    source_before = sha256_file(receipt.parent / "original.wav")
    revision = rig.store.state_revision
    cancellation = RecordingCancellation()
    original = recording_recovery._read_native_receipt

    def cancel_after_read(path: Path):  # type: ignore[no-untyped-def]
        value = original(path)
        assert cancellation.cancel()
        return value

    monkeypatch.setattr(recording_recovery, "_read_native_receipt", cancel_after_read)
    backend = RecordingTakesBackend(rig.store, rig.group_id)
    with pytest.raises(RecordingServiceError) as caught:
        backend.execute(
            request(
                RecoverCommand(operation_id="cancel-recovery", take_id=None if paged else take_id),
                take_id=None if paged else take_id,
            ),
            cancellation,
        )
    assert caught.value.recording_error.code == "RECORDING.CANCELLED"
    assert rig.store.state_revision == revision and rig.store.get_take(take_id).seal is None
    assert not (rig.layout.root / f"provenance/recording-takes/{take_id}/seal.json").exists()
    assert sha256_file(receipt.parent / "original.wav") == source_before


def test_cancelled_legacy_continuation_leaves_no_durable_intent(rig: Rig) -> None:
    from cole_voice_lab.recording_service import RecordingServiceError

    command, path = legacy_command(rig)
    before = path.read_bytes()
    cancellation = RecordingCancellation()
    assert cancellation.cancel()
    with pytest.raises(RecordingServiceError):
        RecordingRecovery(rig.store).continue_legacy(
            command, commit_barrier=cancellation.begin_commit
        )
    intent = rig.layout.root / "provenance/recording-legacy-continuations/continue-legacy.json"
    assert not intent.exists() and rig.store.get_cursor(command.context.subject_id) is None
    assert path.read_bytes() == before


def test_legacy_context_commit_retains_capture_owner(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.recording_ownership import RecordingOwnershipConflict, capture_ownership

    command, _ = legacy_command(rig)
    original = rig.store.open_context
    observed = []

    def owned(*args, **kwargs):  # type: ignore[no-untyped-def]
        with pytest.raises(RecordingOwnershipConflict):
            with capture_ownership(rig.layout):
                pass
        observed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(rig.store, "open_context", owned)
    RecordingRecovery(rig.store).continue_legacy(command)
    assert observed == [True]


def test_supervisor_retries_only_typed_metadata_contention(
    rig: Rig, recovery_index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.recording_takes import RecordingMetadataBusyError

    take_id, _ = emergency(rig)
    original = rig.store.get_take_for_projection
    attempts = []

    def transient(identifier: str):  # type: ignore[no-untyped-def]
        attempts.append(identifier)
        if len(attempts) < 3:
            raise RecordingMetadataBusyError("synthetic short metadata commit")
        return original(identifier)

    monkeypatch.setattr(rig.store, "get_take_for_projection", transient)
    supervisor, pages, failures, enumerated = inventory_supervisor(
        RecordingRecovery(rig.store), recovery_index
    )
    supervisor.start()
    try:
        assert enumerated.wait(3)
    finally:
        supervisor.close()
    assert not failures and pages[-1].entries[0].result.recovered_seal
    assert len(attempts) == 4 and rig.store.get_take(take_id).seal is not None


def test_supervisor_capture_owner_is_busy_without_metadata_retry(
    rig: Rig, recovery_index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.recording_ownership import capture_ownership

    take_id, _ = emergency(rig)
    original = rig.store.get_take_for_projection
    attempts = []

    def observed(identifier: str):  # type: ignore[no-untyped-def]
        attempts.append(identifier)
        return original(identifier)

    monkeypatch.setattr(rig.store, "get_take_for_projection", observed)
    supervisor, pages, failures, enumerated = inventory_supervisor(
        RecordingRecovery(rig.store), recovery_index
    )
    with capture_ownership(rig.layout):
        supervisor.start()
        try:
            assert enumerated.wait(2)
        finally:
            supervisor.close()
    assert not failures and attempts == [take_id]
    assert pages[-1].entries[0].result == RecordingRecoveryStatus(take_id=take_id, status="busy")
    assert pages[-1].scan_state == "complete"


def test_supervisor_stop_vetoes_inflight_receipt_before_commit(
    rig: Rig, recovery_index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import recording_recovery

    take_id, _ = emergency(rig)
    original = recording_recovery._read_native_receipt
    entered, release = threading.Event(), threading.Event()

    def paused(path: Path):  # type: ignore[no-untyped-def]
        value = original(path)
        entered.set()
        assert release.wait(2)
        return value

    monkeypatch.setattr(recording_recovery, "_read_native_receipt", paused)
    supervisor, pages, failures, _ = inventory_supervisor(
        RecordingRecovery(rig.store), recovery_index
    )
    supervisor.start()
    assert entered.wait(2)
    supervisor._stop.set()
    release.set()
    supervisor.close()
    assert not pages and not failures and rig.store.get_take(take_id).seal is None
    assert not (rig.layout.root / f"provenance/recording-takes/{take_id}/seal.json").exists()


def test_supervisor_exhausted_contention_does_not_hide_next_page(
    rig: Rig, recovery_index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import recording_recovery
    from cole_voice_lab.recording_takes import RecordingMetadataBusyError

    first = rig.reserve("first-busy")
    second = rig.reserve("second-empty")
    identifiers = sorted([first.take_id, second.take_id])
    original = rig.store.get_take_for_projection

    def busy(identifier: str):  # type: ignore[no-untyped-def]
        if identifier == identifiers[0]:
            raise RecordingMetadataBusyError("synthetic long commit")
        return original(identifier)

    monkeypatch.setattr(rig.store, "get_take_for_projection", busy)
    monkeypatch.setattr(recording_recovery, "MAX_RECOVERY_PAGE", 1)
    monkeypatch.setattr(recording_recovery, "RECOVERY_BUSY_RETRY_SECONDS", 0.02)
    supervisor, pages, failures, enumerated = inventory_supervisor(
        RecordingRecovery(rig.store), recovery_index
    )
    supervisor.start()
    try:
        assert enumerated.wait(3)
    finally:
        supervisor.close()
    assert not failures
    rows = {entry.take_id: entry for entry in pages[-1].entries}
    assert rows[identifiers[0]].pending and rows[identifiers[0]].result is None
    assert rows[identifiers[1]].result.status == "empty"
    assert pages[-1].scan_state == "scanning" and pages[-1].pending_count == 1


@pytest.mark.parametrize("contention", ["read", "publication"])
def test_supervisor_releases_held_work_and_retries_without_reopen(
    rig: Rig,
    recovery_index: RecordingIndex,
    monkeypatch: pytest.MonkeyPatch,
    contention: str,
) -> None:
    from cole_voice_lab import recording_recovery
    from cole_voice_lab.recording_takes import RecordingMetadataBusyError, RecordingTakeStore

    reservation = rig.reserve("retry-held-recovery")
    held, released, complete = threading.Event(), threading.Event(), threading.Event()
    original_read = rig.store.get_take_for_projection
    original_release = recovery_index.release_recovery_work
    released_work: list[RecordingRecoveryWork] = []
    failures: list[RecordingError] = []

    def release_work(work: RecordingRecoveryWork) -> None:
        original_release(work)
        released_work.append(work)
        released.set()

    def read(take_id: str):  # type: ignore[no-untyped-def]
        if not released.is_set():
            held.set()
            raise RecordingMetadataBusyError("synthetic interrupted historical read")
        return original_read(take_id)

    def hold_commit() -> None:
        with RecordingTakeStore(rig.layout)._metadata_lock():
            held.set()
            assert released.wait(3)

    def changed(_event: RecoveryInventoryChangedEvent) -> None:
        if recovery_index.recovery_snapshot().scan_state == "complete":
            complete.set()

    monkeypatch.setattr(recovery_index, "release_recovery_work", release_work)
    monkeypatch.setattr(recording_recovery, "RECOVERY_BUSY_RETRY_SECONDS", 0.02)
    owner = None
    if contention == "read":
        monkeypatch.setattr(rig.store, "get_take_for_projection", read)
    else:
        owner = threading.Thread(target=hold_commit)
        owner.start()
        assert held.wait(2)
    supervisor = RecordingRecoverySupervisor(
        RecordingRecovery(rig.store),
        index=recovery_index,
        on_changed=changed,
        on_failure=failures.append,
    )
    supervisor.start()
    try:
        assert held.wait(2)
        assert released.wait(2), "temporary contention stranded its claimed inventory work"
        assert complete.wait(3), "released work was not retried in the same open service"
    finally:
        released.set()
        if owner is not None:
            owner.join(timeout=2)
        supervisor.close()
    page = recovery_index.recovery_snapshot()
    assert not failures and released_work[0].take_id == reservation.take_id
    assert page.pending_count == 0 and page.scan_state == "complete"
    assert page.entries[0].result is not None
    assert page.entries[0].result.take_id == reservation.take_id
    assert page.entries[0].result.status == "empty"


@pytest.mark.parametrize("background", [False, True])
def test_real_analysis_submission_retains_recovered_seal_finding(
    rig: Rig, recovery_index: RecordingIndex, background: bool
) -> None:
    from cole_voice_lab.models import utc_now
    from cole_voice_lab.recording_analysis import RecordingAnalysisQueue, analysis_build_identity
    from cole_voice_lab.recording_takes import RecordingMetadataBusyError

    take_id, receipt = emergency(rig)
    original = sha256_file(receipt.parent / "original.wav")
    rig.store.clock = utc_now
    queue = RecordingAnalysisQueue(rig.store, recovery_index, build=analysis_build_identity())
    attempts = 0

    def submit(identifier: str) -> None:
        nonlocal attempts
        attempts += 1
        if background and attempts < 3:
            raise RecordingMetadataBusyError("synthetic commit before analysis submission")
        queue.submit(identifier)

    recovery = RecordingRecovery(
        rig.store,
        subject_id="synthetic-subject",
        submit_analysis=submit,
        index=recovery_index,
    )
    supervisor = None
    try:
        if background:
            supervisor, _, failures, enumerated = inventory_supervisor(recovery, recovery_index)
            supervisor.start()
            assert enumerated.wait(3)
            assert not failures
        else:
            result = recovery.recover_take(take_id)
            assert result.recovered_seal
        page = recovery_index.recovery_snapshot()
        take = rig.store.get_take(take_id)
        assert take.analysis == "pending" and take.decision == "draft"
        assert attempts == (3 if background else 1)
        assert queue.page()[0].status == "pending"
        assert page.recovered_seal_count == 1 and page.pending_count == 0
        assert page.entries[0].observed_take_revision == take.state_revision
        assert page.entries[0].result == RecordingRecoveryStatus(
            take_id=take_id, status="draft", recovered_seal=True
        )
        assert sha256_file(receipt.parent / "original.wav") == original
    finally:
        if supervisor is not None:
            supervisor.close()
        queue.close()


def test_supervisor_close_interrupts_analysis_submission_after_publishing_recovered_seal(
    rig: Rig, recovery_index: RecordingIndex
) -> None:
    from cole_voice_lab.recording_takes import RecordingMetadataBusyError

    take_id, _ = emergency(rig)
    entered = threading.Event()

    def busy(identifier: str) -> None:
        assert identifier == take_id
        entered.set()
        raise RecordingMetadataBusyError("synthetic busy analysis submission")

    supervisor, _, failures, _ = inventory_supervisor(
        RecordingRecovery(rig.store, submit_analysis=busy), recovery_index
    )
    supervisor.start()
    try:
        assert entered.wait(2)
    finally:
        supervisor.close()
    page = recovery_index.recovery_snapshot()
    assert not failures and page.recovered_seal_count == 1
    assert page.entries[0].result.recovered_seal
    assert rig.store.get_take(take_id).seal is not None


@macos_only.REAL_RECORDING_SERVICE
def test_actual_service_eof_cancels_startup_commit_and_restart_recovers(
    rig: Rig, tmp_path: Path
) -> None:
    from test_recording_navigation import ServiceProcess

    take_id, receipt = emergency(rig)
    before = sha256_file(receipt.parent / "original.wav")
    audit = tmp_path / "recovery-audit"
    audit.mkdir(mode=0o700)
    (audit / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "from cole_voice_lab import recording_recovery as r\n"
        f"root = Path({str(audit)!r})\n"
        "read = r._read_native_receipt\n"
        "def paused(path):\n"
        "    result = read(path)\n"
        "    (root / 'entered').touch(mode=0o600)\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not (root / 'release').exists():\n"
        "        if time.monotonic() > deadline: raise RuntimeError('fixture release timed out')\n"
        "        time.sleep(0.005)\n"
        "    return result\n"
        "r._read_native_receipt = paused\n"
        "close = r.RecordingRecoverySupervisor.close\n"
        "def closing(self):\n"
        "    self._stop.set()\n"
        "    (root / 'closing').touch(mode=0o600)\n"
        "    return close(self)\n"
        "r.RecordingRecoverySupervisor.close = closing\n"
    )
    service = ServiceProcess(tmp_path, rig.layout, "recovery-eof", audit_directory=audit)

    def wait_for(path: Path) -> None:
        deadline = time.monotonic() + 5
        while not path.exists():
            assert time.monotonic() < deadline, f"owned child did not reach {path.name}"
            time.sleep(0.005)

    try:
        wait_for(audit / "entered")
        assert service.process.stdin is not None
        service.process.stdin.close()
        wait_for(audit / "closing")
        (audit / "release").touch(mode=0o600)
        assert service.process.wait(timeout=5) == 0
    finally:
        (audit / "release").touch(mode=0o600)
        service.close()
    assert rig.store.get_take(take_id).seal is None
    assert not (rig.layout.root / f"provenance/recording-takes/{take_id}/seal.json").exists()
    restarted = ServiceProcess(tmp_path, rig.layout, "recovery-restart")
    try:
        deadline = time.monotonic() + 5
        from cole_voice_lab.recording_takes import RecordingMetadataBusyError

        while True:
            try:
                if rig.store.get_take_for_projection(take_id).seal is not None:
                    break
            except RecordingMetadataBusyError:
                pass  # Observe only an atomically committed authoritative head.
            assert time.monotonic() < deadline
            time.sleep(0.005)
        restarted.request({"command": "shutdown"})
        assert restarted.process.wait(timeout=5) == 0
    finally:
        restarted.close()
    assert rig.store.get_take(take_id).decision == "draft"
    assert sha256_file(receipt.parent / "original.wav") == before


def test_recovery_identity_page_has_bounded_allocation_for_ten_thousand_directories(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json
    import tracemalloc

    directory = rig.layout.root / "provenance/recording-takes"
    for number in range(10_000):
        (directory / f"take-memory-{number:05d}").mkdir(mode=0o700)
    original = Path.iterdir

    def no_eager_listing(path: Path):  # type: ignore[no-untyped-def]
        assert path != directory, "take identity paging materialized the entire directory"
        return original(path)

    monkeypatch.setattr(Path, "iterdir", no_eager_listing)
    tracemalloc.start()
    started = time.monotonic()
    identifiers = rig.store.iter_take_ids_for_projection(
        after_take_id="take-memory-04999", limit=50
    )
    elapsed = time.monotonic() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert identifiers == tuple(f"take-memory-{number:05d}" for number in range(5000, 5050))
    assert peak < 262_144
    report = {
        "synthetic_directory_count": 10_000,
        "requested_page_size": 50,
        "peak_traced_bytes": peak,
        "elapsed_seconds": elapsed,
        "historical_wav_opens": 0,
    }
    evidence = tmp_path / "recovery-page-allocation.json"
    evidence.write_text(json.dumps(report, sort_keys=True) + "\n")
    evidence.chmod(0o600)
    print(json.dumps(report, sort_keys=True))


def test_foreign_subject_recovery_is_explicit_and_filtered_pages_are_empty(rig: Rig) -> None:
    take_id, _ = emergency(rig)
    recovery = RecordingRecovery(rig.store, subject_id="other-subject")
    assert recovery.recover_take(take_id).status == "unavailable"
    assert recovery.page().entries == ()
    with RecordingIndex(rig.layout, subject_id="other-subject") as index:
        index.begin_recovery_scan(scan_generation="foreign-recovery-scan")
        supervisor, pages, failures, enumerated = inventory_supervisor(recovery, index)
        supervisor.start()
        try:
            assert enumerated.wait(2)
        finally:
            supervisor.close()
        assert not failures and all(not page.entries for page in pages)
    assert rig.store.get_take(take_id).seal is None


def test_supervisor_stop_interrupts_metadata_retry(
    rig: Rig, recovery_index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.recording_takes import RecordingMetadataBusyError

    take_id, _ = emergency(rig)
    entered = threading.Event()

    def busy(identifier: str):  # type: ignore[no-untyped-def]
        entered.set()
        raise RecordingMetadataBusyError("synthetic active metadata commit")

    monkeypatch.setattr(rig.store, "get_take", busy)
    recovery = RecordingRecovery(rig.store)
    assert recovery.recover_take(take_id).status == "busy"
    monkeypatch.setattr(rig.store, "get_take_for_projection", busy)
    entered.clear()
    supervisor, pages, failures, _ = inventory_supervisor(recovery, recovery_index)
    supervisor.start()
    assert entered.wait(2)
    supervisor.close()
    assert not pages and not failures


def test_supervisor_reports_iterator_failure_without_exposing_paths(
    rig: Rig, recovery_index: RecordingIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.errors import SecurityError

    def broken(**kwargs):  # type: ignore[no-untyped-def]
        raise SecurityError("synthetic secret path must not escape")

    monkeypatch.setattr(rig.store, "iter_take_ids_for_projection", broken)
    supervisor, pages, failures, enumerated = inventory_supervisor(
        RecordingRecovery(rig.store), recovery_index
    )
    supervisor.start()
    try:
        assert enumerated.wait(2)
    finally:
        supervisor.close()
    assert len(failures) == 1 and pages[-1].scan_state == "error"
    assert failures[0].code == "RECORDING.SOURCE_CHANGED" and failures[0].retryable
    assert "secret" not in failures[0].message


def test_recovery_commit_cannot_later_be_labelled_cancelled(rig: Rig) -> None:
    take_id, _ = emergency(rig)
    cancellation = RecordingCancellation()
    observed = []

    def after_seal(point: str) -> None:
        if point == "after-seal":
            observed.append(cancellation.cancel())

    rig.store.fault_hook = after_seal
    result = RecordingTakesBackend(rig.store, rig.group_id).execute(
        request(
            RecoverCommand(operation_id="committing-recovery", take_id=take_id), take_id=take_id
        ),
        cancellation,
    )
    assert observed == [False] and result.page.entries[0].recovered_seal
    assert rig.store.get_take(take_id).decision == "draft"

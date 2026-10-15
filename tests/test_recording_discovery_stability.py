"""Only a scan over stable native evidence certifies Complete; changed evidence retries."""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_recording_navigation import NavigationRig
from test_recording_navigation import nav as nav
from test_recording_service import await_responses, build_identity, prepared, request

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.recording_contracts import NavigateCommand, NavigationEvent, RecordingResponse
from cole_voice_lab.recording_discovery import (
    DISCOVERY_DEFERRED_RETRY_SECONDS,
    DiscoveryPublisher,
    RecordingDiscoveryCoordinator,
    _checkpoint,
    _fingerprint,
    _finish_discovery,
    discover_recording_library,
)
from cole_voice_lab.recording_discovery_contracts import (
    DiscoveryBegin,
    DiscoveryFinished,
    DiscoveryInvalid,
    DiscoveryLegacy,
    DiscoveryPublication,
    DiscoveryRequest,
    DiscoverySpoolRef,
)
from cole_voice_lab.recording_service import RecordingServiceSession, RecordingWorkspaceBackend


@pytest.fixture
def session(nav: NavigationRig) -> Iterator[RecordingServiceSession]:
    backend = RecordingWorkspaceBackend(
        nav.rig.layout,
        "synthetic-subject",
        store=nav.rig.store,
        index=nav.index,
        discover=False,
    )
    value = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=backend,
    )
    try:
        prepared(value)
        yield value
    finally:
        value.close()


def continue_missing(
    nav: NavigationRig, session: RecordingServiceSession, operation: str
) -> RecordingResponse:
    selection = nav.cursor.selection
    command = NavigateCommand(operation_id=operation, selection=selection, mode="continue-missing")
    session.submit(request(command, request_id=operation, take_id=selection.take_id))
    return await_responses(session)[0]


def checkpoint_path(nav: NavigationRig) -> Path:
    key = canonical_sha256(["synthetic-subject", nav.context.group.catalog_sha256])
    return nav.rig.layout.root / "provenance/recording-library-discovery" / f"{key}.json"


@pytest.fixture
def worker(nav: NavigationRig) -> Iterator[RecordingDiscoveryCoordinator]:
    # Only its owned child launcher is used; nothing is scheduled on its queue.
    coordinator = RecordingDiscoveryCoordinator(
        nav.rig.layout, index=nav.index, store=nav.rig.store
    )
    try:
        yield coordinator
    finally:
        coordinator.close()


def scan(
    nav: NavigationRig,
    worker: RecordingDiscoveryCoordinator,
    monkeypatch: pytest.MonkeyPatch,
    publications: list[DiscoveryPublication],
    *,
    after_begin: Callable[[], None] | None = None,
    progress: Callable[[str], None] | None = None,
    states: list[str] | None = None,
) -> dict[str, object]:
    """Run one real child; after_begin commits while that child waits for Begin's ACK.

    states receives the committed completeness after every applied publication.
    """

    def runner(
        command: DiscoveryRequest,
        inventory_fd: int,
        inventory: DiscoverySpoolRef | None,
        output_fd: int,
        publisher: DiscoveryPublisher,
    ) -> DiscoveryFinished:
        original = publisher.apply

        def publish(publication: DiscoveryPublication) -> bool:
            publications.append(publication)
            applied = original(publication)
            if states is not None:
                states.append(nav.index.history(nav.context.group.catalog_sha256).completeness)
            if isinstance(publication, DiscoveryBegin) and after_begin is not None:
                after_begin()
            return applied

        monkeypatch.setattr(publisher, "apply", publish)
        return worker._run_worker(command, inventory_fd, inventory, output_fd, publisher)

    return discover_recording_library(
        nav.rig.layout,
        nav.cursor.group_id,
        index=nav.index,
        store=nav.rig.store,
        runner=runner,
        progress=progress,
    )


def test_scan_overlapping_a_save_stays_building_until_a_stable_pass_certifies_complete(
    nav: NavigationRig,
    session: RecordingServiceSession,
    worker: RecordingDiscoveryCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    take = nav.capture()
    digest = take.reservation.group.catalog_sha256
    assert nav.index.history(digest).completeness == "complete"
    assert not checkpoint_path(nav).exists()
    held: list[DiscoveryPublication] = []
    saved = []

    def save_during_scan() -> None:
        assert nav.index.history(digest).completeness == "building"
        saved.append(nav.save(take, "save-during-scan").take)

    report = scan(nav, worker, monkeypatch, held, after_begin=save_during_scan)
    assert saved[0].decision == "kept" and isinstance(held[0], DiscoveryBegin)
    assert nav.index.history(digest).completeness == "building"
    # Only verified legacy freshness is kept; native evidence stays uncertified.
    deferred = _checkpoint(checkpoint_path(nav))
    assert deferred is not None and deferred.native_fingerprint is None
    assert deferred.legacy_fingerprint == _fingerprint(nav.rig.layout, legacy=True)
    assert report["completeness"] == "building"
    assert report.get("deferred") is True and report.get("native_evidence_changed") is True
    rejected = continue_missing(nav, session, "continue-after-changed-scan")
    assert rejected.error is not None and rejected.error.code == "RECORDING.HISTORY_UNKNOWN"

    stable: list[DiscoveryPublication] = []
    report = scan(nav, worker, monkeypatch, stable)
    assert isinstance(stable[0], DiscoveryBegin)
    assert report["completeness"] == "complete"
    assert "deferred" not in report and "native_evidence_changed" not in report
    checkpoint = _checkpoint(checkpoint_path(nav))
    assert checkpoint is not None and checkpoint.native_fingerprint is not None
    assert checkpoint.native_fingerprint == _fingerprint(nav.rig.layout, legacy=False)

    idle: list[DiscoveryPublication] = []
    before = nav.index.revision
    report = scan(nav, worker, monkeypatch, idle)
    assert idle == [] and report["completeness"] == "complete"
    assert nav.index.revision == before
    acknowledged = continue_missing(nav, session, "continue-after-stable-scan")
    assert acknowledged.error is None and isinstance(acknowledged.data, NavigationEvent)
    assert nav.index.history(digest).completeness == "complete"


def test_overlapped_retries_without_a_checkpoint_skip_legacy_and_never_show_complete(
    nav: NavigationRig,
    worker: RecordingDiscoveryCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = nav.context.group.catalog_sha256
    legacy = _fingerprint(nav.rig.layout, legacy=True)
    assert not checkpoint_path(nav).exists()

    def overlapped(
        operation: str,
    ) -> tuple[dict[str, object], list[DiscoveryPublication], list[str]]:
        take = nav.capture(operation)
        publications: list[DiscoveryPublication] = []
        states: list[str] = []

        def save() -> None:
            assert nav.save(take, "save-" + operation).take.decision == "kept"

        report = scan(nav, worker, monkeypatch, publications, after_begin=save, states=states)
        assert report["completeness"] == nav.index.history(digest).completeness == "building"
        assert report.get("deferred") is True and report.get("native_evidence_changed") is True
        checkpoint = _checkpoint(checkpoint_path(nav))
        assert checkpoint is not None and checkpoint.legacy_fingerprint == legacy
        assert checkpoint.native_fingerprint is None
        return report, publications, states

    # The first pass alone publishes legacy metadata (its interim Complete is a residual).
    report, publications, _ = overlapped("first")
    assert report["legacy_backfilled"] is True
    assert any(isinstance(item, DiscoveryLegacy) for item in publications)
    # A prompt retry over still-changing native evidence never repeats that work.
    report, publications, states = overlapped("retry")
    assert report["legacy_backfilled"] is False and report["native_reconciled"] is True
    assert not any(isinstance(item, DiscoveryLegacy) for item in publications)
    assert isinstance(publications[0], DiscoveryBegin) and states
    assert "complete" not in states, states

    # A certified checkpoint loses only its native certification to a later overlap.
    states = []
    report = scan(nav, worker, monkeypatch, [], states=states)
    assert report["completeness"] == "complete" and report["legacy_backfilled"] is False
    assert states and "complete" not in states
    certified = _checkpoint(checkpoint_path(nav))
    assert certified is not None and certified.native_fingerprint is not None
    report, _, states = overlapped("after-certified")
    assert report["legacy_backfilled"] is False and "complete" not in states, states


@pytest.mark.parametrize("change", ["unprojected", "invalid"])
@pytest.mark.serial
def test_actual_native_change_still_publishes_regression_before_applying(
    nav: NavigationRig,
    session: RecordingServiceSession,
    worker: RecordingDiscoveryCoordinator,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    indexed = nav.save(nav.capture("indexed"), "save-indexed").take
    digest = indexed.reservation.group.catalog_sha256
    assert scan(nav, worker, monkeypatch, [])["completeness"] == "complete"
    certified = checkpoint_path(nav).read_bytes()
    pending = None
    if change == "unprojected":
        # An out-of-band store change the owner's index has not observed.
        hooks = nav.rig.store.projection_hook, nav.rig.store.reservation_projection_hook
        nav.rig.store.projection_hook = nav.rig.store.reservation_projection_hook = None
        changed = nav.capture("unprojected").reservation.take_id
        nav.rig.store.projection_hook, nav.rig.store.reservation_projection_hook = hooks
        assert changed not in {entry.take_id for entry in nav.index.history(digest).takes}
    else:
        # Named invalid evidence stays Unknown (not a deferred retry) despite an overlapping Save.
        pending = nav.capture("pending")
        # The owner's inventory still names the removed take, so the child reports it invalid.
        shutil.rmtree(
            nav.rig.layout.root / "provenance/recording-takes" / indexed.reservation.take_id
        )
        changed = indexed.reservation.take_id
    publications: list[DiscoveryPublication] = []
    observed = []

    def begun(_: str) -> None:
        # Begin is committed; no reconciliation of the changed take has applied yet.
        assert publications == [DiscoveryBegin()]
        page = nav.index.history(digest)
        observed.append(page.completeness)
        if change == "unprojected":
            assert changed not in {entry.take_id for entry in page.takes}
        rejected = continue_missing(nav, session, "continue-during-" + change)
        assert rejected.error is not None and rejected.error.code == "RECORDING.HISTORY_UNKNOWN"

    def save_pending() -> None:
        if pending is not None:
            assert nav.save(pending, "save-during-invalid-scan").take.decision == "kept"

    report = scan(nav, worker, monkeypatch, publications, after_begin=save_pending, progress=begun)
    assert observed == ["building"]
    assert any(isinstance(item, DiscoveryInvalid) for item in publications) == (change == "invalid")
    assert "deferred" not in report and "native_evidence_changed" not in report
    after = continue_missing(nav, session, "continue-after-" + change)
    if change == "unprojected":
        assert report["completeness"] == "complete"
        assert changed in {entry.take_id for entry in nav.index.history(digest).takes}
        assert after.error is None and isinstance(after.data, NavigationEvent)
        assert checkpoint_path(nav).read_bytes() != certified
    else:
        assert report["completeness"] == "unknown" and nav.index.history(digest).errors
        assert after.error is not None and after.error.code == "RECORDING.HISTORY_UNKNOWN"
        assert checkpoint_path(nav).read_bytes() == certified


def malformed_completion(
    command: DiscoveryRequest,
    inventory_fd: int,
    inventory: DiscoverySpoolRef | None,
    output_fd: int,
    publisher: DiscoveryPublisher,
) -> DiscoveryFinished:
    """Begin, then claim success without inspected fingerprints, an error or deferral."""
    assert publisher.apply(DiscoveryBegin())
    return DiscoveryFinished()


def test_began_completion_without_legacy_fingerprint_fails_closed_to_unknown(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = nav.context.group.catalog_sha256
    assert nav.index.history(digest).completeness == "complete"
    assert not checkpoint_path(nav).exists()
    raised: list[Exception] = []

    def finish(*args: Any) -> dict[str, object]:
        try:
            return _finish_discovery(*args)
        except Exception as error:
            raised.append(error)
            raise

    monkeypatch.setattr("cole_voice_lab.recording_discovery._finish_discovery", finish)
    report = discover_recording_library(
        nav.rig.layout,
        nav.cursor.group_id,
        index=nav.index,
        store=nav.rig.store,
        runner=malformed_completion,
    )
    # Not mistaken for changed native evidence: a named failure, never a deferred retry.
    assert len(raised) == 1 and isinstance(raised[0], SecurityError)
    assert "inspected legacy fingerprint" in str(raised[0])
    assert report["completeness"] == "unknown"
    assert "deferred" not in report and "native_evidence_changed" not in report
    page = nav.index.history(digest)
    assert page.completeness == "unknown"
    assert [error.code for error in page.errors] == ["RECORDING.HISTORY_UNKNOWN"]
    assert not checkpoint_path(nav).exists()


def test_coordinator_runs_a_malformed_completion_once_without_requeue(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = nav.context.group.catalog_sha256
    group = nav.cursor.group_id
    runs = 0
    completed = threading.Event()

    def counted(*args: Any) -> DiscoveryFinished:
        nonlocal runs
        runs += 1
        return malformed_completion(*args)

    coordinator = RecordingDiscoveryCoordinator(
        nav.rig.layout,
        index=nav.index,
        store=nav.rig.store,
        completed=lambda _: completed.set(),
    )
    monkeypatch.setattr(coordinator, "_run_worker", counted)
    try:
        coordinator.schedule(group)
        assert completed.wait(10)
        # completed fires only after the supervisor requeued deferred work or released it.
        with coordinator._lock:
            assert group not in coordinator._pending
        time.sleep(5 * DISCOVERY_DEFERRED_RETRY_SECONDS)
        assert runs == 1
    finally:
        coordinator.close()
    page = nav.index.history(digest)
    assert page.completeness == "unknown"
    assert [error.code for error in page.errors] == ["RECORDING.HISTORY_UNKNOWN"]
    assert not checkpoint_path(nav).exists()

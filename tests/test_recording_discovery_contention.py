from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_recording_discovery import context
from test_recording_takes import Rig

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.errors import ContractError
from cole_voice_lab.recording_discovery import (
    MAX_DISCOVERY_QUEUE,
    RecordingDiscoveryCoordinator,
    _retry_background_metadata,
    discover_recording_library,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_takes import (
    RecordingMetadataBusyError,
    RecordingTakeStore,
)


@contextmanager
def held_metadata_lock(root: Path) -> Iterator[None]:
    descriptor = os.open(root / "provenance/recording-takes/.metadata.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def test_background_retries_actual_metadata_owner_then_completes(
    tmp_path: Path,
) -> None:
    layout, _, group = context(tmp_path)
    with ThreadPoolExecutor() as executor:
        with held_metadata_lock(layout.root):
            future = executor.submit(discover_recording_library, layout, group)
            time.sleep(0.08)
            assert not future.done()
        report = future.result(timeout=3)
    assert report["completeness"] == "complete" and "deferred" not in report


def test_contention_exhaustion_is_bounded_deferred_and_not_named_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, store, group = context(tmp_path)
    monkeypatch.setattr("cole_voice_lab.recording_discovery.DISCOVERY_METADATA_RETRY_SECONDS", 0.04)
    started = time.monotonic()
    with held_metadata_lock(layout.root):
        report = discover_recording_library(layout, group)
    assert time.monotonic() - started < 0.3
    assert report["completeness"] == "building" and report["deferred"] is True
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        page = index.history(store.get_group(group).catalog_sha256)
        assert not page.errors
    key = canonical_sha256(["synthetic-subject", store.get_group(group).catalog_sha256])
    assert not (layout.root / "provenance/recording-library-discovery" / f"{key}.json").exists()
    assert discover_recording_library(layout, group)["completeness"] == "complete"


def test_background_retries_only_the_typed_transient_error() -> None:
    attempts = 0

    def invalid() -> None:
        nonlocal attempts
        attempts += 1
        raise ContractError("immutable evidence is invalid")

    with pytest.raises(ContractError, match="immutable"):
        _retry_background_metadata(invalid)
    assert attempts == 1


def test_discovery_bootstraps_index_before_repairing_pending_projection(
    tmp_path: Path,
) -> None:
    layout, store, group = context(tmp_path)
    rig = Rig(layout, store, group)
    store.clock = lambda: rig.now
    take = rig.seal(rig.reserve())
    command = rig.command(take)

    def interrupt(boundary: str) -> None:
        if boundary == "before-projection":
            raise OSError("synthetic interruption after store head")

    store.fault_hook = interrupt
    with pytest.raises(OSError, match="synthetic interruption"):
        store.save(command)
    pending = layout.root / "provenance/recording-takes/.pending.json"
    assert pending.exists()
    assert discover_recording_library(layout, group)["completeness"] == "complete"
    assert not pending.exists()
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        page = index.history(take.reservation.group.catalog_sha256)
        assert page.takes[0].decision == "kept"
    assert RecordingTakeStore(layout).save(command).take.decision == "kept"


def test_discovery_only_uses_nonlocking_projection_pages_and_retries_pending_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, store, group = context(tmp_path)
    rig = Rig(layout, store, group)
    store.clock = lambda: rig.now
    sealed = rig.seal(rig.reserve())
    # Missing-evidence checking uses the parent's pre-discovery metadata snapshot.
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        index.update_take(sealed)
    original_page = RecordingTakeStore.iter_takes_for_projection
    original_take = RecordingTakeStore.get_take_for_projection
    attempts = {"page": 0, "take": 0}

    def page(self: RecordingTakeStore, **kwargs: Any) -> Any:
        attempts["page"] += 1
        if attempts["page"] == 1:
            raise RecordingMetadataBusyError("synthetic journal overlap")
        return original_page(self, **kwargs)

    def take(self: RecordingTakeStore, take_id: str) -> Any:
        attempts["take"] += 1
        if attempts["take"] == 1:
            raise RecordingMetadataBusyError("synthetic journal overlap")
        return original_take(self, take_id)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("background projection acquired an interactive metadata lock")

    monkeypatch.setattr(RecordingTakeStore, "iter_takes_for_projection", page)
    monkeypatch.setattr(RecordingTakeStore, "get_take_for_projection", take)
    monkeypatch.setattr(RecordingTakeStore, "iter_takes", forbidden)
    monkeypatch.setattr(RecordingTakeStore, "get_take", forbidden)
    report = discover_recording_library(layout, group)
    assert report["completeness"] == "complete"
    assert attempts["page"] >= 3 and attempts["take"] == 2


def test_owned_child_deferred_exit_requeues_promptly_then_completes(
    tmp_path: Path,
) -> None:
    layout, store, group = context(tmp_path)
    deferred = threading.Event()
    complete = threading.Event()
    reports: list[str] = []

    def completed(_: str) -> None:
        with RecordingIndex(layout, subject_id="synthetic-subject") as index:
            status = index.history(store.get_group(group).catalog_sha256).completeness
        reports.append(status)
        (complete if status == "complete" else deferred).set()

    coordinator = RecordingDiscoveryCoordinator(layout, completed=completed)
    try:
        with held_metadata_lock(layout.root):
            coordinator.schedule(group)
            assert deferred.wait(5)
        started = time.monotonic()
        assert complete.wait(5)
        assert time.monotonic() - started < 5
        assert reports[0] == "building" and reports[-1] == "complete"
    finally:
        coordinator.close()
    assert not coordinator._worker.is_alive()


def test_discovery_cooldown_state_has_a_hard_bound(tmp_path: Path) -> None:
    layout, _, group = context(tmp_path)
    done = threading.Event()
    coordinator = RecordingDiscoveryCoordinator(layout, completed=lambda _: done.set())
    try:
        with coordinator._lock:
            coordinator._scheduled = {f"context-{value}": time.monotonic() for value in range(40)}
        coordinator.schedule(group)
        assert len(coordinator._scheduled) == MAX_DISCOVERY_QUEUE
        assert group in coordinator._scheduled
        assert done.wait(5)
    finally:
        coordinator.close()


def test_deferred_constructor_preserves_genuine_unknown_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.recording_contracts import RecordingError

    layout, store, group = context(tmp_path)
    digest = store.get_group(group).catalog_sha256
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        index.register_catalog(store.get_group(group).catalog)
        index.mark_unknown(
            digest,
            "known-invalid-source",
            RecordingError(
                code="RECORDING.HISTORY_UNKNOWN", message="Known invalid source", retryable=False
            ),
        )
    monkeypatch.setattr("cole_voice_lab.recording_discovery.DISCOVERY_METADATA_RETRY_SECONDS", 0.01)
    with held_metadata_lock(layout.root):
        report = discover_recording_library(layout, group)
    assert report["completeness"] == "unknown" and report["deferred"] is True
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        page = index.history(digest)
        assert len(page.errors) == 1 and page.errors[0].message == "Known invalid source"


def test_pending_projection_exhaustion_does_not_invalidate_an_indexed_take(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, store, group = context(tmp_path)
    rig = Rig(layout, store, group)
    store.clock = lambda: rig.now
    take = rig.seal(rig.reserve())

    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        index.update_take(take)

    def pending(*args: Any, **kwargs: Any) -> Any:
        raise RecordingMetadataBusyError("synthetic pending projection")

    monkeypatch.setattr("cole_voice_lab.recording_discovery.DISCOVERY_METADATA_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(RecordingTakeStore, "get_take_for_projection", pending)
    report = discover_recording_library(layout, group)
    assert report["completeness"] == "building" and report["deferred"] is True
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        page = index.history(take.reservation.group.catalog_sha256)
        assert not page.errors and page.total_take_count == 1

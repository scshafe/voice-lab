from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from recording_fixtures import synthetic_catalog
from test_datasets import _corpus
from test_recording_takes import DEVICE, NOW, Rig

from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_discovery import (
    RecordingDiscoveryCoordinator,
    _fingerprint,
    discover_recording_library,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_takes import RecordingTakeOperation, RecordingTakeStore


def context(tmp_path: Path) -> tuple[AssetLayout, RecordingTakeStore, str]:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    store = RecordingTakeStore(layout, clock=lambda: NOW)
    group = store.open_group(synthetic_catalog(3), "synthetic-subject", DEVICE, "synthetic-host")
    return layout, store, group.group_id


def test_discovery_warm_checkpoint_and_native_churn_never_reopen_legacy_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, store, group = context(tmp_path)
    first = discover_recording_library(layout, group)
    assert first == dict(
        legacy_backfilled=True,
        native_reconciled=True,
        microphone_opened=False,
        completeness="complete",
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unchanged legacy history was reopened")

    monkeypatch.setattr("cole_voice_lab.recording_discovery.legacy_projection_records", forbidden)
    warm = discover_recording_library(layout, group)
    assert warm["completeness"] == "complete" and not warm["native_reconciled"]
    rig = Rig(layout, store, group)
    store.clock = lambda: rig.now
    take = rig.seal(rig.reserve())
    native = discover_recording_library(layout, group)
    assert native["completeness"] == "complete" and native["native_reconciled"]
    assert not native["legacy_backfilled"]
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        assert (
            index.history(take.reservation.group.catalog_sha256).takes[0].take_id
            == take.reservation.take_id
        )
        assert index.native_take_ids(limit=1) == (take.reservation.take_id,)
    assert not discover_recording_library(layout, group)["native_reconciled"]


def test_discovery_preserved_mtime_content_change_invalidates_legacy_checkpoint(
    tmp_path: Path,
) -> None:
    layout = _corpus(tmp_path.resolve() / "legacy").layout
    store = RecordingTakeStore(layout)
    group = store.open_group(synthetic_catalog(3), "synthetic-subject", DEVICE, "synthetic-host")
    assert discover_recording_library(layout, group.group_id)["completeness"] == "complete"
    before = _fingerprint(layout, legacy=True)
    source = next(layout.directory("raw").rglob("*.wav"))
    info = source.stat()
    source.chmod(0o600)
    data = bytearray(source.read_bytes())
    data[-1] ^= 1
    source.write_bytes(data)
    source.chmod(info.st_mode & 0o777)
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert _fingerprint(layout, legacy=True) != before
    report = discover_recording_library(layout, group.group_id)
    assert report["completeness"] == "unknown"


@pytest.mark.parametrize("hazard", ["symlink", "hardlink", "public", "fifo"])
def test_discovery_unsafe_source_never_becomes_complete(tmp_path: Path, hazard: str) -> None:
    layout, _, group = context(tmp_path)
    source = layout.directory("tmp") / "capture-unsafe"
    if hazard == "symlink":
        source.symlink_to(layout.directory("raw"), target_is_directory=True)
    else:
        source.mkdir(mode=0o700)
        entry = source / "entry.json"
        if hazard == "fifo":
            os.mkfifo(entry, 0o600)
        else:
            entry.touch(mode=0o600)
            if hazard == "hardlink":
                os.link(entry, source / "alias.json")
            else:
                entry.chmod(0o644)
    with pytest.raises(SecurityError):
        _fingerprint(layout, legacy=True)
    assert discover_recording_library(layout, group)["completeness"] == "unknown"


def test_missing_native_evidence_preserves_index_rows_with_named_unknown(tmp_path: Path) -> None:
    layout, store, group = context(tmp_path)
    rig = Rig(layout, store, group)
    store.clock = lambda: rig.now
    take = rig.seal(rig.reserve())
    assert discover_recording_library(layout, group)["completeness"] == "complete"
    (layout.root / "provenance/recording-takes" / take.reservation.take_id / "head.json").unlink()
    assert discover_recording_library(layout, group)["completeness"] == "unknown"
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        page = index.history(take.reservation.group.catalog_sha256)
        assert page.errors and page.total_take_count == 1


def test_pre_extension_operation_bytes_and_reservation_replay_survive(tmp_path: Path) -> None:
    layout, store, group = context(tmp_path)
    rig = Rig(layout, store, group)
    reservation = rig.reserve()
    path = layout.root / "provenance/recording-takes/.operations/reserve-1.json"
    payload = path.read_bytes()
    import json

    original = json.loads(payload)
    assert set(original) == {
        "schema_version",
        "operation_id",
        "request_sha256",
        "kind",
        "store_revision",
        "take",
        "save",
        "committed_at",
    }
    assert original["request_sha256"] == canonical_sha256(
        ["reserve", "reserve-1", group, "synthetic-0000", "helper-generation"]
    )
    assert (
        canonical_json_bytes(RecordingTakeOperation.model_validate_json(payload)) + b"\n" == payload
    )
    reopened = RecordingTakeStore(layout)
    assert Rig(layout, reopened, group).reserve() == reservation
    assert path.read_bytes() == payload


def test_discovery_coordinator_deduplicates_and_notifies_once(tmp_path: Path) -> None:
    layout, _, group = context(tmp_path)
    complete = threading.Event()
    groups = []

    def done(value: str) -> None:
        groups.append(value)
        complete.set()

    coordinator = RecordingDiscoveryCoordinator(layout, completed=done)
    try:
        for _ in range(20):
            coordinator.schedule(group)
        assert complete.wait(10)
        assert groups == [group]
        assert len(coordinator._scheduled) == 1
    finally:
        coordinator.close()
    assert not coordinator._worker.is_alive()


def test_discovery_child_parent_loss_exits_without_running_audio(tmp_path: Path) -> None:
    layout, _, group = context(tmp_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cole_voice_lab.recording_discovery",
            "--asset-root",
            str(layout.root),
            "--group-id",
            group,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    started = time.monotonic()
    process.stdin.close()
    assert process.wait(timeout=5) == 0
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("problem", ["public", "noncanonical", "oversize"])
def test_discovery_checkpoint_failure_is_explicit_unknown(tmp_path: Path, problem: str) -> None:
    layout, store, group = context(tmp_path)
    assert discover_recording_library(layout, group)["completeness"] == "complete"
    key = canonical_sha256(["synthetic-subject", store.get_group(group).catalog_sha256])
    path = layout.root / "provenance/recording-library-discovery" / f"{key}.json"
    if problem == "public":
        path.chmod(0o644)
    elif problem == "noncanonical":
        path.write_bytes(b" " + path.read_bytes())
    else:
        path.write_bytes(b"x" * 4097)
    assert discover_recording_library(layout, group)["completeness"] == "unknown"


def test_discovery_process_launch_failure_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, _, group = context(tmp_path)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("synthetic process launch unavailable")

    monkeypatch.setattr("cole_voice_lab.recording_discovery.subprocess.Popen", unavailable)
    done = threading.Event()
    coordinator = RecordingDiscoveryCoordinator(layout, completed=lambda _: done.set())
    coordinator.schedule(group)
    assert done.wait(2)
    assert coordinator._worker.is_alive() and not coordinator._pending
    coordinator.close()

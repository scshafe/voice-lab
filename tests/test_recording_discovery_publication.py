"""Discovery workers prepare evidence; only the existing owner publishes metadata."""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from recording_fixtures import synthetic_catalog
from test_recording_discovery import context
from test_recording_index import native_take
from test_recording_takes import Rig

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import RecordingError, RecordingHistoryEntry
from cole_voice_lab.recording_discovery import (
    DiscoveryPublisher,
    RecordingDiscoveryCoordinator,
    discover_recording_library,
)
from cole_voice_lab.recording_discovery_contracts import (
    DiscoveryBegin,
    DiscoveryInvalid,
    DiscoveryLegacy,
    DiscoveryNative,
    DiscoveryRequest,
    LegacyProjectionHeader,
    LegacyProjectionRow,
)
from cole_voice_lab.recording_discovery_spool import spool_records
from cole_voice_lab.recording_index import (
    RecordingIndex,
    RecordingLegacyPublicationCapacityError,
    history_entry_for_take,
)
from cole_voice_lab.recording_index_backfill import publish_legacy_spool


def legacy_entry() -> RecordingHistoryEntry:
    entry = history_entry_for_take(native_take(synthetic_catalog(3), 0))
    return RecordingHistoryEntry.model_validate(
        entry.model_dump()
        | {
            "take_id": "legacy-synthetic",
            "group_id": None,
            "legacy_session_id": "synthetic-session",
            "state_revision": 0,
        }
    )


def error(message: str = "Unknown source") -> RecordingError:
    return RecordingError(code="RECORDING.HISTORY_UNKNOWN", message=message, retryable=False)


def request(index: RecordingIndex) -> DiscoveryRequest:
    return DiscoveryRequest(
        generation="synthetic-generation",
        group_id="synthetic-group",
        subject_id=index.subject_id,
        catalog_sha256=index.register_catalog(synthetic_catalog(3)),
        index_instance=index.instance_id,
        legacy_token=index.legacy_publication_token(),
    )


def test_discovery_producer_cannot_open_another_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, store, group = context(tmp_path)
    rig = Rig(layout, store, group)
    store.clock = lambda: rig.now
    take = rig.seal(rig.reserve())
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("discovery attempted to construct another index")

        monkeypatch.setattr("cole_voice_lab.recording_discovery.RecordingIndex", forbidden)
        monkeypatch.setattr("sqlite3.connect", forbidden)
        report = discover_recording_library(layout, group, index=index, store=store)
        assert report["completeness"] == "complete"
        assert index.history(take.reservation.group.catalog_sha256).total_take_count == 1


def test_cancelled_discovery_preserves_complete_metadata_without_source_error(
    tmp_path: Path,
) -> None:
    layout, store, group = context(tmp_path)
    with RecordingIndex(layout, subject_id="synthetic-subject") as index:
        assert (
            discover_recording_library(layout, group, index=index, store=store)["completeness"]
            == "complete"
        )
        stop = threading.Event()
        stop.set()
        report = discover_recording_library(layout, group, index=index, store=store, stop=stop)
        assert report["deferred"] is True and report["completeness"] == "complete"
        assert not index.history(store.get_group(group).catalog_sha256).errors


def test_close_cannot_release_the_owner_index_while_its_publisher_is_still_alive() -> None:
    coordinator = RecordingDiscoveryCoordinator.__new__(RecordingDiscoveryCoordinator)
    coordinator._stop = threading.Event()
    coordinator._worker = Mock(spec=threading.Thread)
    coordinator._worker.is_alive.return_value = True
    with pytest.raises(RuntimeError, match="index owner shutdown"):
        coordinator.close()
    assert coordinator._stop.is_set()
    coordinator._worker.join.assert_called_once_with(1.5)


def test_prepared_legacy_iteration_and_serialization_happen_before_index_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_index as module

    layout = AssetLayout.initialize(tmp_path / "assets")
    entry = legacy_entry()
    with RecordingIndex(layout) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        original = module.canonical_json_bytes

        def encoded(value: object) -> bytes:
            assert not index.connection.in_transaction
            return original(value)

        def rows() -> Iterator[tuple[RecordingHistoryEntry, None, tuple[()]]]:
            assert not index.connection.in_transaction
            yield entry, None, ()
            assert not index.connection.in_transaction

        monkeypatch.setattr(module, "canonical_json_bytes", encoded)
        index.replace_legacy_catalog(catalog, rows())
        monkeypatch.setattr(module, "canonical_json_bytes", original)
        assert index.history(catalog).takes == (entry,)


@pytest.mark.parametrize("change", ["remove", "external"])
def test_stale_legacy_publication_preserves_newer_decisions(tmp_path: Path, change: str) -> None:
    layout = AssetLayout.initialize(tmp_path / "assets")
    entry = legacy_entry()
    with RecordingIndex(layout) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        index.replace_legacy_catalog(catalog, ((entry, None, ()),))
        token = index.legacy_publication_token()
        newer = entry.model_copy(update={"decision": "removed", "state_revision": 1})
        if change == "remove":
            index.update_legacy_review(newer)
        else:
            with RecordingIndex(layout) as other:
                other.update_legacy_review(newer)
        assert (
            index.replace_legacy_catalog_if_current(
                catalog, ((entry, None, ()),), expected_token=token
            )
            is None
        )
        assert index.history(catalog).takes == (newer,)


def test_legacy_publication_preserves_unrelated_unknown_and_native_updates(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "assets")
    with RecordingIndex(layout) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        token = index.legacy_publication_token()
        index.update_take(native_take(synthetic_catalog(3), 1))
        index.mark_unknown(catalog, "native-source", error("Native source remains unknown"))
        index.mark_unknown(catalog, "legacy-backfill", error("Old legacy error"))
        assert (
            index.replace_legacy_catalog_if_current(catalog, (), expected_token=token) is not None
        )
        page = index.history(catalog)
        assert page.total_take_count == 1 and page.completeness == "unknown"
        assert [item.message for item in page.errors] == ["Native source remains unknown"]


@pytest.mark.parametrize("bound", ["_MAX_PREPARED_LEGACY_BYTES", "_MAX_PREPARED_LEGACY_RECORDS"])
def test_preparation_capacity_failure_preserves_previous_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    layout = AssetLayout.initialize(tmp_path / "assets")
    entry = legacy_entry()
    with RecordingIndex(layout) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        index.replace_legacy_catalog(catalog, ((entry, None, ()),))
        before = index.history(catalog)
        monkeypatch.setattr(f"cole_voice_lab.recording_index.{bound}", 0)
        with pytest.raises(RecordingLegacyPublicationCapacityError, match="capacity"):
            index.replace_legacy_catalog(catalog, ((entry, None, ()),))
        assert index.history(catalog) == before


def test_conditional_missing_native_evidence_cannot_invalidate_a_newer_save(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "assets")
    take = native_take(synthetic_catalog(3), 0, revision=1)
    with (
        RecordingIndex(layout) as index,
        tempfile.TemporaryFile(dir=layout.directory("tmp")) as spool,
    ):
        index.update_take(take)
        publisher = DiscoveryPublisher(index, request(index), spool.fileno())
        publisher.apply(DiscoveryBegin())
        newer = take.model_copy(update={"state_revision": 2})
        index.update_take(newer)
        assert not publisher.apply(
            DiscoveryInvalid(
                take_id=take.reservation.take_id, expected_state_revision=1, error=error()
            )
        )
        page = index.history(canonical_sha256(synthetic_catalog(3)))
        assert not page.errors and page.takes[0].state_revision == 2


def test_spool_digest_and_range_replay_fail_before_publication(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "assets")
    with (
        RecordingIndex(layout) as index,
        tempfile.TemporaryFile(dir=layout.directory("tmp")) as spool,
    ):
        publisher = DiscoveryPublisher(index, request(index), spool.fileno())
        publisher.apply(DiscoveryBegin())
        source = spool_records(spool.fileno(), (native_take(synthetic_catalog(3), 0),))
        assert publisher.apply(DiscoveryNative(source=source))
        with pytest.raises(SecurityError, match="contiguous"):
            publisher.apply(DiscoveryNative(source=source))
        source = spool_records(spool.fileno(), (native_take(synthetic_catalog(3), 1),))
        os.pwrite(spool.fileno(), b"!", source.offset + 5)
        with pytest.raises(SecurityError, match="digest"):
            publisher.apply(DiscoveryNative(source=source))
        assert index.history(publisher.request.catalog_sha256).total_take_count == 1


@pytest.mark.parametrize("header", ["absent", "duplicate", "wrong-catalog"])
def test_legacy_spool_requires_one_exact_header(tmp_path: Path, header: str) -> None:
    layout = AssetLayout.initialize(tmp_path / "assets")
    with (
        RecordingIndex(layout) as index,
        tempfile.TemporaryFile(dir=layout.directory("tmp")) as spool,
    ):
        catalog = index.register_catalog(synthetic_catalog(3))
        row = LegacyProjectionRow(entry=legacy_entry(), opened_at=None, aliases=())
        correct = LegacyProjectionHeader(catalog_sha256=catalog)
        values = (
            (row,)
            if header == "absent"
            else (correct, correct)
            if header == "duplicate"
            else (LegacyProjectionHeader(catalog_sha256="0" * 64),)
        )
        source = spool_records(spool.fileno(), values)
        with pytest.raises(SecurityError, match="header"):
            publish_legacy_spool(
                index, catalog, spool.fileno(), source, index.legacy_publication_token()
            )
        assert index.history(catalog).total_take_count == 0


def test_legacy_staged_result_stays_contiguous_after_native_alias_stream(tmp_path: Path) -> None:
    from cole_voice_lab.recording_discovery_spool import append_spool

    layout = AssetLayout.initialize(tmp_path / "assets")
    with (
        RecordingIndex(layout) as index,
        tempfile.TemporaryFile(dir=layout.directory("tmp")) as output,
        tempfile.TemporaryFile(dir=layout.directory("tmp")) as stage,
    ):
        publisher = DiscoveryPublisher(index, request(index), output.fileno())
        publisher.apply(DiscoveryBegin())
        native = spool_records(output.fileno(), (native_take(synthetic_catalog(3), 0),))
        assert publisher.apply(DiscoveryNative(source=native))
        legacy = spool_records(
            stage.fileno(),
            (LegacyProjectionHeader(catalog_sha256=publisher.request.catalog_sha256),),
        )
        copied = append_spool(output.fileno(), stage.fileno(), legacy)
        assert copied.offset == native.size_bytes
        assert publisher.apply(
            DiscoveryLegacy(source=copied, expected_token=publisher.request.legacy_token)
        )
        assert index.history(publisher.request.catalog_sha256).total_take_count == 1

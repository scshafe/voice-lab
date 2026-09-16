"""Projection readers cannot steal the canonical service owner's journal or lock."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_recording_takes import Rig
from test_recording_takes import rig as rig

from cole_voice_lab.errors import ContractError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_takes import RecordingMetadataBusyError, RecordingTakeStore


def test_read_only_constructor_does_not_create_evidence_directories(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    before = set(layout.root.rglob("*"))
    with pytest.raises(RecordingMetadataBusyError, match="service owner"):
        RecordingTakeStore(layout, read_only=True)
    assert set(layout.root.rglob("*")) == before


def test_read_only_constructor_and_reads_do_not_acquire_service_lock(rig: Rig) -> None:
    take = rig.seal(rig.reserve())
    with rig.store._metadata_lock():
        reader = RecordingTakeStore(rig.layout, read_only=True)
        assert reader.get_take(take.reservation.take_id) == take
        assert reader.analysis_result("a" * 64) is None
        assert rig.store.analysis_result_for_projection("a" * 64) is None


def test_read_only_store_cannot_repair_committed_projection_before_owner(rig: Rig) -> None:
    take = rig.seal(rig.reserve())

    def unavailable(_take: object) -> None:
        raise OSError("synthetic projection interruption")

    rig.store.projection_hook = unavailable
    with pytest.raises(OSError, match="projection interruption"):
        rig.store.save(rig.command(take))
    pending = rig.layout.root / "provenance/recording-takes/.pending.json"
    original = pending.read_bytes()
    with pytest.raises(RecordingMetadataBusyError):
        RecordingTakeStore(rig.layout, read_only=True)
    assert pending.read_bytes() == original
    projected = []
    owner = RecordingTakeStore(rig.layout, projection_hook=projected.append)
    assert projected[-1] == owner.get_take(take.reservation.take_id)
    assert not pending.exists()


def test_read_only_store_rejects_canonical_mutation(rig: Rig) -> None:
    reader = RecordingTakeStore(rig.layout, read_only=True)
    with pytest.raises(ContractError, match="read-only"):
        reader.reserve(
            operation_id="read-only-reserve",
            group_id=rig.group_id,
            prompt_id="synthetic-0000",
            process_generation="synthetic-helper",
        )


def test_projection_analysis_read_defers_pending_commit(rig: Rig) -> None:
    pending = rig.layout.root / "provenance/recording-takes/.pending.json"
    pending.write_bytes(b"synthetic unfinished commit")
    pending.chmod(0o600)
    with pytest.raises(RecordingMetadataBusyError):
        rig.store.analysis_result_for_projection("a" * 64)


def test_current_reservation_projection_precedes_ack_and_repair_is_historical(rig: Rig) -> None:
    seen = []
    pending = rig.layout.root / "provenance/recording-takes/.pending.json"

    def live_projection(take: object) -> None:
        assert pending.exists()
        seen.append(take)
        raise OSError("synthetic after live projection")

    rig.store.reservation_projection_hook = live_projection
    with pytest.raises(OSError, match="live projection"):
        rig.store.reserve(
            operation_id="live-reserve",
            group_id=rig.group_id,
            prompt_id="synthetic-0000",
            process_generation="synthetic-helper",
            current_session_reservation=True,
        )
    historical = []
    owner = RecordingTakeStore(
        rig.layout, projection_hook=historical.append, reservation_projection_hook=live_projection
    )
    assert len(seen) == 1 and historical == seen
    assert owner.state_revision == 1 and not pending.exists()


def test_current_reservation_requires_hook_before_any_evidence(rig: Rig) -> None:
    before = set(rig.layout.root.rglob("*"))
    with pytest.raises(ContractError, match="owned projection hook"):
        rig.store.reserve(
            operation_id="unowned-reserve",
            group_id=rig.group_id,
            prompt_id="synthetic-0000",
            process_generation="synthetic-helper",
            current_session_reservation=True,
        )
    assert set(rig.layout.root.rglob("*")) == before


def test_live_reserve_retry_restores_protection_after_historical_repair(rig: Rig) -> None:
    historical = []
    protected = []
    rig.store.projection_hook = historical.append

    def interrupted(_take: object) -> None:
        raise OSError("live projection unavailable")

    rig.store.reservation_projection_hook = interrupted
    request = {
        "operation_id": "interrupted-live-reserve",
        "group_id": rig.group_id,
        "prompt_id": "synthetic-0000",
        "process_generation": "synthetic-helper",
        "current_session_reservation": True,
    }
    with pytest.raises(OSError, match="live projection unavailable"):
        rig.store.reserve(**request)
    rig.store.reservation_projection_hook = protected.append
    reservation = rig.store.reserve(**request)
    assert len(historical) == 2 and len(protected) == 1
    assert protected[0] == historical[0] == historical[1] == rig.store.get_take(reservation.take_id)
    assert rig.store.state_revision == 1


def test_live_reserve_replay_does_not_reprotect_a_later_sealed_take(rig: Rig) -> None:
    protected = []
    rig.store.reservation_projection_hook = protected.append
    reservation = rig.store.reserve(
        operation_id="already-sealed-live-reserve",
        group_id=rig.group_id,
        prompt_id="synthetic-0000",
        process_generation="synthetic-helper",
        current_session_reservation=True,
    )
    sealed = rig.seal(reservation)
    assert (
        rig.store.reserve(
            operation_id="already-sealed-live-reserve",
            group_id=rig.group_id,
            prompt_id="synthetic-0000",
            process_generation="synthetic-helper",
            current_session_reservation=True,
        )
        == reservation
    )
    assert len(protected) == 1
    assert rig.store.get_take(reservation.take_id) == sealed


def test_recovery_completion_barrier_does_not_enumerate_history(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.seal(rig.reserve())
    revision = rig.store.state_revision

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("historical read in recovery completion barrier")

    monkeypatch.setattr(rig.store, "_get_take", forbidden)
    monkeypatch.setattr(rig.store, "_take_ids", forbidden)
    with rig.store.recovery_projection_barrier() as covered:
        assert covered == revision

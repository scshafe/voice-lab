"""Inventory snapshots and race guards share the existing service index connection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from test_recording_takes import Rig
from test_recording_takes import rig as rig

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.recording_contracts import (
    CatalogSnapshot,
    RecordingError,
    RecordingRecoveryObservation,
    RecordingRecoveryWork,
)
from cole_voice_lab.recording_index import (
    RecordingIndex,
    RecordingIndexError,
    recording_index_migration,
)

SUBJECT = "synthetic-subject"
GENERATION = "recovery-synthetic-one"
ERROR = RecordingError(
    code="RECORDING.SOURCE_CHANGED", message="Synthetic preserved head unavailable", retryable=True
)


@pytest.fixture
def inventory(rig: Rig) -> Iterator[RecordingIndex]:
    with RecordingIndex(rig.layout, subject_id=SUBJECT) as index:
        index.register_catalog(CatalogSnapshot.embedded(rig.store.get_group(rig.group_id).catalog))
        index.begin_recovery_scan(scan_generation=GENERATION)
        rig.store.projection_hook = index.update_take
        rig.store.reservation_projection_hook = lambda take: index.update_take(
            take, current_session_reservation=True
        )
        yield index


def observation(
    take_id: str,
    *,
    revision: int | None = 0,
    subject: str | None = SUBJECT,
    status: str = "partial",
    recovered: bool = False,
) -> RecordingRecoveryObservation:
    return RecordingRecoveryObservation.model_validate(
        {
            "result": {
                "take_id": take_id,
                "status": status,
                "recovered_seal": recovered,
                "error": ERROR.model_dump() if status == "unavailable" else None,
            },
            "observed_take_revision": revision,
            "subject_id": subject,
        }
    )


def claim(index: RecordingIndex, take_id: str, *, explicit: bool = False) -> RecordingRecoveryWork:
    work = index.claim_recovery_work(take_id, explicit=explicit)
    assert work is not None
    return work


def test_empty_scan_is_not_complete_until_current_store_barrier(
    inventory: RecordingIndex, rig: Rig
) -> None:
    page = inventory.recovery_snapshot()
    assert page.scan_state == "scanning" and not page.enumeration_complete
    with rig.store.recovery_projection_barrier() as revision:
        assert inventory.finish_recovery_scan(covered_store_revision=revision)
    page = inventory.recovery_snapshot()
    assert page.scan_state == "complete" and page.covered_store_revision == revision
    assert not inventory.pending_recovery_take_ids()


def test_live_reservation_is_excluded_even_from_a_preexisting_claim(
    inventory: RecordingIndex, rig: Rig
) -> None:
    operation = "live-reservation"
    take_id = "take-" + canonical_sha256(operation)
    earlier = claim(inventory, take_id)
    reserved = rig.store.reserve(
        operation_id=operation,
        group_id=rig.group_id,
        prompt_id="synthetic-0000",
        process_generation="helper-generation",
        current_session_reservation=True,
    )
    assert inventory.claim_recovery_work(reserved.take_id) is None
    assert not inventory.pending_recovery_take_ids()
    assert not inventory.publish_recovery_work(earlier, observation(take_id, status="empty"))
    assert not inventory.finish_recovery_scan(covered_store_revision=rig.store.state_revision)
    assert inventory.recovery_snapshot().entries[0].pending


def test_explicit_recheck_supersedes_protected_reservation(
    inventory: RecordingIndex, rig: Rig
) -> None:
    reserved = rig.store.reserve(
        operation_id="live-recheck",
        group_id=rig.group_id,
        prompt_id="synthetic-0000",
        process_generation="helper-generation",
        current_session_reservation=True,
    )
    work = claim(inventory, reserved.take_id, explicit=True)
    assert inventory.publish_recovery_work(work, observation(reserved.take_id, status="empty"))
    assert inventory.finish_recovery_scan(covered_store_revision=rig.store.state_revision)
    page = inventory.recovery_snapshot()
    assert page.scan_state == "complete" and page.entries[0].result.status == "empty"


def test_recovering_own_seal_can_publish_after_canonical_projection(
    inventory: RecordingIndex, rig: Rig
) -> None:
    reservation = rig.reserve()
    work = claim(inventory, reservation.take_id)
    take = rig.seal(reservation)
    assert inventory.publish_recovery_work(
        work,
        observation(
            reservation.take_id, revision=take.state_revision, status="draft", recovered=True
        ),
    )
    page = inventory.recovery_snapshot()
    assert page.recovered_seal_count == 1 and page.entries[0].result.recovered_seal
    saved = rig.store.save(rig.command(take)).take
    page = inventory.recovery_snapshot()
    assert page.entries[0].observed_take_revision == saved.state_revision
    assert page.entries[0].result.status == "kept"


def test_old_observation_cannot_override_new_save(inventory: RecordingIndex, rig: Rig) -> None:
    reservation = rig.reserve()
    take = rig.seal(reservation)
    work = claim(inventory, reservation.take_id, explicit=True)
    saved = rig.store.save(rig.command(take)).take
    assert not inventory.publish_recovery_work(
        work,
        observation(
            reservation.take_id, revision=take.state_revision, status="draft", recovered=True
        ),
    )
    assert inventory.recovery_snapshot().entries == ()
    assert inventory.native_take_versions() == ((reservation.take_id, saved.state_revision),)


def test_generation_and_newer_recheck_reject_old_tokens(inventory: RecordingIndex) -> None:
    old = claim(inventory, "take-token")
    assert inventory.claim_recovery_work(old.take_id) is None
    newer = claim(inventory, old.take_id, explicit=True)
    assert not inventory.publish_recovery_work(old, observation(old.take_id))
    assert not inventory.discard_recovery_work(old)
    inventory.begin_recovery_scan(scan_generation="recovery-synthetic-two")
    assert not inventory.publish_recovery_work(newer, observation(newer.take_id))
    current = claim(inventory, old.take_id)
    assert current.token > newer.token
    assert inventory.publish_recovery_work(current, observation(current.take_id))
    assert inventory.claim_recovery_work(current.take_id) is None


def test_other_subject_is_discarded_and_unbound_damage_is_scan_error(
    inventory: RecordingIndex,
) -> None:
    foreign = claim(inventory, "take-foreign")
    assert inventory.publish_recovery_work(
        foreign, observation(foreign.take_id, subject="other-subject")
    )
    assert inventory.recovery_snapshot().finding_count == 0
    unknown = claim(inventory, "take-unbound")
    assert inventory.publish_recovery_work(
        unknown, observation(unknown.take_id, revision=None, subject=None, status="unavailable")
    )
    page = inventory.recovery_snapshot()
    assert page.scan_state == "error" and page.error == ERROR
    assert not inventory.finish_recovery_scan(covered_store_revision=0)


def test_unreadable_known_take_remains_an_attributed_finding(
    inventory: RecordingIndex, rig: Rig
) -> None:
    reservation = rig.reserve()
    work = claim(inventory, reservation.take_id)
    assert inventory.publish_recovery_work(
        work, observation(work.take_id, revision=None, subject=None, status="unavailable")
    )
    page = inventory.recovery_snapshot()
    assert page.scan_state == "scanning" and page.entries[0].result.error == ERROR


def test_intervening_revision_rejects_unversioned_observation(
    inventory: RecordingIndex, rig: Rig
) -> None:
    reservation = rig.reserve()
    work = claim(inventory, reservation.take_id)
    rig.seal(reservation)
    assert not inventory.publish_recovery_work(
        work, observation(work.take_id, revision=None, subject=None, status="unavailable")
    )


def test_new_reservation_after_completion_reopens_inventory(
    inventory: RecordingIndex, rig: Rig
) -> None:
    assert inventory.finish_recovery_scan(covered_store_revision=rig.store.state_revision)
    reservation = rig.reserve()
    page = inventory.recovery_snapshot()
    assert page.scan_state == "scanning" and page.covered_store_revision is None
    assert inventory.pending_recovery_take_ids() == (reservation.take_id,)
    inventory.begin_recovery_scan(scan_generation="recovery-reopened")
    assert inventory.recovery_snapshot().entries[0].pending


def test_two_thousand_findings_are_paged_without_history_revision_churn(
    inventory: RecordingIndex,
) -> None:
    for number in range(2001):
        take_id = f"take-inventory-{number:04d}"
        assert inventory.publish_recovery_work(claim(inventory, take_id), observation(take_id))
    inventory.finish_recovery_scan(covered_store_revision=0)
    cursor = None
    seen = []
    while True:
        page = inventory.recovery_snapshot(cursor=cursor, page_size=50)
        assert len(page.entries) <= 50 and page.finding_count == 2001
        seen.extend(entry.take_id for entry in page.entries)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 2001
    cursor = inventory.recovery_snapshot(page_size=1).next_cursor
    inventory.mark_unknown(
        inventory.connection.execute(
            "SELECT recording_catalog_sha256 FROM recording_catalogs LIMIT 1"
        ).fetchone()[0],
        "synthetic-history",
        ERROR,
    )
    assert inventory.recovery_snapshot(cursor=cursor, page_size=1).entries
    claim(inventory, "take-new")
    with pytest.raises(RecordingIndexError, match="changed snapshot"):
        inventory.recovery_snapshot(cursor=cursor)


@pytest.mark.parametrize("value", [0, 51, True])
def test_inventory_bounds_are_enforced(inventory: RecordingIndex, value: int) -> None:
    with pytest.raises(RecordingIndexError):
        inventory.recovery_snapshot(page_size=value)
    with pytest.raises(RecordingIndexError):
        inventory.pending_recovery_take_ids(limit=value)


def test_wrong_work_identity_and_invalid_completion_are_rejected(inventory: RecordingIndex) -> None:
    work = claim(inventory, "take-one")
    with pytest.raises(RecordingIndexError, match="different claimed take"):
        inventory.publish_recovery_work(work, observation("take-two"))
    with pytest.raises(RecordingIndexError):
        inventory.finish_recovery_scan(covered_store_revision=-1)
    with pytest.raises(RecordingIndexError):
        inventory.recovery_snapshot(cursor="corrupt.cursor")
    assert inventory.discard_recovery_work(work)
    assert not inventory.discard_recovery_work(work)


def test_owned_inventory_publications_do_not_trigger_full_index_audit(
    inventory: RecordingIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("owned publication triggered historical audit")

    monkeypatch.setattr(inventory, "_audit", forbidden)
    work = claim(inventory, "take-owned")
    assert inventory.publish_recovery_work(work, observation(work.take_id))
    inventory.finish_recovery_scan(covered_store_revision=0)
    assert inventory.recovery_snapshot().scan_state == "complete"


def test_external_result_tampering_still_triggers_integrity_failure(
    inventory: RecordingIndex,
) -> None:
    work = claim(inventory, "take-tamper")
    assert inventory.publish_recovery_work(work, observation(work.take_id))
    with sqlite3.connect(inventory.path) as external:
        external.execute(
            "UPDATE recording_recovery_findings SET recording_recovery_result_sha256=?", ("0" * 64,)
        )
    with pytest.raises(RecordingIndexError, match="binding changed"):
        inventory.recovery_snapshot()


def test_complete_inventory_cannot_hide_pending_tampering(inventory: RecordingIndex) -> None:
    work = claim(inventory, "take-pending-tamper")
    assert inventory.publish_recovery_work(work, observation(work.take_id))
    assert inventory.finish_recovery_scan(covered_store_revision=0)
    with sqlite3.connect(inventory.path) as external:
        external.execute("UPDATE recording_recovery_findings SET recording_recovery_pending=1")
    with pytest.raises(RecordingIndexError, match="complete recovery inventory"):
        inventory.recovery_snapshot()


def test_inventory_migration_is_disposable_and_preserves_history(
    inventory: RecordingIndex, rig: Rig
) -> None:
    take = rig.seal(rig.reserve())
    inventory.connection.executescript(recording_index_migration("down", version=3))
    inventory.connection.executescript(recording_index_migration(version=3))
    assert inventory.native_take_ids() == (take.reservation.take_id,)
    with pytest.raises(RecordingIndexError, match="has not started"):
        inventory.recovery_snapshot()


def test_transient_claim_release_preserves_finding_and_allows_retry(
    inventory: RecordingIndex,
) -> None:
    work = claim(inventory, "take-busy-retry")
    assert not inventory.pending_recovery_take_ids()
    assert inventory.release_recovery_work(work)
    assert not inventory.release_recovery_work(work)
    assert not inventory.publish_recovery_work(work, observation(work.take_id))
    assert not inventory.discard_recovery_work(work)
    assert inventory.pending_recovery_take_ids() == (work.take_id,)
    retry = claim(inventory, work.take_id)
    assert not inventory.release_recovery_work(work)
    assert inventory.publish_recovery_work(retry, observation(retry.take_id))
    assert not inventory.publish_recovery_work(retry, observation(retry.take_id))
    assert not inventory.discard_recovery_work(retry)
    assert inventory.recovery_snapshot().entries[0].result.status == "partial"


@pytest.mark.parametrize(
    "assignment",
    [
        "recording_recovery_claimed_generation='other-generation'",
        "recording_recovery_classified_generation='other-generation'",
        "recording_recovery_work_token=999999",
    ],
)
def test_external_queue_control_tampering_fails_closed(
    inventory: RecordingIndex, assignment: str
) -> None:
    claim(inventory, "take-corrupt-control")
    with sqlite3.connect(inventory.path) as external:
        external.execute("UPDATE recording_recovery_findings SET " + assignment)
    with pytest.raises(RecordingIndexError, match="generation/token binding"):
        inventory.recovery_snapshot()


def test_restart_invalidates_claim_even_if_caller_reuses_generation(
    inventory: RecordingIndex,
) -> None:
    work = claim(inventory, "take-restart-token")
    inventory.begin_recovery_scan(scan_generation=GENERATION)
    assert not inventory.publish_recovery_work(work, observation(work.take_id))
    newer = claim(inventory, work.take_id)
    assert newer.token > work.token


def test_bulk_recheck_does_not_remove_current_reservation_protection(
    inventory: RecordingIndex, rig: Rig
) -> None:
    reservation = rig.store.reserve(
        operation_id="live-bulk-recheck",
        group_id=rig.group_id,
        prompt_id="synthetic-0000",
        process_generation="helper-generation",
        current_session_reservation=True,
    )
    before = inventory.recovery_snapshot()
    assert (
        inventory.claim_recovery_work(
            reservation.take_id, explicit=True, allow_current_session_reservation=False
        )
        is None
    )
    assert inventory.recovery_snapshot() == before
    assert not inventory.pending_recovery_take_ids()

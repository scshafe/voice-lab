"""Strict bounded inventory messages never claim unfinished recovery is complete."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cole_voice_lab.recording_contracts import (
    RecordingRecoveryFinding,
    RecordingRecoveryInventoryPage,
    RecoverySnapshotCommand,
)


@pytest.fixture
def scanning_page() -> dict[str, Any]:
    return {
        "scan_generation": "synthetic-scan",
        "inventory_revision": 1,
        "scan_state": "scanning",
        "enumeration_complete": False,
        "pending_count": 1,
        "finding_count": 1,
        "recovered_seal_count": 0,
        "entries": (
            {"take_id": "synthetic-take", "observed_take_revision": None, "pending": True},
        ),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"scan_state": "complete"},
        {"covered_store_revision": 10},
        {"scan_state": "error"},
        {"finding_count": 0},
        {"inventory_revision": -1},
        {"pending_count": 0},
        {"pending_count": 2},
        {"recovered_seal_count": 1},
    ],
)
def test_inventory_refuses_false_coverage_and_incoherent_counts(
    scanning_page: dict[str, Any], change: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        RecordingRecoveryInventoryPage.model_validate(scanning_page | change)


def test_pending_inventory_round_trips_without_a_fabricated_result(
    scanning_page: dict[str, Any],
) -> None:
    page = RecordingRecoveryInventoryPage.model_validate(scanning_page)
    restored = RecordingRecoveryInventoryPage.model_validate_json(page.model_dump_json())
    assert restored == page
    assert restored.entries[0].result is None


def test_inventory_refuses_duplicate_and_unbounded_visible_entries(
    scanning_page: dict[str, Any],
) -> None:
    entry = scanning_page["entries"][0]
    for entries in [(entry, entry), tuple(entry | {"take_id": f"take-{n}"} for n in range(51))]:
        with pytest.raises(ValidationError):
            RecordingRecoveryInventoryPage.model_validate(
                scanning_page | {"entries": entries, "finding_count": len(entries)}
            )


def test_finding_cannot_replace_identity_or_claim_classification_without_result() -> None:
    with pytest.raises(ValidationError):
        RecordingRecoveryFinding(take_id="synthetic-one", pending=False)
    with pytest.raises(ValidationError):
        RecordingRecoveryFinding.model_validate(
            {
                "take_id": "synthetic-one",
                "pending": True,
                "result": {"take_id": "synthetic-other", "status": "busy"},
            }
        )


@pytest.mark.parametrize("size", [0, 51, True, 1.0])
def test_recovery_snapshot_enforces_strict_page_bound(size: Any) -> None:
    with pytest.raises(ValidationError):
        RecoverySnapshotCommand(page_size=size)

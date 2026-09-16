"""Bulk legacy publication preserves the serial index's collision and revision rules."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from recording_fixtures import synthetic_catalog
from test_recording_discovery_publication import error, legacy_entry
from test_recording_index import native_take

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import RecordingGroupSummary, RecordingHistoryEntry
from cole_voice_lab.recording_index import RecordingIndex, RecordingIndexError


def changed(entry: RecordingHistoryEntry, **updates: Any) -> RecordingHistoryEntry:
    return RecordingHistoryEntry.model_validate(entry.model_dump() | updates)


def projection_rows(index: RecordingIndex) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: sorted(
            (tuple(row) for row in index.connection.execute(f"SELECT * FROM {table}")), key=repr
        )
        for table in (
            "recording_prompts",
            "recording_catalog_prompts",
            "recording_groups",
            "recording_catalog_groups",
            "recording_takes",
            "recording_take_aliases",
            "recording_unknown_records",
            "recording_catalogs",
        )
    }


def test_bulk_replacement_equals_serial_rows_and_first_group_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = legacy_entry()
    assert entry.recorded_at is not None and entry.source_sha256 is not None
    later = entry.recorded_at + timedelta(days=1)
    entries = (
        (entry, None, (("tmp/first.wav", entry.source_sha256, "legacy-tmp"),)),
        (
            changed(entry, take_id="legacy-second"),
            later,
            (("raw/second.wav", entry.source_sha256, "legacy-raw"),),
        ),
        (changed(entry, take_id="legacy-third", legacy_session_id="explicit-session"), later, ()),
    )
    groups = (
        RecordingGroupSummary(
            group_id=None,
            legacy_session_id="explicit-session",
            opened_at=entry.recorded_at,
            date_precision="session",
            take_count=1,
            kept_count=1,
        ),
    )
    outputs = []
    for serial in (False, True):
        layout = AssetLayout.initialize(tmp_path / str(serial))
        with RecordingIndex(layout) as index:
            catalog = index.register_catalog(synthetic_catalog(3))
            index.mark_unknown(catalog, "native-preserved", error())
            index.mark_unknown(catalog, "legacy:old", error("Obsolete legacy error"))
            if serial:
                monkeypatch.setattr(index, "_insert_new_prepared_legacy", lambda *a: False)
            else:

                def forbidden(*args: Any, **kwargs: Any) -> None:
                    raise AssertionError("unique new legacy rows did not use bulk publication")

                monkeypatch.setattr(index, "_project", forbidden)
                monkeypatch.setattr(index, "_alias", forbidden)
            for _ in range(2):
                index.replace_legacy_catalog(catalog, entries, groups)  # type: ignore[arg-type]
            outputs.append(projection_rows(index))
            assert index.history(catalog).completeness == "unknown"
            actual_groups = {row[0]: row for row in outputs[-1]["recording_groups"]}
            assert actual_groups["legacy:synthetic-session"][4:] == (None, "unknown")
            assert actual_groups["legacy:explicit-session"][4:] == (
                entry.recorded_at.isoformat(),
                "session",
            )
        with RecordingIndex(layout):
            pass  # Full cache audit must accept the committed bulk representation.
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("case", ["identical", "newer", "older"])
def test_duplicate_take_rows_keep_serial_revision_and_alias_semantics(
    tmp_path: Path, case: str
) -> None:
    base = legacy_entry()
    assert base.source_sha256 is not None
    newest = changed(base, state_revision=2, decision="removed")
    first, second, expected = {
        "identical": (base, base, base),
        "newer": (base, newest, newest),
        "older": (newest, base, newest),
    }[case]
    with RecordingIndex(AssetLayout.initialize(tmp_path / "assets")) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        index.replace_legacy_catalog(
            catalog,
            (
                (first, None, (("tmp/one.wav", base.source_sha256, "legacy-tmp"),)),
                (second, None, (("tmp/two.wav", base.source_sha256, "legacy-tmp"),)),
            ),
        )
        assert index.history(catalog).takes == (expected,)
        assert (
            index.connection.execute("SELECT COUNT(*) FROM recording_take_aliases").fetchone()[0]
            == 2
        )


@pytest.mark.parametrize(
    "field,message",
    [
        ("decision", "durable revision"),
        ("source_sha256", "sealed source"),
        ("catalog_sha256", "catalog identity"),
        ("legacy_session_id", "prompt/context"),
        ("prompt", "prompt/context"),
    ],
)
def test_duplicate_take_conflicts_roll_back_previous_history(
    tmp_path: Path, field: str, message: str
) -> None:
    entry = legacy_entry()
    updates: dict[str, Any] = {
        "decision": "removed",
        "source_sha256": "1" * 64,
        "catalog_sha256": "1" * 64,
        "legacy_session_id": "other-session",
        "prompt": synthetic_catalog(3).prompts[1],
    }
    revision = 0 if field == "decision" else 1
    second = changed(entry, **{field: updates[field], "state_revision": revision})
    with RecordingIndex(AssetLayout.initialize(tmp_path / "assets")) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        index.replace_legacy_catalog(catalog, ((entry, None, ()),))
        before = projection_rows(index)
        with pytest.raises(RecordingIndexError, match=message):
            index.replace_legacy_catalog(catalog, ((entry, None, ()), (second, None, ())))
        assert projection_rows(index) == before


def test_duplicate_alias_for_same_take_preserves_first_kind(tmp_path: Path) -> None:
    entry = legacy_entry()
    assert entry.source_sha256 is not None
    with RecordingIndex(AssetLayout.initialize(tmp_path / "assets")) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        index.replace_legacy_catalog(
            catalog,
            (
                (
                    entry,
                    None,
                    (
                        ("tmp/shared.wav", entry.source_sha256, "legacy-tmp"),
                        ("tmp/shared.wav", entry.source_sha256, "legacy-raw"),
                    ),
                ),
            ),
        )
        row = index.connection.execute("SELECT * FROM recording_take_aliases").fetchone()
        assert row[3] == "legacy-tmp"


@pytest.mark.parametrize("remaining", [False, True])
def test_alias_collision_across_events_rolls_back_scoped_replacement(
    tmp_path: Path, remaining: bool
) -> None:
    entry = legacy_entry()
    assert entry.source_sha256 is not None
    alias = ("tmp/shared.wav", entry.source_sha256, "legacy-tmp")
    with RecordingIndex(AssetLayout.initialize(tmp_path / "assets")) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        other = changed(entry, take_id="legacy-other")
        if remaining:
            other_catalog = synthetic_catalog(4)
            index.register_catalog(other_catalog)
            other = changed(
                other,
                catalog_sha256=canonical_sha256(other_catalog),
                prompt=other_catalog.prompts[3],
            )
            index.update_legacy_review(other)
            index.register_alias(other.take_id, *alias)  # type: ignore[arg-type]
            rows = ((entry, None, (alias,)),)
        else:
            rows = ((entry, None, (alias,)), (other, None, (alias,)))
        before = projection_rows(index)
        with pytest.raises(RecordingIndexError, match="two recording events"):
            index.replace_legacy_catalog(catalog, rows)  # type: ignore[arg-type]
        assert projection_rows(index) == before


@pytest.mark.parametrize("conflict", ["native", "subject", "catalog"])
def test_take_identity_remaining_after_scoped_delete_retains_serial_guards(
    tmp_path: Path, conflict: str
) -> None:
    layout = AssetLayout.initialize(tmp_path / "assets")
    entry = legacy_entry()
    with RecordingIndex(layout) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        if conflict == "native":
            native = native_take(synthetic_catalog(3), 0)
            index.update_take(native)
            entry = changed(entry, take_id=native.reservation.take_id)
        elif conflict == "subject":
            with RecordingIndex(layout, subject_id="another-subject") as other:
                other.register_catalog(synthetic_catalog(3))
                other.update_legacy_review(changed(entry, subject_id="another-subject"))
        else:
            other_catalog = synthetic_catalog(4)
            index.register_catalog(other_catalog)
            index.update_legacy_review(
                changed(
                    entry,
                    catalog_sha256=canonical_sha256(other_catalog),
                    prompt=other_catalog.prompts[3],
                )
            )
            entry = changed(entry, state_revision=1)
        before = projection_rows(index)
        with pytest.raises(RecordingIndexError, match="identity|source or subject"):
            index.replace_legacy_catalog(catalog, ((entry, None, ()),))
        assert projection_rows(index) == before


def test_bulk_unmatched_prompt_and_invalid_alias_preserve_prior_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_index as module

    entry = legacy_entry()
    assert entry.source_sha256 is not None
    with RecordingIndex(AssetLayout.initialize(tmp_path / "assets")) as index:
        catalog = index.register_catalog(synthetic_catalog(3))
        index.replace_legacy_catalog(catalog, ((entry, None, ()),))
        before = projection_rows(index)
        unmatched = changed(entry, prompt=synthetic_catalog(4).prompts[3])
        with pytest.raises(RecordingIndexError, match="exactly match"):
            index.replace_legacy_catalog(catalog, ((unmatched, None, ()),))
        assert projection_rows(index) == before
        original = module.validate_relative_path

        def validate(path: str) -> Any:
            assert not index.connection.in_transaction
            return original(path)

        monkeypatch.setattr(module, "validate_relative_path", validate)
        with pytest.raises(ValueError):
            index.replace_legacy_catalog(
                catalog, ((entry, None, (("../outside.wav", entry.source_sha256, "legacy-tmp"),)),)
            )
        assert projection_rows(index) == before
        index.replace_legacy_catalog(
            catalog,
            (
                (
                    entry,
                    None,
                    (
                        ("tmp/one.wav", entry.source_sha256, "legacy-tmp"),
                        ("tmp/one.wav", entry.source_sha256, "legacy-raw"),
                    ),
                ),
            ),
        )

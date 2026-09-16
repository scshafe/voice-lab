"""Metadata-only native fixtures and inert legacy audio exercise the real index."""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from recording_fixtures import synthetic_catalog
from test_recording_contracts import synthetic_contracts
from test_recording_library import DEVICE, Clock, Process, Timer, take

from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.intake import accept_capture_session
from cole_voice_lab.models import PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    AnalysisState,
    CaptureReservation,
    CaptureSealReceipt,
    DecisionState,
    RecordingError,
    RecordingGroup,
    RecordingHistoryEntry,
    RecordingHistoryPage,
    RecordingSelection,
    RecordingTake,
)
from cole_voice_lab.recording_index import (
    RecordingIndex,
    RecordingIndexError,
    recording_index_migration,
)
from cole_voice_lab.recording_index_backfill import backfill_recording_library
from cole_voice_lab.recording_library import RecordingLibrary
from cole_voice_lab.sqlite_migrations import SqliteMigrationError


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "private-assets")


@pytest.fixture
def catalog() -> PromptSuite:
    return synthetic_catalog(36)


def native_take(
    catalog: PromptSuite,
    ordinal: int,
    *,
    prompt_ordinal: int | None = None,
    decision: DecisionState = "kept",
    analysis: AnalysisState = "pending",
    revision: int | None = None,
    group_ordinal: int = 0,
) -> RecordingTake:
    fixture = synthetic_contracts()
    base = fixture["recording-take"]
    group = RecordingGroup.model_validate(
        base.reservation.group.model_dump()
        | {
            "group_id": f"synthetic-group-{group_ordinal:06d}",
            "subject_id": "cole-self",
            "catalog": catalog.model_dump(),
            "catalog_sha256": canonical_sha256(catalog),
            "room_tone_take_ids": (),
        }
    )
    take_id = f"synthetic-take-{ordinal:06d}"
    reservation = CaptureReservation.model_validate(
        base.reservation.model_dump()
        | {
            "take_id": take_id,
            "operation_id": f"synthetic-reserve-{ordinal:06d}",
            "group": group.model_dump(),
            "prompt": catalog.prompts[
                ordinal % len(catalog.prompts) if prompt_ordinal is None else prompt_ordinal
            ].model_dump(),
            "partial_path": f"tmp/recording-takes/{take_id}/capture.partial.wav",
            "original_path": f"tmp/recording-takes/{take_id}/original.wav",
        }
    )
    seal = CaptureSealReceipt.model_validate(
        base.seal.model_dump()
        | {
            "take_id": take_id,
            "reservation_sha256": canonical_sha256(reservation),
            "original_path": reservation.original_path,
        }
    )
    return RecordingTake.model_validate(
        base.model_dump()
        | {
            "reservation": reservation.model_dump(),
            "seal": seal.model_dump(),
            "decision": decision,
            "analysis": analysis,
            "decision_revision": 0 if decision == "draft" else 1,
            "state_revision": ordinal + 1 if revision is None else revision,
        }
    )


@pytest.fixture
def index(layout: AssetLayout, catalog: PromptSuite) -> Iterator[RecordingIndex]:
    with RecordingIndex(layout) as current:
        current.register_catalog(catalog)
        yield current


def complete(index: RecordingIndex, catalog: PromptSuite) -> str:
    digest = canonical_sha256(catalog)
    index.set_completeness(digest, "complete")
    return digest


def test_new_cache_is_building_not_missing(index: RecordingIndex, catalog: PromptSuite) -> None:
    page = index.history(canonical_sha256(catalog))
    assert page.completeness == "building"
    assert page.missing_prompt_count is None
    assert {row.status for row in page.prompts} == {"unknown"}
    with pytest.raises(RecordingIndexError, match="requires complete"):
        index.next_missing(canonical_sha256(catalog))


@pytest.mark.parametrize(
    "decision,analysis,counts",
    [
        ("draft", "pending", False),
        ("kept", "pending", True),
        ("kept", "running", True),
        ("kept", "needs-review", True),
        ("kept", "failed", True),
        ("kept", "verified", True),
        ("kept", "invalid", False),
        ("rejected", "verified", False),
        ("removed", "verified", False),
    ],
)
def test_coverage_counts_only_kept_noninvalid_decisions(
    index: RecordingIndex,
    catalog: PromptSuite,
    decision: DecisionState,
    analysis: AnalysisState,
    counts: bool,
) -> None:
    digest = complete(index, catalog)
    index.update_take(native_take(catalog, 0, decision=decision, analysis=analysis))
    page = index.history(digest)
    assert page.covered_prompt_count == int(counts)
    assert page.missing_prompt_count == 36 - int(counts)
    assert page.prompts[0].kept_count == int(counts)
    assert page.groups[0].kept_count == int(counts)


def test_pending_saved_take_overlays_navigation_without_mutation(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    draft = native_take(catalog, 0, decision="draft")
    index.update_take(draft)
    before = index.history(digest)
    proposed = native_take(catalog, 0, revision=2)
    selection = RecordingSelection(
        catalog_sha256=digest,
        prompt_id=catalog.prompts[0].prompt_id,
        take_id=proposed.reservation.take_id,
        selection_revision=0,
        index_revision=before.index_revision,
    )
    target = index.resolve_navigation(proposed, selection)
    assert target.next_prompt == catalog.prompts[1]
    after = index.history(digest)
    assert after == before
    index.update_take(proposed)
    assert index.history(digest).covered_prompt_count == 1


def test_navigation_wraps_once_and_completes_without_artificial_room_tone(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    for ordinal in range(35):
        index.update_take(native_take(catalog, ordinal))
    assert (
        index.next_missing(digest, after_prompt_id=catalog.prompts[-1].prompt_id).next_prompt
        == catalog.prompts[-1]
    )
    index.update_take(native_take(catalog, 35))
    assert index.next_missing(digest, after_prompt_id=catalog.prompts[12].prompt_id).complete


def test_exact_prompt_style_change_does_not_reuse_coverage(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    complete(index, catalog)
    index.update_take(native_take(catalog, 0))
    changed = PromptSuite.model_validate(
        catalog.model_dump()
        | {
            "suite_id": "changed-catalog",
            "prompts": tuple(
                p.model_dump() | ({"style": "Changed"} if i == 0 else {})
                for i, p in enumerate(catalog.prompts)
            ),
        }
    )
    digest = index.register_catalog(changed)
    index.set_completeness(digest, "complete")
    assert index.history(digest).covered_prompt_count == 0
    exact = catalog.model_copy(update={"suite_id": "different-catalog-same-exact-prompts"})
    digest = index.register_catalog(exact)
    index.set_completeness(digest, "complete")
    assert index.history(digest).covered_prompt_count == 1


def test_distinct_identical_pcm_events_and_package_aliases_do_not_collapse(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    one = native_take(catalog, 0)
    two = native_take(catalog, 1, prompt_ordinal=0, group_ordinal=1)
    index.update_take(one)
    index.update_take(two)
    assert one.seal is not None
    index.register_alias(
        one.reservation.take_id,
        "raw/synthetic/audio/one.wav",
        one.seal.original_sha256,
        "package-raw",
    )
    page = index.history(digest)
    assert page.total_take_count == 2
    assert page.covered_prompt_count == 1
    assert page.total_group_count == 2
    assert page.prompts[0].kept_count == 2
    with pytest.raises(RecordingIndexError, match="two recording events"):
        index.register_alias(
            two.reservation.take_id,
            "raw/synthetic/audio/one.wav",
            one.seal.original_sha256,
            "package-raw",
        )


def test_incremental_revisions_ignore_old_replays_and_reject_conflicts(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    first = native_take(catalog, 0, revision=5)
    updated = native_take(catalog, 0, revision=6, analysis="invalid")
    first_revision = index.update_take(first)
    assert index.update_take(first) == first_revision
    latest_revision = index.update_take(updated)
    assert index.update_take(first) == latest_revision
    assert index.history(digest).covered_prompt_count == 0
    with pytest.raises(RecordingIndexError, match="without a durable revision"):
        index.update_take(native_take(catalog, 0, revision=6, analysis="verified"))


def test_thousand_retakes_are_paged_bounded_from_one_snapshot(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    index.reconcile_takes(native_take(catalog, i, group_ordinal=i // 4) for i in range(1000))
    seen: set[str] = set()
    cursor = None
    while True:
        page = index.history(digest, cursor=cursor, page_size=173)
        assert page.total_take_count == 1000
        assert page.total_group_count == 250
        assert page.total_prompt_count == 36
        assert page.covered_prompt_count == 36
        assert len(page.takes) <= 173
        assert len(page.groups) <= 173
        assert len(canonical_json_bytes(page)) < 1_048_576
        assert not seen.intersection(take.take_id for take in page.takes)
        seen.update(take.take_id for take in page.takes)
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert len(seen) == 1000
    first = index.history(digest, page_size=173)
    second = index.history(digest, group_cursor=first.next_group_cursor, page_size=173)
    assert len(first.groups) == 173
    assert len(second.groups) == 77


def test_actual_payload_budget_pages_maximum_unicode_prompt_metadata(layout: AssetLayout) -> None:
    base = synthetic_catalog(2)
    catalog = PromptSuite.model_validate(
        base.model_dump()
        | {"prompts": tuple(p.model_dump() | {"text": "😀" * 2000} for p in base.prompts)}
    )
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(catalog)
        index.reconcile_takes(native_take(catalog, i) for i in range(200))
        index.set_completeness(digest, "complete")
        page = index.history(digest, page_size=200)
        assert len(page.takes) < 200
        assert page.has_more
        assert len(canonical_json_bytes(page)) <= 900_000


def test_cursors_reject_mutation_other_catalog_kind_and_rebuild(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    index.reconcile_takes(native_take(catalog, i) for i in range(3))
    first = index.history(digest, page_size=1)
    with pytest.raises(RecordingIndexError, match="different query"):
        index.history(digest, prompt_cursor=first.next_cursor)
    index.update_take(native_take(catalog, 4))
    with pytest.raises(RecordingIndexError, match="changed snapshot"):
        index.history(digest, cursor=first.next_cursor)


def test_repeated_metadata_reads_and_restart_open_zero_wavs(
    layout: AssetLayout, catalog: PromptSuite, monkeypatch: pytest.MonkeyPatch
) -> None:
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(catalog)
        index.reconcile_takes(native_take(catalog, i) for i in range(120))
        index.set_completeness(digest, "complete")
        before = index.history(digest)
    original = os.open

    def guarded(path: object, *args: object, **kwargs: object) -> int:
        assert not str(path).endswith(".wav"), "indexed metadata opened a WAV"
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", guarded)
    with RecordingIndex(layout) as index:
        library = RecordingLibrary(layout, catalog)
        for _ in range(12):
            assert library.indexed_history(index) == before
            assert index.next_missing(digest).complete


def test_named_unknown_preserves_rows_and_blocks_missing_navigation(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    index.update_take(native_take(catalog, 0))
    index.mark_unknown(
        digest,
        "changed-take",
        RecordingError(
            code="RECORDING.SOURCE_CHANGED",
            message="synthetic-take-000000 source identity changed",
            retryable=False,
        ),
    )
    page = index.history(digest)
    assert page.total_take_count == 1
    assert page.error_count == 1
    assert page.completeness == "unknown"
    assert page.missing_prompt_count is None
    with pytest.raises(RecordingIndexError, match="requires complete"):
        index.next_missing(digest)
    with pytest.raises(RecordingIndexError, match="must be reconciled"):
        index.set_completeness(digest, "complete")
    index.clear_unknown(digest, "changed-take")
    index.set_completeness(digest, "complete")
    assert index.history(digest).missing_prompt_count == 35


def test_external_cache_payload_tamper_is_unknown_not_missing(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    index.update_take(native_take(catalog, 0))
    with sqlite3.connect(index.path) as attacker:
        attacker.execute("UPDATE recording_takes SET recording_take_decision='removed'")
    with pytest.raises(RecordingIndexError, match="metadata changed"):
        index.history(digest)


def test_corrupt_cache_is_preserved_for_explicit_rebuild(
    layout: AssetLayout, catalog: PromptSuite
) -> None:
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(catalog)
        index.update_take(native_take(catalog, 0))
        path = index.path
    path.write_bytes(b"corrupt cache bytes")
    with pytest.raises(RecordingIndexError, match="rebuild required"):
        RecordingIndex(layout)
    archived = RecordingIndex.quarantine_cache(layout)
    assert (archived / path.name).read_bytes() == b"corrupt cache bytes"
    with RecordingIndex(layout) as rebuilt:
        rebuilt.register_catalog(catalog)
        assert rebuilt.history(digest).completeness == "building"
        rebuilt.reconcile_takes((native_take(catalog, 0),))
        rebuilt.set_completeness(digest, "complete")
        assert rebuilt.history(digest).covered_prompt_count == 1


@pytest.mark.parametrize(
    "member",
    [
        "recording-library.sqlite3",
        "recording-library.sqlite3-wal",
        "recording-library.sqlite3-shm",
        ".index.lock",
    ],
)
def test_index_symlinks_fail_closed(layout: AssetLayout, member: str, tmp_path: Path) -> None:
    directory = layout.directory("provenance") / "recording-library"
    directory.mkdir(mode=0o700)
    outside = tmp_path / "do-not-change"
    outside.write_bytes(b"preserve me")
    outside.chmod(0o600)
    (directory / member).symlink_to(outside)
    with pytest.raises((SecurityError, RecordingIndexError)):
        RecordingIndex(layout)
    assert outside.read_bytes() == b"preserve me"


def test_unsafe_modes_hardlinks_and_replaced_database_fail_closed(
    layout: AssetLayout, catalog: PromptSuite, tmp_path: Path
) -> None:
    directory = layout.directory("provenance") / "recording-library"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(SecurityError):
        RecordingIndex(layout)
    assert directory.stat().st_mode & 0o777 == 0o755
    directory.chmod(0o700)
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(catalog)
        alias = tmp_path / "alias"
        os.link(index.path, alias)
        with pytest.raises(SecurityError):
            index.history(digest)
        alias.unlink()
        index.path.rename(index.path.with_suffix(".moved"))
        index.path.touch(mode=0o600)
        with pytest.raises(SecurityError):
            index.history(digest)


def test_failed_native_rebuild_rolls_back_existing_snapshot(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    index.update_take(native_take(catalog, 0))
    before = index.history(digest)

    def interrupted() -> Iterator[RecordingTake]:
        assert not index.connection.in_transaction
        yield native_take(catalog, 1)
        raise RuntimeError("synthetic interrupted evidence reader")

    with pytest.raises(RuntimeError, match="interrupted"):
        index.reconcile_takes(interrupted())
    after = index.history(digest)
    assert after.takes == before.takes
    assert after.completeness == "unknown"
    assert after.missing_prompt_count is None


def test_schema_up_down_are_idempotent_and_foreign_keys_enforced() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for _ in range(2):
            connection.executescript(recording_index_migration("up"))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO recording_take_aliases VALUES "
                "('alias','absent','tmp/absent.wav','native-original',?)",
                ("1" * 64,),
            )
        for _ in range(2):
            connection.executescript(recording_index_migration("down"))
        assert not connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()


def legacy_library(layout: AssetLayout) -> RecordingLibrary:
    return RecordingLibrary(
        layout,
        synthetic_catalog(2),
        process_factory=Process,
        timer_factory=Timer,
        clock=Clock(),
        device_resolver=lambda device: device,
    )


def test_legacy_draft_rejected_kept_backfill_preserves_precise_dates(layout: AssetLayout) -> None:
    library = legacy_library(layout)
    session = library.begin_session([p.prompt_id for p in library.prompt_suite.prompts], DEVICE)
    session.start_take()
    session.stop_take()
    session.reject_take("synthetic rejected take")
    session.start_take()
    session.stop_take()
    with RecordingIndex(layout) as index:
        backfill_recording_library(library, index)
        page = library.indexed_history(index)
        assert {item.decision for item in page.takes} == {"draft", "rejected"}
        assert page.covered_prompt_count == 0
        assert {item.date_precision for item in page.takes} == {"take"}
        session.accept_take()
        backfill_recording_library(library, index)
        assert library.indexed_history(index).covered_prompt_count == 1
    session.abort()


def test_legacy_accepted_raw_and_finalized_tmp_deduplicate_across_restart(
    layout: AssetLayout,
) -> None:
    library = legacy_library(layout)
    session = library.begin_session([p.prompt_id for p in library.prompt_suite.prompts], DEVICE)
    take(session)
    take(session)
    result = session.finalize()
    incoming = layout.directory("incoming") / session.evidence.session_id
    shutil.copytree(result.session_directory, incoming)
    accept_capture_session(layout, incoming)
    with RecordingIndex(layout) as index:
        backfill_recording_library(library, index)
        before = library.indexed_history(index)
        assert before.total_take_count == 2
        assert before.covered_prompt_count == 2
        assert {row.export for row in before.takes} == {"accepted-raw"}
        assert (
            index.connection.execute("SELECT count(*) FROM recording_take_aliases").fetchone()[0]
            == 4
        )
    with RecordingIndex(layout) as index:
        assert library.indexed_history(index) == before


def test_legacy_backfill_source_corruption_preserves_old_rows_as_unknown(
    layout: AssetLayout,
) -> None:
    library = legacy_library(layout)
    session = library.begin_session([p.prompt_id for p in library.prompt_suite.prompts], DEVICE)
    take(session)
    with RecordingIndex(layout) as index:
        backfill_recording_library(library, index)
        original_count = library.indexed_history(index).total_take_count
        path = next(session.engine.session_directory.rglob("*.wav"))
        before = path.stat()
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with pytest.raises(RecordingIndexError, match="Unknown"):
            backfill_recording_library(library, index)
        page = library.indexed_history(index)
        assert page.total_take_count == original_count
        assert page.completeness == "unknown"
        assert page.missing_prompt_count is None
    session.abort()


def test_unknown_legacy_unfinished_group_never_invents_recording_date(layout: AssetLayout) -> None:
    library = legacy_library(layout)
    (layout.directory("tmp") / "capture-old-interrupted").mkdir(mode=0o700)
    with RecordingIndex(layout) as index:
        backfill_recording_library(library, index)
        page = library.indexed_history(index)
        assert page.total_take_count == 0
        assert page.total_group_count == 1
        assert page.groups[0].opened_at is None
        assert page.groups[0].date_precision == "unknown"
        assert page.completeness == "unknown"


def test_history_summary_schema_remains_strict_and_bounded(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    page = index.history(canonical_sha256(catalog))
    payload = json.loads(canonical_json_bytes(page))
    payload["missing_prompt_count"] = 36
    with pytest.raises(ValueError, match="incomplete"):
        RecordingHistoryPage.model_validate_json(json.dumps(payload))


def test_legacy_removed_restore_preserves_source_identity_and_draft_distinction(
    layout: AssetLayout,
) -> None:
    library = legacy_library(layout)
    session = library.begin_session([p.prompt_id for p in library.prompt_suite.prompts], DEVICE)
    take(session)
    session.abort()
    old = library.rows()[0].history[0]
    with RecordingIndex(layout) as index:
        backfill_recording_library(library, index)
        original = library.indexed_history(index).takes[0]
        library.remove_take(old, "synthetic operator removal")
        backfill_recording_library(library, index)
        removed = library.indexed_history(index).takes[0]
        assert removed.take_id == original.take_id
        assert removed.source_sha256 == original.source_sha256
        assert removed.decision == "removed"
        assert library.indexed_history(index).covered_prompt_count == 0
        library.restore_take(library.rows()[0].history[0])
        backfill_recording_library(library, index)
        assert library.indexed_history(index).takes[0].decision == "kept"


def test_legacy_interrupted_capture_stays_visible_without_guessed_source_digest(
    layout: AssetLayout,
) -> None:
    library = legacy_library(layout)
    session = library.begin_session([p.prompt_id for p in library.prompt_suite.prompts], DEVICE)
    session.start_take()
    from cole_voice_lab.capture import CaptureInterrupted

    with pytest.raises(CaptureInterrupted):
        session.abort()
    with RecordingIndex(layout) as index:
        backfill_recording_library(library, index)
        page = library.indexed_history(index)
        assert page.total_take_count == 1
        assert page.covered_prompt_count == 0
        assert page.takes[0].source_sha256 is None
        assert page.takes[0].decision == "draft"
        assert page.takes[0].error is not None


def test_legacy_v1_without_sidecar_retains_session_only_date(layout: AssetLayout) -> None:
    from test_recording_library import START

    from cole_voice_lab.capture import TakeEngine
    from cole_voice_lab.resources import load_prompt_suite

    suite = load_prompt_suite("pilot")
    engine = TakeEngine(
        layout=layout,
        prompt_suite=suite,
        subject_id="cole-self",
        device=DEVICE,
        session_id="legacy-pilot",
        process_factory=Process,
        timer_factory=Timer,
        clock=lambda: START,
    )
    for _ in suite.prompts:
        engine.start_take()
        engine.stop_take()
        engine.accept_take()
    engine.finalize()
    library = RecordingLibrary(layout, suite)
    with RecordingIndex(layout) as index:
        backfill_recording_library(library, index)
        page = library.indexed_history(index)
        assert page.covered_prompt_count == len(suite.prompts)
        assert {row.date_precision for row in page.takes} == {"session"}
        assert {row.recorded_at for row in page.takes} == {START}


def test_explicit_rebuild_refuses_a_live_cache_even_between_queries(index: RecordingIndex) -> None:
    with pytest.raises(BlockingIOError):
        RecordingIndex.quarantine_cache(index.layout)


def test_all_snapshot_components_execute_under_one_sql_transaction(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    index.update_take(native_take(catalog, 0))
    statements: list[str] = []
    index.connection.set_trace_callback(statements.append)
    page = index.history(digest)
    index.connection.set_trace_callback(None)
    assert page.total_take_count == sum(row.take_count for row in page.prompts)
    assert page.total_take_count == sum(row.take_count for row in page.groups)
    assert statements.count("BEGIN") == 1
    assert statements.count("COMMIT") == 1
    assert all("OFFSET" not in query for query in statements)
    start, end = statements.index("BEGIN"), statements.index("COMMIT")
    assert all(start < i < end for i, query in enumerate(statements) if query.startswith("SELECT"))


def test_backfill_never_holds_index_transaction_while_reading_audio(
    layout: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_index_backfill as backfill_module

    library = legacy_library(layout)
    session = library.begin_session([p.prompt_id for p in library.prompt_suite.prompts], DEVICE)
    take(session)
    with RecordingIndex(layout) as index:
        original = backfill_module.inspect_canonical_wav

        def guarded(path: Path):
            assert not index.connection.in_transaction
            return original(path)

        monkeypatch.setattr(backfill_module, "inspect_canonical_wav", guarded)
        backfill_recording_library(library, index)
    session.abort()


def test_source_invalidation_names_affected_take_and_prompt_without_changing_decision(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    take = native_take(catalog, 0)
    index.update_take(take)
    error = RecordingError(
        code="RECORDING.SOURCE_CHANGED", message="Selected source identity changed", retryable=False
    )
    index.invalidate_take(take.reservation.take_id, error)
    page = index.history(digest)
    assert page.completeness == "unknown"
    assert page.takes[0].error == error
    assert page.takes[0].decision == "kept"
    assert page.prompts[0].status == "unknown"
    with pytest.raises(RecordingIndexError, match="unindexed"):
        index.invalidate_take("absent", error)


@pytest.mark.parametrize("page_size", [0, 201, True])
def test_history_rejects_unbounded_page_request(
    index: RecordingIndex, catalog: PromptSuite, page_size: int
) -> None:
    with pytest.raises(RecordingIndexError, match="page size"):
        index.history(canonical_sha256(catalog), page_size=page_size)


def test_navigation_rejects_missing_catalog_prompt_and_unbound_overlay(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    with pytest.raises(RecordingIndexError, match="not indexed"):
        index.history("f" * 64)
    with pytest.raises(RecordingIndexError, match="absent"):
        index.next_missing(digest, after_prompt_id="absent")
    proposed = native_take(catalog, 0)
    selection = RecordingSelection(
        catalog_sha256=digest,
        prompt_id=catalog.prompts[1].prompt_id,
        take_id=proposed.reservation.take_id,
        selection_revision=0,
        index_revision=0,
    )
    with pytest.raises(RecordingIndexError, match="does not bind"):
        index.resolve_navigation(proposed, selection)
    other = catalog.model_copy(update={"suite_id": "unrelated-catalog"})
    with pytest.raises(RecordingIndexError, match="another catalog"):
        index.next_missing(digest, proposed_take=native_take(other, 1))


def test_latest_retained_take_orders_exact_prompt_by_capture_date_then_projection(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = canonical_sha256(catalog)
    prompt = catalog.prompts[0]
    earlier = datetime(2026, 9, 1, 12, tzinfo=UTC)
    later = datetime(2026, 9, 2, 12, tzinfo=UTC)

    def legacy(
        take_id: str,
        recorded_at: datetime | None,
        *,
        decision: DecisionState = "kept",
        analysis: AnalysisState = "verified",
        target: Any = prompt,
    ) -> None:
        index.update_legacy_review(
            RecordingHistoryEntry(
                take_id=take_id,
                subject_id="cole-self",
                group_id=None,
                legacy_session_id="legacy-synthetic-session",
                catalog_sha256=digest,
                prompt=target,
                recorded_at=recorded_at,
                date_precision="unknown" if recorded_at is None else "session",
                source_sha256=None,
                source_frames=None,
                decision=decision,
                analysis=analysis,
                export="unprepared",
                state_revision=1,
            )
        )

    legacy("legacy-dated", earlier)
    # Building coverage selects nothing rather than a possibly older take.
    assert index.latest_retained_take(digest, prompt) is None
    complete(index, catalog)
    assert index.latest_retained_take(digest, prompt) == "legacy-dated"
    legacy("legacy-undated", None)
    assert index.latest_retained_take(digest, prompt) == "legacy-dated"
    legacy("legacy-same-date-later-row", earlier)
    assert index.latest_retained_take(digest, prompt) == "legacy-same-date-later-row"
    legacy("legacy-newest-rejected", later, decision="rejected")
    legacy("legacy-newest-removed", later, decision="removed")
    legacy("legacy-newest-draft", later, decision="draft")
    legacy("legacy-newest-invalid", later, analysis="invalid")
    assert index.latest_retained_take(digest, prompt) == "legacy-same-date-later-row"
    legacy("legacy-newest-needs-review", later, analysis="needs-review")
    assert index.latest_retained_take(digest, prompt) == "legacy-newest-needs-review"
    # Unknown dates still count when nothing dated is retained.
    legacy("legacy-only-undated", None, target=catalog.prompts[1])
    assert index.latest_retained_take(digest, catalog.prompts[1]) == "legacy-only-undated"
    assert index.latest_retained_take(digest, catalog.prompts[2]) is None
    # The exact prompt identity carries across catalogs; a changed prompt is not the same.
    other = catalog.model_copy(update={"suite_id": "another-catalog-same-prompts"})
    other_digest = index.register_catalog(other)
    index.set_completeness(other_digest, "complete")
    assert index.latest_retained_take(other_digest, prompt) == "legacy-newest-needs-review"
    with pytest.raises(RecordingIndexError, match="absent"):
        index.latest_retained_take(digest, prompt.model_copy(update={"text": "Changed text."}))


def test_canonical_catalog_tampering_is_detected_on_restart(
    layout: AssetLayout, catalog: PromptSuite
) -> None:
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(catalog)
        path = index.path
    with sqlite3.connect(path) as attacker:
        payload = canonical_json_bytes(catalog.model_copy(update={"suite_id": "tampered"})).decode()
        attacker.execute(
            "UPDATE recording_catalogs SET recording_catalog_json=? "
            "WHERE recording_catalog_sha256=?",
            (payload, digest),
        )
    with pytest.raises(RecordingIndexError, match="catalog identity"):
        RecordingIndex(layout)


def test_catalog_order_tampering_is_detected_before_any_counts(
    layout: AssetLayout, catalog: PromptSuite
) -> None:
    with RecordingIndex(layout) as index:
        index.register_catalog(catalog)
        path = index.path
    with sqlite3.connect(path) as attacker:
        attacker.execute("DELETE FROM recording_catalog_prompts WHERE recording_prompt_ordinal=0")
    with pytest.raises(RecordingIndexError, match="order/content"):
        RecordingIndex(layout)


def test_unversioned_nonempty_sqlite_cannot_be_mistaken_for_new_empty_history(
    layout: AssetLayout,
) -> None:
    directory = layout.directory("provenance") / "recording-library"
    directory.mkdir(mode=0o700)
    path = directory / "recording-library.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE unrelated_data (record TEXT)")
    with pytest.raises(RecordingIndexError, match="nonempty index"):
        RecordingIndex(layout)


def test_unknown_schema_version_and_malformed_cursor_fail_explicitly(
    layout: AssetLayout, catalog: PromptSuite
) -> None:
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(catalog)
        with pytest.raises(RecordingIndexError, match="invalid history cursor"):
            index.history(digest, cursor="malformed")
        path = index.path
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA user_version=99")
    with pytest.raises(RecordingIndexError, match="unsupported"):
        RecordingIndex(layout)


def test_reserved_unsealed_take_has_unknown_sample_date_and_no_source_alias(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = complete(index, catalog)
    take = native_take(catalog, 0, decision="draft")
    reservation_only = RecordingTake.model_validate(
        take.model_dump() | {"seal": None, "frame_range": None, "transport": "starting"}
    )
    index.update_take(reservation_only)
    row = index.history(digest).takes[0]
    assert row.recorded_at is None
    assert row.date_precision == "unknown"
    assert row.source_sha256 is None
    assert (
        index.connection.execute("SELECT count(*) FROM recording_take_aliases").fetchone()[0] == 0
    )


def test_subject_boundaries_apply_to_public_library_and_projection(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    take = native_take(catalog, 0)
    with RecordingIndex(index.layout, subject_id="someone-else") as other:
        with pytest.raises(RecordingIndexError, match="another subject"):
            other.update_take(take)
        library = RecordingLibrary(index.layout, catalog)
        with pytest.raises(Exception, match="context does not match"):
            library.indexed_history(other)
        with pytest.raises(RecordingIndexError, match="context does not match"):
            backfill_recording_library(library, other)


def test_current_revision_property_does_not_query_history(index: RecordingIndex) -> None:
    statements: list[str] = []
    index.connection.set_trace_callback(statements.append)
    assert index.revision > 0
    index.connection.set_trace_callback(None)
    assert not any("recording_takes" in query or "count(" in query for query in statements)


def test_missing_migration_resource_is_reported_as_the_index_error_callers_expect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_index as index_module
    import cole_voice_lab.sqlite_migrations as runner_module

    # An installed package with no wheel resource copy and no checkout around it.
    monkeypatch.setattr(
        runner_module,
        "__file__",
        str(tmp_path / "site-packages" / "cole_voice_lab" / "sqlite_migrations.py"),
    )
    with pytest.raises(
        RecordingIndexError, match="^packaged recording index migration resource is missing$"
    ) as caught:
        index_module._migration_resource()
    assert isinstance(caught.value.__cause__, SqliteMigrationError)
    assert caught.value.__cause__.code == "SQLITE_MIGRATIONS.RESOURCE_MISSING"


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"direction": "sideways"}, "^migration direction must be up or down$"),
        ({"version": 0}, "^unsupported recording migration version$"),
        ({"version": 4}, "^unsupported recording migration version$"),
    ],
    ids=["sideways-direction", "version-0", "version-4"],
)
def test_index_migration_refuses_unknown_direction_and_version(
    arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(RecordingIndexError, match=message):
        recording_index_migration(**arguments)


def test_directory_sync_failure_on_first_open_is_recoverable_on_reopen(
    layout: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first open commits schema version 1, seeds the revision row, then syncs the
    # library directory. A sync that fails there must leave a database the next open can
    # finish migrating, so the seed has to precede the sync (a versioned database with no
    # revision row is refused forever as "revision is missing").
    directory = layout.directory("provenance") / "recording-library"
    real_fsync = os.fsync
    failed: list[int] = []

    def fsync_failing_once_on_the_library_directory(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        library = os.stat(directory) if directory.is_dir() else None
        if (
            not failed
            and library is not None
            and (metadata.st_dev, metadata.st_ino) == (library.st_dev, library.st_ino)
        ):
            failed.append(descriptor)
            raise OSError(errno.EIO, "synthetic directory sync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync_failing_once_on_the_library_directory)
    with pytest.raises(RecordingIndexError, match="rebuild required") as caught:
        RecordingIndex(layout)
    assert isinstance(caught.value.__cause__, OSError)
    assert len(failed) == 1
    database = sqlite3.connect(directory / "recording-library.sqlite3")
    try:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 1
        assert database.execute("SELECT count(*) FROM recording_index_states").fetchone()[0] == 1
    finally:
        database.close()
    with RecordingIndex(layout) as index:
        # Reading the revision proves the seed row survived; a fresh index starts at 0.
        assert index.revision == 0
        assert index.connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_runtime_sql_resource_change_alters_service_build_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_index as index_module
    from cole_voice_lab.recording_service import recording_service_build_identity

    resource = tmp_path / "schema.sql"
    resource.write_bytes(index_module._migration_bytes())
    resource.chmod(0o600)
    monkeypatch.setattr(index_module, "_migration_resource", lambda _name: resource)
    before = recording_service_build_identity()
    with resource.open("ab") as stream:
        stream.write(b"\n-- Changed runtime migration bytes for synthetic identity check.\n")
    after = recording_service_build_identity()
    assert after.source_sha256 != before.source_sha256
    assert after.executable_sha256 == before.executable_sha256
    assert after.environment_sha256 == before.environment_sha256

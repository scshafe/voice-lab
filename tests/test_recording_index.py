"""Metadata-only native fixtures and inert legacy audio exercise the real index."""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from recording_fixtures import synthetic_catalog
from test_recording_contracts import synthetic_contracts
from test_recording_library import DEVICE, Clock, Process, Timer, take
from test_recording_takes import publish_catalog, store_suite

from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.catalog_ports import CorpusSnapshotSecurityError
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.intake import accept_capture_session
from cole_voice_lab.models import Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    RECORDING_GROUP_V2,
    AnalysisState,
    CaptureReservation,
    CaptureSealReceipt,
    CatalogReference,
    CatalogSnapshot,
    DecisionState,
    PromptAlias,
    RecordingError,
    RecordingGroup,
    RecordingHistoryEntry,
    RecordingHistoryPage,
    RecordingPromptNotCountedError,
    RecordingSelection,
    RecordingTake,
)
from cole_voice_lab.recording_index import (
    RecordingIndex,
    RecordingIndexError,
    history_entry_for_take,
    recording_index_migration,
)
from cole_voice_lab.recording_index_backfill import backfill_recording_library
from cole_voice_lab.recording_library import RecordingLibrary
from cole_voice_lab.sqlite_migrations import SqliteMigrationError
from cole_voice_lab.storage_binding import open_catalog_editor


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
    reference: CatalogReference | None = None,
    prompt: Prompt | None = None,
) -> RecordingTake:
    """An index-only take; ``reference`` makes its group a recording-group/v2 of that
    published catalog (``catalog`` is then its suite) and ``prompt`` overrides the prompt."""
    fixture = synthetic_contracts()
    base = fixture["recording-take"]
    catalog_fields: dict[str, Any] = (
        {"catalog": catalog.model_dump(), "catalog_sha256": canonical_sha256(catalog)}
        if reference is None
        else {
            "schema_version": RECORDING_GROUP_V2,
            "catalog_reference": reference.model_dump(),
            "catalog_sha256": reference.catalog_sha256,
        }
    )
    group = RecordingGroup.model_validate(
        base.reservation.group.model_dump(exclude={"catalog"})
        | catalog_fields
        | {
            "group_id": f"synthetic-group-{group_ordinal:06d}",
            "subject_id": "cole-self",
            "room_tone_take_ids": (),
        }
    )
    take_id = f"synthetic-take-{ordinal:06d}"
    if prompt is None:
        prompt = catalog.prompts[
            ordinal % len(catalog.prompts) if prompt_ordinal is None else prompt_ordinal
        ]
    reservation = CaptureReservation.model_validate(
        base.reservation.model_dump()
        | {
            "take_id": take_id,
            "operation_id": f"synthetic-reserve-{ordinal:06d}",
            "group": group.model_dump(),
            "prompt": prompt.model_dump(),
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
        current.register_catalog(CatalogSnapshot.embedded(catalog))
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
    digest = index.register_catalog(CatalogSnapshot.embedded(changed))
    index.set_completeness(digest, "complete")
    assert index.history(digest).covered_prompt_count == 0
    exact = catalog.model_copy(update={"suite_id": "different-catalog-same-exact-prompts"})
    digest = index.register_catalog(CatalogSnapshot.embedded(exact))
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
        digest = index.register_catalog(CatalogSnapshot.embedded(catalog))
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
        digest = index.register_catalog(CatalogSnapshot.embedded(catalog))
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
        digest = index.register_catalog(CatalogSnapshot.embedded(catalog))
        index.update_take(native_take(catalog, 0))
        path = index.path
    path.write_bytes(b"corrupt cache bytes")
    with pytest.raises(RecordingIndexError, match="rebuild required"):
        RecordingIndex(layout)
    archived = RecordingIndex.quarantine_cache(layout)
    assert (archived / path.name).read_bytes() == b"corrupt cache bytes"
    with RecordingIndex(layout) as rebuilt:
        rebuilt.register_catalog(CatalogSnapshot.embedded(catalog))
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
        digest = index.register_catalog(CatalogSnapshot.embedded(catalog))
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
    other_digest = index.register_catalog(CatalogSnapshot.embedded(other))
    index.set_completeness(other_digest, "complete")
    assert index.latest_retained_take(other_digest, prompt) == "legacy-newest-needs-review"
    with pytest.raises(RecordingIndexError, match="absent"):
        index.latest_retained_take(digest, prompt.model_copy(update={"text": "Changed text."}))


def test_canonical_catalog_tampering_is_detected_on_restart(
    layout: AssetLayout, catalog: PromptSuite
) -> None:
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(CatalogSnapshot.embedded(catalog))
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
        index.register_catalog(CatalogSnapshot.embedded(catalog))
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
        digest = index.register_catalog(CatalogSnapshot.embedded(catalog))
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
        ({"version": 5}, "^unsupported recording migration version$"),
    ],
    ids=["sideways-direction", "version-0", "version-5"],
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
        assert index.connection.execute("PRAGMA user_version").fetchone()[0] == 4


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


# Prompt aliases: published catalog versions whose keep-recordings revisions still count
# earlier recordings of the same prompt (index schema version 4).

CORPUS_ID = "synthetic-corpus"


def publish_revision(
    layout: AssetLayout, prompt_id: str, text: str, *, keep_recordings: bool
) -> tuple[CatalogReference, CatalogSnapshot]:
    with open_catalog_editor(layout) as corpus:
        corpus.edit_prompt(
            prompt_id,
            text=text,
            keep_recordings=keep_recordings,
            allow_rewording=keep_recordings,
        )
        reference = corpus.publish_version(CORPUS_ID)
        return reference, corpus.materialize(reference)


@dataclass
class Revisions:
    """Version 1 of a four-prompt catalog and version 2 rewording spoken-0001 (kept)."""

    layout: AssetLayout
    index: RecordingIndex
    first: tuple[CatalogReference, CatalogSnapshot]
    kept: tuple[CatalogReference, CatalogSnapshot]

    def take(
        self,
        version: tuple[CatalogReference, CatalogSnapshot],
        ordinal: int,
        prompt_id: str,
        **options: Any,
    ) -> RecordingTake:
        """A take of a group referencing ``version``, recorded against its ``prompt_id``."""
        reference, snapshot = version
        return native_take(
            snapshot.suite,
            ordinal,
            reference=reference,
            prompt=snapshot.prompt(prompt_id),
            **options,
        )

    def current(self, prompt_id: str) -> Prompt:
        prompt = self.kept[1].prompt(prompt_id)
        assert prompt is not None
        return prompt


@pytest.fixture
def revisions(tmp_path: Path) -> Iterator[Revisions]:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    first = publish_catalog(layout, store_suite(4), catalog_id=CORPUS_ID)
    kept = publish_revision(
        layout, "spoken-0001", "Synthetic sentence number one.", keep_recordings=True
    )
    assert [(a.prompt_sha256, a.accepted_prompt_sha256) for a in kept[1].prompt_aliases] == [
        (
            canonical_sha256(kept[1].prompt("spoken-0001")),
            canonical_sha256(first[1].prompt("spoken-0001")),
        )
    ]
    with RecordingIndex(layout) as index:
        yield Revisions(layout, index, first, kept)


def test_a_keep_recordings_alias_keeps_an_old_take_covering_and_selected(
    revisions: Revisions,
) -> None:
    index = revisions.index
    (first, _), (kept, _) = revisions.first, revisions.kept
    old = revisions.take(revisions.first, 0, "spoken-0001")
    draft = revisions.take(revisions.first, 1, "spoken-0002", decision="draft")
    # A referenced take projects exactly like an embedded one of the same suite.
    embedded = native_take(revisions.first[1].suite, 0, prompt_ordinal=1)
    assert history_entry_for_take(old) == history_entry_for_take(embedded)
    index.update_take(old)
    index.update_take(draft)
    index.register_catalog(revisions.kept[1])
    for reference in (first, kept):
        index.set_completeness(reference.catalog_sha256, "complete")
    current = revisions.current("spoken-0001")
    old_id = old.reservation.take_id
    page = index.history(kept.catalog_sha256)
    assert (page.total_prompt_count, page.covered_prompt_count) == (4, 1)
    assert (page.missing_prompt_count, page.total_take_count) == (3, 2)
    rows = {row.prompt_id: row for row in page.prompts}
    assert rows["spoken-0001"].prompt_sha256 == canonical_sha256(current)
    assert (rows["spoken-0001"].take_count, rows["spoken-0001"].kept_count) == (1, 1)
    assert rows["spoken-0001"].status == "saved-checking"
    assert rows["spoken-0002"].status == "draft"
    assert {row.status for row in (rows["spoken-0000"], rows["spoken-0003"])} == {"missing"}
    entry = next(item for item in page.takes if item.take_id == old_id)
    # The row keeps the prompt the audio was recorded against, under the page's catalog.
    assert entry.prompt == old.reservation.prompt != current
    assert entry.catalog_sha256 == kept.catalog_sha256
    assert index.history(kept.catalog_sha256, prompt_id="spoken-0001").takes == (entry,)
    assert [(g.take_count, g.kept_count) for g in page.groups] == [(2, 1)]
    assert index.latest_retained_take(kept.catalog_sha256, current) == old_id
    # Save & Next skips the prompt the old take covers through its alias.
    assert index.next_missing(kept.catalog_sha256).next_prompt == revisions.current("spoken-0000")
    after_first = index.next_missing(kept.catalog_sha256, after_prompt_id="spoken-0000")
    assert after_first.next_prompt == revisions.current("spoken-0002")
    reviewed, aliases = index.review_aliases(kept.catalog_sha256, old_id)
    assert reviewed.prompt == old.reservation.prompt
    assert old.seal is not None
    assert aliases == ((old.seal.original_path, old.seal.original_sha256, "native-original"),)
    # The version the take was recorded under counts it exactly as before.
    before = index.history(first.catalog_sha256)
    assert (before.covered_prompt_count, before.total_take_count) == (1, 2)


def test_old_and_new_recordings_count_their_prompt_once_and_invalidate_every_catalog(
    revisions: Revisions,
) -> None:
    index = revisions.index
    (first, _), (kept, _) = revisions.first, revisions.kept
    old = revisions.take(revisions.first, 0, "spoken-0001")
    new = revisions.take(revisions.kept, 1, "spoken-0001", group_ordinal=1)
    index.update_take(old)
    index.update_take(new)
    changed, reworded = publish_revision(
        revisions.layout, "spoken-0001", "A different sentence entirely.", keep_recordings=False
    )
    assert reworded.prompt_aliases == ()
    index.register_catalog(reworded)
    for reference in (first, kept, changed):
        index.set_completeness(reference.catalog_sha256, "complete")
    page = index.history(kept.catalog_sha256)
    assert (page.covered_prompt_count, page.missing_prompt_count) == (1, 3)
    assert page.total_take_count == 2 and page.total_group_count == 2
    row = next(item for item in page.prompts if item.prompt_id == "spoken-0001")
    assert (row.take_count, row.kept_count) == (2, 2)
    assert [(g.take_count, g.kept_count) for g in page.groups] == [(1, 1), (1, 1)]
    # Equal capture dates fall back to projection order: the newer recording is selected.
    current = revisions.current("spoken-0001")
    assert index.latest_retained_take(kept.catalog_sha256, current) == new.reservation.take_id
    assert index.next_missing(
        kept.catalog_sha256, after_prompt_id="spoken-0000"
    ).next_prompt == revisions.current("spoken-0002")
    earlier = index.history(first.catalog_sha256)
    assert (earlier.covered_prompt_count, earlier.total_take_count) == (1, 1)
    assert earlier.total_group_count == 1
    assert index.history(changed.catalog_sha256).total_take_count == 0
    index.invalidate_take(
        old.reservation.take_id,
        RecordingError(code="RECORDING.SOURCE_CHANGED", message="synthetic", retryable=False),
    )
    # Unknown wherever the old take counts: its own version and the version keeping it.
    assert index.history(first.catalog_sha256).completeness == "unknown"
    assert index.history(kept.catalog_sha256).completeness == "unknown"
    assert index.history(changed.catalog_sha256).completeness == "complete"


def test_a_default_edit_leaves_the_prompt_missing_for_old_recordings(
    revisions: Revisions,
) -> None:
    index = revisions.index
    old = revisions.take(revisions.first, 0, "spoken-0001")
    index.update_take(old)
    changed, reworded = publish_revision(
        revisions.layout, "spoken-0001", "A different sentence entirely.", keep_recordings=False
    )
    digest = index.register_catalog(reworded)
    assert digest == changed.catalog_sha256
    index.set_completeness(digest, "complete")
    page = index.history(digest)
    assert (page.covered_prompt_count, page.missing_prompt_count) == (0, 4)
    assert page.total_take_count == 0 and page.groups == ()
    current = reworded.prompt("spoken-0001")
    assert current is not None
    row = next(item for item in page.prompts if item.prompt_id == "spoken-0001")
    assert (row.status, row.take_count, row.kept_count) == ("missing", 0, 0)
    assert index.latest_retained_take(digest, current) is None
    assert index.next_missing(digest, after_prompt_id="spoken-0000").next_prompt == current
    # The take is indexed, so its review names the edit instead of a missing row.
    with pytest.raises(RecordingPromptNotCountedError, match="no longer counts") as caught:
        index.review_aliases(digest, old.reservation.take_id)
    assert caught.value.prompt_id == "spoken-0001"
    with pytest.raises(RecordingIndexError, match="absent from this exact catalog"):
        index.review_aliases(digest, "synthetic-take-unindexed")


def test_a_proposed_take_counts_through_an_alias_for_save_and_next(
    revisions: Revisions,
) -> None:
    index = revisions.index
    kept, snapshot = revisions.kept
    index.register_catalog(snapshot)
    index.set_completeness(kept.catalog_sha256, "complete")
    recorded = revisions.first[1].prompt("spoken-0001")
    assert recorded is not None
    unrelated = recorded.model_copy(update={"text": "An unrelated synthetic sentence."})
    for prompt, decision, expected in (
        (recorded, "kept", "spoken-0002"),
        (recorded, "draft", "spoken-0001"),
        (unrelated, "kept", "spoken-0001"),
    ):
        proposal = native_take(
            snapshot.suite, 0, reference=kept, prompt=prompt, decision=cast(Any, decision)
        )
        result = index.next_missing(
            kept.catalog_sha256, after_prompt_id="spoken-0000", proposed_take=proposal
        )
        assert result.next_prompt == revisions.current(expected)


def test_legacy_rows_on_an_accepted_alias_publish_in_bulk_and_serially(
    revisions: Revisions,
) -> None:
    index = revisions.index
    (first, _), (kept, snapshot) = revisions.first, revisions.kept
    digest = index.register_catalog(snapshot)
    recorded = revisions.first[1].prompt("spoken-0001")
    assert recorded is not None
    legacy = RecordingHistoryEntry(
        take_id="legacy-synthetic-alias",
        subject_id="cole-self",
        group_id=None,
        legacy_session_id="legacy-synthetic-session",
        catalog_sha256=first.catalog_sha256,
        prompt=recorded,
        recorded_at=None,
        date_precision="unknown",
        source_sha256=None,
        source_frames=None,
        decision="kept",
        analysis="verified",
        export="unprepared",
        state_revision=0,
    )
    # A repeated identity takes the serial path; a unique one is inserted in bulk.
    for entries in ([(legacy, None, ())], [(legacy, None, ()), (legacy, None, ())]):
        index.replace_legacy_catalog(digest, entries)
        page = index.history(digest)
        assert page.completeness == "complete" and page.covered_prompt_count == 1
        assert [item.take_id for item in page.takes] == [legacy.take_id]
        assert page.takes[0].prompt == recorded
    assert kept.catalog_sha256 == digest


def test_an_alias_across_prompt_ids_never_counts(layout: AssetLayout) -> None:
    old = synthetic_catalog(4)
    reworded = old.prompts[1].model_copy(update={"text": "Reworded synthetic prompt."})
    removed = old.prompts[2]
    suite = PromptSuite(
        suite_id=old.suite_id,
        purpose=old.purpose,
        prompts=(old.prompts[0], reworded, old.prompts[3]),
    )
    # The snapshot contract cannot see prompt ids behind an accepted digest; readers can.
    snapshot = CatalogSnapshot(
        suite=suite,
        catalog_sha256=canonical_sha256(suite),
        prompt_aliases=(
            PromptAlias(
                prompt_sha256=canonical_sha256(reworded),
                accepted_prompt_sha256=canonical_sha256(removed),
            ),
        ),
    )
    take = native_take(old, 0, prompt_ordinal=2)
    legacy = history_entry_for_take(take).model_copy(
        update={"take_id": "legacy-synthetic", "group_id": None, "legacy_session_id": "legacy"}
    )
    with RecordingIndex(layout) as index:
        index.register_catalog(CatalogSnapshot.embedded(old))
        digest = index.register_catalog(snapshot)
        index.set_completeness(digest, "complete")
        index.update_take(take)
        page = index.history(digest)
        assert (page.covered_prompt_count, page.total_take_count) == (0, 0)
        assert index.next_missing(
            digest, after_prompt_id=suite.prompts[0].prompt_id
        ).next_prompt == (reworded)
        with pytest.raises(RecordingIndexError, match="does not exactly match"):
            index.replace_legacy_catalog(digest, [(legacy, None, ())])
        # An existing row of that identity forces the serial path, which refuses it too.
        index.update_legacy_review(legacy)
        with pytest.raises(RecordingIndexError, match="does not exactly match"):
            index.replace_legacy_catalog(digest, [(legacy, None, ())])


@pytest.mark.parametrize("change", ["holdout", "silent-to-spoken", "spoken-to-silent"])
def test_an_alias_that_changes_the_prompts_identity_never_counts(
    layout: AssetLayout, change: str
) -> None:
    """Same prompt id, but held-out audio or the other behaviour: no reader of the index counts
    it, although the snapshot (which sees only digests) lists the alias."""
    base = synthetic_catalog(4)  # synthetic-0000 is a room tone, the rest are spoken
    recorded_suite, ordinal = base, 1
    if change == "holdout":
        recorded_suite = PromptSuite(
            suite_id="synthetic-evaluation",
            purpose="held-out-evaluation",
            prompts=tuple(prompt.model_copy(update={"holdout": True}) for prompt in base.prompts),
        )
        current = base.prompts[1].model_copy(update={"text": "Reworded synthetic prompt."})
    elif change == "silent-to-spoken":
        ordinal = 0
        current = base.prompts[0].model_copy(update={"category": "neutral", "text": "Spoken."})
    else:
        current = base.prompts[1].model_copy(update={"category": "room-tone", "text": "Quiet."})
    prompts = list(base.prompts)
    prompts[ordinal] = current
    suite = PromptSuite(suite_id=base.suite_id, purpose=base.purpose, prompts=tuple(prompts))
    recorded = recorded_suite.prompts[ordinal]
    digest = canonical_sha256(suite)
    snapshot = CatalogSnapshot(
        suite=suite,
        catalog_sha256=digest,
        prompt_aliases=(
            PromptAlias(
                prompt_sha256=canonical_sha256(current),
                accepted_prompt_sha256=canonical_sha256(recorded),
            ),
        ),
    )
    assert snapshot.current_for(canonical_sha256(recorded)) == canonical_sha256(current)
    assert snapshot.current_prompt_for(recorded) is None
    take = native_take(recorded_suite, 0, prompt_ordinal=ordinal)
    reference = CatalogReference(
        catalog_id="synthetic-corpus",
        catalog_version=1,
        catalog_sha256=digest,
        snapshot_sha256="f" * 64,
        suite_id=suite.suite_id,
        purpose=suite.purpose,
        snapshot_path=f"provenance/corpus/snapshots/{digest}.json",
    )
    proposal = native_take(suite, 1, reference=reference, prompt=recorded)
    legacy = history_entry_for_take(take).model_copy(
        update={"take_id": "legacy-synthetic", "group_id": None, "legacy_session_id": "legacy"}
    )
    after = suite.prompts[ordinal - 1].prompt_id
    with RecordingIndex(layout) as index:
        assert index.register_catalog(snapshot) == digest
        index.set_completeness(digest, "complete")
        index.update_take(take)  # indexes the recorded prompt row the SQL rule compares
        page = index.history(digest)
        assert (page.covered_prompt_count, page.total_take_count) == (0, 0)
        row = next(item for item in page.prompts if item.prompt_id == current.prompt_id)
        assert (row.status, row.take_count, row.kept_count) == ("missing", 0, 0)
        assert index.history(digest, prompt_id=current.prompt_id).takes == ()
        assert index.latest_retained_take(digest, current) is None
        assert index.next_missing(digest, after_prompt_id=after).next_prompt == current
        overlaid = index.next_missing(digest, after_prompt_id=after, proposed_take=proposal)
        assert overlaid.next_prompt == current
        with pytest.raises(RecordingPromptNotCountedError):
            index.review_aliases(digest, take.reservation.take_id)
        with pytest.raises(RecordingIndexError, match="does not exactly match"):
            index.replace_legacy_catalog(digest, [(legacy, None, ())])
        index.update_legacy_review(legacy)  # an existing identity forces the serial path
        with pytest.raises(RecordingIndexError, match="does not exactly match"):
            index.replace_legacy_catalog(digest, [(legacy, None, ())])
        index.invalidate_take(
            take.reservation.take_id,
            RecordingError(code="RECORDING.SOURCE_CHANGED", message="synthetic", retryable=False),
        )
        # Only the catalog the take was recorded under becomes Unknown.
        assert index.history(canonical_sha256(recorded_suite)).completeness == "unknown"
        assert index.history(digest).completeness == "complete"


@pytest.mark.parametrize(
    "changes",
    [{"suite_id": "not-the-snapshot-suite"}, {"purpose": "held-out-evaluation"}],
    ids=["suite-id", "purpose"],
)
def test_a_warm_index_refuses_a_take_whose_reference_disagrees_with_its_snapshot(
    revisions: Revisions, changes: dict[str, str]
) -> None:
    index = revisions.index
    kept, snapshot = revisions.kept
    legit = revisions.take(revisions.kept, 0, "spoken-0001")
    forged = native_take(
        snapshot.suite,
        1,
        reference=kept.model_copy(update=changes),
        prompt=snapshot.prompt("spoken-0002"),
        group_ordinal=1,
    )
    index.update_take(legit)
    index.set_completeness(kept.catalog_sha256, "complete")
    revision = index.revision
    for attempt in (
        lambda: index.update_take(forged),
        lambda: index.register_package_aliases((legit, forged), ()),
        lambda: index.reconcile_takes((legit, forged)),
    ):
        with pytest.raises(CorpusSnapshotSecurityError, match="declares suite") as caught:
            attempt()
        assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert index.history(kept.catalog_sha256).completeness == "unknown"
    assert index.revision > revision  # only reconcile's Unknown marking was written
    assert [take.take_id for take in index.history(kept.catalog_sha256).takes] == [
        legit.reservation.take_id
    ]


def test_a_missing_or_altered_snapshot_fails_before_any_index_write(
    revisions: Revisions,
) -> None:
    index = revisions.index
    (first, earlier), (kept, _) = revisions.first, revisions.kept
    take = revisions.take(revisions.kept, 0, "spoken-0001")
    path = revisions.layout.root / kept.snapshot_path
    payload = path.read_bytes()
    index.register_catalog(earlier)
    index.set_completeness(first.catalog_sha256, "complete")
    revision = index.revision
    path.unlink()
    for attempt in (
        lambda: index.update_take(take),
        lambda: index.register_package_aliases((take,), ()),
    ):
        with pytest.raises(CorpusSnapshotSecurityError) as caught:
            attempt()
        assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"
    assert index.revision == revision
    path.write_bytes(payload.replace(b"Synthetic sentence number one.", b"Synthetic sentence ONE."))
    path.chmod(0o600)
    with pytest.raises(CorpusSnapshotSecurityError) as caught:
        index.update_take(take)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert index.revision == revision
    with pytest.raises(CorpusSnapshotSecurityError):
        index.reconcile_takes((take,))
    # A failed rebuild leaves every indexed catalog Unknown, never Missing.
    assert index.history(first.catalog_sha256).completeness == "unknown"
    path.write_bytes(payload)
    path.chmod(0o600)
    index.update_take(take)
    assert index.history(kept.catalog_sha256).total_take_count == 1


def test_version_three_indexes_gain_empty_alias_sets_when_opened(
    layout: AssetLayout, catalog: PromptSuite
) -> None:
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(CatalogSnapshot.embedded(catalog))
        index.update_take(native_take(catalog, 0))
        path = index.path
    database = sqlite3.connect(path)
    try:
        for _ in range(2):  # Down and Up are each idempotent.
            database.executescript(recording_index_migration("down", version=4))
        assert database.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_schema")}
        assert not {"recording_catalog_alias_sets", "recording_prompt_aliases"} & tables
    finally:
        database.close()
    with RecordingIndex(layout) as index:
        connection = index.connection
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM recording_catalog_alias_sets")
        ] == [(digest, "cole-self", "[]")]
        assert index.register_catalog(CatalogSnapshot.embedded(catalog)) == digest
        index.set_completeness(digest, "complete")
        assert index.history(digest).covered_prompt_count == 1
        connection.executescript(recording_index_migration(version=4))
        connection.executescript(recording_index_migration(version=4))
        assert (
            connection.execute("SELECT count(*) FROM recording_catalog_alias_sets").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "statement, message",
    [
        (
            "UPDATE recording_prompt_aliases SET recording_accepted_prompt_sha256='"
            + "0" * 64
            + "'",
            "prompt aliases changed",
        ),
        (
            "UPDATE recording_catalog_alias_sets SET recording_alias_set_json='[]' "
            "WHERE recording_alias_set_json != '[]'",
            "prompt aliases changed",
        ),
        (
            "UPDATE recording_catalog_alias_sets SET recording_alias_set_json=' ' || "
            "recording_alias_set_json WHERE recording_alias_set_json != '[]'",
            "prompt aliases changed",
        ),
        (
            "UPDATE recording_catalog_alias_sets SET recording_alias_set_json='{}'",
            "prompt aliases are invalid",
        ),
        (
            "UPDATE recording_catalog_alias_sets SET recording_alias_set_json="
            "replace(recording_alias_set_json, 'accepted_prompt_sha256\":\"', "
            "'accepted_prompt_sha256\":\"x') WHERE recording_alias_set_json != '[]'",
            "prompt aliases are invalid",
        ),
        (
            "DELETE FROM recording_catalog_alias_sets WHERE recording_alias_set_json = '[]'",
            "has no alias set",
        ),
    ],
    ids=["row", "emptied-set", "noncanonical-set", "malformed-set", "invalid-digest", "no-set"],
)
def test_tampered_alias_projection_is_unknown_not_missing(
    revisions: Revisions, statement: str, message: str
) -> None:
    index = revisions.index
    index.register_catalog(revisions.first[1])
    digest = index.register_catalog(revisions.kept[1])
    assert index.history(digest).total_prompt_count == 4
    attacker = sqlite3.connect(index.path)
    try:
        with attacker:
            assert attacker.execute(statement).rowcount >= 1
    finally:
        attacker.close()
    with pytest.raises(RecordingIndexError, match=message):
        index.history(digest)


def test_an_alias_owned_by_another_current_prompt_is_invalid(revisions: Revisions) -> None:
    index = revisions.index
    snapshot = revisions.kept[1]
    digest = index.register_catalog(snapshot)
    forged = json.dumps(
        [
            {
                "accepted_prompt_sha256": canonical_sha256(snapshot.prompt("spoken-0000")),
                "prompt_sha256": canonical_sha256(snapshot.prompt("spoken-0001")),
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    attacker = sqlite3.connect(index.path)
    try:
        with attacker:
            attacker.execute(
                "UPDATE recording_catalog_alias_sets SET recording_alias_set_json=?", (forged,)
            )
    finally:
        attacker.close()
    with pytest.raises(RecordingIndexError, match="prompt aliases are invalid"):
        index.history(digest)


@pytest.mark.parametrize("embedded_first", [True, False])
def test_one_catalog_digest_cannot_carry_two_alias_sets(
    layout: AssetLayout, catalog: PromptSuite, embedded_first: bool
) -> None:
    earlier = catalog.prompts[1].model_copy(update={"text": "An earlier synthetic wording."})
    aliased = CatalogSnapshot(
        suite=catalog,
        catalog_sha256=canonical_sha256(catalog),
        prompt_aliases=(
            PromptAlias(
                prompt_sha256=canonical_sha256(catalog.prompts[1]),
                accepted_prompt_sha256=canonical_sha256(earlier),
            ),
        ),
    )
    embedded = CatalogSnapshot.embedded(catalog)
    registered, conflicting = (embedded, aliased) if embedded_first else (aliased, embedded)
    with RecordingIndex(layout) as index:
        digest = index.register_catalog(registered)
        revision = index.revision
        with pytest.raises(
            RecordingIndexError, match="different prompt aliases under its identity"
        ):
            index.register_catalog(conflicting)
        assert index.revision == revision
        assert index.register_catalog(registered) == digest
        # Removing the alias set through the owner's own connection is caught at registration.
        index.connection.execute("DELETE FROM recording_catalog_alias_sets")
        with pytest.raises(RecordingIndexError, match="has no alias set"):
            index.register_catalog(registered)

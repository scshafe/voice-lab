"""TC1-TC3: the pilot-v2 coverage consequence (design §3).

Coverage stays exact per prompt hash: pilot-v2's 34 spoken prompts keep every existing take,
its changed room tones start Missing, and no evidence file is removed or rewritten. Step 3
proved this on a pilot-v2 built in memory by the §4 contract; from Step 5 every case runs on
the released resource, which must equal that in-memory catalog.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from recording_fixtures import synthetic_pilot_v2_catalog
from test_recording_library import DEVICE as LEGACY_DEVICE
from test_recording_library import START, Clock, Process, Timer, take
from test_recording_packages import analyzer_build as analyzer_build
from test_recording_packages import saved_take
from test_recording_takes import DEVICE, NOW, Rig

from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.capture import CaptureInterrupted, TakeEngine
from cole_voice_lab.models import Prompt, PromptSuite, utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import legacy_pilot_v1, load_builtin_prompt_suite
from cole_voice_lab.recording_analysis import RecordingAnalysisQueue
from cole_voice_lab.recording_contracts import (
    DecisionCommand,
    OpenContextCommand,
    PreparePackageCommand,
    RecordingPromptNotCountedError,
    RecordingTake,
    ReviewTakeCommand,
)
from cole_voice_lab.recording_discovery import discover_recording_library
from cole_voice_lab.recording_discovery_contracts import LegacyProjectionRow
from cole_voice_lab.recording_edits import commit_legacy_edit
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_index_backfill import (
    backfill_recording_library,
    legacy_projection_records,
)
from cole_voice_lab.recording_library import RecordingLibrary
from cole_voice_lab.recording_packages import RecordingPackageStore
from cole_voice_lab.recording_review import RecordingReviewResolver
from cole_voice_lab.recording_takes import RecordingTakeStore

SUBJECT = "cole-self"
UMIK = DEVICE.model_copy(update={"display_name": "Synthetic UMIK-1"})
PILOT_V2_SHA256 = "0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a028"
PILOT_V2_ROOM_TONE_SHA256 = {
    "room-tone-start": "0377c02d0dd8bfbee08367edc73e016b0d156a9fc112aa7dc099a4f116eb90fb",
    "room-tone-end": "105b37fa88783ade0bb9fb995ae528ca814a54e935759b8978008507fb1f60d1",
}
ROOM_TONES = ("room-tone-start", "room-tone-end")


def pilot_v1() -> PromptSuite:
    return load_builtin_prompt_suite("pilot-v1.json")


def released_pilot_v2() -> PromptSuite:
    return load_builtin_prompt_suite("pilot-v2.json")


def spoken(number: int) -> str:
    """The pilot-v1 spoken prompt at catalog position ``number`` (room-tone-start is 0)."""
    prompt = pilot_v1().prompts[number]
    assert prompt.category != "room-tone"
    return prompt.prompt_id


def open_context(
    store: RecordingTakeStore, catalog: PromptSuite, operation: str
) -> Any:  # ContextEvent
    cursor = store.get_cursor(SUBJECT)
    return store.open_context(
        OpenContextCommand(
            operation_id=operation,
            subject_id=SUBJECT,
            catalog=catalog,
            catalog_sha256=canonical_sha256(catalog),
            capture_device=UMIK,
            capture_host="synthetic-host",
            expected_selection_revision=None
            if cursor is None
            else cursor.selection.selection_revision,
        )
    )


def publish(layout: AssetLayout, index: RecordingIndex, catalog: PromptSuite) -> dict[str, str]:
    """Publish one catalog's legacy projection; return every indexed row's payload."""
    backfill_recording_library(RecordingLibrary(layout, catalog, SUBJECT), index)
    return {
        row[0]: row[1]
        for row in index.connection.execute(
            "SELECT recording_take_id, recording_take_payload_json FROM recording_takes "
            "ORDER BY recording_take_id"
        )
    }


def unknown_records(index: RecordingIndex, catalog: PromptSuite) -> list[str]:
    return [
        row[0]
        for row in index.connection.execute(
            "SELECT recording_unknown_record_id FROM recording_unknown_records WHERE "
            "recording_catalog_sha256=? ORDER BY recording_unknown_record_id",
            (canonical_sha256(catalog),),
        )
    ]


def test_released_pilot_v2_follows_the_design_goldens() -> None:
    pilot, pilot_v2 = pilot_v1(), released_pilot_v2()
    assert pilot_v2 == synthetic_pilot_v2_catalog()
    assert canonical_sha256(pilot_v2) == PILOT_V2_SHA256
    assert [p.prompt_id for p in pilot_v2.prompts] == [p.prompt_id for p in pilot.prompts]
    for before, after in zip(pilot.prompts, pilot_v2.prompts, strict=True):
        if before.category == "room-tone":
            assert after.text is None and after.instructions == before.text
            assert canonical_sha256(after) == PILOT_V2_ROOM_TONE_SHA256[after.prompt_id]
        else:
            assert after == before and canonical_sha256(after) == canonical_sha256(before)
    assert sum(p.category != "room-tone" for p in pilot_v2.prompts) == 34
    assert legacy_pilot_v1(pilot_v2)


# TC1 ---------------------------------------------------------------------------------------


@dataclass
class NativeCoverage:
    rig: Rig
    index: RecordingIndex
    pilot_v1: PromptSuite
    pilot_v2: PromptSuite
    spoken: RecordingTake
    room_tone: RecordingTake


@pytest.fixture
def native_coverage(tmp_path: Path, analyzer_build: Any) -> Iterator[NativeCoverage]:
    """pilot-v1-group native takes on one spoken prompt and on room-tone-start."""
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    rig = Rig(layout, RecordingTakeStore(layout), "", now=utc_now() - timedelta(seconds=20))
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    pilot, pilot_v2 = pilot_v1(), released_pilot_v2()
    rig.group_id = rig.store.open_group(pilot, SUBJECT, UMIK, "synthetic-host").group_id
    index = RecordingIndex(layout, subject_id=SUBJECT)
    rig.store.projection_hook = index.update_take
    queue = RecordingAnalysisQueue(rig.store, index, build=analyzer_build)
    try:
        spoken_take = saved_take(rig, queue, 1, prompt=spoken(1))
        tone = saved_take(rig, queue, 0, prompt="room-tone-start")
        for catalog in (pilot, pilot_v2):
            backfill_recording_library(RecordingLibrary(layout, catalog, SUBJECT), index)
        yield NativeCoverage(rig, index, pilot, pilot_v2, spoken_take, tone)
    finally:
        queue.close()
        index.close()


def test_native_takes_count_under_pilot_v2_only_for_unchanged_prompts(
    native_coverage: NativeCoverage,
) -> None:
    """TC1."""
    state = native_coverage
    v1, v2 = canonical_sha256(state.pilot_v1), canonical_sha256(state.pilot_v2)
    tone_id = state.room_tone.reservation.take_id

    page = state.index.history(v2, page_size=200)
    statuses = {row.prompt_id: row for row in page.prompts}
    assert page.completeness == "complete" and page.covered_prompt_count == 1
    assert statuses[spoken(1)].status == "recorded" and statuses[spoken(1)].kept_count == 1
    for prompt_id in ROOM_TONES:
        assert statuses[prompt_id].status == "missing"
        assert statuses[prompt_id].take_count == 0
    assert {row.take_id for row in page.takes} == {state.spoken.reservation.take_id}
    assert state.index.history(v2, prompt_id="room-tone-start").takes == ()
    assert state.index.next_missing(v2).next_prompt.prompt_id == "room-tone-start"
    assert state.index.next_missing(v2, after_prompt_id=spoken(1)).next_prompt.prompt_id == spoken(
        2
    )

    store = state.rig.store
    resolver = RecordingReviewResolver(state.rig.layout, store, state.index)
    open_context(store, state.pilot_v2, "open-pilot-v2")
    # A room tone pilot-v2 changed is stale history under v2, not changed evidence.
    with pytest.raises(RecordingPromptNotCountedError, match="exact catalog"):
        resolver.resolve(v2, tone_id)
    assert resolver.resolve(v2, state.spoken.reservation.take_id).take_id == (
        state.spoken.reservation.take_id
    )

    open_context(store, state.pilot_v1, "open-pilot-v1")
    page = state.index.history(v1, page_size=200)
    statuses = {row.prompt_id: row for row in page.prompts}
    assert page.covered_prompt_count == 2 and statuses["room-tone-start"].status == "recorded"
    assert [row.take_id for row in state.index.history(v1, prompt_id="room-tone-start").takes] == [
        tone_id
    ]
    assert resolver.resolve(v1, tone_id).take_id == tone_id
    assert state.index.next_missing(v1).next_prompt.prompt_id == spoken(2)


# TC2 ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyEvidence:
    layout: AssetLayout
    finalized: str  # sidecar session with READY, kept, rejected and removed takes
    in_progress: str  # sidecar session with an interrupted draft
    ready_later: str  # sidecar session whose every take is kept, finalized between publications
    sidecar_less: str = "legacy-pilot"


def _advance(clock: Clock) -> None:
    clock.value += timedelta(minutes=1)


def _reject_then_keep(session: Any, clock: Clock) -> None:
    _advance(clock)
    session.start_take()
    session.stop_take()
    session.reject_take("Synthetic rejected take")
    _advance(clock)
    take(session)


def build_legacy_root(root: Path) -> LegacyEvidence:
    """Legacy pilot-v1 evidence: three sidecar sessions and one sidecar-less TakeEngine session."""
    layout = AssetLayout.initialize(root)
    suite = pilot_v1()
    engine = TakeEngine(
        layout=layout,
        prompt_suite=suite,
        subject_id=SUBJECT,
        device=LEGACY_DEVICE,
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

    clock = Clock()
    library = RecordingLibrary(
        layout,
        suite,
        SUBJECT,
        process_factory=Process,
        timer_factory=Timer,
        clock=clock,
        device_resolver=lambda device: device,
    )
    finalized = library.begin_session([spoken(1), spoken(2)], LEGACY_DEVICE)
    _reject_then_keep(finalized, clock)  # room-tone-start
    _reject_then_keep(finalized, clock)  # spoken 1
    for _ in range(2):  # spoken 2, room-tone-end
        _advance(clock)
        take(finalized)
    finalized.finalize()
    for row in library.rows():
        if row.prompt.prompt_id in (spoken(2), "room-tone-end"):
            entry = next(h for h in row.history if h.session_id == finalized.evidence.session_id)
            library.remove_take(entry, "Synthetic removal")

    in_progress = library.begin_session([spoken(1), spoken(3)], LEGACY_DEVICE)
    for _ in range(2):  # room-tone-start, spoken 1
        _advance(clock)
        take(in_progress)
    _advance(clock)
    in_progress.start_take()
    try:
        in_progress.abort()
    except CaptureInterrupted:
        pass
    else:  # pragma: no cover - fixture precondition
        raise AssertionError("an interrupted take must leave a partial draft")

    ready_later = library.begin_session([spoken(4)], LEGACY_DEVICE)
    for _ in range(3):
        _advance(clock)
        take(ready_later)
    ready_later.abort()
    return LegacyEvidence(
        layout,
        finalized.evidence.session_id,
        in_progress.evidence.session_id,
        ready_later.evidence.session_id,
    )


def prepare_pilot_v2_package(
    root: Path, analyzer_build: Any, prompt_ids: Sequence[str] | None = None
) -> tuple[AssetLayout, Any]:
    """Save synthetic verified native takes in a released pilot-v2 group; prepare one package."""
    layout = AssetLayout.initialize(root)
    rig = Rig(layout, RecordingTakeStore(layout), "", now=utc_now() - timedelta(seconds=20))
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    catalog = released_pilot_v2()
    rig.group_id = rig.store.open_group(catalog, SUBJECT, UMIK, "synthetic-host").group_id
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        rig.store.projection_hook = index.update_take
        queue = RecordingAnalysisQueue(rig.store, index, build=analyzer_build)
        try:
            takes = [
                saved_take(rig, queue, number, prompt=prompt.prompt_id)
                for number, prompt in enumerate(catalog.prompts)
                if prompt_ids is None or prompt.prompt_id in prompt_ids
            ]
            package = RecordingPackageStore(
                layout, take_store=rig.store, index=index, analyzer_build=analyzer_build
            ).prepare(
                PreparePackageCommand(
                    operation_id="prepare-pilot-v2",
                    group_id=rig.group_id,
                    take_ids=tuple(t.reservation.take_id for t in takes),
                    room_tone_take_ids=tuple(
                        t.reservation.take_id
                        for t in takes
                        if t.reservation.prompt.category == "room-tone"
                    ),
                )
            )
        finally:
            queue.close()
    return layout, package


@pytest.fixture(scope="session")
def foreign_pilot_v2_package(
    tmp_path_factory: pytest.TempPathFactory, analyzer_build: Any
) -> tuple[Path, str]:
    """A full cole-pilot-v2 native package staged in another root; it has no local map here."""
    root = tmp_path_factory.mktemp("foreign-pilot-v2")
    layout, package = prepare_pilot_v2_package(root / "assets", analyzer_build)
    assert package.selected_suite.suite_id == "cole-pilot-v2"
    return layout.root / package.package_path, package.session_id


def add_foreign_package(evidence: LegacyEvidence, package: tuple[Path, str]) -> str:
    source, session_id = package
    shutil.copytree(source, evidence.layout.directory("tmp") / f"capture-{session_id}")
    return session_id


def test_legacy_rows_are_identical_across_pilot_v1_and_pilot_v2_publications(
    tmp_path: Path, foreign_pilot_v2_package: tuple[Path, str]
) -> None:
    """TC2."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    foreign = add_foreign_package(evidence, foreign_pilot_v2_package)
    layout, pilot, pilot_v2 = evidence.layout, pilot_v1(), released_pilot_v2()
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        first = publish(layout, index, pilot)
        coverage = index.history(canonical_sha256(pilot), page_size=200)
        assert {row.prompt_id for row in coverage.prompts if row.kept_count} == {
            p.prompt_id for p in pilot.prompts
        }

        assert publish(layout, index, pilot_v2) == first
        page = index.history(canonical_sha256(pilot_v2), page_size=200)
        assert not page.has_more
        assert all(row.prompt.category != "room-tone" for row in page.takes)
        assert {row.legacy_session_id for row in page.takes} == {
            evidence.finalized,
            evidence.in_progress,
            evidence.ready_later,
            evidence.sidecar_less,
        }
        prompts = {row.prompt_id: row for row in page.prompts}
        assert prompts[spoken(1)].kept_count == 3  # finalized, in progress and sidecar-less
        assert prompts[spoken(1)].take_count == 4  # plus the rejected take
        assert prompts[spoken(3)].take_count == 2  # the interrupted draft stays listed
        assert page.covered_prompt_count == 34
        for prompt_id in ROOM_TONES:
            assert prompts[prompt_id].take_count == 0 and prompts[prompt_id].kept_count == 0

        assert publish(layout, index, pilot) == first
        again = index.history(canonical_sha256(pilot), page_size=200)
        assert again.prompts == coverage.prompts and again.takes == coverage.takes
        for catalog in (pilot, pilot_v2):
            assert unknown_records(index, catalog) == [f"legacy:{foreign}"]


def test_legacy_edit_and_ready_between_publications_keep_one_row_per_event(
    tmp_path: Path,
) -> None:
    """TC2 variant: a pilot-v2 review decision and a new READY file between publications."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    layout, pilot, pilot_v2 = evidence.layout, pilot_v1(), released_pilot_v2()
    v2 = canonical_sha256(pilot_v2)
    spoken_hashes = {canonical_sha256(p) for p in pilot_v2.prompts if p.category != "room-tone"}
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        publish(layout, index, pilot)
        publish(layout, index, pilot_v2)
        store = RecordingTakeStore(
            layout, clock=lambda: NOW, index_revision_resolver=lambda: index.revision
        )
        context = open_context(store, pilot_v2, "open-pilot-v2")
        target = next(
            row
            for row in index.history(v2, prompt_id=spoken(5)).takes
            if row.legacy_session_id == evidence.sidecar_less
        )
        resolver = RecordingReviewResolver(layout, store, index)
        source = resolver.resolve(v2, target.take_id)
        store.select_review(
            ReviewTakeCommand(
                operation_id="review-legacy",
                take_id=target.take_id,
                selection=context.cursor.selection,
            ),
            source,
        )
        cursor = store.get_cursor(SUBJECT)
        entry, _ = index.review_aliases(v2, target.take_id)
        intent = commit_legacy_edit(
            layout,
            request=DecisionCommand(
                operation_id="reject-legacy",
                take_id=target.take_id,
                selection=cursor.selection,
                expected_decision_revision=source.decision_revision,
                action="reject",
                reason="Synthetic review decision",
            ),
            source=source,
            cursor=cursor,
            entry=entry,
            committed_at=NOW,
        )
        index.update_legacy_review(intent.projection)

        later = Clock()
        later.value += timedelta(days=1)
        library = RecordingLibrary(
            layout,
            pilot,
            SUBJECT,
            process_factory=Process,
            timer_factory=Timer,
            clock=later,
            device_resolver=lambda device: device,
        )
        library.resume_session(evidence.ready_later).finalize()

        def shared_rows() -> dict[str, str]:
            return {
                row[0]: row[2]
                for row in index.connection.execute(
                    "SELECT recording_take_id, recording_prompt_sha256, "
                    "recording_take_payload_json FROM recording_takes"
                )
                if row[1] in spoken_hashes
            }

        publish(layout, index, pilot_v2)
        after_v2 = shared_rows()
        publish(layout, index, pilot)
        assert shared_rows() == after_v2
        publish(layout, index, pilot_v2)
        assert shared_rows() == after_v2

        rows = index.history(v2, page_size=200).takes
        events = [
            (row.legacy_session_id, row.prompt.prompt_id, row.recorded_at, row.source_sha256)
            for row in rows
        ]
        assert len(events) == len(set(events))
        edited = next(row for row in rows if row.take_id == target.take_id)
        assert edited.decision == "rejected"
        assert edited.state_revision == intent.projection.state_revision > 0
        assert {row.export for row in rows if row.legacy_session_id == evidence.ready_later} == {
            "prepared"
        }


def test_private_catalog_sharing_pilot_prompts_publishes_identical_rows(tmp_path: Path) -> None:
    """TC2 L1: legacy rows on a prompt hash never depend on the publishing catalog."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    layout, pilot = evidence.layout, pilot_v1()
    shared = pilot.prompts[1:4]
    private = PromptSuite(
        suite_id="cole-private-shared-v1",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="private-room-tone",
                text="Remain silent for the private catalog.",
                category="room-tone",
                style="Silence",
                expected_seconds=10.0,
            ),
            *shared,
        ),
    )
    hashes = {canonical_sha256(prompt) for prompt in shared}

    def rows(catalog: PromptSuite) -> dict[str, bytes]:
        def native(*_: object) -> None:
            raise AssertionError("legacy-only root published a native take")

        library = RecordingLibrary(layout, catalog, SUBJECT)
        return {
            record.entry.take_id: canonical_json_bytes(record)
            for record in legacy_projection_records(library, native)
            if isinstance(record, LegacyProjectionRow)
            and canonical_sha256(record.entry.prompt) in hashes
        }

    expected = rows(pilot)
    assert {
        LegacyProjectionRow.model_validate_json(payload).entry.legacy_session_id
        for payload in expected.values()
    } == {evidence.finalized, evidence.in_progress, evidence.sidecar_less}
    assert rows(private) == expected
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        before = publish(layout, index, pilot)
        assert publish(layout, index, private) == before


# TC3 ---------------------------------------------------------------------------------------


def evidence_files(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


# Mutable pointers, not evidence: every open-context commit advances the subject cursor and
# the store head (the design's TC3 list names only the cursor), and projections are disposable.
MUTABLE_POINTERS = (
    f"provenance/recording-takes/.cursors/{SUBJECT}.json",
    "provenance/recording-takes/.store-head.json",
)


def _projection_or_pointer(relative: str) -> bool:
    return relative in MUTABLE_POINTERS or relative.startswith(
        ("provenance/recording-library/", "provenance/recording-library-discovery/")
    )


def test_switching_catalogs_removes_and_rewrites_no_evidence(tmp_path: Path) -> None:
    """TC3."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    layout, pilot, pilot_v2 = evidence.layout, pilot_v1(), released_pilot_v2()
    original = evidence_files(layout.root)
    store = RecordingTakeStore(layout, clock=lambda: NOW)
    for number, catalog in enumerate((pilot, pilot_v2, pilot)):
        before = evidence_files(layout.root)
        context = open_context(store, catalog, f"switch-{number}")
        assert context.cursor.selection.prompt_id == "room-tone-start"
        assert context.cursor.selection.take_id is None
        assert discover_recording_library(layout, context.group.group_id)["completeness"] == (
            "complete"
        )
        after = evidence_files(layout.root)
        changed = sorted(
            path
            for path, digest in before.items()
            if after.get(path) != digest and not _projection_or_pointer(path)
        )
        assert changed == []
        assert set(original) <= set(after)
        assert {path for path in before if not _projection_or_pointer(path)} <= set(after)
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        assert index.history(canonical_sha256(pilot_v2)).covered_prompt_count == 34
        assert index.history(canonical_sha256(pilot)).covered_prompt_count == 36


def test_switching_store_catalogs_through_the_service_rewrites_no_evidence_or_store_file(
    tmp_path: Path,
) -> None:
    """TC3 on the app's path: each launch pins the active store version and opens it.

    The service opens the corpus store (lease, lock, WAL) for the catalog and open-context
    requests and closes it again; no file under ``provenance/corpus/`` may change, appear or
    disappear, so the mutable-path allowlist needs no corpus entry.
    """
    from test_recording_service import execute

    from cole_voice_lab.corpus_store import open_corpus_store
    from cole_voice_lab.recording_contracts import CatalogCommand, CatalogEvent, ContextEvent
    from cole_voice_lab.recording_service import RecordingWorkspaceBackend

    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    layout, pilot_v2 = evidence.layout, released_pilot_v2()
    with open_corpus_store(layout, create=True) as corpus:
        corpus.import_prompt_suite(pilot_v2, source="pilot-v2")
        corpus.create_catalog("cole-corpus", purpose=pilot_v2.purpose)
        whole = corpus.publish_version("cole-corpus")
        corpus.create_catalog("cole-corpus-short", purpose=pilot_v2.purpose)
        short = corpus.publish_version(
            "cole-corpus-short",
            tuple(
                prompt.prompt_id for prompt in pilot_v2.prompts if prompt.prompt_id != ROOM_TONES[1]
            ),
        )
    original = evidence_files(layout.root)

    def corpus_files(files: dict[str, str]) -> dict[str, str]:
        return {
            path: digest for path, digest in files.items() if path.startswith("provenance/corpus/")
        }

    for number, reference in enumerate((whole, short, whole)):
        # The operator's activation is a deliberate store write before the next launch.
        with open_corpus_store(layout, create=False) as corpus:
            corpus.activate(SUBJECT, reference)
        before = evidence_files(layout.root)
        assert corpus_files(before)
        backend = RecordingWorkspaceBackend(layout, SUBJECT, discover=False)
        try:
            delivered = execute(backend, CatalogCommand(operation_id=f"catalog-{number}"))
            assert isinstance(delivered, CatalogEvent) and delivered.reference == reference
            cursor = backend.store.get_cursor(SUBJECT)
            context = execute(
                backend,
                OpenContextCommand(
                    operation_id=f"store-switch-{number}",
                    subject_id=SUBJECT,
                    catalog_reference=reference,
                    capture_device=UMIK,
                    capture_host="synthetic-host",
                    expected_selection_revision=None
                    if cursor is None
                    else cursor.selection.selection_revision,
                ),
            )
        finally:
            backend.index.close()
        assert isinstance(context, ContextEvent)
        assert context.group.catalog_reference == reference
        assert context.cursor.selection.prompt_id == "room-tone-start"
        assert context.cursor.selection.take_id is None
        assert discover_recording_library(layout, context.group.group_id)["completeness"] == (
            "complete"
        )
        after = evidence_files(layout.root)
        changed = sorted(
            path
            for path, digest in before.items()
            if after.get(path) != digest and not _projection_or_pointer(path)
        )
        assert changed == []
        assert corpus_files(after) == corpus_files(before)
        assert set(original) <= set(after)
        assert {path for path in before if not _projection_or_pointer(path)} <= set(after)
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        for reference in (whole, short):
            assert index.history(reference.catalog_sha256).covered_prompt_count == 34

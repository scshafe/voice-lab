"""One published catalog version, every recording reader (corpus-store Step 3).

End to end on one asset root: a context opened on a published catalog version records a
kept take. A keep-recordings edit keeps that take counting in every reader (indexed history
and Save & Next, selection and review, the coverage report, and the fine-tune corpus, whose
transcript becomes the corrected text). A default edit leaves the prompt missing in every
reader. A referenced snapshot that disappears or changes fails every reader closed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from test_finetune import FFMPEG, needs_ffmpeg
from test_recording_takes import DEVICE, Rig, publish_catalog, store_suite

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.cli import main
from cole_voice_lab.corpus_store import CorpusSnapshotSecurityError, open_corpus_store
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.finetune import (
    FineTuneCorpus,
    materialize_finetune_dataset,
    select_finetune_corpus,
)
from cole_voice_lab.models import Prompt
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_coverage import prompt_coverage_report
from cole_voice_lab.recording_contracts import (
    CatalogReference,
    CatalogSnapshot,
    ContextEvent,
    DecisionCommand,
    NavigateCommand,
    NavigationEvent,
    OpenContextCommand,
    RecordingPromptNotCountedError,
    RecordingTake,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_library import RecordingLibrary
from cole_voice_lab.recording_review import RecordingReviewResolver
from cole_voice_lab.recording_takes import RecordingTakeStore

SUBJECT = "synthetic-subject"
CATALOG_ID = "synthetic-corpus"
CORRECTED = "Synthetic sentence zero."
PROMPT_IDS = ("spoken-0000", "spoken-0001", "spoken-0002")

type Version = tuple[CatalogReference, CatalogSnapshot]


def launch(rig: Rig, index: RecordingIndex, version: Version, operation: str) -> ContextEvent:
    """What the service does for a context on ``version``: index it, bind, open."""
    reference, snapshot = version
    index.register_catalog(snapshot)
    # The root holds no legacy evidence, so discovery would complete the catalog at once.
    index.set_completeness(reference.catalog_sha256, "complete")
    RecordingLibrary(rig.layout, snapshot, SUBJECT).bind_native_store(rig.store, index)
    cursor = rig.store.get_cursor(SUBJECT)
    return rig.store.open_context(
        OpenContextCommand(
            operation_id=operation,
            subject_id=SUBJECT,
            catalog_reference=reference,
            capture_device=DEVICE,
            capture_host="host",
            expected_selection_revision=None
            if cursor is None
            else cursor.selection.selection_revision,
        )
    )


@dataclass
class Library:
    """A root whose first published version holds one kept take on ``spoken-0000``."""

    rig: Rig
    index: RecordingIndex
    first: Version
    take: RecordingTake

    @property
    def layout(self) -> AssetLayout:
        return self.rig.layout

    @property
    def take_id(self) -> str:
        return self.take.reservation.take_id

    @property
    def recorded(self) -> Prompt:
        return self.take.reservation.prompt

    def launch(self, version: Version, operation: str) -> ContextEvent:
        return launch(self.rig, self.index, version, operation)

    def edit(self, text: str, *, keep_recordings: bool) -> Version:
        with open_corpus_store(self.layout, create=False) as corpus:
            corpus.edit_prompt("spoken-0000", text=text, keep_recordings=keep_recordings)
            reference = corpus.publish_version(CATALOG_ID)
            return reference, corpus.materialize(reference)

    def select(self, prompt_id: str, operation: str) -> NavigationEvent:
        cursor = self.rig.store.get_cursor(SUBJECT)
        assert cursor is not None
        return self.rig.store.navigate(
            NavigateCommand(
                operation_id=operation,
                selection=cursor.selection,
                mode="selected-prompt",
                target_prompt_id=prompt_id,
            ),
            subject_id=SUBJECT,
        )

    def resolver(self) -> RecordingReviewResolver:
        return RecordingReviewResolver(self.layout, self.rig.store, self.index)


@pytest.fixture
def library(tmp_path: Path) -> Iterator[Library]:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    first = publish_catalog(layout, store_suite(3), catalog_id=CATALOG_ID)
    rig = Rig(layout, RecordingTakeStore(layout), "")
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        context = launch(rig, index, first, "context-first")
        assert context.group.catalog_reference == first[0] and context.group.catalog is None
        rig.group_id = context.group.group_id
        reservation = rig.store.reserve(
            operation_id="reserve-first",
            group_id=rig.group_id,
            prompt_id="spoken-0000",
            process_generation="helper-generation",
            selection=context.cursor.selection,
        )
        sealed = rig.seal(reservation, "seal-first")
        cursor = rig.store.get_cursor(SUBJECT)
        assert cursor is not None
        # The store's projection hook indexes the referenced take as it commits.
        take = rig.store.save(rig.command(sealed, "save-first", selection=cursor.selection)).take
        assert take.decision == "kept"
        assert index.history(first[0].catalog_sha256).covered_prompt_count == 1
        yield Library(rig, index, first, take)


def test_a_keep_recordings_edit_keeps_the_take_counting_in_every_reader(
    library: Library,
) -> None:
    second = library.edit(CORRECTED, keep_recordings=True)
    reference, snapshot = second
    current = snapshot.prompt("spoken-0000")
    assert current is not None and current.text == CORRECTED and current != library.recorded
    context = library.launch(second, "context-second")
    assert context.group.catalog_reference == reference

    # Indexed history and Save & Next: the prompt is covered, so continuing skips it.
    page = library.index.history(reference.catalog_sha256)
    assert (page.covered_prompt_count, page.missing_prompt_count) == (1, 2)
    row = next(item for item in page.prompts if item.prompt_id == "spoken-0000")
    assert (row.prompt_sha256, row.kept_count) == (canonical_sha256(current), 1)
    assert library.index.next_missing(reference.catalog_sha256).next_prompt == snapshot.prompt(
        "spoken-0001"
    )
    assert library.index.latest_retained_take(reference.catalog_sha256, current) == library.take_id

    # Selection opens the old take on the current prompt; review authorizes it.
    selected = library.select("spoken-0000", "select-second")
    assert selected.result.next_prompt == current
    assert selected.result.selected_take_id == library.take_id
    source = library.resolver().resolve(reference.catalog_sha256, library.take_id)
    assert source.prompt == library.recorded

    # The coverage report agrees with the index: changed, still covered, nothing lost.
    report = prompt_coverage_report(library.layout, SUBJECT, snapshot, library.first[1])
    rows = {item.prompt_id: item for item in report.prompts}
    edited = rows["spoken-0000"]
    assert (edited.covered, edited.retained_take_count) == (True, 1)
    assert edited.changed is True and edited.covered_in_baseline is True
    assert report.changed_prompt_ids == ("spoken-0000",) and report.lost_prompt_ids == ()
    assert report.missing_prompt_ids == PROMPT_IDS[1:]
    assert report.covered_prompt_count == page.covered_prompt_count
    assert report.exit_status == 0

    # The fine-tune corpus keeps the old take and trains on the corrected text.
    corpus = select_finetune_corpus(library.layout, snapshot, SUBJECT, reference=reference)
    (entry,) = corpus.entries
    assert (entry.take_id, entry.prompt, entry.text) == (library.take_id, current, CORRECTED)
    assert entry.prompt_sha256 == canonical_sha256(current)
    assert entry.recorded_prompt_sha256 == canonical_sha256(library.recorded)
    assert corpus.catalog_reference == reference
    assert corpus.catalog_sha256 == reference.catalog_sha256
    assert corpus.missing_prompt_ids == PROMPT_IDS[1:]
    # Under the version it was recorded against, the same take keeps its recorded text.
    earlier = select_finetune_corpus(
        library.layout, library.first[1], SUBJECT, reference=library.first[0]
    )
    assert earlier.entries[0].prompt == library.recorded
    assert earlier.entries[0].recorded_prompt_sha256 == earlier.entries[0].prompt_sha256


@needs_ffmpeg
def test_the_training_transcript_of_a_kept_recording_is_the_corrected_text(
    library: Library,
) -> None:
    reference, snapshot = library.edit(CORRECTED, keep_recordings=True)
    corpus = select_finetune_corpus(library.layout, snapshot, SUBJECT, reference=reference)
    dataset = materialize_finetune_dataset(
        library.layout, corpus, "derived/finetune/alias/dataset", ffmpeg=FFMPEG
    )
    root = library.layout.root / dataset.dataset_root
    assert [(clip.take_id, clip.text) for clip in dataset.clips] == [(library.take_id, CORRECTED)]
    lines = (root / "metadata.csv").read_text(encoding="utf-8").splitlines()
    assert lines == [
        "audio_file|text",
        f"{(root / 'wavs' / 'spoken-0000.wav').resolve()}|{CORRECTED}",
    ]
    stored = FineTuneCorpus.model_validate_json((root / "corpus.json").read_bytes())
    assert stored == corpus and stored.catalog_reference == reference


def test_a_default_edit_leaves_the_prompt_missing_in_every_reader(library: Library) -> None:
    changed = library.edit("A different sentence entirely.", keep_recordings=False)
    reference, snapshot = changed
    assert snapshot.prompt_aliases == ()
    library.launch(changed, "context-changed")
    current = snapshot.prompt("spoken-0000")
    assert current is not None

    page = library.index.history(reference.catalog_sha256)
    assert (page.covered_prompt_count, page.total_take_count) == (0, 0)
    assert library.index.next_missing(reference.catalog_sha256).next_prompt == current
    assert library.index.latest_retained_take(reference.catalog_sha256, current) is None

    selected = library.select("spoken-0000", "select-changed")
    assert selected.result.next_prompt == current
    assert selected.result.selected_take_id is None
    with pytest.raises(RecordingPromptNotCountedError, match="exact catalog"):
        library.resolver().resolve(reference.catalog_sha256, library.take_id)

    report = prompt_coverage_report(library.layout, SUBJECT, snapshot, library.first[1])
    assert report.covered_prompt_count == 0 and report.missing_prompt_ids == PROMPT_IDS
    assert report.changed_prompt_ids == report.lost_prompt_ids == ("spoken-0000",)
    assert report.exit_status == 0  # the loss is explained by the edit

    corpus = select_finetune_corpus(library.layout, snapshot, SUBJECT, reference=reference)
    assert corpus.entries == () and corpus.missing_prompt_ids == PROMPT_IDS
    # The version the take was recorded against still counts it.
    assert prompt_coverage_report(library.layout, SUBJECT, library.first[1]).covered_prompt_count
    assert library.index.history(library.first[0].catalog_sha256).covered_prompt_count == 1


@pytest.mark.parametrize(
    ("damage", "code"),
    [("altered", "CORPUS.SNAPSHOT_MISMATCH"), ("missing", "CORPUS.SNAPSHOT_MISSING")],
)
def test_a_damaged_snapshot_fails_every_reader_closed(
    library: Library, damage: str, code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    layout, take = library.layout, library.take
    second_reference, second = library.edit(CORRECTED, keep_recordings=True)
    # The take's group seals the first version; the version being read is intact.
    path = layout.root / library.first[0].snapshot_path
    payload = path.read_bytes()
    library.index.close()  # every reader below starts fresh, as after a relaunch
    if damage == "altered":
        path.write_bytes(payload.replace(b"number 1.", b"number 9."))
    else:
        path.unlink()

    def refused(reader: Callable[[], object]) -> None:
        with pytest.raises(CorpusSnapshotSecurityError) as caught:
            reader()
        assert caught.value.code == code and isinstance(caught.value, SecurityError)

    group_id = take.reservation.group.group_id
    refused(lambda: RecordingTakeStore(layout).get_group(group_id))
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        revision = index.revision
        refused(lambda: index.update_take(take))
        assert index.revision == revision
    refused(lambda: prompt_coverage_report(layout, SUBJECT, second))
    refused(lambda: select_finetune_corpus(layout, second, SUBJECT, reference=second_reference))
    # The command line reports the refusal instead of a coverage number.
    arguments = ["prompts", "coverage-report", "--asset-root", str(layout.root)]
    capsys.readouterr()
    assert main([*arguments, "--subject-id", SUBJECT, "--catalog", "pilot-v1"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "catalog snapshot" in captured.err

    path.write_bytes(payload)
    path.chmod(0o600)
    report = prompt_coverage_report(layout, SUBJECT, second)
    assert report.covered_prompt_count == 1
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        index.update_take(take)
        index.register_catalog(second)
        index.set_completeness(second_reference.catalog_sha256, "complete")
        assert index.history(second_reference.catalog_sha256).covered_prompt_count == 1


def _keep(library: Library, prompt_id: str, name: str) -> RecordingTake:
    """Record and keep one take on the cursor's current group, as Save does."""
    rig = library.rig
    cursor = rig.store.get_cursor(SUBJECT)
    assert cursor is not None
    reservation = rig.store.reserve(
        operation_id=f"reserve-{name}",
        group_id=cursor.group_id,
        prompt_id=prompt_id,
        process_generation="helper-generation",
        selection=cursor.selection,
    )
    sealed = rig.seal(reservation, f"seal-{name}")
    cursor = rig.store.get_cursor(SUBJECT)
    assert cursor is not None
    return rig.store.save(rig.command(sealed, f"save-{name}", selection=cursor.selection)).take


def test_the_latest_recording_wins_whichever_revision_it_was_recorded_against(
    library: Library,
) -> None:
    """Old-text and new-text recordings of a kept prompt compete by capture date only."""
    rig = library.rig
    # A later old-text take, recorded before the edit is published and launched.
    rig.now = library.take.reservation.requested_at + timedelta(hours=2)
    later_old = _keep(library, "spoken-0000", "later-old")
    second = library.edit(CORRECTED, keep_recordings=True)
    reference, snapshot = second
    current = snapshot.prompt("spoken-0000")
    assert current is not None
    # The relaunch happens on a clock between the two old-text takes.
    rig.now = library.take.reservation.requested_at + timedelta(hours=1)
    library.launch(second, "context-second")
    new = _keep(library, "spoken-0000", "new")
    assert new.reservation.prompt == current
    recorded = {
        take.reservation.take_id: take.reservation.requested_at
        for take in (library.take, later_old, new)
    }
    assert sorted(recorded, key=recorded.__getitem__) == [
        library.take_id,
        new.reservation.take_id,
        later_old.reservation.take_id,
    ]

    corpus = select_finetune_corpus(library.layout, snapshot, SUBJECT, reference=reference)
    (entry,) = corpus.entries
    assert entry.take_id == later_old.reservation.take_id
    assert (entry.prompt, entry.text) == (current, CORRECTED)
    assert entry.recorded_prompt_sha256 == canonical_sha256(library.recorded)
    latest = library.index.latest_retained_take(reference.catalog_sha256, current)
    assert latest == later_old.reservation.take_id
    report = prompt_coverage_report(library.layout, SUBJECT, snapshot)
    assert report.prompts[0].retained_take_count == 3
    page = library.index.history(reference.catalog_sha256)
    assert (page.prompts[0].kept_count, page.covered_prompt_count) == (3, 1)

    # Selecting the prompt opens that old-text take; removing it leaves the new-text take.
    rig.now = later_old.reservation.requested_at + timedelta(hours=1)
    selected = library.select("spoken-0000", "select-latest")
    assert selected.result.selected_take_id == later_old.reservation.take_id
    rig.store.decide(
        DecisionCommand(
            operation_id="remove-later-old",
            selection=selected.cursor.selection,
            take_id=later_old.reservation.take_id,
            expected_decision_revision=later_old.decision_revision,
            action="remove",
            reason="synthetic removal",
        )
    )
    corpus = select_finetune_corpus(library.layout, snapshot, SUBJECT, reference=reference)
    (entry,) = corpus.entries
    assert entry.take_id == new.reservation.take_id
    assert entry.recorded_prompt_sha256 == entry.prompt_sha256 == canonical_sha256(current)
    assert library.index.latest_retained_take(reference.catalog_sha256, current) == (
        new.reservation.take_id
    )
    assert (
        prompt_coverage_report(library.layout, SUBJECT, snapshot).prompts[0].retained_take_count
        == 2
    )

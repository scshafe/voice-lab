"""Review authorization and prompt selection across published catalog versions.

A keep-recordings edit keeps an earlier take counting for its prompt, so the operator can
still select and review it under the new version; a default edit leaves it outside.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_pilot_v2_coverage import SUBJECT as LEGACY_SUBJECT
from test_pilot_v2_coverage import UMIK, build_legacy_root, publish, released_pilot_v2, spoken
from test_recording_service import request
from test_recording_takes import DEVICE, Rig, publish_catalog, store_suite

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    CatalogReference,
    CatalogSnapshot,
    ContextEvent,
    NavigateCommand,
    NavigationEvent,
    OpenContextCommand,
    RecordingCursor,
    RecordingPromptNotCountedError,
    RecordingTake,
    ReviewTakeCommand,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_review import RecordingReviewResolver
from cole_voice_lab.recording_service import (
    RecordingCancellation,
    RecordingServiceError,
    RecordingWorkspaceBackend,
)
from cole_voice_lab.recording_takes import RecordingSelectionError, RecordingTakeStore
from cole_voice_lab.storage_binding import open_catalog_editor

SUBJECT = "synthetic-subject"
CATALOG_ID = "synthetic-corpus"


@dataclass
class Versions:
    rig: Rig
    index: RecordingIndex
    first: CatalogReference
    take: RecordingTake

    @property
    def cursor(self) -> RecordingCursor:
        cursor = self.rig.store.get_cursor(SUBJECT)
        assert cursor is not None
        return cursor

    def open(self, reference: CatalogReference, operation: str) -> ContextEvent:
        return self.rig.store.open_context(
            OpenContextCommand(
                operation_id=operation,
                subject_id=SUBJECT,
                catalog_reference=reference,
                capture_device=DEVICE,
                capture_host="host",
                expected_selection_revision=self.cursor.selection.selection_revision,
            )
        )

    def select(self, prompt_id: str, operation: str) -> NavigationEvent:
        return self.rig.store.navigate(
            NavigateCommand(
                operation_id=operation,
                selection=self.cursor.selection,
                mode="selected-prompt",
                target_prompt_id=prompt_id,
            ),
            subject_id=SUBJECT,
        )

    def resolver(self) -> RecordingReviewResolver:
        return RecordingReviewResolver(self.rig.layout, self.rig.store, self.index)


def edit_and_publish(
    layout: AssetLayout, text: str, *, keep_recordings: bool
) -> tuple[CatalogReference, CatalogSnapshot]:
    with open_catalog_editor(layout) as corpus:
        corpus.edit_prompt(
            "spoken-0000",
            text=text,
            keep_recordings=keep_recordings,
            allow_rewording=keep_recordings,
        )
        reference = corpus.publish_version(CATALOG_ID)
        return reference, corpus.materialize(reference)


@pytest.fixture
def versions(tmp_path: Path) -> Iterator[Versions]:
    """One kept take on ``spoken-0000``, recorded under version 1 of a published catalog."""
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    rig = Rig(layout, RecordingTakeStore(layout), "")
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    first, _ = publish_catalog(layout, store_suite(3), catalog_id=CATALOG_ID)
    context = rig.store.open_context(
        OpenContextCommand(
            operation_id="context-first",
            subject_id=SUBJECT,
            catalog_reference=first,
            capture_device=DEVICE,
            capture_host="host",
        )
    )
    rig.group_id = context.group.group_id
    selection = context.cursor.selection
    reservation = rig.store.reserve(
        operation_id="reserve-first",
        group_id=rig.group_id,
        prompt_id=selection.prompt_id,
        process_generation="helper-generation",
        selection=selection,
    )
    sealed = rig.seal(reservation, "seal-first")
    cursor = rig.store.get_cursor(SUBJECT)
    assert cursor is not None
    take = rig.store.save(rig.command(sealed, "save-first", selection=cursor.selection)).take
    assert take.decision == "kept"
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        yield Versions(rig, index, first, take)


def test_keep_recordings_edits_keep_an_old_take_selectable_and_reviewable(
    versions: Versions,
) -> None:
    take_id = versions.take.reservation.take_id
    recorded = versions.take.reservation.prompt
    second, snapshot = edit_and_publish(
        versions.rig.layout, "Synthetic sentence zero.", keep_recordings=True
    )
    third, latest = edit_and_publish(
        versions.rig.layout, "Synthetic sentence Zero.", keep_recordings=True
    )
    for reference, catalog, operation in (
        (second, snapshot, "second"),
        (third, latest, "third"),
    ):
        context = versions.open(reference, f"context-{operation}")
        assert context.group.catalog_reference == reference
        current = catalog.prompt("spoken-0000")
        assert current is not None and current != recorded
        # The index (projection) nominates the take; the head check accepts the alias.
        versions.rig.store.retained_take_resolver = (
            lambda digest, prompt, ref=reference, cur=current: (
                take_id if (digest, prompt) == (ref.catalog_sha256, cur) else None
            )
        )
        selected = versions.select("spoken-0000", f"select-{operation}")
        assert selected.result.next_prompt == current
        assert selected.result.selected_take_id == take_id
        assert selected.cursor.selection.take_id == take_id
        versions.select("spoken-0001", f"away-{operation}")
        source = versions.resolver().resolve(reference.catalog_sha256, take_id)
        # The source keeps the recorded prompt as evidence; its id selects the current one.
        assert source.prompt == recorded and source.catalog_sha256 == reference.catalog_sha256
        review = versions.rig.store.select_review(
            ReviewTakeCommand(
                operation_id=f"review-{operation}",
                take_id=take_id,
                selection=versions.cursor.selection,
            ),
            source,
        )
        assert review.cursor.selection.prompt_id == "spoken-0000"
        assert review.cursor.selection.take_id == take_id
        assert review.cursor.selection.catalog_sha256 == reference.catalog_sha256
        assert versions.cursor == review.cursor


def test_default_edit_leaves_an_old_take_outside_the_new_version(versions: Versions) -> None:
    take_id = versions.take.reservation.take_id
    earlier = versions.resolver().resolve(versions.first.catalog_sha256, take_id)
    changed, snapshot = edit_and_publish(
        versions.rig.layout, "A different sentence entirely.", keep_recordings=False
    )
    versions.open(changed, "context-changed")
    assert snapshot.prompt_aliases == ()
    # Even a stale projection row naming the old take selects nothing under the new version.
    versions.rig.store.retained_take_resolver = lambda digest, prompt: take_id
    selected = versions.select("spoken-0000", "select-changed")
    assert selected.result.selected_take_id is None
    assert selected.cursor.selection.take_id is None
    # Stale history, not changed evidence: a named contract error, never a security failure.
    with pytest.raises(RecordingPromptNotCountedError, match="exact catalog") as caught:
        versions.resolver().resolve(changed.catalog_sha256, take_id)
    assert not isinstance(caught.value, SecurityError)
    assert (caught.value.take_id, caught.value.prompt_id) == (take_id, "spoken-0000")
    command = ReviewTakeCommand(
        operation_id="review-changed", take_id=take_id, selection=versions.cursor.selection
    )
    forged = earlier.model_copy(update={"catalog_sha256": changed.catalog_sha256})
    with pytest.raises(RecordingPromptNotCountedError, match="no longer counts"):
        versions.rig.store.select_review(command, forged)
    # A source that names another catalog is still a changed workspace.
    with pytest.raises(RecordingSelectionError, match="exact current catalog"):
        versions.rig.store.select_review(command, earlier)
    # Back under the recorded version the take is reviewable as before.
    versions.open(versions.first, "context-first-again")
    assert versions.resolver().resolve(versions.first.catalog_sha256, take_id) == earlier


def _review_through_the_service(
    layout: AssetLayout, subject_id: str, take_id: str
) -> RecordingServiceError:
    backend = RecordingWorkspaceBackend(layout, subject_id, discover=False)
    try:
        cursor = backend.store.get_cursor(subject_id)
        assert cursor is not None
        command = ReviewTakeCommand(
            operation_id="review-stale", take_id=take_id, selection=cursor.selection
        )
        with pytest.raises(RecordingServiceError) as caught:
            backend.execute(
                request(command, revision=backend.state_revision, take_id=take_id),
                RecordingCancellation(),
            )
        return caught.value
    finally:
        backend.index.close()


def test_the_service_names_native_and_legacy_takes_an_edit_stopped_counting_alike(
    versions: Versions, tmp_path: Path
) -> None:
    take_id = versions.take.reservation.take_id
    changed, _ = edit_and_publish(
        versions.rig.layout, "A different sentence entirely.", keep_recordings=False
    )
    versions.open(changed, "context-changed")
    native = _review_through_the_service(versions.rig.layout, SUBJECT, take_id).recording_error

    evidence = build_legacy_root(tmp_path.resolve() / "legacy" / "assets")
    layout, pilot_v2 = evidence.layout, released_pilot_v2()
    with open_catalog_editor(layout, create=True) as corpus:
        corpus.import_prompt_suite(pilot_v2, source="pilot-v2")
        corpus.create_catalog("cole-corpus", purpose="capture-pilot")
        corpus.publish_version("cole-corpus")
        corpus.edit_prompt(spoken(2), text="A different synthetic sentence.")
        reference = corpus.publish_version("cole-corpus")
        reworded = corpus.materialize(reference)
    with RecordingIndex(layout, subject_id=LEGACY_SUBJECT) as index:
        publish(layout, index, pilot_v2)
        publish(layout, index, reworded)
        page = index.history(canonical_sha256(pilot_v2), prompt_id=spoken(2), page_size=200)
        legacy_id = next(
            item.take_id
            for item in page.takes
            if item.legacy_session_id is not None and item.decision == "kept"
        )
        with pytest.raises(RecordingPromptNotCountedError, match="no longer counts") as caught:
            index.review_aliases(reference.catalog_sha256, legacy_id)
        assert caught.value.prompt_id == spoken(2)
    RecordingTakeStore(layout).open_context(
        OpenContextCommand(
            operation_id="context-legacy",
            subject_id=LEGACY_SUBJECT,
            catalog_reference=reference,
            capture_device=UMIK,
            capture_host="synthetic-host",
        )
    )
    legacy = _review_through_the_service(layout, LEGACY_SUBJECT, legacy_id).recording_error
    for error, (identity, prompt_id) in (
        (native, (take_id, "spoken-0000")),
        (legacy, (legacy_id, spoken(2))),
    ):
        assert error.code == "RECORDING.STALE_REVISION" and not error.retryable
        assert error.message == str(RecordingPromptNotCountedError(identity, prompt_id))


def test_review_fails_closed_when_the_open_versions_snapshot_is_missing(
    versions: Versions,
) -> None:
    take_id = versions.take.reservation.take_id
    second, _ = edit_and_publish(
        versions.rig.layout, "Synthetic sentence zero.", keep_recordings=True
    )
    versions.open(second, "context-second")
    (versions.rig.layout.root / second.snapshot_path).unlink()
    store = RecordingTakeStore(versions.rig.layout, clock=lambda: versions.rig.now)
    resolver = RecordingReviewResolver(versions.rig.layout, store, versions.index)
    with pytest.raises(SecurityError, match="missing") as caught:
        resolver.resolve(second.catalog_sha256, take_id)
    assert getattr(caught.value, "code", None) == "CORPUS.SNAPSHOT_MISSING"

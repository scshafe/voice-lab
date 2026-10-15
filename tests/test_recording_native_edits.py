from __future__ import annotations

import os
import threading
import wave
from pathlib import Path
from typing import Any

import pytest
from test_recording_navigation import NavigationRig
from test_recording_navigation import nav as nav

from cole_voice_lab.canonical import canonical_sha256, sha256_file
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.recording_contracts import (
    DecisionCommand,
    EditTakeCommand,
    RecordingFrameRange,
    RetakeCommand,
    ReviewTakeCommand,
)
from cole_voice_lab.recording_edit_jobs import RecordingEditCoordinator
from cole_voice_lab.recording_edits import (
    current_native_edit,
    native_edit_derivative,
    native_edit_identity,
    render_native_edit,
)
from cole_voice_lab.recording_review import RecordingReviewResolver
from cole_voice_lab.recording_takes import RecordingTakeStore


def edit(
    nav: NavigationRig, take: Any, operation: str = "edit-1", start: int = 10, end: int = 400
) -> Any:
    return nav.rig.store.edit(
        EditTakeCommand(
            operation_id=operation,
            selection=nav.cursor.selection,
            take_id=take.reservation.take_id,
            source_sha256=take.seal.original_sha256,
            expected_edit_revision=take.edit_revision,
            frame_range=RecordingFrameRange(source_frames=480, start_frame=start, end_frame=end),
        )
    )


def decide(nav: NavigationRig, take: Any, operation: str, action: str) -> Any:
    return nav.rig.store.decide(
        DecisionCommand(
            operation_id=operation,
            selection=nav.cursor.selection,
            take_id=take.reservation.take_id,
            expected_decision_revision=take.decision_revision,
            action=action,
            reason=None if action == "restore" else "Synthetic decision",
        )
    )


def test_recipe_is_durable_before_render_and_reset_keeps_original(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    take = nav.capture()
    path = nav.rig.layout.root / take.seal.original_path
    original = path.read_bytes()
    original_identity = native_edit_identity(nav.rig.layout, take.reservation.take_id)
    assert current_native_edit(nav.rig.layout, take.reservation.take_id) is None

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("metadata edit attempted audio analysis/render")

    monkeypatch.setattr("cole_voice_lab.recording_edits.inspect_canonical_wav", forbidden)
    result = edit(nav, take)
    recipe = current_native_edit(nav.rig.layout, take.reservation.take_id)
    assert recipe is not None and result.recipe_sha256 == canonical_sha256(recipe)
    assert result.derivative_state == "pending" and result.take.edit_revision == 1
    assert result.take.analysis == "pending" and result.take.decision == "draft"
    assert native_edit_derivative(nav.rig.layout, take.reservation.take_id) is None
    assert native_edit_identity(nav.rig.layout, take.reservation.take_id) != original_identity
    reset = edit(nav, result.take, "reset", 0, 480)
    assert reset.derivative_state == "not-required" and reset.take.edit_revision == 2
    assert path.read_bytes() == original


def test_save_inline_trim_commits_recipe_and_restart_replays_without_audio(
    nav: NavigationRig,
) -> None:
    take = nav.capture()
    command = nav.rig.command(
        take,
        "save-trim",
        selection=nav.cursor.selection,
        frame_range=RecordingFrameRange(source_frames=480, start_frame=30, end_frame=420),
    )
    result = nav.rig.store.save(command)
    recipe = current_native_edit(nav.rig.layout, take.reservation.take_id)
    assert recipe is not None and recipe.frame_range == result.take.frame_range
    assert result.take.edit_revision == 1 and result.take.decision == "kept"
    reopened = RecordingTakeStore(nav.rig.layout, projection_hook=nav.index.update_take)
    assert reopened.save(command) == result


@pytest.mark.parametrize("saved", [False, True])
def test_remove_restore_retains_original_draft_or_kept_intent(
    nav: NavigationRig, saved: bool
) -> None:
    take = nav.capture()
    if saved:
        take = nav.save(take).take
    source = nav.rig.layout.root / take.seal.original_path
    before = source.read_bytes()
    removed = decide(nav, take, "remove", "remove")
    assert removed.take.decision == "removed"
    assert removed.decision.restore_decision == ("kept" if saved else "draft")
    restored = decide(nav, removed.take, "restore", "restore")
    assert restored.take.decision == take.decision
    assert restored.take.decision_revision == take.decision_revision + 2
    if not saved:
        assert nav.save(restored.take, "save-restored").take.decision == "kept"
    assert source.read_bytes() == before


def test_reject_remove_restore_and_retake_preserve_independent_source(nav: NavigationRig) -> None:
    take = nav.save(nav.capture()).take
    rejected = decide(nav, take, "reject", "reject").take
    removed = decide(nav, rejected, "remove", "remove").take
    restored = decide(nav, removed, "restore", "restore").take
    assert restored.decision == "kept"
    event = nav.rig.store.retake(
        RetakeCommand(operation_id="retake", selection=nav.cursor.selection),
        subject_id="synthetic-subject",
    )
    assert event.cursor.selection.take_id is None
    assert nav.rig.store.get_take(take.reservation.take_id) == restored
    new = nav.capture("new-take")
    assert new.reservation.take_id != take.reservation.take_id
    assert len(nav.rig.store.iter_takes()) == 2


@pytest.mark.parametrize(
    "field,value", [("expected_edit_revision", 9), ("source_sha256", "f" * 64)]
)
def test_stale_trim_does_not_change_recipe(nav: NavigationRig, field: str, value: Any) -> None:
    take = nav.capture()
    command = EditTakeCommand(
        operation_id="stale",
        selection=nav.cursor.selection,
        take_id=take.reservation.take_id,
        source_sha256=take.seal.original_sha256,
        expected_edit_revision=0,
        frame_range=take.frame_range,
    ).model_copy(update={field: value})
    with pytest.raises(ContractError):
        nav.rig.store.edit(command)
    assert current_native_edit(nav.rig.layout, take.reservation.take_id) is None


@pytest.mark.parametrize(
    "boundary",
    [
        "before-edit-recipe",
        "after-edit-recipe",
        "after-take-head",
        "after-store-head",
        "before-projection",
        "after-projection",
    ],
)
def test_edit_commit_fault_recovers_recipe_and_exact_revision(
    nav: NavigationRig, boundary: str
) -> None:
    take = nav.capture()
    command = EditTakeCommand(
        operation_id="fault-edit",
        selection=nav.cursor.selection,
        take_id=take.reservation.take_id,
        source_sha256=take.seal.original_sha256,
        expected_edit_revision=0,
        frame_range=RecordingFrameRange(source_frames=480, start_frame=10, end_frame=400),
    )

    def fail(point: str) -> None:
        if point == boundary:
            raise OSError("synthetic fault")

    nav.rig.store.fault_hook = fail
    with pytest.raises(OSError):
        nav.rig.store.edit(command)
    recovered = RecordingTakeStore(nav.rig.layout, projection_hook=nav.index.update_take)
    result = recovered.edit(command)
    assert result.take.edit_revision == 1
    assert (
        current_native_edit(nav.rig.layout, take.reservation.take_id).frame_range
        == result.take.frame_range
    )


def test_render_retains_exact_pcm_frames_and_checksum_without_changing_source(
    nav: NavigationRig,
) -> None:
    take = nav.capture()
    changed = edit(nav, take, start=17, end=313)
    recipe = current_native_edit(nav.rig.layout, take.reservation.take_id)
    source = nav.rig.layout.root / take.seal.original_path
    before = source.read_bytes()
    result = render_native_edit(
        nav.rig.layout,
        source,
        recipe,
        source_identity=nav.rig.store._source_identity(take.seal.original_path),
        completed_at=nav.rig.now,
    )
    output = nav.rig.layout.root / result.derived_path
    with wave.open(str(source), "rb") as original, wave.open(str(output), "rb") as derived:
        original.setpos(17)
        assert derived.getnframes() == 296
        assert derived.readframes(296) == original.readframes(296)
    assert sha256_file(output)[0] == result.derived_sha256
    assert result.recipe_sha256 == changed.recipe_sha256
    assert source.read_bytes() == before
    assert native_edit_derivative(nav.rig.layout, take.reservation.take_id) == result


def test_late_derivative_cannot_replace_newer_trim(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_edits as module

    take = nav.capture()
    changed = edit(nav, take)
    recipe = current_native_edit(nav.rig.layout, take.reservation.take_id)
    original = module._publish_frame_range

    def delayed(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        edit(nav, changed.take, "newer", 20, 390)
        return result

    monkeypatch.setattr(module, "_publish_frame_range", delayed)
    with pytest.raises(ContractError, match="stale"):
        render_native_edit(
            nav.rig.layout,
            nav.rig.layout.root / take.seal.original_path,
            recipe,
            source_identity=nav.rig.store._source_identity(take.seal.original_path),
            completed_at=nav.rig.now,
        )
    assert current_native_edit(nav.rig.layout, take.reservation.take_id).edit_revision == 2
    assert native_edit_derivative(nav.rig.layout, take.reservation.take_id) is None


@pytest.mark.serial
def test_real_owned_render_process_publishes_exact_current_recipe(nav: NavigationRig) -> None:
    take = nav.capture()
    edit(nav, take)
    events = []
    done = threading.Event()

    def receive(event: Any) -> None:
        events.append(event)
        done.set()

    coordinator = RecordingEditCoordinator(
        nav.rig.store, published=receive, available_bytes=lambda: 1 << 60
    )
    try:
        coordinator.submit(take.reservation.take_id)
        assert done.wait(10)
        assert events[0].state == "ready"
        assert (
            events[0].derived_sha256
            == native_edit_derivative(nav.rig.layout, take.reservation.take_id).derived_sha256
        )
    finally:
        coordinator.close()


def test_selected_native_source_is_verified_cached_and_cursor_is_durable(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_review as module

    first = nav.capture()
    nav.select(1)
    second = nav.capture("second")
    resolver = RecordingReviewResolver(nav.rig.layout, nav.rig.store, nav.index)
    original = module.sha256_file
    opened = []

    def observe(path: Path) -> Any:
        opened.append(path)
        return original(path)

    monkeypatch.setattr(module, "sha256_file", observe)
    source = resolver.resolve(nav.context.group.catalog_sha256, first.reservation.take_id)
    assert opened == [nav.rig.layout.root / first.seal.original_path]
    assert resolver.resolve(nav.context.group.catalog_sha256, first.reservation.take_id) == source
    assert len(opened) == 1
    command = ReviewTakeCommand(
        operation_id="review-first",
        take_id=first.reservation.take_id,
        selection=nav.cursor.selection,
    )
    event = nav.rig.store.select_review(command, source)
    assert event.cursor.selection.take_id == first.reservation.take_id
    assert event.cursor.selection.prompt_id == first.reservation.prompt.prompt_id
    assert nav.rig.store.get_take(second.reservation.take_id).decision == "draft"
    assert nav.rig.store.select_review(command, source) == event


def test_selected_source_preserved_mtime_tamper_invalidates_review_cache(
    nav: NavigationRig,
) -> None:
    take = nav.capture()
    resolver = RecordingReviewResolver(nav.rig.layout, nav.rig.store, nav.index)
    resolver.resolve(nav.context.group.catalog_sha256, take.reservation.take_id)
    source = nav.rig.layout.root / take.seal.original_path
    info = source.stat()
    data = bytearray(source.read_bytes())
    data[-1] ^= 1
    source.write_bytes(data)
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(SecurityError):
        resolver.resolve(nav.context.group.catalog_sha256, take.reservation.take_id)


def test_owned_draft_review_is_read_only_and_does_not_rehash_pcm(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    take = nav.capture()
    resolver = RecordingReviewResolver(nav.rig.layout, nav.rig.store, nav.index)
    before = nav.cursor
    revision = nav.rig.store.state_revision

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("selected registered draft was synchronously rehashed")

    monkeypatch.setattr("cole_voice_lab.recording_review.sha256_file", forbidden)
    source = resolver.resolve(nav.context.group.catalog_sha256, take.reservation.take_id)
    response = nav.rig.store.select_review(
        ReviewTakeCommand(
            operation_id="cheap-review",
            take_id=take.reservation.take_id,
            selection=before.selection,
        ),
        source,
    )
    assert response.cursor == before and nav.rig.store.state_revision == revision
    assert nav.save(take).take.decision == "kept"


def test_review_rejects_source_edit_that_changed_during_preparation(nav: NavigationRig) -> None:
    take = nav.capture()
    resolver = RecordingReviewResolver(nav.rig.layout, nav.rig.store, nav.index)
    source = resolver.resolve(nav.context.group.catalog_sha256, take.reservation.take_id)
    edit(nav, take)
    with pytest.raises(ContractError, match="changed during review"):
        nav.rig.store.select_review(
            ReviewTakeCommand(
                operation_id="late-review",
                take_id=take.reservation.take_id,
                selection=nav.cursor.selection,
            ),
            source,
        )


def test_concurrent_selected_source_cache_stays_bounded(nav: NavigationRig) -> None:
    from concurrent.futures import ThreadPoolExecutor

    takes = [nav.capture(f"cache-{number}") for number in range(12)]
    resolver = RecordingReviewResolver(nav.rig.layout, nav.rig.store, nav.index)
    with ThreadPoolExecutor(max_workers=4) as executor:
        sources = list(
            executor.map(
                lambda take: resolver.resolve(
                    nav.context.group.catalog_sha256, take.reservation.take_id
                ),
                takes * 2,
            )
        )
    assert len(sources) == 24 and len(resolver._verified) <= 8 and len(resolver._owned) <= 8


def test_new_edit_recipe_directories_are_durable_before_ack(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat

    import cole_voice_lab.recording_edits as module

    take = nav.capture()
    synced = set()
    original = module.os.fsync

    def observe(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced.add(metadata.st_ino)
        original(descriptor)

    monkeypatch.setattr(module.os, "fsync", observe)
    edit(nav, take)
    root = nav.rig.layout.root
    assert {
        (root / path).stat().st_ino
        for path in (
            "provenance",
            "provenance/recording-edits",
            "provenance/recording-edits/native",
        )
    } <= synced

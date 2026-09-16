from __future__ import annotations

import shutil
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_recording_index import legacy_library
from test_recording_library import DEVICE, take
from test_recording_takes import DEVICE as NATIVE_DEVICE
from test_recording_takes import NOW

from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake import accept_capture_session
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    DecisionCommand,
    EditTakeCommand,
    OpenContextCommand,
    RecordingFrameRange,
    RecordingHistoryEntry,
    RecordingReviewSource,
    ReviewTakeCommand,
)
from cole_voice_lab.recording_edit_jobs import RecordingEditCoordinator
from cole_voice_lab.recording_edits import (
    RecordingLegacyEditIntent,
    _legacy_key,
    assert_recording_not_removed,
    commit_legacy_edit,
    legacy_action_event,
    legacy_edit_result,
    materialize_legacy_edit,
    read_recording_edit,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_index_backfill import backfill_recording_library
from cole_voice_lab.recording_library import RecordingLibrary
from cole_voice_lab.recording_review import RecordingReviewResolver
from cole_voice_lab.recording_takes import RecordingTakeStore


@dataclass
class LegacyRig:
    layout: AssetLayout
    library: RecordingLibrary
    index: RecordingIndex
    store: RecordingTakeStore
    resolver: RecordingReviewResolver
    source: RecordingReviewSource
    entry: RecordingHistoryEntry

    def act(self, action: str, operation: str) -> RecordingLegacyEditIntent:
        cursor = self.store.get_cursor("cole-self")
        source = self.resolver.resolve(self.library.catalog_sha256, self.source.take_id)
        if action == "edit":
            command = EditTakeCommand(
                operation_id=operation,
                take_id=source.take_id,
                selection=cursor.selection,
                source_sha256=source.source_sha256,
                expected_edit_revision=source.edit_revision,
                frame_range=RecordingFrameRange(source_frames=960, start_frame=17, end_frame=313),
            )
        else:
            command = DecisionCommand(
                operation_id=operation,
                take_id=source.take_id,
                selection=cursor.selection,
                expected_decision_revision=source.decision_revision,
                action=action,
                reason=None if action == "restore" else "Synthetic review decision",
            )
        entry, _ = self.index.review_aliases(self.library.catalog_sha256, source.take_id)
        intent = commit_legacy_edit(
            self.layout,
            request=command,
            source=source,
            cursor=cursor,
            entry=entry,
            committed_at=NOW,
        )
        self.index.update_legacy_review(intent.projection)
        return intent

    def gate(self) -> None:
        assert_recording_not_removed(
            self.layout,
            subject_id=self.entry.subject_id,
            session_id=self.entry.legacy_session_id,
            prompt_id=self.entry.prompt.prompt_id,
            source_sha256=self.entry.source_sha256,
        )


@pytest.fixture(params=[False, True], ids=["staged", "accepted-alias"])
def legacy(tmp_path: Path, request: pytest.FixtureRequest) -> Any:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    library = legacy_library(layout)
    session = library.begin_session([p.prompt_id for p in library.prompt_suite.prompts], DEVICE)
    take(session)
    take(session)
    result = session.finalize()
    if request.param:
        incoming = layout.directory("incoming") / session.evidence.session_id
        shutil.copytree(result.session_directory, incoming)
        accept_capture_session(layout, incoming)
    index = RecordingIndex(layout)
    backfill_recording_library(library, index)
    entry = library.indexed_history(index).takes[0]
    store = RecordingTakeStore(
        layout, clock=lambda: NOW, index_revision_resolver=lambda: index.revision
    )
    context = store.open_context(
        OpenContextCommand(
            operation_id="legacy-context",
            subject_id="cole-self",
            catalog=library.prompt_suite,
            catalog_sha256=library.catalog_sha256,
            capture_device=NATIVE_DEVICE,
            capture_host="synthetic",
        )
    )
    resolver = RecordingReviewResolver(layout, store, index)
    source = resolver.resolve(library.catalog_sha256, entry.take_id)
    store.select_review(
        ReviewTakeCommand(
            operation_id="legacy-review", take_id=entry.take_id, selection=context.cursor.selection
        ),
        source,
    )
    yield LegacyRig(layout, library, index, store, resolver, source, entry)
    index.close()


def test_legacy_trim_intent_blocks_eligibility_until_exact_v1_output(legacy: LegacyRig) -> None:
    source = legacy.layout.root / legacy.source.source_path
    before = source.read_bytes()
    metadata = source.stat()
    intent = legacy.act("edit", "trim")
    assert legacy_action_event(intent).state == "pending"
    with pytest.raises(ContractError, match="pending"):
        legacy.gate()
    assert legacy_edit_result(legacy.layout, intent) is None
    result = materialize_legacy_edit(legacy.layout, intent)
    assert (
        result.edit is not None and result.edit.schema_version == "cole-voice-lab.recording-edit/v1"
    )
    legacy.gate()
    assert result.edit.start_frame == 17 and result.edit.end_frame == 313
    with (
        wave.open(str(source), "rb") as original,
        wave.open(str(legacy.layout.root / result.edit.derived_path), "rb") as output,
    ):
        assert output.readframes(296) == original.readframes(960)[17 * 3 : 313 * 3]
    assert source.read_bytes() == before and source.stat() == metadata
    assert materialize_legacy_edit(legacy.layout, intent) == result
    assert (
        legacy.resolver.resolve(legacy.library.catalog_sha256, legacy.source.take_id).recorded_at
        == legacy.source.recorded_at
    )


def test_legacy_remove_is_immediate_across_aliases_and_restore_preserves_intent(
    legacy: LegacyRig,
) -> None:
    removed = legacy.act("remove", "remove")
    with pytest.raises(ContractError, match="excluded"):
        legacy.gate()
    assert legacy.library.indexed_history(legacy.index).takes[0].decision == "removed"
    materialize_legacy_edit(legacy.layout, removed)
    restored = legacy.act("restore", "restore")
    assert restored.source.decision == "kept" and restored.source.decision_revision == 2
    with pytest.raises(ContractError, match="pending"):
        legacy.gate()
    materialize_legacy_edit(legacy.layout, restored)
    legacy.gate()
    backfill_recording_library(legacy.library, legacy.index)
    assert legacy.library.indexed_history(legacy.index).takes[0].decision == "kept"


def test_legacy_old_render_cannot_replace_a_newer_remove(
    legacy: LegacyRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = legacy.act("edit", "trim")
    import cole_voice_lab.recording_edits as module

    original = module._publish_trim

    def intercept(*args: Any, **kwargs: Any) -> Any:
        output = original(*args, **kwargs)
        legacy.act("remove", "new-remove")
        return output

    monkeypatch.setattr(module, "_publish_trim", intercept)
    with pytest.raises(SecurityError, match="superseded"):
        materialize_legacy_edit(legacy.layout, intent)
    assert (
        read_recording_edit(
            legacy.layout,
            subject_id=legacy.entry.subject_id,
            session_id=legacy.entry.legacy_session_id,
            prompt_id=legacy.entry.prompt.prompt_id,
            source_sha256=legacy.entry.source_sha256,
        )
        is None
    )
    with pytest.raises(ContractError, match="excluded"):
        legacy.gate()


def test_legacy_real_owned_child_materializes_committed_intent(legacy: LegacyRig) -> None:
    intent = legacy.act("edit", "child-trim")
    finished = threading.Event()
    events = []

    def publish(event: Any) -> None:
        events.append(event)
        finished.set()

    coordinator = RecordingEditCoordinator(
        legacy.store, published=publish, available_bytes=lambda: 1 << 60
    )
    try:
        coordinator.submit("legacy:" + _legacy_key(intent))
        assert finished.wait(8)
    finally:
        coordinator.close()
    assert events[-1].event == "review-action" and events[-1].state == "completed"
    assert events[-1].operation_id == "child-trim"
    legacy.gate()


def test_legacy_exact_replay_never_replaces_a_newer_same_timestamp_decision(
    legacy: LegacyRig,
) -> None:
    first = legacy.act("edit", "first-edit")
    newer = legacy.act("remove", "later-remove")
    replay = commit_legacy_edit(
        legacy.layout,
        request=first.request,
        source=first.before,
        cursor=first.cursor,
        entry=first.projection,
        committed_at=NOW,
    )
    assert replay == first
    assert (
        legacy.resolver.resolve(legacy.library.catalog_sha256, legacy.source.take_id).decision
        == "removed"
    )
    with pytest.raises(SecurityError, match="different request"):
        commit_legacy_edit(
            legacy.layout,
            request=first.request.model_copy(update={"expected_edit_revision": 9}),
            source=first.before,
            cursor=first.cursor,
            entry=first.projection,
            committed_at=NOW,
        )
    assert newer.projection.state_revision > first.projection.state_revision


def test_legacy_intent_commit_never_opens_pcm_for_analysis(
    legacy: LegacyRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("interactive legacy intent opened PCM analysis")

    monkeypatch.setattr("cole_voice_lab.recording_edits.inspect_canonical_wav", forbidden)
    intent = legacy.act("edit", "metadata-only")
    assert legacy_action_event(intent).state == "pending"


def test_legacy_v1_pointer_with_lost_reply_recovers_same_operation(
    legacy: LegacyRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_edits as module

    intent = legacy.act("edit", "lost-materialized-reply")
    original = module.write_canonical_json

    def fail_result(path: Path, value: Any) -> None:
        if path.name.endswith(".result.json"):
            raise OSError("synthetic interruption after compatible pointer")
        original(path, value)

    monkeypatch.setattr(module, "write_canonical_json", fail_result)
    with pytest.raises(OSError, match="synthetic interruption"):
        materialize_legacy_edit(legacy.layout, intent)
    assert legacy_edit_result(legacy.layout, intent) is None
    with pytest.raises(ContractError, match="pending"):
        legacy.gate()
    monkeypatch.setattr(module, "write_canonical_json", original)

    def forbid_render(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("lost completion reply caused a duplicate PCM render")

    monkeypatch.setattr(module, "_publish_trim", forbid_render)
    result = materialize_legacy_edit(legacy.layout, intent)
    assert result.edit is not None
    legacy.gate()


@pytest.mark.parametrize(
    "path,value",
    [
        (("request_sha256",), "f" * 64),
        (("session_id",), "another-session"),
        (("source", "recorded_at"), None),
        (("source", "decision_revision"), 42),
        (("source", "edit_revision"), 42),
        (("source", "source_sha256"), "f" * 64),
        (("projection", "decision"), "removed"),
        (("cursor", "selection", "take_id"), "another-take"),
    ],
)
def test_legacy_intent_rejects_forged_source_revision_or_provenance(
    legacy: LegacyRig, path: tuple[str, ...], value: Any
) -> None:
    from pydantic import ValidationError

    intent = legacy.act("edit", "strict-intent")
    payload = intent.model_dump()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        RecordingLegacyEditIntent.model_validate(payload)


def test_uncached_legacy_edit_authorization_never_hashes_pcm_on_control_lane(
    legacy: LegacyRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = RecordingReviewResolver(legacy.layout, legacy.store, legacy.index)
    import cole_voice_lab.recording_review as module

    original = module.sha256_file

    def guard(path: Path) -> Any:
        if path.suffix == ".wav":
            raise AssertionError("uncached edit rehashed PCM on metadata control")
        return original(path)

    monkeypatch.setattr(module, "sha256_file", guard)
    with pytest.raises(SecurityError, match="usable canonical source"):
        resolver.resolve(
            legacy.library.catalog_sha256, legacy.source.take_id, require_verified=True
        )


def test_later_v1_edit_cannot_be_hidden_by_an_older_native_projection(legacy: LegacyRig) -> None:
    from datetime import timedelta

    from cole_voice_lab.recording_edits import project_legacy_edit, update_recording_edit

    intent = legacy.act("edit", "native-trim")
    result = materialize_legacy_edit(legacy.layout, intent)
    assert result.edit is not None
    update_recording_edit(
        legacy.layout,
        legacy.layout.root / legacy.source.source_path,
        result.edit,
        removed=True,
        reason="Later synthetic legacy exclusion",
        updated_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(SecurityError, match="changed after"):
        legacy.gate()
    with pytest.raises(SecurityError, match="changed after"):
        project_legacy_edit(legacy.layout, legacy.entry)
    with pytest.raises(SecurityError, match="usable canonical source"):
        legacy.resolver.resolve(legacy.library.catalog_sha256, legacy.source.take_id)

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from recording_fixtures import synthetic_catalog
from test_recording_contracts import synthetic_contracts
from test_recording_review import Versions, edit_and_publish
from test_recording_review import versions as versions
from test_recording_service import ServiceProcessEvidence, activate_catalog
from test_recording_takes import DEVICE, Rig, store_suite

from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    MAX_CONTROL_FRAME_BYTES,
    CatalogSnapshot,
    ContextEvent,
    DecisionCommand,
    NavigateCommand,
    OpenContextCommand,
    RecordingCursor,
    RecordingResponse,
    RecordingSelection,
    RecordingTake,
    RetakeCommand,
    ServiceRequest,
    recording_frame_bytes,
)
from cole_voice_lab.recording_index import RecordingIndex, RecordingIndexError
from cole_voice_lab.recording_library import RecordingLibrary
from cole_voice_lab.recording_service import (
    RecordingServiceSession,
    RecordingWorkspaceBackend,
    recording_service_build_identity,
)
from cole_voice_lab.recording_takes import (
    RecordingSelectionError,
    RecordingTakeOperation,
    RecordingTakeStore,
)


@dataclass
class NavigationRig:
    rig: Rig
    index: RecordingIndex
    library: RecordingLibrary
    context: ContextEvent

    @property
    def cursor(self) -> RecordingCursor:
        value = self.rig.store.get_cursor("synthetic-subject")
        assert value is not None
        return value

    def capture(self, operation: str = "capture") -> RecordingTake:
        cursor = self.cursor
        reservation = self.rig.store.reserve(
            operation_id=f"reserve-{operation}",
            group_id=cursor.group_id,
            prompt_id=cursor.selection.prompt_id,
            process_generation="helper-generation",
            selection=cursor.selection,
        )
        return self.rig.seal(reservation, f"seal-{operation}")

    def save(self, take: RecordingTake, operation: str = "save", advance: bool = False) -> Any:
        command = self.rig.command(
            take, operation, selection=self.cursor.selection, advance=advance
        )
        return self.library.save_native_take(self.rig.store, command)

    def select(self, prompt: int, operation: str = "select") -> Any:
        return self.rig.store.navigate(
            NavigateCommand(
                operation_id=operation,
                selection=self.cursor.selection,
                mode="selected-prompt",
                target_prompt_id=self.library.prompt_suite.prompts[prompt].prompt_id,
            ),
            subject_id="synthetic-subject",
        )

    def missing(self, operation: str = "continue") -> Any:
        return self.rig.store.navigate(
            NavigateCommand(
                operation_id=operation, selection=self.cursor.selection, mode="continue-missing"
            ),
            subject_id="synthetic-subject",
        )


@pytest.fixture
def nav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NavigationRig:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    rig = Rig(layout, RecordingTakeStore(layout), "")
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    catalog = synthetic_catalog(4)
    # Two distinct room tones retain independent prompt identity and canonical order.
    catalog = PromptSuite.model_validate(
        catalog.model_dump()
        | {
            "prompts": tuple(
                p.model_dump() | ({"category": "room-tone"} if index == 3 else {})
                for index, p in enumerate(catalog.prompts)
            )
        }
    )
    index = RecordingIndex(layout, subject_id="synthetic-subject")
    index.register_catalog(CatalogSnapshot.embedded(catalog))
    index.set_completeness(canonical_sha256(catalog), "complete")
    library = RecordingLibrary(layout, catalog, "synthetic-subject")
    library.bind_native_store(rig.store, index)
    context = rig.store.open_context(
        OpenContextCommand(
            operation_id="context-initial",
            subject_id="synthetic-subject",
            catalog=catalog,
            catalog_sha256=canonical_sha256(catalog),
            capture_device=DEVICE,
            capture_host="host",
        )
    )
    rig.group_id = context.group.group_id

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("navigation tried to open a microphone or legacy WAV scanner")

    monkeypatch.setattr("cole_voice_lab.capture.TakeEngine.start_take", forbidden)
    monkeypatch.setattr("cole_voice_lab.recording_library.list_coreaudio_input_devices", forbidden)
    monkeypatch.setattr(RecordingLibrary, "_scan", forbidden)
    yield NavigationRig(rig, index, library, context)
    index.close()


def test_save_and_next_persists_one_final_target_and_complete_last_take(nav: NavigationRig) -> None:
    for number in range(4):
        take = nav.capture(str(number))
        saved = nav.save(take, f"save-{number}", advance=True)
        assert saved.operation.navigation.index_revision <= nav.index.revision
        if number < 3:
            assert (
                saved.cursor.selection.prompt_id
                == nav.library.prompt_suite.prompts[number + 1].prompt_id
            )
            assert saved.cursor.selection.take_id is None
        else:
            assert saved.operation.navigation.complete and saved.cursor.complete
            assert saved.cursor.selection.take_id == take.reservation.take_id
    assert nav.index.history(nav.context.group.catalog_sha256).covered_prompt_count == 4
    assert nav.cursor.complete


def test_oneoff_save_and_explicit_retake_do_not_insert_room_tone(nav: NavigationRig) -> None:
    nav.select(2)
    first = nav.capture("first")
    saved = nav.save(first)
    assert saved.cursor.selection.prompt_id == first.reservation.prompt.prompt_id
    assert saved.cursor.selection.take_id == first.reservation.take_id
    nav.select(2, "retake")
    second = nav.capture("retake")
    assert second.reservation.take_id != first.reservation.take_id
    assert nav.rig.store.get_take(first.reservation.take_id).decision == "kept"
    assert second.decision == "draft"
    assert nav.index.history(nav.context.group.catalog_sha256).covered_prompt_count == 1
    assert all(t.reservation.prompt.category != "room-tone" for t in nav.rig.store.iter_takes())


def test_continue_missing_wraps_once_and_ignores_table_sort_and_history_pages(
    nav: NavigationRig,
) -> None:
    nav.select(2)
    nav.save(nav.capture())
    first = nav.index.history(nav.context.group.catalog_sha256, page_size=1)
    nav.index.history(nav.context.group.catalog_sha256, cursor=first.next_cursor, page_size=1)
    result = nav.missing()
    assert result.result.next_prompt.prompt_id == "synthetic-0003"
    nav.save(nav.capture("tone-end"), "save-tone-end")
    assert nav.missing("wrap").result.next_prompt.prompt_id == "synthetic-0000"


@pytest.mark.parametrize(
    "analysis,decision,covered",
    [
        ("pending", "kept", True),
        ("needs-review", "kept", True),
        ("failed", "kept", True),
        ("invalid", "kept", False),
        ("pending", "draft", False),
        ("pending", "removed", False),
        ("pending", "rejected", False),
    ],
)
def test_navigation_uses_retained_exact_coverage_not_qc_completion(
    nav: NavigationRig, analysis: str, decision: str, covered: bool
) -> None:
    take = nav.capture()
    if decision != "draft":
        take = nav.save(take).take
    changed = RecordingTake.model_validate(
        take.model_dump()
        | {"analysis": analysis, "decision": decision, "state_revision": take.state_revision + 1}
    )
    nav.index.update_take(changed)
    nav.select(3)
    result = nav.missing().result
    assert result.next_prompt.prompt_id == ("synthetic-0001" if covered else "synthetic-0000")


def test_cross_group_exact_prompt_coverage_without_acoustic_reuse(nav: NavigationRig) -> None:
    saved = nav.save(nav.capture()).take
    previous = nav.cursor
    command = OpenContextCommand(
        operation_id="new-setup",
        subject_id="synthetic-subject",
        catalog=nav.library.prompt_suite,
        catalog_sha256=previous.selection.catalog_sha256,
        capture_device=DEVICE,
        capture_host="host",
        expected_selection_revision=previous.selection.selection_revision,
        new_setup=True,
    )
    event = nav.rig.store.open_context(command)
    assert event.group.group_id != saved.reservation.group.group_id
    assert event.group.room_tone_take_ids == ()
    nav.select(3)
    assert nav.missing().result.next_prompt.prompt_id == "synthetic-0001"
    changed_catalog = PromptSuite.model_validate(
        nav.library.prompt_suite.model_dump()
        | {
            "prompts": tuple(
                p.model_dump()
                | (
                    {"style": "different style", "text": "Changed exact transcript."}
                    if n == 0
                    else {}
                )
                for n, p in enumerate(nav.library.prompt_suite.prompts)
            )
        }
    )
    digest = nav.index.register_catalog(CatalogSnapshot.embedded(changed_catalog))
    nav.index.set_completeness(digest, "complete")
    assert nav.index.next_missing(digest).next_prompt == changed_catalog.prompts[0]


def test_unknown_history_blocks_only_coverage_navigation(nav: NavigationRig) -> None:
    nav.index.set_completeness(nav.context.group.catalog_sha256, "unknown")
    with pytest.raises(RecordingIndexError, match="complete"):
        nav.missing()
    nav.select(2)
    take = nav.capture()
    with pytest.raises(RecordingIndexError, match="complete"):
        nav.save(take, "save-next-denied", advance=True)
    assert nav.rig.store.get_take(take.reservation.take_id).decision == "draft"
    assert nav.save(take, "save-plain").take.decision == "kept"


def test_lost_save_reply_replays_exact_target_after_other_index_changes(nav: NavigationRig) -> None:
    take = nav.capture()
    command = nav.rig.command(take, selection=nav.cursor.selection, advance=True)
    original = nav.rig.store.save(command)
    nav.index.set_completeness(nav.context.group.catalog_sha256, "unknown")
    reopened = RecordingTakeStore(nav.rig.layout, clock=lambda: nav.rig.now)
    nav.library.bind_native_store(reopened, nav.index)
    assert reopened.save(command) == original
    assert reopened.get_cursor("synthetic-subject") == original.cursor
    assert reopened.get_take(take.reservation.take_id).decision_revision == 1


def test_unrelated_qc_revision_does_not_stale_save_or_move_cursor(nav: NavigationRig) -> None:
    first = nav.save(nav.capture("first"), "save-first").take
    nav.select(1)
    second = nav.capture("second")
    cursor = nav.cursor
    changed = first.model_copy(
        update={"state_revision": first.state_revision + 1, "analysis": "invalid"}
    )
    nav.index.update_take(changed)
    assert nav.cursor == cursor
    saved = nav.rig.store.save(nav.rig.command(second, selection=cursor.selection, advance=True))
    assert saved.operation.navigation.next_prompt.prompt_id == "synthetic-0002"


@pytest.mark.parametrize(
    "field,value",
    [
        ("catalog_sha256", "0" * 64),
        ("prompt_id", "other"),
        ("take_id", "other"),
        ("selection_revision", 999),
        ("index_revision", 999999),
    ],
)
def test_displayed_selection_identity_guards(nav: NavigationRig, field: str, value: Any) -> None:
    take = nav.capture()
    selection = RecordingSelection.model_validate(
        nav.cursor.selection.model_dump() | {field: value}
    )
    command = nav.rig.command(take, selection=selection)
    with pytest.raises(RecordingSelectionError):
        nav.library.save_native_take(nav.rig.store, command)
    assert nav.rig.store.get_take(take.reservation.take_id).decision == "draft"


@pytest.mark.parametrize(
    "boundary",
    [
        "before-intent",
        "after-intent",
        "before-operation",
        "after-operation",
        "before-cursor-head",
        "after-cursor-head",
        "before-store-head",
        "after-store-head",
        "before-completion",
        "after-completion",
        "before-ack",
    ],
)
def test_navigation_crash_reopen_and_operation_replay(nav: NavigationRig, boundary: str) -> None:
    before = nav.cursor
    command = NavigateCommand(
        operation_id="select-durable",
        selection=before.selection,
        mode="selected-prompt",
        target_prompt_id="synthetic-0002",
    )

    def fail(at: str) -> None:
        if at == boundary:
            raise OSError("synthetic navigation interruption")

    nav.rig.store.fault_hook = fail
    with pytest.raises(OSError):
        nav.rig.store.navigate(command, subject_id="synthetic-subject")
    store = RecordingTakeStore(nav.rig.layout, clock=lambda: nav.rig.now)
    nav.library.bind_native_store(store, nav.index)
    event = store.navigate(command, subject_id="synthetic-subject")
    assert event.cursor.selection.prompt_id == "synthetic-0002"
    assert event.cursor.selection.selection_revision == before.selection.selection_revision + 1
    assert store.navigate(command, subject_id="synthetic-subject") == event
    assert len(store.iter_takes()) == 0


def test_service_restart_new_group_preserves_recovered_cursor(nav: NavigationRig) -> None:
    nav.select(2)
    take = nav.capture()
    nav.save(take)
    previous = nav.cursor
    store = RecordingTakeStore(nav.rig.layout, clock=lambda: nav.rig.now)
    nav.library.bind_native_store(store, nav.index)
    command = OpenContextCommand(
        operation_id="app-restart",
        subject_id="synthetic-subject",
        catalog=nav.library.prompt_suite,
        catalog_sha256=previous.selection.catalog_sha256,
        capture_device=DEVICE,
        capture_host="host",
        expected_selection_revision=previous.selection.selection_revision,
    )
    event = store.open_context(command)
    assert event.group.reason == "app-start" and event.group.group_id != previous.group_id
    assert event.cursor.selection.prompt_id == previous.selection.prompt_id
    assert event.cursor.selection.take_id == previous.selection.take_id
    assert store.open_context(command) == event
    assert store.context_replay(command) == event


def test_no_historical_wav_reads_during_save_navigation_or_history(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    nav.save(nav.capture("historical"))
    nav.select(1)
    current = nav.capture("current")
    from cole_voice_lab import recording_takes

    original = recording_takes.open_regular_no_follow

    def guarded(path: Path) -> Any:
        if path.suffix == ".wav" and path != nav.rig.layout.root / current.seal.original_path:
            raise AssertionError("navigation opened historical audio")
        return original(path)

    monkeypatch.setattr(recording_takes, "open_regular_no_follow", guarded)
    assert nav.save(current, "next", advance=True).take.decision == "kept"
    nav.missing()
    assert nav.library.indexed_history(nav.index).total_take_count == 2


def test_prompt_selection_durably_selects_its_latest_retained_take(nav: NavigationRig) -> None:
    """Physical P06: returning to a recorded prompt must not show "No recorded take"."""
    empty = nav.select(2, "select-empty")
    assert empty.result.selected_take_id is None and empty.cursor.selection.take_id is None
    first = nav.save(nav.capture("first"), "save-first").take
    second = nav.save(nav.capture("second"), "save-second").take
    assert first.seal is not None and second.seal is not None
    assert second.seal.first_sample_at > first.seal.first_sample_at
    nav.select(1, "away")
    assert nav.cursor.selection.take_id is None
    command = NavigateCommand(
        operation_id="return",
        selection=nav.cursor.selection,
        mode="selected-prompt",
        target_prompt_id="synthetic-0002",
    )
    event = nav.rig.store.navigate(command, subject_id="synthetic-subject")
    assert event.result.selected_take_id == second.reservation.take_id
    assert event.cursor.selection.take_id == second.reservation.take_id
    assert event.cursor.selection.index_revision == event.result.index_revision
    assert nav.cursor == event.cursor
    operation_path = nav.rig.store._operation_path("return")
    stored = RecordingTakeOperation.model_validate_json(operation_path.read_bytes())
    assert stored.navigation == event.result and stored.cursor == event.cursor
    # Earlier evidence without a selected take keeps its exact canonical shape.
    away = nav.rig.store._operation_path("away").read_text()
    assert "selected_take_id" not in away
    assert RecordingTakeOperation.model_validate_json(away).navigation is not None

    # Replay stays the recorded result after the selected take changes; a new selection
    # resolves afresh and ignores the removed take.
    nav.rig.store.decide(
        DecisionCommand(
            operation_id="remove-second",
            selection=event.cursor.selection,
            take_id=second.reservation.take_id,
            expected_decision_revision=second.decision_revision,
            action="remove",
            reason="synthetic operator removal",
        )
    )
    reopened = RecordingTakeStore(nav.rig.layout, clock=lambda: nav.rig.now)
    nav.library.bind_native_store(reopened, nav.index)
    assert reopened.navigate(command, subject_id="synthetic-subject") == event
    nav.rig.store = reopened
    nav.select(1, "away-again")
    again = nav.select(2, "return-again")
    assert again.cursor.selection.take_id == first.reservation.take_id


def test_retake_and_coverage_navigation_never_select_a_take(nav: NavigationRig) -> None:
    kept = nav.save(nav.capture("kept")).take
    assert nav.cursor.selection.take_id == kept.reservation.take_id
    retake = nav.rig.store.retake(
        RetakeCommand(operation_id="retake", selection=nav.cursor.selection),
        subject_id="synthetic-subject",
    )
    assert retake.result.selected_take_id is None and retake.cursor.selection.take_id is None
    stored = RecordingTakeOperation.model_validate_json(
        nav.rig.store._operation_path("retake").read_bytes()
    ).model_dump()
    # A retake record claiming a selected take is rejected even with a matching cursor.
    stored["navigation"]["selected_take_id"] = kept.reservation.take_id
    stored["cursor"]["selection"]["take_id"] = kept.reservation.take_id
    with pytest.raises(ValidationError, match="only prompt selection selects a take"):
        RecordingTakeOperation.model_validate(stored)
    stored["kind"] = "navigate"
    RecordingTakeOperation.model_validate(stored)
    stored["cursor"]["mode"] = "continue-missing"
    with pytest.raises(ValidationError, match="only explicit prompt selection"):
        RecordingTakeOperation.model_validate(stored)
    # Coverage targets are missing by definition and carry no selection.
    missing = nav.missing()
    assert missing.result.selected_take_id is None and missing.cursor.selection.take_id is None


@pytest.mark.parametrize("action", ["remove", "reject"])
def test_prompt_selection_revalidates_native_head_against_a_stale_index_row(
    nav: NavigationRig, action: str
) -> None:
    older = nav.save(nav.capture("older"), "save-older").take
    newer = nav.save(nav.capture("newer"), "save-newer").take
    hook = nav.rig.store.projection_hook
    nav.rig.store.projection_hook = None
    nav.rig.store.decide(
        DecisionCommand(
            operation_id="decide-unprojected",
            selection=nav.cursor.selection,
            take_id=newer.reservation.take_id,
            expected_decision_revision=newer.decision_revision,
            action=action,
            reason="synthetic stale projection",
        )
    )
    nav.rig.store.projection_hook = hook
    assert nav.index.latest_retained_take(
        nav.context.group.catalog_sha256, newer.reservation.prompt
    ) == (newer.reservation.take_id)
    nav.select(1, "away")
    # The stale row selects nothing; it never falls back to a guess or a rejected take.
    stale = nav.select(0, "back-stale")
    assert stale.result.selected_take_id is None and stale.cursor.selection.take_id is None
    nav.index.update_take(nav.rig.store.get_take(newer.reservation.take_id))
    nav.select(1, "away-projected")
    assert nav.select(0, "back-projected").cursor.selection.take_id == older.reservation.take_id


@pytest.mark.parametrize("change", ["invalid-analysis", "other-prompt", "other-subject", "no-head"])
def test_prompt_selection_rejects_native_heads_that_disagree_with_the_row(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    kept = nav.save(nav.capture("kept")).take
    nav.select(1, "away")
    if change == "no-head":
        nav.rig.store.retained_take_resolver = lambda digest, prompt: "take-" + "d" * 64
    else:
        real = nav.rig.store._get_take

        def disagreeing(take_id: str) -> RecordingTake:
            take = real(take_id)
            reservation = take.reservation
            if change == "invalid-analysis":
                return take.model_copy(update={"analysis": "invalid"})
            if change == "other-prompt":
                other = nav.library.prompt_suite.prompts[2]
                reservation = reservation.model_copy(update={"prompt": other})
            else:
                group = reservation.group.model_copy(update={"subject_id": "another-subject"})
                reservation = reservation.model_copy(update={"group": group})
            return take.model_copy(update={"reservation": reservation})

        monkeypatch.setattr(nav.rig.store, "_get_take", disagreeing)
        assert nav.index.latest_retained_take(
            nav.context.group.catalog_sha256, kept.reservation.prompt
        ) == (kept.reservation.take_id)
    event = nav.select(0, "back")
    assert event.result.selected_take_id is None and event.cursor.selection.take_id is None


@pytest.mark.parametrize(
    "decision,analysis", [("removed", "pending"), ("rejected", "pending"), ("kept", "invalid")]
)
def test_prompt_selection_skips_takes_that_are_not_retained(
    nav: NavigationRig, decision: str, analysis: str
) -> None:
    older = nav.save(nav.capture("older"), "save-older").take
    newer = nav.save(nav.capture("newer"), "save-newer").take
    nav.index.update_take(
        RecordingTake.model_validate(
            newer.model_dump()
            | {
                "decision": decision,
                "analysis": analysis,
                "state_revision": newer.state_revision + 1,
            }
        )
    )
    nav.select(1, "away")
    assert nav.select(0, "back").cursor.selection.take_id == older.reservation.take_id


@pytest.mark.parametrize("state", ["building", "unknown"])
def test_incomplete_coverage_selects_no_take_but_still_navigates(
    nav: NavigationRig, state: str
) -> None:
    nav.save(nav.capture())
    nav.select(1, "away")
    nav.index.set_completeness(nav.context.group.catalog_sha256, state)
    event = nav.select(0, "back")
    assert event.cursor.selection.prompt_id == "synthetic-0000"
    assert event.result.selected_take_id is None and event.cursor.selection.take_id is None


def test_legacy_selection_trusts_complete_index_and_reads_no_audio(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = nav.save(nav.capture("native")).take
    nav.select(1, "away")
    requested: list[tuple[str, str]] = []

    def legacy(catalog_sha256: str, prompt: Any) -> str:
        requested.append((catalog_sha256, prompt.prompt_id))
        return "legacy-synthetic-take"

    nav.rig.store.retained_take_resolver = legacy
    original_open = os.open

    def guarded(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith(".wav"):
            raise AssertionError("prompt selection opened audio")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    event = nav.select(0, "legacy-back")
    assert requested == [(nav.context.group.catalog_sha256, "synthetic-0000")]
    assert event.cursor.selection.take_id == "legacy-synthetic-take"
    nav.library.bind_native_store(nav.rig.store, nav.index)
    nav.select(1, "away-native")
    assert nav.select(0, "native-back").cursor.selection.take_id == native.reservation.take_id


def test_workspace_backend_selection_prepares_review_without_another_commit(
    nav: NavigationRig,
) -> None:
    from cole_voice_lab.recording_service import RecordingCancellation

    kept = nav.save(nav.capture("kept")).take
    nav.select(1, "away")
    backend = RecordingWorkspaceBackend(
        nav.rig.layout,
        "synthetic-subject",
        nav.context.group.catalog_sha256,
        store=nav.rig.store,
        index=nav.index,
        discover=False,
    )

    def execute(data: dict[str, Any]) -> Any:
        meta = dict(
            request_id="selection-" + data["operation_id"],
            process_generation="service-generation",
            state_revision=backend.state_revision,
            event_sequence=0,
            take_id=data["selection"]["take_id"],
        )
        request = ServiceRequest.model_validate({"data": data, "meta": meta})
        backend.validate_request(request)
        event = backend.execute(request, RecordingCancellation())
        RecordingResponse.model_validate(
            {"data": event.model_dump(), "error": None, "meta": meta | {"take_id": kept_id}}
        )
        return event

    kept_id = kept.reservation.take_id
    selected = execute(
        {
            "command": "navigate",
            "operation_id": "select-kept",
            "selection": nav.cursor.selection.model_dump(),
            "mode": "selected-prompt",
            "target_prompt_id": "synthetic-0000",
        }
    )
    assert selected.cursor.selection.take_id == kept_id
    revision = nav.rig.store.state_revision
    review = execute(
        {
            "command": "review-take",
            "operation_id": "auto-review",
            "take_id": kept_id,
            "selection": selected.cursor.selection.model_dump(),
        }
    )
    assert review.cursor == selected.cursor and review.source.decision == "kept"
    assert nav.rig.store.state_revision == revision


class ServiceProcess:
    def __init__(
        self,
        directory: Path,
        layout: AssetLayout,
        catalog_sha256: str | None,
        generation: str,
        *,
        audit_directory: Path | None = None,
    ) -> None:
        """A real workspace service; ``catalog_sha256`` None omits ``--catalog-sha256``."""
        self.generation = generation
        self.counter = 0
        self.revision = 0
        self.sequence = 0
        self.client = synthetic_contracts()["capture-seal"].recorder_build
        self.build = recording_service_build_identity()
        self.evidence = ServiceProcessEvidence(directory)
        write_canonical_json(directory / "client-build.json", self.client)
        write_canonical_json(directory / "service-build.json", self.build)
        environment = os.environ.copy()
        if audit_directory is not None:
            environment["PYTHONPATH"] = (
                str(audit_directory) + os.pathsep + environment.get("PYTHONPATH", "")
            )
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cole_voice_lab.recording_service",
                "--asset-root",
                str(layout.root),
                *([] if catalog_sha256 is None else ["--catalog-sha256", catalog_sha256]),
                "--native-workspace",
                "--subject-id",
                "synthetic-subject",
                "--process-generation",
                generation,
                "--expected-client-build",
                str(directory / "client-build.json"),
                "--build-identity",
                str(directory / "service-build.json"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.evidence.stderr,
            env=environment,
            bufsize=0,
        )
        try:
            response = self.request(
                {
                    "command": "handshake",
                    "role": "ui",
                    "build": self.client,
                    "expected_peer_build": self.build,
                }
            )
            assert response.error is None
        except BaseException:
            self.close()
            raise

    def request(
        self, data: dict[str, Any], take_id: str | None = None, *, read: bool = True
    ) -> RecordingResponse | None:
        import select

        self.counter += 1
        message = ServiceRequest.model_validate(
            {
                "data": data,
                "meta": {
                    "request_id": f"request-{self.counter}",
                    "process_generation": self.generation,
                    "state_revision": self.revision,
                    "event_sequence": self.sequence,
                    "take_id": take_id,
                },
            }
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        frame = recording_frame_bytes(message)
        self.evidence.wire("sent", frame)
        self.process.stdin.write(frame)
        self.process.stdin.flush()
        if not read:
            return None
        deadline = time.monotonic() + 15
        while True:

            def read_exact(count: int) -> bytes:
                value = b""
                try:
                    while len(value) < count:
                        remaining = deadline - time.monotonic()
                        assert (
                            remaining > 0
                            and select.select([self.process.stdout], [], [], remaining)[0]
                        ), "service response deadline expired; " + self.evidence.describe(
                            self.process.poll()
                        )
                        chunk = self.process.stdout.read(count - len(value))
                        assert chunk, (
                            "service closed before the complete response frame; "
                            + self.evidence.describe(self.process.poll())
                        )
                        value += chunk
                    return value
                finally:
                    self.evidence.wire("received", value)

            length = read_exact(4)
            payload_size = int.from_bytes(length, "big")
            assert 0 < payload_size <= MAX_CONTROL_FRAME_BYTES
            response = RecordingResponse.model_validate_json(read_exact(payload_size))
            assert response.meta.process_generation == self.generation
            self.sequence = response.meta.event_sequence
            self.revision = response.meta.state_revision
            if response.meta.request_id == message.meta.request_id:
                return response
            # Background publications share the reliable framed stream. Keep waiting
            # for this request's reply, but never swallow an unsolicited error/unknown event.
            assert response.error is None, response.error
            assert response.data is not None and response.data.event in {
                "history",
                "analysis",
                "recovery",
                "recovery-inventory-changed",
            }

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        result = self.process.wait(timeout=5)
        self.evidence.exit(result)
        if result != 0:
            print(self.evidence.describe(result))
        self.evidence.stderr.close()


def test_real_service_bootstrap_capture_save_next_lost_reply_and_cursor_recovery(
    tmp_path: Path,
) -> None:
    import time
    from datetime import UTC, datetime

    from test_datasets import _corpus

    layout = _corpus(tmp_path.resolve() / "legacy").layout
    audit_directory = tmp_path / "audit-site"
    audit_directory.mkdir(mode=0o700)
    audit_log = tmp_path / "wave-opens.log"
    audit_log.touch(mode=0o600)
    (audit_directory / "sitecustomize.py").write_text(
        "import os, sys\n"
        f"audit_path = {str(audit_log)!r}\n"
        # Historical recording audio lives under the asset root. Scoping the audit there
        # keeps this gate exact: the service also hashes the whole stdlib tree for its
        # environment identity, and CPython ships its own sndhdr.wav test data.
        f"asset_root = {str(layout.root)!r}\n"
        "def observe(event, args):\n"
        "    if (event == 'open' and isinstance(args[0], (str, bytes))\n"
        "        and os.fsdecode(args[0]).endswith('.wav')\n"
        "        and os.fsdecode(args[0]).startswith(asset_root)\n"
        "        and '/recording-takes/' not in os.fsdecode(args[0])):\n"
        "        fd = os.open(audit_path, os.O_WRONLY | os.O_APPEND)\n"
        "        try: os.write(fd, b'wave-open\\n')\n"
        "        finally: os.close(fd)\n"
        "sys.addaudithook(observe)\n"
    )
    (audit_directory / "sitecustomize.py").chmod(0o600)
    reference, _ = activate_catalog(layout, store_suite(3))
    digest = reference.catalog_sha256
    service = ServiceProcess(
        tmp_path, layout, None, "service-first", audit_directory=audit_directory
    )
    try:
        # The app's order: catalog first, then a context on the reference it was served.
        served = service.request({"command": "catalog", "operation_id": "catalog-live"})
        assert served.error is None and served.data.reference == reference
        opened = service.request(
            OpenContextCommand(
                operation_id="open-live",
                subject_id="synthetic-subject",
                catalog_reference=served.data.reference,
                capture_device=DEVICE,
                capture_host="synthetic-host",
            ).model_dump()
        )
        assert opened.error is None and opened.data.event == "context"
        cursor = opened.data.cursor
        key = canonical_sha256(["synthetic-subject", digest])
        report_path = layout.root / "provenance/recording-library-discovery" / f"{key}.result.json"
        deadline = time.monotonic() + 30
        while not report_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        import json

        report = json.loads(report_path.read_bytes())
        assert report["completeness"] == "complete", report
        assert report["legacy_backfilled"] and audit_log.read_bytes()
        snapshot = service.request({"command": "snapshot"})
        assert snapshot.data.history.completeness == "complete"
        reserved = service.request(
            {
                "command": "reserve",
                "operation_id": "reserve-live",
                "group_id": cursor.group_id,
                "recorder_generation": "helper-generation",
                "selection": cursor.selection,
            }
        )
        assert reserved.error is None
        reservation = reserved.data.take.reservation
        cursor = reserved.data.cursor
        store = RecordingTakeStore(layout)
        rig = Rig(layout, store, cursor.group_id)
        receipt = rig.receipt(reservation)
        receipt = receipt.model_copy(
            update={"last_sample_at": reservation.requested_at, "sealed_at": datetime.now(UTC)}
        )
        registered = service.request(
            {"command": "register-seal", "operation_id": "seal-live", "receipt": receipt},
            reservation.take_id,
        )
        assert registered.error is None and registered.data.take.decision == "draft"
        command = rig.command(
            registered.data.take, "save-live", selection=cursor.selection, advance=True
        )
        service.request(command.model_dump(), reservation.take_id, read=False)
        operation = layout.root / "provenance/recording-takes/.operations/save-live.json"
        deadline = time.monotonic() + 5
        while not operation.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert operation.exists()
        service.close()
        previous_report = report_path.stat().st_mtime_ns
        audit_log.write_bytes(b"")
        service = ServiceProcess(
            tmp_path, layout, None, "service-restarted", audit_directory=audit_directory
        )
        deadline = time.monotonic() + 30
        while report_path.stat().st_mtime_ns == previous_report and time.monotonic() < deadline:
            time.sleep(0.05)
        assert report_path.stat().st_mtime_ns != previous_report
        report = json.loads(report_path.read_bytes())
        assert report["completeness"] == "complete" and not report["legacy_backfilled"], report
        snapshot = service.request({"command": "snapshot"})
        assert (
            snapshot.data.event == "context"
            and snapshot.data.cursor.selection.prompt_id == "spoken-0001"
        )
        assert snapshot.data.history.covered_prompt_count == 1
        replay = service.request(command.model_dump(), reservation.take_id)
        assert replay.error is None and replay.data.event == "saved"
        assert replay.data.operation.navigation.next_prompt.prompt_id == "spoken-0001"
        assert replay.data.take.decision_revision == 1
        assert replay.data.cursor == snapshot.data.cursor
        # Finish the owned service and its background children before opening
        # an independent audit store. The earlier close deliberately exercises
        # SIGKILL/lost-reply recovery; this final close is orderly quiescence.
        service.request({"command": "shutdown"})
        service.process.wait(timeout=5)
        assert len(RecordingTakeStore(layout).iter_takes()) == 1
        assert audit_log.read_bytes() == b""
    finally:
        service.close()


def test_blocked_context_preparation_does_not_block_metadata_or_replace_newer_selection(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from cole_voice_lab.recording_contracts import PreparedCaptureGate

    started, release = threading.Event(), threading.Event()

    def prepare(*args: Any, **kwargs: Any) -> PreparedCaptureGate:
        started.set()
        assert release.wait(10)
        return PreparedCaptureGate(state="unprepared")

    monkeypatch.setattr(nav.rig.store, "prepare_corpus_gate", prepare)
    backend = RecordingWorkspaceBackend(
        nav.rig.layout,
        "synthetic-subject",
        nav.context.group.catalog_sha256,
        store=nav.rig.store,
        index=nav.index,
        discover=False,
    )
    build = synthetic_contracts()["capture-seal"].recorder_build
    session = RecordingServiceSession(
        build=build, expected_client=build, process_generation="service-test", backend=backend
    )
    counter = 0

    def submit(data: dict[str, Any], take_id: str | None = None) -> str:
        nonlocal counter
        counter += 1
        request_id = f"request-{counter}"
        session.submit(
            ServiceRequest.model_validate(
                {
                    "data": data,
                    "meta": {
                        "request_id": request_id,
                        "process_generation": "service-test",
                        "state_revision": 0,
                        "event_sequence": 0,
                        "take_id": take_id,
                    },
                }
            )
        )
        return request_id

    def receive(request_id: str) -> RecordingResponse:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for response in session.completed():
                if response.meta.request_id == request_id:
                    return response
            time.sleep(0.005)
        raise AssertionError("metadata was blocked behind context preparation")

    try:
        receive(
            submit(
                {"command": "handshake", "role": "ui", "build": build, "expected_peer_build": build}
            )
        )
        before = nav.cursor
        corpus = PromptSuite.model_validate(
            store_suite(3).model_dump() | {"purpose": "capture-corpus"}
        )
        reference, _ = activate_catalog(nav.rig.layout, corpus)
        preparation = submit(
            OpenContextCommand(
                operation_id="prepare-slow",
                subject_id="synthetic-subject",
                catalog_reference=reference,
                capture_device=DEVICE,
                capture_host="host",
                expected_selection_revision=before.selection.selection_revision,
                pilot_approval_path="provenance/synthetic-approval.json",
                pilot_approval_sha256="0" * 64,
            ).model_dump()
        )
        assert started.wait(5)
        move = submit(
            NavigateCommand(
                operation_id="select-during-preparation",
                selection=before.selection,
                mode="selected-prompt",
                target_prompt_id="synthetic-0002",
            ).model_dump()
        )
        response = receive(move)
        assert (
            response.error is None and response.data.cursor.selection.prompt_id == "synthetic-0002"
        )
        release.set()
        stale = receive(preparation)
        assert stale.error.code == "RECORDING.STALE_REVISION"
        assert nav.cursor.selection.prompt_id == "synthetic-0002"
    finally:
        release.set()
        session.close()


def test_workspace_backend_context_history_replay_and_completion_publication(
    nav: NavigationRig,
) -> None:
    from cole_voice_lab.recording_service import RecordingCancellation, RecordingServiceError

    backend = RecordingWorkspaceBackend(
        nav.rig.layout,
        "synthetic-subject",
        nav.context.group.catalog_sha256,
        store=nav.rig.store,
        index=nav.index,
        discover=False,
    )
    events = []
    backend.set_event_publisher(lambda identity, event: events.append((identity, event)))
    backend._discovery_completed("group-obsolete")
    assert events == []
    backend._discovery_completed(nav.cursor.group_id)
    assert events[0][1].event == "history"

    def execute(data: dict[str, Any]) -> Any:
        request = ServiceRequest.model_validate(
            {
                "data": data,
                "meta": dict(
                    request_id="workspace-test",
                    process_generation="service-generation",
                    state_revision=backend.state_revision,
                    event_sequence=0,
                    take_id=data.get("selection", {}).get("take_id"),
                ),
            }
        )
        backend.validate_request(request)
        return backend.execute(request, RecordingCancellation())

    assert execute({"command": "snapshot"}).history.completeness == "complete"
    assert (
        execute({"command": "history", "catalog_sha256": nav.context.group.catalog_sha256}).event
        == "history"
    )
    with pytest.raises(RecordingServiceError, match="requested catalog"):
        execute({"command": "history", "catalog_sha256": "f" * 64})
    # The service opens the subject's active corpus-store catalog, named by its reference.
    reference, _ = activate_catalog(nav.rig.layout, store_suite(3))
    served = execute({"command": "catalog", "operation_id": "catalog"})
    assert served.event == "catalog" and served.reference == reference
    command = OpenContextCommand(
        operation_id="new-setup",
        subject_id="synthetic-subject",
        catalog_reference=served.reference,
        capture_device=DEVICE,
        capture_host="synthetic-host",
        new_setup=True,
        expected_selection_revision=nav.cursor.selection.selection_revision,
    )
    opened = execute(command.model_dump())
    assert opened.group.group_id != nav.context.group.group_id
    assert opened.group.catalog_reference == reference
    assert execute(command.model_dump()) == opened
    with pytest.raises(RecordingServiceError, match="workspace subject"):
        execute(command.model_copy(update={"subject_id": "another-subject"}).model_dump())
    assert nav.rig.store.iter_takes() == ()


@pytest.mark.parametrize(
    "error, code",
    [
        (SecurityError("changed"), "RECORDING.SOURCE_CHANGED"),
        (ContractError("broken"), "RECORDING.INVALID_TRANSITION"),
        (OSError("sync"), "RECORDING.WRITE_FAILED"),
    ],
)
def test_workspace_backend_preserves_named_failures(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    from cole_voice_lab.recording_service import RecordingCancellation, RecordingServiceError

    backend = RecordingWorkspaceBackend(
        nav.rig.layout,
        "synthetic-subject",
        nav.context.group.catalog_sha256,
        store=nav.rig.store,
        index=nav.index,
        discover=False,
    )

    def fail(group_id: str) -> Any:
        raise error

    monkeypatch.setattr(nav.rig.store, "get_group", fail)
    request = ServiceRequest.model_validate(
        {
            "data": {"command": "snapshot"},
            "meta": dict(
                request_id="failed-snapshot",
                process_generation="service-generation",
                state_revision=0,
                event_sequence=0,
            ),
        }
    )
    with pytest.raises(RecordingServiceError) as caught:
        backend.execute(request, RecordingCancellation())
    assert caught.value.recording_error.code == code


def test_save_and_next_skips_a_prompt_covered_only_through_an_alias(versions: Versions) -> None:
    """spoken-0000's earlier take keeps counting after a keep-recordings rewording."""
    rig, index = versions.rig, versions.index
    take_id = versions.take.reservation.take_id
    second, snapshot = edit_and_publish(
        rig.layout, "Synthetic sentence zero.", keep_recordings=True
    )
    library = RecordingLibrary(rig.layout, snapshot, "synthetic-subject")
    library.bind_native_store(rig.store, index)
    index.update_take(versions.take)
    assert index.register_catalog(snapshot) == second.catalog_sha256
    index.set_completeness(second.catalog_sha256, "complete")
    context = versions.open(second, "context-second")
    assert context.cursor.selection.prompt_id == "spoken-0000"
    # The real index nominates the alias-covered take when its prompt is selected.
    selected = versions.select("spoken-0000", "select-covered")
    assert selected.result.next_prompt == snapshot.prompt("spoken-0000")
    assert selected.result.selected_take_id == take_id
    continued = rig.store.navigate(
        NavigateCommand(
            operation_id="continue-missing",
            selection=versions.cursor.selection,
            mode="continue-missing",
        ),
        subject_id="synthetic-subject",
    )
    assert continued.cursor.selection.prompt_id == "spoken-0001"
    versions.select("spoken-0002", "select-last")
    cursor = versions.cursor
    reservation = rig.store.reserve(
        operation_id="reserve-last",
        group_id=cursor.group_id,
        prompt_id="spoken-0002",
        process_generation="helper-generation",
        selection=cursor.selection,
    )
    draft = rig.seal(reservation, "seal-last")
    saved = library.save_native_take(
        rig.store,
        rig.command(draft, "save-last", selection=versions.cursor.selection, advance=True),
    )
    # Save & Next wraps past spoken-0000, which only its alias covers.
    navigation = saved.operation.navigation
    assert navigation is not None
    assert navigation.next_prompt == snapshot.prompt("spoken-0001")
    assert saved.cursor.selection.prompt_id == "spoken-0001"
    page = library.indexed_history(index)
    assert (page.covered_prompt_count, page.missing_prompt_count) == (2, 1)

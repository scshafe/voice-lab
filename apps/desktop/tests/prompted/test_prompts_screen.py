"""Record → Prompts in the main window, driven like a person would (offscreen; any Qt/QML
warning fails a test): a session over the fake recorder, the keyboard (D-011), elapsed
from confirmed frames (D-014), take history with remove and restore, the trim and its
exact preview (D-012), Save & next, a failing take, Finish into a package (D-042)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import AppRig, RecordingOutput, Window, open_window
from PySide6.QtCore import Qt
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from PySide6.QtTest import QTest
from voice_lab_desktop.prompted import audio
from voice_lab_desktop.screens.prompts import elapsed_text

from cole_voice_lab.intake import validate_capture_package

from .conftest import make_app, wait_phase
from .recorder_fakes import FakeRecorder

SPOKEN = "reference-neutral-01"


def key(
    window: Window, which: Qt.Key, modifier: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
) -> None:
    QTest.keyClick(window.root, which, modifier)


def start(qtbot: Any, window: Window, chosen: tuple[str, ...] = (SPOKEN,)) -> Any:
    """A session over ``chosen`` (and the catalog's room tones), opened on its first prompt."""
    prompts = window.model.prompts
    prompts.chooseAll(False)
    for prompt_id in chosen:
        prompts.choosePrompt(prompt_id, True)
    assert prompts.canStart
    window.find("startPromptsButton").clicked.emit()
    wait_phase(qtbot, window.model, "ready")
    return prompts


def record_take(qtbot: Any, window: Window, recorder: FakeRecorder) -> int:
    """R, frames arrive, R: one take of the selected prompt. Returns its number."""
    prompts = window.model.prompts
    count = len(recorder.takes)
    key(window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    recorder.last.advance(4800)
    wait_phase(qtbot, window.model, "recording")
    key(window, Qt.Key.Key_R)
    wait_phase(qtbot, window.model, "ready")
    return int(prompts.reviewNumber)


def test_the_screen_starts_a_session_over_the_chosen_prompts(
    qtbot: Any, prompts_window: Window, recorder: FakeRecorder
) -> None:
    prompts = prompts_window.model.prompts
    assert prompts_window.find("promptsScreen").property("visible")
    assert prompts.catalogValue == "builtin:pilot-v2.json"
    assert prompts.deviceLabel == recorder.device
    assert len(prompts.catalogPrompts) == 36
    room = [row for row in prompts.catalogPrompts if row["always"]]
    assert [row["promptId"] for row in room] == ["room-tone-start", "room-tone-end"]
    assert prompts.selectedCount == 34  # every spoken prompt, by default
    prompts_window.find("chooseNone").clicked.emit()
    assert prompts.selectedCount == 0 and not prompts.canStart
    assert not prompts_window.find("startPromptsButton").property("enabled")
    box = prompts_window.items(f"choose-{SPOKEN}")[0]
    box.setProperty("checked", True)
    box.toggled.emit()
    assert prompts.selectedCount == 1 and prompts.canStart
    assert prompts_window.items("choose-room-tone-start")[0].property("enabled") is False

    prompts_window.find("startPromptsButton").clicked.emit()
    wait_phase(qtbot, prompts_window.model, "ready")
    assert prompts.sessionId.startswith("native-desktop-")
    assert [row["promptId"] for row in prompts.batch] == [
        "room-tone-start",
        SPOKEN,
        "room-tone-end",
    ]
    assert [row["status"] for row in prompts.batch] == ["missing"] * 3
    # The first prompt is room tone: a "Do not speak" card, no script (D-013).
    assert prompts.currentPromptId == "room-tone-start" and prompts.currentSilent
    assert prompts_window.find("silentCard").property("visible")
    assert "silent" in prompts_window.find("silentInstructions").property("text").lower()
    assert not prompts_window.find("scriptText").property("visible")
    prompts_window.items(f"batch-{SPOKEN}")[0]  # the batch shows every prompt
    prompts.selectPrompt(SPOKEN)
    assert prompts_window.find("scriptText").property("text").startswith("A quiet morning")
    assert not prompts.currentSilent
    assert prompts_window.find("recordTakeButton").property("text") == "Record (R)"


def test_r_records_and_stops_with_elapsed_from_confirmed_frames(
    qtbot: Any, prompts_window: Window, recorder: FakeRecorder
) -> None:
    prompts = start(qtbot, prompts_window)
    prompts.selectPrompt(SPOKEN)
    key(prompts_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    assert prompts.phase == "starting" and prompts.stateLabel == "Starting microphone"
    assert prompts.elapsedText == ""  # nothing until frames arrive (D-014)
    # The selected prompt is locked while recording (D-012).
    prompts.selectPrompt("room-tone-end")
    assert prompts.currentPromptId == SPOKEN
    recorder.last.advance(14_400)
    wait_phase(qtbot, prompts_window.model, "recording")
    qtbot.waitUntil(lambda: prompts.elapsedText == "0:00.3", timeout=5000)
    assert prompts.stateLabel == "Recording"
    assert [e["status"] for e in prompts_window.model.sidebar if e["key"] == "prompts"] == ["held"]
    key(prompts_window, Qt.Key.Key_R)
    wait_phase(qtbot, prompts_window.model, "ready")
    assert recorder.last.stops == 1
    assert prompts.reviewNumber == 1 and prompts.reviewStatus == "draft"
    assert prompts.elapsedText == elapsed_text(recorder.frames) == "0:00.5"
    assert [row["label"].split(" · ")[0] for row in prompts.takes] == ["Take 1"]
    assert prompts.takes[0]["statusLabel"] == "draft"
    qtbot.waitUntil(lambda: prompts.frameCount == recorder.frames, timeout=5000)
    assert len(prompts.bins) == 2 * 2048
    assert (prompts.trimStart, prompts.trimEnd) == (0, recorder.frames)
    assert prompts_window.find("recordTakeButton").property("text") == "Retake (R)"
    assert [e["status"] for e in prompts_window.model.sidebar if e["key"] == "prompts"] == ["none"]


def test_keys_are_ignored_while_typing_or_with_modifiers(
    qtbot: Any, prompts_window: Window, recorder: FakeRecorder
) -> None:
    prompts = start(qtbot, prompts_window)
    prompts.selectPrompt(SPOKEN)
    record_take(qtbot, prompts_window, recorder)
    field = prompts_window.find("trimInField")
    field.forceActiveFocus()
    qtbot.waitUntil(lambda: field.property("activeFocus"), timeout=2000)
    for which in (Qt.Key.Key_R, Qt.Key.Key_Space, Qt.Key.Key_Return):
        key(prompts_window, which)
    assert len(recorder.takes) == 1 and prompts.phase == "ready"
    assert prompts.reviewStatus == "draft"  # Return did not keep it
    assert prompts_window.model.player.state == "idle"
    prompts_window.find("scriptText").forceActiveFocus()
    prompts_window.root.contentItem().forceActiveFocus()
    key(prompts_window, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
    key(prompts_window, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier)
    assert len(recorder.takes) == 1
    # Return keeps the take; Space plays it.
    key(prompts_window, Qt.Key.Key_Return)
    assert prompts.reviewStatus == "kept"
    key(prompts_window, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: prompts_window.model.player.state == "playing", timeout=5000)


def test_the_preview_plays_exactly_the_trimmed_frames(
    qtbot: Any, prompts_window: Window, recorder: FakeRecorder
) -> None:
    prompts = start(qtbot, prompts_window)
    prompts.selectPrompt(SPOKEN)
    number = record_take(qtbot, prompts_window, recorder)
    qtbot.waitUntil(lambda: prompts.frameCount == recorder.frames, timeout=5000)
    # Through the stand-in's signal, as a drag would end.
    waveform = prompts_window.find("waveform")
    waveform.trimChanged.emit(1200, 9600)
    assert (prompts.trimStart, prompts.trimEnd) == (1200, 9600)
    assert prompts.takes[0]["trimmed"]
    assert prompts_window.find("trimInField").property("text") == "1200"
    output: RecordingOutput = prompts_window.rig.output
    prompts.togglePlay()
    qtbot.waitUntil(lambda: len(output.played) == 1, timeout=5000)
    original = recorder.last.take_dir / "original.wav"
    played = output.played[0]
    assert played.rate == 48_000 and played.samples.shape == (8400, 1)
    np.testing.assert_array_equal(played.samples[:, 0], audio.read_frames(original, 1200, 9600))
    assert prompts_window.model.player.activeKey == prompts.previewKey
    assert prompts_window.find("playTakeButton").property("text") == "Pause (Space)"
    prompts.togglePlay()
    assert prompts_window.model.player.state == "paused"
    # Through the model (the In/Out fields), then reset; the original is never touched.
    before = original.read_bytes()
    prompts.setTrim(-5, 10**9)
    assert (prompts.trimStart, prompts.trimEnd) == (0, recorder.frames)
    prompts.setTrim(4800, 2400)
    assert (prompts.trimStart, prompts.trimEnd) == (4800, 4801)
    assert prompts_window.model.player.state == "idle"  # a changed trim stops the preview
    prompts.resetTrim()
    assert (prompts.trimStart, prompts.trimEnd) == (0, recorder.frames)
    assert original.read_bytes() == before
    waveform.seekRequested.emit(100)  # nothing playing: ignored
    prompts.togglePlay()
    qtbot.waitUntil(lambda: len(output.played) == 2, timeout=5000)
    assert output.played[1].samples.shape == (recorder.frames, 1)
    waveform.seekRequested.emit(12_000)
    assert prompts_window.model.player.position == pytest.approx(0.25)
    assert number == 1


def test_history_remove_restore_and_save_next(
    qtbot: Any, prompts_window: Window, recorder: FakeRecorder
) -> None:
    prompts = start(qtbot, prompts_window, (SPOKEN, "expressive-02"))
    batch = [row["promptId"] for row in prompts.batch]
    assert batch == ["room-tone-start", SPOKEN, "expressive-02", "room-tone-end"]
    prompts.selectPrompt(SPOKEN)
    record_take(qtbot, prompts_window, recorder)
    record_take(qtbot, prompts_window, recorder)
    assert [row["number"] for row in prompts.takes] == [2, 1]  # newest first
    assert prompts.reviewNumber == 2
    prompts.selectTake(1)
    assert prompts.reviewNumber == 1
    prompts.removeTake(2)
    assert [row["status"] for row in prompts.takes] == ["removed", "draft"]
    restore = [i for i in prompts_window.items("restoreTake") if i.property("visible")]
    assert len(restore) == 1
    restore[0].clicked.emit()
    assert [row["status"] for row in prompts.takes] == ["draft", "draft"]
    # Save & next keeps take 1 and moves on to the next prompt missing a kept take.
    prompts_window.find("saveNextButton").clicked.emit()
    assert prompts.currentPromptId == "expressive-02"
    assert {row["promptId"]: row["status"] for row in prompts.batch}[SPOKEN] == "kept"
    record_take(qtbot, prompts_window, recorder)
    prompts.saveNext()
    assert prompts.currentPromptId == "room-tone-end"
    record_take(qtbot, prompts_window, recorder)
    prompts.saveNext()  # wraps around to the first prompt still missing
    assert prompts.currentPromptId == "room-tone-start"
    record_take(qtbot, prompts_window, recorder)
    prompts.saveNext()
    assert prompts.keptCount == 4 and prompts.finishBlocker == ""
    assert prompts.currentPromptId == "room-tone-start"  # nothing left to move to


def test_a_failing_take_is_never_a_take(
    qtbot: Any, prompts_window: Window, recorder: FakeRecorder
) -> None:
    prompts = start(qtbot, prompts_window)
    prompts.selectPrompt(SPOKEN)
    recorder.fail_next = "continuity failed: 512 frames missing"
    key(prompts_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    recorder.last.advance(2400)
    key(prompts_window, Qt.Key.Key_R)
    wait_phase(qtbot, prompts_window.model, "ready")
    assert prompts.lastError == "continuity failed: 512 frames missing"
    assert prompts_window.find("takeError").property("visible")
    assert [row["status"] for row in prompts.takes] == ["failed"]
    assert prompts.takes[0]["message"] == "continuity failed: 512 frames missing"
    assert prompts.reviewNumber == 0
    # The worker stops by itself (its maximum duration): the take is taken all the same.
    key(prompts_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
    recorder.last.advance(100)
    recorder.last.finish_by_itself()
    wait_phase(qtbot, prompts_window.model, "ready")
    assert prompts.reviewNumber == 2 and prompts.lastError == ""
    # A worker that cannot start leaves a failed take directory, nothing else.
    recorder.start_error = "the capture worker did not start"
    prompts.record()
    wait_phase(qtbot, prompts_window.model, "ready")
    assert prompts.lastError == "the capture worker did not start"
    assert [row["status"] for row in prompts.takes] == ["failed", "draft", "failed"]


def test_finish_builds_the_package_and_uploads_hold_it(
    qtbot: Any, prompts_window: Window, recorder: FakeRecorder
) -> None:
    prompts = start(qtbot, prompts_window)
    for prompt_id in ("room-tone-start", SPOKEN):
        prompts.selectPrompt(prompt_id)
        record_take(qtbot, prompts_window, recorder)
        prompts.save()
    assert "1 prompt(s) still need a kept take." == prompts.finishBlocker
    prompts.finishSession()
    assert prompts.phase == "ready"  # not finished: room-tone-end has no kept take
    prompts.selectPrompt("room-tone-end")
    record_take(qtbot, prompts_window, recorder)
    prompts.save()
    session_id = prompts.sessionId
    prompts_window.find("finishPromptsButton").clicked.emit()
    wait_phase(qtbot, prompts_window.model, "packaged")
    assert prompts_window.find("packageBanner").property("visible")
    assert session_id in prompts.packageText
    store = prompts_window.model.prompted
    path = store.package_dir(session_id)
    assert validate_capture_package(path).manifest.capture_device.backend == "portaudio"
    assert store.load(session_id).state == "packaged"
    # Locked once finished.
    prompts.record()
    assert len(recorder.takes) == 3
    uploads = prompts_window.model.uploads
    rows = {row["sessionId"]: row for row in uploads.rows}
    assert rows[session_id]["state"] == "held" and rows[session_id]["kind"] == "prompted"
    assert "until spike S-1 passes" in rows[session_id]["detail"]
    assert uploads.heldCount == 1 and uploads.pendingCount == 0
    # A new session.
    prompts_window.find("newPromptsSession").clicked.emit()
    assert prompts.phase == "setup" and prompts.sessionId == ""
    qtbot.waitUntil(lambda: prompts.canStart, timeout=5000)


def test_set_aside_and_resume_after_a_restart(
    qtbot: Any, data_dir: Path, tmp_path: Path, recorder: FakeRecorder
) -> None:
    model = make_app(data_dir, tmp_path, recorder)
    try:
        prompts = model.prompts
        prompts.activate()
        qtbot.waitUntil(lambda: prompts.canStart, timeout=5000)
        prompts.startSession()
        first = prompts.sessionId
    finally:
        model.shutdown()
    again = make_app(data_dir, tmp_path, recorder)
    try:
        prompts = again.prompts
        assert prompts.sessionId == first and prompts.phase == "ready"
        prompts.abandonSession()
        assert prompts.phase == "setup"
        assert again.prompted.load(first).state == "abandoned"
    finally:
        again.shutdown()


def test_no_umik_means_no_session(qtbot: Any, data_dir: Path, tmp_path: Path) -> None:
    recorder = FakeRecorder(refused="No UMIK-1 found: plug in the miniDSP UMIK-1.")
    model = make_app(data_dir, tmp_path, recorder)
    try:
        with open_window(AppRig(model, RecordingOutput(), [], None, tmp_path)) as window:
            qtbot.waitUntil(lambda: model.prompts.deviceLabel != "", timeout=5000)
            assert not model.prompts.deviceOk and not model.prompts.canStart
            assert window.find("promptsDeviceChip").property("text") == "No UMIK-1"
            assert "plug in the miniDSP UMIK-1" in window.find("promptsDeviceMessage").property(
                "text"
            )
    finally:
        model.shutdown()

    def broken() -> Any:
        raise ModuleNotFoundError("No module named 'voice_lab_desktop.capture.process'")

    from voice_lab_desktop.screens.prompts import PromptsModel

    unavailable = make_app(data_dir, tmp_path, recorder)
    try:
        prompts: PromptsModel = unavailable.prompts
        prompts.recorder_factory = broken
        prompts._recorder = None
        prompts.checkDevice()
        assert not prompts.deviceOk and "capture worker is not available" in prompts.deviceLabel
    finally:
        unavailable.shutdown()

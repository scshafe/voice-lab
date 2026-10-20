"""The studio's transport and review (RECORDING-V1.md §5, step 6b), in the main window over the
PC rig (offscreen, a stand-in recorder that leaves the worker's files, the recording audio
device): the take picker lists this machine's and the PC's takes; Remove, Reject (only with
a reason) and Restore go through the corpus client and show in the history and the
status; In and Out from the playhead, Save trim and Reset, with a preview of exactly the
trimmed frames; Zoom and Fit; the ±0.1 s steps; the live waveform; a layout the transport
never moves; and a take the client adopted from the PC shows as changed elsewhere."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import QPointF
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from studio_fakes import StudioRecorder
from voice_lab_desktop.prompted.audio import read_frames

from .conftest import open_studio, ready, studio_app, wait_phase
from .test_studio_screen import click, kept_elsewhere, prompt_ids, resize
from .test_studio_screen import geometry as screen_geometry


def recorded(qtbot: Any, window: Window, recorder: StudioRecorder) -> None:
    """Record, frames arrive (written to the worker's partial WAV), Stop: under review."""
    studio = window.model.studio
    count = len(recorder.takes)
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    qtbot.waitUntil(lambda: studio.takes.frameCount > 0, timeout=5000)


def saved(qtbot: Any, window: Window, recorder: StudioRecorder) -> str:
    """A take recorded and saved (kept); it stays selected. Returns its id."""
    studio = window.model.studio
    recorded(qtbot, window, recorder)
    studio.save()
    wait_phase(qtbot, studio, "ready")
    qtbot.waitUntil(lambda: studio.takes.selectedKey.startswith("take-"), timeout=5000)
    qtbot.waitUntil(lambda: studio.takes.frameCount > 0, timeout=5000)
    return str(studio.takes.selectedKey)


def output(window: Window) -> Any:
    return window.model.player._output


def preview(qtbot: Any, window: Window) -> Any:
    """Play the selected take; the clip the device was handed."""
    takes = window.model.studio.takes
    played = len(output(window).played)
    qtbot.waitUntil(lambda: takes.canPlay, timeout=5000)  # loaded: Play is enabled
    takes.togglePlay()
    qtbot.waitUntil(lambda: len(output(window).played) > played, timeout=5000)
    qtbot.waitUntil(lambda: takes.playing, timeout=5000)
    return output(window).played[-1]


def test_the_picker_lists_this_machines_and_the_pcs_takes(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    kept_elsewhere(pc, tmp_path, (0,))
    ids = prompt_ids(pc)
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio, takes = model.studio, model.studio.takes
            assert studio.promptId == ids[1] and takes.takeCount == 0
            assert window.find("studioTakeSelect").property("displayText") == (
                "No takes of this prompt yet"
            )
            # Selecting a prompt selects its latest kept take: here, the PC's, played from there.
            studio.selectPrompt(ids[0])
            qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=10000)
            (pc_take,) = takes.takeOptions
            assert takes.selectedKey == pc_take["value"]
            assert pc_take["label"].endswith("· Kept · PC")
            # Recorded elsewhere, decided here all the same (noted in the spool when changed).
            assert takes.canRemove and takes.canReject and not takes.canRestore
            assert takes.actionNote == ""
            assert takes.canPlay and takes.waveNote == "dB display scale"
            # A take recorded here joins it; while under review it comes first, not saved.
            recorded(qtbot, window, recorder)
            assert takes.takeOptions[0]["value"] == "review"
            assert "Not saved" in takes.takeOptions[0]["label"]
            studio.save()
            wait_phase(qtbot, studio, "ready")
            qtbot.waitUntil(lambda: takes.takeCount == 2, timeout=5000)
            local = studio.client.takes()[0].take_id
            assert takes.selectedKey == local
            labels = {o["value"]: o["label"] for o in takes.takeOptions}
            assert labels[local].endswith("· Kept · here")
            assert [row["where"] for row in takes.history] == ["the PC", "this machine"]
            assert all(row["when"] and row["length"].endswith(" s") for row in takes.history)
            assert takes.canRemove and takes.canReject and takes.actionNote == ""
            # The picker's list is the history: every take, and a click selects it.
            click(window, "studioTakeSelect")
            popup = window.find("studioHistoryPopup")
            qtbot.waitUntil(lambda: popup.property("visible"), timeout=5000)
            name = f"studioHistoryRow-{pc_take['value']}"
            qtbot.waitUntil(lambda: bool(window.items(name)), timeout=5000)
            window.items(name)[0].clicked.emit()
            assert takes.selectedKey == pc_take["value"]
            qtbot.waitUntil(lambda: not popup.property("visible"), timeout=5000)
            qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=10000)
    finally:
        model.shutdown()


def test_each_decision_goes_through_the_client(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    take_id = saved(qtbot, studio_window, recorder)
    client = studio.client

    def decision() -> tuple[str, str | None]:
        local = client.spool.take(take_id).sync.decision
        return local.decision, local.reason

    assert decision() == ("kept", None) and studio.coverageText == "1 / 34"
    click(studio_window, "studioRemoveTake")
    qtbot.waitUntil(lambda: takes.history[0]["status"].startswith("Removed"), timeout=5000)
    assert decision() == ("removed", None)
    qtbot.waitUntil(lambda: studio.coverageText == "0 / 34", timeout=5000)
    assert studio.promptStatus == "Missing" and takes.canRestore and not takes.canRemove
    click(studio_window, "studioRestoreTake")
    qtbot.waitUntil(lambda: decision() == ("kept", None), timeout=5000)
    qtbot.waitUntil(lambda: studio.coverageText == "1 / 34", timeout=5000)
    # Reject asks why; the reason goes with the decision.
    click(studio_window, "studioRejectTake")
    dialog = studio_window.find("studioRejectDialog")
    qtbot.waitUntil(lambda: dialog.property("visible"), timeout=5000)
    reason = studio_window.items("studioRejectReason")[0]
    confirm = studio_window.items("studioConfirmReject")[0]
    assert not confirm.property("enabled")
    reason.setProperty("text", "a chair creaked")
    assert confirm.property("enabled")
    confirm.clicked.emit()
    qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=5000)
    assert decision() == ("rejected", "a chair creaked")
    qtbot.waitUntil(
        lambda: takes.history[0]["status"].startswith("Rejected (a chair creaked)"), timeout=5000
    )
    assert studio.promptStatus == "Missing"
    takes.restore()
    qtbot.waitUntil(lambda: decision() == ("kept", None), timeout=5000)
    # A draft left by leaving its prompt is kept by Save once selected.
    ids = prompt_ids(pc)
    studio.selectPrompt(ids[1])
    recorded(qtbot, studio_window, recorder)
    studio.selectPrompt(ids[2])
    studio.selectPrompt(ids[1])
    qtbot.waitUntil(lambda: takes.takeCount == 1, timeout=5000)
    assert takes.selectedKey == "" and not studio.canSave  # a draft is not selected for you
    (draft,) = takes.takeOptions
    assert "Draft" in draft["label"]
    takes.selectTake(draft["value"])
    assert studio.canSave and studio.canSaveNext
    click(studio_window, "studioSave")
    qtbot.waitUntil(lambda: "Kept" in takes.takeOptions[0]["label"], timeout=5000)
    assert client.spool.take(draft["value"]).sync.decision.decision == "kept"


def test_reject_without_a_reason_is_impossible(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    take_id = saved(qtbot, studio_window, recorder)
    for blank in ("", "   ", "\n\t"):
        assert takes.reject(blank) is False
    assert studio.client.spool.take(take_id).sync.decision.decision == "kept"
    click(studio_window, "studioRejectTake")
    reason = studio_window.items("studioRejectReason")[0]
    confirm = studio_window.items("studioConfirmReject")[0]
    reason.setProperty("text", "    ")
    assert not confirm.property("enabled")
    reason.accepted.emit()  # Return in the field
    assert studio.client.spool.take(take_id).sync.decision.decision == "kept"
    studio_window.find("studioRejectDialog").reject()
    # The take under review is not a take yet: never rejected, never discarded (Save keeps
    # it; Retake or leaving keeps it as a draft).
    recorded(qtbot, studio_window, recorder)
    assert takes.selectedKey == "review"
    assert not takes.canReject and not takes.canRemove
    assert takes.actionNote == "Not saved yet: Save keeps it; Retake or leaving keeps it as a draft"
    assert not studio.canDiscard


def test_trims_save_and_reset_with_a_preview_of_exactly_the_kept_frames(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    recorder.seconds = 0.5  # 24,000 frames
    recorded(qtbot, studio_window, recorder)
    assert (takes.trimStart, takes.trimEnd, takes.frameCount) == (0, 24_000, 24_000)
    # In and Out from the playhead, on the take under review: kept with it at Save.
    takes.seekTo(4_800)
    click(studio_window, "studioSetIn")
    takes.seekTo(19_200)
    click(studio_window, "studioSetOut")
    assert (takes.trimStart, takes.trimEnd, takes.trimDirty) == (4_800, 19_200, True)
    assert takes.inText == "00:00.10" and takes.outText == "00:00.40"
    assert not takes.canSaveTrim  # Save keeps the take with its trim
    assert studio_window.find("studioTrimEdited").property("visible")
    studio.save()
    wait_phase(qtbot, studio, "ready")
    qtbot.waitUntil(lambda: takes.selectedKey.startswith("take-"), timeout=5000)
    take = studio.client.spool.take(takes.selectedKey)
    sync = take.sync
    assert (sync.trim.start_frame, sync.trim.end_frame, sync.decision.decision) == (
        4_800,
        19_200,
        "kept",
    )
    qtbot.waitUntil(lambda: takes.frameCount == 24_000, timeout=5000)
    assert (takes.trimStart, takes.trimEnd, takes.trimDirty) == (4_800, 19_200, False)
    # The preview plays exactly the kept frames of the original.
    clip = preview(qtbot, studio_window)
    assert np.array_equal(clip.samples[:, 0], read_frames(take.audio, 4_800, 19_200))
    assert clip.samples.shape == (14_400, 1)
    assert takes.timeText == "00:00.00 / 00:00.30"
    # A new In, saved: the recipe changes, the original never does.
    original = take.audio.read_bytes()
    takes.seekTo(9_600)
    takes.setIn()
    assert not takes.playing  # an edit stops the preview of the old frames
    assert takes.trimDirty and takes.canSaveTrim
    click(studio_window, "studioSaveTrim")
    qtbot.waitUntil(lambda: not takes.trimDirty, timeout=5000)
    sync = studio.client.spool.take(take.take_id).sync
    assert (sync.trim.start_frame, sync.trim.end_frame) == (9_600, 19_200)
    clip = preview(qtbot, studio_window)
    assert np.array_equal(clip.samples[:, 0], read_frames(take.audio, 9_600, 19_200))
    # Reset: the whole take again.
    click(studio_window, "studioResetTrim")
    qtbot.waitUntil(lambda: takes.trimEnd - takes.trimStart == 24_000, timeout=5000)
    sync = studio.client.spool.take(take.take_id).sync
    assert (sync.trim.start_frame, sync.trim.end_frame) == (None, None)
    clip = preview(qtbot, studio_window)
    assert np.array_equal(clip.samples[:, 0], read_frames(take.audio))
    assert take.audio.read_bytes() == original
    # Dragging the handles sets both edges at once.
    takes.setTrimRange(1_000, 23_000)
    assert (takes.trimStart, takes.trimEnd, takes.trimDirty) == (1_000, 23_000, True)
    takes.resetTrim()
    assert (takes.trimStart, takes.trimEnd, takes.trimDirty) == (0, 24_000, False)


def test_zoom_and_fit(qtbot: Any, studio_window: Window, recorder: StudioRecorder) -> None:
    takes = studio_window.model.studio.takes
    recorder.seconds = 0.5
    saved(qtbot, studio_window, recorder)
    wave = studio_window.find("studioWave")
    assert (takes.viewStart, takes.viewEnd) == (0, 24_000)
    assert not takes.canZoomOut and takes.canZoomIn
    assert 0 < len(takes.bins) <= 2048 and all(-1 <= v <= 1 for v in takes.bins)
    takes.seekTo(12_000)
    click(studio_window, "studioZoomIn")
    assert (takes.viewStart, takes.viewEnd) == (6_000, 18_000)
    assert (wave.property("viewStart"), wave.property("viewEnd")) == (6_000, 18_000)
    assert takes.canZoomOut
    while takes.canZoomIn:
        takes.zoomIn()
    assert takes.viewEnd - takes.viewStart == 2_400  # a twentieth of a second
    assert takes.viewStart <= 12_000 <= takes.viewEnd
    takes.zoomOut()
    assert takes.viewEnd - takes.viewStart == 4_800
    click(studio_window, "studioFit")
    assert (takes.viewStart, takes.viewEnd) == (0, 24_000) and not takes.canZoomOut
    # A click in the zoomed waveform moves the playhead to the frame under it.
    takes.zoomIn()
    start, end = takes.viewStart, takes.viewEnd
    corner = wave.mapToScene(QPointF(wave.width() / 4, wave.height() / 2)).toPoint()
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.mouseClick(
        studio_window.root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, corner
    )
    expected = start + (end - start) / 4
    qtbot.waitUntil(lambda: abs(takes.playhead - expected) < (end - start) / 50, timeout=5000)


def test_steps_of_a_tenth_of_a_second(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    takes = studio_window.model.studio.takes
    recorder.seconds = 0.5
    saved(qtbot, studio_window, recorder)
    takes.setTrimRange(4_800, 19_200)
    takes.seekToStart()
    assert takes.playhead == 4_800
    click(studio_window, "studioStepForward")
    assert takes.playhead == 9_600
    click(studio_window, "studioStepBack")
    assert takes.playhead == 4_800
    takes.stepBack()  # never before In
    assert takes.playhead == 4_800
    takes.seekToEnd()
    takes.stepForward()  # never after Out
    assert takes.playhead == 19_200
    takes.seekBy(-9_600)  # what ← and → will call (6c), in frames
    assert takes.playhead == 9_600
    # Playing from the playhead, then a step: the device follows, inside the kept frames.
    clip = preview(qtbot, studio_window)
    assert clip.samples.shape[0] == 14_400
    qtbot.waitUntil(lambda: output(studio_window).frame == 4_800, timeout=5000)
    takes.stepForward()
    assert takes.playhead == 14_400 and output(studio_window).frame == 9_600
    takes.togglePlay()
    assert not takes.playing


def test_the_live_waveform_while_recording(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    assert takes.live and takes.slots == 500
    assert not takes.canPlay and not studio_window.find("studioTakeSelect").property("enabled")
    for _ in range(5):
        recorder.last.advance(4_800, amplitude=0.25)
    qtbot.waitUntil(lambda: len(takes.bins) == 2 * 50, timeout=5000)
    assert takes.waveNote == "Live · dB display scale"
    assert takes.levelText == "-12.0 dBFS" and takes.levelPeakDb == pytest.approx(-12.0, abs=0.05)
    assert studio_window.find("studioLevel").property("level") == takes.levelPeakDb
    # The dB display scale: −12 dBFS draws at 80% of the half-height.
    assert max(takes.bins) == pytest.approx(0.8, abs=0.005)
    assert min(takes.bins) == pytest.approx(-0.8, abs=0.005)
    assert studio_window.find("studioLevel").property("visible")
    assert studio_window.find("studioWave").property("live")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    assert not takes.live and takes.selectedKey == "review"
    assert not studio_window.find("studioLevel").property("visible")
    assert takes.levelPeakDb == takes.levelFloorDb


SIX_B = (
    "studioTakeSelect",
    "studioRemoveTake",
    "studioRejectTake",
    "studioRestoreTake",
    "studioWave",
    "studioSetIn",
    "studioSetOut",
    "studioSaveTrim",
    "studioResetTrim",
    "studioZoomOut",
    "studioZoomIn",
    "studioFit",
    "studioStepBack",
    "studioPlay",
    "studioStepForward",
    "studioScriptCard",
    "studioFooter",
    "studioStatus",
)


def geometry(qtbot: Any, window: Window) -> dict[str, tuple[float, float, float, float]]:
    """6b's regions, read once the window has laid out (``test_studio_screen.geometry``)."""
    return screen_geometry(qtbot, window, SIX_B)


def test_transport_and_review_never_move_the_layout(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    for size in ((1440, 900), (960, 600)):
        resize(qtbot, studio_window, *size)
        idle = geometry(qtbot, studio_window)
        # Inside the content area (the 16-point inset), the trim row's last button included.
        right = size[0] - 16
        assert all(x + w <= right for x, _y, w, _h in idle.values()), idle
        seen = {}
        count = len(recorder.takes)
        studio.toggleRecord()
        qtbot.waitUntil(lambda count=count: len(recorder.takes) == count + 1, timeout=5000)
        recorder.last.advance(9_600)
        wait_phase(qtbot, studio, "recording")
        seen["recording"] = geometry(qtbot, studio_window)
        studio.toggleRecord()
        wait_phase(qtbot, studio, "review")
        qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
        takes.seekTo(2_400)
        takes.setIn()
        seen["review, trimmed"] = geometry(qtbot, studio_window)
        studio.save()
        wait_phase(qtbot, studio, "ready")
        qtbot.waitUntil(lambda: takes.selectedKey.startswith("take-"), timeout=5000)
        qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
        seen["saved"] = geometry(qtbot, studio_window)
        preview(qtbot, studio_window)
        seen["playing"] = geometry(qtbot, studio_window)
        takes.togglePlay()
        takes.zoomIn()
        seen["zoomed"] = geometry(qtbot, studio_window)
        takes.remove()
        seen["removed"] = geometry(qtbot, studio_window)
        for state, found in seen.items():
            assert found == idle, (size, state)
        takes.restore()
        studio.selectPrompt(prompt_ids(pc)[5 if size == (1440, 900) else 6])


def test_a_take_changed_elsewhere_shows_so(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, library: Any
) -> None:
    recorder = StudioRecorder(simulated=False)  # a take the PC accepts
    model = studio_app(data_dir, tmp_path, pc, recorder, enabled=True)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio, takes = model.studio, model.studio.takes
            take_id = saved(qtbot, window, recorder)
            client = studio.client
            qtbot.waitUntil(lambda: client.status().sent == 2, timeout=30000)  # upload, kept
            qtbot.waitUntil(
                lambda: takes.history[0]["where"] == "this machine and the PC", timeout=10000
            )
            # Another client removes the take on the PC; here it is rejected meanwhile.
            status, answer = pc.send(
                "POST",
                f"/api/v1/corpus/takes/{take_id}/decision",
                {"request_id": "req-" + "e" * 32, "decision": "removed", "expected_revision": 1},
            )
            assert status == 202, answer
            pc.publish()
            assert takes.reject("too fast")
            # The PC refuses the change and the client adopts its state: the history says so.
            qtbot.waitUntil(lambda: takes.history[0]["changedElsewhere"], timeout=30000)
            assert takes.history[0]["status"].startswith("Removed")
            assert "changed elsewhere" in takes.history[0]["status"]
            assert client.spool.take(take_id).sync.decision.decision == "removed"
            qtbot.waitUntil(lambda: studio.syncText.endswith(": changed elsewhere"), timeout=5000)
            assert studio.promptStatus == "Missing"
            click(window, "studioTakeSelect")
            qtbot.waitUntil(lambda: bool(window.items("studioHistoryStatus")), timeout=5000)
            status_line = window.items("studioHistoryStatus")[0]
            assert "changed elsewhere" in status_line.property("text")
    finally:
        model.shutdown()


def test_a_late_history_never_moves_the_selection(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    """The PC's history of the selected prompt, answering only once a take is starting (and
    then recorded and under review), never changes the selection or the waveform."""
    kept_elsewhere(pc, tmp_path, (1,))
    ids = prompt_ids(pc)
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path):
            ready(qtbot, model)
            studio, takes = model.studio, model.studio.takes
            client = studio.client
            real = client.prompt_history
            asked, gate = threading.Event(), threading.Event()

            def late(prompt_id: str) -> Any:
                asked.set()
                assert gate.wait(10)
                return real(prompt_id)

            client.prompt_history = late  # type: ignore[method-assign]
            studio.selectPrompt(ids[1])  # its latest kept take is the PC's
            qtbot.waitUntil(asked.is_set, timeout=5000)
            assert takes.selectedKey == ""
            studio.toggleRecord()
            qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
            assert studio.phase == "starting"
            gate.set()  # the answer comes in while the take starts
            qtbot.waitUntil(lambda: len(takes.history) == 1, timeout=10000)
            assert takes.selectedKey == "" and takes.frameCount == 0
            recorder.last.advance(9_600)
            wait_phase(qtbot, studio, "recording")
            qtbot.waitUntil(lambda: takes.live and len(takes.bins) > 0, timeout=5000)
            assert takes.selectedKey == ""
            studio.toggleRecord()
            wait_phase(qtbot, studio, "review")
            qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
            assert takes.selectedKey == "review" and not takes.live
    finally:
        model.shutdown()

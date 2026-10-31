"""The prompt studio in the main window, driven as a person would (offscreen; any Qt or QML
warning fails a test), over the PC (``corpus_rig.PC``) and a stand-in recorder that leaves
the worker's files: the pinned catalog and its coverage from the PC's library and this
machine's takes, Continue Missing, the filters and search, Record → Stop → Save into the
local spool, Save & Next, Retake and Discard, and a layout that transport never moves."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import Window
from corpus_rig import PC, bind_library, client, record
from PySide6.QtCore import QPointF, Qt
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder

from .conftest import open_studio, ready, studio_app, wait_phase


def click(window: Window, name: str) -> None:
    """A left click in the middle of the item named ``name``, where a person would click."""
    item = window.find(name)
    assert item.isVisible() and item.isEnabled(), name
    centre = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window.root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)


def visible_texts(item: Any) -> list[str]:
    """The text of every visible item under ``item``."""
    if not item.isVisible():
        return []
    found = []
    text = item.property("text")
    if isinstance(text, str) and text:
        found.append(text)
    for child in item.childItems():
        found.extend(visible_texts(child))
    return found


def prompt_ids(pc: PC) -> list[str]:
    return [prompt.prompt_id for prompt in pc.catalog[1].suite.prompts]


def kept_elsewhere(pc: PC, tmp_path: Path, indices: tuple[int, ...]) -> None:
    """Takes of these prompts recorded and kept by another machine, on the PC."""
    other_dir = tmp_path / "other-machine"
    other_dir.mkdir()
    bind_library(other_dir)
    other = client(other_dir, pc, enabled=True)
    other.run_pass()
    ids = prompt_ids(pc)
    takes = [record(other, ids[index], seed=700 + index) for index in indices]
    assert other.run_pass().uploaded == len(takes)
    for take in takes:
        other.decide(take.take_id, "kept")
    assert other.run_pass().applied == len(takes)
    assert pc.library().coverage.kept == len(indices)


def take_one(qtbot: Any, window: Window, recorder: StudioRecorder, frames: int = 12_000) -> None:
    """Record (the footer's control), frames arrive, Stop: a take under review."""
    studio = window.model.studio
    count = len(recorder.takes)
    click(window, "studioRecord")
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    recorder.last.advance(frames)
    wait_phase(qtbot, studio, "recording")
    click(window, "studioRecord")
    wait_phase(qtbot, studio, "review")


def rows(window: Window) -> list[str]:
    model = window.model.studio.rowsModel
    return [row["promptId"] for row in model.rows]


def test_the_workspace_loads_the_pinned_catalog_and_its_coverage(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    kept_elsewhere(pc, tmp_path, (0, 2))
    ids = prompt_ids(pc)
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            qtbot.waitExposed(window.root)
            ready(qtbot, model)
            studio = model.studio
            qtbot.waitUntil(lambda: studio.coverageText == "2 / 34", timeout=5000)
            assert window.find("studioScreen").property("visible")
            assert window.find("studioCoverage").property("text") == "2 / 34"
            assert studio.coverageSpoken == "2 of 34 prompts recorded"
            assert studio.coverageSource.startswith("The PC's library (read ")
            assert studio.catalogText == "cole-corpus · v1" and studio.switchText == ""
            # Every prompt of the pinned version, in catalog order, with its status.
            assert rows(window) == ids and studio.rowCount == 34
            statuses = {row["promptId"]: row["statusTitle"] for row in studio.rowsModel.rows}
            assert statuses[ids[0]] == statuses[ids[2]] == "Recorded"
            assert statuses[ids[1]] == "Missing"
            assert window.items(f"studioRow-{ids[1]}")
            # It opens on the first missing prompt: its script, its labels, its guidance.
            prompt = pc.catalog[1].suite.prompts[1]
            assert studio.promptId == ids[1] and studio.promptStatus == "Missing"
            assert window.find("studioScript").property("text") == prompt.text
            assert window.find("studioPromptHeading").property("visible")
            assert not window.find("studioBanner").property("visible")
            assert window.find("studioGuidance").property("text") == f"Guidance: {prompt.style}"
            assert window.find("studioDuration").property("text").startswith("About ")
            assert studio.labelChips == ["Reference", "Reference Candidate"]
            chips = window.find("studioLabelChips")
            # Both chips show in a header the inspector leaves wide enough (D-058's layout).
            if window.find("studioScreen").property("inspectorOn"):
                studio.toggleInspector()
            qtbot.waitUntil(lambda: chips.property("shown") == 2, timeout=5000)
            assert chips.property("spokenLabel") == "Labels: Reference, Reference Candidate"
            assert studio.takesText == "No takes of this prompt yet"
            assert window.find("studioDevice").property("text") == "UMIK-1"
            assert studio.statusText == "Ready to record"
            # Corpus-first: no session, group or package anywhere on the screen.
            texts = " ".join(visible_texts(window.find("studioScreen"))).lower()
            assert "a quiet morning" not in texts  # prompt 0 is recorded; prompt 1 is shown
            assert prompt.text is not None and prompt.text.lower()[:20] in texts
            for word in ("session", "group", "package", "finish"):
                assert word not in texts, word
    finally:
        model.shutdown()


def test_continue_missing_walks_the_catalog_in_order(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    kept_elsewhere(pc, tmp_path, (0, 2, 3))
    ids = prompt_ids(pc)
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio = model.studio
            qtbot.waitUntil(lambda: studio.coverageText == "3 / 34", timeout=5000)
            assert studio.promptId == ids[1]
            # Filters never change where it goes: hide everything but one label.
            studio.setShow("missing")
            studio.setSearch("technical")
            click(window, "studioContinueMissing")
            assert studio.promptId == ids[4]  # 2 and 3 are recorded
            click(window, "studioContinueMissing")
            assert studio.promptId == ids[5]
            # From the last prompt it wraps to the first missing one.
            studio.setShow("all")
            studio.setSearch("")
            studio.selectPrompt(ids[-1])
            assert studio.promptId == ids[-1]
            click(window, "studioContinueMissing")
            assert studio.promptId == ids[1]
    finally:
        model.shutdown()


def test_the_filters_and_the_search(qtbot: Any, studio_window: Window, pc: PC) -> None:
    studio = studio_window.model.studio
    ids = prompt_ids(pc)
    assert studio.rowCount == 34 and studio.labelsTitle == "Labels: Any"
    tokens = [option["token"] for option in studio.labelOptions]
    assert tokens[:2] == ["reference", "reference-candidate"] and "technical" in tokens
    # Labels: OR among themselves.
    studio.setLabel("reference-candidate", True)
    assert studio.labelsTitle == "Labels: Reference Candidate"
    assert rows(studio_window) == ids[:4]
    studio.setLabel("technical", True)
    assert studio.labelsTitle == "Labels: Reference Candidate, Technical"
    assert len(rows(studio_window)) == 9
    # Search: AND with the labels; case-insensitive over the title, id, text and guidance.
    search = studio_window.find("studioSearch")
    search.setProperty("text", "QUANTIZATION")
    search.textEdited.emit()
    assert rows(studio_window) == ["technical-04"]
    # The current prompt is outside the filter: it stays reachable, pinned above the list.
    assert studio.promptId == ids[0]
    assert studio.currentOutside and studio.currentRow["promptId"] == ids[0]
    assert studio_window.find("studioCurrentOutside").property("visible")
    search.setProperty("text", "kettle")
    search.textEdited.emit()
    assert studio.rowCount == 0
    assert studio_window.find("studioLibraryEmpty").property("visible")
    studio.showAllLabels()
    assert studio.labelsTitle == "Labels: Any" and studio.rowCount == 0
    search.setProperty("text", "")
    search.textEdited.emit()
    assert studio.rowCount == 34
    # Show: Missing, Needs attention (a draft), All.
    studio.selectPrompt(ids[3])
    assert studio.promptId == ids[3] and not studio.currentOutside
    studio.setShow("attention")
    assert studio.rowCount == 0
    assert studio.currentOutside and studio.currentRow["promptId"] == ids[3]
    assert studio_window.find("studioCurrentOutside").property("visible")
    studio.setShow("missing")
    assert studio.rowCount == 34 and not studio.currentOutside
    studio.setShow("nonsense")
    assert studio.show == "missing"
    # The Labels menu: a toggle per label, and Show all labels.
    click(studio_window, "studioLabels")
    popup = studio_window.find("studioLabelsPopup")
    qtbot.waitUntil(lambda: popup.property("visible"), timeout=5000)
    box = studio_window.items("studioLabel-technical")[0]
    box.setProperty("checked", True)
    box.toggled.emit()
    assert studio.labelsTitle == "Labels: Technical" and studio.rowCount == 5
    studio_window.items("studioAllLabels")[0].clicked.emit()
    assert studio.labelsTitle == "Labels: Any" and studio.rowCount == 34


def test_record_stop_save_keeps_a_local_take(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    model = studio_window.model
    studio = model.studio
    ids = prompt_ids(pc)
    assert studio.promptId == ids[0] and studio.coverageText == "0 / 34"
    assert not studio.canSave and not studio.canSaveNext and not studio.canRetake
    click(studio_window, "studioRecord")
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    assert studio.phase == "starting" and studio.elapsedText == "00:00.00"
    # The sidebar entry shows a take is being recorded; the library waits.
    assert [e["status"] for e in model.sidebar if e["key"] == "prompts"] == ["held"]
    assert not studio.canNavigate and not studio.canContinueMissing
    studio.selectPrompt(ids[5])
    assert studio.promptId == ids[0]
    recorder.last.advance(48_000 + 24_000)
    wait_phase(qtbot, studio, "recording")
    qtbot.waitUntil(lambda: studio.elapsedText == "00:01.50", timeout=5000)
    assert studio_window.find("studioElapsed").property("text") == "00:01.50"
    assert studio.statusText == "Recording"
    click(studio_window, "studioRecord")
    wait_phase(qtbot, studio, "review")
    assert recorder.last.stops == 1
    assert studio.canSave and studio.canSaveNext and studio.canRetake
    # A good take is never discarded: Save, Retake or leaving keeps it.
    assert not studio.canDiscard and not studio_window.find("studioDiscard").property("enabled")
    assert studio.client.takes() == []  # nothing is saved before Save
    click(studio_window, "studioSave")
    wait_phase(qtbot, studio, "ready")
    # Durable here at once: the take, kept, of this prompt of the pinned version.
    takes = studio.client.takes()
    assert len(takes) == 1
    take = takes[0]
    assert take.prompt_id == ids[0] and take.sync.decision.decision == "kept"
    assert take.record.catalog == pc.catalog[0]
    assert take.record.simulated and take.sync.upload == "local-only"
    assert take.audio.is_file()
    # The prompt's status and the coverage count it; Save stays on the prompt.
    assert studio.promptId == ids[0]
    assert studio.statusText == "Saved · kept on this machine"
    assert studio.promptStatus == "Recorded · this machine"
    assert studio.coverageText == "1 / 34"
    assert studio.rowsModel.rows[0]["statusTitle"] == "Recorded · this machine"
    assert studio.takesText == "1 take of this prompt"
    assert studio.syncText.endswith("1 take here")
    assert [e["status"] for e in model.sidebar if e["key"] == "prompts"] == ["none"]


def test_save_and_next_goes_to_the_next_missing_prompt(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio = studio_window.model.studio
    ids = prompt_ids(pc)
    assert studio.promptId == ids[0]
    take_one(qtbot, studio_window, recorder)
    click(studio_window, "studioSaveNext")
    qtbot.waitUntil(lambda: studio.promptId == ids[1], timeout=5000)
    assert studio.phase == "ready" and studio.coverageText == "1 / 34"
    assert studio.statusText == "Saved · kept on this machine"
    # From the last prompt it wraps: the next missing one is the second.
    studio.selectPrompt(ids[-1])
    take_one(qtbot, studio_window, recorder)
    studio.saveNext()  # what Return will call (6c)
    qtbot.waitUntil(lambda: studio.promptId == ids[1], timeout=5000)
    assert studio.coverageText == "2 / 34"
    assert [t.prompt_id for t in studio.client.takes()] == [ids[0], ids[-1]]
    # A second Save while one is saving does nothing (no second take, no skipped prompt).
    take_one(qtbot, studio_window, recorder)
    studio.saveNext()
    studio.saveNext()
    studio.save()
    qtbot.waitUntil(lambda: studio.promptId == ids[2], timeout=5000)
    assert len(studio.client.takes()) == 3 and studio.coverageText == "3 / 34"


def test_retake_sets_the_earlier_take_aside_and_discard_drops_a_failed_one(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio = studio_window.model.studio
    ids = prompt_ids(pc)
    take_one(qtbot, studio_window, recorder)
    click(studio_window, "studioRetake")
    qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
    # The take it replaces is saved and set aside (D-058, item 2): never lost, not coverage,
    # and no reason to type.
    qtbot.waitUntil(
        lambda: [t.sync.decision.decision for t in studio.client.takes()] == ["rejected"],
        timeout=5000,
    )
    assert studio.client.takes()[0].sync.decision.reason == "superseded by retake"
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()  # what R will call (6c)
    wait_phase(qtbot, studio, "review")
    click(studio_window, "studioSave")
    wait_phase(qtbot, studio, "ready")
    decisions = sorted(t.sync.decision.decision for t in studio.client.takes())
    assert decisions == ["kept", "rejected"] and studio.coverageText == "1 / 34"
    # A draft alone is Needs attention, not coverage; leaving it for another prompt saves it.
    studio.selectPrompt(ids[1])
    take_one(qtbot, studio_window, recorder)
    studio.selectPrompt(ids[1])  # the selected prompt again: the take stays under review
    assert studio.phase == "review" and len(studio.client.takes()) == 2
    studio.selectPrompt(ids[2])
    qtbot.waitUntil(lambda: studio.rowsModel.rows[1]["statusTitle"] == "Draft", timeout=5000)
    assert len(studio.client.takes()) == 3
    studio.setShow("attention")
    assert rows(studio_window) == [ids[1]]
    studio.setShow("all")
    # A failed take: Discard removes it, and nothing of it is kept.
    recorder.fail_next = "deviation of 4800 frames at block 3"
    click(studio_window, "studioRecord")
    qtbot.waitUntil(lambda: len(recorder.takes) == 4, timeout=5000)
    click(studio_window, "studioRecord")
    wait_phase(qtbot, studio, "failed")
    assert studio.statusText == "The take failed · Retake or Discard"
    # Its cause, whole, in the take's place.
    assert studio.problemText == "The take failed: deviation of 4800 frames at block 3"
    assert studio_window.find("studioProblem").property("text") == studio.problemText
    assert studio_window.find("studioProblemFlick").property("visible")
    assert studio.statusTone == "danger" and studio.canDiscard and not studio.canSave
    recording = studio.client.spool.recording_root
    assert len(list(recording.iterdir())) == 2  # its directory and its rec-….json
    click(studio_window, "studioDiscard")
    wait_phase(qtbot, studio, "ready")
    assert list(recording.iterdir()) == [] and len(studio.client.takes()) == 3
    assert studio.statusText == "Take discarded"
    # A take that does not start leaves nothing behind.
    recorder.start_error = "the device is busy"
    click(studio_window, "studioRecord")
    qtbot.waitUntil(lambda: studio.statusText.startswith("The take did not start"), timeout=5000)
    assert studio.phase == "ready" and list(recording.iterdir()) == []


def test_a_good_take_is_never_discarded(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    """Discard is for a failed take only. A good take, stopped, is kept: set aside by Retake
    (rejected, superseded, D-058), a draft by leaving it, kept by Save; Remove sets a saved one
    aside afterwards."""
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    take_one(qtbot, studio_window, recorder)
    assert not studio.canDiscard and not studio_window.find("studioDiscard").property("enabled")
    studio.discard()  # what a stray call would do: nothing
    assert studio.phase == "review" and takes.selectedKey == "review"
    assert list(studio.client.spool.recording_root.iterdir())  # still there
    click(studio_window, "studioRetake")
    qtbot.waitUntil(
        lambda: [t.sync.decision.decision for t in studio.client.takes()] == ["rejected"],
        timeout=5000,
    )
    # The new take has started (its worker starts off the GUI thread): only now is it last.
    qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    assert not studio.canDiscard
    click(studio_window, "studioSave")
    wait_phase(qtbot, studio, "ready")
    # Afterwards: Remove sets the kept take aside (and Restore brings it back).
    (kept,) = [t for t in studio.client.takes() if t.sync.decision.decision == "kept"]
    takes.selectTake(kept.take_id)
    qtbot.waitUntil(lambda: takes.canRemove, timeout=5000)
    click(studio_window, "studioRemoveTake")
    assert studio.client.spool.take(kept.take_id).sync.decision.decision == "removed"
    assert len(studio.client.takes()) == 2


REGIONS = (
    "studioLibrary",
    "studioCoverage",
    "studioContinueMissing",
    "studioSearch",
    "studioShow",
    "studioLabels",
    "studioCatalog",
    "studioLibraryList",
    "studioHeader",
    "studioPromptTitle",
    "studioLabelChips",
    "studioDevice",
    "studioScriptCard",
    "studioPromptSection",
    "studioInstructionsSection",
    "studioTakePicker",
    "studioWaveform",
    "studioTrimRow",
    "studioTimer",
    "studioPlaybackRow",
    "studioFooter",
    "studioRecord",
    "studioRetake",
    "studioDiscard",
    "studioSave",
    "studioSaveNext",
    "studioStatus",
    "studioStatusText",
    "studioSyncText",
)


def _regions(
    window: Window, names: tuple[str, ...]
) -> dict[str, tuple[float, float, float, float]]:
    found = {}
    for name in names:
        item = window.find(name)
        corner = item.mapToScene(QPointF(0, 0))
        found[name] = (corner.x(), corner.y(), item.width(), item.height())
    return found


def drawn(qtbot: Any, window: Window) -> None:
    """Wait until the window has laid out and drawn a new frame (and fail unless it did)."""
    root = window.root
    with qtbot.waitSignal(root.frameSwapped, timeout=5000) as frame:
        root.update()
    assert frame.signal_triggered


def geometry(
    qtbot: Any, window: Window, names: tuple[str, ...] = REGIONS
) -> dict[str, tuple[float, float, float, float]]:
    """Every region's place and size in the scene, read once the window has laid out: after
    a frame is drawn, and again after the next one, the two the same (the layout settled)."""
    drawn(qtbot, window)
    first = _regions(window, names)
    drawn(qtbot, window)
    found = _regions(window, names)
    assert found == first, "the layout was still moving"
    return found


def with_details(qtbot: Any, window: Window, idle: dict[str, Any]) -> dict[str, Any]:
    """Make the sync badge show a notice (its word changes). Every region stays as in
    ``idle``; only the badge's width follows its word, in place: its right edge and its height
    stay."""
    model = window.model.studio
    assert model.syncBadge == "Uploads off"
    model.client.spool._notice(
        model.client.takes()[0], "changed-elsewhere", "Changed elsewhere: the PC's state."
    )
    model.client.sync_now()
    qtbot.waitUntil(lambda: model.syncBadge == "A notice", timeout=5000)
    found = geometry(qtbot, window)
    sync, before = found["studioSyncText"], idle["studioSyncText"]
    line, was = found["studioStatusText"], idle["studioStatusText"]
    assert {**found, "studioSyncText": before, "studioStatusText": was} == idle
    assert (sync[1], sync[3]) == (before[1], before[3])
    assert sync[0] + sync[2] == before[0] + before[2]
    # The take's line keeps its place and takes the room the badge leaves it.
    assert (line[0], line[1], line[3]) == (was[0], was[1], was[3])
    assert line[0] + line[2] <= sync[0]
    return found


def test_transport_never_moves_the_layout(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio = studio_window.model.studio
    resize(qtbot, studio_window, 1440, 980)
    idle = geometry(qtbot, studio_window)
    # Regions stack without overlap, at their final size. With the inspector (D-058's layout),
    # the take's picker and trim row are its own.
    inspector = studio_window.find("studioScreen").property("inspectorOn")
    order = [
        "studioHeader",
        "studioScriptCard",
        *(() if inspector else ("studioTakePicker",)),
        "studioWaveform",
        *(() if inspector else ("studioTrimRow",)),
        "studioTimer",
        "studioPlaybackRow",
        "studioFooter",
        "studioStatus",
    ]
    for upper, lower in zip(order, order[1:], strict=False):
        assert idle[upper][1] + idle[upper][3] <= idle[lower][1], (upper, lower)
    if inspector:
        pane = scene_rect(studio_window.find("studioInspector"))
        for name in ("studioTakePicker", "studioTrimRow"):
            assert idle[name][0] >= pane[0], name
    assert idle["studioFooter"][3] == 56 and idle["studioRecord"][2:] == (56, 56)
    assert idle["studioWaveform"][3] >= 120 and idle["studioLibrary"][2] == 280
    seen = {"idle": idle}

    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    seen["starting"] = geometry(qtbot, studio_window)
    recorder.last.advance(9_600)
    wait_phase(qtbot, studio, "recording")
    seen["recording"] = geometry(qtbot, studio_window)
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    seen["review"] = geometry(qtbot, studio_window)
    studio.save()
    seen["saving"] = geometry(qtbot, studio_window)
    wait_phase(qtbot, studio, "ready")
    seen["saved"] = geometry(qtbot, studio_window)
    for state, found in seen.items():
        assert found == idle, state
    # The sync line with a notice: Details appears beside it, and no region moves.
    noted = with_details(qtbot, studio_window, idle)
    seen = {}
    recorder.fail_next = "a lost buffer"
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    seen["recording"] = geometry(qtbot, studio_window)
    studio.toggleRecord()
    wait_phase(qtbot, studio, "failed")
    seen["failed"] = geometry(qtbot, studio_window)
    for state, found in seen.items():
        assert found == noted, f"{state}, with Details"

    # Only the window's size moves it: taller gives the script and the waveform more room.
    resize(qtbot, studio_window, 1440, 1180)
    taller = geometry(qtbot, studio_window)
    assert taller["studioScriptCard"][3] == pytest.approx(idle["studioScriptCard"][3] + 140)
    assert taller["studioWaveform"][3] == pytest.approx(idle["studioWaveform"][3] + 60)
    assert taller["studioFooter"][3] == 56


def test_the_mode_switch(qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC) -> None:
    from voice_lab_desktop.config import Config, ConfigError

    with pytest.raises(ConfigError, match="prompts_mode"):
        Config(prompts_mode="booth").validated()
    model = studio_app(data_dir, tmp_path, pc, StudioRecorder(), prompts_mode="sessions")
    try:
        assert model.studio is None and model.property("promptsMode") == "sessions"
        assert model.models["prompts"] is model.prompts
        with open_studio(qtbot, model, tmp_path) as window:
            qtbot.waitExposed(window.root)
            assert window.find("promptsScreen").property("visible")
            assert not window.find_all("studioScreen")
    finally:
        model.shutdown()


def test_without_a_microphone_nothing_records(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC
) -> None:
    recorder = StudioRecorder(refused="no UMIK-1 among the inputs")
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            qtbot.waitExposed(window.root)
            studio = model.studio
            wait_phase(qtbot, studio, "ready")
            qtbot.waitUntil(lambda: studio.deviceLabel != "", timeout=10000)
            assert not studio.deviceOk and not studio.canRecord
            assert window.find("studioDevice").property("text") == "No microphone"
            assert studio.statusText == "No microphone · click it to look again"
            assert studio.problemText == "No microphone: no UMIK-1 among the inputs"
            assert not window.find("studioRecord").property("enabled")
            studio.toggleRecord()
            assert recorder.takes == []
            # Plugged in: a click on the microphone looks again.
            recorder.refused = None
            click(window, "studioDevice")
            qtbot.waitUntil(lambda: studio.deviceOk and studio.canRecord, timeout=10000)
            assert studio.statusText == "Ready to record" and studio.problemText == ""
    finally:
        model.shutdown()


def resize(qtbot: Any, window: Window, width: int, height: int) -> None:
    """The window at ``width`` × ``height``, laid out and drawn at that size."""
    root = window.root
    root.resize(width, height)
    qtbot.waitUntil(lambda: (root.width(), root.height()) == (width, height), timeout=5000)
    drawn(qtbot, window)


def scene_rect(item: Any) -> tuple[float, float, float, float]:
    corner = item.mapToScene(QPointF(0, 0))
    return corner.x(), corner.y(), item.width(), item.height()


def assert_prompt_whole(window: Window, text: str) -> int:
    """The prompt is all there: its laid-out height fits its section, inside the card, and it
    does not scroll. Returns the reading size it is set at."""
    card, flick = window.find("studioScriptCard"), window.find("studioScriptFlick")
    script = window.find("studioScript")
    assert script.property("text") == text
    assert script.property("implicitHeight") <= flick.height()
    assert not flick.property("interactive") and flick.property("contentY") == 0
    cx, cy, cw, ch = scene_rect(card)
    fx, fy, fw, fh = scene_rect(flick)
    assert cx <= fx and fx + fw <= cx + cw and cy <= fy and fy + fh <= cy + ch
    size = int(card.property("readingSize"))
    assert 16 <= size <= 32
    return size


@pytest.mark.parametrize("chosen", [24, 28, 32])
@pytest.mark.parametrize("size", [(960, 600), (1440, 900)])
def test_the_longest_prompt_is_shown_whole(
    qtbot: Any,
    studio_window: Window,
    recorder: StudioRecorder,
    pc: PC,
    size: tuple[int, int],
    chosen: int,
) -> None:
    """At each reading size and window size: the longest prompt whole, at the chosen size or
    the largest smaller one that fits (16 at least), the same while recording and in review,
    with the sync line's Details shown or not, and no region moved."""
    studio = studio_window.model.studio
    longest = max(pc.catalog[1].suite.prompts, key=lambda prompt: len(prompt.text or ""))
    assert longest.text is not None and len(longest.text) >= 140
    studio.selectPrompt(longest.prompt_id)
    studio.setReadingSize(chosen)
    resize(qtbot, studio_window, *size)
    idle = geometry(qtbot, studio_window)
    reading = assert_prompt_whole(studio_window, longest.text)
    assert reading <= chosen
    if size == (1440, 900) and chosen == 24:
        assert reading == 24  # the default, untouched on a roomy window
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    assert geometry(qtbot, studio_window) == idle
    assert assert_prompt_whole(studio_window, longest.text) == reading
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    assert geometry(qtbot, studio_window) == idle
    assert assert_prompt_whole(studio_window, longest.text) == reading
    studio.save()
    wait_phase(qtbot, studio, "ready")
    # A notice: Details shows on the status line; the prompt and every region stay.
    assert geometry(qtbot, studio_window) == idle
    with_details(qtbot, studio_window, idle)
    assert assert_prompt_whole(studio_window, longest.text) == reading


def test_nothing_is_cut_off_on_the_smallest_window(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    run_catalog = pytest.importorskip("corpus_take_fixtures").run_catalog
    from cole_voice_lab.models import COLE_SUBJECT

    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio = model.studio
            resize(qtbot, window, 960, 600)
            lines = (
                "studioLabels",
                "studioCatalog",
                "studioSwitchNotice",
                "studioCoverageSource",
                "studioStatusText",
            )

            def whole() -> None:
                for name in lines:
                    item = window.find(name)
                    if name == "studioLabels":  # a button: its label
                        item = item.property("contentItem")
                    assert item.property("truncated") is False, (name, item.property("text"))

            studio.setLabel("reference-candidate", True)
            # Sending held until S-1, with a take here.
            take_one(qtbot, window, recorder)
            studio.save()
            wait_phase(qtbot, studio, "ready")
            assert window.find("studioSyncText").property("text").startswith("Uploads off")
            whole()
            # A failed take names its cause.
            recorder.fail_next = (
                "deviation of 4800 frames at block 3, beyond the 64-frame tolerance"
            )
            studio.toggleRecord()
            qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
            studio.toggleRecord()
            wait_phase(qtbot, studio, "failed")
            assert studio.problemText.endswith("beyond the 64-frame tolerance")
            problem = window.find("studioProblem")
            assert problem.property("text") == studio.problemText
            assert not problem.property("truncated")
            assert problem.property("implicitHeight") <= window.find("studioProblemFlick").height()
            whole()
            studio.discard()
            # A new version on the PC: the switch notice and Switch now.
            run_catalog(pc.layout, "add-prompt", "warm-05", "--text", "The kettle clicked off.")
            run_catalog(pc.layout, "publish")
            run_catalog(pc.layout, "activate", "--subject-id", COLE_SUBJECT, "--version", "2")
            studio.syncNow()
            qtbot.waitUntil(lambda: studio.canSwitch, timeout=10000)
            qtbot.waitUntil(lambda: "version 2" in studio.coverageSource, timeout=10000)
            assert window.find("studioSwitch").property("visible")
            assert window.find("studioSwitchNotice").property("text").startswith("Version 2")
            whole()
    finally:
        model.shutdown()

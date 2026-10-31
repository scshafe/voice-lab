"""Redo (D-058; the recording UX review's item 2): R in review records the prompt again and sets
the attempt under review aside (rejected, "superseded by retake") with no dialog and no
typing; ⌘Z reverses the last keep or retake (the take is a draft again, on its prompt,
selected); the picker lists set-aside attempts under the others, like a take folder."""

from __future__ import annotations

from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder

from .conftest import wait_phase
from .test_studio_core_loop import open_popups
from .test_studio_menus import actions
from .test_studio_screen import prompt_ids

SUPERSEDED = "superseded by retake"


def key(window: Window, which: Qt.Key, modifier: Qt.KeyboardModifier | None = None) -> None:
    QTest.keyClick(window.root, which, modifier or Qt.KeyboardModifier.NoModifier)


def take(qtbot: Any, window: Window, recorder: StudioRecorder) -> None:
    """R, and R again once it records: a take under review."""
    studio = window.model.studio
    count = len(recorder.takes)
    key(window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    key(window, Qt.Key.Key_R)
    wait_phase(qtbot, studio, "review")


def redo_then_keep(qtbot: Any, window: Window, recorder: StudioRecorder) -> tuple[str, str]:
    """R, R, R, R, Return on the selected prompt: the set-aside take's id and the kept one's."""
    studio = window.model.studio
    prompt = studio.promptId
    take(qtbot, window, recorder)
    take(qtbot, window, recorder)  # R in review: redo
    assert open_popups(window) == []  # no reason to type, no dialog
    key(window, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: studio.promptId != prompt, timeout=5000)
    qtbot.waitUntil(lambda: len(studio.client.takes()) == 2, timeout=5000)
    mine = {t.sync.decision.decision: t for t in studio.client.takes()}
    assert set(mine) == {"kept", "rejected"}
    assert mine["rejected"].sync.decision.reason == SUPERSEDED
    return mine["rejected"].take_id, mine["kept"].take_id


def test_r_in_review_records_again_and_sets_the_attempt_aside(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    ids = prompt_ids(pc)
    aside, kept = redo_then_keep(qtbot, studio_window, recorder)
    # The prompt is recorded: not in Needs attention, its set-aside attempt under the kept one.
    rows = {row["promptId"]: row for row in studio.rowsModel.rows}
    assert rows[ids[0]]["statusTitle"].startswith("Recorded")
    studio.setShow("attention")
    assert ids[0] not in [row["promptId"] for row in studio.rowsModel.rows]
    studio.setShow("all")
    studio.selectPrompt(ids[0])
    qtbot.waitUntil(lambda: [row["key"] for row in takes.history] == [kept, aside], timeout=5000)
    assert [row["superseded"] for row in takes.history] == [False, True]
    assert takes.history[1]["status"] == "Rejected (superseded by retake)"
    assert studio.pacing.redone == 1


def test_command_z_undoes_the_keep_then_the_retake(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    ids = prompt_ids(pc)
    items = actions(studio_window)
    assert not studio.canUndo and not items["undo"].isEnabled()
    aside, kept = redo_then_keep(qtbot, studio_window, recorder)
    assert studio.promptId == ids[1]
    assert studio.undoText == "Undo Save" and items["undo"].text() == "Undo Save"
    # ⌘Z: the kept take is a draft again, back on its prompt, selected.
    key(studio_window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: studio.promptId == ids[0], timeout=5000)
    assert studio.client.spool.take(kept).sync.decision.decision == "draft"
    qtbot.waitUntil(lambda: takes.selectedKey == kept, timeout=5000)
    assert studio.statusText == "Undone: the take kept is a draft again"
    assert studio.undoText == "Undo Retake"
    # Again, from the Edit menu: the attempt the retake set aside is a draft again.
    items["undo"].trigger()
    qtbot.waitUntil(
        lambda: studio.client.spool.take(aside).sync.decision.decision == "draft", timeout=5000
    )
    qtbot.waitUntil(lambda: takes.selectedKey == aside, timeout=5000)
    assert studio.statusText == "Undone: the take set aside is a draft again"
    assert not studio.canUndo and not items["undo"].isEnabled()
    # Save keeps the draft selected, as before.
    studio.save()
    qtbot.waitUntil(
        lambda: studio.client.spool.take(aside).sync.decision.decision == "kept", timeout=5000
    )


def test_command_z_while_a_take_is_under_review_leaves_it_there(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    take(qtbot, studio_window, recorder)
    take(qtbot, studio_window, recorder)  # the first set aside; the second under review
    qtbot.waitUntil(lambda: studio.undoText == "Undo Retake", timeout=5000)
    (aside,) = [t.take_id for t in studio.client.takes()]
    studio.undo()
    qtbot.waitUntil(
        lambda: studio.client.spool.take(aside).sync.decision.decision == "draft", timeout=5000
    )
    assert studio.phase == "review" and takes.selectedKey == "review"


def test_leaving_a_take_under_review_still_keeps_it_as_a_draft(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio = studio_window.model.studio
    take(qtbot, studio_window, recorder)
    studio.selectPrompt(prompt_ids(pc)[3])
    qtbot.waitUntil(lambda: len(studio.client.takes()) == 1, timeout=5000)
    assert studio.client.takes()[0].sync.decision.decision == "draft"
    assert not studio.canUndo  # leaving is not a retake: nothing to undo

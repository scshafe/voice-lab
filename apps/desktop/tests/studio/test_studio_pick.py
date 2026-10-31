"""Keep the better take, not only the newest (D-058; the recording UX review's item 8): keys 1–9
audition this prompt's takes, newest first; Return (or ⌘S) keeps the selected one, the take
under review, a draft or an attempt a retake set aside, and the prompt's other undecided
attempts are set aside as a retake would; the picker marks the take Return keeps."""

from __future__ import annotations

from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import Qt
from studio_fakes import StudioRecorder

from .test_studio_redo import SUPERSEDED, key, take
from .test_studio_screen import prompt_ids


def decisions(window: Window) -> dict[str, tuple[str, str | None]]:
    return {
        t.take_id: (t.sync.decision.decision, t.sync.decision.reason)
        for t in window.model.studio.client.takes()
    }


def three_attempts(qtbot: Any, window: Window, recorder: StudioRecorder) -> list[str]:
    """R, R three times on the selected prompt (each R in review a redo): the first two set
    aside, the third under review. The history's keys, newest first."""
    for _ in range(3):
        take(qtbot, window, recorder)
    takes = window.model.studio.takes
    qtbot.waitUntil(lambda: len(takes.history) == 3, timeout=5000)
    assert [row["number"] for row in takes.history] == [1, 2, 3]
    return [row["key"] for row in takes.history]


def test_keys_audition_this_prompts_takes_newest_first(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    takes = studio_window.model.studio.takes
    player = studio_window.model.player
    review, second, first = three_attempts(qtbot, studio_window, recorder)
    key(studio_window, Qt.Key.Key_3)
    qtbot.waitUntil(lambda: takes.selectedKey == first, timeout=5000)
    qtbot.waitUntil(
        lambda: str(player.property("activeKey")).startswith(f"studio:{first}:"), timeout=5000
    )
    key(studio_window, Qt.Key.Key_1)
    qtbot.waitUntil(lambda: takes.selectedKey == review, timeout=5000)
    qtbot.waitUntil(
        lambda: str(player.property("activeKey")).startswith("studio:review:"), timeout=5000
    )
    key(studio_window, Qt.Key.Key_9)  # no ninth take: nothing changes
    assert takes.selectedKey == review
    assert [row["willKeep"] for row in takes.history] == [True, False, False]


def test_return_keeps_the_picked_take_and_sets_the_others_aside(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    ids = prompt_ids(pc)
    review, second, first = three_attempts(qtbot, studio_window, recorder)
    key(studio_window, Qt.Key.Key_2)
    qtbot.waitUntil(lambda: takes.selectedKey == second, timeout=5000)
    qtbot.waitUntil(lambda: takes.history[1]["willKeep"], timeout=5000)
    assert studio.canSaveNext
    key(studio_window, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: studio.promptId == ids[1], timeout=5000)
    qtbot.waitUntil(lambda: len(decisions(studio_window)) == 3, timeout=5000)
    qtbot.waitUntil(
        lambda: (
            sorted(decisions(studio_window).values(), key=str)
            == sorted([("kept", None), ("rejected", SUPERSEDED), ("rejected", SUPERSEDED)], key=str)
        ),
        timeout=5000,
    )
    assert decisions(studio_window)[second] == ("kept", None)
    assert studio.coverageText == f"1 / {len(ids)}"  # the prompt counts once
    # ⌘Z: the take set aside last (the one under review) is a draft again; ⌘Z again: the
    # kept attempt is set aside again, as it was.
    while studio.canUndo and decisions(studio_window)[second][0] == "kept":
        key(studio_window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        qtbot.waitUntil(lambda: studio.phase == "ready", timeout=5000)
    assert decisions(studio_window)[second] == ("rejected", SUPERSEDED)


def test_save_keeps_a_draft_and_sets_the_prompts_other_drafts_aside(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    ids = prompt_ids(pc)
    # Two drafts of one prompt: each left by going to another prompt.
    for _ in range(2):
        studio.selectPrompt(ids[4])
        qtbot.waitUntil(lambda: studio.promptId == ids[4], timeout=5000)
        take(qtbot, studio_window, recorder)
        studio.selectPrompt(ids[5])
        qtbot.waitUntil(lambda: studio.promptId == ids[5], timeout=5000)
    qtbot.waitUntil(lambda: len(decisions(studio_window)) == 2, timeout=5000)
    studio.selectPrompt(ids[4])
    qtbot.waitUntil(lambda: len(takes.history) == 2, timeout=5000)
    older = takes.history[1]["key"]
    key(studio_window, Qt.Key.Key_2)
    qtbot.waitUntil(lambda: takes.selectedKey == older, timeout=5000)
    studio.save()
    qtbot.waitUntil(lambda: decisions(studio_window)[older] == ("kept", None), timeout=5000)
    other = next(k for k in decisions(studio_window) if k != older)
    assert decisions(studio_window)[other] == ("rejected", SUPERSEDED)

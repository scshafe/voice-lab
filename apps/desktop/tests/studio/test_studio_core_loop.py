"""The core loop (D-058): open the prompts, R to record, R or Space to stop, Return to save and
go to the next prompt, and again, rapid fire.

The owner's acceptance test: the loop gets faster, never slower. Through it no dialog, menu
or popup opens, the keyboard focus never moves, the keys work the moment the next prompt shows,
and a Return pressed while the take is checked (just before it seals) is kept and done when the
take is ready, never dropped. Every change to the studio runs it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import shiboken6
from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder

from .conftest import wait_phase
from .test_studio_screen import prompt_ids

#: Prompts recorded in a row.
TAKES = 6
QUEUED = "Stopping · will save when ready"


def key(window: Window, which: Qt.Key, modifier: Qt.KeyboardModifier | None = None) -> None:
    QTest.keyClick(window.root, which, modifier or Qt.KeyboardModifier.NoModifier)


def open_popups(window: Window) -> list[str]:
    """Every dialog, menu or popup of the window that is open."""
    return [
        obj.objectName() or obj.metaObject().className()
        for obj in window.root.findChildren(QObject)
        if obj.inherits("QQuickPopup") and obj.property("visible")
    ]


def focus(window: Window) -> int | None:
    """The item with the keyboard focus (its address: the same item, the same number)."""
    item = window.root.activeFocusItem()
    return None if item is None else int(shiboken6.getCppPointer(item)[0])


def steady(window: Window, focused: int | None) -> Callable[[], None]:
    """A check that the loop changed nothing around it: no popup, the same focus, the keys
    on."""
    screen = window.find("studioScreen")

    def check() -> None:
        assert open_popups(window) == []
        assert focus(window) == focused
        assert screen.property("keysEnabled") and not screen.property("menuOpen")

    return check


def watch_prompts(window: Window) -> list[tuple[str, bool, bool]]:
    """Each change of the selected prompt as it happened: (prompt, R would record, the keys
    are on), read in the change itself, before anything else runs."""
    studio, screen = window.model.studio, window.find("studioScreen")
    seen: list[tuple[str, bool, bool]] = []
    studio.promptChanged.connect(
        lambda: seen.append(
            (studio.promptId, bool(studio.canRecord), bool(screen.property("keysEnabled")))
        )
    )
    return seen


def test_the_core_loop_rapid_fire(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, screen = studio_window.model.studio, studio_window.find("studioScreen")
    ids = prompt_ids(pc)
    recorder.hold_seal = True  # each take is checked until the test says it is done
    # Open the prompts: the keys are on at once.
    studio_window.model.navigate("status")
    studio_window.model.navigate("prompts")
    assert screen.property("keysEnabled") and studio.canRecord
    check = steady(studio_window, focus(studio_window))
    seen = watch_prompts(studio_window)
    for index in range(TAKES):
        assert studio.promptId == ids[index]
        check()
        key(studio_window, Qt.Key.Key_R)
        qtbot.waitUntil(lambda index=index: len(recorder.takes) == index + 1, timeout=5000)
        recorder.last.advance(4_800)
        wait_phase(qtbot, studio, "recording")
        check()
        # Stop with R, then with Space, in turn.
        key(studio_window, Qt.Key.Key_R if index % 2 == 0 else Qt.Key.Key_Space)
        assert studio.phase == "stopping"
        typed_ahead = index % 3 != 2
        if typed_ahead:
            # Return before the take seals: kept, and said so.
            key(studio_window, Qt.Key.Key_Return)
            assert studio.statusText == QUEUED and studio.phase == "stopping"
            check()
            recorder.seal()
        else:
            recorder.seal()
            wait_phase(qtbot, studio, "review")
            check()
            key(studio_window, Qt.Key.Key_Return)
        following = ids[index + 1]
        qtbot.waitUntil(lambda following=following: studio.promptId == following, timeout=5000)
        # The moment the next prompt showed, R would already record and the keys were on.
        first = next(entry for entry in seen if entry[0] == following)
        assert first == (following, True, True)
        assert studio.phase == "ready"
        check()
    # Each prompt has its one take, kept.
    kept = {take.prompt_id: take.sync.decision.decision for take in studio.client.takes()}
    assert kept == dict.fromkeys(ids[:TAKES], "kept")
    assert studio.coverageText == f"{TAKES} / {len(ids)}"


def test_a_save_typed_ahead_stays_and_a_failed_take_drops_it(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio = studio_window.model.studio
    ids = prompt_ids(pc)
    recorder.hold_seal = True
    # ⌘S (Ctrl+S here) typed ahead saves and stays on the prompt.
    key(studio_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    key(studio_window, Qt.Key.Key_R)
    key(studio_window, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert studio.statusText == QUEUED
    recorder.seal()
    qtbot.waitUntil(
        lambda: studio.phase == "ready" and studio.coverageText == f"1 / {len(ids)}",
        timeout=5000,
    )
    assert studio.promptId == ids[0]
    # A take that fails drops the Return typed ahead: it says why, and nothing moves.
    recorder.fail_next = "a continuity break"
    key(studio_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    key(studio_window, Qt.Key.Key_Space)
    key(studio_window, Qt.Key.Key_Return)
    assert studio.statusText == QUEUED
    recorder.seal()
    wait_phase(qtbot, studio, "failed")
    assert studio.promptId == ids[0] and studio.statusText == "The take failed · Retake or Discard"
    # Discarded, the next take's Return is its own: nothing was left queued.
    studio.discard()
    key(studio_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 3, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    key(studio_window, Qt.Key.Key_R)
    recorder.seal()
    wait_phase(qtbot, studio, "review")
    assert studio.promptId == ids[0] and studio.statusText != QUEUED

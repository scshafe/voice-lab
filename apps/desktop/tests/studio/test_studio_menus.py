"""The menu bar (D-058; the recording UX review's item 4): every recording command in a native
menu with its key, disabled (never hidden) when the phase forbids it; key hints on the
buttons; ⌘/ for the shortcuts; ⌘↑ and ⌘↓ for the prompt before and after, ⌘J for Continue
Missing, ⌘+ ⌘− ⌘0 for the reading size.

Offscreen there is no system menu bar: the tests read the menu's actions and trigger them, as
a click (or Full Keyboard Access) would. The keys are the screen's ``Shortcut``s."""

from __future__ import annotations

from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder
from voice_lab_desktop import menus

from .conftest import wait_phase
from .test_studio_core_loop import open_popups
from .test_studio_screen import prompt_ids
from .test_studio_takes import saved

RECORDING = [
    "record",
    "play",
    "save",
    "save-next",
    "discard",
    "remove",
    "reject",
    "restore",
    "check",
    "continue-missing",
    "next-flagged",
    "previous-prompt",
    "next-prompt",
]


def bar(window: Window) -> menus.MenuBar:
    menu_bar = window.engine.menus
    assert isinstance(menu_bar, menus.MenuBar)
    return menu_bar


def actions(window: Window) -> dict[str, QAction]:
    return bar(window).actions


def enabled(window: Window) -> set[str]:
    return {name for name, action in actions(window).items() if action.isEnabled()} & set(RECORDING)


def key(window: Window, which: Qt.Key, modifier: Qt.KeyboardModifier | None = None) -> None:
    QTest.keyClick(window.root, which, modifier or Qt.KeyboardModifier.NoModifier)


CTRL = Qt.KeyboardModifier.ControlModifier


def test_the_menus_in_the_higs_order_with_their_keys(studio_window: Window) -> None:
    menu_bar = bar(studio_window).bar
    titles = [action.text() for action in menu_bar.actions()]
    assert titles == ["File", "View", "Recording", "Window", "Help"]
    recording = next(a.menu() for a in menu_bar.actions() if a.text() == "Recording")
    names = [a.objectName().removeprefix("menu-") for a in recording.actions() if a.objectName()]
    assert names == RECORDING
    shown = {name: action.shortcut() for name, action in actions(studio_window).items()}
    assert shown["record"] == QKeySequence("R") and shown["play"] == QKeySequence("Space")
    assert shown["save"] == QKeySequence(QKeySequence.StandardKey.Save)
    assert shown["save-next"] == QKeySequence("Return")
    assert shown["continue-missing"] == QKeySequence("Ctrl+J")
    assert shown["previous-prompt"] == QKeySequence("Ctrl+Up")
    assert shown["next-prompt"] == QKeySequence("Ctrl+Down")
    assert shown["shortcuts"] == QKeySequence("Ctrl+/")
    assert shown["minimize"] == QKeySequence("Ctrl+M")
    for name in ("discard", "remove", "reject", "restore", "check", "zoom"):
        assert shown[name].isEmpty()
    # Qt never fires a menu's key: each is the screen's Shortcut's, so none is taken twice.
    assert all(
        action.shortcutContext() == Qt.ShortcutContext.WidgetShortcut
        for action in actions(studio_window).values()
    )


def test_each_item_follows_the_phase(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, items = studio_window.model.studio, actions(studio_window)
    recorder.hold_seal = True
    # Ready: Record and the prompts.
    assert items["record"].text() == "Record"
    assert enabled(studio_window) == {"record", "check", "continue-missing", "next-prompt"}
    # Recording: Stop, nothing else.
    key(studio_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    assert items["record"].text() == "Stop" and enabled(studio_window) == {"record"}
    # Stopping: nothing (a Return typed ahead waits for the take).
    key(studio_window, Qt.Key.Key_R)
    assert studio.phase == "stopping" and enabled(studio_window) == set()
    recorder.seal()
    wait_phase(qtbot, studio, "review")
    # Review: Retake, Play, Save and Save & Next.
    qtbot.waitUntil(lambda: items["play"].isEnabled(), timeout=5000)
    assert items["record"].text() == "Retake"
    assert enabled(studio_window) == {
        "record",
        "play",
        "save",
        "save-next",
        "check",
        "continue-missing",
        "next-prompt",
    }
    items["play"].trigger()
    qtbot.waitUntil(lambda: items["play"].text() == "Pause", timeout=5000)
    items["play"].trigger()
    qtbot.waitUntil(lambda: items["play"].text() == "Play", timeout=5000)
    # Save from the menu; then the kept take's own decisions.
    items["save"].trigger()
    wait_phase(qtbot, studio, "ready")
    qtbot.waitUntil(lambda: items["remove"].isEnabled(), timeout=5000)
    assert {"remove", "reject", "record"} <= enabled(studio_window)
    assert not items["restore"].isEnabled()


def test_a_failed_take_is_discarded_from_the_menu(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, items = studio_window.model.studio, actions(studio_window)
    recorder.fail_next = "a continuity break"
    items["record"].trigger()
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    items["record"].trigger()
    wait_phase(qtbot, studio, "failed")
    assert items["record"].text() == "Retake" and items["discard"].isEnabled()
    items["discard"].trigger()
    wait_phase(qtbot, studio, "ready")
    assert studio.statusText == "Take discarded"


def test_the_menu_waits_behind_a_dialog_and_on_another_screen(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    items = actions(studio_window)
    saved(qtbot, studio_window, recorder)
    assert items["reject"].isEnabled()
    # Reject Take… opens the picker's dialog; while it is open, the menu waits.
    items["reject"].trigger()
    dialog = studio_window.find("studioRejectDialog")
    qtbot.waitUntil(lambda: dialog.property("visible"), timeout=5000)
    qtbot.waitUntil(lambda: enabled(studio_window) == set(), timeout=5000)
    dialog.close()
    qtbot.waitUntil(lambda: items["record"].isEnabled(), timeout=5000)
    # Check Input… opens its window (the microphone waits for Start).
    items["check"].trigger()
    check = studio_window.find("studioCheckDialog")
    qtbot.waitUntil(lambda: check.property("visible"), timeout=5000)
    assert enabled(studio_window) == set() and len(recorder.takes) == 1
    studio_window.model.studio.check.cancel()
    qtbot.waitUntil(lambda: items["record"].isEnabled(), timeout=5000)
    # Another screen: nothing.
    studio_window.model.navigate("status")
    qtbot.waitUntil(lambda: enabled(studio_window) == set(), timeout=5000)
    assert not actions(studio_window)["shortcuts"].isEnabled()
    studio_window.model.navigate("prompts")
    qtbot.waitUntil(lambda: items["record"].isEnabled(), timeout=5000)


def test_previous_next_and_continue_missing(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, items = studio_window.model.studio, actions(studio_window)
    ids = prompt_ids(pc)
    assert studio.promptId == ids[0] and not items["previous-prompt"].isEnabled()
    key(studio_window, Qt.Key.Key_Down, CTRL)
    qtbot.waitUntil(lambda: studio.promptId == ids[1], timeout=5000)
    items["next-prompt"].trigger()
    qtbot.waitUntil(lambda: studio.promptId == ids[2], timeout=5000)
    key(studio_window, Qt.Key.Key_Up, CTRL)
    qtbot.waitUntil(lambda: studio.promptId == ids[1], timeout=5000)
    items["previous-prompt"].trigger()
    qtbot.waitUntil(lambda: studio.promptId == ids[0], timeout=5000)
    # In the list as it shows: a search narrows it.
    studio.setSearch(studio.rowsModel.rows[5]["title"])
    shown = [row["promptId"] for row in studio.rowsModel.rows]
    assert ids[0] not in shown
    key(studio_window, Qt.Key.Key_Down, CTRL)  # the selected prompt is hidden: the first shown
    qtbot.waitUntil(lambda: studio.promptId == shown[0], timeout=5000)
    studio.setSearch("")
    # ⌘J: the next missing prompt, as the button does.
    studio.selectPrompt(ids[0])
    qtbot.waitUntil(lambda: studio.promptId == ids[0], timeout=5000)
    saved(qtbot, studio_window, recorder)
    key(studio_window, Qt.Key.Key_J, CTRL)
    qtbot.waitUntil(lambda: studio.promptId == ids[1], timeout=5000)
    # A take under review is kept as a draft when ⌘↓ leaves it, as any selection does.
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    key(studio_window, Qt.Key.Key_Down, CTRL)
    qtbot.waitUntil(lambda: studio.promptId == ids[2], timeout=5000)
    qtbot.waitUntil(lambda: len(studio.client.takes()) == 2, timeout=5000)
    drafts = [t for t in studio.client.takes() if t.prompt_id == ids[1]]
    assert [t.sync.decision.decision for t in drafts] == ["draft"]


def test_the_reading_size_from_the_view_menu_and_its_keys(
    qtbot: Any, studio_window: Window
) -> None:
    studio, items = studio_window.model.studio, actions(studio_window)
    assert studio.readingSize == 24 and not items["smaller"].isEnabled()
    key(studio_window, Qt.Key.Key_Equal, CTRL)
    assert studio.readingSize == 28
    items["bigger"].trigger()
    assert studio.readingSize == 32 and not items["bigger"].isEnabled()
    key(studio_window, Qt.Key.Key_Minus, CTRL)
    assert studio.readingSize == 28
    key(studio_window, Qt.Key.Key_0, CTRL)
    assert studio.readingSize == 24 and not items["standard-size"].isEnabled()


def test_the_shortcut_overlay(qtbot: Any, studio_window: Window) -> None:
    overlay = studio_window.find("studioShortcuts")
    screen = studio_window.find("studioScreen")
    key(studio_window, Qt.Key.Key_Slash, CTRL)
    qtbot.waitUntil(lambda: overlay.property("visible"), timeout=5000)
    assert screen.property("menuOpen") and not screen.property("keysEnabled")
    descriptions = [row["description"] for row in studio_window.model.studio.shortcuts]
    assert descriptions[0] == "Record; Stop" and "These shortcuts" in descriptions
    key(studio_window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: open_popups(studio_window) == [], timeout=5000)
    actions(studio_window)["shortcuts"].trigger()
    qtbot.waitUntil(lambda: overlay.property("visible"), timeout=5000)
    key(studio_window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not overlay.property("visible"), timeout=5000)


def test_the_buttons_show_their_keys(studio_window: Window) -> None:
    keys = studio_window.model.studio.keys
    assert keys == {
        "record": "R",
        "play": "Space",
        "save": QKeySequence(QKeySequence.StandardKey.Save).toString(
            QKeySequence.SequenceFormat.NativeText
        ),
        "saveNext": QKeySequence("Return").toString(QKeySequence.SequenceFormat.NativeText),
        "continueMissing": QKeySequence("Ctrl+J").toString(QKeySequence.SequenceFormat.NativeText),
    }
    buttons = (
        "studioRetake",
        "studioSave",
        "studioSaveNext",
        "studioPlay",
        "studioContinueMissing",
    )
    hints = {
        name: [hint.property("text") for hint in studio_window.items("keyHint", item)]
        for name in buttons
        for item in [studio_window.find(name)]
    }
    assert hints == {
        "studioRetake": [keys["record"]],
        "studioSave": [keys["save"]],
        "studioSaveNext": [keys["saveNext"]],
        "studioPlay": [keys["play"]],
        "studioContinueMissing": [keys["continueMissing"]],
    }
    assert studio_window.find("studioRecordKey").property("text") == "R"


def test_the_window_menu(studio_window: Window) -> None:
    items = actions(studio_window)
    assert items["minimize"].isEnabled() and items["zoom"].isEnabled()
    assert items["close-window"].shortcut() == QKeySequence(QKeySequence.StandardKey.Close)
    items["zoom"].trigger()  # offscreen, the window's state is the platform's to keep
    items["minimize"].trigger()
    assert isinstance(studio_window.root, QObject)

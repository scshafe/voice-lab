"""The studio's keys (RECORDING-V1.md §5; NATIVE_RECORDING_UX.md §4 "Keyboard behavior"):
R records and stops; Space stops a recording and otherwise plays or pauses (it never opens
the microphone); Return is Save & Next; Ctrl+S (⌘S on macOS) is Save; in the waveform, ←/→
step a tenth of a second and Home/End go to the kept frames' edges. Keys are ignored while
typing, while a menu or dialog is open, on another screen, and on a held key's repeats."""

from __future__ import annotations

from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder

from .conftest import wait_phase
from .test_studio_screen import click, prompt_ids
from .test_studio_takes import saved


def key(
    window: Window, which: Qt.Key, modifier: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
) -> None:
    QTest.keyClick(window.root, which, modifier)


def test_r_space_return_and_save(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    ids = prompt_ids(pc)
    # Space never starts a recording.
    key(studio_window, Qt.Key.Key_Space)
    assert recorder.takes == [] and studio.phase == "ready"
    # R records, R stops.
    key(studio_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    key(studio_window, Qt.Key.Key_R)
    wait_phase(qtbot, studio, "review")
    # Space plays the take under review (once it has loaded: Play is enabled), and pauses it.
    qtbot.waitUntil(lambda: takes.canPlay, timeout=5000)
    key(studio_window, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: takes.playing, timeout=5000)
    key(studio_window, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: not takes.playing, timeout=5000)
    # Ctrl+S saves and stays.
    key(studio_window, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(
        lambda: studio.phase == "ready" and studio.coverageText == "1 / 34", timeout=5000
    )
    assert studio.promptId == ids[0]
    # Space stops a recording; Return saves and goes on.
    key(studio_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    key(studio_window, Qt.Key.Key_Space)
    wait_phase(qtbot, studio, "review")
    key(studio_window, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: studio.promptId == ids[1], timeout=5000)
    assert len(studio.client.takes()) == 2


def test_the_waveform_keys(qtbot: Any, studio_window: Window, recorder: StudioRecorder) -> None:
    takes = studio_window.model.studio.takes
    recorder.seconds = 0.5
    saved(qtbot, studio_window, recorder)
    takes.setTrimRange(4_800, 19_200)
    wave = studio_window.find("studioWave")
    click(studio_window, "studioWave")  # a click takes the focus (and moves the playhead)
    qtbot.waitUntil(lambda: wave.hasActiveFocus(), timeout=5000)
    key(studio_window, Qt.Key.Key_Home)
    assert takes.playhead == 4_800
    key(studio_window, Qt.Key.Key_Right)
    assert takes.playhead == 9_600
    key(studio_window, Qt.Key.Key_Left)
    assert takes.playhead == 4_800
    key(studio_window, Qt.Key.Key_End)
    assert takes.playhead == 19_200
    # A held key's repeats are ignored.
    repeat = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier, "", True
    )
    QCoreApplication.sendEvent(wave, repeat)
    assert takes.playhead == 19_200
    once = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier, "", False
    )
    QCoreApplication.sendEvent(wave, once)
    assert takes.playhead == 14_400


def test_keys_are_ignored_while_typing_in_menus_dialogs_and_elsewhere(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    screen = studio_window.find("studioScreen")
    # Every key of the page ignores a held key's repeats.
    shortcuts = [studio_window.find(name) for name in (
        "studioKeyRecord", "studioKeySpace", "studioKeySaveNext", "studioKeySave",
    )]  # fmt: skip
    assert all(shortcut.property("autoRepeat") is False for shortcut in shortcuts)
    # Typing in the search field: R is a letter of the search.
    search = studio_window.find("studioSearch")
    search.forceActiveFocus()
    qtbot.waitUntil(lambda: screen.property("typing"), timeout=5000)
    key(studio_window, Qt.Key.Key_R)
    assert search.property("text") == "r" and recorder.takes == []
    assert not any(shortcut.property("enabled") for shortcut in shortcuts)
    search.setProperty("text", "")
    search.textEdited.emit()
    studio_window.find("studioWave").forceActiveFocus()
    qtbot.waitUntil(lambda: screen.property("keysEnabled"), timeout=5000)
    # A menu open: the Labels menu, the take list.
    for opener, popup in (
        ("studioLabels", "studioLabelsPopup"),
        ("studioTakeSelect", "studioHistoryPopup"),
    ):
        if opener == "studioTakeSelect":
            saved(qtbot, studio_window, recorder)
        click(studio_window, opener)
        qtbot.waitUntil(
            lambda popup=popup: studio_window.find(popup).property("visible"), timeout=5000
        )
        assert screen.property("menuOpen") and not screen.property("keysEnabled")
        count = len(recorder.takes)
        key(studio_window, Qt.Key.Key_R)
        assert len(recorder.takes) == count
        studio_window.find(popup).close()
        qtbot.waitUntil(lambda: screen.property("keysEnabled"), timeout=5000)
    # The Reject dialog: its reason field takes the letters.
    click(studio_window, "studioRejectTake")
    dialog = studio_window.find("studioRejectDialog")
    qtbot.waitUntil(lambda: dialog.property("visible"), timeout=5000)
    reason = studio_window.items("studioRejectReason")[0]
    qtbot.waitUntil(lambda: reason.hasActiveFocus(), timeout=5000)
    count, prompt = len(recorder.takes), studio.promptId
    key(studio_window, Qt.Key.Key_R)
    assert len(recorder.takes) == count and reason.property("text") == "r"
    # Return is the dialog's (Reject, with its reason), not the page's Save & Next.
    key(studio_window, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=5000)
    assert studio.promptId == prompt and len(recorder.takes) == count
    qtbot.waitUntil(lambda: takes.history[0]["decision"] == "rejected", timeout=5000)
    assert takes.history[0]["status"] == "Rejected (r)"
    qtbot.waitUntil(lambda: screen.property("keysEnabled"), timeout=5000)
    # Another screen: the page's keys are its own.
    studio_window.model.navigate("status")
    qtbot.waitUntil(lambda: not screen.property("keysEnabled"), timeout=5000)
    key(studio_window, Qt.Key.Key_R)
    assert len(recorder.takes) == count and studio.phase == "ready"
    studio_window.model.navigate("prompts")
    qtbot.waitUntil(lambda: screen.property("keysEnabled"), timeout=5000)
    key(studio_window, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)

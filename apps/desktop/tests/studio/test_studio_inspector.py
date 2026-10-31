"""The layout (D-058): a right inspector that hides (⌥⌘I) holds the take's history and picking,
its trim, Save and Discard, the flags' details, a spoken prompt's delivery notes, the pace and
the sync, so the centre keeps the prompt, the waveform and Record, Retake and Save & Next; the
library on the left folds away (⌃⌘S, Focus). Both are kept for the next start."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import Window, visual_items
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from .test_studio_menus import actions
from .test_studio_screen import geometry, resize, scene_rect

#: What the inspector holds.
INSPECTOR = (
    "studioCheckInput",
    "studioTakePicker",
    "studioRemoveTake",
    "studioRejectTake",
    "studioRestoreTake",
    "studioSave",
    "studioDiscard",
    "studioTrimRow",
    "studioInspectorFlags",
    "studioInspectorNotes",
    "studioSessionText",
    "studioInspectorSync",
)
#: What the centre keeps.
CENTRE = ("studioScriptCard", "studioWaveform", "studioRecord", "studioRetake", "studioSaveNext")


def key(window: Window, which: Qt.Key, modifiers: Qt.KeyboardModifier) -> None:
    QTest.keyClick(window.root, which, modifiers)


Rect = tuple[float, float, float, float]


def inside(rect: Rect, area: Rect) -> bool:
    """``rect`` lies across ``area``'s width."""
    x, _y, w, _h = rect
    ax, _ay, aw, _ah = area
    return ax <= x and x + w <= ax + aw + 0.5


def test_the_centre_keeps_the_prompt_the_waveform_and_the_transport(
    qtbot: Any, studio_window: Window
) -> None:
    resize(qtbot, studio_window, 1440, 900)
    geometry(qtbot, studio_window)  # laid out
    inspector = scene_rect(studio_window.find("studioInspector"))
    workspace = scene_rect(studio_window.find("studioWorkspace"))
    for name in INSPECTOR:
        assert inside(scene_rect(studio_window.find(name)), inspector), name
    for name in CENTRE:
        assert inside(scene_rect(studio_window.find(name)), workspace), name
    footer = studio_window.find("studioFooter")
    names = [item.objectName() for item in visual_items(footer, "studioRetake")]
    assert names == ["studioRetake"]
    assert visual_items(footer, "studioSave") == [] and visual_items(footer, "studioDiscard") == []
    # A spoken prompt's instructions are its delivery notes in the inspector, not on its card.
    studio = studio_window.model.studio
    notes = studio_window.find("studioInspectorNotes")
    assert notes.property("text") == (studio.instructions or "None")
    assert not studio_window.find("studioInstructionsSection").isVisible()


def test_option_command_i_hides_the_inspector_and_it_stays_hidden(
    qtbot: Any, studio_window: Window, data_dir: Path
) -> None:
    studio, items = studio_window.model.studio, actions(studio_window)
    resize(qtbot, studio_window, 1440, 900)
    wide = scene_rect(studio_window.find("studioWaveform"))
    assert items["inspector"].text() == "Hide Inspector"
    key(
        studio_window,
        Qt.Key.Key_I,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
    )
    qtbot.waitUntil(lambda: not studio.inspectorShown, timeout=5000)
    inspector = studio_window.find("studioInspector")
    qtbot.waitUntil(lambda: not inspector.isVisible(), timeout=5000)
    assert items["inspector"].text() == "Show Inspector"
    assert not studio_window.find("studioInspectorToggle").property("checked")
    geometry(qtbot, studio_window)
    wider = scene_rect(studio_window.find("studioWaveform"))
    assert wider[0] == wide[0] and wider[2] > wide[2]  # the centre takes its room
    # The card shows the instructions again: nothing holds them elsewhere now.
    assert studio_window.find("studioInstructionsSection").isVisible()
    assert json.loads((data_dir / "studio" / "view.json").read_text()) == {"inspector": False}
    assert studio._load_view()["inspector"] is False
    items["inspector"].trigger()
    qtbot.waitUntil(lambda: inspector.isVisible(), timeout=5000)


def test_control_command_s_folds_the_library_away(
    qtbot: Any, studio_window: Window, data_dir: Path
) -> None:
    studio, items = studio_window.model.studio, actions(studio_window)
    resize(qtbot, studio_window, 1440, 900)
    before = scene_rect(studio_window.find("studioWaveform"))
    key(
        studio_window,
        Qt.Key.Key_S,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier,
    )
    qtbot.waitUntil(lambda: not studio.libraryShown, timeout=5000)
    library = studio_window.find("studioLibrary")
    qtbot.waitUntil(lambda: not library.isVisible(), timeout=5000)
    assert items["library"].text() == "Show Library"
    geometry(qtbot, studio_window)
    after = scene_rect(studio_window.find("studioWaveform"))
    assert after[0] < before[0] and after[2] > before[2]
    # The header's toggle brings it back.
    from .test_studio_screen import click

    click(studio_window, "studioLibraryToggle")
    qtbot.waitUntil(lambda: library.isVisible(), timeout=5000)
    assert json.loads((data_dir / "studio" / "view.json").read_text()) == {"library": True}


def test_on_the_smallest_window_the_reference_layout_returns(
    qtbot: Any, studio_window: Window
) -> None:
    """No room for the inspector beside a centre of 440 points: its button and menu item wait,
    and the take's picker, trim, Save and Discard are the centre's, as the reference has them;
    every region stays inside the window, with the library shown or folded."""
    studio, items = studio_window.model.studio, actions(studio_window)
    screen = studio_window.find("studioScreen")
    resize(qtbot, studio_window, 960, 600)
    qtbot.waitUntil(lambda: not screen.property("inspectorOn"), timeout=5000)
    assert studio.inspectorShown and not studio.inspectorRoom
    assert not studio_window.find("studioInspectorToggle").isEnabled()
    assert not items["inspector"].isEnabled()
    found = geometry(qtbot, studio_window)
    workspace = scene_rect(studio_window.find("studioWorkspace"))
    for name in ("studioTakePicker", "studioTrimRow", "studioSave", "studioDiscard"):
        assert inside(scene_rect(studio_window.find(name)), workspace), name
    assert studio_window.find("studioInstructionsSection").isVisible()
    right = scene_rect(screen)[0] + screen.width()
    assert all(x + w <= right + 0.5 for x, _y, w, _h in found.values() if w > 0), found
    studio.toggleLibrary()
    found = geometry(qtbot, studio_window)
    assert all(x + w <= right + 0.5 for x, _y, w, _h in found.values() if w > 0), found
    studio.toggleLibrary()

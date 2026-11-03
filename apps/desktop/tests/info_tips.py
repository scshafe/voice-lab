"""The app's ⓘ (D-059, docs/design/INFO-TIPS.md): scshafe-qt's ``SuiInfoTip`` through
``VoiceLab.Controls``' ``InfoTip``. A page's test checks that the text it moved is the tip's,
and that the tip opens on hover, on a click and from the keyboard without taking the page's
focus or keys; the component's own behaviour is scshafe-qt's to test."""

from __future__ import annotations

from typing import Any

from conftest import Window
from PySide6.QtCore import QObject, QPoint, QPointF, Qt
from PySide6.QtTest import QTest


def body(tip: Any) -> str:
    """What the tip's popover says (its title aside)."""
    text = tip.findChild(QObject, "infoTipBody")
    assert text is not None
    return str(text.property("text"))


def is_tip(item: Any) -> bool:
    """An ⓘ: its ``text`` is its popover's, never drawn in the page."""
    return item.property("opened") is not None and item.property("placement") is not None


def shown_texts(item: Any) -> list[str]:
    """The text every visible item under ``item`` draws, an ⓘ's popover text left out."""
    if not item.isVisible() or is_tip(item):
        return []
    found = []
    text = item.property("text")
    if isinstance(text, str) and text:
        found.append(text)
    for child in item.childItems():
        found.extend(shown_texts(child))
    return found


def _centre(tip: Any) -> QPoint:
    return tip.mapToScene(QPointF(tip.width() / 2, tip.height() / 2)).toPoint()


def opens_on_hover_click_and_keys(qtbot: Any, window: Window | Any, tip: Any) -> None:
    """``tip`` opens on hover and closes when the pointer leaves; a click pins it open and
    leaves the focus where it was; with the focus (Tab), Space and Return open it and Esc
    closes it. It ends closed, with the focus back where it was. ``window``: the main
    window's ``Window``, or the window the tip is in (Settings, say)."""
    root = window.root if isinstance(window, Window) else window
    assert tip.isVisible() and tip.isEnabled() and body(tip) != ""
    # Laid out first: a page that just showed may not have placed the tip yet.
    with qtbot.waitSignal(root.frameSwapped, timeout=5000):
        root.update()
    was = root.activeFocusItem()
    # Hover.
    QTest.mouseMove(root, _centre(tip))
    qtbot.waitUntil(lambda: tip.property("opened"), timeout=3000)
    QTest.mouseMove(root, QPoint(2, root.height() - 2))
    qtbot.waitUntil(lambda: not tip.property("opened"), timeout=3000)
    # A click: open until the next one, and the focus stays.
    for _ in range(2):
        opened = bool(tip.property("opened"))
        QTest.mouseClick(
            root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, _centre(tip)
        )
        qtbot.waitUntil(lambda opened=opened: tip.property("opened") != opened, timeout=3000)
        assert root.activeFocusItem() is was
    # The keyboard.
    tip.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: tip.property("activeFocus"), timeout=3000)
    keys = ((Qt.Key.Key_Space, Qt.Key.Key_Escape), (Qt.Key.Key_Return, Qt.Key.Key_Return))
    for opens, closes in keys:
        QTest.keyClick(root, opens)
        qtbot.waitUntil(lambda: tip.property("opened"), timeout=3000)
        QTest.keyClick(root, closes)
        qtbot.waitUntil(lambda: not tip.property("opened"), timeout=3000)
    if was is not None:
        was.forceActiveFocus()

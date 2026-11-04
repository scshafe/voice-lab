"""The app shell (checklist A): the sidebar, the screen headers, the toasts."""

from __future__ import annotations

import json
from typing import Any

import pytest
from voice_lab_desktop.app import SCREENS

SPA_HEADERS = {
    "corpus": (
        "Voice Journey Corpus Browser",
        "Spot-check workbench over committed manifests. Playback is release-gated and "
        "per-click only.",
    ),
    "journey": (
        "The Voice Journey",
        "Seven years of practice, measured: range, sustain, timbre, vibrato, and the "
        "same-song verdict.",
    ),
    "practice": (
        "The Practice Room",
        "Tonight's session, argued from the data — due songs and frontier flags from the coach.",
    ),
    "referee": ("The Referee", "Blind same-song A/B trials. Years stay hidden until you judge."),
}


@pytest.mark.checklist("A-01")
def test_sidebar_reaches_every_screen(qtbot: Any, window: Any) -> None:
    sidebar = window.model.sidebar
    assert [entry["key"] for entry in sidebar] == [key for key, *_ in SCREENS]
    assert {entry["text"] for entry in sidebar} >= {"Corpus", "Journey", "Practice", "Referee"}
    stack = window.find("screens")
    sidebar_list = window.find("sidebarList")
    resets: list[int] = []
    window.model.sidebarChanged.connect(lambda: resets.append(1))
    for index, entry in enumerate(sidebar):
        sidebar_list.activated.emit(index)
        key = entry["key"]
        qtbot.waitUntil(lambda key=key: window.model.screen == key)
        assert stack.property("currentIndex") == index
        assert sidebar_list.property("selectedIndex") == index
    # Navigating never replaces the sidebar's entries (a model reset mislabels its headers).
    assert resets == []
    # Nor does a change of an entry (the uploads' count): the row changes in place.
    model = window.model.sidebarModel
    reset: list[int] = []
    changed: list[int] = []
    model.modelReset.connect(lambda: reset.append(1))
    model.dataChanged.connect(lambda top, _bottom, _roles: changed.append(top.row()))
    rows = [dict(row) for row in window.model.sidebar]
    rows[1]["count"] = 3
    model.update(rows)
    assert reset == [] and changed == [1]
    assert model.rows[1]["count"] == 3 and model.rowCount() == len(SCREENS)
    shown = sorted(
        (item.y(), item.property("text"))
        for item in window.items("sectionHeader", sidebar_list)
        if item.isVisible()
    )
    assert [text for _y, text in shown] == ["Lab", "Record", "Corpus", "Analysis", "Referee"]


def test_the_screens_list_hides_like_notes_sidebar(qtbot: Any, window: Any) -> None:
    """D-059: the screens list (not the prompt library) hides with its pin or View → Hide
    Screens List; hidden, the pointer resting at the window's top-left shows it over the
    content, which does not move, until the pointer leaves. The choice is kept."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtTest import QTest

    model, root = window.model, window.root
    shell = window.find("shell")
    stack = window.find("screens")
    pin = window.find("sidebarPin")
    data = window.rig.model.context.config.data_dir / "window.json"
    assert model.screensShown and shell.property("sidebarOpen") and pin.property("checked")
    docked = stack.mapToScene(QPointF(0, 0)).x()
    assert docked > 0

    def centre(item: Any) -> QPoint:
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()

    # The pin hides it; the screens take the room.
    QTest.mouseClick(root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre(pin))
    qtbot.waitUntil(lambda: not model.screensShown, timeout=5000)
    qtbot.waitUntil(lambda: stack.mapToScene(QPointF(0, 0)).x() == 0, timeout=5000)
    assert json.loads(data.read_text())["screens"] is False
    # The pointer resting at the top-left shows it over the screens, which stay put.
    zone = window.find("sidebarRevealZone")
    assert zone.isVisible() and zone.mapToScene(QPointF(0, 0)).toPoint() == QPoint(0, 0)
    QTest.mouseMove(root, centre(zone))
    qtbot.waitUntil(lambda: shell.property("sidebarRevealed"), timeout=5000)
    sidebar_list = window.find("sidebarList")
    qtbot.waitUntil(lambda: sidebar_list.mapToScene(QPointF(0, 0)).x() >= 0, timeout=5000)
    assert stack.mapToScene(QPointF(0, 0)).x() == 0 and not model.screensShown
    # It goes once the pointer leaves it.
    QTest.mouseMove(root, QPoint(root.width() - 10, root.height() - 10))
    qtbot.waitUntil(lambda: not shell.property("sidebarRevealed"), timeout=5000)
    # View → Show Screens List docks it again.
    menu = window.engine.menus.actions["screens"]
    assert menu.text() == "Show Screens List"
    menu.trigger()
    qtbot.waitUntil(lambda: shell.property("sidebarOpen"), timeout=5000)
    assert model.screensShown and pin.property("checked") and menu.text() == "Hide Screens List"
    qtbot.waitUntil(lambda: stack.mapToScene(QPointF(0, 0)).x() == docked, timeout=5000)
    assert json.loads(data.read_text())["screens"] is True


@pytest.mark.checklist("A-03")
def test_every_screen_has_the_spa_title_and_tagline(qtbot: Any, window: Any) -> None:
    from PySide6.QtCore import QObject

    stack = window.find("screens")
    for key, (title, tagline) in SPA_HEADERS.items():
        window.model.navigate(key)
        screen = stack.findChild(QObject, f"{key}Screen")
        assert screen is not None, key
        assert screen.findChild(QObject, "screenTitle").property("text") == title
        assert screen.findChild(QObject, "screenTagline").property("text") == tagline


@pytest.mark.checklist("A-05")
def test_toasts_carry_the_spa_messages(qtbot: Any, window: Any) -> None:
    toasts = window.find("toasts")
    assert toasts.property("count") == 0
    # Every screen's model reaches the one toast host (the SPA's tray).
    for model in window.model.models.values():
        model.toast.emit(f"from {type(model).__name__}", "ok")
    qtbot.waitUntil(lambda: toasts.property("count") == 4)  # the host keeps the newest four
    qtbot.waitUntil(lambda: len(window.items("body", toasts)) == 4)  # laid out on a frame
    shown = sorted(item.property("text") for item in window.items("body", toasts))
    names = ("CorpusModel", "JourneyModel", "PracticeModel", "RefereeModel")
    assert shown == [f"from {name}" for name in names]
    # A toast is dismissed on request, as the SPA's tray dismissed one on a click.
    window.items("closeButton", toasts)[0].clicked.emit()
    qtbot.waitUntil(lambda: toasts.property("count") == 3)


@pytest.mark.checklist("A-07")
def test_click_throughs_replace_deep_links(qtbot: Any, window: Any) -> None:
    from synthetic import CLUSTERS

    model = window.model
    corpus = model.corpus
    first, second = list(CLUSTERS)

    def filtered_to(cluster: str) -> bool:
        rows = corpus.rows.rows
        return (
            model.screen == "corpus"
            and corpus.searchText == cluster
            and corpus.state == "loaded"
            and not corpus.loading
            and bool(rows)
            and all(cluster == row.get("lyricMatchCluster") for row in rows)
        )

    # The Journey's song links (scorecards, threads) open the Corpus on that song ...
    model.navigate("journey")
    qtbot.waitUntil(lambda: model.journey.state == "loaded", timeout=10000)
    model.journey.selectSubject("threads")
    model.journey.openCluster(first)
    qtbot.waitUntil(lambda: filtered_to(first), timeout=10000)
    # ... and the Journey keeps the tab it was on (the SPA's ?subject=).
    model.navigate("journey")
    qtbot.waitUntil(lambda: model.journey.state == "loaded", timeout=10000)
    assert model.journey.subject == "threads"
    # The Practice's due and frontier songs do the same.
    model.navigate("practice")
    qtbot.waitUntil(lambda: model.practice.state == "loaded", timeout=10000)
    model.practice.openCluster(second)
    qtbot.waitUntil(lambda: filtered_to(second), timeout=10000)

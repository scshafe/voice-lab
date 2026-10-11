"""The app shell (checklist A): the sidebar, the screen headers, the toasts."""

from __future__ import annotations

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
    shown = sorted(
        (item.y(), item.property("text"))
        for item in window.items("sectionHeader", sidebar_list)
        if item.isVisible()
    )
    assert [text for _y, text in shown] == ["Lab", "Corpus", "Analysis", "Referee"]


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

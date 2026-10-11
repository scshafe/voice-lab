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
    for index, entry in enumerate(sidebar):
        window.find("sidebarList").activated.emit(index)
        key = entry["key"]
        qtbot.waitUntil(lambda key=key: window.model.screen == key)
        assert stack.property("currentIndex") == index

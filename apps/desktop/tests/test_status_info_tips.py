"""The Status screen's explanations, the screens' taglines and the sign-in placeholder's how-to
behind an ⓘ (D-059, docs/design/INFO-TIPS.md): each text is the tip's, the tip opens on hover,
on a click and from the keyboard, and the screen no longer shows it. What changes (the account,
the approval's summary, the gates, the job runner's lines, the spool) stays visible."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import info_tips
from conftest import Window, make_rig, open_window
from PySide6.QtCore import QObject
from synthetic import config_for
from voice_lab_desktop.app import SCREENS

#: Each card's explanation, and how it begins.
STATUS_TIPS = {
    "statusUploadsCardInfo": "Recordings go only to the PC you approve",
    "statusGatesCardInfo": "What the operator enabled on the server.",
    "statusJobsCardInfo": "The PC's journey pipeline (GET /api/v1/watch).",
    "statusSpoolCardInfo": "Measurement sessions stay on this machine",
    "statusReportCardInfo": "The web view renders the evidence report;",
    "settingsSourceInfo": "Settings from ",
}


def test_the_status_screens_explanations_are_info_tips(qtbot: Any, window: Window) -> None:
    window.model.navigate("status")
    screen = window.find("statusScreen")
    qtbot.waitUntil(lambda: screen.isVisible(), timeout=5000)
    for name, start in STATUS_TIPS.items():
        tip = window.find(name)
        assert info_tips.body(tip).startswith(start), name
        info_tips.opens_on_hover_click_and_keys(qtbot, window, tip)
    shown = info_tips.shown_texts(screen)
    assert not any(text.startswith(tuple(STATUS_TIPS.values())) for text in shown)
    # What changes stays: the account and the approval's summary.
    assert window.find("accountText").property("text").startswith("Signed in as ")
    assert window.find("approvalSummary").isVisible()


def test_every_screens_tagline_is_an_info_tip_beside_its_title(qtbot: Any, window: Window) -> None:
    stack = window.find("screens")
    for key, *_ in SCREENS:
        window.model.navigate(key)
        screen = stack.childItems()[window.model.screenIndex]
        tagline = [t for t in window.items("screenTagline", screen) if t.isVisible()]
        if key == "prompts":  # the studio has no screen header
            assert tagline == []
            continue
        (tip,) = tagline
        assert info_tips.body(tip) != "" and tip.property("title") != "", key
        title = screen.findChild(QObject, "screenTitle")
        assert title.property("text") == tip.property("title"), key
        assert info_tips.body(tip) not in info_tips.shown_texts(screen), key
    window.model.navigate("uploads")
    (tip,) = [t for t in window.items("screenTagline") if t.isVisible()]
    info_tips.opens_on_hover_click_and_keys(qtbot, window, tip)


def test_not_set_up_the_how_to_is_behind_an_info_tip(
    qtbot: Any, tmp_path: Path, synthetic_root: Path
) -> None:
    with (
        make_rig(tmp_path, config_for(synthetic_root), configured=False, signed_in=False) as rig,
        open_window(rig) as window,
    ):
        window.model.navigate("status")
        assert window.find("setupBanner").isVisible()
        tip = window.find("setupInfo")
        assert info_tips.body(tip) == (
            "Set the voice-lab server and the Pocket ID issuer before signing in."
        )
        info_tips.opens_on_hover_click_and_keys(qtbot, window, tip)
        window.model.navigate("journey")
        needs = window.find("journeyScreen").findChild(QObject, "needsServer")
        assert needs.isVisible() and needs.property("title") == "Set up the server first"
        how = needs.findChild(QObject, "needsServerInfo")
        assert how.isVisible()
        assert (
            info_tips.body(how) == "Set the voice-lab server and the Pocket ID issuer in Settings."
        )

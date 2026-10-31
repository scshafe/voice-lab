"""Say why, in place, and Settings (D-058; the recording UX review's item 9): the take's state is
the waveform's own caption, a disabled Record names its blocker, and Settings (⌘,) is a window
of its own that opens on the pane last used, with corpus sync and Approve (so a fresh install
reaches a synced take without editing a file) and the quality flags' two levels. The sync
badge itself: test_studio_status.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder, voice_samples

from .conftest import open_studio, ready, studio_app, wait_phase
from .test_studio_menus import actions
from .test_studio_quality import expected, record_take


def settings_window(window: Window) -> Any:
    return window.find("settingsWindow")


def click_in(window: Any, item: Any) -> None:
    """A click in the middle of ``item``, in its own window."""
    centre = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)


def item_in(window: Any, name: str) -> Any:
    from conftest import visual_items

    (found,) = visual_items(window.contentItem(), name)
    return found


def test_settings_opens_on_the_pane_last_used(
    qtbot: Any, studio_window: Window, data_dir: Path
) -> None:
    app = studio_window.model
    settings = settings_window(studio_window)
    assert not settings.isVisible()
    # ⌘, (Ctrl+, here), and the menu's Settings…, which macOS puts in the app's menu.
    QTest.keyClick(studio_window.root, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: settings.isVisible(), timeout=5000)
    assert settings.property("title") == "Settings" and app.settingsPane == "general"
    assert item_in(settings, "serverField").property("text") == app.server
    click_in(settings, item_in(settings, "settingsPane-uploads"))
    qtbot.waitUntil(lambda: app.settingsPane == "uploads", timeout=5000)
    assert json.loads((data_dir / "window.json").read_text()) == {"settings_pane": "uploads"}
    settings.close()
    qtbot.waitUntil(lambda: not settings.isVisible(), timeout=5000)
    item = actions(studio_window)["settings"]
    assert item.menuRole() == QAction.MenuRole.PreferencesRole
    item.trigger()
    qtbot.waitUntil(lambda: settings.isVisible(), timeout=5000)
    settings.close()


def test_corpus_sync_and_approve_from_settings(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC
) -> None:
    holder: dict[str, Any] = {}
    model = studio_app(
        data_dir,
        tmp_path,
        pc,
        StudioRecorder(),
        enabled=lambda: "model" in holder and holder["model"].context.config.corpus_sync,
    )
    holder["model"] = model
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio = model.studio
            qtbot.waitUntil(lambda: studio.syncBadge == "Uploads off", timeout=10000)
            model.openSettings()
            model.setSettingsPane("uploads")
            settings = settings_window(window)
            qtbot.waitUntil(lambda: settings.isVisible(), timeout=5000)
            box = item_in(settings, "settingsCorpusSync")
            assert not box.property("checked")
            click_in(settings, box)
            qtbot.waitUntil(lambda: model.corpusSync, timeout=5000)
            assert "corpus_sync = true" in (tmp_path / "config.toml").read_text()
            # On, the corpus client sends: here, no approval stands yet, and the badge says so.
            qtbot.waitUntil(lambda: studio.syncBadge == "Needs your OK", timeout=10000)
            click_in(settings, item_in(settings, "settingsApprove"))
            dialog = window.find("approveDialog")
            qtbot.waitUntil(lambda: dialog.property("visible"), timeout=10000)
            model.approval.cancel()
            model.setCorpusSync(False)
            qtbot.waitUntil(lambda: studio.syncBadge == "Uploads off", timeout=10000)
            settings.close()
    finally:
        model.shutdown()


def test_the_quality_levels_from_settings(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, data_dir: Path
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    assert (studio.lowSnrDb, studio.driftDb) == (20.0, 6.0)
    studio.setQualityLevels(12.0, -3.0)  # a drift out of range keeps the one before
    assert (studio.lowSnrDb, studio.driftDb) == (12.0, 6.0)
    view = json.loads((data_dir / "studio" / "view.json").read_text())
    assert view == {"quality": {"low_snr_db": 12.0, "drift_db": 6.0}}
    # A take 15 dB over the room is no longer Low SNR under 12.
    seconds = expected(studio_window, studio.promptId)
    recorder.voice = lambda seed: voice_samples(seconds, floor_dbfs=-55.0, seed=seed)
    record_take(qtbot, studio_window, recorder)
    qtbot.waitUntil(lambda: takes.qualityState == "checked", timeout=5000)
    assert [flag["code"] for flag in takes.flags] == []
    # The Settings window shows them, and its fields set them.
    studio_window.model.openSettings()
    studio_window.model.setSettingsPane("recording")
    settings = settings_window(studio_window)
    qtbot.waitUntil(lambda: settings.isVisible(), timeout=5000)
    assert item_in(settings, "settingsLowSnr").property("text") == "12"
    settings.close()


def test_the_take_says_what_it_is_in_its_own_caption(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    caption = studio_window.find("studioWaveNote")
    record_take(qtbot, studio_window, recorder)
    qtbot.waitUntil(
        lambda: caption.property("text") == "Not saved yet · dB display scale", timeout=5000
    )
    studio.save()
    wait_phase(qtbot, studio, "ready")
    qtbot.waitUntil(lambda: takes.takeCaption == "Kept · this machine", timeout=5000)
    qtbot.waitUntil(
        lambda: caption.property("text") == "Kept · this machine · dB display scale",
        timeout=5000,
    )


def test_a_disabled_record_names_its_blocker(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC
) -> None:
    recorder = StudioRecorder(refused="no UMIK-1 is connected")
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            studio = model.studio
            qtbot.waitUntil(lambda: studio.phase == "ready", timeout=10000)
            qtbot.waitUntil(
                lambda: (
                    studio.recordBlocker == "No microphone: plug the UMIK-1 in, then click its chip"
                ),
                timeout=10000,
            )
            button = window.find("studioRecord")
            assert not button.isEnabled()
            assert button.property("Accessible.description") in (None, studio.recordBlocker)
            centre = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
            # From elsewhere onto the button: a move Qt cannot take for no move at all.
            QTest.mouseMove(window.root, QPointF(2, 2).toPoint())
            QTest.mouseMove(window.root, centre.toPoint())
            tip = window.find("studioRecordTip")
            qtbot.waitUntil(lambda: tip.property("visible"), timeout=5000)
            assert tip.property("contentItem").property("text") == studio.recordBlocker
    finally:
        model.shutdown()

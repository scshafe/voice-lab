"""The window, driven like a person would (offscreen; any Qt/QML warning fails a test)."""

from __future__ import annotations

import threading

import httpx
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from PySide6.QtTest import QTest

import scshafe_qt
from voice_lab_desktop.cli import qml_path
from voice_lab_desktop.measurement.controller import Controller
from voice_lab_desktop.measurement.simulated import BUILT_IN, SimulatedBackend, SimulatedLevelReader
from voice_lab_desktop.tokens import MemoryStore


def make_ui(qtbot, config, store, backend, levels, **kwargs):
    controller = Controller(config, store, backend, levels, **kwargs)
    engine = QQmlApplicationEngine()
    scshafe_qt.register(engine)
    engine.rootContext().setContextProperty("app", controller)
    engine.load(str(qml_path()))
    assert engine.rootObjects(), "Main.qml failed to load"
    window = engine.rootObjects()[0]
    qtbot.waitExposed(window)
    window.requestActivate()
    controller.start()
    return controller, window, engine


def close(controller, window, engine):
    # QML goes first: bindings must never outlive the controller they read.
    window.close()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    controller.shutdown()


@pytest.fixture
def ui(qtbot, config, signed_in, backend, levels, pinned):
    controller, window, engine = make_ui(qtbot, config, signed_in, backend, levels)
    qtbot.waitUntil(lambda: controller.property("canStart"), timeout=10000)
    yield controller, window
    close(controller, window, engine)


def find(window, name: str) -> QObject:
    obj = window.findChild(QObject, name)
    assert obj is not None, name
    return obj


def space(window) -> None:
    QTest.keyClick(window, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)


def test_a_session_from_the_keyboard_is_saved_and_uploaded(qtbot, ui, intake):
    controller, window = ui
    controller.setSkipFree(True)
    assert find(window, "skipFreeButton").property("checked")
    QTest.mouseClick(window, Qt.MouseButton.LeftButton)  # focus the window, not a field
    controller.startSession()
    qtbot.waitUntil(lambda: controller.property("phase") == "segment", timeout=10000)
    assert find(window, "sessionTitle").property("text").startswith("Session s-")
    while controller.property("phase") == "segment":
        mode = controller.property("currentMode")
        space(window)
        qtbot.waitUntil(lambda: controller.property("phase") == "recording" or mode == "timed", timeout=5000)
        if mode == "manual":
            qtbot.waitUntil(lambda: controller.ritual.recording.seconds > 1.2, timeout=10000)
            assert find(window, "meterText").property("text").endswith("dBFS peak")
            space(window)
        qtbot.waitUntil(lambda: controller.property("phase") in ("segment", "review"), timeout=10000)
    assert controller.property("phase") == "review"
    assert [row["state"] for row in controller.property("segments")] == ["done"] * 5
    assert find(window, "finishButton").property("visible")
    controller.finishSession()
    assert controller.property("phase") == "done"
    qtbot.waitUntil(lambda: len(intake.accepted) == 1, timeout=20000)
    qtbot.waitUntil(lambda: controller.property("pendingCount") == 0 and not controller.property("uploadBusy"), timeout=10000)
    assert controller.property("uploadText") == "Every session is uploaded"


def test_without_a_umik_nothing_records(qtbot, config, signed_in, levels, pinned):
    controller, window, engine = make_ui(qtbot, config, signed_in, SimulatedBackend(devices=[BUILT_IN]), levels)
    try:
        qtbot.waitUntil(lambda: "No UMIK-1 found" in controller.property("deviceMessage"), timeout=10000)
        assert find(window, "deviceChip").property("text") == "No UMIK-1"
        assert "built-in and other microphones are refused" in find(window, "deviceMessage").property("text")
        assert not find(window, "startButton").property("enabled")
        assert find(window, "startBlocker").property("text") == "Plug in the UMIK-1 (see Microphone)."
    finally:
        close(controller, window, engine)


def test_an_unreadable_level_blocks_the_start(qtbot, config, signed_in, backend, pinned):
    levels = SimulatedLevelReader(unreadable="pw-dump is not installed")
    controller, window, engine = make_ui(qtbot, config, signed_in, backend, levels)
    try:
        qtbot.waitUntil(lambda: controller.property("levelState") == "unreadable", timeout=10000)
        assert "pw-dump is not installed" in find(window, "levelMessage").property("text")
        assert not find(window, "startButton").property("enabled") and not find(window, "pinButton").property("enabled")
    finally:
        close(controller, window, engine)


def test_pin_then_start(qtbot, config, signed_in, backend, levels, state):
    controller, window, engine = make_ui(qtbot, config, signed_in, backend, levels)
    try:
        qtbot.waitUntil(lambda: controller.property("levelState") == "unpinned", timeout=10000)
        assert find(window, "startBlocker").property("text") == "Pin the input level first."
        assert find(window, "pinButton").property("enabled")
        controller.pinLevel()
        assert controller.property("levelState") == "ok" and controller.property("canStart")
        assert state.pin().level.volume == 63
        levels.move_to(80)
        qtbot.waitUntil(lambda: controller.property("levelState") == "moved", timeout=10000)  # the setup poll sees it
        assert find(window, "pinButton").property("text") == "Re-pin…" and not controller.property("canStart")
    finally:
        close(controller, window, engine)


def test_the_level_moving_while_recording_aborts_the_session(qtbot, ui, levels, sessions, intake):
    controller, window = ui
    controller.startSession()
    qtbot.waitUntil(lambda: controller.property("phase") == "segment", timeout=10000)
    controller.toggleRecording()
    assert controller.property("phase") == "recording"
    levels.move_to(40)
    qtbot.waitUntil(lambda: controller.property("phase") == "aborted", timeout=10000)
    assert find(window, "abortBanner").property("visible")
    assert "40%" in controller.property("abortReason")
    assert sessions.pending() == [] and intake.requests == []


def test_escape_opens_the_abort_dialog(qtbot, ui):
    controller, window = ui
    controller.startSession()
    qtbot.waitUntil(lambda: controller.property("phase") == "segment", timeout=10000)
    QTest.keyClick(window, Qt.Key.Key_Escape)
    dialog = find(window, "abortDialog")
    qtbot.waitUntil(lambda: dialog.property("visible"), timeout=5000)
    controller.abortSession()
    dialog.close()
    assert controller.property("phase") == "aborted" and controller.property("abortReason") == "stopped by you"
    controller.newSession()
    qtbot.waitUntil(lambda: controller.property("canStart"), timeout=10000)


def test_settings_are_saved_to_the_file(qtbot, ui, config):
    controller, _window = ui
    assert controller.saveSettings("https://voice-journey-next.colobus-stargazer.ts.net", 30)
    assert controller.property("server") == "https://voice-journey-next.colobus-stargazer.ts.net"
    assert 'distance_cm = 30' in config.file.read_text()
    assert not controller.saveSettings("http://not-https.example", 30)


def test_sign_in_from_the_window(qtbot, config, backend, levels, pinned, issuer):
    def open_url(url: str) -> None:
        threading.Thread(target=lambda: httpx.get(url, follow_redirects=True, timeout=10), daemon=True).start()

    store = MemoryStore()
    controller, window, engine = make_ui(qtbot, config, store, backend, levels, open_browser=open_url)
    try:
        assert find(window, "signInButton").property("text") == "Sign in"
        controller.signIn()
        qtbot.waitUntil(lambda: controller.property("signedIn"), timeout=15000)
        assert find(window, "accountChip").property("text") == "owner@example.test"
        assert store.tokens is not None
        controller.signOut()
        assert store.tokens is None and find(window, "signInButton").property("text") == "Sign in"
    finally:
        close(controller, window, engine)

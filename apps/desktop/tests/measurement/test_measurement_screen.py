"""The measurement screen in the main window, driven like a person would (offscreen; any
Qt/QML warning fails a test). voice-journey-capture's window tests, moved onto the screen:
the guard refusing to start, a whole session from the keyboard, the abort on a moved
level, redo, the meter. Plus what the screen adds: scanning only while shown, a session
that keeps running on another screen, PortAudio missing, ``--measure``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import AppRig, RecordingOutput, Window, offline_corpus, open_window
from PySide6.QtCore import Qt
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from PySide6.QtTest import QTest
from voice_lab_desktop import app as app_module
from voice_lab_desktop import cli
from voice_lab_desktop.app import AppModel
from voice_lab_desktop.measurement.controller import SETUP_POLL_MS
from voice_lab_desktop.measurement.devices import DeviceError
from voice_lab_desktop.measurement.simulated import (
    BUILT_IN,
    SimulatedBackend,
    SimulatedLevelReader,
)
from voice_lab_desktop.net import new_http
from voice_lab_desktop.tokens import MemoryStore, TokenStore


@contextmanager
def screen(
    qtbot: Any,
    config: Any,
    tokens: TokenStore,
    backend: Any,
    levels: Any,
    tmp_path: Path,
    **kwargs: Any,
) -> Iterator[Window]:
    """The main window, opened on the measurement screen."""
    model = AppModel(
        config,
        tokens,
        http=new_http(),
        output=RecordingOutput(),
        open_url=lambda url: None,
        measurement_backend=backend,
        level_reader=levels,
        start_screen="measurement",
        **{"corpus_factory": offline_corpus, **kwargs},
    )
    try:
        with open_window(AppRig(model, RecordingOutput(), [], None, tmp_path)) as window:
            qtbot.waitExposed(window.root)
            window.root.requestActivate()
            yield window
    finally:
        model.shutdown()


@pytest.fixture
def ui(
    qtbot: Any, config: Any, signed_in: Any, backend: Any, levels: Any, pinned: Any, tmp_path: Path
) -> Iterator[Window]:
    with screen(qtbot, config, signed_in, backend, levels, tmp_path) as window:
        qtbot.waitUntil(lambda: window.model.measurement.canStart, timeout=10000)
        yield window


def space(window: Window) -> None:
    QTest.keyClick(window.root, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)


def started(qtbot: Any, window: Window) -> Any:
    controller = window.model.measurement
    controller.startSession()
    qtbot.waitUntil(lambda: controller.phase == "segment", timeout=10000)
    return controller


def test_a_session_from_the_keyboard_is_saved_and_handed_to_uploads(
    qtbot: Any, ui: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, controller = ui, ui.model.measurement
    uploads = window.model.uploads
    handed: list[int] = []
    monkeypatch.setattr(uploads, "uploadAll", lambda: handed.append(1))
    controller.setSkipFree(True)
    assert window.find("skipFreeButton").property("checked")
    started(qtbot, window)
    assert window.find("sessionTitle").property("text").startswith("Session s-")
    while controller.phase == "segment":
        mode = controller.currentMode
        space(window)
        qtbot.waitUntil(
            lambda mode=mode: controller.phase == "recording" or mode == "timed", timeout=5000
        )
        if mode == "manual":
            qtbot.waitUntil(lambda: controller.ritual.recording.seconds > 1.2, timeout=10000)
            assert window.find("meterText").property("text").endswith("dBFS peak")
            space(window)
        qtbot.waitUntil(lambda: controller.phase in ("segment", "review"), timeout=10000)
    assert controller.phase == "review"
    assert [row["state"] for row in controller.segments] == ["done"] * 5
    assert window.find("finishButton").property("visible")
    controller.finishSession()
    assert controller.phase == "done"
    # Saved, and handed to Uploads: listed there and sent (the app is signed in).
    assert handed == [1]
    assert [row["state"] for row in uploads.rows] == ["pending"]
    assert uploads.rows[0]["sessionId"] == controller.sessionId


def test_a_saved_session_waits_for_a_sign_in(
    qtbot: Any,
    config: Any,
    backend: Any,
    levels: Any,
    pinned: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with screen(qtbot, config, MemoryStore(), backend, levels, tmp_path) as window:
        controller = window.model.measurement
        handed: list[int] = []
        monkeypatch.setattr(window.model.uploads, "uploadAll", lambda: handed.append(1))
        qtbot.waitUntil(lambda: controller.canStart, timeout=10000)
        controller.setSkipFree(True)
        started(qtbot, window)
        while controller.phase == "segment":
            controller.toggleRecording()
            if controller.currentMode == "manual":
                qtbot.waitUntil(lambda: controller.ritual.recording.seconds > 1.2, timeout=10000)
                controller.toggleRecording()
            qtbot.waitUntil(lambda: controller.phase in ("segment", "review"), timeout=10000)
        controller.finishSession()
        assert controller.phase == "done" and handed == []
        assert window.model.uploads.pendingCount == 1


def test_without_a_umik_nothing_records(
    qtbot: Any, config: Any, signed_in: Any, levels: Any, pinned: Any, tmp_path: Path
) -> None:
    backend = SimulatedBackend(devices=[BUILT_IN])
    with screen(qtbot, config, signed_in, backend, levels, tmp_path) as window:
        controller = window.model.measurement
        qtbot.waitUntil(lambda: "No UMIK-1 found" in controller.deviceMessage, timeout=10000)
        assert window.find("deviceChip").property("text") == "No UMIK-1"
        message = window.find("deviceMessage").property("text")
        assert "built-in and other microphones are refused" in message
        assert not window.find("startButton").property("enabled")
        blocker = window.find("startBlocker").property("text")
        assert blocker == "Plug in the UMIK-1 (see Microphone)."


def test_an_unreadable_level_blocks_the_start(
    qtbot: Any, config: Any, signed_in: Any, backend: Any, pinned: Any, tmp_path: Path
) -> None:
    levels = SimulatedLevelReader(unreadable="pw-dump is not installed")
    with screen(qtbot, config, signed_in, backend, levels, tmp_path) as window:
        controller = window.model.measurement
        qtbot.waitUntil(lambda: controller.levelState == "unreadable", timeout=10000)
        assert "pw-dump is not installed" in window.find("levelMessage").property("text")
        assert not window.find("startButton").property("enabled")
        assert not window.find("pinButton").property("enabled")


def test_pin_then_start(
    qtbot: Any,
    config: Any,
    signed_in: Any,
    backend: Any,
    levels: Any,
    state: Any,
    tmp_path: Path,
) -> None:
    with screen(qtbot, config, signed_in, backend, levels, tmp_path) as window:
        controller = window.model.measurement
        qtbot.waitUntil(lambda: controller.levelState == "unpinned", timeout=10000)
        assert window.find("startBlocker").property("text") == "Pin the input level first."
        assert window.find("pinButton").property("enabled")
        controller.pinLevel()
        assert controller.levelState == "ok" and controller.canStart
        assert state.pin().level.volume == 63
        levels.move_to(80)
        # The setup poll sees it while the screen is shown.
        qtbot.waitUntil(lambda: controller.levelState == "moved", timeout=10000)
        assert window.find("pinButton").property("text") == "Re-pin…"
        assert not controller.canStart


def test_the_level_moving_while_recording_aborts_the_session(
    qtbot: Any, ui: Window, levels: Any, sessions: Any, intake: Any
) -> None:
    controller = started(qtbot, ui)
    controller.toggleRecording()
    assert controller.phase == "recording"
    levels.move_to(40)
    qtbot.waitUntil(lambda: controller.phase == "aborted", timeout=10000)
    assert ui.find("abortBanner").property("visible")
    assert "40%" in controller.abortReason
    assert sessions.pending() == [] and intake.intake_requests == []  # nothing uploaded


def test_escape_opens_the_abort_dialog(qtbot: Any, ui: Window) -> None:
    controller = started(qtbot, ui)
    QTest.keyClick(ui.root, Qt.Key.Key_Escape)
    dialog = ui.find("abortDialog")
    qtbot.waitUntil(lambda: dialog.property("visible"), timeout=5000)
    # The dialog is open: Space must not start a segment behind it.
    assert not ui.find("spaceShortcut").property("enabled")
    controller.abortSession()
    dialog.close()
    assert controller.phase == "aborted" and controller.abortReason == "stopped by you"
    controller.newSession()
    qtbot.waitUntil(lambda: controller.canStart, timeout=10000)


def test_r_redoes_the_last_segment(qtbot: Any, ui: Window) -> None:
    controller = started(qtbot, ui)
    assert controller.currentMode == "timed"  # room silence stops by itself
    space(ui)
    qtbot.waitUntil(lambda: controller.phase == "segment" and controller.canRedo, timeout=10000)
    assert [row["state"] for row in controller.segments][:2] == ["done", "current"]
    assert ui.find("redoButton").property("enabled")
    QTest.keyClick(ui.root, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: not controller.canRedo, timeout=5000)
    assert [row["state"] for row in controller.segments][0] == "current"
    assert controller.currentIndex == 0


def test_the_meter_follows_the_recording(qtbot: Any, ui: Window) -> None:
    controller = started(qtbot, ui)
    space(ui)  # room silence: timed
    qtbot.waitUntil(lambda: controller.phase == "segment" and controller.currentIndex == 1)
    space(ui)  # /a/ soft: manual
    qtbot.waitUntil(lambda: controller.meterRms > 0 and controller.meterPeak > 0, timeout=10000)
    meter = ui.find("meter")
    assert meter.property("peak") == controller.meterPeak
    assert meter.property("rms") == controller.meterRms
    assert meter.property("clipping") is False
    assert meter.property("text") == controller.meterText
    assert ui.find("elapsedText").property("text").startswith("0:")
    space(ui)
    qtbot.waitUntil(lambda: controller.phase == "segment", timeout=10000)
    assert (controller.meterPeak, controller.meterRms) == (0.0, 0.0)


def test_the_devices_are_scanned_only_while_the_screen_is_shown(
    qtbot: Any, ui: Window, levels: Any
) -> None:
    model, controller = ui.model, ui.model.measurement
    model.navigate("status")
    reads = levels.reads
    qtbot.wait(int(SETUP_POLL_MS * 1.6))
    assert levels.reads == reads  # no poll while another screen is shown
    levels.move_to(70)
    assert controller.levelState == "ok"
    model.navigate("measurement")
    qtbot.waitUntil(lambda: controller.levelState == "moved", timeout=10000)
    assert levels.reads > reads


def test_a_session_keeps_running_on_another_screen(qtbot: Any, ui: Window) -> None:
    model, controller = ui.model, ui.model.measurement
    started(qtbot, ui)
    space(ui)  # room silence (timed)
    qtbot.waitUntil(lambda: controller.phase == "segment" and controller.currentIndex == 1)
    space(ui)  # /a/ soft: manual, recording
    assert controller.phase == "recording"
    entry = next(e for e in model.sidebar if e["key"] == "measurement")
    assert entry["status"] == "held"  # visible from every screen
    model.navigate("corpus")
    before = controller.ritual.recording.seconds
    qtbot.waitUntil(lambda: controller.ritual.recording.seconds > before + 0.5, timeout=10000)
    # The keyboard acts on the measurement screen only.
    assert not ui.find("spaceShortcut").property("enabled")
    space(ui)
    assert controller.phase == "recording"
    model.navigate("measurement")
    assert ui.find("spaceShortcut").property("enabled")
    space(ui)
    qtbot.waitUntil(lambda: controller.phase == "segment", timeout=10000)
    assert [row["state"] for row in controller.segments][:2] == ["done", "done"]
    controller.abortSession()
    entry = next(e for e in model.sidebar if e["key"] == "measurement")
    assert entry["status"] == "none"


def test_closing_the_app_mid_session_aborts_it_with_the_reason(
    qtbot: Any, config: Any, signed_in: Any, backend: Any, levels: Any, pinned: Any, tmp_path: Path
) -> None:
    with screen(qtbot, config, signed_in, backend, levels, tmp_path) as window:
        qtbot.waitUntil(lambda: window.model.measurement.canStart, timeout=10000)
        controller = started(qtbot, window)
        ritual = controller.ritual
    assert ritual.status == "aborted"
    record = (ritual.session_dir / "session.json").read_text()
    assert "the app was closed during the session" in record


def test_without_portaudio_the_screen_says_so(
    qtbot: Any,
    config: Any,
    signed_in: Any,
    levels: Any,
    pinned: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def missing() -> Any:
        calls.append(1)
        raise DeviceError("PortAudio is not installed (Arch: sudo pacman -S portaudio)")

    monkeypatch.setattr(app_module, "_portaudio_backend", missing)
    model = AppModel(
        config,
        signed_in,
        http=new_http(),
        output=RecordingOutput(),
        open_url=lambda url: None,
        corpus_factory=offline_corpus,
    )
    try:
        assert calls == []  # nothing loads PortAudio before the screen is shown
        with open_window(AppRig(model, RecordingOutput(), [], None, tmp_path)) as window:
            model.navigate("measurement")
            controller = model.measurement
            assert calls == [1]
            assert controller.deviceMessage.startswith("PortAudio is not installed")
            assert window.find("deviceChip").property("text") == "No UMIK-1"
            assert not controller.canStart
            controller.refreshDevices()  # nothing to scan: no crash
            controller.startSession()
            assert controller.phase == "setup"
    finally:
        model.shutdown()


def test_the_distance_comes_from_the_settings(qtbot: Any, ui: Window, config: Any) -> None:
    controller = ui.model.measurement
    assert "35 cm" in ui.find("distanceText").property("text")
    assert ui.model.saveSettings(config.server_url, config.issuer, config.client_id, 30)
    assert controller.distanceCm == 30 and ui.model.distanceCm == 30
    assert "30 cm" in ui.find("distanceText").property("text")
    assert "distance_cm = 30" in config.file.read_text()
    assert not ui.model.saveSettings(config.server_url, config.issuer, config.client_id, 2)
    assert controller.distanceCm == 30


def test_measure_opens_the_window_on_the_measurement_screen(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv("VOICE_LAB_DESKTOP_DATA", str(data_dir))
    monkeypatch.setenv("VOICE_LAB_DESKTOP_CONFIG", str(tmp_path / "config.toml"))
    seen: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        cli,
        "run",
        lambda config, fixed_port=False, start_screen=None: (
            seen.append((fixed_port, start_screen)) or 0
        ),
    )
    assert cli.main(["--measure"]) == 0
    assert cli.main([]) == 0
    assert seen == [(False, "measurement"), (False, None)]
    with pytest.raises(ValueError, match="no screen"):
        AppModel(
            cli.load_config(),
            MemoryStore(),
            http=new_http(),
            start_screen="nowhere",
        )


def test_the_cal_file_is_imported_from_the_screen(qtbot: Any, ui: Window, tmp_path: Path) -> None:
    controller = ui.model.measurement
    toasts: list[tuple[str, str]] = []
    controller.toast.connect(lambda text, tone: toasts.append((text, tone)))
    assert controller.calOk is False and "uncalibrated" in controller.calText
    junk = tmp_path / "junk.txt"
    junk.write_text("nope")
    controller.importCalFile(str(junk))
    assert toasts[-1][1] == "danger" and toasts[-1][0].startswith("Cal file not imported")
    cal = tmp_path / "7012345.txt"
    cal.write_text(
        '"Sens Factor =-1.4dB, SERNO: 7012345"\n' + "".join(f"{20 + i}\t0.1\n" for i in range(30))
    )
    controller.importCalFile("   ")  # nothing to import: nothing happens
    controller.importCalFile(cal.as_uri())  # a dropped file's URL works too
    assert toasts[-1] == ("Cal file 7012345.txt imported.", "ok")
    assert controller.calOk and "serial 7012345" in ui.find("calText").property("text")


def test_pinning_refuses_what_it_cannot_read_and_logs_a_re_pin(
    qtbot: Any,
    config: Any,
    signed_in: Any,
    backend: Any,
    levels: Any,
    pinned: Any,
    tmp_path: Path,
) -> None:
    with screen(qtbot, config, signed_in, backend, levels, tmp_path) as window:
        controller = window.model.measurement
        toasts: list[tuple[str, str]] = []
        controller.toast.connect(lambda text, tone: toasts.append((text, tone)))
        qtbot.waitUntil(lambda: controller.levelState == "ok", timeout=10000)
        levels.move_to(70)
        controller.pinLevel()  # a deliberate re-pin is a chain era event
        assert toasts[-1][1] == "warn" and "chain era event" in toasts[-1][0]
        assert "70%" in controller.pinnedText
        levels.unreadable = "amixer failed"
        controller.pinLevel()
        assert toasts[-1] == ("Not pinned: the level cannot be read (amixer failed).", "danger")
        assert controller.levelState == "unreadable" and not controller.canStart


def test_a_start_the_probe_refuses_goes_back_to_setup(
    qtbot: Any, config: Any, signed_in: Any, levels: Any, pinned: Any, tmp_path: Path
) -> None:
    backend = SimulatedBackend(speed=200, block_frames=4800, zeros=True)
    with screen(qtbot, config, signed_in, backend, levels, tmp_path) as window:
        controller = window.model.measurement
        toasts: list[tuple[str, str]] = []
        controller.toast.connect(lambda text, tone: toasts.append((text, tone)))
        qtbot.waitUntil(lambda: controller.canStart, timeout=10000)
        controller.setCalTone(True)
        assert window.find("calToneButton").property("checked")
        controller.startSession()
        qtbot.waitUntil(lambda: bool(toasts), timeout=10000)
        assert toasts[-1][1] == "danger" and "digital silence" in toasts[-1][0]
        assert controller.phase == "setup" and controller.canStart


def test_a_too_short_segment_is_recorded_again(qtbot: Any, ui: Window, backend: Any) -> None:
    controller = started(qtbot, ui)
    toasts: list[tuple[str, str]] = []
    controller.toast.connect(lambda text, tone: toasts.append((text, tone)))
    space(ui)  # room silence (timed)
    qtbot.waitUntil(lambda: controller.phase == "segment" and controller.currentIndex == 1)
    controller.redoSegment()  # back to room silence, then forward again
    space(ui)
    qtbot.waitUntil(lambda: controller.phase == "segment" and controller.currentIndex == 1)
    backend.speed = 1.0  # real time from here: a quick start and stop is a short take
    space(ui)  # /a/ soft, stopped at once: too short
    space(ui)
    qtbot.waitUntil(lambda: controller.phase == "segment", timeout=10000)
    assert toasts and toasts[-1][1] == "warn"
    assert controller.currentIndex == 1  # the same segment, to record again
    controller.setCalTone(True)  # options are fixed during a session
    assert controller.calTone is False

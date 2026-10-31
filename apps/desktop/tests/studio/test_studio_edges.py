"""Edge protection (D-058; the recording UX review's item 3): Speak shows only once 200 ms of the
take are confirmed (so its head has silence when the reader waits for it), and a stop while the
voice still sounds holds the microphone 300 ms more, so the last word is whole. A stop after
the voice stops at once: the core loop is no slower. Nothing ever starts a recording."""

from __future__ import annotations

from typing import Any

from conftest import Window
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder

from .conftest import wait_phase

QUIET, LOUD = 0.0001, 0.25


def start(qtbot: Any, window: Window, recorder: StudioRecorder) -> Any:
    """R: the stand-in's take, once it has started."""
    count = len(recorder.takes)
    QTest.keyClick(window.root, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    return recorder.last


def test_speak_shows_once_200_ms_are_confirmed(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio = studio_window.model.studio
    cue = studio_window.find("studioSpeakCue")
    assert not cue.isVisible()
    take = start(qtbot, studio_window, recorder)
    take.advance(4_800, amplitude=QUIET)  # 100 ms: live, not yet Speak
    wait_phase(qtbot, studio, "recording")
    qtbot.waitUntil(lambda: cue.isVisible(), timeout=5000)
    assert not studio.speak and cue.property("text") == "Wait…"
    take.advance(4_800, amplitude=QUIET)  # 200 ms
    qtbot.waitUntil(lambda: studio.speak, timeout=5000)
    assert cue.property("text") == "Speak"
    assert studio_window.find("studioRecord").property("cue")
    QTest.keyClick(studio_window.root, Qt.Key.Key_R)
    assert not studio.speak
    wait_phase(qtbot, studio, "review")
    assert not cue.isVisible()


def test_a_stop_while_the_voice_sounds_holds_its_tail(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio = studio_window.model.studio
    take = start(qtbot, studio_window, recorder)
    take.advance(48_000, amplitude=QUIET)  # a second of room
    wait_phase(qtbot, studio, "recording")
    take.advance(14_400, amplitude=LOUD)  # then the voice, still sounding
    qtbot.waitUntil(lambda: studio.takes.tail_voiced, timeout=5000)
    QTest.keyClick(studio_window.root, Qt.Key.Key_R)
    assert studio.phase == "stopping" and take.stops == 0  # the microphone stays open
    qtbot.waitUntil(lambda: take.stops == 1, timeout=5000)
    wait_phase(qtbot, studio, "review")


def test_a_stop_after_the_voice_stops_at_once(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio = studio_window.model.studio
    take = start(qtbot, studio_window, recorder)
    take.advance(48_000, amplitude=LOUD)  # the voice ...
    wait_phase(qtbot, studio, "recording")
    take.advance(14_400, amplitude=QUIET)  # ... then the room again
    # The live read has seen the quiet end.
    qtbot.waitUntil(lambda: studio.takes._live.frames >= 62_400, timeout=5000)
    assert not studio.takes.tail_voiced
    QTest.keyClick(studio_window.root, Qt.Key.Key_R)
    assert take.stops == 1  # at once: nothing held
    wait_phase(qtbot, studio, "review")

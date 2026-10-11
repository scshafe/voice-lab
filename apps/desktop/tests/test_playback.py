"""The player: decode in memory, shape (gain, low-pass), one take at a time, queues."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from conftest import RecordingOutput
from synthetic import tone_m4a
from voice_lab_desktop.playback import (
    REFEREE_LOWPASS_HZ,
    Clip,
    PlaybackError,
    Player,
    decode,
    shape,
)
from voice_lab_desktop.tasks import Tasks


def rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def test_a_memo_decodes_in_memory() -> None:
    clip = decode(tone_m4a(440.0, seconds=0.5))
    assert clip.rate == 44100
    assert clip.samples.dtype == np.float32 and clip.samples.shape[1] == 1
    assert 0.45 < clip.seconds < 0.6
    assert 0.3 < rms(clip.samples[2000:-2000]) < 0.4  # a 0.5 sine


def test_what_is_not_audio_is_refused() -> None:
    with pytest.raises(PlaybackError, match="empty"):
        decode(b"")
    with pytest.raises(PlaybackError, match="could not be decoded"):
        decode(b"this is not a recording")


def test_gain_and_the_low_pass() -> None:
    rate = 44100
    t = np.arange(rate, dtype=np.float32) / rate
    low = 0.25 * np.sin(2 * np.pi * 440 * t)
    high = 0.25 * np.sin(2 * np.pi * 12000 * t)
    clip = Clip((low + high).astype(np.float32).reshape(-1, 1), rate)
    filtered = shape(clip, lowpass_hz=REFEREE_LOWPASS_HZ)
    assert rms(filtered.samples) == pytest.approx(rms(low.reshape(-1, 1)), rel=0.02)
    quieter = shape(Clip(low.astype(np.float32).reshape(-1, 1), rate), gain_db=-6.0)
    assert rms(quieter.samples) == pytest.approx(rms(low) * 10 ** (-6 / 20), rel=0.01)
    louder = shape(Clip(np.full((10, 1), 0.9, np.float32), rate), gain_db=6.0)
    assert float(louder.samples.max()) == 1.0  # clipped, never overdriven


def test_one_take_at_a_time_and_queues(qtbot: Any) -> None:
    tasks = Tasks()
    memo = tone_m4a()
    fetched: list[str] = []

    def fetch(source: str) -> bytes:
        fetched.append(source)
        if source == "missing":
            raise PlaybackError("no such take")
        return memo

    output = RecordingOutput()
    player = Player(tasks, fetch, output)
    finished: list[str] = []
    failed: list[tuple[str, str]] = []
    player.finished.connect(finished.append)
    player.failed.connect(lambda key, message: failed.append((key, message)))
    try:
        player.play("corpus:a", "a")
        qtbot.waitUntil(lambda: player.state == "playing")
        assert player.activeKey == "corpus:a" and len(output.played) == 1
        assert 0.45 < player.duration < 0.6
        player.pause()
        assert player.state == "paused" and not output.running
        player.seek(0.25)
        assert player.position == pytest.approx(0.25, abs=0.01)
        player.resume()
        assert player.state == "playing" and output.frame == int(0.25 * 44100)
        player.play("corpus:b", "b")  # the next take stops the first
        qtbot.waitUntil(lambda: player.activeKey == "corpus:b" and player.state == "playing")
        output.finish()
        qtbot.waitUntil(lambda: finished == ["corpus:b"])
        assert player.state == "idle"

        player.play_queue([("era:0", "c"), ("era:0", "d")])
        qtbot.waitUntil(lambda: fetched[-1] == "c" and player.state == "playing")
        output.finish()
        qtbot.waitUntil(lambda: fetched[-1] == "d" and player.state == "playing")
        output.finish()
        qtbot.waitUntil(lambda: finished[-2:] == ["era:0", "era:0"] and player.state == "idle")

        player.play("corpus:x", "missing")
        qtbot.waitUntil(lambda: player.state == "failed")
        assert failed == [("corpus:x", "no such take")] and player.error == "no such take"
        player.stop()
        assert (player.state, player.activeKey, player.duration) == ("idle", "", 0.0)
    finally:
        tasks.close()


def test_the_player_bar_drives_the_one_player(qtbot: Any) -> None:
    import scshafe_qt
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from voice_lab_desktop.cli import qml_dir

    tasks = Tasks()
    output = RecordingOutput()
    memo = tone_m4a()
    player = Player(tasks, lambda source: memo, output)
    engine = QQmlApplicationEngine()
    scshafe_qt.register(engine)
    engine.addImportPath(str(qml_dir()))
    engine.rootContext().setContextProperty("player", player)
    screens = QUrl.fromLocalFile(str(qml_dir() / "screens")).toString()
    source = (
        f'import QtQuick\nimport "{screens}"\n'
        "Window { width: 600; height: 80; visible: true\n"
        '  PlayerBar { objectName: "a"; key: "referee:a"; source: "x"; gainDb: -6;'
        " lowpassHz: 8000 }\n"
        '  PlayerBar { objectName: "b"; key: "referee:b"; source: "y"; available: false;'
        ' unavailableText: "no playback" }\n}'
    )
    engine.loadData(source.encode())
    root = engine.rootObjects()[0]
    try:
        a = root.findChild(QObject, "a")
        b = root.findChild(QObject, "b")
        button = a.findChild(QObject, "playButton")
        assert button.property("text") == "Play"
        assert not b.findChild(QObject, "playButton").property("enabled")
        assert b.findChild(QObject, "playerUnavailable").property("visible")
        button.clicked.emit()
        qtbot.waitUntil(lambda: player.state == "playing")
        assert player.activeKey == "referee:a" and button.property("text") == "Pause"
        assert a.findChild(QObject, "playerTime").property("text") == "0:00 / 0:00"
        clean = shape(decode(memo), gain_db=-6.0, lowpass_hz=REFEREE_LOWPASS_HZ)
        assert np.allclose(output.played[-1].samples, clean.samples)
        button.clicked.emit()
        assert player.state == "paused" and button.property("text") == "Resume"
    finally:
        from voice_lab_desktop.cli import teardown

        teardown(engine, tasks.close)  # QML first: bindings must not outlive the player

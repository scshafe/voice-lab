"""Quality at review and Check Input (D-058; the recording UX review's item 1).

The flags never block: the core loop's test (``test_studio_core_loop.py``) runs with them.
Here: what each flag means on synthetic audio (``studio_fakes.voice_samples``: a room and a
voice, no person's), that the PC's QC is the PC's own function, that a verdict waits while a
take is recorded or saved and is kept by take id, that Inconsistent follows the session, and
that Check Input measures the room and a line without ever making a take."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder, voice_samples, write_wav
from voice_lab_desktop.screens import studio_check
from voice_lab_desktop.studio import audio, quality

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.finetune import (
    FINETUNE_PEAK_CEILING_DBFS,
    FINETUNE_TARGET_RMS,
    normalization_gain_db,
)

from .conftest import wait_phase
from .test_studio_core_loop import open_popups
from .test_studio_screen import click, prompt_ids


def wav_of(tmp_path: Path, samples: Any, name: str = "take.wav") -> tuple[bytes, Any]:
    """``samples`` as a canonical WAV's bytes, and as the studio decodes them."""
    path = tmp_path / name
    write_wav(path, samples)
    data = path.read_bytes()
    return data, audio.read_take_bytes(data)


def verdict(tmp_path: Path, samples: Any, **options: Any) -> quality.Verdict:
    data, decoded = wav_of(tmp_path, samples)
    options.setdefault("spoken", True)
    options.setdefault("expected_seconds", round(len(samples) / 48_000, 2))
    return quality.check(data, decoded, **options)


def codes(found: quality.Verdict) -> list[str]:
    return [flag.code for flag in found.flags]


# -- the checks -------------------------------------------------------------------------------


def test_an_ordinary_take_has_no_flags(tmp_path: Path) -> None:
    found = verdict(tmp_path, voice_samples(3.0))
    assert found.flags == () and found.pc_findings == ()
    levels = found.levels
    assert levels.speech_dbfs == pytest.approx(-40.0, abs=0.5)
    assert levels.floor_dbfs == pytest.approx(-80.0, abs=1.0)
    assert levels.snr_db == pytest.approx(40.0, abs=1.0)
    assert levels.seconds == pytest.approx(3.0)
    assert quality.describe(found).splitlines()[0] == "No flags."


def test_the_pcs_qc_is_the_pcs_own_rule(tmp_path: Path) -> None:
    ordinary = voice_samples(3.0)
    clipped = ordinary.copy()
    clipped[60_000:60_010] = -1.0
    dropout = ordinary.copy()
    dropout[50_000:65_000] = 0.0  # 312 ms of digital silence inside the voice
    quiet = voice_samples(3.0, speech_dbfs=-50.0)  # its peak is under −35 dBFS
    for name, samples in (
        ("ordinary", ordinary),
        ("clipped", clipped),
        ("dropout", dropout),
        ("quiet", quiet),
    ):
        data, _ = wav_of(tmp_path, samples, f"{name}.wav")
        pc = inspect_canonical_wav(tmp_path / f"{name}.wav").qc_metrics("original.wav")
        assert quality.pc_findings(data, spoken=True) == (pc.findings, pc.clipped_samples)
    # Clipping is its own flag (danger), never a second PC QC flag.
    found = verdict(tmp_path, clipped)
    assert codes(found) == ["clip"] and found.flags[0].tone == "danger"
    assert found.flags[0].detail == "Clipped: 10 samples at full scale."
    # The PC's other findings, in words.
    assert (
        verdict(tmp_path, dropout)
        .flags[0]
        .detail.startswith("The PC's QC will ask for a review: a dropout")
    )
    found = verdict(tmp_path, quiet)
    assert codes(found) == ["pc-qc"]
    assert found.flags[0].detail == "The PC's QC will ask for a review: a peak under −35 dBFS."


def test_a_peak_near_full_scale_is_clipping_too(tmp_path: Path) -> None:
    samples = voice_samples(3.0)
    samples *= 10 ** (-0.5 / 20) / np.max(np.abs(samples))
    found = verdict(tmp_path, samples)
    assert codes(found) == ["clip"]
    assert found.flags[0].detail == "Clipped: its peak is −0.5 dBFS, within 1 dB of full scale."


def test_low_snr_is_judged_against_the_rooms_floor(tmp_path: Path) -> None:
    found = verdict(tmp_path, voice_samples(3.0, floor_dbfs=-55.0))
    assert codes(found) == ["low-snr"]
    assert found.flags[0].detail == "The speech stands 15 dB over the room's floor (under 20)."
    # No voice at all (the microphone turned away): nothing stands over the room.
    silent = verdict(tmp_path, voice_samples(3.0, speech_dbfs=-200.0, floor_dbfs=-40.0))
    assert codes(silent) == ["low-snr"]
    assert silent.flags[0].detail == (
        "Nothing stands 10 dB over the take's quietest tenth of a second: no speech, or no "
        "pause to measure the room by."
    )
    # A level alone is never judged here: a quiet voice in a quiet room has a good SNR. Only
    # the PC's own rule (a peak under −35 dBFS) speaks of its level, as the PC will.
    quiet = verdict(tmp_path, voice_samples(3.0, speech_dbfs=-47.0, floor_dbfs=-90.0))
    assert codes(quiet) == ["pc-qc"] and quiet.pc_findings == ("peak-too-quiet",)


def test_inconsistent_against_the_sessions_recent_kept_takes(tmp_path: Path) -> None:
    take = voice_samples(3.0)  # −40 dBFS
    assert codes(verdict(tmp_path, take, recent=[-33.0, -33.0])) == []  # too few yet
    found = verdict(tmp_path, take, recent=[-33.0, -33.0, -33.0])
    assert codes(found) == ["inconsistent"]
    assert found.flags[0].detail == "7.0 dB quieter than your last 3 kept takes (their median)."
    louder = verdict(tmp_path, voice_samples(3.0, speech_dbfs=-33.0), recent=[-40.0] * 12)
    assert louder.flags[0].detail == "7.0 dB louder than your last 10 kept takes (their median)."
    assert codes(verdict(tmp_path, take, recent=[-46.0, -44.0, -40.0])) == []


def test_short_long_and_cut_edges(tmp_path: Path) -> None:
    take = voice_samples(1.2)
    assert codes(verdict(tmp_path, take, expected_seconds=3.0)) == ["short"]
    assert verdict(tmp_path, take, expected_seconds=3.0).flags[0].detail == (
        "1.2 s, for about 3.0 s expected."
    )
    assert codes(verdict(tmp_path, voice_samples(8.5), expected_seconds=3.0)) == ["long"]
    assert codes(verdict(tmp_path, voice_samples(7.5), expected_seconds=3.0)) == []
    for lead, tail, title in ((0.0, 0.4, "Cut start"), (0.4, 0.0, "Cut end"), (0, 0, "Cut edges")):
        take = voice_samples(3.0, lead=lead, tail=tail)
        take[60_000:75_000] = voice_samples(3.0, speech_dbfs=-200.0)[60_000:75_000]  # a pause
        found = verdict(tmp_path, take)
        assert [flag.title for flag in found.flags] == [title]
    assert verdict(tmp_path, voice_samples(3.0, lead=0.0)).flags[0].detail == (
        "Speech in the first 0.1 s: a word may be cut."
    )


def test_room_tone_is_judged_by_the_pcs_qc_and_its_length_only(tmp_path: Path) -> None:
    room = voice_samples(3.0, speech_dbfs=-200.0, floor_dbfs=-70.0)
    assert codes(verdict(tmp_path, room, spoken=False)) == []  # quiet is allowed, as on the PC
    assert codes(verdict(tmp_path, room, spoken=False, expected_seconds=8.0)) == ["short"]


def test_the_training_gain_is_finetunes() -> None:
    assert 10 ** (quality.TRAINING_RMS_DBFS / 20) == pytest.approx(FINETUNE_TARGET_RMS)
    assert quality.TRAINING_PEAK_CEILING_DBFS == FINETUNE_PEAK_CEILING_DBFS
    for rms in (-60.0, -50.0, -35.5, -20.0, -12.0):
        for peak in (-40.0, -25.0, -10.0, -3.0, -0.5):
            assert quality.training_gain_db(rms, peak) == normalization_gain_db(rms, peak)


def test_the_store_keeps_verdicts_privately(tmp_path: Path) -> None:
    path = tmp_path / "studio" / quality.STORE_FILE
    store = quality.QualityStore(path)
    assert store.peek("take-a") is None and store.get("take-a") is None
    found = verdict(tmp_path, voice_samples(3.0, lead=0.0))
    store.put("take-a", "prompt-1", found)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    again = quality.QualityStore(path)
    assert again.peek("take-a") is None  # not read yet: peek never reads the file
    again.warm()
    assert again.peek("take-a") == found and again.get("take-a") == found
    saved = json.loads(path.read_text())
    assert saved["takes"]["take-a"]["prompt_id"] == "prompt-1"
    # An unreadable or older file is read as empty: verdicts are made again.
    for text in ("{", json.dumps({"version": 0, "takes": {}}), json.dumps({"version": 1})):
        path.write_text(text)
        assert quality.QualityStore(path).get("take-a") is None


def test_check_input_levels_and_verdicts() -> None:
    def levels(**voice: Any) -> quality.CheckLevels:
        samples = voice_samples(9.0, lead=2.0, tail=0.0, **voice).astype(np.float32)
        return quality.check_levels(samples, quiet_seconds=2.0, settle_seconds=0.25)

    good = levels()
    assert good.floor_dbfs == pytest.approx(-80.0, abs=1.0)
    assert good.snr_db == pytest.approx(40.0, abs=1.0)
    assert good.verdict() == ("Good to record.", "ok")
    rows = {row["label"]: row["value"] for row in good.rows()}
    assert list(rows) == ["Room", "Speech", "SNR", "After the training gain", "Clipping"]
    assert rows["Clipping"] == "none"
    assert rows["After the training gain"].startswith("+20.0 dB: RMS −20.0 dBFS")
    assert levels(floor_dbfs=-70.0).verdict() == (
        "Good enough to record; a quieter room would help.",
        "info",
    )
    assert levels(floor_dbfs=-55.0).verdict()[1] == "warn"
    assert levels(speech_dbfs=-200.0).verdict() == (
        "No speech stood over the room: is the UMIK-1 facing you?",
        "warn",
    )
    assert levels(speech_dbfs=-1.0).verdict() == (
        "Clipping: move back from the microphone.",
        "danger",
    )
    # A gain the peak limits says so.
    spiky = voice_samples(9.0, lead=2.0, tail=0.0)
    spiky[200_000] = 0.8
    limited = quality.check_levels(spiky.astype(np.float32), quiet_seconds=2.0, settle_seconds=0.25)
    rows = {row["label"]: row["value"] for row in limited.rows()}
    assert rows["After the training gain"].endswith("(the peak limits it)")
    assert "(the peak limits it)" not in {row["value"] for row in good.rows()}


# -- in the studio ----------------------------------------------------------------------------


def expected(window: Window, prompt_id: str) -> float:
    return float(window.model.studio._by_id[prompt_id].prompt.expected_seconds)


def record_take(qtbot: Any, window: Window, recorder: StudioRecorder) -> None:
    studio = window.model.studio
    count = len(recorder.takes)
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")


def test_a_take_is_flagged_at_review_and_keeps_its_flags(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, data_dir: Path
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    seconds = expected(studio_window, studio.promptId)
    recorder.voice = lambda seed: voice_samples(seconds, lead=0.0, tail=seconds / 5, seed=seed)
    record_take(qtbot, studio_window, recorder)
    qtbot.waitUntil(lambda: takes.qualityState == "checked", timeout=5000)
    assert [flag["title"] for flag in takes.flags] == ["Cut start"]
    (chip,) = studio_window.items("studioFlag-edge")
    assert chip.isVisible() and chip.property("text") == "Cut start"
    # A click says why, with the levels; the keys wait while it is open.
    click(studio_window, "studioFlags")
    popup = studio_window.find("studioFlagsPopup")
    qtbot.waitUntil(lambda: popup.property("visible"), timeout=5000)
    detail = studio_window.find("studioFlagsDetail").property("text").splitlines()
    assert detail[0] == "Speech in the first 0.1 s: a word may be cut."
    assert detail[1].startswith("Speech −40.0 dBFS · room −")
    assert studio_window.find("studioScreen").property("menuOpen")
    popup.close()
    qtbot.waitUntil(lambda: open_popups(studio_window) == [], timeout=5000)
    # Saved, the take keeps its verdict (by its id), in the picker and in the store.
    studio.save()
    wait_phase(qtbot, studio, "ready")
    (take,) = studio.client.takes()
    qtbot.waitUntil(lambda: takes.selectedKey == take.take_id, timeout=5000)
    assert [flag["title"] for flag in takes.flags] == ["Cut start"]
    assert takes.history[0]["flags"] == "Cut start"
    assert takes.takeOptions[0]["label"].endswith("· 1 flag")
    stored = json.loads((data_dir / "studio" / quality.STORE_FILE).read_text())
    assert stored["takes"][take.take_id]["prompt_id"] == studio.promptId
    assert [f["code"] for f in stored["takes"][take.take_id]["verdict"]["flags"]] == ["edge"]
    # Its level joined the session's (Inconsistent compares against it).
    assert takes.recent_levels == [pytest.approx(-40.0, abs=0.5)]


def test_a_verdict_waits_while_a_take_is_recorded_or_saved(
    qtbot: Any,
    studio_window: Window,
    recorder: StudioRecorder,
    pc: PC,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    ids = prompt_ids(pc)
    phases: list[str] = []
    check = quality.check

    def watched(*args: Any, **kwargs: Any) -> quality.Verdict:
        phases.append(studio.phase)  # the phase when a verdict starts
        return check(*args, **kwargs)

    monkeypatch.setattr(quality, "check", watched)
    recorder.hold_seal = True
    recorder.voice = lambda seed: voice_samples(expected(studio_window, studio.promptId), seed=seed)
    for index in range(3):
        studio.toggleRecord()
        qtbot.waitUntil(lambda index=index: len(recorder.takes) == index + 1, timeout=5000)
        recorder.last.advance(4_800)
        wait_phase(qtbot, studio, "recording")
        studio.toggleRecord()
        studio.saveNext()  # typed ahead: the save comes first, the verdict after it
        recorder.seal()
        qtbot.waitUntil(lambda index=index: studio.promptId == ids[index + 1], timeout=5000)
    qtbot.waitUntil(lambda: len(takes.recent_levels) == 3, timeout=10000)
    assert len(phases) == 3 and not {"starting", "recording", "stopping", "saving"} & set(phases)
    store = quality.QualityStore(Path(studio.quality_store.path))
    assert all(store.get(take.take_id) is not None for take in studio.client.takes())


def test_inconsistent_follows_the_session(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    levels = iter((-40.0, -40.0, -40.0, -33.0))
    recorder.voice = lambda seed: voice_samples(
        expected(studio_window, studio.promptId),
        speech_dbfs=next(levels),
        floor_dbfs=-90.0,
        seed=seed,
    )
    for _ in range(3):
        record_take(qtbot, studio_window, recorder)
        studio.saveNext()
        wait_phase(qtbot, studio, "ready")
    qtbot.waitUntil(lambda: len(takes.recent_levels) == 3, timeout=10000)
    record_take(qtbot, studio_window, recorder)
    qtbot.waitUntil(lambda: takes.qualityState == "checked", timeout=5000)
    assert [flag["code"] for flag in takes.flags] == ["inconsistent"]


def test_check_input_measures_the_room_and_a_line_never_a_take(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, data_dir: Path
) -> None:
    studio, check = studio_window.model.studio, studio_window.model.studio.check
    screen = studio_window.find("studioScreen")
    recorder.voice = lambda seed: voice_samples(9.0, lead=2.0, tail=0.0, seed=seed)
    # Opening it opens nothing else: the microphone waits for Start.
    click(studio_window, "studioCheckInput")
    dialog = studio_window.find("studioCheckDialog")
    qtbot.waitUntil(lambda: dialog.property("visible"), timeout=5000)
    assert recorder.takes == [] and screen.property("menuOpen")
    click(studio_window, "studioCheckStart")
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    take = recorder.last
    assert take.take_dir.parent == data_dir / "studio" / studio_check.CHECK_DIR
    assert studio.statusText == studio_check.NOT_RECORDING
    assert not studio.canRecord and not studio.canCheck
    # Quiet first, then the line; the check stops itself.
    take.advance(4_800, amplitude=0.0001)
    qtbot.waitUntil(lambda: check.stage == "quiet", timeout=5000)
    assert check.instruction == "Quiet, please"
    take.advance(96_000, amplitude=0.0001)
    qtbot.waitUntil(lambda: check.stage == "speak", timeout=5000)
    assert check.instruction == "Read this aloud"
    take.advance(336_000, amplitude=0.05)
    qtbot.waitUntil(lambda: check.stage == "done", timeout=10000)
    assert take.stops == 1 and not check.running
    assert check.summary == "Good to record." and check.summaryTone == "ok"
    rows = {row["label"]: row["value"] for row in check.results}
    assert float(rows["SNR"].split()[0]) == pytest.approx(40.0, abs=1.0)
    # Nothing was kept: no take, and the check's audio is gone.
    assert studio.client.takes() == []
    assert not take.take_dir.exists()
    assert studio.takes.room_floor_dbfs == pytest.approx(-80.0, abs=1.0)
    qtbot.waitUntil(
        lambda: studio.canRecord and studio.statusText == "Ready to record", timeout=5000
    )
    click(studio_window, "studioCheckClose")
    qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=5000)
    qtbot.waitUntil(lambda: screen.property("keysEnabled"), timeout=5000)


def test_stopping_a_check_keeps_nothing(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, check = studio_window.model.studio, studio_window.model.studio.check
    studio.openCheck()
    studio.startCheck()
    qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
    recorder.last.advance(4_800, amplitude=0.0001)
    qtbot.waitUntil(lambda: check.stage == "quiet", timeout=5000)
    QTest.keyClick(studio_window.root, Qt.Key.Key_Escape)  # the dialog's Escape: Stop
    qtbot.waitUntil(lambda: not check.running and not check.dialogOpen, timeout=5000)
    assert recorder.last.stops == 1 and check.results == [] and check.stage == "idle"
    assert not recorder.last.take_dir.exists()
    assert studio.client.takes() == [] and studio.canRecord

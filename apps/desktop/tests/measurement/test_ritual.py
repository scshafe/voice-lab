"""The ritual: capture.mjs's segments, 48 kHz / 24-bit WAVs, session.json, and the level guard around everything."""

from __future__ import annotations

import hashlib
import json
import re
import wave
from datetime import datetime

import pytest
from helpers import new_ritual, record_segment, record_session

from voice_lab_desktop import package
from voice_lab_desktop.measurement.ritual import RitualError, plan_segments, session_id_for
from voice_lab_desktop.measurement.simulated import UMIK, SimulatedBackend

# voice-journey src/capture.mjs SPINE_SEGMENTS + planSession, verbatim.
CAPTURE_MJS = [
    ("room-silence", "room silence — stand still, say nothing (noise floor)", "timed", 10),
    ("cal-tone", "94 dB calibrator tone on the capsule", "timed", 10),
    ("vowel-soft", "sustained /a/ — comfortable pitch, soft", "manual", None),
    ("vowel-loud", "sustained /a/ — same pitch, full voice", "manual", None),
    ("glide", "slow glide — bottom of range to top and back", "manual", None),
    ("anchor-song", "the anchor song — one verse, same song every session", "manual", None),
    ("free-practice", "free practice — everything else, mic keeps rolling", "manual", None),
]


@pytest.mark.parametrize("cal_tone,skip_free", [(False, False), (True, False), (False, True), (True, True)])
def test_plan_matches_capture_mjs(cal_tone, skip_free):
    expected = [s for s in CAPTURE_MJS if (s[0] != "cal-tone" or cal_tone) and (s[0] != "free-practice" or not skip_free)]
    plan = plan_segments(cal_tone, skip_free)
    assert [(p.key, p.label, p.mode, p.seconds) for p in plan] == expected
    assert [p.file for p in plan] == [f"{i + 1:02d}-{s[0]}.wav" for i, s in enumerate(expected)]
    assert all(package.FILE_NAME_RE.match(p.file) for p in plan)


def test_session_id_is_capture_mjs_shaped():
    sid = session_id_for(datetime(2026, 10, 3, 7, 5, 9))
    assert sid == "s-20261003-070509" and package.SESSION_ID_RE.match(sid)


def test_a_whole_session(backend, levels, pinned, sessions):
    ritual = record_session(backend, levels, pinned, sessions, skip_free=False)
    m = ritual.manifest
    assert m["schemaVersion"] == "voice-journey.capture-session.v1" and m["status"] == "completed"
    assert m["chainId"] == "umik1" and m["chainKind"] == "measurement" and m["controlled"] is True
    cap = m["capture"]
    assert (cap["sampleRate"], cap["bitDepth"], cap["channels"]) == (48000, 24, 1)
    assert cap["inputVolume"] == 63 and cap["distanceCm"] == 35 and cap["calFileSha256"] is None
    assert cap["levelPinnedAt"] and cap["inputLevel"]["source"] == "coreaudio" and cap["device"]["name"] == UMIK.name
    assert [s["key"] for s in m["segments"]] == ["room-silence", "vowel-soft", "vowel-loud", "glide", "anchor-song", "free-practice"]
    assert [f["name"] for f in m["files"]] == [s["file"] for s in m["segments"]]
    for seg, entry in zip(m["segments"], m["files"], strict=True):
        path = ritual.session_dir / entry["name"]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["sizeBytes"] == path.stat().st_size == seg["sizeBytes"]
        with wave.open(str(path)) as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 3, 48000)
            assert w.getnframes() == seg["frames"]
        assert seg["peakDbfs"] == pytest.approx(-24.0, abs=0.1) and seg["clippedSamples"] == 0
        assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", seg["startedAt"])
    silence = m["segments"][0]
    assert silence["frames"] == 480000 and silence["durationSeconds"] == 10.0  # exactly 10 s, not wall-clock
    package.validate_manifest(m)
    on_disk = json.loads((ritual.session_dir / "session.json").read_text())
    assert on_disk == m
    assert [s.upload_state for s in sessions.sessions()] == ["pending"]


def test_cal_tone_option_and_cal_file(backend, levels, pinned, sessions, tmp_path):
    cal = tmp_path / "7012345.txt"
    cal.write_text('"Sens Factor =-1.378dB, SERNO: 7012345"\n' + "".join(f"{20 + i * 10}.0\t{-0.1 * i:.2f}\n" for i in range(40)))
    pinned.import_cal_file(cal)
    ritual = record_session(backend, levels, pinned, sessions, cal_tone=True, skip_free=True)
    cap = ritual.manifest["capture"]
    assert cap["calFileSha256"] == hashlib.sha256(cal.read_bytes()).hexdigest()
    assert cap["calFile"] == "7012345.txt" and cap["calSerial"] == "7012345"
    assert [s["key"] for s in ritual.manifest["segments"]][:2] == ["room-silence", "cal-tone"]


def test_a_changed_cal_file_refuses_the_session(backend, levels, pinned, sessions, tmp_path):
    cal = tmp_path / "cal.txt"
    cal.write_text("".join(f"{20 + i}\t0.1\n" for i in range(20)))
    stored = pinned.import_cal_file(cal)
    stored.path.write_text("tampered")
    with pytest.raises(Exception, match="changed since it was imported"):
        new_ritual(backend, levels, pinned, sessions).begin()
    assert sessions.sessions() == []


def test_refused_when_not_pinned(backend, levels, state, sessions):
    with pytest.raises(RitualError, match="not pinned"):
        new_ritual(backend, levels, state, sessions).begin()
    assert sessions.sessions() == [] and backend.opened == []


def test_refused_when_the_level_moved_before_the_session(backend, levels, pinned, sessions):
    levels.move_to(70)
    with pytest.raises(RitualError, match="input level is 70% .* pinned at 63%"):
        new_ritual(backend, levels, pinned, sessions).begin()
    assert sessions.sessions() == []
    log = [json.loads(line) for line in pinned.log_path.read_text().splitlines()]
    assert log[-1]["event"] == "session-start" and log[-1]["ok"] is False and log[-1]["level"]["volume"] == 70


def test_refused_when_the_level_cannot_be_read(backend, levels, pinned, sessions):
    levels.unreadable = "pw-dump exited 1"
    with pytest.raises(RitualError, match="cannot be read .*refused"):
        new_ritual(backend, levels, pinned, sessions).begin()
    assert sessions.sessions() == []


def test_refused_when_muted(backend, levels, pinned, sessions):
    from voice_lab_desktop.measurement.levels import InputLevel

    levels.level = InputLevel(63, (0.63,), True, "coreaudio", "VolumeScalar input element 0")
    with pytest.raises(RitualError, match="muted"):
        new_ritual(backend, levels, pinned, sessions).begin()


def test_digital_silence_is_refused(levels, pinned, sessions):
    with pytest.raises(RitualError, match="digital silence"):
        new_ritual(SimulatedBackend(speed=200, zeros=True), levels, pinned, sessions).begin()
    assert sessions.sessions() == []


def test_a_device_that_will_not_open_is_refused(levels, pinned, sessions):
    with pytest.raises(RitualError, match="cannot open"):
        new_ritual(SimulatedBackend(fail_open="cannot open at 48000 Hz"), levels, pinned, sessions).begin()


def test_level_moved_between_segments_aborts(backend, levels, pinned, sessions):
    ritual = new_ritual(backend, levels, pinned, sessions)
    ritual.begin()
    record_segment(ritual)
    levels.move_to(64)
    with pytest.raises(RitualError, match="input level is 64%"):
        ritual.start_segment()
    assert ritual.status == "aborted"
    saved = json.loads((ritual.session_dir / "session.json").read_text())
    assert saved["status"] == "aborted" and "64%" in saved["error"] and "files" not in saved
    assert len(saved["segments"]) == 1
    assert sessions.pending() == [] and sessions.sessions()[0].upload_state == "not-uploadable"


def test_level_moved_during_a_segment_aborts(backend, levels, pinned, sessions):
    ritual = new_ritual(backend, levels, pinned, sessions)
    ritual.begin()
    record_segment(ritual)
    ritual.start_segment()
    ritual.poll_level()  # unchanged: fine
    levels.move_to(50)
    with pytest.raises(RitualError):
        ritual.poll_level()
    assert ritual.status == "aborted" and ritual.recording is None


def test_level_moved_by_the_end_of_a_segment_aborts(backend, levels, pinned, sessions):
    ritual = new_ritual(backend, levels, pinned, sessions)
    ritual.begin()
    recording = ritual.start_segment()
    recording.finished.wait(10)
    levels.unreadable = "amixer exited 1"
    with pytest.raises(RitualError, match="cannot be read"):
        ritual.finish_segment()
    assert ritual.status == "aborted"


def test_a_too_short_manual_take_is_redone_and_redo_last(backend, levels, pinned, sessions):
    ritual = new_ritual(backend, levels, pinned, sessions)
    ritual.begin()
    record_segment(ritual)
    ritual.start_segment()
    with pytest.raises(RitualError, match="shorter than 1 s"):
        ritual.finish_segment()
    assert ritual.status == "open" and ritual.current.key == "vowel-soft"
    first = record_segment(ritual)
    assert first["key"] == "vowel-soft"
    ritual.redo_last()
    assert ritual.current.key == "vowel-soft" and not (ritual.session_dir / first["file"]).exists()
    again = record_segment(ritual, manual_seconds=2.0)
    assert again["durationSeconds"] >= 2.0


def test_complete_refuses_an_unfinished_session(backend, levels, pinned, sessions):
    ritual = new_ritual(backend, levels, pinned, sessions)
    ritual.begin()
    with pytest.raises(RitualError, match="not finished"):
        ritual.complete()


def test_input_overflows_are_counted(levels, pinned, sessions):
    backend = SimulatedBackend(speed=200, block_frames=4800, overflow_every=5)
    ritual = record_session(backend, levels, pinned, sessions, skip_free=True)
    assert ritual.manifest["segments"][0]["inputOverflows"] > 0


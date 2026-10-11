"""The level pin, its log, and the cal file."""

from __future__ import annotations

import hashlib
import json

import pytest

from voice_lab_desktop.measurement.levels import InputLevel
from voice_lab_desktop.measurement.pin import CalFileError, GuardError, LevelMoved, LevelMuted, NotPinned, StateStore, guard

LEVEL = InputLevel(63, (0.63,), False, "coreaudio", "VolumeScalar input element 0")


def test_guard():
    store_pin = None
    with pytest.raises(NotPinned, match="now 63%"):
        guard(store_pin, LEVEL)


def test_pin_log_and_repin_is_an_era_event(tmp_path):
    store = StateStore(tmp_path)
    previous, pin = store.set_pin(LEVEL, "Umik-1")
    assert previous is None and store.pin() == pin
    guard(store.pin(), LEVEL)
    moved = InputLevel(70, (0.7,), False, "coreaudio", "VolumeScalar input element 0")
    with pytest.raises(LevelMoved, match="70% .* pinned at 63%"):
        guard(store.pin(), moved)
    with pytest.raises(LevelMuted):
        guard(store.pin(), InputLevel(63, (0.63,), True, "coreaudio", "x"))
    previous, _ = store.set_pin(moved, "Umik-1")
    assert previous == pin
    events = [json.loads(line) for line in store.log_path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["pin", "re-pin (chain era event)"]
    assert events[1]["previous"]["volume"] == 63 and events[1]["level"]["volume"] == 70


def test_a_muted_input_cannot_be_pinned(tmp_path):
    with pytest.raises(LevelMuted):
        StateStore(tmp_path).set_pin(InputLevel(63, (0.63,), True, "alsa", "x"), "Umik-1")


def test_a_damaged_state_file_is_an_error_not_a_reset(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    with pytest.raises(GuardError, match="damaged"):
        StateStore(tmp_path).pin()


def cal_text(serial="7012345", rows=40) -> str:
    return f'"Sens Factor =-1.378dB, SERNO: {serial}"\n' + "".join(f"{10 + i * 25:.3f}\t{-0.05 * i:.3f}\n" for i in range(rows))


def test_import_cal_file(tmp_path):
    source = tmp_path / "7012345.txt"
    source.write_text(cal_text())
    store = StateStore(tmp_path / "data")
    cal = store.import_cal_file(source)
    assert cal.serial == "7012345" and cal.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert cal.path.read_bytes() == source.read_bytes() and cal.path.parent == tmp_path / "data" / "calibration"
    assert store.current_cal_sha256() == (cal, cal.sha256)
    source.unlink()  # the copy is what counts
    assert store.current_cal_sha256()[1] == cal.sha256


@pytest.mark.parametrize("content,match", [
    ("hello\nworld\n", "does not look like"),
    (cal_text(rows=5), "does not look like"),
    ("x" * (1024 * 1024 + 1), "too large"),
])
def test_import_cal_file_refuses_non_cal_files(tmp_path, content, match):
    source = tmp_path / "bad.txt"
    source.write_text(content)
    with pytest.raises(CalFileError, match=match):
        StateStore(tmp_path / "data").import_cal_file(source)
    with pytest.raises(CalFileError, match="cannot read"):
        StateStore(tmp_path / "data").import_cal_file(tmp_path / "missing.txt")


def test_a_changed_or_missing_cal_copy_is_an_error(tmp_path):
    source = tmp_path / "c.txt"
    source.write_text(cal_text())
    store = StateStore(tmp_path / "data")
    cal = store.import_cal_file(source)
    cal.path.write_text(cal_text(serial="9999999"))
    with pytest.raises(CalFileError, match="changed"):
        store.current_cal_sha256()
    cal.path.unlink()
    with pytest.raises(CalFileError, match="cannot be read"):
        store.current_cal_sha256()

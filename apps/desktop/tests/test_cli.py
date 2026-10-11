"""The command line: the smoke run, settings, devices, the pin, the cal file, the sessions,
and the import of voice-journey-capture's data."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from helpers import record_session
from voice_lab_desktop import cli
from voice_lab_desktop.measurement import levels as levels_module
from voice_lab_desktop.measurement.simulated import (
    BUILT_IN,
    SimulatedBackend,
    SimulatedLevelReader,
)

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, data_dir: Path, tmp_path: Path) -> Path:
    monkeypatch.setenv("VOICE_LAB_DESKTOP_DATA", str(data_dir))
    monkeypatch.setenv("VOICE_LAB_DESKTOP_CONFIG", str(tmp_path / "config.toml"))
    return data_dir


def test_smoke_runs_offscreen_in_a_temp_dir(env: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-m", "voice_lab_desktop", "--smoke"],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(SRC)},
    )
    assert run.returncode == 0, run.stderr
    assert "smoke: ok" in run.stdout
    assert list(env.iterdir()) == []  # the smoke session went to a temporary dir


def test_check_config_prints_the_settings(
    env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICE_LAB_DESKTOP_SERVER", "https://voice-lab.example.ts.net")
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    assert cli.main(["--check-config"]) == 1  # no issuer, and no keychain in the test
    out = capsys.readouterr().out
    assert "server_url  https://voice-lab.example.ts.net" in out
    assert "issuer      (not set)" in out
    assert "client_id   voice-lab-desktop" in out and "keychain: NONE" in out
    assert "environment (VOICE_LAB_DESKTOP_SERVER)" in out


def test_a_bad_server_is_a_usage_error(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--server", "http://plain.example", "--sessions"]) == 2
    assert "must be https" in capsys.readouterr().err


def test_upload_needs_a_server(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--upload"]) == 2
    assert "no server is set" in capsys.readouterr().err


def test_devices_and_pin_level(
    env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = SimulatedLevelReader()
    monkeypatch.setattr(cli, "_real_backend", lambda: SimulatedBackend())
    monkeypatch.setattr(levels_module, "PlatformLevelReader", lambda: reader)
    assert cli.main(["--devices"]) == 1  # not pinned yet
    out = capsys.readouterr().out
    assert "Umik-1  Gain: 18dB (Core Audio, 1 ch, 48000 Hz) UMIK-1" in out
    assert "pin: the input level is not pinned" in out
    assert cli.main(["--pin-level"]) == 0
    assert "pinned at 63%" in capsys.readouterr().out
    assert (env / "measurement" / "level-pins.jsonl").exists()
    assert cli.main(["--devices"]) == 0 and "pin: matches" in capsys.readouterr().out
    reader.move_to(64)
    assert cli.main(["--pin-level"]) == 0
    out = capsys.readouterr().out
    assert "re-pinned at 64%" in out and "chain era event" in out
    monkeypatch.setattr(cli, "_real_backend", lambda: SimulatedBackend(devices=[BUILT_IN]))
    assert cli.main(["--devices"]) == 1
    assert "No UMIK-1 found" in capsys.readouterr().out


def test_import_cal_and_sessions(
    env: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    backend: Any,
    levels: Any,
    pinned: Any,
    sessions: Any,
) -> None:
    cal = tmp_path / "7012345.txt"
    cal.write_text(
        '"Sens Factor =-1.4dB, SERNO: 7012345"\n' + "".join(f"{20 + i}\t0.1\n" for i in range(30))
    )
    assert cli.main(["--import-cal", str(cal)]) == 0
    assert "serial 7012345" in capsys.readouterr().out
    (tmp_path / "junk.txt").write_text("nope")
    assert cli.main(["--import-cal", str(tmp_path / "junk.txt")]) == 1
    assert cli.main(["--sessions"]) == 0 and "no local sessions" in capsys.readouterr().out
    ritual = record_session(backend, levels, pinned, sessions, skip_free=True)
    assert cli.main(["--sessions"]) == 0
    assert f"{ritual.session_id}  completed  pending" in capsys.readouterr().out
    sessions.mark_failed(ritual.session_dir, "refused")
    assert cli.main(["--retry-failed"]) == 0
    assert "1 session(s) pending again" in capsys.readouterr().out
    sessions.mark_accepted(ritual.session_dir, "https://x")
    assert cli.main(["--prune-accepted"]) == 0 and not ritual.session_dir.exists()


def test_import_capture_data_copies_and_never_overwrites(
    env: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    backend: Any,
    levels: Any,
) -> None:
    from voice_lab_desktop.measurement.pin import StateStore
    from voice_lab_desktop.measurement.simulated import UMIK
    from voice_lab_desktop.measurement.store import SessionStore

    old = tmp_path / "home" / ".local" / "share" / "voice-journey-capture"
    old_state, old_sessions = StateStore(old), SessionStore(old)
    old_state.set_pin(levels.read(UMIK), UMIK.name)
    recorded = record_session(backend, levels, old_state, old_sessions, skip_free=True)
    assert cli.main(["--import-capture-data"]) == 0
    out = capsys.readouterr().out
    assert "copied state.json" in out and "copied sessions" in out
    target = env / "measurement"
    assert StateStore(target).pin() == old_state.pin()
    assert (target / "level-pins.jsonl").read_bytes() == (old / "level-pins.jsonl").read_bytes()
    assert [s.session_id for s in SessionStore(target).sessions()] == [recorded.session_id]
    assert (old / "sessions" / recorded.session_id).is_dir()  # copied, never moved
    assert cli.main(["--import-capture-data", str(old)]) == 1
    assert "already holds" in capsys.readouterr().err
    assert cli.main(["--import-capture-data", str(tmp_path / "nowhere")]) == 1

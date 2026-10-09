"""The command line: the smoke run, settings, devices, the pin, the cal file, the session list."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from helpers import record_session

from voice_journey_capture import cli, levels
from voice_journey_capture.simulated import BUILT_IN, SimulatedBackend, SimulatedLevelReader


@pytest.fixture
def env(monkeypatch, data_dir, tmp_path):
    monkeypatch.setenv("VOICE_JOURNEY_CAPTURE_DATA", str(data_dir))
    monkeypatch.setenv("VOICE_JOURNEY_CAPTURE_CONFIG", str(tmp_path / "config.toml"))
    return data_dir


def test_smoke_runs_offscreen_in_a_temp_dir(env):
    run = subprocess.run([sys.executable, "-m", "voice_journey_capture", "--smoke"], capture_output=True, text=True, timeout=120,
                         env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert run.returncode == 0, run.stderr
    assert "smoke: ok" in run.stdout
    assert list(env.iterdir()) == []  # the smoke session went to a temporary dir


def test_check_config_prints_the_settings(env, capsys, monkeypatch):
    monkeypatch.setenv("VOICE_JOURNEY_CAPTURE_SERVER", "https://voice-journey-next.colobus-stargazer.ts.net")
    import keyring
    from keyring.backends import fail
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    assert cli.main(["--check-config"]) == 1  # no keychain in the test
    out = capsys.readouterr().out
    assert "server_url  https://voice-journey-next.colobus-stargazer.ts.net" in out
    assert "client_id   voice-journey-capture" in out and "keychain: NONE" in out
    assert "environment (VOICE_JOURNEY_CAPTURE_SERVER)" in out


def test_a_bad_server_is_a_usage_error(env, capsys):
    assert cli.main(["--server", "http://plain.example", "--sessions"]) == 2
    assert "must be https" in capsys.readouterr().err


def test_devices_and_pin_level(env, capsys, monkeypatch):
    reader = SimulatedLevelReader()
    monkeypatch.setattr(cli, "_real_backend", lambda: SimulatedBackend())
    monkeypatch.setattr(levels, "PlatformLevelReader", lambda: reader)
    assert cli.main(["--devices"]) == 1  # not pinned yet
    out = capsys.readouterr().out
    assert "Umik-1  Gain: 18dB (Core Audio, 1 ch, 48000 Hz) UMIK-1" in out and "pin: the input level is not pinned" in out
    assert cli.main(["--pin-level"]) == 0
    assert "pinned at 63%" in capsys.readouterr().out
    assert cli.main(["--devices"]) == 0 and "pin: matches" in capsys.readouterr().out
    reader.move_to(64)
    assert cli.main(["--pin-level"]) == 0
    out = capsys.readouterr().out
    assert "re-pinned at 64%" in out and "chain era event" in out
    monkeypatch.setattr(cli, "_real_backend", lambda: SimulatedBackend(devices=[BUILT_IN]))
    assert cli.main(["--devices"]) == 1
    assert "No UMIK-1 found" in capsys.readouterr().out


def test_import_cal_and_sessions(env, capsys, tmp_path, backend, levels, pinned, sessions):
    cal = tmp_path / "7012345.txt"
    cal.write_text('"Sens Factor =-1.4dB, SERNO: 7012345"\n' + "".join(f"{20 + i}\t0.1\n" for i in range(30)))
    assert cli.main(["--import-cal", str(cal)]) == 0
    assert "serial 7012345" in capsys.readouterr().out
    (tmp_path / "junk.txt").write_text("nope")
    assert cli.main(["--import-cal", str(tmp_path / "junk.txt")]) == 1
    assert cli.main(["--sessions"]) == 0 and "no local sessions" in capsys.readouterr().out
    ritual = record_session(backend, levels, pinned, sessions, skip_free=True)
    assert cli.main(["--sessions"]) == 0
    assert f"{ritual.session_id}  completed  pending" in capsys.readouterr().out
    sessions.mark_failed(ritual.session_dir, "refused")
    assert cli.main(["--retry-failed"]) == 0 and "1 session(s) pending again" in capsys.readouterr().out
    sessions.mark_accepted(ritual.session_dir, "https://x")
    assert cli.main(["--prune-accepted"]) == 0 and not ritual.session_dir.exists()

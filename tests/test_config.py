"""Settings: defaults, the file, the environment, and the Settings panel's writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_journey_capture.config import DEFAULT_CLIENT_ID, DEFAULT_ISSUER, DEFAULT_SERVER, Config, ConfigError, load_config, save_settings


def test_defaults_are_the_final_server(tmp_path):
    config = load_config(env={"VOICE_JOURNEY_CAPTURE_CONFIG": str(tmp_path / "none.toml"), "VOICE_JOURNEY_CAPTURE_DATA": str(tmp_path)})
    assert config.server_url == DEFAULT_SERVER == "https://voice-journey.colobus-stargazer.ts.net"
    assert config.issuer == DEFAULT_ISSUER == "https://id.colobus-stargazer.ts.net"
    assert config.client_id == DEFAULT_CLIENT_ID == "voice-journey-capture"
    assert config.distance_cm == 35 and config.bearer == "access" and config.data_dir == tmp_path and config.source == "defaults"


def test_file_then_environment(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text('server_url = "https://voice-journey-next.colobus-stargazer.ts.net/"\ndistance_cm = 30\nbearer = "id"\n')
    config = load_config(file, env={})
    assert config.server_url == "https://voice-journey-next.colobus-stargazer.ts.net" and config.distance_cm == 30 and config.bearer == "id"
    env = {"VOICE_JOURNEY_CAPTURE_SERVER": "https://other.example.ts.net", "VOICE_JOURNEY_CAPTURE_CLIENT_ID": "vjc-test"}
    config = load_config(file, env=env)
    assert config.server_url == "https://other.example.ts.net" and config.client_id == "vjc-test"
    assert config.env_keys == ("server_url", "client_id") and "environment" in config.source


def test_default_data_dir_is_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert Config().data_dir == tmp_path / "share" / "voice-journey-capture"


@pytest.mark.parametrize("text,match", [
    ('server_url = "http://voice-journey.example"\n', "must be https"),
    ('server_url = "https://h.example/api"\n', "without a path"),
    ('server_url = "https://user:pw@h.example"\n', "credentials"),
    ("distance_cm = 2\n", "distance_cm"),
    ('distance_cm = "35"\n', "whole number"),
    ('bearer = "cookie"\n', "bearer"),
    ('surprise = "x"\n', "unknown keys"),
    ("not toml", "config.toml"),
])
def test_bad_settings_are_refused(tmp_path, text, match):
    file = tmp_path / "config.toml"
    file.write_text(text)
    with pytest.raises(ConfigError, match=match):
        load_config(file, env={})


def test_loopback_http_is_allowed_for_tests():
    assert Config(server_url="http://127.0.0.1:8787", issuer="http://localhost:1").validated().server_url == "http://127.0.0.1:8787"


def test_save_settings_keeps_other_keys_and_respects_the_environment(tmp_path):
    file = tmp_path / "cfg" / "config.toml"
    config = load_config(env={"VOICE_JOURNEY_CAPTURE_CONFIG": str(file), "VOICE_JOURNEY_CAPTURE_DATA": str(tmp_path)})
    config = save_settings(config, {"server_url": "https://voice-journey-next.colobus-stargazer.ts.net", "distance_cm": 40})
    assert file.read_text() == 'server_url = "https://voice-journey-next.colobus-stargazer.ts.net"\ndistance_cm = 40\n'
    config = save_settings(config, {"distance_cm": 35})
    assert config.server_url == "https://voice-journey-next.colobus-stargazer.ts.net" and config.distance_cm == 35
    with pytest.raises(ConfigError, match="must be https"):
        save_settings(config, {"server_url": "ftp://x"})
    assert "ftp" not in file.read_text()
    pinned = load_config(file, env={"VOICE_JOURNEY_CAPTURE_SERVER": "https://env.example.ts.net"})
    with pytest.raises(ConfigError, match="VOICE_JOURNEY_CAPTURE_SERVER is set"):
        save_settings(pinned, {"server_url": "https://x.example"})
    kept = save_settings(pinned, {"distance_cm": 50})
    assert kept.server_url == "https://env.example.ts.net" and kept.distance_cm == 50
    assert isinstance(kept.data_dir, Path)

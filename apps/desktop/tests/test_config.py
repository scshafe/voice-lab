"""Settings: defaults, the file, the environment, and the Settings dialog's writes."""

from __future__ import annotations

from pathlib import Path

import pytest
from voice_lab_desktop.config import (
    DEFAULT_CLIENT_ID,
    Config,
    ConfigError,
    load_config,
    save_settings,
)


def test_the_defaults_name_no_server(tmp_path: Path) -> None:
    config = load_config(
        env={
            "VOICE_LAB_DESKTOP_CONFIG": str(tmp_path / "none.toml"),
            "VOICE_LAB_DESKTOP_DATA": str(tmp_path),
        }
    )
    assert (config.server_url, config.issuer) == ("", "")
    assert not config.configured
    assert config.client_id == DEFAULT_CLIENT_ID == "voice-lab-desktop"
    assert config.distance_cm == 35 and config.bearer == "access"
    assert config.data_dir == tmp_path and config.source == "defaults"
    assert config.measurement_dir == tmp_path / "measurement"
    with pytest.raises(ConfigError, match="no server is set"):
        config.require_server()
    with pytest.raises(ConfigError, match="no Pocket ID issuer is set"):
        config.require_issuer()


def test_file_then_environment(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(
        'server_url = "https://voice-lab.example.ts.net/"\n'
        'issuer = "https://id.example.ts.net"\n'
        'distance_cm = 30\nbearer = "id"\n'
    )
    config = load_config(file, env={})
    assert config.server_url == "https://voice-lab.example.ts.net"
    assert config.configured and config.distance_cm == 30 and config.bearer == "id"
    env = {
        "VOICE_LAB_DESKTOP_SERVER": "https://other.example.ts.net",
        "VOICE_LAB_DESKTOP_CLIENT_ID": "vld-test",
    }
    config = load_config(file, env=env)
    assert config.server_url == "https://other.example.ts.net" and config.client_id == "vld-test"
    assert config.env_keys == ("server_url", "client_id") and "environment" in config.source


def test_default_data_dir_is_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert Config().data_dir == tmp_path / "share" / "voice-lab-desktop"


@pytest.mark.parametrize(
    "text,match",
    [
        ('server_url = "http://voice-lab.example"\n', "must be https"),
        ('server_url = "https://h.example/api"\n', "without a path"),
        ('server_url = "https://user:pw@h.example"\n', "credentials"),
        ('issuer = "http://id.example"\n', "issuer must be https"),
        ("distance_cm = 2\n", "distance_cm"),
        ('distance_cm = "35"\n', "whole number"),
        ('bearer = "cookie"\n', "bearer"),
        ('client_id = "a b"\n', "client_id"),
        ('surprise = "x"\n', "unknown keys"),
        ("not toml", "config.toml"),
    ],
)
def test_bad_settings_are_refused(tmp_path: Path, text: str, match: str) -> None:
    file = tmp_path / "config.toml"
    file.write_text(text)
    with pytest.raises(ConfigError, match=match):
        load_config(file, env={})


def test_a_named_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no settings file"):
        load_config(tmp_path / "missing.toml", env={})


def test_loopback_http_is_allowed_for_tests() -> None:
    config = Config(server_url="http://127.0.0.1:8787", issuer="http://localhost:1").validated()
    assert config.server_url == "http://127.0.0.1:8787"


def test_save_settings_keeps_other_keys_and_respects_the_environment(tmp_path: Path) -> None:
    file = tmp_path / "cfg" / "config.toml"
    config = load_config(
        env={"VOICE_LAB_DESKTOP_CONFIG": str(file), "VOICE_LAB_DESKTOP_DATA": str(tmp_path)}
    )
    config = save_settings(
        config, {"server_url": "https://voice-lab.example.ts.net", "distance_cm": 40}
    )
    assert file.read_text() == 'server_url = "https://voice-lab.example.ts.net"\ndistance_cm = 40\n'
    config = save_settings(config, {"distance_cm": 35})
    assert config.server_url == "https://voice-lab.example.ts.net" and config.distance_cm == 35
    with pytest.raises(ConfigError, match="must be https"):
        save_settings(config, {"server_url": "ftp://x"})
    with pytest.raises(ConfigError, match="unknown settings"):
        save_settings(config, {"colour": "blue"})
    assert "ftp" not in file.read_text()
    pinned = load_config(file, env={"VOICE_LAB_DESKTOP_SERVER": "https://env.example.ts.net"})
    with pytest.raises(ConfigError, match="VOICE_LAB_DESKTOP_SERVER is set"):
        save_settings(pinned, {"server_url": "https://x.example"})
    kept = save_settings(pinned, {"distance_cm": 50})
    assert kept.server_url == "https://env.example.ts.net" and kept.distance_cm == 50
    assert isinstance(kept.data_dir, Path)

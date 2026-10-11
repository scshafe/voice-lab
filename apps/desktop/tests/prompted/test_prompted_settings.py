"""Record: prompts' settings: ``catalog_file`` and ``prompted_uploads`` (D-042)."""

from __future__ import annotations

from pathlib import Path

import pytest
from voice_lab_desktop.config import Config, ConfigError, load_config, save_settings


def test_prompted_settings_are_read_and_written(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text('catalog_file = "/somewhere/suite.json"\nprompted_uploads = true\n')
    config = load_config(file, {"VOICE_LAB_DESKTOP_DATA": str(tmp_path / "data")})
    assert config.catalog_file == "/somewhere/suite.json" and config.prompted_uploads is True
    assert config.prompted_dir == tmp_path / "data" / "prompted"
    saved = save_settings(config, {"prompted_uploads": False})
    assert saved.prompted_uploads is False
    assert "prompted_uploads = false\n" in file.read_text()
    assert Config().prompted_uploads is False and Config().catalog_file == ""

    file.write_text('prompted_uploads = "yes"\n')
    with pytest.raises(ConfigError, match="prompted_uploads must be a true or false"):
        load_config(file, {})
    file.write_text("catalog_file = true\n")
    with pytest.raises(ConfigError, match="catalog_file must be a string"):
        load_config(file, {})
    file.write_text("distance_cm = true\n")
    with pytest.raises(ConfigError, match="distance_cm must be a whole number"):
        load_config(file, {})
    with pytest.raises(ConfigError, match="prompted_uploads must be true or false"):
        Config(prompted_uploads="yes").validated()  # type: ignore[arg-type]

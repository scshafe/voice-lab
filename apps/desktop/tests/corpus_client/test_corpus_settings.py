"""The corpus client's setting, ``corpus_sync`` (off until spike S-1 passed, D-042's reason),
and the client the app builds from its own settings, sign-in and task pool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from corpus_rig import PC, signed_in
from voice_lab_desktop.config import Config, ConfigError, load_config, save_settings
from voice_lab_desktop.corpus.client import CorpusClient
from voice_lab_desktop.tasks import Tasks
from voice_lab_desktop.viewmodel import AppContext


def test_corpus_sync_is_off_until_the_owner_turns_it_on(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text("corpus_sync = true\n")
    config = load_config(file, {})
    assert config.corpus_sync is True and config.corpus_dir == config.data_dir / "corpus"
    saved = save_settings(config, {"corpus_sync": False})
    assert saved.corpus_sync is False and "corpus_sync = false\n" in file.read_text()
    assert Config().corpus_sync is False
    file.write_text('corpus_sync = "yes"\n')
    with pytest.raises(ConfigError, match="corpus_sync must be a true or false"):
        load_config(file, {})
    with pytest.raises(ConfigError, match="corpus_sync must be true or false"):
        Config(corpus_sync="yes").validated()  # type: ignore[arg-type]


def test_the_apps_client_follows_its_settings(
    qtbot: Any, pc: PC, data_dir: Path, library: Any
) -> None:
    session = signed_in(pc.url, data_dir)
    config = Config(server_url=pc.url, data_dir=data_dir).validated()
    tasks = Tasks()
    context = AppContext(config, session, session.http, tasks, open_url=lambda _url: None)
    corpus = CorpusClient.from_context(context)
    try:
        assert corpus.root == data_dir / "corpus" and corpus.scheduler is not None
        result = corpus.run_pass()
        assert result.reached and result.gated and not corpus.status().enabled
        # Turned on in Settings: the next pass sends; this machine's approval names the
        # tailnet's home host, not this loopback server, so no upload starts (and says why).
        context.set_config(Config(server_url=pc.url, data_dir=data_dir, corpus_sync=True))
        result = corpus.run_pass()
        assert not result.gated and corpus.status().enabled
        assert result.upload_blocked.startswith(f"the settings name {pc.url}")
    finally:
        corpus.shutdown()
        tasks.close()

"""Open straight onto the next prompt (D-058; the recording UX review's item 7): signed in with a
catalog cached, the app opens on Record → Prompts where you left off (the prompt, the library's
Show, search and labels, the reading size), in the window's last frame, with zero clicks
before R; the window is titled with the prompt's id."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from conftest import RecordingOutput
from corpus_rig import PC, client
from fakes import fake_jwt
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder
from voice_lab_desktop.config import Config
from voice_lab_desktop.tokens import MemoryStore, Tokens

from .conftest import open_studio, ready, studio_app, wait_phase
from .test_studio_screen import prompt_ids


def signed_in_app(data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder) -> Any:
    """The app as a person starts it: signed in, configured, no start screen asked for."""
    from voice_lab_desktop.app import AppModel
    from voice_lab_desktop.measurement.simulated import SimulatedBackend, SimulatedLevelReader
    from voice_lab_desktop.net import new_http

    settings = Config(
        server_url=pc.url,
        issuer="https://id.example.test",
        data_dir=data_dir,
        file=tmp_path / "config.toml",
    ).validated()
    now = time.time()
    tokens = Tokens(
        access_token=fake_jwt({"sub": "cole", "exp": int(now) + 3600}),
        id_token=fake_jwt({"sub": "cole", "exp": int(now) + 3600, "email": "cole@example.test"}),
        expires_at=now + 3600,
        refresh_token=None,
    )
    return AppModel(
        settings,
        MemoryStore(tokens),
        http=new_http(),
        output=RecordingOutput(),
        open_url=lambda _url: None,
        measurement_backend=SimulatedBackend(speed=200, block_frames=4800),
        level_reader=SimulatedLevelReader(),
        recorder_factory=lambda: recorder,
        corpus_factory=lambda context: client(data_dir, pc, enabled=False, tasks=context.tasks),
    )


def test_the_next_start_is_where_you_left_off(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    ids = prompt_ids(pc)
    first = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, first, tmp_path) as window:
            ready(qtbot, first)
            studio = first.studio
            studio.selectPrompt(ids[5])
            qtbot.waitUntil(lambda: studio.promptId == ids[5], timeout=5000)
            studio.setShow("missing")
            studio.setSearch("neutral")
            studio.setReadingSize(28)
            first.saveWindow(40, 60, 1100, 700, False)
    finally:
        first.shutdown()
    view = json.loads((data_dir / "studio" / "view.json").read_text())
    assert (view["last_prompt"], view["show"], view["search"]) == (ids[5], "missing", "neutral")
    # The next start: signed in with the catalog cached, it opens on the studio, there.
    second = signed_in_app(data_dir, tmp_path, pc, recorder)
    try:
        assert second.screen == "prompts"
        with open_studio(qtbot, second, tmp_path) as window:
            ready(qtbot, second)
            studio = second.studio
            qtbot.waitUntil(lambda: studio.promptId == ids[5], timeout=5000)
            assert (studio.show, studio.searchText, studio.readingSize) == (
                "missing",
                "neutral",
                28,
            )
            root = window.root
            assert (root.width(), root.height()) == (1100, 700)
            assert root.property("title") == ids[5]
            # Zero clicks before R.
            assert window.find("studioScreen").property("keysEnabled")
            QTest.keyClick(root, Qt.Key.Key_R)
            qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
            recorder.last.advance(4_800)
            wait_phase(qtbot, studio, "recording")
            second.navigate("status")
            assert root.property("title") == "Status"
    finally:
        second.shutdown()


def test_without_a_catalog_the_app_opens_as_before(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    model = signed_in_app(data_dir, tmp_path, pc, recorder)
    try:
        assert model.screen == "corpus"  # no catalog cached yet: the studio cannot record
    finally:
        model.shutdown()


def test_a_frame_that_cannot_be_used_is_ignored(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    (data_dir / "window.json").write_text(json.dumps({"frame": {"x": 0, "y": 0, "width": 100}}))
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        assert model.windowGeometry == {}
    finally:
        model.shutdown()

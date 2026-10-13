"""Step 6c's polish: the header's text-size choice (§5), kept per viewer and still never
clipping a prompt; the take picker's short line, whole on the smallest window."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from studio_fakes import StudioRecorder

from .conftest import open_studio, ready, studio_app
from .test_studio_screen import assert_prompt_whole, click, prompt_ids, resize
from .test_studio_takes import recorded, saved


def test_the_text_size_is_chosen_and_kept(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    longest = max(pc.catalog[1].suite.prompts, key=lambda prompt: len(prompt.text or ""))
    assert longest.text is not None
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio = model.studio
            resize(qtbot, window, 1440, 900)
            card = window.find("studioScriptCard")
            assert studio.readingSize == 24 and card.property("readingSize") == 24
            # The header's choice: Extra large.
            click(window, "studioReadingSize")
            qtbot.waitUntil(lambda: bool(window.items("studioReadingSize-32")), timeout=5000)
            window.items("studioReadingSize-32")[0].clicked.emit()
            qtbot.waitUntil(lambda: studio.readingSize == 32, timeout=5000)
            assert window.find("studioReadingSize").property("displayText") == "Aa 32"
            studio.selectPrompt(prompt_ids(pc)[5])  # a short prompt: at the chosen size
            qtbot.waitUntil(lambda: card.property("readingSize") == 32, timeout=5000)
            # The longest prompt still shows whole, stepping down as it must.
            studio.selectPrompt(longest.prompt_id)
            assert 24 <= assert_prompt_whole(window, longest.text) <= 32
            resize(qtbot, window, 960, 600)
            assert assert_prompt_whole(window, longest.text) >= 16
    finally:
        model.shutdown()
    # Kept for this viewer (this machine's account), private.
    view = data_dir / "studio" / "view.json"
    assert json.loads(view.read_text()) == {"reading_size": 32}
    assert stat.S_IMODE(view.stat().st_mode) == 0o600
    assert stat.S_IMODE(view.parent.stat().st_mode) == 0o700
    again = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        assert again.studio.readingSize == 32
        again.studio.setReadingSize(30)  # not a size the header offers
        assert again.studio.readingSize == 32
    finally:
        again.shutdown()


def test_the_pickers_line_is_whole_on_the_smallest_window(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    takes = studio_window.model.studio.takes
    recorder.seconds = 2.0
    resize(qtbot, studio_window, 960, 600)
    line = studio_window.find("studioTakeSelectText")
    saved(qtbot, studio_window, recorder)
    recorded(qtbot, studio_window, recorder)  # "… · Not saved · here", the longest
    for key in [row["key"] for row in takes.history]:
        takes.selectTake(key)
        qtbot.waitUntil(lambda key=key: takes.selectedKey == key, timeout=5000)
        qtbot.waitUntil(
            lambda: (
                line.property("text")
                == takes.takeOptions[
                    [o["value"] for o in takes.takeOptions].index(takes.selectedKey)
                ]["label"]
            ),
            timeout=5000,
        )
        assert line.property("truncated") is False, line.property("text")
    assert "· 2.0 s · Not saved · here" in takes.takeOptions[0]["label"]

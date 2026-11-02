"""Step 6c's polish: the header's text-size choice (§5), kept per viewer and still never
clipping a prompt; the take picker's short line, whole on the smallest window."""

from __future__ import annotations

import json
import math
import stat
import threading
from pathlib import Path
from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from studio_fakes import StudioRecorder
from voice_lab_desktop.screens.studio_takes import HERE_UNREAD

from .conftest import open_studio, ready, studio_app, wait_phase
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
    assert json.loads(view.read_text())["reading_size"] == 32
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
    """Every take's line shows whole at 960 × 600, its flags' count included, in this host's
    font: the row keeps room for the widest line a take can have (the Mac's font is wider than
    the PC's), and there its three buttons fold into one menu."""
    takes = studio_window.model.studio.takes
    recorder.seconds = 2.0
    # The opening window holds the line and the three buttons.
    assert studio_window.find("studioRemoveTake").isVisible()
    assert not studio_window.find("studioTakeActions").isVisible()
    resize(qtbot, studio_window, 960, 600)
    assert studio_window.find("studioTakeActions").isVisible()
    for name in ("studioRemoveTake", "studioRejectTake", "studioRestoreTake"):
        assert not studio_window.find(name).isVisible(), name
    line = studio_window.find("studioTakeSelectText")
    widest = math.ceil(studio_window.find("studioTakeLineWidest").property("advanceWidth"))
    assert line.width() >= widest
    saved(qtbot, studio_window, recorder)
    recorded(qtbot, studio_window, recorder)
    # Both judged (the stand-in's takes are short and quiet): each line counts two flags.
    qtbot.waitUntil(
        lambda: [o["label"].endswith(" · 2 flags") for o in takes.takeOptions] == [True, True],
        timeout=10000,
    )
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
        assert line.property("implicitWidth") <= widest, line.property("text")
    assert takes.takeOptions[0]["label"].endswith(" · 2.0 s · Not saved · here · 2 flags")
    assert " · 2.0 s · Kept · here · 2 flags" in takes.takeOptions[1]["label"]
    # The library folded away, the row holds the line and the three buttons again.
    studio_window.model.studio.toggleLibrary()
    qtbot.waitUntil(lambda: studio_window.find("studioRemoveTake").isVisible(), timeout=5000)
    assert not studio_window.find("studioTakeActions").isVisible()
    assert line.property("truncated") is False


def test_the_folded_take_actions_decide_as_the_buttons_do(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio = studio_window.model.studio
    screen = studio_window.find("studioScreen")
    resize(qtbot, studio_window, 960, 600)
    assert not studio_window.find("studioTakeActions").isEnabled()  # no take: nothing applies
    take_id = saved(qtbot, studio_window, recorder)
    menu = studio_window.find("studioTakeActionsMenu")

    def decision() -> str:
        return str(studio.client.spool.take(take_id).sync.decision.decision)

    def choose(name: str) -> None:
        click(studio_window, "studioTakeActions")
        qtbot.waitUntil(lambda: menu.property("visible"), timeout=5000)
        assert screen.property("menuOpen") and not screen.property("keysEnabled")
        click(studio_window, name)
        qtbot.waitUntil(lambda: not menu.property("visible"), timeout=5000)

    def on() -> list[bool]:
        return [
            studio_window.find(f"studioTakeAction{name}").isEnabled()
            for name in ("Remove", "Reject", "Restore")
        ]

    assert on() == [True, True, False]
    choose("studioTakeActionRemove")
    qtbot.waitUntil(lambda: decision() == "removed", timeout=5000)
    assert on() == [False, False, True]
    choose("studioTakeActionRestore")
    qtbot.waitUntil(lambda: decision() == "kept", timeout=5000)
    # Reject… asks why, as the button does.
    choose("studioTakeActionReject")
    dialog = studio_window.find("studioRejectDialog")
    qtbot.waitUntil(lambda: dialog.property("visible"), timeout=5000)
    studio_window.items("studioRejectReason")[0].setProperty("text", "a chair creaked")
    studio_window.items("studioConfirmReject")[0].clicked.emit()
    qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=5000)
    assert decision() == "rejected"


def test_recovery_and_reads_at_start_run_off_the_gui_thread(
    qtbot: Any,
    data_dir: Path,
    tmp_path: Path,
    pc: PC,
    recorder: StudioRecorder,
    monkeypatch: Any,
) -> None:
    """The first listing of this machine's takes (which recovers what an earlier run left),
    the PC library cache's first parse and the partial WAV's reads while recording all run
    on workers: the window answers meanwhile and says it is checking; nothing records until
    the takes are listed."""
    from voice_lab_desktop.corpus.library import LibraryCache
    from voice_lab_desktop.corpus.spool import CorpusSpool
    from voice_lab_desktop.studio.audio import LiveWaveform

    gui = threading.get_ident()
    entered, gate = threading.Event(), threading.Event()
    threads: dict[str, set[int]] = {"recover": set(), "parse": set(), "partial": set()}
    recover, load, poll = CorpusSpool._recover, LibraryCache._load, LiveWaveform.poll

    def slow_recover(self: Any) -> Any:
        threads["recover"].add(threading.get_ident())
        entered.set()
        assert gate.wait(10)
        return recover(self)

    def watched_load(self: Any) -> Any:
        if not self.loaded:
            threads["parse"].add(threading.get_ident())
        return load(self)

    def watched_poll(self: Any) -> Any:
        threads["partial"].add(threading.get_ident())
        return poll(self)

    monkeypatch.setattr(CorpusSpool, "_recover", slow_recover)
    monkeypatch.setattr(LibraryCache, "_load", watched_load)
    monkeypatch.setattr(LiveWaveform, "poll", watched_poll)
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            studio = model.studio
            qtbot.waitUntil(entered.is_set, timeout=5000)
            qtbot.waitExposed(window.root)
            qtbot.waitUntil(
                lambda: studio.statusText == "Checking this machine's takes", timeout=5000
            )
            assert not studio.ready and not studio.canRecord
            assert not window.find("studioRecord").property("enabled")
            studio.toggleRecord()
            assert recorder.takes == []
            gate.set()
            qtbot.waitUntil(lambda: studio.ready and studio.canRecord, timeout=10000)
            ready(qtbot, model)
            studio.toggleRecord()
            qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
            recorder.last.advance(9_600)
            wait_phase(qtbot, studio, "recording")
            qtbot.waitUntil(lambda: len(studio.takes.bins) > 0, timeout=5000)
            assert threads["recover"] and threads["parse"] and threads["partial"]
            for kind, seen in threads.items():
                assert gui not in seen, kind
    finally:
        gate.set()
        model.shutdown()


def test_takes_that_cannot_be_listed_at_start_say_so(
    qtbot: Any,
    data_dir: Path,
    tmp_path: Path,
    pc: PC,
    recorder: StudioRecorder,
    monkeypatch: Any,
) -> None:
    from voice_lab_desktop.corpus.spool import CorpusSpool

    def unreadable(self: Any) -> None:
        raise PermissionError("the spool is not readable")

    monkeypatch.setattr(CorpusSpool, "_recover", unreadable)
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path):
            studio = model.studio
            qtbot.waitUntil(
                lambda: studio.statusText == "This machine's takes could not be read",
                timeout=10000,
            )
            assert studio.statusTone == "danger" and not studio.canRecord
            assert studio.problemText.endswith("the spool is not readable")
            # The prompt's history comes from the PC, but cannot be compared with the takes
            # here: the picker says so (never an error raised on the GUI thread).
            qtbot.waitUntil(lambda: studio.takes.historyNote != "", timeout=10000)
            assert studio.takes.historyNote == f"{HERE_UNREAD}: the spool is not readable"
    finally:
        model.shutdown()

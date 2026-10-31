"""Flagged prompts (D-058; the recording UX review's item 5): Show splits Needs attention into
Flagged here, Failed PC QC and Drafts only; Next Flagged (⇧⌘J) walks both kinds of flagged
prompt; a row recorded this session gets a badge once it is flagged; and the session's
flagged pass ("N prompts flagged this session · Review now") walks them with the usual loop:
a redo and Return clear a prompt and go to the next."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from studio_fakes import StudioRecorder, voice_samples
from voice_lab_desktop.studio import library as studio_library
from voice_lab_desktop.studio.library import PromptState

from .conftest import open_studio, ready, studio_app, wait_phase
from .test_studio_quality import expected
from .test_studio_screen import click, prompt_ids


def test_show_and_next_flagged_over_the_states() -> None:
    order = ("a", "b", "c", "d", "e")
    states = {
        "a": PromptState("kept"),
        "b": PromptState("needs-attention"),
        "c": PromptState("draft"),
        "d": PromptState("kept"),
        "e": PromptState("missing"),
    }
    flagged = frozenset({"d"})
    assert studio_library.next_flagged(order, flagged, states, None) == "b"
    assert studio_library.next_flagged(order, flagged, states, "b") == "d"
    assert studio_library.next_flagged(order, flagged, states, "d") == "b"  # wraps
    assert studio_library.next_flagged(order, frozenset(), {"a": PromptState("kept")}, "a") is None
    assert [o["value"] for o in studio_library.SHOW_OPTIONS] == [
        "all",
        "missing",
        "attention",
        "flagged",
        "failed",
        "drafts",
    ]


def test_the_take_that_stands_for_a_prompt_here() -> None:
    class Take:
        def __init__(self, take_id: str, prompt: str, decision: str, minute: int, remote=False):
            self.take_id, self.prompt_id, self.remote = take_id, prompt, remote
            self.requested_at = datetime(2026, 10, 30, tzinfo=UTC) + timedelta(minutes=minute)
            self.sync = type("S", (), {"decision": type("D", (), {"decision": decision})()})()

    takes = [
        Take("t1", "a", "kept", 1),
        Take("t2", "a", "draft", 5),  # a newer draft never stands before a kept take
        Take("t3", "a", "kept", 3),
        Take("t4", "b", "draft", 1),
        Take("t5", "b", "draft", 2),
        Take("t6", "c", "rejected", 9),
        Take("t7", "d", "kept", 9, remote=True),  # no audio here: no flags
    ]
    assert studio_library.standing_takes(takes) == {"a": "t3", "b": "t5"}  # type: ignore[arg-type]


def record_and_save(
    qtbot: Any, window: Window, recorder: StudioRecorder, *, moves: bool = True, **voice: Any
) -> str:
    """A take of the selected prompt, saved with Save & Next (``moves``: on to another
    prompt); the prompt it was of."""
    studio = window.model.studio
    prompt = studio.promptId
    seconds = expected(window, prompt)
    shape = {"tail": seconds / 5, **voice}
    recorder.voice = lambda seed: voice_samples(seconds, seed=seed, **shape)
    count = len(recorder.takes)
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    studio.saveNext()
    if moves:
        qtbot.waitUntil(lambda: studio.promptId != prompt, timeout=5000)
    else:
        wait_phase(qtbot, studio, "ready")
    return prompt


def test_flagged_here_shows_badges_and_is_reached_by_next_flagged(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    ids = prompt_ids(pc)
    clean = record_and_save(qtbot, studio_window, recorder)
    cut = record_and_save(qtbot, studio_window, recorder, lead=0.0)  # Cut start
    record_and_save(qtbot, studio_window, recorder)
    assert (clean, cut) == (ids[0], ids[1])
    flagged = "1 prompt flagged since you started"
    qtbot.waitUntil(lambda: studio.flaggedText == flagged, timeout=10000)
    rows = {row["promptId"]: row for row in studio.rowsModel.rows}
    assert rows[cut]["flag"] == "Flagged" and rows[clean]["flag"] == ""
    # Show: Flagged here lists it alone.
    studio.setShow("flagged")
    assert [row["promptId"] for row in studio.rowsModel.rows] == [cut]
    studio.setShow("drafts")
    assert studio.rowCount == 0
    studio.setShow("all")
    # ⇧⌘J reaches it, its flagged take selected.
    QTest.keyClick(
        studio_window.root,
        Qt.Key.Key_J,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    qtbot.waitUntil(lambda: studio.promptId == cut, timeout=5000)
    qtbot.waitUntil(lambda: [f["code"] for f in takes.flags] == ["edge"], timeout=5000)
    # A clean take kept stands for it now: it is no longer flagged.
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == 4, timeout=5000)
    recorder.voice = lambda seed: voice_samples(
        expected(studio_window, cut), seed=seed, tail=expected(studio_window, cut) / 5
    )
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    studio.save()
    wait_phase(qtbot, studio, "ready")
    qtbot.waitUntil(lambda: studio.flaggedText == "", timeout=10000)
    assert not studio.canNextFlagged
    studio.setShow("flagged")
    assert studio.rowCount == 0


def test_the_sessions_flagged_pass(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio = studio_window.model.studio
    first = record_and_save(qtbot, studio_window, recorder, lead=0.0)
    record_and_save(qtbot, studio_window, recorder)
    third = record_and_save(qtbot, studio_window, recorder, tail=0.0)
    flagged = "2 prompts flagged since you started"
    qtbot.waitUntil(lambda: studio.flaggedText == flagged, timeout=10000)
    # Review now: the first, and a quiet line saying where the pass is.
    click(studio_window, "studioReviewFlagged")
    qtbot.waitUntil(lambda: studio.promptId == first, timeout=5000)
    assert studio.passText == "Flagged pass · 1 of 2"
    # The usual loop clears it, and Save & Next goes on to the next flagged prompt.
    record_and_save(qtbot, studio_window, recorder)
    assert studio.promptId == third and studio.passText == "Flagged pass · 2 of 2"
    record_and_save(qtbot, studio_window, recorder, moves=False)
    qtbot.waitUntil(lambda: studio.passText == "", timeout=5000)
    assert studio.statusText == "Saved · the flagged pass is done"
    qtbot.waitUntil(lambda: studio.flaggedText == "", timeout=10000)


def test_a_failed_pc_qc_lands_as_a_badge_and_a_flag(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, library: Any
) -> None:
    recorder = StudioRecorder(simulated=False)  # a take the PC accepts, and judges
    model = studio_app(data_dir, tmp_path, pc, recorder, enabled=True)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio = model.studio
            ids = prompt_ids(pc)
            # Its peak is under −35 dBFS: the PC will ask for a review.
            quiet = record_and_save(qtbot, window, recorder, speech_dbfs=-50.0, floor_dbfs=-95.0)
            assert quiet == ids[0]
            qtbot.waitUntil(lambda: studio.client.status().queued == 0, timeout=30000)
            pc.publish()
            studio.syncNow()
            qtbot.waitUntil(lambda: studio._state(quiet).status == "needs-attention", timeout=30000)
            qtbot.waitUntil(
                lambda: (
                    {r["promptId"]: r for r in studio.rowsModel.rows}[quiet]["flag"]
                    == "Failed PC QC"
                ),
                timeout=10000,
            )
            studio.setShow("failed")
            assert [row["promptId"] for row in studio.rowsModel.rows] == [quiet]
            studio.setShow("all")
            studio.nextFlagged()
            qtbot.waitUntil(lambda: studio.promptId == quiet, timeout=5000)
    finally:
        model.shutdown()

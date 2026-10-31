"""Pacing (D-058; the recording UX review's item 6): the strip ("So far · …"), a quiet break hint
at about 30 minutes (never a dialog), and the match line, off by default: Check Input keeps the
first reading of its line on this machine and compares each later one with it."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from conftest import Window
from corpus_rig import PC
from studio_fakes import StudioRecorder, voice_samples
from voice_lab_desktop.screens import studio_check
from voice_lab_desktop.studio import pacing

from .conftest import wait_phase
from .test_studio_quality import expected
from .test_studio_screen import click, prompt_ids


class Clock:
    """Monotonic seconds a test moves on."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def minutes(self, count: float) -> None:
        self.now += 60 * count


def takes_for(clock: Clock, session: pacing.Pacing, minutes: int) -> None:
    """A take every three minutes, for ``minutes``."""
    for _ in range(minutes // 3):
        clock.minutes(3)
        session.take_started()


def test_the_clock_breaks_and_the_hint() -> None:
    clock = Clock()
    session = pacing.Pacing(clock)
    assert session.minutes() == 0 and not session.hint
    assert session.text(left=1_852, left_seconds=8_094) == (
        "So far · 0 kept · 0 redone · 1,852 left ≈ 2 h 15 min reading"
    )
    session.take_started()
    session.kept, session.redone = 42, 3
    takes_for(clock, session, 27)
    assert session.minutes() == 27 and not session.hint
    takes_for(clock, session, 3)
    clock.minutes(1)
    assert session.hint and session.minutes() == 31
    assert session.text(left=1_610, left_seconds=7_080) == (
        "So far · 42 kept · 3 redone · 31 min · 1,610 left ≈ 1 h 58 min reading"
    )
    session.dismiss_hint()  # back after another half hour
    assert not session.hint
    takes_for(clock, session, 30)
    assert session.hint
    # A break (ten minutes without a take) clears it; the next take starts the clock again.
    clock.minutes(10)
    assert session.on_break and not session.hint
    session.take_started()
    assert session.minutes() == 0 and not session.hint
    assert pacing.reading_text(40) == "≈ 40 s" and pacing.reading_text(720) == "≈ 12 min"


def record(qtbot: Any, window: Window, recorder: StudioRecorder, *, save: bool = True) -> None:
    studio = window.model.studio
    count = len(recorder.takes)
    studio.toggleRecord()
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    if save:
        studio.saveNext()
        wait_phase(qtbot, studio, "ready")


def test_the_strip_and_the_break_hint(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    studio = studio_window.model.studio
    clock = Clock()
    studio.pacing.clock = clock
    ids = prompt_ids(pc)
    total = sum(expected(studio_window, prompt) for prompt in ids)
    text = studio_window.find("studioSessionText")
    assert text.property("text") == (
        f"So far · 0 kept · 0 redone · {len(ids)} left {pacing.reading_text(total)} reading"
    )
    record(qtbot, studio_window, recorder)
    record(qtbot, studio_window, recorder, save=False)
    record(qtbot, studio_window, recorder)  # R in review: recorded again
    left = sum(expected(studio_window, prompt) for prompt in ids[2:])
    qtbot.waitUntil(
        lambda: (
            studio.sessionText == f"So far · 2 kept · 1 redone · 0 min · {len(ids) - 2} left "
            f"{pacing.reading_text(left)} reading"
        ),
        timeout=5000,
    )
    # Half an hour of takes on: a quiet line, never a dialog; OK puts it away.
    for _ in range(11):
        clock.minutes(3)
        studio.pacing.take_started()
    studio._update_pace()
    hint = studio_window.find("studioBreakHint")  # its row keeps its place, shown or not
    qtbot.waitUntil(lambda: hint.isVisible(), timeout=5000)
    assert studio.breakHint == pacing.HINT_TEXT and "33 min" in studio.sessionText
    assert not studio_window.find("studioScreen").property("menuOpen")
    click(studio_window, "studioDismissBreak")
    qtbot.waitUntil(lambda: not hint.isVisible() and studio.breakHint == "", timeout=5000)


def run_check(qtbot: Any, window: Window, recorder: StudioRecorder, **voice: Any) -> Any:
    """Check Input, start to end, over a stand-in whose reading has this voice."""
    studio, check = window.model.studio, window.model.studio.check
    recorder.voice = lambda seed: voice_samples(9.0, lead=2.0, tail=1.0, seed=seed, **voice)
    count = len(recorder.takes)
    studio.startCheck()
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    recorder.last.advance(436_800, amplitude=0.0001)
    qtbot.waitUntil(lambda: check.stage == "done", timeout=10000)
    return {row["label"]: (row["value"], row["tone"]) for row in check.results}


def test_the_match_line_is_off_by_default_then_compares(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, data_dir: Path
) -> None:
    studio, check = studio_window.model.studio, studio_window.model.studio.check
    match = data_dir / "studio" / studio_check.MATCH_DIR
    studio.openCheck()
    assert not studio.matchLine
    # Off: nothing is kept.
    rows = run_check(qtbot, studio_window, recorder)
    assert "Match line" not in rows and not match.exists()
    # On (and kept with the reading size, in the view file): the first reading is kept.
    click(studio_window, "studioMatchLine")
    qtbot.waitUntil(lambda: studio.matchLine, timeout=5000)
    studio.setReadingSize(28)
    view = json.loads((data_dir / "studio" / "view.json").read_text())
    assert view == {"reading_size": 28, "match_line": True}
    rows = run_check(qtbot, studio_window, recorder)
    assert rows["Match line"] == ("kept as your reference (on this machine only)", "")
    for name in (studio_check.REFERENCE_WAV, studio_check.REFERENCE_JSON):
        assert stat.S_IMODE((match / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(match.stat().st_mode) == 0o700
    assert check.referenceText.startswith("Your reference: ")
    # The next reading is compared with it: 7 dB quieter is worth a look.
    rows = run_check(qtbot, studio_window, recorder, speech_dbfs=-47.0)
    assert rows["Match line: level"] == ("7.0 dB quieter", "warn")
    assert rows["Match line: length"][0].endswith(("longer", "shorter"))
    level, _ = rows["Match line: SNR"]
    assert level.endswith("worse")
    # Play reference plays it; Forget reference deletes it.
    click(studio_window, "studioPlayReference")
    qtbot.waitUntil(
        lambda: studio_window.model.player.property("activeKey") == "check:reference",
        timeout=5000,
    )
    click(studio_window, "studioForgetReference")
    assert not match.exists() and check.referenceText == ""
    assert studio.client.takes() == []  # never a take
    # A new run keeps the choice.
    assert studio._load_view()["match_line"] is True


@pytest.mark.parametrize("bad", ["[", json.dumps([1]), json.dumps({"reading_size": 30})])
def test_a_view_file_that_cannot_be_read_gives_the_defaults(
    studio_window: Window, data_dir: Path, bad: str
) -> None:
    path = data_dir / "studio" / "view.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bad)
    view = studio_window.model.studio._load_view()
    assert view["reading_size"] == 24 and view["match_line"] is False

"""Check Input (D-058; the recording UX review's item 1): the UMIK-1 measured before a session.

An explicit, optional action, never a take: the operator opens it, presses **Start**, and the
capture worker (the same one a take uses, D-039) runs for ``QUIET_SECONDS`` of quiet and then
``SPEAK_SECONDS`` while the operator reads ``CHECK_LINE`` aloud. The microphone opens on Start
only and closes by itself at the end; the window says *Checking input · not recording* all the
while. What the worker wrote goes into ``<data dir>/studio/check/`` and is deleted once
measured: nothing enters the corpus, nothing is sent.

It shows the room's floor, the speech's level and peak, the SNR, clipping, and the level the
same speech would have after the training copies' gain (FINETUNE.md; ``quality.training_gain_db``),
with a one-line verdict. The floor is the session's reference beside each take's flags.

Every connection goes to a declared ``@Slot`` (#41).
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Final

from PySide6.QtCore import QTimer, Signal, Slot

from ..capture.types import PARTIAL_NAME, SAMPLE_RATE, RunningTake, TakeRecorder
from ..corpus.files import private_dir
from ..studio import audio, quality
from ..viewmodel import AppContext, ViewModel, qproperty

#: The quiet first, then the line read aloud.
QUIET_SECONDS: Final = 2.0
SPEAK_SECONDS: Final = 7.0
#: The quiet's first quarter second is left out (the room settling after the click).
SETTLE_SECONDS: Final = 0.25
#: Aesop's "The North Wind and the Sun", phonetics' usual passage: about five seconds read.
CHECK_LINE: Final = (
    "The North Wind and the Sun were disputing which was the stronger, "
    "when a traveller came along wrapped in a warm cloak."
)
POLL_MS: Final = 50
STOP_TIMEOUT_S: Final = 30.0
#: Under ``<data dir>/studio/``: what a check's worker writes, deleted once measured.
CHECK_DIR: Final = "check"
#: The status line while the microphone is open for a check.
NOT_RECORDING: Final = "Checking input · not recording"


class InputCheckModel(ViewModel):
    checkChanged = Signal()
    levelChanged = Signal()

    dialogOpen = qproperty(bool, "dialogOpen", checkChanged)
    #: idle, starting, quiet, speak, measuring, done or failed.
    stage = qproperty(str, "stage", checkChanged)
    running = qproperty(bool, "running", checkChanged)
    instruction = qproperty(str, "instruction", checkChanged)
    line = qproperty(str, "line", checkChanged)
    results = qproperty("QVariantList", "results", checkChanged)
    summary = qproperty(str, "summary", checkChanged)
    summaryTone = qproperty(str, "summaryTone", checkChanged)
    remainingText = qproperty(str, "remainingText", levelChanged)
    levelPeakDb = qproperty(float, "levelPeakDb", levelChanged)
    levelFloorDb = qproperty(float, "levelFloorDb", levelChanged)

    def __init__(self, context: AppContext, root: Path) -> None:
        super().__init__(
            context,
            {
                "dialogOpen": False,
                "stage": "idle",
                "running": False,
                "instruction": "",
                "line": CHECK_LINE,
                "results": [],
                "summary": "",
                "summaryTone": "neutral",
                "remainingText": "",
                "levelPeakDb": audio.FLOOR_DB,
                "levelFloorDb": audio.FLOOR_DB,
            },
        )
        self.root = root
        #: The last check's levels this run (the session's reference), or ``None``.
        self.levels: quality.CheckLevels | None = None
        self._running: RunningTake | None = None
        self._directory: Path | None = None
        self._live: audio.LiveWaveform | None = None
        self._live_reading = False
        self._stop_asked = False
        self._closed = False
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._on_poll)

    # -- what the studio tells it ------------------------------------------------------------

    def open(self) -> None:
        """The dialog, idle (or with the last results); the microphone stays closed."""
        self._set(self.checkChanged, dialogOpen=True)

    def shutdown(self) -> None:
        self._closed = True
        self._poll.stop()
        running, self._running = self._running, None
        if running is not None:
            running.stop()

    def start(self, recorder: TakeRecorder) -> None:
        """Open the microphone for the check (the studio allows it only between takes)."""
        if self._v["running"]:
            return
        directory = self.root / uuid.uuid4().hex
        try:
            shutil.rmtree(self.root, ignore_errors=True)  # what an earlier check left
            private_dir(directory)
        except OSError as exc:
            self._failed(f"The check could not start: {exc}")
            return
        self._directory = directory
        self._stop_asked = False
        self._live = audio.LiveWaveform(directory / PARTIAL_NAME)
        self._set(
            self.checkChanged,
            running=True,
            stage="starting",
            instruction="Starting the microphone",
            results=[],
            summary="",
            summaryTone="neutral",
        )
        self._set(self.levelChanged, remainingText="", levelPeakDb=audio.FLOOR_DB)

        def done(running: RunningTake | None, error: BaseException | None) -> None:
            if self._closed:
                if running is not None:
                    running.stop()
                return
            if error is not None or running is None:
                self._failed(f"The microphone did not start: {error or 'no worker'}")
                return
            self._running = running
            if self._stop_asked:
                running.stop()
            self._poll.start()

        self.context.tasks.submit(lambda: recorder.start(directory), done)

    @Slot()
    def cancel(self) -> None:
        """Close the dialog; a check running is stopped and its audio deleted unmeasured."""
        if self._v["running"]:
            self._stop_asked = True
            self._set(self.checkChanged, stage="idle", instruction="")
            if self._running is not None:
                self._running.stop()
        self._set(self.checkChanged, dialogOpen=False)

    # -- the check ----------------------------------------------------------------------------

    @Slot()
    def _on_poll(self) -> None:
        running = self._running
        if running is None:
            self._poll.stop()
            return
        seconds = running.confirmed_frames / SAMPLE_RATE
        stage = self._v["stage"]
        if self._stop_asked:
            pass
        elif seconds >= QUIET_SECONDS + SPEAK_SECONDS:
            self._stop_asked = True
            running.stop()
            self._set(self.checkChanged, stage="measuring", instruction="Measuring")
        elif seconds >= QUIET_SECONDS:
            if stage != "speak":
                self._set(self.checkChanged, stage="speak", instruction="Read this aloud")
        elif running.confirmed_frames > 0 and stage == "starting":
            self._set(self.checkChanged, stage="quiet", instruction="Quiet, please")
        if not self._stop_asked:
            left = QUIET_SECONDS if seconds < QUIET_SECONDS else QUIET_SECONDS + SPEAK_SECONDS
            self._set(self.levelChanged, remainingText=f"{max(0.0, left - seconds):.0f} s")
        self._read_level()
        if running.done:
            self._poll.stop()
            self._finish(running)

    def _read_level(self) -> None:
        live = self._live
        if live is None or self._live_reading:
            return
        self._live_reading = True

        def work() -> float:
            live.poll()
            return live.peak_dbfs

        def done(peak: float | None, error: BaseException | None) -> None:
            self._live_reading = False
            if error is not None:
                raise error
            if not self._closed and peak is not None and live is self._live:
                self._set(self.levelChanged, levelPeakDb=peak)

        self.context.tasks.submit(work, done)

    def _finish(self, running: RunningTake) -> None:
        directory, cancelled = self._directory, self._v["stage"] == "idle"

        def work() -> quality.CheckLevels | None:
            try:
                captured = running.wait(STOP_TIMEOUT_S)
                if cancelled:
                    return None
                return quality.check_levels(
                    audio.read_take(captured.path),
                    quiet_seconds=QUIET_SECONDS,
                    settle_seconds=SETTLE_SECONDS,
                )
            finally:
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)

        def done(levels: quality.CheckLevels | None, error: BaseException | None) -> None:
            self._running = self._live = None
            if self._closed:
                return
            if error is not None:
                self._failed(f"The check failed: {error}")
                return
            if levels is None:  # cancelled
                self._set(self.checkChanged, running=False, stage="idle", instruction="")
                return
            self.levels = levels
            summary, tone = levels.verdict()
            self._set(
                self.checkChanged,
                running=False,
                stage="done",
                instruction="",
                results=levels.rows(),
                summary=summary,
                summaryTone=tone,
            )
            self._set(self.levelChanged, remainingText="", levelPeakDb=audio.FLOOR_DB)

        self.context.tasks.submit(work, done)

    def _failed(self, message: str) -> None:
        self._running = self._live = None
        self._set(
            self.checkChanged,
            running=False,
            stage="failed",
            instruction="",
            results=[],
            summary=message,
            summaryTone="danger",
        )

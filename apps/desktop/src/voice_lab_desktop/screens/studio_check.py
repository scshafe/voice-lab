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

**The match line** (D-058, item 6), off by default (the studio's ``matchLine``): the first
check with it on keeps its reading of the line as the reference, in ``<data dir>/studio/match/``
(0600, never sent, never a take: the catalog holds no non-prompt material, D-017). Each later
check compares its level, SNR and length with it on the spot, to keep the voice the same on
day three as on day one; **Play reference** plays it, and **Forget reference** deletes it.

Every connection goes to a declared ``@Slot`` (#41).
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Final

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
#: Under ``<data dir>/studio/``: the match line's reference reading and its levels.
MATCH_DIR: Final = "match"
REFERENCE_WAV: Final = "reference.wav"
REFERENCE_JSON: Final = "reference.json"
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
    #: The match line's reference: "Your reference: 30 Oct 18:02", or "" (none).
    referenceText = qproperty(str, "referenceText", checkChanged)

    def __init__(self, context: AppContext, root: Path, *, match_root: Path) -> None:
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
                "referenceText": "",
            },
        )
        self.root = root
        self.match_root = match_root
        #: The match line is on (the studio's ``matchLine``, off by default).
        self.match_enabled = False
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
        self._set(self.checkChanged, dialogOpen=True, referenceText=self._reference_text())

    # -- the match line -----------------------------------------------------------------------

    def _reference(self) -> tuple[quality.CheckLevels, datetime] | None:
        """The match line's reference levels and when it was read, or ``None``."""
        try:
            data = json.loads((self.match_root / REFERENCE_JSON).read_text(encoding="utf-8"))
            levels = quality.CheckLevels.from_json(data["levels"])
            at = datetime.fromisoformat(data["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return levels, at

    def _reference_text(self) -> str:
        known = self._reference()
        if known is None:
            return ""
        return "Your reference: " + known[1].astimezone().strftime("%-d %b %H:%M")

    def _keep_reference(self, wav: Path, levels: quality.CheckLevels) -> None:
        """This reading becomes the match line's reference (a worker's call)."""
        private_dir(self.match_root)
        shutil.copyfile(wav, self.match_root / REFERENCE_WAV)
        os.chmod(self.match_root / REFERENCE_WAV, 0o600)
        record = {"levels": levels.to_json(), "at": datetime.now().astimezone().isoformat()}
        target = self.match_root / REFERENCE_JSON
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream)

    @Slot()
    def playReference(self) -> None:  # noqa: N802
        """Play the reference reading of the line (from the end of its quiet)."""
        path = self.match_root / REFERENCE_WAV
        if not path.is_file() or self._v["running"]:
            return
        start = round(QUIET_SECONDS * SAMPLE_RATE)

        def load() -> Any:
            samples = audio.read_take(path)
            return audio.clip(samples, min(start, int(samples.shape[0]) - 1), int(samples.shape[0]))

        self.context.player.play_clip("check:reference", load)

    @Slot()
    def forgetReference(self) -> None:  # noqa: N802
        """Delete the reference: the next check with the match line on keeps a new one."""
        if self._v["running"]:
            return
        self.context.player.stop()
        shutil.rmtree(self.match_root, ignore_errors=True)
        self._set(self.checkChanged, referenceText="")

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
        matching = self.match_enabled

        def work() -> tuple[quality.CheckLevels, list[dict[str, str]]] | None:
            try:
                captured = running.wait(STOP_TIMEOUT_S)
                if cancelled:
                    return None
                levels = quality.check_levels(
                    audio.read_take(captured.path),
                    quiet_seconds=QUIET_SECONDS,
                    settle_seconds=SETTLE_SECONDS,
                )
                rows = levels.rows()
                if matching:
                    reference = self._reference()
                    if reference is None:
                        self._keep_reference(captured.path, levels)
                        rows.append(
                            {
                                "label": "Match line",
                                "value": "kept as your reference (on this machine only)",
                                "tone": "",
                            }
                        )
                    else:
                        rows.extend(quality.match_rows(levels, reference[0]))
                return levels, rows
            finally:
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)

        def done(
            result: tuple[quality.CheckLevels, list[dict[str, str]]] | None,
            error: BaseException | None,
        ) -> None:
            self._running = self._live = None
            if self._closed:
                return
            if error is not None:
                self._failed(f"The check failed: {error}")
                return
            if result is None:  # cancelled
                self._set(self.checkChanged, running=False, stage="idle", instruction="")
                return
            levels, rows = result
            self.levels = levels
            summary, tone = levels.verdict()
            self._set(
                self.checkChanged,
                running=False,
                stage="done",
                instruction="",
                results=rows,
                summary=summary,
                summaryTone=tone,
                referenceText=self._reference_text(),
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

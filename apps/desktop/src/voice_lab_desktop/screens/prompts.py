"""Record → Prompts (design §6.1, phase 4c; D-010 to D-016, D-042): prompted recording.

A session records a batch of a catalog's prompts (the ones chosen plus every room tone,
D-010) through the capture worker (``capture/``, D-039): one take at a time into its own
private directory, validated before it exists. The screen shows one large, read-only
script, or for a silent prompt a "Do not speak" card (D-013); a grouped transport (R
records and stops, Space plays and pauses, Return saves; scoped to this screen, D-011);
the elapsed time from the frames the worker confirmed, never a clock (D-014); the selected
prompt's take history with remove and restore; the waveform of the take under review with
trim handles, whose preview plays exactly the trimmed frames (D-012). Save keeps the take
with its trim; Save & next moves on to the next prompt still missing a kept take, in
catalog order. Finish builds the capture package (``prompted/package.py``): one kept,
untrimmed original per prompt. Trims, the history and the continuity evidence stay here.

The recorder is injectable (``recorder_factory``): the app's default runs the capture
worker; tests use a stand-in.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final

from PySide6.QtCore import QTimer, Signal, Slot

from cole_voice_lab.models import Prompt, utc_now

from ..capture.types import SAMPLE_RATE, CapturedTake, RunningTake, TakeRecorder
from ..prompted import audio, catalog, package
from ..prompted.store import PromptedStore, SessionRecord, StoreError, TakeEntry
from ..viewmodel import AppContext, ViewModel, qproperty

#: How often a running take's confirmed frames are read.
POLL_MS: Final = 50
#: How long the worker may take to stop and validate a take.
STOP_TIMEOUT_S: Final = 30.0
BUSY_PHASES: Final = frozenset({"starting", "recording", "stopping"})
STATUS_LABELS: Final = {
    "missing": ("missing", "neutral"),
    "draft": ("draft", "info"),
    "kept": ("kept", "ok"),
    "removed": ("removed", "neutral"),
    "failed": ("failed", "danger"),
    "damaged": ("damaged", "danger"),
}
STATE_LABELS: Final = {
    "starting": "Starting microphone",
    "recording": "Recording",
    "stopping": "Stopping — checking audio",
}


def default_recorder() -> TakeRecorder:
    """The PortAudio capture worker (``capture/process.py``, D-039), imported when first
    needed."""
    from ..capture.process import WorkerRecorder

    return WorkerRecorder()


def elapsed_text(frames: int) -> str:
    """D-014: confirmed frames as ``m:ss.s``; empty until the first frames arrive."""
    if frames <= 0:
        return ""
    seconds = frames / SAMPLE_RATE
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _excerpt(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def prompt_title(prompt: Prompt) -> str:
    if prompt.spoken:
        return _excerpt(prompt.text or prompt.prompt_id)
    return "Room tone" if prompt.category == "room-tone" else _excerpt(prompt.content)


class PromptsModel(ViewModel):
    setupChanged = Signal()
    sessionChanged = Signal()
    takesChanged = Signal()
    transportChanged = Signal()
    waveformChanged = Signal()
    #: A session became a capture package (the app refreshes Uploads).
    packaged = Signal()

    # -- before a session
    catalogOptions = qproperty("QVariantList", "catalogOptions", setupChanged)
    catalogValue = qproperty(str, "catalogValue", setupChanged)
    catalogMessage = qproperty(str, "catalogMessage", setupChanged)
    catalogPrompts = qproperty("QVariantList", "catalogPrompts", setupChanged)
    selectedCount = qproperty(int, "selectedCount", setupChanged)
    deviceLabel = qproperty(str, "deviceLabel", setupChanged)
    deviceOk = qproperty(bool, "deviceOk", setupChanged)
    canStart = qproperty(bool, "canStart", setupChanged)
    # -- the session
    phase = qproperty(str, "phase", transportChanged)
    sessionId = qproperty(str, "sessionId", sessionChanged)
    sessionLabel = qproperty(str, "sessionLabel", sessionChanged)
    batch = qproperty("QVariantList", "batch", sessionChanged)
    keptCount = qproperty(int, "keptCount", sessionChanged)
    batchCount = qproperty(int, "batchCount", sessionChanged)
    finishBlocker = qproperty(str, "finishBlocker", sessionChanged)
    packageText = qproperty(str, "packageText", sessionChanged)
    currentPromptId = qproperty(str, "currentPromptId", sessionChanged)
    currentIndex = qproperty(int, "currentIndex", sessionChanged)
    currentText = qproperty(str, "currentText", sessionChanged)
    currentSilent = qproperty(bool, "currentSilent", sessionChanged)
    currentInstructions = qproperty(str, "currentInstructions", sessionChanged)
    currentStyle = qproperty(str, "currentStyle", sessionChanged)
    currentGuide = qproperty(str, "currentGuide", sessionChanged)
    # -- the selected prompt's takes and the one under review
    takes = qproperty("QVariantList", "takes", takesChanged)
    reviewNumber = qproperty(int, "reviewNumber", takesChanged)
    reviewStatus = qproperty(str, "reviewStatus", takesChanged)
    previewKey = qproperty(str, "previewKey", takesChanged)
    # -- the transport
    stateLabel = qproperty(str, "stateLabel", transportChanged)
    elapsedText = qproperty(str, "elapsedText", transportChanged)
    lastError = qproperty(str, "lastError", transportChanged)
    # -- the waveform of the take under review
    bins = qproperty("QVariantList", "bins", waveformChanged)
    frameCount = qproperty(int, "frameCount", waveformChanged)
    trimStart = qproperty(int, "trimStart", waveformChanged)
    trimEnd = qproperty(int, "trimEnd", waveformChanged)
    waveformLoading = qproperty(bool, "waveformLoading", waveformChanged)

    def __init__(
        self,
        context: AppContext,
        *,
        recorder_factory: Callable[[], TakeRecorder] = default_recorder,
        store: PromptedStore | None = None,
    ) -> None:
        super().__init__(
            context,
            {
                "catalogOptions": [],
                "catalogValue": catalog.DEFAULT_SOURCE,
                "catalogMessage": "",
                "catalogPrompts": [],
                "selectedCount": 0,
                "deviceLabel": "",
                "deviceOk": False,
                "canStart": False,
                "phase": "setup",
                "sessionId": "",
                "sessionLabel": "",
                "batch": [],
                "keptCount": 0,
                "batchCount": 0,
                "finishBlocker": "",
                "packageText": "",
                "currentPromptId": "",
                "currentIndex": -1,
                "currentText": "",
                "currentSilent": False,
                "currentInstructions": "",
                "currentStyle": "",
                "currentGuide": "",
                "takes": [],
                "reviewNumber": 0,
                "reviewStatus": "",
                "previewKey": "",
                "stateLabel": "",
                "elapsedText": "",
                "lastError": "",
                "bins": [],
                "frameCount": 0,
                "trimStart": 0,
                "trimEnd": 0,
                "waveformLoading": False,
            },
        )
        self.store = store or PromptedStore(context.config.data_dir)
        self.recorder_factory = recorder_factory
        self._recorder: TakeRecorder | None = None
        self._catalog: catalog.Catalog | None = None
        self._selected: set[str] = set()
        self._session: SessionRecord | None = None
        self._running: RunningTake | None = None
        self._take: tuple[str, int, Path, datetime] | None = None  # prompt, number, dir, asked
        self._stop_asked = False
        self._finishing = False
        self._waveform_generation = 0
        self._shown = False
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._on_poll)
        context.configChanged.connect(self._load_catalog)
        self._load_catalog()
        self._resume()

    # -- lifecycle --------------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        """A take is being recorded (its sidebar entry shows it)."""
        return self._v["phase"] in BUSY_PHASES

    def activate(self) -> None:
        self._shown = True
        if self._v["phase"] == "setup":
            self.checkDevice()

    def deactivate(self) -> None:
        self._shown = False

    def shutdown(self) -> None:
        """The app is closing: a take being recorded is stopped (the worker keeps what it
        wrote; it never becomes a take)."""
        self._poll.stop()
        running, self._running = self._running, None
        if running is not None:
            running.stop()

    def _recorder_or_none(self) -> TakeRecorder | None:
        if self._recorder is None:
            try:
                self._recorder = self.recorder_factory()
            except Exception as exc:  # noqa: BLE001 - no capture worker in this build
                self._set(
                    self.setupChanged,
                    deviceOk=False,
                    deviceLabel=f"The capture worker is not available ({exc}).",
                )
                self._update_can_start()
                return None
        return self._recorder

    @Slot()
    def checkDevice(self) -> None:  # noqa: N802 (QML name)
        """Which device a take would open (a UMIK-1, or why there is none)."""
        recorder = self._recorder_or_none()
        if recorder is None:
            return

        def done(label: str | None, error: BaseException | None) -> None:
            if error is not None:
                self._set(self.setupChanged, deviceOk=False, deviceLabel=str(error))
            else:
                self._set(self.setupChanged, deviceOk=True, deviceLabel=label or "")
            self._update_can_start()

        self.context.tasks.submit(recorder.device_label, done)

    # -- the catalog and the batch ----------------------------------------------------------

    def _load_catalog(self) -> None:
        config = self.context.config
        options = catalog.choices(config.catalog_file or None)
        value = self._v["catalogValue"]
        if value not in {option["value"] for option in options}:
            value = catalog.DEFAULT_SOURCE
        self._set(self.setupChanged, catalogOptions=options)
        self.setCatalog(value)

    @Slot(str)
    def setCatalog(self, value: str) -> None:  # noqa: N802
        if self._session is not None:
            return
        try:
            loaded = catalog.load(value, catalog_file=self.context.config.catalog_file or None)
        except catalog.CatalogRefused as exc:
            self._catalog = None
            self._selected = set()
            self._set(
                self.setupChanged, catalogValue=value, catalogMessage=str(exc), catalogPrompts=[]
            )
            self._update_can_start()
            return
        self._catalog = loaded
        self._selected = {p.prompt_id for p in loaded.suite.prompts if p.category != "room-tone"}
        self._set(self.setupChanged, catalogValue=value, catalogMessage="")
        self._sync_catalog_rows()

    def _sync_catalog_rows(self) -> None:
        rows = []
        if self._catalog is not None:
            for prompt in self._catalog.suite.prompts:
                room = prompt.category == "room-tone"
                rows.append(
                    {
                        "promptId": prompt.prompt_id,
                        "title": prompt_title(prompt),
                        "category": prompt.category,
                        "always": room,
                        "selected": room or prompt.prompt_id in self._selected,
                    }
                )
        spoken = sum(1 for row in rows if row["selected"] and not row["always"])
        self._set(self.setupChanged, catalogPrompts=rows, selectedCount=spoken)
        self._update_can_start()

    @Slot(str, bool)
    def choosePrompt(self, prompt_id: str, chosen: bool) -> None:  # noqa: N802
        if self._session is not None or self._catalog is None:
            return
        if chosen:
            self._selected.add(prompt_id)
        else:
            self._selected.discard(prompt_id)
        self._sync_catalog_rows()

    @Slot(bool)
    def chooseAll(self, chosen: bool) -> None:  # noqa: N802
        if self._session is not None or self._catalog is None:
            return
        spoken = {p.prompt_id for p in self._catalog.suite.prompts if p.category != "room-tone"}
        self._selected = spoken if chosen else set()
        self._sync_catalog_rows()

    def _update_can_start(self) -> None:
        ready = (
            self._session is None
            and self._catalog is not None
            and self._v["selectedCount"] > 0
            and self._v["deviceOk"]
        )
        self._set(self.setupChanged, canStart=ready)

    @Slot()
    def startSession(self) -> None:  # noqa: N802
        """A new session over the chosen prompts and every room tone."""
        if not self._v["canStart"] or self._catalog is None:
            return
        loaded = self._catalog
        try:
            prompt_ids = catalog.batch_ids(loaded.suite, self._selected)
            session = self.store.create(
                catalog_source=loaded.source,
                catalog_label=loaded.label,
                catalog=loaded.suite,
                catalog_sha256=loaded.sha256,
                prompt_ids=prompt_ids,
                device=self._v["deviceLabel"],
            )
        except (catalog.CatalogRefused, StoreError, OSError) as exc:
            self.toast.emit(f"Session not started: {exc}", "danger")
            return
        self._open(session)
        self.toast.emit(f"Session started: {len(prompt_ids)} prompts, room tone included.", "ok")

    def _resume(self) -> None:
        session = self.store.open_session()
        if session is None:
            session = next(
                (s for s in reversed(self.store.sessions()) if s.state == "finished"), None
            )
        if session is not None:
            self._open(session)

    def _open(self, session: SessionRecord) -> None:
        self._session = session
        self._set(
            self.sessionChanged,
            sessionId=session.session_id,
            sessionLabel=f"{session.catalog_label} · started "
            f"{session.started_at.astimezone().strftime('%Y-%m-%d %H:%M')}",
            packageText="",
        )
        self._set(
            self.transportChanged, phase="finished" if session.state == "finished" else "ready"
        )
        self._update_can_start()
        self._refresh_batch()
        statuses = self.store.statuses(session)
        first = next((pid for pid in session.prompt_ids if statuses[pid] != "kept"), None)
        self._select(first or session.prompt_ids[0])

    def _prompt(self, prompt_id: str) -> Prompt:
        assert self._session is not None
        return next(p for p in self._session.catalog.prompts if p.prompt_id == prompt_id)

    def _refresh_batch(self) -> None:
        session = self._session
        if session is None:
            self._set(self.sessionChanged, batch=[], keptCount=0, batchCount=0, finishBlocker="")
            return
        statuses = self.store.statuses(session)
        rows = []
        for index, prompt_id in enumerate(session.prompt_ids):
            prompt = self._prompt(prompt_id)
            status = statuses[prompt_id]
            label, tone = STATUS_LABELS[status]
            rows.append(
                {
                    "promptId": prompt_id,
                    "index": index,
                    "title": prompt_title(prompt),
                    "category": prompt.category,
                    "silent": not prompt.spoken,
                    "status": status,
                    "statusLabel": label,
                    "tone": tone,
                    "current": prompt_id == self._v["currentPromptId"],
                }
            )
        kept = sum(1 for row in rows if row["status"] == "kept")
        missing = [row["promptId"] for row in rows if row["status"] != "kept"]
        blocker = "" if not missing else f"{len(missing)} prompt(s) still need a kept take."
        self._set(
            self.sessionChanged,
            batch=rows,
            keptCount=kept,
            batchCount=len(rows),
            finishBlocker=blocker,
        )

    # -- the selected prompt and its takes --------------------------------------------------

    @Slot(str)
    def selectPrompt(self, prompt_id: str) -> None:  # noqa: N802
        """Select the recording target; locked while a take is recorded (D-012)."""
        if self._session is None or self.recording or prompt_id not in self._session.prompt_ids:
            return
        self._select(prompt_id)

    def _select(self, prompt_id: str, review: int | None = None) -> None:
        assert self._session is not None
        prompt = self._prompt(prompt_id)
        guide = f"about {prompt.expected_seconds:g} s"
        self._set(
            self.sessionChanged,
            currentPromptId=prompt_id,
            currentIndex=self._session.prompt_ids.index(prompt_id),
            currentText=prompt.text if prompt.spoken and prompt.text else "",
            currentSilent=not prompt.spoken,
            currentInstructions=prompt.instructions or ("" if prompt.spoken else prompt.content),
            currentStyle=prompt.style,
            currentGuide=guide,
        )
        self._refresh_batch()
        self._refresh_takes(review=review, reset=True)

    def _entries(self) -> list[TakeEntry]:
        if self._session is None or not self._v["currentPromptId"]:
            return []
        return self.store.takes(self._session, self._v["currentPromptId"])

    def _refresh_takes(self, *, review: int | None = None, reset: bool = False) -> None:
        entries = self._entries()
        usable = [e for e in entries if e.record is not None]
        current = self._v["reviewNumber"]
        if review is not None:
            chosen = review
        elif not reset and any(e.number == current for e in usable):
            chosen = current
        else:
            kept = [e for e in usable if e.status == "kept"]
            pick = (kept or usable)[-1:] if (kept or usable) else []
            chosen = pick[0].number if pick else 0
        rows = []
        for entry in reversed(entries):
            label, tone = STATUS_LABELS[entry.status]
            when = (
                entry.record.requested_at
                if entry.record
                else (entry.failure.requested_at if entry.failure else None)
            )
            stamp = when.astimezone().strftime("%H:%M:%S") if when else "?"
            rows.append(
                {
                    "number": entry.number,
                    "label": f"Take {entry.number} · {stamp} · {label}",
                    "status": entry.status,
                    "statusLabel": label,
                    "tone": tone,
                    "selected": entry.number == chosen,
                    "usable": entry.record is not None,
                    "message": entry.failure.message if entry.failure else "",
                    "trimmed": bool(entry.record and entry.record.trim),
                }
            )
        status = next((e.status for e in usable if e.number == chosen), "")
        old = self._v["reviewNumber"]
        self._set(self.takesChanged, takes=rows, reviewNumber=chosen, reviewStatus=status)
        if chosen != old or reset or review is not None:
            self._load_waveform()
        else:
            self._sync_trim()

    def _review_entry(self) -> TakeEntry | None:
        number = self._v["reviewNumber"]
        return next(
            (e for e in self._entries() if e.number == number and e.record is not None), None
        )

    def _sync_trim(self) -> None:
        entry = self._review_entry()
        if entry is None or entry.record is None:
            self._set(self.waveformChanged, trimStart=0, trimEnd=0)
            self._set(self.takesChanged, previewKey="")
            return
        trim = entry.record.trim
        start, end = (trim.start_frame, trim.end_frame) if trim else (0, entry.record.frames)
        self._set(self.waveformChanged, trimStart=start, trimEnd=end)
        session_id = self._v["sessionId"]
        key = f"prompt:{session_id}:{entry.prompt_id}:{entry.number}:{start}-{end}"
        self._set(self.takesChanged, previewKey=key)

    def _load_waveform(self) -> None:
        self._waveform_generation += 1
        generation = self._waveform_generation
        entry = self._review_entry()
        if entry is None or entry.record is None:
            self._set(self.waveformChanged, bins=[], frameCount=0, waveformLoading=False)
            self._sync_trim()
            return
        self._set(self.waveformChanged, waveformLoading=True)
        self._sync_trim()
        path = entry.original

        def done(result: tuple[list[float], int] | None, error: BaseException | None) -> None:
            if generation != self._waveform_generation:
                return
            if error is not None or result is None:
                self._set(self.waveformChanged, bins=[], frameCount=0, waveformLoading=False)
                self.toast.emit(f"The waveform could not be drawn: {error}", "warn")
                return
            bins, frames = result
            self._set(self.waveformChanged, bins=bins, frameCount=frames, waveformLoading=False)

        self.context.tasks.submit(lambda: audio.waveform(path), done)

    @Slot(int)
    def selectTake(self, number: int) -> None:  # noqa: N802
        if self.recording:
            return
        if any(e.number == number and e.record is not None for e in self._entries()):
            self.context.player.stop()
            self._refresh_takes(review=number)

    @Slot(int)
    def removeTake(self, number: int) -> None:  # noqa: N802
        """Exclude a take (reversible, D-012); a removed take is never packaged."""
        self._decide(number, "removed")

    @Slot(int)
    def restoreTake(self, number: int) -> None:  # noqa: N802
        if self._session is None or self.recording or self._v["phase"] == "finished":
            return
        try:
            self.store.restore(self._session, self._v["currentPromptId"], number)
        except StoreError as exc:
            self.toast.emit(str(exc), "danger")
            return
        self._refresh_batch()
        self._refresh_takes()

    def _decide(self, number: int, decision: str) -> bool:
        if self._session is None or self.recording or self._v["phase"] == "finished":
            return False
        try:
            self.store.decide(self._session, self._v["currentPromptId"], number, decision)  # type: ignore[arg-type]
        except StoreError as exc:
            self.toast.emit(str(exc), "danger")
            return False
        self._refresh_batch()
        self._refresh_takes()
        return True

    # -- trim -------------------------------------------------------------------------------

    @Slot(int, int)
    def setTrim(self, start: int, end: int) -> None:  # noqa: N802
        """The take under review's trim recipe (frames ``[start, end)``); the original is
        never touched."""
        entry = self._review_entry()
        if self._session is None or entry is None or entry.record is None or self.recording:
            return
        frames = entry.record.frames
        start = max(0, min(int(start), frames - 1))
        end = max(start + 1, min(int(end), frames))
        try:
            self.store.set_trim(self._session, entry.prompt_id, entry.number, (start, end))
        except StoreError as exc:
            self.toast.emit(str(exc), "warn")
            return
        if self.context.player.activeKey == self._v["previewKey"]:
            self.context.player.stop()
        self._refresh_takes()

    @Slot()
    def resetTrim(self) -> None:  # noqa: N802
        entry = self._review_entry()
        if entry is not None and entry.record is not None:
            self.setTrim(0, entry.record.frames)

    # -- the transport ----------------------------------------------------------------------

    @Slot()
    def toggleRecord(self) -> None:  # noqa: N802
        """R: record when idle, stop while recording."""
        if self._v["phase"] in ("starting", "recording"):
            self.stop()
        else:
            self.record()

    @Slot()
    def record(self) -> None:
        """A new take of the selected prompt (also Retake)."""
        if self._session is None or self._v["phase"] != "ready":
            return
        recorder = self._recorder_or_none()
        if recorder is None:
            self.toast.emit(self._v["deviceLabel"], "danger")
            return
        prompt_id = self._v["currentPromptId"]
        self.context.player.stop()
        try:
            number, take_dir = self.store.new_take_dir(self._session, prompt_id)
        except (StoreError, OSError) as exc:
            self.toast.emit(f"No take started: {exc}", "danger")
            return
        self._take = (prompt_id, number, take_dir, utc_now())
        self._stop_asked = False
        self._set(
            self.transportChanged,
            phase="starting",
            stateLabel=STATE_LABELS["starting"],
            elapsedText="",
            lastError="",
        )

        def done(running: RunningTake | None, error: BaseException | None) -> None:
            if error is not None or running is None:
                self._take_failed(str(error) if error is not None else "the take did not start")
                return
            self._running = running
            if self._stop_asked:
                running.stop()
            self._poll.start()

        self.context.tasks.submit(lambda: recorder.start(take_dir), done)

    @Slot()
    def stop(self) -> None:
        if self._v["phase"] not in ("starting", "recording"):
            return
        self._stop_asked = True
        self._set(self.transportChanged, phase="stopping", stateLabel=STATE_LABELS["stopping"])
        if self._running is not None:
            self._running.stop()

    def _on_poll(self) -> None:
        running = self._running
        if running is None:
            self._poll.stop()
            return
        frames = running.confirmed_frames
        if frames > 0 and self._v["phase"] == "starting":
            self._set(
                self.transportChanged, phase="recording", stateLabel=STATE_LABELS["recording"]
            )
        self._set(self.transportChanged, elapsedText=elapsed_text(frames))
        if running.done:
            self._poll.stop()
            self._finish_take(running)

    def _finish_take(self, running: RunningTake) -> None:
        if self._v["phase"] != "stopping":
            self._set(self.transportChanged, phase="stopping", stateLabel=STATE_LABELS["stopping"])

        def done(captured: CapturedTake | None, error: BaseException | None) -> None:
            self._running = None
            if error is not None or captured is None:
                self._take_failed(str(error) if error is not None else "no take")
                return
            self._take_done(captured)

        self.context.tasks.submit(lambda: running.wait(STOP_TIMEOUT_S), done)

    def _take_done(self, captured: CapturedTake) -> None:
        assert self._session is not None and self._take is not None
        prompt_id, number, _dir, _asked = self._take
        self._take = None
        try:
            self.store.record_take(self._session, prompt_id, number, captured)
        except (StoreError, OSError) as exc:
            self._take_failed(f"the take could not be recorded here: {exc}")
            return
        self._set(
            self.transportChanged,
            phase="ready",
            stateLabel="",
            elapsedText=elapsed_text(captured.frames),
        )
        self._refresh_batch()
        if prompt_id == self._v["currentPromptId"]:
            self._refresh_takes(review=number)

    def _take_failed(self, message: str) -> None:
        """The take did not complete: its directory keeps what was written, with the
        reason; it never becomes a take."""
        if self._session is not None and self._take is not None:
            prompt_id, number, _dir, asked = self._take
            try:
                self.store.record_failure(
                    self._session, prompt_id, number, requested_at=asked, message=message
                )
            except (StoreError, OSError):
                pass
        self._take = None
        self._running = None
        self._poll.stop()
        self._set(
            self.transportChanged, phase="ready", stateLabel="", elapsedText="", lastError=message
        )
        self.toast.emit(f"The take failed and was not kept: {message}", "danger")
        self._refresh_takes()

    @Slot()
    def togglePlay(self) -> None:  # noqa: N802
        """Space: play exactly the trimmed frames of the take under review; pause; resume."""
        if self.recording:
            return
        player = self.context.player
        key = self._v["previewKey"]
        entry = self._review_entry()
        if not key or entry is None or entry.record is None:
            return
        if player.activeKey == key and str(player.state) in ("playing", "paused"):
            player.toggle(key)
            return
        start, end = self._v["trimStart"], self._v["trimEnd"]
        path = entry.original
        player.play_clip(key, lambda: audio.clip(path, start, end))

    @Slot(int)
    def seek(self, frame: int) -> None:
        """Move the preview to ``frame`` of the original (within the trim)."""
        player = self.context.player
        if player.activeKey != self._v["previewKey"]:
            return
        start, end = self._v["trimStart"], self._v["trimEnd"]
        frame = max(start, min(int(frame), end))
        player.seek((frame - start) / SAMPLE_RATE)

    @Slot()
    def save(self) -> None:
        """Return: keep the take under review, with its trim (Keep never opens the
        microphone, D-011)."""
        number = self._v["reviewNumber"]
        if number and self._v["phase"] == "ready" and self._decide(number, "kept"):
            self.toast.emit(f"Take {number} kept.", "ok")

    @Slot()
    def saveNext(self) -> None:  # noqa: N802
        """Keep the take, then the next prompt still missing a kept take (catalog order,
        wrapping around)."""
        number = self._v["reviewNumber"]
        if not number or self._v["phase"] != "ready" or not self._decide(number, "kept"):
            return
        assert self._session is not None
        statuses = self.store.statuses(self._session)
        order = list(self._session.prompt_ids)
        here = order.index(self._v["currentPromptId"])
        for prompt_id in order[here + 1 :] + order[: here + 1]:
            if statuses[prompt_id] != "kept":
                self._select(prompt_id)
                return
        self.toast.emit("Every prompt has a kept take: finish the session.", "ok")

    # -- finishing --------------------------------------------------------------------------

    @Slot()
    def finishSession(self) -> None:  # noqa: N802
        """Build the capture package: one kept, untrimmed take per prompt."""
        session = self._session
        if session is None or self.recording or self._finishing:
            return
        try:
            package.kept_takes(self.store, session)
        except package.PackageNotReady as exc:
            self.toast.emit(f"Not finished: {exc}", "warn")
            return
        if session.finished_at is None:
            session = self.store.update(session, state="finished", finished_at=utc_now())
            self._session = session
        self._finishing = True
        self.context.player.stop()
        self._set(self.transportChanged, phase="finished", stateLabel="")
        store = self.store

        def done(result: tuple[Path, str] | None, error: BaseException | None) -> None:
            self._finishing = False
            if error is not None or result is None:
                message = str(error) if error is not None else "no package"
                self._set(self.sessionChanged, packageText=f"Not packaged: {message}")
                self.toast.emit(f"The package was not built: {message}", "danger")
                return
            path, digest = result
            assert self._session is not None
            self._session = store.update(
                self._session, state="packaged", package_manifest_sha256=digest
            )
            self._set(
                self.sessionChanged,
                packageText=f"Packaged as {path.name} (manifest {digest[:12]}…). It waits in "
                "Uploads; takes, trims and history stay on this machine.",
            )
            self._set(self.transportChanged, phase="packaged")
            self.toast.emit("Session packaged.", "ok")
            self.packaged.emit()

        self.context.tasks.submit(lambda: package.build(store, session), done)

    @Slot()
    def abandonSession(self) -> None:  # noqa: N802
        """Set the session aside (its takes stay here); a new one can start."""
        if self._session is None or self.recording or self._v["phase"] == "finished":
            return
        self.store.update(self._session, state="abandoned")
        self._close_session()
        self.toast.emit("Session set aside; its takes stay on this machine.", "info")

    @Slot()
    def newSession(self) -> None:  # noqa: N802
        if self._v["phase"] == "packaged":
            self._close_session()

    def _close_session(self) -> None:
        self.context.player.stop()
        self._session = None
        self._set(self.transportChanged, phase="setup", stateLabel="", elapsedText="", lastError="")
        self._set(
            self.sessionChanged,
            sessionId="",
            sessionLabel="",
            currentPromptId="",
            currentIndex=-1,
            currentText="",
            currentSilent=False,
            currentInstructions="",
            currentStyle="",
            currentGuide="",
            packageText="",
        )
        self._set(self.takesChanged, takes=[], reviewNumber=0, reviewStatus="", previewKey="")
        self._set(self.waveformChanged, bins=[], frameCount=0, trimStart=0, trimEnd=0)
        self._refresh_batch()
        self._sync_catalog_rows()
        if self._shown:
            self.checkDevice()

"""Record → Prompts as the prompt studio (D-051, D-052; RECORDING-V1.md §5, step 6a).

The Swift app's fixed workspace (docs/NATIVE_RECORDING_UX.md §4), corpus-first (D-018): no
sessions, groups or packages. The library lists every prompt of the catalog version this
run pinned (``corpus/catalog.py``) with its status from the PC's library and this
machine's takes (``studio/library.py``), coverage (``n / 1,886``), Continue Missing (the
next prompt with no kept take, in catalog order, whatever the filters), search, Show and
Labels. The header carries the prompt's label chips, the script card its PROMPT · READ
ALOUD or its INSTRUCTIONS, and the footer the Record/Stop control; the status line says what
the take and the sync are doing (``studio/status.py``).

A take: Record binds it to the selected prompt of the pinned version
(``CorpusClient.begin_take``) and starts the capture worker (D-039) in its directory; Stop
ends it, and the worker's validated take waits for review. Save keeps it
(``save_take`` then the ``kept`` decision, off the GUI thread: durable on this machine at
once, its upload queued); Save & Next then selects the next missing prompt. Retake records
again; the take it replaces is not lost: it is saved as an undecided draft, as is a take left
by selecting another prompt. Discard removes a failed take (or an unsaved one, on request).
Reject, Remove, Restore, trims, the take picker and the waveform are step 6b's; keys 6c's
(``toggleRecord``, ``save`` and ``saveNext`` are what R, Space, Return and Ctrl+S call).

Every connection made here goes to a declared ``@Slot`` (#41): QML reads this object.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, Literal

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from cole_voice_lab.canonical import canonical_sha256

from ..capture.types import SAMPLE_RATE, CapturedTake, RunningTake, TakeRecorder
from ..corpus.catalog import PinnedCatalog
from ..corpus.client import CorpusClient, PendingTake
from ..corpus.spool import SpooledTake, SpoolError
from ..corpus.status import SyncStatus
from ..studio import library, presentation, status
from ..studio.library import PromptState
from ..studio.presentation import PromptView
from ..tablemodel import RoleRows
from ..viewmodel import AppContext, ViewModel, qproperty
from .prompts import default_recorder

#: How often a running take's confirmed frames are read.
POLL_MS: Final = 50
#: How long the worker may take to stop and validate a take.
STOP_TIMEOUT_S: Final = 30.0
ROW_ROLES: Final = ("promptId", "title", "status", "statusTitle", "kind", "current")
#: The phases of the transport. ``blocked``: no catalog to record against.
type Phase = Literal[
    "blocked", "ready", "starting", "recording", "stopping", "review", "failed", "saving"
]
BUSY: Final = frozenset({"starting", "recording", "stopping", "saving"})
STATE_TEXT: Final[dict[str, str]] = {
    "starting": "Starting the microphone",
    "recording": "Recording",
    "stopping": "Stopping · checking the audio",
    "review": "Take recorded · Save keeps it, Retake records again",
    "saving": "Saving",
}
CONTINUE_HELP: Final = (
    "Selects the next missing prompt in catalog order; ignores search and filters."
)


def elapsed_text(frames: int) -> str:
    """Confirmed frames as ``mm:ss.cc`` (D-014: the audio's own time, never a clock)."""
    seconds = max(0, frames) / SAMPLE_RATE
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - 60 * minutes:05.2f}"


def _when(moment: datetime) -> str:
    local = moment.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


class StudioModel(ViewModel):
    libraryChanged = Signal()
    rowsChanged = Signal()
    promptChanged = Signal()
    transportChanged = Signal()
    statusChanged = Signal()
    deviceChanged = Signal()

    # -- the library (sidebar)
    coverageText = qproperty(str, "coverageText", libraryChanged)
    coverageSpoken = qproperty(str, "coverageSpoken", libraryChanged)
    coverageSource = qproperty(str, "coverageSource", libraryChanged)
    catalogText = qproperty(str, "catalogText", libraryChanged)
    switchText = qproperty(str, "switchText", libraryChanged)
    canSwitch = qproperty(bool, "canSwitch", libraryChanged)
    canContinueMissing = qproperty(bool, "canContinueMissing", libraryChanged)
    continueHelp = qproperty(str, "continueHelp", libraryChanged)
    searchText = qproperty(str, "searchText", libraryChanged)
    show = qproperty(str, "show", libraryChanged)
    showOptions = qproperty("QVariantList", "showOptions", libraryChanged)
    labelOptions = qproperty("QVariantList", "labelOptions", libraryChanged)
    labelsTitle = qproperty(str, "labelsTitle", libraryChanged)
    rowCount = qproperty(int, "rowCount", rowsChanged)
    emptyText = qproperty(str, "emptyText", rowsChanged)
    currentOutside = qproperty(bool, "currentOutside", rowsChanged)
    currentRow = qproperty("QVariantMap", "currentRow", rowsChanged)
    # -- the selected prompt (header and script card)
    promptId = qproperty(str, "promptId", promptChanged)
    promptTitle = qproperty(str, "promptTitle", promptChanged)
    promptKind = qproperty(str, "promptKind", promptChanged)
    banner = qproperty(str, "banner", promptChanged)
    promptText = qproperty(str, "promptText", promptChanged)
    placeholder = qproperty(str, "placeholder", promptChanged)
    instructions = qproperty(str, "instructions", promptChanged)
    guidance = qproperty(str, "guidance", promptChanged)
    duration = qproperty(str, "duration", promptChanged)
    labelChips = qproperty("QVariantList", "labelChips", promptChanged)
    pinnedChips = qproperty(int, "pinnedChips", promptChanged)
    labelsSpoken = qproperty(str, "labelsSpoken", promptChanged)
    promptStatus = qproperty(str, "promptStatus", promptChanged)
    takesText = qproperty(str, "takesText", promptChanged)
    # -- the transport (footer)
    phase = qproperty(str, "phase", transportChanged)
    elapsedText = qproperty(str, "elapsedText", transportChanged)
    canRecord = qproperty(bool, "canRecord", transportChanged)
    canStop = qproperty(bool, "canStop", transportChanged)
    canRetake = qproperty(bool, "canRetake", transportChanged)
    canSave = qproperty(bool, "canSave", transportChanged)
    canSaveNext = qproperty(bool, "canSaveNext", transportChanged)
    canDiscard = qproperty(bool, "canDiscard", transportChanged)
    canNavigate = qproperty(bool, "canNavigate", transportChanged)
    # -- the status line
    statusText = qproperty(str, "statusText", statusChanged)
    statusTone = qproperty(str, "statusTone", statusChanged)
    syncText = qproperty(str, "syncText", statusChanged)
    syncTone = qproperty(str, "syncTone", statusChanged)
    noticeId = qproperty(str, "noticeId", statusChanged)
    # -- the microphone
    deviceOk = qproperty(bool, "deviceOk", deviceChanged)
    deviceLabel = qproperty(str, "deviceLabel", deviceChanged)

    def _get_rows(self) -> RoleRows:
        return self._rows

    # The library's rows (a list model: 1,886 prompts are delegated lazily).
    rowsModel = Property(QObject, _get_rows, constant=True)

    def __init__(
        self,
        context: AppContext,
        client: CorpusClient,
        *,
        recorder_factory: Callable[[], TakeRecorder] = default_recorder,
    ) -> None:
        super().__init__(
            context,
            {
                "coverageText": "–",
                "coverageSpoken": "",
                "coverageSource": "",
                "catalogText": "",
                "switchText": "",
                "canSwitch": False,
                "canContinueMissing": False,
                "continueHelp": CONTINUE_HELP,
                "searchText": "",
                "show": "all",
                "showOptions": list(library.SHOW_OPTIONS),
                "labelOptions": [],
                "labelsTitle": presentation.labels_menu_title(()),
                "rowCount": 0,
                "emptyText": "",
                "currentOutside": False,
                "currentRow": {},
                "promptId": "",
                "promptTitle": "",
                "promptKind": "spoken",
                "banner": "",
                "promptText": "",
                "placeholder": "",
                "instructions": "",
                "guidance": "",
                "duration": "",
                "labelChips": [],
                "pinnedChips": 1,
                "labelsSpoken": "",
                "promptStatus": "",
                "takesText": "",
                "phase": "blocked",
                "elapsedText": elapsed_text(0),
                "canRecord": False,
                "canStop": False,
                "canRetake": False,
                "canSave": False,
                "canSaveNext": False,
                "canDiscard": False,
                "canNavigate": False,
                "statusText": "",
                "statusTone": "neutral",
                "syncText": "",
                "syncTone": "neutral",
                "noticeId": "",
                "deviceOk": False,
                "deviceLabel": "",
            },
        )
        self.client = client
        self.recorder_factory = recorder_factory
        self._rows = RoleRows(ROW_ROLES, self)
        self._recorder: TakeRecorder | None = None
        self._pinned: PinnedCatalog | None = None
        self._views: tuple[PromptView, ...] = ()
        self._by_id: dict[str, PromptView] = {}
        self._order: tuple[str, ...] = ()
        self._digests: dict[str, str] = {}
        self._states: dict[str, PromptState] = {}
        self._takes: list[SpooledTake] = []
        self._labels: set[str] = set()
        self._current: str | None = None
        self._pending: PendingTake | None = None
        self._captured: CapturedTake | None = None
        self._running: RunningTake | None = None
        self._stop_asked = False
        self._failure = ""
        self._outcome: status.Line | None = None
        self._shown = False
        self._closed = False
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._on_poll)
        client.add_listener(self._client_changed)
        self._refresh()

    # -- lifecycle --------------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        """A take is being recorded or saved (the sidebar entry shows it; navigation waits)."""
        return self._v["phase"] in BUSY

    def start(self) -> None:
        """The corpus client's first pass (the PC's catalog and library, then the outboxes)."""
        self.client.start()

    def activate(self) -> None:
        self._shown = True
        if not self._v["deviceOk"]:
            self.checkDevice()
        self._refresh()

    def deactivate(self) -> None:
        self._shown = False

    def shutdown(self) -> None:
        """The app is closing: a take being recorded is stopped (its recording directory
        stays; the client saves or counts it at the next start)."""
        self._closed = True
        self._poll.stop()
        self.client.remove_listener(self._client_changed)
        self.client.shutdown()
        running, self._running = self._running, None
        if running is not None:
            running.stop()

    def _recorder_or_none(self) -> TakeRecorder | None:
        if self._recorder is None:
            try:
                self._recorder = self.recorder_factory()
            except Exception as exc:  # noqa: BLE001 - no capture worker in this build
                self._set(
                    self.deviceChanged,
                    deviceOk=False,
                    deviceLabel=f"The capture worker is not available ({exc}).",
                )
                self._update_transport()
                self._update_status()
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
                self._set(self.deviceChanged, deviceOk=False, deviceLabel=str(error))
            else:
                self._set(self.deviceChanged, deviceOk=True, deviceLabel=label or "")
            self._update_transport()
            self._update_status()

        self.context.tasks.submit(recorder.device_label, done)

    # -- the catalog, the takes and the library ---------------------------------------------

    @Slot()
    def _client_changed(self) -> None:
        """The corpus client may have changed (a pass, a local change): read it again."""
        if not self._closed:
            self._refresh()

    def _refresh(self) -> None:
        pinned = self.client.pinned
        if pinned is not self._pinned and (
            pinned is None
            or self._pinned is None
            or pinned.snapshot_sha256 != self._pinned.snapshot_sha256
        ):
            self._adopt_catalog(pinned)
        self._takes = self.client.takes()
        self._recount()
        self._update_status()

    def _adopt_catalog(self, pinned: PinnedCatalog | None) -> None:
        self._pinned = pinned
        if pinned is None:
            self._views = ()
        else:
            suite = pinned.snapshot.suite
            self._views = presentation.present(suite.prompts, suite.purpose)
        self._by_id = {view.prompt_id: view for view in self._views}
        self._order = tuple(view.prompt_id for view in self._views)
        self._digests = {view.prompt_id: canonical_sha256(view.prompt) for view in self._views}
        options = presentation.label_options(self._views)
        self._labels &= set(options)
        if self._current not in self._by_id:
            self._current = None
        if self._v["phase"] == "blocked" and pinned is not None:
            self._set(self.transportChanged, phase="ready")
        elif pinned is None and self._v["phase"] not in BUSY:
            self._set(self.transportChanged, phase="blocked")
        self._show_prompt()  # the same prompt id may read otherwise in another version

    def _recount(self) -> None:
        pinned = self._pinned
        cached = self.client.library.latest
        pc = cached.library if cached is not None else None
        if pinned is None:
            self._states = {}
        else:
            self._states = library.prompt_states(
                self._views, pinned.snapshot, pc, self._takes, digests=self._digests
            )
        if self._current is None:
            if self._order:
                first = library.next_missing(self._order, self._states, None)
                self._current = first or self._order[0]
            self._show_prompt()
        self._update_library()
        self._update_rows()
        self._update_prompt_status()
        self._update_transport()

    def _state(self, prompt_id: str) -> PromptState:
        return self._states.get(prompt_id, library.MISSING)

    def _update_library(self) -> None:
        sync = self.client.status()
        total = len(self._views)
        catalog = status.catalog_line(sync)
        if self._pinned is None:
            coverage, spoken, source = "–", "No catalog", ""
        else:
            recorded = library.covered(self._states)
            coverage = f"{recorded:,} / {total:,}"
            spoken = f"{recorded:,} of {total:,} prompts recorded"
            source = self._coverage_source()
        options = presentation.label_options(self._views)
        selected = tuple(token for token in options if token in self._labels)
        self._set(
            self.libraryChanged,
            coverageText=coverage,
            coverageSpoken=spoken,
            coverageSource=source,
            catalogText=catalog.text,
            switchText=status.switch_line(sync),
            canSwitch=sync.switch_pending,
            labelOptions=[
                {
                    "token": token,
                    "title": presentation.label_title(token),
                    "selected": token in self._labels,
                }
                for token in options
            ],
            labelsTitle=presentation.labels_menu_title(selected),
        )
        self._update_navigation()

    def _coverage_source(self) -> str:
        cache = self.client.library
        cached = cache.latest
        if cached is None:
            return "This machine's takes only: the PC's library has not been read yet"
        pinned = self._pinned
        version = cached.library.catalog.catalog_version
        read = _when(cache.checked_at or cached.fetched_at)
        if pinned is not None and cached.library.catalog == pinned.reference:
            text = f"The PC's library (read {read}) and this machine's takes"
        else:
            text = f"The PC's library of version {version} (read {read}) and this machine's takes"
        if cache.unavailable:
            text += " · the PC is rebuilding its library"
        return text

    def _update_rows(self) -> None:
        search, show = self._v["searchText"], self._v["show"]
        labels = frozenset(self._labels)
        rows: list[dict[str, Any]] = []
        for view in self._views:
            state = self._state(view.prompt_id)
            if library.shows(view, state, search=search, show=show, labels=labels):
                rows.append(self._row(view, state))
        current = self._current
        outside = current is not None and not any(row["promptId"] == current for row in rows)
        if not self._views:
            empty = ""
        elif not rows:
            empty = "No prompt matches the search and filters."
        else:
            empty = ""
        self._rows.update(rows)
        current_row = (
            self._row(self._by_id[current], self._state(current))
            if current is not None and outside
            else {}
        )
        self._set(
            self.rowsChanged,
            rowCount=len(rows),
            emptyText=empty,
            currentOutside=outside,
            currentRow=current_row,
        )

    def _row(self, view: PromptView, state: PromptState) -> dict[str, Any]:
        return {
            "promptId": view.prompt_id,
            "title": view.title,
            "status": state.status,
            "statusTitle": state.title,
            "kind": view.kind,
            "current": view.prompt_id == self._current,
        }

    # -- the library's controls ---------------------------------------------------------------

    @Slot(str)
    def setSearch(self, text: str) -> None:  # noqa: N802
        self._set(self.libraryChanged, searchText=text)
        self._update_rows()

    @Slot(str)
    def setShow(self, value: str) -> None:  # noqa: N802
        if value not in {option["value"] for option in library.SHOW_OPTIONS}:
            return
        self._set(self.libraryChanged, show=value)
        self._update_rows()

    @Slot(str, bool)
    def setLabel(self, token: str, selected: bool) -> None:  # noqa: N802
        if token not in presentation.label_options(self._views):
            return
        if selected:
            self._labels.add(token)
        else:
            self._labels.discard(token)
        self._update_library()
        self._update_rows()

    @Slot()
    def showAllLabels(self) -> None:  # noqa: N802
        self._labels.clear()
        self._update_library()
        self._update_rows()

    # -- the selected prompt -----------------------------------------------------------------

    def _navigable(self) -> bool:
        return self._pinned is not None and self._v["phase"] not in BUSY

    def _update_navigation(self) -> None:
        can = self._navigable() and any(
            state.status in library.UNCOVERED for state in self._states.values()
        )
        self._set(self.libraryChanged, canContinueMissing=can)

    @Slot(str)
    def selectPrompt(self, prompt_id: str) -> None:  # noqa: N802
        """Select the recording target (never while a take is recorded or saved; selecting
        the selected prompt changes nothing)."""
        if not self._navigable() or prompt_id not in self._by_id or prompt_id == self._current:
            return
        self._leave_take()
        self._select(prompt_id)

    @Slot()
    def continueMissing(self) -> None:  # noqa: N802
        """The next prompt with no kept take after the selected one, in catalog order
        (wrapping once); search and filters never change where it goes."""
        if not self._navigable():
            return
        target = library.next_missing(self._order, self._states, self._current)
        if target is None:
            self._outcome = status.Line("Every prompt is recorded", "ok")
            self._update_status()
            return
        if target == self._current:
            return  # the only prompt still missing is the one selected
        self._leave_take()
        self._select(target)

    def _select(self, prompt_id: str) -> None:
        self._current = prompt_id
        self._outcome = None
        self._show_prompt()
        self._update_rows()
        self._update_transport()
        self._update_status()

    def _show_prompt(self) -> None:
        view = self._by_id.get(self._current or "")
        if view is None:
            self._set(
                self.promptChanged,
                promptId="",
                promptTitle="Recording",
                promptKind="spoken",
                banner="",
                promptText="",
                placeholder=self.client.status().record_blocked
                or "Your selected prompt will appear here.",
                instructions="",
                guidance="",
                duration="",
                labelChips=[],
                pinnedChips=1,
                labelsSpoken="",
            )
            self._update_prompt_status()
            return
        titles = [presentation.label_title(token) for token in view.labels]
        self._set(
            self.promptChanged,
            promptId=view.prompt_id,
            promptTitle=view.title,
            promptKind=view.kind,
            banner=view.banner,
            promptText=view.display_prompt,
            placeholder="" if view.display_prompt else presentation.NO_SPOKEN_PROMPT,
            instructions=view.display_instructions,
            guidance=view.guidance,
            duration=view.duration,
            labelChips=titles,
            pinnedChips=view.pinned_labels,
            labelsSpoken="Labels: " + ", ".join(titles),
        )
        self._update_prompt_status()

    def _update_prompt_status(self) -> None:
        current = self._current
        if current is None:
            self._set(self.promptChanged, promptStatus="", takesText="")
            return
        state = self._state(current)
        unsent = [take for take in self._takes if take.prompt_id == current]
        takes = state.pc_takes + sum(1 for take in unsent if take.sync.upload != "accepted")
        if takes == 0:
            text = "No takes of this prompt yet"
        else:
            text = f"{takes:,} take{'' if takes == 1 else 's'} of this prompt"
        self._set(self.promptChanged, promptStatus=state.title, takesText=text)

    # -- the transport ------------------------------------------------------------------------

    def _update_transport(self) -> None:
        phase = self._v["phase"]
        ready = self._pinned is not None and self._current is not None and self._v["deviceOk"]
        self._set(
            self.transportChanged,
            canRecord=ready and phase in ("ready", "review", "failed"),
            canStop=phase in ("starting", "recording"),
            canRetake=ready and phase in ("review", "failed"),
            canSave=phase == "review",
            canSaveNext=phase == "review",
            canDiscard=phase in ("review", "failed"),
            canNavigate=self._navigable(),
        )
        self._update_navigation()

    def _phase(self, phase: Phase, **values: Any) -> None:
        self._set(self.transportChanged, phase=phase, **values)
        self._update_transport()
        self._update_status()

    @Slot()
    def toggleRecord(self) -> None:  # noqa: N802
        """R: record (or record again) when idle, stop while recording."""
        if self._v["phase"] in ("starting", "recording"):
            self.stop()
        else:
            self.record()

    @Slot()
    def retake(self) -> None:
        """Record the selected prompt again: the take under review is saved as a draft."""
        if self._v["phase"] in ("review", "failed"):
            self.record()

    @Slot()
    def record(self) -> None:
        """A new take of the selected prompt, against the pinned catalog version."""
        if not self._v["canRecord"] or self._current is None:
            return
        recorder = self._recorder_or_none()
        if recorder is None:
            return
        self._leave_take()
        try:
            pending = self.client.begin_take(self._current)
        except (SpoolError, OSError) as exc:
            self._outcome = status.Line(f"No take started: {exc}", "danger")
            self._update_status()
            return
        self._pending = pending
        self._captured = None
        self._stop_asked = False
        self._outcome = None
        self.context.player.stop()
        self._phase("starting", elapsedText=elapsed_text(0))

        def done(running: RunningTake | None, error: BaseException | None) -> None:
            if self._closed:
                if running is not None:
                    running.stop()
                return
            if error is not None or running is None:
                self._discard_pending()
                message = str(error) if error is not None else "the worker did not start"
                self._outcome = status.Line(f"The take did not start: {message}", "danger")
                self._phase("ready")
                return
            self._running = running
            if self._stop_asked:
                running.stop()
            self._poll.start()

        self.context.tasks.submit(lambda: recorder.start(pending.directory), done)

    @Slot()
    def stop(self) -> None:
        if self._v["phase"] not in ("starting", "recording"):
            return
        self._stop_asked = True
        self._phase("stopping")
        if self._running is not None:
            self._running.stop()

    @Slot()
    def _on_poll(self) -> None:
        running = self._running
        if running is None:
            self._poll.stop()
            return
        frames = running.confirmed_frames
        if frames > 0 and self._v["phase"] == "starting":
            self._phase("recording")
        self._set(self.transportChanged, elapsedText=elapsed_text(frames))
        if running.done:
            self._poll.stop()
            self._finish(running)

    def _finish(self, running: RunningTake) -> None:
        if self._v["phase"] != "stopping":
            self._phase("stopping")

        def done(captured: CapturedTake | None, error: BaseException | None) -> None:
            self._running = None
            if self._closed:
                return
            if error is not None or captured is None:
                self._failure = str(error) if error is not None else "no take"
                self._phase("failed")
                return
            self._captured = captured
            self._phase("review", elapsedText=elapsed_text(captured.frames))

        self.context.tasks.submit(lambda: running.wait(STOP_TIMEOUT_S), done)

    @Slot()
    def discard(self) -> None:
        """Remove the failed (or unsaved) take under review; nothing of it is kept."""
        if self._v["phase"] not in ("review", "failed"):
            return
        self._discard_pending()
        self._outcome = status.Line("Take discarded", "neutral")
        self._phase("ready", elapsedText=elapsed_text(0))

    def _discard_pending(self) -> None:
        pending, self._pending, self._captured = self._pending, None, None
        self._failure = ""
        if pending is not None:
            try:
                self.client.discard_take(pending)
            except SpoolError as exc:
                self._outcome = status.Line(f"The take could not be discarded: {exc}", "danger")

    def _leave_take(self) -> None:
        """Before another take or prompt: a failed take is discarded; a take under review is
        saved as an undecided draft (off the GUI thread), never lost."""
        phase = self._v["phase"]
        if phase == "failed":
            self._discard_pending()
        elif phase == "review" and self._pending is not None and self._captured is not None:
            pending, captured = self._pending, self._captured
            self._pending = self._captured = None
            self._keep_draft(pending, captured)
        if phase in ("review", "failed"):
            self._phase("ready", elapsedText=elapsed_text(0))

    def _keep_draft(self, pending: PendingTake, captured: CapturedTake) -> None:
        client = self.client

        def done(_take: SpooledTake | None, error: BaseException | None) -> None:
            if self._closed:
                return
            if error is not None:
                if not isinstance(error, (SpoolError, OSError)):
                    raise error
                self._outcome = status.Line(
                    f"The earlier take was not saved ({error}); it stays on disk", "warn"
                )
            self._refresh()

        self.context.tasks.submit(lambda: client.save_take(pending, captured), done)

    @Slot()
    def save(self) -> None:
        """Ctrl+S: keep the take under review (Save), and stay on this prompt."""
        self._save(advance=False)

    @Slot()
    def saveNext(self) -> None:  # noqa: N802
        """Return: keep the take under review, then the next missing prompt."""
        self._save(advance=True)

    def _save(self, *, advance: bool) -> None:
        if self._v["phase"] != "review" or self._pending is None or self._captured is None:
            return
        pending, captured, client = self._pending, self._captured, self.client
        self._phase("saving")

        def work() -> tuple[SpooledTake, str]:
            take = client.save_take(pending, captured)
            try:
                take = client.decide(take.take_id, "kept")
            except SpoolError as exc:
                return take, str(exc)
            return take, ""

        def done(result: tuple[SpooledTake, str] | None, error: BaseException | None) -> None:
            if self._closed:
                return
            if error is not None or result is None:
                if error is not None and not isinstance(error, (SpoolError, OSError)):
                    raise error
                self._outcome = status.Line(f"Not saved: {error}", "danger")
                self._phase("review")
                return
            _take, problem = result
            self._pending = self._captured = None
            if problem:
                self._outcome = status.Line(f"Saved as a draft, not kept: {problem}", "warn")
            else:
                self._outcome = status.Line("Saved · kept on this machine", "ok")
            self._phase("ready", elapsedText=elapsed_text(0))
            self._refresh()
            if advance and not problem:
                self._advance()

        self.context.tasks.submit(work, done)

    def _advance(self) -> None:
        target = library.next_missing(self._order, self._states, self._current)
        if target is None:
            self._outcome = status.Line("Saved · every prompt is recorded", "ok")
            self._update_status()
            return
        outcome = self._outcome
        self._select(target)
        self._outcome = outcome
        self._update_status()

    # -- the catalog version ------------------------------------------------------------------

    @Slot()
    def switchCatalog(self) -> None:  # noqa: N802
        """Start the next session now: the PC's active version is pinned (between takes)."""
        if not self.client.status().switch_pending or self._v["phase"] not in ("ready",):
            return
        self.client.start_new_session()
        self._outcome = status.Line("Switched to the PC's active catalog version", "info")
        self._refresh()

    # -- the status line ----------------------------------------------------------------------

    @Slot()
    def dismissNotice(self) -> None:  # noqa: N802
        notice_id = self._v["noticeId"]
        if notice_id:
            self.client.dismiss_notice(notice_id)

    @Slot()
    def syncNow(self) -> None:  # noqa: N802
        self.client.sync_now()

    def _transport_line(self, sync: SyncStatus) -> status.Line:
        phase = self._v["phase"]
        if phase == "blocked":
            return status.Line("No catalog yet: nothing can be recorded", "warn")
        if phase == "failed":
            return status.Line(f"The take failed: {self._failure}", "danger")
        if phase in STATE_TEXT:
            if phase == "review" and self._outcome is not None:
                return self._outcome
            return status.Line(STATE_TEXT[phase], "warn" if phase == "recording" else "info")
        if self._outcome is not None:
            return self._outcome
        if not self._v["deviceOk"]:
            label = self._v["deviceLabel"] or "checking the microphone"
            return status.Line(f"No microphone: {label}", "warn")
        return status.Line("Ready to record", "neutral")

    def _update_status(self) -> None:
        sync = self.client.status()
        transport = self._transport_line(sync)
        titles = {view.prompt_id: view.title for view in self._views}
        notice = status.notice_line(sync.notices, titles)
        line = notice if notice is not None else status.sync_line(sync)
        self._set(
            self.statusChanged,
            statusText=transport.text,
            statusTone=transport.tone,
            syncText=line.text,
            syncTone=line.tone,
            noticeId=sync.notices[-1].notice_id if sync.notices else "",
        )
        self._set(self.libraryChanged, switchText=status.switch_line(sync))

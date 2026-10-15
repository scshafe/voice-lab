"""The prompt studio's takes (RECORDING-V1.md §5 "Transport and review", step 6b).

``TakeReviewModel`` fills the regions step 6a held at their final size: the take picker and
its history, the waveform, the trim row, the timer's playhead and the playback row. The
studio (``screens/studio.py``) owns it and tells it what changed: the selected prompt, a
take being recorded (the live waveform), the take under review, a save, the client's state.
QML reads it as ``app.studioModel.takes``.

- **Takes:** every take of the prompt, from this machine and from the PC
  (``studio/takes.py``), newest first, with when, how long, status and where it lives.
  Selecting a prompt selects its latest kept take; the take under review comes first while
  it waits for Save.
- **Decisions:** Remove, Reject (a reason is required) and Restore go through the corpus
  client (``decide``, ``restore``) for every saved take; one recorded elsewhere (another
  client's, or a Swift take imported from the Mac) is noted in the spool first
  (``remote_take``) and played from the PC.
- **The waveform** of the selected take on the dB display scale, with Zoom and Fit; while a
  take is recorded, the live waveform the worker's partial WAV gives and its peak level.
- **Trims:** In and Out are set from the playhead (or by dragging the waveform's handles);
  Save trim (``set_trim``) and Reset. The take under review keeps its edits until Save.
  Leaving a saved take with an edit not saved (another take or prompt, Continue Missing,
  Record, another screen) asks first: Save trim, Discard edit or Cancel (``guard``).
- **Playback** through the app's one player: the preview plays exactly the trimmed frames
  (D-012), with ±0.1 s steps; never while a take is recorded.

Every connection goes to a declared ``@Slot`` (#41).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from PySide6.QtCore import Signal, Slot

from cole_voice_lab.api.corpus_contracts import TakeHistory
from cole_voice_lab.corpus_read_model import LibraryTake

from ..api import ApiError
from ..auth import AuthError
from ..capture.types import SAMPLE_RATE, CapturedTake
from ..config import ConfigError
from ..corpus.client import CorpusClient
from ..corpus.records import Notice
from ..corpus.spool import SpooledTake, SpoolError
from ..playback import PlaybackError
from ..studio import audio, takes
from ..studio.takes import REVIEW, TakeEntry
from ..viewmodel import AppContext, ViewModel, qproperty

#: Playback steps (the ±0.1 s buttons; ←/→ in the waveform, step 6c).
STEP_FRAMES: Final = SAMPLE_RATE // 10
#: The narrowest view Zoom reaches.
MIN_VIEW_FRAMES: Final = SAMPLE_RATE // 20
#: Where a failure to reach the PC's takes leaves the picker.
PC_UNREACHED: Final = "The PC's takes of this prompt could not be read"
#: Where a failure to read this machine's takes (to compare them with the PC's) leaves it.
HERE_UNREAD: Final = "This machine's takes could not be compared with the PC's"


def clock(frames: int) -> str:
    seconds = max(0, frames) / SAMPLE_RATE
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - 60 * minutes:05.2f}"


class TakeReviewModel(ViewModel):
    takesChanged = Signal()
    waveChanged = Signal()
    trimChanged = Signal()
    playChanged = Signal()
    confirmChanged = Signal()
    restoreChanged = Signal()

    # -- the picker and the history
    takeOptions = qproperty("QVariantList", "takeOptions", takesChanged)
    selectedKey = qproperty(str, "selectedKey", takesChanged)
    history = qproperty("QVariantList", "history", takesChanged)
    historyNote = qproperty(str, "historyNote", takesChanged)
    takeCount = qproperty(int, "takeCount", takesChanged)
    canRemove = qproperty(bool, "canRemove", takesChanged)
    canReject = qproperty(bool, "canReject", takesChanged)
    canRestore = qproperty(bool, "canRestore", takesChanged)
    actionNote = qproperty(str, "actionNote", takesChanged)
    # -- the waveform
    bins = qproperty("QVariantList", "bins", waveChanged)
    slots = qproperty(int, "slots", waveChanged)
    viewStart = qproperty(int, "viewStart", waveChanged)
    viewEnd = qproperty(int, "viewEnd", waveChanged)
    frameCount = qproperty(int, "frameCount", waveChanged)
    live = qproperty(bool, "live", waveChanged)
    waveNote = qproperty(str, "waveNote", waveChanged)
    canZoomIn = qproperty(bool, "canZoomIn", waveChanged)
    canZoomOut = qproperty(bool, "canZoomOut", waveChanged)
    levelPeak = qproperty(float, "levelPeak", waveChanged)
    levelText = qproperty(str, "levelText", waveChanged)
    # -- the trim
    trimStart = qproperty(int, "trimStart", trimChanged)
    trimEnd = qproperty(int, "trimEnd", trimChanged)
    trimDirty = qproperty(bool, "trimDirty", trimChanged)
    inText = qproperty(str, "inText", trimChanged)
    outText = qproperty(str, "outText", trimChanged)
    canEditTrim = qproperty(bool, "canEditTrim", trimChanged)
    canSaveTrim = qproperty(bool, "canSaveTrim", trimChanged)
    canResetTrim = qproperty(bool, "canResetTrim", trimChanged)
    # -- playback
    playhead = qproperty(int, "playhead", playChanged)
    timeText = qproperty(str, "timeText", playChanged)
    playing = qproperty(bool, "playing", playChanged)
    canPlay = qproperty(bool, "canPlay", playChanged)
    # -- the question before an unsaved trim edit is left
    confirmOpen = qproperty(bool, "confirmOpen", confirmChanged)
    # -- Restore's question when what came before is not known here
    restoreAskOpen = qproperty(bool, "restoreAskOpen", restoreChanged)

    def __init__(
        self,
        context: AppContext,
        client: CorpusClient,
        *,
        busy: Callable[[], bool],
    ) -> None:
        super().__init__(
            context,
            {
                "takeOptions": [],
                "selectedKey": "",
                "history": [],
                "historyNote": "",
                "takeCount": 0,
                "canRemove": False,
                "canReject": False,
                "canRestore": False,
                "actionNote": "",
                "bins": [],
                "slots": 0,
                "viewStart": 0,
                "viewEnd": 0,
                "frameCount": 0,
                "live": False,
                "waveNote": "No take selected",
                "canZoomIn": False,
                "canZoomOut": False,
                "levelPeak": 0.0,
                "levelText": "",
                "trimStart": 0,
                "trimEnd": 0,
                "trimDirty": False,
                "inText": clock(0),
                "outText": clock(0),
                "canEditTrim": False,
                "canSaveTrim": False,
                "canResetTrim": False,
                "playhead": 0,
                "timeText": "",
                "playing": False,
                "canPlay": False,
                "confirmOpen": False,
                "restoreAskOpen": False,
            },
        )
        self.client = client
        self.busy = busy
        self._prompt: str | None = None
        self._prompt_sha256 = ""
        self._all_local: list[SpooledTake] = []
        self._notices: tuple[Notice, ...] = ()
        self._pc: list[LibraryTake] | None = None
        self._pc_tag: str | None = None
        self._pc_fetching = False
        self._pc_again = False
        self._pc_generation = 0
        self._review: TakeEntry | None = None
        self._review_path: Path | None = None
        self._entries: list[TakeEntry] = []
        self._selected: TakeEntry | None = None
        self._samples: audio.Samples | None = None
        self._samples_key = ""
        self._load_generation = 0
        #: Trim edits not saved yet, by take key (the take under review keeps its own).
        self._edits: dict[str, tuple[int, int]] = {}
        self._leaving: Callable[[], None] | None = None
        self._live: audio.LiveWaveform | None = None
        self._pending_seek: int | None = None
        self._closed = False
        self._live_reading = False
        #: Told each PC history read (prompt id, library tag, history): the studio's status.
        self.on_history: Callable[[str, str | None, TakeHistory], None] | None = None
        player = context.player
        player.changed.connect(self._player_changed)
        player.positionChanged.connect(self._player_position)

    def shutdown(self) -> None:
        self._closed = True

    @property
    def _edit(self) -> tuple[int, int] | None:
        """The selected take's trim edit, not saved yet."""
        return self._edits.get(self._selected.key) if self._selected is not None else None

    @_edit.setter
    def _edit(self, value: tuple[int, int] | None) -> None:
        if self._selected is None:
            return
        if value is None:
            self._edits.pop(self._selected.key, None)
        else:
            self._edits[self._selected.key] = value

    # -- what the studio tells it ------------------------------------------------------------

    def set_prompt(self, prompt_id: str | None, prompt_sha256: str, *, latest: bool) -> None:
        """A new prompt (``latest``: select its latest kept take, as an explicit selection
        does; Continue Missing, Save & Next and Retake select none)."""
        self.stop_playback()
        self._prompt, self._prompt_sha256 = prompt_id, prompt_sha256
        self._pc, self._pc_tag = None, None
        # An answer for the prompt before is ignored when it comes.
        self._pc_generation += 1
        self._pc_fetching = self._pc_again = False
        self._set(self.takesChanged, historyNote="")
        self._review = None
        self._review_path = None
        self._rebuild()
        target = takes.latest_kept(self._entries) if latest else None
        self._select(target)
        self._fetch_history(select_latest=latest and target is None)

    def refresh(self, local: Sequence[SpooledTake], notices: tuple[Notice, ...]) -> None:
        """The client's takes and notices (after any change); the PC's history is read again
        when the PC's library changed since it was read."""
        prompt = self._prompt
        self._all_local = list(local)
        self._notices = notices
        self._rebuild()
        cached = self.client.library.peek()
        tag = cached.tag if cached is not None else None
        if prompt is not None and tag is not None and tag != self._pc_tag:
            self._fetch_history(select_latest=False)

    def begin_live(self, path: Path) -> None:
        """A take is being recorded into ``path``'s directory (the worker's partial WAV)."""
        self.stop_playback()
        self._select(None, load=False)
        self._live = audio.LiveWaveform(path)
        self._set(
            self.waveChanged,
            live=True,
            bins=[],
            slots=audio.LIVE_SLOTS,
            frameCount=0,
            waveNote="Waiting for the first frames · dB display scale",
            levelPeak=0.0,
            levelText="",
            canZoomIn=False,
            canZoomOut=False,
        )

    def poll_live(self) -> None:
        """Read what the worker wrote since (off the GUI thread, one read at a time)."""
        live = self._live
        if live is None or self._live_reading:
            return
        self._live_reading = True

        def work() -> tuple[bool, list[float], float, str]:
            grew = live.poll()
            return grew, live.bins(), live.peak_dbfs, live.error

        def done(
            result: tuple[bool, list[float], float, str] | None, error: BaseException | None
        ) -> None:
            self._live_reading = False
            if error is not None:
                raise error
            if self._closed or live is not self._live or result is None:
                return
            grew, bins, peak, problem = result
            if grew:
                self._set(
                    self.waveChanged,
                    bins=bins,
                    waveNote="Live · dB display scale",
                    levelPeak=max(0.0, (peak - audio.FLOOR_DB) / -audio.FLOOR_DB),
                    levelText=f"{peak:.1f} dBFS",
                )
            elif problem:
                self._set(
                    self.waveChanged,
                    waveNote=f"No live waveform: {problem}",
                    levelText="",
                    levelPeak=0.0,
                )

        self.context.tasks.submit(work, done)

    def end_live(self) -> None:
        if self._live is None:
            return
        self._live = None
        self._set(self.waveChanged, live=False, levelPeak=0.0, levelText="")
        if self._selected is None:
            self._clear_wave("No take selected")

    def review(self, captured: CapturedTake) -> None:
        """The worker's take, under review until Save (it comes first, selected)."""
        self.end_live()
        self._review = TakeEntry(
            key=REVIEW,
            requested_at=captured.requested_at,
            frames=captured.frames,
            decision="unsaved",
            reason=None,
            trim=(None, None),
            where="unsaved",
        )
        self._review_path = captured.path
        self._edits.pop(REVIEW, None)
        self._rebuild()
        self._select(self._review)

    def clear_review(self, *, select: str | None = None) -> None:
        """The take under review is gone (saved as ``select``, or discarded)."""
        self.end_live()
        if self._review is not None and self._samples_key == REVIEW:
            self.stop_playback()
        self._review = None
        self._review_path = None
        self._edits.pop(REVIEW, None)
        self._rebuild()
        target = next((e for e in self._entries if e.key == select), None) if select else None
        if target is not None or self._selected is None or self._selected.key == REVIEW:
            self._select(target)

    def pending_trim(self) -> tuple[int | None, int | None] | None:
        """The trim edited on the take under review (``None``: not edited)."""
        edit, review = self._edits.get(REVIEW), self._review
        if edit is None or review is None:
            return None
        return self._range_or_whole(edit, review.frames)

    def selected_draft(self) -> str:
        """The selected saved take when it is an undecided draft (Save keeps it)."""
        entry = self._selected
        if entry is not None and entry.decidable and entry.decision == "draft":
            return entry.take_id
        return ""

    def take_id_for_change(self, entry: TakeEntry) -> str:
        """The spool's take for a change of ``entry``: a take recorded elsewhere is noted
        first. Raises: ``SpoolError``."""
        if entry.local is not None:
            return entry.take_id
        if entry.pc is None or self._prompt is None:
            raise SpoolError("this take is not saved")
        return self.client.remote_take(entry.pc, self._prompt).take_id

    def selected_take_for_change(self) -> str:
        """``take_id_for_change`` of the selected take. Raises: ``SpoolError``."""
        if self._selected is None:
            raise SpoolError("no take is selected")
        return self.take_id_for_change(self._selected)

    def selected_edit(self) -> tuple[int | None, int | None] | None:
        """An unsaved trim edit of the selected saved take (Save keeps it with the take)."""
        entry = self._selected
        if entry is None or entry.key == REVIEW or self._edit is None:
            return None
        return self._range_or_whole(self._edit, entry.frames)

    def transport_changed(self) -> None:
        self._update_actions()
        self._update_trim()
        self._update_play()

    # -- the takes ----------------------------------------------------------------------------

    def _rebuild(self) -> None:
        mine = [take for take in self._all_local if take.prompt_id == self._prompt]
        entries = takes.merge(mine, self._pc, self._notices)
        if self._review is not None:
            entries.insert(0, self._review)
        self._entries = entries
        if self._selected is not None:
            again = next((e for e in entries if e.key == self._selected.key), None)
            if again is None:
                self._select(None)
            else:
                trim_moved = again.trim != self._selected.trim
                self._selected = again
                if trim_moved:
                    self._edit = None  # the take's trim changed under the edit
        self._set(
            self.takesChanged,
            takeOptions=[{"value": e.key, "label": e.label} for e in entries],
            history=[self._history_row(e) for e in entries],
            takeCount=len(entries),
        )
        self._update_actions()
        self._update_trim()
        self._update_play()

    def _history_row(self, entry: TakeEntry) -> dict[str, Any]:
        selected = self._selected is not None and self._selected.key == entry.key
        return {
            "key": entry.key,
            "label": entry.label,
            "when": entry.when,
            "length": f"{entry.seconds:.1f} s",
            "status": entry.status,
            "where": entry.where_title,
            "origin": takes.ORIGIN_TITLES.get(entry.origin, entry.origin),
            "decision": entry.decision,
            "changedElsewhere": entry.changed_elsewhere,
            "selected": selected,
        }

    def _fetch_history(self, *, select_latest: bool) -> None:
        prompt = self._prompt
        if prompt is None:
            return
        if self._pc_fetching:
            self._pc_again = True  # read again once this answer is in
            return
        self._pc_fetching = True
        self._pc_generation += 1
        generation = self._pc_generation
        cached = self.client.library.peek()
        tag = cached.tag if cached is not None else None
        client = self.client

        def done(history: TakeHistory | None, error: BaseException | None) -> None:
            if generation != self._pc_generation:
                return
            self._pc_fetching = False
            if self._closed:
                return
            if self._pc_again:
                self._pc_again = False
                self._fetch_history(select_latest=select_latest)
            if error is not None or history is None:
                if isinstance(error, (SpoolError, OSError)):
                    # The spool, not the PC: the studio's status says so too.
                    self._set(self.takesChanged, historyNote=f"{HERE_UNREAD}: {error}")
                    return
                if error is not None and not isinstance(error, (ApiError, AuthError, ConfigError)):
                    raise error
                self._pc_tag = tag
                note = PC_UNREACHED + (f": {error}" if error is not None else "")
                self._set(self.takesChanged, historyNote=note)
                return
            self._pc_tag = tag
            if history.row.prompt_sha256 != self._prompt_sha256:
                self._pc = []
                note = "The PC's active catalog holds another revision of this prompt"
            else:
                self._pc = list(history.takes)
                note = ""
            self._set(self.takesChanged, historyNote=note)
            self._rebuild()
            if self.on_history is not None:
                self.on_history(prompt, tag, history)
            # A late answer never takes the selection or the waveform from a take being
            # recorded, nor from the take under review.
            idle = not self.busy() and self._live is None and self._review is None
            if select_latest and self._selected is None and idle:
                self._select(takes.latest_kept(self._entries))

        def work() -> TakeHistory:
            history = client.prompt_history(prompt)
            client.observe_history(history)  # a newer state of a take here is taken
            return history

        self.context.tasks.submit(work, done)

    @Slot(str)
    def selectTake(self, key: str) -> None:  # noqa: N802 (QML name)
        """Show (and play) another take of the prompt; never while one is recorded."""
        if self.busy():
            return
        entry = next((e for e in self._entries if e.key == key), None)
        if entry is not None and (self._selected is None or entry.key != self._selected.key):
            self.guard(lambda: self._select(entry))

    def _select(self, entry: TakeEntry | None, *, load: bool = True) -> None:
        if self._selected is None or entry is None or entry.key != self._selected.key:
            self.stop_playback()
            if self._selected is not None and self._selected.key != REVIEW:
                self._edit = None  # asked first (``guard``): saved or discarded
        self._selected = entry
        self._set(
            self.takesChanged,
            selectedKey=entry.key if entry is not None else "",
            history=[self._history_row(e) for e in self._entries],
        )
        self._update_actions()
        if entry is None:
            self._samples, self._samples_key = None, ""
            if self._live is None:
                self._clear_wave("No take selected")
        elif load:
            self._load(entry)
        self._update_trim()
        self._update_play()

    def _load(self, entry: TakeEntry) -> None:
        if self._samples_key == entry.key and self._samples is not None:
            self._show_samples(entry)
            return
        self._samples, self._samples_key = None, ""
        self._load_generation += 1
        generation = self._load_generation
        self._clear_wave("Loading the take")
        client = self.client
        path: Path | None = None
        if entry.key == REVIEW:
            path = self._review_path
        elif entry.audio_here and entry.local is not None:
            path = entry.local.audio

        def work() -> audio.Samples:
            if path is not None:
                return audio.read_take(path)
            return audio.read_take_bytes(client.take_audio(entry.take_id))

        def done(samples: audio.Samples | None, error: BaseException | None) -> None:
            if self._closed or generation != self._load_generation:
                return
            if error is not None or samples is None:
                known = (PlaybackError, ApiError, AuthError, ConfigError, OSError)
                if error is not None and not isinstance(error, known):
                    raise error
                self._clear_wave(f"The take cannot be shown: {error}")
                return
            self._samples, self._samples_key = samples, entry.key
            selected = self._selected
            if selected is not None and selected.key == entry.key:
                self._show_samples(selected)

        self.context.tasks.submit(work, done)

    def _show_samples(self, entry: TakeEntry) -> None:
        assert self._samples is not None
        frames = int(self._samples.shape[0])
        start, _end = self._trim_range(entry)
        self._set(self.playChanged, playhead=start)
        self._view(0, frames, note="dB display scale")
        self._update_trim()
        self._update_play()

    def _clear_wave(self, note: str) -> None:
        self._set(
            self.waveChanged,
            bins=[],
            slots=0,
            viewStart=0,
            viewEnd=0,
            frameCount=0,
            waveNote=note,
            canZoomIn=False,
            canZoomOut=False,
        )

    # -- decisions ----------------------------------------------------------------------------

    def _update_actions(self) -> None:
        entry = self._selected
        busy = self.busy()
        decidable = entry is not None and entry.decidable and not busy
        decision = entry.decision if entry is not None else ""
        note = ""
        if entry is not None and entry.key == REVIEW:
            note = "Not saved yet: Save keeps it; Retake or leaving keeps it as a draft"
        self._set(
            self.takesChanged,
            canRemove=decidable and decision in ("draft", "kept"),
            canReject=decidable and decision in ("draft", "kept"),
            canRestore=decidable and decision in ("rejected", "removed"),
            actionNote=note,
        )

    def _decide(self, change: Callable[[str], SpooledTake], done: str) -> bool:
        entry = self._selected
        if entry is None or not entry.decidable or self.busy():
            return False
        try:
            change(self.take_id_for_change(entry))
        except SpoolError as exc:
            self.toast.emit(str(exc), "danger")
            return False
        self.toast.emit(done, "ok")
        return True

    @Slot()
    def remove(self) -> None:
        """Remove the selected take (reversible: Restore brings it back)."""
        if self._v["canRemove"]:
            self._decide(lambda take_id: self.client.decide(take_id, "removed"), "Take removed")

    @Slot(str, result=bool)
    def reject(self, reason: str) -> bool:
        """Reject the selected take; the reason is required (and kept with the decision)."""
        reason = " ".join(reason.split())
        if not self._v["canReject"] or not reason:
            return False
        return self._decide(
            lambda take_id: self.client.decide(take_id, "rejected", reason[:500]), "Take rejected"
        )

    @Slot()
    def restore(self) -> None:
        """A rejected or removed take back to its decision before; when that is not known
        here (a take recorded elsewhere, an import included, seen only rejected or removed),
        the operator is asked: Restore as kept, or as draft. No guess."""
        entry = self._selected
        if not self._v["canRestore"] or entry is None:
            return
        if self._restore_target(entry) is None:
            self._set(self.restoreChanged, restoreAskOpen=True)
            return
        self._decide(self.client.restore, "Take restored")

    def _restore_target(self, entry: TakeEntry) -> str | None:
        """What Restore sets ``entry`` back to: a take only the PC knows has no history here
        (the PC's history says nothing of the decision before)."""
        if entry.local is None:
            return None
        return self.client.restore_target(entry.take_id)

    @Slot(str)
    def restoreAs(self, decision: str) -> None:  # noqa: N802
        """The operator's answer: restore as ``kept`` or ``draft``."""
        if not self._v["restoreAskOpen"] or decision not in ("kept", "draft"):
            return
        self._set(self.restoreChanged, restoreAskOpen=False)
        to: Literal["kept", "draft"] = "kept" if decision == "kept" else "draft"
        self._decide(
            lambda take_id: self.client.restore(take_id, to), f"Take restored as {decision}"
        )

    @Slot()
    def restoreCancel(self) -> None:  # noqa: N802
        self._set(self.restoreChanged, restoreAskOpen=False)

    # -- the trim -----------------------------------------------------------------------------

    @staticmethod
    def _range_or_whole(edit: tuple[int, int], frames: int) -> tuple[int | None, int | None]:
        return (None, None) if edit == (0, frames) else edit

    def _trim_range(self, entry: TakeEntry) -> tuple[int, int]:
        if self._edit is not None:
            return self._edit
        return entry.trim_frames()

    def _frames(self) -> int:
        return int(self._samples.shape[0]) if self._samples is not None else 0

    def _update_trim(self) -> None:
        entry = self._selected
        loaded = entry is not None and self._samples is not None and self._samples_key == entry.key
        if entry is None or not loaded:
            self._set(
                self.trimChanged,
                trimStart=0,
                trimEnd=0,
                trimDirty=False,
                inText=clock(0),
                outText=clock(0),
                canEditTrim=False,
                canSaveTrim=False,
                canResetTrim=False,
            )
            return
        start, end = self._trim_range(entry)
        dirty = self._edit is not None and self._edit != entry.trim_frames()
        editable = (entry.decidable or entry.key == REVIEW) and not self.busy()
        self._set(
            self.trimChanged,
            trimStart=start,
            trimEnd=end,
            trimDirty=dirty,
            inText=clock(start),
            outText=clock(end),
            canEditTrim=editable,
            canSaveTrim=editable and entry.decidable and dirty,
            canResetTrim=editable and (dirty or entry.trimmed),
        )

    def _set_edit(self, start: int, end: int) -> None:
        entry = self._selected
        frames = self._frames()
        if entry is None or not self._v["canEditTrim"] or frames == 0:
            return
        start = max(0, min(int(start), frames - 1))
        end = max(start + 1, min(int(end), frames))
        if (start, end) == self._trim_range(entry):
            return
        if self._player_key().startswith(self._play_prefix()):
            self.stop_playback()
        self._edit = (start, end)
        playhead = min(max(self._v["playhead"], start), end)
        self._set(self.playChanged, playhead=playhead)
        self._update_trim()
        self._update_play()

    @Slot()
    def setIn(self) -> None:  # noqa: N802
        """In at the playhead."""
        if self._selected is not None and self._v["playhead"] < self._v["trimEnd"]:
            self._set_edit(self._v["playhead"], self._v["trimEnd"])

    @Slot()
    def setOut(self) -> None:  # noqa: N802
        """Out at the playhead."""
        if self._selected is not None and self._v["playhead"] > self._v["trimStart"]:
            self._set_edit(self._v["trimStart"], self._v["playhead"])

    @Slot(int, int)
    def setTrimRange(self, start: int, end: int) -> None:  # noqa: N802
        """Both edges at once (the waveform's handles)."""
        self._set_edit(start, end)

    @Slot()
    def saveTrim(self) -> None:  # noqa: N802
        """Keep the edited trim as the take's recipe (the original never changes)."""
        entry = self._selected
        if entry is None or not self._v["canSaveTrim"] or self._edit is None:
            return
        start, end = self._range_or_whole(self._edit, entry.frames)
        try:
            self.client.set_trim(self.take_id_for_change(entry), start, end)
        except SpoolError as exc:
            self.toast.emit(str(exc), "danger")
            return
        self._edit = None
        self._update_trim()
        self.toast.emit("Trim saved", "ok")

    @Slot()
    def resetTrim(self) -> None:  # noqa: N802
        """Drop the edit; a saved trim goes back to the whole take."""
        entry = self._selected
        if entry is None or not self._v["canResetTrim"]:
            return
        self.stop_playback()
        self._edit = None
        if entry.decidable and entry.trimmed:
            try:
                self.client.set_trim(self.take_id_for_change(entry), None, None)
            except SpoolError as exc:
                self.toast.emit(str(exc), "danger")
                return
            self.toast.emit("Trim reset: the whole take", "ok")
        self._set(self.playChanged, playhead=0)
        self._update_trim()
        self._update_play()

    # -- leaving an edit not saved -------------------------------------------------------------

    def trim_at_risk(self) -> bool:
        """A saved take is selected with a trim edit not saved (leaving it would lose it)."""
        entry = self._selected
        return entry is not None and entry.key != REVIEW and bool(self._v["trimDirty"])

    def guard(self, action: Callable[[], None]) -> None:
        """Do ``action``, which leaves the selected take, once its unsaved trim edit is saved
        or discarded: with one, the operator is asked first (Save trim, Discard edit,
        Cancel)."""
        if not self.trim_at_risk():
            action()
            return
        self._leaving = action
        self._set(self.confirmChanged, confirmOpen=True)

    def _answered(self) -> Callable[[], None] | None:
        action, self._leaving = self._leaving, None
        self._set(self.confirmChanged, confirmOpen=False)
        return action

    @Slot()
    def confirmSaveTrim(self) -> None:  # noqa: N802
        """Save the trim, then go on."""
        action = self._answered()
        self.saveTrim()
        if action is not None and not self.trim_at_risk():
            action()

    @Slot()
    def confirmDiscardEdit(self) -> None:  # noqa: N802
        """Drop the edit, then go on."""
        action = self._answered()
        self.stop_playback()
        self._edit = None
        self._update_trim()
        self._update_play()
        if action is not None:
            action()

    @Slot()
    def confirmCancel(self) -> None:  # noqa: N802
        """Stay on the take, the edit kept."""
        self._answered()

    # -- zoom ---------------------------------------------------------------------------------

    def _view(self, start: int, end: int, *, note: str | None = None) -> None:
        samples = self._samples
        if samples is None:
            return
        frames = int(samples.shape[0])
        width = max(min(MIN_VIEW_FRAMES, frames), min(end - start, frames))
        start = max(0, min(start, frames - width))
        end = start + width
        values: dict[str, Any] = {
            "bins": audio.bins(samples, start, end),
            "slots": 0,
            "viewStart": start,
            "viewEnd": end,
            "frameCount": frames,
            "live": False,
            "canZoomIn": width > MIN_VIEW_FRAMES,
            "canZoomOut": width < frames,
        }
        if note is not None:
            values["waveNote"] = note
        self._set(self.waveChanged, **values)

    @Slot()
    def zoomIn(self) -> None:  # noqa: N802
        """Half the view, around the playhead."""
        if not self._v["canZoomIn"]:
            return
        start, end = self._v["viewStart"], self._v["viewEnd"]
        width = max(MIN_VIEW_FRAMES, (end - start) // 2)
        centre = self._v["playhead"] if start <= self._v["playhead"] <= end else (start + end) // 2
        self._view(centre - width // 2, centre - width // 2 + width)

    @Slot()
    def zoomOut(self) -> None:  # noqa: N802
        if not self._v["canZoomOut"]:
            return
        start, end = self._v["viewStart"], self._v["viewEnd"]
        width = (end - start) * 2
        centre = (start + end) // 2
        self._view(centre - width // 2, centre - width // 2 + width)

    @Slot()
    def fit(self) -> None:
        """The whole take."""
        if self._samples is not None and self._v["canZoomOut"]:
            self._view(0, self._frames())

    # -- playback -----------------------------------------------------------------------------

    def _player_key(self) -> str:
        return str(self.context.player.property("activeKey") or "")

    def _play_prefix(self) -> str:
        return f"studio:{self._selected.key}:" if self._selected is not None else "studio:-"

    def _play_key(self) -> str:
        start, end = self._v["trimStart"], self._v["trimEnd"]
        return f"{self._play_prefix()}{start}-{end}"

    def _active(self) -> bool:
        return self._player_key() == self._play_key() and self._selected is not None

    def _update_play(self) -> None:
        entry = self._selected
        loaded = entry is not None and self._samples is not None and self._samples_key == entry.key
        playing = self._active() and str(self.context.player.state) == "playing"
        if loaded:
            start, end = self._v["trimStart"], self._v["trimEnd"]
            text = f"{clock(self._v['playhead'] - start)} / {clock(end - start)}"
        else:
            text = ""
        self._set(
            self.playChanged,
            playing=playing,
            canPlay=loaded and not self.busy(),
            timeText=text,
        )

    def stop_playback(self) -> None:
        if self._player_key().startswith("studio:"):
            self.context.player.stop()
        self._pending_seek = None

    @Slot()
    def togglePlay(self) -> None:  # noqa: N802
        """Space: play exactly the trimmed frames of the selected take; pause; resume."""
        if not self._v["canPlay"] or self._samples is None:
            return
        player = self.context.player
        if self._active() and str(player.state) in ("playing", "paused"):
            player.toggle(self._play_key())
            return
        start, end = self._v["trimStart"], self._v["trimEnd"]
        samples = self._samples
        playhead = self._v["playhead"]
        self._pending_seek = playhead if start < playhead < end else None
        if playhead >= end:
            self._set(self.playChanged, playhead=start)
        player.play_clip(self._play_key(), lambda: audio.clip(samples, start, end))

    @Slot(int)
    def seekTo(self, frame: int) -> None:  # noqa: N802
        """The playhead to ``frame`` of the take, within the kept frames (a waveform click;
        Home and End, step 6c)."""
        if self._selected is None or self._samples is None:
            return
        start, end = self._v["trimStart"], self._v["trimEnd"]
        frame = max(start, min(int(frame), end))
        self._set(self.playChanged, playhead=frame)
        if self._active():
            self.context.player.seek((frame - start) / SAMPLE_RATE)
        self._update_play()

    @Slot(int)
    def seekBy(self, frames: int) -> None:  # noqa: N802
        """The playhead ``frames`` later (earlier when negative): ←/→ (step 6c)."""
        self.seekTo(self._v["playhead"] + int(frames))

    @Slot()
    def stepBack(self) -> None:  # noqa: N802
        self.seekBy(-STEP_FRAMES)

    @Slot()
    def stepForward(self) -> None:  # noqa: N802
        self.seekBy(STEP_FRAMES)

    @Slot()
    def seekToStart(self) -> None:  # noqa: N802
        self.seekTo(self._v["trimStart"])

    @Slot()
    def seekToEnd(self) -> None:  # noqa: N802
        self.seekTo(self._v["trimEnd"])

    @Slot()
    def _player_changed(self) -> None:
        if self._closed:
            return
        player = self.context.player
        if self._active() and self._pending_seek is not None and str(player.state) == "playing":
            frame, self._pending_seek = self._pending_seek, None
            player.seek((frame - self._v["trimStart"]) / SAMPLE_RATE)
        self._update_play()

    @Slot()
    def _player_position(self) -> None:
        if self._closed or not self._active():
            return
        position = float(self.context.player.property("position"))
        frame = self._v["trimStart"] + round(position * SAMPLE_RATE)
        self._set(self.playChanged, playhead=min(frame, self._v["trimEnd"]))
        self._update_play()

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
- **Quality flags** (D-058, ``studio/quality.py``): each take here gets a verdict, off the GUI
  thread and only between takes (never while one is recorded or saved), kept by take id in
  ``<data dir>/studio/quality.json``. The take under review is judged against this session's
  recent kept takes (``note_kept``). Flags never block anything.

Every connection goes to a declared ``@Slot`` (#41).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from PySide6.QtCore import Signal, Slot

from cole_voice_lab.api.corpus_contracts import TakeHistory
from cole_voice_lab.corpus_read_model import LibraryTake
from cole_voice_lab.corpus_takes import take_id_for
from cole_voice_lab.errors import ContractError

from ..api import ApiError
from ..auth import AuthError
from ..capture.types import SAMPLE_RATE, CapturedTake
from ..config import ConfigError
from ..corpus.client import CorpusClient
from ..corpus.records import Notice
from ..corpus.spool import SpooledTake, SpoolError
from ..playback import PlaybackError
from ..studio import audio, quality, takes
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


#: The session's kept takes whose speech levels Inconsistent compares against (at most).
SESSION_LEVELS: Final = 50


class _HereUnread(Exception):
    """The PC's history came, but this machine's takes could not be read to compare it."""


@dataclass(frozen=True)
class _Judging:
    """A take waiting for its verdict: its WAV and samples in memory (the take under review
    moves when it is saved), and what it is judged against."""

    prompt_id: str
    wav: bytes
    samples: audio.Samples
    spoken: bool
    expected_seconds: float
    recent: tuple[float, ...]
    #: The take under review: judged now, never read from the store.
    fresh: bool
    thresholds: quality.Thresholds


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
    qualityChanged = Signal()
    #: The menu's Reject Take…: the picker opens its dialog (a reason is required).
    rejectRequested = Signal()
    #: A take was judged (any take, selected or not): the library's flagged prompts follow.
    verdictsChanged = Signal()

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
    #: The waveform's own caption: what the selected take is (D-058, item 9: errors never
    #: take its place; they are the status line's).
    takeCaption = qproperty(str, "takeCaption", takesChanged)
    canZoomIn = qproperty(bool, "canZoomIn", waveChanged)
    canZoomOut = qproperty(bool, "canZoomOut", waveChanged)
    #: The live take's peak in dBFS (``audio.FLOOR_DB`` for none), for SuiLevelMeter.
    levelPeakDb = qproperty(float, "levelPeakDb", waveChanged)
    levelFloorDb = qproperty(float, "levelFloorDb", waveChanged)
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
    # -- the selected take's quality flags (D-058): each {code, title, detail, tone}
    flags = qproperty("QVariantList", "flags", qualityChanged)
    #: "" (none: no take, or a take only the PC has), "checking" or "checked".
    qualityState = qproperty(str, "qualityState", qualityChanged)
    qualityDetail = qproperty(str, "qualityDetail", qualityChanged)

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
                "takeCaption": "",
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
                "levelPeakDb": audio.FLOOR_DB,
                "levelFloorDb": audio.FLOOR_DB,
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
                "flags": [],
                "qualityState": "",
                "qualityDetail": "",
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
        #: Where verdicts are kept (the studio sets it: ``<data dir>/studio/quality.json``).
        self.quality_store: quality.QualityStore | None = None
        #: Verdicts of takes here, by take id: this run's, and the store's as they are read.
        self._verdicts: dict[str, quality.Verdict] = {}
        #: Takes waiting for a verdict, oldest first (judged one at a time, between takes).
        self._wanted: dict[str, _Judging] = {}
        self._judging = ""
        #: Kept this session before their verdict came (their level joins it then).
        self._kept_unjudged: set[str] = set()
        #: The take under review's id (``take-<sha256>``: what Save will call it).
        self._review_id = ""
        self._spoken = True
        self._expected_seconds = 0.0
        #: The speech levels of this session's kept takes, oldest first (Inconsistent).
        self.recent_levels: list[float] = []
        #: Check Input's room floor this session, shown beside a take's levels.
        self.room_floor_dbfs: float | None = None
        #: The levels the operator calibrated (Settings → Recording), for new verdicts.
        self.thresholds = quality.Thresholds()
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

    def set_prompt(
        self,
        prompt_id: str | None,
        prompt_sha256: str,
        *,
        latest: bool,
        spoken: bool = True,
        expected_seconds: float = 0.0,
    ) -> None:
        """A new prompt (``latest``: select its latest kept take, as an explicit selection
        does; Continue Missing, Save & Next and Retake select none). ``spoken`` and
        ``expected_seconds``: what its takes' flags are judged against."""
        self.stop_playback()
        self._prompt, self._prompt_sha256 = prompt_id, prompt_sha256
        self._spoken, self._expected_seconds = spoken, expected_seconds
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
        self._set(self.takesChanged, takeCaption=self._caption())
        self._set(
            self.waveChanged,
            live=True,
            bins=[],
            slots=audio.LIVE_SLOTS,
            frameCount=0,
            waveNote="Waiting for the first frames · dB display scale",
            levelPeakDb=audio.FLOOR_DB,
            levelText="",
            canZoomIn=False,
            canZoomOut=False,
        )
        self._update_quality()

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
                    levelPeakDb=peak,
                    levelText=f"{peak:.1f} dBFS",
                )
            elif problem:
                self._set(
                    self.waveChanged,
                    waveNote=f"No live waveform: {problem}",
                    levelText="",
                    levelPeakDb=audio.FLOOR_DB,
                )

        self.context.tasks.submit(work, done)

    def end_live(self) -> None:
        if self._live is None:
            return
        self._live = None
        self._set(self.takesChanged, takeCaption=self._caption())
        self._set(self.waveChanged, live=False, levelPeakDb=audio.FLOOR_DB, levelText="")
        if self._selected is None:
            self._clear_wave("No take selected")
        self._update_quality()

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
        self._review_id = take_id_for(captured.sha256)
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
        self._review_id = ""
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
        self._judge_next()  # a verdict that waited for the take to be saved

    # -- the takes ----------------------------------------------------------------------------

    def _rebuild(self) -> None:
        mine = [take for take in self._all_local if take.prompt_id == self._prompt]
        entries = takes.merge(mine, self._pc, self._notices)
        # Attempts a retake set aside go under the others, newest first, like a take folder.
        entries.sort(key=lambda entry: entry.superseded)
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
            takeOptions=[{"value": e.key, "label": self._label(e)} for e in entries],
            history=[self._history_row(e) for e in entries],
            takeCount=len(entries),
            takeCaption=self._caption(),
        )
        self._update_actions()
        self._update_trim()
        self._update_play()

    def _label(self, entry: TakeEntry) -> str:
        """The picker's short line, with its flags' count when it has any."""
        verdict = self._verdict(entry)
        count = len(verdict.flags) if verdict is not None else 0
        return entry.label + (f" · {count} flag{'' if count == 1 else 's'}" if count else "")

    def _caption(self) -> str:
        """What the selected take is, for the waveform's caption."""
        if self._live is not None:
            return "Recording"
        entry = self._selected
        if entry is None:
            return ""
        if entry.key == REVIEW:
            return "Not saved yet"
        return f"{entry.status} · {entry.where_title}"

    def _history_row(self, entry: TakeEntry) -> dict[str, Any]:
        selected = self._selected is not None and self._selected.key == entry.key
        verdict = self._verdict(entry)
        return {
            "key": entry.key,
            "label": self._label(entry),
            "flags": ", ".join(flag.title for flag in verdict.flags) if verdict else "",
            "when": entry.when,
            "length": f"{entry.seconds:.1f} s",
            "status": entry.status,
            "where": entry.where_title,
            "origin": takes.ORIGIN_TITLES.get(entry.origin, entry.origin),
            "decision": entry.decision,
            "superseded": entry.superseded,
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
                if isinstance(error, _HereUnread):
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
            try:
                client.observe_history(history)  # a newer state of a take here is taken
            except (SpoolError, OSError) as exc:
                raise _HereUnread(str(exc) or type(exc).__name__) from exc
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
            takeCaption=self._caption(),
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
        self._update_quality()

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

        def work() -> tuple[audio.Samples, bytes | None]:
            if path is not None:
                wav = path.read_bytes()  # kept for its verdict (the PC's QC reads bytes)
                return audio.read_take_bytes(wav), wav
            return audio.read_take_bytes(client.take_audio(entry.take_id)), None

        def done(
            result: tuple[audio.Samples, bytes | None] | None, error: BaseException | None
        ) -> None:
            if self._closed or generation != self._load_generation:
                return
            if error is not None or result is None:
                known = (PlaybackError, ApiError, AuthError, ConfigError, OSError)
                if error is not None and not isinstance(error, known):
                    raise error
                self._clear_wave(f"The take cannot be shown: {error}")
                return
            samples, wav = result
            self._samples, self._samples_key = samples, entry.key
            selected = self._selected
            if selected is not None and selected.key == entry.key:
                self._show_samples(selected)
            if wav is not None:
                self._want(entry, wav, samples)

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

    # -- quality flags (D-058) ----------------------------------------------------------------

    def _verdict_id(self, entry: TakeEntry) -> str:
        return self._review_id if entry.key == REVIEW else entry.take_id

    def _verdict(self, entry: TakeEntry) -> quality.Verdict | None:
        """The take's verdict when known (this run's, or the store's once it was read)."""
        take_id = self._verdict_id(entry)
        known = self._verdicts.get(take_id)
        if known is None and take_id and entry.key != REVIEW and self.quality_store is not None:
            known = self.quality_store.peek(take_id)
            if known is not None:
                self._verdicts[take_id] = known
        return known

    def _want(self, entry: TakeEntry, wav: bytes, samples: audio.Samples) -> None:
        """Judge a take here whose verdict is not known (once loaded)."""
        take_id = self._verdict_id(entry)
        if not take_id or self._prompt is None or self._verdict(entry) is not None:
            return
        if take_id in self._wanted or take_id == self._judging:
            return
        review = entry.key == REVIEW
        self._wanted[take_id] = _Judging(
            prompt_id=self._prompt,
            wav=wav,
            samples=samples,
            spoken=self._spoken,
            expected_seconds=self._expected_seconds,
            recent=tuple(self.recent_levels) if review else (),
            fresh=review,
            thresholds=self.thresholds,
        )
        self._update_quality()
        self._judge_next()

    def _judge_next(self) -> None:
        """Judge the oldest take waiting, never while a take is recorded or saved (the PC's
        QC measures every sample in Python: Save & Next never waits on it)."""
        if self._judging or not self._wanted or self._closed or self.busy():
            return
        take_id, job = next(iter(self._wanted.items()))
        self._judging = take_id
        store = self.quality_store

        def work() -> quality.Verdict | None:
            if store is not None and not job.fresh:
                known = store.get(take_id)
                if known is not None:
                    return known
            try:
                verdict = quality.check(
                    job.wav,
                    job.samples,
                    spoken=job.spoken,
                    expected_seconds=job.expected_seconds,
                    recent=job.recent,
                    thresholds=job.thresholds,
                )
            except ContractError:
                return None  # not a PCM WAV: the take's own checks say so elsewhere
            if store is not None:
                try:
                    store.put(take_id, job.prompt_id, verdict)
                except OSError:
                    pass  # kept for this run only
            return verdict

        def done(verdict: quality.Verdict | None, error: BaseException | None) -> None:
            self._judging = ""
            self._wanted.pop(take_id, None)
            if self._closed:
                return
            if error is not None:
                raise error
            if verdict is not None:
                self._verdicts[take_id] = verdict
                if take_id in self._kept_unjudged:
                    self._kept_unjudged.discard(take_id)
                    self._add_level(verdict)
            self._rebuild()
            self._update_quality()
            if verdict is not None:
                self.verdictsChanged.emit()
            self._judge_next()

        self.context.tasks.submit(work, done)

    def note_kept(self, take_id: str) -> None:
        """A take was kept this session: its speech level joins those Inconsistent compares
        against (when its verdict comes, if it has not yet)."""
        verdict = self._verdicts.get(take_id)
        if verdict is None:
            self._kept_unjudged.add(take_id)
        else:
            self._add_level(verdict)

    def _add_level(self, verdict: quality.Verdict) -> None:
        level = verdict.levels.speech_dbfs
        if level is not None and verdict.levels.snr_db is not None:
            self.recent_levels.append(level)
            del self.recent_levels[:-SESSION_LEVELS]

    def _update_quality(self) -> None:
        entry = self._selected
        if entry is None or self._live is not None:
            self._set(self.qualityChanged, flags=[], qualityState="", qualityDetail="")
            return
        verdict = self._verdict(entry)
        if verdict is None:
            take_id = self._verdict_id(entry)
            waiting = take_id in self._wanted or (take_id != "" and take_id == self._judging)
            state = "checking" if waiting else ""
            self._set(self.qualityChanged, flags=[], qualityState=state, qualityDetail="")
            return
        self._set(
            self.qualityChanged,
            flags=[
                {"code": f.code, "title": f.title, "detail": f.detail, "tone": f.tone}
                for f in verdict.flags
            ],
            qualityState="checked",
            qualityDetail=quality.describe(verdict, room_floor_dbfs=self.room_floor_dbfs),
        )

    def quality_read(self) -> None:
        """The store was read: the takes' flags show."""
        self._rebuild()
        self._update_quality()
        self.verdictsChanged.emit()

    def room_checked(self, floor_dbfs: float) -> None:
        """Check Input measured the room: its floor is shown beside a take's levels."""
        self.room_floor_dbfs = floor_dbfs
        self._update_quality()

    # -- decisions ----------------------------------------------------------------------------

    def _update_actions(self) -> None:
        entry = self._selected
        busy = self.busy()
        decidable = entry is not None and entry.decidable and not busy
        decision = entry.decision if entry is not None else ""
        note = ""
        if entry is not None and entry.key == REVIEW:
            note = "Not saved yet: Save keeps it; Retake sets it aside (⌘Z undoes)"
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

    @Slot()
    def askReject(self) -> None:  # noqa: N802
        """Reject Take… from the menu: the picker asks for the reason."""
        if self._v["canReject"]:
            self.rejectRequested.emit()

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

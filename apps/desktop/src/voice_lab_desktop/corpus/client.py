"""The corpus client: what the prompt studio (step 6) records and decides through (D-052).

``CorpusClient`` puts the pieces together over ``<data dir>/corpus``:

- the catalog cache (``catalog.py``), pinned for this client's session when it is made;
- the spool (``spool.py``): ``begin_take`` binds the next take to a prompt of the pinned
  version before the worker starts (so a take never mixes versions, even if the session
  switches meanwhile), ``save_take`` makes it durable here at once, and ``decide``,
  ``restore`` and ``set_trim`` change it here and queue the change for the PC;
- the sync engine (``sync.py``), run off the GUI thread on the app's task pool
  (``tasks.py``): soon after every local change, and on a timer that backs off while the
  PC cannot be reached (``SyncScheduler``);
- the PC's library as last read (``library.py``), which each pass refreshes: the studio's
  coverage counts it with the takes here.

``status()`` is the whole state as a plain value (``SyncStatus``); ``add_listener`` calls
back on the GUI thread whenever it may have changed. Nothing here is a QObject: the studio's
view model (step 6) wraps it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from PySide6.QtCore import QTimer

from cole_voice_lab.api.corpus_contracts import TakeHistory
from cole_voice_lab.corpus_read_model import LibraryTake
from cole_voice_lab.corpus_take_store import TakeDecisionName
from cole_voice_lab.corpus_takes import TakeClient
from cole_voice_lab.models import Prompt, utc_now
from cole_voice_lab.recording_contracts import CatalogReference

from ..api import ApiClient
from ..capture.types import CapturedTake
from ..measurement import home_host
from ..tasks import Tasks
from .catalog import CatalogCache, PinnedCatalog
from .library import LibraryCache
from .records import Notice, RemoteOrigin
from .spool import (
    CorpusSpool,
    SpooledTake,
    SpoolError,
    default_capture_host,
    default_client,
    doubling,
    restore_target,
)
from .status import HELD_FOR_S1, NO_CATALOG, SyncStatus
from .sync import PassResult, SyncEngine, TakeUploader

if TYPE_CHECKING:
    from ..viewmodel import AppContext

#: A pass soon after a local change (changes made in a burst go together).
POKE_SECONDS: Final = 1.0
#: While the PC cannot be reached: 5 s, doubled per failed pass, at most 5 minutes.
OFFLINE_BASE_SECONDS: Final = 5.0
OFFLINE_CAP_SECONDS: Final = 300.0
#: While operations wait on the PC (a deferred take, a request its writer has not decided).
WAITING_BASE_SECONDS: Final = 10.0
WAITING_CAP_SECONDS: Final = 300.0
#: Otherwise the PC's catalog is checked this often.
IDLE_SECONDS: Final = 600.0


@dataclass(frozen=True)
class PendingTake:
    """A take about to be recorded: its private directory (the capture worker's
    ``take_dir``) and the exact prompt and catalog version it is recorded against."""

    directory: Path
    prompt: Prompt
    catalog: CatalogReference


def remote_origin(pc: LibraryTake) -> RemoteOrigin | None:
    """The PC's ``origin`` of a take (D-053: ``imported`` for a Swift take of the Mac), read
    when the PC's contracts carry it."""
    origin = getattr(pc, "origin", None)
    kind = getattr(origin, "kind", None)
    if not isinstance(kind, str) or not kind:
        return None
    source = getattr(origin, "source_take_id", None)
    before = getattr(origin, "restore_decision", None)
    return RemoteOrigin(
        kind=kind[:64],
        source_take_id=str(source)[:500] if source else None,
        restore_decision=before if before in ("kept", "draft") else None,
    )


def next_delay(result: PassResult, failures: int, waits: int) -> float | None:
    """Seconds until the next pass after ``result`` (``None``: only a local change or the UI
    starts one), with ``failures`` passes in a row that could not reach the PC and ``waits``
    passes in a row that left operations waiting."""
    if result.sign_in_required:
        return None
    if not result.reached or result.offline:
        return doubling(failures, base=OFFLINE_BASE_SECONDS, cap=OFFLINE_CAP_SECONDS)
    if result.waiting and not result.gated:
        return doubling(waits, base=WAITING_BASE_SECONDS, cap=WAITING_CAP_SECONDS)
    return IDLE_SECONDS


class SyncScheduler:
    """Runs the engine's passes on the task pool, one at a time, and times the next one.

    Lives on the GUI thread (its timer is a ``QTimer``); ``done`` hears every pass there."""

    def __init__(
        self,
        engine: SyncEngine,
        tasks: Tasks,
        *,
        done: Callable[[PassResult | None, BaseException | None], None],
    ) -> None:
        self.engine = engine
        self.tasks = tasks
        self._done = done
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self.busy = False
        self.stopped = False
        self._again = False
        self._force = False
        self.failures = 0
        self.waits = 0

    def request(self, seconds: float = 0.0, *, force: bool = False) -> None:
        """A pass in ``seconds`` (sooner than one already timed; ``force``: operations in a
        backoff go too)."""
        if self.stopped:
            return
        self._force = self._force or force
        if self.busy:
            self._again = True
            return
        milliseconds = int(seconds * 1000)
        if not self._timer.isActive() or self._timer.remainingTime() > milliseconds:
            self._timer.start(milliseconds)

    def stop(self) -> None:
        self.stopped = True
        self._timer.stop()

    def _fire(self) -> None:
        if self.stopped or self.busy:
            return
        self.busy = True
        force, self._force = self._force, False
        self.tasks.submit(lambda: self.engine.run_pass(force=force), self._finished)

    def _finished(self, result: PassResult | None, error: BaseException | None) -> None:
        self.busy = False
        delay: float | None
        if result is not None:
            self.failures = self.failures + 1 if (not result.reached or result.offline) else 0
            self.waits = self.waits + 1 if (result.reached and result.waiting) else 0
            delay = next_delay(result, self.failures, self.waits)
        else:
            self.failures += 1
            delay = doubling(self.failures, base=OFFLINE_BASE_SECONDS, cap=OFFLINE_CAP_SECONDS)
        if self._again:
            self._again = False
            delay = 0.0
        if delay is not None and not self.stopped:
            self.request(delay)
        self._done(result, error)


class CorpusClient:
    """The corpus on this machine and its sync to the PC, over ``root``
    (``<data dir>/corpus``). Made on the GUI thread; one per app run (a new one is a new
    session, which pins the PC's active catalog version as last seen)."""

    def __init__(
        self,
        root: Path,
        *,
        api: Callable[[], ApiClient],
        uploader: Callable[[], TakeUploader],
        enabled: Callable[[], bool],
        tasks: Tasks | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        take_client: TakeClient | None = None,
        capture_host: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.take_client = take_client or default_client()
        self.capture_host = capture_host or default_capture_host()
        self.spool = CorpusSpool(
            self.root, clock=clock, client=self.take_client, capture_host=self.capture_host
        )
        self.catalog = CatalogCache(self.root / "catalog", clock=clock)
        self.catalog.open_session()
        self.library = LibraryCache(self.root / "library", clock=clock)
        self.enabled = enabled
        self.engine = SyncEngine(
            self.spool,
            self.catalog,
            api=api,
            uploader=uploader,
            enabled=enabled,
            clock=clock,
            sleep=sleep,
            monotonic=monotonic,
            library=self.library,
        )
        self._tasks = tasks
        self._listeners: list[Callable[[], None]] = []
        self._last: PassResult | None = None
        self.scheduler = (
            SyncScheduler(self.engine, tasks, done=self._pass_done) if tasks is not None else None
        )

    @classmethod
    def from_context(cls, context: AppContext) -> CorpusClient:
        """The app's client: its settings (read at each pass, so a change in Settings holds
        from the next one), its sign-in, its task pool, and the approved node over the
        tailnet."""

        def uploader() -> TakeUploader:
            config = context.config
            return TakeUploader(
                library_dir=config.library_dir,
                server_url=config.require_server(),
                bearer=context.session.bearer,
                client_factory=home_host.tailnet_client,
            )

        return cls(
            context.config.corpus_dir,
            api=context.api,
            uploader=uploader,
            enabled=lambda: context.config.corpus_sync,
            tasks=context.tasks,
        )

    # -- running ----------------------------------------------------------------------------

    def start(self) -> None:
        """The first pass now (the PC's catalog, then whatever is queued)."""
        if self.scheduler is not None:
            self.scheduler.request(0.0)

    def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.stop()

    def sync_now(self) -> None:
        """A pass now, operations in a backoff included (the UI's "Sync now"). Safe from any
        thread."""
        scheduler = self.scheduler
        if scheduler is None:
            return

        def now() -> None:
            scheduler.request(0.0, force=True)
            self._call_listeners()

        self._on_gui_thread(now)

    def run_pass(self, *, force: bool = False) -> PassResult:
        """One pass on this thread (the command line and tests)."""
        result = self.engine.run_pass(force=force)
        self._last = result
        return result

    def _on_gui_thread(self, work: Callable[[], object]) -> None:
        """``work`` on the GUI thread: the scheduler's timer and the listeners live there,
        and a take may be saved from a worker (``save_take`` hashes the WAV)."""
        if self._tasks is None:
            work()
        else:
            self._tasks.on_gui_thread(work)

    def _poke(self) -> None:
        """A local change: a pass soon, and the listeners told (on the GUI thread)."""
        scheduler = self.scheduler

        def poke() -> None:
            if scheduler is not None:
                scheduler.request(POKE_SECONDS)
            self._call_listeners()

        self._on_gui_thread(poke)

    def _pass_done(self, result: PassResult | None, error: BaseException | None) -> None:
        if result is not None:
            self._last = result
        self._call_listeners()  # the task pool's results arrive on the GUI thread
        if error is not None:
            raise error  # a bug: the engine reports what it can describe in its result

    def add_listener(self, listener: Callable[[], None]) -> None:
        """``listener()`` on the GUI thread whenever the status may have changed."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        self._on_gui_thread(self._call_listeners)

    def _call_listeners(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    # -- the catalog --------------------------------------------------------------------------

    @property
    def pinned(self) -> PinnedCatalog | None:
        """The catalog version this session records against."""
        return self.catalog.pinned

    def start_new_session(self) -> PinnedCatalog | None:
        """Pin the PC's active version as last seen (the UI's switch, between takes)."""
        pinned = self.catalog.open_session()
        self._notify()
        return pinned

    # -- takes ----------------------------------------------------------------------------------

    def begin_take(self, prompt_id: str) -> PendingTake:
        """A new take of ``prompt_id`` of the pinned version: its recording directory.

        Raises: ``SpoolError`` with no catalog pinned, or a prompt the version does not hold."""
        pinned = self.catalog.pinned
        if pinned is None:
            raise SpoolError(NO_CATALOG)
        try:
            prompt = pinned.prompt(prompt_id)
        except KeyError as exc:
            raise SpoolError(
                f"catalog version {pinned.reference.catalog_version} has no prompt {prompt_id}"
            ) from exc
        return PendingTake(
            self.spool.begin_recording(prompt, pinned.reference), prompt, pinned.reference
        )

    def save_take(self, pending: PendingTake, captured: CapturedTake) -> SpooledTake:
        """Save the worker's validated take: durable here at once, its upload queued.

        Raises: ``SpoolError``."""
        take = self.spool.save_take(
            pending.directory,
            captured,
            prompt=pending.prompt,
            catalog=pending.catalog,
            client=self.take_client,
            capture_host=self.capture_host,
        )
        self._poke()
        return take

    def discard_take(self, pending: PendingTake) -> None:
        """A take that failed or was abandoned before it was saved."""
        self.spool.discard_recording(pending.directory)

    def takes(self) -> list[SpooledTake]:
        return self.spool.takes()

    def takes_for_prompt(self, prompt_id: str) -> list[SpooledTake]:
        return self.spool.takes_for_prompt(prompt_id)

    def decide(
        self, take_id: str, decision: TakeDecisionName, reason: str | None = None
    ) -> SpooledTake:
        take = self.spool.decide(take_id, decision, reason)
        self._poke()
        return take

    def restore(self, take_id: str, to: TakeDecisionName | None = None) -> SpooledTake:
        """Back to the decision before; ``to`` when that is not known here (``SpoolError``
        ``RESTORE_UNKNOWN`` without it: the UI asks)."""
        take = self.spool.restore(take_id, to)
        self._poke()
        return take

    def restore_target(self, take_id: str) -> TakeDecisionName | None:
        """What Restore would set the take back to (``None``: not known here)."""
        return restore_target(self.spool.take(take_id).sync)

    @property
    def ready(self) -> bool:
        """This machine's takes were listed (what an earlier run left was recovered)."""
        return self.spool.listed

    def prepare(self) -> None:
        """The first listing (with the recovery of what an earlier run left) and the PC's
        library as last read, parsed: slow work for a worker thread at start."""
        self.spool.takes()
        _ = self.library.latest

    def set_trim(self, take_id: str, start_frame: int | None, end_frame: int | None) -> SpooledTake:
        take = self.spool.set_trim(take_id, start_frame, end_frame)
        self._poke()
        return take

    # -- the PC's takes (read only; call off the GUI thread) --------------------------------------

    def prompt_history(self, prompt_id: str) -> TakeHistory:
        """Every take the PC counts for ``prompt_id`` of its active version, newest first.

        Raises: ``ConfigError`` without a server; the API's errors (``ApiError``,
        ``SignInRequired``)."""
        return self.engine.api().prompt_takes(prompt_id)

    def remote_take(self, pc: LibraryTake, prompt_id: str) -> SpooledTake:
        """A take of the PC's history recorded elsewhere, noted here (``learn_remote``) so
        that ``decide``, ``restore`` and ``set_trim`` work on it; one already here is
        refreshed from ``pc`` instead. Its origin (a Swift take imported from the Mac) is the
        PC's.

        Raises: ``SpoolError``."""
        take = self.spool.learn_remote(pc, prompt_id, origin=remote_origin(pc))
        self._notify()
        return take

    def observe_history(self, history: TakeHistory) -> int:
        """The PC's history of a prompt, read again: each take here takes a newer state the
        PC shows (never an older one). Returns: how many changed (the listeners hear it)."""
        changed = 0
        known = {take.take_id for take in self.spool.takes()}
        for pc in history.takes:
            if pc.take_id in known and self.spool.observe(pc.take_id, pc):
                changed += 1
        if changed:
            self._notify()
        return changed

    def take_audio(self, take_id: str) -> bytes:
        """A take's ``original.wav`` from the PC, in memory (never written here, D-038); the
        PC serves it only under its playback approval.

        Raises: as ``prompt_history``."""
        return self.engine.api().take_audio(take_id)

    # -- what it says -------------------------------------------------------------------------

    def notices(self) -> tuple[Notice, ...]:
        return self.spool.notices()

    def dismiss_notice(self, notice_id: str) -> None:
        self.spool.dismiss(notice_id)
        self._notify()

    def status(self, *, wait: bool = True) -> SyncStatus:
        """The client now. ``wait=False`` never waits for the first listing (the GUI thread
        while it runs elsewhere): the takes are then the ones listed so far."""
        takes = self.spool.takes() if (wait or self.spool.listed) else self.spool.peek()
        record = self.spool.sync_record()
        last = self._last
        state = self.catalog.state
        latest = self.catalog.latest
        pinned = self.catalog.pinned
        enabled = bool(self.enabled())
        return SyncStatus(
            enabled=enabled,
            held="" if enabled else HELD_FOR_S1,
            busy=self.scheduler is not None and self.scheduler.busy,
            offline=last.offline if last is not None else False,
            sign_in_required=last.sign_in_required if last is not None else False,
            last_sync_at=record.last_sync_at,
            last_attempt_at=record.last_attempt_at,
            last_error=record.last_error or "",
            upload_blocked=last.upload_blocked if last is not None else "",
            takes=sum(1 for take in takes if not take.remote),
            queued=sum(len(take.sync.outbox) for take in takes),
            waiting_takes=sum(
                1 for take in takes if take.sync.outbox or take.sync.refresh is not None
            ),
            refreshing=sum(1 for take in takes if take.sync.refresh is not None),
            awaiting_receipt=sum(
                1
                for take in takes
                if take.sync.outbox
                and take.sync.outbox[0].kind == "upload"
                and getattr(take.sync.outbox[0], "awaiting_receipt", False)
            ),
            sent=sum(take.sync.sent for take in takes),
            refused=sum(take.sync.refused for take in takes),
            dropped=sum(take.sync.dropped for take in takes),
            local_only=sum(1 for take in takes if take.sync.upload == "local-only"),
            damaged=tuple(self.spool.damaged),
            unsaved_recordings=tuple(self.spool.unsaved),
            pinned_catalog=pinned.reference if pinned is not None else None,
            pc_catalog=latest.reference if latest is not None else None,
            pc_catalog_unavailable=state.pc_unavailable,
            switch_pending=self.catalog.switch_pending,
            can_record=pinned is not None,
            record_blocked="" if pinned is not None else NO_CATALOG,
            notices=self.spool.notices(),
            ready=self.spool.listed,
        )

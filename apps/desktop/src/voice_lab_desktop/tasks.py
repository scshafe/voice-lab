"""Slow work off the GUI thread, results back on it.

The app's view models never block the GUI thread on the network or the disk: they hand a
callable to :class:`Tasks`, which runs it on a small pool and delivers ``done(result,
error)`` on the GUI thread through a queued signal, so Qt properties only ever change on
the GUI thread (as voice-journey-capture's controller did).
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

type Done[T] = Callable[[T | None, BaseException | None], None]


class _Bridge(QObject):
    """Runs callables on the thread it lives on (the GUI thread), queued."""

    run = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.run.connect(self._call, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _call(self, fn: Callable[[], None]) -> None:
        fn()


class Tasks:
    """A small worker pool whose results come back on the GUI thread."""

    def __init__(self, workers: int = 4, name: str = "vld") -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._bridge = _Bridge()
        self.closed = False

    def submit[T](self, work: Callable[[], T], done: Done[T]) -> None:
        """Run ``work`` on the pool; ``done(result, None)`` or ``done(None, error)`` follows
        on the GUI thread (never after :meth:`close`)."""
        if self.closed:
            return

        def run() -> None:
            result: T | None = None
            error: BaseException | None = None
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - handed to the GUI thread
                error = exc
            if not self.closed:
                self._bridge.run.emit(lambda: None if self.closed else done(result, error))

        self._pool.submit(run)

    def on_gui_thread(self, fn: Callable[[], Any]) -> None:
        """Run ``fn`` on the GUI thread soon (for progress from a worker)."""
        if not self.closed:
            self._bridge.run.emit(fn)

    def close(self) -> None:
        self.closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)

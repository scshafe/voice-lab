"""The PC's one GPU slot (D-008 as amended by D-026; D-046).

@canonical speech.gpu -- every GPU job on the home host (speech now; fine-tuning, auditions
and Speak when phase 5 brings them to the PC) holds an exclusive ``flock`` on
``<root>/.gpu-slot.lock`` while it uses the GPU, and names itself in ``<root>/gpu-slot.json``
(``GpuSlotHolder``): its kind, its job, when it started and when it expects to end. The lock
is the truth: a holder record whose lock nobody holds is stale and is ignored, so a crash
never wedges the slot.

The rules, for every kind: nobody preempts a holder. A long job (anything but speech) that
is about to take the slot first checks ``speech_waiting`` and lets queued speech go first.
While a long job holds the slot, the API refuses new speech at once (``503 gpu_busy``) with
the holder's expected end as ``Retry-After``, so an assistant answers in text rather than
waiting hours.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Final

from ..canonical import open_regular_no_follow, write_canonical_json
from .contracts import GpuSlotHolder

LOCK_NAME: Final = ".gpu-slot.lock"
HOLDER_NAME: Final = "gpu-slot.json"
MAX_HOLDER_BYTES: Final = 4096


class GpuSlotTimeout(Exception):
    """The slot stayed held for longer than the caller would wait."""


def _open_lock(root: Path) -> int:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def current_holder(root: Path) -> GpuSlotHolder | None:
    """Who holds the slot now, or ``None`` when nobody does (a stale record is ignored)."""
    if not (root / LOCK_NAME).exists():
        return None
    descriptor = _open_lock(root)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EWOULDBLOCK, errno.EAGAIN}:  # pragma: no cover
                raise
        else:
            return None  # nobody holds the exclusive lock
        try:
            with open_regular_no_follow(root / HOLDER_NAME) as stream:
                return GpuSlotHolder.model_validate_json(stream.read(MAX_HOLDER_BYTES))
        except Exception:  # noqa: BLE001 (a holder that wrote no record yet, or a torn one)
            return None
    finally:
        os.close(descriptor)


def busy_with_long_job(root: Path, *, now: datetime, default_seconds: int) -> int | None:
    """Seconds to wait when a long job holds the slot (for ``Retry-After``), else ``None``."""
    holder = current_holder(root)
    if holder is None or holder.kind == "speech":
        return None
    if holder.expected_end_at is None or holder.expected_end_at <= now:
        return default_seconds
    return max(1, int((holder.expected_end_at - now).total_seconds()) + 1)


def speech_waiting(queue: Path) -> bool:
    """Whether speech is queued (a long job then lets it take the slot first)."""
    try:
        return any(not name.startswith(".") for name in os.listdir(queue))
    except FileNotFoundError:
        return False


@contextmanager
def hold_slot(
    root: Path,
    holder: GpuSlotHolder,
    *,
    timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 0.5,
) -> Iterator[None]:
    """Hold the slot for ``holder`` (waiting at most ``timeout_seconds`` for it).

    Raises: ``GpuSlotTimeout`` when the slot stays held that long.
    """
    descriptor = _open_lock(root)
    try:
        waited = 0.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EWOULDBLOCK, errno.EAGAIN}:  # pragma: no cover
                    raise
            if waited >= timeout_seconds:
                raise GpuSlotTimeout("the GPU slot stayed held")
            sleep(poll_seconds)
            waited += poll_seconds
        write_canonical_json(root / HOLDER_NAME, holder)
        try:
            yield
        finally:
            (root / HOLDER_NAME).unlink(missing_ok=True)
    finally:
        os.close(descriptor)

"""The PC's one GPU slot (D-008 as amended by D-026; D-046).

@canonical speech.gpu -- every GPU job on the home host (speech and model benchmarks now;
fine-tuning, auditions and Speak when phase 5 brings them to the PC) holds an exclusive ``flock`` on
``<root>/.gpu-slot.lock`` while it uses the GPU, and names itself in ``<root>/gpu-slot.json``
(``GpuSlotHolder``): its kind, its job, when it started and when it expects to end. The lock
is the truth: a holder record whose lock nobody holds is stale and is ignored, so a crash
never wedges the slot.

The rules, for every kind: nobody preempts a holder. The resident speech worker holds the
slot while its model is loaded (``GpuSlotLease``). A long job (anything but speech) that wants
the slot writes ``<root>/gpu-slot.wanted`` (``want_slot``: its holder, its pid, the boot it
was written in and how long it will wait) and then waits for the lock; the speech worker gives
the slot up as soon as it has no speech job in hand and none is queued (``slot_wanted``), and
does not take it again while the marker is there and its queue is empty. So queued speech goes
first and a long job waits for the speech queue to drain, never for the resident worker's idle
timeout. A want whose process died, which was written before the host last booted, or which
is past its wait is stale: ``slot_wanted`` removes it and says so, so a killed job never keeps
speech off the GPU. While a long job holds the slot, the API refuses new speech at once
(``503 gpu_busy``) with the holder's expected end as ``Retry-After``, so an assistant answers
in text rather than waiting hours.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from ..canonical import open_regular_no_follow, write_canonical_json
from ..models import utc_now
from .contracts import GpuSlotHolder, GpuSlotWant

LOCK_NAME: Final = ".gpu-slot.lock"
HOLDER_NAME: Final = "gpu-slot.json"
WANTED_NAME: Final = "gpu-slot.wanted"
MAX_HOLDER_BYTES: Final = 4096
# A want's grace past the wait it announced, and the age after which a want that cannot be
# read (written by something else, or by hand) is taken as stale.
WANT_GRACE_SECONDS: Final = 60
UNREADABLE_WANT_SECONDS: Final = 3600
DEFAULT_WANT_SECONDS: Final = 3600.0
BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")


class GpuSlotTimeout(Exception):
    """The slot stayed held for longer than the caller would wait."""


class GpuSlotAbandoned(Exception):
    """The caller stopped waiting for the slot (a cancel arrived while it waited)."""


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


def boot_id(path: Path = BOOT_ID_PATH) -> str:
    """This boot's id (Linux's ``boot_id``; ``unknown`` where the host has none)."""
    try:
        return path.read_text(encoding="ascii").strip()[:64] or "unknown"
    except (OSError, UnicodeDecodeError):
        return "unknown"


def process_alive(pid: int) -> bool:
    """Whether process ``pid`` exists (one of another user's counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def want_slot(
    root: Path,
    holder: GpuSlotHolder,
    *,
    wait_seconds: float = DEFAULT_WANT_SECONDS,
    now: datetime | None = None,
) -> None:
    """Say that a long job wants the slot (it then waits at most ``wait_seconds`` for the lock,
    and withdraws); the want names this process and boot, so a reader can tell it is stale."""
    written = utc_now() if now is None else now
    write_canonical_json(
        root / WANTED_NAME,
        GpuSlotWant(
            holder=holder,
            pid=os.getpid(),
            boot_id=boot_id(),
            expires_at=written + timedelta(seconds=max(0.0, wait_seconds) + WANT_GRACE_SECONDS),
        ),
    )


def withdraw_want(root: Path) -> None:
    (root / WANTED_NAME).unlink(missing_ok=True)


def _stale_reason(
    path: Path, now: datetime, alive: Callable[[int], bool], boot: Callable[[], str]
) -> tuple[str | None, tuple[int, int]]:
    """Why the want at ``path`` is stale (``None``: it is not), and the identity (device,
    inode) of the file that was judged."""
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        identity = (metadata.st_dev, metadata.st_ino)
        data = stream.read(MAX_HOLDER_BYTES)
    try:
        want = GpuSlotWant.model_validate_json(data)
    except Exception:  # noqa: BLE001 (another writer's marker, or one written by hand)
        age = now.timestamp() - metadata.st_mtime
        return (f"unreadable and {int(age)} s old" if age > UNREADABLE_WANT_SECONDS else None), (
            identity
        )
    if want.boot_id != boot():
        reason = f"{want.holder.kind} {want.holder.job_id} wanted it before the host rebooted"
    elif not alive(want.pid):
        reason = f"{want.holder.kind} {want.holder.job_id}'s process {want.pid} is gone"
    elif want.expires_at <= now:
        reason = f"{want.holder.kind} {want.holder.job_id} is past its wait"
    else:
        reason = None
    return reason, identity


def _unlink_if_same(path: Path, identity: tuple[int, int]) -> bool:
    """Remove ``path`` only while it is still the file judged stale: it is first moved aside
    (atomically), and a want written meanwhile is put back, never removed."""
    aside = path.with_name(f".{path.name}.stale-{os.getpid()}-{time.monotonic_ns()}")
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return False
    metadata = os.lstat(aside)
    if (metadata.st_dev, metadata.st_ino) == identity:
        os.unlink(aside)
        return True
    try:
        os.link(aside, path)  # a newer want: restore it unless yet another replaced it
    except FileExistsError:
        pass
    os.unlink(aside)
    return False


def slot_wanted(
    root: Path,
    *,
    now: datetime | None = None,
    alive: Callable[[int], bool] = process_alive,
    boot: Callable[[], str] = boot_id,
) -> bool:
    """Whether a long job is waiting for the slot; a stale want is removed (and logged), but
    only that file: a want written while it was judged stays."""
    path = root / WANTED_NAME
    moment = utc_now() if now is None else now
    try:
        reason, identity = _stale_reason(path, moment, alive, boot)
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 (not a regular file: judged by its age, as unreadable)
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return False
        age = moment.timestamp() - metadata.st_mtime
        reason = (
            f"not a regular file and {int(age)} s old" if age > UNREADABLE_WANT_SECONDS else None
        )
        identity = (metadata.st_dev, metadata.st_ino)
    if reason is None:
        return True
    if not _unlink_if_same(path, identity):
        return os.path.lexists(path)  # a newer want replaced it meanwhile: it counts
    print(f"voice-lab gpu slot: removed a stale want ({reason})", file=sys.stderr, flush=True)
    return False


class GpuSlotLease:
    """The slot held across jobs (the resident speech worker's), taken and given up by hand."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._descriptor: int | None = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(
        self,
        holder: GpuSlotHolder,
        *,
        timeout_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 0.5,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        """Take the slot for ``holder`` (no-op when already held).

        Raises: ``GpuSlotTimeout`` when it stays held by another for ``timeout_seconds``;
        ``GpuSlotAbandoned`` when ``stop()`` turns true while it waits.
        """
        if self._descriptor is not None:
            return
        descriptor = _open_lock(self.root)
        waited = 0.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EWOULDBLOCK, errno.EAGAIN}:  # pragma: no cover
                    os.close(descriptor)
                    raise
            if waited >= timeout_seconds:
                os.close(descriptor)
                raise GpuSlotTimeout("the GPU slot stayed held")
            if stop is not None and stop():
                os.close(descriptor)
                raise GpuSlotAbandoned("the wait for the GPU slot was abandoned")
            sleep(poll_seconds)
            waited += poll_seconds
        try:
            write_canonical_json(self.root / HOLDER_NAME, holder)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        (self.root / HOLDER_NAME).unlink(missing_ok=True)
        os.close(self._descriptor)
        self._descriptor = None

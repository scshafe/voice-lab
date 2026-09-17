"""Write-once blobs and compare-and-set refs in one private local directory.

@canonical storage_local -- LocalBlobStore and LocalRefStore implement BlobStore and RefStore
on one local directory, which is the namespace: every entry is an owned private file or
directory that is checked and never repaired, a blob is published once with
rename_no_replace_at and checked against its ref on every read, a ref changes only under
.refs.lock and is durable before swap returns, and temporaries exist only under tmp/, where
sweep_leftovers removes them only while no writer can be active.
Related: storage_ports.py (the ports and the STORAGE.* errors), storage_contracts.py (BlobRef,
RefValue, ref names), canonical.py (rename_no_replace_at), storage_binding.py (the composition
root), tests/storage_conformance.py (the checks every store passes),
tests/test_storage_local.py (this module's own checks).

Layout of a store root (directories 0700, files 0600, all owned by the current user):

    <root>/blobs/<first two hex>/<sha256>          one blob; never replaced or removed
    <root>/refs/<segment>.d/.../<last segment>.json  one ref value (canonical JSON + newline)
    <root>/tmp/<pid>-<32 hex>.tmp                  a writer's temporary
    <root>/.refs.lock                              held exclusively by a swap
    <root>/.tmp.lock                               held shared by writers, exclusively by a sweep

A ref's directories carry a ``.d`` suffix and its file a ``.json`` suffix, so the ref ``a`` and
the refs under ``a.json/`` can never claim the same entry. Nothing else may be in the root.

Checks. The root must be an absolute path with no symlink anywhere in it. The root and every
entry under it must be owned by this process's user with no group or other permission bits,
a file must have exactly one link (a ref file that a concurrent swap replaced after this reader
opened it has none, and still holds a complete value), and no entry may be a symlink. A
violation raises ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) naming the path problem;
nothing is chmod-ed except directories this store has just created, and those only through a
descriptor opened without following symlinks. Every operation opens the root and each
directory below it by descriptor without following symlinks and checks that the root is still
the directory the store was opened on. A ref that did not pass ``BlobRef`` validation (built
with ``model_construct``) is refused with ``STORAGE.PROTOCOL`` before any I/O, whether it is
read or would be written as a swap's target.

Concurrency and durability. Readers take no lock. A blob is written to a temporary, fsynced
and renamed from the ``tmp/`` descriptor into the shard descriptor with
``rename_no_replace_at``; the writer then checks through the shard descriptor that the entry
at the blob's name is the file it wrote. When a put finds the blob already stored (before it
writes, or because another writer published the same bytes first), the stored blob is
verified, and it and its shard directory are fsynced before the ref is returned, so a blob
published by a writer that died before syncing is durable before anyone relies on it. A
directory the store needs is fsynced into its parent whether this call made it or found it.
A swap holds ``.refs.lock`` (polled for at most ``lock_timeout`` seconds, then
``STORAGE.UNAVAILABLE``) while it compares versions, writes a temporary, ``os.replace``-s the
ref file and fsyncs its directory.

Crash leftovers. Only temporaries can be left behind, and they are never visible as a blob or a
ref. A writer holds ``.tmp.lock`` shared from before it creates its temporary until the
temporary has been renamed or removed. ``sweep_leftovers`` (run by every constructor) takes
``.tmp.lock`` exclusively without waiting: if that fails, some writer is active and nothing is
examined; if it succeeds, no writer can exist, so every temporary is a leftover and is
removed. There is no window in which a live writer's temporary looks abandoned, and no time
threshold is involved. A sweep can be skipped while writers are busy; the next construction
tries again.

A filesystem that cannot rename without replacing never will, so a put there fails with
``STORAGE.PROTOCOL`` (not retryable). Opening a lock, blob or ref file fails with
``STORAGE.INTEGRITY`` when the entry is not an owned private regular file (a socket, for
example), whatever error the open itself reported. Any other ``OSError`` becomes
``STORAGE.QUOTA`` (no space or quota) or ``STORAGE.UNAVAILABLE`` (retryable). This module
imports only the standard library and the package's ``canonical``, ``storage_contracts`` and
``storage_ports`` modules.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from .canonical import (
    NoReplaceRenameUnavailableError,
    canonical_json_bytes,
    file_identity,
    fsync_directory,
    rename_no_replace_at,
)
from .storage_contracts import MAX_STORAGE_COUNTER, StorageNameError, validate_ref_name
from .storage_ports import (
    MAX_BLOB_BYTES,
    BlobRef,
    RefValue,
    StorageError,
    StorageErrorCode,
    storage_error,
)

__all__ = [
    "BLOBS_DIRECTORY",
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "MAX_REF_FILE_BYTES",
    "REFS_DIRECTORY",
    "REFS_LOCK",
    "REF_DIRECTORY_SUFFIX",
    "REF_FILE_SUFFIX",
    "STORE_MEMBERS",
    "TEMPORARY_DIRECTORY",
    "TEMPORARY_LOCK",
    "LeftoverSweep",
    "LocalBlobStore",
    "LocalRefStore",
]

BLOBS_DIRECTORY: Final = "blobs"
REFS_DIRECTORY: Final = "refs"
TEMPORARY_DIRECTORY: Final = "tmp"
REFS_LOCK: Final = ".refs.lock"
TEMPORARY_LOCK: Final = ".tmp.lock"
STORE_MEMBERS: Final = frozenset(
    {BLOBS_DIRECTORY, REFS_DIRECTORY, TEMPORARY_DIRECTORY, REFS_LOCK, TEMPORARY_LOCK}
)
REF_DIRECTORY_SUFFIX: Final = ".d"
REF_FILE_SUFFIX: Final = ".json"
# A ref value is a few hundred bytes; anything longer is refused before it is parsed.
MAX_REF_FILE_BYTES: Final = 4 * 1024
DEFAULT_LOCK_TIMEOUT_SECONDS: Final = 10.0
MAX_LOCK_TIMEOUT_SECONDS: Final = 3600.0
_LOCK_POLL_SECONDS: Final = 0.01
_READ_CHUNK_BYTES: Final = 1024 * 1024
_TEMPORARY_NAME: Final = re.compile(r"[0-9]{1,10}-[0-9a-f]{32}\.tmp")
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Non-blocking, so a FIFO planted at a file's name cannot block the open; regular files ignore
# the flag for reads.
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_NEW_FILE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCK_FLAGS: Final = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
# What opening a symlink (or, for a directory open, a non-directory) without following reports.
_NOT_A_DIRECTORY_ERRNOS: Final = frozenset({errno.ENOTDIR, errno.ELOOP, errno.EMLINK})
_NOT_A_FILE_ERRNOS: Final = frozenset({errno.ELOOP, errno.EMLINK, errno.EISDIR})
_NO_SPACE_ERRNOS: Final = frozenset({errno.ENOSPC, errno.EDQUOT})


@dataclass(frozen=True, slots=True)
class LeftoverSweep:
    """What one ``sweep_leftovers`` call did.

    ``removed`` names the temporaries removed from ``tmp/``. ``writers_active`` is ``True``
    when a writer held ``.tmp.lock``, in which case nothing was examined or removed.
    """

    removed: tuple[str, ...]
    writers_active: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _integrity(message: str) -> StorageError:
    return storage_error("STORAGE.INTEGRITY", message)


@contextmanager
def _os_errors(action: str) -> Iterator[None]:
    """Turn any ``OSError`` raised inside into ``STORAGE.QUOTA`` or ``STORAGE.UNAVAILABLE``."""
    try:
        yield
    except OSError as error:
        code: StorageErrorCode = (
            "STORAGE.QUOTA" if error.errno in _NO_SPACE_ERRNOS else "STORAGE.UNAVAILABLE"
        )
        raise storage_error(code, f"{action} failed: {error}") from error


def _private_problem(
    metadata: os.stat_result, *, directory: bool, unlinked_ok: bool = False
) -> str | None:
    """Why ``metadata`` is not an owned private entry of its kind; ``None`` when it is.

    A directory's type is not checked: directories are only ever opened with ``O_DIRECTORY``.
    """
    if not directory and not stat.S_ISREG(metadata.st_mode):
        return "is not a regular file (a symlink, directory or special file)"
    if metadata.st_uid != os.getuid():
        return f"is owned by uid {metadata.st_uid}, not {os.getuid()}"
    if metadata.st_mode & 0o077:
        return f"has group or other permissions (mode {stat.S_IMODE(metadata.st_mode):#o})"
    if not directory and metadata.st_nlink != 1 and not (unlinked_ok and metadata.st_nlink == 0):
        return f"has {metadata.st_nlink} hard links"
    return None


def _require_private(
    metadata: os.stat_result, label: Path, *, directory: bool, unlinked_ok: bool = False
) -> None:
    problem = _private_problem(metadata, directory=directory, unlinked_ok=unlinked_ok)
    if problem is not None:
        kind = "directory" if directory else "file"
        raise _integrity(f"the local store refuses a {kind} that {problem}: {label}")


def _checked_root(root: Path) -> Path:
    text = os.fspath(root)
    if not os.path.isabs(text) or os.path.normpath(text) != text:
        raise _integrity(f"a local store root is an absolute, normalized path: {text}")
    resolved = os.path.realpath(text)
    if resolved != text:
        raise _integrity(
            f"a local store root has no symlink in its path (it resolves to {resolved}): {text}"
        )
    return Path(text)


def _open_directory(parent: int | None, name: str | Path, label: Path) -> int | None:
    """Open an owned private directory without following symlinks; ``None`` when missing.

    ``parent`` is the descriptor ``name`` is relative to, or ``None`` for an absolute path.
    """
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in _NOT_A_DIRECTORY_ERRNOS:
            raise _integrity(
                f"the local store refuses a symlink or non-directory where a directory "
                f"belongs: {label}"
            ) from error
        raise OSError(error.errno, error.strerror, str(label)) from error
    try:
        _require_private(os.fstat(descriptor), label, directory=True)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_directory(parent: int, name: str, label: Path) -> int:
    """Open an owned private directory that must exist."""
    descriptor = _open_directory(parent, name, label)
    if descriptor is None:
        raise _integrity(
            f"the local store is missing a directory: {label} (only a new store is opened "
            "with create=True)"
        )
    return descriptor


def _make_directory(parent: int, name: str, label: Path) -> None:
    """Create a private child directory unless an entry of that name exists (checked later).

    A new directory is opened without following symlinks and given 0700 through that
    descriptor. Either way the parent is fsynced, so an entry that a writer which died before
    syncing created is durable too.
    """
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        os.fsync(parent)
        return
    descriptor = _open_directory(parent, name, label)
    if descriptor is None:
        raise _integrity(f"a directory the local store just created vanished: {label}")
    try:
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    os.fsync(parent)


def _open_file(parent: int, name: str, label: Path, flags: int) -> int | None:
    """Open a file without following a symlink; ``None`` when missing.

    Raises: ``StorageIntegrityError`` when the entry is a symlink or directory, or when the
    open failed and the entry is not an owned private regular file (a socket, for example);
    ``OSError`` naming ``label`` for any other failure.
    """
    try:
        return os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in _NOT_A_FILE_ERRNOS:
            raise _integrity(
                f"the local store refuses a symlink or directory where a file belongs: {label}"
            ) from error
        failure = error
    # The error a special file gives depends on its kind and the platform (EOPNOTSUPP or ENXIO
    # for a socket), so the entry itself decides whether this is an unsafe entry.
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        metadata = None
    if metadata is not None:
        _require_private(metadata, label, directory=False)
    raise OSError(failure.errno, failure.strerror, str(label)) from failure


def _checked_ref(ref: BlobRef) -> BlobRef:
    """``ref`` validated again, so a ``BlobRef`` built without validation reaches no file.

    Raises: ``StorageError`` (``STORAGE.PROTOCOL``) when ``ref`` is not a valid ``BlobRef``.
    """
    if isinstance(ref, BlobRef):
        with suppress(ValueError):
            return BlobRef.model_validate(dict(ref))
    sha256 = getattr(ref, "sha256", None)
    size = getattr(ref, "size", None)
    raise storage_error(
        "STORAGE.PROTOCOL",
        f"not a valid blob ref (a SHA-256 digest and a size of 0 to {MAX_BLOB_BYTES} bytes): "
        f"sha256={sha256!r:.80}, size={size!r:.40}",
    )


def _open_lock(parent: int, name: str, label: Path, *, create: bool) -> int:
    """Open one of the store's lock files, creating it only when ``create`` is set."""
    if create:
        try:
            descriptor = os.open(name, _LOCK_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            os.fchmod(descriptor, 0o600)
            os.fsync(parent)
            return descriptor
    opened = _open_file(parent, name, label, _LOCK_FLAGS)
    if opened is None:
        raise _integrity(f"the local store is missing its lock file: {label}")
    try:
        _require_private(os.fstat(opened), label, directory=False)
    except BaseException:
        os.close(opened)
        raise
    return opened


def _read_bounded(descriptor: int, limit: int) -> bytes:
    """At most ``limit + 1`` bytes, so a file longer than ``limit`` shows as such."""
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_temporary(directory: int, data: bytes) -> tuple[str, tuple[int, int]]:
    """Write ``data`` to a new fsynced 0600 temporary; return its name and identity.

    The caller holds ``.tmp.lock`` shared. On failure the temporary is removed.
    """
    name = f"{os.getpid()}-{secrets.token_hex(16)}.tmp"
    descriptor = os.open(name, _NEW_FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(data)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            identity = file_identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
    except BaseException:
        _discard(directory, name)
        raise
    return name, identity


def _discard(directory: int, name: str) -> None:
    with suppress(FileNotFoundError):
        os.unlink(name, dir_fd=directory)


def _version_text(version: int | None) -> str:
    return "absent" if version is None else f"at version {version}"


class _StoreRoot:
    """One store directory: its checked path, identity, lock timeout and descriptor helpers."""

    def __init__(self, root: Path, *, create: bool, lock_timeout: float) -> None:
        if not 0 < lock_timeout <= MAX_LOCK_TIMEOUT_SECONDS:
            raise ValueError(
                f"a lock timeout is more than 0 and at most {MAX_LOCK_TIMEOUT_SECONDS:g} "
                f"seconds, not {lock_timeout!r}"
            )
        self.path = _checked_root(root)
        self.lock_timeout = lock_timeout
        with _os_errors(f"opening the local store {self.path}"):
            self.identity = self._prepare(create=create)

    def _prepare(self, *, create: bool) -> tuple[int, int]:
        descriptor = _open_directory(None, self.path, self.path)
        made = False
        if descriptor is None and create:
            os.mkdir(self.path, 0o700)
            made = True
            descriptor = _open_directory(None, self.path, self.path)
        if descriptor is None:
            raise storage_error(
                "STORAGE.UNAVAILABLE",
                f"there is no local store at {self.path} (create=True makes one)",
            )
        with ExitStack() as stack:
            stack.callback(os.close, descriptor)
            if made:
                os.fchmod(descriptor, 0o700)
                fsync_directory(self.path.parent)
            unexpected = sorted(set(os.listdir(descriptor)) - STORE_MEMBERS)
            if unexpected:
                raise _integrity(
                    f"the local store root holds entries a store never writes "
                    f"({', '.join(unexpected)}): {self.path}"
                )
            for name in (BLOBS_DIRECTORY, REFS_DIRECTORY, TEMPORARY_DIRECTORY):
                if create:
                    _make_directory(descriptor, name, self.path / name)
                os.close(_require_directory(descriptor, name, self.path / name))
            for name in (REFS_LOCK, TEMPORARY_LOCK):
                os.close(_open_lock(descriptor, name, self.path / name, create=create))
            return file_identity(os.fstat(descriptor))

    def open(self, stack: ExitStack) -> int:
        """The root's descriptor (closed by ``stack``), after checking it is still the root."""
        descriptor = _open_directory(None, self.path, self.path)
        if descriptor is None:
            raise storage_error(
                "STORAGE.UNAVAILABLE", f"the local store root is missing: {self.path}"
            )
        stack.callback(os.close, descriptor)
        if file_identity(os.fstat(descriptor)) != self.identity:
            raise _integrity(f"the local store root was replaced: {self.path}")
        return descriptor

    def member(self, stack: ExitStack, root: int, name: str) -> int:
        """One of the root's directories (closed by ``stack``), which must exist."""
        descriptor = _require_directory(root, name, self.path / name)
        stack.callback(os.close, descriptor)
        return descriptor

    def find_below(
        self, stack: ExitStack, member: int, label: Path, names: tuple[str, ...]
    ) -> int | None:
        """The directory ``names`` below an open member ``label``; ``None`` when missing."""
        descriptor = member
        for name in names:
            label = label / name
            child = _open_directory(descriptor, name, label)
            if child is None:
                return None
            stack.callback(os.close, child)
            descriptor = child
        return descriptor

    def make_below(self, stack: ExitStack, member: int, label: Path, names: tuple[str, ...]) -> int:
        """The directory ``names`` below an open member ``label``, creating what is missing.

        Every directory on the way is fsynced into its parent, found or made.
        """
        descriptor = member
        for name in names:
            label = label / name
            _make_directory(descriptor, name, label)
            descriptor = _require_directory(descriptor, name, label)
            stack.callback(os.close, descriptor)
        return descriptor

    @contextmanager
    def locked(self, root: int, name: str, operation: int) -> Iterator[None]:
        """Hold ``flock(operation)`` on a lock file, polling for at most ``lock_timeout``."""
        descriptor = _open_lock(root, name, self.path / name, create=False)
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise storage_error(
                            "STORAGE.UNAVAILABLE",
                            f"the local store lock stayed busy for {self.lock_timeout:g} s: "
                            f"{self.path / name}",
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
            yield
        finally:
            os.close(descriptor)  # closing the only descriptor releases the lock

    def sweep(self) -> LeftoverSweep:
        with _os_errors(f"sweeping the local store {self.path}"), ExitStack() as stack:
            root = self.open(stack)
            lock = _open_lock(root, TEMPORARY_LOCK, self.path / TEMPORARY_LOCK, create=False)
            stack.callback(os.close, lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return LeftoverSweep(removed=(), writers_active=True)
            directory = self.member(stack, root, TEMPORARY_DIRECTORY)
            removed: list[str] = []
            for name in sorted(os.listdir(directory)):
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if _TEMPORARY_NAME.fullmatch(name) is None or not stat.S_ISREG(metadata.st_mode):
                    raise _integrity(
                        "the local store's temporary directory holds an entry no writer "
                        f"creates: {self.path / TEMPORARY_DIRECTORY / name}"
                    )
                os.unlink(name, dir_fd=directory)
                removed.append(name)
            if removed:
                os.fsync(directory)
            return LeftoverSweep(removed=tuple(removed), writers_active=False)


class LocalBlobStore:
    """``BlobStore`` on a private local directory: ``<root>/blobs/<xx>/<sha256>``.

    Blobs are write-once: a stored blob is never replaced, repaired or removed (a corrupt one
    is refused by ``get`` and by a ``put`` of the same bytes). See the module docstring for the
    layout, the checks, concurrency and crash leftovers.
    """

    def __init__(
        self,
        root: Path,
        *,
        create: bool = False,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        """Open (or with ``create`` make) the store at ``root`` and sweep crash leftovers.

        Params: ``root``, an absolute path with no symlink in it; ``create``, whether a
        missing root and missing layout entries are made (the root's parent must exist);
        ``lock_timeout``, how long a writer waits for ``.tmp.lock`` (0 < seconds <= 3600).
        Raises: ``ValueError`` for a bad ``lock_timeout``; ``StorageError`` with
        ``STORAGE.UNAVAILABLE`` when the root is missing and ``create`` is not set (or on any
        other filesystem failure); ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) for a
        relative, unnormalized or symlinked root, an unsafe root or layout entry, an entry a
        store never writes, or (without ``create``) an incomplete layout.
        """
        self._root = _StoreRoot(root, create=create, lock_timeout=lock_timeout)
        self.sweep_leftovers()

    @property
    def root(self) -> Path:
        """The store directory (the namespace)."""
        return self._root.path

    def sweep_leftovers(self) -> LeftoverSweep:
        """Remove temporaries left by dead writers, only when no writer can be active.

        Returns: what was removed, or ``writers_active=True`` when a writer held
        ``.tmp.lock`` and nothing was examined.
        Raises: ``StorageIntegrityError`` when the root was replaced or is unsafe, or
        ``tmp/`` holds an entry that is not a regular file with a temporary's name;
        ``StorageError`` (``STORAGE.UNAVAILABLE``) on a filesystem failure.
        """
        return self._root.sweep()

    def put(self, data: bytes) -> BlobRef:
        """Store ``data`` once; see ``BlobStore.put``.

        Raises: ``StorageError`` with ``STORAGE.TOO_LARGE`` (before any I/O),
        ``STORAGE.QUOTA`` (no space), ``STORAGE.PROTOCOL`` (the filesystem cannot rename
        without replacing, which does not pass by itself) or ``STORAGE.UNAVAILABLE`` (any
        other filesystem failure, or a busy ``.tmp.lock``); ``StorageIntegrityError``
        (``STORAGE.INTEGRITY``) when the stored blob with this digest is corrupt or unsafe,
        or the store's layout is.
        """
        if len(data) > MAX_BLOB_BYTES:
            raise storage_error(
                "STORAGE.TOO_LARGE", f"a blob is at most {MAX_BLOB_BYTES} bytes, not {len(data)}"
            )
        ref = BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        name, label = self._location(ref)
        with _os_errors(f"storing blob {ref.sha256} in {self.root}"), ExitStack() as stack:
            root = self._root.open(stack)
            blobs = self._root.member(stack, root, BLOBS_DIRECTORY)
            shard = self._root.make_below(stack, blobs, label.parent.parent, (name[:2],))
            if self._read(shard, name, label, ref, missing_ok=True, sync=True) is not None:
                os.fsync(shard)
                return ref
            stack.enter_context(self._root.locked(root, TEMPORARY_LOCK, fcntl.LOCK_SH))
            temporaries = self._root.member(stack, root, TEMPORARY_DIRECTORY)
            temporary, identity = _write_temporary(temporaries, data)
            try:
                rename_no_replace_at(temporaries, temporary, shard, name)
            except FileExistsError:
                _discard(temporaries, temporary)
                # Another writer published these bytes first; its blob must hold up, durably.
                self._read(shard, name, label, ref, missing_ok=False, sync=True)
                os.fsync(shard)
                return ref
            except NoReplaceRenameUnavailableError as error:
                _discard(temporaries, temporary)
                raise storage_error(
                    "STORAGE.PROTOCOL",
                    f"the filesystem under the local store {self.root} cannot publish a blob "
                    f"without replacing, so no blob can be stored there: {error}",
                ) from error
            except BaseException:
                _discard(temporaries, temporary)
                raise
            os.fsync(shard)
            os.fsync(temporaries)
            try:
                published = os.stat(name, dir_fd=shard, follow_symlinks=False)
            except FileNotFoundError:
                published = None
            if published is None or file_identity(published) != identity:
                raise _integrity(f"the published blob is not the file this writer wrote: {label}")
        return ref

    def has(self, ref: BlobRef) -> bool:
        """Whether a blob of the ref's size is stored, from metadata only; see ``BlobStore.has``.

        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the stored entry is a
        symlink, a directory or special file, has more than one link or is not private, or
        the store's layout is unsafe; ``StorageError`` with ``STORAGE.PROTOCOL`` (a ``ref``
        that is not a valid ``BlobRef``, before any I/O) or ``STORAGE.UNAVAILABLE`` (a
        filesystem failure).
        """
        name, label = self._location(ref)
        with _os_errors(f"checking blob {ref.sha256} in {self.root}"), ExitStack() as stack:
            shard = self._find_shard(stack, name, label)
            if shard is None:
                return False
            try:
                metadata = os.stat(name, dir_fd=shard, follow_symlinks=False)
            except FileNotFoundError:
                return False
            _require_private(metadata, label, directory=False)
            return metadata.st_size == ref.size

    def get(self, ref: BlobRef) -> bytes:
        """Read one blob after checking it; see ``BlobStore.get``.

        Raises: ``StorageError`` with ``STORAGE.PROTOCOL`` (a ``ref`` that is not a valid
        ``BlobRef``, before any I/O), ``STORAGE.NOT_FOUND`` or ``STORAGE.UNAVAILABLE``;
        ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the stored file is unsafe or
        its size or digest differs from the ref's, or the store's layout is unsafe.
        """
        name, label = self._location(ref)
        with _os_errors(f"reading blob {ref.sha256} in {self.root}"), ExitStack() as stack:
            shard = self._find_shard(stack, name, label)
            data = (
                None
                if shard is None
                else self._read(shard, name, label, ref, missing_ok=True, sync=False)
            )
            if data is None:
                raise storage_error(
                    "STORAGE.NOT_FOUND", f"no blob {ref.sha256} is stored in {self.root}"
                )
            return data

    def _location(self, ref: BlobRef) -> tuple[str, Path]:
        """The blob's file name and path; the ref is validated again before it names a file.

        Raises: ``StorageError`` (``STORAGE.PROTOCOL``) for a ref that is not a valid
        ``BlobRef``.
        """
        # A valid BlobRef's digest is exactly 64 lowercase hex characters: a safe file name.
        name = _checked_ref(ref).sha256
        return name, self._root.path / BLOBS_DIRECTORY / name[:2] / name

    def _find_shard(self, stack: ExitStack, name: str, label: Path) -> int | None:
        root = self._root.open(stack)
        blobs = self._root.member(stack, root, BLOBS_DIRECTORY)
        return self._root.find_below(stack, blobs, label.parent.parent, (name[:2],))

    def _read(
        self, shard: int, name: str, label: Path, ref: BlobRef, *, missing_ok: bool, sync: bool
    ) -> bytes | None:
        """The verified bytes of a stored blob; ``None`` when missing and ``missing_ok``.

        With ``sync``, the verified file is also fsynced (it may have been published by a
        writer that died before syncing it).
        """
        descriptor = _open_file(shard, name, label, _READ_FLAGS)
        if descriptor is None:
            if missing_ok:
                return None
            raise _integrity(f"a blob vanished after it was published: {label}")
        try:
            _require_private(os.fstat(descriptor), label, directory=False)
            os.set_blocking(descriptor, True)
            data = _read_bounded(descriptor, ref.size)
            if len(data) != ref.size:
                raise _integrity(f"the stored blob does not hold exactly {ref.size} bytes: {label}")
            if hashlib.sha256(data).hexdigest() != ref.sha256:
                raise _integrity(f"the stored blob does not match its digest: {label}")
            if sync:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return data


class LocalRefStore:
    """``RefStore`` on a private local directory: ``<root>/refs/<name as .d dirs>.json``.

    Reads take no lock. A swap holds ``.refs.lock`` and fsyncs before it returns. See the
    module docstring for the layout, the checks, concurrency and crash leftovers.
    """

    def __init__(
        self,
        root: Path,
        *,
        create: bool = False,
        clock: Callable[[], datetime] = _utc_now,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        """Open (or with ``create`` make) the store at ``root`` and sweep crash leftovers.

        Params: as ``LocalBlobStore``, plus ``clock``, the timezone-aware time a swap
        records as ``updated_at``; ``lock_timeout`` also bounds the wait for ``.refs.lock``.
        Raises: as ``LocalBlobStore``.
        """
        self._root = _StoreRoot(root, create=create, lock_timeout=lock_timeout)
        self._clock = clock
        self.sweep_leftovers()

    @property
    def root(self) -> Path:
        """The store directory (the namespace)."""
        return self._root.path

    def sweep_leftovers(self) -> LeftoverSweep:
        """Remove temporaries left by dead writers; see ``LocalBlobStore.sweep_leftovers``."""
        return self._root.sweep()

    def read(self, name: str) -> RefValue | None:
        """The current value of a ref, without a lock; see ``RefStore.read``.

        Raises: ``StorageNameError`` for an invalid name (before any I/O);
        ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the ref file or a directory
        above it is unsafe, the file is over ``MAX_REF_FILE_BYTES``, not a canonical
        ``RefValue``, or names another ref; ``StorageError`` (``STORAGE.UNAVAILABLE``) on a
        filesystem failure.
        """
        directories, file_name = self._location(name)
        with _os_errors(f"reading ref {name} in {self.root}"), ExitStack() as stack:
            root = self._root.open(stack)
            refs = self._root.member(stack, root, REFS_DIRECTORY)
            label = self._root.path / REFS_DIRECTORY
            directory = self._root.find_below(stack, refs, label, directories)
            if directory is None:
                return None
            return self._read(directory, name, file_name, label.joinpath(*directories))

    def swap(self, name: str, *, expected_version: int | None, target: BlobRef) -> RefValue:
        """Point a ref at ``target`` by compare-and-set; see ``RefStore.swap``.

        Raises: ``StorageNameError`` for an invalid name, and ``StorageError``
        (``STORAGE.PROTOCOL``) for a ``target`` that is not a valid ``BlobRef`` (both before
        any I/O); ``StorageError`` with ``STORAGE.CONFLICT`` (the ref is not at
        ``expected_version``), ``STORAGE.QUOTA``
        (the version counter is exhausted, or no space) or ``STORAGE.UNAVAILABLE`` (a lock
        stayed busy for ``lock_timeout``, or a filesystem failure);
        ``StorageIntegrityError`` as ``read``; ``ValueError`` when the clock returns a naive
        time.
        """
        directories, file_name = self._location(name)
        target = _checked_ref(target)
        with _os_errors(f"swapping ref {name} in {self.root}"), ExitStack() as stack:
            root = self._root.open(stack)
            stack.enter_context(self._root.locked(root, REFS_LOCK, fcntl.LOCK_EX))
            refs = self._root.member(stack, root, REFS_DIRECTORY)
            label = self._root.path / REFS_DIRECTORY
            directory = self._root.find_below(stack, refs, label, directories)
            path = label.joinpath(*directories)
            current = None if directory is None else self._read(directory, name, file_name, path)
            current_version = None if current is None else current.version
            if expected_version != current_version:
                raise storage_error(
                    "STORAGE.CONFLICT",
                    f"ref {name} is {_version_text(current_version)}; the swap expected it "
                    f"{_version_text(expected_version)}",
                )
            if current is not None and current.version >= MAX_STORAGE_COUNTER:
                raise storage_error("STORAGE.QUOTA", f"ref {name} has used every version")
            value = RefValue(
                name=name,
                version=1 if current is None else current.version + 1,
                target=target,
                updated_at=self._clock(),
            )
            # Found or made, each directory above the ref file is synced into its parent.
            directory = self._root.make_below(stack, refs, label, directories)
            stack.enter_context(self._root.locked(root, TEMPORARY_LOCK, fcntl.LOCK_SH))
            temporaries = self._root.member(stack, root, TEMPORARY_DIRECTORY)
            temporary, _ = _write_temporary(temporaries, canonical_json_bytes(value) + b"\n")
            try:
                os.replace(temporary, file_name, src_dir_fd=temporaries, dst_dir_fd=directory)
            except BaseException:
                _discard(temporaries, temporary)
                raise
            os.fsync(directory)
            os.fsync(temporaries)
        return value

    def _location(self, name: str) -> tuple[tuple[str, ...], str]:
        """The directory names and file name of a ref, below ``refs/``.

        Raises: ``StorageNameError`` for an invalid name, or one whose path would not stay a
        file strictly below ``<root>/refs/`` once normalized.
        """
        segments = validate_ref_name(name).split("/")
        directories = tuple(f"{segment}{REF_DIRECTORY_SUFFIX}" for segment in segments[:-1])
        file_name = f"{segments[-1]}{REF_FILE_SUFFIX}"
        refs = self._root.path / REFS_DIRECTORY
        joined = refs.joinpath(*directories, file_name)
        resolved = Path(os.path.normpath(joined))
        if (
            resolved != joined
            or resolved == refs
            or not resolved.is_relative_to(refs)
            or resolved.relative_to(refs).parts != (*directories, file_name)
        ):
            raise StorageNameError(f"a ref name must stay below the store's refs/: {name!r}")
        return directories, file_name

    def _read(self, directory: int, name: str, file_name: str, parent: Path) -> RefValue | None:
        label = parent / file_name
        descriptor = _open_file(directory, file_name, label, _READ_FLAGS)
        if descriptor is None:
            return None
        try:
            # A swap that replaced this file after it was opened leaves it with no link; its
            # complete value is still the one that was current at the open.
            _require_private(os.fstat(descriptor), label, directory=False, unlinked_ok=True)
            os.set_blocking(descriptor, True)
            payload = _read_bounded(descriptor, MAX_REF_FILE_BYTES)
        finally:
            os.close(descriptor)
        if len(payload) > MAX_REF_FILE_BYTES:
            raise _integrity(
                f"a stored ref value is larger than {MAX_REF_FILE_BYTES} bytes: {label}"
            )
        try:
            value = RefValue.model_validate_json(payload)
        except ValueError as error:
            raise _integrity(f"the stored ref value is not valid: {label}") from error
        if value.name != name:
            raise _integrity(f"the stored ref value names ref {value.name!r}: {label}")
        if canonical_json_bytes(value) + b"\n" != payload:
            raise _integrity(f"the stored ref value is not in canonical form: {label}")
        return value

"""Replication passes: every owner's files, read without blocking a writer, checked and streamed.

@canonical library_replication -- collect_pass walks the whole asset root, refuses any path
library_classification does not cover, then runs one ReplicationSource per owner
(library_sources lists them, referrers before referents); each source lists its owner's files
without taking any lock a lab writer takes, and every file is read through a ReplicationReader
that streams bounded chunks, hashes them, and proves the file kept its identity and matches the
digest its seal or record names. An owner whose files moved during the pass is reported
``changed`` and one whose files are unsafe or contradict their records ``refused``; either way
nothing it read counts as fresh, and the next generation keeps that owner's previous entries.
A file a source removed from the Mac on purpose (an archived fine-tune run's checkpoints) is
offered as a ``CarriedPath`` instead: there are no bytes to read, and the generation builder keeps
the entry of the generation that holds it, so the vault's newest manifest still names it.
Each source names its referents (the owners whose files its files name); a referent is always
read after its referrers, so everything a referrer names already exists when its referent is
read, and an owner whose referent did not complete is reported ``changed`` too (it falls back
with that referent), so a generation never pairs fresh records with the older files they name.
Related: library_classification.py (what is copied, and how), library_generations.py (turns a
pass into a stored generation, verifies and restores it), storage_contracts.py (FileClass,
PlannedManifestEntry), recording_takes.py (the lock-free projection readers),
tests/test_replication_sources.py.

Step 5 decisions recorded here:
- ``SourceChangedError`` is not a ``StorageError``: nothing about the vault failed, and a
  background copier must never count a moving source as an unreachable vault. A pass turns it
  into the owner's result and never raises it (except from the classification walk itself,
  when the tree changes in a way the walk cannot follow; it is then retryable).
- The sources live in this module rather than in their owner modules: the owner modules are
  large, and ``storage_binding`` (which later imports this module) is imported by two of them,
  so owner-side sources would create import cycles. Owner modules are imported lazily, inside
  the sources, so importing this module loads no recording, catalog or adapter module.
- No source calls ``RecordingTakeStore.state_revision``, ``get_take`` or anything else that
  takes ``.metadata.lock``, ``.corpus.lock``, ``.index.lock`` or any writer's lock, and none
  opens a lock file; the prompt database is read only through SQLite (``mode=ro``) and never
  with ``os.open``, so a pass running inside the recording service cannot drop that process's
  database locks.

Nothing here constructs a storage adapter; library_generations turns a pass into a manifest.
"""

from __future__ import annotations

import errno
import fcntl
import functools
import hashlib
import os
import secrets
import shutil
import sqlite3
import stat
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal, NamedTuple, Protocol, cast, get_args

from pydantic import BaseModel, ValidationError

from .canonical import canonical_json_bytes, canonical_sha256
from .errors import ContractError, SecurityError, VoiceLabError
from .library_classification import (
    CORPUS_DATABASE_PATH,
    FILE_CLASSES,
    ClassificationRule,
    RestoreMode,
    classify,
    matching_rules,
    rule_named,
    subtree_stays_on_mac,
)
from .models import CaptureManifest
from .paths import AssetLayout, validate_absolute_path
from .storage_contracts import MAX_BLOB_BYTES, MEMOS_PREFIX, FileClass, PlannedManifestEntry
from .storage_ports import storage_error

if TYPE_CHECKING:
    from .finetune import FineTuneRun
    from .recording_library import DesktopSessionEvidence
    from .recording_takes import RecordingTakeStore

__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "MAX_CHUNK_BYTES",
    "MAX_RECORD_BYTES",
    "PRETRAINED_PIN_SOURCE",
    "CarriedPath",
    "CorpusDatabaseSource",
    "FileIdentity",
    "FineTuneRunSource",
    "MemosSource",
    "OwnerResult",
    "PassEntry",
    "PassResult",
    "RecordTreeSource",
    "ReplicationItem",
    "ReplicationReader",
    "ReplicationSink",
    "ReplicationSource",
    "ScanRoot",
    "ScratchDirectory",
    "SourceChangedError",
    "SourceEntry",
    "SourceProblem",
    "TakeStoreSource",
    "WithheldPath",
    "backup_database",
    "collect_pass",
    "library_sources",
    "memos_source_path",
    "require_classified_tree",
    "source_classes",
]

DEFAULT_CHUNK_BYTES: Final = 1024 * 1024
MAX_CHUNK_BYTES: Final = MAX_BLOB_BYTES
# A record a source parses (a run, a sidecar, a manifest) is read whole, up to this size.
MAX_RECORD_BYTES: Final = 64 * 1024 * 1024
# The manifest field a fine-tune run's pretrained-weights pin entry names.
PRETRAINED_PIN_SOURCE: Final = "finetune-run.runtime.pretrained_sha256"
BACKUP_TIMEOUT_SECONDS: Final = 10.0
SCRATCH_PREFIX: Final = ".vault-"
# Scratch names; the lock name matches the Mac-only lock rule wherever it is.
DATABASE_BACKUP_NAME: Final = "database-backup.sqlite3"
SCRATCH_LOCK_NAME: Final = ".scratch.lock"
TAKE_PAGE_SIZE: Final = 200

_TAKES: Final = "provenance/recording-takes"
_TAKE_PENDING: Final = f"{_TAKES}/.pending.json"
_TAKE_STORE_HEAD: Final = f"{_TAKES}/.store-head.json"
_TAKE_AUDIO: Final = "tmp/recording-takes"
_NATIVE_EDITS: Final = "provenance/recording-edits/native"
_SIDECARS: Final = "provenance/capture-desktop"
_FINETUNE_RUNS: Final = "derived/finetune"
_FINETUNE_QUEUE: Final = "provenance/finetune-jobs/head.json"
_SNAPSHOTS: Final = "provenance/corpus/snapshots"
_READY: Final = "READY"
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_CHILD_DIRECTORY_FLAGS: Final = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

type SourceProblem = Literal["changed", "missing", "linked", "unsafe", "mismatch", "invalid"]
type OwnerState = Literal["complete", "changed", "refused"]
type EntryKind = Literal["file", "pin"]
_PROBLEMS: Final = frozenset(get_args(SourceProblem.__value__))
_REFUSALS: Final = frozenset({"linked", "unsafe", "mismatch", "invalid"})


class SourceChangedError(VoiceLabError):
    """A file an owner source reads moved, vanished, is unsafe or contradicts its record.

    ``path`` is the manifest path concerned (for the prompt database, its own path, never the
    scratch copy). ``problem`` says what happened: ``changed`` (the file, or the owner's guard,
    moved during the pass) and ``missing`` (it vanished) are retryable, with ``code``
    ``REPLICATION.SOURCE_CHANGED``; ``linked`` (a file with more than one hard link),
    ``unsafe`` (a symbolic link, not a regular file, not owned by this user, readable by others
    where the owner keeps it private, or unreadable), ``mismatch`` (its bytes or size differ
    from what a seal or record names) and ``invalid`` (a record that names other files is
    invalid or disagrees with them) are refusals, ``code`` ``REPLICATION.SOURCE_REFUSED``,
    which only a change to the library can clear. A pass reports either as the owner's result.
    ``str(error)`` is ``"<path>: <detail>"``; instances pickle with every field intact.
    Raises (on construction): ``ValueError`` for an unknown problem.
    """

    def __init__(self, path: str, problem: SourceProblem, detail: str) -> None:
        if problem not in _PROBLEMS:
            raise ValueError(f"unknown replication source problem: {problem!r}")
        self.path = path
        self.problem: SourceProblem = problem
        self.detail = detail
        super().__init__(f"{path}: {detail}")

    @property
    def refused(self) -> bool:
        """Whether this is a refusal (the owner's files are unsafe or inconsistent)."""
        return self.problem in _REFUSALS

    @property
    def retryable(self) -> bool:
        return not self.refused

    @property
    def code(self) -> str:
        return "REPLICATION.SOURCE_REFUSED" if self.refused else "REPLICATION.SOURCE_CHANGED"

    def __reduce__(self) -> tuple[type[SourceChangedError], tuple[str, SourceProblem, str]]:
        return (type(self), (self.path, self.problem, self.detail))


class FileIdentity(NamedTuple):
    """What must stay equal while a file is read: device, inode, size and both times."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def of(cls, metadata: os.stat_result) -> FileIdentity:
        return cls(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )


def _require_chunk_bytes(chunk_bytes: int) -> None:
    if not 1 <= chunk_bytes <= MAX_CHUNK_BYTES:
        raise ValueError(f"a chunk is 1 to {MAX_CHUNK_BYTES} bytes, not {chunk_bytes}")


def _open_failure(path: str, error: OSError) -> SourceChangedError:
    if error.errno == errno.ENOENT:
        return SourceChangedError(path, "missing", "was removed")
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        return SourceChangedError(
            path, "unsafe", "is, or is reached through, a symbolic link or a non-directory"
        )
    return SourceChangedError(path, "unsafe", f"cannot be opened: {error.strerror}")


def _open_parent(root: Path, relative: str, label: str) -> tuple[int, str]:
    """Open the directory holding ``relative``, one component at a time, never following links.

    Returns: the directory's descriptor (the caller closes it) and the final name.
    Raises: ``SourceChangedError`` about ``label`` when a component is missing, a symbolic
    link or not a directory, or cannot be opened.
    """
    parts = PurePosixPath(relative).parts
    descriptor: int | None = None
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
        for part in parts[:-1]:
            child = os.open(part, _CHILD_DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise _open_failure(label, error) from error
    return descriptor, parts[-1]


@dataclass(frozen=True, slots=True)
class ReplicationItem:
    """One file a source offers to a pass.

    ``path`` is the manifest path (relative to the asset root); ``source`` is where the bytes
    are read, relative to ``root`` (it differs from ``path`` only for the prompt database,
    whose bytes come from a private backup). ``expected_sha256`` and ``expected_size`` are
    what the file's seal or record names (``None`` when the file is itself the record);
    ``private`` requires no group or other permission bits, and ``owned`` requires the file to
    be this user's (the phone's memos drop is written by another account and only readable
    through a group, so its items set both to ``False``). A ``pin`` item is not read: its
    digest is the pin a record names (``pin_source``) and its size the pinned file's.
    Raises (on construction): ``ValueError`` when a pin lacks its digest, size or source, or a
    file names a pin source.
    """

    path: str
    file_class: FileClass
    kind: EntryKind
    mode: RestoreMode
    expected_sha256: str | None
    expected_size: int | None
    root: Path
    source: str
    private: bool = True
    pin_source: str | None = None
    owned: bool = True

    def __post_init__(self) -> None:
        pinned = (self.expected_sha256, self.expected_size, self.pin_source)
        if (self.kind == "pin") != (None not in pinned) or (
            self.kind == "file" and self.pin_source is not None
        ):
            raise ValueError("a pin item names its digest, size and pin source; a file no pin")

    def open(
        self, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES
    ) -> AbstractContextManager[ReplicationReader]:
        """Open the file for one bounded, checked read.

        The file is opened below the root one component at a time with ``O_NOFOLLOW``; its
        identity is taken with ``fstat`` and must be a regular file owned by this user, with
        one link, private where required, and of the expected size. Leaving the ``with``
        block normally re-checks the identity (by descriptor and by name) and the digest.
        Params: ``chunk_bytes``, the largest piece ``chunks`` yields (1 to
        ``MAX_CHUNK_BYTES``).
        Returns: a context manager yielding the ``ReplicationReader``.
        Raises: ``ValueError`` for a pin item or a bad chunk size (before any I/O);
        ``SourceChangedError`` when the file cannot be opened safely, has the wrong type,
        owner, mode, link count or size, or (on leaving the block) changed or does not match
        ``expected_sha256``.
        """
        if self.kind != "file":
            raise ValueError("a pin item is recorded by its pin; it has no bytes to read")
        _require_chunk_bytes(chunk_bytes)
        return _open_reader(self, chunk_bytes)


@contextmanager
def _open_reader(item: ReplicationItem, chunk_bytes: int) -> Iterator[ReplicationReader]:
    parent, name = _open_parent(item.root, item.source, item.path)
    try:
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
        except OSError as error:
            raise _open_failure(item.path, error) from error
        try:
            reader = ReplicationReader(item, descriptor, parent, name, chunk_bytes)
            yield reader
            reader.verify()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


class ReplicationReader:
    """A bounded, hashing read of one ``ReplicationItem`` that proves the file held still.

    Built only by ``ReplicationItem.open``. ``identity`` is the file's identity when it was
    opened; ``chunks`` streams the bytes once, never more than the chunk size at a time;
    ``verify`` (run when the ``with`` block ends) re-checks the identity through the
    descriptor and through the name, and the digest against the item's record. ``sha256``
    and ``size`` are available once verified.
    """

    def __init__(
        self,
        item: ReplicationItem,
        descriptor: int,
        parent: int,
        name: str,
        chunk_bytes: int,
    ) -> None:
        metadata = os.fstat(descriptor)
        problem: tuple[SourceProblem, str] | None = None
        if not stat.S_ISREG(metadata.st_mode):
            problem = ("unsafe", "is not a regular file")
        elif item.owned and metadata.st_uid != os.getuid():
            problem = ("unsafe", "is not owned by this user")
        elif item.private and stat.S_IMODE(metadata.st_mode) & 0o077:
            problem = ("unsafe", "is readable or writable by other users")
        elif metadata.st_nlink != 1:
            problem = ("linked", f"has {metadata.st_nlink} hard links")
        elif item.expected_size is not None and metadata.st_size != item.expected_size:
            problem = (
                "mismatch",
                f"is {metadata.st_size} bytes, but its record names {item.expected_size}",
            )
        if problem is not None:
            raise SourceChangedError(item.path, *problem)
        os.set_blocking(descriptor, True)
        self._item = item
        self._descriptor = descriptor
        self._parent = parent
        self._name = name
        self._chunk_bytes = chunk_bytes
        self.identity = FileIdentity.of(metadata)
        self._digest = hashlib.sha256()
        self._size = 0
        self._started = False
        self._finished = False
        self._sha256: str | None = None

    def chunks(self) -> Iterator[bytes]:
        """The file's bytes, in pieces of at most the chunk size, read once.

        Raises: ``ValueError`` when called twice; ``SourceChangedError`` when the file grows
        past its opened size or cannot be read.
        """
        if self._started:
            raise ValueError("a replication reader streams its file once")
        self._started = True
        while True:
            try:
                data = os.read(self._descriptor, self._chunk_bytes)
            except OSError as error:
                raise SourceChangedError(
                    self._item.path, "unsafe", f"cannot be read: {error.strerror}"
                ) from error
            if not data:
                break
            self._size += len(data)
            if self._size > self.identity.size:
                raise SourceChangedError(self._item.path, "changed", "grew while it was being read")
            self._digest.update(data)
            yield data
        self._finished = True

    def verify(self) -> None:
        """Prove the file held still and matches its record; run once every chunk was read.

        Raises: ``ValueError`` before the last chunk was read; ``SourceChangedError``
        (``missing``) when the name is gone, (``changed``) when the identity through the
        descriptor or the name, or the byte count, differs from the opened file, and
        (``mismatch``) when the digest differs from the item's ``expected_sha256``.
        """
        if not self._finished:
            raise ValueError("a replication reader is verified only after its last chunk")
        after = FileIdentity.of(os.fstat(self._descriptor))
        try:
            current = FileIdentity.of(
                os.stat(self._name, dir_fd=self._parent, follow_symlinks=False)
            )
        except FileNotFoundError as error:
            raise SourceChangedError(
                self._item.path, "missing", "was removed while it was being read"
            ) from error
        if after != self.identity or current != self.identity or self._size != self.identity.size:
            raise SourceChangedError(self._item.path, "changed", "changed while it was being read")
        digest = self._digest.hexdigest()
        expected = self._item.expected_sha256
        if expected is not None and digest != expected:
            raise SourceChangedError(
                self._item.path,
                "mismatch",
                f"has SHA-256 {digest}, but its record names {expected}",
            )
        self._sha256 = digest

    @property
    def sha256(self) -> str:
        """The verified digest. Raises: ``ValueError`` before ``verify`` succeeded."""
        if self._sha256 is None:
            raise ValueError("a replication reader's digest is known only once verified")
        return self._sha256

    @property
    def size(self) -> int:
        """The verified byte count. Raises: ``ValueError`` before ``verify`` succeeded."""
        if self._sha256 is None:
            raise ValueError("a replication reader's size is known only once verified")
        return self._size


@dataclass(frozen=True, slots=True)
class WithheldPath:
    """A copied-class file a source left out of this pass, and why (it is not an error)."""

    path: str
    file_class: FileClass
    reason: str


@dataclass(frozen=True, slots=True)
class CarriedPath:
    """A file that is gone from the Mac on purpose and whose stored entry must be kept.

    A fine-tune run whose checkpoints were archived and then removed (``local-removal.json``)
    has no bytes left to read, but the vault still holds them: the generation the removal
    record names has the entry, with its chunks. A pass offers this instead of a
    ``ReplicationItem``, and the generation builder copies that generation's entry into the
    new one, so the vault's newest manifest keeps naming the file and a restore of the newest
    generation still brings the run back. ``sha256``, ``size`` and ``kind`` are what the
    removal record names: the entry that is carried must be exactly that content.
    Raises (on construction): ``ValueError`` when a pin carries no pin source, or a file one.
    """

    path: str
    file_class: FileClass
    kind: EntryKind
    mode: RestoreMode
    sha256: str
    size: int
    generation: int
    reason: str
    pin_source: str | None = None

    def __post_init__(self) -> None:
        if (self.kind == "pin") != (self.pin_source is not None):
            raise ValueError("a carried pin names its pin source; a carried file none")


type SourceEntry = ReplicationItem | WithheldPath | CarriedPath


@dataclass(frozen=True, slots=True)
class PassEntry:
    """One file a pass read completely and verified, as its owner offered it."""

    owner: str
    path: str
    file_class: FileClass
    kind: EntryKind
    sha256: str
    size: int
    mode: RestoreMode
    pin_source: str | None

    def planned(self) -> PlannedManifestEntry:
        """The manifest plan entry for this file.

        Raises: ``pydantic.ValidationError`` when the path cannot be restored (for example
        one under ``provenance/vault``).
        """
        return PlannedManifestEntry(
            path=self.path,
            file_class=self.file_class,
            kind=self.kind,
            sha256=self.sha256,
            size=self.size,
            mode=self.mode,
            pin_source=self.pin_source,
        )


@dataclass(frozen=True, slots=True)
class OwnerResult:
    """What one owner's source gave a pass.

    ``complete``: ``entries`` are this pass's view of the owner. ``changed`` (retryable) or
    ``refused`` (the owner's files are unsafe or inconsistent): ``entries`` is empty, nothing
    read counts as fresh, and ``reason``, ``problem`` and ``problem_path`` name the first
    failure; the next generation keeps the owner's previous entries. An owner whose own files
    were fine but one of whose referents did not complete is ``changed`` with ``held_back_by``
    naming that referent (its ``problem_path`` is the referent's; follow ``held_back_by`` to
    the owner that failed). ``withheld`` lists the copied-class files the source left out,
    with why; ``carried`` the files that are gone from the Mac on purpose and whose stored
    entry an earlier generation still holds (``CarriedPath``).
    """

    owner: str
    state: OwnerState
    entries: tuple[PassEntry, ...]
    withheld: tuple[WithheldPath, ...]
    reason: str | None = None
    problem: SourceProblem | None = None
    problem_path: str | None = None
    held_back_by: str | None = None
    carried: tuple[CarriedPath, ...] = ()


@dataclass(frozen=True, slots=True)
class PassResult:
    """Every owner's result, in source order (owners whose classes were not wanted are absent)."""

    owners: tuple[OwnerResult, ...]

    def owner(self, name: str) -> OwnerResult:
        """The result of the owner called ``name``. Raises: ``KeyError`` when it did not run."""
        for result in self.owners:
            if result.owner == name:
                return result
        raise KeyError(name)

    @property
    def complete(self) -> bool:
        return all(result.state == "complete" for result in self.owners)

    @property
    def entries(self) -> tuple[PassEntry, ...]:
        """The entries of every complete owner, in pass order."""
        return tuple(entry for result in self.owners for entry in result.entries)


class ReplicationSink(Protocol):
    """Where a pass hands each item's bytes.

    For every item read: ``begin`` once, ``chunk`` for each piece (never larger than the
    pass's chunk size), then ``finish`` with the verified entry, or with ``None`` when the
    item was not read completely and verified (its bytes must then be discarded). A pin item
    gets ``begin`` and ``finish`` only. An exception from the sink stops the pass. A verified
    entry counts only if the pass result keeps it: an owner can still be reported ``changed``
    after its items were finished, when one of its referents does not complete.
    """

    def begin(self, owner: str, item: ReplicationItem) -> None: ...

    def chunk(self, data: bytes) -> None: ...

    def finish(self, entry: PassEntry | None) -> None: ...


class ReplicationSource(Protocol):
    """One owner's files, listed without blocking any writer.

    ``name`` names the owner; ``rules`` are the copied classification rules whose files it
    offers (each copied rule has exactly one source); ``referents`` names the owners whose
    files its files name (a pass reads them after this owner and holds this owner back when
    one of them does not complete). ``items`` yields a ``ReplicationItem`` for every file it
    offers and a ``WithheldPath`` for every file it leaves out; it raises
    ``SourceChangedError`` when the owner moved (or is unsafe) as a whole. When reading an
    offered item fails, the pass throws that ``SourceChangedError`` into the generator at the
    ``yield`` of the item, so the source can re-check its guard and raise the error that
    describes the owner (a source that returns another item instead has the failed one
    recorded as withheld).
    """

    @property
    def name(self) -> str: ...

    @property
    def rules(self) -> frozenset[str]: ...

    @property
    def referents(self) -> frozenset[str]: ...

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]: ...


def source_classes(source: ReplicationSource) -> frozenset[FileClass]:
    """The classes a source's rules copy.

    Raises: ``ValueError`` for an unknown or a Mac-only rule.
    """
    classes: set[FileClass] = set()
    for name in source.rules:
        try:
            rule = rule_named(name)
        except KeyError as error:
            raise ValueError(f"source {source.name} serves unknown rule {name}") from error
        if not rule.copied:
            raise ValueError(f"source {source.name} cannot serve Mac-only rule {name}")
        classes.add(cast("FileClass", rule.disposition))
    return frozenset(classes)


# -- tree walking ------------------------------------------------------------------------


def _listing(descriptor: int) -> list[tuple[str, os.stat_result]]:
    entries: list[tuple[str, os.stat_result]] = []
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            try:
                entries.append((entry.name, entry.stat(follow_symlinks=False)))
            except FileNotFoundError:
                continue
    return sorted(entries, key=lambda item: item[0])


def _scan_directory(descriptor: int, prefix: str) -> Iterator[tuple[str, os.stat_result]]:
    """Every non-directory below an open directory (which this closes), sorted, pruned."""
    try:
        for name, metadata in _listing(descriptor):
            path = f"{prefix}/{name}" if prefix else name
            if not stat.S_ISDIR(metadata.st_mode):
                yield path, metadata
            elif not subtree_stays_on_mac(path):
                try:
                    child = os.open(name, _CHILD_DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise _open_failure(path, error) from error
                yield from _scan_directory(child, path)
    finally:
        os.close(descriptor)


def _open_child(parent: int, name: str, relative: str) -> int | None:
    try:
        return os.open(name, _CHILD_DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _open_failure(relative, error) from error


def _scan(root: Path, relative: str) -> Iterator[tuple[str, os.stat_result]]:
    """Every non-directory at or below ``relative``, sorted, never following a link.

    A missing path yields nothing; a path that is not a directory yields itself; Mac-only
    subtrees below it are skipped.
    Raises: ``SourceChangedError`` for a component that is a link or cannot be opened.
    """
    try:
        parent, name = _open_parent(root, relative, relative)
    except SourceChangedError as error:
        if error.problem == "missing":
            return
        raise
    try:
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(metadata.st_mode):
            yield relative, metadata
            return
        descriptor = _open_child(parent, name, relative)
    finally:
        os.close(parent)
    if descriptor is not None:
        yield from _scan_directory(descriptor, relative)


def _list(root: Path, relative: str) -> list[tuple[str, os.stat_result]]:
    """The entries directly in the directory ``relative`` (none when it is missing), sorted."""
    try:
        parent, name = _open_parent(root, relative, relative)
    except SourceChangedError as error:
        if error.problem == "missing":
            return []
        raise
    try:
        descriptor = _open_child(parent, name, relative)
    finally:
        os.close(parent)
    if descriptor is None:
        return []
    try:
        return _listing(descriptor)
    finally:
        os.close(descriptor)


def require_classified_tree(layout: AssetLayout) -> int:
    """Classify every file under the asset root before a pass reads anything.

    Mac-only subtrees are not entered; symbolic links and special files are classified like
    files and never followed.
    Params: ``layout``, a validated asset root.
    Returns: how many paths were classified.
    Raises: ``StorageError`` (``STORAGE.PROTOCOL``) naming the first path no rule covers, or a
    path under ``memos/``: the ``memos`` class is read from its own root, never from the asset
    root; ``SourceChangedError`` when a directory turns into something the walk cannot enter
    while it runs (retry the pass).
    """
    count = 0
    for path, _ in _scan_directory(os.open(layout.root, _DIRECTORY_FLAGS), ""):
        if classify(path).rule.disposition == "memos":
            raise storage_error(
                "STORAGE.PROTOCOL",
                f"the asset root holds {path}, but memos live outside it and are copied from "
                "their own root (--memos-root); move it out of the asset root",
            )
        count += 1
    return count


# -- reading helpers -----------------------------------------------------------------------


def _read_record(root: Path, relative: str, *, private: bool = True) -> bytes:
    """A record's bytes, read whole through a checked reader.

    Raises: ``SourceChangedError`` as ``ReplicationItem.open``, and (``invalid``) for a record
    larger than ``MAX_RECORD_BYTES``.
    """
    item = ReplicationItem(
        path=relative,
        file_class="records",
        kind="file",
        mode=0o600,
        expected_sha256=None,
        expected_size=None,
        root=root,
        source=relative,
        private=private,
    )
    with item.open() as reader:
        if reader.identity.size > MAX_RECORD_BYTES:
            raise SourceChangedError(
                relative, "invalid", f"is larger than the {MAX_RECORD_BYTES}-byte record limit"
            )
        data = b"".join(reader.chunks())
    return data


def _optional_record(root: Path, relative: str) -> bytes | None:
    if not os.path.lexists(root / relative):
        return None
    return _read_record(root, relative)


def _parse[ModelT: BaseModel](model: type[ModelT], data: bytes, path: str) -> ModelT:
    try:
        return model.model_validate_json(data)
    except ValidationError as error:
        raise SourceChangedError(
            path,
            "invalid",
            f"is not a valid {model.__name__} record ({error.error_count()} problem(s))",
        ) from error


def _digest(data: bytes) -> tuple[str, int]:
    return hashlib.sha256(data).hexdigest(), len(data)


def _evidence(model: BaseModel) -> tuple[str, int]:
    """The digest and size of the canonical evidence file that holds ``model``."""
    return _digest(canonical_json_bytes(model) + b"\n")


def _decided(path: str) -> ClassificationRule | None:
    """The rule that decides ``path``, or ``None`` when none does (never raises)."""
    return next(iter(matching_rules(path)), None)


def _served(path: str, rules: frozenset[str]) -> ClassificationRule | None:
    """The rule of ``path`` when one of ``rules`` decides it, else ``None``."""
    rule = classify(path).rule
    return rule if rule.name in rules else None


def _item(
    layout: AssetLayout,
    path: str,
    rule: ClassificationRule,
    expected: tuple[str | None, int | None] = (None, None),
    *,
    private: bool = True,
) -> ReplicationItem:
    return ReplicationItem(
        path=path,
        file_class=cast("FileClass", rule.disposition),
        kind="file",
        mode=rule.mode,
        expected_sha256=expected[0],
        expected_size=expected[1],
        root=layout.root,
        source=path,
        private=private,
    )


def _withheld(path: str, rule: ClassificationRule, reason: str) -> WithheldPath:
    return WithheldPath(path, cast("FileClass", rule.disposition), reason)


def _guarded(
    inner: Generator[SourceEntry, None, None], recheck: Callable[[], None]
) -> Generator[SourceEntry, None, None]:
    """``inner``'s entries; every failure, and the end, first re-checks the owner's guard.

    A guard that moved turns any failure into its own ``changed`` error, so a file that
    changed because its owner committed is reported as the commit, not as a refusal. A failure
    thrown in at a ``yield`` is passed on to ``inner`` first (so guards nest, inner first).
    """
    with closing(inner):
        thrown: SourceChangedError | None = None
        while True:
            try:
                entry = next(inner) if thrown is None else inner.throw(thrown)
            except StopIteration:
                break
            except SourceChangedError:
                recheck()
                raise
            thrown = None
            try:
                yield entry
            except SourceChangedError as error:
                thrown = error
    recheck()


# -- sources ---------------------------------------------------------------------------------


class ScanRoot(NamedTuple):
    """A directory (or file) a record source scans, and the order of its parts.

    ``order`` names subpaths of ``base`` to scan in that order; ``"*"`` stands for
    everything under ``base`` that no other part names.
    """

    base: str
    order: tuple[str, ...] = ("*",)


def _scan_ordered(root: Path, scan: ScanRoot) -> Iterator[tuple[str, os.stat_result]]:
    named = tuple(f"{scan.base}/{part}" for part in scan.order if part != "*")
    for part in scan.order:
        if part != "*":
            yield from _scan(root, f"{scan.base}/{part}")
            continue
        for path, metadata in _scan(root, scan.base):
            if not any(path == other or path.startswith(f"{other}/") for other in named):
                yield path, metadata


def _name_digest(path: str) -> str:
    """The SHA-256 a digest-named file carries: the dotted part before its extension."""
    return PurePosixPath(path).name.split(".")[-2]


@dataclass(frozen=True, slots=True)
class RecordTreeSource:
    """An owner whose files are records no other record binds: each is copied as it is.

    Every file under ``roots`` (scanned in order) that one of ``rules`` decides is offered
    with no expected digest, except that a file of a ``digest_named`` rule (its name ends in
    ``.<sha256>.<extension>``) is bound to the digest in its name; the reader still proves each
    file held still while read. A file of an ``in_place`` rule (its writer creates it, then
    writes its bytes, with no rename) that is still empty when listed is being written, and is
    withheld. ``private=False`` reads files the lab does not keep private (the operator writes
    them), so group- or other-readable bits do not refuse them.
    """

    name: str
    rules: frozenset[str]
    roots: tuple[ScanRoot, ...]
    referents: frozenset[str] = frozenset()
    private: bool = True
    in_place: frozenset[str] = frozenset()
    digest_named: frozenset[str] = frozenset()

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        for scan in self.roots:
            for path, metadata in _scan_ordered(layout.root, scan):
                rule = _served(path, self.rules)
                if rule is None:
                    continue
                if rule.name in self.in_place and metadata.st_size == 0:
                    yield _withheld(
                        path, rule, "is still being written; it is carried once its bytes are in"
                    )
                    continue
                expected = (_name_digest(path), None) if rule.name in self.digest_named else None
                yield _item(layout, path, rule, expected or (None, None), private=self.private)


@dataclass(frozen=True, slots=True)
class DigestNamedSource:
    """An owner whose files are named by their own SHA-256 (rendered edits)."""

    name: str
    rules: frozenset[str]
    base: str
    referents: frozenset[str] = frozenset()

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        for path, _ in _scan(layout.root, self.base):
            rule = _served(path, self.rules)
            if rule is not None:
                yield _item(layout, path, rule, (_name_digest(path), None))


class _TakeStoreGuard:
    """The take store's commit fence for one pass: no pending journal, the same store head."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.head = self._current()

    def _current(self) -> bytes | None:
        if os.path.lexists(self._root / _TAKE_PENDING):
            raise SourceChangedError(_TAKE_PENDING, "changed", "a take store commit is running")
        return _optional_record(self._root, _TAKE_STORE_HEAD)

    def recheck(self) -> None:
        if self._current() != self.head:
            raise SourceChangedError(
                _TAKE_STORE_HEAD, "changed", "the take store committed during the pass"
            )


def _store_call[ResultT](path: str, action: Callable[[], ResultT]) -> ResultT:
    """Run one read-only take store call; its failures become source failures."""
    from .recording_takes import RecordingMetadataBusyError

    try:
        return action()
    except RecordingMetadataBusyError as error:
        raise SourceChangedError(path, "changed", "a take store commit is running") from error
    except (ContractError, SecurityError, ValidationError, OSError) as error:
        raise SourceChangedError(
            path, "invalid", f"the take store refused a read: {error}"
        ) from error


def _take_store_rules() -> frozenset[str]:
    return frozenset(
        {
            "take-records",
            "take-operations",
            "take-cursors",
            "take-analysis-heads",
            "take-store-head",
            "native-edit-head",
            "native-edit-recipes",
            "native-originals",
        }
    )


@dataclass(frozen=True, slots=True)
class TakeStoreSource:
    """Native takes: records, their native edit recipes and their sealed originals.

    Read through ``RecordingTakeStore(read_only=True)`` and its projection readers, which
    never take ``.metadata.lock``. The guard requires no ``.pending.json`` and the same
    ``.store-head.json`` bytes before, during every failure and after the owner's files are
    read, so a commit during the pass makes the owner ``changed``. Takes (the referrers) are
    read before the operation journal, and every file a take head names is bound to it: the
    head, reservation and seal to their canonical bytes and the original to the seal's
    digest and size. An unsealed take's audio and the files of a take no head names yet are
    withheld. Takes name their groups and, through them, catalog snapshots.
    """

    name: str = "recording-takes"
    rules: frozenset[str] = field(default_factory=_take_store_rules)
    page_size: int = TAKE_PAGE_SIZE
    referents: frozenset[str] = frozenset({"recording-groups", "prompt-database"})

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        if not os.path.lexists(layout.root / _TAKES):
            return
        from .recording_takes import RecordingTakeStore

        guard = _TakeStoreGuard(layout.root)
        store = _store_call(_TAKES, functools.partial(RecordingTakeStore, layout, read_only=True))
        yield from _guarded(self._records(layout, store, guard), guard.recheck)

    def _records(
        self, layout: AssetLayout, store: RecordingTakeStore, guard: _TakeStoreGuard
    ) -> Generator[SourceEntry, None, None]:
        root = layout.root
        taken: set[str] = set()
        after: str | None = None
        while True:
            page = _store_call(
                _TAKES,
                functools.partial(
                    store.iter_take_ids_for_projection, after_take_id=after, limit=self.page_size
                ),
            )
            for take_id in page:
                if (yield from self._take(layout, store, take_id)):
                    taken.add(take_id)
            if len(page) < self.page_size:
                break
            after = page[-1]
        for part in (".operations", ".cursors", ".analysis"):
            for path, _ in _scan(root, f"{_TAKES}/{part}"):
                rule = _served(path, self.rules)
                if rule is not None:
                    yield _item(layout, path, rule)
        for base in (_NATIVE_EDITS, _TAKE_AUDIO):
            for path, _ in _scan(root, base):
                rule = _served(path, self.rules)
                if rule is not None and path.split("/")[len(base.split("/"))] not in taken:
                    yield _withheld(path, rule, "no take record names this file")
        if guard.head is not None:
            yield _item(
                layout, _TAKE_STORE_HEAD, rule_named("take-store-head"), _digest(guard.head)
            )

    def _take(
        self, layout: AssetLayout, store: RecordingTakeStore, take_id: str
    ) -> Generator[SourceEntry, None, bool]:
        """One take's files; returns whether a committed head names the take."""
        root = layout.root
        directory = f"{_TAKES}/{take_id}"
        offered: set[str] = set()
        head_path = f"{directory}/head.json"
        committed = os.path.lexists(root / head_path)
        if committed:
            take = _store_call(head_path, functools.partial(store.get_take_for_projection, take_id))
            records = rule_named("take-records")
            bound: list[tuple[str, BaseModel | None]] = [
                (head_path, take),
                (f"{directory}/reservation.json", take.reservation),
                (f"{directory}/seal.json", take.seal),
            ]
            for path, record in bound:
                if record is not None:
                    offered.add(path)
                    yield _item(layout, path, records, _evidence(record))
            original = take.reservation.original_path
            original_rule = _served(original, self.rules)
            if original_rule is None:
                raise SourceChangedError(
                    original, "invalid", "a take reservation names audio outside its take"
                )
            if take.seal is not None:
                yield _item(
                    layout,
                    original,
                    original_rule,
                    (take.seal.original_sha256, take.seal.original_size_bytes),
                )
            elif os.path.lexists(root / original):
                yield _withheld(
                    original,
                    original_rule,
                    "the take is not sealed yet; its audio is carried once its seal names it",
                )
            for path, _ in _scan(root, f"{_NATIVE_EDITS}/{take_id}"):
                rule = _served(path, self.rules)
                if rule is not None:
                    yield _item(layout, path, rule)
        for path, _ in _scan(root, directory):
            rule = _served(path, self.rules)
            if rule is not None and path not in offered:
                yield _withheld(path, rule, "no committed take head names this file")
        return committed


def _edit_record_rules() -> frozenset[str]:
    return frozenset(
        {
            "desktop-edits",
            "legacy-edit-heads",
            "legacy-edit-results",
            "legacy-edit-requests",
            "native-edit-render-records",
        }
    )


class _IdentityGuard:
    """The identities of an owner's served files; a pass fails the owner if any of them moves.

    ``directories`` lists, each time the guard looks, the directories (or files) whose served
    files it watches, so a file or directory that appears counts as a change too.
    """

    def __init__(
        self, root: Path, directories: Callable[[], Iterable[str]], rules: frozenset[str]
    ) -> None:
        self._root = root
        self._directories = directories
        self._rules = rules
        self.identities = self._current()

    def _current(self) -> dict[str, FileIdentity]:
        return {
            path: FileIdentity.of(metadata)
            for directory in self._directories()
            for path, metadata in _scan(self._root, directory)
            if _served(path, self._rules) is not None
        }

    def recheck(self) -> None:
        current = self._current()
        moved = {
            path
            for path in current.keys() | self.identities.keys()
            if current.get(path) != self.identities.get(path)
        }
        if moved:
            raise SourceChangedError(min(moved), "changed", "changed during the pass")


def _package_items(
    layout: AssetLayout, directory: str, rules: frozenset[str], *, raw: bool
) -> Generator[SourceEntry, None, None]:
    """A READY capture package: the marker, the manifest and QC it names, the files it lists.

    For an accepted raw session the intake receipt and the intake QC report it names are
    bound too, and the directory must be named by the manifest digest.
    """
    from .capture import ReadyMarker

    root = layout.root
    ready_path = f"{directory}/{_READY}"
    ready_bytes = _read_record(root, ready_path)
    ready = _parse(ReadyMarker, ready_bytes, ready_path)
    manifest_path = f"{directory}/manifest.json"
    manifest_bytes = _read_record(root, manifest_path)
    if _digest(manifest_bytes)[0] != ready.manifest_sha256:
        raise SourceChangedError(
            manifest_path, "mismatch", "does not match the digest its READY marker names"
        )
    manifest = _parse(CaptureManifest, manifest_bytes, manifest_path)
    name = PurePosixPath(directory).name
    if name != (ready.manifest_sha256 if raw else f"capture-{ready.session_id}"):
        raise SourceChangedError(directory, "invalid", "is not named for the package it holds")
    expected: dict[str, tuple[str | None, int | None]] = {
        f"{directory}/{entry.path}": (entry.sha256, entry.size_bytes) for entry in manifest.files
    }
    expected[ready_path] = _digest(ready_bytes)
    expected[manifest_path] = _digest(manifest_bytes)
    expected[f"{directory}/qc.json"] = (ready.qc_report_sha256, None)
    if raw:
        from .intake import INTAKE_QC_REPORT_NAME, RECEIPT_NAME, IntakeReceipt

        receipt_path = f"{directory}/{RECEIPT_NAME}"
        receipt_bytes = _read_record(root, receipt_path)
        receipt = _parse(IntakeReceipt, receipt_bytes, receipt_path)
        if receipt.manifest_sha256 != ready.manifest_sha256 or receipt.raw_path != directory:
            raise SourceChangedError(
                receipt_path, "invalid", "names another raw session than the one it is in"
            )
        expected[receipt_path] = _digest(receipt_bytes)
        expected[f"{directory}/{INTAKE_QC_REPORT_NAME}"] = (
            receipt.intake_qc_report_sha256,
            None,
        )
    for path, _ in _scan(root, directory):
        rule = _served(path, rules)
        if rule is not None:
            yield _item(layout, path, rule, expected.get(path, (None, None)))


def _capture_session_rules() -> frozenset[str]:
    return frozenset({"desktop-sessions", "capture-sessions", "rejected-takes"})


@dataclass(frozen=True, slots=True)
class CaptureSessionSource:
    """Legacy desktop sessions and capture packages under ``tmp/capture-*``.

    The desktop session records are read first. The guard is the identity of every file the
    owner serves: the session records and everything in the capture directories, so a take
    recorded, retaken or rejected during the pass (the writers rewrite the audio or the
    rejected-take log apart from, or without, a session record) makes the owner ``changed``,
    not ``refused``. A session with a READY marker is carried whole, bound to its marker and
    manifest. Before READY, a session's audio is carried when its desktop session record names
    the take's digest, and interrupted ``.partial.wav`` leftovers of the retired desktop app
    are carried as they are (the index rebuild reads them); anything else is withheld.
    Rejected takes are carried as their log names them. Native package directories need
    their package maps, and desktop sessions accepted since live under ``raw/``.
    """

    name: str = "capture-sessions"
    rules: frozenset[str] = field(default_factory=_capture_session_rules)
    referents: frozenset[str] = frozenset({"recording-packages", "raw-packages"})

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        root = layout.root
        guard = _IdentityGuard(
            root,
            lambda: (_SIDECARS, *(path for path, _ in self._directories(root))),
            self.rules,
        )
        sidecars = sorted(path for path in guard.identities if path.startswith(f"{_SIDECARS}/"))
        yield from _guarded(self._sessions(layout, sidecars), guard.recheck)

    def _directories(self, root: Path) -> Iterator[tuple[str, ClassificationRule]]:
        """Each ``tmp/capture-*`` directory this owner serves, with the rule that decides it."""
        for name, metadata in _list(root, "tmp"):
            directory = f"tmp/{name}"
            if not name.startswith("capture-") or not stat.S_ISDIR(metadata.st_mode):
                continue
            rule = _decided(f"{directory}/{_READY}")
            if rule is not None and rule.name in self.rules:
                yield directory, rule

    def _sessions(
        self, layout: AssetLayout, sidecar_paths: Sequence[str]
    ) -> Generator[SourceEntry, None, None]:
        from .recording_library import DesktopSessionEvidence

        root = layout.root
        sidecars: dict[str, DesktopSessionEvidence] = {}
        for path in sidecar_paths:
            data = _read_record(root, path)
            evidence = _parse(DesktopSessionEvidence, data, path)
            if PurePosixPath(path).name != f"{evidence.session_id}.json":
                raise SourceChangedError(path, "invalid", "is not named for its session")
            sidecars[evidence.session_id] = evidence
            yield _item(layout, path, rule_named("desktop-sessions"), _digest(data))
        for directory, rule in self._directories(root):
            name = PurePosixPath(directory).name
            if rule.name == "rejected-takes":
                yield from self._rejected(layout, directory)
            elif os.path.lexists(root / directory / _READY):
                yield from _package_items(layout, directory, self.rules, raw=False)
            else:
                yield from self._unfinished(layout, directory, sidecars.get(name[8:]))

    def _unfinished(
        self, layout: AssetLayout, directory: str, evidence: DesktopSessionEvidence | None
    ) -> Generator[SourceEntry, None, None]:
        from .capture import expected_recording_paths

        named: dict[str, str] = {}
        if evidence is not None:
            prompts = evidence.selected_suite.prompts
            relatives = expected_recording_paths(evidence.selected_suite)
            for prompt, relative in zip(prompts, relatives, strict=True):
                for take in evidence.takes:
                    if take.prompt_id == prompt.prompt_id and not take.rejected:
                        if take.audio_sha256 is not None:
                            named[f"{directory}/{relative}"] = take.audio_sha256
        for path, _ in _scan(layout.root, directory):
            rule = _served(path, self.rules)
            if rule is None:
                continue
            if path in named:
                yield _item(layout, path, rule, (named[path], None))
            elif path.endswith(".partial.wav") and evidence is not None:
                yield _item(layout, path, rule)
            else:
                yield _withheld(
                    path,
                    rule,
                    "the session has no READY marker and no desktop session record names this "
                    "file's audio",
                )

    def _rejected(self, layout: AssetLayout, directory: str) -> Generator[SourceEntry, None, None]:
        from .capture import REJECTED_LOG_NAME, RejectedTakeLog

        root = layout.root
        log_path = f"{directory}/{REJECTED_LOG_NAME}"
        named: dict[str, tuple[str | None, int | None]] = {}
        if os.path.lexists(root / log_path):
            data = _read_record(root, log_path)
            log = _parse(RejectedTakeLog, data, log_path)
            if directory != f"tmp/capture-{log.session_id}.rejected":
                raise SourceChangedError(log_path, "invalid", "names another session")
            named[log_path] = _digest(data)
            for entry in log.entries:
                named[f"{directory}/{entry.relative}"] = (entry.sha256, entry.size_bytes)
        for path, _ in _scan(root, directory):
            rule = _served(path, self.rules)
            if rule is None:
                continue
            if path in named:
                yield _item(layout, path, rule, named[path])
            else:
                yield _withheld(path, rule, "the rejected-take log does not name this file yet")


@dataclass(frozen=True, slots=True)
class RawPackageSource:
    """Accepted raw sessions: every file bound to its READY marker, manifest and receipt.

    A raw session of a native package prepared on this Mac needs that package's map: the
    library resolves the session's takes through it (``load_recording_package_map``, which
    refuses a local session without one, and ``resolve_native_package_alias``). The map is
    written before the package directory is published and so before any accept, so raw
    sessions are read before the package records and fall back with them.
    """

    name: str = "raw-packages"
    rules: frozenset[str] = frozenset({"raw-packages"})
    referents: frozenset[str] = frozenset({"recording-packages"})

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        for name, metadata in _list(layout.root, "raw"):
            path = f"raw/{name}"
            rule = _served(path, self.rules)
            if rule is None:
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                yield _item(layout, path, rule)
            elif not os.path.lexists(layout.root / path / _READY):
                raise SourceChangedError(path, "invalid", "an accepted raw session has no READY")
            else:
                yield from _package_items(layout, path, self.rules, raw=True)


@dataclass(frozen=True, slots=True)
class ReferenceSource:
    """Published references: the manifest and the reference audio its output binds.

    A reference names its raw source, its consent and its headphone review.
    """

    name: str = "references"
    rules: frozenset[str] = frozenset({"references"})
    referents: frozenset[str] = frozenset({"lab-approvals", "raw-packages"})

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        from .references import REFERENCE_MANIFEST_NAME, ReferenceManifest

        root = layout.root
        for name, metadata in _list(root, "references"):
            directory = f"references/{name}"
            rule = _served(directory, self.rules)
            if rule is None:
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                yield _item(layout, directory, rule)
                continue
            manifest_path = f"{directory}/{REFERENCE_MANIFEST_NAME}"
            if not os.path.lexists(root / manifest_path):
                raise SourceChangedError(directory, "invalid", "a reference has no manifest")
            data = _read_record(root, manifest_path)
            manifest = _parse(ReferenceManifest, data, manifest_path)
            if manifest.reference_id != name:
                raise SourceChangedError(manifest_path, "invalid", "names another reference")
            expected: dict[str, tuple[str | None, int | None]] = {
                manifest_path: _digest(data),
                manifest.output.path: (manifest.output.sha256, manifest.output.size_bytes),
            }
            for path, _ in _scan(root, directory):
                path_rule = _served(path, self.rules)
                if path_rule is not None:
                    yield _item(layout, path, path_rule, expected.get(path, (None, None)))


def _fine_tune_rules() -> frozenset[str]:
    return frozenset(
        {
            "fine-tune-pretrained-link",
            "fine-tune-archive-records",
            "fine-tune-removal-history",
            "fine-tune-run",
        }
    )


def _withhold_all(paths: Collection[str], reason: str) -> Iterator[WithheldPath]:
    for path in paths:
        yield _withheld(path, classify(path).rule, reason)


def _fine_tune_order(directory: str, path: str) -> tuple[int, str]:
    """Run records first (they bind the rest), then everything else by path."""
    relative = path.removeprefix(f"{directory}/")
    if relative == "run.json":
        return 0, path
    if relative == "dataset/dataset.json":
        return 1, path
    parts = PurePosixPath(relative).parts
    if len(parts) == 3 and parts[0] == "samples" and parts[2] == "samples.json":
        return 2, path
    return 3, path


def _sample_set(directory: str, path: str) -> str | None:
    """The sample set directory (``<run>/samples/<label>``) holding ``path``, if any."""
    parts = PurePosixPath(path.removeprefix(f"{directory}/")).parts
    if len(parts) > 2 and parts[0] == "samples":
        return f"{directory}/samples/{parts[1]}"
    return None


def _content_sha256(layout: AssetLayout, path: str) -> str:
    """The SHA-256 of a run file, read through a checked reader (shared-readable allowed).

    Raises: ``SourceChangedError`` as ``ReplicationItem.open``.
    """
    item = _item(layout, path, classify(path).rule, private=False)
    with item.open() as reader:
        for _ in reader.chunks():
            pass
    return reader.sha256


_SAMPLES_REWRITTEN: Final = (
    "a synthesizer is rewriting this sample set, or an interrupted one left it; the set is "
    "carried once its files match samples.json again (synthesize the set again to replace an "
    "interrupted one)"
)
_SAMPLES_UNRECORDED: Final = (
    "a synthesizer is writing this sample set, or an interrupted one left its work directory, "
    "and no samples.json records the set yet; it is carried once one does (synthesize the set "
    "again to replace an interrupted one)"
)


class _RunState(NamedTuple):
    """What must hold still while a run is read.

    ``files``: the identities of the run's served files. ``synthesizing``: the sample set
    directories holding a synthesizer's work directory (``synthesis-tmp``), which the
    synthesizer creates when it starts and removes when it exits.
    """

    files: dict[str, FileIdentity]
    synthesizing: frozenset[str]


@dataclass(frozen=True, slots=True)
class FineTuneRunSource:
    """Finished fine-tune runs, only while no fine-tune job or Speak request is active.

    A running job writes a run's samples after its ``run.json`` says ``finished``, and Speak
    reads runs, so any active job or live Speak request makes the whole owner ``changed``
    (the vault keeps the runs it had); the same holds when one starts during the pass. A
    finished run's state is its guard: the identities of its files and which of its sample
    sets hold a synthesizer's work directory. When that state moves during the pass, a read
    fails, or the run ends, the owner is ``changed``; a run that is not finished is withheld.
    Checkpoints, dataset clips and the metadata file are bound to the digests ``run.json`` and
    ``dataset.json`` name. The hard-linked pretrained weights become a pin entry (the digest
    ``run.json`` pins, the file's size) and are never read. Run files may be shared-readable
    (runs made on the command line keep the umask). A run's corpus names the catalog snapshot
    it was selected from.
    Sample sets (``samples/<label>/``) are the one part of a finished run that
    ``voice-lab finetune synthesize`` rewrites with no job to announce it: it rewrites the
    samples in place, then hashes the checkpoint, then replaces ``samples.json``, and an
    interrupted synthesis leaves the samples and their record disagreeing. So a set is carried
    only when every sample its ``samples.json`` names (each directly in the set) that is there
    has the named digest (like any other record here, a named file that is gone is not a
    failure); otherwise the whole set is withheld, not refused. A set with no ``samples.json``
    is withheld while a synthesizer's work directory is in it, and carried as it is otherwise.
    A work directory an interrupted synthesizer left in a set whose files match its record
    changes nothing: the directory stays on the Mac and the set is carried. So no moment of a
    synthesis makes a pass refuse the run: a sample that differs from its record when the set
    is bound withholds the set, and one that changes after that has moved the guard.
    """

    name: str = "fine-tune-runs"
    rules: frozenset[str] = field(default_factory=_fine_tune_rules)
    referents: frozenset[str] = frozenset({"prompt-database"})

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        if not os.path.lexists(layout.root / _FINETUNE_RUNS):
            return
        recheck = functools.partial(self._require_idle, layout)
        recheck()
        yield from _guarded(self._runs(layout), recheck)

    @staticmethod
    def _require_idle(layout: AssetLayout) -> None:
        from .recording_finetune_jobs import active_finetune_job, active_speech_request

        try:
            job = active_finetune_job(layout)
            speech = active_speech_request(layout)
        except (ContractError, SecurityError) as error:
            raise SourceChangedError(
                _FINETUNE_QUEUE, "invalid", f"the fine-tune queue cannot be read: {error}"
            ) from error
        if job is not None:
            raise SourceChangedError(
                _FINETUNE_RUNS,
                "changed",
                f"fine-tune job {job.operation_id} is {job.state}; runs are carried once no "
                "job is active",
            )
        if speech is not None:
            raise SourceChangedError(
                _FINETUNE_RUNS,
                "changed",
                f"Speak request {speech.operation_id} is running; runs are carried once it ends",
            )

    def _state(self, layout: AssetLayout, directory: str) -> _RunState:
        from .finetune import SYNTHESIS_WORK_DIRECTORY

        root = layout.root
        samples = f"{directory}/samples"
        return _RunState(
            files={
                path: FileIdentity.of(metadata)
                for path, metadata in _scan(root, directory)
                if _served(path, self.rules) is not None
            },
            synthesizing=frozenset(
                f"{samples}/{label}"
                for label, metadata in _list(root, samples)
                if stat.S_ISDIR(metadata.st_mode)
                and any(
                    name == SYNTHESIS_WORK_DIRECTORY
                    for name, _ in _list(root, f"{samples}/{label}")
                )
            ),
        )

    def _runs(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        for name, metadata in _list(layout.root, _FINETUNE_RUNS):
            if stat.S_ISDIR(metadata.st_mode):
                yield from self._run(layout, f"{_FINETUNE_RUNS}/{name}", name)

    def _require_unchanged(self, layout: AssetLayout, directory: str, before: _RunState) -> None:
        if self._state(layout, directory) != before:
            raise SourceChangedError(directory, "changed", "the run changed during the pass")

    def _run(
        self, layout: AssetLayout, directory: str, run_id: str
    ) -> Generator[SourceEntry, None, None]:
        from .finetune import FineTuneRun

        root = layout.root
        before = self._state(layout, directory)
        run_path = f"{directory}/run.json"
        if run_path not in before.files:
            yield from _withhold_all(before.files, "the run has no run record yet")
            return
        run_bytes = _read_record(root, run_path, private=False)
        run = _parse(FineTuneRun, run_bytes, run_path)
        if run.run_id != run_id or run.run_root != directory:
            raise SourceChangedError(run_path, "invalid", "names another run")
        if run.status != "finished":
            yield from _withhold_all(before.files, f"the run is {run.status}, not finished")
            return
        yield from _guarded(
            self._finished(layout, directory, run, run_bytes, before),
            functools.partial(self._require_unchanged, layout, directory, before),
        )

    @staticmethod
    def _removed(
        layout: AssetLayout, directory: str, run_id: str, before: _RunState
    ) -> Generator[SourceEntry, None, None]:
        """Carry the entries of a run whose checkpoints were archived and then removed.

        ``local-removal.json`` names the generation whose manifest holds the removed files;
        every later generation carries those entries, so the vault's newest generation still
        names them and a restore of it brings the whole run back. A named file that is still
        there (a crash between the record and the unlinks) is left to the ordinary read.
        Raises: ``SourceChangedError`` (``invalid``) when the record names another run or a
        file its classification does not carry the way the record does.
        """
        from .finetune import FINETUNE_REMOVAL_NAME, FineTuneRunRemoval

        listing = f"{directory}/{FINETUNE_REMOVAL_NAME}"
        if listing not in before.files:
            return
        removal = _parse(
            FineTuneRunRemoval, _read_record(layout.root, listing, private=False), listing
        )
        if removal.run_id != run_id:
            raise SourceChangedError(listing, "invalid", "names another run")
        for file in removal.files:
            path = f"{directory}/{file.path}"
            if path in before.files:
                continue
            rule = classify(path).rule
            if rule.name not in _fine_tune_rules() or (file.kind == "pin") != (
                rule.transfer == "pin"
            ):
                raise SourceChangedError(
                    listing, "invalid", f"names {file.path}, which is not one of its run's files"
                )
            yield CarriedPath(
                path=path,
                file_class="training",
                kind=file.kind,
                mode=rule.mode,
                sha256=file.sha256,
                size=file.size,
                generation=removal.generation,
                reason=(
                    f"removed from this Mac after the copy in generation {removal.generation} "
                    f"was verified; restore it with `voice-lab finetune restore-run --run "
                    f"{run_id}`"
                ),
                pin_source=PRETRAINED_PIN_SOURCE if file.kind == "pin" else None,
            )

    @staticmethod
    def _bind_samples(
        layout: AssetLayout,
        listing: str,
        files: Mapping[str, FileIdentity],
        expected: dict[str, tuple[str | None, int | None]],
    ) -> bool:
        """Bind a sample set to its ``samples.json`` when the samples it names that are there match.

        Adds the record's digest and each named sample's digest to ``expected`` and returns
        ``True``; returns ``False`` (binding nothing) when a named sample that is there has
        another digest.
        Raises: ``SourceChangedError`` (``invalid``) for a record that is not a sample set or
        names a file outside its set, or names one twice; as ``ReplicationItem.open`` for a
        record or sample that cannot be read safely.
        """
        from .finetune import FineTuneSampleSet

        data = _read_record(layout.root, listing, private=False)
        record = _parse(FineTuneSampleSet, data, listing)
        folder = PurePosixPath(listing).parent
        named: dict[str, tuple[str | None, int | None]] = {}
        for sample in record.samples:
            path = sample.relative_path
            if PurePosixPath(path).parent != folder or path == listing or path in named:
                raise SourceChangedError(
                    listing, "invalid", f"names {path}, which is not one more sample of its set"
                )
            named[path] = (sample.sha256, None)
        for path, (digest, _) in sorted(named.items()):
            if path in files and _content_sha256(layout, path) != digest:
                return False
        expected.update(named)
        expected[listing] = _digest(data)
        return True

    def _finished(
        self,
        layout: AssetLayout,
        directory: str,
        run: FineTuneRun,
        run_bytes: bytes,
        before: _RunState,
    ) -> Generator[SourceEntry, None, None]:
        from .finetune import FineTuneDataset

        root = layout.root
        run_path = f"{directory}/run.json"
        expected: dict[str, tuple[str | None, int | None]] = {run_path: _digest(run_bytes)}
        for checkpoint in run.checkpoints:
            expected[checkpoint.relative_path] = (checkpoint.sha256, checkpoint.size_bytes)
        yield from self._removed(layout, directory, run.run_id, before)
        withheld = {
            folder: _SAMPLES_UNRECORDED
            for folder in before.synthesizing
            if f"{folder}/samples.json" not in before.files
        }
        for path in sorted(before.files, key=functools.partial(_fine_tune_order, directory)):
            order = _fine_tune_order(directory, path)[0]
            folder = _sample_set(directory, path)
            if order == 1:
                data = _read_record(root, path, private=False)
                dataset = _parse(FineTuneDataset, data, path)
                if canonical_sha256(dataset) != run.dataset_sha256 or dataset.dataset_root != (
                    f"{directory}/dataset"
                ):
                    raise SourceChangedError(path, "mismatch", "is not the dataset its run names")
                expected[path] = _digest(data)
                expected[f"{directory}/dataset/metadata.csv"] = (dataset.metadata_csv_sha256, None)
                for clip in dataset.clips:
                    expected[f"{dataset.dataset_root}/{clip.relative_path}"] = (clip.sha256, None)
            elif (
                order == 2
                and folder is not None
                and not self._bind_samples(layout, path, before.files, expected)
            ):
                withheld[folder] = _SAMPLES_REWRITTEN
            rule = classify(path).rule
            if folder in withheld:
                yield _withheld(path, rule, withheld[folder])
            elif rule.transfer != "pin":
                yield _item(layout, path, rule, expected.get(path, (None, None)), private=False)
            elif PurePosixPath(path).name != (
                f"pretrained_{PurePosixPath(run.runtime.pretrained_path).name}"
            ):
                raise SourceChangedError(path, "invalid", "holds weights its run does not pin")
            else:
                yield ReplicationItem(
                    path=path,
                    file_class="training",
                    kind="pin",
                    mode=rule.mode,
                    expected_sha256=run.runtime.pretrained_sha256,
                    expected_size=before.files[path].size,
                    root=root,
                    source=path,
                    private=False,
                    pin_source=PRETRAINED_PIN_SOURCE,
                )


def backup_database(source: Path, target: Path) -> None:
    """Copy a live SQLite database into ``target`` with the online backup API, read-only.

    The source is opened only by SQLite, through a ``mode=ro`` URI (never with ``os.open``,
    so the calling process keeps its own database locks), and copied in one backup step, so a
    concurrent writer neither blocks nor restarts it: the copy is the last committed state.
    The copy is switched to ``journal_mode=DELETE`` (so its bytes are stable) and must pass
    ``PRAGMA quick_check``.
    Params: ``source``, the live database; ``target``, an existing empty private file.
    Returns: nothing.
    Raises: ``sqlite3.OperationalError`` when the source cannot be opened or read now (for
    example busy); ``sqlite3.DatabaseError`` when it is not a database or the copy fails its
    check.
    """
    reader = sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
        timeout=BACKUP_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        copy = sqlite3.connect(target, isolation_level=None)
        try:
            reader.backup(copy, pages=-1)
            copy.execute("PRAGMA trusted_schema = OFF")
            journal = copy.execute("PRAGMA journal_mode = DELETE").fetchall()
            check = copy.execute("PRAGMA quick_check").fetchall()
        finally:
            copy.close()
    finally:
        reader.close()
    if journal != [("delete",)] or check != [("ok",)]:
        raise sqlite3.DatabaseError(f"the database backup failed its check: {check}")


class ScratchDirectory:
    """A private scratch directory ``tmp/.vault-<random>`` (0700), locked while it lives.

    Built by a pass (for the prompt database's backup) or a push (for its spooled copy).
    Creating one first removes the scratch directories whose owners are gone (their
    ``.scratch.lock`` is free); ``remove`` deletes this one and releases its lock. Everything
    under ``tmp/.vault-*`` stays on the Mac.
    Raises (on construction): ``SourceChangedError`` when ``tmp`` cannot be opened safely;
    ``OSError`` when the directory or its lock cannot be created.
    """

    def __init__(self, root: Path) -> None:
        _sweep_scratch(root)
        self.relative = f"tmp/{SCRATCH_PREFIX}{secrets.token_hex(8)}"
        self._root = root
        parent, name = _open_parent(root, self.relative, self.relative)
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            self._directory = os.open(name, _CHILD_DIRECTORY_FLAGS, dir_fd=parent)
        finally:
            os.close(parent)
        os.fchmod(self._directory, 0o700)
        self._lock = os.open(
            SCRATCH_LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._directory,
        )
        fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def new_file(self, name: str) -> tuple[Path, str]:
        """Create an empty private file; returns its path and its root-relative name."""
        os.close(
            os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._directory,
            )
        )
        relative = f"{self.relative}/{name}"
        return self._root / relative, relative

    def remove(self) -> None:
        try:
            shutil.rmtree(self._root / self.relative)
        finally:
            os.close(self._lock)
            os.close(self._directory)


def _sweep_scratch(root: Path) -> None:
    """Remove the scratch of passes that are gone (their scratch lock is free)."""
    for name, metadata in _list(root, "tmp"):
        if not name.startswith(SCRATCH_PREFIX) or not stat.S_ISDIR(metadata.st_mode):
            continue
        relative = f"tmp/{name}/{SCRATCH_LOCK_NAME}"
        try:
            parent, lock_name = _open_parent(root, relative, relative)
        except SourceChangedError:
            continue
        try:
            lock = os.open(lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except OSError:
            continue
        finally:
            os.close(parent)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            continue
        else:
            shutil.rmtree(root / "tmp" / name)
        finally:
            os.close(lock)


@dataclass(frozen=True, slots=True)
class CorpusDatabaseSource:
    """The prompt database, as one online backup, then the catalog snapshots.

    The database is never read as a file: ``backup`` copies it into a private scratch file
    (``tmp/.vault-<random>/``, 0700/0600), whose bytes are offered at the database's own path
    and removed once the pass consumed them (stale scratch of a vanished pass is swept first).
    Snapshots are listed after the backup, so every snapshot the copied database names is
    there. A snapshot still hard-linked to its temporary when listed belongs to a publish
    whose transaction had not committed when the backup was taken (the store links it before
    COMMIT and unlinks the temporary right after), so neither the copy nor any record read
    earlier in the pass can name it: it is withheld, and the owner stays complete.
    """

    name: str = "prompt-database"
    rules: frozenset[str] = frozenset({"corpus-database", "corpus-snapshots"})
    referents: frozenset[str] = frozenset()
    backup: Callable[[Path, Path], None] = backup_database

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        root = layout.root
        try:
            metadata: os.stat_result | None = os.lstat(root / CORPUS_DATABASE_PATH)
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if not stat.S_ISREG(metadata.st_mode):
                raise SourceChangedError(CORPUS_DATABASE_PATH, "unsafe", "is not a regular file")
            yield from self._database(layout)
        for path, listed in _scan(root, _SNAPSHOTS):
            rule = _served(path, self.rules)
            if rule is None:
                continue
            if stat.S_ISREG(listed.st_mode) and listed.st_nlink > 1:
                yield _withheld(
                    path,
                    rule,
                    "a catalog snapshot is being published; it is carried once its publish ends",
                )
            else:
                yield _item(layout, path, rule)

    def _database(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        root = layout.root
        scratch = ScratchDirectory(root)
        try:
            target, relative = scratch.new_file(DATABASE_BACKUP_NAME)
            try:
                self.backup(root / CORPUS_DATABASE_PATH, target)
            except sqlite3.OperationalError as error:
                raise SourceChangedError(
                    CORPUS_DATABASE_PATH, "changed", f"the online backup did not finish: {error}"
                ) from error
            except sqlite3.Error as error:
                raise SourceChangedError(
                    CORPUS_DATABASE_PATH, "invalid", f"the online backup failed: {error}"
                ) from error
            copy = ReplicationItem(
                path=CORPUS_DATABASE_PATH,
                file_class="prompt-database",
                kind="file",
                mode=rule_named("corpus-database").mode,
                expected_sha256=None,
                expected_size=None,
                root=root,
                source=relative,
            )
            digest = hashlib.sha256()
            with copy.open() as reader:
                for chunk in reader.chunks():
                    digest.update(chunk)
            yield ReplicationItem(
                path=CORPUS_DATABASE_PATH,
                file_class="prompt-database",
                kind="file",
                mode=copy.mode,
                expected_sha256=digest.hexdigest(),
                expected_size=reader.size,
                root=root,
                source=relative,
            )
        finally:
            scratch.remove()


def memos_source_path(path: str) -> str:
    """Where the manifest path ``memos/<path>`` is read below the memos root: ``<path>``.

    Raises: ``ValueError`` for a path that is not below ``memos/``.
    """
    prefix = f"{MEMOS_PREFIX}/"
    if not path.startswith(prefix) or path == prefix:
        raise ValueError(f"{path!r:.120} is not below {prefix}")
    return path.removeprefix(prefix)


def _open_absolute_directory(root: Path, label: str) -> int:
    """Open an absolute directory from ``/``, one component at a time, never following a link.

    Raises: ``SourceChangedError`` about ``label``: ``missing`` when a component is gone,
    ``unsafe`` when one is a symbolic link, not a directory or cannot be opened.
    """
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in root.parts[1:]:
            child = os.open(part, _CHILD_DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as error:
        os.close(descriptor)
        raise _open_failure(label, error) from error
    return descriptor


@dataclass(frozen=True, slots=True)
class MemosSource:
    """The phone's Voice Memos drop: every file below ``root``, named ``memos/<path>``.

    The drop (``/data/voice-lab/memos/phone`` on the PC, design §4.1) lies outside the asset
    root, so it is its own source with its own root (design §4.3: the ``memos`` class, opt-in
    per destination like ``training``). The mirror's account (``vj-mirror``) writes it and the
    copier reads it through its group, so its items are neither ``private`` nor ``owned``;
    every other check holds: the root and every component below it are opened without
    following a link, and a file must be a regular file with one link that holds still while
    it is read, so a symbolic link or a special file anywhere in the drop refuses the owner (the
    vault keeps its previous memos entries). rsync's temporaries and its ``.rsync-partial``
    directory (hidden names, ``memos-mirror-temporaries``) stay behind. No record binds a memo
    and a memo names no other owner's files, so each file is copied as it is. A missing root
    makes the owner ``changed`` (retryable), like any vanished source.
    Raises (on construction): ``ValueError`` for a root that is not absolute and normalized.
    """

    root: Path
    name: str = "memos"
    rules: frozenset[str] = frozenset({"memos"})
    referents: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        validate_absolute_path(str(self.root))

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        descriptor = _open_absolute_directory(self.root, MEMOS_PREFIX)
        for path, _ in _scan_directory(descriptor, MEMOS_PREFIX):
            rule = classify(path).rule
            if rule.name not in self.rules:
                continue
            yield ReplicationItem(
                path=path,
                file_class="memos",
                kind="file",
                mode=rule.mode,
                expected_sha256=None,
                expected_size=None,
                root=self.root,
                source=memos_source_path(path),
                private=False,
                owned=False,
            )


def library_sources() -> tuple[ReplicationSource, ...]:
    """One source per owner, each before every owner its files name (its referents).

    A record is written after the files it names, so reading referrers first means every file
    a record read earlier names already exists when its owner is read later. The references:
    edit records name takes, rendered edits and legacy or raw recordings; analysis jobs and
    package records name takes; native package directories and the raw sessions accepted from
    them need their package maps, and desktop sessions accepted since live under ``raw/``;
    continuations name the desktop session they continue and the take store context they
    open, and continuity evidence names session audio; takes name groups, and groups name
    catalog snapshots; fine-tune job records name runs, and runs name snapshots; transfer
    plans name datasets, approvals and consents; datasets and references name consents, gates,
    reviews and raw sessions; gates and reviews name raw sessions. Takes also name analysis
    jobs and package maps, but no reader needs those back (a missing job is queued again and a
    package publication is never resolved to its map), so analysis and packages come first
    and fall back with the takes. Because raw sessions fall back with the package maps, a
    take store commit during a pass also holds back every owner that names raw sessions.
    """
    return (
        RecordTreeSource(
            "recording-edits",
            _edit_record_rules(),
            (ScanRoot("provenance/recording-edits", ("*", "legacy/.operations")),),
            referents=frozenset(
                {"capture-sessions", "recording-takes", "derived-edits", "raw-packages"}
            ),
        ),
        RecordTreeSource(
            "recording-analysis",
            frozenset({"analysis-jobs"}),
            (ScanRoot("provenance/recording-analysis"),),
            referents=frozenset({"recording-takes"}),
        ),
        RecordTreeSource(
            "legacy-continuations",
            frozenset({"legacy-continuations"}),
            (ScanRoot("provenance/recording-legacy-continuations"),),
            referents=frozenset({"capture-sessions", "recording-takes"}),
        ),
        RecordTreeSource(
            "capture-continuity",
            frozenset({"capture-continuity"}),
            (ScanRoot("provenance/capture-continuity"),),
            referents=frozenset({"capture-sessions"}),
            in_place=frozenset({"capture-continuity"}),
        ),
        CaptureSessionSource(),
        RecordTreeSource(
            "fine-tune-job-records",
            frozenset({"fine-tune-job-records"}),
            (ScanRoot("provenance/finetune-jobs"),),
            referents=frozenset({"fine-tune-runs"}),
        ),
        FineTuneRunSource(),
        RecordTreeSource(
            "transfer-plans",
            frozenset({"transfer-plans"}),
            (ScanRoot("derived/transfer-plans"),),
            referents=frozenset({"datasets", "transfer-approvals", "lab-approvals"}),
        ),
        RecordTreeSource(
            "datasets",
            frozenset({"datasets"}),
            (ScanRoot("derived/datasets"),),
            referents=frozenset({"lab-approvals", "raw-packages"}),
        ),
        ReferenceSource(),
        # The operator writes transfer approvals by hand and the lab does not require them to
        # be private, so a shared-readable one is copied, and cannot hold back the consents.
        RecordTreeSource(
            "transfer-approvals",
            frozenset({"transfer-approvals"}),
            (ScanRoot("provenance/transfer-approvals"),),
            private=False,
        ),
        RecordTreeSource(
            "lab-approvals",
            frozenset(
                {
                    "pilot-gates",
                    "headphone-reviews",
                    "consent-history",
                    "consents",
                    "diagnostic-root",
                }
            ),
            (
                ScanRoot("provenance/pilot-gates"),
                ScanRoot("provenance/headphone-reviews"),
                ScanRoot("provenance/consents"),
                ScanRoot("provenance/diagnostic-root.json"),
            ),
            referents=frozenset({"raw-packages"}),
            in_place=frozenset({"consent-history"}),
            digest_named=frozenset({"consent-history"}),
        ),
        RawPackageSource(),
        RecordTreeSource(
            "recording-packages",
            frozenset({"package-records", "package-origin"}),
            (
                ScanRoot("provenance/recording-packages", ("sessions", "operations", "plans", "*")),
                ScanRoot("provenance/recording-package-origin.json"),
            ),
            referents=frozenset({"recording-takes"}),
        ),
        TakeStoreSource(),
        RecordTreeSource(
            "recording-groups",
            frozenset({"recording-groups"}),
            (ScanRoot("provenance/recording-groups"),),
            referents=frozenset({"prompt-database"}),
        ),
        DigestNamedSource(
            "derived-edits",
            frozenset({"desktop-edit-renders", "native-edit-renders"}),
            "derived/recording-edits",
        ),
        CorpusDatabaseSource(),
    )


# -- the pass --------------------------------------------------------------------------------


def _require_offered(source: ReplicationSource, entry: SourceEntry) -> None:
    """A source may offer only what its own rules decide, as those rules say.

    Raises: ``StorageError`` (``STORAGE.PROTOCOL``) otherwise.
    """
    rule = classify(entry.path).rule
    if isinstance(entry, WithheldPath):
        agrees = True
    elif isinstance(entry, CarriedPath):
        agrees = (entry.kind == "pin") == (rule.transfer == "pin") and entry.mode == rule.mode
    else:
        # A memos file is read below its own root, at its path without the prefix; a backup is
        # read from its scratch copy; every other file is read at its own path.
        expected = memos_source_path(entry.path) if rule.disposition == "memos" else entry.path
        agrees = (
            (entry.kind == "pin") == (rule.transfer == "pin")
            and entry.mode == rule.mode
            and (entry.source == expected) == (rule.transfer != "sqlite-backup")
        )
    if rule.name not in source.rules or entry.file_class != rule.disposition or not agrees:
        raise storage_error(
            "STORAGE.PROTOCOL",
            f"source {source.name} offered {entry.path} against its classification ({rule.name})",
        )


def _transfer(
    owner: str, item: ReplicationItem, sink: ReplicationSink, chunk_bytes: int
) -> PassEntry:
    sink.begin(owner, item)
    entry: PassEntry | None = None
    try:
        if item.kind == "pin":
            sha256, size = cast("str", item.expected_sha256), cast("int", item.expected_size)
        else:
            with item.open(chunk_bytes=chunk_bytes) as reader:
                for chunk in reader.chunks():
                    sink.chunk(chunk)
            sha256, size = reader.sha256, reader.size
        entry = PassEntry(
            owner=owner,
            path=item.path,
            file_class=item.file_class,
            kind=item.kind,
            sha256=sha256,
            size=size,
            mode=item.mode,
            pin_source=item.pin_source,
        )
    finally:
        sink.finish(entry)
    return entry


def _run_source(
    layout: AssetLayout,
    source: ReplicationSource,
    sink: ReplicationSink,
    wanted: frozenset[str],
    chunk_bytes: int,
) -> OwnerResult:
    entries: list[PassEntry] = []
    withheld: list[WithheldPath] = []
    carried: list[CarriedPath] = []
    failed: tuple[ReplicationItem, SourceChangedError] | None = None
    generator = source.items(layout)
    try:
        while True:
            try:
                produced = next(generator) if failed is None else generator.throw(failed[1])
            except StopIteration:
                produced = None
            if failed is not None:
                withheld.append(WithheldPath(failed[0].path, failed[0].file_class, str(failed[1])))
                failed = None
            if produced is None:
                break
            _require_offered(source, produced)
            if produced.file_class not in wanted:
                continue
            if isinstance(produced, WithheldPath):
                withheld.append(produced)
                continue
            if isinstance(produced, CarriedPath):
                carried.append(produced)
                continue
            try:
                entries.append(_transfer(source.name, produced, sink, chunk_bytes))
            except SourceChangedError as error:
                failed = (produced, error)
    except SourceChangedError as error:
        return OwnerResult(
            owner=source.name,
            state="refused" if error.refused else "changed",
            entries=(),
            withheld=tuple(withheld),
            reason=str(error),
            problem=error.problem,
            problem_path=error.path,
            carried=tuple(carried),
        )
    finally:
        generator.close()
    return OwnerResult(
        source.name, "complete", tuple(entries), tuple(withheld), carried=tuple(carried)
    )


def _require_referrer_order(sources: Sequence[ReplicationSource]) -> None:
    """Every source runs before the owners its files name (those absent are not checked).

    Raises: ``ValueError`` for a source naming itself or an owner that runs before it.
    """
    positions = {source.name: index for index, source in enumerate(sources)}
    for index, source in enumerate(sources):
        for referent in sorted(source.referents):
            if positions.get(referent, len(sources)) <= index:
                raise ValueError(
                    f"source {source.name} names {referent}'s files, so {referent} must be "
                    "read after it"
                )


def _hold_back(
    sources: Sequence[ReplicationSource], results: Sequence[OwnerResult]
) -> tuple[OwnerResult, ...]:
    """Report ``changed`` every complete owner one of whose referents did not complete.

    Referents run after their referrers, so walking the owners backwards settles each
    referent before the owners that name it (which makes the rule transitive).
    """
    settled: dict[str, OwnerResult] = {}
    for source, result in zip(reversed(sources), reversed(results), strict=True):
        blocking = next(
            (
                settled[other.name]
                for other in sources
                if other.name in source.referents and settled[other.name].state != "complete"
            ),
            None,
        )
        if result.state == "complete" and blocking is not None:
            result = OwnerResult(
                owner=result.owner,
                state="changed",
                entries=(),
                withheld=result.withheld,
                reason=(
                    f"held back with {blocking.owner}, which did not complete and whose files "
                    "this owner's files name"
                ),
                problem="changed",
                problem_path=blocking.problem_path,
                held_back_by=blocking.owner,
                carried=result.carried,
            )
        settled[source.name] = result
    return tuple(settled[source.name] for source in sources)


def collect_pass(
    layout: AssetLayout,
    sources: Sequence[ReplicationSource],
    sink: ReplicationSink,
    *,
    classes: Collection[FileClass],
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> PassResult:
    """Read every wanted owner's files into ``sink`` without blocking any writer.

    First the whole root is classified (``require_classified_tree``); then each source whose
    rules copy a wanted class runs in order, and each item of a wanted class is streamed to
    the sink in pieces of at most ``chunk_bytes``. A source failure never stops the pass: its
    owner is reported ``changed`` or ``refused``, and then every owner whose files name that
    owner's files (directly or through other owners) is reported ``changed`` with
    ``held_back_by`` set, even though its own files were read. Referents that do not run in
    this pass (not among ``sources``, or copying no wanted class) hold nothing back.
    Params: ``layout``; ``sources``, usually ``library_sources()`` (names must be unique, and
    each source must come before the owners its ``referents`` name); ``sink``; ``classes``,
    the classes the destination takes (at least one); ``chunk_bytes`` (1 to
    ``MAX_CHUNK_BYTES``).
    Returns: the ``PassResult``.
    Raises: ``ValueError`` for no or an unknown class, a bad chunk size, repeated source
    names, a source listed after an owner its files name, or a source serving an unknown or
    Mac-only rule (before any I/O); ``SecurityError`` for an unsafe asset root;
    ``StorageError`` (``STORAGE.PROTOCOL``) for an unclassified path, or for a source offering
    a file against its classification; ``SourceChangedError`` from the classification walk
    (retryable); whatever the sink raises.
    """
    wanted = frozenset(classes)
    if not wanted or not wanted <= FILE_CLASSES:
        raise ValueError(f"a pass copies one or more known file classes, not {sorted(wanted)}")
    _require_chunk_bytes(chunk_bytes)
    names = [source.name for source in sources]
    if len(set(names)) != len(names):
        raise ValueError("replication source names must be unique")
    running = [source for source in sources if source_classes(source) & wanted]
    _require_referrer_order(sources)
    layout = AssetLayout.validate(layout.root)
    require_classified_tree(layout)
    results = [_run_source(layout, source, sink, wanted, chunk_bytes) for source in running]
    return PassResult(_hold_back(running, results))

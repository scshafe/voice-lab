"""Library generations: sealed manifests chained under a head ref, the ledger, verify, restore.

@canonical library_generations -- push_generation is the only writer of a library's vault
generations: one push at a time (provenance/vault/.push.lock), it refuses a head this Mac did
not leave (rollback, fork, undecryptable, digest mismatch, broken chain), turns a replication
pass into a ManifestPlan, names that plan in an upload-started ledger entry before its first
new blob, uploads only content the previous generation does not hold, stores the manifest
behind a sealed ManifestEnvelope, swaps the head by compare-and-set and then records the local
manifest copy, state.json and the upload-completed entry. verify_generation and
restore_library trust nothing the vault says: every chunk is fetched by the ciphertext digest
an authenticated manifest names, the manifest is proven by its envelope's digest, and every
file's plaintext digest is checked after reassembly. restore_library writes only into an empty
target, through a private sibling staging directory, and never writes provenance/vault.
Related: library_replication.py (the pass and its sources), library_classification.py (which
paths are copied), storage_contracts.py (ManifestPlan, LibraryManifest, ManifestEnvelope,
ReplicationState, LedgerEntry), storage_ports.py (BlobStore, RefStore, the STORAGE.* errors),
tests/test_library_replication.py.

This module takes ports and never builds an adapter: the composition root (storage_binding)
passes a sealed ``BlobStore`` (``storage_sealing.SealedBlobStore``), a ``RefStore``, the
``DestinationContext`` its binding and approval describe, and the sealed store's largest
plaintext (``storage_sealing.MAX_SEALED_PLAINTEXT_BYTES``) as ``chunk_bytes``; importing
``storage_sealing`` here, even lazily, would break the import boundary. Owner modules are
imported lazily (only ``finetune``, to re-link a restored run's pinned weights), so importing
this module loads no recording, catalog or adapter module.

Step 6 decisions recorded here:
- A push that finds the head exactly one version past this Mac's state, naming the generation
  whose ``upload-started`` entry is the ledger's last entry and chaining from the state's
  manifest, finishes that interrupted push (local copy, state, completed entry) instead of
  refusing it as a fork: only this Mac can seal such a manifest, and a crash between the head
  swap and ``state.json`` must not stop every later push. A state that already names the
  head's generation while the ledger's last entry is that generation's unanswered
  ``upload-started`` gets its completed entry the same way.
- The ledger is read as a whole on every append, but only its last entry is held against the
  manifest copy it names: every earlier entry is covered field by field by the next entry's
  chain digest, and re-parsing every generation's kept manifest on every read would make a
  push cost grow with the number of generations. ``state.json`` anchors the chain's end
  (``require_recorded``), so deleting entries off the tail is as visible as editing one.
- An ``upload-started`` entry counts the new file content planned (its plaintext bytes and
  pieces), because the sealed size of a piece is the sealing layer's business; an
  ``upload-completed`` entry counts the stored blobs the generation added (new file chunks,
  manifest chunks and the envelope) with their stored sizes.
- A restore given this Mac's records holds the head against ``state.json`` before it writes
  anything, with the rollback, digest and fork checks a push makes: those records are the
  only anchor a restore has, and one onto a Mac that keeps none can only trust the vault.
- A restored library is opened once as the recording service's first launch opens one
  (``RecordingTakeStore`` in write mode, inside the staging directory), which only creates
  the take store's Mac-only scaffolding: its metadata lock and its empty operation, cursor,
  analysis and prepared-gate directories. Read-only readers (``prompts coverage-report``,
  ``finetune corpus``) require that scaffolding, and a library restored for a drill is read
  by them before any app opens it. Nothing is pending in a restored tree (the journal is
  Mac-only), so that open repairs nothing.
- Refusals that are not the vault's fault are not ``StorageError``: ``PushRunningError``
  (another push holds the lock; retryable) and ``RestoreRefusedError`` (the target or the
  disk cannot take the restore).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, Literal, NamedTuple, get_args

from pydantic import BaseModel, ValidationError

from .canonical import (
    canonical_json_bytes,
    fsync_directory,
    rename_no_replace,
    require_private_metadata,
    write_canonical_json,
    write_new_private_file,
)
from .errors import SecurityError, VoiceLabError
from .library_classification import FILE_CLASSES, matching_rules
from .library_replication import (
    MAX_CHUNK_BYTES,
    PRETRAINED_PIN_SOURCE,
    OwnerResult,
    PassEntry,
    PassResult,
    ReplicationItem,
    ReplicationSource,
    ScratchDirectory,
    SourceChangedError,
    collect_pass,
    library_sources,
)
from .models import utc_now
from .paths import ASSET_DIRECTORIES, AssetLayout, validate_absolute_path, validate_relative_path
from .storage_contracts import (
    MAX_MANIFEST_BYTES,
    VAULT_PROVENANCE_ROOT,
    FileClass,
    LedgerDestination,
    LedgerEntry,
    LedgerEntryKind,
    LibraryManifest,
    ManifestEntry,
    ManifestEnvelope,
    ManifestPlan,
    PlannedManifestEntry,
    ReplicationState,
    StorageBackend,
    destination_digest,
    ledger_entry_digest,
    manifest_digest,
    manifest_path_key,
    manifest_payload_digest,
    manifest_plan,
)
from .storage_ports import (
    BlobRef,
    BlobStore,
    RefStore,
    RefValue,
    StorageError,
    library_head_ref,
    storage_error,
    validate_library_id,
)

__all__ = [
    "LEDGER_DIRECTORY",
    "MANIFEST_COPIES_DIRECTORY",
    "PUSH_LOCK_NAME",
    "READ_ONLY_TREES",
    "RESTORE_MARGIN_BYTES",
    "STATE_NAME",
    "VAULT_RECORDS_ROOT",
    "DestinationContext",
    "HeadProblem",
    "OwnerOutcome",
    "PinOutcome",
    "PushResult",
    "PushRunningError",
    "ReplicationLedger",
    "RestoreRefusedError",
    "RestoreResult",
    "StoredManifest",
    "VaultRecords",
    "VerifiedEntry",
    "VerifyReport",
    "push_generation",
    "read_stored_manifest",
    "restore_library",
    "store_manifest",
    "verify_generation",
]

VAULT_RECORDS_ROOT: Final = VAULT_PROVENANCE_ROOT
STATE_NAME: Final = "state.json"
LEDGER_DIRECTORY: Final = "ledger"
MANIFEST_COPIES_DIRECTORY: Final = "manifests"
PUSH_LOCK_NAME: Final = ".push.lock"
# state.json and ledger entries are small; anything larger is not one of them.
MAX_VAULT_RECORD_BYTES: Final = 1024 * 1024
# Ledger entries are named by an eight-digit sequence.
MAX_LEDGER_SEQUENCE: Final = 99_999_999
# A restore needs the manifest's bytes plus a tenth plus this much free space.
RESTORE_MARGIN_BYTES: Final = 256 * 1024 * 1024
RESTORE_MARGIN_DIVISOR: Final = 10
RESTORING_INFIX: Final = ".restoring-"
# The trees whose writers publish read-only (files 0400, directories below the tree 0500);
# the tree directory itself stays writable so its writer can publish the next one.
READ_ONLY_TREES: Final = (
    "raw",
    "references",
    "derived/datasets",
    "derived/transfer-plans",
    "provenance/pilot-gates",
)

_LEDGER_FILE: Final = re.compile(r"([0-9]{8})\.json")
# canonical.write_new_private_file / write_canonical_json temporaries a crash can leave.
_LEFTOVER: Final = re.compile(r"\..+\.tmp-[0-9a-f]{16}")
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_BYTES: Final = 1024 * 1024
_SHA256: Final = re.compile("[0-9a-f]{64}")

type HeadProblem = Literal[
    "missing",
    "undecryptable",
    "not-an-envelope",
    "foreign-library",
    "digest-mismatch",
    "not-a-manifest",
    "rollback",
    "fork",
    "chain",
]
type PushOutcome = Literal["pushed", "up-to-date", "source-changed", "dry-run"]
type OwnerState = Literal["complete", "changed", "refused"]
type VerifyState = Literal["present", "mismatch", "missing", "pinned"]
type PinState = Literal["linked", "copied", "missing"]
type RestoreProblem = Literal[
    "unsafe-target", "target-not-empty", "insufficient-space", "unrestorable-path"
]
type Clock = Callable[[], datetime]
_RESTORE_PROBLEMS: Final = frozenset(get_args(RestoreProblem.__value__))


class _ContentKey(NamedTuple):
    sha256: str
    size: int


# -- errors ----------------------------------------------------------------------------------


def _integrity(message: str) -> StorageError:
    return storage_error("STORAGE.INTEGRITY", message)


def _head_failure(problem: HeadProblem, detail: str) -> StorageError:
    """The refusal of a head, a manifest or a chain: ``STORAGE.INTEGRITY`` naming the check."""
    return _integrity(f"vault head check failed ({problem}): {detail}")


class PushRunningError(VoiceLabError):
    """Another push of this library holds ``provenance/vault/.push.lock``; try again later.

    ``code`` is ``REPLICATION.PUSH_RUNNING`` and ``retryable`` is ``True``. It is not a
    ``StorageError``: the vault did nothing wrong. Instances pickle with their message.
    """

    code: Final = "REPLICATION.PUSH_RUNNING"
    retryable: Final = True


class RestoreRefusedError(VoiceLabError):
    """A restore was refused before anything was published at its target.

    ``code`` is ``REPLICATION.RESTORE_REFUSED``; ``problem`` says why: ``unsafe-target``
    (the target is not a directory, not this user's, too broad, or its parent is missing or
    reached through a symbolic link), ``target-not-empty`` (the target holds something, or
    something appeared there during the restore), ``insufficient-space`` (the target's
    filesystem has less free space than the manifest's bytes plus a tenth plus
    ``RESTORE_MARGIN_BYTES``) or ``unrestorable-path`` (the manifest names a path a restore
    never writes, such as anything under ``provenance/vault``). ``str(error)`` is the message;
    instances pickle with every field intact.
    Raises (on construction): ``ValueError`` for an unknown problem.
    """

    code: Final = "REPLICATION.RESTORE_REFUSED"

    def __init__(self, problem: RestoreProblem, message: str) -> None:
        if problem not in _RESTORE_PROBLEMS:
            raise ValueError(f"unknown restore problem: {problem!r}")
        self.problem: RestoreProblem = problem
        super().__init__(message)

    def __reduce__(self) -> tuple[type[RestoreRefusedError], tuple[RestoreProblem, str]]:
        return (type(self), (self.problem, str(self)))


# -- the destination -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DestinationContext:
    """Where a push sends a library, as its ledger entries record it.

    ``backend``, ``endpoint`` and ``namespace`` come from the binding; a ``tailnet-vault``
    destination also names the approved node (``node_stable_id``, ``tag``) and the digest of
    the approval the upload relies on (``approval_sha256``); a ``local-directory`` destination
    names none of the three.
    Raises (on construction): ``pydantic.ValidationError`` when the fields break a
    ``LedgerDestination`` rule; ``ValueError`` when ``approval_sha256`` is missing for a
    tailnet destination, present for a local one, or not 64 lowercase hex characters.
    """

    backend: StorageBackend
    endpoint: str
    namespace: str
    node_stable_id: str | None = None
    tag: str | None = None
    approval_sha256: str | None = None

    def __post_init__(self) -> None:
        tailnet = self.ledger_destination.backend == "tailnet-vault"
        if (self.approval_sha256 is not None) != tailnet:
            raise ValueError(
                "approval_sha256 is required for tailnet-vault and forbidden for local-directory"
            )
        if self.approval_sha256 is not None and _SHA256.fullmatch(self.approval_sha256) is None:
            raise ValueError("approval_sha256 is 64 lowercase hex characters")

    @property
    def ledger_destination(self) -> LedgerDestination:
        """The destination as a ledger entry names it."""
        return LedgerDestination(
            backend=self.backend,
            endpoint=self.endpoint,
            namespace=self.namespace,
            node_stable_id=self.node_stable_id,
            tag=self.tag,
        )

    @property
    def digest(self) -> str:
        """The ``destination_digest`` ``state.json`` records for this destination."""
        return destination_digest(self.ledger_destination)


# -- Mac-side records ------------------------------------------------------------------------


def _read_private_file(path: Path, limit: int) -> bytes | None:
    """The bytes of a private regular file with one link, or ``None`` when it is absent.

    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the file is a link, not a
    regular file, not private to this user, larger than ``limit`` or changes size while read.
    """
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _integrity(f"{path} cannot be opened safely: {error.strerror}") from error
    try:
        metadata = os.fstat(descriptor)
        try:
            require_private_metadata(metadata, str(path))
        except SecurityError as error:
            raise _integrity(f"{path} is not a private regular file with one link") from error
        if metadata.st_size > limit:
            raise _integrity(f"{path} is larger than the {limit}-byte limit for its kind")
        os.set_blocking(descriptor, True)
        pieces: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining > 0 and (piece := os.read(descriptor, min(remaining, _READ_BYTES))):
            pieces.append(piece)
            remaining -= len(piece)
        data = b"".join(pieces)
        if len(data) != metadata.st_size:
            raise _integrity(f"{path} changed size while it was read")
        return data
    finally:
        os.close(descriptor)


def _parse_record[ModelT: BaseModel](model: type[ModelT], data: bytes, label: str) -> ModelT:
    """A Mac-side record: a valid ``model`` whose file is its canonical JSON plus a newline.

    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) otherwise.
    """
    try:
        value = model.model_validate_json(data)
    except ValidationError as error:
        raise _integrity(
            f"{label} is not a valid {model.__name__} ({error.error_count()} problem(s))"
        ) from error
    if canonical_json_bytes(value) + b"\n" != data:
        raise _integrity(f"{label} is not in canonical form")
    return value


class VaultRecords:
    """This Mac's records of its vault copies, under ``provenance/vault`` (never copied).

    ``state.json`` (the newest generation this Mac pushed; replaced atomically, 0600),
    ``manifests/<generation:08d>.json`` (the plaintext manifest of each pushed generation;
    write-once, 0600), ``ledger/`` (``ReplicationLedger``) and ``.push.lock`` (held by one push
    at a time; no lab writer takes it). Every directory and file must be a private,
    non-linked entry of this user: an unsafe one is refused, never repaired.
    """

    def __init__(self, layout: AssetLayout) -> None:
        self.layout = layout
        self.root = layout.root / VAULT_RECORDS_ROOT

    def _walk(self, name: str | None, *, create: bool) -> Path:
        relative = VAULT_RECORDS_ROOT if name is None else f"{VAULT_RECORDS_ROOT}/{name}"
        path = self.layout.root
        for part in PurePosixPath(relative).parts:
            path = path / part
            try:
                metadata = os.lstat(path)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(path, 0o700)
                os.chmod(path, 0o700)
                fsync_directory(path.parent)
                metadata = os.lstat(path)
            try:
                require_private_metadata(metadata, str(path), directory=True)
            except SecurityError as error:
                raise _integrity(f"{path} is not a private directory of this user") from error
        return path

    def directory(self, name: str | None = None) -> Path | None:
        """``provenance/vault`` (or its child ``name``), or ``None`` when it is missing.

        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when it, or a directory above
        it below the asset root, is a link, not a directory or not private to this user.
        """
        try:
            return self._walk(name, create=False)
        except FileNotFoundError:
            return None

    def make_directory(self, name: str | None = None) -> Path:
        """``provenance/vault`` (or its child ``name``), made at 0700 where missing.

        Raises: as ``directory``; ``OSError`` when a directory cannot be made.
        """
        return self._walk(name, create=True)

    def read_state(self, library_id: str) -> ReplicationState | None:
        """The replication state, or ``None`` when this Mac has pushed nothing.

        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) for an unsafe, invalid or
        non-canonical ``state.json``, or one that belongs to another library.
        """
        directory = self.directory()
        if directory is None:
            return None
        data = _read_private_file(directory / STATE_NAME, MAX_VAULT_RECORD_BYTES)
        if data is None:
            return None
        state = _parse_record(ReplicationState, data, f"{directory / STATE_NAME}")
        if state.library_id != library_id:
            raise _integrity(
                f"{directory / STATE_NAME} records library {state.library_id}, not {library_id}"
            )
        return state

    def write_state(self, state: ReplicationState) -> None:
        """Replace ``state.json`` atomically (0600).

        Raises: ``StorageIntegrityError`` as ``directory``; ``OSError`` from the write.
        """
        write_canonical_json(self.make_directory() / STATE_NAME, state, mode=0o600)

    def manifest_copy_path(self, generation: int) -> Path:
        """Where the local copy of ``generation``'s manifest is kept."""
        return self.root / MANIFEST_COPIES_DIRECTORY / f"{generation:08d}.json"

    def read_manifest_copy(self, generation: int) -> LibraryManifest | None:
        """The local copy of a generation's manifest, or ``None`` when none is kept.

        The caller compares its ``manifest_digest`` with the digest it expects.
        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) for an unsafe, invalid or
        non-canonical copy, or one of another generation.
        """
        if generation < 1 or self.directory(MANIFEST_COPIES_DIRECTORY) is None:
            return None
        path = self.manifest_copy_path(generation)
        data = _read_private_file(path, MAX_MANIFEST_BYTES + 1)
        if data is None:
            return None
        manifest = _parse_record(LibraryManifest, data, str(path))
        if manifest.generation != generation:
            raise _integrity(f"{path} holds generation {manifest.generation}")
        return manifest

    def write_manifest_copy(self, manifest: LibraryManifest) -> None:
        """Keep the plaintext manifest of a pushed generation (write-once, 0600).

        An identical copy already kept is left as it is.
        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when a different copy of that
        generation is kept (move ``provenance/vault/manifests`` aside only after resetting
        the replication state), or as ``directory``; ``OSError`` from the write.
        """
        data = canonical_json_bytes(manifest) + b"\n"
        self.make_directory(MANIFEST_COPIES_DIRECTORY)
        path = self.manifest_copy_path(manifest.generation)
        existing = _read_private_file(path, MAX_MANIFEST_BYTES + 1)
        if existing is None:
            write_new_private_file(path, data, mode=0o600)
        elif existing != data:
            raise _integrity(
                f"{path} already keeps a different manifest of generation {manifest.generation}"
            )

    @contextmanager
    def push_lock(self) -> Iterator[None]:
        """Hold ``provenance/vault/.push.lock`` without waiting.

        Raises: ``PushRunningError`` when another push (in any process, or another holder
        in this one) holds it; ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) for an unsafe
        lock file or directory.
        """
        path = self.make_directory() / PUSH_LOCK_NAME
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as error:
            raise _integrity(f"{path} cannot be opened safely: {error.strerror}") from error
        try:
            try:
                require_private_metadata(os.fstat(descriptor), str(path))
            except SecurityError as error:
                raise _integrity(f"{path} is not a private regular file with one link") from error
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise PushRunningError(
                    f"a push of this library is already running ({path} is held); try again "
                    "once it ends"
                ) from error
            yield
        finally:
            os.close(descriptor)


# -- the ledger ------------------------------------------------------------------------------


def _pairing(entry: LedgerEntry) -> tuple[object, ...]:
    return (
        entry.library_id,
        entry.generation,
        entry.approval_sha256,
        entry.destination,
        entry.payload_sha256,
        entry.classes,
    )


class ReplicationLedger:
    """The append-only record of every upload, under ``provenance/vault/ledger``.

    Entries are ``<sequence:08d>.json`` files (canonical JSON plus a newline, write-once,
    0600). Reading checks the whole chain: contiguous sequences from 1, each entry chained to
    the digest of the one before it (``ledger_entry_digest``), and every ``upload-completed``
    entry directly after the ``upload-started`` entry it completes (same library, generation,
    approval, destination, payload and classes). Every field of every entry but the last is
    covered by the next entry's chain digest; the last entry is not, so it is also checked
    against the manifest copy it names (same manifest, and its files those the upload
    started with), and ``require_recorded`` proves that the ledger still records the upload
    ``state.json`` names, which is what makes a truncated tail visible. Only
    ``canonical.write_new_private_file`` temporaries may sit beside the entries.
    """

    def __init__(self, layout: AssetLayout) -> None:
        self.records = VaultRecords(layout)

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Every entry, in order, once the chain is proven.

        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) naming the first break: an
        unexpected file, a missing sequence, an unsafe, invalid or non-canonical entry, a
        broken chain, an unpaired completion, or a last entry that disagrees with the
        manifest copy of its generation.
        """
        directory = self.records.directory(LEDGER_DIRECTORY)
        if directory is None:
            return ()
        names: list[tuple[int, str]] = []
        for name in sorted(os.listdir(directory)):
            match = _LEDGER_FILE.fullmatch(name)
            if match is not None:
                names.append((int(match[1]), name))
            elif _LEFTOVER.fullmatch(name) is None:
                raise _integrity(f"the ledger holds {name}, which is not a ledger entry")
        entries: list[LedgerEntry] = []
        for position, (sequence, name) in enumerate(names, start=1):
            if sequence != position:
                raise _integrity(f"ledger entry {position:08d} is missing")
            data = _read_private_file(directory / name, MAX_VAULT_RECORD_BYTES)
            if data is None:
                raise _integrity(f"ledger entry {name} vanished while the ledger was read")
            entry = _parse_record(LedgerEntry, data, f"ledger entry {name}")
            if entry.sequence != sequence:
                raise _integrity(f"ledger entry {name} says it is entry {entry.sequence}")
            self._require_linked(entries[-1] if entries else None, entry)
            entries.append(entry)
        if entries:
            self._require_kept_manifest(entries[-1])
        return tuple(entries)

    def _require_linked(self, previous: LedgerEntry | None, entry: LedgerEntry) -> None:
        expected = None if previous is None else ledger_entry_digest(previous)
        if entry.previous_entry_sha256 != expected:
            raise _integrity(
                f"ledger entry {entry.sequence:08d} does not chain to the entry before it"
            )
        if entry.kind != "upload-completed":
            return
        if (
            previous is None
            or previous.kind != "upload-started"
            or _pairing(previous) != _pairing(entry)
        ):
            raise _integrity(
                f"ledger entry {entry.sequence:08d} completes an upload the entry before it did "
                "not start"
            )

    def _require_kept_manifest(self, entry: LedgerEntry) -> None:
        """Hold the chain's last entry against the manifest copy of its generation.

        No later entry's chain digest covers the last entry, so it is the one entry whose
        fields an editor could change unnoticed; where the Mac keeps the generation's
        manifest (write-once), that copy decides.
        """
        copy = self.records.read_manifest_copy(entry.generation)
        if copy is None:
            return
        if entry.manifest_sha256 is not None and manifest_digest(copy) != entry.manifest_sha256:
            raise _integrity(
                f"ledger entry {entry.sequence:08d} names another manifest of generation "
                f"{entry.generation} than the one this Mac keeps"
            )
        if manifest_payload_digest(copy) != entry.payload_sha256:
            raise _integrity(
                f"ledger entry {entry.sequence:08d} names a manifest whose files are not the "
                "ones its upload started with"
            )

    def _require_named_tail(self, state: ReplicationState, entry: LedgerEntry) -> None:
        """Hold the entry that records ``state``'s generation against the digest it names.

        ``state.ledger_tail_sha256`` is that entry's own digest, or — in the window between
        writing an ``upload-completed`` entry and recording it in ``state.json``, and while a
        completion is still pending — the digest of the ``upload-started`` entry it pairs
        with, which is exactly its ``previous_entry_sha256``. Nothing else is accepted, so
        editing the one entry no chain digest covers is visible.
        """
        accepted = {ledger_entry_digest(entry)}
        if entry.kind == "upload-completed" and entry.previous_entry_sha256 is not None:
            accepted.add(entry.previous_entry_sha256)
        if state.ledger_tail_sha256 not in accepted:
            raise _integrity(
                f"ledger entry {entry.sequence:08d} records the generation {state.generation} "
                f"upload {STATE_NAME} names, but {STATE_NAME} names ledger entry "
                f"{state.ledger_tail_sha256}, which it is not; the entry was changed"
            )

    def require_recorded(self, state: ReplicationState | None) -> tuple[LedgerEntry, ...]:
        """Every entry, once the chain and the upload ``state.json`` names are proven.

        Deleting entries off the end of the chain leaves a chain that still proves itself, so
        the state is the ledger's anchor: the generation it names must be the one the last
        ``upload-completed`` entry recorded (same manifest and head version), unless the
        entries after it are the unanswered ``upload-started`` entries of later attempts, or
        the state's own generation is still waiting for its completion (a push that stopped
        between ``state.json`` and its last entry). That entry must also be the one
        ``state.ledger_tail_sha256`` names (``_require_named_tail``).
        Params: ``state``, the replication state read for this library (``None`` when this
        Mac has pushed nothing).
        Returns: the entries, as ``entries``.
        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) as ``entries``, when no
        entry records the state's generation, or when the entry that does is not the one the
        state names.
        """
        entries = self.entries()
        if state is None or state.head is None:
            return entries
        for entry in reversed(entries):
            if entry.kind == "upload-completed":
                if (entry.generation, entry.manifest_sha256, entry.head_version) == (
                    state.generation,
                    state.manifest_sha256,
                    state.head.version,
                ):
                    self._require_named_tail(state, entry)
                    return entries
                break
            if entry.generation == state.generation:
                # The completion this Mac is about to write for its own started upload.
                self._require_named_tail(state, entry)
                return entries
        raise _integrity(
            f"the ledger does not record the generation {state.generation} upload "
            f"{STATE_NAME} names; entries are missing"
        )

    def append(
        self,
        *,
        kind: LedgerEntryKind,
        library_id: str,
        generation: int,
        destination: DestinationContext,
        payload_sha256: str,
        manifest_sha256: str | None,
        classes: tuple[FileClass, ...],
        byte_count: int,
        blob_count: int,
        head_version: int | None,
        recorded_at: datetime,
    ) -> LedgerEntry:
        """Append one entry after proving the chain it extends.

        Params: the entry's fields (see ``LedgerEntry``); the sequence and the previous
        entry's digest are the ledger's, and the approval digest is the destination's.
        Returns: the entry written.
        Raises: ``StorageIntegrityError`` as ``require_recorded`` (nothing is appended to a
        broken chain, or to one that no longer records what ``state.json`` names);
        ``StorageError`` (``STORAGE.QUOTA``) past ``MAX_LEDGER_SEQUENCE`` entries;
        ``pydantic.ValidationError`` for fields that break a ``LedgerEntry`` rule;
        ``OSError`` from the write (``FileExistsError`` when another writer appended first).
        """
        entries = self.require_recorded(self.records.read_state(library_id))
        sequence = len(entries) + 1
        if sequence > MAX_LEDGER_SEQUENCE:
            raise storage_error(
                "STORAGE.QUOTA", f"the ledger is full ({MAX_LEDGER_SEQUENCE} entries)"
            )
        entry = LedgerEntry(
            sequence=sequence,
            previous_entry_sha256=ledger_entry_digest(entries[-1]) if entries else None,
            kind=kind,
            library_id=library_id,
            generation=generation,
            approval_sha256=destination.approval_sha256,
            destination=destination.ledger_destination,
            payload_sha256=payload_sha256,
            manifest_sha256=manifest_sha256,
            classes=classes,
            byte_count=byte_count,
            blob_count=blob_count,
            head_version=head_version,
            recorded_at=recorded_at,
        )
        write_new_private_file(
            self.records.make_directory(LEDGER_DIRECTORY) / f"{sequence:08d}.json",
            canonical_json_bytes(entry) + b"\n",
        )
        return entry


# -- stored manifests ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredManifest:
    """A generation as the vault holds it: the envelope blob, the envelope and its manifest."""

    ref: BlobRef
    envelope: ManifestEnvelope
    manifest: LibraryManifest

    @property
    def digest(self) -> str:
        """The manifest's ``manifest_digest`` (the envelope's proven ``manifest_sha256``)."""
        return self.envelope.manifest_sha256


def store_manifest(
    blob_store: BlobStore, manifest: LibraryManifest, *, chunk_bytes: int
) -> StoredManifest:
    """Store a manifest as chunks of its canonical JSON plus one envelope blob naming them.

    Params: ``blob_store``, the library's sealed store; ``manifest``; ``chunk_bytes``, the
    largest piece put (1 to the store's largest put).
    Returns: the stored manifest; its ``ref`` is what the head must name.
    Raises: ``ValueError`` for a chunk size below 1 and ``StorageError``
    (``STORAGE.TOO_LARGE``) for a manifest over ``MAX_MANIFEST_BYTES`` (both before anything
    is stored), or whatever ``put`` raises.
    """
    if chunk_bytes < 1:
        raise ValueError(f"a manifest chunk is at least 1 byte, not {chunk_bytes}")
    data = canonical_json_bytes(manifest)
    if len(data) > MAX_MANIFEST_BYTES:
        raise storage_error(
            "STORAGE.TOO_LARGE",
            f"generation {manifest.generation}'s manifest is {len(data)} bytes, over the "
            f"{MAX_MANIFEST_BYTES}-byte limit",
        )
    chunks = tuple(
        blob_store.put(data[offset : offset + chunk_bytes])
        for offset in range(0, len(data), chunk_bytes)
    )
    envelope = ManifestEnvelope(
        library_id=manifest.library_id,
        generation=manifest.generation,
        manifest_sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        chunks=chunks,
    )
    return StoredManifest(blob_store.put(canonical_json_bytes(envelope)), envelope, manifest)


def _get_stored(blob_store: BlobStore, ref: BlobRef, label: str) -> bytes:
    try:
        return blob_store.get(ref)
    except StorageError as error:
        if error.code == "STORAGE.NOT_FOUND":
            raise _head_failure("missing", f"{label} {ref.sha256} is not in the vault") from error
        if error.code == "STORAGE.INTEGRITY":
            raise _head_failure("undecryptable", f"{label} {ref.sha256}: {error}") from error
        raise


def _parse_stored[ModelT: BaseModel](
    model: type[ModelT], data: bytes, problem: HeadProblem, label: str
) -> ModelT:
    try:
        value = model.model_validate_json(data)
    except ValidationError as error:
        raise _head_failure(
            problem, f"{label} is not a valid {model.__name__} ({error.error_count()} problem(s))"
        ) from error
    if canonical_json_bytes(value) != data:
        raise _head_failure(problem, f"{label} is not in canonical form")
    return value


def read_stored_manifest(
    blob_store: BlobStore, target: BlobRef, *, library_id: str
) -> StoredManifest:
    """Fetch and prove the manifest an envelope blob names.

    The envelope blob and every manifest chunk are fetched by the ciphertext refs the head and
    the (authenticated) envelope name, so the store checks each digest and the sealed store
    authenticates each blob; the reassembled bytes must have the envelope's size and digest
    and be the canonical JSON of a valid ``LibraryManifest`` of the envelope's library and
    generation.
    Params: ``blob_store``, the library's sealed store; ``target``, the envelope's ref (a head
    ref's target); ``library_id``, the library the manifest must belong to.
    Returns: the ``StoredManifest``.
    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) whose message names the failed
    check: ``(missing)``, ``(undecryptable)``, ``(not-an-envelope)``, ``(foreign-library)``,
    ``(digest-mismatch)`` or ``(not-a-manifest)``; any other ``StorageError`` of the store
    (``STORAGE.UNAVAILABLE``, for example) unchanged.
    """
    data = _get_stored(blob_store, target, "the manifest envelope")
    envelope = _parse_stored(ManifestEnvelope, data, "not-an-envelope", "the head's blob")
    if envelope.library_id != library_id:
        raise _head_failure(
            "foreign-library",
            f"the envelope is for library {envelope.library_id}, not {library_id}",
        )
    digest = hashlib.sha256()
    pieces: list[bytes] = []
    size = 0
    for index, ref in enumerate(envelope.chunks, start=1):
        piece = _get_stored(blob_store, ref, f"manifest chunk {index}")
        size += len(piece)
        if size > envelope.size:
            raise _head_failure(
                "digest-mismatch", f"the manifest's chunks hold more than {envelope.size} bytes"
            )
        digest.update(piece)
        pieces.append(piece)
    if size != envelope.size or digest.hexdigest() != envelope.manifest_sha256:
        raise _head_failure(
            "digest-mismatch",
            f"the manifest's chunks do not reassemble into manifest {envelope.manifest_sha256}",
        )
    manifest = _parse_stored(LibraryManifest, b"".join(pieces), "not-a-manifest", "the manifest")
    if (manifest.library_id, manifest.generation) != (envelope.library_id, envelope.generation):
        raise _head_failure(
            "not-a-manifest",
            f"the manifest is generation {manifest.generation} of {manifest.library_id}, not the "
            f"generation {envelope.generation} its envelope announces",
        )
    return StoredManifest(target, envelope, manifest)


def _entry_plaintext(blob_store: BlobStore, entry: ManifestEntry) -> Iterator[bytes]:
    """A file's bytes, chunk by chunk; the last step proves they are the entry's file.

    Raises: ``StorageError`` (``STORAGE.NOT_FOUND``) for a missing chunk and
    ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) for a chunk that fails its digest or does
    not open, or chunks that do not reassemble into the entry's size and SHA-256 (each names
    the entry's path); any other store error unchanged.
    """
    digest = hashlib.sha256()
    size = 0
    for index, ref in enumerate(entry.chunks, start=1):
        try:
            piece = blob_store.get(ref)
        except StorageError as error:
            if error.code not in ("STORAGE.NOT_FOUND", "STORAGE.INTEGRITY"):
                raise
            raise storage_error(
                error.code,
                f"{entry.path}: chunk {index} of {len(entry.chunks)} ({ref.sha256}): {error}",
            ) from error
        size += len(piece)
        if size > entry.size:
            raise _integrity(f"{entry.path}: its chunks hold more than its {entry.size} bytes")
        digest.update(piece)
        yield piece
    if size != entry.size or digest.hexdigest() != entry.sha256:
        raise _integrity(
            f"{entry.path}: its chunks do not reassemble into the {entry.size}-byte file "
            f"{entry.sha256} its entry names"
        )


# -- head resolution -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Vault:
    records: VaultRecords
    ledger: ReplicationLedger
    blob_store: BlobStore
    ref_store: RefStore
    library_id: str

    @property
    def head_name(self) -> str:
        return library_head_ref(self.library_id)


@dataclass(frozen=True, slots=True)
class _Finish:
    """What a caller that may write (a push) lends head resolution to finish a push."""

    destination: DestinationContext
    clock: Clock


@dataclass(frozen=True, slots=True)
class _Resolved:
    state: ReplicationState | None
    stored: StoredManifest | None
    adopted: int | None

    @property
    def generation(self) -> int:
        return 0 if self.state is None else self.state.generation


class _Pushed(NamedTuple):
    generation: int
    manifest_sha256: str | None
    head: RefValue | None


def _pushed(state: ReplicationState | None) -> _Pushed:
    if state is None:
        return _Pushed(0, None, None)
    return _Pushed(state.generation, state.manifest_sha256, state.head)


def _sent(stored: StoredManifest, previous: LibraryManifest | None) -> tuple[int, int]:
    """The stored blobs a generation added and their bytes (all chunks without ``previous``)."""
    kept: set[BlobRef] = set()
    if previous is not None:
        kept = {ref for entry in previous.entries for ref in entry.chunks}
    added = {ref for entry in stored.manifest.entries for ref in entry.chunks} - kept
    blobs = [*added, *stored.envelope.chunks, stored.ref]
    return len(blobs), sum(ref.size for ref in blobs)


def _started_this(
    entry: LedgerEntry | None, vault: _Vault, manifest: LibraryManifest, finish: _Finish
) -> bool:
    """Whether ``entry`` is the unanswered start of the push that stored ``manifest``."""
    return (
        entry is not None
        and entry.kind == "upload-started"
        and entry.library_id == vault.library_id
        and entry.generation == manifest.generation
        and entry.destination == finish.destination.ledger_destination
        and entry.approval_sha256 == finish.destination.approval_sha256
        and entry.payload_sha256 == manifest_payload_digest(manifest)
    )


def _kept_copy(
    records: VaultRecords, generation: int, digest: str | None
) -> LibraryManifest | None:
    copy = records.read_manifest_copy(generation)
    return copy if copy is not None and manifest_digest(copy) == digest else None


def _complete(
    vault: _Vault,
    finish: _Finish,
    stored: StoredManifest,
    head: RefValue,
    previous: LibraryManifest | None,
) -> LedgerEntry:
    blobs, size = _sent(stored, previous)
    manifest = stored.manifest
    return vault.ledger.append(
        kind="upload-completed",
        library_id=vault.library_id,
        generation=manifest.generation,
        destination=finish.destination,
        payload_sha256=manifest_payload_digest(manifest),
        manifest_sha256=stored.digest,
        classes=manifest.classes,
        byte_count=size,
        blob_count=blobs,
        head_version=head.version,
        recorded_at=finish.clock(),
    )


def _state(
    vault: _Vault, finish: _Finish, stored: StoredManifest, head: RefValue, tail: str
) -> ReplicationState:
    return ReplicationState(
        library_id=vault.library_id,
        generation=stored.manifest.generation,
        manifest_sha256=stored.digest,
        head=head,
        destination_sha256=finish.destination.digest,
        ledger_tail_sha256=tail,
        updated_at=finish.clock(),
    )


def _finish(
    vault: _Vault,
    finish: _Finish,
    stored: StoredManifest,
    head: RefValue,
    previous: LibraryManifest | None,
    started: LedgerEntry,
) -> ReplicationState:
    """Record a generation the head now names: local copy, state, completed entry.

    ``state.json`` is written twice: first naming the ``upload-started`` entry (so a crash
    before the completion still leaves a state the next push can settle), then naming the
    ``upload-completed`` entry once it is on disk. A crash between the two leaves a state
    naming the started entry, which ``ReplicationLedger.require_recorded`` accepts because
    that digest is the completed entry's ``previous_entry_sha256``.
    """
    vault.records.write_manifest_copy(stored.manifest)
    vault.records.write_state(_state(vault, finish, stored, head, ledger_entry_digest(started)))
    completed = _complete(vault, finish, stored, head, previous)
    settled = _state(vault, finish, stored, head, ledger_entry_digest(completed))
    vault.records.write_state(settled)
    return settled


def _adopt(
    vault: _Vault, state: ReplicationState | None, head: RefValue, finish: _Finish | None
) -> _Resolved:
    """Finish this Mac's own interrupted push, or refuse a head it did not leave (a fork)."""
    generation, digest, expected = _pushed(state)
    version = 0 if expected is None else expected.version
    detail = (
        f"the vault's head is at version {head.version}, but this Mac last left it at version "
        f"{version}"
    )
    if finish is None:
        raise _head_failure(
            "fork", f"{detail}; if a push from this Mac was interrupted, push again to finish it"
        )
    if head.version != version + 1:
        raise _head_failure("fork", detail)
    stored = read_stored_manifest(vault.blob_store, head.target, library_id=vault.library_id)
    manifest = stored.manifest
    entries = vault.ledger.require_recorded(state)
    started = entries[-1] if entries else None
    if (
        manifest.generation != generation + 1
        or manifest.previous_manifest_sha256 != digest
        or started is None
        or not _started_this(started, vault, manifest, finish)
    ):
        raise _head_failure(
            "fork", f"{detail}, and the head names a generation this Mac did not start"
        )
    previous = None if digest is None else _kept_copy(vault.records, generation, digest)
    adopted = _finish(vault, finish, stored, head, previous, started)
    return _Resolved(adopted, stored, manifest.generation)


def _settle(
    vault: _Vault,
    finish: _Finish,
    stored: StoredManifest,
    head: RefValue,
    entries: tuple[LedgerEntry, ...],
    state: ReplicationState | None,
) -> ReplicationState | None:
    """Complete the ledger when the state already names the generation its last entry started."""
    if not _started_this(entries[-1] if entries else None, vault, stored.manifest, finish):
        return state
    manifest = stored.manifest
    previous = _kept_copy(vault.records, manifest.generation - 1, manifest.previous_manifest_sha256)
    completed = _complete(vault, finish, stored, head, previous)
    settled = _state(vault, finish, stored, head, ledger_entry_digest(completed))
    vault.records.write_state(settled)
    return settled


def _resolve_head(
    vault: _Vault, finish: _Finish | None, destination: DestinationContext | None = None
) -> _Resolved:
    """Prove that the vault's head is the one this Mac left (see ``push_generation``).

    ``destination`` is checked whether or not this pass may write (``finish``), so a dry run
    refuses exactly where the push it previews would.
    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) naming the failed check.
    """
    state = vault.records.read_state(vault.library_id)
    if (
        state is not None
        and destination is not None
        and state.destination_sha256 != destination.digest
    ):
        raise _integrity(
            f"{STATE_NAME} records copies of library {vault.library_id} to another destination "
            f"than the binding names ({destination.backend} "
            f"{destination.endpoint} namespace {destination.namespace}); rebind "
            "with --reset-state to start this library's copy there from generation 1"
        )
    generation, digest, expected = _pushed(state)
    head = vault.ref_store.read(vault.head_name)
    following = vault.records.read_manifest_copy(generation + 1)
    if head is None:
        if expected is not None:
            raise _head_failure(
                "rollback",
                f"the vault has no head for library {vault.library_id}, but this Mac pushed "
                f"generation {generation}",
            )
        if following is not None:
            raise _head_failure(
                "rollback", "this Mac keeps generation 1's manifest, but the vault has no head"
            )
        return _Resolved(state, None, None)
    if expected is None or head.version > expected.version:
        return _adopt(vault, state, head, finish)
    if head.version < expected.version:
        raise _head_failure(
            "rollback",
            f"the vault's head is at version {head.version}, older than version "
            f"{expected.version}, which this Mac pushed",
        )
    if head.target != expected.target:
        raise _head_failure(
            "fork",
            f"the vault's head at version {head.version} names another manifest than the one "
            "this Mac pushed",
        )
    stored = read_stored_manifest(vault.blob_store, head.target, library_id=vault.library_id)
    if stored.digest != digest or stored.manifest.generation != generation:
        raise _head_failure(
            "digest-mismatch",
            f"the head names generation {stored.manifest.generation} ({stored.digest}), but this "
            f"Mac pushed generation {generation} ({digest})",
        )
    if following is not None:
        raise _head_failure(
            "rollback",
            f"this Mac keeps generation {generation + 1}'s manifest, but the vault's head is "
            f"still at generation {generation}",
        )
    earlier = vault.records.read_manifest_copy(generation - 1)
    if earlier is not None and manifest_digest(earlier) != stored.manifest.previous_manifest_sha256:
        raise _head_failure(
            "chain",
            f"generation {generation} does not follow the generation {generation - 1} this Mac "
            "keeps",
        )
    entries = vault.ledger.require_recorded(state)
    if finish is not None:
        state = _settle(vault, finish, stored, head, entries, state)
    return _Resolved(state, stored, None)


def _named_generation(
    records: VaultRecords | None, stored: StoredManifest, generation: int
) -> LibraryManifest:
    """``generation``'s manifest: the head's, or a local copy the chain from the head proves.

    Raises: ``StorageError`` (``STORAGE.NOT_FOUND``) for a generation the vault's head does
    not reach or whose manifest this Mac does not keep; ``StorageIntegrityError``
    (``STORAGE.INTEGRITY``, ``(chain)``) when a kept copy is not the one the chain names.
    """
    head = stored.manifest
    if generation == head.generation:
        return head
    if not 1 <= generation < head.generation:
        raise storage_error(
            "STORAGE.NOT_FOUND",
            f"generation {generation} is not in the vault, whose head is generation "
            f"{head.generation}",
        )
    current = head
    for step in range(head.generation - 1, generation - 1, -1):
        copy = None if records is None else records.read_manifest_copy(step)
        if copy is None:
            raise storage_error(
                "STORAGE.NOT_FOUND",
                f"generation {step}'s manifest is not kept on this Mac, so generation "
                f"{generation} cannot be reached from the vault's head",
            )
        if manifest_digest(copy) != current.previous_manifest_sha256:
            raise _head_failure(
                "chain",
                f"the generation {step} manifest this Mac keeps is not the one generation "
                f"{step + 1} follows",
            )
        current = copy
    return current


# -- pushing ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnerOutcome:
    """What one owner contributed to a push.

    ``complete``: the pass's view of the owner is in the generation, and ``carried`` counts
    the files it withheld that the previous generation's entries still stand for.
    ``changed`` (retryable) or ``refused`` (its files are unsafe, inconsistent or cannot be
    restored): the generation keeps the owner's ``carried`` previous entries, and ``reason``,
    ``problem_path`` and ``held_back_by`` say why (as ``OwnerResult``).
    """

    owner: str
    state: OwnerState
    carried: int
    reason: str | None = None
    problem_path: str | None = None
    held_back_by: str | None = None


@dataclass(frozen=True, slots=True)
class PushResult:
    """The outcome of one ``push_generation``.

    ``outcome``: ``pushed`` (a new generation is the head), ``up-to-date`` (the files equal
    the head's, so nothing was written), ``source-changed`` (a file changed between the
    pass and its upload; the push stopped after its ``upload-started`` entry, nothing is
    the head that was not before, and another push will do; ``source_change`` says what
    moved, as a retryable ``changed`` problem) or ``dry-run`` (a pass ran and the generation
    was planned, but nothing was read again, uploaded or written; ``uploaded_files``,
    ``sent_blobs`` and ``sent_bytes`` are then what a push would send).
    ``generation``, ``manifest_sha256`` and
    ``head`` describe the vault's head after the call (generation 0 and ``None`` when nothing
    was ever pushed). ``adopted_generation`` names an interrupted earlier push this call
    finished first. ``owners`` lists every owner
    the pass ran; ``waiting_paths`` are files a complete owner withheld that no generation
    holds yet. ``uploaded_files`` counts the distinct file contents uploaded,
    ``reused_files`` the file entries whose content the previous generation already held, and
    ``sent_blobs``/``sent_bytes`` the stored blobs and bytes this call added.
    """

    outcome: PushOutcome
    library_id: str
    generation: int
    manifest_sha256: str | None
    head: RefValue | None
    owners: tuple[OwnerOutcome, ...]
    waiting_paths: tuple[str, ...]
    uploaded_files: int
    reused_files: int
    sent_blobs: int
    sent_bytes: int
    adopted_generation: int | None = None
    source_change: SourceChangedError | None = None

    @property
    def refused_owners(self) -> tuple[OwnerOutcome, ...]:
        """Owners whose files were refused; their previous entries were carried."""
        return tuple(owner for owner in self.owners if owner.state == "refused")

    @property
    def waiting_owners(self) -> tuple[OwnerOutcome, ...]:
        """Owners that did not complete (changed or refused)."""
        return tuple(owner for owner in self.owners if owner.state != "complete")

    @property
    def exit_status(self) -> int:
        """0 when the head holds this pass's view with no refused owner, else 1."""
        return 0 if self.outcome != "source-changed" and not self.refused_owners else 1


class _Progress:
    def __init__(self) -> None:
        self.files = 0
        self.blobs = 0
        self.bytes = 0

    def put(self, blob_store: BlobStore, data: bytes) -> BlobRef:
        ref = blob_store.put(data)
        self.blobs += 1
        self.bytes += ref.size
        return ref


class _PushSink:
    """Remembers how to read each carried item again, spooling database backups meanwhile.

    A backup's scratch copy is deleted once the pass has consumed it, so its bytes are
    spooled into the push's own scratch directory and uploaded from there.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self.items: dict[str, ReplicationItem] = {}
        self._scratch: ScratchDirectory | None = None
        self._item: ReplicationItem | None = None
        self._spool: BinaryIO | None = None
        self._spooled = 0

    def begin(self, owner: str, item: ReplicationItem) -> None:
        self._item = item
        if item.source == item.path:
            return
        scratch = self._scratch or ScratchDirectory(self._root)
        self._scratch = scratch
        self._spooled += 1
        path, relative = scratch.new_file(f"spool-{self._spooled}.sqlite3")
        stream = os.fdopen(os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "wb")
        self._spool = stream
        self._item = replace(item, source=relative)

    def chunk(self, data: bytes) -> None:
        if self._spool is not None:
            self._spool.write(data)

    def finish(self, entry: PassEntry | None) -> None:
        item, self._item = self._item, None
        if self._spool is not None:
            spool, self._spool = self._spool, None
            spool.close()
        if entry is not None and item is not None:
            self.items[entry.path] = item

    def close(self) -> None:
        if self._scratch is not None:
            self._scratch.remove()


def _planned(entry: PassEntry | ManifestEntry) -> PlannedManifestEntry:
    if isinstance(entry, PassEntry):
        return entry.planned()
    return PlannedManifestEntry.model_validate(
        {name: getattr(entry, name) for name in PlannedManifestEntry.model_fields}
    )


class _Plan(NamedTuple):
    entries: tuple[PlannedManifestEntry, ...]
    owners: tuple[OwnerOutcome, ...]
    waiting: tuple[str, ...]


def _owner_entries(owner: OwnerResult) -> tuple[list[PlannedManifestEntry], str | None]:
    """The owner's planned entries, or the path of the first one a restore could not write."""
    planned: list[PlannedManifestEntry] = []
    for entry in owner.entries:
        try:
            planned.append(_planned(entry))
        except ValidationError:
            return [], entry.path
    return planned, None


def _plan_files(
    result: PassResult,
    previous: LibraryManifest | None,
    sources: Sequence[ReplicationSource],
    wanted: frozenset[str],
) -> _Plan:
    """The files a generation carries: each complete owner's view, others' previous entries.

    A complete owner's withheld files keep their previous entries (a file is never dropped
    because it is temporarily unfit to copy); a file that is simply gone is dropped. A
    changed or refused owner keeps every previous entry whose path its rules decide. An owner
    whose entry names a path a restore could not write is refused.
    """
    owner_of_rule = {rule: source.name for source in sources for rule in source.rules}
    earlier: dict[str, list[ManifestEntry]] = {}
    by_path: dict[str, ManifestEntry] = {}
    for entry in () if previous is None else previous.entries:
        rules = matching_rules(entry.path)
        name = owner_of_rule.get(rules[0].name) if rules else None
        if name is not None and entry.file_class in wanted:
            earlier.setdefault(name, []).append(entry)
            by_path[entry.path] = entry
    files: dict[str, PlannedManifestEntry] = {}
    outcomes: list[OwnerOutcome] = []
    waiting: list[str] = []
    for owner in result.owners:
        kept = earlier.get(owner.owner, [])
        state, reason, problem_path = owner.state, owner.reason, owner.problem_path
        if state == "complete":
            planned, unrestorable = _owner_entries(owner)
            if unrestorable is None:
                files.update((entry.path, entry) for entry in planned)
                carried = 0
                for withheld in owner.withheld:
                    old = by_path.get(withheld.path)
                    if withheld.path in files:
                        continue
                    if old is None:
                        waiting.append(withheld.path)
                    else:
                        files[old.path] = _planned(old)
                        carried += 1
                outcomes.append(OwnerOutcome(owner.owner, "complete", carried))
                continue
            state, problem_path = "refused", unrestorable
            reason = f"{unrestorable}: a restore could not write a file of this name"
        files.update((entry.path, _planned(entry)) for entry in kept)
        outcomes.append(
            OwnerOutcome(owner.owner, state, len(kept), reason, problem_path, owner.held_back_by)
        )
    return _Plan(
        tuple(files[path] for path in sorted(files)), tuple(outcomes), tuple(sorted(waiting))
    )


def _new_contents(
    plan: ManifestPlan,
    reusable: Mapping[_ContentKey, tuple[BlobRef, ...]],
    readable: Collection[str],
) -> list[PlannedManifestEntry]:
    """One entry per file content the vault does not hold yet, in plan order.

    Only an entry whose path this pass read can be uploaded; a content the pass did not read
    is always in ``reusable``, because it comes from the previous manifest.
    """
    seen: set[_ContentKey] = set(reusable)
    new: list[PlannedManifestEntry] = []
    for entry in plan.entries:
        key = _ContentKey(entry.sha256, entry.size)
        if entry.kind == "file" and entry.size > 0 and key not in seen and entry.path in readable:
            seen.add(key)
            new.append(entry)
    return new


def _upload_file(
    blob_store: BlobStore, item: ReplicationItem, chunk_bytes: int, progress: _Progress
) -> tuple[BlobRef, ...]:
    """Read a planned file again and put it in pieces of ``chunk_bytes``.

    Raises: ``SourceChangedError`` when the file no longer has the planned identity, size or
    digest (pieces already put stay unreferenced); whatever ``put`` raises.
    """
    refs: list[BlobRef] = []
    buffer = bytearray()
    with item.open(chunk_bytes=chunk_bytes) as reader:
        for data in reader.chunks():
            buffer += data
            while len(buffer) >= chunk_bytes:
                refs.append(progress.put(blob_store, bytes(buffer[:chunk_bytes])))
                del buffer[:chunk_bytes]
        if buffer:
            refs.append(progress.put(blob_store, bytes(buffer)))
    progress.files += 1
    return tuple(refs)


def _manifest(
    plan: ManifestPlan, chunks: Mapping[_ContentKey, tuple[BlobRef, ...]]
) -> LibraryManifest:
    entries = tuple(
        ManifestEntry.model_validate(
            {name: getattr(entry, name) for name in PlannedManifestEntry.model_fields}
            | {
                "chunks": chunks[_ContentKey(entry.sha256, entry.size)]
                if entry.kind == "file" and entry.size > 0
                else ()
            }
        )
        for entry in plan.entries
    )
    fields = {name: getattr(plan, name) for name in ManifestPlan.model_fields}
    return LibraryManifest.model_validate(fields | {"entries": entries})


def _require_push_arguments(
    classes: Collection[FileClass], chunk_bytes: int
) -> tuple[FileClass, ...]:
    wanted = tuple(sorted(set(classes)))
    if not wanted or not set(wanted) <= FILE_CLASSES:
        raise ValueError(f"a push copies one or more known file classes, not {list(wanted)}")
    if not 1 <= chunk_bytes <= MAX_CHUNK_BYTES:
        raise ValueError(f"a chunk is 1 to {MAX_CHUNK_BYTES} bytes, not {chunk_bytes}")
    return wanted


def push_generation(
    layout: AssetLayout,
    *,
    blob_store: BlobStore,
    ref_store: RefStore,
    library_id: str,
    destination: DestinationContext,
    classes: Collection[FileClass],
    chunk_bytes: int,
    clock: Clock = utc_now,
    full: bool = False,
    dry_run: bool = False,
    ledger: ReplicationLedger | None = None,
    sources: Sequence[ReplicationSource] | None = None,
) -> PushResult:
    """Copy the library's current files to the vault as its next generation.

    Under ``.push.lock`` (never waiting): (1) the head must be the one this Mac left:
    ``state.json``'s head (same version and target), whose manifest has the state's digest and
    generation, follows the previous generation this Mac keeps, and has no newer local copy
    beside it; an absent state requires an absent head. A head one version ahead that names
    the generation the ledger's last ``upload-started`` entry started, chaining from the
    state's manifest, is this Mac's interrupted push, which is finished first. The ledger
    must still record the upload the state names (``ReplicationLedger.require_recorded``).
    (2) A
    replication pass reads every owner of ``classes``; complete owners give their files,
    others keep their previous entries (``OwnerOutcome``). (3) The plan is the next
    generation; if its files and classes equal the head's, nothing is written
    (``up-to-date``). (4) Otherwise the ``upload-started`` entry names the plan's payload
    digest and the new content planned, then (5) each file content the previous generation
    does not hold is read again, proven unchanged and put in pieces of ``chunk_bytes`` (a
    change stops the push: ``source-changed``); (6) the manifest is stored behind an envelope,
    the head is swapped from the state's version, and the local copy, ``state.json`` and the
    ``upload-completed`` entry are written.
    Params: ``layout``; ``blob_store``, the library's sealed store; ``ref_store``;
    ``library_id``; ``destination``, as the ledger records it; ``classes``, the destination's
    classes; ``chunk_bytes``, the largest piece read or put (1 to ``MAX_CHUNK_BYTES``; the
    composition root passes the sealed store's ``MAX_SEALED_PLAINTEXT_BYTES``); ``clock``;
    ``full``, which re-uploads every file this pass reads instead of reusing the previous
    generation's chunks and writes a generation even when nothing changed (the fix when
    ``verify_generation`` finds a blob the vault no longer holds); ``dry_run``, which runs
    the head check and the pass, plans the generation and reports what it would send without
    reading a file again, uploading anything or writing any record (an interrupted push is
    then reported as a fork rather than finished, because finishing it would write);
    ``ledger`` (default: the layout's); ``sources`` (default: ``library_sources()``).
    Returns: the ``PushResult``.
    Raises: ``ValueError`` for no or an unknown class or a bad chunk size, and
    ``StorageNameError`` for a bad library id (before any I/O); ``PushRunningError``;
    ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the head check fails (message
    ``vault head check failed (<check>)``, with ``(fork)`` also when the head changed during
    the push), when ``state.json`` records another destination than ``destination``, or for
    an unsafe or broken ``provenance/vault`` record; every error
    ``collect_pass`` raises; every ``StorageError`` the stores raise (the push stops; a
    started entry without a completed one records it).
    """
    wanted = _require_push_arguments(classes, chunk_bytes)
    validate_library_id(library_id)
    layout = AssetLayout.validate(layout.root)
    records = VaultRecords(layout)
    vault = _Vault(
        records,
        ReplicationLedger(layout) if ledger is None else ledger,
        blob_store,
        ref_store,
        library_id,
    )
    finish = _Finish(destination, clock)
    sources = library_sources() if sources is None else sources
    with records.push_lock():
        resolved = _resolve_head(vault, None if dry_run else finish, destination)
        sink = _PushSink(layout.root)
        try:
            passed = collect_pass(layout, sources, sink, classes=wanted, chunk_bytes=chunk_bytes)
            return _push(
                vault, finish, resolved, passed, sink, sources, wanted, chunk_bytes, full, dry_run
            )
        finally:
            sink.close()


def _push(
    vault: _Vault,
    finish: _Finish,
    resolved: _Resolved,
    passed: PassResult,
    sink: _PushSink,
    sources: Sequence[ReplicationSource],
    wanted: tuple[FileClass, ...],
    chunk_bytes: int,
    full: bool,
    dry_run: bool,
) -> PushResult:
    stored = resolved.stored
    previous = None if stored is None else stored.manifest
    files = _plan_files(passed, previous, sources, frozenset(wanted))
    plan = ManifestPlan(
        library_id=vault.library_id,
        generation=resolved.generation + 1,
        previous_manifest_sha256=None if stored is None else stored.digest,
        created_at=finish.clock(),
        classes=wanted,
        entries=files.entries,
    )
    held = {
        _ContentKey(entry.sha256, entry.size): entry.chunks
        for entry in (() if previous is None else previous.entries)
        if entry.kind == "file" and entry.size > 0
    }
    readable = set(sink.items)
    # ``--full`` re-uploads every file this pass read, whatever the previous generation holds
    # (the fix when ``vault verify`` finds a blob the vault lost). A file the pass did not
    # read cannot be re-uploaded and keeps its chunks either way.
    reusable = (
        held
        if not full
        else {
            key: refs
            for key, refs in held.items()
            if key
            not in {
                _ContentKey(entry.sha256, entry.size)
                for entry in plan.entries
                if entry.path in readable
            }
        }
    )
    reused = sum(
        entry.kind == "file" and _ContentKey(entry.sha256, entry.size) in reusable
        for entry in plan.entries
    )
    current = _pushed(resolved.state).head
    unchanged = PushResult(
        outcome="up-to-date",
        library_id=vault.library_id,
        generation=resolved.generation,
        manifest_sha256=None if stored is None else stored.digest,
        head=current,
        owners=files.owners,
        waiting_paths=files.waiting,
        uploaded_files=0,
        reused_files=reused,
        sent_blobs=0,
        sent_bytes=0,
        adopted_generation=resolved.adopted,
    )
    if (
        not full
        and previous is not None
        and (plan.classes, plan.entries) == (previous.classes, manifest_plan(previous).entries)
    ):
        return unchanged
    new = _new_contents(plan, reusable, readable)
    if dry_run:
        return replace(
            unchanged,
            outcome="dry-run",
            uploaded_files=len(new),
            sent_blobs=sum(-(-entry.size // chunk_bytes) for entry in new),
            sent_bytes=sum(entry.size for entry in new),
        )
    started = vault.ledger.append(
        kind="upload-started",
        library_id=vault.library_id,
        generation=plan.generation,
        destination=finish.destination,
        payload_sha256=manifest_payload_digest(plan),
        manifest_sha256=None,
        classes=plan.classes,
        byte_count=sum(entry.size for entry in new),
        blob_count=sum(-(-entry.size // chunk_bytes) for entry in new),
        head_version=None,
        recorded_at=finish.clock(),
    )
    progress = _Progress()
    chunks = dict(held)
    try:
        for entry in new:
            item = replace(
                sink.items[entry.path], expected_sha256=entry.sha256, expected_size=entry.size
            )
            chunks[_ContentKey(entry.sha256, entry.size)] = _upload_file(
                vault.blob_store, item, chunk_bytes, progress
            )
    except SourceChangedError as error:
        # Whatever the re-read found, the file is no longer the one the plan names.
        change = SourceChangedError(
            error.path, "changed", f"changed after the pass read it ({error.detail})"
        )
        return replace(
            unchanged,
            outcome="source-changed",
            uploaded_files=progress.files,
            sent_blobs=progress.blobs,
            sent_bytes=progress.bytes,
            source_change=change,
        )
    written = store_manifest(vault.blob_store, _manifest(plan, chunks), chunk_bytes=chunk_bytes)
    try:
        head = vault.ref_store.swap(
            vault.head_name,
            expected_version=None if current is None else current.version,
            target=written.ref,
        )
    except StorageError as error:
        if error.code != "STORAGE.CONFLICT":
            raise
        raise _head_failure(
            "fork", f"the vault's head changed while generation {plan.generation} was pushed"
        ) from error
    _finish(vault, finish, written, head, previous, started)
    blobs, size = _sent(written, previous)
    return replace(
        unchanged,
        outcome="pushed",
        generation=plan.generation,
        manifest_sha256=written.digest,
        head=head,
        uploaded_files=progress.files,
        sent_blobs=blobs,
        sent_bytes=size,
    )


# -- verifying -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiedEntry:
    """One manifest entry as ``verify_generation`` found it.

    ``present``: every chunk was fetched, opened and the reassembled file has the entry's
    size and SHA-256. ``missing``: a chunk is not in the vault. ``mismatch``: a chunk failed
    its digest or did not open, or the file did not reassemble (``detail`` says which).
    ``pinned``: a pin entry, which the vault never holds.
    """

    path: str
    file_class: FileClass
    kind: Literal["file", "pin"]
    size: int
    state: VerifyState
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """What ``verify_generation`` found for one generation.

    ``unknown_paths`` are requested paths that name no entry of the generation.
    """

    library_id: str
    generation: int
    manifest_sha256: str
    created_at: datetime
    entries: tuple[VerifiedEntry, ...]
    unknown_paths: tuple[str, ...]

    def count(self, state: VerifyState) -> int:
        """How many entries are in ``state``."""
        return sum(entry.state == state for entry in self.entries)

    @property
    def ok(self) -> bool:
        """Every selected entry is present or pinned, and every requested path was found."""
        return not self.unknown_paths and all(
            entry.state in ("present", "pinned") for entry in self.entries
        )

    @property
    def exit_status(self) -> int:
        return 0 if self.ok else 1


def _selected(path: str, paths: Collection[str]) -> bool:
    return any(path == named or path.startswith(f"{named}/") for named in paths)


def _verify_entry(blob_store: BlobStore, entry: ManifestEntry) -> VerifiedEntry:
    if entry.kind == "pin":
        return VerifiedEntry(entry.path, entry.file_class, entry.kind, entry.size, "pinned")
    state: VerifyState = "present"
    detail = None
    try:
        for _ in _entry_plaintext(blob_store, entry):
            pass
    except StorageError as error:
        if error.code not in ("STORAGE.NOT_FOUND", "STORAGE.INTEGRITY"):
            raise
        state = "missing" if error.code == "STORAGE.NOT_FOUND" else "mismatch"
        detail = str(error)
    return VerifiedEntry(entry.path, entry.file_class, entry.kind, entry.size, state, detail)


def verify_generation(
    layout: AssetLayout,
    *,
    blob_store: BlobStore,
    ref_store: RefStore,
    library_id: str,
    generation: int | None = None,
    paths: Collection[str] | None = None,
    classes: Collection[FileClass] | None = None,
) -> VerifyReport:
    """Download and check every chunk of a generation; the vault's word counts for nothing.

    The head is checked as ``push_generation`` checks it (under ``.push.lock``, without
    finishing an interrupted push). ``generation`` names the head's generation (the default)
    or an older one whose manifest this Mac keeps and the chain from the head proves. Each
    selected file entry's chunks are fetched and opened and the reassembled file hashed; pin
    entries are reported ``pinned``.
    Params: ``layout``; ``blob_store``, the library's sealed store; ``ref_store``;
    ``library_id``; ``generation``; ``paths``, manifest paths or directories whose entries to
    check (default: all); ``classes``, the classes to check (default: all). An entry is
    checked when it matches both filters.
    Returns: the ``VerifyReport``.
    Raises: ``StorageNameError`` for a bad library id; ``PushRunningError``;
    ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the head check fails;
    ``StorageError`` (``STORAGE.NOT_FOUND``) when nothing was pushed yet or the named
    generation cannot be reached; any other store error (``STORAGE.UNAVAILABLE``, for
    example) unchanged.
    """
    validate_library_id(library_id)
    layout = AssetLayout.validate(layout.root)
    records = VaultRecords(layout)
    vault = _Vault(records, ReplicationLedger(layout), blob_store, ref_store, library_id)
    with records.push_lock():
        resolved = _resolve_head(vault, None)
    if resolved.stored is None:
        raise storage_error(
            "STORAGE.NOT_FOUND", f"this Mac has not pushed a generation of library {library_id}"
        )
    manifest = (
        resolved.stored.manifest
        if generation is None
        else _named_generation(records, resolved.stored, generation)
    )
    wanted_paths = None if paths is None else tuple(paths)
    selected = [
        entry
        for entry in manifest.entries
        if (wanted_paths is None or _selected(entry.path, wanted_paths))
        and (classes is None or entry.file_class in classes)
    ]
    unknown = (
        ()
        if wanted_paths is None
        else tuple(
            sorted(
                named
                for named in set(wanted_paths)
                if not any(_selected(entry.path, (named,)) for entry in manifest.entries)
            )
        )
    )
    return VerifyReport(
        library_id=library_id,
        generation=manifest.generation,
        manifest_sha256=manifest_digest(manifest),
        created_at=manifest.created_at,
        entries=tuple(_verify_entry(blob_store, entry) for entry in selected),
        unknown_paths=unknown,
    )


# -- restoring -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PinOutcome:
    """How a pin entry was restored: hard-linked or copied from the runtime, or missing."""

    path: str
    pin_source: str
    state: PinState
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """A library restored at ``target``.

    ``generation``, ``manifest_sha256`` and ``created_at`` name the restored generation;
    ``class_counts`` counts its entries per class (every class it covers, pins included);
    ``restored_files`` and ``restored_bytes`` count the files written from the vault;
    ``pins`` says how each pin entry was restored. The recording index is not restored: it
    rebuilds itself when the library is next opened.
    """

    target: Path
    library_id: str
    generation: int
    manifest_sha256: str
    created_at: datetime
    class_counts: Mapping[FileClass, int]
    restored_files: int
    restored_bytes: int
    pins: tuple[PinOutcome, ...]

    @property
    def pin_missing(self) -> tuple[PinOutcome, ...]:
        """The pin entries whose files are not in place (their run cannot synthesize yet)."""
        return tuple(pin for pin in self.pins if pin.state == "missing")


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _require_empty_target(target: Path) -> bool:
    """Whether ``target`` exists (as an empty directory of this user); refuse anything else.

    Raises: ``ValueError`` for a relative or unnormalized target; ``RestoreRefusedError``.
    """
    validate_absolute_path(str(target))
    if len(target.parts) < 4:
        raise RestoreRefusedError("unsafe-target", f"{target} is not specific enough")
    parent = target.parent
    try:
        parent_metadata = os.lstat(parent)
    except FileNotFoundError as error:
        raise RestoreRefusedError("unsafe-target", f"{parent} does not exist") from error
    if not stat.S_ISDIR(parent_metadata.st_mode) or os.path.realpath(parent) != str(parent):
        raise RestoreRefusedError(
            "unsafe-target", f"{parent} is not a directory reached without symbolic links"
        )
    try:
        metadata = os.lstat(target)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RestoreRefusedError("unsafe-target", f"{target} is not a directory of this user")
    if os.listdir(target):
        raise RestoreRefusedError("target-not-empty", f"{target} is not empty")
    return True


def _restorable_parts(path: str) -> tuple[str, ...]:
    """The components of a path a restore may write.

    Raises: ``RestoreRefusedError`` (``unrestorable-path``) for a path that is not canonical,
    names ``provenance/vault`` (in any case or normalization), or is not inside one of the
    asset directories.
    """
    try:
        validate_relative_path(path)
    except ValueError as error:
        raise RestoreRefusedError(
            "unrestorable-path", f"the manifest names a path that is not canonical: {path!r}"
        ) from error
    key = manifest_path_key(path)
    if key == VAULT_RECORDS_ROOT or key.startswith(f"{VAULT_RECORDS_ROOT}/"):
        raise RestoreRefusedError(
            "unrestorable-path",
            f"the manifest names {path}, but provenance/vault never leaves the Mac and is never "
            "restored",
        )
    parts = PurePosixPath(path).parts
    if len(parts) < 2 or parts[0] not in ASSET_DIRECTORIES:
        raise RestoreRefusedError(
            "unrestorable-path",
            f"the manifest names {path}, which is outside the asset directories",
        )
    return parts


def _require_head_this_mac_pushed(
    records: VaultRecords, library_id: str, head: RefValue, stored: StoredManifest
) -> None:
    """Hold a head against ``state.json``, the same anchor a push resolves it against.

    A restore onto a Mac that keeps no records of the library (``records is None``) has no
    anchor and can only take what the vault holds; where the state exists, a head the vault
    host rolled back, or one that does not follow the generation this Mac pushed, is refused
    before anything is restored.
    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) naming the failed check.
    """
    state = records.read_state(library_id)
    if state is None or state.head is None:
        return
    manifest = stored.manifest
    if head.version < state.head.version or manifest.generation < state.generation:
        raise _head_failure(
            "rollback",
            f"the vault's head is at version {head.version} (generation {manifest.generation}), "
            f"older than version {state.head.version} (generation {state.generation}), which "
            "this Mac pushed",
        )
    if manifest.generation == state.generation and stored.digest != state.manifest_sha256:
        raise _head_failure(
            "digest-mismatch",
            f"the head names generation {manifest.generation} ({stored.digest}), but this Mac "
            f"pushed generation {state.generation} ({state.manifest_sha256})",
        )
    if (
        manifest.generation == state.generation + 1
        and manifest.previous_manifest_sha256 != state.manifest_sha256
    ):
        raise _head_failure(
            "fork",
            f"the head's generation {manifest.generation} does not follow the generation "
            f"{state.generation} this Mac pushed",
        )


def _manifest_to_restore(
    blob_store: BlobStore,
    ref_store: RefStore,
    library_id: str,
    selection: tuple[int, str] | None,
    records: VaultRecords | None,
) -> LibraryManifest:
    head = ref_store.read(library_head_ref(library_id))
    if head is None:
        raise storage_error(
            "STORAGE.NOT_FOUND", f"the vault holds no generation of library {library_id}"
        )
    stored = read_stored_manifest(blob_store, head.target, library_id=library_id)
    if records is not None:
        _require_head_this_mac_pushed(records, library_id, head, stored)
    if selection is None:
        return stored.manifest
    generation, digest = selection
    if generation > stored.manifest.generation:
        raise _head_failure(
            "rollback",
            f"the vault's head is generation {stored.manifest.generation}, older than the "
            f"generation {generation} to restore",
        )
    manifest = _named_generation(records, stored, generation)
    if manifest_digest(manifest) != digest:
        raise _head_failure(
            "digest-mismatch",
            f"generation {generation} is manifest {manifest_digest(manifest)}, not {digest}",
        )
    return manifest


def _make_directories(staging: Path, parts: Sequence[str]) -> Path:
    directory = staging
    for part in parts:
        directory = directory / part
        if not os.path.lexists(directory):
            os.mkdir(directory, 0o700)
            os.chmod(directory, 0o700)
    return directory


def _restore_file(blob_store: BlobStore, staging: Path, entry: ManifestEntry) -> None:
    parts = PurePosixPath(entry.path).parts
    destination = _make_directories(staging, parts[:-1]) / parts[-1]
    with os.fdopen(os.open(destination, _CREATE_FLAGS, 0o600), "wb") as stream:
        for piece in _entry_plaintext(blob_store, entry):
            stream.write(piece)
        stream.flush()
        os.fchmod(stream.fileno(), entry.mode)
        os.fsync(stream.fileno())


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, _READ_FLAGS), "rb") as stream:
        while piece := stream.read(_READ_BYTES):
            digest.update(piece)
    return digest.hexdigest()


def _pinned_file(staging: Path, entry: ManifestEntry, runtime_root: Path) -> Path | str:
    """The runtime file a restored run pins for ``entry``, or why there is none."""
    from .finetune import FineTuneRun

    run_path = staging.joinpath(*PurePosixPath(entry.path).parents[1].parts, "run.json")
    try:
        run = FineTuneRun.model_validate_json(run_path.read_bytes())
    except FileNotFoundError:
        return "the generation holds no run record for these weights"
    except ValidationError:
        return "the run record is not a valid fine-tune run"
    pinned = PurePosixPath(run.runtime.pretrained_path)
    if run.runtime.pretrained_sha256 != entry.sha256:
        return "the run pins other weights"
    if PurePosixPath(entry.path).name != f"pretrained_{pinned.name}":
        return "the file is not named for the weights its run pins"
    try:
        relative = validate_relative_path(pinned.relative_to(run.runtime.runtime_root).as_posix())
    except ValueError:
        return "the run's pinned weights are not inside its runtime"
    candidate = runtime_root.joinpath(*PurePosixPath(relative).parts)
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        return f"the runtime has no {relative}"
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != entry.size:
        return f"the runtime's {relative} is not a regular file of {entry.size} bytes"
    return candidate


def _restore_pin(staging: Path, entry: ManifestEntry, runtime_root: Path | None) -> PinOutcome:
    """Re-link (or copy) a pinned file from the runtime, only when its bytes match the pin."""
    source = PRETRAINED_PIN_SOURCE
    if entry.pin_source != source:
        return PinOutcome(entry.path, str(entry.pin_source), "missing", "unknown pin source")
    if runtime_root is None:
        return PinOutcome(entry.path, source, "missing", "no runtime root was given")
    candidate = _pinned_file(staging, entry, runtime_root)
    if isinstance(candidate, str):
        return PinOutcome(entry.path, source, "missing", candidate)
    parts = PurePosixPath(entry.path).parts
    directory = _make_directories(staging, parts[:-1])
    temporary = directory / f".{parts[-1]}.pin-{secrets.token_hex(8)}"
    state: PinState = "linked"
    try:
        os.link(candidate, temporary, follow_symlinks=False)
    except OSError:
        state = "copied"
        with (
            os.fdopen(os.open(candidate, _READ_FLAGS), "rb") as reader,
            os.fdopen(os.open(temporary, _CREATE_FLAGS, 0o600), "wb") as writer,
        ):
            shutil.copyfileobj(reader, writer, _READ_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
    if _sha256_of(temporary) != entry.sha256:
        os.unlink(temporary)
        return PinOutcome(
            entry.path, source, "missing", "the runtime's weights do not match the run's pin"
        )
    if state == "copied":
        os.chmod(temporary, entry.mode)
    rename_no_replace(temporary, directory / parts[-1])
    return PinOutcome(entry.path, source, state)


def _open_as_owner(staging: Path) -> None:
    """Give the restored library what the recording service's first open gives a library."""
    from .recording_takes import RecordingTakeStore

    RecordingTakeStore(AssetLayout.validate(staging))


def _read_only(relative: str) -> bool:
    return any(relative.startswith(f"{tree}/") for tree in READ_ONLY_TREES)


def _seal_directories(staging: Path) -> None:
    """Give every directory its final mode, deepest first, and flush it."""
    directories = [Path(directory) for directory, _, _ in os.walk(staging)]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        relative = directory.relative_to(staging).as_posix()
        os.chmod(directory, 0o500 if _read_only(relative) else 0o700)
        fsync_directory(directory)


def _remove_tree(path: Path) -> None:
    for directory, _, _ in os.walk(path):
        os.chmod(directory, 0o700)
    shutil.rmtree(path)


def _publish(staging: Path, target: Path, existed: bool) -> None:
    if existed:
        try:
            os.rmdir(target)
        except OSError as error:
            raise RestoreRefusedError(
                "target-not-empty", f"{target} changed while the library was restored"
            ) from error
    try:
        rename_no_replace(staging, target)
    except FileExistsError as error:
        raise RestoreRefusedError(
            "target-not-empty", f"{target} appeared while the library was restored"
        ) from error
    fsync_directory(target.parent)


def restore_library(
    target: Path,
    *,
    blob_store: BlobStore,
    ref_store: RefStore,
    library_id: str,
    generation: int | None = None,
    manifest_sha256: str | None = None,
    accept_latest: bool = False,
    records: VaultRecords | None = None,
    runtime_root: Path | None = None,
    free_bytes: Callable[[Path], int] = _free_bytes,
) -> RestoreResult:
    """Restore one generation of a library into an empty asset root.

    The generation is either named (``generation`` and ``manifest_sha256``: the head's
    manifest must be exactly that, or an older generation must be reachable from the head
    through manifest copies ``records`` keeps) or ``accept_latest`` (the head's generation,
    whose time and counts the result carries). Where ``records`` are given, the head is held
    against ``state.json`` first, as a push holds it: a head older than the generation this
    Mac pushed, or one that does not follow it, is refused rather than restored. A restore
    onto a Mac that keeps no records has no such anchor. Before anything is written: every
    entry path must be restorable, and the target's filesystem must have the entries' bytes
    plus a tenth plus ``RESTORE_MARGIN_BYTES`` free. The library is written into a private sibling
    ``<target>.restoring-<random>`` (0700) with all ten asset directories; each file is
    written 0600 from its verified chunks, fsynced and given its entry's mode (0400 or
    0600); directories below a ``READ_ONLY_TREES`` tree become 0500, all others 0700; pin
    entries are hard-linked (or copied) from ``runtime_root`` only when the file there has
    the pinned digest, and otherwise listed as missing. Only then is the staging directory
    renamed to the target (an empty target directory is removed first); on any failure it is
    removed and nothing is left at the target. ``provenance/vault`` is never written. Before
    the rename the take store is opened once in write mode, which adds only its Mac-only
    scaffolding (the metadata lock and empty directories) that its read-only readers need.
    Params: ``target``, an absolute, normalized path whose parent exists without symbolic
    links; ``blob_store``, the library's sealed store; ``ref_store``; ``library_id``;
    ``generation`` and ``manifest_sha256``, or ``accept_latest``; ``records``, the Mac-side
    vault records whose manifest copies reach older generations; ``runtime_root``, the
    fine-tune runtime to re-link pinned weights from; ``free_bytes``, the free space of a
    directory's filesystem.
    Returns: the ``RestoreResult``.
    Raises: ``ValueError`` when neither or both ways of naming the generation are given, or
    for a bad target path (before any I/O); ``StorageNameError`` for a bad library id;
    ``RestoreRefusedError``; ``StorageError`` (``STORAGE.NOT_FOUND``) when the vault holds no
    generation, the named one cannot be reached, or a chunk is missing;
    ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the head, a manifest, a chunk or a
    reassembled file fails its check (``(rollback)``, ``(digest-mismatch)`` or ``(fork)``
    when the head is not the one ``records`` anchor, and ``(rollback)`` or
    ``(digest-mismatch)`` when the named generation is not what the vault holds);
    ``SecurityError`` when the restored root fails
    ``AssetLayout.validate``; ``OSError`` from the filesystem.
    """
    selection: tuple[int, str] | None = None
    if generation is not None and manifest_sha256 is not None and not accept_latest:
        selection = (generation, manifest_sha256)
    elif not accept_latest or generation is not None or manifest_sha256 is not None:
        raise ValueError(
            "name the generation to restore (generation and manifest_sha256) or accept the latest"
        )
    validate_library_id(library_id)
    existed = _require_empty_target(target)
    manifest = _manifest_to_restore(blob_store, ref_store, library_id, selection, records)
    files = [entry for entry in manifest.entries if entry.kind == "file"]
    pins = [entry for entry in manifest.entries if entry.kind == "pin"]
    for entry in manifest.entries:
        _restorable_parts(entry.path)
    needed = sum(entry.size for entry in (files if runtime_root is None else manifest.entries))
    required = needed + needed // RESTORE_MARGIN_DIVISOR + RESTORE_MARGIN_BYTES
    available = free_bytes(target.parent)
    if available < required:
        raise RestoreRefusedError(
            "insufficient-space",
            f"restoring generation {manifest.generation} needs {required} bytes free beside "
            f"{target}, but {available} are",
        )
    staging = target.parent / f"{target.name}{RESTORING_INFIX}{secrets.token_hex(8)}"
    os.mkdir(staging, 0o700)
    try:
        os.chmod(staging, 0o700)
        for name in ASSET_DIRECTORIES:
            _make_directories(staging, (name,))
        for entry in files:
            _restore_file(blob_store, staging, entry)
        outcomes = tuple(_restore_pin(staging, entry, runtime_root) for entry in pins)
        _open_as_owner(staging)
        _seal_directories(staging)
        _publish(staging, target, existed)
    except BaseException:
        if os.path.lexists(staging):
            _remove_tree(staging)
        raise
    AssetLayout.validate(target)
    counts = {file_class: 0 for file_class in manifest.classes}
    for entry in manifest.entries:
        counts[entry.file_class] += 1
    return RestoreResult(
        target=target,
        library_id=library_id,
        generation=manifest.generation,
        manifest_sha256=manifest_digest(manifest),
        created_at=manifest.created_at,
        class_counts=counts,
        restored_files=len(files),
        restored_bytes=sum(entry.size for entry in files),
        pins=outcomes,
    )

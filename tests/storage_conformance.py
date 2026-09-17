"""Storage port conformance: what every BlobStore and RefStore must do.

Every check runs once per backend spec in ``SPECS`` (test ids end in ``[local]``, ...). A spec
opens stores (in this process or in a spawned child, so it must pickle), states its
capabilities, and offers hooks that damage or inspect the files behind the store. Portable
checks run for every spec. A check that needs a capability or a hook is its own test, which
starts by requiring them, so a spec without one reports it SKIPPED with a reason naming the
capability; no assertion is dropped inside a test that passes. Specs: ``local`` (the local
store), ``sealed-local`` (the sealed store over the local store, with a fixed synthetic test
key; its refs are random, its largest put is ``MAX_SEALED_PLAINTEXT_BYTES``, and its hooks
damage the ciphertext files of the local store underneath), ``http`` (the vault client
against a ``storage_server`` on the loopback, whose hooks act on the server's backing
directory and whose refusals never name it) and ``sealed-http`` (the sealed store over that
client: what the Mac really pushes). A later step appends its own ``SpecFactory`` to
``SPECS``.

Collected by pytest through ``python_files`` (pyproject). Multi-process checks spawn children
that run the module-level ``_..._in_child`` functions and synchronize only through barriers,
events and queues. Checks of one implementation's internals (the local store's refusal
details, its sweep, its lock timeout) live in ``test_storage_local.py``.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import socket
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, chdir, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, get_args

import pytest

from cole_voice_lab import storage_local
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.storage_contracts import VaultNamespaceConfig, VaultServerConfig
from cole_voice_lab.storage_http_client import VaultConnection
from cole_voice_lab.storage_local import LocalBlobStore, LocalRefStore
from cole_voice_lab.storage_ports import (
    MAX_BLOB_BYTES,
    BlobRef,
    BlobStore,
    RefStore,
    RefValue,
    StorageError,
    StorageIntegrityError,
    StorageNameError,
    library_head_ref,
)
from cole_voice_lab.storage_sealing import (
    MAX_SEALED_PLAINTEXT_BYTES,
    LibraryKey,
    SealedBlobStore,
    open_sealed_blob,
)
from cole_voice_lab.storage_server import (
    LOOPBACK_HOST,
    NAMESPACES_DIRECTORY,
    VaultServer,
    hash_token,
    new_token,
)

CONTEXT = multiprocessing.get_context("spawn")
CHILD_TIMEOUT_SECONDS = 120.0
WRITERS = 4
PAYLOAD = b"synthetic conformance payload"
HEAD = library_head_ref("conformance-library")

type BlobDamage = Literal[
    "flipped-byte",
    "truncated",
    "extended",
    "symlink",
    "hard-link",
    "group-readable",
    "fifo",
    "socket",
]
type RefCorruption = Literal["garbage", "oversized", "another-name", "reformatted"]
type BackingProblem = Literal[
    "root-group-bits",
    "symlinked-root",
    "replaced-root",
    "removed-root",
    "foreign-root-entry",
    "removed-refs-directory",
    "removed-ref-lock",
    "group-readable-temporary-lock",
    "foreign-temporary-entry",
    "shard-group-bits",
    "symlinked-shard",
    "unreadable-shard",
    "symlinked-ref-directory",
    "unreadable-blob",
]
type Operation = Literal["get", "has", "put", "put-new", "read", "swap"]


@dataclass(frozen=True)
class Refusal:
    """How a backing problem must be refused.

    ``fresh``: the operations that must fail on stores opened after the damage; ``existing``:
    those that must fail on stores opened before it (the ones that touch the damaged part).
    """

    code: Literal["STORAGE.INTEGRITY", "STORAGE.UNAVAILABLE"]
    fresh: frozenset[Operation]
    existing: frozenset[Operation]


EVERY_OPERATION: frozenset[Operation] = frozenset(get_args(Operation.__value__))
BLOB_OPERATIONS: frozenset[Operation] = frozenset({"get", "has", "put"})
REF_OPERATIONS: frozenset[Operation] = frozenset({"read", "swap"})
NO_OPERATION: frozenset[Operation] = frozenset()
REFUSALS: dict[BackingProblem, Refusal] = {
    "root-group-bits": Refusal("STORAGE.INTEGRITY", EVERY_OPERATION, EVERY_OPERATION),
    "symlinked-root": Refusal("STORAGE.INTEGRITY", EVERY_OPERATION, EVERY_OPERATION),
    "replaced-root": Refusal("STORAGE.INTEGRITY", EVERY_OPERATION, EVERY_OPERATION),
    # A missing backing (an unmounted disk) may come back: retryable.
    "removed-root": Refusal("STORAGE.UNAVAILABLE", EVERY_OPERATION, EVERY_OPERATION),
    "foreign-root-entry": Refusal("STORAGE.INTEGRITY", EVERY_OPERATION, NO_OPERATION),
    "removed-refs-directory": Refusal("STORAGE.INTEGRITY", EVERY_OPERATION, REF_OPERATIONS),
    "removed-ref-lock": Refusal("STORAGE.INTEGRITY", EVERY_OPERATION, frozenset({"swap"})),
    "group-readable-temporary-lock": Refusal(
        "STORAGE.INTEGRITY", EVERY_OPERATION, frozenset({"put-new", "swap"})
    ),
    "foreign-temporary-entry": Refusal("STORAGE.INTEGRITY", EVERY_OPERATION, NO_OPERATION),
    "shard-group-bits": Refusal("STORAGE.INTEGRITY", BLOB_OPERATIONS, BLOB_OPERATIONS),
    "symlinked-shard": Refusal("STORAGE.INTEGRITY", BLOB_OPERATIONS, BLOB_OPERATIONS),
    "unreadable-shard": Refusal("STORAGE.UNAVAILABLE", BLOB_OPERATIONS, BLOB_OPERATIONS),
    "symlinked-ref-directory": Refusal("STORAGE.INTEGRITY", REF_OPERATIONS, REF_OPERATIONS),
    "unreadable-blob": Refusal(
        "STORAGE.UNAVAILABLE", frozenset({"get", "put"}), frozenset({"get", "put"})
    ),
}
# Refs that never passed validation (a caller bug); every store refuses them before any I/O.
FORGED_REFS = {
    "path-digest": BlobRef.model_construct(sha256="../../outside", size=1),
    "short-digest": BlobRef.model_construct(sha256="not-a-digest", size=-5),
    "negative-size": BlobRef.model_construct(sha256="a" * 64, size=-1),
    "oversized": BlobRef.model_construct(sha256="a" * 64, size=MAX_BLOB_BYTES + 1),
    "text-size": BlobRef.model_construct(sha256="a" * 64, size="1"),
    "no-fields": BlobRef.model_construct(),
}
BAD_REF_NAMES = (
    "",
    "/",
    "/libraries/x/head",
    "libraries/x/head/",
    "libraries//head",
    ".",
    "..",
    "libraries/../head",
    "libraries/./head",
    "Libraries/x/head",
    "libraries/x y/head",
    "libraries/é/head",
    "-libraries/x",
    "a" * 65,
    "/".join("a" * 9),
    "libraries\\x\\head",
    "libraries/x/head\x00",
)


@dataclass
class CrashLeftovers:
    """What ``leave_crash_leftovers`` left, which no store may ever show.

    ``blobs`` are the refs the leftover temporaries' bytes would have been stored under;
    ``ref_names`` are refs a dead writer was creating.
    """

    blobs: tuple[BlobRef, ...]
    ref_names: tuple[str, ...]


@dataclass
class PausedPut:
    """A writer in another process, stopped after writing its temporary.

    ``outcome`` is the writer's ``report`` tuple once the ``with`` block has exited.
    """

    data: bytes
    outcome: tuple[str, str, str] | None = None

    @property
    def ref(self) -> BlobRef:
        """The writer's ref; the writer must have succeeded."""
        assert self.outcome is not None and self.outcome[0] == "ok", self.outcome
        return BlobRef.model_validate_json(self.outcome[1])


class StorageSpec(Protocol):
    """One backend under test. It pickles, so spawned children can open stores through it.

    Capabilities: ``deterministic_refs`` (the same bytes always get the same ref and one stored
    copy), ``max_put_bytes`` (the largest ``put`` accepted), ``errors_name_backing_paths``
    (refusals name the backing directory; a store behind a server keeps its paths to itself),
    ``backing_root`` (the directory behind the store, or ``None`` when the damage and
    inspection hooks are unavailable).
    """

    name: ClassVar[str]
    deterministic_refs: ClassVar[bool]
    max_put_bytes: ClassVar[int]
    errors_name_backing_paths: ClassVar[bool]

    @property
    def backing_root(self) -> Path | None: ...

    def open_blob_store(self) -> BlobStore: ...

    def open_ref_store(self) -> RefStore: ...

    def stored_blob_files(self) -> list[Path]:
        """Every stored blob file behind the store."""
        ...

    def damage_blob(self, ref: BlobRef, damage: BlobDamage) -> None:
        """Damage the stored file of ``ref`` in place."""
        ...

    def corrupt_ref(self, name: str, corruption: RefCorruption) -> None:
        """Overwrite the stored value of ref ``name`` in place with a value no store accepts.

        ``garbage`` is not JSON, ``oversized`` is longer than any ref value, ``another-name``
        is a valid value of another ref, ``reformatted`` is the value in a non-canonical form.
        """
        ...

    def loosen_backing(self, problem: BackingProblem) -> Callable[[], bool]:
        """Damage the backing (see ``REFUSALS``); the callable says whether it still is."""
        ...

    def leave_crash_leftovers(self) -> CrashLeftovers:
        """Leave what writers that died mid-put and mid-swap leave behind."""
        ...

    def leftover_temporaries(self) -> tuple[str, ...]:
        """The writers' temporaries currently behind the store."""
        ...

    def paused_put(self, data: bytes) -> AbstractContextManager[PausedPut]:
        """Run ``put(data)`` in another process, held after its temporary is written.

        The writer resumes when the ``with`` block exits; its outcome is then recorded,
        whether it succeeded or not.
        """
        ...


# -- the local spec -----------------------------------------------------------------------


@dataclass(frozen=True)
class LocalSpec:
    """``LocalBlobStore`` and ``LocalRefStore`` on one private directory.

    ``scratch`` is a private directory outside the store for symlink targets and extra links.
    """

    root: Path
    scratch: Path
    name: ClassVar[str] = "local"
    deterministic_refs: ClassVar[bool] = True
    max_put_bytes: ClassVar[int] = MAX_BLOB_BYTES
    errors_name_backing_paths: ClassVar[bool] = True

    @property
    def backing_root(self) -> Path | None:
        return self.root

    def open_blob_store(self) -> BlobStore:
        return LocalBlobStore(self.root)

    def open_ref_store(self) -> RefStore:
        return LocalRefStore(self.root)

    def blob_path(self, ref: BlobRef) -> Path:
        return self.root / "blobs" / ref.sha256[:2] / ref.sha256

    def stored_form_holds(self, stored: bytes, data: bytes) -> bool:
        """Whether ``stored``, as the store writes it to a file, is the stored form of ``data``."""
        return stored == data

    def ref_path(self, name: str) -> Path:
        *directories, last = name.split("/")
        return self.root.joinpath("refs", *(f"{part}.d" for part in directories), f"{last}.json")

    def stored_blob_files(self) -> list[Path]:
        return sorted(path for path in (self.root / "blobs").rglob("*") if path.is_file())

    def damage_blob(self, ref: BlobRef, damage: BlobDamage) -> None:
        path = self.blob_path(ref)
        if damage == "flipped-byte":
            data = bytearray(path.read_bytes())
            data[0] ^= 0xFF
            path.write_bytes(bytes(data))
        elif damage == "truncated":
            os.truncate(path, ref.size - 1)
        elif damage == "extended":
            with path.open("ab") as stream:
                stream.write(b"\0")
        elif damage == "symlink":
            copy = self.scratch / f"copy-{ref.sha256}"
            shutil.copy2(path, copy)
            path.unlink()
            path.symlink_to(copy)
        elif damage == "hard-link":
            os.link(path, self.scratch / f"link-{ref.sha256}")
        elif damage == "group-readable":
            path.chmod(0o640)
        elif damage == "fifo":
            path.unlink()
            os.mkfifo(path, 0o600)
        else:
            path.unlink()
            # A socket path must be short, so bind the bare name from inside its directory.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server, chdir(path.parent):
                server.bind(path.name)
            path.chmod(0o600)

    def corrupt_ref(self, name: str, corruption: RefCorruption) -> None:
        path = self.ref_path(name)
        value = RefValue.model_validate_json(path.read_bytes())
        if corruption == "garbage":
            payload = b"{not a ref value}\n"
        elif corruption == "oversized":
            payload = canonical_json_bytes(value) + b" " * 4096 + b"\n"
        elif corruption == "another-name":
            other = value.model_copy(update={"name": library_head_ref("another-library")})
            payload = canonical_json_bytes(other) + b"\n"
        else:
            payload = json.dumps(value.model_dump(mode="json"), indent=1).encode() + b"\n"
        path.write_bytes(payload)

    def loosen_backing(self, problem: BackingProblem) -> Callable[[], bool]:
        root = self.root
        shard = next((root / "blobs").iterdir())
        blob = next(shard.iterdir())
        if problem in {"root-group-bits", "shard-group-bits", "group-readable-temporary-lock"}:
            path = {"root-group-bits": root, "shard-group-bits": shard}.get(
                problem, root / ".tmp.lock"
            )
            path.chmod(0o750 if path.is_dir() else 0o640)
            return lambda: bool(stat.S_IMODE(path.stat().st_mode) & 0o077)
        if problem in {"unreadable-shard", "unreadable-blob"}:
            path = shard if problem == "unreadable-shard" else blob
            path.chmod(0)
            return lambda: stat.S_IMODE(path.stat().st_mode) == 0
        if problem in {"removed-root", "replaced-root"}:
            root.rename(self.scratch / problem)
            if problem == "replaced-root":
                root.mkdir(mode=0o700)
                return lambda: not (root / "blobs").exists()
            return lambda: not root.exists()
        if problem in {"removed-refs-directory", "removed-ref-lock"}:
            path = root / ("refs" if problem == "removed-refs-directory" else ".refs.lock")
            path.rename(self.scratch / problem)
            return lambda: not path.exists()
        if problem in {"foreign-root-entry", "foreign-temporary-entry"}:
            path = root / "notes" if problem == "foreign-root-entry" else root / "tmp" / "notes"
            path.mkdir(mode=0o700)
            return path.exists
        if problem == "symlinked-root":
            moved = self.scratch / "moved-root"
            root.rename(moved)
            root.symlink_to(moved)
            return root.is_symlink
        directory = shard if problem == "symlinked-shard" else root / "refs/libraries.d"
        moved = self.scratch / f"moved-{directory.name}"
        shutil.copytree(directory, moved, symlinks=True)
        shutil.rmtree(directory)
        directory.symlink_to(moved)
        return directory.is_symlink

    def leave_crash_leftovers(self) -> CrashLeftovers:
        dead_put = b"a writer died after writing this"
        orphan = b"a temporary no writer holds"
        dead_swap = library_head_ref("dead-swapper")
        # Both writers stay alive until both hold a temporary, so the second one's store
        # construction finds a writer active and sweeps nothing; then both die at once.
        die = CONTEXT.Event()
        putting = CONTEXT.Event()
        swapping = CONTEXT.Event()
        putter = start_child(_die_while_putting, self, dead_put, putting, die)
        assert putting.wait(timeout=CHILD_TIMEOUT_SECONDS), "the dying putter never paused"
        swapper = start_child(_die_while_swapping, self, dead_swap, swapping, die)
        assert swapping.wait(timeout=CHILD_TIMEOUT_SECONDS), "the dying swapper never paused"
        die.set()
        finish(putter, exitcode=17)
        finish(swapper, exitcode=18)
        temporary = self.root / "tmp" / f"99999-{'a' * 32}.tmp"
        temporary.write_bytes(orphan)
        temporary.chmod(0o600)
        stored = [leftover.read_bytes() for leftover in sorted((self.root / "tmp").iterdir())]
        assert orphan in stored
        assert any(self.stored_form_holds(item, dead_put) for item in stored)
        return CrashLeftovers(
            blobs=tuple(digest_ref(item) for item in stored), ref_names=(dead_swap,)
        )

    def leftover_temporaries(self) -> tuple[str, ...]:
        return tuple(sorted(os.listdir(self.root / "tmp")))

    @contextmanager
    def paused_put(self, data: bytes) -> Iterator[PausedPut]:
        paused = CONTEXT.Event()
        resume = CONTEXT.Event()
        results = CONTEXT.Queue()
        child = start_child(_put_paused_in_child, self, data, paused, resume, results)
        outcome = PausedPut(data)
        try:
            assert paused.wait(timeout=CHILD_TIMEOUT_SECONDS), "the writer never paused"
            yield outcome
        except BaseException:
            child.kill()
            child.join()
            raise
        resume.set()
        outcome.outcome = results.get(timeout=CHILD_TIMEOUT_SECONDS)
        finish(child)


def restore_access(base: Path) -> None:
    """Give back the owner access a damage hook took, so the temporary tree can be removed."""
    for directory, names, files in os.walk(base):
        for name in names + files:
            path = Path(directory, name)
            if not path.is_symlink() and not stat.S_IMODE(path.stat().st_mode) & 0o700:
                path.chmod(0o700 if path.is_dir() else 0o600)


@contextmanager
def local_spec(base: Path) -> Iterator[LocalSpec]:
    made = LocalSpec(root=base / "local-store", scratch=base / "local-scratch")
    made.scratch.mkdir(mode=0o700)
    # Both stores share the root; the second creation finds the layout already made.
    assert LocalBlobStore(made.root, create=True).root == made.root
    assert LocalRefStore(made.root, create=True).root == made.root
    try:
        yield made
    finally:
        restore_access(base)


# -- the sealed spec over the local store --------------------------------------------------

SEALED_LIBRARY = "conformance-library"
# A fixed synthetic key, never a real one: specs are pickled into children, keys are not.
SEALED_TEST_KEY = bytes(range(0x40, 0x60))


def sealed_test_key() -> LibraryKey:
    return LibraryKey(SEALED_TEST_KEY)


@dataclass(frozen=True)
class SealedLocalSpec(LocalSpec):
    """``SealedBlobStore`` over ``LocalBlobStore``, and ``LocalRefStore``, on one directory.

    Refs name ciphertext, so the damage hooks act on the local store's ciphertext files.
    """

    name: ClassVar[str] = "sealed-local"
    deterministic_refs: ClassVar[bool] = False
    max_put_bytes: ClassVar[int] = MAX_SEALED_PLAINTEXT_BYTES

    def open_blob_store(self) -> BlobStore:
        return SealedBlobStore(
            LocalBlobStore(self.root), key=sealed_test_key(), library_id=SEALED_LIBRARY
        )

    def stored_form_holds(self, stored: bytes, data: bytes) -> bool:
        try:
            opened = open_sealed_blob(sealed_test_key(), SEALED_LIBRARY, stored, label="leftover")
        except StorageIntegrityError:
            return False
        return opened == data


@contextmanager
def sealed_local_spec(base: Path) -> Iterator[SealedLocalSpec]:
    with local_spec(base) as local:
        yield SealedLocalSpec(root=local.root, scratch=local.scratch)


# -- the HTTP specs over a loopback vault server ------------------------------------------

HTTP_NAMESPACE = "conformance"
# Room for every blob a check stores, including the 16 MiB one, with nothing to spare for a
# quota check: the server's own tests cover the quota.
HTTP_QUOTA_BYTES = 256 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 60.0
# The servers this process runs, by port, so a spec can reach the one behind it. A spawned
# child finds none: it is a client of the parent's server and opens no store of its own.
SERVERS: dict[int, VaultServer] = {}


def no_sleep(_seconds: float) -> None:
    """The client's backoff, for a spec that must not spend real time on a retry."""


@dataclass(frozen=True)
class RefusingStore:
    """The stores of a namespace whose backing refused to be opened again.

    A server whose backing has been damaged under it cannot build its stores, and every
    request it is given then fails with what the backing refused. Standing in for both stores
    keeps that refusal behind the server, where a client sees the vault's own sanitized
    answer instead of a refusal raised in the test process.
    """

    error: StorageError

    def put(self, data: bytes) -> BlobRef:
        raise self.error

    def has(self, ref: BlobRef) -> bool:
        raise self.error

    def get(self, ref: BlobRef) -> bytes:
        raise self.error

    def read(self, name: str) -> RefValue | None:
        raise self.error

    def swap(self, name: str, *, expected_version: int | None, target: BlobRef) -> RefValue:
        raise self.error


@dataclass(frozen=True)
class HttpSpec(LocalSpec):
    """``HttpBlobStore`` and ``HttpRefStore`` against a vault server on the loopback.

    The server keeps the local stores of one namespace, so the damage and inspection hooks are
    the local ones acting on its backing directory. Opening a store here also builds the
    server's stores again, which is what makes a damaged backing visible to a fresh store (and
    what sweeps crash leftovers); a refusal names paths on the server, so it never reaches the
    client (capability: errors_name_backing_paths). It pickles into a spawned child as the
    port and the token string, never as a connection or a server.
    """

    port: int
    token: str
    namespace: str = HTTP_NAMESPACE
    name: ClassVar[str] = "http"
    errors_name_backing_paths: ClassVar[bool] = False

    def client(self) -> VaultConnection:
        return VaultConnection.loopback_for_tests(
            self.port,
            self.namespace,
            lambda: self.token,
            timeout=HTTP_TIMEOUT_SECONDS,
            sleep=no_sleep,
        )

    def reopen_server_stores(self) -> None:
        """Build the server's stores again, as opening a local store would; a child has none.

        A rebuild the backing refuses is left for the server to raise, not raised here: both
        stores become ``RefusingStore``s, so every operation on a store opened afterwards
        crosses the wire and comes back as the vault's own answer. Without that, a problem no
        already-open store can see (``foreign-root-entry``, ``foreign-temporary-entry``) would
        be checked entirely inside this process, against no client and no server at all.
        """
        server = SERVERS.get(self.port)
        if server is None:
            return
        namespace = server.namespaces[self.namespace]
        blobs: Any
        refs: Any
        try:
            blobs = LocalBlobStore(self.root)
            refs = LocalRefStore(self.root)
        except StorageError as error:
            # One refusal stands for both: the rebuild is all of the namespace or none of it.
            blobs = refs = RefusingStore(error)
        namespace.blobs = blobs
        namespace.refs = refs

    def open_blob_store(self) -> BlobStore:
        self.reopen_server_stores()
        return self.client().blob_store()

    def open_ref_store(self) -> RefStore:
        self.reopen_server_stores()
        return self.client().ref_store()

    def leave_crash_leftovers(self) -> CrashLeftovers:
        """What a vault server that died mid-put and mid-swap leaves in its backing."""
        return LocalSpec(root=self.root, scratch=self.scratch).leave_crash_leftovers()

    @contextmanager
    def paused_put(self, data: bytes) -> Iterator[PausedPut]:
        """A put from another process, held in the server's worker after its temporary."""
        paused = threading.Event()
        resume = threading.Event()
        results = CONTEXT.Queue()
        with paused_publish(paused, resume):
            child = start_child(_put_in_child_at_once, self, data, results)
            outcome = PausedPut(data)
            try:
                assert paused.wait(timeout=CHILD_TIMEOUT_SECONDS), "the writer never paused"
                yield outcome
            except BaseException:
                resume.set()
                child.kill()
                child.join()
                raise
            resume.set()
            outcome.outcome = results.get(timeout=CHILD_TIMEOUT_SECONDS)
            finish(child)


@dataclass(frozen=True)
class SealedHttpSpec(HttpSpec):
    """``SealedBlobStore`` over the HTTP blob store: what the Mac really pushes to a vault."""

    name: ClassVar[str] = "sealed-http"
    deterministic_refs: ClassVar[bool] = False
    max_put_bytes: ClassVar[int] = MAX_SEALED_PLAINTEXT_BYTES

    def open_blob_store(self) -> BlobStore:
        return SealedBlobStore(
            super().open_blob_store(), key=sealed_test_key(), library_id=SEALED_LIBRARY
        )


@contextmanager
def paused_publish(paused: threading.Event, resume: threading.Event) -> Iterator[None]:
    """Hold the next blob publish in this process (a server worker's) until ``resume``.

    Only the first publish waits, so the writer that is meant to win the race is not held
    behind the one that is meant to lose it.
    """
    publish = storage_local.rename_no_replace_at
    armed = threading.Event()
    armed.set()
    lock = threading.Lock()

    def pause_then_publish(
        source_directory: int, source: str, target_directory: int, target: str
    ) -> None:
        with lock:
            first = armed.is_set()
            armed.clear()
        if first:
            paused.set()
            if not resume.wait(timeout=CHILD_TIMEOUT_SECONDS):
                raise TimeoutError("the test never resumed the paused writer")
        publish(source_directory, source, target_directory, target)

    storage_local.rename_no_replace_at = pause_then_publish
    try:
        yield
    finally:
        resume.set()
        storage_local.rename_no_replace_at = publish


@contextmanager
def loopback_vault(base: Path) -> Iterator[tuple[Path, int, str]]:
    """One vault server on the loopback with fresh tokens; yields its namespace root and port."""
    read_token, write_token = new_token(), new_token()
    config = VaultServerConfig(
        namespaces=(
            VaultNamespaceConfig(
                name=HTTP_NAMESPACE,
                read_token_sha256=hash_token(read_token),
                write_token_sha256=hash_token(write_token),
                quota_bytes=HTTP_QUOTA_BYTES,
            ),
        ),
        max_workers=8,
        request_timeout_seconds=int(HTTP_TIMEOUT_SECONDS),
    )
    data_root = base / "vault-data"
    server = VaultServer(
        (LOOPBACK_HOST, 0),
        config=config,
        data_root=data_root,
        access_log=base / "vault-access.jsonl",
        provision=True,
    )
    port = int(server.server_address[1])
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    SERVERS[port] = server
    try:
        yield data_root / NAMESPACES_DIRECTORY / HTTP_NAMESPACE, port, write_token
    finally:
        SERVERS.pop(port, None)
        server.shutdown()
        thread.join(timeout=CHILD_TIMEOUT_SECONDS)
        server.server_close()


@contextmanager
def http_spec(base: Path) -> Iterator[HttpSpec]:
    scratch = base / "http-scratch"
    scratch.mkdir(mode=0o700)
    try:
        with loopback_vault(base) as (root, port, token):
            yield HttpSpec(root=root, scratch=scratch, port=port, token=token)
    finally:
        restore_access(base)


@contextmanager
def sealed_http_spec(base: Path) -> Iterator[SealedHttpSpec]:
    with http_spec(base) as plain:
        yield SealedHttpSpec(
            root=plain.root, scratch=plain.scratch, port=plain.port, token=plain.token
        )


@dataclass(frozen=True)
class SpecFactory:
    """A named way to build a spec in a private base directory (and tear it down after)."""

    id: str
    make: Callable[[Path], AbstractContextManager[Any]] = field(repr=False)


SPECS: list[SpecFactory] = [
    SpecFactory("local", local_spec),
    SpecFactory("sealed-local", sealed_local_spec),
    SpecFactory("http", http_spec),
    SpecFactory("sealed-http", sealed_http_spec),
]


@pytest.fixture(params=SPECS, ids=lambda factory: factory.id)
def spec(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StorageSpec]:
    base = Path(os.path.realpath(tmp_path)) / "spec"
    base.mkdir(mode=0o700)
    with request.param.make(base) as made:
        yield made


def backing_root(spec: StorageSpec) -> Path:
    if spec.backing_root is None:
        pytest.skip(f"{spec.name} has no backing files (capability: backing_root)")
    return spec.backing_root


def require_deterministic_refs(spec: StorageSpec) -> None:
    if not spec.deterministic_refs:
        pytest.skip(f"{spec.name} gives equal bytes new refs (capability: deterministic_refs)")


def require_random_refs(spec: StorageSpec) -> None:
    if spec.deterministic_refs:
        pytest.skip(f"{spec.name} gives equal bytes one ref (capability: deterministic_refs)")


def expected_refusals(spec: StorageSpec, operations: frozenset[Operation]) -> frozenset[Operation]:
    """The operations ``spec`` must refuse when ``REFUSALS`` names ``operations``.

    ``put`` stores bytes that are already stored, so a store with deterministic refs reads the
    stored blob. A store with random refs (capability: deterministic_refs) stores every put as
    new bytes, as ``put-new`` does, so its ``put`` must fail exactly where ``put-new`` must.
    """
    if spec.deterministic_refs or "put-new" in operations:
        return operations
    return operations - {"put"}


def require_backing_paths_in_errors(spec: StorageSpec) -> Path:
    root = backing_root(spec)
    if not spec.errors_name_backing_paths:
        pytest.skip(
            f"{spec.name} keeps its backing paths out of errors "
            "(capability: errors_name_backing_paths)"
        )
    return root


def digest_ref(data: bytes) -> BlobRef:
    return BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def storage_failure(
    code: str, action: Callable[[], object], *, integrity: bool = False
) -> StorageError:
    error_class = StorageIntegrityError if integrity else StorageError
    with pytest.raises(error_class) as caught:
        action()
    assert caught.value.code == code, str(caught.value)
    assert caught.value.retryable is (code in {"STORAGE.UNAVAILABLE", "STORAGE.TIMEOUT"})
    return caught.value


def stored_blob_identities(spec: StorageSpec) -> dict[Path, tuple[int, int, int, int]]:
    """Mode, inode, size and modification time of every stored blob file."""
    return {
        path: (metadata.st_mode, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        for path in spec.stored_blob_files()
        for metadata in (path.lstat(),)
    }


def backing_snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.lstat().st_mode,
            path.lstat().st_ino,
            path.lstat().st_size,
        )
        for path in sorted(root.rglob("*"))
    }


# -- child processes ----------------------------------------------------------------------


def start_child(target: Callable[..., None], *args: object) -> Any:
    child = CONTEXT.Process(target=target, args=args, daemon=True)
    child.start()
    return child


def finish(child: Any, *, exitcode: int = 0) -> None:
    child.join(timeout=CHILD_TIMEOUT_SECONDS)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail(f"child process {child.name} did not finish")
    assert child.exitcode == exitcode


def report(results: Any, action: Callable[[], str]) -> None:
    """Put ``("ok", payload, "")`` or ``(status, detail, message)`` on ``results``."""
    try:
        payload = action()
    except StorageError as error:
        results.put(("storage-error", error.code, str(error)))
    except Exception as error:  # the parent fails the check with this report
        results.put(("error", type(error).__name__, str(error)))
    else:
        results.put(("ok", payload, ""))


def collect(results: Any, children: list[Any]) -> list[tuple[str, str, str]]:
    outcomes = [results.get(timeout=CHILD_TIMEOUT_SECONDS) for _ in children]
    for child in children:
        finish(child)
    return outcomes


def _put_in_child(spec: StorageSpec, data: bytes, barrier: Any, results: Any) -> None:
    def action() -> str:
        store = spec.open_blob_store()
        barrier.wait(timeout=CHILD_TIMEOUT_SECONDS)
        return store.put(data).model_dump_json()

    report(results, action)


def _swap_in_child(
    spec: StorageSpec, name: str, target: BlobRef, barrier: Any, results: Any
) -> None:
    def action() -> str:
        store = spec.open_ref_store()
        barrier.wait(timeout=CHILD_TIMEOUT_SECONDS)
        return store.swap(name, expected_version=1, target=target).model_dump_json()

    report(results, action)


def _swap_repeatedly_in_child(
    spec: StorageSpec, name: str, targets: list[BlobRef], ready: Any, done: Any, results: Any
) -> None:
    def action() -> str:
        try:
            store = spec.open_ref_store()
            ready.set()
            version = 1
            for target in targets:
                version = store.swap(name, expected_version=version, target=target).version
            return str(version)
        finally:
            done.set()

    report(results, action)


def _put_paused_in_child(
    spec: StorageSpec, data: bytes, paused: Any, resume: Any, results: Any
) -> None:
    publish = storage_local.rename_no_replace_at

    def pause_then_publish(
        source_directory: int, source: str, target_directory: int, target: str
    ) -> None:
        paused.set()
        if not resume.wait(timeout=CHILD_TIMEOUT_SECONDS):
            raise TimeoutError("the parent never resumed the paused writer")
        publish(source_directory, source, target_directory, target)

    storage_local.rename_no_replace_at = pause_then_publish
    report(results, lambda: spec.open_blob_store().put(data).model_dump_json())


def _put_in_child_at_once(spec: StorageSpec, data: bytes, results: Any) -> None:
    """Put ``data`` from another process, with nothing to wait for but the store itself."""
    report(results, lambda: spec.open_blob_store().put(data).model_dump_json())


def _die_on_signal(paused: Any, die: Any, exitcode: int) -> None:
    paused.set()
    die.wait(timeout=CHILD_TIMEOUT_SECONDS)
    os._exit(exitcode)


def _die_while_putting(spec: StorageSpec, data: bytes, paused: Any, die: Any) -> None:
    """Write the temporary of ``put(data)``, then die before publishing it (exit 17)."""
    storage_local.rename_no_replace_at = lambda *_arguments: _die_on_signal(paused, die, 17)
    spec.open_blob_store().put(data)


def _die_while_swapping(spec: StorageSpec, name: str, paused: Any, die: Any) -> None:
    """Write the temporary of a new ref ``name``, then die before replacing (exit 18)."""
    target = spec.open_blob_store().put(b"the target of a swap that never happened")
    store = spec.open_ref_store()
    os.replace = lambda *_args, **_kwargs: _die_on_signal(paused, die, 18)
    store.swap(name, expected_version=None, target=target)


# -- blobs --------------------------------------------------------------------------------


def test_put_returns_a_ref_that_get_reads_back(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    ref = blobs.put(PAYLOAD)
    other = blobs.put(PAYLOAD + b"!")
    assert ref != other
    assert blobs.get(ref) == PAYLOAD
    assert blobs.get(other) == PAYLOAD + b"!"
    assert spec.open_blob_store().get(ref) == PAYLOAD


def test_a_ref_is_the_digest_and_size_of_the_stored_bytes(spec: StorageSpec) -> None:
    require_deterministic_refs(spec)
    blobs = spec.open_blob_store()
    assert blobs.put(PAYLOAD) == blobs.put(PAYLOAD) == digest_ref(PAYLOAD)
    assert spec.open_blob_store().put(PAYLOAD) == digest_ref(PAYLOAD)
    assert blobs.put(b"") == digest_ref(b"")


def test_putting_the_same_bytes_twice_is_harmless(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    first = blobs.put(PAYLOAD)
    second = blobs.put(PAYLOAD)
    assert blobs.get(first) == blobs.get(second) == PAYLOAD
    assert spec.open_blob_store().get(first) == PAYLOAD


def test_equal_bytes_are_stored_once(spec: StorageSpec) -> None:
    backing_root(spec)
    require_deterministic_refs(spec)
    blobs = spec.open_blob_store()
    blobs.put(PAYLOAD)
    blobs.put(PAYLOAD)
    spec.open_blob_store().put(PAYLOAD)
    assert len(spec.stored_blob_files()) == 1


def test_equal_bytes_get_new_refs(spec: StorageSpec) -> None:
    require_random_refs(spec)
    blobs = spec.open_blob_store()
    first = blobs.put(PAYLOAD)
    second = blobs.put(PAYLOAD)
    assert first != second
    assert blobs.get(first) == blobs.get(second) == PAYLOAD


def test_empty_bytes_are_a_blob(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    ref = blobs.put(b"")
    assert blobs.has(ref)
    assert blobs.get(ref) == b""


def test_the_largest_blob_is_accepted_and_one_byte_more_is_refused(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    largest = b"\x5a" * spec.max_put_bytes
    ref = blobs.put(largest)
    assert blobs.get(ref) == largest
    error = storage_failure("STORAGE.TOO_LARGE", lambda: blobs.put(largest + b"\x5a"))
    assert str(spec.max_put_bytes) in str(error)


def test_an_oversized_put_changes_nothing_behind_the_store(spec: StorageSpec) -> None:
    root = backing_root(spec)
    blobs = spec.open_blob_store()
    blobs.put(PAYLOAD)
    before = backing_snapshot(root)
    storage_failure("STORAGE.TOO_LARGE", lambda: blobs.put(b"\x5a" * (spec.max_put_bytes + 1)))
    assert backing_snapshot(root) == before


def test_has_is_true_for_a_stored_ref(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    ref = blobs.put(PAYLOAD)
    assert blobs.has(ref)
    assert spec.open_blob_store().has(ref)


def test_has_is_false_until_put_and_true_after(spec: StorageSpec) -> None:
    require_deterministic_refs(spec)
    blobs = spec.open_blob_store()
    assert not blobs.has(digest_ref(PAYLOAD))
    blobs.put(PAYLOAD)
    assert blobs.has(digest_ref(PAYLOAD))
    assert spec.open_blob_store().has(digest_ref(PAYLOAD))


def test_a_ref_that_was_never_stored_is_not_found(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    stored = blobs.put(PAYLOAD)
    # One digest shares the stored blob's first two hex digits, the other shares nothing.
    neighbour = stored.sha256[:2] + "0" * 62
    stranger = ("1" if stored.sha256[0] == "0" else "0") * 64
    for sha256 in (neighbour, stranger):
        missing = BlobRef(sha256=sha256, size=7)
        assert not blobs.has(missing)
        error = storage_failure("STORAGE.NOT_FOUND", lambda ref=missing: blobs.get(ref))
        assert sha256 in str(error)


def test_a_ref_of_the_wrong_size_is_not_the_stored_blob(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    ref = blobs.put(PAYLOAD)
    wrong = BlobRef(sha256=ref.sha256, size=ref.size + 1)
    assert not blobs.has(wrong)
    storage_failure("STORAGE.INTEGRITY", lambda: blobs.get(wrong), integrity=True)
    assert blobs.get(ref) == PAYLOAD


@pytest.mark.parametrize("forged", list(FORGED_REFS))
def test_a_ref_that_skipped_validation_is_refused_and_changes_nothing(
    spec: StorageSpec, forged: str
) -> None:
    ref = FORGED_REFS[forged]
    blobs = spec.open_blob_store()
    refs = spec.open_ref_store()
    current = refs.swap(HEAD, expected_version=None, target=blobs.put(PAYLOAD))
    storage_failure("STORAGE.PROTOCOL", lambda: blobs.get(ref))
    storage_failure("STORAGE.PROTOCOL", lambda: blobs.has(ref))
    storage_failure("STORAGE.PROTOCOL", lambda: refs.swap(HEAD, expected_version=1, target=ref))
    storage_failure(
        "STORAGE.PROTOCOL", lambda: refs.swap("fresh", expected_version=None, target=ref)
    )
    # A swap that stored such a target would leave a ref that neither read nor swap accepts.
    assert refs.read(HEAD) == current
    assert refs.read("fresh") is None
    assert refs.swap(HEAD, expected_version=1, target=current.target).version == 2


@pytest.mark.parametrize("damage", get_args(BlobDamage.__value__))
def test_a_damaged_stored_blob_is_refused_and_never_replaced(
    spec: StorageSpec, damage: BlobDamage
) -> None:
    root = backing_root(spec)
    blobs = spec.open_blob_store()
    ref = blobs.put(PAYLOAD)
    spec.damage_blob(ref, damage)
    before = backing_snapshot(root)
    storage_failure("STORAGE.INTEGRITY", lambda: blobs.get(ref), integrity=True)
    storage_failure("STORAGE.INTEGRITY", lambda: spec.open_blob_store().get(ref), integrity=True)
    if damage in {"flipped-byte", "truncated", "extended"}:
        # A private regular file: the metadata answer is its size, the proof is get.
        assert blobs.has(ref) is (damage == "flipped-byte")
    else:
        storage_failure("STORAGE.INTEGRITY", lambda: blobs.has(ref), integrity=True)
    assert backing_snapshot(root) == before


@pytest.mark.parametrize("damage", get_args(BlobDamage.__value__))
def test_a_put_of_a_damaged_blobs_bytes_is_refused_and_replaces_nothing(
    spec: StorageSpec, damage: BlobDamage
) -> None:
    root = backing_root(spec)
    require_deterministic_refs(spec)
    blobs = spec.open_blob_store()
    ref = blobs.put(PAYLOAD)
    spec.damage_blob(ref, damage)
    before = backing_snapshot(root)
    storage_failure("STORAGE.INTEGRITY", lambda: blobs.put(PAYLOAD), integrity=True)
    storage_failure(
        "STORAGE.INTEGRITY", lambda: spec.open_blob_store().put(PAYLOAD), integrity=True
    )
    assert backing_snapshot(root) == before


def put_concurrently(spec: StorageSpec, data: bytes) -> set[BlobRef]:
    """Put ``data`` from ``WRITERS`` processes at once; every writer must succeed."""
    barrier = CONTEXT.Barrier(WRITERS)
    results = CONTEXT.Queue()
    children = [start_child(_put_in_child, spec, data, barrier, results) for _ in range(WRITERS)]
    outcomes = collect(results, children)
    assert [status for status, _, _ in outcomes] == ["ok"] * WRITERS, outcomes
    return {BlobRef.model_validate_json(payload) for _, payload, _ in outcomes}


def concurrent_payload(label: bytes) -> bytes:
    return hashlib.sha256(label).digest() * (1024 * 1024 // 32)


def test_concurrent_writer_processes_putting_the_same_bytes_agree(spec: StorageSpec) -> None:
    data = concurrent_payload(b"agree")
    refs = put_concurrently(spec, data)
    blobs = spec.open_blob_store()
    assert all(blobs.get(ref) == data for ref in refs)


def test_concurrent_writers_of_the_same_bytes_leave_one_copy_and_no_temporary(
    spec: StorageSpec,
) -> None:
    backing_root(spec)
    require_deterministic_refs(spec)
    data = concurrent_payload(b"one copy")
    assert put_concurrently(spec, data) == {digest_ref(data)}
    assert len(spec.stored_blob_files()) == 1
    assert spec.leftover_temporaries() == ()


def test_a_writer_that_loses_the_publish_race_returns_the_stored_blob(spec: StorageSpec) -> None:
    backing_root(spec)
    require_deterministic_refs(spec)
    data = b"two writers, one blob"
    with spec.paused_put(data) as loser:
        # The paused writer checked that nothing was stored and wrote its temporary.
        winner = spec.open_blob_store().put(data)
        stored = stored_blob_identities(spec)
    assert loser.ref == winner
    # The loser neither replaced nor rewrote the winner's file.
    assert len(stored) == 1 and stored_blob_identities(spec) == stored
    assert spec.open_blob_store().get(winner) == data
    assert spec.leftover_temporaries() == ()


def test_a_writer_that_loses_the_publish_race_to_a_damaged_blob_is_refused(
    spec: StorageSpec,
) -> None:
    backing_root(spec)
    require_deterministic_refs(spec)
    data = b"two writers, one damaged blob"
    with spec.paused_put(data) as loser:
        winner = spec.open_blob_store().put(data)
        spec.damage_blob(winner, "flipped-byte")
        stored = stored_blob_identities(spec)
    assert loser.outcome is not None
    assert loser.outcome[:2] == ("storage-error", "STORAGE.INTEGRITY"), loser.outcome
    # The damaged winner is kept as it is: the loser neither repaired nor replaced it.
    assert stored_blob_identities(spec) == stored
    storage_failure("STORAGE.INTEGRITY", lambda: spec.open_blob_store().get(winner), integrity=True)
    assert spec.leftover_temporaries() == ()


# -- refs ---------------------------------------------------------------------------------


def test_a_missing_ref_reads_as_none(spec: StorageSpec) -> None:
    refs = spec.open_ref_store()
    assert refs.read(HEAD) is None
    refs.swap(HEAD, expected_version=None, target=spec.open_blob_store().put(PAYLOAD))
    assert refs.read(library_head_ref("another-library")) is None
    assert refs.read("libraries") is None
    assert refs.read(f"{HEAD}/deeper") is None


def test_versions_count_up_from_one_and_read_returns_the_latest(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    refs = spec.open_ref_store()
    targets = [blobs.put(PAYLOAD + bytes([number])) for number in range(3)]
    expected: int | None = None
    for number, target in enumerate(targets, start=1):
        value = refs.swap(HEAD, expected_version=expected, target=target)
        assert (value.name, value.version, value.target) == (HEAD, number, target)
        assert value.updated_at.utcoffset() == timedelta(0)
        assert refs.read(HEAD) == value
        assert spec.open_ref_store().read(HEAD) == value
        expected = value.version


def test_refs_with_shared_prefixes_are_independent(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    refs = spec.open_ref_store()
    names = ("libraries", "libraries.json/head", "libraries/head.json", "libraries/head", HEAD)
    values = {
        name: refs.swap(name, expected_version=None, target=blobs.put(name.encode()))
        for name in names
    }
    for name, value in values.items():
        assert refs.read(name) == value
        assert blobs.get(value.target) == name.encode()


def test_a_stale_or_absent_expected_version_conflicts(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    refs = spec.open_ref_store()
    first = blobs.put(PAYLOAD)
    second = blobs.put(PAYLOAD + b"2")
    storage_failure("STORAGE.CONFLICT", lambda: refs.swap(HEAD, expected_version=1, target=first))
    assert refs.read(HEAD) is None
    one = refs.swap(HEAD, expected_version=None, target=first)
    two = refs.swap(HEAD, expected_version=1, target=second)
    for expected in (None, 1, 3):
        error = storage_failure(
            "STORAGE.CONFLICT",
            lambda version=expected: refs.swap(HEAD, expected_version=version, target=first),
        )
        assert HEAD in str(error)
    assert refs.read(HEAD) == two != one


def test_concurrent_swaps_from_one_version_have_exactly_one_winner(spec: StorageSpec) -> None:
    blobs = spec.open_blob_store()
    refs = spec.open_ref_store()
    refs.swap(HEAD, expected_version=None, target=blobs.put(PAYLOAD))
    targets = [blobs.put(PAYLOAD + bytes([number])) for number in range(WRITERS)]
    barrier = CONTEXT.Barrier(WRITERS)
    results = CONTEXT.Queue()
    children = [
        start_child(_swap_in_child, spec, HEAD, target, barrier, results) for target in targets
    ]
    outcomes = collect(results, children)
    winners = [payload for status, payload, _ in outcomes if status == "ok"]
    losers = [payload for status, payload, _ in outcomes if status != "ok"]
    assert len(winners) == 1, outcomes
    assert losers == ["STORAGE.CONFLICT"] * (WRITERS - 1), outcomes
    current = refs.read(HEAD)
    assert current is not None and current.model_dump_json() == winners[0]
    assert current.version == 2


def test_lock_free_readers_see_only_whole_values_while_a_writer_swaps(
    spec: StorageSpec,
) -> None:
    blobs = spec.open_blob_store()
    refs = spec.open_ref_store()
    refs.swap(HEAD, expected_version=None, target=blobs.put(PAYLOAD))
    targets = [blobs.put(PAYLOAD + bytes([number % 7])) for number in range(200)]
    ready = CONTEXT.Event()
    done = CONTEXT.Event()
    results = CONTEXT.Queue()
    child = start_child(_swap_repeatedly_in_child, spec, HEAD, targets, ready, done, results)
    assert ready.wait(timeout=CHILD_TIMEOUT_SECONDS)
    last = 1
    reads = 0
    while not done.is_set():
        value = refs.read(HEAD)
        assert value is not None and value.version >= last
        last = value.version
        reads += 1
    assert collect(results, [child]) == [("ok", str(len(targets) + 1), "")]
    final = refs.read(HEAD)
    assert final is not None and final.version == len(targets) + 1 and reads > 0


@pytest.mark.parametrize("corruption", get_args(RefCorruption.__value__))
def test_a_corrupted_ref_value_is_refused(spec: StorageSpec, corruption: RefCorruption) -> None:
    backing_root(spec)
    refs = spec.open_ref_store()
    target = spec.open_blob_store().put(PAYLOAD)
    refs.swap(HEAD, expected_version=None, target=target)
    spec.corrupt_ref(HEAD, corruption)
    storage_failure("STORAGE.INTEGRITY", lambda: refs.read(HEAD), integrity=True)
    storage_failure(
        "STORAGE.INTEGRITY",
        lambda: refs.swap(HEAD, expected_version=1, target=target),
        integrity=True,
    )


@pytest.mark.parametrize("name", BAD_REF_NAMES)
def test_an_invalid_ref_name_is_refused(spec: StorageSpec, name: str) -> None:
    refs = spec.open_ref_store()
    target = spec.open_blob_store().put(PAYLOAD)
    with pytest.raises(StorageNameError):
        refs.read(name)
    with pytest.raises(StorageNameError):
        refs.swap(name, expected_version=None, target=target)


def test_an_invalid_ref_name_is_refused_before_any_io(spec: StorageSpec) -> None:
    root = backing_root(spec)
    refs = spec.open_ref_store()
    target = spec.open_blob_store().put(PAYLOAD)
    before = backing_snapshot(root)
    for name in BAD_REF_NAMES:
        with pytest.raises(StorageNameError):
            refs.read(name)
        with pytest.raises(StorageNameError):
            refs.swap(name, expected_version=None, target=target)
    assert backing_snapshot(root) == before


# -- crashes, privacy and the backing directory ------------------------------------------


def test_crash_leftovers_are_swept_and_never_visible(spec: StorageSpec) -> None:
    backing_root(spec)
    blobs = spec.open_blob_store()
    refs = spec.open_ref_store()
    leftovers = spec.leave_crash_leftovers()
    assert len(spec.leftover_temporaries()) == 3

    def invisible(blob_store: BlobStore, ref_store: RefStore) -> None:
        for ref in leftovers.blobs:
            assert not blob_store.has(ref)
            storage_failure("STORAGE.NOT_FOUND", lambda ref=ref: blob_store.get(ref))
        for name in leftovers.ref_names:
            assert ref_store.read(name) is None

    invisible(blobs, refs)
    fresh_blobs = spec.open_blob_store()
    assert spec.leftover_temporaries() == ()
    fresh_refs = spec.open_ref_store()
    invisible(fresh_blobs, fresh_refs)
    ref = fresh_blobs.put(PAYLOAD)
    for name in leftovers.ref_names:
        assert fresh_refs.swap(name, expected_version=None, target=ref).version == 1


def test_opening_a_store_never_disturbs_a_live_writer(spec: StorageSpec) -> None:
    backing_root(spec)
    target = spec.open_blob_store().put(PAYLOAD)
    with spec.paused_put(b"a live writer's bytes") as live:
        held = spec.leftover_temporaries()
        assert len(held) == 1
        spec.open_blob_store()
        refs = spec.open_ref_store()
        # Writers never wait for each other.
        assert refs.swap(HEAD, expected_version=None, target=target).version == 1
        assert spec.leftover_temporaries() == held
    assert spec.open_blob_store().get(live.ref) == live.data
    assert spec.leftover_temporaries() == ()


def test_every_entry_behind_the_store_is_private(spec: StorageSpec) -> None:
    root = backing_root(spec)
    blobs = spec.open_blob_store()
    refs = spec.open_ref_store()
    refs.swap(HEAD, expected_version=None, target=blobs.put(PAYLOAD))
    refs.swap("top-level", expected_version=None, target=blobs.put(b""))
    entries = [root, *sorted(root.rglob("*"))]
    kinds = set()
    for path in entries:
        metadata = path.lstat()
        assert metadata.st_uid == os.getuid(), path
        if stat.S_ISDIR(metadata.st_mode):
            kinds.add("directory")
            assert stat.S_IMODE(metadata.st_mode) == 0o700, path
        else:
            kinds.add("file")
            assert stat.S_ISREG(metadata.st_mode), path
            assert stat.S_IMODE(metadata.st_mode) == 0o600, path
            assert metadata.st_nlink == 1, path
    assert kinds == {"directory", "file"}


def refuse_on_unsafe_backing(
    spec: StorageSpec, problem: BackingProblem
) -> list[tuple[Operation, StorageError]]:
    """Damage the backing as ``problem`` says; every operation ``REFUSALS`` names must fail.

    Returns: each refused operation with its error.
    """
    before_blobs = spec.open_blob_store()
    before_refs = spec.open_ref_store()
    ref = before_blobs.put(PAYLOAD)
    before_refs.swap(HEAD, expected_version=None, target=ref)
    still_unsafe = spec.loosen_backing(problem)
    refusal = REFUSALS[problem]

    def operations(
        blobs: Callable[[], BlobStore], refs: Callable[[], RefStore]
    ) -> dict[Operation, Callable[[], object]]:
        return {
            "get": lambda: blobs().get(ref),
            "has": lambda: blobs().has(ref),
            "put": lambda: blobs().put(PAYLOAD),
            "put-new": lambda: blobs().put(PAYLOAD + b" and more"),
            "read": lambda: refs().read(HEAD),
            "swap": lambda: refs().swap(HEAD, expected_version=1, target=ref),
        }

    fresh = operations(spec.open_blob_store, spec.open_ref_store)
    existing = operations(lambda: before_blobs, lambda: before_refs)
    integrity = refusal.code == "STORAGE.INTEGRITY"
    refused: list[tuple[Operation, StorageError]] = []
    # The stores opened before the damage go first: opening a fresh one may replace what a
    # server behind the spec holds, and then nothing already open would be left to check.
    for table, names in ((existing, refusal.existing), (fresh, refusal.fresh)):
        for name in sorted(expected_refusals(spec, names)):
            refused.append((name, storage_failure(refusal.code, table[name], integrity=integrity)))
            assert still_unsafe(), name
    return refused


@pytest.mark.parametrize("problem", list(REFUSALS))
def test_an_unsafe_backing_is_refused_and_left_alone(
    spec: StorageSpec, problem: BackingProblem
) -> None:
    root = backing_root(spec)
    refusals = refuse_on_unsafe_backing(spec, problem)

    assert refusals
    if not spec.errors_name_backing_paths:
        # The oracle for that capability, and for where the refusal came from: a store's own
        # refusal names the directory it refused, so a refusal that does not name it is the
        # one the server sanitized and sent back.
        for name, error in refusals:
            assert str(root) not in str(error), (name, str(error))


@pytest.mark.parametrize("problem", list(REFUSALS))
def test_a_refusal_of_an_unsafe_backing_names_the_backing_directory(
    spec: StorageSpec, problem: BackingProblem
) -> None:
    root = require_backing_paths_in_errors(spec)
    for name, error in refuse_on_unsafe_backing(spec, problem):
        assert str(root) in str(error), (name, str(error))

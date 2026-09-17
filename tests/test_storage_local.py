"""The local store's own checks: its import closure, root refusals, locks, sweep and faults.

Scenario tests for ``src/cole_voice_lab/storage_local.py`` beyond the port behaviour that
``tests/storage_conformance.py`` checks for every backend. Every store lives under
``tmp_path``; faults are injected by patching this module's seams in-process.
"""

from __future__ import annotations

import ast
import errno
import fcntl
import hashlib
import json
import math
import os
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import chdir
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cole_voice_lab import storage_local
from cole_voice_lab.canonical import (
    NoReplaceRenameUnavailableError,
    NoReplaceRenameUnsupportedPlatformError,
    canonical_json_bytes,
    file_identity,
)
from cole_voice_lab.storage_contracts import MAX_STORAGE_COUNTER
from cole_voice_lab.storage_local import (
    MAX_REF_FILE_BYTES,
    STORE_MEMBERS,
    LeftoverSweep,
    LocalBlobStore,
    LocalRefStore,
)
from cole_voice_lab.storage_ports import (
    BlobRef,
    RefValue,
    StorageError,
    StorageIntegrityError,
    StorageNameError,
    library_head_ref,
)

SOURCE = Path(__file__).resolve().parents[1] / "src" / "cole_voice_lab"
HEAD = library_head_ref("local-library")
PAYLOAD = b"synthetic local payload"
EPOCH = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def base(tmp_path: Path) -> Iterator[Path]:
    resolved = Path(os.path.realpath(tmp_path)) / "base"
    resolved.mkdir(mode=0o700)
    yield resolved
    # Give back any access a test took away, so the temporary tree can be removed.
    for directory, names, files in os.walk(resolved):
        for name in names + files:
            path = Path(directory, name)
            if not path.is_symlink() and not stat.S_IMODE(path.stat().st_mode) & 0o700:
                path.chmod(0o700 if path.is_dir() else 0o600)


@pytest.fixture
def root(base: Path) -> Path:
    store = base / "store"
    LocalBlobStore(store, create=True)
    return store


def failure(
    code: str, action: Callable[[], object], *, match: str = "", integrity: bool = False
) -> StorageError:
    with pytest.raises(StorageIntegrityError if integrity else StorageError, match=match) as caught:
        action()
    assert caught.value.code == code, str(caught.value)
    assert isinstance(caught.value, StorageIntegrityError) is integrity
    return caught.value


def entries(directory: Path) -> list[str]:
    return sorted(os.listdir(directory))


def digest_ref(data: bytes) -> BlobRef:
    return BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def blob_file(root: Path, ref: BlobRef) -> Path:
    return root / "blobs" / ref.sha256[:2] / ref.sha256


def plant_socket(path: Path) -> None:
    """A private UNIX socket at ``path`` (bound by its bare name, as socket paths are short)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server, chdir(path.parent):
        server.bind(path.name)
    path.chmod(0o600)


def record_fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record the identity of every descriptor ``os.fsync`` is called on from now on."""
    synced: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        synced.append(file_identity(os.fstat(descriptor)))
        real_fsync(descriptor)

    monkeypatch.setattr(storage_local.os, "fsync", recording_fsync)
    return synced


def held_lock(path: Path, operation: int) -> int:
    descriptor = os.open(path, os.O_RDWR)
    fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
    return descriptor


def lock_is_free(path: Path) -> bool:
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)
    return True


# -- import closure -----------------------------------------------------------------------

ALLOWED_PACKAGE_MODULES = frozenset(
    {
        "cole_voice_lab",
        "cole_voice_lab.canonical",
        "cole_voice_lab.errors",
        "cole_voice_lab.models",
        "cole_voice_lab.paths",
        "cole_voice_lab.storage_contracts",
        "cole_voice_lab.storage_local",
        "cole_voice_lab.storage_ports",
    }
)


def test_loading_the_local_store_loads_only_the_storage_base_modules(tmp_path: Path) -> None:
    probe = (
        "import sys\n"
        "import cole_voice_lab.storage_local\n"
        "print('\\n'.join(sorted(name for name in sys.modules"
        " if name.partition('.')[0] == 'cole_voice_lab')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.split())
    assert "cole_voice_lab.storage_local" in loaded
    assert loaded <= ALLOWED_PACKAGE_MODULES, sorted(loaded - ALLOWED_PACKAGE_MODULES)


def test_the_local_store_imports_only_the_standard_library_and_the_storage_base() -> None:
    allowed_package = {"errors", "canonical", "storage_contracts", "storage_ports"}
    allowed_external = set(sys.stdlib_module_names) | {"__future__"}
    tree = ast.parse((SOURCE / "storage_local.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            assert node.level == 1 and node.module in allowed_package, (node.lineno, node.module)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").partition(".")[0] in allowed_external, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.partition(".")[0] in allowed_external, alias.name
        elif isinstance(node, ast.Call):
            called = getattr(node.func, "id", getattr(node.func, "attr", None))
            assert called not in {"import_module", "__import__", "run_module"}, node.lineno


def test_every_storage_error_is_built_by_the_port_factory() -> None:
    tree = ast.parse((SOURCE / "storage_local.py").read_text(encoding="utf-8"))
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "storage_error" in constructed
    assert not constructed & {"StorageError", "StorageIntegrityError"}


# -- the root -----------------------------------------------------------------------------


def test_create_makes_a_private_layout_even_under_a_restrictive_umask(base: Path) -> None:
    previous = os.umask(0o277)
    try:
        blobs = LocalBlobStore(base / "store", create=True)
        refs = LocalRefStore(base / "store", create=True)
    finally:
        os.umask(previous)
    assert blobs.root == refs.root == base / "store"
    assert set(entries(base / "store")) == STORE_MEMBERS
    for path in [base / "store", *(base / "store").iterdir()]:
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected, path
    blobs.put(PAYLOAD)
    shard = next((base / "store" / "blobs").iterdir())
    assert stat.S_IMODE(shard.stat().st_mode) == 0o700


def test_a_missing_root_is_retryably_unavailable_and_never_created(base: Path) -> None:
    for store_class in (LocalBlobStore, LocalRefStore):
        error = failure(
            "STORAGE.UNAVAILABLE",
            lambda store_class=store_class: store_class(base / "store"),
            match="no local store",
        )
        assert error.retryable
    assert entries(base) == []
    error = failure(
        "STORAGE.UNAVAILABLE",
        lambda: LocalBlobStore(base / "missing" / "store", create=True),
        match="missing/store",
    )
    assert entries(base) == []


@pytest.mark.parametrize(
    ("given", "match"),
    [
        ("relative/store", "absolute, normalized"),
        ("{base}/store/../store", "absolute, normalized"),
        ("{base}/link/store", "no symlink in its path"),
        ("{base}/link-to-store", "no symlink in its path"),
    ],
)
def test_a_root_path_that_is_relative_unnormalized_or_symlinked_is_refused(
    root: Path, base: Path, given: str, match: str
) -> None:
    (base / "link").symlink_to(base)
    (base / "link-to-store").symlink_to(root)
    for create in (False, True):
        failure(
            "STORAGE.INTEGRITY",
            lambda create=create: LocalRefStore(Path(given.format(base=base)), create=create),
            match=match,
            integrity=True,
        )


def test_an_existing_unsafe_root_is_refused_without_being_changed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root.chmod(0o750)
    failure(
        "STORAGE.INTEGRITY",
        lambda: LocalBlobStore(root, create=True),
        match=r"has group or other permissions \(mode 0o750\)",
        integrity=True,
    )
    assert stat.S_IMODE(root.stat().st_mode) == 0o750
    root.chmod(0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(storage_local.os, "getuid", lambda: real_uid + 1)
    failure(
        "STORAGE.INTEGRITY",
        lambda: LocalRefStore(root),
        match=f"is owned by uid {real_uid}, not {real_uid + 1}",
        integrity=True,
    )


def test_a_root_that_is_a_file_is_refused(base: Path) -> None:
    (base / "store").write_bytes(b"not a directory")
    failure(
        "STORAGE.INTEGRITY",
        lambda: LocalBlobStore(base / "store", create=True),
        match="symlink or non-directory",
        integrity=True,
    )


def test_a_root_holding_anything_a_store_never_writes_is_refused(base: Path) -> None:
    home_like = base / "store"
    home_like.mkdir(mode=0o700)
    (home_like / "Documents").mkdir()
    (home_like / ".profile").write_bytes(b"")
    failure(
        "STORAGE.INTEGRITY",
        lambda: LocalBlobStore(home_like, create=True),
        match=r"never writes \(\.profile, Documents\)",
        integrity=True,
    )
    assert entries(home_like) == [".profile", "Documents"]


def test_an_incomplete_layout_opens_only_with_create(root: Path) -> None:
    (root / "refs").rmdir()
    (root / ".refs.lock").unlink()
    failure(
        "STORAGE.INTEGRITY",
        lambda: LocalRefStore(root),
        match=r"missing a directory: .*/refs \(only a new store",
        integrity=True,
    )
    assert "refs" not in entries(root)
    LocalRefStore(root, create=True)
    assert set(entries(root)) == STORE_MEMBERS
    (root / ".refs.lock").unlink()
    failure(
        "STORAGE.INTEGRITY",
        lambda: LocalRefStore(root),
        match=r"missing its lock file: .*/\.refs\.lock",
        integrity=True,
    )


@pytest.mark.parametrize(
    "kind", ["symlink", "directory", "fifo", "socket", "hard-link", "group-readable"]
)
def test_an_unsafe_lock_file_is_refused(root: Path, base: Path, kind: str) -> None:
    lock = root / ".tmp.lock"
    if kind == "symlink":
        lock.rename(base / "moved-lock")
        lock.symlink_to(base / "moved-lock")
        match = "symlink or directory where a file belongs"
    elif kind == "directory":
        lock.unlink()
        lock.mkdir(mode=0o700)
        match = "symlink or directory where a file belongs"
    elif kind == "fifo":
        lock.unlink()
        os.mkfifo(lock, 0o600)
        match = "is not a regular file"
    elif kind == "socket":
        # Opening a socket fails (EOPNOTSUPP on darwin, ENXIO on Linux): still an unsafe entry.
        lock.unlink()
        plant_socket(lock)
        match = "is not a regular file"
    elif kind == "hard-link":
        os.link(lock, base / "extra-link")
        match = "has 2 hard links"
    else:
        lock.chmod(0o640)
        match = "has group or other permissions"
    for create in (False, True):
        failure(
            "STORAGE.INTEGRITY",
            lambda create=create: LocalBlobStore(root, create=create),
            match=match,
            integrity=True,
        )


def test_an_open_store_refuses_a_replaced_or_vanished_root(root: Path, base: Path) -> None:
    blobs = LocalBlobStore(root)
    refs = LocalRefStore(root)
    ref = blobs.put(PAYLOAD)
    root.rename(base / "moved")
    failure("STORAGE.UNAVAILABLE", lambda: blobs.get(ref), match="root is missing")
    LocalBlobStore(base / "fresh", create=True).root.rename(root)
    failure("STORAGE.INTEGRITY", lambda: refs.read(HEAD), match="was replaced", integrity=True)
    failure("STORAGE.INTEGRITY", lambda: blobs.has(ref), match="was replaced", integrity=True)


def test_an_unreadable_directory_is_a_retryable_failure_naming_its_path(root: Path) -> None:
    blobs = LocalBlobStore(root)
    ref = blobs.put(PAYLOAD)
    shard = blob_file(root, ref).parent
    shard.chmod(0)
    error = failure("STORAGE.UNAVAILABLE", lambda: blobs.get(ref), match="Permission denied")
    assert str(shard) in str(error) and str(root) in str(error)
    assert error.retryable


@pytest.mark.parametrize("made", ["root", "shard"])
def test_a_new_directory_swapped_for_a_symlink_is_refused_and_its_target_left_alone(
    base: Path, monkeypatch: pytest.MonkeyPatch, made: str
) -> None:
    outside = base / "outside"
    outside.mkdir(mode=0o700)
    (outside / "private").write_bytes(b"not the store's")
    (outside / "private").chmod(0o600)
    outside.chmod(0o750)
    target = outside if made == "root" else outside / "private"
    store = base / "store"
    if made == "shard":
        LocalBlobStore(store, create=True)
    real_mkdir = os.mkdir
    swapped: list[str] = []

    def mkdir_then_swap(name: str | Path, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        real_mkdir(name, mode, dir_fd=dir_fd)
        if Path(name) == store or (dir_fd is not None and len(str(name)) == 2):
            os.rmdir(name, dir_fd=dir_fd)
            os.symlink(target, name, dir_fd=dir_fd)
            swapped.append(str(name))

    monkeypatch.setattr(storage_local.os, "mkdir", mkdir_then_swap)
    action = (
        (lambda: LocalBlobStore(store, create=True))
        if made == "root"
        else (lambda: LocalBlobStore(store).put(PAYLOAD))
    )
    failure("STORAGE.INTEGRITY", action, match="symlink or non-directory", integrity=True)
    assert len(swapped) == 1
    assert stat.S_IMODE(outside.stat().st_mode) == 0o750
    assert stat.S_IMODE((outside / "private").stat().st_mode) == 0o600
    assert entries(outside) == ["private"]


def test_a_new_directory_that_vanishes_before_it_is_opened_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)
    real_mkdir = os.mkdir

    def mkdir_then_remove(
        name: str | Path, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        real_mkdir(name, mode, dir_fd=dir_fd)
        os.rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(storage_local.os, "mkdir", mkdir_then_remove)
    failure(
        "STORAGE.INTEGRITY",
        lambda: blobs.put(PAYLOAD),
        match="just created vanished: .*/blobs/[0-9a-f]{2}$",
        integrity=True,
    )


@pytest.mark.parametrize("timeout", [0.0, -1.0, math.nan, math.inf, 3600.5])
def test_a_lock_timeout_out_of_range_is_a_caller_error(root: Path, timeout: float) -> None:
    for store_class in (LocalBlobStore, LocalRefStore):
        with pytest.raises(ValueError, match="lock timeout"):
            store_class(root, lock_timeout=timeout)


def test_the_longest_lock_timeout_is_accepted(root: Path) -> None:
    assert LocalRefStore(root, lock_timeout=3600.0).root == root


# -- locks --------------------------------------------------------------------------------


def test_a_swap_waits_for_the_ref_lock_then_fails_retryably(root: Path) -> None:
    blobs = LocalBlobStore(root)
    refs = LocalRefStore(root, lock_timeout=0.05)
    first = refs.swap(HEAD, expected_version=None, target=blobs.put(PAYLOAD))
    holder = held_lock(root / ".refs.lock", fcntl.LOCK_EX)
    try:
        error = failure(
            "STORAGE.UNAVAILABLE",
            lambda: refs.swap(HEAD, expected_version=1, target=blobs.put(b"next")),
            match=r"lock stayed busy for 0\.05 s: .*/\.refs\.lock",
        )
        assert error.retryable
        # Readers take no lock.
        assert refs.read(HEAD) == first
    finally:
        os.close(holder)
    assert refs.swap(HEAD, expected_version=1, target=first.target).version == 2
    assert entries(root / "tmp") == []


def test_a_swap_that_meets_a_held_ref_lock_proceeds_once_it_is_released(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = LocalRefStore(root)
    target = LocalBlobStore(root).put(PAYLOAD)
    holder = held_lock(root / ".refs.lock", fcntl.LOCK_EX)
    waits: list[float] = []
    real_sleep = storage_local.time.sleep

    def release_while_waiting(seconds: float) -> None:
        waits.append(seconds)
        os.close(holder)
        real_sleep(0)

    monkeypatch.setattr(storage_local.time, "sleep", release_while_waiting)
    assert refs.swap(HEAD, expected_version=None, target=target).version == 1
    assert waits == [storage_local._LOCK_POLL_SECONDS]


def test_a_put_of_new_bytes_waits_while_a_sweep_holds_the_temporary_lock(root: Path) -> None:
    blobs = LocalBlobStore(root, lock_timeout=0.05)
    stored = blobs.put(PAYLOAD)
    sweeper = held_lock(root / ".tmp.lock", fcntl.LOCK_EX)
    try:
        failure("STORAGE.UNAVAILABLE", lambda: blobs.put(b"new bytes"), match=r"\.tmp\.lock")
        # A blob that is already stored needs no temporary, so no lock.
        assert blobs.put(PAYLOAD) == stored
        assert entries(root / "tmp") == []
    finally:
        os.close(sweeper)


@pytest.mark.parametrize("operation", ["put", "swap"])
def test_a_writer_holds_the_temporary_lock_while_its_temporary_exists(
    root: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    blobs = LocalBlobStore(root)
    refs = LocalRefStore(root)
    target = blobs.put(PAYLOAD)
    observed: list[tuple[str, bool, list[str]]] = []
    real_write = storage_local._write_temporary
    real_rename = storage_local.rename_no_replace_at
    real_replace = os.replace

    def write(directory: int, data: bytes) -> tuple[str, tuple[int, int]]:
        observed.append(("before-create", lock_is_free(root / ".tmp.lock"), entries(root / "tmp")))
        return real_write(directory, data)

    def rename(source_directory: int, source: str, target_directory: int, name: str) -> None:
        observed.append(("publish", lock_is_free(root / ".tmp.lock"), entries(root / "tmp")))
        real_rename(source_directory, source, target_directory, name)

    def replace(*arguments: object, **options: object) -> None:
        observed.append(("publish", lock_is_free(root / ".tmp.lock"), entries(root / "tmp")))
        real_replace(*arguments, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(storage_local, "_write_temporary", write)
    monkeypatch.setattr(storage_local, "rename_no_replace_at", rename)
    monkeypatch.setattr(storage_local.os, "replace", replace)
    if operation == "put":
        blobs.put(b"fresh bytes")
    else:
        refs.swap(HEAD, expected_version=None, target=target)
    assert [(stage, free) for stage, free, _ in observed] == [
        ("before-create", False),
        ("publish", False),
    ]
    assert observed[0][2] == [] and len(observed[1][2]) == 1
    assert lock_is_free(root / ".tmp.lock")
    assert entries(root / "tmp") == []


# -- sweep --------------------------------------------------------------------------------


def plant_temporary(root: Path, name: str = f"4242-{'b' * 32}.tmp") -> Path:
    path = root / "tmp" / name
    path.write_bytes(b"left behind")
    path.chmod(0o600)
    return path


def test_a_sweep_removes_every_temporary_when_no_writer_is_active(root: Path) -> None:
    blobs = LocalBlobStore(root)
    first = plant_temporary(root)
    second = plant_temporary(root, f"7-{'c' * 32}.tmp")
    second.chmod(0o644)
    assert blobs.sweep_leftovers() == LeftoverSweep(
        removed=(first.name, second.name), writers_active=False
    )
    assert entries(root / "tmp") == []
    assert LocalRefStore(root).sweep_leftovers() == LeftoverSweep((), writers_active=False)


def test_a_sweep_examines_nothing_while_any_writer_holds_the_temporary_lock(root: Path) -> None:
    planted = plant_temporary(root)
    (root / "tmp" / "not-a-temporary").mkdir()
    writer = held_lock(root / ".tmp.lock", fcntl.LOCK_SH)
    try:
        refs = LocalRefStore(root)
        assert refs.sweep_leftovers() == LeftoverSweep(removed=(), writers_active=True)
        assert LocalBlobStore(root).sweep_leftovers().writers_active
    finally:
        os.close(writer)
    assert planted.exists()
    failure(
        "STORAGE.INTEGRITY",
        refs.sweep_leftovers,
        match="holds an entry no writer creates: .*/tmp/not-a-temporary",
        integrity=True,
    )


@pytest.mark.parametrize("kind", ["wrong-name", "symlink", "directory", "fifo"])
def test_a_sweep_refuses_an_entry_no_writer_creates_and_removes_nothing(
    root: Path, base: Path, kind: str
) -> None:
    good = plant_temporary(root)
    name = f"1-{'d' * 32}.tmp"
    odd = root / "tmp" / (f"1-{'d' * 31}.tmp" if kind == "wrong-name" else name)
    if kind == "wrong-name":
        odd.write_bytes(b"")
    elif kind == "symlink":
        odd.symlink_to(base / "elsewhere")
    elif kind == "directory":
        odd.mkdir()
    else:
        os.mkfifo(odd, 0o600)
    failure(
        "STORAGE.INTEGRITY",
        lambda: LocalBlobStore(root),
        match=f"no writer creates: .*/tmp/{odd.name}",
        integrity=True,
    )
    assert good.exists()
    assert odd.is_symlink() or odd.exists()


# -- blobs --------------------------------------------------------------------------------


def test_the_blob_layout_is_digest_named(root: Path) -> None:
    ref = LocalBlobStore(root).put(PAYLOAD)
    path = blob_file(root, ref)
    assert path.read_bytes() == PAYLOAD
    assert [p.relative_to(root).as_posix() for p in (root / "blobs").rglob("*")] == [
        f"blobs/{ref.sha256[:2]}",
        f"blobs/{ref.sha256[:2]}/{ref.sha256}",
    ]


@pytest.mark.parametrize(
    ("sha256", "size", "match"),
    [
        ("../../../outside", 1, r"sha256='\.\./\.\./\.\./outside', size=1$"),
        ("a" * 64 + "\n", 1, "size=1$"),
        ("a" * 64, -1, "size=-1$"),
        ("x" * 200, 1, "sha256='x{79}, size=1$"),
    ],
)
def test_a_ref_that_is_not_valid_never_names_a_file(
    root: Path, sha256: str, size: int, match: str
) -> None:
    blobs = LocalBlobStore(root)
    stored = blobs.put(b"x")
    forged = BlobRef.model_construct(sha256=sha256, size=size)
    for action in (blobs.get, blobs.has):
        error = failure("STORAGE.PROTOCOL", lambda action=action: action(forged), match=match)
        assert "not a valid blob ref" in str(error) and not error.retryable
    assert blobs.get(stored) == b"x"


def test_a_value_that_is_not_a_blob_ref_is_refused(root: Path) -> None:
    blobs = LocalBlobStore(root)
    refs = LocalRefStore(root)
    stand_in = digest_ref(PAYLOAD).model_dump()
    failure(
        "STORAGE.PROTOCOL",
        lambda: refs.swap(HEAD, expected_version=None, target=stand_in),  # type: ignore[arg-type]
        match="sha256=None, size=None$",
    )
    failure(
        "STORAGE.PROTOCOL",
        lambda: blobs.get(stand_in),  # type: ignore[arg-type]
        match="not a valid blob ref",
    )
    assert refs.read(HEAD) is None


def test_an_oversized_put_touches_nothing(root: Path) -> None:
    before = sorted(path.relative_to(root) for path in root.rglob("*"))
    error = failure(
        "STORAGE.TOO_LARGE",
        lambda: LocalBlobStore(root).put(b"\0" * (storage_local.MAX_BLOB_BYTES + 1)),
        match="at most 16777216 bytes, not 16777217",
    )
    assert not error.retryable
    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before


def test_a_failed_temporary_write_is_removed_and_reported_as_quota(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)
    real_fchmod = os.fchmod
    failed: list[int] = []

    def no_space_for_files(descriptor: int, mode: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            failed.append(descriptor)
            raise OSError(errno.ENOSPC, "No space left on device")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(storage_local.os, "fchmod", no_space_for_files)
    error = failure("STORAGE.QUOTA", lambda: blobs.put(PAYLOAD), match="No space left")
    assert not error.retryable and str(root) in str(error)
    # The temporary was written, then removed.
    assert len(failed) == 1
    assert entries(root / "tmp") == []
    assert not blobs.has(digest_ref(PAYLOAD))


@pytest.mark.parametrize(
    "refusal", [NoReplaceRenameUnavailableError, NoReplaceRenameUnsupportedPlatformError]
)
def test_a_filesystem_that_cannot_publish_without_replacing_is_a_lasting_failure(
    root: Path, monkeypatch: pytest.MonkeyPatch, refusal: type[OSError]
) -> None:
    blobs = LocalBlobStore(root)

    def refuse(_source_directory: int, _source: str, _target_directory: int, name: str) -> None:
        raise refusal(errno.EINVAL, "Invalid argument (no-replace rename refused)", name)

    monkeypatch.setattr(storage_local, "rename_no_replace_at", refuse)
    error = failure(
        "STORAGE.PROTOCOL",
        lambda: blobs.put(PAYLOAD),
        match=r"cannot publish a blob without replacing.*no-replace rename refused",
    )
    assert not error.retryable and str(root) in str(error)
    assert entries(root / "tmp") == []
    assert not blobs.has(digest_ref(PAYLOAD))


def publish_a_winner_first(
    monkeypatch: pytest.MonkeyPatch, root: Path, winner: bytes
) -> list[tuple[int, int]]:
    """Make the next publish find ``winner`` already published; return the winner's identity."""
    real_rename = storage_local.rename_no_replace_at
    published: list[tuple[int, int]] = []

    def publish(source_directory: int, source: str, target_directory: int, name: str) -> None:
        staged = root / "tmp" / "winner.tmp"
        staged.write_bytes(winner)
        staged.chmod(0o600)
        published.append(file_identity(staged.stat()))
        real_rename(source_directory, staged.name, target_directory, name)
        real_rename(source_directory, source, target_directory, name)

    monkeypatch.setattr(storage_local, "rename_no_replace_at", publish)
    return published


def test_a_writer_that_finds_its_blob_published_verifies_the_winner(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)
    winner = publish_a_winner_first(monkeypatch, root, PAYLOAD)
    ref = blobs.put(PAYLOAD)
    assert blobs.get(ref) == PAYLOAD
    assert [file_identity(blob_file(root, ref).stat())] == winner
    assert entries(root / "tmp") == []


@pytest.mark.parametrize("found", ["before-writing", "after-losing-the-race"])
def test_a_put_that_finds_its_blob_stored_makes_it_durable_before_returning(
    root: Path, monkeypatch: pytest.MonkeyPatch, found: str
) -> None:
    blobs = LocalBlobStore(root)
    ref = digest_ref(PAYLOAD)
    if found == "before-writing":
        blobs.put(PAYLOAD)
    else:
        publish_a_winner_first(monkeypatch, root, PAYLOAD)
    synced = record_fsyncs(monkeypatch)
    assert blobs.put(PAYLOAD) == ref
    # The blob, its entry in the shard and the shard's entry in blobs/ are all flushed, in
    # case the writer that published them died before it could sync.
    blob = blob_file(root, ref)
    for path in (blob, blob.parent, root / "blobs"):
        assert file_identity(path.stat()) in synced, path


def test_a_corrupt_winner_is_refused_to_the_writer_that_lost(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)
    publish_a_winner_first(monkeypatch, root, b"x" * len(PAYLOAD))
    synced = record_fsyncs(monkeypatch)
    failure(
        "STORAGE.INTEGRITY",
        lambda: blobs.put(PAYLOAD),
        match="does not match its digest",
        integrity=True,
    )
    assert file_identity(blob_file(root, digest_ref(PAYLOAD)).stat()) not in synced
    assert entries(root / "tmp") == []


def test_a_conflict_with_nothing_published_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)

    def phantom_conflict(_source_directory: int, _source: str, _directory: int, name: str) -> None:
        raise FileExistsError(errno.EEXIST, "File exists", name)

    monkeypatch.setattr(storage_local, "rename_no_replace_at", phantom_conflict)
    failure(
        "STORAGE.INTEGRITY",
        lambda: blobs.put(PAYLOAD),
        match="vanished after it was published",
        integrity=True,
    )
    assert entries(root / "tmp") == []


@pytest.mark.parametrize("redirect", ["elsewhere", "other-file"])
def test_a_publish_that_lands_anywhere_but_the_blob_path_is_refused(
    root: Path, base: Path, monkeypatch: pytest.MonkeyPatch, redirect: str
) -> None:
    blobs = LocalBlobStore(root)
    target = blob_file(root, digest_ref(PAYLOAD))

    def misdirected(source_directory: int, source: str, _directory: int, _name: str) -> None:
        os.rename(source, base / "stolen", src_dir_fd=source_directory)
        if redirect == "other-file":
            target.write_bytes(PAYLOAD)
            target.chmod(0o600)

    monkeypatch.setattr(storage_local, "rename_no_replace_at", misdirected)
    failure(
        "STORAGE.INTEGRITY",
        lambda: blobs.put(PAYLOAD),
        match="not the file this writer wrote",
        integrity=True,
    )


def test_a_shard_swapped_for_a_symlink_mid_put_never_redirects_the_blob(
    root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)
    ref = digest_ref(PAYLOAD)
    shard = blob_file(root, ref).parent
    outside = base / "outside"
    outside.mkdir(mode=0o700)
    real_write = storage_local._write_temporary

    def write_then_swap_the_shard(directory: int, data: bytes) -> tuple[str, tuple[int, int]]:
        written = real_write(directory, data)
        shard.rename(base / "moved-shard")
        shard.symlink_to(outside)
        return written

    monkeypatch.setattr(storage_local, "_write_temporary", write_then_swap_the_shard)
    blobs.put(PAYLOAD)
    # The blob went into the directory the writer opened and checked, not through the link.
    assert entries(outside) == []
    assert (base / "moved-shard" / ref.sha256).read_bytes() == PAYLOAD
    failure("STORAGE.INTEGRITY", lambda: blobs.get(ref), match="symlink", integrity=True)


def test_an_interrupted_publish_removes_its_temporary(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)

    def interrupted(*_arguments: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage_local, "rename_no_replace_at", interrupted)
    with pytest.raises(KeyboardInterrupt):
        blobs.put(PAYLOAD)
    assert entries(root / "tmp") == []
    assert lock_is_free(root / ".tmp.lock")


def test_an_open_that_fails_on_an_entry_that_then_vanished_reports_the_open_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)
    ref = blobs.put(PAYLOAD)
    real_open = os.open

    def fail_and_remove(name: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == ref.sha256:
            os.unlink(name, dir_fd=kwargs["dir_fd"])  # type: ignore[arg-type]
            raise OSError(errno.EIO, "synthetic open failure")
        return real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(storage_local.os, "open", fail_and_remove)
    error = failure("STORAGE.UNAVAILABLE", lambda: blobs.get(ref), match="synthetic open failure")
    assert str(blob_file(root, ref)) in str(error)


def test_an_unreadable_private_blob_is_a_retryable_failure(root: Path) -> None:
    blobs = LocalBlobStore(root)
    ref = blobs.put(PAYLOAD)
    blob_file(root, ref).chmod(0)
    assert blobs.has(ref)
    for action in (blobs.get, lambda _ref: blobs.put(PAYLOAD)):
        failure(
            "STORAGE.UNAVAILABLE",
            lambda action=action: action(ref),
            match="Permission denied",
        )


def test_a_blob_owned_by_another_user_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)
    ref = blobs.put(PAYLOAD)
    blob_inode = blob_file(root, ref).stat().st_ino
    real_uid = os.getuid()
    real_fstat = os.fstat

    def foreign_blob(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        if metadata.st_ino != blob_inode:
            return metadata
        values = list(metadata)
        values[stat.ST_UID] = real_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(storage_local.os, "fstat", foreign_blob)
    failure("STORAGE.INTEGRITY", lambda: blobs.get(ref), match="is owned by uid", integrity=True)
    failure(
        "STORAGE.INTEGRITY", lambda: blobs.put(PAYLOAD), match="is owned by uid", integrity=True
    )


# -- refs ---------------------------------------------------------------------------------


def ref_file(root: Path, name: str) -> Path:
    *directories, last = name.split("/")
    return root.joinpath("refs", *(f"{part}.d" for part in directories), f"{last}.json")


def stored_value(root: Path, name: str, version: int, target: BlobRef) -> RefValue:
    """Plant a canonical value in place of what the store wrote for ``name``."""
    if LocalRefStore(root).read(name) is None:
        LocalRefStore(root).swap(name, expected_version=None, target=target)
    value = RefValue(name=name, version=version, target=target, updated_at=EPOCH)
    ref_file(root, name).write_bytes(canonical_json_bytes(value) + b"\n")
    return value


def test_a_ref_is_canonical_json_under_suffixed_directories(root: Path) -> None:
    target = LocalBlobStore(root).put(PAYLOAD)
    clock = iter((EPOCH, EPOCH + timedelta(hours=1)))
    refs = LocalRefStore(root, clock=lambda: next(clock))
    first = refs.swap(HEAD, expected_version=None, target=target)
    second = refs.swap(HEAD, expected_version=1, target=target)
    assert (first.updated_at, second.updated_at) == (EPOCH, EPOCH + timedelta(hours=1))
    path = root / "refs" / "libraries.d" / "local-library.d" / "head.json"
    assert path.read_bytes() == canonical_json_bytes(second) + b"\n"
    assert json.loads(path.read_bytes())["version"] == 2
    assert [p.name for p in sorted((root / "refs").rglob("*"))] == [
        "libraries.d",
        "local-library.d",
        "head.json",
    ]
    assert entries(root / "tmp") == []


def test_a_clock_in_another_timezone_is_stored_as_utc(root: Path) -> None:
    target = LocalBlobStore(root).put(PAYLOAD)
    eastern = EPOCH.astimezone(__import__("datetime").timezone(timedelta(hours=-4)))
    value = LocalRefStore(root, clock=lambda: eastern).swap(
        HEAD, expected_version=None, target=target
    )
    assert value.updated_at == EPOCH and value.updated_at.tzinfo is UTC
    assert LocalRefStore(root).read(HEAD) == value


def test_a_naive_clock_is_a_caller_error_that_changes_nothing(root: Path) -> None:
    target = LocalBlobStore(root).put(PAYLOAD)
    refs = LocalRefStore(root, clock=lambda: datetime(2026, 9, 17))
    with pytest.raises(ValueError, match="explicit timezone"):
        refs.swap(HEAD, expected_version=None, target=target)
    assert refs.read(HEAD) is None
    assert entries(root / "tmp") == [] and entries(root / "refs") == []
    assert lock_is_free(root / ".refs.lock")


def test_a_conflict_creates_no_directories(root: Path) -> None:
    target = LocalBlobStore(root).put(PAYLOAD)
    refs = LocalRefStore(root)
    failure(
        "STORAGE.CONFLICT",
        lambda: refs.swap(HEAD, expected_version=3, target=target),
        match=r"is absent; the swap expected it at version 3",
    )
    assert entries(root / "refs") == []
    refs.swap(HEAD, expected_version=None, target=target)
    failure(
        "STORAGE.CONFLICT",
        lambda: refs.swap(HEAD, expected_version=None, target=target),
        match=r"is at version 1; the swap expected it absent",
    )


def test_a_ref_at_the_last_version_cannot_be_swapped(root: Path) -> None:
    target = LocalBlobStore(root).put(PAYLOAD)
    refs = LocalRefStore(root)
    last = stored_value(root, HEAD, MAX_STORAGE_COUNTER, target)
    assert refs.read(HEAD) == last
    error = failure(
        "STORAGE.QUOTA",
        lambda: refs.swap(HEAD, expected_version=MAX_STORAGE_COUNTER, target=target),
        match="used every version",
    )
    assert not error.retryable
    assert refs.read(HEAD) == last


def test_an_interrupted_replace_removes_its_temporary_and_keeps_the_value(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = LocalBlobStore(root).put(PAYLOAD)
    refs = LocalRefStore(root)
    first = refs.swap(HEAD, expected_version=None, target=target)

    def fail(*_arguments: object, **_options: object) -> None:
        raise OSError(errno.EIO, "synthetic replace failure")

    monkeypatch.setattr(storage_local.os, "replace", fail)
    error = failure(
        "STORAGE.UNAVAILABLE",
        lambda: refs.swap(HEAD, expected_version=1, target=target),
        match="synthetic replace failure",
    )
    assert HEAD in str(error) and str(root) in str(error)
    monkeypatch.undo()
    assert entries(root / "tmp") == []
    assert refs.read(HEAD) == first
    assert lock_is_free(root / ".refs.lock") and lock_is_free(root / ".tmp.lock")


def test_a_reader_accepts_a_value_replaced_after_it_opened_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = LocalBlobStore(root)
    refs = LocalRefStore(root)
    first = refs.swap(HEAD, expected_version=None, target=blobs.put(PAYLOAD))
    real_open = storage_local._open_file
    replaced: list[int] = []

    def open_then_replace(parent: int, name: str, label: Path, flags: int) -> int | None:
        descriptor = real_open(parent, name, label, flags)
        if descriptor is not None and name == "head.json" and not replaced:
            replaced.append(os.fstat(descriptor).st_ino)
            monkeypatch.setattr(storage_local, "_open_file", real_open)
            refs.swap(HEAD, expected_version=1, target=blobs.put(b"second"))
            assert os.fstat(descriptor).st_nlink == 0
        return descriptor

    monkeypatch.setattr(storage_local, "_open_file", open_then_replace)
    assert refs.read(HEAD) == first
    assert replaced
    current = refs.read(HEAD)
    assert current is not None and current.version == 2


def test_a_swap_syncs_every_ref_directory_into_its_parent_even_when_it_exists(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = LocalRefStore(root)
    target = LocalBlobStore(root).put(PAYLOAD)
    refs.swap(HEAD, expected_version=None, target=target)
    synced = record_fsyncs(monkeypatch)
    assert refs.swap(HEAD, expected_version=1, target=target).version == 2
    for relative in ("refs", "refs/libraries.d", "refs/libraries.d/local-library.d"):
        assert file_identity((root / relative).stat()) in synced, relative


def test_a_ref_file_that_is_a_socket_is_refused(root: Path) -> None:
    refs = LocalRefStore(root)
    target = LocalBlobStore(root).put(PAYLOAD)
    refs.swap(HEAD, expected_version=None, target=target)
    ref_file(root, HEAD).unlink()
    plant_socket(ref_file(root, HEAD))
    for action in (
        lambda: refs.read(HEAD),
        lambda: refs.swap(HEAD, expected_version=1, target=target),
    ):
        failure("STORAGE.INTEGRITY", action, match="is not a regular file", integrity=True)


def test_a_hard_linked_ref_file_is_refused(root: Path, base: Path) -> None:
    refs = LocalRefStore(root)
    refs.swap(HEAD, expected_version=None, target=LocalBlobStore(root).put(PAYLOAD))
    os.link(ref_file(root, HEAD), base / "extra-link")
    failure("STORAGE.INTEGRITY", lambda: refs.read(HEAD), match="has 2 hard links", integrity=True)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"x" * (MAX_REF_FILE_BYTES + 1), "larger than 4096 bytes"),
        (b"{}", "not valid"),
        (b"\xff\xfe", "not valid"),
        ("{name}", "names ref 'libraries/other/head'"),
        ("{spaced}", "not in canonical form"),
        ("{unterminated}", "not in canonical form"),
    ],
)
def test_a_stored_ref_value_is_checked_before_it_is_believed(
    root: Path, payload: bytes | str, match: str
) -> None:
    target = LocalBlobStore(root).put(PAYLOAD)
    refs = LocalRefStore(root)
    value = refs.swap(HEAD, expected_version=None, target=target)
    other = value.model_copy(update={"name": "libraries/other/head"})
    variants = {
        "{name}": canonical_json_bytes(other) + b"\n",
        "{spaced}": json.dumps(value.model_dump(mode="json")).encode() + b"\n",
        "{unterminated}": canonical_json_bytes(value),
    }
    data = variants[payload] if isinstance(payload, str) else payload
    ref_file(root, HEAD).write_bytes(data)
    error = failure("STORAGE.INTEGRITY", lambda: refs.read(HEAD), match=match, integrity=True)
    assert str(ref_file(root, HEAD)) in str(error)


def test_a_ref_file_of_exactly_the_bound_is_parsed(root: Path) -> None:
    target = LocalBlobStore(root).put(PAYLOAD)
    refs = LocalRefStore(root)
    refs.swap(HEAD, expected_version=None, target=target)
    ref_file(root, HEAD).write_bytes(b" " * MAX_REF_FILE_BYTES)
    failure("STORAGE.INTEGRITY", lambda: refs.read(HEAD), match="not valid", integrity=True)


@pytest.mark.parametrize("name", ["../escape", "..", ".", "a/..", "a/./b", "a/../../b"])
def test_a_name_that_would_leave_refs_is_refused_even_without_the_grammar(
    root: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(storage_local, "validate_ref_name", lambda value: value)
    monkeypatch.setattr(storage_local, "REF_DIRECTORY_SUFFIX", "")
    monkeypatch.setattr(storage_local, "REF_FILE_SUFFIX", "")
    refs = LocalRefStore(root)
    target = LocalBlobStore(root).put(PAYLOAD)
    before = sorted(path.relative_to(root.parent) for path in root.parent.rglob("*"))
    with pytest.raises(StorageNameError, match="must stay below the store's refs/"):
        refs.read(name)
    with pytest.raises(StorageNameError, match="must stay below the store's refs/"):
        refs.swap(name, expected_version=None, target=target)
    assert sorted(path.relative_to(root.parent) for path in root.parent.rglob("*")) == before


def test_the_suffixes_keep_every_valid_name_below_refs(root: Path) -> None:
    refs = LocalRefStore(root)
    for name in ("a", "a.json/b", "a..b/c", "a/b.d", "libraries/x.json/head"):
        directories, file_name = refs._location(name)
        assert all(part.endswith(".d") for part in directories)
        assert file_name.endswith(".json")
        assert len(directories) == name.count("/")

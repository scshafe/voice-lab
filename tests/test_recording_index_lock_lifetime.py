"""Real child-process lock probes cover SQLite descriptor ownership on Unix."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cole_voice_lab.errors import SecurityError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_index import RecordingIndex

# SQLite's Unix WAL dead-man-switch byte and database shared-lock range.
_SQLITE_LOCKS = (("-shm", 128, 1), ("", 1_073_741_826, 510))
_LOCK_PROBE = """
import fcntl, os, sys
descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)
try:
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB,
                    int(sys.argv[3]), int(sys.argv[2]), os.SEEK_SET)
    except BlockingIOError:
        print('blocked')
    else:
        print('available')
finally:
    os.close(descriptor)
"""


def _assert_sqlite_locks_held(index: RecordingIndex) -> None:
    for suffix, offset, length in _SQLITE_LOCKS:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                _LOCK_PROBE,
                str(index.path) + suffix,
                str(offset),
                str(length),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.stdout.strip() == "blocked", (suffix, result.stdout, result.stderr)


def test_path_guards_preserve_sqlite_locks_through_transactions(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private-assets")
    with RecordingIndex(layout) as index:
        _assert_sqlite_locks_held(index)
        with index._transaction() as connection:
            connection.execute("SELECT * FROM recording_index_states").fetchall()
            index._check_paths()
            _assert_sqlite_locks_held(index)
        _assert_sqlite_locks_held(index)


@pytest.mark.parametrize("constructor_fails", [False, True])
def test_second_instance_cleanup_preserves_live_connection_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constructor_fails: bool
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private-assets")
    with RecordingIndex(layout) as live:
        # A raw read transaction pins SQLite's locks without taking our application
        # flock, allowing the second constructor to exercise its normal ownership.
        live.connection.execute("BEGIN")
        live.connection.execute("SELECT * FROM recording_index_states").fetchall()
        try:
            _assert_sqlite_locks_held(live)
            if constructor_fails:

                def fail_audit(self: RecordingIndex) -> None:
                    raise SecurityError("synthetic constructor audit failure")

                monkeypatch.setattr(RecordingIndex, "_audit", fail_audit)
                with pytest.raises(SecurityError, match="synthetic constructor audit failure"):
                    RecordingIndex(layout)
            else:
                with RecordingIndex(layout):
                    _assert_sqlite_locks_held(live)
            _assert_sqlite_locks_held(live)
        finally:
            live.connection.execute("ROLLBACK")


def test_close_waits_for_the_index_transaction_mutex(tmp_path: Path) -> None:
    index = RecordingIndex(AssetLayout.initialize(tmp_path / "private-assets"))
    original_mutex = index._mutex
    entered = threading.Event()
    errors: list[BaseException] = []

    class ObservedMutex:
        def __enter__(self) -> None:
            entered.set()
            original_mutex.acquire()

        def __exit__(self, *_: object) -> None:
            original_mutex.release()

    index._mutex = ObservedMutex()  # type: ignore[assignment]

    def close_index() -> None:
        try:
            index.close()
        except BaseException as error:
            errors.append(error)

    closer = threading.Thread(target=close_index)
    try:
        with original_mutex:
            closer.start()
            assert entered.wait(10), "close never attempted the transaction mutex"
            assert not index._closed
            assert index._connection is not None
        closer.join(timeout=10)
        assert not closer.is_alive()
        assert not errors
        assert index._closed
    finally:
        closer.join(timeout=10)
        index.close()


def test_new_database_replacement_is_rejected_before_sqlite_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private-assets")
    path = layout.directory("provenance") / "recording-library" / "recording-library.sqlite3"
    retained = tmp_path / "original-created-database"
    original_close = os.close
    replaced = False

    def replace_on_creation_close(descriptor: int) -> None:
        nonlocal replaced
        metadata = os.fstat(descriptor)
        if not replaced and path.exists():
            current = path.stat(follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) == (current.st_dev, current.st_ino):
                replaced = True
                path.rename(retained)
                path.touch(mode=0o600)
        original_close(descriptor)

    monkeypatch.setattr(os, "close", replace_on_creation_close)
    with pytest.raises(SecurityError, match="database was replaced"):
        RecordingIndex(layout)
    assert replaced
    assert path.read_bytes() == b""
    assert retained.read_bytes() == b""


@pytest.mark.parametrize("member", [".index.lock", ".index.lease"])
def test_disappeared_guard_is_rejected_before_query(tmp_path: Path, member: str) -> None:
    layout = AssetLayout.initialize(tmp_path / "private-assets")
    with RecordingIndex(layout) as index:
        (index.directory / member).rename(tmp_path / (member + "-retained"))
        with pytest.raises(SecurityError, match="member disappeared"):
            index.analysis_jobs()


def test_a_new_index_on_btrfs_opens_and_its_checks_see_strays_made_since_the_last(
    btrfs_directory: Path,
) -> None:
    """#112, as for the corpus store: btrfs lists through a descriptor only what was there
    when it was opened or when the previous listing through it ended."""
    layout = AssetLayout.initialize(btrfs_directory / "private-assets")
    with RecordingIndex(layout) as index:
        index.analysis_jobs()
        stray = index.directory / "notes.txt"
        stray.touch(mode=0o600)
        with pytest.raises(SecurityError, match="unexpected recording index member"):
            index._check_paths()  # the check a query starts with
        stray.unlink()
        index.analysis_jobs()

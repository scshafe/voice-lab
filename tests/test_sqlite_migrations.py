"""Resource lookup, marker parsing and atomic user_version stages of the shared runner."""

from __future__ import annotations

import hashlib
import os
import pickle
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

import pytest

import cole_voice_lab.sqlite_migrations as sqlite_migrations
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.sqlite_migrations import (
    SqliteMigrationError,
    apply_migration,
    current_user_version,
    migration_bytes,
    migration_resource,
    migration_resources,
    migration_sql,
)

INDEX_MIGRATION_NAME = "20260911070000_create_recording_library_projection.sql"
PROBE_NAME = "20990101000000_create_probe_rows.sql"
HEADER = "-- Probe migration written by tests; never packaged.\n"
UP_MARKER = "-- +migrate Up\n"
DOWN_MARKER = "-- +migrate Down\n"
UP = "CREATE TABLE IF NOT EXISTS probe_rows (probe_id INTEGER PRIMARY KEY);\n"
DOWN = "DROP TABLE IF EXISTS probe_rows;\n"
# Sets user_version inside the Up section (as index migrations 1 and 3 do) before failing, so
# a version left at 0 proves the pragma was rolled back rather than never reached.
BROKEN_UP = UP + "PRAGMA user_version = 1;\n" + "INSERT INTO absent_rows VALUES (1);\n"
CHECKOUT = Path(sqlite_migrations.__file__).resolve().parents[2]


def _write_migration(directory: Path, name: str = PROBE_NAME, *, text: str | None = None) -> Path:
    path = directory / name
    path.write_text((HEADER + UP_MARKER + UP + DOWN_MARKER + DOWN) if text is None else text)
    return path


def _resolver(directory: Path) -> Callable[[str], Traversable]:
    return lambda name: directory / name


@dataclass(frozen=True, slots=True)
class Store:
    path: Path
    connection: sqlite3.Connection
    directory_fd: int

    def table_names(self) -> tuple[str, ...]:
        rows = self.connection.execute("SELECT name FROM sqlite_schema ORDER BY name").fetchall()
        return tuple(row[0] for row in rows)


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture
def unpackaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the packaged lookup at an empty package so only the checkout copy can resolve."""
    package = tmp_path / "empty-package"
    package.mkdir(mode=0o700)
    monkeypatch.setattr(sqlite_migrations, "files", lambda _package: package)
    return package


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    directory = tmp_path / "store"
    directory.mkdir(mode=0o700)
    path = directory / "probe.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield Store(path, connection, directory_fd)
    finally:
        connection.close()
        os.close(directory_fd)


def test_error_carries_a_namespaced_code_and_a_plain_message() -> None:
    error = SqliteMigrationError("SQLITE_MIGRATIONS.MALFORMED", "probe message")
    assert isinstance(error, ContractError)
    assert error.code == "SQLITE_MIGRATIONS.MALFORMED"
    assert str(error) == "probe message"


def test_error_survives_a_pickle_round_trip_with_code_and_message() -> None:
    error = SqliteMigrationError("SQLITE_MIGRATIONS.APPLY_FAILED", "probe message")
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is SqliteMigrationError
    assert restored.code == "SQLITE_MIGRATIONS.APPLY_FAILED"
    assert str(restored) == "probe message"


def test_packaged_copy_wins_over_the_checkout_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "package" / "resources" / "recording_migrations"
    packaged.mkdir(parents=True)
    expected = _write_migration(packaged, INDEX_MIGRATION_NAME)
    monkeypatch.setattr(sqlite_migrations, "files", lambda _package: tmp_path / "package")
    assert Path(str(migration_resource(INDEX_MIGRATION_NAME))) == expected


def test_checkout_copy_is_used_when_nothing_is_packaged(unpackaged: Path) -> None:
    resource = migration_resource(INDEX_MIGRATION_NAME)
    assert Path(str(resource)) == CHECKOUT / "migrations" / INDEX_MIGRATION_NAME
    assert resource.is_file()


@pytest.mark.parametrize(
    "module_location",
    [
        "site-packages/cole_voice_lab/sqlite_migrations.py",
        "src/cole_voice_lab/sqlite_migrations.py",
    ],
    ids=["installed-without-resources", "src-layout-without-pyproject"],
)
def test_missing_packaged_copy_outside_a_checkout_fails_as_resource_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unpackaged: Path, module_location: str
) -> None:
    monkeypatch.setattr(sqlite_migrations, "__file__", str(tmp_path / module_location))
    with pytest.raises(SqliteMigrationError, match="resource is missing") as caught:
        migration_resource(INDEX_MIGRATION_NAME)
    assert caught.value.code == "SQLITE_MIGRATIONS.RESOURCE_MISSING"
    assert INDEX_MIGRATION_NAME in str(caught.value)


def test_absent_checkout_file_is_returned_unchecked_and_refused_at_read(unpackaged: Path) -> None:
    # The lookup only decides where a migration lives; open_regular_no_follow is the one gate
    # on what is there, so an absent checkout file fails closed exactly like a symlink.
    name = "20990101000000_never_written.sql"
    assert Path(str(migration_resource(name))) == CHECKOUT / "migrations" / name
    with pytest.raises(SecurityError, match="cannot safely open regular file"):
        migration_bytes(name)


def test_bytes_are_read_exactly_from_the_resolved_file(migrations: Path) -> None:
    path = _write_migration(migrations)
    assert migration_bytes(PROBE_NAME, resolve=_resolver(migrations)) == path.read_bytes()


def test_symlinked_migration_is_refused(migrations: Path) -> None:
    _write_migration(migrations)
    (migrations / "alias.sql").symlink_to(migrations / PROBE_NAME)
    with pytest.raises(SecurityError, match="cannot safely open regular file"):
        migration_bytes("alias.sql", resolve=_resolver(migrations))


def test_resource_fingerprints_keep_order_and_cover_whole_files(migrations: Path) -> None:
    first = _write_migration(migrations, "20990101000000_first.sql")
    second = _write_migration(migrations, "20990102000000_second.sql", text=HEADER + "x")
    fingerprints = migration_resources((second.name, first.name), resolve=_resolver(migrations))
    assert fingerprints == (
        (
            f"migrations/{second.name}",
            hashlib.sha256(second.read_bytes()).hexdigest(),
            len(second.read_bytes()),
        ),
        (
            f"migrations/{first.name}",
            hashlib.sha256(first.read_bytes()).hexdigest(),
            len(first.read_bytes()),
        ),
    )


def test_sql_sections_split_on_the_markers_and_drop_the_header(migrations: Path) -> None:
    _write_migration(migrations)
    resolve = _resolver(migrations)
    assert migration_sql(PROBE_NAME, "up", resolve=resolve) == UP
    assert migration_sql(PROBE_NAME, "down", resolve=resolve) == DOWN


@pytest.mark.parametrize(
    "text, message",
    [
        (HEADER + UP + DOWN_MARKER + DOWN, "lacks the marker '-- +migrate Up'"),
        (HEADER + UP_MARKER + UP + DOWN, "lacks the marker '-- +migrate Down'"),
        (
            HEADER + DOWN_MARKER + DOWN + UP_MARKER + UP,
            "has the marker '-- +migrate Down' before '-- +migrate Up'",
        ),
    ],
    ids=["no-up-marker", "no-down-marker", "down-before-up"],
)
def test_file_without_both_markers_in_order_is_malformed(
    migrations: Path, text: str, message: str
) -> None:
    _write_migration(migrations, text=text)
    with pytest.raises(SqliteMigrationError) as caught:
        migration_sql(PROBE_NAME, "up", resolve=_resolver(migrations))
    assert caught.value.code == "SQLITE_MIGRATIONS.MALFORMED"
    assert str(caught.value) == f"sqlite migration {PROBE_NAME} {message}"


def test_file_that_is_not_utf8_is_malformed(migrations: Path) -> None:
    (migrations / PROBE_NAME).write_bytes(b"\xff" + (UP_MARKER + UP + DOWN_MARKER).encode())
    with pytest.raises(SqliteMigrationError, match="not UTF-8") as caught:
        migration_sql(PROBE_NAME, "down", resolve=_resolver(migrations))
    assert caught.value.code == "SQLITE_MIGRATIONS.MALFORMED"


def test_user_version_reads_the_pragma(store: Store) -> None:
    assert current_user_version(store.connection) == 0
    store.connection.execute("PRAGMA user_version = 7")
    assert current_user_version(store.connection) == 7


def test_stage_creates_schema_advances_version_and_leaves_no_transaction(store: Store) -> None:
    apply_migration(store.connection, version=1, up_sql=UP, directory_fd=store.directory_fd)
    assert current_user_version(store.connection) == 1
    assert store.table_names() == ("probe_rows",)
    assert not store.connection.in_transaction
    apply_migration(store.connection, version=2, up_sql=DOWN, directory_fd=store.directory_fd)
    assert current_user_version(store.connection) == 2
    assert store.table_names() == ()


def test_stage_without_a_directory_descriptor_leaves_the_sync_to_the_caller(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", synced.append)
    apply_migration(store.connection, version=1, up_sql=UP, directory_fd=None)
    assert synced == []
    assert current_user_version(store.connection) == 1
    assert store.table_names() == ("probe_rows",)
    apply_migration(store.connection, version=2, up_sql=DOWN, directory_fd=store.directory_fd)
    assert synced == [store.directory_fd]


def test_failed_statement_rolls_the_stage_back_and_keeps_the_connection_usable(
    store: Store,
) -> None:
    with pytest.raises(SqliteMigrationError, match="rolled back") as caught:
        apply_migration(
            store.connection, version=1, up_sql=BROKEN_UP, directory_fd=store.directory_fd
        )
    assert caught.value.code == "SQLITE_MIGRATIONS.APPLY_FAILED"
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert current_user_version(store.connection) == 0
    assert store.table_names() == ()
    assert not store.connection.in_transaction
    apply_migration(store.connection, version=1, up_sql=UP, directory_fd=store.directory_fd)
    assert current_user_version(store.connection) == 1


def test_stage_out_of_sequence_is_refused_before_touching_the_database(store: Store) -> None:
    for version in (0, 2):
        with pytest.raises(SqliteMigrationError, match="cannot follow user_version 0") as caught:
            apply_migration(
                store.connection, version=version, up_sql=UP, directory_fd=store.directory_fd
            )
        assert caught.value.code == "SQLITE_MIGRATIONS.UNSUPPORTED_VERSION"
    assert current_user_version(store.connection) == 0
    assert store.table_names() == ()


def test_stage_inside_an_open_transaction_is_refused_without_committing_it(
    store: Store,
) -> None:
    store.connection.execute("BEGIN")
    store.connection.execute(UP)
    with pytest.raises(SqliteMigrationError, match="already open") as caught:
        apply_migration(store.connection, version=1, up_sql=DOWN, directory_fd=store.directory_fd)
    assert caught.value.code == "SQLITE_MIGRATIONS.APPLY_FAILED"
    assert store.connection.in_transaction
    store.connection.execute("ROLLBACK")
    assert store.table_names() == ()
    assert current_user_version(store.connection) == 0


def test_locked_database_fails_the_stage_without_a_dangling_transaction(store: Store) -> None:
    holder = sqlite3.connect(store.path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        waiter = sqlite3.connect(store.path, isolation_level=None, timeout=0.0)
        try:
            with pytest.raises(SqliteMigrationError, match="database is locked") as caught:
                apply_migration(waiter, version=1, up_sql=UP, directory_fd=store.directory_fd)
            assert caught.value.code == "SQLITE_MIGRATIONS.APPLY_FAILED"
            # BEGIN IMMEDIATE itself lost to the holder, so nothing was rolled back.
            assert "failed with no transaction to roll back" in str(caught.value)
            assert "rolled back:" not in str(caught.value)
            assert not waiter.in_transaction
            holder.execute("ROLLBACK")
            apply_migration(waiter, version=1, up_sql=UP, directory_fd=store.directory_fd)
            assert current_user_version(waiter) == 1
        finally:
            waiter.close()
    finally:
        holder.close()


def test_directory_sync_failure_after_commit_is_reported_with_the_version_stored(
    store: Store,
) -> None:
    # A pipe end is an open descriptor that fsync refuses; it stands in for a directory
    # whose sync fails after the stage has already committed.
    unsyncable, other_end = os.pipe()
    try:
        with pytest.raises(SqliteMigrationError, match="committed but") as caught:
            apply_migration(store.connection, version=1, up_sql=UP, directory_fd=unsyncable)
    finally:
        os.close(unsyncable)
        os.close(other_end)
    assert caught.value.code == "SQLITE_MIGRATIONS.APPLY_FAILED"
    assert isinstance(caught.value.__cause__, OSError)
    assert current_user_version(store.connection) == 1
    assert not store.connection.in_transaction


@pytest.mark.serial
def test_real_index_migrations_parse_and_apply_through_the_shared_runner(store: Store) -> None:
    names = (
        INDEX_MIGRATION_NAME,
        "20260911080000_create_recording_analysis_queue.sql",
        "20260913090000_create_recording_recovery_inventory.sql",
    )
    assert tuple(entry[0] for entry in migration_resources(names)) == tuple(
        f"migrations/{name}" for name in names
    )
    for version, name in enumerate(names, start=1):
        apply_migration(
            store.connection,
            version=version,
            up_sql=migration_sql(name, "up"),
            directory_fd=store.directory_fd,
        )
    assert current_user_version(store.connection) == 3
    assert "recording_recovery_findings" in store.table_names()

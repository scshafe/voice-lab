"""Shared PRAGMA user_version migration runner for the lab's private SQLite databases.

@canonical sqlite_migrations -- every PRAGMA user_version migration in the lab is applied
through this module; each database keeps its own version gate, seed rows and error wrapping.
Related: recording_index.py (first caller), migrations/ (the one maintained SQL source),
setup.py (copies migrations/*.sql into the wheel's resources/recording_migrations).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable, Sequence
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from .canonical import open_regular_no_follow
from .errors import ContractError

type SqliteMigrationCode = Literal[
    "SQLITE_MIGRATIONS.RESOURCE_MISSING",
    "SQLITE_MIGRATIONS.MALFORMED",
    "SQLITE_MIGRATIONS.UNSUPPORTED_VERSION",
    "SQLITE_MIGRATIONS.APPLY_FAILED",
]
type MigrationDirection = Literal["up", "down"]

_PACKAGE = "cole_voice_lab"
_PACKAGED_DIRECTORY = ("resources", "recording_migrations")
_SOURCE_DIRECTORY = "migrations"
_UP_MARKER = "-- +migrate Up\n"
_DOWN_MARKER = "-- +migrate Down\n"


class SqliteMigrationError(ContractError):
    """A migration file could not be read or one stage could not be applied.

    ``code`` names the failure: ``SQLITE_MIGRATIONS.RESOURCE_MISSING`` (no packaged copy
    and this module is not running from a source checkout), ``SQLITE_MIGRATIONS.MALFORMED``
    (bytes are not UTF-8, a marker is absent or the markers are out of order),
    ``SQLITE_MIGRATIONS.UNSUPPORTED_VERSION`` (a stage out of sequence) and
    ``SQLITE_MIGRATIONS.APPLY_FAILED`` (SQLite refused the stage, a transaction was already
    open, or the directory sync after the commit failed). Database modules wrap it in their
    own named error; ``str(error)`` is the message alone. Instances pickle, so one raised
    in a worker process arrives intact.
    """

    def __init__(self, code: SqliteMigrationCode, message: str) -> None:
        self.code: SqliteMigrationCode = code
        super().__init__(message)

    def __reduce__(self) -> tuple[type[SqliteMigrationError], tuple[SqliteMigrationCode, str]]:
        return (type(self), (self.code, str(self)))


def migration_resource(name: str) -> Traversable:
    """Locate one migration file by resource name, packaged copy first.

    Params: ``name`` is the timestamped file name, for example
    ``20260911070000_create_recording_library_projection.sql``.
    Returns: the packaged ``resources/recording_migrations/<name>`` when the installed
    wheel carries it (setup.py copies every ``migrations/*.sql`` there), otherwise
    ``<checkout>/migrations/<name>`` when this module runs from a ``src`` checkout that has
    a ``pyproject.toml`` beside it. The checkout path is returned unchecked: whether it is
    a readable regular file is decided by ``open_regular_no_follow`` in ``migration_bytes``
    (an absent, symlinked or otherwise irregular file there is a ``SecurityError``).
    Raises: ``SqliteMigrationError`` with ``SQLITE_MIGRATIONS.RESOURCE_MISSING`` when no
    packaged copy exists and the module is not in a checkout; a checkout is recognised only
    by that layout, never guessed.
    """
    packaged = files(_PACKAGE).joinpath(*_PACKAGED_DIRECTORY, name)
    if packaged.is_file():
        return packaged
    source_file = Path(__file__).resolve()
    source_root = source_file.parents[2]
    if source_file.parents[1].name != "src" or not (source_root / "pyproject.toml").is_file():
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.RESOURCE_MISSING",
            f"packaged sqlite migration resource is missing: {name}",
        )
    return source_root / _SOURCE_DIRECTORY / name


def migration_bytes(
    name: str, *, resolve: Callable[[str], Traversable] = migration_resource
) -> bytes:
    """Read one migration's exact bytes without following a final symlink.

    Params: ``name`` is the resource name; ``resolve`` is the lookup, ``migration_resource``
    unless a database module passes its own wrapper (its tests substitute resources there).
    Returns: the whole file, header comment and both directions included.
    Raises: whatever ``resolve`` raises (``SQLITE_MIGRATIONS.RESOURCE_MISSING`` by default);
    ``SecurityError`` from ``open_regular_no_follow`` when the resolved path is a symlink or
    not a regular file.
    """
    with as_file(resolve(name)) as path, open_regular_no_follow(path) as stream:
        return stream.read()


def migration_resources(
    names: Sequence[str], *, resolve: Callable[[str], Traversable] = migration_resource
) -> tuple[tuple[str, str, int], ...]:
    """Fingerprint runtime migration bytes so they are part of a build identity.

    Params: ``names`` are resource names in application order; ``resolve`` as in
    ``migration_bytes``.
    Returns: one ``("migrations/<name>", sha256 hex, byte length)`` per name, in the given
    order; the whole file is hashed so reversible down code is fingerprinted too.
    Raises: as ``migration_bytes``.
    """
    return tuple(
        (f"{_SOURCE_DIRECTORY}/{name}", hashlib.sha256(payload).hexdigest(), len(payload))
        for name in names
        for payload in (migration_bytes(name, resolve=resolve),)
    )


def migration_sql(
    name: str,
    direction: MigrationDirection,
    *,
    resolve: Callable[[str], Traversable] = migration_resource,
) -> str:
    """Return one direction of a migration file with its header comment discarded.

    A file reads ``<header>-- +migrate Up\\n<up>-- +migrate Down\\n<down>``; both markers
    must appear exactly, each on its own line, Up before Down.
    Params: ``name`` is the resource name; ``direction`` is ``"up"`` or ``"down"``;
    ``resolve`` as in ``migration_bytes``.
    Returns: the SQL text between the markers (up) or after the Down marker (down).
    Raises: ``SqliteMigrationError`` with ``SQLITE_MIGRATIONS.MALFORMED`` when the bytes are
    not UTF-8, a marker is absent, or the Down marker precedes the Up marker;
    ``migration_bytes`` failures otherwise.
    """
    payload = migration_bytes(name, resolve=resolve)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.MALFORMED", f"sqlite migration {name} is not UTF-8: {error}"
        ) from error
    head, up_marker, body = text.partition(_UP_MARKER)
    if not up_marker:
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.MALFORMED",
            f"sqlite migration {name} lacks the marker {_UP_MARKER.strip()!r}",
        )
    if _DOWN_MARKER in head:
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.MALFORMED",
            f"sqlite migration {name} has the marker {_DOWN_MARKER.strip()!r} before "
            f"{_UP_MARKER.strip()!r}",
        )
    up, down_marker, down = body.partition(_DOWN_MARKER)
    if not down_marker:
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.MALFORMED",
            f"sqlite migration {name} lacks the marker {_DOWN_MARKER.strip()!r}",
        )
    return {"up": up, "down": down}[direction]


def current_user_version(connection: sqlite3.Connection) -> int:
    """Read ``PRAGMA user_version``; 0 means no migration has versioned the database.

    Params: ``connection`` is an open connection; read it under the database's exclusive
    lock whenever the value decides which stages are pending.
    Returns: the stored integer.
    Raises: ``sqlite3.Error`` unchanged when the database cannot be read.
    """
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def apply_migration(
    connection: sqlite3.Connection, *, version: int, up_sql: str, directory_fd: int | None
) -> None:
    """Apply one Up stage atomically and advance ``user_version`` to ``version``.

    Runs ``BEGIN IMMEDIATE; <up_sql> PRAGMA user_version = <version>; COMMIT;`` as one
    script, then ``os.fsync(directory_fd)`` so directory entries the stage created (journal
    or WAL files) are durable. The caller holds the database's exclusive lock and decides
    which stages are pending; this function never inspects the schema.
    Params: ``connection`` is an autocommit connection (``isolation_level=None``) with no
    open transaction; ``version`` must be exactly ``current_user_version(connection) + 1``;
    ``up_sql`` comes from ``migration_sql(name, "up")``; ``directory_fd`` is an open
    descriptor of the directory holding the database file, or ``None`` when the caller
    performs the directory sync itself after its own autocommit follow-up rows (the
    recording index seeds a row after its first stage and syncs once after the seed).
    Returns: nothing; afterwards ``current_user_version(connection) == version``.
    Raises: ``SqliteMigrationError`` with ``SQLITE_MIGRATIONS.APPLY_FAILED`` before touching
    the database when a transaction is already open (``executescript`` would commit it
    silently); ``SQLITE_MIGRATIONS.UNSUPPORTED_VERSION`` before touching the database when
    ``version`` is out of sequence; ``SQLITE_MIGRATIONS.APPLY_FAILED`` when a statement
    fails (the stage is rolled back, ``user_version`` is unchanged and the connection is
    reusable; the message says whether this function rolled back or no transaction was
    open, as when ``BEGIN IMMEDIATE`` itself lost to another writer) or when the directory
    sync fails after the commit (the version is stored).
    """
    if connection.in_transaction:
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.APPLY_FAILED",
            f"sqlite migration {version} refused: a transaction is already open",
        )
    current = current_user_version(connection)
    if version != current + 1:
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.UNSUPPORTED_VERSION",
            f"sqlite migration {version} cannot follow user_version {current}",
        )
    script = f"BEGIN IMMEDIATE;\n{up_sql}\nPRAGMA user_version = {version};\nCOMMIT;"
    try:
        connection.executescript(script)
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
            outcome = "failed and was rolled back"
        else:
            outcome = "failed with no transaction to roll back"
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.APPLY_FAILED", f"sqlite migration {version} {outcome}: {error}"
        ) from error
    if directory_fd is None:
        return
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise SqliteMigrationError(
            "SQLITE_MIGRATIONS.APPLY_FAILED",
            f"sqlite migration {version} committed but its directory did not sync: {error}",
        ) from error

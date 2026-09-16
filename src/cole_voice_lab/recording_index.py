"""Rebuildable metadata projection behind RecordingLibrary's indexed capability.

@canonical recording_index -- SQLite serves bounded coherent metadata snapshots;
immutable take/legacy evidence owns decisions, source identities and raw authority.
Related: recording_takes.py, recording_library.py, recording_index_backfill.py,
sqlite_migrations.py (the shared user_version runner behind the index's schema stages).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from .canonical import canonical_json_bytes, canonical_sha256
from .errors import ContractError, SecurityError
from .models import Prompt, PromptSuite, StableId
from .paths import AssetLayout, validate_relative_path
from .recording_contracts import (
    MAX_HISTORY_PAGE_SIZE,
    AnalysisState,
    DecisionState,
    RecordingError,
    RecordingGroupSummary,
    RecordingHistoryEntry,
    RecordingHistoryPage,
    RecordingNavigationResult,
    RecordingPromptSummary,
    RecordingRecoveryFinding,
    RecordingRecoveryInventoryPage,
    RecordingRecoveryObservation,
    RecordingRecoveryStatus,
    RecordingRecoveryWork,
    RecordingSelection,
    RecordingTake,
)
from .sqlite_migrations import (
    SqliteMigrationError,
    apply_migration,
    current_user_version,
    migration_bytes,
    migration_resource,
    migration_resources,
    migration_sql,
)

_MIGRATION_NAME = "20260911070000_create_recording_library_projection.sql"
_ANALYSIS_MIGRATION_NAME = "20260911080000_create_recording_analysis_queue.sql"
_RECOVERY_MIGRATION_NAME = "20260913090000_create_recording_recovery_inventory.sql"
# Index schema versions 1..3 in application order; the corpus store versions its own file.
_INDEX_MIGRATION_NAMES = (_MIGRATION_NAME, _ANALYSIS_MIGRATION_NAME, _RECOVERY_MIGRATION_NAME)
_DATABASE_NAME = "recording-library.sqlite3"
_MAX_CATALOG_PROMPTS = 10_000
# Leaves space for the response envelope/correlation IDs below the 1 MiB frame.
_MAX_HISTORY_PAYLOAD_BYTES = 900_000
_MAX_PREPARED_LEGACY_BYTES = 32 * 1024 * 1024
_MAX_PREPARED_LEGACY_RECORDS = 200_000
_LEGACY_COLLISION_QUERY_BATCH = 500
_IndexState = Literal["complete", "building", "unknown"]
_AliasKind = Literal[
    "native-original", "legacy-tmp", "legacy-raw", "rejected", "package-tmp", "package-raw"
]
_RETAINED = "recording_take_decision = 'kept' AND recording_take_analysis != 'invalid'"


def is_retained(decision: DecisionState, analysis: AnalysisState) -> bool:
    """The coverage predicate of _RETAINED for readers that never open this index."""
    return decision == "kept" and analysis != "invalid"


class RecordingIndexError(ContractError):
    """Named metadata failure; callers must not turn this into empty coverage."""

    def __init__(self, message: str, *, stale: bool = False) -> None:
        self.recording_error = RecordingError(
            code="RECORDING.STALE_REVISION" if stale else "RECORDING.HISTORY_UNKNOWN",
            message=message[:1024],
            retryable=stale,
        )
        super().__init__(message)


class RecordingLegacyPublicationCapacityError(RecordingIndexError):
    """A validated legacy projection exceeds the bounded publication capacity."""


@dataclass(frozen=True, slots=True)
class _PreparedProjection:
    prompt_sha256: str
    prompt_json: str
    entry_json: str
    entry_sha256: str

    @classmethod
    def from_entry(cls, entry: RecordingHistoryEntry) -> _PreparedProjection:
        prompt = canonical_json_bytes(entry.prompt)
        payload = canonical_json_bytes(entry)
        return cls(
            hashlib.sha256(prompt).hexdigest(),
            prompt.decode(),
            payload.decode(),
            hashlib.sha256(payload).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class _PreparedLegacyEntry:
    entry: RecordingHistoryEntry
    opened_at: datetime | None
    projection: _PreparedProjection
    aliases: tuple[tuple[str, str, _AliasKind, str], ...]


@dataclass(frozen=True, slots=True)
class _PreparedLegacyInsertions:
    take_ids: tuple[str, ...]
    alias_ids: tuple[str, ...]
    prompts: tuple[tuple[str, str, str], ...]
    groups: tuple[tuple[str | None, ...], ...]
    takes: tuple[tuple[str | int | None, ...], ...]
    aliases: tuple[tuple[str, str, str, str, str], ...]


@dataclass(frozen=True, slots=True)
class _PreparedLegacyCatalog:
    entries: tuple[_PreparedLegacyEntry, ...]
    groups: tuple[tuple[str, str, str | None, str], ...]
    errors: tuple[tuple[str, str], ...]
    insertions: _PreparedLegacyInsertions | None


def _prepare_legacy_insertions(
    entries: tuple[_PreparedLegacyEntry, ...],
) -> _PreparedLegacyInsertions | None:
    """Prepare a bulk insertion only for unique identities; collisions keep serial guards."""
    take_ids: set[str] = set()
    alias_ids: set[str] = set()
    prompts: dict[str, tuple[str, str, str]] = {}
    groups: dict[str, tuple[str | None, ...]] = {}
    takes: list[tuple[str | int | None, ...]] = []
    aliases: list[tuple[str, str, str, str, str]] = []
    for item in entries:
        entry = item.entry
        if entry.take_id in take_ids:
            return None
        take_ids.add(entry.take_id)
        projection = item.projection
        prompts.setdefault(
            projection.prompt_sha256,
            (projection.prompt_sha256, entry.prompt.prompt_id, projection.prompt_json),
        )
        group_key = _group_key(entry)
        groups.setdefault(
            group_key,
            (
                group_key,
                entry.subject_id,
                entry.group_id,
                entry.legacy_session_id,
                None if item.opened_at is None else item.opened_at.isoformat(),
                "unknown" if item.opened_at is None else "session",
            ),
        )
        takes.append(
            (
                entry.take_id,
                entry.subject_id,
                group_key,
                projection.prompt_sha256,
                entry.catalog_sha256,
                "legacy",
                None if entry.recorded_at is None else entry.recorded_at.isoformat(),
                entry.decision,
                entry.analysis,
                entry.state_revision,
                projection.entry_json,
                projection.entry_sha256,
            )
        )
        for path, digest, kind, alias_id in item.aliases:
            if alias_id in alias_ids:
                return None
            alias_ids.add(alias_id)
            aliases.append((alias_id, entry.take_id, path, kind, digest))
    return _PreparedLegacyInsertions(
        tuple(take_ids),
        tuple(alias_ids),
        tuple(prompts.values()),
        tuple(groups.values()),
        tuple(takes),
        tuple(aliases),
    )


def _prepare_legacy_catalog(
    subject_id: str,
    entries: Iterable[
        tuple[RecordingHistoryEntry, datetime | None, tuple[tuple[str, str, _AliasKind], ...]]
    ],
    groups: Iterable[RecordingGroupSummary],
    unknown_errors: Iterable[tuple[str, RecordingError]],
) -> _PreparedLegacyCatalog:
    """Consume every caller iterator and encode incoming metadata before taking the index lock."""
    byte_count = 0
    record_count = 0

    def account(payload: bytes) -> None:
        nonlocal byte_count, record_count
        byte_count += len(payload)
        record_count += 1
        if byte_count > _MAX_PREPARED_LEGACY_BYTES or record_count > _MAX_PREPARED_LEGACY_RECORDS:
            raise RecordingLegacyPublicationCapacityError(
                "legacy publication exceeds its prepared metadata capacity "
                "(32 MiB or 200,000 records); prior history is preserved"
            )

    prepared_entries = []
    for entry, opened_at, aliases in entries:
        if entry.subject_id != subject_id or entry.legacy_session_id is None:
            raise RecordingIndexError(
                "legacy projection has an incompatible subject or event identity"
            )
        if len(aliases) > 32:
            raise RecordingIndexError("legacy projection has too many source aliases")
        account(
            canonical_json_bytes(
                [
                    entry.model_dump(mode="json"),
                    entry.prompt.model_dump(mode="json"),
                    None if opened_at is None else opened_at.isoformat(),
                    aliases,
                ]
            )
        )
        prepared_aliases = []
        for path, digest, kind in aliases:
            validate_relative_path(path)
            if kind not in ("legacy-raw", "legacy-tmp", "rejected"):
                raise RecordingIndexError("legacy projection has a non-legacy source alias")
            if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
                raise RecordingIndexError("legacy source alias has an invalid SHA-256 identity")
            prepared_aliases.append(
                (path, digest, kind, canonical_sha256({"path": path, "sha256": digest}))
            )
        prepared_entries.append(
            _PreparedLegacyEntry(
                entry, opened_at, _PreparedProjection.from_entry(entry), tuple(prepared_aliases)
            )
        )
    prepared_groups = []
    for group in groups:
        account(canonical_json_bytes(group))
        if group.legacy_session_id is None or group.group_id is not None:
            raise RecordingIndexError("legacy backfill cannot import a native group identity")
        prepared_groups.append(
            (
                f"legacy:{group.legacy_session_id}",
                group.legacy_session_id,
                None if group.opened_at is None else group.opened_at.isoformat(),
                group.date_precision,
            )
        )
    prepared_errors = []
    for record_id, error in unknown_errors:
        if (
            not record_id
            or len(record_id) > 1024
            or not (record_id.startswith("legacy:") or record_id == "legacy-backfill")
        ):
            raise RecordingIndexError("legacy Unknown identity is outside its owned namespace")
        account(canonical_json_bytes([record_id, error.model_dump(mode="json")]))
        prepared_errors.append((record_id, canonical_json_bytes(error).decode()))
    entries_tuple = tuple(prepared_entries)
    return _PreparedLegacyCatalog(
        entries_tuple,
        tuple(prepared_groups),
        tuple(prepared_errors),
        _prepare_legacy_insertions(entries_tuple),
    )


def _migration_resource(name: str = _MIGRATION_NAME) -> Traversable:
    """The index's lookup seam (tests substitute it); a missing copy is the index's error."""
    try:
        return migration_resource(name)
    except SqliteMigrationError as error:
        raise RecordingIndexError(
            "packaged recording index migration resource is missing"
        ) from error


def _migration_bytes(name: str = _MIGRATION_NAME) -> bytes:
    return migration_bytes(name, resolve=_migration_resource)


def recording_index_migration_resources() -> tuple[tuple[str, str, int], ...]:
    """Fingerprint the exact runtime SQL resource, including reversible down code."""
    return migration_resources(_INDEX_MIGRATION_NAMES, resolve=_migration_resource)


def recording_index_migration(direction: Literal["up", "down"] = "up", *, version: int = 1) -> str:
    """Read the single timestamped schema source (or its packaged build resource).

    Raises: ``RecordingIndexError`` for a direction other than up/down, a version outside
    1..3, or when neither a packaged nor a checkout copy of the file exists;
    ``SqliteMigrationError`` (``SQLITE_MIGRATIONS.MALFORMED``) when the file is not UTF-8 or
    its markers are absent or out of order; ``SecurityError`` when the resolved path is a
    symlink or not a regular file. ``RecordingIndex.__init__`` wraps the malformed case as
    ``cannot open recording index; rebuild required: …``.
    """
    if direction not in ("up", "down"):
        raise RecordingIndexError("migration direction must be up or down")
    if version not in (1, 2, 3):
        raise RecordingIndexError("unsupported recording migration version")
    return migration_sql(
        _INDEX_MIGRATION_NAMES[version - 1], direction, resolve=_migration_resource
    )


def _private_metadata(metadata: os.stat_result, label: str, *, directory: bool = False) -> None:
    valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (
        not valid_type
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or (not directory and metadata.st_nlink != 1)
    ):
        raise SecurityError(f"recording index requires an owned private file/directory: {label}")


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def history_entry_for_take(take: RecordingTake) -> RecordingHistoryEntry:
    """Project one native take head exactly as the index stores it."""
    seal = take.seal
    return RecordingHistoryEntry(
        take_id=take.reservation.take_id,
        subject_id=take.reservation.group.subject_id,
        group_id=take.reservation.group.group_id,
        legacy_session_id=None,
        catalog_sha256=take.reservation.group.catalog_sha256,
        prompt=take.reservation.prompt,
        recorded_at=None if seal is None else seal.first_sample_at,
        date_precision="unknown" if seal is None else "take",
        source_sha256=None if seal is None else seal.original_sha256,
        source_frames=None if seal is None else seal.audio.frames,
        decision=take.decision,
        analysis=take.analysis,
        export=take.export,
        state_revision=take.state_revision,
    )


def _group_key(entry: RecordingHistoryEntry) -> str:
    return (
        f"native:{entry.group_id}"
        if entry.group_id is not None
        else f"legacy:{entry.legacy_session_id}"
    )


class RecordingIndex:
    """Private, serialized SQLite cache with explicit completeness and bounded reads.

    Root and directory descriptors stay open for the lifetime of the connection.
    Every transaction rechecks no-follow path identities, owner/mode/link counts,
    and all SQLite sidecars. sqlite3 exposes no OPEN_NOFOLLOW flag; a private
    parent plus descriptor guards and an interprocess lock protect cooperating
    writers, while path substitutions are rejected before/after SQLite opens.
    This cache cannot grant accepted-raw or replace selected-source validation.
    """

    def __init__(self, layout: AssetLayout, *, subject_id: str = "cole-self") -> None:
        self.layout = AssetLayout.validate(layout.root)
        self.subject_id = subject_id
        self.directory = self.layout.directory("provenance") / "recording-library"
        self.path = self.directory / _DATABASE_NAME
        self._descriptors: list[int] = []
        self._directory_bindings: list[tuple[Path, int]] = []
        self._mutex = threading.RLock()
        self._legacy_mutation_epoch = 0
        self.recovery_wake = threading.Event()
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        try:
            for path in (self.layout.root, self.layout.directory("provenance")):
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                self._descriptors.append(descriptor)
                self._directory_bindings.append((path, descriptor))
                _private_metadata(os.fstat(descriptor), str(path), directory=True)
            provenance_fd = self._descriptors[-1]
            try:
                os.mkdir("recording-library", mode=0o700, dir_fd=provenance_fd)
                os.fsync(provenance_fd)
            except FileExistsError:
                pass  # Creation race is resolved by the no-follow owned-directory open below.
            directory_fd = os.open(
                "recording-library",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=provenance_fd,
            )
            self._descriptors.append(directory_fd)
            self._directory_bindings.append((self.directory, directory_fd))
            _private_metadata(os.fstat(directory_fd), str(self.directory), directory=True)
            self._lease_fd = self._open_private(".index.lease", os.O_RDWR | os.O_CREAT)
            fcntl.flock(self._lease_fd, fcntl.LOCK_SH)
            self._lock_fd = self._open_private(".index.lock", os.O_RDWR | os.O_CREAT)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                self._database_identity = self._bind_database()
                self._check_paths()
                self._connection = sqlite3.connect(
                    f"file:{quote(str(self.path), safe='/')}?mode=rw",
                    uri=True,
                    isolation_level=None,
                    timeout=10.0,
                    check_same_thread=False,
                )
                self._connection.row_factory = sqlite3.Row
                self._check_paths()
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute("PRAGMA trusted_schema = OFF")
                self._connection.execute("PRAGMA synchronous = FULL")
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA cache_size = -2048")
                self._connection.execute("PRAGMA temp_store = MEMORY")
                self._connection.execute("PRAGMA wal_autocheckpoint = 256")
                version = current_user_version(self._connection)
                if version not in (0, 1, 2, 3):
                    raise RecordingIndexError(
                        f"unsupported recording index schema version: {version}"
                    )
                if version == 0:
                    if self._connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                        raise RecordingIndexError("unversioned nonempty index requires rebuild")
                    # The index syncs the directory itself, after the autocommit seed row:
                    # a sync failure between the stage and the seed would otherwise leave a
                    # versioned database with no revision row that no later open can repair.
                    apply_migration(
                        self._connection,
                        version=1,
                        up_sql=recording_index_migration(),
                        directory_fd=None,
                    )
                    self._connection.execute(
                        "INSERT OR IGNORE INTO recording_index_states VALUES (1, ?, 0)",
                        (uuid4().hex,),
                    )
                    os.fsync(directory_fd)
                if version < 2:
                    apply_migration(
                        self._connection,
                        version=2,
                        up_sql=recording_index_migration(version=2),
                        directory_fd=directory_fd,
                    )
                if version < 3:
                    apply_migration(
                        self._connection,
                        version=3,
                        up_sql=recording_index_migration(version=3),
                        directory_fd=directory_fd,
                    )
                self._check_paths()
                self._audit()
                self._data_version = self._connection.execute("PRAGMA data_version").fetchone()[0]
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except (
            OSError,
            sqlite3.Error,
            ValidationError,
            ValueError,
            SqliteMigrationError,
        ) as error:
            self.close()
            raise RecordingIndexError(
                f"cannot open recording index; rebuild required: {error}"
            ) from error
        except BaseException:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Internal connection access, exposed for focused query-plan/transaction tests."""
        if self._connection is None or self._closed:
            raise RecordingIndexError("recording index is closed")
        return self._connection

    def _open_private(self, name: str, flags: int) -> int:
        descriptor = os.open(
            name,
            flags | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._directory_bindings[-1][1],
        )
        self._descriptors.append(descriptor)
        _private_metadata(os.fstat(descriptor), name)
        return descriptor

    def _bind_database(self) -> tuple[int, int]:
        """Bind one inode without auxiliary descriptors on a live SQLite file.

        Closing any such descriptor can discard this process's SQLite POSIX
        locks, including locks held by another RecordingIndex instance. Only a
        newly and exclusively created inode may be opened here; that descriptor
        closes before SQLite connects. Never adopt a replacement after creation.
        """
        directory_fd = self._directory_bindings[-1][1]
        try:
            metadata = os.stat(_DATABASE_NAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            descriptor = os.open(
                _DATABASE_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                _private_metadata(metadata, _DATABASE_NAME)
            finally:
                os.close(descriptor)
        _private_metadata(metadata, _DATABASE_NAME)
        identity = _identity(metadata)
        current = os.stat(_DATABASE_NAME, dir_fd=directory_fd, follow_symlinks=False)
        _private_metadata(current, _DATABASE_NAME)
        if _identity(current) != identity:
            raise SecurityError("recording index database was replaced")
        return identity

    def _check_paths(self) -> None:
        for path, descriptor in self._directory_bindings:
            current = os.stat(path, follow_symlinks=False)
            _private_metadata(current, str(path), directory=True)
            if _identity(current) != _identity(os.fstat(descriptor)):
                raise SecurityError(f"recording index directory was replaced: {path}")
        directory_fd = self._directory_bindings[-1][1]
        allowed = {
            ".index.lock",
            ".index.lease",
            _DATABASE_NAME,
            *(_DATABASE_NAME + s for s in ("-wal", "-shm", "-journal")),
        }
        members = os.listdir(directory_fd)
        for name in members:
            if name not in allowed:
                raise SecurityError(f"unexpected recording index member: {name}")
            # Do not open/close database or sidecar inodes while SQLite is live:
            # POSIX close releases every process-owned record lock on that file.
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _private_metadata(current, name)
            if name == _DATABASE_NAME and _identity(current) != self._database_identity:
                raise SecurityError("recording index database was replaced")
            if name == ".index.lock" and _identity(current) != _identity(os.fstat(self._lock_fd)):
                raise SecurityError("recording index lock was replaced")
            if name == ".index.lease" and _identity(current) != _identity(os.fstat(self._lease_fd)):
                raise SecurityError("recording index lease was replaced")
        for required in (_DATABASE_NAME, ".index.lock", ".index.lease"):
            if required not in members:
                raise SecurityError(f"recording index member disappeared: {required}")

    def _audit(self) -> None:
        if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecordingIndexError(
                "recording index structural integrity failed; rebuild required"
            )
        if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RecordingIndexError("recording index references are corrupt; rebuild required")
        self._revision(self.connection)
        self._audit_recovery_inventory(self.connection)
        for row in self.connection.execute("SELECT * FROM recording_takes"):
            self._decode(row)
        for row in self.connection.execute("SELECT * FROM recording_catalogs"):
            suite = PromptSuite.model_validate_json(row["recording_catalog_json"])
            if canonical_sha256(suite) != row["recording_catalog_sha256"]:
                raise RecordingIndexError("indexed catalog identity is corrupt; rebuild required")
            prompts = self.connection.execute(
                "SELECT p.recording_prompt_json, p.recording_prompt_sha256 FROM "
                "recording_catalog_prompts cp "
                "JOIN recording_prompts p USING(recording_prompt_sha256) "
                "WHERE cp.recording_catalog_sha256=? AND cp.recording_subject_id=? ORDER BY "
                "recording_prompt_ordinal",
                (row["recording_catalog_sha256"], row["recording_subject_id"]),
            ).fetchall()
            if [r["recording_prompt_sha256"] for r in prompts] != [
                canonical_sha256(p) for p in suite.prompts
            ]:
                raise RecordingIndexError("indexed catalog order/content changed; rebuild required")
            if any(
                canonical_json_bytes(p).decode() != r["recording_prompt_json"]
                for p, r in zip(suite.prompts, prompts, strict=True)
            ):
                raise RecordingIndexError("indexed prompt metadata changed; rebuild required")

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            connection = self.connection
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                self._check_paths()
                version = connection.execute("PRAGMA data_version").fetchone()[0]
                if version != self._data_version:
                    self._audit()
                    self._data_version = version
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield connection
                    self._check_paths()
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
            except (OSError, sqlite3.Error, ValidationError, ValueError) as error:
                raise RecordingIndexError(
                    f"recording index query failed; history is unknown: {error}"
                ) from error
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def close(self) -> None:
        """Close cache/guards; never remove evidence or repair unsafe permissions."""
        with self._mutex:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            for descriptor in reversed(self._descriptors):
                os.close(descriptor)
            self._descriptors.clear()
            self._closed = True

    def __enter__(self) -> RecordingIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def project_analysis_job(
        self,
        *,
        job_sha256: str,
        sequence: int,
        priority: str,
        status: str,
        attempt: int,
        payload: str,
    ) -> None:
        """Project a synced bounded canonical job head; scheduling never grants raw authority."""
        if len(payload.encode("utf-8")) > 1_048_576:
            raise RecordingIndexError("analysis job exceeds metadata bound")
        with self._transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO recording_analysis_jobs VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(recording_analysis_job_sha256) DO UPDATE SET "
                "recording_analysis_priority=excluded.recording_analysis_priority, "
                "recording_analysis_status=excluded.recording_analysis_status, "
                "recording_analysis_attempt=excluded.recording_analysis_attempt, "
                "recording_analysis_job_json=excluded.recording_analysis_job_json",
                (job_sha256, sequence, priority, status, attempt, payload),
            )

    def analysis_jobs(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> tuple[tuple[int, str], ...]:
        """Bounded stable FIFO metadata page, including independent priority lanes."""
        if not 1 <= limit <= 200 or after_sequence < 0:
            raise RecordingIndexError("analysis queue page is outside its bounds")
        where = ["recording_analysis_sequence > ?"]
        values: list[str | int] = [after_sequence]
        if status is not None:
            where.append("recording_analysis_status = ?")
            values.append(status)
        if priority is not None:
            where.append("recording_analysis_priority = ?")
            values.append(priority)
        values.append(limit)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT recording_analysis_sequence,recording_analysis_job_json "
                "FROM recording_analysis_jobs WHERE "
                + " AND ".join(where)
                + " ORDER BY recording_analysis_sequence LIMIT ?",
                values,
            ).fetchall()
            return tuple((row[0], row[1]) for row in rows)

    def reset_analysis_projection(self) -> None:
        """Explicit queue rebuild: canonical job heads remain untouched."""
        with self._transaction(write=True) as connection:
            connection.execute("DELETE FROM recording_analysis_jobs")

    @property
    def revision(self) -> int:
        """Return the guarded current index revision without querying rows or counts."""
        with self._transaction() as connection:
            return self._revision(connection)

    def _bump(self, connection: sqlite3.Connection) -> int:
        connection.execute(
            "UPDATE recording_index_states SET recording_index_revision=recording_index_revision+1"
        )
        return self._revision(connection)

    def _revision(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT recording_index_revision FROM recording_index_states WHERE "
            "recording_index_state_id=1"
        ).fetchone()
        if row is None:
            raise RecordingIndexError("recording index revision is missing; rebuild required")
        return cast(int, row[0])

    def _register_catalog(
        self, connection: sqlite3.Connection, suite: PromptSuite, subject_id: str
    ) -> str:
        digest = canonical_sha256(suite)
        if len(suite.prompts) > _MAX_CATALOG_PROMPTS:
            raise RecordingIndexError("catalog exceeds the bounded 10000-prompt metadata contract")
        row = connection.execute(
            "SELECT recording_catalog_json FROM recording_catalogs WHERE "
            "recording_catalog_sha256=? AND recording_subject_id=?",
            (digest, subject_id),
        ).fetchone()
        payload = canonical_json_bytes(suite).decode()
        if row is not None:
            if row[0] != payload:
                raise RecordingIndexError("indexed catalog changed under its identity")
            return digest
        connection.execute(
            "INSERT INTO recording_catalogs VALUES (?, ?, ?, 'building')",
            (digest, subject_id, payload),
        )
        for ordinal, prompt in enumerate(suite.prompts):
            prompt_digest = canonical_sha256(prompt)
            connection.execute(
                "INSERT OR IGNORE INTO recording_prompts VALUES (?, ?, ?)",
                (prompt_digest, prompt.prompt_id, canonical_json_bytes(prompt).decode()),
            )
            connection.execute(
                "INSERT INTO recording_catalog_prompts VALUES (?, ?, ?, ?)",
                (digest, subject_id, prompt_digest, ordinal),
            )
        self._bump(connection)
        return digest

    @property
    def instance_id(self) -> str:
        with self._transaction() as connection:
            return str(
                connection.execute(
                    "SELECT recording_index_instance_id FROM recording_index_states"
                ).fetchone()[0]
            )

    def native_take_ids(
        self, *, after_take_id: str | None = None, limit: int = MAX_HISTORY_PAGE_SIZE
    ) -> tuple[str, ...]:
        """Bounded identities for background missing-evidence detection, without audio reads."""
        return tuple(
            take_id
            for take_id, _revision in self.native_take_versions(
                after_take_id=after_take_id, limit=limit
            )
        )

    def native_take_versions(
        self, *, after_take_id: str | None = None, limit: int = MAX_HISTORY_PAGE_SIZE
    ) -> tuple[tuple[str, int], ...]:
        """Page subject-bound native identities and revisions for conditional discovery."""
        if not 1 <= limit <= MAX_HISTORY_PAGE_SIZE:
            raise ValueError("native identity page size is outside bounds")
        with self._transaction() as connection:
            return tuple(
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT recording_take_id, recording_take_state_revision FROM "
                    "recording_takes WHERE recording_subject_id=? "
                    "AND recording_take_origin='native' AND (? IS NULL OR recording_take_id>?) "
                    "ORDER BY recording_take_id LIMIT ?",
                    (self.subject_id, after_take_id, after_take_id, limit),
                )
            )

    def register_catalog(self, suite: PromptSuite) -> str:
        """Bind exact immutable prompts once; new catalogs start explicitly building."""
        with self._transaction(write=True) as connection:
            return self._register_catalog(connection, suite, self.subject_id)

    def _require_catalog(self, connection: sqlite3.Connection, catalog_sha256: str) -> _IndexState:
        row = connection.execute(
            "SELECT recording_catalog_completeness FROM recording_catalogs WHERE "
            "recording_catalog_sha256=? AND recording_subject_id=?",
            (catalog_sha256, self.subject_id),
        ).fetchone()
        if row is None:
            raise RecordingIndexError(
                "catalog is not indexed; explicit metadata backfill is required"
            )
        return cast(_IndexState, row[0])

    def set_completeness(self, catalog_sha256: str, state: _IndexState) -> int:
        """Mark explicit discovery/reconciliation status; only successful discovery is complete."""
        if state not in ("complete", "building", "unknown"):
            raise RecordingIndexError("invalid index completeness state")
        with self._transaction(write=True) as connection:
            self._require_catalog(connection, catalog_sha256)
            if (
                state == "complete"
                and connection.execute(
                    "SELECT 1 FROM recording_unknown_records WHERE recording_catalog_sha256=? "
                    "AND recording_subject_id=? LIMIT 1",
                    (catalog_sha256, self.subject_id),
                ).fetchone()
            ):
                raise RecordingIndexError(
                    "named unknown evidence must be reconciled before history is complete"
                )
            connection.execute(
                "UPDATE recording_catalogs SET recording_catalog_completeness=? WHERE "
                "recording_catalog_sha256=? AND recording_subject_id=?",
                (state, catalog_sha256, self.subject_id),
            )
            return self._bump(connection)

    def mark_unknown(self, catalog_sha256: str, record_id: str, error: RecordingError) -> int:
        """Name an affected source; block missing navigation and preserve current rows."""
        if not record_id or len(record_id) > 1024:
            raise RecordingIndexError("unknown evidence record identity must be bounded")
        with self._transaction(write=True) as connection:
            self._require_catalog(connection, catalog_sha256)
            connection.execute(
                "INSERT INTO recording_unknown_records VALUES (?, ?, ?, ?) ON CONFLICT DO "
                "UPDATE SET recording_unknown_error_json=excluded.recording_unknown_error_json",
                (record_id, catalog_sha256, self.subject_id, canonical_json_bytes(error).decode()),
            )
            connection.execute(
                "UPDATE recording_catalogs SET recording_catalog_completeness='unknown' WHERE "
                "recording_catalog_sha256=? AND recording_subject_id=?",
                (catalog_sha256, self.subject_id),
            )
            return self._bump(connection)

    def invalidate_take(self, take_id: str, error: RecordingError) -> int:
        """Mark all matching catalog projections Unknown after a selected source changes.

        This does not invent a durable invalid-analysis decision. A later committed
        analysis/recovery revision goes through update_take; reconciliation clears
        its named unknown record and explicitly restores completeness.
        """
        with self._transaction(write=True) as connection:
            self._invalidate_take(connection, take_id, error)
            return self._bump(connection)

    def invalidate_take_if_current(
        self, take_id: str, expected_state_revision: int, error: RecordingError
    ) -> bool:
        """Reject stale background missing-evidence findings in the publication transaction."""
        with self._transaction(write=True) as connection:
            current = connection.execute(
                "SELECT recording_take_state_revision FROM recording_takes WHERE "
                "recording_subject_id=? AND recording_take_id=?",
                (self.subject_id, take_id),
            ).fetchone()
            if current is None or current[0] != expected_state_revision:
                return False
            self._invalidate_take(connection, take_id, error)
            self._bump(connection)
            return True

    def _invalidate_take(
        self, connection: sqlite3.Connection, take_id: str, error: RecordingError
    ) -> None:
        row = connection.execute(
            "SELECT recording_prompt_sha256 FROM recording_takes "
            "WHERE recording_take_id=? AND recording_subject_id=?",
            (take_id, self.subject_id),
        ).fetchone()
        if row is None:
            raise RecordingIndexError("cannot invalidate an unindexed take")
        catalogs = connection.execute(
            "SELECT recording_catalog_sha256 FROM recording_catalog_prompts "
            "WHERE recording_prompt_sha256=? AND recording_subject_id=?",
            (row[0], self.subject_id),
        ).fetchall()
        for catalog in catalogs:
            connection.execute(
                "INSERT INTO recording_unknown_records VALUES (?, ?, ?, ?) "
                "ON CONFLICT DO UPDATE SET "
                "recording_unknown_error_json=excluded.recording_unknown_error_json",
                (take_id, catalog[0], self.subject_id, canonical_json_bytes(error).decode()),
            )
            connection.execute(
                "UPDATE recording_catalogs SET recording_catalog_completeness='unknown' "
                "WHERE recording_catalog_sha256=? AND recording_subject_id=?",
                (catalog[0], self.subject_id),
            )

    def clear_unknown(self, catalog_sha256: str, record_id: str) -> int:
        """Remove a reconciled error; completeness remains explicit until discovery finishes."""
        with self._transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM recording_unknown_records WHERE recording_catalog_sha256=? AND "
                "recording_subject_id=? AND recording_unknown_record_id=?",
                (catalog_sha256, self.subject_id, record_id),
            )
            return self._bump(connection)

    def _project(
        self,
        connection: sqlite3.Connection,
        entry: RecordingHistoryEntry,
        *,
        origin: Literal["native", "legacy"],
        opened_at: datetime | None = None,
        group_precision: str = "unknown",
        prepared: _PreparedProjection | None = None,
    ) -> bool:
        existing = connection.execute(
            "SELECT * FROM recording_takes WHERE recording_take_id=?", (entry.take_id,)
        ).fetchone()
        if existing is not None:
            previous = self._decode(existing)
            if (
                origin != existing["recording_take_origin"]
                or previous.subject_id != entry.subject_id
            ):
                raise RecordingIndexError("take identity conflicts with another source or subject")
            if entry.state_revision < previous.state_revision:
                return False
            if entry.state_revision == previous.state_revision:
                if entry != previous:
                    raise RecordingIndexError("take metadata changed without a durable revision")
                return False
            if previous.catalog_sha256 != entry.catalog_sha256:
                raise RecordingIndexError("take revision changed its immutable catalog identity")
            if previous.source_sha256 is not None and (
                previous.source_sha256 != entry.source_sha256
                or previous.source_frames != entry.source_frames
                or previous.recorded_at != entry.recorded_at
            ):
                raise RecordingIndexError(
                    "take revision changed its immutable sealed source identity"
                )
            if (previous.group_id, previous.legacy_session_id, previous.prompt) != (
                entry.group_id,
                entry.legacy_session_id,
                entry.prompt,
            ):
                raise RecordingIndexError("take revision changed immutable prompt/context identity")
        prepared = _PreparedProjection.from_entry(entry) if prepared is None else prepared
        connection.execute(
            "INSERT OR IGNORE INTO recording_prompts VALUES (?, ?, ?)",
            (
                prepared.prompt_sha256,
                entry.prompt.prompt_id,
                prepared.prompt_json,
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO recording_groups VALUES (?, ?, ?, ?, ?, ?)",
            (
                _group_key(entry),
                entry.subject_id,
                entry.group_id,
                entry.legacy_session_id,
                None if opened_at is None else opened_at.isoformat(),
                group_precision,
            ),
        )
        values = (
            entry.take_id,
            entry.subject_id,
            _group_key(entry),
            prepared.prompt_sha256,
            entry.catalog_sha256,
            origin,
            None if entry.recorded_at is None else entry.recorded_at.isoformat(),
            entry.decision,
            entry.analysis,
            entry.state_revision,
            prepared.entry_json,
            prepared.entry_sha256,
        )
        connection.execute(
            "INSERT INTO recording_takes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON "
            "CONFLICT(recording_take_id) DO UPDATE SET "
            "recording_take_recorded_at=excluded.recording_take_recorded_at, "
            "recording_take_decision=excluded.recording_take_decision, "
            "recording_take_analysis=excluded.recording_take_analysis, "
            "recording_take_state_revision=excluded.recording_take_state_revision, "
            "recording_take_payload_json=excluded.recording_take_payload_json, "
            "recording_take_payload_sha256=excluded.recording_take_payload_sha256",
            values,
        )
        return True

    def _update_take(self, connection: sqlite3.Connection, take: RecordingTake) -> bool:
        if take.reservation.group.subject_id != self.subject_id:
            raise RecordingIndexError("take projection belongs to another subject")
        self._register_catalog(connection, take.reservation.group.catalog, self.subject_id)
        changed = self._project(
            connection,
            history_entry_for_take(take),
            origin="native",
            opened_at=take.reservation.group.opened_at,
            group_precision="take",
        )
        if take.seal is not None:
            self._alias(
                connection,
                take.reservation.take_id,
                take.seal.original_path,
                take.seal.original_sha256,
                "native-original",
            )
        return changed

    def update_take(self, take: RecordingTake, *, current_session_reservation: bool = False) -> int:
        """Project a committed canonical take for TakeStore's post-sync hook."""
        with self._transaction(write=True) as connection:
            changed = self._update_take(connection, take)
            self._project_recovery_take(connection, take, current_session_reservation)
            return self._bump(connection) if changed else self._revision(connection)

    def _alias(
        self,
        connection: sqlite3.Connection,
        take_id: str,
        path: str,
        digest: str,
        kind: _AliasKind,
        *,
        prepared_identity: str | None = None,
    ) -> None:
        if prepared_identity is None:
            validate_relative_path(path)
            alias_id = canonical_sha256({"path": path, "sha256": digest})
        else:
            # Only _prepare_legacy_catalog supplies this identity, after validating
            # the exact immutable path/digest pair outside the publication lock.
            alias_id = prepared_identity
        existing = connection.execute(
            "SELECT recording_take_id FROM recording_take_aliases WHERE recording_alias_id=?",
            (alias_id,),
        ).fetchone()
        if existing is not None and existing[0] != take_id:
            raise RecordingIndexError("one source alias cannot identify two recording events")
        connection.execute(
            "INSERT OR IGNORE INTO recording_take_aliases VALUES (?, ?, ?, ?, ?)",
            (alias_id, take_id, path, kind, digest),
        )

    def register_alias(self, take_id: str, path: str, sha256: str, kind: _AliasKind) -> int:
        """Bind a validated package copy to its original take without duplicating history."""
        with self._transaction(write=True) as connection:
            self._alias(connection, take_id, path, sha256, kind)
            return self._bump(connection)

    def update_legacy_review(self, entry: RecordingHistoryEntry) -> int:
        """Project a durable legacy action; preserve all canonical source aliases."""
        if entry.subject_id != self.subject_id or entry.legacy_session_id is None:
            raise RecordingIndexError("legacy review projection has a different subject/origin")
        with self._transaction(write=True) as connection:
            changed = self._project(connection, entry, origin="legacy")
            if changed:
                self._legacy_mutation_epoch += 1
            return self._bump(connection) if changed else self._revision(connection)

    def legacy_publication_token(self) -> tuple[int, int]:
        """Bind prepared legacy evidence to owned edits and independently committed changes."""
        with self._transaction() as connection:
            return self._legacy_mutation_epoch, int(
                connection.execute("PRAGMA data_version").fetchone()[0]
            )

    def review_aliases(
        self, catalog_sha256: str, take_id: str
    ) -> tuple[RecordingHistoryEntry, tuple[tuple[str, str, _AliasKind], ...]]:
        """Return one catalog-bound metadata row and bounded aliases; no source bytes are read."""
        with self._transaction() as connection:
            self._require_catalog(connection, catalog_sha256)
            row = connection.execute(
                "SELECT t.* FROM recording_takes t JOIN recording_catalog_prompts cp ON "
                "t.recording_prompt_sha256=cp.recording_prompt_sha256 AND "
                "t.recording_subject_id=cp.recording_subject_id WHERE "
                "cp.recording_catalog_sha256=? AND cp.recording_subject_id=? "
                "AND t.recording_take_id=?",
                (catalog_sha256, self.subject_id, take_id),
            ).fetchone()
            if row is None:
                raise RecordingIndexError("selected take is absent from this exact catalog")
            aliases = connection.execute(
                "SELECT recording_alias_path,recording_alias_sha256,recording_alias_kind "
                "FROM recording_take_aliases WHERE recording_take_id=? "
                "ORDER BY recording_alias_path LIMIT 33",
                (take_id,),
            ).fetchall()
            if len(aliases) > 32:
                raise RecordingIndexError("selected take has too many source aliases")
            return self._decode(row), tuple(
                (str(item[0]), str(item[1]), cast(_AliasKind, item[2])) for item in aliases
            )

    def register_package_aliases(
        self,
        takes: Sequence[RecordingTake],
        aliases: Sequence[tuple[str, str, str, _AliasKind]],
    ) -> int:
        """Publish a bounded complete package projection in one SQLite transaction."""
        if len(takes) > 10_000 or len(aliases) > 20_000:
            raise RecordingIndexError("package alias projection exceeds its bounded contract")
        with self._transaction(write=True) as connection:
            for take in takes:
                self._update_take(connection, take)
            for take_id, path, digest, kind in aliases:
                self._alias(connection, take_id, path, digest, kind)
            return self._bump(connection)

    def resolve_navigation(
        self, proposed_take: RecordingTake, selection: RecordingSelection
    ) -> RecordingNavigationResult:
        """Resolve Save's read-only proposed keep overlay before the immutable operation sync."""
        if (
            proposed_take.reservation.group.catalog_sha256 != selection.catalog_sha256
            or proposed_take.reservation.prompt.prompt_id != selection.prompt_id
            or selection.take_id != proposed_take.reservation.take_id
        ):
            raise RecordingIndexError("Save navigation selection does not bind its proposed take")
        return self.next_missing(
            selection.catalog_sha256,
            after_prompt_id=selection.prompt_id,
            proposed_take=proposed_take,
        )

    def latest_retained_take(self, catalog_sha256: str, prompt: Prompt) -> str | None:
        """Newest retained take of one exact prompt in any group; None unless coverage is complete.

        Ordered by capture time, unknown dates last, then projection order. Metadata only.
        """
        prompt_sha256 = canonical_sha256(prompt)
        with self._transaction() as connection:
            if self._require_catalog(connection, catalog_sha256) != "complete":
                return None
            if (
                connection.execute(
                    "SELECT 1 FROM recording_catalog_prompts WHERE recording_catalog_sha256=? "
                    "AND recording_subject_id=? AND recording_prompt_sha256=?",
                    (catalog_sha256, self.subject_id, prompt_sha256),
                ).fetchone()
                is None
            ):
                raise RecordingIndexError("selected prompt is absent from the exact catalog")
            row = connection.execute(
                "SELECT * FROM recording_takes WHERE recording_subject_id=? AND "
                "recording_prompt_sha256=? AND "
                + _RETAINED
                + " ORDER BY recording_take_recorded_at DESC NULLS LAST, rowid DESC LIMIT 1",
                (self.subject_id, prompt_sha256),
            ).fetchone()
            return None if row is None else self._decode(row).take_id

    def next_missing(
        self,
        catalog_sha256: str,
        *,
        after_prompt_id: str | None = None,
        proposed_take: RecordingTake | None = None,
    ) -> RecordingNavigationResult:
        """Search canonical order with one wrap; only complete exact metadata can navigate."""
        with self._transaction() as connection:
            if self._require_catalog(connection, catalog_sha256) != "complete":
                raise RecordingIndexError("Continue Missing requires complete catalog metadata")
            overlay_id = None
            overlay_prompt = None
            overlay_covered = False
            if proposed_take is not None:
                if (
                    proposed_take.reservation.group.subject_id != self.subject_id
                    or proposed_take.reservation.group.catalog_sha256 != catalog_sha256
                ):
                    raise RecordingIndexError(
                        "proposed navigation take belongs to another catalog/subject"
                    )
                overlay_id = proposed_take.reservation.take_id
                overlay_prompt = canonical_sha256(proposed_take.reservation.prompt)
                overlay_covered = proposed_take.satisfies_coverage
            rows = connection.execute(
                "SELECT cp.recording_prompt_ordinal, p.recording_prompt_id, "
                "p.recording_prompt_sha256, p.recording_prompt_json, "
                "EXISTS(SELECT 1 FROM recording_takes t WHERE "
                "t.recording_subject_id=cp.recording_subject_id AND "
                "t.recording_prompt_sha256=cp.recording_prompt_sha256 AND "
                + _RETAINED
                + " AND (? IS NULL OR t.recording_take_id != ?)) AS covered "
                "FROM recording_catalog_prompts cp JOIN recording_prompts p "
                "USING(recording_prompt_sha256) WHERE cp.recording_catalog_sha256=? AND "
                "cp.recording_subject_id=? ORDER BY cp.recording_prompt_ordinal",
                (overlay_id, overlay_id, catalog_sha256, self.subject_id),
            ).fetchall()
            start = -1
            if after_prompt_id is not None:
                matches = [
                    r["recording_prompt_ordinal"]
                    for r in rows
                    if r["recording_prompt_id"] == after_prompt_id
                ]
                if not matches:
                    raise RecordingIndexError("navigation prompt is absent from the exact catalog")
                start = matches[0]
            target = None
            for row in rows[start + 1 :] + rows[: start + 1]:
                covered = bool(row["covered"]) or (
                    overlay_covered and row["recording_prompt_sha256"] == overlay_prompt
                )
                if not covered:
                    target = Prompt.model_validate_json(row["recording_prompt_json"])
                    break
            return RecordingNavigationResult(
                catalog_sha256=catalog_sha256,
                index_revision=self._revision(connection),
                next_prompt=target,
                complete=target is None,
            )

    def _decode(self, row: sqlite3.Row) -> RecordingHistoryEntry:
        entry = RecordingHistoryEntry.model_validate_json(row["recording_take_payload_json"])
        expected = (
            entry.take_id,
            entry.subject_id,
            _group_key(entry),
            canonical_sha256(entry.prompt),
            entry.catalog_sha256,
            None if entry.recorded_at is None else entry.recorded_at.isoformat(),
            entry.decision,
            entry.analysis,
            entry.state_revision,
            canonical_json_bytes(entry).decode(),
            canonical_sha256(entry),
        )
        actual = tuple(
            row[key]
            for key in (
                "recording_take_id",
                "recording_subject_id",
                "recording_group_key",
                "recording_prompt_sha256",
                "recording_source_catalog_sha256",
                "recording_take_recorded_at",
                "recording_take_decision",
                "recording_take_analysis",
                "recording_take_state_revision",
                "recording_take_payload_json",
                "recording_take_payload_sha256",
            )
        )
        if expected != actual:
            raise RecordingIndexError(
                f"indexed take metadata changed: {entry.take_id}; rebuild required"
            )
        return entry

    def _cursor(
        self,
        connection: sqlite3.Connection,
        catalog: str,
        kind: str,
        revision: int,
        offset: int,
        take_prompt_id: str | None = None,
    ) -> str:
        instance = connection.execute(
            "SELECT recording_index_instance_id FROM recording_index_states"
        ).fetchone()[0]
        query = f"{instance}:{self.subject_id}:{catalog}:{kind}"
        if take_prompt_id is not None:
            query += f":prompt:{take_prompt_id}"
        scope = hashlib.sha256(query.encode()).hexdigest()
        return f"{kind}.{revision}.{offset}.{scope}"

    def _position(
        self,
        connection: sqlite3.Connection,
        catalog: str,
        kind: str,
        revision: int,
        cursor: str | None,
        take_prompt_id: str | None = None,
    ) -> int:
        if cursor is None:
            return -1
        parts = cursor.split(".")
        if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
            raise RecordingIndexError("invalid history cursor", stale=True)
        offset = int(parts[2])
        if cursor != self._cursor(connection, catalog, kind, revision, offset, take_prompt_id):
            raise RecordingIndexError(
                "history cursor belongs to a changed snapshot or different query", stale=True
            )
        return offset

    def history(
        self,
        catalog_sha256: str,
        *,
        prompt_id: str | None = None,
        cursor: str | None = None,
        prompt_cursor: str | None = None,
        group_cursor: str | None = None,
        page_size: int = 50,
    ) -> RecordingHistoryPage:
        """Read bounded takes, prompt rows, groups and totals from the same SQLite snapshot."""
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_HISTORY_PAGE_SIZE:
            raise RecordingIndexError("history page size must be an integer in [1, 200]")
        with self._transaction() as connection:
            completeness = self._require_catalog(connection, catalog_sha256)
            revision = self._revision(connection)
            selected_prompt_sha256 = None
            if prompt_id is not None:
                prompt_id = TypeAdapter(StableId).validate_python(prompt_id)
                selected = connection.execute(
                    "SELECT p.recording_prompt_sha256 FROM recording_catalog_prompts cp "
                    "JOIN recording_prompts p USING(recording_prompt_sha256) WHERE "
                    "cp.recording_catalog_sha256=? AND cp.recording_subject_id=? "
                    "AND p.recording_prompt_id=?",
                    (catalog_sha256, self.subject_id, prompt_id),
                ).fetchone()
                if selected is None:
                    raise RecordingIndexError("history prompt does not belong to the exact catalog")
                selected_prompt_sha256 = selected[0]
            positions = [
                self._position(
                    connection,
                    catalog_sha256,
                    kind,
                    revision,
                    value,
                    prompt_id if kind == "t" else None,
                )
                for kind, value in zip(
                    ("t", "p", "g"), (cursor, prompt_cursor, group_cursor), strict=True
                )
            ]
            bindings = (catalog_sha256, self.subject_id)
            join = (
                " FROM recording_takes t JOIN recording_catalog_prompts cp ON "
                "t.recording_prompt_sha256=cp.recording_prompt_sha256 AND "
                "t.recording_subject_id=cp.recording_subject_id WHERE "
                "cp.recording_catalog_sha256=? AND cp.recording_subject_id=? "
            )
            take_count = connection.execute("SELECT count(*)" + join, bindings).fetchone()[0]
            prompt_count = connection.execute(
                "SELECT count(*) FROM recording_catalog_prompts WHERE "
                "recording_catalog_sha256=? AND recording_subject_id=?",
                bindings,
            ).fetchone()[0]
            covered_count = connection.execute(
                "SELECT count(DISTINCT t.recording_prompt_sha256)" + join + "AND " + _RETAINED,
                bindings,
            ).fetchone()[0]
            take_filter = (
                "" if selected_prompt_sha256 is None else "AND t.recording_prompt_sha256=? "
            )
            take_bindings = (
                bindings if selected_prompt_sha256 is None else (*bindings, selected_prompt_sha256)
            )
            rows = connection.execute(
                "SELECT t.rowid AS recording_page_position, t.*"
                + join
                + take_filter
                + "AND t.rowid > ? ORDER BY t.rowid LIMIT ?",
                (*take_bindings, positions[0], page_size + 1),
            ).fetchall()
            takes = []
            for row in rows[:page_size]:
                entry = self._decode(row)
                unknown = connection.execute(
                    "SELECT recording_unknown_error_json FROM recording_unknown_records "
                    "WHERE recording_catalog_sha256=? AND recording_subject_id=? "
                    "AND recording_unknown_record_id=?",
                    (*bindings, entry.take_id),
                ).fetchone()
                takes.append(
                    entry.model_copy(
                        update={
                            "catalog_sha256": catalog_sha256,
                            "error": entry.error
                            if unknown is None
                            else RecordingError.model_validate_json(unknown[0]),
                        }
                    )
                )
            prompt_rows = connection.execute(
                "SELECT p.recording_prompt_id, p.recording_prompt_sha256, "
                "cp.recording_prompt_ordinal, count(t.recording_take_id) AS take_count, "
                "sum(CASE WHEN " + _RETAINED + " THEN 1 ELSE 0 END) AS kept_count "
                "FROM recording_catalog_prompts cp JOIN recording_prompts p "
                "USING(recording_prompt_sha256) "
                "LEFT JOIN recording_takes t ON "
                "t.recording_prompt_sha256=cp.recording_prompt_sha256 AND "
                "t.recording_subject_id=cp.recording_subject_id "
                "WHERE cp.recording_catalog_sha256=? AND cp.recording_subject_id=? "
                "AND cp.recording_prompt_ordinal > ? GROUP BY cp.recording_prompt_ordinal "
                "ORDER BY cp.recording_prompt_ordinal LIMIT ?",
                (*bindings, positions[1], page_size + 1),
            ).fetchall()
            prompts = [
                self._prompt_summary(connection, row, completeness, catalog_sha256)
                for row in prompt_rows[:page_size]
            ]
            group_query = (
                " FROM recording_groups g WHERE g.recording_subject_id=? AND (EXISTS(SELECT 1 "
                "FROM recording_takes t JOIN recording_catalog_prompts cp ON "
                "t.recording_prompt_sha256=cp.recording_prompt_sha256 AND "
                "t.recording_subject_id=cp.recording_subject_id WHERE "
                "t.recording_group_key=g.recording_group_key AND "
                "t.recording_subject_id=g.recording_subject_id AND "
                "cp.recording_catalog_sha256=?) "
                "OR EXISTS(SELECT 1 FROM recording_catalog_groups cg WHERE "
                "cg.recording_group_key=g.recording_group_key AND "
                "cg.recording_subject_id=g.recording_subject_id AND "
                "cg.recording_catalog_sha256=?)) "
            )
            group_bindings = (self.subject_id, catalog_sha256, catalog_sha256)
            group_count = connection.execute(
                "SELECT count(*)" + group_query, group_bindings
            ).fetchone()[0]
            group_rows = connection.execute(
                "SELECT g.rowid AS recording_page_position, g.*"
                + group_query
                + "AND g.rowid > ? ORDER BY g.rowid LIMIT ?",
                (*group_bindings, positions[2], page_size + 1),
            ).fetchall()
            groups = [
                self._group_summary(connection, row, catalog_sha256)
                for row in group_rows[:page_size]
            ]
            error_rows = connection.execute(
                "SELECT recording_unknown_error_json FROM recording_unknown_records WHERE "
                "recording_catalog_sha256=? AND recording_subject_id=? ORDER BY "
                "recording_unknown_record_id LIMIT 16",
                bindings,
            ).fetchall()
            error_count = connection.execute(
                "SELECT count(*) FROM recording_unknown_records WHERE "
                "recording_catalog_sha256=? AND recording_subject_id=?",
                bindings,
            ).fetchone()[0]
            while True:
                lengths = (len(takes), len(prompts), len(groups))
                windows = (rows, prompt_rows, group_rows)
                keys = (
                    "recording_page_position",
                    "recording_prompt_ordinal",
                    "recording_page_position",
                )
                next_cursors = [
                    self._cursor(
                        connection,
                        catalog_sha256,
                        kind,
                        revision,
                        window[length - 1][key] if length else position,
                        prompt_id if kind == "t" else None,
                    )
                    if len(window) > length
                    else None
                    for kind, window, key, length, position in zip(
                        ("t", "p", "g"),
                        windows,
                        keys,
                        lengths,
                        positions,
                        strict=True,
                    )
                ]
                page = RecordingHistoryPage(
                    catalog_sha256=catalog_sha256,
                    take_prompt_id=prompt_id,
                    index_revision=revision,
                    completeness=completeness,
                    takes=tuple(takes),
                    next_cursor=next_cursors[0],
                    has_more=next_cursors[0] is not None,
                    prompts=tuple(prompts),
                    next_prompt_cursor=next_cursors[1],
                    has_more_prompts=next_cursors[1] is not None,
                    groups=tuple(groups),
                    next_group_cursor=next_cursors[2],
                    has_more_groups=next_cursors[2] is not None,
                    total_take_count=take_count,
                    total_group_count=group_count,
                    total_prompt_count=prompt_count,
                    covered_prompt_count=covered_count,
                    missing_prompt_count=prompt_count - covered_count
                    if completeness == "complete"
                    else None,
                    errors=tuple(RecordingError.model_validate_json(row[0]) for row in error_rows),
                    error_count=error_count,
                )
                size = len(canonical_json_bytes(page))
                if size <= _MAX_HISTORY_PAYLOAD_BYTES:
                    return page
                excess = size - _MAX_HISTORY_PAYLOAD_BYTES + 512
                while excess > 0:
                    if takes:
                        excess -= len(canonical_json_bytes(takes.pop())) + 1
                    elif groups:
                        excess -= len(canonical_json_bytes(groups.pop())) + 1
                    elif prompts:
                        excess -= len(canonical_json_bytes(prompts.pop())) + 1
                    else:
                        raise RecordingIndexError(
                            "bounded history metadata exceeds the control frame budget"
                        )

    def _prompt_summary(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        completeness: _IndexState,
        catalog_sha256: str,
    ) -> RecordingPromptSummary:
        latest = connection.execute(
            "SELECT * FROM recording_takes WHERE recording_subject_id=? AND "
            "recording_prompt_sha256=? ORDER BY CASE WHEN "
            + _RETAINED
            + (
                " THEN 0 WHEN recording_take_decision='draft' THEN 1 ELSE 2 END, "
                "recording_take_recorded_at DESC, recording_take_id DESC LIMIT 1"
            ),
            (self.subject_id, row["recording_prompt_sha256"]),
        ).fetchone()
        entry = None if latest is None else self._decode(latest)
        status: Literal["missing", "unknown", "draft", "saved-checking", "recorded", "needs-review"]
        unknown = connection.execute(
            "SELECT 1 FROM recording_unknown_records u JOIN recording_takes t "
            "ON t.recording_take_id=u.recording_unknown_record_id "
            "AND t.recording_subject_id=u.recording_subject_id "
            "WHERE u.recording_catalog_sha256=? AND u.recording_subject_id=? "
            "AND t.recording_prompt_sha256=? LIMIT 1",
            (catalog_sha256, self.subject_id, row["recording_prompt_sha256"]),
        ).fetchone()
        if unknown is not None:
            status = "unknown"
        elif row["kept_count"]:
            status = (
                "recorded"
                if entry is not None and entry.analysis == "verified"
                else "needs-review"
                if entry is not None and entry.analysis in ("needs-review", "failed")
                else "saved-checking"
            )
        elif entry is not None and entry.decision == "draft":
            status = "draft"
        else:
            status = "missing" if completeness == "complete" else "unknown"
        return RecordingPromptSummary(
            prompt_id=row["recording_prompt_id"],
            prompt_sha256=row["recording_prompt_sha256"],
            canonical_ordinal=row["recording_prompt_ordinal"],
            take_count=row["take_count"],
            kept_count=row["kept_count"],
            status=status,
            latest_recorded_at=None if entry is None else entry.recorded_at,
            date_precision="unknown" if entry is None else entry.date_precision,
        )

    def _group_summary(
        self, connection: sqlite3.Connection, row: sqlite3.Row, catalog: str
    ) -> RecordingGroupSummary:
        counts = connection.execute(
            "SELECT count(*), coalesce(sum(CASE WHEN "
            + _RETAINED
            + (
                " THEN 1 ELSE 0 END),0) FROM recording_takes t JOIN recording_catalog_prompts "
                "cp ON t.recording_prompt_sha256=cp.recording_prompt_sha256 AND "
                "t.recording_subject_id=cp.recording_subject_id WHERE "
                "cp.recording_catalog_sha256=? AND cp.recording_subject_id=? AND "
                "t.recording_group_key=?"
            ),
            (catalog, self.subject_id, row["recording_group_key"]),
        ).fetchone()
        return RecordingGroupSummary(
            group_id=row["recording_group_id"],
            legacy_session_id=row["recording_legacy_session_id"],
            opened_at=None
            if row["recording_group_opened_at"] is None
            else datetime.fromisoformat(row["recording_group_opened_at"]),
            date_precision=row["recording_group_date_precision"],
            take_count=counts[0],
            kept_count=counts[1],
        )

    def reconcile_takes(self, takes: Iterable[RecordingTake]) -> int:
        """Rebuild native projections from canonical bounded-reader output in one transaction.

        Caller performs TakeStore.recover first. No WAV or independent verification
        is performed. A failed iteration rolls back the replacement atomically.
        Catalog completeness remains building until legacy discovery also finishes.
        """
        with self._transaction(write=True) as connection:
            catalogs = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT recording_catalog_sha256 FROM recording_catalogs WHERE "
                    "recording_subject_id=?",
                    (self.subject_id,),
                )
            )
            connection.execute(
                "UPDATE recording_catalogs SET recording_catalog_completeness='building' "
                "WHERE recording_subject_id=?",
                (self.subject_id,),
            )
            self._bump(connection)
        try:
            # TakeStore readers acquire the evidence lock. Never call them under
            # our SQLite lock: Save holds evidence first, then updates this index.
            # An unlinked private spool also bounds retained model/catalog memory.
            with tempfile.TemporaryFile(mode="w+b", dir=self.layout.directory("tmp")) as spool:
                seen_catalogs: set[str] = set()
                for take in takes:
                    if take.reservation.group.subject_id != self.subject_id:
                        raise RecordingIndexError("take projection belongs to another subject")
                    digest = take.reservation.group.catalog_sha256
                    spool.write(
                        canonical_json_bytes(
                            {
                                "entry": history_entry_for_take(take).model_dump(mode="json"),
                                "opened_at": take.reservation.group.opened_at.isoformat(),
                                "catalog": None
                                if digest in seen_catalogs
                                else take.reservation.group.catalog.model_dump(mode="json"),
                            }
                        )
                        + b"\n"
                    )
                    seen_catalogs.add(digest)
                spool.seek(0)
                with self._transaction(write=True) as connection:
                    connection.execute(
                        "DELETE FROM recording_takes WHERE recording_subject_id=? "
                        "AND recording_take_origin='native'",
                        (self.subject_id,),
                    )
                    for line in spool:
                        record = json.loads(line)
                        if record["catalog"] is not None:
                            suite = PromptSuite.model_validate_json(
                                canonical_json_bytes(record["catalog"])
                            )
                            self._register_catalog(connection, suite, self.subject_id)
                        entry = RecordingHistoryEntry.model_validate_json(
                            canonical_json_bytes(record["entry"])
                        )
                        self._project(
                            connection,
                            entry,
                            origin="native",
                            opened_at=datetime.fromisoformat(record["opened_at"]),
                            group_precision="take",
                        )
                        if entry.source_sha256 is not None:
                            self._alias(
                                connection,
                                entry.take_id,
                                f"tmp/recording-takes/{entry.take_id}/original.wav",
                                entry.source_sha256,
                                "native-original",
                            )
                    connection.execute(
                        "DELETE FROM recording_unknown_records WHERE recording_subject_id=? "
                        "AND recording_unknown_record_id='native-reconcile'",
                        (self.subject_id,),
                    )
                    return self._bump(connection)
        except Exception as error:
            for catalog in catalogs:
                self.mark_unknown(
                    catalog,
                    "native-reconcile",
                    RecordingError(
                        code="RECORDING.HISTORY_UNKNOWN",
                        message=f"Native metadata rebuild failed: {error}"[:1024],
                        retryable=False,
                    ),
                )
            raise

    def replace_legacy_catalog(
        self,
        catalog_sha256: str,
        entries: Iterable[
            tuple[RecordingHistoryEntry, datetime | None, tuple[tuple[str, str, _AliasKind], ...]]
        ],
        groups: Iterable[RecordingGroupSummary] = (),
        unknown_errors: Iterable[tuple[str, RecordingError]] = (),
    ) -> int:
        """Prepare validated legacy metadata outside the lock, then replace it atomically."""
        prepared = _prepare_legacy_catalog(self.subject_id, entries, groups, unknown_errors)
        revision = self._replace_prepared_legacy(catalog_sha256, prepared, expected_token=None)
        assert revision is not None
        return revision

    def replace_legacy_catalog_if_current(
        self,
        catalog_sha256: str,
        entries: Iterable[
            tuple[RecordingHistoryEntry, datetime | None, tuple[tuple[str, str, _AliasKind], ...]]
        ],
        groups: Iterable[RecordingGroupSummary] = (),
        unknown_errors: Iterable[tuple[str, RecordingError]] = (),
        *,
        expected_token: tuple[int, int],
    ) -> int | None:
        """Publish staged evidence only if no local legacy edit/external commit superseded it."""
        prepared = _prepare_legacy_catalog(self.subject_id, entries, groups, unknown_errors)
        return self._replace_prepared_legacy(
            catalog_sha256, prepared, expected_token=expected_token
        )

    def _replace_prepared_legacy(
        self,
        catalog_sha256: str,
        prepared: _PreparedLegacyCatalog,
        *,
        expected_token: tuple[int, int] | None,
    ) -> int | None:
        with self._transaction(write=True) as connection:
            self._require_catalog(connection, catalog_sha256)
            if expected_token is not None and expected_token != (
                self._legacy_mutation_epoch,
                int(connection.execute("PRAGMA data_version").fetchone()[0]),
            ):
                return None
            connection.execute(
                "DELETE FROM recording_takes WHERE recording_take_origin='legacy' AND "
                "recording_subject_id=? "
                "AND recording_prompt_sha256 IN (SELECT recording_prompt_sha256 FROM "
                "recording_catalog_prompts WHERE recording_catalog_sha256=? AND "
                "recording_subject_id=?)",
                (self.subject_id, catalog_sha256, self.subject_id),
            )
            connection.execute(
                "DELETE FROM recording_unknown_records WHERE recording_catalog_sha256=? AND "
                "recording_subject_id=? AND (recording_unknown_record_id LIKE 'legacy:%' "
                "OR recording_unknown_record_id='legacy-backfill')",
                (catalog_sha256, self.subject_id),
            )
            connection.execute(
                "DELETE FROM recording_catalog_groups WHERE recording_catalog_sha256=? AND "
                "recording_subject_id=?",
                (catalog_sha256, self.subject_id),
            )
            for group_key, session_id, opened_at, precision in prepared.groups:
                connection.execute(
                    "INSERT INTO recording_groups VALUES (?, ?, NULL, ?, ?, ?) ON "
                    "CONFLICT(recording_group_key,recording_subject_id) DO UPDATE SET "
                    "recording_group_opened_at=excluded.recording_group_opened_at, "
                    "recording_group_date_precision=excluded.recording_group_date_precision",
                    (
                        group_key,
                        self.subject_id,
                        session_id,
                        opened_at,
                        precision,
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO recording_catalog_groups VALUES (?, ?, ?)",
                    (catalog_sha256, self.subject_id, group_key),
                )
            bulk_inserted = self._insert_new_prepared_legacy(
                connection, catalog_sha256, prepared.insertions
            )
            for item in () if bulk_inserted else prepared.entries:
                entry = item.entry
                if (
                    connection.execute(
                        "SELECT 1 FROM recording_catalog_prompts WHERE "
                        "recording_catalog_sha256=? AND recording_subject_id=? AND "
                        "recording_prompt_sha256=?",
                        (catalog_sha256, self.subject_id, item.projection.prompt_sha256),
                    ).fetchone()
                    is None
                ):
                    raise RecordingIndexError(
                        "legacy projection does not exactly match its indexed prompt"
                    )
                self._project(
                    connection,
                    entry,
                    origin="legacy",
                    opened_at=item.opened_at,
                    group_precision="unknown" if item.opened_at is None else "session",
                    prepared=item.projection,
                )
                for path, digest, kind, alias_id in item.aliases:
                    self._alias(
                        connection, entry.take_id, path, digest, kind, prepared_identity=alias_id
                    )
            for record_id, error_json in prepared.errors:
                connection.execute(
                    "INSERT INTO recording_unknown_records VALUES (?, ?, ?, ?)",
                    (
                        record_id,
                        catalog_sha256,
                        self.subject_id,
                        error_json,
                    ),
                )
            has_unknown = (
                connection.execute(
                    "SELECT 1 FROM recording_unknown_records WHERE recording_catalog_sha256=? "
                    "AND recording_subject_id=? LIMIT 1",
                    (catalog_sha256, self.subject_id),
                ).fetchone()
                is not None
            )
            connection.execute(
                "UPDATE recording_catalogs SET recording_catalog_completeness=? WHERE "
                "recording_catalog_sha256=? AND recording_subject_id=?",
                ("unknown" if has_unknown else "complete", catalog_sha256, self.subject_id),
            )
            revision = self._bump(connection)
            self._legacy_mutation_epoch += 1
            return revision

    def _insert_new_prepared_legacy(
        self,
        connection: sqlite3.Connection,
        catalog_sha256: str,
        prepared: _PreparedLegacyInsertions | None,
    ) -> bool:
        """Insert unique new rows in bulk; existing identities retain _project/_alias checks."""
        if prepared is None:
            return False
        # The caller's scoped delete may leave another catalog, origin, subject,
        # or package alias with an incoming identity. Preserve the existing serial
        # revision/collision rules for the entire batch whenever that happens.
        for table, column, identities in (
            ("recording_takes", "recording_take_id", prepared.take_ids),
            ("recording_take_aliases", "recording_alias_id", prepared.alias_ids),
        ):
            for offset in range(0, len(identities), _LEGACY_COLLISION_QUERY_BATCH):
                chunk = identities[offset : offset + _LEGACY_COLLISION_QUERY_BATCH]
                placeholders = ",".join("?" for _ in chunk)
                if (
                    connection.execute(
                        f"SELECT 1 FROM {table} WHERE {column} IN ({placeholders}) LIMIT 1", chunk
                    ).fetchone()
                    is not None
                ):
                    return False
        prompt_hashes = {
            row[0]
            for row in connection.execute(
                "SELECT recording_prompt_sha256 FROM recording_catalog_prompts WHERE "
                "recording_catalog_sha256=? AND recording_subject_id=?",
                (catalog_sha256, self.subject_id),
            )
        }
        if any(prompt[0] not in prompt_hashes for prompt in prepared.prompts):
            raise RecordingIndexError("legacy projection does not exactly match its indexed prompt")
        connection.executemany(
            "INSERT OR IGNORE INTO recording_prompts VALUES (?, ?, ?)", prepared.prompts
        )
        connection.executemany(
            "INSERT OR IGNORE INTO recording_groups VALUES (?, ?, ?, ?, ?, ?)", prepared.groups
        )
        connection.executemany(
            "INSERT INTO recording_takes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            prepared.takes,
        )
        connection.executemany(
            "INSERT INTO recording_take_aliases VALUES (?, ?, ?, ?, ?)", prepared.aliases
        )
        return True

    @classmethod
    def quarantine_cache(cls, layout: AssetLayout) -> Path:
        """Move one closed private cache aside for explicit rebuild; never discard evidence.

        Requires exclusive cache ownership and safe regular members. Unsafe paths
        fail closed. The resulting directory preserves the failed cache for review.
        """
        layout = AssetLayout.validate(layout.root)
        directory = layout.directory("provenance") / "recording-library"
        _private_metadata(directory.lstat(), str(directory), directory=True)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock_fd = -1
        lease_fd = -1
        try:
            lease_fd = os.open(".index.lease", os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
            _private_metadata(os.fstat(lease_fd), ".index.lease")
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fd = os.open(".index.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
            _private_metadata(os.fstat(lock_fd), ".index.lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            allowed = {
                ".index.lock",
                ".index.lease",
                _DATABASE_NAME,
                *(_DATABASE_NAME + s for s in ("-wal", "-shm", "-journal")),
            }
            for name in os.listdir(directory_fd):
                if name not in allowed:
                    raise SecurityError(f"unexpected cache member during rebuild: {name}")
                _private_metadata(os.stat(name, dir_fd=directory_fd, follow_symlinks=False), name)
            destination = directory.with_name(f"recording-library-quarantine-{uuid4().hex}")
            os.rename(directory, destination)
            parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return destination
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            if lease_fd >= 0:
                os.close(lease_fd)
            os.close(directory_fd)

    def _recovery_state(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM recording_recovery_scans WHERE recording_subject_id=?",
            (self.subject_id,),
        ).fetchone()
        if row is None:
            raise RecordingIndexError("recovery inventory has not started")
        return cast(sqlite3.Row, row)

    def _recovery_row(self, connection: sqlite3.Connection, take_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM recording_recovery_findings WHERE recording_subject_id=? "
                "AND recording_take_id=?",
                (self.subject_id, take_id),
            ).fetchone(),
        )

    def _recovery_changed(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE recording_recovery_scans SET recording_recovery_inventory_revision="
            "recording_recovery_inventory_revision+1 WHERE recording_subject_id=?",
            (self.subject_id,),
        )
        # Readers acquire the same transaction mutex before observing this wake.
        self.recovery_wake.set()

    def _write_recovery_row(
        self,
        connection: sqlite3.Connection,
        take_id: str,
        *,
        observed_revision: int | None,
        token: int,
        pending: bool,
        protected: bool,
        classified: str | None,
        claimed: str | None,
        result: RecordingRecoveryStatus | None,
    ) -> None:
        payload = None if result is None else canonical_json_bytes(result).decode()
        digest = None if result is None else canonical_sha256(result)
        connection.execute(
            "INSERT INTO recording_recovery_findings VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(recording_subject_id,recording_take_id) DO UPDATE SET "
            "recording_recovery_observed_take_revision=excluded.recording_recovery_observed_take_revision,"
            "recording_recovery_work_token=excluded.recording_recovery_work_token,"
            "recording_recovery_pending=excluded.recording_recovery_pending,"
            "recording_recovery_protected=excluded.recording_recovery_protected,"
            "recording_recovery_classified_generation=excluded.recording_recovery_classified_generation,"
            "recording_recovery_claimed_generation=excluded.recording_recovery_claimed_generation,"
            "recording_recovery_result_json=excluded.recording_recovery_result_json,"
            "recording_recovery_result_sha256=excluded.recording_recovery_result_sha256,"
            "recording_recovery_recovered_seal=excluded.recording_recovery_recovered_seal",
            (
                self.subject_id,
                take_id,
                observed_revision,
                token,
                int(pending),
                int(protected),
                classified,
                claimed,
                payload,
                digest,
                int(result is not None and result.recovered_seal),
            ),
        )

    @staticmethod
    def _decode_recovery_result(row: sqlite3.Row) -> RecordingRecoveryStatus | None:
        payload = row["recording_recovery_result_json"]
        if payload is None:
            if (
                row["recording_recovery_result_sha256"] is not None
                or row["recording_recovery_recovered_seal"]
            ):
                raise RecordingIndexError("recovery inventory result binding changed")
            return None
        result = RecordingRecoveryStatus.model_validate_json(payload)
        if (
            result.take_id != row["recording_take_id"]
            or canonical_sha256(result) != row["recording_recovery_result_sha256"]
            or int(result.recovered_seal) != row["recording_recovery_recovered_seal"]
        ):
            raise RecordingIndexError("recovery inventory result binding changed")
        return result

    def _audit_recovery_inventory(self, connection: sqlite3.Connection) -> None:
        states = {
            row["recording_subject_id"]: row
            for row in connection.execute("SELECT * FROM recording_recovery_scans")
        }
        for state in states.values():
            TypeAdapter(StableId).validate_python(state["recording_subject_id"])
            TypeAdapter(StableId).validate_python(state["recording_recovery_scan_generation"])
            error = state["recording_recovery_error_json"]
            if error is not None:
                RecordingError.model_validate_json(error)
            pending = connection.execute(
                "SELECT COUNT(*) FROM recording_recovery_findings WHERE recording_subject_id=? "
                "AND recording_recovery_pending=1",
                (state["recording_subject_id"],),
            ).fetchone()[0]
            if state["recording_recovery_scan_state"] == "complete" and pending:
                raise RecordingIndexError("complete recovery inventory contains pending work")
        for row in connection.execute("SELECT * FROM recording_recovery_findings"):
            TypeAdapter(StableId).validate_python(row["recording_take_id"])
            state = states[row["recording_subject_id"]]
            generation = state["recording_recovery_scan_generation"]
            claimed = row["recording_recovery_claimed_generation"]
            classified = row["recording_recovery_classified_generation"]
            token = row["recording_recovery_work_token"]
            if (
                (claimed is not None and (claimed != generation or token < 1))
                or (classified is not None and classified != generation)
                or token > state["recording_recovery_next_token"]
            ):
                raise RecordingIndexError("recovery inventory generation/token binding changed")
            result = self._decode_recovery_result(row)
            if result is not None or row["recording_recovery_pending"]:
                RecordingRecoveryFinding(
                    take_id=row["recording_take_id"],
                    observed_take_revision=row["recording_recovery_observed_take_revision"],
                    pending=bool(row["recording_recovery_pending"]),
                    result=result,
                )
            elif (
                row["recording_recovery_observed_take_revision"] is None
                or row["recording_recovery_classified_generation"] is None
            ):
                raise RecordingIndexError("unclassified recovery inventory row is hidden")

    def begin_recovery_scan(self, *, scan_generation: str) -> None:
        """Start a fresh subject scan; retained findings are visibly awaiting recheck."""
        TypeAdapter(StableId).validate_python(scan_generation)
        with self._transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO recording_recovery_scans VALUES (?,?,0,0,'scanning',0,NULL,NULL) "
                "ON CONFLICT(recording_subject_id) DO UPDATE SET "
                "recording_recovery_scan_generation=excluded.recording_recovery_scan_generation,"
                "recording_recovery_scan_state='scanning',recording_recovery_enumeration_complete=0,"
                "recording_recovery_covered_store_revision=NULL,recording_recovery_error_json=NULL",
                (self.subject_id, scan_generation),
            )
            connection.execute(
                "UPDATE recording_recovery_findings SET recording_recovery_pending=1,"
                "recording_recovery_protected=0,recording_recovery_classified_generation=NULL,"
                "recording_recovery_claimed_generation=NULL,recording_recovery_work_token=0 "
                "WHERE recording_subject_id=?",
                (self.subject_id,),
            )
            self._recovery_changed(connection)

    def recovery_snapshot(
        self, *, cursor: str | None = None, page_size: int = 50
    ) -> RecordingRecoveryInventoryPage:
        """Read a bounded inventory page without canonical history reads or recovery work."""
        if isinstance(page_size, bool) or not 1 <= page_size <= 50:
            raise RecordingIndexError("recovery page size must be an integer in [1, 50]")
        with self._transaction() as connection:
            state = self._recovery_state(connection)
            generation = str(state["recording_recovery_scan_generation"])
            revision = int(state["recording_recovery_inventory_revision"])
            position = self._position(connection, generation, "recovery", revision, cursor)
            counts = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(recording_recovery_pending),0),"
                "COALESCE(SUM(recording_recovery_recovered_seal "
                "AND NOT recording_recovery_pending),0) "
                "FROM recording_recovery_findings WHERE recording_subject_id=? AND "
                "(recording_recovery_pending=1 OR recording_recovery_result_json IS NOT NULL)",
                (self.subject_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM recording_recovery_findings WHERE recording_subject_id=? AND "
                "(recording_recovery_pending=1 OR recording_recovery_result_json IS NOT NULL) "
                "ORDER BY recording_take_id LIMIT ? OFFSET ?",
                (self.subject_id, page_size + 1, position + 1),
            ).fetchall()
            entries = tuple(
                RecordingRecoveryFinding(
                    take_id=row["recording_take_id"],
                    observed_take_revision=row["recording_recovery_observed_take_revision"],
                    pending=bool(row["recording_recovery_pending"]),
                    result=self._decode_recovery_result(row),
                )
                for row in rows[:page_size]
            )
            page = RecordingRecoveryInventoryPage(
                scan_generation=generation,
                inventory_revision=revision,
                scan_state=state["recording_recovery_scan_state"],
                enumeration_complete=bool(state["recording_recovery_enumeration_complete"]),
                pending_count=counts[1],
                finding_count=counts[0],
                recovered_seal_count=counts[2],
                covered_store_revision=state["recording_recovery_covered_store_revision"],
                entries=entries,
                next_cursor=self._cursor(
                    connection, generation, "recovery", revision, position + len(entries)
                )
                if len(rows) > page_size
                else None,
                error=None
                if state["recording_recovery_error_json"] is None
                else RecordingError.model_validate_json(state["recording_recovery_error_json"]),
            )
            if len(canonical_json_bytes(page)) > _MAX_HISTORY_PAYLOAD_BYTES:
                raise RecordingIndexError("recovery inventory page exceeds its framed byte bound")
            return page

    def claim_recovery_work(
        self,
        take_id: str,
        *,
        explicit: bool = False,
        allow_current_session_reservation: bool = True,
    ) -> RecordingRecoveryWork | None:
        """Claim one generation/token; automatic scans skip current-session reservations."""
        TypeAdapter(StableId).validate_python(take_id)
        with self._transaction(write=True) as connection:
            state = self._recovery_state(connection)
            generation = str(state["recording_recovery_scan_generation"])
            row = self._recovery_row(connection, take_id)
            if (
                row is not None
                and row["recording_recovery_protected"]
                and not allow_current_session_reservation
            ):
                return None
            if (
                row is not None
                and not explicit
                and (
                    row["recording_recovery_protected"]
                    or row["recording_recovery_classified_generation"] == generation
                    or row["recording_recovery_claimed_generation"] == generation
                )
            ):
                return None
            current = connection.execute(
                "SELECT recording_take_state_revision FROM recording_takes WHERE "
                "recording_subject_id=? AND recording_take_id=?",
                (self.subject_id, take_id),
            ).fetchone()
            observed = (
                current[0]
                if current is not None
                else row["recording_recovery_observed_take_revision"]
                if row is not None
                else None
            )
            token = int(state["recording_recovery_next_token"]) + 1
            connection.execute(
                "UPDATE recording_recovery_scans SET recording_recovery_next_token=?,"
                "recording_recovery_scan_state=CASE WHEN recording_recovery_scan_state='error' "
                "THEN 'error' ELSE 'scanning' END,recording_recovery_covered_store_revision=NULL "
                "WHERE recording_subject_id=?",
                (token, self.subject_id),
            )
            self._write_recovery_row(
                connection,
                take_id,
                observed_revision=observed,
                token=token,
                pending=True,
                protected=False,
                classified=None,
                claimed=generation,
                result=None if row is None else self._decode_recovery_result(row),
            )
            self._recovery_changed(connection)
            return RecordingRecoveryWork(
                scan_generation=generation,
                take_id=take_id,
                token=token,
                expected_take_revision=observed,
            )

    def _current_recovery_work(
        self, connection: sqlite3.Connection, work: RecordingRecoveryWork
    ) -> sqlite3.Row | None:
        state = self._recovery_state(connection)
        row = self._recovery_row(connection, work.take_id)
        if (
            state["recording_recovery_scan_generation"] != work.scan_generation
            or row is None
            or row["recording_recovery_work_token"] != work.token
            or row["recording_recovery_claimed_generation"] != work.scan_generation
        ):
            return None
        return row

    def publish_recovery_work(
        self, work: RecordingRecoveryWork, observation: RecordingRecoveryObservation
    ) -> bool:
        """Publish only the claimed generation/token and actual observed canonical revision."""
        if observation.result.take_id != work.take_id:
            raise RecordingIndexError("recovery observation identifies a different claimed take")
        with self._transaction(write=True) as connection:
            row = self._current_recovery_work(connection, work)
            if row is None:
                return False
            if row["recording_recovery_protected"]:
                return False
            current = row["recording_recovery_observed_take_revision"]
            observed = observation.observed_take_revision
            if (observed is None and current != work.expected_take_revision) or (
                observed is not None and current is not None and observed < current
            ):
                connection.execute(
                    "UPDATE recording_recovery_findings SET "
                    "recording_recovery_claimed_generation=NULL "
                    "WHERE recording_subject_id=? AND recording_take_id=?",
                    (self.subject_id, work.take_id),
                )
                self._recovery_changed(connection)
                return False
            if observation.subject_id is not None and observation.subject_id != self.subject_id:
                self._discard_recovery_row(connection, work.take_id)
                return True
            if observation.subject_id is None and work.expected_take_revision is None:
                self._discard_recovery_row(connection, work.take_id)
                self._fail_recovery_scan(
                    connection,
                    observation.result.error
                    or RecordingError(
                        code="RECORDING.SOURCE_CHANGED",
                        message="Preserved evidence has no verified subject binding",
                        retryable=True,
                    ),
                )
                return True
            result = observation.result
            visible = result.recovered_seal or result.status in {
                "partial",
                "empty",
                "busy",
                "unavailable",
            }
            self._write_recovery_row(
                connection,
                work.take_id,
                observed_revision=observed if observed is not None else current,
                token=work.token,
                pending=False,
                protected=False,
                classified=work.scan_generation,
                claimed=None,
                result=result if visible else None,
            )
            self._recovery_changed(connection)
            return True

    def _discard_recovery_row(self, connection: sqlite3.Connection, take_id: str) -> None:
        connection.execute(
            "DELETE FROM recording_recovery_findings WHERE recording_subject_id=? "
            "AND recording_take_id=?",
            (self.subject_id, take_id),
        )
        self._recovery_changed(connection)

    def discard_recovery_work(self, work: RecordingRecoveryWork) -> bool:
        """Discard a verified other-subject finding only while its claim still matches."""
        with self._transaction(write=True) as connection:
            if self._current_recovery_work(connection, work) is None:
                return False
            self._discard_recovery_row(connection, work.take_id)
            return True

    def release_recovery_work(self, work: RecordingRecoveryWork) -> bool:
        """Requeue a transiently blocked observation without losing prior visible findings."""
        with self._transaction(write=True) as connection:
            row = self._current_recovery_work(connection, work)
            if row is None or row["recording_recovery_claimed_generation"] is None:
                return False
            connection.execute(
                "UPDATE recording_recovery_findings SET recording_recovery_claimed_generation=NULL "
                "WHERE recording_subject_id=? AND recording_take_id=?",
                (self.subject_id, work.take_id),
            )
            self._recovery_changed(connection)
            return True

    def pending_recovery_take_ids(self, *, limit: int = 50) -> tuple[str, ...]:
        """Page known unclassified work, excluding protected and already claimed captures."""
        if isinstance(limit, bool) or not 1 <= limit <= 50:
            raise RecordingIndexError("pending recovery page size is outside [1, 50]")
        with self._transaction() as connection:
            return tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT recording_take_id FROM recording_recovery_findings "
                    "WHERE recording_subject_id=? "
                    "AND recording_recovery_pending=1 AND recording_recovery_protected=0 "
                    "AND recording_recovery_claimed_generation IS NULL "
                    "ORDER BY recording_take_id LIMIT ?",
                    (self.subject_id, limit),
                )
            )

    def finish_recovery_scan(self, *, covered_store_revision: int) -> bool:
        """Under the store barrier, certify completed enumeration and no pending work."""
        if isinstance(covered_store_revision, bool) or covered_store_revision < 0:
            raise RecordingIndexError("covered recording store revision must be nonnegative")
        with self._transaction(write=True) as connection:
            state = self._recovery_state(connection)
            pending = connection.execute(
                "SELECT 1 FROM recording_recovery_findings WHERE recording_subject_id=? "
                "AND recording_recovery_pending=1 LIMIT 1",
                (self.subject_id,),
            ).fetchone()
            complete = pending is None and state["recording_recovery_scan_state"] != "error"
            target = "complete" if complete else state["recording_recovery_scan_state"]
            covered = covered_store_revision if complete else None
            changed = (
                not state["recording_recovery_enumeration_complete"]
                or target != state["recording_recovery_scan_state"]
                or covered != state["recording_recovery_covered_store_revision"]
            )
            connection.execute(
                "UPDATE recording_recovery_scans SET recording_recovery_enumeration_complete=1,"
                "recording_recovery_scan_state=?,recording_recovery_covered_store_revision=? "
                "WHERE recording_subject_id=?",
                (target, covered, self.subject_id),
            )
            if changed:
                self._recovery_changed(connection)
            return complete

    def _fail_recovery_scan(self, connection: sqlite3.Connection, error: RecordingError) -> None:
        connection.execute(
            "UPDATE recording_recovery_scans SET recording_recovery_scan_state='error',"
            "recording_recovery_error_json=?,recording_recovery_covered_store_revision=NULL "
            "WHERE recording_subject_id=?",
            (canonical_json_bytes(error).decode(), self.subject_id),
        )
        self._recovery_changed(connection)

    def fail_recovery_scan(self, error: RecordingError) -> None:
        """Keep known findings and expose a scan failure; never certify incomplete evidence."""
        with self._transaction(write=True) as connection:
            self._recovery_state(connection)
            self._fail_recovery_scan(connection, error)

    def _project_recovery_take(
        self, connection: sqlite3.Connection, take: RecordingTake, current_session: bool
    ) -> None:
        state = connection.execute(
            "SELECT * FROM recording_recovery_scans WHERE recording_subject_id=?",
            (self.subject_id,),
        ).fetchone()
        if state is None:
            return  # Group-scoped services do not own a startup recovery inventory.
        take_id = take.reservation.take_id
        indexed = connection.execute(
            "SELECT recording_take_state_revision FROM recording_takes WHERE recording_take_id=? "
            "AND recording_subject_id=?",
            (take_id, self.subject_id),
        ).fetchone()
        if indexed is None or indexed[0] != take.state_revision:
            return  # An older discovery projection cannot lower the inventory guard.
        generation = str(state["recording_recovery_scan_generation"])
        row = self._recovery_row(connection, take_id)
        if row is not None and row["recording_recovery_observed_take_revision"] is not None:
            if row["recording_recovery_observed_take_revision"] > take.state_revision:
                return
            if (
                row["recording_recovery_observed_take_revision"] == take.state_revision
                and not current_session
            ):
                return
        result = None if row is None else self._decode_recovery_result(row)
        if take.seal is not None:
            # A recovered seal remains a visible outcome within its generation.
            result = (
                result.model_copy(update={"status": take.decision})
                if result is not None
                and result.recovered_seal
                and row is not None
                and row["recording_recovery_classified_generation"] == generation
                else None
            )
        protected = take.seal is None and (
            current_session or bool(row is not None and row["recording_recovery_protected"])
        )
        pending = take.seal is None
        self._write_recovery_row(
            connection,
            take_id,
            observed_revision=take.state_revision,
            token=0 if row is None else row["recording_recovery_work_token"],
            pending=pending,
            protected=protected,
            classified=None if pending else generation,
            claimed=None
            if pending or row is None
            else row["recording_recovery_claimed_generation"],
            result=result,
        )
        if pending:
            connection.execute(
                "UPDATE recording_recovery_scans SET recording_recovery_scan_state=CASE "
                "WHEN recording_recovery_scan_state='error' THEN 'error' ELSE 'scanning' END,"
                "recording_recovery_covered_store_revision=NULL WHERE recording_subject_id=?",
                (self.subject_id,),
            )
        self._recovery_changed(connection)

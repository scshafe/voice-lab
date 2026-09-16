"""Authoritative prompt and catalog store behind every published catalog version.

@canonical corpus_store -- prompts, their append-only revisions and published catalog
versions live in provenance/corpus/corpus.sqlite3; every version is pinned by the canonical
digest of the PromptSuite it materializes and backed by an immutable content-addressed
snapshot file, and takes reference a version instead of embedding the catalog.
Related: recording_contracts.py (CatalogReference, CatalogSnapshot, PromptAlias),
sqlite_migrations.py, migrations/20260916100000_create_corpus_prompt_catalog_store.sql,
prompt_catalogs.py (the released built-ins that seed and pin historical digests).

The store is never quarantined or rebuilt: its rows are the editing history and its
snapshots are evidence, so every structural problem is a hard ``CorpusStoreError``. Path,
ownership and mode invariants fail as ``CorpusStoreSecurityError``; a snapshot that a
reference names but that is missing or altered (suite or aliases: the reference pins the
sha256 of the whole file) fails as ``CorpusSnapshotSecurityError``. Both are
``SecurityError`` subclasses so existing fail-closed handling applies unchanged.
"""

from __future__ import annotations

import enum
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final, Literal
from urllib.parse import quote

from pydantic import Field, TypeAdapter, ValidationError

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    file_identity,
    require_private_metadata,
)
from .errors import ContractError, SecurityError
from .models import (
    PROMPT_SUITE_V2,
    ContractModel,
    Prompt,
    PromptSuite,
    PromptSuitePurpose,
    Sha256,
    StableId,
    has_hidden_control,
    utc_now,
)
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    MAX_CATALOG_SUITE_BYTES,
    CatalogReference,
    CatalogSnapshot,
    PromptAlias,
    RecordingGroup,
    UtcTimestamp,
    catalog_snapshot_path,
    keep_recordings_identity,
)
from .sqlite_migrations import (
    MigrationDirection,
    SqliteMigrationError,
    apply_migration,
    current_user_version,
    migration_resources,
    migration_sql,
)

type CorpusStoreCode = Literal[
    "CORPUS.STORE_MISSING",
    "CORPUS.STORE_UNAVAILABLE",
    "CORPUS.STORE_UNSAFE",
    "CORPUS.PROMPT_DUPLICATE",
    "CORPUS.PROMPT_UNKNOWN",
    "CORPUS.PROMPT_RETIRED",
    "CORPUS.PROMPT_INVALID",
    "CORPUS.PROMPT_UNCHANGED",
    "CORPUS.SOURCE_INVALID",
    "CORPUS.SOURCE_REFERENCE_INVALID",
    "CORPUS.CATALOG_DUPLICATE",
    "CORPUS.CATALOG_UNKNOWN",
    "CORPUS.CATALOG_INVALID",
    "CORPUS.VERSION_UNKNOWN",
    "CORPUS.VERSION_UNCHANGED",
    "CORPUS.SUBJECT_INVALID",
    "CORPUS.NO_ACTIVE_CATALOG",
    "CORPUS.SNAPSHOT_MISSING",
    "CORPUS.SNAPSHOT_MISMATCH",
    "CORPUS.EMPTY_CATALOG",
]
type PromptSource = Literal["pilot-v1", "pilot-v2", "harvard", "arctic", "manual", "import"]

CORPUS_MIGRATION_NAME: Final = "20260916100000_create_corpus_prompt_catalog_store.sql"
CORPUS_DIRECTORY: Final = "corpus"
CORPUS_DATABASE_NAME: Final = "corpus.sqlite3"
SNAPSHOT_DIRECTORY: Final = "snapshots"
MAX_CATALOG_PROMPTS: Final = 10_000
MAX_CATALOG_SNAPSHOT_BYTES: Final = 16 * 1024 * 1024
MAX_CATALOG_DESCRIPTION_CHARS: Final = 1024
# Resolved group catalogs kept per owner (take store, index); a process binds a handful.
MAX_CACHED_GROUP_CATALOGS: Final = 8
STORE_MISSING_MESSAGE: Final = (
    f"no corpus store at provenance/{CORPUS_DIRECTORY}/{CORPUS_DATABASE_NAME}; "
    "run `voice-lab catalog init` to create one"
)
_LEASE_NAME = ".corpus.lease"
_LOCK_NAME = ".corpus.lock"
_SNAPSHOT_NAME = re.compile(r"^[0-9a-f]{64}\.json$")
_SNAPSHOT_TEMPORARY_NAME = re.compile(r"^\.[0-9a-f]{64}\.json\.tmp-[0-9a-f]{16}$")
_STABLE_ID: TypeAdapter[str] = TypeAdapter(StableId)
_PURPOSE: TypeAdapter[PromptSuitePurpose] = TypeAdapter(PromptSuitePurpose)
_SOURCE: TypeAdapter[PromptSource] = TypeAdapter(PromptSource)
_UTC_TIMESTAMP: TypeAdapter[datetime] = TypeAdapter(UtcTimestamp)
_VERSION_PROMPT_DIGESTS = (
    "SELECT r.prompt_sha256 FROM corpus_catalog_version_prompts vp "
    "JOIN corpus_prompt_revisions r "
    "ON r.prompt_id = vp.prompt_id AND r.prompt_revision = vp.prompt_revision "
    "WHERE vp.catalog_id = ? AND vp.catalog_version = ? ORDER BY vp.catalog_prompt_ordinal"
)
_VERSION_ALIASES = (
    "SELECT r.prompt_sha256, a.accepted_prompt_sha256 FROM corpus_catalog_version_prompts vp "
    "JOIN corpus_prompt_revisions r "
    "ON r.prompt_id = vp.prompt_id AND r.prompt_revision = vp.prompt_revision "
    "JOIN corpus_prompt_aliases a "
    "ON a.prompt_id = vp.prompt_id AND a.prompt_revision = vp.prompt_revision "
    "WHERE vp.catalog_id = ? AND vp.catalog_version = ? "
    "ORDER BY r.prompt_sha256, a.accepted_prompt_sha256"
)
_LIVE_PROMPT_COLUMNS = (
    "SELECT p.prompt_id, p.prompt_source, p.prompt_source_reference, p.prompt_created_at, "
    "p.prompt_retired_at, r.prompt_revision, r.prompt_sha256, r.prompt_text, "
    "r.prompt_category, r.prompt_style, r.prompt_instructions, r.prompt_labels_json, "
    "r.prompt_expected_seconds, r.prompt_reference_candidate, r.prompt_holdout, "
    "r.prompt_revision_keeps_recordings, r.prompt_revision_created_at "
    "FROM corpus_prompts p JOIN corpus_prompt_revisions r ON r.prompt_id = p.prompt_id "
)
_CURRENT_PROMPT_QUERY = _LIVE_PROMPT_COLUMNS + "AND r.prompt_revision = p.prompt_current_revision "


class CorpusStoreError(ContractError):
    """A named corpus store failure; ``code`` is one ``CORPUS.<FAILURE>`` value.

    ``str(error)`` is the message alone. Instances pickle with code and message intact so
    one raised in another process arrives as the same error.
    """

    def __init__(self, code: CorpusStoreCode, message: str) -> None:
        self.code: CorpusStoreCode = code
        super().__init__(message)

    def __reduce__(self) -> tuple[type[CorpusStoreError], tuple[CorpusStoreCode, str]]:
        return (type(self), (self.code, str(self)))


class CorpusStoreSecurityError(SecurityError, CorpusStoreError):
    """A store directory, lock or database file failed an ownership, mode or identity check.

    Always ``CORPUS.STORE_UNSAFE``; nothing is repaired or moved aside.
    """


class CorpusSnapshotSecurityError(SecurityError, CorpusStoreError):
    """A snapshot that a reference names is missing, altered, or disagrees with the reference.

    ``CORPUS.SNAPSHOT_MISSING`` when no file exists, ``CORPUS.SNAPSHOT_MISMATCH`` otherwise.
    """


class PromptSourceReference(ContractModel):
    """Provenance of an imported sentence set, stored as canonical JSON beside its prompts."""

    url: str = Field(min_length=1, max_length=2048)
    fetched_at: UtcTimestamp
    sha256: Sha256
    license: str = Field(min_length=1, max_length=65_536)


class _Unchanged(enum.Enum):
    token = "unchanged"


UNCHANGED: Final = _Unchanged.token
"""Passed for an ``edit_prompt`` field that keeps its current value."""


@dataclass(frozen=True, slots=True)
class PromptRecord:
    """One prompt revision with its prompt-level provenance and lifecycle."""

    prompt: Prompt
    prompt_sha256: str
    revision: int
    keeps_recordings: bool
    accepted_prompt_sha256s: tuple[str, ...]
    source: PromptSource
    source_reference: str | None
    created_at: datetime
    revision_created_at: datetime
    retired_at: datetime | None


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    """One catalog with every version it has published, ascending."""

    catalog_id: str
    suite_id: str
    purpose: PromptSuitePurpose
    description: str
    created_at: datetime
    retired_at: datetime | None
    versions: tuple[CatalogReference, ...]


def corpus_store_migration_resources() -> tuple[tuple[str, str, int], ...]:
    """Fingerprint the corpus migration so a build identity can include it.

    Returns: the one ``("migrations/<name>", sha256 hex, byte length)`` entry.
    Raises: as ``sqlite_migrations.migration_resources``.
    """
    return migration_resources((CORPUS_MIGRATION_NAME,))


def corpus_store_migration(direction: MigrationDirection = "up") -> str:
    """One direction of the corpus schema migration, header discarded.

    Params: ``direction`` is ``"up"`` or ``"down"``.
    Returns: the SQL text.
    Raises: as ``sqlite_migrations.migration_sql``.
    """
    return migration_sql(CORPUS_MIGRATION_NAME, direction)


def _validation_message(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        parts.append(f"{location}: {item['msg']}" if location else item["msg"])
    return "; ".join(parts)


def _validated[ValueT](
    adapter: TypeAdapter[ValueT], value: object, code: CorpusStoreCode, label: str
) -> ValueT:
    try:
        return adapter.validate_python(value)
    except ValidationError as error:
        raise CorpusStoreError(code, f"{label}: {_validation_message(error)}") from error


def _hygienic_prompt(prompt: Prompt) -> Prompt:
    """Apply the prompt-suite/v2 text rule now, so every stored prompt can publish later."""
    if any(
        value is not None and has_hidden_control(value)
        for value in (prompt.text, prompt.instructions, prompt.style)
    ):
        raise CorpusStoreError(
            "CORPUS.PROMPT_INVALID",
            f"prompt {prompt.prompt_id} text cannot contain format or control characters",
        )
    return prompt


def _validated_prompt(payload: dict[str, object]) -> Prompt:
    try:
        prompt = Prompt.model_validate(payload)
    except ValidationError as error:
        raise CorpusStoreError(
            "CORPUS.PROMPT_INVALID", f"prompt is invalid: {_validation_message(error)}"
        ) from error
    return _hygienic_prompt(prompt)


def _validated_source_reference(source: PromptSource, reference: str | None) -> str | None:
    """Imported sets carry a reference; when one is given it must be the exact contract."""
    if reference is None:
        if source in ("harvard", "arctic"):
            raise CorpusStoreError(
                "CORPUS.SOURCE_REFERENCE_INVALID",
                f"prompt source {source} requires a source reference",
            )
        return None
    try:
        parsed = PromptSourceReference.model_validate_json(reference)
    except ValidationError as error:
        raise CorpusStoreError(
            "CORPUS.SOURCE_REFERENCE_INVALID",
            f"prompt source reference is invalid: {_validation_message(error)}",
        ) from error
    return canonical_json_bytes(parsed).decode()


def _prompt_payload(prompt: Prompt) -> dict[str, object]:
    return {
        "prompt_id": prompt.prompt_id,
        "text": prompt.text,
        "category": prompt.category,
        "style": prompt.style,
        "instructions": prompt.instructions,
        "labels": prompt.labels,
        "expected_seconds": prompt.expected_seconds,
        "reference_candidate": prompt.reference_candidate,
        "holdout": prompt.holdout,
    }


def _aware(value: datetime) -> datetime:
    """Return a clock reading unchanged; a naive one is the caller's ``ValueError``."""
    if value.utcoffset() is None:
        raise ValueError("corpus store clock must return a timezone-aware datetime")
    return value


def _problem(error: ValueError) -> str:
    """One-line text of a row validation failure (pydantic errors span several lines)."""
    return _validation_message(error) if isinstance(error, ValidationError) else str(error)


def _timestamp(value: object, column: str) -> datetime:
    """Parse a stored ``*_at`` column; a naive or malformed value is a ``ValueError``."""
    try:
        return _UTC_TIMESTAMP.validate_python(value)
    except ValidationError as error:
        raise ValueError(f"{column}: {_validation_message(error)}") from error


def _snapshot_payload(stream: BinaryIO, label: str) -> bytes:
    """Read one already opened snapshot after its private-file and size checks."""
    metadata = os.fstat(stream.fileno())
    try:
        require_private_metadata(metadata, label)
    except SecurityError as error:
        raise CorpusSnapshotSecurityError("CORPUS.SNAPSHOT_MISMATCH", str(error)) from error
    if metadata.st_size > MAX_CATALOG_SNAPSHOT_BYTES:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH", f"catalog snapshot exceeds its byte limit: {label}"
        )
    return stream.read(MAX_CATALOG_SNAPSHOT_BYTES + 1)


def _decode_snapshot(payload: bytes, reference: CatalogReference) -> CatalogSnapshot:
    """Validate snapshot bytes against the contract and the reference that named them.

    The whole-file digest comes first: it is what binds the alias half of the file, which
    ``catalog_sha256`` (the suite digest) does not cover.
    """
    if hashlib.sha256(payload).hexdigest() != reference.snapshot_sha256:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} does not have the published bytes",
        )
    try:
        snapshot = CatalogSnapshot.model_validate_json(payload)
    except ValidationError as error:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} is not a valid snapshot: "
            f"{_validation_message(error)}",
        ) from error
    if payload != canonical_json_bytes(snapshot) + b"\n":
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} is not canonical",
        )
    if snapshot.catalog_sha256 != reference.catalog_sha256:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} carries digest "
            f"{snapshot.catalog_sha256}, not the referenced one",
        )
    if snapshot.suite.suite_id != reference.suite_id or snapshot.suite.purpose != reference.purpose:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} declares suite "
            f"{snapshot.suite.suite_id}/{snapshot.suite.purpose}, not "
            f"{reference.suite_id}/{reference.purpose}",
        )
    return snapshot


def read_catalog_snapshot(layout: AssetLayout, reference: CatalogReference) -> CatalogSnapshot:
    """Read and verify the snapshot a reference names, without opening the database.

    Params: ``layout`` is the asset root; ``reference`` names the snapshot by its
    content-addressed path.
    Returns: the validated ``CatalogSnapshot`` whose file bytes, digest, suite id and
    purpose equal the reference's.
    Raises: ``CorpusSnapshotSecurityError`` with ``CORPUS.SNAPSHOT_MISSING`` when no file
    exists at the path, ``CORPUS.SNAPSHOT_MISMATCH`` when the path has a symlink component
    (the file itself included) or cannot be examined, the entry is not a regular file
    (checked by ``lstat`` before any open, so a planted FIFO cannot block the reader), the
    file cannot be opened or is not an owned private regular file with one link, exceeds
    ``MAX_CATALOG_SNAPSHOT_BYTES``, has bytes other than the ``snapshot_sha256`` the
    reference pins, is not a canonical ``CatalogSnapshot``, or disagrees with the reference.
    """
    try:
        # confined_path lstat-walks the components itself, so it can raise OSError too.
        path = confined_path(layout.root, reference.snapshot_path)
        metadata = os.lstat(path)
    except SecurityError as error:
        raise CorpusSnapshotSecurityError("CORPUS.SNAPSHOT_MISMATCH", str(error)) from error
    except FileNotFoundError as error:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISSING", f"catalog snapshot is missing: {reference.snapshot_path}"
        ) from error
    except (OSError, ValueError) as error:
        # ValueError: a reference built without validation can carry an absolute or
        # escaping snapshot_path, which confined_path refuses before touching the disk.
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot cannot be examined: {reference.snapshot_path}: {error}",
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot is not a regular file: {reference.snapshot_path}",
        )
    try:
        # O_NONBLOCK: a FIFO swapped in after the lstat above cannot block the open;
        # _snapshot_payload's fstat then refuses anything that is not a private regular file.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISSING", f"catalog snapshot is missing: {reference.snapshot_path}"
        ) from error
    except OSError as error:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot cannot be opened: {reference.snapshot_path}: {error}",
        ) from error
    with os.fdopen(descriptor, "rb") as stream:
        payload = _snapshot_payload(stream, reference.snapshot_path)
    return _decode_snapshot(payload, reference)


def require_referenced_snapshot(snapshot: CatalogSnapshot, reference: CatalogReference) -> None:
    """Check that an in-memory snapshot is exactly the file a reference seals.

    For readers handed a (reference, snapshot) pair, e.g. from ``CorpusStore.materialize``,
    that record the reference next to results derived from the snapshot. The snapshot's
    file bytes (canonical JSON plus a newline, as ``publish_version`` writes them) are
    checked exactly as ``read_catalog_snapshot`` checks a file, so the prompt aliases are
    bound too. A plain suite's ``CatalogSnapshot.embedded`` matches only a published version
    that accepts no aliases.
    Params: ``snapshot`` is the catalog the caller selects through; ``reference`` is the
    published version the caller will name.
    Returns: nothing.
    Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) with
    ``CORPUS.SNAPSHOT_MISMATCH`` when the bytes, suite digest, suite id or purpose disagree.
    """
    _decode_snapshot(canonical_json_bytes(snapshot) + b"\n", reference)


def resolve_group_catalog(layout: AssetLayout, group: RecordingGroup) -> CatalogSnapshot:
    """The exact catalog a recording group binds, as a snapshot; the one group-catalog reader.

    Pure file access, never the database, so the read-only discovery worker and the
    coverage report can use it while another process holds the store.
    Params: ``layout`` is the asset root the group belongs to; ``group`` is a validated
    ``RecordingGroup`` of either version.
    Returns: for recording-group/v1, ``CatalogSnapshot.embedded`` of the embedded suite (no
    aliases); for recording-group/v2, the snapshot file its ``catalog_reference`` names,
    verified by ``read_catalog_snapshot`` (whole-file digest, suite digest, suite id,
    purpose and content-addressed path all equal the sealed reference).
    Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) for a v2 group:
    ``CORPUS.SNAPSHOT_MISSING`` when the referenced snapshot file does not exist,
    ``CORPUS.SNAPSHOT_MISMATCH`` when it is altered, unsafe or disagrees with the
    reference. A v2 group never falls back to anything else.
    """
    if group.catalog is not None:
        return CatalogSnapshot.embedded(group.catalog)
    # RecordingGroup validation guarantees exactly one form and binds the reference's
    # catalog_sha256 to the group's; read_catalog_snapshot binds the file to the reference.
    assert group.catalog_reference is not None
    return read_catalog_snapshot(layout, group.catalog_reference)


type GroupCatalogKey = tuple[str, CatalogReference | None]


class GroupCatalogCache:
    """A small per-owner LRU in front of ``resolve_group_catalog``.

    Entries are keyed by the suite digest plus, for a recording-group/v2, the whole sealed
    ``CatalogReference`` -- every field the file check compares (snapshot digest, suite
    digest, suite id, purpose, path) -- so a cached entry is exactly what a fresh verified
    read of that reference returns, a reference that disagrees with a snapshot another group
    already verified is still read (and refused), and an embedded catalog never shares an
    entry with a snapshot of the same suite. Each reference is therefore verified once per
    owner instance; a file altered after that is detected by the next owner (a restarted
    service, a discovery worker, a new index). Thread-safe; the file is read outside the
    cache lock.
    """

    def __init__(self, layout: AssetLayout, *, capacity: int = MAX_CACHED_GROUP_CATALOGS) -> None:
        """Params: ``layout`` is the asset root every resolved group belongs to;
        ``capacity`` is the most snapshots kept (least recently used leave first).
        Raises: ``ValueError`` for a capacity below one.
        """
        if capacity < 1:
            raise ValueError("a group catalog cache needs room for at least one catalog")
        self.layout = layout
        self.capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[GroupCatalogKey, CatalogSnapshot] = OrderedDict()

    @staticmethod
    def key(group: RecordingGroup) -> GroupCatalogKey:
        """The identity two groups share exactly when one verified read serves both.

        Readers that resolve each distinct group catalog once (reconcile, coverage) dedupe
        on this key too, so it binds everything ``read_catalog_snapshot`` checks.
        Params: ``group`` is a validated group of either version.
        Returns: ``(catalog_sha256, None)`` for recording-group/v1 (the digest binds the
        embedded suite) or ``(catalog_sha256, catalog_reference)`` for recording-group/v2
        (the sealed reference, whose snapshot digest also binds the prompt aliases and whose
        suite id and purpose the file must repeat).
        Raises: nothing.
        """
        return group.catalog_sha256, group.catalog_reference

    def resolve(self, group: RecordingGroup) -> CatalogSnapshot:
        """The exact catalog ``group`` binds, from the cache or ``resolve_group_catalog``.

        Params: ``group`` is a validated group of either version.
        Returns: the embedded suite without aliases (v1) or the verified snapshot (v2).
        Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) with
        ``CORPUS.SNAPSHOT_MISSING`` or ``CORPUS.SNAPSHOT_MISMATCH`` when a v2 group's snapshot
        file is missing, altered or disagrees with its reference; nothing is cached then.
        """
        return self._resolve(self.key(group), lambda: resolve_group_catalog(self.layout, group))

    def resolve_reference(self, reference: CatalogReference) -> CatalogSnapshot:
        """The snapshot a recording-group/v2 carrying ``reference`` binds, before one exists.

        Shares the entry such a group resolves through (``key`` is
        ``(reference.catalog_sha256, reference)``), so minting the group afterwards reads the
        file no second time.
        Params: ``reference`` is a validated catalog reference (e.g. an open-context's).
        Returns: the snapshot ``read_catalog_snapshot`` verifies against the reference.
        Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) with
        ``CORPUS.SNAPSHOT_MISSING`` or ``CORPUS.SNAPSHOT_MISMATCH`` as
        ``read_catalog_snapshot``; nothing is cached then.
        """
        return self._resolve(
            (reference.catalog_sha256, reference),
            lambda: read_catalog_snapshot(self.layout, reference),
        )

    def _resolve(
        self, key: GroupCatalogKey, read: Callable[[], CatalogSnapshot]
    ) -> CatalogSnapshot:
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
        # A concurrent duplicate read resolves the same verified bytes.
        snapshot = read()
        with self._lock:
            self._entries[key] = snapshot
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
        return snapshot


def open_corpus_store(
    layout: AssetLayout, *, create: bool, clock: Callable[[], datetime] = utc_now
) -> CorpusStore:
    """Open the corpus store under ``provenance/corpus`` as a context manager.

    Params: ``layout`` is the validated asset root; ``create`` allows the directory and an
    empty database to be created when absent (an existing store is opened either way);
    ``clock`` supplies every stored timestamp and must return timezone-aware datetimes.
    Returns: an open ``CorpusStore``; ``with open_corpus_store(...) as store:`` closes it.
    Raises: ``ValueError`` when ``clock`` returns a naive datetime (checked once here,
    before anything is opened or created, and again for every write);
    ``CorpusStoreError`` with ``CORPUS.STORE_MISSING`` when ``create`` is false and
    no database exists, ``CORPUS.STORE_UNAVAILABLE`` when the database cannot be opened,
    migrated or passes no integrity check, or when its schema version is not 0 or 1;
    ``CorpusStoreSecurityError`` (``CORPUS.STORE_UNSAFE``) when any directory or member is
    not owned and private, is a symlink, was replaced, or is not part of the store;
    ``SecurityError`` from ``AssetLayout.validate`` for an unsafe root.
    """
    return CorpusStore(layout, create=create, clock=clock)


class CorpusStore:
    """Guarded SQLite store: descriptor-bound directories, one exclusive lock per write.

    The root, ``provenance``, ``corpus`` and ``snapshots`` directories stay bound for the
    store's lifetime and every transaction rechecks their identities, owners, modes and the
    membership of ``corpus``. A shared flock on ``.corpus.lease`` marks the store live; an
    exclusive flock on ``.corpus.lock`` serializes open-and-migrate and every transaction
    across processes, so two publishers queue and receive consecutive version numbers.

    Opening also discards snapshot scratch: a ``.<sha>.json.tmp-*`` entry only ever exists
    while a publisher holds the exclusive lock, so one found at open is the remains of a
    crashed publish (an unfinished write, or the second name of a snapshot that was already
    linked). No row or reference ever names such an entry; removing it is not a repair of
    evidence, and nothing else under the store is ever moved, rewritten or removed.
    """

    def __init__(self, layout: AssetLayout, *, create: bool, clock: Callable[[], datetime]) -> None:
        self.layout = AssetLayout.validate(layout.root)
        self.directory = self.layout.directory("provenance") / CORPUS_DIRECTORY
        self.path = self.directory / CORPUS_DATABASE_NAME
        self.snapshot_directory = self.directory / SNAPSHOT_DIRECTORY
        # A naive clock is the caller's bug: refuse it before anything is opened or created.
        _aware(clock())
        self._clock = clock
        self._descriptors: list[int] = []
        self._directory_bindings: list[tuple[Path, int]] = []
        self._mutex = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        try:
            for path in (self.layout.root, self.layout.directory("provenance")):
                self._bind_directory(
                    path, os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                )
            provenance_fd = self._descriptors[-1]
            if create:
                try:
                    os.mkdir(CORPUS_DIRECTORY, mode=0o700, dir_fd=provenance_fd)
                    os.fsync(provenance_fd)
                except FileExistsError:
                    pass  # A concurrent creator; the owned-directory check below decides.
            try:
                self._require_private(
                    os.stat(CORPUS_DIRECTORY, dir_fd=provenance_fd, follow_symlinks=False),
                    CORPUS_DIRECTORY,
                    directory=True,
                )
            except FileNotFoundError as error:
                raise CorpusStoreError("CORPUS.STORE_MISSING", STORE_MISSING_MESSAGE) from error
            corpus_fd = os.open(
                CORPUS_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=provenance_fd
            )
            self._bind_directory(self.directory, corpus_fd)
            self._corpus_fd = corpus_fd
            if not create:
                self._require_database_present()
            self._lease_fd = self._open_private(_LEASE_NAME, os.O_RDWR | os.O_CREAT)
            fcntl.flock(self._lease_fd, fcntl.LOCK_SH)
            self._lock_fd = self._open_private(_LOCK_NAME, os.O_RDWR | os.O_CREAT)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                self._database_identity = self._bind_database(create)
                try:
                    os.mkdir(SNAPSHOT_DIRECTORY, mode=0o700, dir_fd=corpus_fd)
                    os.fsync(corpus_fd)
                except FileExistsError:
                    pass  # Present from an earlier open; the owned-directory check decides.
                self._require_private(
                    os.stat(SNAPSHOT_DIRECTORY, dir_fd=corpus_fd, follow_symlinks=False),
                    SNAPSHOT_DIRECTORY,
                    directory=True,
                )
                self._snapshots_fd = os.open(
                    SNAPSHOT_DIRECTORY,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=corpus_fd,
                )
                self._bind_directory(self.snapshot_directory, self._snapshots_fd)
                self._discard_snapshot_scratch()
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
                self._connection.execute("PRAGMA temp_store = MEMORY")
                version = current_user_version(self._connection)
                if version not in (0, 1):
                    raise CorpusStoreError(
                        "CORPUS.STORE_UNAVAILABLE",
                        f"unsupported corpus store schema version: {version}",
                    )
                if version == 0:
                    if self._connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                        raise CorpusStoreError(
                            "CORPUS.STORE_UNAVAILABLE",
                            "unversioned nonempty database cannot become the corpus store",
                        )
                    apply_migration(
                        self._connection,
                        version=1,
                        up_sql=corpus_store_migration("up"),
                        directory_fd=corpus_fd,
                    )
                self._check_paths()
                self._audit()
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except CorpusStoreError:
            self.close()
            raise
        except (OSError, sqlite3.Error, SqliteMigrationError) as error:
            self.close()
            raise CorpusStoreError(
                "CORPUS.STORE_UNAVAILABLE", f"cannot open corpus store: {error}"
            ) from error
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> CorpusStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the connection, locks and directory descriptors; idempotent."""
        with self._mutex:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            for descriptor in reversed(self._descriptors):
                os.close(descriptor)
            self._descriptors.clear()
            self._closed = True

    # -- guarded filesystem boundary -------------------------------------------------

    def _bind_directory(self, path: Path, descriptor: int) -> None:
        self._descriptors.append(descriptor)
        self._directory_bindings.append((path, descriptor))
        self._require_private(os.fstat(descriptor), str(path), directory=True)

    @staticmethod
    def _require_private(metadata: os.stat_result, label: str, *, directory: bool = False) -> None:
        try:
            require_private_metadata(metadata, label, directory=directory)
        except SecurityError as error:
            raise CorpusStoreSecurityError(
                "CORPUS.STORE_UNSAFE", f"corpus store {error}"
            ) from error

    def _open_private(self, name: str, flags: int) -> int:
        """Open (or create) one guard file; an existing entry must already be private."""
        try:
            existing = os.stat(name, dir_fd=self._corpus_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass  # Created by the open below with mode 0600.
        else:
            self._require_private(existing, name)
        descriptor = os.open(
            name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._corpus_fd
        )
        self._descriptors.append(descriptor)
        self._require_private(os.fstat(descriptor), name)
        return descriptor

    def _discard_snapshot_scratch(self) -> None:
        """Remove temporaries a crashed publish left behind; see the class docstring."""
        removed = False
        for name in os.listdir(self._snapshots_fd):
            if not _SNAPSHOT_TEMPORARY_NAME.match(name):
                continue
            metadata = os.stat(name, dir_fd=self._snapshots_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise CorpusStoreSecurityError(
                    "CORPUS.STORE_UNSAFE", f"unexpected catalog snapshot member: {name}"
                )
            os.unlink(name, dir_fd=self._snapshots_fd)
            removed = True
        if removed:
            os.fsync(self._snapshots_fd)

    def _require_database_present(self) -> None:
        try:
            os.stat(CORPUS_DATABASE_NAME, dir_fd=self._corpus_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            raise CorpusStoreError("CORPUS.STORE_MISSING", STORE_MISSING_MESSAGE) from error

    def _bind_database(self, create: bool) -> tuple[int, int]:
        """Bind the database inode without leaving a descriptor open on a live SQLite file.

        Closing any extra descriptor on a file SQLite has open discards this process's POSIX
        locks on it, so only a newly and exclusively created inode is opened here, and that
        descriptor closes before SQLite connects.
        """
        try:
            metadata = os.stat(CORPUS_DATABASE_NAME, dir_fd=self._corpus_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            if not create:
                raise CorpusStoreError("CORPUS.STORE_MISSING", STORE_MISSING_MESSAGE) from error
            descriptor = os.open(
                CORPUS_DATABASE_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._corpus_fd,
            )
            try:
                metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(self._corpus_fd)
        self._require_private(metadata, CORPUS_DATABASE_NAME)
        identity = file_identity(metadata)
        current = os.stat(CORPUS_DATABASE_NAME, dir_fd=self._corpus_fd, follow_symlinks=False)
        self._require_private(current, CORPUS_DATABASE_NAME)
        if file_identity(current) != identity:
            raise CorpusStoreSecurityError(
                "CORPUS.STORE_UNSAFE", "corpus store database was replaced"
            )
        return identity

    def _check_paths(self) -> None:
        for path, descriptor in self._directory_bindings:
            current = os.stat(path, follow_symlinks=False)
            self._require_private(current, str(path), directory=True)
            if file_identity(current) != file_identity(os.fstat(descriptor)):
                raise CorpusStoreSecurityError(
                    "CORPUS.STORE_UNSAFE", f"corpus store directory was replaced: {path}"
                )
        sidecars = tuple(CORPUS_DATABASE_NAME + suffix for suffix in ("-wal", "-shm", "-journal"))
        allowed = {_LOCK_NAME, _LEASE_NAME, CORPUS_DATABASE_NAME, SNAPSHOT_DIRECTORY, *sidecars}
        members = os.listdir(self._corpus_fd)
        for name in members:
            if name not in allowed:
                raise CorpusStoreSecurityError(
                    "CORPUS.STORE_UNSAFE", f"unexpected corpus store member: {name}"
                )
            # Stat only: opening and closing a live SQLite file or sidecar drops record locks.
            current = os.stat(name, dir_fd=self._corpus_fd, follow_symlinks=False)
            if name == SNAPSHOT_DIRECTORY:
                self._require_private(current, name, directory=True)
                continue
            self._require_private(current, name)
            bound = {
                CORPUS_DATABASE_NAME: self._database_identity,
                _LOCK_NAME: file_identity(os.fstat(self._lock_fd)),
                _LEASE_NAME: file_identity(os.fstat(self._lease_fd)),
            }.get(name)
            if bound is not None and file_identity(current) != bound:
                raise CorpusStoreSecurityError(
                    "CORPUS.STORE_UNSAFE", f"corpus store member was replaced: {name}"
                )
        for required in (CORPUS_DATABASE_NAME, _LOCK_NAME, _LEASE_NAME, SNAPSHOT_DIRECTORY):
            if required not in members:
                raise CorpusStoreSecurityError(
                    "CORPUS.STORE_UNSAFE", f"corpus store member disappeared: {required}"
                )
        for name in os.listdir(self._snapshots_fd):
            if not (_SNAPSHOT_NAME.match(name) or _SNAPSHOT_TEMPORARY_NAME.match(name)):
                raise CorpusStoreSecurityError(
                    "CORPUS.STORE_UNSAFE", f"unexpected catalog snapshot member: {name}"
                )
            self._require_private(
                os.stat(name, dir_fd=self._snapshots_fd, follow_symlinks=False), name
            )

    def _audit(self) -> None:
        connection = self._require_open()
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise CorpusStoreError(
                "CORPUS.STORE_UNAVAILABLE", "corpus store structural integrity check failed"
            )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CorpusStoreError(
                "CORPUS.STORE_UNAVAILABLE", "corpus store references are inconsistent"
            )

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None or self._closed:
            raise CorpusStoreError("CORPUS.STORE_UNAVAILABLE", "corpus store is closed")
        return self._connection

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            connection = self._require_open()
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                self._check_paths()
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield connection
                    self._check_paths()
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
            except CorpusStoreError:
                raise
            except (OSError, sqlite3.Error) as error:
                raise CorpusStoreError(
                    "CORPUS.STORE_UNAVAILABLE", f"corpus store query failed: {error}"
                ) from error
            except (ValueError, ValidationError) as error:
                raise CorpusStoreError(
                    "CORPUS.STORE_UNAVAILABLE", f"corpus store row is invalid: {_problem(error)}"
                ) from error
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _now(self) -> str:
        """The clock's current instant as stored text.

        Called before a transaction opens, so a clock that turns naive after the open-time
        check stays the caller's ``ValueError`` instead of a store failure.
        """
        return _aware(self._clock()).astimezone(UTC).isoformat(timespec="microseconds")

    # -- snapshots ---------------------------------------------------------------------

    def _snapshot_bytes(self, name: str) -> bytes:
        # O_NONBLOCK: a FIFO planted since _check_paths cannot block the open.
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=self._snapshots_fd,
        )
        with os.fdopen(descriptor, "rb") as stream:
            return _snapshot_payload(stream, f"{SNAPSHOT_DIRECTORY}/{name}")

    def _read_snapshot(self, reference: CatalogReference) -> CatalogSnapshot:
        name = f"{reference.catalog_sha256}.json"
        try:
            payload = self._snapshot_bytes(name)
        except FileNotFoundError as error:
            raise CorpusSnapshotSecurityError(
                "CORPUS.SNAPSHOT_MISSING",
                f"catalog snapshot is missing: {reference.snapshot_path}",
            ) from error
        return _decode_snapshot(payload, reference)

    def _write_snapshot(self, name: str, payload: bytes) -> None:
        """Write the content-addressed snapshot once; an existing file must already be it."""
        try:
            os.stat(name, dir_fd=self._snapshots_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if self._snapshot_bytes(name) != payload:
                raise CorpusSnapshotSecurityError(
                    "CORPUS.SNAPSHOT_MISMATCH",
                    f"existing catalog snapshot differs from the content it names: {name}",
                )
            return
        temporary = f".{name}.tmp-{secrets.token_hex(8)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._snapshots_fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # link() fails on an existing name, so a snapshot can never be overwritten.
            os.link(temporary, name, src_dir_fd=self._snapshots_fd, dst_dir_fd=self._snapshots_fd)
        finally:
            # A failed write or link leaves no scratch; a crash in between is cleaned at open.
            os.unlink(temporary, dir_fd=self._snapshots_fd)
        os.fsync(self._snapshots_fd)

    # -- prompts -----------------------------------------------------------------------

    def _record(self, row: sqlite3.Row, accepted: Sequence[str]) -> PromptRecord:
        try:
            labels = json.loads(row["prompt_labels_json"])
            if not isinstance(labels, list):
                raise ValueError("labels are not a JSON array")
            prompt = Prompt.model_validate(
                {
                    "prompt_id": row["prompt_id"],
                    "text": row["prompt_text"],
                    "category": row["prompt_category"],
                    "style": row["prompt_style"],
                    "instructions": row["prompt_instructions"],
                    "labels": tuple(labels),
                    "expected_seconds": float(row["prompt_expected_seconds"]),
                    "reference_candidate": bool(row["prompt_reference_candidate"]),
                    "holdout": bool(row["prompt_holdout"]),
                }
            )
            source = _SOURCE.validate_python(row["prompt_source"])
            retired = row["prompt_retired_at"]
            created_at = _timestamp(row["prompt_created_at"], "prompt_created_at")
            revision_created_at = _timestamp(
                row["prompt_revision_created_at"], "prompt_revision_created_at"
            )
            retired_at = None if retired is None else _timestamp(retired, "prompt_retired_at")
        except (ValueError, ValidationError) as error:
            raise CorpusStoreError(
                "CORPUS.STORE_UNAVAILABLE",
                f"stored prompt revision is invalid: {row['prompt_id']}: {_problem(error)}",
            ) from error
        digest = canonical_sha256(prompt)
        if digest != row["prompt_sha256"]:
            raise CorpusStoreError(
                "CORPUS.STORE_UNAVAILABLE",
                f"stored prompt digest disagrees with its content: {row['prompt_id']}",
            )
        return PromptRecord(
            prompt=prompt,
            prompt_sha256=digest,
            revision=int(row["prompt_revision"]),
            keeps_recordings=bool(row["prompt_revision_keeps_recordings"]),
            accepted_prompt_sha256s=tuple(sorted(accepted)),
            source=source,
            source_reference=row["prompt_source_reference"],
            created_at=created_at,
            revision_created_at=revision_created_at,
            retired_at=retired_at,
        )

    @staticmethod
    def _accepted(connection: sqlite3.Connection, prompt_id: str, revision: int) -> tuple[str, ...]:
        rows = connection.execute(
            "SELECT accepted_prompt_sha256 FROM corpus_prompt_aliases "
            "WHERE prompt_id = ? AND prompt_revision = ? ORDER BY accepted_prompt_sha256",
            (prompt_id, revision),
        ).fetchall()
        return tuple(row["accepted_prompt_sha256"] for row in rows)

    def _current(self, connection: sqlite3.Connection, prompt_id: str) -> PromptRecord:
        row = connection.execute(
            _CURRENT_PROMPT_QUERY + "WHERE p.prompt_id = ?", (prompt_id,)
        ).fetchone()
        if row is None:
            raise CorpusStoreError("CORPUS.PROMPT_UNKNOWN", f"unknown prompt: {prompt_id}")
        return self._record(row, self._accepted(connection, prompt_id, row["prompt_revision"]))

    @staticmethod
    def _require_unique_text(connection: sqlite3.Connection, prompt: Prompt) -> None:
        if prompt.text is None:
            return
        row = connection.execute(
            "SELECT p.prompt_id FROM corpus_prompts p JOIN corpus_prompt_revisions r "
            "ON r.prompt_id = p.prompt_id AND r.prompt_revision = p.prompt_current_revision "
            "WHERE p.prompt_retired_at IS NULL AND r.prompt_text = ? AND p.prompt_id != ?",
            (prompt.text, prompt.prompt_id),
        ).fetchone()
        if row is not None:
            raise CorpusStoreError(
                "CORPUS.PROMPT_DUPLICATE",
                f"prompt {prompt.prompt_id} repeats the text of live prompt {row['prompt_id']}",
            )

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        prompt: Prompt,
        *,
        revision: int,
        keeps_recordings: bool,
        accepted: Sequence[str],
        created_at: str,
    ) -> str:
        digest = canonical_sha256(prompt)
        connection.execute(
            "INSERT INTO corpus_prompt_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                prompt.prompt_id,
                revision,
                digest,
                prompt.text,
                prompt.category,
                prompt.style,
                prompt.instructions,
                canonical_json_bytes(list(prompt.labels)).decode(),
                prompt.expected_seconds,
                int(prompt.reference_candidate),
                int(prompt.holdout),
                int(keeps_recordings),
                created_at,
            ),
        )
        connection.executemany(
            "INSERT INTO corpus_prompt_aliases VALUES (?, ?, ?)",
            [(prompt.prompt_id, revision, value) for value in sorted(accepted)],
        )
        return digest

    def _insert_prompt(
        self,
        connection: sqlite3.Connection,
        prompt: Prompt,
        *,
        source: PromptSource,
        source_reference: str | None,
        created_at: str,
    ) -> PromptRecord:
        if connection.execute(
            "SELECT 1 FROM corpus_prompts WHERE prompt_id = ?", (prompt.prompt_id,)
        ).fetchone():
            raise CorpusStoreError(
                "CORPUS.PROMPT_DUPLICATE", f"prompt id already exists: {prompt.prompt_id}"
            )
        self._require_unique_text(connection, prompt)
        ordinal = connection.execute(
            "SELECT COALESCE(MAX(prompt_insertion_ordinal), -1) + 1 FROM corpus_prompts"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO corpus_prompts VALUES (?, ?, ?, NULL, ?, ?, 1)",
            (prompt.prompt_id, ordinal, created_at, source, source_reference),
        )
        self._insert_revision(
            connection,
            prompt,
            revision=1,
            keeps_recordings=False,
            accepted=(),
            created_at=created_at,
        )
        return self._current(connection, prompt.prompt_id)

    def add_prompt(
        self,
        prompt_id: str,
        *,
        text: str | None,
        category: str,
        style: str,
        expected_seconds: float,
        instructions: str | None = None,
        labels: Sequence[str] = (),
        reference_candidate: bool = False,
        holdout: bool = False,
        source: str,
        source_reference: str | None = None,
    ) -> PromptRecord:
        """Add one prompt as revision 1.

        Params: the ``Prompt`` contract fields (``text`` may be ``None`` only for silent
        categories, which carry ``instructions``); ``source`` names where the prompt came
        from; ``source_reference`` is the ``PromptSourceReference`` JSON for imported sets.
        Returns: the stored record with its canonical ``prompt_sha256``.
        Raises: ``CorpusStoreError`` with ``CORPUS.PROMPT_INVALID`` when the fields do not
        form a valid ``Prompt`` or text, instructions or style carry a format or control
        character (the prompt-suite/v2 rule, applied here so the prompt can always publish),
        ``CORPUS.SOURCE_INVALID`` for an unknown source, ``CORPUS.SOURCE_REFERENCE_INVALID``
        for a malformed reference or a harvard/arctic prompt without one,
        ``CORPUS.PROMPT_DUPLICATE`` when the id exists or a live prompt already has exactly
        this text; store and path failures as ``open_corpus_store``.
        """
        prompt = _validated_prompt(
            {
                "prompt_id": prompt_id,
                "text": text,
                "category": category,
                "style": style,
                "instructions": instructions,
                "labels": tuple(labels),
                "expected_seconds": expected_seconds,
                "reference_candidate": reference_candidate,
                "holdout": holdout,
            }
        )
        prompt_source = _validated(_SOURCE, source, "CORPUS.SOURCE_INVALID", "prompt source")
        reference = _validated_source_reference(prompt_source, source_reference)
        now = self._now()
        with self._transaction(write=True) as connection:
            return self._insert_prompt(
                connection, prompt, source=prompt_source, source_reference=reference, created_at=now
            )

    def import_prompt_suite(
        self, suite: PromptSuite, *, source: str, source_reference: str | None = None
    ) -> tuple[str, ...]:
        """Seed every prompt of a suite in one transaction, in suite order.

        Params: ``suite`` is a validated ``PromptSuite`` (a built-in or an imported file);
        ``source`` and ``source_reference`` as in ``add_prompt``, applied to every prompt.
        Returns: the added prompt ids in order.
        Raises: as ``add_prompt`` (``CORPUS.PROMPT_INVALID`` covers a v1 suite whose text
        carries format or control characters); ``CORPUS.PROMPT_DUPLICATE`` when any prompt
        repeats an existing id or a live prompt's text, in which case nothing is added.
        """
        prompts = tuple(_hygienic_prompt(prompt) for prompt in suite.prompts)
        prompt_source = _validated(_SOURCE, source, "CORPUS.SOURCE_INVALID", "prompt source")
        reference = _validated_source_reference(prompt_source, source_reference)
        now = self._now()
        with self._transaction(write=True) as connection:
            return tuple(
                self._insert_prompt(
                    connection,
                    prompt,
                    source=prompt_source,
                    source_reference=reference,
                    created_at=now,
                ).prompt.prompt_id
                for prompt in prompts
            )

    def edit_prompt(
        self,
        prompt_id: str,
        *,
        keep_recordings: bool = False,
        text: str | None | _Unchanged = UNCHANGED,
        category: str | _Unchanged = UNCHANGED,
        style: str | _Unchanged = UNCHANGED,
        instructions: str | None | _Unchanged = UNCHANGED,
        labels: Sequence[str] | _Unchanged = UNCHANGED,
        expected_seconds: float | _Unchanged = UNCHANGED,
        reference_candidate: bool | _Unchanged = UNCHANGED,
        holdout: bool | _Unchanged = UNCHANGED,
    ) -> PromptRecord:
        """Append a revision with the given fields changed; unmentioned fields stay.

        A default revision accepts no earlier digest, so takes recorded against the earlier
        text stop counting until the prompt is re-recorded. With ``keep_recordings`` the
        revision accepts the previous revision's digest plus everything that revision
        accepted (minus its own digest when the edit reverts to an earlier text); the chain
        breaks at the first revision made without the flag. A keep-recordings revision must
        keep the prompt's ``keep_recordings_identity``: it cannot change the held-out flag
        or the spoken/silent behaviour (a room tone never becomes speech coverage, and
        evaluation audio never becomes training audio); make such a change as a default edit.
        A catalog version's identity is its suite content, so recordings kept only through
        a revert (A, then B kept, then A again kept) count again only in a version whose
        content differs from the one already published for A; ``publish_version`` names
        such prompts when it refuses to republish that content.
        Params: ``prompt_id``; ``keep_recordings``; each field as in ``add_prompt`` or
        ``UNCHANGED``.
        Returns: the new current record.
        Raises: ``CorpusStoreError`` with ``CORPUS.PROMPT_UNKNOWN``, ``CORPUS.PROMPT_RETIRED``,
        ``CORPUS.PROMPT_INVALID`` (the edited fields do not form a valid ``Prompt``, or a
        keep-recordings edit changes the held-out flag or spoken/silent behaviour),
        ``CORPUS.PROMPT_UNCHANGED`` (the edit reproduces the current content exactly) or
        ``CORPUS.PROMPT_DUPLICATE`` (another live prompt already has the new text); store and
        path failures as ``open_corpus_store``.
        """
        requested: dict[str, object] = {
            "text": text,
            "category": category,
            "style": style,
            "instructions": instructions,
            "labels": labels if isinstance(labels, _Unchanged) else tuple(labels),
            "expected_seconds": expected_seconds,
            "reference_candidate": reference_candidate,
            "holdout": holdout,
        }
        now = self._now()
        with self._transaction(write=True) as connection:
            current = self._current(connection, prompt_id)
            if current.retired_at is not None:
                raise CorpusStoreError(
                    "CORPUS.PROMPT_RETIRED", f"prompt is retired and cannot be edited: {prompt_id}"
                )
            payload = _prompt_payload(current.prompt)
            payload.update(
                {key: value for key, value in requested.items() if value is not UNCHANGED}
            )
            prompt = _validated_prompt(payload)
            digest = canonical_sha256(prompt)
            if digest == current.prompt_sha256:
                raise CorpusStoreError(
                    "CORPUS.PROMPT_UNCHANGED", f"edit leaves prompt {prompt_id} unchanged"
                )
            self._require_unique_text(connection, prompt)
            accepted: set[str] = set()
            if keep_recordings:
                if keep_recordings_identity(prompt) != keep_recordings_identity(current.prompt):
                    raise CorpusStoreError(
                        "CORPUS.PROMPT_INVALID",
                        f"a keep-recordings edit of {prompt_id} cannot change its held-out flag "
                        "or whether it is spoken or silent; make that change as a default edit",
                    )
                accepted = {*current.accepted_prompt_sha256s, current.prompt_sha256} - {digest}
            revision = current.revision + 1
            self._insert_revision(
                connection,
                prompt,
                revision=revision,
                keeps_recordings=keep_recordings,
                accepted=sorted(accepted),
                created_at=now,
            )
            connection.execute(
                "UPDATE corpus_prompts SET prompt_current_revision = ? WHERE prompt_id = ?",
                (revision, prompt_id),
            )
            return self._current(connection, prompt_id)

    def retire_prompt(self, prompt_id: str) -> PromptRecord:
        """Retire a prompt: new versions omit it; published snapshots keep it.

        Params: ``prompt_id``.
        Returns: the record with ``retired_at`` set.
        Raises: ``CorpusStoreError`` with ``CORPUS.PROMPT_UNKNOWN`` or
        ``CORPUS.PROMPT_RETIRED`` (already retired); store and path failures as
        ``open_corpus_store``.
        """
        now = self._now()
        with self._transaction(write=True) as connection:
            current = self._current(connection, prompt_id)
            if current.retired_at is not None:
                raise CorpusStoreError(
                    "CORPUS.PROMPT_RETIRED", f"prompt is already retired: {prompt_id}"
                )
            connection.execute(
                "UPDATE corpus_prompts SET prompt_retired_at = ? WHERE prompt_id = ?",
                (now, prompt_id),
            )
            return self._current(connection, prompt_id)

    def list_prompts(self, *, include_retired: bool = False) -> tuple[PromptRecord, ...]:
        """Every prompt's current revision in insertion order.

        Params: ``include_retired`` also lists retired prompts.
        Returns: the records, possibly empty.
        Raises: store and path failures as ``open_corpus_store``.
        """
        with self._transaction() as connection:
            condition = "" if include_retired else "WHERE p.prompt_retired_at IS NULL "
            rows = connection.execute(
                _CURRENT_PROMPT_QUERY + condition + "ORDER BY p.prompt_insertion_ordinal"
            ).fetchall()
            return tuple(
                self._record(
                    row, self._accepted(connection, row["prompt_id"], row["prompt_revision"])
                )
                for row in rows
            )

    def prompt_history(self, prompt_id: str) -> tuple[PromptRecord, ...]:
        """Every revision of one prompt, ascending.

        Params: ``prompt_id``.
        Returns: the records, revision 1 first.
        Raises: ``CorpusStoreError`` with ``CORPUS.PROMPT_UNKNOWN``; store and path failures
        as ``open_corpus_store``.
        """
        with self._transaction() as connection:
            rows = connection.execute(
                _LIVE_PROMPT_COLUMNS + "WHERE p.prompt_id = ? ORDER BY r.prompt_revision",
                (prompt_id,),
            ).fetchall()
            if not rows:
                raise CorpusStoreError("CORPUS.PROMPT_UNKNOWN", f"unknown prompt: {prompt_id}")
            return tuple(
                self._record(row, self._accepted(connection, prompt_id, row["prompt_revision"]))
                for row in rows
            )

    # -- catalogs ----------------------------------------------------------------------

    @staticmethod
    def _catalog_row(connection: sqlite3.Connection, catalog_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM corpus_catalogs WHERE catalog_id = ?", (catalog_id,)
        ).fetchone()
        if row is None:
            raise CorpusStoreError("CORPUS.CATALOG_UNKNOWN", f"unknown catalog: {catalog_id}")
        return row

    @staticmethod
    def _reference(catalog: sqlite3.Row, version: sqlite3.Row) -> CatalogReference:
        try:
            return CatalogReference(
                catalog_id=catalog["catalog_id"],
                catalog_version=version["catalog_version"],
                catalog_sha256=version["catalog_sha256"],
                snapshot_sha256=version["catalog_snapshot_sha256"],
                suite_id=catalog["catalog_suite_id"],
                purpose=catalog["catalog_purpose"],
                snapshot_path=version["catalog_snapshot_path"],
            )
        except ValidationError as error:
            raise CorpusStoreError(
                "CORPUS.STORE_UNAVAILABLE",
                f"stored catalog version is invalid: {catalog['catalog_id']}: "
                f"{_validation_message(error)}",
            ) from error

    def _catalog_record(
        self, connection: sqlite3.Connection, catalog: sqlite3.Row
    ) -> CatalogRecord:
        versions = connection.execute(
            "SELECT * FROM corpus_catalog_versions WHERE catalog_id = ? ORDER BY catalog_version",
            (catalog["catalog_id"],),
        ).fetchall()
        try:
            purpose = _PURPOSE.validate_python(catalog["catalog_purpose"])
            retired = catalog["catalog_retired_at"]
            created_at = _timestamp(catalog["catalog_created_at"], "catalog_created_at")
            retired_at = None if retired is None else _timestamp(retired, "catalog_retired_at")
        except (ValueError, ValidationError) as error:
            raise CorpusStoreError(
                "CORPUS.STORE_UNAVAILABLE",
                f"stored catalog is invalid: {catalog['catalog_id']}: {_problem(error)}",
            ) from error
        return CatalogRecord(
            catalog_id=catalog["catalog_id"],
            suite_id=catalog["catalog_suite_id"],
            purpose=purpose,
            description=catalog["catalog_description"],
            created_at=created_at,
            retired_at=retired_at,
            versions=tuple(self._reference(catalog, version) for version in versions),
        )

    def _require_published(
        self, connection: sqlite3.Connection, reference: CatalogReference
    ) -> None:
        """The reference must name a published version exactly as the store recorded it."""
        catalog = self._catalog_row(connection, reference.catalog_id)
        version = connection.execute(
            "SELECT * FROM corpus_catalog_versions WHERE catalog_id = ? AND catalog_version = ?",
            (reference.catalog_id, reference.catalog_version),
        ).fetchone()
        if version is None:
            raise CorpusStoreError(
                "CORPUS.VERSION_UNKNOWN",
                f"catalog {reference.catalog_id} has no version {reference.catalog_version}",
            )
        if self._reference(catalog, version) != reference:
            raise CorpusSnapshotSecurityError(
                "CORPUS.SNAPSHOT_MISMATCH",
                f"reference to catalog {reference.catalog_id} version "
                f"{reference.catalog_version} disagrees with the published version",
            )

    def _verified_snapshot(
        self, connection: sqlite3.Connection, reference: CatalogReference
    ) -> CatalogSnapshot:
        """Read the snapshot of a stored reference and cross-check it against the rows.

        The reference's ``snapshot_sha256`` already pins the file; this additionally requires
        the file's prompt digests and aliases to be exactly what the version's revision rows
        say, so neither half can be forged even together with a matching reference.
        """
        snapshot = self._read_snapshot(reference)
        key = (reference.catalog_id, reference.catalog_version)
        digests = tuple(
            row["prompt_sha256"]
            for row in connection.execute(_VERSION_PROMPT_DIGESTS, key).fetchall()
        )
        if digests != tuple(canonical_sha256(prompt) for prompt in snapshot.suite.prompts):
            raise CorpusSnapshotSecurityError(
                "CORPUS.SNAPSHOT_MISMATCH",
                f"catalog snapshot {reference.snapshot_path} disagrees with the published "
                "prompt revisions",
            )
        aliases = tuple(
            (row["prompt_sha256"], row["accepted_prompt_sha256"])
            for row in connection.execute(_VERSION_ALIASES, key).fetchall()
        )
        if aliases != tuple(
            (alias.prompt_sha256, alias.accepted_prompt_sha256) for alias in snapshot.prompt_aliases
        ):
            raise CorpusSnapshotSecurityError(
                "CORPUS.SNAPSHOT_MISMATCH",
                f"catalog snapshot {reference.snapshot_path} disagrees with the published "
                "prompt aliases",
            )
        return snapshot

    def create_catalog(
        self,
        catalog_id: str,
        *,
        suite_id: str | None = None,
        purpose: str,
        description: str = "",
    ) -> CatalogRecord:
        """Create an empty catalog that later versions materialize as one ``PromptSuite``.

        Params: ``catalog_id``; ``suite_id`` becomes ``PromptSuite.suite_id`` of every
        version (``catalog_id`` when ``None``); ``purpose`` is the suite purpose;
        ``description`` is free text up to ``MAX_CATALOG_DESCRIPTION_CHARS``.
        Returns: the record with no versions.
        Raises: ``CorpusStoreError`` with ``CORPUS.CATALOG_INVALID`` for an id, suite id,
        purpose or description outside the contracts, ``CORPUS.CATALOG_DUPLICATE`` when the
        id exists; store and path failures as ``open_corpus_store``.
        """
        catalog_id = _validated(_STABLE_ID, catalog_id, "CORPUS.CATALOG_INVALID", "catalog id")
        suite_id = (
            catalog_id
            if suite_id is None
            else _validated(_STABLE_ID, suite_id, "CORPUS.CATALOG_INVALID", "suite id")
        )
        catalog_purpose = _validated(_PURPOSE, purpose, "CORPUS.CATALOG_INVALID", "purpose")
        if len(description) > MAX_CATALOG_DESCRIPTION_CHARS:
            raise CorpusStoreError(
                "CORPUS.CATALOG_INVALID",
                f"description exceeds {MAX_CATALOG_DESCRIPTION_CHARS} characters",
            )
        now = self._now()
        with self._transaction(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM corpus_catalogs WHERE catalog_id = ?", (catalog_id,)
            ).fetchone():
                raise CorpusStoreError(
                    "CORPUS.CATALOG_DUPLICATE", f"catalog id already exists: {catalog_id}"
                )
            connection.execute(
                "INSERT INTO corpus_catalogs VALUES (?, ?, ?, ?, NULL, ?)",
                (catalog_id, suite_id, catalog_purpose, now, description),
            )
            return self._catalog_record(connection, self._catalog_row(connection, catalog_id))

    def _publish_prompts(
        self, connection: sqlite3.Connection, prompt_ids: Sequence[str] | None
    ) -> tuple[PromptRecord, ...]:
        if prompt_ids is None:
            return self._live_records(connection)
        ids = tuple(prompt_ids)
        if len(ids) != len(set(ids)):
            raise CorpusStoreError(
                "CORPUS.PROMPT_DUPLICATE", "a prompt id is listed more than once"
            )
        records = []
        for prompt_id in ids:
            record = self._current(connection, prompt_id)
            if record.retired_at is not None:
                raise CorpusStoreError(
                    "CORPUS.PROMPT_RETIRED", f"retired prompts cannot be published: {prompt_id}"
                )
            records.append(record)
        return tuple(records)

    def _live_records(self, connection: sqlite3.Connection) -> tuple[PromptRecord, ...]:
        rows = connection.execute(
            _CURRENT_PROMPT_QUERY
            + "WHERE p.prompt_retired_at IS NULL ORDER BY p.prompt_insertion_ordinal"
        ).fetchall()
        return tuple(
            self._record(row, self._accepted(connection, row["prompt_id"], row["prompt_revision"]))
            for row in rows
        )

    def publish_version(
        self, catalog_id: str, prompt_ids: Sequence[str] | None = None
    ) -> CatalogReference:
        """Materialize, validate, hash and snapshot the next version of a catalog.

        Params: ``catalog_id``; ``prompt_ids`` selects live prompts in the given order, or
        ``None`` for every live prompt in insertion order.
        Returns: the reference to the new version; its ``catalog_sha256`` is the canonical
        digest of a ``prompt-suite/v2`` ``PromptSuite``, its ``snapshot_sha256`` the digest
        of the snapshot file's bytes, and the snapshot at ``snapshot_path`` is written before
        the version row exists.
        Raises: ``CorpusStoreError`` with ``CORPUS.CATALOG_UNKNOWN``, ``CORPUS.PROMPT_UNKNOWN``,
        ``CORPUS.PROMPT_RETIRED``, ``CORPUS.PROMPT_DUPLICATE`` (an id listed twice),
        ``CORPUS.EMPTY_CATALOG`` (nothing to publish), ``CORPUS.CATALOG_INVALID`` (the suite
        fails the ``PromptSuite`` contract, exceeds ``MAX_CATALOG_PROMPTS``, its canonical
        bytes exceed ``MAX_CATALOG_SUITE_BYTES`` (the app receives a catalog in one control
        frame) or its snapshot would exceed ``MAX_CATALOG_SNAPSHOT_BYTES``),
        ``CORPUS.VERSION_UNCHANGED`` (this
        exact content is already published; the message names the catalog and version and,
        when that version accepts other earlier recordings than the current revisions do,
        the prompts concerned);
        ``CorpusSnapshotSecurityError`` when a snapshot file of the same digest exists with
        different bytes; store and path failures as ``open_corpus_store``.
        """
        now = self._now()
        with self._transaction(write=True) as connection:
            catalog = self._catalog_row(connection, catalog_id)
            records = self._publish_prompts(connection, prompt_ids)
            if not records:
                raise CorpusStoreError(
                    "CORPUS.EMPTY_CATALOG", f"catalog {catalog_id} has no prompts to publish"
                )
            if len(records) > MAX_CATALOG_PROMPTS:
                raise CorpusStoreError(
                    "CORPUS.CATALOG_INVALID",
                    f"catalog {catalog_id} exceeds {MAX_CATALOG_PROMPTS} prompts",
                )
            try:
                suite = PromptSuite.model_validate(
                    {
                        "schema_version": PROMPT_SUITE_V2,
                        "suite_id": catalog["catalog_suite_id"],
                        "purpose": catalog["catalog_purpose"],
                        "prompts": tuple(record.prompt for record in records),
                    }
                )
                suite_bytes = canonical_json_bytes(suite)
                digest = hashlib.sha256(suite_bytes).hexdigest()
                aliases = sorted(
                    {
                        (record.prompt_sha256, accepted)
                        for record in records
                        for accepted in record.accepted_prompt_sha256s
                    }
                )
                snapshot = CatalogSnapshot(
                    suite=suite,
                    catalog_sha256=digest,
                    prompt_aliases=tuple(
                        PromptAlias(prompt_sha256=current, accepted_prompt_sha256=accepted)
                        for current, accepted in aliases
                    ),
                )
            except ValidationError as error:
                raise CorpusStoreError(
                    "CORPUS.CATALOG_INVALID",
                    f"catalog {catalog_id} does not materialize a valid suite: "
                    f"{_validation_message(error)}",
                ) from error
            if len(suite_bytes) > MAX_CATALOG_SUITE_BYTES:
                # The app receives a catalog whole, in one service message.
                raise CorpusStoreError(
                    "CORPUS.CATALOG_INVALID",
                    f"catalog {catalog_id} would be {len(suite_bytes)} bytes with "
                    f"{len(records)} prompts, more than the {MAX_CATALOG_SUITE_BYTES} bytes "
                    "the recording app receives in one message; publish fewer prompts",
                )
            payload = canonical_json_bytes(snapshot) + b"\n"
            if len(payload) > MAX_CATALOG_SNAPSHOT_BYTES:
                raise CorpusStoreError(
                    "CORPUS.CATALOG_INVALID",
                    f"catalog {catalog_id} snapshot would exceed {MAX_CATALOG_SNAPSHOT_BYTES} "
                    "bytes",
                )
            existing = connection.execute(
                "SELECT catalog_id, catalog_version FROM corpus_catalog_versions "
                "WHERE catalog_sha256 = ?",
                (digest,),
            ).fetchone()
            if existing is not None:
                message = (
                    f"this exact content is already published as catalog "
                    f"{existing['catalog_id']} version {existing['catalog_version']}"
                )
                published_aliases = {
                    (row[0], row[1])
                    for row in connection.execute(
                        _VERSION_ALIASES, (existing["catalog_id"], existing["catalog_version"])
                    )
                }
                differing = {current for current, _ in set(aliases) ^ published_aliases}
                changed = sorted(
                    record.prompt.prompt_id
                    for record in records
                    if record.prompt_sha256 in differing
                )
                if changed:
                    # A version's identity is its suite digest, so a keep-recordings revert
                    # cannot republish the same content with other accepted recordings.
                    shown = ", ".join(changed[:10]) + (
                        f" and {len(changed) - 10} more" if len(changed) > 10 else ""
                    )
                    message += (
                        f", which accepts other earlier recordings of {shown} than the current "
                        "revisions do; a version is identified by its content, so that "
                        "version's recording rule stays until a version with other content "
                        "is published"
                    )
                raise CorpusStoreError("CORPUS.VERSION_UNCHANGED", message)
            version = connection.execute(
                "SELECT COALESCE(MAX(catalog_version), 0) + 1 FROM corpus_catalog_versions "
                "WHERE catalog_id = ?",
                (catalog_id,),
            ).fetchone()[0]
            self._write_snapshot(f"{digest}.json", payload)
            connection.execute(
                "INSERT INTO corpus_catalog_versions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    catalog_id,
                    version,
                    digest,
                    catalog_snapshot_path(digest),
                    hashlib.sha256(payload).hexdigest(),
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO corpus_catalog_version_prompts VALUES (?, ?, ?, ?, ?)",
                [
                    (catalog_id, version, record.prompt.prompt_id, record.revision, ordinal)
                    for ordinal, record in enumerate(records)
                ],
            )
            return self._reference(
                catalog,
                connection.execute(
                    "SELECT * FROM corpus_catalog_versions "
                    "WHERE catalog_id = ? AND catalog_version = ?",
                    (catalog_id, version),
                ).fetchone(),
            )

    def resolve_reference(self, catalog_id: str, version: int | None = None) -> CatalogReference:
        """Look up a published version by catalog id and version number.

        Params: ``catalog_id``; ``version`` or ``None`` for the latest published version.
        Returns: the stored reference.
        Raises: ``CorpusStoreError`` with ``CORPUS.CATALOG_UNKNOWN`` or
        ``CORPUS.VERSION_UNKNOWN`` (no such version, or nothing published yet); store and
        path failures as ``open_corpus_store``.
        """
        with self._transaction() as connection:
            catalog = self._catalog_row(connection, catalog_id)
            if version is None:
                row = connection.execute(
                    "SELECT * FROM corpus_catalog_versions WHERE catalog_id = ? "
                    "ORDER BY catalog_version DESC LIMIT 1",
                    (catalog_id,),
                ).fetchone()
                if row is None:
                    raise CorpusStoreError(
                        "CORPUS.VERSION_UNKNOWN", f"catalog {catalog_id} has no published version"
                    )
            else:
                row = connection.execute(
                    "SELECT * FROM corpus_catalog_versions "
                    "WHERE catalog_id = ? AND catalog_version = ?",
                    (catalog_id, version),
                ).fetchone()
                if row is None:
                    raise CorpusStoreError(
                        "CORPUS.VERSION_UNKNOWN", f"catalog {catalog_id} has no version {version}"
                    )
            return self._reference(catalog, row)

    def list_catalogs(self) -> tuple[CatalogRecord, ...]:
        """Every catalog with its published versions, in creation order.

        Returns: the records, possibly empty.
        Raises: store and path failures as ``open_corpus_store``.
        """
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM corpus_catalogs ORDER BY catalog_created_at, catalog_id"
            ).fetchall()
            return tuple(self._catalog_record(connection, row) for row in rows)

    def materialize(self, reference: CatalogReference) -> CatalogSnapshot:
        """Read the snapshot of a published version and verify it against the reference.

        Params: ``reference`` from ``publish_version``, ``resolve_reference`` or
        ``active_catalog``.
        Returns: the ``CatalogSnapshot`` (exact suite plus prompt aliases).
        Raises: ``CorpusStoreError`` with ``CORPUS.CATALOG_UNKNOWN`` or
        ``CORPUS.VERSION_UNKNOWN``; ``CorpusSnapshotSecurityError`` with
        ``CORPUS.SNAPSHOT_MISMATCH`` when the reference disagrees with the stored version,
        the file's bytes are not the published ones (suite or aliases), the file is unsafe
        or not canonical, or its prompt digests or aliases disagree with the version's
        revision rows, ``CORPUS.SNAPSHOT_MISSING`` when the file is gone; store and path
        failures as ``open_corpus_store``.
        """
        with self._transaction() as connection:
            self._require_published(connection, reference)
            return self._verified_snapshot(connection, reference)

    # -- subjects ----------------------------------------------------------------------

    def activate(self, subject_id: str, reference: CatalogReference) -> None:
        """Make a published version the one a subject records against.

        Params: ``subject_id``; ``reference`` to a published version whose snapshot must be
        readable now.
        Returns: nothing; ``active_catalog(subject_id)`` returns this version afterwards.
        Raises: ``CorpusStoreError`` with ``CORPUS.SUBJECT_INVALID``, ``CORPUS.CATALOG_UNKNOWN``
        or ``CORPUS.VERSION_UNKNOWN``; ``CorpusSnapshotSecurityError`` as ``materialize``;
        store and path failures as ``open_corpus_store``.
        """
        subject_id = _validated(_STABLE_ID, subject_id, "CORPUS.SUBJECT_INVALID", "subject id")
        with self._transaction(write=True) as connection:
            self._require_published(connection, reference)
            self._verified_snapshot(connection, reference)
            connection.execute(
                "INSERT INTO corpus_subjects VALUES (?, ?, ?) ON CONFLICT(subject_id) DO UPDATE "
                "SET subject_active_catalog_id = excluded.subject_active_catalog_id, "
                "subject_active_catalog_version = excluded.subject_active_catalog_version",
                (subject_id, reference.catalog_id, reference.catalog_version),
            )

    def active_catalog(self, subject_id: str) -> tuple[PromptSuite, CatalogReference]:
        """The suite a subject records against, read from its verified snapshot.

        Params: ``subject_id``.
        Returns: the exact ``PromptSuite`` and the reference to the active version.
        Raises: ``CorpusStoreError`` with ``CORPUS.NO_ACTIVE_CATALOG`` (message names
        ``voice-lab catalog init``); ``CorpusSnapshotSecurityError`` as ``materialize``; store
        and path failures as ``open_corpus_store``.
        """
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT c.*, v.* FROM corpus_subjects s "
                "JOIN corpus_catalog_versions v ON v.catalog_id = s.subject_active_catalog_id "
                "AND v.catalog_version = s.subject_active_catalog_version "
                "JOIN corpus_catalogs c ON c.catalog_id = v.catalog_id "
                "WHERE s.subject_id = ?",
                (subject_id,),
            ).fetchone()
            if row is None:
                raise CorpusStoreError(
                    "CORPUS.NO_ACTIVE_CATALOG",
                    f"subject {subject_id} has no active catalog; run `voice-lab catalog init` "
                    "or `voice-lab catalog activate`",
                )
            reference = self._reference(row, row)
            return self._verified_snapshot(connection, reference).suite, reference

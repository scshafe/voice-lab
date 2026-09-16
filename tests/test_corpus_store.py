"""The corpus store: guarded open, prompt revisions and aliases, published versions, snapshots."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import multiprocessing
import os
import pickle
import sqlite3
import stat
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import cole_voice_lab.corpus_store as corpus_store
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.corpus_store import (
    CORPUS_DATABASE_NAME,
    CORPUS_MIGRATION_NAME,
    STORE_MISSING_MESSAGE,
    UNCHANGED,
    CorpusSnapshotSecurityError,
    CorpusStore,
    CorpusStoreError,
    CorpusStoreSecurityError,
    GroupCatalogCache,
    PromptRecord,
    PromptSourceReference,
    corpus_store_migration,
    corpus_store_migration_resources,
    open_corpus_store,
    read_catalog_snapshot,
    require_referenced_snapshot,
    resolve_group_catalog,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import PROMPT_SUITE_V2, CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import BUILTIN_PROMPT_SUITE_SHA256, load_builtin_prompt_suite
from cole_voice_lab.recording_contracts import (
    CATALOG_SNAPSHOT_ROOT,
    RECORDING_GROUP_V1,
    RECORDING_GROUP_V2,
    CatalogReference,
    CatalogSnapshot,
    PreparedCaptureGate,
    PromptAlias,
    RecordingGroup,
    catalog_snapshot_path,
)
from cole_voice_lab.sqlite_migrations import migration_bytes

SUBJECT = "cole-self"
CATALOG = "cole-corpus"
EPOCH = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
SOURCE_REFERENCE = PromptSourceReference(
    url="https://example.invalid/harvard.html",
    fetched_at=EPOCH,
    sha256="a" * 64,
    license="IEEE Recommended Practice for Speech Quality Measurements",
)


class Clock:
    """Deterministic, strictly increasing UTC timestamps."""

    def __init__(self, start: datetime = EPOCH) -> None:
        self.current = start

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "private-assets")


@pytest.fixture
def store(layout: AssetLayout) -> Iterator[CorpusStore]:
    with open_corpus_store(layout, create=True, clock=Clock()) as opened:
        yield opened


def add(store: CorpusStore, prompt_id: str, text: str, **overrides: Any) -> PromptRecord:
    fields: dict[str, Any] = {
        "text": text,
        "category": "neutral",
        "style": "Natural neutral speech.",
        "expected_seconds": 6.0,
        "source": "manual",
    }
    fields.update(overrides)
    return store.add_prompt(prompt_id, **fields)


def seeded(store: CorpusStore, count: int = 3) -> tuple[PromptRecord, ...]:
    return tuple(
        add(store, f"prompt-{n:02d}", f"Sentence number {n}.") for n in range(1, count + 1)
    )


def published(store: CorpusStore, catalog_id: str = CATALOG) -> CatalogReference:
    store.create_catalog(catalog_id, purpose="capture-corpus", description="the living corpus")
    return store.publish_version(catalog_id)


def raw(store: CorpusStore) -> sqlite3.Connection:
    """An attacker's connection: the same file, none of the store's or schema's guards."""
    connection = sqlite3.connect(store.path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA ignore_check_constraints = ON")
    return connection


def snapshot_file(store: CorpusStore, reference: CatalogReference) -> Path:
    return store.layout.root / reference.snapshot_path


def file_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# -- errors and migration resources ---------------------------------------------------


def test_error_carries_its_code_and_survives_pickling() -> None:
    error = CorpusStoreError("CORPUS.PROMPT_UNKNOWN", "probe")
    assert isinstance(error, ContractError)
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is CorpusStoreError
    assert (restored.code, str(restored)) == ("CORPUS.PROMPT_UNKNOWN", "probe")
    security = pickle.loads(
        pickle.dumps(CorpusSnapshotSecurityError("CORPUS.SNAPSHOT_MISSING", "gone"))
    )
    assert isinstance(security, SecurityError)
    assert isinstance(security, CorpusStoreError)
    assert security.code == "CORPUS.SNAPSHOT_MISSING"
    assert isinstance(CorpusStoreSecurityError("CORPUS.STORE_UNSAFE", "x"), SecurityError)


def test_migration_resource_is_fingerprinted_and_reversible_on_an_empty_database() -> None:
    (name, digest, length), *rest = corpus_store_migration_resources()
    assert rest == []
    assert name == f"migrations/{CORPUS_MIGRATION_NAME}"
    assert len(digest) == 64 and length > 0
    header = migration_bytes(CORPUS_MIGRATION_NAME).decode().partition("-- +migrate Up\n")[0]
    assert "-- IRREVERSIBLE:" in header
    assert "No ON DELETE cascades" in header
    up = corpus_store_migration("up")
    assert up.count("ON DELETE RESTRICT") == up.count("REFERENCES") == 7
    assert "CASCADE" not in up
    assert up.count("DEFERRABLE INITIALLY DEFERRED") == 1
    assert f"'{CATALOG_SNAPSHOT_ROOT}/' || catalog_sha256 || '.json'" in up
    assert up.count("-- ") > up.count("CREATE TABLE") + up.count("CREATE INDEX")
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(corpus_store_migration("up"))
    connection.executescript(corpus_store_migration("up"))
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
    }
    assert tables == {
        "corpus_prompts",
        "corpus_prompt_revisions",
        "corpus_prompt_aliases",
        "corpus_catalogs",
        "corpus_catalog_versions",
        "corpus_catalog_version_prompts",
        "corpus_subjects",
    }
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO corpus_prompt_aliases VALUES ('absent', 1, ?)", ("0" * 64,))
    connection.execute(
        "INSERT INTO corpus_catalogs VALUES ('c', 'c', 'capture-corpus', 't', NULL, '')"
    )
    for path, snapshot_sha256 in (
        ("elsewhere.json", "b" * 64),
        (catalog_snapshot_path("a" * 64), "B" * 64),
        (catalog_snapshot_path("a" * 64), "g" * 64),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO corpus_catalog_versions VALUES ('c', 1, ?, ?, ?, 't')",
                ("a" * 64, path, snapshot_sha256),
            )
    connection.execute(
        "INSERT INTO corpus_catalog_versions VALUES ('c', 1, ?, ?, ?, 't')",
        ("a" * 64, catalog_snapshot_path("a" * 64), "b" * 64),
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute("DELETE FROM corpus_catalogs")
    # A prompt row and its current revision reference each other: the deferred key lets the
    # store write the prompt row first, and RESTRICT refuses deleting either at once.
    connection.execute("BEGIN")
    connection.execute("INSERT INTO corpus_prompts VALUES ('p', 0, 't', NULL, 'manual', NULL, 1)")
    connection.execute(
        "INSERT INTO corpus_prompt_revisions "
        "VALUES ('p', 1, ?, 'Text.', 'neutral', 'Plain.', NULL, '[]', 1.0, 0, 0, 0, 't')",
        ("c" * 64,),
    )
    connection.execute("COMMIT")
    for table in ("corpus_prompt_revisions", "corpus_prompts"):
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(f"DELETE FROM {table}")
    # As the header says, a Down on a populated store needs foreign keys off.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.executescript(corpus_store_migration("down"))
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript(corpus_store_migration("down"))
    connection.executescript(corpus_store_migration("down"))
    assert connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0] == 0
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_dangling_current_revision_is_refused_by_the_schema_and_the_audit(
    layout: AssetLayout,
) -> None:
    with open_corpus_store(layout, create=True) as store:
        seeded(store, 1)
        path = store.path
    guarded = sqlite3.connect(path, isolation_level=None)
    guarded.execute("PRAGMA foreign_keys = ON")
    guarded.execute("BEGIN")
    guarded.execute("UPDATE corpus_prompts SET prompt_current_revision = 9")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        guarded.execute("COMMIT")
    guarded.execute("ROLLBACK")
    guarded.close()
    with open_corpus_store(layout, create=False) as store:
        assert [record.revision for record in store.list_prompts()] == [1]
        with raw(store) as attacker:
            attacker.execute("UPDATE corpus_prompts SET prompt_current_revision = 9")
    with pytest.raises(CorpusStoreError, match="references are inconsistent") as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


# -- opening the store --------------------------------------------------------------


def test_opening_without_create_needs_an_existing_database(layout: AssetLayout) -> None:
    with pytest.raises(CorpusStoreError) as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_MISSING"
    assert str(caught.value) == STORE_MISSING_MESSAGE
    assert "voice-lab catalog init" in STORE_MISSING_MESSAGE
    corpus = layout.root / "provenance" / "corpus"
    assert not corpus.exists()
    corpus.mkdir(mode=0o700)
    with pytest.raises(CorpusStoreError) as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_MISSING"
    assert sorted(path.name for path in corpus.iterdir()) == []


def test_create_makes_private_members_and_a_second_open_reuses_them(layout: AssetLayout) -> None:
    with open_corpus_store(layout, create=True) as store:
        seeded(store, 1)
        assert store.directory == layout.root / "provenance" / "corpus"
        assert store.path == store.directory / CORPUS_DATABASE_NAME
        assert mode(store.directory) == 0o700
        assert mode(store.snapshot_directory) == 0o700
        for name in (CORPUS_DATABASE_NAME, ".corpus.lease", ".corpus.lock"):
            assert mode(store.directory / name) == 0o600, name
        assert mode(store.path.with_name(CORPUS_DATABASE_NAME + "-wal")) == 0o600
    with open_corpus_store(layout, create=True) as again:
        assert [record.prompt.prompt_id for record in again.list_prompts()] == ["prompt-01"]
    with open_corpus_store(layout, create=False) as third:
        assert [record.prompt.prompt_id for record in third.list_prompts()] == ["prompt-01"]


def test_database_vanishing_after_the_presence_check_is_still_missing(
    layout: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    (layout.root / "provenance" / "corpus").mkdir(mode=0o700)
    monkeypatch.setattr(CorpusStore, "_require_database_present", lambda self: None)
    with pytest.raises(CorpusStoreError) as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_MISSING"
    assert not (layout.root / "provenance" / "corpus" / CORPUS_DATABASE_NAME).exists()


def test_database_replaced_between_binding_stats_is_refused(
    layout: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_corpus_store(layout, create=True):
        pass
    directory = layout.root / "provenance" / "corpus"
    real_stat = os.stat
    database_stats: list[str] = []

    def swapping_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if path == CORPUS_DATABASE_NAME and "dir_fd" in kwargs:
            database_stats.append(path)
            if len(database_stats) == 2:
                replacement = directory / "replacement"
                replacement.write_bytes((directory / CORPUS_DATABASE_NAME).read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, directory / CORPUS_DATABASE_NAME)
        return result

    monkeypatch.setattr(os, "stat", swapping_stat)
    with pytest.raises(CorpusStoreSecurityError, match="database was replaced") as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNSAFE"


def test_unsupported_schema_version_and_unversioned_content_fail_closed(
    layout: AssetLayout,
) -> None:
    with open_corpus_store(layout, create=True) as store:
        path = store.path
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute("PRAGMA user_version = 7")
    with pytest.raises(
        CorpusStoreError, match="unsupported corpus store schema version: 7"
    ) as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute("PRAGMA user_version = 0")
    with pytest.raises(CorpusStoreError, match="unversioned nonempty database") as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_corrupt_database_bytes_are_reported_never_repaired(layout: AssetLayout) -> None:
    with open_corpus_store(layout, create=True) as store:
        path = store.path
    path.write_bytes(b"not a database at all")
    with pytest.raises(CorpusStoreError, match="cannot open corpus store") as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    assert path.read_bytes() == b"not a database at all"


def test_structural_integrity_failure_is_a_named_error(layout: AssetLayout) -> None:
    with open_corpus_store(layout, create=True) as store:
        seeded(store, 3)
        path = store.path
    # Header bytes 36-39 count freelist pages; claiming one where none exists is a
    # structural inconsistency quick_check reports instead of raising.
    data = bytearray(path.read_bytes())
    assert not path.with_name(path.name + "-wal").exists()
    data[36:40] = (1).to_bytes(4, "big")
    path.write_bytes(bytes(data))
    with pytest.raises(CorpusStoreError, match="structural integrity") as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_inconsistent_references_are_a_named_error(layout: AssetLayout) -> None:
    with open_corpus_store(layout, create=True) as store:
        seeded(store, 1)
        with raw(store) as attacker:
            attacker.execute(
                "INSERT INTO corpus_prompt_aliases VALUES ('prompt-01', 9, ?)", ("0" * 64,)
            )
    with pytest.raises(CorpusStoreError, match="references are inconsistent") as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_unexpected_failure_during_open_releases_the_lease(
    layout: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_corpus_store(layout, create=True):
        pass

    def refuse(*_: Any, **__: Any) -> None:
        raise RuntimeError("probe")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    with pytest.raises(RuntimeError, match="probe"):
        open_corpus_store(layout, create=False)
    descriptor = os.open(layout.root / "provenance" / "corpus" / ".corpus.lease", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_unsafe_directory_mode_fails_closed_without_repair(layout: AssetLayout) -> None:
    with open_corpus_store(layout, create=True) as store:
        directory = store.directory
    directory.chmod(0o755)
    with pytest.raises(CorpusStoreSecurityError, match="owned private directory") as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNSAFE"
    assert mode(directory) == 0o755


@pytest.mark.parametrize("name", ["corpus", ".corpus.lock", ".corpus.lease", "snapshots"])
def test_symlinked_store_member_is_a_security_error_not_an_outage(
    layout: AssetLayout, tmp_path: Path, name: str
) -> None:
    with open_corpus_store(layout, create=True):
        pass
    directory = layout.root / "provenance" / "corpus"
    member = directory if name == "corpus" else directory / name
    real = tmp_path / f"{name}-real"
    os.rename(member, real)
    member.symlink_to(real)
    for create in (False, True):
        with pytest.raises(CorpusStoreSecurityError, match="owned private") as caught:
            open_corpus_store(layout, create=create)
        assert caught.value.code == "CORPUS.STORE_UNSAFE"
        assert isinstance(caught.value, SecurityError)
    assert member.is_symlink()


def test_open_discards_snapshot_scratch_a_crashed_publish_left(layout: AssetLayout) -> None:
    with open_corpus_store(layout, create=True, clock=Clock()) as store:
        seeded(store, 1)
        reference = published(store)
        path = snapshot_file(store, reference)
        original = path.read_bytes()
        snapshots = store.snapshot_directory
    # Crash between link() and unlink(): the snapshot has two names.
    second_name = snapshots / f".{path.name}.tmp-{'0' * 16}"
    os.link(path, second_name)
    assert path.stat().st_nlink == 2
    # Crash before link(): a partial write under a temporary name.
    partial = snapshots / f".{'e' * 64}.json.tmp-{'1' * 16}"
    partial.write_bytes(b"partial")
    partial.chmod(0o600)
    with open_corpus_store(layout, create=False) as again:
        assert again.materialize(reference).catalog_sha256 == reference.catalog_sha256
    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1
    assert os.listdir(snapshots) == [path.name]
    # Only regular files are scratch; anything else under a scratch name is a tamper.
    bogus = snapshots / f".{'f' * 64}.json.tmp-{'2' * 16}"
    bogus.mkdir(mode=0o700)
    with pytest.raises(
        CorpusStoreSecurityError, match="unexpected catalog snapshot member"
    ) as caught:
        open_corpus_store(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNSAFE"
    assert bogus.is_dir()


def test_closed_store_refuses_queries_and_close_is_idempotent(layout: AssetLayout) -> None:
    store = open_corpus_store(layout, create=True)
    store.close()
    store.close()
    with pytest.raises(CorpusStoreError, match="closed") as caught:
        store.list_prompts()
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_naive_clock_is_a_caller_error_at_open_and_at_every_write(layout: AssetLayout) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        open_corpus_store(layout, create=True, clock=lambda: datetime(2026, 1, 1))
    assert not (layout.root / "provenance" / "corpus").exists()
    readings = iter((EPOCH, datetime(2026, 1, 1)))
    with open_corpus_store(layout, create=True, clock=lambda: next(readings)) as store:
        with pytest.raises(ValueError, match="timezone-aware"):
            add(store, "prompt-01", "Sentence.")
        assert store.list_prompts() == ()


# -- path guards between transactions ------------------------------------------------


def test_replaced_directory_is_detected_before_the_next_query(store: CorpusStore) -> None:
    moved = store.snapshot_directory.with_name("snapshots-moved")
    os.rename(store.snapshot_directory, moved)
    store.snapshot_directory.mkdir(mode=0o700)
    with pytest.raises(CorpusStoreSecurityError, match="directory was replaced") as caught:
        store.list_prompts()
    assert caught.value.code == "CORPUS.STORE_UNSAFE"


@pytest.mark.parametrize("name", [".corpus.lock", CORPUS_DATABASE_NAME])
def test_replaced_member_is_detected_before_the_next_query(store: CorpusStore, name: str) -> None:
    original = store.directory / name
    replacement = store.directory / "replacement"
    replacement.write_bytes(original.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, original)
    with pytest.raises(CorpusStoreSecurityError, match=f"member was replaced: {name}") as caught:
        store.list_prompts()
    assert caught.value.code == "CORPUS.STORE_UNSAFE"


def test_missing_and_stray_members_are_detected_before_the_next_query(
    store: CorpusStore,
) -> None:
    stray = store.directory / "notes.txt"
    stray.write_bytes(b"")
    with pytest.raises(CorpusStoreSecurityError, match="unexpected corpus store member") as caught:
        store.list_prompts()
    assert caught.value.code == "CORPUS.STORE_UNSAFE"
    stray.unlink()
    stray = store.snapshot_directory / "notes.txt"
    stray.write_bytes(b"")
    with pytest.raises(CorpusStoreSecurityError, match="unexpected catalog snapshot member"):
        store.list_prompts()
    stray.unlink()
    (store.directory / ".corpus.lease").unlink()
    with pytest.raises(CorpusStoreSecurityError, match="member disappeared: .corpus.lease"):
        store.list_prompts()


def test_snapshot_with_open_permissions_is_refused_before_it_is_read(store: CorpusStore) -> None:
    seeded(store)
    reference = published(store)
    snapshot_file(store, reference).chmod(0o644)
    with pytest.raises(CorpusStoreSecurityError, match="owned private regular file") as caught:
        store.materialize(reference)
    assert caught.value.code == "CORPUS.STORE_UNSAFE"


def test_dropped_table_is_a_named_query_failure(store: CorpusStore) -> None:
    with raw(store) as attacker:
        attacker.execute("DROP TABLE corpus_subjects")
    with pytest.raises(CorpusStoreError, match="query failed") as caught:
        store.active_catalog(SUBJECT)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_transaction_guard_skips_rollback_when_nothing_is_open(store: CorpusStore) -> None:
    with pytest.raises(RuntimeError, match="after commit"):
        with store._transaction() as connection:
            connection.execute("COMMIT")
            raise RuntimeError("after commit")
    assert store.list_prompts() == ()


def _read_snapshot_in_child(root: str, reference_json: str, results: Any) -> None:
    reference = CatalogReference.model_validate_json(reference_json)
    try:
        read_catalog_snapshot(AssetLayout.validate(Path(root)), reference)
    except CorpusStoreError as error:
        results.put((type(error).__name__, error.code, str(error)))
    else:
        results.put(("returned", None, ""))


def test_planted_fifo_at_the_snapshot_path_cannot_block_any_reader(store: CorpusStore) -> None:
    seeded(store, 1)
    reference = published(store)
    path = snapshot_file(store, reference)
    path.unlink()
    os.mkfifo(path, 0o600)
    # A read-only open of a FIFO waits for a writer forever, so the file-only reader runs in
    # a child process: a regression fails on the timeout instead of hanging the suite.
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    child = context.Process(
        target=_read_snapshot_in_child,
        args=(str(store.layout.root), reference.model_dump_json(), results),
        daemon=True,
    )
    child.start()
    child.join(timeout=60)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("read_catalog_snapshot blocked on a FIFO planted at the snapshot path")
    assert child.exitcode == 0
    assert results.get(timeout=10) == (
        "CorpusSnapshotSecurityError",
        "CORPUS.SNAPSHOT_MISMATCH",
        f"catalog snapshot is not a regular file: {reference.snapshot_path}",
    )
    assert stat.S_ISFIFO(path.lstat().st_mode)
    # The store stats every snapshot member before any open (and opens non-blocking).
    with pytest.raises(CorpusStoreSecurityError, match="owned private regular file") as caught:
        store.materialize(reference)
    assert caught.value.code == "CORPUS.STORE_UNSAFE"


# -- prompts -------------------------------------------------------------------------


def test_added_prompt_is_revision_one_with_the_contract_digest(store: CorpusStore) -> None:
    record = add(
        store,
        "warm-01",
        "We can take this one step at a time.",
        category="warm-narration",
        labels=("gentle",),
        reference_candidate=True,
    )
    assert record.revision == 1
    assert record.keeps_recordings is False
    assert record.accepted_prompt_sha256s == ()
    assert record.prompt_sha256 == canonical_sha256(record.prompt)
    assert record.prompt.labels == ("gentle",)
    assert record.source == "manual"
    assert record.source_reference is None
    assert record.retired_at is None
    # The clock's first reading is the open-time awareness check.
    assert record.created_at == record.revision_created_at == EPOCH + timedelta(seconds=2)
    assert store.list_prompts() == (record,)
    assert store.prompt_history("warm-01") == (record,)
    with raw(store) as reader:
        ordinals = reader.execute("SELECT prompt_insertion_ordinal FROM corpus_prompts").fetchall()
    assert [row[0] for row in ordinals] == [0]


def test_silent_prompt_needs_no_text_and_source_reference_is_canonicalized(
    store: CorpusStore,
) -> None:
    record = store.add_prompt(
        "room-tone-start",
        text=None,
        category="room-tone",
        style="Silence; keep the recording position unchanged.",
        expected_seconds=15.0,
        instructions="Remain still and silent while the room tone is recorded.",
        source="harvard",
        source_reference=SOURCE_REFERENCE.model_dump_json(indent=2),
    )
    assert record.prompt.text is None
    assert record.source == "harvard"
    assert record.source_reference == canonical_json_bytes(SOURCE_REFERENCE).decode()
    add(store, "spoken", "A repeated sentence.")
    store.add_prompt(
        "room-tone-end",
        text=None,
        category="room-tone",
        style="Silence.",
        expected_seconds=15.0,
        instructions="Remain still.",
        source="manual",
    )
    assert [record.prompt.prompt_id for record in store.list_prompts()] == [
        "room-tone-start",
        "spoken",
        "room-tone-end",
    ]


def test_a_source_reference_records_an_accepted_unpinned_source_only_when_there_is_one(
    store: CorpusStore,
) -> None:
    pinned = canonical_json_bytes(SOURCE_REFERENCE).decode()
    # References written before the field existed are the canonical form of a pinned source.
    assert "unpinned_source_accepted" not in json.loads(pinned)
    assert PromptSourceReference.model_validate_json(pinned).unpinned_source_accepted is False
    unpinned = SOURCE_REFERENCE.model_copy(update={"unpinned_source_accepted": True})
    unpinned_json = unpinned.model_dump_json()
    assert json.loads(canonical_json_bytes(unpinned))["unpinned_source_accepted"] is True
    explicit_false = SOURCE_REFERENCE.model_dump(mode="json") | {"unpinned_source_accepted": False}
    for index, (reference, stored) in enumerate(
        (
            (json.dumps(explicit_false), pinned),
            (unpinned_json, canonical_json_bytes(unpinned).decode()),
        )
    ):
        record = add(
            store,
            f"harvard-01-0{index + 1}",
            f"Sentence number {index + 1}.",
            source="harvard",
            source_reference=reference,
        )
        assert record.source_reference == stored
    with pytest.raises(CorpusStoreError, match="unpinned_source_accepted") as caught:
        add(
            store,
            "harvard-01-03",
            "Sentence number 3.",
            source="harvard",
            source_reference=json.dumps(explicit_false | {"unpinned_source_accepted": "yes"}),
        )
    assert caught.value.code == "CORPUS.SOURCE_REFERENCE_INVALID"


@pytest.mark.parametrize(
    "fields, code, message",
    [
        ({"category": "sonnet"}, "CORPUS.PROMPT_INVALID", "category"),
        ({"text": None}, "CORPUS.PROMPT_INVALID", "only room tone and procedure prompts"),
        ({"source": "wikipedia"}, "CORPUS.SOURCE_INVALID", "prompt source"),
        ({"source": "arctic"}, "CORPUS.SOURCE_REFERENCE_INVALID", "requires a source reference"),
        (
            {"source": "import", "source_reference": '{"url": "x"}'},
            "CORPUS.SOURCE_REFERENCE_INVALID",
            "fetched_at",
        ),
    ],
    ids=["bad-category", "spoken-without-text", "bad-source", "no-reference", "bad-reference"],
)
def test_invalid_prompt_fields_are_refused_before_any_write(
    store: CorpusStore, fields: dict[str, Any], code: str, message: str
) -> None:
    request: dict[str, Any] = {
        "text": "Sentence.",
        "category": "neutral",
        "style": "Natural neutral speech.",
        "expected_seconds": 6.0,
        "source": "manual",
        **fields,
    }
    with pytest.raises(CorpusStoreError, match=message) as caught:
        store.add_prompt("prompt-01", **request)
    assert caught.value.code == code
    assert store.list_prompts() == ()


@pytest.mark.parametrize(
    "fields",
    [
        {"text": "Hello\x07world."},
        {"text": "Hello\u200dworld."},
        {"style": "Neutral\x1b[31m"},
        {"text": "Hello\x00world."},
        {"text": None, "category": "room-tone", "instructions": "Stay\x07still."},
    ],
    ids=["bell", "zero-width-joiner", "escape-in-style", "nul", "bell-in-instructions"],
)
def test_format_and_control_characters_are_refused_when_a_prompt_is_added(
    store: CorpusStore, fields: dict[str, Any]
) -> None:
    with pytest.raises(CorpusStoreError, match="format or control characters") as caught:
        add(store, "prompt-01", **{"text": "Sentence.", **fields})
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert store.list_prompts() == ()


def test_edit_and_import_apply_the_same_text_hygiene(store: CorpusStore) -> None:
    add(store, "prompt-01", "Sentence one.")
    with pytest.raises(CorpusStoreError, match="format or control characters") as caught:
        store.edit_prompt("prompt-01", text="Sentence\u200bone.")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert [record.revision for record in store.prompt_history("prompt-01")] == [1]
    # prompt-suite/v1 exactness has no hygiene rule, so an operator-supplied v1 file can
    # carry a control character; the store refuses it before anything is written.
    legacy = PromptSuite(
        suite_id="legacy",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="clean-01",
                text="A clean sentence.",
                category="neutral",
                style="Plain.",
                expected_seconds=3.0,
            ),
            Prompt(
                prompt_id="bell-01",
                text="Hello\x07world.",
                category="neutral",
                style="Plain.",
                expected_seconds=3.0,
            ),
        ),
    )
    with pytest.raises(CorpusStoreError, match="prompt bell-01 text cannot") as caught:
        store.import_prompt_suite(legacy, source="import")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert [record.prompt.prompt_id for record in store.list_prompts()] == ["prompt-01"]


def test_duplicate_prompt_id_or_live_text_is_refused(store: CorpusStore) -> None:
    add(store, "prompt-01", "Sentence one.")
    with pytest.raises(CorpusStoreError, match="prompt id already exists") as caught:
        add(store, "prompt-01", "Different sentence.")
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    with pytest.raises(
        CorpusStoreError, match="repeats the text of live prompt prompt-01"
    ) as caught:
        add(store, "prompt-02", "Sentence one.")
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    store.retire_prompt("prompt-01")
    add(store, "prompt-02", "Sentence one.")
    assert [record.prompt.prompt_id for record in store.list_prompts()] == ["prompt-02"]


def test_spoken_words_ignore_case_punctuation_and_spacing() -> None:
    assert corpus_store.spoken_words("  It's   the BIRCH canoe!  ") == (
        "its",
        "the",
        "birch",
        "canoe",
    )
    assert corpus_store.spoken_words("Café, NAÏVE déjà vu.") == ("café", "naïve", "déjà", "vu")
    assert corpus_store.spoken_words("snake_case & 42") == ("snakecase", "42")
    assert corpus_store.spoken_words("...") == ()


def test_a_prompt_that_reads_like_another_live_prompt_is_refused_everywhere(
    store: CorpusStore,
) -> None:
    """Two live prompts that read the same would count one recording twice."""
    add(store, "prompt-01", "The birch canoe slid on the smooth planks.")
    add(store, "prompt-02", "Sentence two.")
    add(store, "dots-01", "...")
    same = "prompt echo-01 reads the same as live prompt prompt-01"
    with pytest.raises(CorpusStoreError, match=same) as caught:
        add(store, "echo-01", "the Birch canoe slid on the smooth  planks!")
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    assert (
        '(ignoring case, punctuation and spacing): "the Birch canoe slid on the smooth  planks!" '
        'and "The birch canoe slid on the smooth planks."'
    ) in str(caught.value)
    # Text without words reads the same only when it is the same text.
    add(store, "dots-02", "!!!")
    with pytest.raises(CorpusStoreError, match="repeats the text of live prompt dots-01"):
        add(store, "dots-03", "...")
    with pytest.raises(CorpusStoreError, match="prompt-02 reads the same as live prompt prompt-01"):
        store.edit_prompt("prompt-02", text="The birch canoe slid on the smooth planks?")
    # A prompt may keep its own reading, e.g. in a keep-recordings punctuation fix.
    kept = store.edit_prompt(
        "prompt-01", text="The birch canoe slid on the smooth planks!", keep_recordings=True
    )
    assert kept.revision == 2
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="operator-suite",
        purpose="capture-corpus",
        prompts=tuple(
            Prompt(
                prompt_id=prompt_id,
                text=text,
                category="neutral",
                style="Plain.",
                expected_seconds=3.0,
            )
            for prompt_id, text in (
                ("fresh-01", "A fresh sentence."),
                ("fresh-02", "a FRESH sentence"),
            )
        ),
    )
    with pytest.raises(CorpusStoreError, match="fresh-02 reads the same as live prompt fresh-01"):
        store.import_prompt_suite(suite, source="import")
    assert [r.prompt.prompt_id for r in store.list_prompts()] == [
        "prompt-01",
        "prompt-02",
        "dots-01",
        "dots-02",
    ]
    # Retiring a prompt frees its reading.
    store.retire_prompt("prompt-01")
    assert add(store, "echo-01", "the Birch canoe slid on the smooth planks").revision == 1


def test_unknown_prompt_is_named_everywhere(store: CorpusStore) -> None:
    for call in (
        lambda: store.edit_prompt("absent", text="x"),
        lambda: store.retire_prompt("absent"),
        lambda: store.prompt_history("absent"),
    ):
        with pytest.raises(CorpusStoreError, match="unknown prompt: absent") as caught:
            call()
        assert caught.value.code == "CORPUS.PROMPT_UNKNOWN"


def test_default_edit_creates_a_revision_that_accepts_nothing(store: CorpusStore) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    second = store.edit_prompt("prompt-01", text="Sentence one, revised.")
    assert second.revision == 2
    assert second.prompt.text == "Sentence one, revised."
    assert second.prompt_sha256 != first.prompt_sha256
    assert second.keeps_recordings is False
    assert second.accepted_prompt_sha256s == ()
    assert second.created_at == first.created_at
    assert second.revision_created_at > first.revision_created_at
    assert store.list_prompts() == (second,)
    assert store.prompt_history("prompt-01") == (first, second)


def test_every_field_can_change_and_a_no_op_edit_is_refused(store: CorpusStore) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    edited = store.edit_prompt(
        "prompt-01",
        text=None,
        category="room-tone",
        style="Silence.",
        instructions="Stay silent.",
        labels=["quiet"],
        expected_seconds=15.0,
        reference_candidate=True,
        holdout=True,
    )
    assert edited.prompt.model_dump() == {
        "prompt_id": "prompt-01",
        "category": "room-tone",
        "style": "Silence.",
        "instructions": "Stay silent.",
        "labels": ("quiet",),
        "expected_seconds": 15.0,
        "reference_candidate": True,
        "holdout": True,
    }
    with pytest.raises(CorpusStoreError, match="leaves prompt prompt-01 unchanged") as caught:
        store.edit_prompt("prompt-01", style="Silence.")
    assert caught.value.code == "CORPUS.PROMPT_UNCHANGED"
    with pytest.raises(CorpusStoreError, match="prompt is invalid") as caught:
        store.edit_prompt("prompt-01", category="neutral")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert store.prompt_history("prompt-01") == (first, edited)
    assert UNCHANGED is corpus_store.UNCHANGED


def test_edit_cannot_take_another_live_prompts_text(store: CorpusStore) -> None:
    add(store, "prompt-01", "Sentence one.")
    add(store, "prompt-02", "Sentence two.")
    with pytest.raises(
        CorpusStoreError, match="repeats the text of live prompt prompt-01"
    ) as caught:
        store.edit_prompt("prompt-02", text="Sentence one.")
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    assert store.list_prompts()[1].revision == 1


def test_a_dry_run_edit_runs_every_check_and_stores_nothing(store: CorpusStore) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    add(store, "prompt-02", "Sentence two.")
    preview = store.edit_prompt(
        "prompt-01",
        text="Sentence one, revised.",
        keep_recordings=True,
        allow_rewording=True,
        dry_run=True,
    )
    assert (preview.revision, preview.prompt.text) == (2, "Sentence one, revised.")
    assert preview.keeps_recordings is True
    assert preview.accepted_prompt_sha256s == (first.prompt_sha256,)
    assert store.prompt_history("prompt-01") == (first,)
    assert store.list_prompts()[0] == first
    for changes, code in (
        ({"text": "Sentence two."}, "CORPUS.PROMPT_DUPLICATE"),
        ({"text": "Sentence one."}, "CORPUS.PROMPT_UNCHANGED"),
        ({"text": None}, "CORPUS.PROMPT_INVALID"),
    ):
        with pytest.raises(CorpusStoreError) as caught:
            store.edit_prompt("prompt-01", dry_run=True, **changes)
        assert caught.value.code == code
    store.retire_prompt("prompt-02")
    with pytest.raises(CorpusStoreError) as retired:
        store.edit_prompt("prompt-02", style="Brisk.", dry_run=True)
    assert retired.value.code == "CORPUS.PROMPT_RETIRED"
    written = store.edit_prompt(
        "prompt-01", text="Sentence one, revised.", keep_recordings=True, allow_rewording=True
    )
    assert written.prompt == preview.prompt and written.revision == preview.revision
    assert written.accepted_prompt_sha256s == preview.accepted_prompt_sha256s
    assert store.prompt_history("prompt-01") == (first, written)


def test_an_edit_is_refused_once_the_prompt_moved_past_the_revision_the_caller_saw(
    store: CorpusStore,
) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    # Another editor lands a revision between the caller's look and its write.
    other = store.edit_prompt("prompt-01", style="Brisk.", expected_revision=1)
    assert other.revision == 2
    for dry_run in (True, False):
        with pytest.raises(CorpusStoreError, match="at revision 2, not the expected") as caught:
            store.edit_prompt(
                "prompt-01",
                text="Sentence one!",
                keep_recordings=True,
                expected_revision=1,
                dry_run=dry_run,
            )
        assert caught.value.code == "CORPUS.PROMPT_CHANGED"
    assert store.prompt_history("prompt-01") == (first, other)
    kept = store.edit_prompt(
        "prompt-01", text="Sentence one!", keep_recordings=True, expected_revision=2
    )
    assert kept.accepted_prompt_sha256s == (other.prompt_sha256,)


def test_a_prompt_no_prompt_suite_v2_catalog_can_hold_is_never_stored(store: CorpusStore) -> None:
    """Every stored prompt can publish somewhere: the store publishes prompt-suite/v2 only."""
    with pytest.raises(CorpusStoreError, match="fits no prompt-suite/v2 catalog") as caught:
        add(store, "tone-01", "Silence please.", category="room-tone")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert "room tone requires instructions and no text" in str(caught.value)
    assert "Value error" not in str(caught.value)
    add(store, "step-01", "Press Record.", category="neutral")
    with pytest.raises(CorpusStoreError, match="fits no prompt-suite/v2 catalog") as caught:
        store.edit_prompt("step-01", text=None, category="procedure", instructions="Press it.")
    assert "physical-test: physical-test prompts require the test label" in str(caught.value)
    assert len(store.prompt_history("step-01")) == 1
    procedure = store.edit_prompt(
        "step-01", text=None, category="procedure", instructions="Press it.", labels=("test",)
    )
    assert procedure.prompt.category == "procedure"
    # A prompt-suite/v1 room tone carries its text; the store takes all of a suite or nothing.
    pilot = load_builtin_prompt_suite("pilot-v1.json")
    with pytest.raises(CorpusStoreError, match="prompt room-tone-start fits no") as caught:
        store.import_prompt_suite(pilot, source="pilot-v1")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert [record.prompt.prompt_id for record in store.list_prompts()] == ["step-01"]


def test_prompt_catalog_fit_names_each_purpose_that_refuses_a_prompt() -> None:
    spoken = Prompt(
        prompt_id="held-01",
        text="A held out sentence.",
        category="neutral",
        style="Plain.",
        expected_seconds=2.0,
        holdout=True,
    )
    fit = corpus_store.prompt_catalog_fit(spoken)
    assert list(fit.suites) == ["held-out-evaluation"] and fit.holds("held-out-evaluation")
    assert not fit.holds("capture-pilot") and not fit.holds("karaoke")
    assert fit.suites["held-out-evaluation"].prompts == (spoken,)
    assert fit.explanation() == (
        "capture-pilot, capture-corpus, physical-test: prompt holdout flags must agree with "
        "suite purpose"
    )
    tested = spoken.model_copy(update={"holdout": False, "labels": ("test",)})
    only = corpus_store.prompt_catalog_fit(tested, ("capture-pilot", "physical-test", "karaoke"))
    assert list(only.suites) == ["physical-test"]
    assert only.suites["physical-test"].suite_id.startswith("cole-physical-test-")
    assert only.problems == {
        "capture-pilot": "only physical-test catalogs may use the test label or procedures"
    }


def test_publish_names_the_live_prompts_its_catalog_purpose_refuses(store: CorpusStore) -> None:
    seeded(store, 1)
    for number in range(12):
        add(store, f"held-{number:02d}", f"Held sentence {number}.", holdout=True)
    with pytest.raises(CorpusStoreError, match="cannot hold the live prompts held-00") as caught:
        store.create_catalog(CATALOG, purpose="capture-pilot")
    assert caught.value.code == "CORPUS.CATALOG_UNPUBLISHABLE"
    assert store.list_catalogs() == ()
    # Only a store written outside its API can hold such a catalog; publishing still says why.
    with raw(store) as outside:
        outside.execute(
            "INSERT INTO corpus_catalogs VALUES (?, ?, 'capture-pilot', ?, NULL, '')",
            (CATALOG, CATALOG, EPOCH.isoformat(timespec="microseconds")),
        )
    with pytest.raises(CorpusStoreError, match="does not materialize a valid suite") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    message = str(caught.value)
    assert (
        "prompts a capture-pilot catalog cannot hold: held-00 (prompt holdout flags must agree "
        "with suite purpose), held-01 (" in message
    )
    assert "held-09 (prompt holdout flags must agree with suite purpose) and 2 more" in message
    assert "held-10" not in message and "prompt-01" not in message
    assert message.endswith("; retire them or edit them to fit")
    assert "Value error" not in message
    # A failure no prompt explains (here a forged purpose) names none.
    with raw(store) as attacker:
        attacker.execute("UPDATE corpus_catalogs SET catalog_purpose = 'karaoke'")
    with pytest.raises(CorpusStoreError, match="does not materialize a valid suite") as caught:
        store.publish_version(CATALOG, prompt_ids=("prompt-01",))
    assert "cannot hold" not in str(caught.value)


def test_catalog_definitions_refuse_a_mismatched_prefix_and_a_terminal_unsafe_description() -> None:
    definition = corpus_store.catalog_definition(
        "cole-corpus", purpose="capture-pilot", description="Living corpus · v2"
    )
    assert definition == corpus_store.CatalogDefinition(
        "cole-corpus", "cole-corpus", "capture-pilot", "Living corpus · v2"
    )
    physical = corpus_store.catalog_definition(
        "bench", suite_id="cole-physical-test-bench", purpose="physical-test"
    )
    assert physical.suite_id == "cole-physical-test-bench"
    for fields, message in (
        ({"catalog_id": "bench", "purpose": "physical-test"}, "cannot name a physical-test"),
        (
            {"catalog_id": "cole-physical-test-x", "purpose": "capture-pilot"},
            "cannot name a capture-pilot",
        ),
        ({"catalog_id": "c", "purpose": "capture-pilot", "description": "a\nb"}, "control"),
        (
            {"catalog_id": "c", "purpose": "capture-pilot", "description": "safe\x1b]0;x\x07"},
            "control",
        ),
        ({"catalog_id": "c", "purpose": "capture-pilot", "description": "a\u2028b"}, "separator"),
        ({"catalog_id": "c", "purpose": "capture-pilot", "description": "a\u200eb"}, "format"),
    ):
        with pytest.raises(CorpusStoreError, match=message) as caught:
            corpus_store.catalog_definition(**fields)
        assert caught.value.code == "CORPUS.CATALOG_INVALID"
    with pytest.raises(CorpusStoreError, match="subject id") as subject:
        corpus_store.validated_subject_id("Cole Self")
    assert subject.value.code == "CORPUS.SUBJECT_INVALID"
    assert corpus_store.validated_subject_id(SUBJECT) == SUBJECT


def test_creating_a_catalog_applies_its_definition(store: CorpusStore) -> None:
    with pytest.raises(CorpusStoreError, match="control") as caught:
        store.create_catalog("second", purpose="capture-pilot", description="safe\x1b[2J")
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    with pytest.raises(CorpusStoreError, match="cannot name a physical-test"):
        store.create_catalog("second", purpose="physical-test")
    assert store.list_catalogs() == ()


def test_initializing_a_catalog_stores_seed_version_and_activation_or_nothing(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = load_builtin_prompt_suite("pilot-v2.json")
    pin = BUILTIN_PROMPT_SUITE_SHA256["pilot-v2.json"]
    definition = corpus_store.catalog_definition(
        "pilot-mirror", suite_id=seed.suite_id, purpose=seed.purpose
    )
    for fields in ({"seed": seed}, {"subject_id": SUBJECT}):
        with pytest.raises(ValueError, match="exactly one subject"):
            store.initialize_catalog(definition, **fields)
    with pytest.raises(CorpusStoreError, match="subject id") as caught:
        store.initialize_catalog(definition, seed=seed, subject_id="Not A Subject")
    assert caught.value.code == "CORPUS.SUBJECT_INVALID"

    def refuse(*_: object) -> None:
        raise CorpusStoreError("CORPUS.STORE_UNAVAILABLE", "activation failed")

    monkeypatch.setattr(CorpusStore, "_activate", refuse)
    with pytest.raises(CorpusStoreError, match="activation failed"):
        store.initialize_catalog(definition, seed=seed, subject_id=SUBJECT)
    assert store.list_prompts(include_retired=True) == () and store.list_catalogs() == ()
    # The refused version's content-addressed snapshot may stay; publishing reuses it.
    leftover = list(store.snapshot_directory.iterdir())
    assert [path.name for path in leftover] == [f"{pin}.json"]
    monkeypatch.undo()

    record, reference = store.initialize_catalog(definition, seed=seed, subject_id=SUBJECT)
    assert reference is not None
    assert reference.catalog_sha256 == pin
    assert record.versions == (reference,) and record.catalog_id == "pilot-mirror"
    assert store.active_catalog(SUBJECT) == (seed, reference)
    assert {r.source for r in store.list_prompts()} == {"import"}
    with pytest.raises(CorpusStoreError, match="holds 36 prompt") as caught:
        store.initialize_catalog(corpus_store.catalog_definition("other", purpose="capture-corpus"))
    assert caught.value.code == "CORPUS.CATALOG_DUPLICATE"


def test_initializing_without_a_seed_creates_only_the_empty_catalog(store: CorpusStore) -> None:
    definition = corpus_store.catalog_definition("cole-corpus", purpose="capture-corpus")
    record, reference = store.initialize_catalog(definition)
    assert reference is None and record.versions == () and record.purpose == "capture-corpus"
    assert store.list_prompts() == ()


def test_a_version_number_outside_the_sqlite_range_is_simply_unknown(store: CorpusStore) -> None:
    seeded(store, 1)
    store.create_catalog(CATALOG, purpose="capture-corpus")
    store.publish_version(CATALOG)
    for version in (0, -1, 2**63, 2**70):
        with pytest.raises(CorpusStoreError, match=f"has no version {version}$") as caught:
            store.resolve_reference(CATALOG, version)
        assert caught.value.code == "CORPUS.VERSION_UNKNOWN"
    assert store.resolve_reference(CATALOG, 1).catalog_version == 1


@pytest.mark.parametrize(
    ("before", "after", "words", "characters"),
    [
        ("The birch canoe.", "the Birch canoe!", 0, 0),
        ("It's easy.", "Its  easy", 0, 0),
        ("Four words right here.", "Four words right there.", 1, 1),
        ("The smoth planks.", "The smooth planks.", 1, 1),
        ("Send it to me.", "Send it me.", 1, 3),
        ("Colour me red.", "Color me red.", 1, 1),
        ("Synthetic sentence number 0.", "Synthetic sentence zero.", 2, None),
        ("A cat sat.", "A dog sat.", 1, 3),
        ("A cat sat.", "A horse sat.", 1, None),
        ("One two three.", "Three two one.", 2, None),
        ("One.", "One two three four.", None, None),
        ("A long sentence about birds.", "Bananas are yellow.", None, None),
    ],
)
def test_text_changes_are_measured_on_the_words_read_aloud(
    before: str, after: str, words: int | None, characters: int | None
) -> None:
    change = corpus_store.text_change(before, after)
    assert (change.before, change.after) == (before, after)
    assert (change.word_edits, change.character_edits) == (words, characters)
    assert change.typo_class is (words is not None and characters is not None)
    assert change.spoken_words_unchanged is (words == 0)


def _levenshtein(first: str, second: str) -> int:
    previous = list(range(len(second) + 1))
    for row, left in enumerate(first, 1):
        current = [row]
        for column, right in enumerate(second, 1):
            current.append(
                min(previous[column] + 1, current[-1] + 1, previous[column - 1] + (left != right))
            )
        previous = current
    return previous[-1]


def test_the_banded_edit_distance_agrees_with_the_full_one_within_its_band() -> None:
    import random

    generator = random.Random(20260916)
    for _ in range(3000):
        first = "".join(generator.choice("ab ") for _ in range(generator.randint(0, 9)))
        second = "".join(generator.choice("ab ") for _ in range(generator.randint(0, 9)))
        limit = generator.randint(0, 4)
        exact = _levenshtein(first, second)
        expected = exact if exact <= limit else None
        assert corpus_store._edits_within(first, second, limit) == expected, (first, second)


def test_keep_recordings_chain_accumulates_until_a_default_edit_breaks_it(
    store: CorpusStore,
) -> None:
    first = add(store, "prompt-01", "the quick brown fox")
    second = store.edit_prompt("prompt-01", text="The quick brown fox", keep_recordings=True)
    assert second.keeps_recordings is True
    assert second.accepted_prompt_sha256s == (first.prompt_sha256,)
    third = store.edit_prompt("prompt-01", text="The quick brown fox.", keep_recordings=True)
    assert third.accepted_prompt_sha256s == tuple(
        sorted((first.prompt_sha256, second.prompt_sha256))
    )
    reverted = store.edit_prompt("prompt-01", text="The quick brown fox", keep_recordings=True)
    assert reverted.prompt_sha256 == second.prompt_sha256
    assert reverted.accepted_prompt_sha256s == tuple(
        sorted((first.prompt_sha256, third.prompt_sha256))
    )
    fifth = store.edit_prompt("prompt-01", text="A completely different sentence.")
    assert fifth.keeps_recordings is False
    assert fifth.accepted_prompt_sha256s == ()
    sixth = store.edit_prompt(
        "prompt-01", text="A completely different sentence!", keep_recordings=True
    )
    assert sixth.accepted_prompt_sha256s == (fifth.prompt_sha256,)
    assert [record.revision for record in store.prompt_history("prompt-01")] == [1, 2, 3, 4, 5, 6]


def test_keep_recordings_edit_cannot_change_held_out_or_spoken_behaviour(
    store: CorpusStore,
) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    tone = add(
        store,
        "tone-01",
        None,
        category="room-tone",
        style="Silence.",
        instructions="Stay silent.",
    )
    for prompt_id, changes in (
        ("prompt-01", {"holdout": True}),
        ("prompt-01", {"text": None, "category": "room-tone", "instructions": "Stay silent."}),
        ("tone-01", {"text": "Now spoken.", "category": "neutral", "instructions": None}),
    ):
        with pytest.raises(CorpusStoreError, match="cannot change its held-out flag") as caught:
            store.edit_prompt(prompt_id, keep_recordings=True, **changes)
        assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert store.list_prompts() == (first, tone)
    # Another spoken category or style keeps the identity; a default edit may change anything.
    kept = store.edit_prompt(
        "prompt-01", keep_recordings=True, category="technical", style="Careful."
    )
    assert kept.accepted_prompt_sha256s == (first.prompt_sha256,)
    moved = store.edit_prompt("prompt-01", holdout=True)
    assert moved.prompt.holdout and moved.accepted_prompt_sha256s == ()
    back = store.edit_prompt("prompt-01", keep_recordings=True, text="Sentence one!")
    assert back.accepted_prompt_sha256s == (moved.prompt_sha256,)


def test_a_keep_recordings_edit_rewords_only_when_the_caller_vouches_for_the_recordings(
    store: CorpusStore,
) -> None:
    """The typo-class bound is the store's rule, so every editor applies it."""
    first = add(store, "prompt-01", "Synthetic sentence number 0.")
    tone = add(
        store, "tone-01", None, category="room-tone", style="Silence.", instructions="Be still."
    )
    for dry_run in (True, False):
        with pytest.raises(CorpusStoreError, match="is a rewording") as caught:
            store.edit_prompt(
                "prompt-01",
                text="Synthetic sentence zero.",
                keep_recordings=True,
                dry_run=dry_run,
            )
        assert caught.value.code == "CORPUS.REWORDING_REFUSED"
        message = str(caught.value)
        assert '"Synthetic sentence number 0." -> "Synthetic sentence zero."' in message
        assert "add --allow-rewording if the recordings already say the new text" in message
    assert store.prompt_history("prompt-01") == (first,)
    # The identity rule is checked first: a spoken prompt never becomes silent coverage.
    with pytest.raises(CorpusStoreError, match="cannot change its held-out flag") as caught:
        store.edit_prompt(
            "prompt-01",
            text=None,
            category="room-tone",
            instructions="Be still.",
            keep_recordings=True,
            allow_rewording=True,
        )
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    with pytest.raises(ValueError, match="only qualifies a keep_recordings edit"):
        store.edit_prompt("prompt-01", text="Synthetic sentence zero.", allow_rewording=True)
    # A typo-class fix, a silent prompt's instructions and a default rewording need no word.
    typo = store.edit_prompt("prompt-01", text="Synthetic sentense number 0!", keep_recordings=True)
    assert typo.accepted_prompt_sha256s == (first.prompt_sha256,)
    silent = store.edit_prompt("tone-01", instructions="Stay silent now.", keep_recordings=True)
    assert silent.accepted_prompt_sha256s == (tone.prompt_sha256,)
    reworded = store.edit_prompt(
        "prompt-01", text="Synthetic sentence zero.", keep_recordings=True, allow_rewording=True
    )
    assert reworded.accepted_prompt_sha256s == tuple(
        sorted((first.prompt_sha256, typo.prompt_sha256))
    )
    assert store.edit_prompt("prompt-01", text="A new sentence entirely.").revision == 4


def test_every_live_prompt_must_fit_every_catalog_of_the_store(store: CorpusStore) -> None:
    """``publish`` takes every live prompt, so the store refuses what a catalog cannot hold."""
    held = {"holdout": True}
    # Without catalogs any storable prompt is taken.
    add(store, "held-00", "Held sentence zero.", **held)
    with pytest.raises(CorpusStoreError, match="cannot hold the live prompts held-00;") as caught:
        store.create_catalog(CATALOG, purpose="capture-pilot")
    assert caught.value.code == "CORPUS.CATALOG_UNPUBLISHABLE"
    assert str(caught.value).endswith(
        "every version publishes every live prompt (`voice-lab catalog publish`), so nothing "
        "was written"
    )
    # A retired prompt is never published again, so it constrains no new catalog.
    store.retire_prompt("held-00")
    store.create_catalog(CATALOG, purpose="capture-pilot")
    seeded(store, 1)
    unpublishable = "CORPUS.PROMPT_UNPUBLISHABLE"
    with pytest.raises(CorpusStoreError) as caught:
        add(store, "held-01", "Held sentence one.", **held)
    assert caught.value.code == unpublishable
    assert str(caught.value) == (
        "`voice-lab catalog publish` publishes every live prompt, and a catalog of this store "
        "could never publish held-01 (catalog cole-corpus is capture-pilot: prompt holdout "
        "flags must agree with suite purpose); nothing was written"
    )
    with pytest.raises(CorpusStoreError, match="only physical-test catalogs") as caught:
        add(store, "label-01", "A labelled sentence.", labels=("test",))
    assert caught.value.code == unpublishable
    for dry_run in (True, False):
        with pytest.raises(CorpusStoreError, match="could never publish prompt-01") as caught:
            store.edit_prompt("prompt-01", holdout=True, dry_run=dry_run)
        assert caught.value.code == unpublishable
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="evaluation",
        purpose="held-out-evaluation",
        prompts=tuple(
            Prompt(
                prompt_id=f"eval-{number:02d}",
                text=f"Evaluation sentence {number}.",
                category="neutral",
                style="Plain.",
                expected_seconds=3.0,
                holdout=True,
            )
            for number in range(12)
        ),
    )
    with pytest.raises(CorpusStoreError, match="eval-09 .* and 2 more; nothing") as caught:
        store.import_prompt_suite(suite, source="import")
    assert caught.value.code == unpublishable and "eval-10" not in str(caught.value)
    assert [r.prompt.prompt_id for r in store.list_prompts(include_retired=True)] == [
        "held-00",
        "prompt-01",
    ]
    assert len(store.prompt_history("prompt-01")) == 1
    with pytest.raises(CorpusStoreError, match="cannot hold the live prompts prompt-01") as caught:
        store.create_catalog("evaluation", purpose="held-out-evaluation")
    assert caught.value.code == "CORPUS.CATALOG_UNPUBLISHABLE"
    # A duplicate id is named before the fit, as before.
    with pytest.raises(CorpusStoreError, match="already exists") as caught:
        store.create_catalog(CATALOG, purpose="held-out-evaluation")
    assert caught.value.code == "CORPUS.CATALOG_DUPLICATE"
    assert [record.catalog_id for record in store.list_catalogs()] == [CATALOG]


def test_republishing_reverted_content_names_the_recordings_that_version_does_not_accept(
    store: CorpusStore,
) -> None:
    first, _ = seeded(store, 2)
    original = published(store)
    reworded = {"keep_recordings": True, "allow_rewording": True}
    store.edit_prompt("prompt-01", text="Sentence number one.", **reworded)
    store.edit_prompt("prompt-01", text=first.prompt.text, **reworded)
    # The reverted suite is version 1's content, which accepts no earlier recording.
    with pytest.raises(CorpusStoreError, match=f"catalog {CATALOG} version 1, which") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.VERSION_UNCHANGED"
    assert "other earlier recordings of prompt-01 than the current revisions" in str(caught.value)
    assert "prompt-02" not in str(caught.value)
    assert store.materialize(original).prompt_aliases == ()
    # Changing another prompt publishes the revert with its accepted recordings.
    store.edit_prompt("prompt-02", text="Sentence number two!")
    second = store.materialize(store.publish_version(CATALOG))
    assert second.current_prompt_for(first.prompt) == first.prompt
    assert len(second.prompt_aliases) == 1


def test_republishing_names_at_most_ten_prompts_whose_accepted_recordings_differ(
    store: CorpusStore,
) -> None:
    records = seeded(store, 12)
    published(store)
    for record in records:
        store.edit_prompt(record.prompt.prompt_id, text="Edited " + str(record.prompt.text))
        store.edit_prompt(
            record.prompt.prompt_id,
            text=str(record.prompt.text),
            keep_recordings=True,
            allow_rewording=True,
        )
    with pytest.raises(CorpusStoreError, match="prompt-10 and 2 more than") as caught:
        store.publish_version(CATALOG)
    assert "prompt-01, prompt-02" in str(caught.value) and "prompt-11" not in str(caught.value)


def test_retired_prompt_leaves_new_versions_and_refuses_edits(store: CorpusStore) -> None:
    seeded(store, 2)
    before = published(store)
    retired = store.retire_prompt("prompt-01")
    assert retired.retired_at is not None and retired.retired_at > retired.created_at
    assert [record.prompt.prompt_id for record in store.list_prompts()] == ["prompt-02"]
    assert [record.prompt.prompt_id for record in store.list_prompts(include_retired=True)] == [
        "prompt-01",
        "prompt-02",
    ]
    for call in (
        lambda: store.retire_prompt("prompt-01"),
        lambda: store.edit_prompt("prompt-01", text="Changed."),
        lambda: store.publish_version(CATALOG, prompt_ids=("prompt-01",)),
    ):
        with pytest.raises(CorpusStoreError, match="retired") as caught:
            call()
        assert caught.value.code == "CORPUS.PROMPT_RETIRED"
    after = store.publish_version(CATALOG)
    assert after.catalog_version == 2
    assert [p.prompt_id for p in store.materialize(after).suite.prompts] == ["prompt-02"]
    assert [p.prompt_id for p in store.materialize(before).suite.prompts] == [
        "prompt-01",
        "prompt-02",
    ]


def test_import_seeds_a_suite_in_order_or_nothing_at_all(store: CorpusStore) -> None:
    builtin = load_builtin_prompt_suite("evaluation-v1.json")
    assert store.import_prompt_suite(builtin, source="import") == tuple(
        prompt.prompt_id for prompt in builtin.prompts
    )
    records = store.list_prompts()
    assert [record.prompt for record in records] == list(builtin.prompts)
    assert {record.source for record in records} == {"import"}
    fresh = builtin.prompts[0].model_copy(
        update={"prompt_id": "fresh-01", "text": "A sentence nobody has yet.", "holdout": False}
    )
    for prompts, message in (
        ((fresh, builtin.prompts[1].model_copy(update={"holdout": False})), "already exists"),
        ((fresh, fresh.model_copy(update={"prompt_id": "fresh-02"})), "repeats the text"),
    ):
        second = PromptSuite(suite_id="second", purpose="capture-pilot", prompts=prompts)
        with pytest.raises(CorpusStoreError, match=message) as caught:
            store.import_prompt_suite(second, source="import")
        assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
        assert store.list_prompts() == records


def test_tampered_prompt_rows_are_named_failures(store: CorpusStore) -> None:
    seeded(store, 3)
    with raw(store) as attacker:
        for prompt_id, column, value in (
            ("prompt-01", "prompt_labels_json", "{}"),
            ("prompt-02", "prompt_style", ""),
            ("prompt-03", "prompt_text", "Swapped."),
        ):
            attacker.execute(
                f"UPDATE corpus_prompt_revisions SET {column} = ? WHERE prompt_id = ?",
                (value, prompt_id),
            )
    for prompt_id, message in (
        ("prompt-01", "stored prompt revision is invalid: prompt-01"),
        ("prompt-02", "stored prompt revision is invalid: prompt-02"),
        ("prompt-03", "stored prompt digest disagrees with its content: prompt-03"),
    ):
        with pytest.raises(CorpusStoreError, match=message) as caught:
            store.prompt_history(prompt_id)
        assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_tampered_timestamps_and_column_types_are_named_failures(store: CorpusStore) -> None:
    seeded(store, 3)
    published(store)
    with raw(store) as attacker:
        attacker.execute(
            "UPDATE corpus_prompts SET prompt_created_at = 'garbage' WHERE prompt_id = 'prompt-01'"
        )
        attacker.execute(
            "UPDATE corpus_prompts SET prompt_created_at = '2026-01-01T00:00:00' "
            "WHERE prompt_id = 'prompt-02'"
        )
        attacker.execute(
            "UPDATE corpus_prompt_revisions SET prompt_revision = 'abc' "
            "WHERE prompt_id = 'prompt-03'"
        )
    for prompt_id, message in (
        (
            "prompt-01",
            "stored prompt revision is invalid: prompt-01: prompt_created_at: "
            "Input should be a valid datetime",
        ),
        (
            "prompt-02",
            "stored prompt revision is invalid: prompt-02: prompt_created_at: "
            ".*require an explicit timezone$",
        ),
        ("prompt-03", "corpus store row is invalid: invalid literal"),
    ):
        with pytest.raises(CorpusStoreError, match=message) as caught:
            store.prompt_history(prompt_id)
        assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    with pytest.raises(CorpusStoreError, match="stored prompt revision is invalid"):
        store.list_prompts()
    with raw(store) as attacker:
        attacker.execute("UPDATE corpus_catalogs SET catalog_created_at = 'garbage'")
    with pytest.raises(
        CorpusStoreError, match="stored catalog is invalid: cole-corpus: catalog_created_at: "
    ) as caught:
        store.list_catalogs()
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    with raw(store) as attacker:
        attacker.execute(
            "UPDATE corpus_catalogs SET catalog_created_at = '2026-09-16T10:00:00+00:00', "
            "catalog_purpose = 'karaoke'"
        )
    with pytest.raises(
        CorpusStoreError, match="stored catalog is invalid: cole-corpus: Input should be 'capture-"
    ) as caught:
        store.list_catalogs()
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    with pytest.raises(CorpusStoreError, match="stored catalog version is invalid") as caught:
        store.resolve_reference(CATALOG)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


# -- catalogs and versions -------------------------------------------------------------


def test_create_publish_activate_materialize_round_trip_equals_the_contract(
    store: CorpusStore,
) -> None:
    records = seeded(store, 3)
    catalog = store.create_catalog(CATALOG, purpose="capture-corpus", description="the corpus")
    assert (catalog.catalog_id, catalog.suite_id, catalog.purpose) == (
        CATALOG,
        CATALOG,
        "capture-corpus",
    )
    assert catalog.description == "the corpus"
    assert catalog.retired_at is None
    assert catalog.versions == ()
    reference = store.publish_version(CATALOG)
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id=CATALOG,
        purpose="capture-corpus",
        prompts=tuple(record.prompt for record in records),
    )
    digest = canonical_sha256(suite)
    snapshot = CatalogSnapshot(suite=suite, catalog_sha256=digest, prompt_aliases=())
    payload = canonical_json_bytes(snapshot) + b"\n"
    assert reference == CatalogReference(
        catalog_id=CATALOG,
        catalog_version=1,
        catalog_sha256=digest,
        snapshot_sha256=file_sha256(payload),
        suite_id=CATALOG,
        purpose="capture-corpus",
        snapshot_path=catalog_snapshot_path(digest),
    )
    store.activate(SUBJECT, reference)
    assert store.active_catalog(SUBJECT) == (suite, reference)
    assert store.materialize(reference) == snapshot
    assert read_catalog_snapshot(store.layout, reference) == snapshot
    path = snapshot_file(store, reference)
    assert path == store.snapshot_directory / f"{digest}.json"
    assert mode(path) == 0o600
    assert path.read_bytes() == payload
    assert sorted(store.snapshot_directory.iterdir()) == [path]
    assert store.resolve_reference(CATALOG) == reference
    assert store.resolve_reference(CATALOG, 1) == reference
    assert store.list_catalogs()[0].versions == (reference,)
    with raw(store) as reader:
        ordinals = reader.execute(
            "SELECT catalog_prompt_ordinal FROM corpus_catalog_version_prompts "
            "ORDER BY catalog_prompt_ordinal"
        ).fetchall()
    assert [row[0] for row in ordinals] == [0, 1, 2]


def test_catalog_creation_validates_ids_purpose_description_and_uniqueness(
    store: CorpusStore,
) -> None:
    for fields, message in (
        ({"catalog_id": "Bad Id", "purpose": "capture-corpus"}, "catalog id"),
        ({"catalog_id": "ok", "suite_id": "@", "purpose": "capture-corpus"}, "suite id"),
        ({"catalog_id": "ok", "purpose": "karaoke"}, "purpose"),
        (
            {"catalog_id": "ok", "purpose": "capture-corpus", "description": "x" * 1025},
            "description",
        ),
    ):
        with pytest.raises(CorpusStoreError, match=message) as caught:
            store.create_catalog(**fields)
        assert caught.value.code == "CORPUS.CATALOG_INVALID"
    explicit = store.create_catalog("mirror", suite_id="cole-pilot-v2", purpose="capture-pilot")
    assert explicit.suite_id == "cole-pilot-v2"
    with pytest.raises(CorpusStoreError, match="already exists") as caught:
        store.create_catalog("mirror", purpose="capture-corpus")
    assert caught.value.code == "CORPUS.CATALOG_DUPLICATE"
    assert [catalog.catalog_id for catalog in store.list_catalogs()] == ["mirror"]


def test_unknown_catalog_and_version_are_named(store: CorpusStore) -> None:
    for call in (
        lambda: store.publish_version("absent"),
        lambda: store.resolve_reference("absent"),
    ):
        with pytest.raises(CorpusStoreError, match="unknown catalog: absent") as caught:
            call()
        assert caught.value.code == "CORPUS.CATALOG_UNKNOWN"
    store.create_catalog(CATALOG, purpose="capture-corpus")
    with pytest.raises(CorpusStoreError, match="no published version") as caught:
        store.resolve_reference(CATALOG)
    assert caught.value.code == "CORPUS.VERSION_UNKNOWN"
    seeded(store, 1)
    reference = store.publish_version(CATALOG)
    with pytest.raises(CorpusStoreError, match="has no version 2") as caught:
        store.resolve_reference(CATALOG, 2)
    assert caught.value.code == "CORPUS.VERSION_UNKNOWN"
    later = reference.model_copy(update={"catalog_version": 2})
    with pytest.raises(CorpusStoreError, match="has no version 2") as caught:
        store.materialize(later)
    assert caught.value.code == "CORPUS.VERSION_UNKNOWN"
    elsewhere = reference.model_copy(update={"catalog_id": "absent"})
    with pytest.raises(CorpusStoreError, match="unknown catalog: absent"):
        store.activate(SUBJECT, elsewhere)


def test_publishing_validates_selection_and_the_materialized_suite(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create_catalog(CATALOG, purpose="capture-corpus")
    with pytest.raises(CorpusStoreError, match="no prompts to publish") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.EMPTY_CATALOG"
    seeded(store, 2)
    with pytest.raises(CorpusStoreError, match="listed more than once") as caught:
        store.publish_version(CATALOG, prompt_ids=("prompt-01", "prompt-01"))
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    with pytest.raises(CorpusStoreError, match="unknown prompt: absent") as caught:
        store.publish_version(CATALOG, prompt_ids=("prompt-01", "absent"))
    assert caught.value.code == "CORPUS.PROMPT_UNKNOWN"
    with pytest.raises(CorpusStoreError, match="cannot hold the live prompts") as caught:
        store.create_catalog("held-out", purpose="held-out-evaluation")
    assert caught.value.code == "CORPUS.CATALOG_UNPUBLISHABLE"
    # Publishing validates the suite itself, whatever wrote the catalog row.
    with raw(store) as outside:
        outside.execute(
            "INSERT INTO corpus_catalogs VALUES "
            "('held-out', 'held-out', 'held-out-evaluation', ?, NULL, '')",
            (EPOCH.isoformat(timespec="microseconds"),),
        )
    with pytest.raises(CorpusStoreError, match="holdout flags must agree") as caught:
        store.publish_version("held-out")
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    monkeypatch.setattr(corpus_store, "MAX_CATALOG_PROMPTS", 1)
    with pytest.raises(CorpusStoreError, match="exceeds 1 prompts") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    monkeypatch.undo()
    # The publish-time byte bound is the same bound every reader enforces, so a version
    # that publishes can always be read back.
    monkeypatch.setattr(corpus_store, "MAX_CATALOG_SNAPSHOT_BYTES", 64)
    with pytest.raises(CorpusStoreError, match="snapshot would exceed 64 bytes") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    assert store.list_catalogs()[0].versions == ()
    assert list(store.snapshot_directory.iterdir()) == []


def test_publishing_refuses_a_suite_one_byte_over_what_the_app_receives_in_one_message(
    store: CorpusStore,
) -> None:
    from test_recording_contracts import catalog_of_exactly

    from cole_voice_lab.recording_contracts import MAX_CATALOG_SUITE_BYTES

    exact = catalog_of_exactly(MAX_CATALOG_SUITE_BYTES)
    pad = exact.prompts[-1]
    store.import_prompt_suite(exact, source="manual")
    # The same pad one character longer, under an id of the same length.
    assert pad.text is not None
    add(store, "shape-pae", pad.text + "p", style=pad.style, expected_seconds=6.3)
    store.create_catalog(exact.suite_id, purpose=exact.purpose)
    shaped = tuple(prompt.prompt_id for prompt in exact.prompts[:-1])
    with pytest.raises(CorpusStoreError, match="app receives in one message") as caught:
        store.publish_version(exact.suite_id, prompt_ids=(*shaped, "shape-pae"))
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    assert f"would be {MAX_CATALOG_SUITE_BYTES + 1} bytes with {len(exact.prompts)}" in str(
        caught.value
    )
    assert store.list_catalogs()[0].versions == ()
    assert list(store.snapshot_directory.iterdir()) == []
    reference = store.publish_version(exact.suite_id, prompt_ids=(*shaped, pad.prompt_id))
    assert store.materialize(reference).suite == exact
    assert reference.catalog_sha256 == canonical_sha256(exact)


def test_failed_snapshot_write_leaves_no_scratch_and_no_version(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(store, 1)
    store.create_catalog(CATALOG, purpose="capture-corpus")

    def refuse(*_: Any, **__: Any) -> None:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(corpus_store.os, "link", refuse)
    with pytest.raises(CorpusStoreError, match="query failed.*no space") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    monkeypatch.undo()
    assert os.listdir(store.snapshot_directory) == []
    assert store.list_catalogs()[0].versions == ()
    assert store.publish_version(CATALOG).catalog_version == 1


def test_explicit_prompt_ids_set_the_order_and_identical_content_is_not_republished(
    store: CorpusStore,
) -> None:
    seeded(store, 3)
    store.create_catalog(CATALOG, purpose="capture-corpus")
    first = store.publish_version(CATALOG, prompt_ids=("prompt-03", "prompt-01"))
    assert [p.prompt_id for p in store.materialize(first).suite.prompts] == [
        "prompt-03",
        "prompt-01",
    ]
    with pytest.raises(CorpusStoreError, match=f"catalog {CATALOG} version 1") as caught:
        store.publish_version(CATALOG, prompt_ids=("prompt-03", "prompt-01"))
    assert caught.value.code == "CORPUS.VERSION_UNCHANGED"
    second = store.publish_version(CATALOG, prompt_ids=("prompt-01", "prompt-03"))
    assert second.catalog_version == 2
    assert second.catalog_sha256 != first.catalog_sha256
    store.create_catalog("twin", suite_id=CATALOG, purpose="capture-corpus")
    with pytest.raises(CorpusStoreError, match=f"catalog {CATALOG} version 2") as caught:
        store.publish_version("twin", prompt_ids=("prompt-01", "prompt-03"))
    assert caught.value.code == "CORPUS.VERSION_UNCHANGED"
    assert store.resolve_reference(CATALOG) == second
    assert store.list_catalogs()[0].versions == (first, second)


def test_default_edit_changes_the_catalog_digest_and_keep_edits_publish_aliases(
    store: CorpusStore,
) -> None:
    first, second, _ = seeded(store, 3)
    before = published(store)
    edited = store.edit_prompt("prompt-01", text="Sentence number 1!")
    after = store.publish_version(CATALOG)
    assert after.catalog_sha256 != before.catalog_sha256
    assert store.materialize(after).prompt_aliases == ()
    kept = store.edit_prompt("prompt-02", text="Sentence number 2!", keep_recordings=True)
    kept_again = store.edit_prompt("prompt-02", text="Sentence number 2?", keep_recordings=True)
    third = store.publish_version(CATALOG)
    snapshot = store.materialize(third)
    assert snapshot.prompt_aliases == tuple(
        PromptAlias(prompt_sha256=kept_again.prompt_sha256, accepted_prompt_sha256=accepted)
        for accepted in sorted((second.prompt_sha256, kept.prompt_sha256))
    )
    assert snapshot.accepted_hashes(kept_again.prompt_sha256) == {
        kept_again.prompt_sha256,
        kept.prompt_sha256,
        second.prompt_sha256,
    }
    assert snapshot.current_for(second.prompt_sha256) == kept_again.prompt_sha256
    assert snapshot.current_for(first.prompt_sha256) is None
    assert snapshot.current_for(edited.prompt_sha256) == edited.prompt_sha256
    assert store.materialize(before).prompt_aliases == ()


def test_activation_needs_a_published_readable_version_and_a_valid_subject(
    store: CorpusStore,
) -> None:
    with pytest.raises(CorpusStoreError, match="no active catalog") as caught:
        store.active_catalog(SUBJECT)
    assert caught.value.code == "CORPUS.NO_ACTIVE_CATALOG"
    assert "voice-lab catalog init" in str(caught.value)
    seeded(store, 2)
    first = published(store)
    with pytest.raises(CorpusStoreError, match="subject id") as caught:
        store.activate("Not Valid", first)
    assert caught.value.code == "CORPUS.SUBJECT_INVALID"
    store.activate(SUBJECT, first)
    store.retire_prompt("prompt-02")
    second = store.publish_version(CATALOG)
    assert store.active_catalog(SUBJECT)[1] == first
    store.activate(SUBJECT, second)
    assert store.active_catalog(SUBJECT)[1] == second
    store.activate("other-subject", first)
    assert store.active_catalog("other-subject")[1] == first
    snapshot_file(store, second).unlink()
    with pytest.raises(CorpusSnapshotSecurityError, match="missing") as caught:
        store.activate("third-subject", second)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"
    with pytest.raises(CorpusStoreError, match="no active catalog"):
        store.active_catalog("third-subject")


def test_tampered_catalog_rows_are_named_failures(store: CorpusStore) -> None:
    seeded(store, 1)
    reference = published(store)
    with raw(store) as attacker:
        attacker.execute(
            "UPDATE corpus_catalogs SET catalog_retired_at = '2026-09-16T12:00:00+02:00'"
        )
    assert store.list_catalogs()[0].retired_at == datetime(2026, 9, 16, 10, tzinfo=UTC)
    with raw(store) as attacker:
        attacker.execute(
            "UPDATE corpus_catalog_versions SET catalog_snapshot_path = 'elsewhere.json'"
        )
    with pytest.raises(CorpusStoreError, match="stored catalog version is invalid") as caught:
        store.resolve_reference(CATALOG)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    with pytest.raises(CorpusStoreError, match="stored catalog version is invalid"):
        store.materialize(reference)
    with raw(store) as attacker:
        attacker.execute(
            "UPDATE corpus_catalog_versions SET catalog_snapshot_path = ?, "
            "catalog_snapshot_sha256 = ?",
            (reference.snapshot_path, "0" * 64),
        )
    # The row now pins other bytes than the file has: the reference in hand is stale.
    with pytest.raises(CorpusSnapshotSecurityError, match="disagrees with the published") as caught:
        store.materialize(reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    stale = store.resolve_reference(CATALOG)
    assert stale.snapshot_sha256 == "0" * 64
    with pytest.raises(CorpusSnapshotSecurityError, match="published bytes") as caught:
        store.materialize(stale)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


def test_snapshot_that_disagrees_with_the_version_rows_is_a_security_error(
    store: CorpusStore,
) -> None:
    first, _ = seeded(store, 2)
    store.edit_prompt("prompt-01", text="Sentence number 1!", keep_recordings=True)
    reference = published(store)
    store.activate(SUBJECT, reference)
    assert len(store.materialize(reference).prompt_aliases) == 1
    with raw(store) as attacker:
        attacker.execute("DELETE FROM corpus_prompt_aliases")
    for call in (
        lambda: store.materialize(reference),
        lambda: store.activate("other-subject", reference),
        lambda: store.active_catalog(SUBJECT),
    ):
        with pytest.raises(CorpusSnapshotSecurityError, match="published prompt aliases") as caught:
            call()
        assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    with pytest.raises(CorpusStoreError, match="no active catalog"):
        store.active_catalog("other-subject")
    with raw(store) as attacker:
        attacker.execute(
            "INSERT INTO corpus_prompt_aliases VALUES ('prompt-01', 2, ?)", (first.prompt_sha256,)
        )
    assert store.materialize(reference).suite.prompts[0].text == "Sentence number 1!"
    with raw(store) as attacker:
        attacker.execute("DELETE FROM corpus_catalog_version_prompts WHERE prompt_id = 'prompt-02'")
    with pytest.raises(CorpusSnapshotSecurityError, match="published prompt revisions") as caught:
        store.materialize(reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


# -- snapshots -----------------------------------------------------------------------


def test_reference_that_disagrees_with_the_published_version_fails_closed(
    store: CorpusStore,
) -> None:
    seeded(store, 1)
    reference = published(store)
    forged = reference.model_copy(update={"suite_id": "other"})
    with pytest.raises(CorpusSnapshotSecurityError, match="disagrees with the published") as caught:
        store.materialize(forged)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert isinstance(caught.value, SecurityError)
    with pytest.raises(CorpusSnapshotSecurityError, match="not other/capture-corpus") as caught:
        read_catalog_snapshot(store.layout, forged)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


def test_altered_snapshot_is_a_security_error(store: CorpusStore) -> None:
    seeded(store, 2)
    reference = published(store)
    path = snapshot_file(store, reference)
    original = path.read_bytes()
    store.create_catalog("other", suite_id="other", purpose="capture-corpus")
    other = store.publish_version("other")
    for payload, message in (
        (b"{}\n", "not a valid snapshot"),
        # Whitespace-only changes: the same JSON value with other bytes.
        (json.dumps(json.loads(original), indent=2).encode() + b"\n", "not canonical"),
        (original.rstrip(b"\n"), "not canonical"),
        (original + b" ", "not canonical"),
        (snapshot_file(store, other).read_bytes(), "carries digest"),
    ):
        path.write_bytes(payload)
        for call in (
            lambda: store.materialize(reference),
            lambda: read_catalog_snapshot(store.layout, reference),
        ):
            with pytest.raises(CorpusSnapshotSecurityError, match="published bytes") as caught:
                call()
            assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
            assert isinstance(caught.value, SecurityError)
        # A reference forged to pin the altered bytes still fails the structural checks
        # in the file-only reader and the row comparison in the store.
        forged = reference.model_copy(update={"snapshot_sha256": file_sha256(payload)})
        with pytest.raises(CorpusSnapshotSecurityError, match=message) as caught:
            read_catalog_snapshot(store.layout, forged)
        assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
        with pytest.raises(CorpusSnapshotSecurityError, match="disagrees with the published"):
            store.materialize(forged)
    path.write_bytes(original)
    assert store.materialize(reference).catalog_sha256 == reference.catalog_sha256


def test_alias_only_alteration_of_a_snapshot_is_a_security_error(store: CorpusStore) -> None:
    first, second = seeded(store, 2)
    kept = store.edit_prompt("prompt-01", text="Sentence number 1!", keep_recordings=True)
    reference = published(store)
    store.activate(SUBJECT, reference)
    path = snapshot_file(store, reference)
    original = path.read_bytes()
    snapshot = store.materialize(reference)
    assert snapshot.prompt_aliases == (
        PromptAlias(prompt_sha256=kept.prompt_sha256, accepted_prompt_sha256=first.prompt_sha256),
    )
    forged_alias = PromptAlias(prompt_sha256=second.prompt_sha256, accepted_prompt_sha256="f" * 64)
    for aliases in (
        (),
        tuple(sorted((*snapshot.prompt_aliases, forged_alias), key=lambda a: a.prompt_sha256)),
        (forged_alias,),
    ):
        rewritten = CatalogSnapshot(
            suite=snapshot.suite, catalog_sha256=snapshot.catalog_sha256, prompt_aliases=aliases
        )
        payload = canonical_json_bytes(rewritten) + b"\n"
        assert rewritten.catalog_sha256 == reference.catalog_sha256
        path.write_bytes(payload)
        for call in (
            lambda: store.materialize(reference),
            lambda: store.activate("other-subject", reference),
            lambda: store.active_catalog(SUBJECT),
            lambda: read_catalog_snapshot(store.layout, reference),
        ):
            with pytest.raises(CorpusSnapshotSecurityError, match="published bytes") as caught:
                call()
            assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
        # The file-only reader has no rows to compare against, so a reference forged to
        # match the rewritten file passes it: the reference itself is the trust anchor and
        # lives in the store row and in sealed group evidence. The store compares rows.
        forged = reference.model_copy(update={"snapshot_sha256": file_sha256(payload)})
        assert read_catalog_snapshot(store.layout, forged).prompt_aliases == aliases
        with pytest.raises(CorpusSnapshotSecurityError, match="disagrees with the published"):
            store.materialize(forged)
    with pytest.raises(CorpusStoreError, match="no active catalog"):
        store.active_catalog("other-subject")
    path.write_bytes(original)
    assert store.materialize(reference) == snapshot
    assert store.active_catalog(SUBJECT) == (snapshot.suite, reference)


def test_missing_snapshot_is_a_security_error(store: CorpusStore) -> None:
    seeded(store, 1)
    reference = published(store)
    snapshot_file(store, reference).unlink()
    with pytest.raises(CorpusSnapshotSecurityError, match="missing") as caught:
        store.materialize(reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"
    assert isinstance(caught.value, SecurityError)
    with pytest.raises(CorpusSnapshotSecurityError, match="missing") as caught:
        read_catalog_snapshot(store.layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"


def test_file_reader_refuses_symlinks_links_open_modes_and_oversized_files(
    store: CorpusStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(store, 1)
    reference = published(store)
    path = snapshot_file(store, reference)
    linked = tmp_path / "hard-link.json"
    os.link(path, linked)
    with pytest.raises(CorpusSnapshotSecurityError, match="owned private regular file") as caught:
        read_catalog_snapshot(store.layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    linked.unlink()
    path.chmod(0o644)
    with pytest.raises(CorpusSnapshotSecurityError, match="owned private regular file"):
        read_catalog_snapshot(store.layout, reference)
    path.chmod(0o600)
    monkeypatch.setattr(corpus_store, "MAX_CATALOG_SNAPSHOT_BYTES", 16)
    with pytest.raises(CorpusSnapshotSecurityError, match="exceeds its byte limit"):
        read_catalog_snapshot(store.layout, reference)
    monkeypatch.undo()
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(CorpusSnapshotSecurityError, match="symlink component") as caught:
        read_catalog_snapshot(store.layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(CorpusSnapshotSecurityError, match="not a regular file") as caught:
        read_catalog_snapshot(store.layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    path.rmdir()
    path.write_bytes(outside.read_bytes())
    path.chmod(0o000)
    with pytest.raises(CorpusSnapshotSecurityError, match="cannot be opened") as caught:
        read_catalog_snapshot(store.layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    path.chmod(0o600)
    assert read_catalog_snapshot(store.layout, reference).catalog_sha256 == reference.catalog_sha256
    other_root = AssetLayout.initialize(tmp_path / "other-assets")
    (other_root.root / "provenance" / "corpus").mkdir(mode=0o700)
    (other_root.root / "provenance" / "corpus" / "snapshots").symlink_to(store.snapshot_directory)
    with pytest.raises(CorpusSnapshotSecurityError, match="symlink component") as caught:
        read_catalog_snapshot(other_root, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    (other_root.root / "provenance" / "corpus" / "snapshots").unlink()
    (other_root.root / "provenance" / "corpus" / "snapshots").write_bytes(b"")
    with pytest.raises(CorpusSnapshotSecurityError, match="cannot be examined") as caught:
        read_catalog_snapshot(other_root, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


def _swap_after_lstat(
    monkeypatch: pytest.MonkeyPatch, target: Path, swap: Callable[[Path], None]
) -> None:
    """Replace ``target`` right after the reader's own lstat of it, before its open."""
    real_lstat = os.lstat

    def lstat_then_swap(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        metadata = real_lstat(path, *args, **kwargs)
        # Path.lstat in confined_path's walk also lands here; only the reader's own call
        # (made from the corpus_store module) is the check whose window is under test.
        caller = sys._getframe(1).f_globals.get("__name__")
        if caller == corpus_store.__name__ and Path(path) == target:
            monkeypatch.setattr(corpus_store.os, "lstat", real_lstat)
            swap(target)
        return metadata

    monkeypatch.setattr(corpus_store.os, "lstat", lstat_then_swap)


def _swap_to_fifo(path: Path) -> None:
    path.unlink()
    os.mkfifo(path, 0o600)


def _swap_to_symlink(path: Path) -> None:
    outside = path.parent.parent / "swapped-outside.json"
    outside.write_bytes(path.read_bytes())
    outside.chmod(0o600)
    path.unlink()
    path.symlink_to(outside)


@pytest.mark.parametrize(
    ("swap", "code", "message"),
    [
        (_swap_to_fifo, "CORPUS.SNAPSHOT_MISMATCH", "owned private regular file"),
        (Path.unlink, "CORPUS.SNAPSHOT_MISSING", "is missing"),
        (_swap_to_symlink, "CORPUS.SNAPSHOT_MISMATCH", "cannot be opened"),
    ],
    ids=["fifo", "deleted", "symlink"],
)
def test_snapshot_swapped_between_the_file_readers_check_and_open_fails_closed(
    store: CorpusStore,
    monkeypatch: pytest.MonkeyPatch,
    swap: Callable[[Path], None],
    code: str,
    message: str,
) -> None:
    seeded(store, 1)
    reference = published(store)
    _swap_after_lstat(monkeypatch, snapshot_file(store, reference), swap)
    with pytest.raises(CorpusSnapshotSecurityError, match=message) as caught:
        read_catalog_snapshot(store.layout, reference)
    assert caught.value.code == code


@pytest.mark.parametrize("snapshot_path", ["/etc/hosts", "../../../../../etc/hosts"])
def test_unvalidated_reference_with_an_escaping_path_is_a_named_mismatch(
    store: CorpusStore, snapshot_path: str
) -> None:
    seeded(store, 1)
    forged = published(store).model_copy(update={"snapshot_path": snapshot_path})
    with pytest.raises(CorpusSnapshotSecurityError, match="cannot be examined") as caught:
        read_catalog_snapshot(store.layout, forged)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


def test_snapshot_files_are_content_addressed_and_never_overwritten(store: CorpusStore) -> None:
    seeded(store, 1)
    reference = published(store)
    path = snapshot_file(store, reference)
    with raw(store) as attacker:
        attacker.execute("DELETE FROM corpus_catalog_version_prompts")
        attacker.execute("DELETE FROM corpus_catalog_versions")
    # The row is gone but the file survived (as after a crash between the write and the
    # commit): republishing the same content reuses it untouched.
    identity = path.stat()
    assert store.publish_version(CATALOG) == reference
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (identity.st_ino, identity.st_mtime_ns)
    with raw(store) as attacker:
        attacker.execute("DELETE FROM corpus_catalog_version_prompts")
        attacker.execute("DELETE FROM corpus_catalog_versions")
    path.write_bytes(b"{}\n")
    with pytest.raises(
        CorpusSnapshotSecurityError, match="differs from the content it names"
    ) as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert path.read_bytes() == b"{}\n"
    assert store.list_catalogs()[0].versions == ()
    assert [name for name in os.listdir(store.snapshot_directory) if name.startswith(".")] == []


# -- the group catalog reader --------------------------------------------------------------


def group_for(catalog: PromptSuite | CatalogReference) -> RecordingGroup:
    """A capture group that embeds ``catalog`` (v1) or references it (v2)."""
    embedded = isinstance(catalog, PromptSuite)
    purpose = catalog.purpose
    return RecordingGroup(
        schema_version=RECORDING_GROUP_V1 if embedded else RECORDING_GROUP_V2,
        group_id="group-synthetic",
        context_revision=0,
        subject_id=SUBJECT,
        catalog=catalog if embedded else None,
        catalog_reference=None if embedded else catalog,
        catalog_sha256=canonical_sha256(catalog) if embedded else catalog.catalog_sha256,
        capture_device=CaptureDevice(
            backend="coreaudio", device_id="synthetic", display_name="Synthetic UMIK-1"
        ),
        capture_host="synthetic-host",
        opened_at=EPOCH,
        reason="app-start",
        gate=PreparedCaptureGate(
            state="unprepared" if purpose == "capture-corpus" else "not-required"
        ),
    )


def test_group_catalog_is_the_embedded_suite_or_the_referenced_snapshot_without_sqlite(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = seeded(store, 2)
    store.edit_prompt("prompt-01", text="Sentence number 1!", keep_recordings=True)
    reference = published(store)
    published_snapshot = store.materialize(reference)
    builtin = load_builtin_prompt_suite("pilot-v2.json")

    def no_database(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the group catalog reader opened a database")

    monkeypatch.setattr(sqlite3, "connect", no_database)
    embedded = resolve_group_catalog(store.layout, group_for(builtin))
    assert embedded == CatalogSnapshot.embedded(builtin)
    assert embedded.prompt_aliases == () and embedded.suite == builtin
    assert embedded.catalog_sha256 == BUILTIN_PROMPT_SUITE_SHA256["pilot-v2.json"]
    referenced = resolve_group_catalog(store.layout, group_for(reference))
    assert referenced == published_snapshot and referenced.prompt_aliases
    current = referenced.prompt("prompt-01")
    assert current is not None and current.text == "Sentence number 1!"
    # The recorded first revision counts for the current prompt under the same id only.
    assert referenced.current_prompt_for(first.prompt) == current
    assert referenced.current_prompt_for(current) == current
    assert referenced.current_prompt_for(first.prompt.model_copy(update={"prompt_id": "x"})) is None
    assert referenced.prompt("prompt-99") is None
    assert embedded.current_prompt_for(first.prompt) is None


def test_group_catalog_reference_fails_closed_when_its_snapshot_is_missing_or_altered(
    store: CorpusStore,
) -> None:
    seeded(store, 2)
    reference = published(store)
    group = group_for(reference)
    path = snapshot_file(store, reference)
    original = path.read_bytes()
    path.write_bytes(original.replace(b"Sentence number 1.", b"Sentence number 7."))
    with pytest.raises(CorpusSnapshotSecurityError, match="published bytes") as caught:
        resolve_group_catalog(store.layout, group)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert isinstance(caught.value, SecurityError)
    path.unlink()
    with pytest.raises(CorpusSnapshotSecurityError, match="missing") as caught:
        resolve_group_catalog(store.layout, group)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"
    assert isinstance(caught.value, SecurityError)
    path.write_bytes(original)
    path.chmod(0o600)
    assert resolve_group_catalog(store.layout, group) == store.materialize(reference)


def test_an_in_memory_snapshot_must_be_exactly_the_file_its_reference_seals(
    store: CorpusStore,
) -> None:
    seeded(store, 2)
    first = published(store)
    store.edit_prompt("prompt-01", text="Sentence number 1!", keep_recordings=True)
    second = store.publish_version(CATALOG)
    one, two = store.materialize(first), store.materialize(second)
    assert one.prompt_aliases == () and two.prompt_aliases
    require_referenced_snapshot(one, first)
    require_referenced_snapshot(two, second)
    # A plain suite stands for a published version only when that version accepts no alias.
    require_referenced_snapshot(CatalogSnapshot.embedded(one.suite), first)
    renamed = first.model_copy(update={"suite_id": "another-suite"})
    repurposed = first.model_copy(update={"purpose": "capture-pilot"})
    for snapshot, reference in (
        (CatalogSnapshot.embedded(two.suite), second),  # its aliases would be dropped
        (two, first),
        (one, second),
        (one, renamed),
        (one, repurposed),
    ):
        with pytest.raises(CorpusSnapshotSecurityError) as caught:
            require_referenced_snapshot(snapshot, reference)
        assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
        assert isinstance(caught.value, SecurityError)


def test_group_catalog_cache_verifies_each_catalog_once_and_evicts_the_oldest(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="at least one"):
        GroupCatalogCache(store.layout, capacity=0)
    seeded(store, 2)
    reference = published(store)
    referenced, embedded = (
        group_for(reference),
        group_for(load_builtin_prompt_suite("pilot-v2.json")),
    )
    # An embedded suite and a snapshot of it never share an entry; the sealed reference counts.
    assert GroupCatalogCache.key(embedded) == (embedded.catalog_sha256, None)
    assert GroupCatalogCache.key(referenced) == (reference.catalog_sha256, reference)
    reads: list[str] = []
    real = corpus_store.resolve_group_catalog

    def counted(layout: Any, group: RecordingGroup) -> CatalogSnapshot:
        reads.append(group.catalog_sha256)
        return real(layout, group)

    monkeypatch.setattr(corpus_store, "resolve_group_catalog", counted)
    cache = GroupCatalogCache(store.layout, capacity=1)
    assert cache.resolve(referenced) == store.materialize(reference)
    snapshot_file(store, reference).unlink()
    # Verified bytes are served from the cache; a new owner detects the missing file.
    assert cache.resolve(referenced).catalog_sha256 == reference.catalog_sha256
    assert reads == [reference.catalog_sha256]
    with pytest.raises(CorpusSnapshotSecurityError, match="missing"):
        GroupCatalogCache(store.layout).resolve(referenced)
    assert cache.resolve(embedded).prompt_aliases == ()
    with pytest.raises(CorpusSnapshotSecurityError, match="missing"):
        cache.resolve(referenced)  # evicted by the embedded catalog, so read again
    assert cache.resolve(embedded).catalog_sha256 == embedded.catalog_sha256
    assert reads.count(embedded.catalog_sha256) == 1


def test_a_reference_resolved_before_its_group_exists_shares_the_group_entry(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(store, 2)
    reference = published(store)
    reads: list[CatalogReference] = []
    real = corpus_store.read_catalog_snapshot

    def counted(layout: Any, named: CatalogReference) -> CatalogSnapshot:
        reads.append(named)
        return real(layout, named)

    monkeypatch.setattr(corpus_store, "read_catalog_snapshot", counted)
    cache = GroupCatalogCache(store.layout)
    missing = reference.model_copy(update={"catalog_version": 2})
    snapshot = cache.resolve_reference(reference)
    assert snapshot == store.materialize(reference)
    # The group minted on that reference is served from the same entry.
    assert cache.resolve(group_for(reference)) is snapshot
    assert cache.resolve_reference(reference) is snapshot
    assert reads == [reference]
    forged = reference.model_copy(update={"suite_id": "another-suite"})
    with pytest.raises(CorpusSnapshotSecurityError, match="declares suite"):
        cache.resolve_reference(forged)
    snapshot_file(store, reference).unlink()
    # A failed read caches nothing; a verified entry keeps its checked bytes.
    for _ in range(2):
        with pytest.raises(CorpusSnapshotSecurityError, match="missing"):
            cache.resolve_reference(missing)
    assert reads == [reference, forged, missing, missing]
    assert cache.resolve_reference(reference) is snapshot


@pytest.mark.parametrize(
    "changes",
    [{"suite_id": "another-suite"}, {"purpose": "capture-pilot"}],
    ids=["suite-id", "purpose"],
)
def test_warm_cache_still_refuses_a_reference_that_disagrees_with_a_verified_snapshot(
    store: CorpusStore, changes: dict[str, str]
) -> None:
    seeded(store, 2)
    reference = published(store)
    cache = GroupCatalogCache(store.layout)
    assert cache.resolve(group_for(reference)) == store.materialize(reference)
    # Same suite digest and snapshot bytes, but a reference the file does not repeat.
    forged = group_for(reference.model_copy(update=changes))
    assert GroupCatalogCache.key(forged) != GroupCatalogCache.key(group_for(reference))
    with pytest.raises(CorpusSnapshotSecurityError, match="declares suite") as caught:
        cache.resolve(forged)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert cache.resolve(group_for(reference)) == store.materialize(reference)


# -- hash stability against the released built-in -------------------------------------


def test_seeded_pilot_v2_spoken_prompts_hash_exactly_like_the_built_in(store: CorpusStore) -> None:
    builtin = load_builtin_prompt_suite("pilot-v2.json")
    spoken = [prompt for prompt in builtin.prompts if prompt.spoken]
    assert len(spoken) == 34
    for prompt in spoken:
        record = store.add_prompt(
            prompt.prompt_id,
            text=prompt.text,
            category=prompt.category,
            style=prompt.style,
            expected_seconds=prompt.expected_seconds,
            reference_candidate=prompt.reference_candidate,
            holdout=prompt.holdout,
            source="pilot-v2",
        )
        assert record.prompt == prompt
        assert record.prompt_sha256 == canonical_sha256(prompt)
    assert len(store.list_prompts()) == 34


def test_publishing_every_pilot_v2_prompt_reproduces_the_released_catalog_digest(
    store: CorpusStore,
) -> None:
    builtin = load_builtin_prompt_suite("pilot-v2.json")
    assert len(builtin.prompts) == 36
    assert store.import_prompt_suite(builtin, source="pilot-v2") == tuple(
        prompt.prompt_id for prompt in builtin.prompts
    )
    store.create_catalog("pilot-v2-mirror", suite_id=builtin.suite_id, purpose=builtin.purpose)
    reference = store.publish_version("pilot-v2-mirror")
    assert reference.catalog_sha256 == BUILTIN_PROMPT_SUITE_SHA256["pilot-v2.json"]
    assert reference.suite_id == "cole-pilot-v2"
    snapshot = store.materialize(reference)
    assert snapshot.suite == builtin
    assert canonical_json_bytes(snapshot.suite) == canonical_json_bytes(builtin)


# -- concurrency ------------------------------------------------------------------------


def _publish_from_child(root: str, prompt_id: str, barrier: Any, results: Any) -> None:
    layout = AssetLayout.validate(Path(root))
    try:
        with open_corpus_store(layout, create=False) as store:
            barrier.wait(timeout=60)
            reference = store.publish_version(CATALOG, prompt_ids=("shared-01", prompt_id))
            results.put(("published", reference.catalog_version, reference.catalog_sha256))
    except Exception as error:  # The parent asserts on whatever happened.
        results.put(("failed", repr(error)))


def test_two_processes_publishing_at_once_queue_on_the_lock_and_get_consecutive_versions(
    layout: AssetLayout,
) -> None:
    with open_corpus_store(layout, create=True) as store:
        add(store, "shared-01", "A sentence both children publish.")
        add(store, "left-01", "The left child's sentence.")
        add(store, "right-01", "The right child's sentence.")
        store.create_catalog(CATALOG, purpose="capture-corpus")
        store.publish_version(CATALOG, prompt_ids=("shared-01",))
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    children = [
        context.Process(
            target=_publish_from_child, args=(str(layout.root), prompt_id, barrier, results)
        )
        for prompt_id in ("left-01", "right-01")
    ]
    for child in children:
        child.start()
    outcomes = [results.get(timeout=120) for _ in children]
    for child in children:
        child.join(timeout=120)
        assert child.exitcode == 0
    assert sorted(outcome[0] for outcome in outcomes) == ["published", "published"]
    assert sorted(outcome[1] for outcome in outcomes) == [2, 3]
    assert len({outcome[2] for outcome in outcomes}) == 2
    with open_corpus_store(layout, create=False) as store:
        versions = store.list_catalogs()[0].versions
        assert [reference.catalog_version for reference in versions] == [1, 2, 3]
        assert len({reference.catalog_sha256 for reference in versions}) == 3
        for reference in versions:
            store.materialize(reference)
        with raw(store) as attacker:
            assert attacker.execute("PRAGMA quick_check").fetchone()[0] == "ok"

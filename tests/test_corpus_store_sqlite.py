"""The SQLite corpus store: guarded open, locks, leases, migrations, clock and tampering.

Checks of ``corpus_store_sqlite.CorpusStore`` beyond the catalog ports: they construct the
store directly (with a deterministic clock where timestamps matter), reach its files, rows
and private members, and tamper with them. The port-level behaviour is in
``catalog_conformance.py``, whose fixtures and helpers these tests reuse.
"""

from __future__ import annotations

import errno
import fcntl
import json
import multiprocessing
import os
import sqlite3
import stat
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from catalog_conformance import (
    CATALOG,
    EPOCH,
    SUBJECT,
    add,
    file_sha256,
    mode,
    published,
    race_publishers,
    seeded,
    snapshot_file,
)
from catalog_conformance import layout as layout

import cole_voice_lab.corpus_store as corpus_store
import cole_voice_lab.corpus_store_sqlite as corpus_store_sqlite
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.catalog_ports import (
    CorpusSnapshotSecurityError,
    CorpusStoreError,
    CorpusStoreSecurityError,
)
from cole_voice_lab.corpus_store import STORE_MISSING_MESSAGE, read_catalog_snapshot
from cole_voice_lab.corpus_store_sqlite import (
    CORPUS_DATABASE_NAME,
    CORPUS_DIRECTORY,
    CORPUS_MIGRATION_NAME,
    CorpusStore,
    corpus_store_migration,
    corpus_store_migration_resources,
)
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import BUILTIN_PROMPT_SUITE_SHA256, load_builtin_prompt_suite
from cole_voice_lab.recording_contracts import (
    CATALOG_SNAPSHOT_ROOT,
    CatalogReference,
    CatalogSnapshot,
    PromptAlias,
    catalog_snapshot_path,
)
from cole_voice_lab.sqlite_migrations import migration_bytes


class Clock:
    """Deterministic, strictly increasing UTC timestamps."""

    def __init__(self, start: datetime = EPOCH) -> None:
        self.current = start

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


@pytest.fixture
def store(layout: AssetLayout) -> Iterator[CorpusStore]:
    with CorpusStore(layout, create=True, clock=Clock()) as opened:
        yield opened


def raw(store: CorpusStore) -> sqlite3.Connection:
    """An attacker's connection: the same file, none of the store's or schema's guards."""
    connection = sqlite3.connect(store.path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA ignore_check_constraints = ON")
    return connection


# -- migration resources -----------------------------------------------------------------


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
    with CorpusStore(layout, create=True) as store:
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
    with CorpusStore(layout, create=False) as store:
        assert [record.revision for record in store.list_prompts()] == [1]
        with raw(store) as attacker:
            attacker.execute("UPDATE corpus_prompts SET prompt_current_revision = 9")
    with pytest.raises(CorpusStoreError, match="references are inconsistent") as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


# -- opening the store --------------------------------------------------------------


def test_opening_without_create_needs_an_existing_database(layout: AssetLayout) -> None:
    with pytest.raises(CorpusStoreError) as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_MISSING"
    assert str(caught.value) == STORE_MISSING_MESSAGE
    assert "voice-lab catalog init" in STORE_MISSING_MESSAGE
    assert f"at provenance/{CORPUS_DIRECTORY}/{CORPUS_DATABASE_NAME};" in STORE_MISSING_MESSAGE
    corpus = layout.root / "provenance" / "corpus"
    assert not corpus.exists()
    corpus.mkdir(mode=0o700)
    with pytest.raises(CorpusStoreError) as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_MISSING"
    assert sorted(path.name for path in corpus.iterdir()) == []


def test_create_makes_private_members_and_a_second_open_reuses_them(layout: AssetLayout) -> None:
    with CorpusStore(layout, create=True) as store:
        seeded(store, 1)
        assert store.directory == layout.root / "provenance" / "corpus"
        assert store.path == store.directory / CORPUS_DATABASE_NAME
        assert mode(store.directory) == 0o700
        assert mode(store.snapshot_directory) == 0o700
        for name in (CORPUS_DATABASE_NAME, ".corpus.lease", ".corpus.lock"):
            assert mode(store.directory / name) == 0o600, name
        assert mode(store.path.with_name(CORPUS_DATABASE_NAME + "-wal")) == 0o600
    with CorpusStore(layout, create=True) as again:
        assert [record.prompt.prompt_id for record in again.list_prompts()] == ["prompt-01"]
    with CorpusStore(layout, create=False) as third:
        assert [record.prompt.prompt_id for record in third.list_prompts()] == ["prompt-01"]


def test_database_vanishing_after_the_presence_check_is_still_missing(
    layout: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    (layout.root / "provenance" / "corpus").mkdir(mode=0o700)
    monkeypatch.setattr(CorpusStore, "_require_database_present", lambda self: None)
    with pytest.raises(CorpusStoreError) as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_MISSING"
    assert not (layout.root / "provenance" / "corpus" / CORPUS_DATABASE_NAME).exists()


def test_database_replaced_between_binding_stats_is_refused(
    layout: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    with CorpusStore(layout, create=True):
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
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNSAFE"


def test_unsupported_schema_version_and_unversioned_content_fail_closed(
    layout: AssetLayout,
) -> None:
    with CorpusStore(layout, create=True) as store:
        path = store.path
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute("PRAGMA user_version = 7")
    with pytest.raises(
        CorpusStoreError, match="unsupported corpus store schema version: 7"
    ) as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute("PRAGMA user_version = 0")
    with pytest.raises(CorpusStoreError, match="unversioned nonempty database") as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_corrupt_database_bytes_are_reported_never_repaired(layout: AssetLayout) -> None:
    with CorpusStore(layout, create=True) as store:
        path = store.path
    path.write_bytes(b"not a database at all")
    with pytest.raises(CorpusStoreError, match="cannot open corpus store") as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    assert path.read_bytes() == b"not a database at all"


def test_structural_integrity_failure_is_a_named_error(layout: AssetLayout) -> None:
    with CorpusStore(layout, create=True) as store:
        seeded(store, 3)
        path = store.path
    # Header bytes 36-39 count freelist pages; claiming one where none exists is a
    # structural inconsistency quick_check reports instead of raising.
    data = bytearray(path.read_bytes())
    assert not path.with_name(path.name + "-wal").exists()
    data[36:40] = (1).to_bytes(4, "big")
    path.write_bytes(bytes(data))
    with pytest.raises(CorpusStoreError, match="structural integrity") as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_inconsistent_references_are_a_named_error(layout: AssetLayout) -> None:
    with CorpusStore(layout, create=True) as store:
        seeded(store, 1)
        with raw(store) as attacker:
            attacker.execute(
                "INSERT INTO corpus_prompt_aliases VALUES ('prompt-01', 9, ?)", ("0" * 64,)
            )
    with pytest.raises(CorpusStoreError, match="references are inconsistent") as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_unexpected_failure_during_open_releases_the_lease(
    layout: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    with CorpusStore(layout, create=True):
        pass

    def refuse(*_: Any, **__: Any) -> None:
        raise RuntimeError("probe")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    with pytest.raises(RuntimeError, match="probe"):
        CorpusStore(layout, create=False)
    descriptor = os.open(layout.root / "provenance" / "corpus" / ".corpus.lease", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_unsafe_directory_mode_fails_closed_without_repair(layout: AssetLayout) -> None:
    with CorpusStore(layout, create=True) as store:
        directory = store.directory
    directory.chmod(0o755)
    with pytest.raises(CorpusStoreSecurityError, match="owned private directory") as caught:
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNSAFE"
    assert mode(directory) == 0o755


@pytest.mark.parametrize("name", ["corpus", ".corpus.lock", ".corpus.lease", "snapshots"])
def test_symlinked_store_member_is_a_security_error_not_an_outage(
    layout: AssetLayout, tmp_path: Path, name: str
) -> None:
    with CorpusStore(layout, create=True):
        pass
    directory = layout.root / "provenance" / "corpus"
    member = directory if name == "corpus" else directory / name
    real = tmp_path / f"{name}-real"
    os.rename(member, real)
    member.symlink_to(real)
    for create in (False, True):
        with pytest.raises(CorpusStoreSecurityError, match="owned private") as caught:
            CorpusStore(layout, create=create)
        assert caught.value.code == "CORPUS.STORE_UNSAFE"
        assert isinstance(caught.value, SecurityError)
    assert member.is_symlink()


def test_open_discards_snapshot_scratch_a_crashed_publish_left(layout: AssetLayout) -> None:
    with CorpusStore(layout, create=True, clock=Clock()) as store:
        seeded(store, 1)
        reference = published(store)
        path = snapshot_file(store.layout, reference)
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
    with CorpusStore(layout, create=False) as again:
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
        CorpusStore(layout, create=False)
    assert caught.value.code == "CORPUS.STORE_UNSAFE"
    assert bogus.is_dir()


def test_naive_clock_is_a_caller_error_at_open_and_at_every_write(layout: AssetLayout) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CorpusStore(layout, create=True, clock=lambda: datetime(2026, 1, 1))
    assert not (layout.root / "provenance" / "corpus").exists()
    readings = iter((EPOCH, datetime(2026, 1, 1)))
    with CorpusStore(layout, create=True, clock=lambda: next(readings)) as store:
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


def test_a_new_store_on_btrfs_opens_and_its_checks_see_strays_made_since_the_last(
    btrfs_directory: Path,
) -> None:
    """#112: the store listed its directories through the descriptors it opened them with,
    and btrfs lists through a descriptor only what was there when it was opened or when the
    previous listing through it ended. A new store refused the database it had just made,
    and the check before a query missed a stray made since the last check."""
    layout = AssetLayout.initialize(btrfs_directory / "private-assets")
    with CorpusStore(layout, create=True, clock=Clock()) as store:
        seeded(store, 1)
        for directory, refusal in (
            (store.directory, "unexpected corpus store member"),
            (store.snapshot_directory, "unexpected catalog snapshot member"),
        ):
            stray = directory / "notes.txt"
            stray.write_bytes(b"")
            with pytest.raises(CorpusStoreSecurityError, match=refusal):
                store._check_paths()  # the check a query starts with
            stray.unlink()
        assert [record.prompt.prompt_id for record in store.list_prompts()] == ["prompt-01"]


def test_snapshot_with_open_permissions_is_refused_before_it_is_read(store: CorpusStore) -> None:
    seeded(store)
    reference = published(store)
    snapshot_file(store.layout, reference).chmod(0o644)
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
    path = snapshot_file(store.layout, reference)
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


def test_added_prompt_row_takes_the_clock_reading_after_the_open_check_and_the_first_ordinal(
    store: CorpusStore,
) -> None:
    record = add(
        store,
        "warm-01",
        "We can take this one step at a time.",
        category="warm-narration",
        labels=("gentle",),
        reference_candidate=True,
    )
    # The clock's first reading is the open-time awareness check.
    assert record.created_at == record.revision_created_at == EPOCH + timedelta(seconds=2)
    with raw(store) as reader:
        ordinals = reader.execute("SELECT prompt_insertion_ordinal FROM corpus_prompts").fetchall()
    assert [row[0] for row in ordinals] == [0]


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


def test_a_failed_activation_stores_no_part_of_an_initialization_but_may_leave_its_snapshot(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = load_builtin_prompt_suite("pilot-v2.json")
    pin = BUILTIN_PROMPT_SUITE_SHA256["pilot-v2.json"]
    definition = corpus_store.catalog_definition(
        "pilot-mirror", suite_id=seed.suite_id, purpose=seed.purpose
    )

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
    _, reference = store.initialize_catalog(definition, seed=seed, subject_id=SUBJECT)
    assert reference is not None and reference.catalog_sha256 == pin
    assert list(store.snapshot_directory.iterdir()) == leftover


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


def test_published_version_rows_number_their_prompts_in_catalog_order(store: CorpusStore) -> None:
    seeded(store, 3)
    published(store)
    with raw(store) as reader:
        ordinals = reader.execute(
            "SELECT catalog_prompt_ordinal FROM corpus_catalog_version_prompts "
            "ORDER BY catalog_prompt_ordinal"
        ).fetchall()
    assert [row[0] for row in ordinals] == [0, 1, 2]


def test_publishing_validates_what_a_row_written_outside_the_api_materializes_and_its_size(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create_catalog(CATALOG, purpose="capture-corpus")
    seeded(store, 2)
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
    monkeypatch.setattr(corpus_store_sqlite, "MAX_CATALOG_PROMPTS", 1)
    with pytest.raises(CorpusStoreError, match="exceeds 1 prompts") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    monkeypatch.undo()
    # The publish-time byte bound is the same bound every reader enforces, so a version
    # that publishes can always be read back.
    monkeypatch.setattr(corpus_store_sqlite, "MAX_CATALOG_SNAPSHOT_BYTES", 64)
    with pytest.raises(CorpusStoreError, match="snapshot would exceed 64 bytes") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    assert store.list_catalogs()[0].versions == ()
    assert list(store.snapshot_directory.iterdir()) == []


def test_failed_snapshot_write_leaves_no_scratch_and_no_version(
    store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(store, 1)
    store.create_catalog(CATALOG, purpose="capture-corpus")

    def refuse(*_: Any, **__: Any) -> None:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(corpus_store_sqlite.os, "link", refuse)
    with pytest.raises(CorpusStoreError, match="query failed.*no space") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    monkeypatch.undo()
    assert os.listdir(store.snapshot_directory) == []
    assert store.list_catalogs()[0].versions == ()
    assert store.publish_version(CATALOG).catalog_version == 1


def test_activation_refuses_a_version_whose_snapshot_file_is_gone(store: CorpusStore) -> None:
    seeded(store, 2)
    reference = published(store)
    snapshot_file(store.layout, reference).unlink()
    with pytest.raises(CorpusSnapshotSecurityError, match="missing") as caught:
        store.activate("third-subject", reference)
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


def test_altered_snapshot_is_a_security_error(store: CorpusStore) -> None:
    seeded(store, 2)
    reference = published(store)
    path = snapshot_file(store.layout, reference)
    original = path.read_bytes()
    store.create_catalog("other", suite_id="other", purpose="capture-corpus")
    other = store.publish_version("other")
    for payload, message in (
        (b"{}\n", "not a valid snapshot"),
        # Whitespace-only changes: the same JSON value with other bytes.
        (json.dumps(json.loads(original), indent=2).encode() + b"\n", "not canonical"),
        (original.rstrip(b"\n"), "not canonical"),
        (original + b" ", "not canonical"),
        (snapshot_file(store.layout, other).read_bytes(), "carries digest"),
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
    path = snapshot_file(store.layout, reference)
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
    snapshot_file(store.layout, reference).unlink()
    with pytest.raises(CorpusSnapshotSecurityError, match="missing") as caught:
        store.materialize(reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"
    assert isinstance(caught.value, SecurityError)
    with pytest.raises(CorpusSnapshotSecurityError, match="missing") as caught:
        read_catalog_snapshot(store.layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"


def test_snapshot_files_are_content_addressed_and_never_overwritten(store: CorpusStore) -> None:
    seeded(store, 1)
    reference = published(store)
    path = snapshot_file(store.layout, reference)
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


# -- concurrency ------------------------------------------------------------------------


def test_concurrent_publishers_leave_a_database_that_passes_its_integrity_check(
    layout: AssetLayout,
) -> None:
    outcomes = race_publishers(layout)
    assert sorted(outcome[0] for outcome in outcomes) == ["published", "published"]
    with CorpusStore(layout, create=False) as store:
        assert [reference.catalog_version for reference in store.list_catalogs()[0].versions] == [
            1,
            2,
            3,
        ]
        with raw(store) as attacker:
            assert attacker.execute("PRAGMA quick_check").fetchone()[0] == "ok"

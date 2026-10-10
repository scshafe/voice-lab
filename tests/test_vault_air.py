"""The MacBook Air vault (design §4.3, phase 3): resume, cadence, staleness, memos and its tag.

The Air sleeps and travels, so its copy is built for intermittent reach: a push that stops
part-way resumes from the destination's journal of stored chunks; an hourly run pushes only
when the copy is a week old and does nothing (successfully) when the node is asleep; a
read-only report flags a copy older than 14 days; the phone's Voice Memos drop, outside the
asset root, is copied as its own opt-in ``memos`` class; and the Air's approval names its own
tag. The library tests use the sealed local vault of ``tests/vault_library_fixtures.py``; the
command tests use the fake Keychain, fake tailnet and loopback vault of
``tests/vault_binding_fixtures.py``. Everything is synthetic, built in ``tmp_path``: no real
library, credential, unit, tailnet peer or vault is touched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from vault_binding_fixtures import (
    LIBRARY_ID as BOUND_LIBRARY,
)
from vault_binding_fixtures import (
    TAILNET_SUFFIX,
    FakeResolver,
    add_library_records,
    approval_arguments,
    approval_record,
    approved_peer,
    install_vault_seams,
    running_vault,
    store_vault_token,
)
from vault_library_fixtures import (
    LIBRARY_ID,
    LOCAL_DESTINATION,
    TAILNET_DESTINATION,
    HookedBlobStore,
    Vault,
    add_records,
    private_directory,
    restore_permissions,
    write_file,
)

import cole_voice_lab.library_generations as generations
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.cli import build_parser, main
from cole_voice_lab.library_generations import (
    PushResult,
    ReplicationLedger,
    RestoreRefusedError,
    ResumeJournal,
    VaultRecords,
    push_generation,
    read_freshness,
    read_stored_manifest,
    rehash_restored_library,
    restore_library,
    verify_generation,
)
from cole_voice_lab.library_replication import (
    MemosSource,
    ReplicationSink,
    collect_pass,
    memos_source_path,
    require_classified_tree,
    subtree_stays_on_mac,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.storage_binding import (
    LibraryVault,
    VaultStores,
    open_vault_for_restore,
    probe_library_vault,
)
from cole_voice_lab.storage_contracts import (
    FileClass,
    LibraryManifest,
    ManifestEntry,
    ReplicationState,
    ResumeJournalEntry,
    StorageBinding,
    library_head_ref,
)
from cole_voice_lab.storage_ports import (
    BlobRef,
    RefStore,
    RefValue,
    StorageError,
    StorageIntegrityError,
    storage_error,
)

CHUNK = 4096
HEAD = library_head_ref(LIBRARY_ID)
LAB_CLASSES: tuple[FileClass, ...] = (
    "derived-edits",
    "prompt-database",
    "recordings",
    "records",
    "training",
)
WITH_MEMOS: tuple[FileClass, ...] = tuple(sorted((*LAB_CLASSES, "memos")))
ROOM = 2**62
REVIEWS = "provenance/headphone-reviews"
# Three new files of four pieces each (three whole chunks and a short last one).
BIG = {f"{REVIEWS}/big-{n}.json": bytes([0x40 + n]) * (3 * CHUNK + 100) for n in (1, 2, 3)}
JOURNAL = "provenance/vault/destinations/default/resume.jsonl"
MEMOS = {
    "20261009 101500.m4a": b"memo one" * 900,
    "20261009 101600.m4a": b"memo two\n",
    "CloudRecordings.db": b"synthetic metadata database\n",
    "_mirror/latest.json": b'{"recordings":2}\n',
}
MEMO_TEMPORARIES = (".20261009 101700.m4a.AbC123", ".rsync-partial/20261009 101800.m4a")


@pytest.fixture(autouse=True)
def _writable_tmp_path(tmp_path: Path) -> Iterator[None]:
    yield
    restore_permissions(tmp_path)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault.create(tmp_path.resolve() / "vault")


@pytest.fixture
def library(tmp_path: Path) -> AssetLayout:
    layout = AssetLayout.initialize(tmp_path.resolve() / "library" / "assets")
    add_records(layout)
    return layout


@pytest.fixture
def drop(tmp_path: Path) -> Path:
    """The phone's memos drop: group-readable files, as the mirror's account leaves them."""
    root = private_directory(tmp_path.resolve() / "voice-lab" / "memos" / "phone")
    for relative, data in MEMOS.items():
        write_file(root, relative, data, mode=0o640)
    for relative in MEMO_TEMPORARIES:
        write_file(root, relative, b"an rsync transfer in progress\n", mode=0o600)
    return root


def push(layout: AssetLayout, vault: Vault, **options: Any) -> PushResult:
    arguments: dict[str, Any] = {
        "blob_store": vault.blobs,
        "ref_store": vault.refs,
        "library_id": LIBRARY_ID,
        "destination": LOCAL_DESTINATION,
        "classes": LAB_CLASSES,
        "chunk_bytes": CHUNK,
        "clock": vault.clock,
    }
    return push_generation(layout, **(arguments | options))


def manifest_of(vault: Vault, head: RefValue | None) -> LibraryManifest:
    assert head is not None
    return read_stored_manifest(vault.blobs, head.target, library_id=LIBRARY_ID).manifest


def by_path(manifest: LibraryManifest) -> dict[str, ManifestEntry]:
    return {entry.path: entry for entry in manifest.entries}


def journal(layout: AssetLayout) -> list[ResumeJournalEntry]:
    path = layout.root / JOURNAL
    if not path.exists():
        return []
    return [ResumeJournalEntry.model_validate_json(line) for line in path.read_bytes().splitlines()]


def asleep_after(vault: Vault, count: int) -> HookedBlobStore:
    """A store that takes ``count`` blobs and then fails as an Air that went to sleep would."""
    hooked = HookedBlobStore(vault.local)

    def before_put(_: bytes) -> None:
        if len(hooked.puts) >= count:
            raise storage_error("STORAGE.UNAVAILABLE", "the Air went to sleep")

    hooked.before_put = before_put
    return hooked


def interrupted(layout: AssetLayout, vault: Vault, count: int) -> HookedBlobStore:
    """Add the three big files and push until the vault stops answering after ``count`` blobs."""
    for relative, data in BIG.items():
        write_file(layout.root, relative, data)
    hooked = asleep_after(vault, count)
    with pytest.raises(StorageError, match="the Air went to sleep"):
        push(layout, vault, blob_store=vault.sealed(hooked))
    return hooked


def lose(vault: Vault, ref: BlobRef) -> None:
    path = vault.blob_file(ref)
    mode = path.parent.stat().st_mode & 0o777
    path.parent.chmod(0o700)
    path.unlink()
    path.parent.chmod(mode)


def tamper(vault: Vault, ref: BlobRef) -> None:
    path = vault.blob_file(ref)
    mode = path.stat().st_mode & 0o777
    path.chmod(0o600)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))
    path.chmod(mode)


# -- the resume journal ----------------------------------------------------------------------


def test_an_interrupted_push_resumes_without_sending_what_it_stored(
    library: AssetLayout, vault: Vault
) -> None:
    first = push(library, vault)
    state = (library.root / "provenance/vault/destinations/default/state.json").read_bytes()
    hooked = interrupted(library, vault, 5)

    # The head never names a partial generation: the previous one is intact and current.
    assert vault.refs.read(HEAD) == first.head
    assert (library.root / "provenance/vault/destinations/default/state.json").read_bytes() == (
        state
    )
    assert verify_generation(
        library, blob_store=vault.blobs, ref_store=vault.refs, library_id=LIBRARY_ID
    ).generation == (first.generation)
    stored = journal(library)
    assert [entry.ref for entry in stored] == hooked.puts
    assert [(entry.file_sha256, entry.offset) for entry in stored[:4]] == [
        (stored[0].file_sha256, offset) for offset in (0, CHUNK, 2 * CHUNK, 3 * CHUNK)
    ]
    assert {entry.destination_sha256 for entry in stored} == {LOCAL_DESTINATION.digest}
    journal_path = library.root / JOURNAL
    assert journal_path.stat().st_mode & 0o777 == 0o600

    # The plan grows before the Air wakes: the next push still resumes.
    write_file(library.root, f"{REVIEWS}/grown.json", b'{"grown":true}\n')
    counting = HookedBlobStore(vault.local)
    second = push(library, vault, blob_store=vault.sealed(counting))

    assert (second.outcome, second.generation) == ("pushed", 2)
    assert (second.resumed_blobs, second.resumed_bytes) == (5, sum(ref.size for ref in hooked.puts))
    assert not set(counting.puts) & set(hooked.puts)
    assert set(hooked.puts) <= set(counting.gets)
    assert len(counting.puts) == second.sent_blobs
    pieces = {ref for entry in manifest_of(vault, second.head).entries for ref in entry.chunks}
    assert set(hooked.puts) <= pieces
    assert not journal_path.exists()
    assert ReplicationLedger(library).entries()[-1].kind == "upload-completed"
    report = verify_generation(
        library, blob_store=vault.blobs, ref_store=vault.refs, library_id=LIBRARY_ID
    )
    assert report.ok and report.generation == 2


def test_a_tampered_or_lost_piece_is_sent_again_and_a_changed_file_is_sealed_again(
    library: AssetLayout, vault: Vault
) -> None:
    push(library, vault)
    hooked = interrupted(library, vault, 6)
    big_1, big_2 = sorted(BIG)[:2]
    stored = journal(library)
    tamper(vault, stored[1].ref)
    lose(vault, stored[2].ref)
    write_file(library.root, big_2, b"\x7f" * (2 * CHUNK + 7))

    counting = HookedBlobStore(vault.local)
    second = push(library, vault, blob_store=vault.sealed(counting))

    entries = by_path(manifest_of(vault, second.head))
    assert second.resumed_blobs == 2
    assert entries[big_1].chunks[0] == stored[0].ref
    assert entries[big_1].chunks[3] == stored[3].ref
    assert stored[1].ref not in entries[big_1].chunks
    assert stored[2].ref not in entries[big_1].chunks
    # big-2 changed: its two journalled pieces belong to the old digest and are not reused.
    assert not {stored[4].ref, stored[5].ref} & set(entries[big_2].chunks)
    assert len(entries[big_2].chunks) == 3
    assert hooked.puts == [entry.ref for entry in stored]
    assert verify_generation(
        library, blob_store=vault.blobs, ref_store=vault.refs, library_id=LIBRARY_ID
    ).ok


def test_a_vault_that_cannot_answer_a_read_back_stops_the_push(
    library: AssetLayout, vault: Vault
) -> None:
    first = push(library, vault)
    interrupted(library, vault, 2)
    journalled = {entry.ref for entry in journal(library)}

    def asleep_again(ref: BlobRef) -> None:
        if ref in journalled:
            raise storage_error("STORAGE.TIMEOUT", "asleep again")

    away = HookedBlobStore(vault.local, before_get=asleep_again)

    with pytest.raises(StorageError, match="asleep again"):
        push(library, vault, blob_store=vault.sealed(away))

    assert vault.refs.read(HEAD) == first.head
    assert len(journal(library)) == 2


def test_a_full_push_another_chunk_size_and_a_dry_run_reuse_nothing(
    library: AssetLayout, vault: Vault
) -> None:
    push(library, vault)
    interrupted(library, vault, 3)
    before = (library.root / JOURNAL).read_bytes()

    dry = push(library, vault, dry_run=True)
    assert dry.outcome == "dry-run" and (library.root / JOURNAL).read_bytes() == before

    wider = asleep_after(vault, 4)
    with pytest.raises(StorageError):
        push(library, vault, blob_store=vault.sealed(wider), chunk_bytes=2 * CHUNK)
    assert {entry.chunk_bytes for entry in journal(library)} == {CHUNK, 2 * CHUNK}

    full = push(library, vault, full=True)
    assert (full.outcome, full.resumed_blobs) == ("pushed", 0)
    assert not (library.root / JOURNAL).exists()


def test_a_push_that_finds_the_library_unchanged_clears_a_stale_journal(
    library: AssetLayout, vault: Vault
) -> None:
    interrupted(library, vault, 3)
    pushed = push(library, vault)
    assert pushed.resumed_blobs == 3
    write_file(library.root, JOURNAL, journal_line(journal_entry(0)))

    again = push(library, vault)

    assert again.outcome == "up-to-date"
    assert not (library.root / JOURNAL).exists()


def journal_entry(offset: int, **changes: Any) -> ResumeJournalEntry:
    fields: dict[str, Any] = {
        "library_id": LIBRARY_ID,
        "destination_sha256": LOCAL_DESTINATION.digest,
        "file_sha256": "e" * 64,
        "file_size": 2 * CHUNK,
        "offset": offset,
        "chunk_bytes": CHUNK,
        "plaintext_sha256": "f" * 64,
        "plaintext_size": CHUNK,
        "ref": BlobRef(sha256="a" * 64, size=CHUNK + 33),
        "recorded_at": datetime(2026, 10, 9, tzinfo=UTC),
    }
    return ResumeJournalEntry.model_validate(fields | changes)


def journal_line(entry: ResumeJournalEntry) -> bytes:
    return canonical_json_bytes(entry) + b"\n"


def test_the_journal_drops_a_torn_tail_and_foreign_lines_before_it_grows(
    library: AssetLayout,
) -> None:
    records = VaultRecords(library)
    resume = ResumeJournal(records)
    assert resume.load(LIBRARY_ID, LOCAL_DESTINATION.digest) == {}
    resume.clear()
    records.make_directory()
    assert resume.load(LIBRARY_ID, LOCAL_DESTINATION.digest) == {}
    kept, later = journal_entry(0), journal_entry(CHUNK)
    foreign = journal_entry(0, destination_sha256=TAILNET_DESTINATION.digest)
    other = journal_entry(CHUNK, library_id="another-library")
    path = write_file(library.root, JOURNAL, b"")
    path.write_bytes(
        journal_line(kept) + journal_line(foreign) + journal_line(other) + b'{"schema_ver'
    )

    found = resume.load(LIBRARY_ID, LOCAL_DESTINATION.digest)

    assert found == {("e" * 64, 2 * CHUNK): {0: kept}}
    assert path.read_bytes() == journal_line(kept)
    resume.append(later)
    assert path.read_bytes() == journal_line(kept) + journal_line(later)
    # A line that is not an entry ends what is read; nothing after it counts.
    path.write_bytes(journal_line(kept) + b"not json\n" + journal_line(later))
    assert resume.load(LIBRARY_ID, LOCAL_DESTINATION.digest) == {("e" * 64, 2 * CHUNK): {0: kept}}
    assert path.read_bytes() == journal_line(kept)
    # A clean journal is read as it is and not rewritten.
    stamp = path.stat().st_mtime_ns
    assert resume.load(LIBRARY_ID, LOCAL_DESTINATION.digest)
    assert path.stat().st_mtime_ns == stamp
    resume.clear()
    assert not path.exists()


def test_an_unsafe_journal_is_refused_never_repaired(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    push(library, vault)
    path = library.root / JOURNAL
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_bytes(b"")
    path.symlink_to(outside)
    for relative, data in BIG.items():
        write_file(library.root, relative, data)

    with pytest.raises(StorageIntegrityError, match="resume.jsonl"):
        push(library, vault)
    resume = ResumeJournal(VaultRecords(library))
    with pytest.raises(StorageIntegrityError, match="cannot be opened safely"):
        resume.append(journal_entry(0))
    path.unlink()
    path.write_bytes(b"")
    path.chmod(0o644)
    with pytest.raises(StorageIntegrityError, match="not a private regular file"):
        resume.append(journal_entry(0))


# -- freshness: when a copy was last current, and last verified ------------------------------


def test_freshness_counts_from_the_last_time_a_push_found_the_copy_current(
    library: AssetLayout, vault: Vault
) -> None:
    nothing = read_freshness(library, LIBRARY_ID)
    assert (nothing.generation, nothing.age(datetime.now(UTC))) == (0, None)

    first = push(library, vault)
    state = VaultRecords(library).read_state(LIBRARY_ID)
    assert state is not None
    pushed = read_freshness(library, LIBRARY_ID)
    assert (pushed.generation, pushed.current_at, pushed.verified_at) == (1, state.updated_at, None)
    assert pushed.created_at == manifest_of(vault, first.head).created_at

    report = verify_generation(
        library, blob_store=vault.blobs, ref_store=vault.refs, library_id=LIBRARY_ID
    )
    verified = read_freshness(library, LIBRARY_ID)
    assert report.ok and verified.verified_at is not None
    assert verified.current_at == pushed.current_at
    # A verify of a part of the generation is not a verify of the copy.
    verify_generation(
        library,
        blob_store=vault.blobs,
        ref_store=vault.refs,
        library_id=LIBRARY_ID,
        classes=("records",),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert read_freshness(library, LIBRARY_ID).verified_at == verified.verified_at

    again = push(library, vault)
    current = read_freshness(library, LIBRARY_ID)
    assert again.outcome == "up-to-date"
    assert current.current_at is not None and pushed.current_at is not None
    assert current.current_at > pushed.current_at
    assert current.verified_at == verified.verified_at
    assert current.age(current.current_at + timedelta(days=8)) == timedelta(days=8)

    write_file(library.root, f"{REVIEWS}/next.json", b'{"next":1}\n')
    push(library, vault)
    assert read_freshness(library, LIBRARY_ID).verified_at is None


def test_a_verify_is_recorded_only_for_the_head_this_host_still_records(
    library: AssetLayout, vault: Vault
) -> None:
    push(library, vault)
    records = VaultRecords(library)
    report = verify_generation(
        library, blob_store=vault.blobs, ref_store=vault.refs, library_id=LIBRARY_ID
    )
    checked = library.root / "provenance/vault/destinations/default/checked.json"
    before = checked.read_bytes()
    stamp = datetime(2031, 1, 1, tzinfo=UTC)

    with records.push_lock():
        generations._record_verified(records, LIBRARY_ID, report, lambda: stamp)
    assert checked.read_bytes() == before
    generations._record_verified(records, LIBRARY_ID, replace(report, generation=7), lambda: stamp)
    assert checked.read_bytes() == before

    state = records.read_state(LIBRARY_ID)
    assert state is not None
    other = state.model_copy(update={"generation": 9})
    assert records.read_check(other) is None
    assert records.read_check(None) is None
    unpushed = ReplicationState.model_validate(
        state.model_dump()
        | {"generation": 0, "manifest_sha256": None, "head": None, "ledger_tail_sha256": None}
    )
    with pytest.raises(ValueError, match="only a pushed generation can be current"):
        records.record_current(unpushed, stamp)
    with pytest.raises(ValueError, match="only a pushed generation can be verified"):
        records.record_verified(unpushed, stamp)
    checked.unlink()
    assert records.record_verified(state, stamp).current_at == state.updated_at


def test_a_destination_with_no_records_has_no_check(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "empty")
    records = VaultRecords(layout, "air")
    state = ReplicationState(
        library_id=LIBRARY_ID,
        generation=1,
        manifest_sha256="f" * 64,
        head=RefValue(
            name=HEAD,
            version=1,
            target=BlobRef(sha256="a" * 64, size=1),
            updated_at=datetime(2026, 10, 9, tzinfo=UTC),
        ),
        destination_sha256="d" * 64,
        ledger_tail_sha256="b" * 64,
        updated_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    assert records.read_check(state) is None
    ResumeJournal(records).clear()


# -- the memos class -------------------------------------------------------------------------


def memo_paths(manifest: LibraryManifest) -> set[str]:
    return {entry.path for entry in manifest.entries if entry.file_class == "memos"}


def test_the_memos_drop_is_copied_under_its_prefix_and_restored_under_its_own_root(
    library: AssetLayout, vault: Vault, drop: Path, tmp_path: Path
) -> None:
    pushed = push(library, vault, classes=WITH_MEMOS, memos_root=drop)

    manifest = manifest_of(vault, pushed.head)
    assert memo_paths(manifest) == {f"memos/{relative}" for relative in MEMOS}
    entries = by_path(manifest)
    assert {entries[f"memos/{name}"].mode for name in MEMOS} == {0o600}

    target = private_directory(tmp_path.resolve() / "restore") / "lab"
    memos_target = tmp_path.resolve() / "restore" / "phone"
    restored = restore_library(
        target,
        blob_store=vault.blobs,
        ref_store=vault.refs,
        library_id=LIBRARY_ID,
        accept_latest=True,
        free_bytes=lambda _: ROOM,
        memos_target=memos_target,
    )
    assert (restored.memos_target, restored.skipped_memos) == (memos_target, 0)
    for relative, data in MEMOS.items():
        assert (memos_target / relative).read_bytes() == data
        assert (memos_target / relative).stat().st_mode & 0o777 == 0o600
    assert not (target / "memos").exists()
    assert not list(memos_target.rglob(".*"))
    report = rehash_restored_library(target, restored.manifest, memos_target=memos_target)
    assert report.ok and len(report.entries) == restored.restored_files
    missing = rehash_restored_library(target, restored.manifest)
    assert {entry.path for entry in missing.problems} == memo_paths(manifest)
    assert {entry.detail for entry in missing.problems} == {"no memos root was given"}

    # Without a memos root, a restore leaves them out and says how many.
    elsewhere = private_directory(tmp_path.resolve() / "lab-only") / "lab"
    lab_only = restore_library(
        elsewhere,
        blob_store=vault.blobs,
        ref_store=vault.refs,
        library_id=LIBRARY_ID,
        accept_latest=True,
        free_bytes=lambda _: ROOM,
    )
    assert lab_only.skipped_memos == len(MEMOS)
    assert lab_only.restored_files == restored.restored_files - len(MEMOS)


def test_a_restore_keeps_the_memos_apart_and_needs_room_and_an_empty_root(
    library: AssetLayout, vault: Vault, drop: Path, tmp_path: Path
) -> None:
    push(library, vault, classes=WITH_MEMOS, memos_root=drop)
    base = private_directory(tmp_path.resolve() / "restore")

    def restore(target: Path, memos_target: Path, **options: Any) -> Any:
        arguments: dict[str, Any] = {"free_bytes": lambda _: ROOM}
        return restore_library(
            target,
            blob_store=vault.blobs,
            ref_store=vault.refs,
            library_id=LIBRARY_ID,
            accept_latest=True,
            memos_target=memos_target,
            **(arguments | options),
        )

    for memos_target in (base / "lab", base / "lab" / "memos"):
        with pytest.raises(RestoreRefusedError, match="overlap") as refused:
            restore(base / "lab", memos_target)
        assert refused.value.problem == "unsafe-target"
    occupied = private_directory(base / "occupied")
    write_file(occupied, "x.m4a", b"x")
    with pytest.raises(RestoreRefusedError, match="not empty"):
        restore(base / "lab", occupied)
    other = private_directory(tmp_path.resolve() / "other")
    with pytest.raises(RestoreRefusedError, match="the memos of generation 1") as short:
        restore(
            base / "lab",
            other / "phone",
            free_bytes=lambda path: ROOM if path == base else 0,
        )
    assert short.value.problem == "insufficient-space"
    assert sorted(path.name for path in base.iterdir()) == ["occupied"]


def test_a_memo_that_could_not_be_put_back_under_its_root_is_refused() -> None:
    def memo(**changes: Any) -> ManifestEntry:
        fields: dict[str, Any] = {
            "path": "memos/a.m4a",
            "file_class": "memos",
            "kind": "file",
            "sha256": "a" * 64,
            "size": 1,
            "mode": 0o600,
            "pin_source": None,
            "chunks": (BlobRef(sha256="b" * 64, size=34),),
        }
        return ManifestEntry.model_validate(fields | changes)

    assert generations._memos_parts(memo()) == ("a.m4a",)
    assert generations._memos_parts(memo(path="memos/_mirror/latest.json")) == (
        "_mirror",
        "latest.json",
    )
    for path in ("raw/a.m4a", "memos", "memosx/a.m4a"):
        with pytest.raises(RestoreRefusedError, match="not a file below memos/"):
            generations._memos_parts(memo(path=path))
    pin = memo(kind="pin", pin_source="finetune-run.runtime.pretrained_sha256", chunks=())
    with pytest.raises(RestoreRefusedError, match="is not a file"):
        generations._memos_parts(pin)
    assert memos_source_path("memos/a/b.m4a") == "a/b.m4a"
    with pytest.raises(ValueError, match="is not below memos/"):
        memos_source_path("memos/")


def test_a_link_in_the_drop_refuses_the_memos_and_keeps_their_previous_entries(
    library: AssetLayout, vault: Vault, drop: Path, tmp_path: Path
) -> None:
    first = push(library, vault, classes=WITH_MEMOS, memos_root=drop)
    outside = tmp_path / "outside.m4a"
    outside.write_bytes(b"not the phone's\n")
    (drop / "20261009 101900.m4a").symlink_to(outside)
    write_file(library.root, f"{REVIEWS}/next.json", b'{"next":1}\n')

    second = push(library, vault, classes=WITH_MEMOS, memos_root=drop)

    (refused,) = second.refused_owners
    assert (refused.owner, refused.problem_path) == ("memos", "memos/20261009 101900.m4a")
    assert refused.carried == len(MEMOS) and second.exit_status == 1
    assert memo_paths(manifest_of(vault, second.head)) == memo_paths(manifest_of(vault, first.head))


def test_a_drop_that_is_gone_holds_the_memos_back_and_a_bad_root_is_refused(
    library: AssetLayout, tmp_path: Path
) -> None:
    class Sink:
        def begin(self, owner: str, item: Any) -> None: ...

        def chunk(self, data: bytes) -> None: ...

        def finish(self, entry: Any) -> None: ...

    sink: ReplicationSink = Sink()
    missing = MemosSource(tmp_path.resolve() / "no-such-drop")
    result = collect_pass(library, (missing,), sink, classes=("memos",))
    assert (result.owner("memos").state, result.owner("memos").problem) == ("changed", "missing")
    (tmp_path / "linked").symlink_to(tmp_path)
    linked = MemosSource(tmp_path.resolve() / "linked")
    assert collect_pass(library, (linked,), sink, classes=("memos",)).owner("memos").state == (
        "refused"
    )
    with pytest.raises(ValueError):
        MemosSource(Path("memos/phone"))


def test_memos_never_come_from_the_asset_root_and_their_temporaries_stay_behind(
    library: AssetLayout,
) -> None:
    assert subtree_stays_on_mac("memos/.rsync-partial")
    assert subtree_stays_on_mac("memos/_mirror/.cache")
    assert not subtree_stays_on_mac("memos/_mirror")
    write_file(library.root, "memos/stray.m4a", b"stray\n")
    with pytest.raises(StorageError, match="memos live outside it") as refused:
        require_classified_tree(library)
    assert refused.value.code == "STORAGE.PROTOCOL"


def test_a_push_needs_the_memos_root_exactly_when_it_copies_memos(
    library: AssetLayout, vault: Vault, drop: Path
) -> None:
    with pytest.raises(ValueError, match="needs the memos root"):
        push(library, vault, classes=WITH_MEMOS)
    with pytest.raises(ValueError, match="needs the memos root"):
        push(library, vault, memos_root=drop)


# -- probing a vault that sleeps ---------------------------------------------------------------


class Refs:
    """A ref store that answers a read, or fails it."""

    def __init__(self, failure: StorageError | None = None) -> None:
        self.failure = failure
        self.reads: list[str] = []

    def read(self, name: str) -> RefValue | None:
        self.reads.append(name)
        if self.failure is not None:
            raise self.failure
        return None

    def swap(self, name: str, *, expected_version: int | None, target: BlobRef) -> RefValue:
        raise AssertionError("a probe never writes")


def local_vault(refs: RefStore) -> LibraryVault:
    binding = StorageBinding(
        destination="usb",
        backend="local-directory",
        endpoint="/media/usb/voice-lab-vault",
        namespace="cole",
        library_id=LIBRARY_ID,
        classes=("records",),
        approval_id=None,
        approval_sha256=None,
        key_item=f"private.cole.voice-lab.vault.{LIBRARY_ID}.key",
        token_item=None,
        created_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    stores = VaultStores(LIBRARY_ID, cast(Any, object()), refs, CHUNK, "0" * 16, None)
    return LibraryVault(binding, None, LOCAL_DESTINATION, stores)


def test_a_probe_reads_the_head_once_and_names_a_failure_that_may_pass() -> None:
    refs = Refs()
    assert probe_library_vault(local_vault(refs)).reachable
    assert refs.reads == [HEAD]
    away = probe_library_vault(local_vault(Refs(storage_error("STORAGE.TIMEOUT", "slow"))))
    assert (away.reachable, away.code, away.reason) == (False, "STORAGE.TIMEOUT", "slow")
    with pytest.raises(StorageError, match="unauthorized"):
        probe_library_vault(
            local_vault(Refs(storage_error("STORAGE.UNAUTHORIZED", "unauthorized")))
        )


# -- the Air's commands: cadence, staleness, memos and its tag ---------------------------------

AIR = "air"
AIR_ENDPOINT = f"voice-lab-vault-air.{TAILNET_SUFFIX}"
AIR_TAG = "tag:voice-lab-vault-air"
AIR_NODE = "nAirVault7CNTRL"
AIR_CLASSES = ("memos", "prompt-database", "recordings", "records")


def air_peer(**changes: Any) -> Any:
    return approved_peer(
        **(
            {"node_stable_id": AIR_NODE, "tags": (AIR_TAG,), "dns_name": f"{AIR_ENDPOINT}."}
            | changes
        )
    )


@dataclass
class Cli:
    """Runs ``voice-lab`` in this process and keeps what it printed."""

    capsys: pytest.CaptureFixture[str]

    def __call__(self, *arguments: str) -> tuple[int, str, str]:
        status = main(list(arguments))
        captured = self.capsys.readouterr()
        return status, captured.out, captured.err

    def json(self, *arguments: str, status: int = 0) -> Any:
        found, out, err = self(*arguments)
        assert found == status, err
        return json.loads(out)

    def refused(self, *arguments: str, code: str) -> str:
        found, out, err = self(*arguments)
        assert found == 2, out
        assert err.startswith(f"voice-lab: {code}: "), err
        return err


@dataclass
class Air:
    """The PC with the Air bound as a tailnet destination that copies memos."""

    root: Path
    drop: Path
    cli: Cli
    seams: Any
    access_log: Path

    def vault(self, *arguments: str) -> list[str]:
        return ["vault", *arguments, "--asset-root", str(self.root), "--destination", AIR]

    def push(self, *extra: str, status: int = 0) -> Any:
        return self.cli.json(
            *self.vault("push"), "--memos-root", str(self.drop), *extra, status=status
        )

    def accesses(self) -> int:
        return len(self.access_log.read_bytes().splitlines())

    @property
    def records(self) -> VaultRecords:
        return VaultRecords(AssetLayout.validate(self.root), AIR)

    def age_copy(self, days: int) -> None:
        """Make the copy look ``days`` old, as an Air that stayed away would leave it."""
        state = self.records.read_state(BOUND_LIBRARY)
        assert state is not None
        self.records.record_current(state, datetime.now(UTC) - timedelta(days=days))


@pytest.fixture
def air(
    tmp_path: Path, drop: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Air]:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)
    layout = AssetLayout.initialize(tmp_path.resolve() / "lab")
    add_library_records(layout)
    cli = Cli(capsys)
    with running_vault(tmp_path, namespaces=(AIR,)) as server:
        seams = install_vault_seams(
            monkeypatch, resolver=FakeResolver(air_peer()), port=server.port
        )
        root = str(layout.root)
        cli.json("vault", "init", "--asset-root", root, "--library-id", BOUND_LIBRARY)
        sheets = private_directory(tmp_path.resolve() / "sheets")
        cli.json("vault", "key", "create", "--asset-root", root, "--sheet-out", str(sheets / "s"))
        approval = approval_record(
            approval_id="air-2026-10-09",
            destination=AIR,
            endpoint=AIR_ENDPOINT,
            node_stable_id=AIR_NODE,
            tag=AIR_TAG,
            namespace=AIR,
            classes=AIR_CLASSES,
        )
        fields = ["vault", "approve", "--asset-root", root, *approval_arguments(approval)]
        preview = cli.json(*fields, "--preview")
        assert preview["approval"]["tag"] == AIR_TAG
        digest = preview["approval_sha256"]
        cli.json(*fields, "--reviewed-approval-sha256", digest, "--i-approve-this-destination")
        store_vault_token(seams.keychain, "write", token=server.write_token, destination=AIR)
        cli.json(
            "vault",
            "bind",
            "--asset-root",
            root,
            "--destination",
            AIR,
            "--backend",
            "tailnet-vault",
            "--endpoint",
            AIR_ENDPOINT,
            "--namespace",
            AIR,
            "--classes",
            *AIR_CLASSES,
            "--approval-id",
            approval.approval_id,
            "--approval-sha256",
            digest,
        )
        yield Air(layout.root, drop, cli, seams, server.access_log)


def test_the_air_is_pushed_weekly_and_skipped_quietly_while_it_sleeps(air: Air) -> None:
    first = air.push("--if-older-than", "7d", "--skip-unreachable")
    assert (first["outcome"], first["generation"]) == ("pushed", 1)
    assert {connection.identity.tag for connection in air.seams.connections} == {AIR_TAG}

    # An hour later the copy is fresh: nothing is asked of the tailnet or the vault.
    accesses, lookups = air.accesses(), len(air.seams.resolver.endpoints)
    fresh = air.push("--if-older-than", "7d", "--skip-unreachable")
    assert (fresh["outcome"], fresh["skipped"]) == ("skipped", "fresh")
    assert (air.accesses(), len(air.seams.resolver.endpoints)) == (accesses, lookups)

    # A week on, the Air is asleep: the probe sees it offline and nothing is sent.
    air.age_copy(8)
    air.seams.resolver.peer = air_peer(online=False)
    asleep = air.push("--if-older-than", "7d", "--skip-unreachable")
    assert (asleep["outcome"], asleep["skipped"]) == ("skipped", "unreachable")
    assert "offline" in asleep["detail"] and air.accesses() == accesses
    air.seams.resolver.failure = storage_error("STORAGE.TIMEOUT", "tailscale did not answer")
    assert air.push("--skip-unreachable")["skipped"] == "unreachable"
    air.seams.resolver.failure = None

    # It wakes up: the same run copies the newest state, and the copy is current again.
    air.seams.resolver.peer = air_peer(online=True)
    write_file(air.drop, "20261010 090000.m4a", b"a new memo\n", mode=0o640)
    woke = air.push("--if-older-than", "7d", "--skip-unreachable")
    assert (woke["outcome"], woke["generation"]) == ("pushed", 2)
    freshness = read_freshness(AssetLayout.validate(air.root), BOUND_LIBRARY, AIR)
    assert freshness.current_at is not None
    assert datetime.now(UTC) - freshness.current_at < timedelta(minutes=5)

    # An unchanged library a week later is found current, not copied again.
    air.age_copy(8)
    assert air.push("--if-older-than", "7d", "--skip-unreachable")["outcome"] == "up-to-date"


def test_a_node_that_is_not_the_approved_one_is_never_taken_for_a_sleeping_air(air: Air) -> None:
    for peer in (
        air_peer(node_stable_id="nOther1CNTRL"),
        air_peer(tags=("tag:voice-lab-vault",)),
        air_peer(dns_name=f"voice-lab-vault.{TAILNET_SUFFIX}."),
    ):
        air.seams.resolver.peer = replace(peer, online=False)
        err = air.cli.refused(
            *air.vault("push"),
            "--memos-root",
            str(air.drop),
            "--skip-unreachable",
            code="STORAGE.IDENTITY_MISMATCH",
        )
        assert "not the approved vault" in err


def test_the_air_is_verified_once_per_generation(air: Air) -> None:
    nothing = air.cli.json(*air.vault("verify"), "--if-unverified", "--skip-unreachable")
    assert nothing["skipped"] == "nothing-pushed"
    air.push()
    verified = air.cli.json(*air.vault("verify"), "--if-unverified", "--skip-unreachable")
    assert verified["ok"] is True and verified["checked"] > len(MEMOS)
    again = air.cli.json(*air.vault("verify"), "--if-unverified", "--skip-unreachable")
    assert again["skipped"] == "verified"
    air.cli.refused(
        *air.vault("verify"), "--if-unverified", "--class", "memos", code="VAULT_CLI.USAGE_INVALID"
    )
    write_file(air.drop, "20261010 090000.m4a", b"a new memo\n", mode=0o640)
    air.push()
    air.seams.resolver.peer = air_peer(online=False)
    asleep = air.cli.json(*air.vault("verify"), "--if-unverified", "--skip-unreachable")
    assert asleep["skipped"] == "unreachable"


def test_the_memos_root_is_named_exactly_when_the_destination_copies_memos(
    air: Air, tmp_path: Path
) -> None:
    air.cli.refused(*air.vault("push"), code="VAULT_CLI.USAGE_INVALID")
    err = air.cli.refused(
        *air.vault("push"),
        "--memos-root",
        str(tmp_path / "nowhere"),
        code="VAULT_CLI.USAGE_INVALID",
    )
    assert "is not a directory" in err
    local = private_directory(tmp_path.resolve() / "usb")
    air.cli.json(
        "vault",
        "bind",
        "--asset-root",
        str(air.root),
        "--destination",
        "usb",
        "--backend",
        "local-directory",
        "--endpoint",
        str(local),
        "--namespace",
        "usb",
        "--classes",
        "records",
    )
    err = air.cli.refused(
        "vault",
        "push",
        "--asset-root",
        str(air.root),
        "--destination",
        "usb",
        "--memos-root",
        str(air.drop),
        code="VAULT_CLI.USAGE_INVALID",
    )
    assert "takes no --memos-root" in err


def test_a_drill_of_the_air_puts_the_memos_back_under_their_own_root(
    air: Air, tmp_path: Path
) -> None:
    air.push()
    base = private_directory(tmp_path.resolve() / "drill")
    drilled = air.cli.json(
        *air.vault("drill"),
        "--target",
        str(base / "lab"),
        "--memos-target",
        str(base / "phone"),
        "--accept-latest",
    )
    assert drilled["ok"] is True and drilled["memos_target"] == str(base / "phone")
    assert drilled["rehashed_files"] == drilled["restored_files"]
    for relative, data in MEMOS.items():
        assert (base / "phone" / relative).read_bytes() == data
    # A drill that leaves the memos out does not pass.
    short = air.cli.json(
        *air.vault("drill"), "--target", str(base / "lab-only"), "--accept-latest", status=1
    )
    assert short["ok"] is False and len(short["problems"]) == len(MEMOS)
    restored = air.cli.json(
        *air.vault("restore"), "--target", str(base / "restored"), "--accept-latest"
    )
    assert (restored["skipped_memos"], restored["memos_target"]) == (len(MEMOS), None)


def test_staleness_flags_the_air_after_fourteen_days(air: Air) -> None:
    root = str(air.root)
    never = air.cli.json("vault", "staleness", "--asset-root", root, "--json")
    assert never["stale"] == [AIR] and never["ok"] is False
    assert never["destinations"][0]["age_seconds"] is None
    assert air.cli("vault", "staleness", "--asset-root", root)[1] == (
        "air: nothing copied yet - STALE (over 14 d)\n"
    )
    air.push()
    line = air.cli("vault", "staleness", "--asset-root", root)[1]
    assert line.startswith("air: generation 1, current 0 d 0 h ago") and "STALE" not in line
    fresh = air.cli.json("vault", "staleness", "--asset-root", root, "--json", "--check")
    (row,) = fresh["destinations"]
    assert (row["destination"], row["generation"], row["stale"]) == (AIR, 1, False)
    assert row["stale_after_seconds"] == 14 * 86_400 and fresh["ok"] is True
    air.age_copy(15)
    stale = air.cli.json("vault", "staleness", "--asset-root", root, "--json", "--check", status=1)
    assert stale["stale"] == [AIR]
    assert air.cli.json("vault", "staleness", "--asset-root", root, "--json")["ok"] is False
    status, out, _ = air.cli("vault", "staleness", "--asset-root", root)
    assert status == 0 and out.startswith("air: generation 1, current 15 d") and "STALE" in out
    relaxed = air.cli.json(
        "vault", "staleness", "--asset-root", root, "--json", "--stale-after", "air=30d"
    )
    assert relaxed["stale"] == []
    air.cli.json(*air.vault("verify"))
    verified = air.cli("vault", "staleness", "--asset-root", root)[1]
    assert ", verified " in verified


def test_staleness_of_a_host_with_nothing_bound(tmp_path: Path, capsys: Any) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "lab")
    cli = Cli(capsys)
    empty = cli.json("vault", "staleness", "--asset-root", str(layout.root), "--json")
    assert (empty["library_id"], empty["destinations"], empty["ok"]) == (None, [], True)
    status, out, _ = cli("vault", "staleness", "--asset-root", str(layout.root))
    assert status == 0 and out == "no destination has been bound or copied to\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["vault", "staleness", "--stale-after", "air"],
        ["vault", "staleness", "--stale-after", "air=14"],
        ["vault", "staleness", "--stale-after", "Air=14d"],
        ["vault", "staleness", "--stale-after", "air=0d"],
        ["vault", "push", "--if-older-than", "7 days"],
        ["vault", "restore", "--target", "/x/y/z", "--accept-latest", "--tag", "tag:voice-lab"],
    ],
)
def test_durations_destinations_and_tags_are_refused_when_malformed(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(arguments)


def test_durations_take_one_unit() -> None:
    parsed = build_parser().parse_args(
        ["vault", "staleness", "--stale-after", "laptop=36h", "--stale-after", "air=90m"]
    )
    assert parsed.stale_after == [("laptop", timedelta(hours=36)), ("air", timedelta(minutes=90))]
    assert build_parser().parse_args(
        ["vault", "push", "--if-older-than", "3600s"]
    ).if_older_than == (timedelta(hours=1))


def test_a_restore_by_endpoint_checks_the_air_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch, resolver=FakeResolver(air_peer()), port=1)
    from vault_binding_fixtures import initialized_library, store_library_key

    layout = initialized_library(tmp_path.resolve() / "lab")
    store_library_key(seams.keychain)
    store_vault_token(seams.keychain, "read", destination=AIR)
    open_vault_for_restore(
        layout,
        backend="tailnet-vault",
        endpoint=AIR_ENDPOINT,
        namespace=AIR,
        node_stable_id=AIR_NODE,
        tailnet_suffix=TAILNET_SUFFIX,
        destination=AIR,
        tag=AIR_TAG,
    )
    assert seams.connections[-1].identity.tag == AIR_TAG
    with pytest.raises(StorageError, match="nor --tag"):
        open_vault_for_restore(
            layout,
            backend="local-directory",
            endpoint=str(tmp_path / "usb"),
            tag=AIR_TAG,
        )


def test_every_ledger_entry_of_the_air_names_its_tag(air: Air) -> None:
    air.push()
    entries = ReplicationLedger(AssetLayout.validate(air.root), AIR).entries()
    assert {entry.destination.tag for entry in entries} == {AIR_TAG}
    assert {entry.destination.node_stable_id for entry in entries} == {AIR_NODE}

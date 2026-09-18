"""Manifest chain, verify and restore (library vault plan, Step 6).

Scenario tests for ``src/cole_voice_lab/library_generations.py`` (pushes, the head checks, the
ledger, verification, restore) on synthetic fixture roots: a small library of records built
in ``tmp_path`` and, for the round trip, the pilot-v2 legacy root with native pilot-v2 takes and
a package, a published corpus store and a finished stub fine-tune run. The vault is a sealed
store over a local store in ``tmp_path`` with a fixed synthetic key. A misbehaving vault host
(rolled back or forked heads, damaged or swapped chunks, a store that answers one ref with
another stored blob) is played by editing the local store's files or by a wrapper store.
Nothing touches a Keychain, a network peer or the real library.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pickle
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError
from test_pilot_v2_coverage import SUBJECT, build_legacy_root, pilot_v1, publish, released_pilot_v2
from test_recording_packages import analyzer_build as analyzer_build
from test_references import _accepted, _publish
from vault_library_fixtures import (
    CATALOG_ID,
    LIBRARY_ID,
    LOCAL_DESTINATION,
    OTHER_KEY,
    TAILNET_DESTINATION,
    Clock,
    HookedBlobStore,
    Vault,
    add_finished_run,
    add_mac_only_files,
    add_native_takes,
    add_records,
    private_directory,
    publish_corpus,
    restore_permissions,
    tree,
    write_file,
)

import cole_voice_lab.library_generations as generations
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256, write_canonical_json
from cole_voice_lab.cli import main
from cole_voice_lab.finetune import FineTuneRun, select_finetune_corpus
from cole_voice_lab.library_classification import CORPUS_DATABASE_PATH, classify
from cole_voice_lab.library_generations import (
    RESTORE_MARGIN_BYTES,
    DestinationContext,
    PinOutcome,
    PushResult,
    PushRunningError,
    ReplicationLedger,
    RestoreRefusedError,
    RestoreResult,
    StoredManifest,
    VaultRecords,
    VerifyReport,
    push_generation,
    read_stored_manifest,
    restore_library,
    store_manifest,
    verify_generation,
)
from cole_voice_lab.library_replication import (
    MAX_CHUNK_BYTES,
    PRETRAINED_PIN_SOURCE,
    RecordTreeSource,
    ReplicationItem,
    ReplicationSource,
    ScanRoot,
    SourceChangedError,
    SourceEntry,
    WithheldPath,
    library_sources,
)
from cole_voice_lab.paths import ASSET_DIRECTORIES, AssetLayout
from cole_voice_lab.recording_discovery import discover_recording_library
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_takes import RecordingTakeStore
from cole_voice_lab.storage_binding import open_catalog_reader
from cole_voice_lab.storage_contracts import (
    FileClass,
    LedgerEntry,
    LibraryManifest,
    ManifestEntry,
    ReplicationState,
    ledger_entry_digest,
    manifest_digest,
    manifest_payload_digest,
)
from cole_voice_lab.storage_ports import (
    BlobRef,
    RefValue,
    StorageError,
    StorageIntegrityError,
    StorageNameError,
    library_head_ref,
    storage_error,
)
from cole_voice_lab.storage_sealing import MAX_SEALED_PLAINTEXT_BYTES

ALL_CLASSES: tuple[FileClass, ...] = tuple(sorted(get_args(FileClass.__value__)))
CHUNK = 4096
HEAD = library_head_ref(LIBRARY_ID)
CONSENT = "provenance/consents/cole-self-v1.json"
CHANGED = "provenance/headphone-reviews/changed.json"
ROOM = 2**62
FIXED = datetime(2026, 9, 17, 12, tzinfo=UTC)
# What the take store's first open adds to a restored library (all Mac-only, all empty).
OWNER_FILES = ("provenance/recording-takes/.metadata.lock",)
OWNER_DIRECTORIES = (
    "provenance/recording-takes/.operations",
    "provenance/recording-takes/.cursors",
    "provenance/recording-takes/.analysis",
    "provenance/recording-groups/.prepared-gates",
)
# Every directory the small library fixture has below a read-only tree; each must come back
# at 0500 and every other restored directory at 0700 (named here, not derived from the
# module under test, so narrowing its list of read-only trees fails these tests).
READ_ONLY_DIRECTORIES = ("derived/datasets/pilot-1", "derived/transfer-plans/plan-1")


# -- helpers -------------------------------------------------------------------------------


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
    add_mac_only_files(layout)
    return layout


def push(layout: AssetLayout, vault: Vault, **options: Any) -> PushResult:
    arguments: dict[str, Any] = {
        "blob_store": vault.blobs,
        "ref_store": vault.refs,
        "library_id": LIBRARY_ID,
        "destination": LOCAL_DESTINATION,
        "classes": ALL_CLASSES,
        "chunk_bytes": CHUNK,
        "clock": vault.clock,
    }
    return push_generation(layout, **(arguments | options))


def verify(layout: AssetLayout, vault: Vault, **options: Any) -> VerifyReport:
    arguments: dict[str, Any] = {
        "blob_store": vault.blobs,
        "ref_store": vault.refs,
        "library_id": LIBRARY_ID,
    }
    return verify_generation(layout, **(arguments | options))


def restore(target: Path, vault: Vault, **options: Any) -> RestoreResult:
    arguments: dict[str, Any] = {
        "blob_store": vault.blobs,
        "ref_store": vault.refs,
        "library_id": LIBRARY_ID,
        "free_bytes": lambda _: ROOM,
    }
    if "generation" not in options and "manifest_sha256" not in options:
        arguments["accept_latest"] = True
    return restore_library(target, **(arguments | options))


def change(layout: AssetLayout, text: str) -> None:
    write_file(layout.root, CHANGED, text.encode())


def manifest_of(vault: Vault, head: RefValue | None) -> LibraryManifest:
    assert head is not None
    return read_stored_manifest(vault.blobs, head.target, library_id=LIBRARY_ID).manifest


def by_path(manifest: LibraryManifest) -> dict[str, ManifestEntry]:
    return {entry.path: entry for entry in manifest.entries}


def ledger(layout: AssetLayout) -> tuple[LedgerEntry, ...]:
    return ReplicationLedger(layout).entries()


def recorded(layout: AssetLayout) -> tuple[LedgerEntry, ...]:
    """The ledger read as a push reads it: the chain plus the upload ``state.json`` names."""
    return ReplicationLedger(layout).require_recorded(VaultRecords(layout).read_state(LIBRARY_ID))


def kinds(layout: AssetLayout) -> list[str]:
    return [entry.kind for entry in ledger(layout)]


def state_path(layout: AssetLayout) -> Path:
    return layout.root / "provenance/vault/state.json"


def ledger_path(layout: AssetLayout, sequence: int) -> Path:
    return layout.root / f"provenance/vault/ledger/{sequence:08d}.json"


def manifest_copies(layout: AssetLayout) -> list[Path]:
    """The manifest copies this Mac keeps, in name order; empty before the first push."""
    directory = layout.root / "provenance/vault/manifests"
    return sorted(directory.iterdir()) if directory.is_dir() else []


def new_target(tmp_path: Path, name: str = "restore") -> Path:
    return private_directory(tmp_path.resolve() / name) / "assets"


def siblings(target: Path) -> list[str]:
    return sorted(path.name for path in target.parent.iterdir() if path.name != target.name)


def vault_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def rewrite(path: Path, data: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(data)


def first_file(manifest: LibraryManifest, path: str | None = None) -> ManifestEntry:
    return next(
        entry
        for entry in manifest.entries
        if entry.kind == "file" and entry.size > 0 and (path is None or entry.path == path)
    )


# -- the destination and the refusals -------------------------------------------------------


def test_a_destination_names_its_node_and_approval_only_for_a_tailnet_vault() -> None:
    assert LOCAL_DESTINATION.ledger_destination.node_stable_id is None
    assert TAILNET_DESTINATION.ledger_destination.tag == "tag:voice-lab-vault"
    with pytest.raises(ValueError, match="approval_sha256 is required for tailnet-vault"):
        replace(TAILNET_DESTINATION, approval_sha256=None)
    with pytest.raises(ValueError, match="forbidden for local-directory"):
        replace(LOCAL_DESTINATION, approval_sha256="1" * 64)
    with pytest.raises(ValueError, match="64 lowercase hex"):
        replace(TAILNET_DESTINATION, approval_sha256="A" * 64)
    with pytest.raises(ValidationError, match="node_stable_id is required for tailnet-vault"):
        replace(TAILNET_DESTINATION, node_stable_id=None)
    with pytest.raises(ValidationError, match="absolute, normalized directory"):
        DestinationContext(backend="local-directory", endpoint="relative/dir", namespace="cole")


def test_push_and_restore_refusals_are_named_and_survive_pickling() -> None:
    running = pickle.loads(pickle.dumps(PushRunningError("a push is running")))
    assert type(running) is PushRunningError and str(running) == "a push is running"
    assert (running.code, running.retryable) == ("REPLICATION.PUSH_RUNNING", True)
    assert not isinstance(running, StorageError)
    refused = pickle.loads(pickle.dumps(RestoreRefusedError("insufficient-space", "full")))
    assert type(refused) is RestoreRefusedError
    assert (refused.problem, str(refused), refused.code) == (
        "insufficient-space",
        "full",
        "REPLICATION.RESTORE_REFUSED",
    )
    with pytest.raises(ValueError, match="unknown restore problem"):
        RestoreRefusedError("broken", "no")  # type: ignore[arg-type]


# -- pushing ---------------------------------------------------------------------------------


def test_a_first_push_stores_one_sealed_generation_and_records_it_on_the_mac(
    library: AssetLayout, vault: Vault
) -> None:
    publish_corpus(library)
    result = push(library, vault)

    assert result.outcome == "pushed" and result.exit_status == 0
    assert result.head is not None and result.head.name == HEAD
    assert (result.generation, result.head.version, result.adopted_generation) == (1, 1, None)
    assert all(owner.state == "complete" for owner in result.owners)
    assert result.waiting_paths == () and result.waiting_owners == ()
    stored = read_stored_manifest(vault.blobs, result.head.target, library_id=LIBRARY_ID)
    manifest = stored.manifest
    assert stored.digest == result.manifest_sha256 == manifest_digest(manifest)
    assert manifest.classes == ALL_CLASSES and manifest.previous_manifest_sha256 is None
    entries = by_path(manifest)
    assert {CORPUS_DATABASE_PATH, CONSENT, "provenance/diagnostic-root.json"} <= set(entries)
    assert entries["provenance/headphone-reviews/empty.json"].chunks == ()
    assert all(classify(path).copied for path in entries)
    assert not any(
        path.startswith(("provenance/vault", "tmp/.vault-", "derived/speech", "runtimes"))
        for path in entries
    )
    for entry in manifest.entries:
        if entry.path != CORPUS_DATABASE_PATH:
            data = (library.root / entry.path).read_bytes()
            assert hashlib.sha256(data).hexdigest() == entry.sha256, entry.path
    assert entries["provenance/pilot-gates/gate-1.json"].mode == 0o400
    assert entries[CONSENT].mode == 0o600
    assert len(entries[CORPUS_DATABASE_PATH].chunks) > 1

    blobs = vault.blob_files()
    assert (len(blobs), sum(map(len, blobs.values()))) == (result.sent_blobs, result.sent_bytes)
    for data in blobs.values():
        assert data.startswith(b"CVLS") and b"SQLite format 3" not in data
    files = [entry for entry in manifest.entries if entry.kind == "file" and entry.size > 0]
    assert result.uploaded_files == len({(entry.sha256, entry.size) for entry in files})
    assert result.reused_files == 0

    state = ReplicationState.model_validate_json(state_path(library).read_bytes())
    assert (state.generation, state.manifest_sha256, state.head) == (
        1,
        result.manifest_sha256,
        result.head,
    )
    records = VaultRecords(library)
    copy = records.manifest_copy_path(1)
    assert copy.read_bytes() == canonical_json_bytes(manifest) + b"\n"
    assert records.read_manifest_copy(1) == manifest
    started, completed = ledger(library)
    assert (started.kind, completed.kind) == ("upload-started", "upload-completed")
    assert started.payload_sha256 == completed.payload_sha256 == manifest_payload_digest(manifest)
    assert completed.previous_entry_sha256 == ledger_entry_digest(started)
    assert (completed.manifest_sha256, completed.head_version) == (result.manifest_sha256, 1)
    assert started.byte_count == sum(entry.size for entry in files)
    assert started.blob_count == sum(-(-entry.size // CHUNK) for entry in files)
    assert (completed.blob_count, completed.byte_count) == (result.sent_blobs, result.sent_bytes)
    assert started.destination == LOCAL_DESTINATION.ledger_destination
    assert started.approval_sha256 is None and started.classes == ALL_CLASSES
    for path in (library.root / "provenance/vault").rglob("*"):
        mode = stat.S_IMODE(path.lstat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path
    # The database was spooled into the push's own scratch, which is gone again.
    assert [path.name for path in (library.root / "tmp").iterdir()] == [".vault-0123456789abcdef"]


class LedgerProbe(HookedBlobStore):
    """Records the ledger when the first blob of a push is put."""

    def __init__(self, vault: Vault, layout: AssetLayout) -> None:
        super().__init__(vault.local, before_put=self.probe)
        self.layout = layout
        self.seen: list[tuple[LedgerEntry, ...]] = []

    def probe(self, data: bytes) -> None:
        if not self.seen:
            self.seen.append(ledger(self.layout))


def test_the_started_entry_is_on_disk_before_the_first_blob_leaves(
    library: AssetLayout, vault: Vault
) -> None:
    probe = LedgerProbe(vault, library)
    result = push(library, vault, blob_store=vault.sealed(probe), destination=TAILNET_DESTINATION)

    ((started,),) = probe.seen
    assert (started.kind, started.generation) == ("upload-started", 1)
    assert started.approval_sha256 == TAILNET_DESTINATION.approval_sha256
    assert started.destination == TAILNET_DESTINATION.ledger_destination
    assert len(probe.puts) == result.sent_blobs
    assert ledger(library)[-1].approval_sha256 == TAILNET_DESTINATION.approval_sha256


def test_a_second_push_uploads_only_content_the_vault_does_not_hold(
    library: AssetLayout, vault: Vault
) -> None:
    first = push(library, vault)
    root = library.root
    write_file(root, CONSENT, b'{"consent":"changed"}\n')
    write_file(root, "provenance/headphone-reviews/review-2.json", b'{"review":1}\n')
    for name in ("review-3", "review-4"):
        write_file(root, f"provenance/headphone-reviews/{name}.json", b"x" * (3 * CHUNK + 1))
    before = set(vault.blob_files())
    second = push(library, vault)

    assert (second.outcome, second.generation, second.uploaded_files) == ("pushed", 2, 2)
    old, new = by_path(manifest_of(vault, first.head)), by_path(manifest_of(vault, second.head))
    stored = read_stored_manifest(vault.blobs, second.head.target, library_id=LIBRARY_ID)  # type: ignore[union-attr]
    assert stored.manifest.previous_manifest_sha256 == first.manifest_sha256
    for path, entry in old.items():
        if path != CONSENT:
            assert new[path].chunks == entry.chunks, path
    assert new[CONSENT].chunks != old[CONSENT].chunks
    reviews = "provenance/headphone-reviews"
    assert new[f"{reviews}/review-2.json"].chunks == old[f"{reviews}/review-1.json"].chunks
    assert new[f"{reviews}/review-3.json"].chunks == new[f"{reviews}/review-4.json"].chunks
    assert len(new[f"{reviews}/review-3.json"].chunks) == 4
    added = set(vault.blob_files()) - before
    expected = {
        ref.sha256
        for ref in (
            *new[CONSENT].chunks,
            *new[f"{reviews}/review-3.json"].chunks,
            *stored.envelope.chunks,
            stored.ref,
        )
    }
    assert added == expected and second.sent_blobs == len(added)
    reusable = {(entry.sha256, entry.size) for entry in old.values() if entry.chunks}
    assert second.reused_files == sum(
        (entry.sha256, entry.size) in reusable for entry in new.values() if entry.kind == "file"
    )
    started, completed = ledger(library)[-2:]
    assert started.byte_count == len(b'{"consent":"changed"}\n') + 3 * CHUNK + 1
    assert started.blob_count == 1 + 4
    assert (completed.blob_count, completed.generation) == (len(added), 2)


def test_an_unchanged_library_is_up_to_date_and_nothing_is_written(
    library: AssetLayout, vault: Vault
) -> None:
    publish_corpus(library)
    first = push(library, vault)
    stores = vault_snapshot(vault.root)
    records = vault_snapshot(library.root / "provenance/vault")
    again = push(library, vault)

    assert (again.outcome, again.exit_status) == ("up-to-date", 0)
    assert (again.generation, again.manifest_sha256, again.head) == (
        first.generation,
        first.manifest_sha256,
        first.head,
    )
    assert (again.uploaded_files, again.sent_blobs, again.sent_bytes) == (0, 0, 0)
    assert again.reused_files == len(
        [e for e in manifest_of(vault, first.head).entries if e.chunks]
    )
    assert vault_snapshot(vault.root) == stores
    assert vault_snapshot(library.root / "provenance/vault") == records


class HookedLedger(ReplicationLedger):
    """Runs ``after_start`` once an ``upload-started`` entry is written."""

    def __init__(self, layout: AssetLayout, after_start: Callable[[], None]) -> None:
        super().__init__(layout)
        self.after_start = after_start

    def append(self, **fields: Any) -> LedgerEntry:  # type: ignore[override]
        entry = super().append(**fields)
        if entry.kind == "upload-started":
            self.after_start()
        return entry


def test_a_file_that_changes_between_the_pass_and_its_upload_stops_the_push(
    library: AssetLayout, vault: Vault
) -> None:
    publish_corpus(library)
    first = push(library, vault)
    write_file(library.root, CONSENT, b'{"consent":"second"}\n')
    state, head = state_path(library).read_bytes(), vault.head_file.read_bytes()

    def rewrite_consent() -> None:
        write_file(library.root, CONSENT, b'{"consent":"third, and longer"}\n')

    stopped = push(library, vault, ledger=HookedLedger(library, rewrite_consent))
    assert (stopped.outcome, stopped.exit_status, stopped.generation) == ("source-changed", 1, 1)
    assert stopped.head == first.head and stopped.uploaded_files == 0
    change_error = stopped.source_change
    assert isinstance(change_error, SourceChangedError)
    assert (change_error.path, change_error.problem, change_error.retryable) == (
        CONSENT,
        "changed",
        True,
    )
    assert "changed after the pass read it" in str(change_error)
    assert state_path(library).read_bytes() == state and vault.head_file.read_bytes() == head
    assert kinds(library) == ["upload-started", "upload-completed", "upload-started"]
    assert [path.name for path in (library.root / "tmp").iterdir()] == [".vault-0123456789abcdef"]

    resumed = push(library, vault)
    assert (resumed.outcome, resumed.generation) == ("pushed", 2)
    assert kinds(library)[-2:] == ["upload-started", "upload-completed"]
    consent = by_path(manifest_of(vault, resumed.head))[CONSENT]
    third = b'{"consent":"third, and longer"}\n'
    assert consent.sha256 == hashlib.sha256(third).hexdigest()


LOCK_HOLDER = """
import fcntl, os, sys
descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)
fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.read()
"""


def test_one_push_runs_at_a_time(library: AssetLayout, vault: Vault) -> None:
    records = VaultRecords(library)
    with records.push_lock():
        with pytest.raises(PushRunningError, match="already running"):
            push(library, vault)
        with pytest.raises(PushRunningError):
            verify(library, vault)
    lock = library.root / "provenance/vault/.push.lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    holder = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", LOCK_HOLDER, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline() == "held\n"
        with pytest.raises(PushRunningError) as refused:
            push(library, vault)
        assert refused.value.retryable
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        assert holder.wait(timeout=30) == 0
    assert kinds(library) == []
    assert push(library, vault).outcome == "pushed"


@dataclass(frozen=True)
class FailingSource:
    """A source whose owner fails as a whole with ``problem``."""

    inner: ReplicationSource
    problem: str

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def rules(self) -> frozenset[str]:
        return self.inner.rules

    @property
    def referents(self) -> frozenset[str]:
        return self.inner.referents

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        raise SourceChangedError(f"{self.name}-guard", self.problem, "moved during the pass")  # type: ignore[arg-type]
        yield  # pragma: no cover - makes this a generator


def sources_with(failing: dict[str, str]) -> tuple[ReplicationSource, ...]:
    return tuple(
        FailingSource(source, failing[source.name]) if source.name in failing else source
        for source in library_sources()
    )


def lab_paths(manifest: LibraryManifest) -> set[str]:
    return {
        entry.path
        for entry in manifest.entries
        if classify(entry.path).rule.name
        in {"consents", "consent-history", "pilot-gates", "headphone-reviews", "diagnostic-root"}
    }


def test_owners_that_do_not_complete_keep_their_previous_entries(
    library: AssetLayout, vault: Vault
) -> None:
    publish_corpus(library)
    first = push(library, vault, sources=sources_with({"lab-approvals": "changed"}))
    owners = {owner.owner: owner for owner in first.owners}
    waiting = owners["lab-approvals"]
    assert (waiting.state, waiting.carried, waiting.problem_path) == (
        "changed",
        0,
        "lab-approvals-guard",
    )
    assert "moved during the pass" in (waiting.reason or "")
    held_back = {owner.owner for owner in first.waiting_owners} - {"lab-approvals"}
    assert held_back == {"datasets", "references", "transfer-plans"}
    assert {owners[name].held_back_by for name in held_back} <= {"lab-approvals", "datasets"}
    assert first.exit_status == 0 and first.refused_owners == ()
    omitted = by_path(manifest_of(vault, first.head))
    assert lab_paths(manifest_of(vault, first.head)) == set()
    assert "derived/datasets/pilot-1/manifest.json" not in omitted

    second = push(library, vault)
    carried = lab_paths(manifest_of(vault, second.head))
    assert CONSENT in carried and len(carried) == 5

    write_file(library.root, CONSENT, b'{"consent":"not yet copied"}\n')
    third = push(library, vault, sources=sources_with({"lab-approvals": "unsafe"}))
    refused = {owner.owner: owner for owner in third.owners}["lab-approvals"]
    assert (refused.state, refused.carried) == ("refused", len(carried))
    assert third.refused_owners == (refused,) and third.exit_status == 1
    assert (third.outcome, third.generation) == ("up-to-date", 2)

    held = push(library, vault, sources=sources_with({"transfer-approvals": "changed"}))
    owners = {owner.owner: owner for owner in held.owners}
    assert owners["transfer-approvals"].state == "changed"
    plans = owners["transfer-plans"]
    assert (plans.state, plans.held_back_by, plans.carried) == ("changed", "transfer-approvals", 1)
    assert plans.problem_path == "transfer-approvals-guard"
    assert (held.outcome, held.exit_status) == ("pushed", 0)
    kept, before = by_path(manifest_of(vault, held.head)), by_path(manifest_of(vault, second.head))
    for path in (
        "derived/transfer-plans/plan-1/manifest.json",
        "provenance/transfer-approvals/approval-1.json",
    ):
        assert kept[path] == before[path]
    assert kept[CONSENT].sha256 == hashlib.sha256(b'{"consent":"not yet copied"}\n').hexdigest()


def test_a_withheld_file_keeps_its_previous_entry_and_a_removed_one_is_dropped(
    library: AssetLayout, vault: Vault
) -> None:
    root = library.root
    data = b'{"consent":"an earlier version"}\n'
    history = f"provenance/consents/history/cole-self-v1.{hashlib.sha256(data).hexdigest()}.json"
    write_file(root, history, data)
    first = push(library, vault)
    old = by_path(manifest_of(vault, first.head))
    assert history in old

    (root / history).write_bytes(b"")
    later = f"provenance/consents/history/cole-self-v1.{'e' * 64}.json"
    write_file(root, later, b"")
    (root / "provenance/headphone-reviews/review-1.json").unlink()
    second = push(library, vault)

    assert second.outcome == "pushed"
    new = by_path(manifest_of(vault, second.head))
    assert new[history] == old[history]
    assert later not in new and "provenance/headphone-reviews/review-1.json" not in new
    owner = {owner.owner: owner for owner in second.owners}["lab-approvals"]
    assert (owner.state, owner.carried) == ("complete", 1)
    assert second.waiting_paths == (later,)


def test_an_owner_holding_a_file_a_restore_could_not_write_is_refused(
    library: AssetLayout, vault: Vault
) -> None:
    first = push(library, vault)
    name = f"provenance/consents/zero{chr(0x200B)}width.json"
    write_file(library.root, name)
    result = push(library, vault)

    owner = {owner.owner: owner for owner in result.owners}["lab-approvals"]
    assert (owner.state, owner.problem_path) == ("refused", name)
    assert "a restore could not write a file of this name" in (owner.reason or "")
    assert owner.carried == len(lab_paths(manifest_of(vault, first.head)))
    assert (result.outcome, result.exit_status) == ("up-to-date", 1)


def test_only_the_destination_classes_are_read_and_kept(library: AssetLayout, vault: Vault) -> None:
    publish_corpus(library)
    first = push(library, vault, classes=("records", "prompt-database", "records"))
    manifest = manifest_of(vault, first.head)
    assert manifest.classes == ("prompt-database", "records")
    assert {entry.file_class for entry in manifest.entries} == {"prompt-database", "records"}
    assert not {"raw-packages", "datasets", "fine-tune-runs"} & {o.owner for o in first.owners}

    second = push(library, vault, classes=("records",))
    manifest = manifest_of(vault, second.head)
    assert manifest.classes == ("records",) and CORPUS_DATABASE_PATH not in by_path(manifest)
    assert "prompt-database" not in {owner.owner for owner in second.owners}

    for classes in ((), ("speech",)):
        with pytest.raises(ValueError, match="one or more known file classes"):
            push(library, vault, classes=classes)
    for size in (0, MAX_CHUNK_BYTES + 1):
        with pytest.raises(ValueError, match="a chunk is 1 to"):
            push(library, vault, chunk_bytes=size)
    with pytest.raises(StorageNameError):
        push(library, vault, library_id="Cole")
    assert len(ledger(library)) == 4


def test_previous_entries_no_running_source_decides_are_dropped(
    library: AssetLayout, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = push(library, vault)
    approvals = "provenance/transfer-approvals/approval-1.json"
    assert approvals in by_path(manifest_of(vault, first.head))
    without = tuple(s for s in library_sources() if s.name != "transfer-approvals")
    second = push(library, vault, sources=without)
    assert approvals not in by_path(manifest_of(vault, second.head))

    third = push(library, vault)
    assert approvals in by_path(manifest_of(vault, third.head))
    real = generations.matching_rules
    monkeypatch.setattr(
        generations,
        "matching_rules",
        lambda path: () if path == approvals else real(path),
    )
    fourth = push(library, vault, sources=sources_with({"transfer-approvals": "changed"}))
    owner = {owner.owner: owner for owner in fourth.owners}["transfer-approvals"]
    assert (owner.state, owner.carried) == ("changed", 0)
    assert approvals not in by_path(manifest_of(vault, fourth.head))


@dataclass(frozen=True)
class OfferAndWithhold:
    """A source that offers each file and then withholds it too."""

    inner: RecordTreeSource

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def rules(self) -> frozenset[str]:
        return self.inner.rules

    @property
    def referents(self) -> frozenset[str]:
        return self.inner.referents

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        for entry in self.inner.items(layout):
            yield entry
            if isinstance(entry, ReplicationItem):
                yield WithheldPath(entry.path, entry.file_class, "offered and withheld")


def test_a_file_offered_fresh_is_never_replaced_by_its_previous_entry(
    library: AssetLayout, vault: Vault
) -> None:
    approvals = "provenance/transfer-approvals/approval-1.json"
    push(library, vault)
    write_file(library.root, approvals, b'{"approval":"renewed"}\n')
    source = OfferAndWithhold(
        RecordTreeSource(
            "transfer-approvals",
            frozenset({"transfer-approvals"}),
            (ScanRoot("provenance/transfer-approvals"),),
            private=False,
        )
    )
    sources = tuple(source if s.name == source.name else s for s in library_sources())
    result = push(library, vault, sources=sources)
    owner = {owner.owner: owner for owner in result.owners}["transfer-approvals"]
    assert (owner.state, owner.carried) == ("complete", 0)
    entry = by_path(manifest_of(vault, result.head))[approvals]
    assert entry.sha256 == hashlib.sha256(b'{"approval":"renewed"}\n').hexdigest()


@dataclass(frozen=True)
class ForgedDatabaseSource:
    """Offers a backup copy that does not match its expected digest."""

    name: str = "prompt-database"
    rules: frozenset[str] = frozenset({"corpus-database", "corpus-snapshots"})
    referents: frozenset[str] = frozenset()

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        relative = "tmp/.vault-forged/backup.sqlite3"
        write_file(layout.root, relative, b"not the database")
        yield ReplicationItem(
            path=CORPUS_DATABASE_PATH,
            file_class="prompt-database",
            kind="file",
            mode=0o600,
            expected_sha256="0" * 64,
            expected_size=None,
            root=layout.root,
            source=relative,
        )


def test_a_database_backup_that_fails_its_read_is_not_carried(
    library: AssetLayout, vault: Vault
) -> None:
    sources = tuple(
        ForgedDatabaseSource() if s.name == "prompt-database" else s for s in library_sources()
    )
    result = push(library, vault, sources=sources)
    owner = {owner.owner: owner for owner in result.owners}["prompt-database"]
    assert (owner.state, owner.problem_path) == ("refused", CORPUS_DATABASE_PATH)
    assert CORPUS_DATABASE_PATH not in by_path(manifest_of(vault, result.head))
    # The push's spool scratch is gone; the forged file and the stale scratch stay.
    assert sorted(path.name for path in (library.root / "tmp").iterdir()) == [
        ".vault-0123456789abcdef",
        ".vault-forged",
    ]


# -- the head checks -------------------------------------------------------------------------


def test_a_rolled_back_or_removed_head_is_refused_and_nothing_is_sent(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    push(library, vault)
    first_head = vault.head_file.read_bytes()
    change(library, "2")
    push(library, vault)
    vault.head_file.write_bytes(first_head)
    change(library, "3")
    blobs, entries = set(vault.blob_files()), ledger(library)

    with pytest.raises(StorageIntegrityError, match=r"\(rollback\).*older than version 2"):
        push(library, vault)
    with pytest.raises(StorageIntegrityError, match=r"\(rollback\)"):
        verify(library, vault)
    # A restore holds the head against the same records, so the rolled-back generation is
    # never published at the target.
    target = new_target(tmp_path)
    with pytest.raises(StorageIntegrityError, match=r"\(rollback\).*older than version 2"):
        restore(target, vault, records=VaultRecords(library))
    assert not target.exists() and siblings(target) == []
    # A Mac that keeps no records of this library has no anchor and takes what the vault holds.
    assert restore(target, vault).generation == 1
    vault.head_file.unlink()
    with pytest.raises(StorageIntegrityError, match=r"\(rollback\).*has no head"):
        push(library, vault)
    assert set(vault.blob_files()) == blobs and ledger(library) == entries


def test_a_head_this_mac_did_not_leave_is_a_fork(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    first = push(library, vault)
    change(library, "2")
    second = push(library, vault)
    assert first.head is not None and second.head is not None
    kept = vault.head_file.read_bytes()

    vault.refs.swap(HEAD, expected_version=2, target=first.head.target)
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*did not start"):
        push(library, vault)
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*push again to finish it"):
        verify(library, vault)

    sideways = second.head.model_copy(update={"target": first.head.target})
    vault.head_file.write_bytes(canonical_json_bytes(sideways) + b"\n")
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*names another manifest"):
        push(library, vault)

    ahead = second.head.model_copy(update={"version": 4})
    vault.head_file.write_bytes(canonical_json_bytes(ahead) + b"\n")
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*left it at version 2$"):
        push(library, vault)
    vault.head_file.write_bytes(kept)
    assert push(library, vault).outcome == "up-to-date"

    # Another Mac holding the same library finds a head it never pushed.
    other = AssetLayout.initialize(tmp_path.resolve() / "other" / "assets")
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*left it at version 0$"):
        push(other, vault)
    fresh = Vault.create(tmp_path.resolve() / "fresh-vault")
    assert push(other, fresh).generation == 1
    third = AssetLayout.initialize(tmp_path.resolve() / "third" / "assets")
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*did not start"):
        push(third, fresh)


def test_a_head_that_does_not_open_is_refused(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    first = push(library, vault)
    assert first.head is not None
    wrong_key = vault.sealed(key=OTHER_KEY)
    with pytest.raises(StorageIntegrityError, match=r"\(undecryptable\)"):
        push(library, vault, blob_store=wrong_key)
    with pytest.raises(StorageIntegrityError, match=r"\(undecryptable\)"):
        verify(library, vault, blob_store=wrong_key)
    target = new_target(tmp_path)
    with pytest.raises(StorageIntegrityError, match=r"\(undecryptable\)"):
        restore(target, vault, blob_store=wrong_key)
    assert not target.exists() and siblings(target) == []

    envelope = vault.blob_file(first.head.target)
    damaged = bytearray(envelope.read_bytes())
    damaged[-1] ^= 1
    envelope.write_bytes(bytes(damaged))
    with pytest.raises(StorageIntegrityError, match=r"\(undecryptable\)"):
        push(library, vault)
    envelope.unlink()
    with pytest.raises(StorageIntegrityError, match=r"\(missing\)"):
        push(library, vault)


def test_a_head_whose_manifest_is_not_the_one_the_state_names_is_refused(
    library: AssetLayout, vault: Vault
) -> None:
    push(library, vault)
    path = state_path(library)
    original = path.read_bytes()
    state = ReplicationState.model_validate_json(original)
    for changes in ({"manifest_sha256": "0" * 64}, {"generation": 7}):
        write_canonical_json(path, state.model_copy(update=changes))
        with pytest.raises(StorageIntegrityError, match=r"\(digest-mismatch\)"):
            push(library, vault)
        with pytest.raises(StorageIntegrityError, match=r"\(digest-mismatch\)"):
            verify(library, vault)
    path.write_bytes(original)
    assert push(library, vault).outcome == "up-to-date"


def test_a_generation_that_does_not_follow_the_kept_one_is_refused(
    library: AssetLayout, vault: Vault
) -> None:
    push(library, vault)
    change(library, "2")
    push(library, vault)
    copy = VaultRecords(library).manifest_copy_path(1)
    kept = LibraryManifest.model_validate_json(copy.read_bytes())
    altered = kept.model_copy(update={"created_at": kept.created_at + timedelta(seconds=1)})
    rewrite(copy, canonical_json_bytes(altered) + b"\n")

    with pytest.raises(StorageIntegrityError, match=r"\(chain\)"):
        push(library, vault)
    with pytest.raises(StorageIntegrityError, match=r"\(chain\)"):
        verify(library, vault)
    assert len(ReplicationLedger(library).entries()) == 4


def test_a_newer_kept_manifest_beside_an_older_head_is_a_rollback(
    library: AssetLayout, vault: Vault
) -> None:
    push(library, vault)
    state, head = state_path(library).read_bytes(), vault.head_file.read_bytes()
    change(library, "2")
    push(library, vault)
    vault.head_file.write_bytes(head)
    state_path(library).write_bytes(state)
    with pytest.raises(StorageIntegrityError, match=r"\(rollback\).*generation 2's manifest"):
        push(library, vault)

    state_path(library).unlink()
    vault.head_file.unlink()
    with pytest.raises(StorageIntegrityError, match=r"\(rollback\).*generation 1's manifest"):
        push(library, vault)


def two_files(library: AssetLayout) -> tuple[str, str, str]:
    small = "provenance/headphone-reviews/small.json"
    left = "provenance/headphone-reviews/left.json"
    right = "provenance/headphone-reviews/right.json"
    write_file(library.root, small, b"s" * 100)
    write_file(library.root, left, b"l" * (2 * CHUNK))
    write_file(library.root, right, b"r" * (2 * CHUNK))
    return small, left, right


def test_a_host_that_swaps_stored_chunks_is_refused_on_every_read(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    _, left, right = two_files(library)
    first = push(library, vault)
    stored = read_stored_manifest(vault.blobs, first.head.target, library_id=LIBRARY_ID)  # type: ignore[union-attr]
    entries = by_path(stored.manifest)
    one, two = vault.blob_file(entries[left].chunks[0]), vault.blob_file(entries[right].chunks[0])
    one_bytes, two_bytes = one.read_bytes(), two.read_bytes()
    one.write_bytes(two_bytes)
    two.write_bytes(one_bytes)

    report = verify(library, vault)
    states = {entry.path: entry.state for entry in report.entries}
    assert states[left] == states[right] == "mismatch" and report.exit_status == 1
    assert {state for path, state in states.items() if path not in (left, right)} <= {"present"}
    target = new_target(tmp_path)
    with pytest.raises(StorageIntegrityError, match="left.json: chunk 1 of 2"):
        restore(target, vault)
    assert not target.exists() and siblings(target) == []
    one.write_bytes(one_bytes)
    two.write_bytes(two_bytes)

    piece = vault.blob_file(stored.envelope.chunks[0])
    piece_bytes = piece.read_bytes()
    piece.write_bytes(one_bytes)
    with pytest.raises(StorageIntegrityError, match=r"\(undecryptable\).*manifest chunk 1"):
        push(library, vault)
    with pytest.raises(StorageIntegrityError, match=r"\(undecryptable\)"):
        verify(library, vault)
    with pytest.raises(StorageIntegrityError, match=r"\(undecryptable\)"):
        restore(target, vault)
    piece.write_bytes(piece_bytes)
    assert verify(library, vault).ok


def test_a_store_answering_with_another_valid_blob_is_caught_by_the_digests(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    small, left, right = two_files(library)
    first = push(library, vault)
    change(library, "2")
    second = push(library, vault)
    assert first.head is not None and second.head is not None
    stored = read_stored_manifest(vault.blobs, second.head.target, library_id=LIBRARY_ID)
    entries = by_path(stored.manifest)

    def answering(swap: dict[str, BlobRef]) -> Any:
        return vault.sealed(HookedBlobStore(vault.local, swap=swap))

    equal = answering({entries[left].chunks[0].sha256: entries[right].chunks[0]})
    report = verify(library, vault, blob_store=equal)
    found = {entry.path: entry for entry in report.entries}
    assert found[left].state == "mismatch" and "do not reassemble" in (found[left].detail or "")
    assert found[right].state == "present"
    target = new_target(tmp_path)
    with pytest.raises(StorageIntegrityError, match="left.json: its chunks do not reassemble"):
        restore(target, vault, blob_store=equal)
    assert not target.exists() and siblings(target) == []

    larger = answering({entries[small].chunks[0].sha256: entries[right].chunks[0]})
    found = {entry.path: entry for entry in verify(library, vault, blob_store=larger).entries}
    assert "hold more than its 100 bytes" in (found[small].detail or "")

    manifest_piece = answering({stored.envelope.chunks[0].sha256: entries[right].chunks[0]})
    with pytest.raises(StorageIntegrityError, match=r"\(digest-mismatch\)"):
        push(library, vault, blob_store=manifest_piece)

    older = answering({second.head.target.sha256: first.head.target})
    with pytest.raises(StorageIntegrityError, match=r"\(digest-mismatch\).*generation 1"):
        push(library, vault, blob_store=older)
    with pytest.raises(StorageIntegrityError, match=r"\(rollback\)"):
        restore(
            target,
            vault,
            blob_store=older,
            generation=2,
            manifest_sha256=second.manifest_sha256,
        )

    chunk_as_head = answering({second.head.target.sha256: entries[left].chunks[0]})
    with pytest.raises(StorageIntegrityError, match=r"\(not-an-envelope\)"):
        verify(library, vault, blob_store=chunk_as_head)
    assert not target.exists()


def test_a_stored_manifest_is_proven_before_it_is_read(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = vault.blobs
    manifest = LibraryManifest(
        library_id=LIBRARY_ID,
        generation=1,
        previous_manifest_sha256=None,
        created_at=FIXED,
        classes=("records",),
        entries=(),
    )
    data = canonical_json_bytes(manifest)
    stored = store_manifest(blobs, manifest, chunk_bytes=16)
    assert len(stored.envelope.chunks) == -(-len(data) // 16)
    assert read_stored_manifest(blobs, stored.ref, library_id=LIBRARY_ID) == stored
    assert stored.digest == manifest_digest(manifest)

    def envelope_ref(**changes: Any) -> BlobRef:
        return blobs.put(canonical_json_bytes(stored.envelope.model_copy(update=changes)))

    def pieces_ref(payload: bytes, **changes: Any) -> BlobRef:
        chunks = tuple(blobs.put(payload[i : i + 16]) for i in range(0, len(payload), 16))
        fields = {
            "chunks": chunks,
            "size": len(payload),
            "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        }
        return envelope_ref(**(fields | changes))

    forged = LibraryManifest.model_construct(
        **dict(manifest)
        | {
            "entries": (
                ManifestEntry.model_construct(
                    path="provenance/vault/state.json",
                    file_class="records",
                    kind="file",
                    sha256=hashlib.sha256(b"").hexdigest(),
                    size=0,
                    mode=0o600,
                    pin_source=None,
                    chunks=(),
                ),
            )
        }
    )
    other = store_manifest(
        blobs, manifest.model_copy(update={"library_id": "other"}), chunk_bytes=64
    )
    absent = BlobRef(sha256="0" * 64, size=100)
    cases = [
        (other.ref, "foreign-library", "for library other"),
        (
            blobs.put(stored.envelope.model_dump_json(indent=1).encode()),
            "not-an-envelope",
            "canonical",
        ),
        (blobs.put(b"{}"), "not-an-envelope", "not a valid ManifestEnvelope"),
        (envelope_ref(size=stored.envelope.size - 1), "digest-mismatch", "hold more than"),
        (envelope_ref(size=stored.envelope.size + 1), "digest-mismatch", "do not reassemble"),
        (envelope_ref(manifest_sha256="0" * 64), "digest-mismatch", "do not reassemble"),
        (envelope_ref(generation=2), "not-a-manifest", "not the generation 2"),
        (pieces_ref(manifest.model_dump_json(indent=1).encode()), "not-a-manifest", "canonical"),
        (pieces_ref(canonical_json_bytes(forged)), "not-a-manifest", "not a valid LibraryManifest"),
        (envelope_ref(chunks=(absent,)), "missing", "manifest chunk 1"),
        (absent, "missing", "the manifest envelope"),
    ]
    for ref, problem, detail in cases:
        with pytest.raises(StorageIntegrityError, match=rf"\({problem}\): .*{detail}"):
            read_stored_manifest(blobs, ref, library_id=LIBRARY_ID)

    down = vault.sealed(
        HookedBlobStore(vault.local, failure=storage_error("STORAGE.UNAVAILABLE", "down"))
    )
    with pytest.raises(StorageError) as unavailable:
        read_stored_manifest(down, stored.ref, library_id=LIBRARY_ID)
    assert unavailable.value.code == "STORAGE.UNAVAILABLE"

    counting = HookedBlobStore(vault.local)
    with pytest.raises(ValueError, match="at least 1 byte, not 0"):
        store_manifest(vault.sealed(counting), manifest, chunk_bytes=0)
    monkeypatch.setattr(generations, "MAX_MANIFEST_BYTES", len(data) - 1)
    with pytest.raises(StorageError) as too_large:
        store_manifest(vault.sealed(counting), manifest, chunk_bytes=16)
    assert too_large.value.code == "STORAGE.TOO_LARGE" and counting.puts == []


# -- interrupted pushes ----------------------------------------------------------------------


def crash_on(kind: str | None = None) -> Callable[..., Any]:
    def crash(*_: Any, **fields: Any) -> Any:
        raise OSError(errno.EIO, "the disk went away")

    real = ReplicationLedger.append

    def crash_completed(self: ReplicationLedger, **fields: Any) -> LedgerEntry:
        if fields["kind"] == kind:
            raise OSError(errno.EIO, "the disk went away")
        return real(self, **fields)

    return crash if kind is None else crash_completed


def test_a_push_interrupted_after_its_head_swap_is_finished_by_the_next_push(
    library: AssetLayout, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(VaultRecords, "write_manifest_copy", crash_on())
    with pytest.raises(OSError, match="the disk went away"):
        push(library, vault)
    monkeypatch.undo()
    head = vault.refs.read(HEAD)
    assert head is not None and head.version == 1
    assert VaultRecords(library).read_state(LIBRARY_ID) is None
    assert kinds(library) == ["upload-started"]
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*push again to finish it"):
        verify(library, vault)
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*did not start"):
        push(library, vault, destination=TAILNET_DESTINATION)

    # Only the head whose manifest holds exactly the files the started entry named is this
    # Mac's own interrupted push; another holder of the key could chain one from the same
    # state, and adopting it would write a foreign manifest as this Mac's own.
    started_payload = ledger(library)[0].payload_sha256
    rewrite_entry(library, 1, payload_sha256="0" * 64)
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*did not start"):
        push(library, vault)
    assert VaultRecords(library).read_state(LIBRARY_ID) is None
    assert not (library.root / "provenance/vault/manifests").exists()
    assert kinds(library) == ["upload-started"]
    rewrite_entry(library, 1, payload_sha256=started_payload)

    finished = push(library, vault)
    assert (finished.outcome, finished.generation, finished.adopted_generation) == (
        "up-to-date",
        1,
        1,
    )
    assert finished.head == head
    started, completed = ledger(library)
    assert completed.kind == "upload-completed" and completed.head_version == 1
    assert completed.payload_sha256 == started.payload_sha256
    blobs = vault.blob_files()
    assert (completed.blob_count, completed.byte_count) == (
        len(blobs),
        sum(map(len, blobs.values())),
    )
    assert VaultRecords(library).read_state(LIBRARY_ID).generation == 1  # type: ignore[union-attr]
    assert verify(library, vault).ok


def test_a_push_interrupted_before_its_completed_entry_is_settled(
    library: AssetLayout, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ReplicationLedger, "append", crash_on("upload-completed"))
    with pytest.raises(OSError):
        push(library, vault)
    monkeypatch.undo()
    assert VaultRecords(library).read_state(LIBRARY_ID).generation == 1  # type: ignore[union-attr]
    assert kinds(library) == ["upload-started"]
    settled = push(library, vault)
    assert (settled.outcome, settled.adopted_generation) == ("up-to-date", None)
    assert kinds(library) == ["upload-started", "upload-completed"]

    change(library, "2")
    before = set(vault.blob_files())
    monkeypatch.setattr(ReplicationLedger, "append", crash_on("upload-completed"))
    with pytest.raises(OSError):
        push(library, vault)
    monkeypatch.undo()
    added = set(vault.blob_files()) - before
    assert push(library, vault).outcome == "up-to-date"
    completed = ledger(library)[-1]
    assert (completed.kind, completed.generation, completed.blob_count) == (
        "upload-completed",
        2,
        len(added),
    )
    assert push(library, vault).outcome == "up-to-date"
    assert len(ledger(library)) == 4


def test_a_head_that_moves_during_the_push_is_a_fork(library: AssetLayout, vault: Vault) -> None:
    first = push(library, vault)
    assert first.head is not None
    change(library, "2")
    moved: list[RefValue] = []

    def move_head(data: bytes) -> None:
        if not moved:
            moved.append(vault.refs.swap(HEAD, expected_version=1, target=first.head.target))  # type: ignore[union-attr]

    racing = vault.sealed(HookedBlobStore(vault.local, before_put=move_head))
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*changed while generation 2"):
        push(library, vault, blob_store=racing)
    assert kinds(library) == ["upload-started", "upload-completed", "upload-started"]
    assert VaultRecords(library).read_state(LIBRARY_ID).generation == 1  # type: ignore[union-attr]
    with pytest.raises(StorageIntegrityError, match=r"\(fork\)"):
        push(library, vault)


class DownRefs:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def read(self, name: str) -> RefValue | None:
        return self.vault.refs.read(name)

    def swap(self, name: str, *, expected_version: int | None, target: BlobRef) -> RefValue:
        raise storage_error("STORAGE.UNAVAILABLE", "the vault is unreachable")


def test_a_head_swap_that_fails_for_another_reason_is_passed_on(
    library: AssetLayout, vault: Vault
) -> None:
    with pytest.raises(StorageError) as caught:
        push(library, vault, ref_store=DownRefs(vault))
    assert caught.value.code == "STORAGE.UNAVAILABLE" and caught.value.retryable
    assert kinds(library) == ["upload-started"]
    assert push(library, vault).outcome == "pushed"


# -- the ledger ------------------------------------------------------------------------------


def rewrite_entry(layout: AssetLayout, number: int, **changes: Any) -> None:
    path = ledger_path(layout, number)
    entry = LedgerEntry.model_validate_json(path.read_bytes())
    rewrite(path, canonical_json_bytes(entry.model_copy(update=changes)) + b"\n")


def rechain(layout: AssetLayout, start: int) -> None:
    sequence = start
    while ledger_path(layout, sequence).exists():
        previous = LedgerEntry.model_validate_json(ledger_path(layout, sequence - 1).read_bytes())
        rewrite_entry(layout, sequence, previous_entry_sha256=ledger_entry_digest(previous))
        sequence += 1


@pytest.fixture
def two_generations(library: AssetLayout, vault: Vault) -> AssetLayout:
    push(library, vault)
    change(library, "2")
    push(library, vault)
    change(library, "3")
    return library


def damage_ledger(layout: AssetLayout, damage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    first = ledger_path(layout, 1)
    if damage == "edit":
        rewrite_entry(layout, 1, byte_count=0)
    elif damage == "remove":
        ledger_path(layout, 2).unlink()
    elif damage == "rename":
        first.rename(ledger_path(layout, 9))
    elif damage == "stray":
        write_file(first.parent, "notes.txt")
    elif damage == "non-canonical":
        entry = json.loads(first.read_bytes())
        rewrite(first, json.dumps(entry, indent=1).encode() + b"\n")
    elif damage == "invalid":
        rewrite(first, b"{}\n")
    elif damage == "readable":
        first.chmod(0o644)
    elif damage == "linked":
        outside = first.parent.parent / "outside-copy"
        shutil.copy2(first, outside)
        first.unlink()
        first.symlink_to(outside)
    elif damage == "unpaired":
        rewrite_entry(layout, 2, classes=("records",))
        rechain(layout, 3)
    elif damage == "sequence":
        rewrite_entry(layout, 2, sequence=3)
    elif damage == "payload":
        # The chain's last entry is the one no later entry's digest covers.
        rewrite_entry(layout, 3, payload_sha256="0" * 64)
        rewrite_entry(layout, 4, payload_sha256="0" * 64)
        rechain(layout, 4)
    elif damage == "manifest":
        rewrite_entry(layout, 4, manifest_sha256="0" * 64)
    elif damage == "truncated":
        ledger_path(layout, 4).unlink()
        ledger_path(layout, 3).unlink()
    elif damage == "erased":
        for sequence in (4, 3, 2, 1):
            ledger_path(layout, sequence).unlink()
    elif damage == "oversized":
        monkeypatch.setattr(generations, "MAX_VAULT_RECORD_BYTES", 100)
    elif damage == "vanished":
        real = generations._read_private_file

        def vanish(path: Path, limit: int) -> bytes | None:
            return None if path == ledger_path(layout, 2) else real(path, limit)

        monkeypatch.setattr(generations, "_read_private_file", vanish)
    else:  # pragma: no cover - every damage is listed
        raise AssertionError(damage)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("edit", "entry 00000002 does not chain"),
        ("remove", "entry 00000002 is missing"),
        ("rename", "entry 00000001 is missing"),
        ("stray", "holds notes.txt"),
        ("non-canonical", "not in canonical form"),
        ("invalid", "not a valid LedgerEntry"),
        ("readable", "not a private regular file"),
        ("linked", "cannot be opened safely"),
        ("unpaired", "entry 00000002 completes an upload"),
        ("sequence", "says it is entry 3"),
        ("payload", "files are not the ones its upload started with"),
        ("manifest", "names another manifest of generation 2"),
        ("truncated", "does not record the generation 2 upload"),
        ("erased", "does not record the generation 2 upload"),
        ("oversized", "larger than the 100-byte limit"),
        ("vanished", "vanished while the ledger was read"),
    ],
)
def test_a_tampered_ledger_is_refused_and_never_extended(
    two_generations: AssetLayout,
    vault: Vault,
    damage: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = two_generations
    damage_ledger(layout, damage, monkeypatch)
    names = sorted(path.name for path in ledger_path(layout, 1).parent.iterdir())
    blobs = set(vault.blob_files())
    with pytest.raises(StorageIntegrityError, match=message):
        recorded(layout)
    with pytest.raises(StorageIntegrityError, match=message):
        push(layout, vault)
    assert sorted(path.name for path in ledger_path(layout, 1).parent.iterdir()) == names
    assert set(vault.blob_files()) == blobs


def test_the_ledger_tolerates_crash_leftovers_and_refuses_to_overflow(
    two_generations: AssetLayout, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = two_generations
    write_file(ledger_path(layout, 1).parent, f".00000005.json.tmp-{'a' * 16}")
    assert len(ReplicationLedger(layout).entries()) == 4
    empty = AssetLayout.initialize(layout.root.parent / "empty")
    assert ReplicationLedger(empty).entries() == ()
    assert VaultRecords(empty).read_state(LIBRARY_ID) is None
    assert VaultRecords(empty).read_manifest_copy(1) is None
    assert not (empty.root / "provenance/vault").exists()
    monkeypatch.setattr(generations, "MAX_LEDGER_SEQUENCE", 4)
    blobs = set(vault.blob_files())
    with pytest.raises(StorageError) as full:
        push(layout, vault)
    assert full.value.code == "STORAGE.QUOTA" and "the ledger is full" in str(full.value)
    assert set(vault.blob_files()) == blobs and len(ledger(layout)) == 4


def test_a_state_that_names_another_destination_stops_the_push(
    library: AssetLayout, vault: Vault
) -> None:
    """A binding that moved cannot continue this library's chain: rebind and reset instead."""
    first = push(library, vault)
    state = VaultRecords(library).read_state(LIBRARY_ID)
    assert state is not None and state.destination_sha256 == LOCAL_DESTINATION.digest
    elsewhere = replace(LOCAL_DESTINATION, namespace="spare")
    change(library, "2")

    with pytest.raises(StorageIntegrityError, match="another destination than the binding names"):
        push(library, vault, destination=elsewhere)
    with pytest.raises(StorageIntegrityError, match="rebind"):
        push(library, vault, destination=TAILNET_DESTINATION)
    assert VaultRecords(library).read_state(LIBRARY_ID) == state
    assert len(ledger(library)) == 2
    # Verification takes no destination, so it still reads the generation that was pushed.
    assert verify(library, vault).generation == 1
    assert push(library, vault).generation == first.generation + 1


def test_a_state_that_names_another_ledger_entry_stops_the_push(
    library: AssetLayout, vault: Vault
) -> None:
    """state.json names the entry that recorded it, so editing the ledger's tail shows."""
    push(library, vault)
    change(library, "2")
    push(library, vault)
    state = VaultRecords(library).read_state(LIBRARY_ID)
    assert state is not None
    assert state.ledger_tail_sha256 == ledger_entry_digest(ledger(library)[-1])

    # The last entry is the one no later entry's chain digest covers.
    rewrite_entry(library, 4, byte_count=0)
    for command in (lambda: push(library, vault), lambda: verify(library, vault)):
        with pytest.raises(StorageIntegrityError, match="the entry was changed"):
            command()
    rewrite_entry(library, 4, byte_count=ledger(library)[-1].byte_count)

    # A state that names an entry the ledger never held is refused just as loudly.
    rewrite(
        state_path(library),
        canonical_json_bytes(state.model_copy(update={"ledger_tail_sha256": "0" * 64})) + b"\n",
    )
    with pytest.raises(StorageIntegrityError, match="names ledger entry 0{64}"):
        push(library, vault)


def test_a_push_interrupted_before_its_state_is_finished_by_the_next_one(
    library: AssetLayout, vault: Vault
) -> None:
    """The window between the completed entry and the state rewrite is not a broken ledger."""
    push(library, vault)
    change(library, "2")
    push(library, vault)
    entries = ledger(library)
    state = VaultRecords(library).read_state(LIBRARY_ID)
    assert state is not None and entries[-1].kind == "upload-completed"

    # As a crash between the ledger entry and the second state write leaves it.
    started = entries[-2]
    rewrite(
        state_path(library),
        canonical_json_bytes(
            state.model_copy(update={"ledger_tail_sha256": ledger_entry_digest(started)})
        )
        + b"\n",
    )
    assert verify(library, vault).ok
    change(library, "3")
    third = push(library, vault)

    assert third.generation == 3
    settled = VaultRecords(library).read_state(LIBRARY_ID)
    assert settled is not None
    assert settled.ledger_tail_sha256 == ledger_entry_digest(ledger(library)[-1])


def test_a_full_push_uploads_every_file_again_and_dry_runs_write_nothing(
    library: AssetLayout, vault: Vault
) -> None:
    """``--full`` is the documented fix when verify finds a blob the vault lost."""
    first = push(library, vault)
    assert first.outcome == "pushed"
    blobs = set(vault.blob_files())

    planned = push(library, vault, dry_run=True)
    assert planned.outcome == "up-to-date" and set(vault.blob_files()) == blobs

    full = push(library, vault, full=True)
    assert full.outcome == "pushed" and full.generation == 2
    assert full.uploaded_files == first.uploaded_files and full.reused_files == 0
    # Sealing is randomized, so every re-uploaded chunk is a new blob beside the old one.
    assert set(vault.blob_files()) > blobs
    assert verify(library, vault).ok

    change(library, "2")
    # Everything the dry run must leave exactly as it found it, captured before it runs.
    before_blobs = set(vault.blob_files())
    before_state = VaultRecords(library).read_state(LIBRARY_ID)
    before_manifests = sorted(path.name for path in manifest_copies(library))
    before_ledger = len(ledger(library))
    dry = push(library, vault, dry_run=True)
    assert (dry.outcome, dry.generation, dry.uploaded_files) == ("dry-run", 2, 1)
    assert dry.exit_status == 0 and dry.sent_bytes > 0
    assert set(vault.blob_files()) == before_blobs
    assert VaultRecords(library).read_state(LIBRARY_ID) == before_state
    assert sorted(path.name for path in manifest_copies(library)) == before_manifests
    assert len(ledger(library)) == before_ledger == 4
    assert push(library, vault).generation == 3


def test_a_dry_run_refuses_a_destination_the_state_does_not_name(
    library: AssetLayout, vault: Vault
) -> None:
    """A dry run answers for the push it previews, so it makes the same refusal."""
    push(library, vault)
    change(library, "2")
    elsewhere = replace(LOCAL_DESTINATION, namespace="spare")

    with pytest.raises(StorageIntegrityError, match="another destination than the binding names"):
        push(library, vault, destination=elsewhere, dry_run=True)
    assert push(library, vault, dry_run=True).outcome == "dry-run"


# -- the Mac-side records --------------------------------------------------------------------


def test_unsafe_or_foreign_vault_records_are_refused_never_repaired(
    library: AssetLayout, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = push(library, vault)
    assert first.head is not None
    records = VaultRecords(library)
    path = state_path(library)
    original = path.read_bytes()

    path.chmod(0o644)
    with pytest.raises(StorageIntegrityError, match="state.json is not a private regular file"):
        push(library, vault)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.chmod(0o600)

    other = ReplicationState(
        library_id="other-library",
        generation=1,
        manifest_sha256=first.manifest_sha256,
        head=first.head.model_copy(update={"name": library_head_ref("other-library")}),
        destination_sha256=LOCAL_DESTINATION.digest,
        ledger_tail_sha256=ledger_entry_digest(ledger(library)[-1]),
        updated_at=FIXED,
    )
    write_canonical_json(path, other)
    with pytest.raises(StorageIntegrityError, match="records library other-library"):
        verify(library, vault)
    rewrite(path, json.dumps(json.loads(original), indent=1).encode() + b"\n")
    with pytest.raises(StorageIntegrityError, match="state.json is not in canonical form"):
        push(library, vault)
    rewrite(path, original)

    vault_directory = library.root / "provenance/vault"
    vault_directory.chmod(0o750)
    with pytest.raises(StorageIntegrityError, match="vault is not a private directory"):
        push(library, vault)
    vault_directory.chmod(0o700)

    manifests = vault_directory / "manifests"
    manifests.rename(vault_directory.parent / "manifests-elsewhere")
    manifests.symlink_to(vault_directory.parent / "manifests-elsewhere")
    with pytest.raises(StorageIntegrityError, match="manifests is not a private directory"):
        records.read_manifest_copy(1)
    manifests.unlink()
    (vault_directory.parent / "manifests-elsewhere").rename(manifests)

    kept = records.manifest_copy_path(1)
    write_file(manifests, "00000002.json", kept.read_bytes())
    with pytest.raises(StorageIntegrityError, match="holds generation 1"):
        records.read_manifest_copy(2)
    rewrite(manifests / "00000002.json", b"{}\n")
    with pytest.raises(StorageIntegrityError, match="not a valid LibraryManifest"):
        records.read_manifest_copy(2)
    (manifests / "00000002.json").unlink()
    assert records.read_manifest_copy(0) is None and records.read_manifest_copy(3) is None
    manifest = LibraryManifest.model_validate_json(kept.read_bytes())
    records.write_manifest_copy(manifest)
    with pytest.raises(StorageIntegrityError, match="already keeps a different manifest"):
        records.write_manifest_copy(
            manifest.model_copy(update={"created_at": manifest.created_at + timedelta(1)})
        )

    lock = vault_directory / ".push.lock"
    lock.chmod(0o644)
    with pytest.raises(StorageIntegrityError, match=".push.lock is not a private regular file"):
        push(library, vault)
    lock.unlink()
    lock.symlink_to(vault_directory / "state.json")
    with pytest.raises(StorageIntegrityError, match=".push.lock cannot be opened safely"):
        push(library, vault)
    lock.unlink()

    real_read = os.read
    monkeypatch.setattr(generations.os, "read", lambda fd, size: real_read(fd, size)[:-1] or b"")
    with pytest.raises(StorageIntegrityError, match="changed size while it was read"):
        records.read_state(LIBRARY_ID)
    monkeypatch.undo()
    assert push(library, vault).outcome == "up-to-date"


# -- verifying -------------------------------------------------------------------------------


def test_verify_fetches_every_chunk_and_reports_each_entry(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    publish_corpus(library)
    add_finished_run(library, tmp_path.resolve() / "runtime")
    first = push(library, vault)
    assert first.head is not None
    stored = read_stored_manifest(vault.blobs, first.head.target, library_id=LIBRARY_ID)
    manifest = stored.manifest
    counting = HookedBlobStore(vault.local)
    report = verify(library, vault, blob_store=vault.sealed(counting))

    assert report.ok and report.exit_status == 0 and report.unknown_paths == ()
    assert (report.generation, report.manifest_sha256, report.created_at) == (
        1,
        first.manifest_sha256,
        manifest.created_at,
    )
    assert {entry.path: entry.state for entry in report.entries} == {
        entry.path: "pinned" if entry.kind == "pin" else "present" for entry in manifest.entries
    }
    assert report.count("pinned") == 1 and report.count("missing") == 0
    fetched = {ref.sha256 for ref in counting.gets}
    assert {ref.sha256 for entry in manifest.entries for ref in entry.chunks} <= fetched

    subset = verify(
        library,
        vault,
        paths=["provenance/consents", "provenance/diagnostic-root.json", "nowhere/at-all"],
        classes=["records"],
    )
    assert {entry.path for entry in subset.entries} == {
        CONSENT,
        "provenance/diagnostic-root.json",
    }
    assert subset.unknown_paths == ("nowhere/at-all",) and not subset.ok
    assert subset.exit_status == 1
    training = verify(library, vault, classes=["training"])
    assert {entry.file_class for entry in training.entries} == {"training"}
    assert training.ok

    gate = first_file(manifest, "provenance/pilot-gates/gate-1.json")
    vault.blob_file(gate.chunks[0]).unlink()
    database = first_file(manifest, CORPUS_DATABASE_PATH)
    damaged = vault.blob_file(database.chunks[1])
    damaged.write_bytes(damaged.read_bytes()[:-1])
    found = {entry.path: entry for entry in verify(library, vault).entries}
    assert found[gate.path].state == "missing" and gate.path in (found[gate.path].detail or "")
    assert found[database.path].state == "mismatch"
    assert "chunk 2 of" in (found[database.path].detail or "")

    def unreachable(ref: BlobRef) -> None:
        if ref == database.chunks[0]:
            raise storage_error("STORAGE.UNAVAILABLE", "the vault went away")

    flaky = vault.sealed(HookedBlobStore(vault.local, before_get=unreachable))
    with pytest.raises(StorageError) as down:
        verify(library, vault, blob_store=flaky)
    assert down.value.code == "STORAGE.UNAVAILABLE"


def test_verify_reaches_older_generations_only_through_kept_manifests(
    library: AssetLayout, vault: Vault
) -> None:
    with pytest.raises(StorageError, match="has not pushed a generation") as nothing:
        verify(library, vault)
    assert nothing.value.code == "STORAGE.NOT_FOUND"
    pushed = []
    for number in range(3):
        change(library, str(number))
        pushed.append(push(library, vault))
    assert verify(library, vault, generation=1).manifest_sha256 == pushed[0].manifest_sha256
    assert verify(library, vault, generation=3).generation == 3
    for generation in (0, 4):
        with pytest.raises(StorageError, match="is not in the vault") as unreachable:
            verify(library, vault, generation=generation)
        assert unreachable.value.code == "STORAGE.NOT_FOUND"

    records = VaultRecords(library)
    kept = records.manifest_copy_path(2)
    kept_bytes = kept.read_bytes()
    kept.unlink()
    with pytest.raises(StorageError, match="generation 2's manifest is not kept"):
        verify(library, vault, generation=1)
    write_file(kept.parent, kept.name, kept_bytes)

    oldest = records.manifest_copy_path(1)
    manifest = LibraryManifest.model_validate_json(oldest.read_bytes())
    rewrite(
        oldest,
        canonical_json_bytes(manifest.model_copy(update={"created_at": FIXED})) + b"\n",
    )
    with pytest.raises(StorageIntegrityError, match=r"\(chain\).*generation 1 manifest"):
        verify(library, vault, generation=1)


# -- restoring -------------------------------------------------------------------------------


def test_a_restore_into_an_empty_root_writes_every_file_with_its_mode(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    publish_corpus(library)
    runtime = tmp_path.resolve() / "runtime"
    weights = add_finished_run(library, runtime)
    first = push(library, vault)
    manifest = manifest_of(vault, first.head)
    target = new_target(tmp_path)
    result = restore(target, vault, runtime_root=runtime)

    assert (result.target, result.library_id, result.generation) == (target, LIBRARY_ID, 1)
    assert (result.manifest_sha256, result.created_at) == (
        first.manifest_sha256,
        manifest.created_at,
    )
    counts = {file_class: 0 for file_class in manifest.classes}
    for entry in manifest.entries:
        counts[entry.file_class] += 1
    assert result.class_counts == counts and counts["training"] > 1
    files = [entry for entry in manifest.entries if entry.kind == "file"]
    assert (result.restored_files, result.restored_bytes) == (
        len(files),
        sum(entry.size for entry in files),
    )
    (pin,) = [entry for entry in manifest.entries if entry.kind == "pin"]
    assert result.pins == (PinOutcome(pin.path, PRETRAINED_PIN_SOURCE, "linked"),)
    assert result.pin_missing == ()

    found = tree(target)
    assert {path for path, (kind, _) in found.items() if kind == "file"} == {
        entry.path for entry in manifest.entries
    } | set(OWNER_FILES)
    for path in OWNER_FILES:
        assert found[path] == ("file", 0o600) and (target / path).read_bytes() == b""
    for path in OWNER_DIRECTORIES:
        assert found[path] == ("dir", 0o700)
    for entry in files:
        restored = target / entry.path
        assert hashlib.sha256(restored.read_bytes()).hexdigest() == entry.sha256
        assert found[entry.path] == ("file", entry.mode), entry.path
    assert os.path.samefile(target / pin.path, weights)
    for path, (kind, mode) in found.items():
        if kind == "dir":
            assert mode == (0o500 if path in READ_ONLY_DIRECTORIES else 0o700), path
    assert set(READ_ONLY_DIRECTORIES) <= set(found)
    assert found["derived/datasets"] == found["provenance/pilot-gates"] == ("dir", 0o700)
    assert found["raw/loose-note.txt"] == ("file", 0o400)
    for name in ASSET_DIRECTORIES:
        assert found[name] == ("dir", 0o700)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert "provenance/vault" not in found and siblings(target) == []
    AssetLayout.validate(target)
    with open_catalog_reader(AssetLayout.validate(target)) as restored:
        with open_catalog_reader(library) as live:
            assert restored.list_catalogs() == live.list_catalogs()
            assert restored.list_prompts() == live.list_prompts()


def test_a_restored_raw_session_and_reference_come_back_read_only(
    vault: Vault, tmp_path: Path
) -> None:
    """Cole's recordings live in nested directories under ``raw/`` and ``references/``.

    An accepted raw session and a published reference are the two read-only trees the small
    fixture cannot build, and the only ones whose directories a writer nests, so this is the
    test that fails if a restore hands the recordings back writable.
    """
    fixture = _accepted(private_directory(tmp_path.resolve() / "accepted"))
    _publish(fixture)
    layout = fixture.layout
    session = f"raw/{fixture.receipt.manifest_sha256}"
    reference = "references/cole-neutral-v1"
    assert push(layout, vault).outcome == "pushed"
    target = new_target(tmp_path, "restored")
    restore(target, vault)

    found = tree(target)
    assert found["raw"] == found["references"] == ("dir", 0o700)
    for directory in (session, f"{session}/audio", reference):
        assert found[directory] == ("dir", 0o500), directory
    inside = [path for path in found if path.startswith((f"{session}/", f"{reference}/"))]
    assert f"{session}/audio/000-room-tone.wav" in inside and len(inside) > 5
    assert {found[path] for path in inside} == {("file", 0o400), ("dir", 0o500)}
    for path in inside:
        if found[path][0] == "file":
            assert (target / path).read_bytes() == (layout.root / path).read_bytes(), path


def test_a_restore_refuses_a_target_that_is_not_an_empty_directory(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    push(library, vault)
    target = new_target(tmp_path)
    write_file(target, "note.txt")
    base = tmp_path.resolve()
    private_directory(base / "real")
    (base / "link").symlink_to(base / "real")
    write_file(base / "real", "file")
    (base / "real" / "symlink").symlink_to(base / "real" / "file")
    refusals = [
        (target, "target-not-empty", "is not empty"),
        (base / "real" / "file", "unsafe-target", "not a directory of this user"),
        (base / "real" / "symlink", "unsafe-target", "not a directory of this user"),
        (base / "missing" / "assets", "unsafe-target", "does not exist"),
        (base / "real" / "file" / "assets", "unsafe-target", "without symbolic links"),
        (base / "link" / "assets", "unsafe-target", "without symbolic links"),
        (Path("/tmp/assets"), "unsafe-target", "not specific enough"),
    ]
    for path, problem, message in refusals:
        with pytest.raises(RestoreRefusedError, match=message) as refused:
            restore(path, vault)
        assert refused.value.problem == problem, path
    for path in (Path("relative/assets"), Path("/private/tmp/../assets/x/y")):
        with pytest.raises(ValueError, match="absolute and normalized"):
            restore(path, vault)
    assert [path.name for path in target.iterdir()] == ["note.txt"]
    assert siblings(target) == [] and not (base / "real" / "assets").exists()

    empty = new_target(tmp_path, "empty")
    empty.mkdir(mode=0o700)
    assert restore(empty, vault).generation == 1
    assert (empty / "provenance" / "consents").is_dir() and siblings(empty) == []


def test_a_restore_needs_room_for_the_library_and_a_margin(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    runtime = tmp_path.resolve() / "runtime"
    add_finished_run(library, runtime, pinned=False)
    first = push(library, vault)
    manifest = manifest_of(vault, first.head)
    files = sum(entry.size for entry in manifest.entries if entry.kind == "file")
    everything = sum(entry.size for entry in manifest.entries)
    assert everything > files
    asked: list[Path] = []

    def free(required: int) -> Callable[[Path], int]:
        def answer(path: Path) -> int:
            asked.append(path)
            return required

        return answer

    target = new_target(tmp_path)
    for needed, options in ((files, {}), (everything, {"runtime_root": runtime})):
        required = needed + needed // 10 + RESTORE_MARGIN_BYTES
        with pytest.raises(RestoreRefusedError, match=f"needs {required} bytes") as refused:
            restore(target, vault, free_bytes=free(required - 1), **options)
        assert refused.value.problem == "insufficient-space"
        assert not target.exists() and siblings(target) == []
    assert asked == [target.parent, target.parent]
    required = files + files // 10 + RESTORE_MARGIN_BYTES
    assert restore(target, vault, free_bytes=free(required)).generation == 1


def test_a_restore_takes_exactly_the_generation_it_names(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    empty_target = new_target(tmp_path, "nothing")
    with pytest.raises(StorageError, match="holds no generation") as nothing:
        restore(empty_target, vault)
    assert nothing.value.code == "STORAGE.NOT_FOUND"
    write_file(library.root, CONSENT, b'{"consent":"first"}\n')
    first = push(library, vault)
    write_file(library.root, CONSENT, b'{"consent":"second"}\n')
    second = push(library, vault)
    target = new_target(tmp_path)
    digest = second.manifest_sha256
    for options in (
        {"accept_latest": False},
        {"accept_latest": True, "generation": 2, "manifest_sha256": digest},
        {"accept_latest": True, "generation": 2},
        {"generation": 2},
        {"manifest_sha256": digest},
    ):
        with pytest.raises(ValueError, match="name the generation to restore"):
            restore_library(
                target,
                blob_store=vault.blobs,
                ref_store=vault.refs,
                library_id=LIBRARY_ID,
                **options,  # type: ignore[arg-type]
            )
    with pytest.raises(StorageError) as unreachable:
        restore(target, vault, generation=1, manifest_sha256=first.manifest_sha256)
    assert unreachable.value.code == "STORAGE.NOT_FOUND"
    with pytest.raises(StorageIntegrityError, match=r"\(digest-mismatch\)"):
        restore(target, vault, generation=2, manifest_sha256=first.manifest_sha256)
    with pytest.raises(StorageIntegrityError, match=r"\(rollback\)"):
        restore(target, vault, generation=3, manifest_sha256=digest)
    assert not target.exists() and siblings(target) == []

    exact = restore(target, vault, generation=2, manifest_sha256=digest)
    assert exact.generation == 2
    assert (target / CONSENT).read_bytes() == b'{"consent":"second"}\n'
    older = new_target(tmp_path, "older")
    kept = restore(
        older,
        vault,
        generation=1,
        manifest_sha256=first.manifest_sha256,
        records=VaultRecords(library),
    )
    assert (kept.generation, kept.manifest_sha256) == (1, first.manifest_sha256)
    assert (older / CONSENT).read_bytes() == b'{"consent":"first"}\n'
    assert kept.created_at < exact.created_at


def forge_head(vault: Vault, manifest: LibraryManifest) -> StoredManifest:
    """Store ``manifest`` (validated or not) and point the head at it."""
    stored = store_manifest(vault.blobs, manifest, chunk_bytes=CHUNK)
    current = vault.refs.read(HEAD)
    vault.refs.swap(
        HEAD, expected_version=None if current is None else current.version, target=stored.ref
    )
    return stored


def test_a_restore_holds_the_head_against_the_records_this_mac_keeps(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    first = push(library, vault)
    change(library, "2")
    second = push(library, vault)
    records = VaultRecords(library)
    manifest = manifest_of(vault, second.head)

    # Records that name no generation are no anchor: such a Mac takes what the vault holds.
    fresh = VaultRecords(AssetLayout.initialize(tmp_path.resolve() / "fresh" / "assets"))
    assert restore(new_target(tmp_path, "fresh-restore"), vault, records=fresh).generation == 2

    sideways = manifest.model_copy(update={"created_at": manifest.created_at + timedelta(1)})
    forge_head(vault, sideways)
    with pytest.raises(StorageIntegrityError, match=r"\(digest-mismatch\).*pushed generation 2"):
        restore(new_target(tmp_path, "sideways"), vault, records=records)

    unchained = manifest.model_copy(
        update={"generation": 3, "previous_manifest_sha256": first.manifest_sha256}
    )
    forge_head(vault, unchained)
    with pytest.raises(StorageIntegrityError, match=r"\(fork\).*does not follow the generation 2"):
        restore(new_target(tmp_path, "unchained"), vault, records=records)
    for name in ("sideways", "unchained"):
        assert siblings(new_target(tmp_path, name)) == []


@pytest.mark.parametrize(
    ("path", "model_refuses"),
    [
        ("provenance/vault/state.json", True),
        ("PROVENANCE/Vault/ledger/00000001.json", True),
        ("raw//double.wav", True),
        ("raw/../escape.wav", True),
        ("notes/outside.json", False),
        ("tmp", False),
    ],
)
def test_a_manifest_naming_a_path_restore_never_writes_is_refused(
    library: AssetLayout,
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    model_refuses: bool,
) -> None:
    first = push(library, vault)
    good = manifest_of(vault, first.head)
    template = first_file(good)
    forged_entry = ManifestEntry.model_construct(**dict(template) | {"path": path})
    forged = LibraryManifest.model_construct(
        **dict(good)
        | {
            "generation": 2,
            "previous_manifest_sha256": first.manifest_sha256,
            "entries": tuple(sorted((*good.entries, forged_entry), key=lambda entry: entry.path)),
        }
    )
    stored = forge_head(vault, forged)
    target = new_target(tmp_path)
    if model_refuses:
        # The model refuses the path wherever the manifest is read...
        with pytest.raises(StorageIntegrityError, match=r"\(not-a-manifest\)"):
            restore(target, vault)
        assert not target.exists() and siblings(target) == []
        # ... and restore refuses it on its own, for a manifest the model never saw.
        monkeypatch.setattr(
            generations,
            "read_stored_manifest",
            lambda *_, **__: StoredManifest(stored.ref, stored.envelope, forged),
        )
    with pytest.raises(RestoreRefusedError) as refused:
        restore(target, vault)
    assert refused.value.problem == "unrestorable-path"
    assert path in str(refused.value) or repr(path) in str(refused.value)
    assert not target.exists() and siblings(target) == []


def test_a_failed_restore_leaves_nothing_at_its_target(
    library: AssetLayout, vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = push(library, vault)
    manifest = manifest_of(vault, first.head)
    entry = first_file(manifest, "provenance/pilot-gates/gate-1.json")
    blob = vault.blob_file(entry.chunks[0])
    blob_bytes = blob.read_bytes()
    blob.unlink()
    target = new_target(tmp_path)
    with pytest.raises(StorageError) as missing:
        restore(target, vault)
    assert missing.value.code == "STORAGE.NOT_FOUND" and entry.path in str(missing.value)
    assert not target.exists() and siblings(target) == []
    target.mkdir(mode=0o700)
    with pytest.raises(StorageError):
        restore(target, vault)
    assert list(target.iterdir()) == [] and siblings(target) == []
    blob.write_bytes(blob_bytes)
    blob.chmod(0o600)

    staged: list[Path] = []

    def publish_fails(staging: Path, destination: Path, existed: bool) -> None:
        staged.append(staging)
        assert stat.S_IMODE((staging / "derived/datasets/pilot-1").stat().st_mode) == 0o500
        raise OSError(errno.EIO, "the rename failed")

    monkeypatch.setattr(generations, "_publish", publish_fails)
    with pytest.raises(OSError, match="the rename failed"):
        restore(target, vault)
    assert staged and not staged[0].exists() and siblings(target) == []
    monkeypatch.undo()

    real_fsync = generations.fsync_directory

    def parent_fsync_fails(path: Path) -> None:
        if path == target.parent:
            raise OSError(errno.EIO, "the directory flush failed")
        real_fsync(path)

    monkeypatch.setattr(generations, "fsync_directory", parent_fsync_fails)
    with pytest.raises(OSError, match="the directory flush failed"):
        restore(target, vault)
    assert (target / CONSENT).exists() and siblings(target) == []


def test_a_target_that_changes_during_the_restore_is_refused(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    push(library, vault)
    target = new_target(tmp_path)
    target.mkdir(mode=0o700)

    def intrude(ref: BlobRef) -> None:
        if not (target / "intruder").exists():
            write_file(target, "intruder")

    intruding = vault.sealed(HookedBlobStore(vault.local, before_get=intrude))
    with pytest.raises(RestoreRefusedError, match="changed while") as refused:
        restore(target, vault, blob_store=intruding)
    assert refused.value.problem == "target-not-empty"
    assert [path.name for path in target.iterdir()] == ["intruder"] and siblings(target) == []

    appearing_target = new_target(tmp_path, "appearing")

    def appear(ref: BlobRef) -> None:
        private_directory(appearing_target)

    appearing = vault.sealed(HookedBlobStore(vault.local, before_get=appear))
    with pytest.raises(RestoreRefusedError, match="appeared while") as appeared:
        restore(appearing_target, vault, blob_store=appearing)
    assert appeared.value.problem == "target-not-empty"
    assert list(appearing_target.iterdir()) == [] and siblings(appearing_target) == []


def test_pinned_weights_are_linked_only_when_the_runtime_holds_the_pinned_bytes(
    library: AssetLayout, vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path.resolve()
    runtime = base / "runtime"
    weights = add_finished_run(library, runtime, "run-pinned")
    add_finished_run(library, runtime, "run-unpinned", pinned=False)
    push(library, vault)
    pinned = "derived/finetune/run-pinned/checkpoints/pretrained_model.safetensors"
    unpinned = "derived/finetune/run-unpinned/checkpoints/pretrained_model.safetensors"

    def outcomes(result: RestoreResult) -> dict[str, tuple[str, str | None]]:
        return {pin.path: (pin.state, pin.reason) for pin in result.pins}

    bare = restore(new_target(tmp_path, "bare"), vault)
    assert outcomes(bare) == {
        pinned: ("missing", "no runtime root was given"),
        unpinned: ("missing", "no runtime root was given"),
    }
    assert len(bare.pin_missing) == 2
    assert not (base / "bare/assets" / pinned).exists()

    linked_root = new_target(tmp_path, "linked")
    linked = restore(linked_root, vault, runtime_root=runtime)
    assert outcomes(linked) == {
        pinned: ("linked", None),
        unpinned: ("missing", "the runtime's weights do not match the run's pin"),
    }
    assert os.path.samefile(linked_root / pinned, weights)
    assert not (linked_root / unpinned).exists()
    assert not [p for p in tree(linked_root) if ".pin-" in p]

    def cross_device(*_: Any, **__: Any) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(generations.os, "link", cross_device)
    copied_root = new_target(tmp_path, "copied")
    copied = restore(copied_root, vault, runtime_root=runtime)
    monkeypatch.undo()
    assert outcomes(copied)[pinned] == ("copied", None)
    assert (copied_root / pinned).read_bytes() == weights.read_bytes()
    assert not os.path.samefile(copied_root / pinned, weights)
    assert tree(copied_root)[pinned] == ("file", 0o600)
    assert not [p for p in tree(copied_root) if ".pin-" in p]

    empty = private_directory(base / "empty-runtime")
    missing = restore(new_target(tmp_path, "missing"), vault, runtime_root=empty)
    assert outcomes(missing)[pinned] == (
        "missing",
        "the runtime has no pretrained/model.safetensors",
    )
    resized = base / "resized-runtime"
    write_file(resized, "pretrained/model.safetensors", b"other weights")
    other = restore(new_target(tmp_path, "resized"), vault, runtime_root=resized)
    assert "is not a regular file of" in (outcomes(other)[pinned][1] or "")
    folder = base / "folder-runtime"
    private_directory(folder / "pretrained" / "model.safetensors")
    shaped = restore(new_target(tmp_path, "folder"), vault, runtime_root=folder)
    assert "is not a regular file of" in (outcomes(shaped)[pinned][1] or "")


def test_pins_whose_run_cannot_name_their_weights_stay_missing(
    library: AssetLayout, vault: Vault, tmp_path: Path
) -> None:
    runtime = tmp_path.resolve() / "runtime"
    add_finished_run(library, runtime)
    run_bytes = (library.root / "derived/finetune/run-finished/run.json").read_bytes()
    run = FineTuneRun.model_validate_json(run_bytes)
    size = len((runtime / "pretrained/model.safetensors").read_bytes())
    fresh = Vault.create(tmp_path.resolve() / "forged-vault")
    runs: dict[str, bytes | None] = {}
    pins: dict[str, tuple[str, str, str]] = {}

    def add(
        run_id: str,
        record: bytes | None,
        *,
        name: str = "pretrained_model.safetensors",
        pin: str | None = None,
        source: str = PRETRAINED_PIN_SOURCE,
    ) -> None:
        runs[run_id] = record
        pins[f"derived/finetune/{run_id}/checkpoints/{name}"] = (
            pin or run.runtime.pretrained_sha256,
            source,
            run_id,
        )

    outside = run.runtime.model_copy(update={"pretrained_path": "/elsewhere/model.safetensors"})
    other = run.runtime.model_copy(update={"pretrained_sha256": "9" * 64})
    add("run-a", None)
    add("run-b", b"{}\n")
    add("run-c", run_bytes, name="pretrained_other.safetensors")
    add("run-d", canonical_json_bytes(run.model_copy(update={"runtime": outside})))
    add("run-e", run_bytes, source="another-run.weights")
    add("run-f", canonical_json_bytes(run.model_copy(update={"runtime": other})))
    entries = []
    for run_id, record in runs.items():
        if record is not None:
            chunks = (fresh.blobs.put(record),)
            entries.append(
                ManifestEntry(
                    path=f"derived/finetune/{run_id}/run.json",
                    file_class="training",
                    kind="file",
                    sha256=hashlib.sha256(record).hexdigest(),
                    size=len(record),
                    mode=0o600,
                    pin_source=None,
                    chunks=chunks,
                )
            )
    for path, (digest, source, _) in pins.items():
        entries.append(
            ManifestEntry(
                path=path,
                file_class="training",
                kind="pin",
                sha256=digest,
                size=size,
                mode=0o600,
                pin_source=source,
                chunks=(),
            )
        )
    forge_head(
        fresh,
        LibraryManifest(
            library_id=LIBRARY_ID,
            generation=1,
            previous_manifest_sha256=None,
            created_at=FIXED,
            classes=("training",),
            entries=tuple(sorted(entries, key=lambda entry: entry.path)),
        ),
    )
    result = restore(new_target(tmp_path), fresh, runtime_root=runtime)
    reasons = {pins[pin.path][2]: (pin.pin_source, pin.reason) for pin in result.pins}
    assert reasons == {
        "run-a": (PRETRAINED_PIN_SOURCE, "the generation holds no run record for these weights"),
        "run-b": (PRETRAINED_PIN_SOURCE, "the run record is not a valid fine-tune run"),
        "run-c": (PRETRAINED_PIN_SOURCE, "the file is not named for the weights its run pins"),
        "run-d": (PRETRAINED_PIN_SOURCE, "the run's pinned weights are not inside its runtime"),
        "run-e": ("another-run.weights", "unknown pin source"),
        "run-f": (PRETRAINED_PIN_SOURCE, "the run pins other weights"),
    }
    assert all(pin.state == "missing" for pin in result.pins)


# -- the round trip ------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundTrip:
    source: AssetLayout
    target: AssetLayout
    group_id: str
    runtime: Path
    vault: Vault
    pushed: PushResult
    report: VerifyReport
    restored: RestoreResult


@pytest.fixture(scope="module")
def round_trip(
    tmp_path_factory: pytest.TempPathFactory, analyzer_build: Any
) -> Iterator[RoundTrip]:
    base = tmp_path_factory.mktemp("round-trip").resolve()
    source = build_legacy_root(base / "library" / "assets").layout
    publish_corpus(source)
    group_id = add_native_takes(source.root, analyzer_build)
    runtime = base / "runtime"
    add_finished_run(source, runtime)
    add_records(source, loose=False)
    add_mac_only_files(source)
    vault = Vault.create(base / "vault")
    arguments: dict[str, Any] = {
        "blob_store": vault.blobs,
        "ref_store": vault.refs,
        "library_id": LIBRARY_ID,
    }
    pushed = push_generation(
        source,
        destination=LOCAL_DESTINATION,
        classes=ALL_CLASSES,
        chunk_bytes=MAX_SEALED_PLAINTEXT_BYTES,
        clock=Clock(),
        **arguments,
    )
    report = verify_generation(source, **arguments)
    target = private_directory(base / "restored") / "assets"
    restored = restore_library(target, accept_latest=True, runtime_root=runtime, **arguments)
    yield RoundTrip(
        source,
        AssetLayout.validate(target),
        group_id,
        runtime,
        vault,
        pushed,
        report,
        restored,
    )
    restore_permissions(base)


def test_the_round_trip_carries_every_copied_file_and_nothing_else(round_trip: RoundTrip) -> None:
    trip = round_trip
    assert trip.pushed.outcome == "pushed" and trip.pushed.exit_status == 0
    assert all(owner.state == "complete" for owner in trip.pushed.owners)
    assert trip.report.ok and trip.report.count("pinned") == 1
    assert trip.restored.pin_missing == () and trip.restored.generation == 1
    manifest = manifest_of(trip.vault, trip.pushed.head)
    assert {"capture-sessions", "recording-takes", "fine-tune-runs", "prompt-database"} <= {
        owner.owner for owner in trip.pushed.owners
    }
    found = tree(trip.target.root)
    assert {path for path, (kind, _) in found.items() if kind == "file"} == {
        entry.path for entry in manifest.entries
    } | set(OWNER_FILES)
    assert all(classify(path).rule.disposition == "mac-only" for path in OWNER_FILES)
    assert all(found[path][0] == "dir" for path in OWNER_DIRECTORIES)
    for entry in manifest.entries:
        if entry.kind == "file" and entry.path != CORPUS_DATABASE_PATH:
            restored = (trip.target.root / entry.path).read_bytes()
            assert restored == (trip.source.root / entry.path).read_bytes(), entry.path
    rejected = [path for path in found if ".rejected/" in path]
    assert rejected and all(found[path] == ("file", 0o600) for path in rejected)
    assert any(path.startswith("tmp/capture-native-") for path in found)
    assert any(path.endswith(".partial.wav") for path in found)
    assert not any(
        path.startswith(
            (
                "provenance/vault",
                "provenance/recording-library",
                "derived/speech",
                "derived/recording-waveforms",
                "tmp/.vault-",
            )
        )
        or path.endswith((".lock", ".lease", "capture.partial.wav", "native-recovery.json"))
        for path in found
        if path not in OWNER_FILES
    )
    again = push_generation(
        trip.source,
        blob_store=trip.vault.blobs,
        ref_store=trip.vault.refs,
        library_id=LIBRARY_ID,
        destination=LOCAL_DESTINATION,
        classes=ALL_CLASSES,
        chunk_bytes=MAX_SEALED_PLAINTEXT_BYTES,
    )
    assert again.outcome == "up-to-date"


def without_revisions(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_revisions(item)
            for key, item in value.items()
            if key
            not in {"index_revision", "next_cursor", "next_prompt_cursor", "next_group_cursor"}
        }
    if isinstance(value, list):
        return [without_revisions(item) for item in value]
    return value


def rebuilt_index(layout: AssetLayout, group_id: str) -> tuple[dict[str, str], dict[str, Any]]:
    """Rebuild the recording index from the library's files; return its rows and histories."""
    for name in ("provenance/recording-library", "provenance/recording-library-discovery"):
        shutil.rmtree(layout.root / name, ignore_errors=True)
    catalogs = (pilot_v1(), released_pilot_v2())
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        for catalog in catalogs:
            publish(layout, index, catalog)
        summary = discover_recording_library(layout, group_id, index=index)
        assert summary["completeness"] == "complete", summary
        rows = {
            row[0]: row[1]
            for row in index.connection.execute(
                "SELECT recording_take_id, recording_take_payload_json FROM recording_takes "
                "ORDER BY recording_take_id"
            )
        }
        histories = {
            canonical_sha256(catalog): without_revisions(
                index.history(canonical_sha256(catalog), page_size=200).model_dump(mode="json")
            )
            for catalog in catalogs
        }
    return rows, histories


def test_the_restored_library_rebuilds_the_same_recording_index(round_trip: RoundTrip) -> None:
    source = rebuilt_index(round_trip.source, round_trip.group_id)
    restored = rebuilt_index(round_trip.target, round_trip.group_id)
    assert restored == source
    rows = source[0]
    payloads = [json.loads(payload) for payload in rows.values()]
    assert any(payload.get("group_id") for payload in payloads)
    assert any(payload.get("legacy_session_id") for payload in payloads)
    assert all(history["takes"] for history in source[1].values())


def test_the_restored_library_reports_the_same_coverage(
    round_trip: RoundTrip, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)
    for options in (
        ["--catalog", CATALOG_ID, "--json"],
        ["--catalog", CATALOG_ID, "--baseline", "pilot-v1"],
        ["--catalog", "pilot-v1", "--json"],
    ):
        printed = []
        for layout in (round_trip.source, round_trip.target):
            status = main(
                [
                    "prompts",
                    "coverage-report",
                    "--asset-root",
                    str(layout.root),
                    "--subject-id",
                    SUBJECT,
                    *options,
                ]
            )
            printed.append((status, capsys.readouterr().out))
        assert printed[0] == printed[1], options
        if "--json" in options:
            assert json.loads(printed[0][1])["covered_prompt_count"] > 0


def test_the_restored_library_selects_the_same_fine_tune_corpus(
    round_trip: RoundTrip, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)
    selections = []
    printed = []
    for layout in (round_trip.source, round_trip.target):
        with open_catalog_reader(layout) as reader:
            reference = reader.resolve_reference(CATALOG_ID)
            snapshot = reader.materialize(reference)
        selections.append(
            select_finetune_corpus(
                layout, snapshot, SUBJECT, reference=reference, clock=lambda: FIXED
            )
        )
        status = main(
            [
                "finetune",
                "corpus",
                "--asset-root",
                str(layout.root),
                "--catalog",
                CATALOG_ID,
                "--allow-missing",
            ]
        )
        printed.append((status, capsys.readouterr().out))
    assert selections[0] == selections[1]
    assert {entry.source_kind for entry in selections[0].entries} == {"native", "legacy"}
    assert selections[0].selected_at == FIXED
    assert printed[0] == printed[1] and printed[0][0] == 0


def test_the_restored_library_opens_with_its_catalog_and_take_store(round_trip: RoundTrip) -> None:
    with open_catalog_reader(round_trip.target) as restored:
        with open_catalog_reader(round_trip.source) as live:
            assert restored.list_catalogs() == live.list_catalogs()
            reference = live.resolve_reference(CATALOG_ID)
            assert restored.resolve_reference(CATALOG_ID) == reference
            assert restored.materialize(reference) == live.materialize(reference)
    live_store = RecordingTakeStore(round_trip.source, read_only=True)
    restored_store = RecordingTakeStore(round_trip.target)
    live_ids = live_store.iter_take_ids_for_projection(after_take_id=None, limit=200)
    assert restored_store.iter_take_ids_for_projection(after_take_id=None, limit=200) == live_ids
    assert live_ids
    for take_id in live_ids:
        assert restored_store.get_take_for_projection(take_id) == (
            live_store.get_take_for_projection(take_id)
        )
    assert restored_store.get_cursor(SUBJECT) == RecordingTakeStore(round_trip.source).get_cursor(
        SUBJECT
    )

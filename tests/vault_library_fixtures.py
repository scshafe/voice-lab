"""Synthetic libraries and an encrypted local vault for the library-generation tests.

A shared helper module (not a test module): ``tests/test_library_replication.py`` builds its
libraries and stores here. The library builders are copies of the Step 5 helpers in
``tests/test_replication_sources.py`` (that module is not imported, so its tests are never
collected twice), reshaped to add to one root. Every library lives under a resolved private
``tmp_path``; all audio is synthetic; the vault key is a fixed synthetic key, never a real one.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from finetune_speech_runs import write_speech_run
from recording_fixtures import write_synthetic_wav
from test_pilot_v2_coverage import (
    SUBJECT,
    pilot_v1,
    prepare_pilot_v2_package,
    released_pilot_v2,
    spoken,
)
from test_recording_takes import Rig

from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.library_generations import DestinationContext
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import RecordingFrameRange, RecordingTake
from cole_voice_lab.recording_takes import RecordingTakeStore
from cole_voice_lab.storage_binding import open_catalog_editor
from cole_voice_lab.storage_local import LocalBlobStore, LocalRefStore
from cole_voice_lab.storage_ports import BlobRef, BlobStore, StorageError
from cole_voice_lab.storage_sealing import LibraryKey, SealedBlobStore

LIBRARY_ID = "cole-library-1"
CATALOG_ID = "cole-pilot"
# Fixed synthetic keys: the vault's, and another library key's.
TEST_KEY = bytes(range(0x20, 0x40))
OTHER_KEY = bytes(range(0x60, 0x80))
LOCAL_DESTINATION = DestinationContext(
    backend="local-directory", endpoint="/Volumes/Library Copy/voice-lab-vault", namespace="cole"
)
TAILNET_DESTINATION = DestinationContext(
    backend="tailnet-vault",
    endpoint="voice-lab-vault.colobus-stargazer.ts.net",
    namespace="cole",
    node_stable_id="nQRJBE7CNTRL",
    tag="tag:voice-lab-vault",
    approval_sha256="1" * 64,
)
RUN_FINISHED = "run-finished"
RUNTIME_WEIGHTS = b"pretrained-weights of the synthetic runtime"
PACKAGE_PROMPTS = ("room-tone-start", "reference-neutral-01")
HEX16 = "0123456789abcdef"


# -- files ---------------------------------------------------------------------------------


def private_directory(path: Path) -> Path:
    """``path`` as a directory; it and any missing parent are created at 0700."""
    missing = [directory for directory in (path, *path.parents) if not directory.exists()]
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    return path


def write_file(root: Path, relative: str, data: bytes = b"{}\n", mode: int = 0o600) -> Path:
    path = root / relative
    private_directory(path.parent)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def restore_permissions(root: Path) -> None:
    """Make every directory 0700 and file 0600 again, so ``tmp_path`` can be removed."""
    for directory, directories, files in os.walk(root):
        for name in directories:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o700)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o600)


def tree(root: Path) -> dict[str, tuple[str, int]]:
    """Every entry below ``root``: ``("dir"|"file"|"link", mode)`` by relative path."""
    found: dict[str, tuple[str, int]] = {}
    for directory, directories, files in os.walk(root):
        for name in (*directories, *files):
            path = Path(directory) / name
            metadata = path.lstat()
            kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "file"
            found[path.relative_to(root).as_posix()] = (kind, metadata.st_mode & 0o7777)
    return found


def file_digests(root: Path, paths: Iterator[str] | list[str]) -> dict[str, str]:
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}


class Clock:
    """A clock that moves one second per reading."""

    def __init__(self, start: datetime = datetime(2026, 9, 17, 12, tzinfo=UTC)) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


# -- libraries -----------------------------------------------------------------------------


def publish_corpus(layout: AssetLayout) -> None:
    """A corpus store with released pilot-v2 published as ``cole-pilot`` version 1."""
    with open_catalog_editor(layout, create=True) as corpus:
        corpus.import_prompt_suite(released_pilot_v2(), source="pilot-v2")
        corpus.create_catalog(CATALOG_ID, purpose="capture-pilot")
        corpus.publish_version(CATALOG_ID)


def add_records(layout: AssetLayout, *, loose: bool = True) -> None:
    """Copied records of every restore mode: private, read-only trees and empty files.

    ``loose`` also puts a file directly in ``raw/`` and ``references/`` (a library a legacy
    projection reads must not have one: it requires every entry of ``raw/`` to be a session).
    """
    root = layout.root
    write_file(root, "provenance/consents/cole-self-v1.json", b'{"consent":"synthetic"}\n')
    write_file(root, "provenance/headphone-reviews/review-1.json", b'{"review":1}\n')
    write_file(root, "provenance/headphone-reviews/empty.json", b"")
    write_file(root, "provenance/transfer-approvals/approval-1.json", b'{"approval":1}\n')
    for relative in (
        "derived/datasets/pilot-1/manifest.json",
        "derived/transfer-plans/plan-1/manifest.json",
        "provenance/pilot-gates/gate-1.json",
        "provenance/diagnostic-root.json",
    ):
        write_file(root, relative, f'{{"record":"{relative}"}}\n'.encode(), mode=0o400)
    (root / "derived/datasets/pilot-1").chmod(0o500)
    (root / "derived/transfer-plans/plan-1").chmod(0o500)
    if loose:
        write_file(root, "raw/loose-note.txt", b"a file beside the raw sessions\n", mode=0o400)
        write_file(root, "references/loose-note.txt", b"a file beside the references\n", mode=0o400)


def add_mac_only_files(layout: AssetLayout) -> None:
    """Files that never leave the Mac, beside the copied ones."""
    root = layout.root
    for relative in (
        "provenance/vault/approvals/approval-1.json",
        f"tmp/.vault-{HEX16}/database-backup.sqlite3",
        "derived/speech/speak-1/speech.json",
        f"derived/recording-waveforms/{'0' * 64}-v2.cvlwave",
        "runtimes/worker.lock",
        ".DS_Store",
    ):
        write_file(root, relative)


def only_group(layout: AssetLayout) -> str:
    (group,) = (layout.root / "provenance/recording-groups").glob("group-*.json")
    return group.stem


def new_rig(layout: AssetLayout, group_id: str) -> Rig:
    rig = Rig(layout, RecordingTakeStore(layout), group_id, now=utc_now() - timedelta(seconds=5))
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    return rig


def pilot_prompt(number: int) -> str:
    return released_pilot_v2().prompts[number].prompt_id


def sealed_draft(rig: Rig, name: str, prompt_id: str) -> RecordingTake:
    rig.now = utc_now() - timedelta(seconds=5)
    return rig.seal(rig.reserve(f"reserve-{name}", prompt_id), f"seal-{name}")


def add_native_takes(root: Path, build: Any) -> str:
    """Native pilot-v2 takes and a package, a trimmed take and an unsealed draft; the group."""
    layout, _ = prepare_pilot_v2_package(root, build, prompt_ids=PACKAGE_PROMPTS)
    group_id = only_group(layout)
    rig = new_rig(layout, group_id)
    edited = sealed_draft(rig, "edit", pilot_prompt(2))
    rig.store.save(
        rig.command(
            edited,
            operation="save-edit",
            frame_range=RecordingFrameRange(source_frames=480, start_frame=0, end_frame=240),
        )
    )
    rig.now = utc_now() - timedelta(seconds=5)
    draft = rig.reserve("reserve-draft", pilot_prompt(3))
    audio = layout.root / "tmp/recording-takes" / draft.take_id
    write_synthetic_wav(audio / "original.wav", 0.01)
    write_file(audio.parent, f"{draft.take_id}/capture.partial.wav", b"RIFF")
    write_file(audio.parent, f"{draft.take_id}/native-recovery.json")
    return group_id


def add_finished_run(
    layout: AssetLayout,
    runtime: Path,
    run_id: str = RUN_FINISHED,
    *,
    weights: bytes = RUNTIME_WEIGHTS,
    pinned: bool = True,
) -> Path:
    """A finished stub run whose checkpoints hard-link the runtime's pretrained weights.

    With ``pinned`` the run pins the weights' real digest (a restore can re-link them);
    otherwise it keeps the helper's synthetic pin, which no file matches. Returns the
    runtime's weights file.
    """
    started = utc_now() - timedelta(minutes=30)
    run = write_speech_run(
        layout,
        run_id,
        catalog=pilot_v1(),
        subject_id=SUBJECT,
        started_at=started,
        finished_at=started + timedelta(minutes=10),
        entries=(spoken(1),),
        clips=(spoken(1),),
        samples=True,
    )
    source = runtime / "pretrained/model.safetensors"
    if not source.exists():
        write_file(runtime, "pretrained/model.safetensors", weights, mode=0o644)
    directory = layout.root / "derived/finetune" / run_id
    if pinned:
        identity = run.runtime.model_copy(
            update={
                "runtime_root": "/synthetic/runtime",
                "pretrained_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
        write_canonical_json(directory / "run.json", run.model_copy(update={"runtime": identity}))
    os.link(source, directory / "checkpoints/pretrained_model.safetensors")
    write_file(directory, "train.log", b"epoch 1\n", mode=0o644)
    return source


# -- the vault ------------------------------------------------------------------------------


@dataclass
class Vault:
    """An encrypted local vault: a sealed local blob store and a local ref store in ``root``."""

    root: Path
    key: bytes = TEST_KEY
    library_id: str = LIBRARY_ID
    clock: Clock = field(default_factory=Clock)

    @classmethod
    def create(cls, root: Path) -> Vault:
        LocalBlobStore(root, create=True)
        LocalRefStore(root, create=True)
        return cls(root)

    @property
    def local(self) -> LocalBlobStore:
        return LocalBlobStore(self.root)

    def sealed(self, inner: BlobStore | None = None, *, key: bytes | None = None) -> BlobStore:
        return SealedBlobStore(
            self.local if inner is None else inner,
            key=LibraryKey(self.key if key is None else key),
            library_id=self.library_id,
        )

    @property
    def blobs(self) -> BlobStore:
        return self.sealed()

    @property
    def refs(self) -> LocalRefStore:
        return LocalRefStore(self.root, clock=self.clock)

    def blob_file(self, ref: BlobRef) -> Path:
        return self.root / "blobs" / ref.sha256[:2] / ref.sha256

    def blob_files(self) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in sorted((self.root / "blobs").glob("*/*"))
            if path.is_file()
        }

    @property
    def head_file(self) -> Path:
        return self.root / "refs" / "libraries.d" / f"{self.library_id}.d" / "head.json"


class HookedBlobStore:
    """A ``BlobStore`` that runs hooks around the calls of another one."""

    def __init__(
        self,
        inner: BlobStore,
        *,
        before_put: Callable[[bytes], None] | None = None,
        before_get: Callable[[BlobRef], None] | None = None,
        swap: dict[str, BlobRef] | None = None,
        failure: StorageError | None = None,
    ) -> None:
        self.inner = inner
        self.before_put = before_put
        self.before_get = before_get
        self.swap = {} if swap is None else swap
        self.failure = failure
        self.puts: list[BlobRef] = []
        self.gets: list[BlobRef] = []

    def put(self, data: bytes) -> BlobRef:
        if self.before_put is not None:
            self.before_put(data)
        ref = self.inner.put(data)
        self.puts.append(ref)
        return ref

    def has(self, ref: BlobRef) -> bool:
        raise AssertionError("a vault read must never trust has()")

    def get(self, ref: BlobRef) -> bytes:
        self.gets.append(ref)
        if self.before_get is not None:
            self.before_get(ref)
        if self.failure is not None:
            raise self.failure
        # A host that answers a ref with another stored blob, bypassing its digest check.
        substitute = self.swap.get(ref.sha256)
        return self.inner.get(ref if substitute is None else substitute)

"""Replication sources and path classification (library vault plan, Step 5).

The scenarios run on synthetic fixture roots: the pilot-v2 legacy root (``build_legacy_root``)
with a published corpus store, a native pilot-v2 package root, an accepted raw session with a
published reference, and the files the fine-tune pipeline and this plan add (a finished stub
run with its hard-linked pretrained weights, a Speak clip, vault records, scratch and temporary
names, locks, SQLite journals, a quarantined index, Finder metadata). Concurrency is driven
through explicit hooks (sink callbacks, a wrapped backup, a wrapped source) and real writer
processes gated by events; nothing sleeps. All audio is synthetic.
"""

from __future__ import annotations

import builtins
import errno
import fcntl
import hashlib
import io
import multiprocessing
import os
import pickle
import shutil
import sqlite3
import subprocess
import sys
import tracemalloc
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from finetune_speech_runs import write_speech_run
from recording_fixtures import write_synthetic_wav
from test_pilot_v2_coverage import (
    SUBJECT,
    UMIK,
    build_legacy_root,
    pilot_v1,
    prepare_pilot_v2_package,
    released_pilot_v2,
    spoken,
)
from test_recording_packages import analyzer_build as analyzer_build
from test_recording_takes import Rig
from test_references import _accepted, _publish

import cole_voice_lab.library_classification as classification_module
import cole_voice_lab.library_replication as replication
import cole_voice_lab.recording_finetune_jobs as finetune_jobs
from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.capture import REJECTED_LOG_NAME, ReadyMarker, RejectedTakeLog
from cole_voice_lab.errors import SecurityError, VoiceLabError
from cole_voice_lab.finetune import (
    FineTuneArchivedFile,
    FineTuneRunArchive,
    FineTuneRunRemoval,
    FineTuneSample,
    FineTuneSampleSet,
)
from cole_voice_lab.intake import accept_capture_session, validate_capture_package
from cole_voice_lab.library_classification import (
    CLASSIFICATION_RULES,
    CORPUS_DATABASE_PATH,
    EVERY_LATER_RULE,
    ClassificationRule,
    classify,
    matching_rules,
    rule_named,
    subtree_stays_on_mac,
)
from cole_voice_lab.library_replication import (
    DEFAULT_CHUNK_BYTES,
    MAX_CHUNK_BYTES,
    PRETRAINED_PIN_SOURCE,
    CaptureSessionSource,
    CarriedPath,
    CorpusDatabaseSource,
    FineTuneRunSource,
    MemosSource,
    OwnerResult,
    PassEntry,
    PassResult,
    RawPackageSource,
    RecordTreeSource,
    ReferenceSource,
    ReplicationItem,
    ReplicationSource,
    SourceChangedError,
    SourceEntry,
    TakeStoreSource,
    WithheldPath,
    backup_database,
    collect_pass,
    library_sources,
    require_classified_tree,
    source_classes,
)
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_coverage import PromptCoverageProjectionError, prompt_coverage_report
from cole_voice_lab.recording_analysis import RecordingAnalysisJob, RecordingAnalysisQueue
from cole_voice_lab.recording_contracts import (
    OpenContextCommand,
    RecordingFrameRange,
    RecordingTake,
    SaveTakeCommand,
    catalog_snapshot_path,
)
from cole_voice_lab.recording_finetune_jobs import (
    SPEECH_ACTIVE_MARKER,
    FineTuneJob,
    FineTuneJobHead,
    SpeechActiveMarker,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_packages import (
    RecordingPackageCommit,
    load_recording_package_map,
)
from cole_voice_lab.recording_takes import (
    RecordingMetadataBusyError,
    RecordingTakeStore,
    write_recording_evidence,
)
from cole_voice_lab.storage_binding import catalog_store_location, open_catalog_editor
from cole_voice_lab.storage_contracts import FileClass, ManifestPlan
from cole_voice_lab.storage_ports import StorageError

ALL_CLASSES: tuple[FileClass, ...] = get_args(FileClass.__value__)
CONTEXT = multiprocessing.get_context("spawn")
CHILD_TIMEOUT = 60
HEX16 = "0123456789abcdef"
HEX32 = HEX16 * 2
SHA = HEX32 * 2
CATALOG_ID = "cole-pilot"
PACKAGE_PROMPTS = ("room-tone-start", "reference-neutral-01")
RUN_FINISHED = "run-finished"
RUNTIME_WEIGHTS = b"pretrained-weights"
# The finished fixture run was archived and one of its checkpoints removed: the pass must
# still offer that file, so the vault keeps naming it.
REMOVED_GENERATION = 3
REMOVED_CHECKPOINT = FineTuneArchivedFile(
    path="checkpoints/model_9.pt", sha256="c" * 64, size=1_024, kind="file"
)
CONSENT_HISTORY_BYTES = b'{"consent":"an earlier synthetic version"}\n'


# -- sinks, passes and trees -------------------------------------------------------------


@dataclass
class Read:
    """One item a pass handed the sink."""

    owner: str
    item: ReplicationItem
    chunks: list[bytes] = field(default_factory=list)
    entry: PassEntry | None = None
    finished: bool = False

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


class RecordingSink:
    """Keeps every item's bytes; optional hooks run inside ``begin`` and ``chunk``."""

    def __init__(
        self,
        *,
        on_begin: Callable[[str, ReplicationItem], None] | None = None,
        on_chunk: Callable[[Read, bytes], None] | None = None,
    ) -> None:
        self.reads: list[Read] = []
        self._on_begin = on_begin
        self._on_chunk = on_chunk

    def begin(self, owner: str, item: ReplicationItem) -> None:
        assert not self.reads or self.reads[-1].finished, "begin before the last item finished"
        self.reads.append(Read(owner, item))
        if self._on_begin is not None:
            self._on_begin(owner, item)

    def chunk(self, data: bytes) -> None:
        read = self.reads[-1]
        read.chunks.append(bytes(data))
        if self._on_chunk is not None:
            self._on_chunk(read, data)

    def finish(self, entry: PassEntry | None) -> None:
        read = self.reads[-1]
        assert not read.finished
        read.entry, read.finished = entry, True

    def carried(self) -> dict[str, bytes]:
        return {read.item.path: read.data for read in self.reads if read.entry is not None}

    def begun(self) -> set[str]:
        return {read.item.path for read in self.reads}


def run_pass(
    layout: AssetLayout,
    sources: tuple[ReplicationSource, ...] | None = None,
    sink: RecordingSink | None = None,
    *,
    classes: tuple[FileClass, ...] = ALL_CLASSES,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> tuple[PassResult, RecordingSink]:
    sink = sink or RecordingSink()
    result = collect_pass(
        layout,
        library_sources() if sources is None else sources,
        sink,
        classes=classes,
        chunk_bytes=chunk_bytes,
    )
    return result, sink


def assert_never_half_copied(sink: RecordingSink) -> None:
    """Every carried item's bytes are complete and are exactly what its entry names."""
    for read in sink.reads:
        assert read.finished
        if read.entry is not None and read.entry.kind == "file":
            assert hashlib.sha256(read.data).hexdigest() == read.entry.sha256, read.item.path
            assert len(read.data) == read.entry.size
        if read.item.kind == "pin":
            assert read.chunks == []


def tree_paths(root: Path) -> list[str]:
    """Every non-directory under ``root``, links included and never followed."""
    found: list[str] = []
    for directory, directories, files in os.walk(root):
        base = Path(directory)
        for name in [*files, *(d for d in directories if (base / d).is_symlink())]:
            found.append((base / name).relative_to(root).as_posix())
    return sorted(found)


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
    for directory, directories, files in os.walk(root):
        for name in directories:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o700)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o600)


@pytest.fixture(autouse=True)
def _writable_tmp_path(tmp_path: Path) -> Iterator[None]:
    yield
    restore_permissions(tmp_path)


def wait_for(event: Any, child: Any) -> bool:
    """Wait for ``event`` while ``child`` lives; a child that died cannot set it any more."""
    for _ in range(CHILD_TIMEOUT * 10):
        if event.wait(0.1):
            return True
        if not child.is_alive():
            return bool(event.is_set())
    return False


def stop(child: Any) -> None:
    child.join(timeout=CHILD_TIMEOUT)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail(f"child process {child.name} did not finish")
    assert child.exitcode == 0


# -- fixture libraries ---------------------------------------------------------------------


def publish_corpus(layout: AssetLayout) -> None:
    with open_catalog_editor(layout, create=True) as corpus:
        corpus.import_prompt_suite(released_pilot_v2(), source="pilot-v2")
        corpus.create_catalog(CATALOG_ID, purpose="capture-pilot")
        corpus.publish_version(CATALOG_ID)


def only_group(layout: AssetLayout) -> str:
    (group,) = (layout.root / "provenance/recording-groups").glob("group-*.json")
    return group.stem


def new_rig(layout: AssetLayout, group_id: str) -> Rig:
    rig = Rig(layout, RecordingTakeStore(layout), group_id, now=utc_now() - timedelta(seconds=5))
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    return rig


def sealed_draft(rig: Rig, name: str, prompt_id: str) -> RecordingTake:
    rig.now = utc_now() - timedelta(seconds=5)
    return rig.seal(rig.reserve(f"reserve-{name}", prompt_id), f"seal-{name}")


def save_take(rig: Rig, take: RecordingTake, name: str) -> RecordingTake:
    return rig.store.save(rig.command(take, operation=f"save-{name}")).take


def pilot_prompt(number: int) -> str:
    return released_pilot_v2().prompts[number].prompt_id


@dataclass(frozen=True)
class NativeLibrary:
    layout: AssetLayout
    group_id: str
    edited: str
    draft: str


def build_native_library(root: Path, build: Any, *, corpus: bool = False) -> NativeLibrary:
    """Native pilot-v2 takes and a package, plus a trimmed take and an unsealed draft."""
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
    write_file(audio.parent, f"{draft.take_id}/.native-recovery-{HEX32}.tmp")
    if corpus:
        publish_corpus(layout)
    return NativeLibrary(layout, group_id, edited.reservation.take_id, draft.take_id)


def add_native_extras(library: NativeLibrary) -> None:
    root = library.layout.root
    # Another subject's workspace, so the store keeps a cursor (cole-self's saves are unaffected).
    catalog = released_pilot_v2()
    RecordingTakeStore(library.layout).open_context(
        OpenContextCommand(
            operation_id="open-context-other-subject",
            subject_id="synthetic-subject",
            catalog=catalog,
            catalog_sha256=canonical_sha256(catalog),
            capture_device=UMIK,
            capture_host="synthetic-host",
            expected_selection_revision=None,
        )
    )
    native = root / "provenance/recording-edits/native"
    (recipe,) = (
        path.stem for path in (native / library.edited).glob("*.json") if path.name != "head.json"
    )
    write_file(native, f"{library.edited}/{recipe}.derivative.json")
    write_file(native, ".render-owner.lock", b"")
    rendered = private_directory(root / "derived/recording-edits/native" / recipe)
    write_synthetic_wav(rendered / "render.wav", 0.01)
    digest = hashlib.sha256((rendered / "render.wav").read_bytes()).hexdigest()
    (rendered / "render.wav").rename(rendered / f"{digest}.wav")
    write_file(rendered, f".trim-{HEX16}.wav", b"RIFF")
    packages = root / "provenance/recording-packages"
    write_file(packages, "jobs/head.json")
    write_file(packages, "jobs/.owner.lock", b"")
    write_file(packages, f"work/prepare-pilot-v2/copy-{library.edited}-ab12.partial", b"RIFF")
    write_file(root, "tmp/.native-package-stage-native-1/audio/000-room-tone.wav", b"RIFF")
    write_file(root, f"provenance/recording-library-discovery/{SHA}.json")
    write_file(root, f"provenance/recording-library-discovery/{SHA}.result.json")
    write_file(root, f"provenance/recording-groups/.prepared-gates/{SHA}.json")
    write_file(root, "provenance/recording-library/recording-library.sqlite3-journal", b"")


def add_fine_tune_runs(layout: AssetLayout, runtime: Path) -> None:
    root = layout.root
    started = utc_now() - timedelta(minutes=30)
    write_speech_run(
        layout,
        RUN_FINISHED,
        catalog=pilot_v1(),
        subject_id=SUBJECT,
        started_at=started,
        finished_at=started + timedelta(minutes=10),
        entries=(spoken(1),),
        clips=(spoken(1),),
        samples=True,
    )
    run = root / "derived/finetune" / RUN_FINISHED
    weights = write_file(runtime, "pretrained/model.safetensors", RUNTIME_WEIGHTS)
    os.link(weights, run / "checkpoints/pretrained_model.safetensors")
    write_file(run, "train.log", b"epoch 1\n", mode=0o644)
    archived_at = started + timedelta(minutes=20)
    write_canonical_json(
        run / "archived.json",
        FineTuneRunArchive(
            run_id=RUN_FINISHED,
            library_id="lib-20260917-0123456789ab",
            destination_sha256="a" * 64,
            generation=REMOVED_GENERATION,
            manifest_sha256="b" * 64,
            archived_at=archived_at,
            files=(REMOVED_CHECKPOINT,),
        ),
    )
    write_canonical_json(
        run / "local-removal.json",
        FineTuneRunRemoval(
            run_id=RUN_FINISHED,
            generation=REMOVED_GENERATION,
            manifest_sha256="b" * 64,
            removed_at=archived_at,
            files=(REMOVED_CHECKPOINT,),
        ),
    )
    write_file(run, "local-removal-restored-1.json")
    write_file(run, f".run.json.tmp-{HEX16}")
    write_file(run, "samples/finetuned/synthesis-tmp/reference.wav", b"RIFF")
    write_speech_run(
        layout,
        "run-training",
        catalog=pilot_v1(),
        subject_id=SUBJECT,
        started_at=started,
        finished_at=None,
        entries=(spoken(1),),
        clips=(spoken(1),),
        status="running",
    )
    write_file(root, "derived/finetune/run-empty/train.log", b"starting\n")
    write_file(root, "derived/finetune/.DS_Store", b"")
    jobs = root / "provenance/finetune-jobs"
    write_file(jobs, "requests/finetune-1.json")
    write_file(jobs, "terminal/finetune-1.json")
    write_file(jobs, ".owner.lock", b"")
    write_recording_evidence(jobs / "head.json", FineTuneJobHead())


def consent_history_name(data: bytes, consent_id: str = "cole-self-v1") -> str:
    """The name the consent module gives an earlier version: its id and its digest."""
    return f"{consent_id}.{hashlib.sha256(data).hexdigest()}.json"


def add_lab_records(layout: AssetLayout) -> None:
    """Every other kind of file the lab or this plan writes, at its real path."""
    root = layout.root
    for relative in (
        "provenance/vault/destinations/default/binding.json",
        "provenance/vault/destinations/default/state.json",
        "provenance/vault/library.json",
        "provenance/vault/destinations/default/ledger/00000001.json",
        "provenance/vault/destinations/default/manifests/00000001.json",
        "provenance/vault/destinations/default/approvals/approval-1.json",
        "provenance/vault/destinations/default/requests/2026-09-17.jsonl",
        "provenance/vault/destinations/default/.push.lock",
        f"tmp/.vault-{HEX16}/database-backup.sqlite3",
        "tmp/tmpab12cd34",
        f"tmp/.intake-pilot-001-{os.getpid()}/manifest.json",
        f"tmp/.reference-neutral-{HEX16}/reference.wav",
        f"tmp/.dataset-pilot-{HEX16}/manifest.json",
        f"tmp/.transfer-plan-plan-1-{HEX16}/manifest.json",
        f"tmp/blind-review-1-{HEX16}/clip-01.wav",
        "incoming/pilot-001/READY",
        "provenance/intake/sessions/pilot-001.json",
        "provenance/home-host/ledger/00000001.json",
        "runtimes/worker.lock",
        "runtimes/.evicting-runtime-f5-1/python",
        "models/f5-tts/model.safetensors",
        "experiments/run-1/result.json",
        "evaluations/review-1/answers.json",
        "derived/speech/speak-1/speech.json",
        "derived/speech/speak-1/synthesis-tmp/reference.wav",
        f"derived/recording-waveforms/{SHA}-v2.cvlwave",
        f"derived/recording-waveforms/.waveform-{HEX32}.tmp",
        f"provenance/recording-library-quarantine-{HEX32}/recording-library.sqlite3",
        "provenance/recording-library-discovery/checkpoint.json",
        f"provenance/capture-continuity/desktop-1/{HEX32}.json",
        "provenance/recording-legacy-continuations/continue-1.json",
        "provenance/recording-edits/legacy/.operations/edit-1.json",
        f"provenance/recording-edits/legacy/{SHA}/head.json",
        f"provenance/recording-edits/legacy/{SHA}/edit-1.result.json",
        f"provenance/recording-edits/native/.legacy-intent-{SHA}.lock",
        f"provenance/consents/.cole-self-v1.json.tmp-{HEX16}",
        "provenance/transfer-approvals/approval-1.json",
        "provenance/headphone-reviews/review-1.json",
        ".DS_Store",
        "tmp/capture-legacy-pilot/.DS_Store",
    ):
        write_file(root, relative)
    history = CONSENT_HISTORY_BYTES
    write_file(root, f"provenance/consents/history/{consent_history_name(history)}", history)
    for relative in (
        "derived/datasets/pilot-1/manifest.json",
        "derived/transfer-plans/plan-1/manifest.json",
        "provenance/pilot-gates/gate-1.json",
        "provenance/host-transfers/move-1.txt",
        "provenance/diagnostic-root.json",
    ):
        write_file(root, relative, mode=0o400)
    (root / "derived/datasets/pilot-1").chmod(0o500)
    (root / "derived/transfer-plans/plan-1").chmod(0o500)
    speech = root / "derived/speech/speak-1"
    write_synthetic_wav(speech / "speech.wav", 0.01)
    trims = private_directory(root / "derived/recording-edits" / SHA)
    write_synthetic_wav(trims / "trim.wav", 0.01)
    digest = hashlib.sha256((trims / "trim.wav").read_bytes()).hexdigest()
    (trims / "trim.wav").rename(trims / f"{digest}.wav")


@dataclass(frozen=True)
class Libraries:
    legacy: AssetLayout
    native: NativeLibrary
    accepted: AssetLayout

    @property
    def layouts(self) -> tuple[AssetLayout, ...]:
        return (self.legacy, self.native.layout, self.accepted)


@pytest.fixture(scope="module")
def libraries(tmp_path_factory: pytest.TempPathFactory, analyzer_build: Any) -> Iterator[Libraries]:
    base = tmp_path_factory.mktemp("replication-libraries").resolve()
    legacy = build_legacy_root(base / "legacy" / "assets").layout
    publish_corpus(legacy)
    add_fine_tune_runs(legacy, base / "runtime")
    add_lab_records(legacy)
    native = build_native_library(base / "native" / "assets", analyzer_build)
    add_native_extras(native)
    accepted_root = base / "accepted"
    accepted_root.mkdir(mode=0o700)
    fixture = _accepted(accepted_root)
    _publish(fixture)
    libraries = Libraries(legacy, native, fixture.layout)
    # A pass leaves SQLite's shared-memory files beside the database, as the service does.
    run_pass(legacy, (CorpusDatabaseSource(),), classes=("prompt-database",))
    yield libraries
    restore_permissions(base)


# -- classification ------------------------------------------------------------------------


def test_every_rule_classifies_its_own_example_and_names_only_later_rules_it_shadows() -> None:
    names = [rule.name for rule in CLASSIFICATION_RULES]
    for index, rule in enumerate(CLASSIFICATION_RULES):
        assert classify(rule.example).rule is rule, rule.name
        assert rule is rule_named(rule.name)
        if rule.wins_over != (EVERY_LATER_RULE,):
            assert set(rule.wins_over) <= set(names[index + 1 :]), rule.name
        for shadowed in matching_rules(rule.example)[1:]:
            assert EVERY_LATER_RULE in rule.wins_over or shadowed.name in rule.wins_over


def test_the_database_entry_is_where_the_catalog_store_keeps_its_database(
    libraries: Libraries,
) -> None:
    assert CORPUS_DATABASE_PATH == catalog_store_location(libraries.legacy)
    rule = classify(CORPUS_DATABASE_PATH).rule
    assert (rule.disposition, rule.transfer) == ("prompt-database", "sqlite-backup")
    for journal in ("-wal", "-shm", "-journal"):
        assert not classify(f"{CORPUS_DATABASE_PATH}{journal}").copied


def memos_drop(root: Path) -> Path:
    """A synthetic phone memos drop: two memos, the metadata database, the mirror's manifest,
    and an rsync transfer in progress (a hidden temporary and the partial directory)."""
    for relative in (
        "20261009 101500.m4a",
        "20261009 101600.m4a",
        "CloudRecordings.db",
        "_mirror/latest.json",
        ".20261009 101700.m4a.AbC123",
        ".rsync-partial/20261009 101800.m4a",
    ):
        write_file(root, relative, f"memo {relative}\n".encode(), mode=0o640)
    return root


def test_every_fixture_path_is_classified_exactly_once(
    libraries: Libraries, tmp_path: Path
) -> None:
    legacy = libraries.legacy.root
    native = libraries.native.layout.root
    transient = (
        write_file(native, "provenance/recording-takes/.pending.json"),
        write_file(legacy, SPEECH_ACTIVE_MARKER),
    )
    memos = [f"memos/{path}" for path in tree_paths(memos_drop(tmp_path / "phone"))]
    hit: set[str] = set()
    try:
        assert (legacy / f"{CORPUS_DATABASE_PATH}-shm").exists()
        for paths in (*(tree_paths(layout.root) for layout in libraries.layouts), memos):
            for path in paths:
                rules = matching_rules(path)
                assert rules, f"unclassified fixture path {path}"
                first, *later = rules
                for shadowed in later:
                    assert (
                        EVERY_LATER_RULE in first.wins_over or shadowed.name in first.wins_over
                    ), f"{path} matches {first.name} and {shadowed.name}"
                assert classify(path).rule is first
                hit.add(first.name)
    finally:
        for path in transient:
            path.unlink()
    # Every rule meets a real fixture file.
    assert {rule.name for rule in CLASSIFICATION_RULES} - hit == set()


def test_mac_only_subtrees_are_skipped_only_where_nothing_below_is_copied() -> None:
    for index, decided in enumerate(CLASSIFICATION_RULES):
        if decided.subtree:
            assert not any(earlier.copied for earlier in CLASSIFICATION_RULES[:index])
    assert subtree_stays_on_mac("provenance/vault")
    assert subtree_stays_on_mac("provenance/vault/destinations/default/ledger")
    assert subtree_stays_on_mac(f"tmp/.vault-{HEX16}")
    assert subtree_stays_on_mac("derived/finetune/run-1/samples/base/synthesis-tmp")
    assert not subtree_stays_on_mac("derived/finetune/run-1/samples/base")
    assert not subtree_stays_on_mac("provenance")
    assert not subtree_stays_on_mac("tmp")
    assert not subtree_stays_on_mac("provenance/recording-library-discovery-extra")


@pytest.mark.parametrize(
    "path",
    [
        "provenance/stray-notes.txt",
        "provenance/recording-takes/take-XYZ/head.json",
        "tmp/capture-Bad/audio/000-room-tone.wav",
        "raw",
        "../raw/x",
        "raw//x",
        "/raw/x",
    ],
)
def test_an_unclassified_path_is_a_protocol_refusal(path: str) -> None:
    with pytest.raises(StorageError, match="classif") as caught:
        classify(path)
    assert caught.value.code == "STORAGE.PROTOCOL" and not caught.value.retryable


def make_rule(
    name: str, pattern: str, disposition: Any = "mac-only", **changes: Any
) -> ClassificationRule:
    fields: dict[str, Any] = {
        "owner": "test",
        "reason": "test",
        "example": pattern.replace("{id}", "x").replace("{/**}", "").replace("/**", "/x"),
    }
    return ClassificationRule(name, pattern, disposition, **(fields | changes))


@pytest.mark.parametrize(
    ("rules", "problem"),
    [
        ((make_rule("a", "x"), make_rule("a", "y")), "unique"),
        ((make_rule("A", "x"), make_rule("b", "y", "records")), "malformed"),
        ((make_rule("a", "x", "secrets"),), "unknown class"),
        (
            (make_rule("a", "x", transfer="pin"), make_rule("b", "y", "records")),
            "transfer or restore",
        ),
        ((make_rule("a", "x", mode=0o400), make_rule("b", "y", "records")), "transfer or restore"),
        ((make_rule("a", "x", "records", wins_over=("b",)),), "later rules"),
        ((make_rule("a", "x", "records"), make_rule("b", "y{/**}")), "precede every copied"),
        ((make_rule("a", "x", "records", example="/x"),), "absolute"),
        ((make_rule("a", "{id}{/**}"), make_rule("b", "x/y", "records")), "own example"),
    ],
)
def test_a_broken_table_is_refused(rules: tuple[Any, ...], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        classification_module._checked(rules)


@pytest.mark.parametrize(
    ("pattern", "problem"),
    [("x/{unknown}", "unknown pattern part"), ("x/}y", "unbalanced"), ("x/{y", "unbalanced")],
)
def test_a_malformed_pattern_is_refused(pattern: str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        make_rule("a", pattern, example="x")


def test_patterns_match_whole_paths_and_named_parts() -> None:
    subtree = make_rule("a", "tmp/a{/**}")
    assert subtree.subtree and subtree.matches("tmp/a") and subtree.matches("tmp/a/b/c")
    assert not subtree.matches("tmp/ab") and subtree.covers_directory("tmp/a")
    below = make_rule("b", "tmp/b/**")
    assert below.subtree and not below.matches("tmp/b") and below.matches("tmp/b/c")
    copied = make_rule("c", "tmp/c/**", "records")
    assert not copied.subtree and not copied.covers_directory("tmp/c")
    anywhere = make_rule("d", "**/.x", example=".x")
    assert anywhere.matches(".x") and anywhere.matches("a/b/.x") and not anywhere.matches("a.x")
    choice = make_rule("e", "a/{b|c.d}", example="a/b")
    assert choice.matches("a/c.d") and not choice.matches("a/cxd")
    assert classify("raw/x").disposition == "recordings" and classify("raw/x").copied


TAKE = f"take-{SHA}"
# What the plan (its "copied" and "Mac only" lists) and Step 5's decisions say for each kind of
# path, written out here rather than read from the table: (disposition, restore mode, transfer),
# with no mode or transfer for a file that stays on the Mac.
PLAN_DISPOSITIONS: Mapping[str, tuple[str, int | None, str | None]] = {
    # Mac only: locks, journals, recorder scratch, projections, caches, activity markers.
    "provenance/recording-takes/.metadata.lock": ("mac-only", None, None),
    "provenance/recording-takes/.pending.json": ("mac-only", None, None),
    f"tmp/recording-takes/{TAKE}/capture.partial.wav": ("mac-only", None, None),
    f"tmp/recording-takes/{TAKE}/native-recovery.json": ("mac-only", None, None),
    "provenance/corpus/.corpus.lock": ("mac-only", None, None),
    "provenance/corpus/.corpus.lease": ("mac-only", None, None),
    "provenance/corpus/corpus.sqlite3-wal": ("mac-only", None, None),
    "provenance/corpus/corpus.sqlite3-shm": ("mac-only", None, None),
    "provenance/corpus/corpus.sqlite3-journal": ("mac-only", None, None),
    "provenance/recording-library/recording-library.sqlite3": ("mac-only", None, None),
    "provenance/recording-library/.index.lease": ("mac-only", None, None),
    f"provenance/recording-library-quarantine-{HEX32}/recording-library.sqlite3": (
        "mac-only",
        None,
        None,
    ),
    f"provenance/recording-library-discovery/{SHA}.json": ("mac-only", None, None),
    f"provenance/recording-groups/.prepared-gates/{SHA}.json": ("mac-only", None, None),
    f"derived/recording-waveforms/{SHA}-v2.cvlwave": ("mac-only", None, None),
    "provenance/finetune-jobs/head.json": ("mac-only", None, None),
    "provenance/finetune-jobs/speech-active.json": ("mac-only", None, None),
    "provenance/finetune-jobs/.owner.lock": ("mac-only", None, None),
    "derived/finetune/run-1/samples/base/synthesis-tmp/reference.wav": ("mac-only", None, None),
    "derived/speech/speak-1/speech.json": ("mac-only", None, None),
    "derived/speech/speak-1/speech.wav": ("mac-only", None, None),
    "provenance/vault/destinations/default/binding.json": ("mac-only", None, None),
    "provenance/vault/destinations/default/state.json": ("mac-only", None, None),
    "provenance/vault/destinations/default/ledger/00000001.json": ("mac-only", None, None),
    f"tmp/.vault-{HEX16}/database-backup.sqlite3": ("mac-only", None, None),
    "provenance/recording-packages/work/prepare-1/copy-take-ab12.partial": ("mac-only", None, None),
    "tmp/.native-package-stage-native-1/READY": ("mac-only", None, None),
    "incoming/pilot-001/READY": ("mac-only", None, None),
    "provenance/intake/sessions/pilot-001.json": ("mac-only", None, None),
    "provenance/home-host/ledger/00000001.json": ("mac-only", None, None),
    "runtimes/worker.lock": ("mac-only", None, None),
    "models/f5-tts/model.safetensors": ("mac-only", None, None),
    "experiments/run-1/result.json": ("mac-only", None, None),
    "evaluations/review-1/answers.json": ("mac-only", None, None),
    "raw/.DS_Store": ("mac-only", None, None),
    # Copied records.
    f"provenance/recording-takes/{TAKE}/head.json": ("records", 0o600, "file"),
    "provenance/recording-takes/.store-head.json": ("records", 0o600, "file"),
    f"provenance/recording-analysis/jobs/{SHA}.json": ("records", 0o600, "file"),
    "provenance/recording-package-origin.json": ("records", 0o600, "file"),
    "provenance/recording-packages/sessions/native-1.json": ("records", 0o600, "file"),
    f"provenance/capture-desktop/desktop-{HEX32}.json": ("records", 0o600, "file"),
    f"provenance/consents/history/cole-self-v1.{SHA}.json": ("records", 0o600, "file"),
    "provenance/transfer-approvals/approval-1.json": ("records", 0o600, "file"),
    "provenance/finetune-jobs/terminal/finetune-1.json": ("records", 0o600, "file"),
    "derived/transfer-plans/plan-1/manifest.json": ("records", 0o400, "file"),
    "provenance/pilot-gates/gate-1.json": ("records", 0o400, "file"),
    "provenance/host-transfers/move-1.txt": ("records", 0o400, "file"),
    "provenance/diagnostic-root.json": ("records", 0o400, "file"),
    # The prompt database travels as its online backup.
    "provenance/corpus/corpus.sqlite3": ("prompt-database", 0o600, "sqlite-backup"),
    f"provenance/corpus/snapshots/{SHA}.json": ("prompt-database", 0o600, "file"),
    # Recordings.
    f"tmp/recording-takes/{TAKE}/original.wav": ("recordings", 0o600, "file"),
    "tmp/capture-desktop-1/audio/001-neutral-01.wav": ("recordings", 0o600, "file"),
    "tmp/capture-desktop-1.rejected/rejected-takes.json": ("recordings", 0o600, "file"),
    "tmp/capture-desktop-1.rejected/001-neutral-01.take-1.wav": ("recordings", 0o600, "file"),
    f"raw/{SHA}/audio/001-neutral-01.wav": ("recordings", 0o400, "file"),
    "references/cole-neutral-v1/reference.wav": ("recordings", 0o400, "file"),
    # Derived edits and the opt-in training material.
    f"derived/recording-edits/native/{SHA}/{SHA}.wav": ("derived-edits", 0o600, "file"),
    "derived/datasets/pilot-1/manifest.json": ("training", 0o400, "file"),
    "derived/finetune/run-1/checkpoints/model_last.pt": ("training", 0o600, "file"),
    "derived/finetune/run-1/checkpoints/pretrained_model_1250000.safetensors": (
        "training",
        0o600,
        "pin",
    ),
    "derived/finetune/run-1/archived.json": ("training", 0o600, "file"),
}


@pytest.mark.parametrize("path", sorted(PLAN_DISPOSITIONS))
def test_the_table_decides_each_path_as_the_plan_says(path: str) -> None:
    disposition, mode, transfer = PLAN_DISPOSITIONS[path]
    rule = classify(path).rule
    assert rule.disposition == disposition
    if rule.copied:
        assert (rule.mode, rule.transfer) == (mode, transfer)


# -- quiescent passes ----------------------------------------------------------------------


def copied_paths(layout: AssetLayout) -> dict[str, ClassificationRule]:
    return {path: classify(path).rule for path in tree_paths(layout.root) if classify(path).copied}


def test_a_quiescent_pass_carries_or_withholds_every_copied_file_exactly_once(
    libraries: Libraries,
) -> None:
    withheld: dict[str, str] = {}
    for layout in libraries.layouts:
        result, sink = run_pass(layout, chunk_bytes=4096)
        assert result.complete, [owner for owner in result.owners if owner.state != "complete"]
        assert [owner.owner for owner in result.owners] == [s.name for s in library_sources()]
        assert_never_half_copied(sink)
        carried = [entry.path for entry in result.entries]
        held = [item.path for owner in result.owners for item in owner.withheld]
        assert len(carried) == len(set(carried)) and not set(carried) & set(held)
        expected = copied_paths(layout)
        assert set(carried) | set(held) == set(expected)
        for entry in result.entries:
            decided = expected[entry.path]
            assert (entry.file_class, entry.mode) == (decided.disposition, decided.mode)
            assert entry.kind == ("pin" if decided.transfer == "pin" else "file")
            assert entry.planned().path == entry.path
        for owner in result.owners:
            for item in owner.withheld:
                withheld[f"{layout.root.parent.name}:{item.path}"] = item.reason
        assert all(classify(path).copied for path in sink.begun())
        # A pass leaves no scratch behind (a stale one without a lock stays, see below).
        assert sorted(p.name for p in layout.directory("tmp").glob(".vault-*")) == (
            [f".vault-{HEX16}"] if layout == libraries.legacy else []
        )
        # The entries form one restorable generation (unique, no file-versus-directory clash).
        ManifestPlan(
            library_id="lib-1",
            generation=1,
            previous_manifest_sha256=None,
            created_at=utc_now(),
            classes=tuple(sorted(ALL_CLASSES)),
            entries=tuple(sorted((e.planned() for e in result.entries), key=lambda e: e.path)),
        )
    draft = libraries.native.draft
    assert withheld == {
        f"native:tmp/recording-takes/{draft}/original.wav": (
            "the take is not sealed yet; its audio is carried once its seal names it"
        ),
        **{
            f"legacy:derived/finetune/run-training/{path}": "the run is running, not finished"
            for path in (
                "checkpoints/model_last.pt",
                "dataset/corpus.json",
                "dataset/dataset.json",
                f"dataset/wavs/{spoken(1)}.wav",
                "run.json",
            )
        },
        "legacy:derived/finetune/run-empty/train.log": "the run has no run record yet",
    }


def entries_of(result: PassResult) -> dict[str, PassEntry]:
    return {entry.path: entry for entry in result.entries}


def referrers_of(owner: str) -> set[str]:
    """Every library owner whose files name ``owner``'s files, directly or through others."""
    found: set[str] = set()
    frontier = {owner}
    while frontier:
        frontier = {s.name for s in library_sources() if s.referents & frontier} - found
        found |= frontier
    return found


def assert_held_back_only_with(result: PassResult, failed: str) -> None:
    """``failed``'s referrers fall back with it; every other owner is complete."""
    held = referrers_of(failed)
    for owner in result.owners:
        if owner.owner == failed:
            assert owner.state != "complete" and owner.held_back_by is None
        elif owner.owner in held:
            assert (owner.state, owner.problem, owner.entries) == ("changed", "changed", ())
            assert owner.held_back_by is not None and owner.held_back_by in held | {failed}
            assert owner.problem_path == result.owner(failed).problem_path
            assert owner.reason is not None and owner.held_back_by in owner.reason
        else:
            assert owner.state == "complete", owner


def test_each_file_travels_as_its_rule_says(libraries: Libraries) -> None:
    legacy, _ = run_pass(libraries.legacy)
    entries = entries_of(legacy)
    run = f"derived/finetune/{RUN_FINISHED}"
    pin = entries[f"{run}/checkpoints/pretrained_model.safetensors"]
    assert (pin.kind, pin.sha256, pin.size, pin.pin_source) == (
        "pin",
        "6" * 64,
        len(RUNTIME_WEIGHTS),
        PRETRAINED_PIN_SOURCE,
    )
    assert pin.planned().pin_source == PRETRAINED_PIN_SOURCE
    assert entries[f"{run}/train.log"].file_class == "training"
    assert entries[f"{run}/archived.json"].owner == "fine-tune-runs"
    assert entries["derived/datasets/pilot-1/manifest.json"].mode == 0o400
    assert entries["provenance/diagnostic-root.json"].mode == 0o400
    database = entries[CORPUS_DATABASE_PATH]
    assert (database.file_class, database.owner) == ("prompt-database", "prompt-database")
    rejected = [path for path in entries if ".rejected/" in path]
    assert rejected and all(entries[path].mode == 0o600 for path in rejected)
    assert not any(path.startswith(("provenance/vault", "tmp/.vault-")) for path in entries)
    accepted, _ = run_pass(libraries.accepted)
    raw = [entry for entry in accepted.entries if entry.path.startswith("raw/")]
    references = [entry for entry in accepted.entries if entry.path.startswith("references/")]
    assert raw and references
    assert {entry.mode for entry in raw + references} == {0o400}
    # A pass over one class reads nothing else.
    records, sink = run_pass(libraries.legacy, classes=("records",))
    assert {entry.file_class for entry in records.entries} == {"records"}
    assert {read.item.file_class for read in sink.reads} == {"records"}
    assert "fine-tune-runs" not in [owner.owner for owner in records.owners]


# -- concurrent saves ----------------------------------------------------------------------


def _save_when_told(
    root: str, group_id: str, prompt_id: str, name: str, go: Any, results: Any
) -> None:
    try:
        assert go.wait(CHILD_TIMEOUT)
        rig = new_rig(AssetLayout.validate(Path(root)), group_id)
        take = save_take(rig, sealed_draft(rig, name, prompt_id), name)
    except Exception as error:  # the parent fails the scenario with this report
        results.put(("error", f"{type(error).__name__}: {error}"))
    else:
        results.put(("saved", take.reservation.take_id))


class SaveWriter:
    """A writer process that saves one new take when asked, and reports how it went."""

    def __init__(self, library: NativeLibrary, name: str) -> None:
        self.go = CONTEXT.Event()
        self.results = CONTEXT.Queue()
        self.child = CONTEXT.Process(
            target=_save_when_told,
            args=(
                str(library.layout.root),
                library.group_id,
                pilot_prompt(4),
                name,
                self.go,
                self.results,
            ),
            daemon=True,
        )
        self.child.start()
        self.take_id: str | None = None

    def save(self) -> None:
        assert self.take_id is None
        self.go.set()
        status, payload = self.results.get(timeout=CHILD_TIMEOUT)
        assert status == "saved", payload
        self.take_id = payload

    def close(self) -> None:
        self.go.set()
        stop(self.child)


@dataclass(frozen=True)
class HookedSource:
    """A source that runs ``before`` when its owner starts."""

    inner: ReplicationSource
    before: Callable[[], None]

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
        self.before()
        yield from self.inner.items(layout)


@pytest.fixture
def native(tmp_path: Path, analyzer_build: Any) -> NativeLibrary:
    return build_native_library(tmp_path.resolve() / "assets", analyzer_build, corpus=True)


def take_entries(result: PassResult, take_id: str) -> set[str]:
    return {
        entry.path for entry in result.owner("recording-takes").entries if take_id in entry.path
    }


@pytest.mark.parametrize(
    "moment",
    [
        "before-the-take-store-starts",
        "while-take-records-are-listed",
        "while-an-original-is-read",
        "after-the-take-store-finished",
    ],
)
def test_a_take_saved_during_a_pass_is_complete_in_it_or_carried_to_the_next(
    native: NativeLibrary, moment: str
) -> None:
    writer = SaveWriter(native, "concurrent")
    try:
        sources = list(library_sources())
        if moment == "before-the-take-store-starts":
            position = [source.name for source in sources].index("recording-takes")
            sources[position] = HookedSource(sources[position], writer.save)

        def on_begin(owner: str, item: ReplicationItem) -> None:
            if writer.take_id is not None:
                return
            if (moment == "while-take-records-are-listed" and owner == "recording-takes") or (
                moment == "after-the-take-store-finished" and owner == "recording-groups"
            ):
                writer.save()

        def on_chunk(read: Read, data: bytes) -> None:
            if moment == "while-an-original-is-read" and writer.take_id is None:
                if read.item.path.endswith("/original.wav"):
                    writer.save()

        first, sink = run_pass(
            native.layout,
            tuple(sources),
            RecordingSink(on_begin=on_begin, on_chunk=on_chunk),
            chunk_bytes=64,
        )
    finally:
        writer.close()
    take_id = writer.take_id
    assert take_id is not None
    assert_never_half_copied(sink)
    takes = first.owner("recording-takes")
    if moment == "before-the-take-store-starts":
        assert first.complete
        assert len(take_entries(first, take_id)) == 4
    elif moment == "after-the-take-store-finished":
        assert first.complete and take_entries(first, take_id) == set()
    else:
        assert_held_back_only_with(first, "recording-takes")
        assert (takes.state, takes.problem, takes.entries) == ("changed", "changed", ())
        assert takes.problem_path == "provenance/recording-takes/.store-head.json"
        assert takes.reason is not None and "committed during the pass" in takes.reason
    # The next pass carries the take whole, with the decision the writer saved.
    second, again = run_pass(native.layout)
    assert second.complete
    assert take_entries(second, take_id) == {
        f"provenance/recording-takes/{take_id}/head.json",
        f"provenance/recording-takes/{take_id}/reservation.json",
        f"provenance/recording-takes/{take_id}/seal.json",
        f"tmp/recording-takes/{take_id}/original.wav",
    }
    head = again.carried()[f"provenance/recording-takes/{take_id}/head.json"]
    assert RecordingTake.model_validate_json(head).decision == "kept"


Pass = tuple[PassResult, RecordingSink]


def generation(previous: Pass, current: Pass) -> dict[str, bytes]:
    """The files one generation holds: each complete owner's fresh files, else its previous."""
    files: dict[str, bytes] = {}
    for owner in current[0].owners:
        result, sink = current if owner.state == "complete" else previous
        kept = result.owner(owner.owner)
        assert kept.state == "complete", kept
        carried = sink.carried()
        files.update({entry.path: carried[entry.path] for entry in kept.entries})
    return files


def take_head(take_id: str) -> str:
    return f"provenance/recording-takes/{take_id}/head.json"


def package_map_path(session_id: str) -> str:
    return f"provenance/recording-packages/sessions/{session_id}.json"


def assert_references_closed(files: Mapping[str, bytes]) -> None:
    """Every file a carried record names, and a restored library's readers need, is carried."""
    for path, data in files.items():
        parts = path.split("/")
        if path.startswith("provenance/recording-analysis/jobs/"):
            job = RecordingAnalysisJob.model_validate_json(data)
            named = {take_head(job.ticket.seal.take_id)}
        elif path.startswith("provenance/recording-packages/sessions/"):
            commit = RecordingPackageCommit.model_validate_json(data)
            named = {take_head(entry.take_id) for entry in commit.package.entries}
            # load_recording_package_map also needs the root's origin and the registration.
            named |= {
                "provenance/recording-package-origin.json",
                f"provenance/recording-packages/operations/{commit.plan.command.operation_id}.json",
            }
        elif (
            parts[0] == "tmp" and parts[1].startswith("capture-native-") and parts[2:] == ["READY"]
        ):
            named = {package_map_path(parts[1].removeprefix("capture-"))}
        elif parts[0] == "raw" and parts[2:] == ["READY"]:
            # A raw session of a native package prepared here resolves its takes through the
            # package map (RecordingLibrary._package_history, resolve_native_package_alias).
            session = ReadyMarker.model_validate_json(data).session_id
            named = {package_map_path(session)} if session.startswith("native-") else set()
        elif parts[:2] == ["provenance", "recording-takes"] and parts[3:] == ["head.json"]:
            group = RecordingTake.model_validate_json(data).reservation.group
            named = {f"provenance/recording-groups/{group.group_id}.json"}
            if group.catalog_reference is not None:
                named.add(catalog_snapshot_path(group.catalog_reference.catalog_sha256))
        else:
            continue
        assert named <= files.keys(), (path, sorted(named - files.keys()))


def queue_analysis(layout: AssetLayout, rig: Rig, take: RecordingTake, build: Any) -> str:
    """Queue ``take`` for analysis as the service does after a seal; returns the job digest."""
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        queue = RecordingAnalysisQueue(rig.store, index, build=build, recover=False)
        try:
            return queue.submit(take.reservation.take_id).ticket.job_sha256
        finally:
            queue.close()


def restore_tree(files: Mapping[str, bytes], root: Path) -> AssetLayout:
    layout = AssetLayout.initialize(root)
    for path, data in files.items():
        write_file(root, path, data)
    return layout


@pytest.mark.parametrize("moment", ["while-the-take-store-is-read", "after-the-jobs-were-read"])
def test_an_analysis_job_queued_during_a_pass_never_outlives_its_take_in_a_generation(
    native: NativeLibrary, analyzer_build: Any, tmp_path: Path, moment: str
) -> None:
    previous = run_pass(native.layout)
    assert previous[0].complete
    rig = new_rig(native.layout, native.group_id)
    queued: list[tuple[str, str]] = []

    def queue_a_new_take() -> None:
        if not queued:
            take = sealed_draft(rig, "analysed", pilot_prompt(4))
            job = queue_analysis(native.layout, rig, take, analyzer_build)
            queued.append((take.reservation.take_id, job))

    sources = list(library_sources())
    if moment == "after-the-jobs-were-read":
        # The last owner read between the analysis jobs and the takes.
        position = [source.name for source in sources].index("recording-packages")
        sources[position] = HookedSource(sources[position], queue_a_new_take)

    def on_begin(owner: str, item: ReplicationItem) -> None:
        if moment == "while-the-take-store-is-read" and owner == "recording-takes":
            queue_a_new_take()

    current = run_pass(native.layout, tuple(sources), RecordingSink(on_begin=on_begin))
    ((take_id, job),) = queued
    job_path = f"provenance/recording-analysis/jobs/{job}.json"
    analysis = current[0].owner("recording-analysis")
    assert job_path not in {entry.path for entry in analysis.entries}
    if moment == "while-the-take-store-is-read":
        assert_held_back_only_with(current[0], "recording-takes")
        assert analysis.held_back_by == "recording-takes"
    else:
        assert current[0].complete
        assert take_head(take_id) in take_entries(current[0], take_id)
    files = generation(previous, current)
    assert_references_closed(files)
    # A restored library whose analyzer was rebuilt queues every job again, and finds its take.
    restored = restore_tree(files, tmp_path.resolve() / "restored")
    store = RecordingTakeStore(restored)
    with RecordingIndex(restored, subject_id=SUBJECT) as index:
        store.projection_hook = index.update_take
        rebuilt = analyzer_build.model_copy(update={"build_id": "rebuilt-app"})
        RecordingAnalysisQueue(store, index, build=rebuilt).close()
    # The next pass carries the job together with its take.
    after = run_pass(native.layout)
    assert after[0].complete
    assert {job_path, take_head(take_id)} <= {entry.path for entry in after[0].entries}
    assert_references_closed(generation(current, after))


def test_an_owner_whose_referent_did_not_complete_is_held_back_with_it(
    native: NativeLibrary,
) -> None:
    def backup(source: Path, target: Path) -> None:
        raise sqlite3.OperationalError("database is locked")

    sources = (*library_sources()[:-1], CorpusDatabaseSource(backup=backup))
    result, sink = run_pass(native.layout, sources)
    assert_held_back_only_with(result, "prompt-database")
    groups, takes = result.owner("recording-groups"), result.owner("recording-takes")
    assert (groups.held_back_by, takes.held_back_by) == ("prompt-database", "recording-groups")
    assert result.owner("recording-analysis").held_back_by == "recording-takes"
    assert takes.reason == (
        "held back with recording-groups, which did not complete and whose files this "
        "owner's files name"
    )
    # Their files were read (and verified), but none of them counts.
    assert any(read.entry is not None and read.owner == "recording-takes" for read in sink.reads)
    assert not {entry.owner for entry in result.entries} & referrers_of("prompt-database")


@dataclass(frozen=True)
class UnpublishedPackage:
    """The native fixture's package with its map and directory moved aside (not prepared yet)."""

    layout: AssetLayout
    session: str
    final: Path
    stage: Path
    session_map: Path
    held: Path

    @classmethod
    def withdraw(cls, library: NativeLibrary, held: Path) -> UnpublishedPackage:
        root = library.layout.root
        (final,) = [
            path for path in (root / "tmp").iterdir() if path.name.startswith("capture-native-")
        ]
        session = final.name.removeprefix("capture-")
        package = cls(
            library.layout,
            session,
            final,
            root / "tmp" / f".native-package-stage-{session}",
            root / package_map_path(session),
            held,
        )
        package.unpublish()
        return package

    def unpublish(self) -> None:
        os.rename(self.session_map, self.held)
        os.rename(self.final, self.stage)

    def publish(self) -> None:
        # RecordingPackageStore._publish: the map first, then the staged directory.
        os.rename(self.held, self.session_map)
        os.rename(self.stage, self.final)

    def accept(self) -> str:
        """Accept the published package on this Mac, as intake does; returns its raw path."""
        incoming = self.layout.directory("incoming") / self.session
        shutil.copytree(self.final, incoming)
        return accept_capture_session(self.layout, incoming, min_free_bytes=0).raw_path

    def unaccept(self, raw_path: str) -> None:
        raw = self.layout.root / raw_path
        raw.chmod(0o700)
        restore_permissions(raw)
        shutil.rmtree(raw)


def test_a_package_published_or_accepted_at_any_point_of_a_pass_is_never_carried_without_its_map(
    tmp_path: Path, analyzer_build: Any
) -> None:
    library = build_native_library(tmp_path.resolve() / "assets", analyzer_build)
    package = UnpublishedPackage.withdraw(library, tmp_path.resolve() / "held-map.json")
    names = [source.name for source in library_sources()]
    seen: dict[str, tuple[bool, bool, bool]] = {}
    accepted: list[str] = []

    def publish_and_accept() -> None:
        package.publish()
        accepted.append(package.accept())

    for position, name in enumerate(names):
        sources = list(library_sources())
        sources[position] = HookedSource(sources[position], publish_and_accept)
        result, _ = run_pass(library.layout, tuple(sources))
        assert result.complete, name
        carried = {entry.path for entry in result.entries}
        (raw_path,) = accepted
        seen[name] = (
            f"tmp/capture-{package.session}/READY" in carried,
            f"{raw_path}/READY" in carried,
            package_map_path(package.session) in carried,
        )
        assert seen[name][2] or not (seen[name][0] or seen[name][1]), name
        package.unaccept(accepted.pop())
        package.unpublish()
    # Published and accepted before the capture directories are read, all three are carried;
    # before the raw sessions are read, the raw session and the map; before the package
    # records are read, only the map; after that, none (the next pass carries them).
    sessions, raw = names.index("capture-sessions"), names.index("raw-packages")
    packages = names.index("recording-packages")
    assert seen == {
        name: (position <= sessions, position <= raw, position <= packages)
        for position, name in enumerate(names)
    }


def test_a_raw_session_falls_back_with_the_package_map_it_needs(
    tmp_path: Path, analyzer_build: Any
) -> None:
    library = build_native_library(tmp_path.resolve() / "assets", analyzer_build, corpus=True)
    package = UnpublishedPackage.withdraw(library, tmp_path.resolve() / "held-map.json")
    # The last generation was taken before the package was prepared.
    previous = run_pass(library.layout)
    assert previous[0].complete
    package.publish()
    raw_path = package.accept()
    # This pass reads the new raw session, but a Save commits while the takes are read.
    writer = SaveWriter(library, "concurrent")
    try:

        def on_begin(owner: str, item: ReplicationItem) -> None:
            if owner == "recording-takes" and writer.take_id is None:
                writer.save()

        current = run_pass(library.layout, sink=RecordingSink(on_begin=on_begin))
    finally:
        writer.close()
    assert_held_back_only_with(current[0], "recording-takes")
    assert current[0].owner("raw-packages").held_back_by == "recording-packages"
    assert current[0].owner("recording-packages").held_back_by == "recording-takes"
    # The raw session was read, but the generation keeps the previous (empty) raw sessions.
    assert f"{raw_path}/READY" in current[1].carried()
    held_back = generation(previous, current)
    assert not any(path.startswith("raw/") for path in held_back)
    assert_references_closed(held_back)
    # The next pass carries the raw session with its map, and the restored library reads it.
    after = run_pass(library.layout)
    assert after[0].complete
    files = generation(current, after)
    assert {f"{raw_path}/READY", package_map_path(package.session)} <= files.keys()
    assert_references_closed(files)
    restored = restore_tree(files, tmp_path.resolve() / "restored")
    raw = validate_capture_package(restored.root / raw_path, allow_intake_receipt=True)
    assert load_recording_package_map(restored, package.session, package=raw) is not None
    RecordingTakeStore(restored)  # recreates the empty store directories a pass never carries
    report = prompt_coverage_report(restored, SUBJECT, released_pilot_v2())
    assert report.exit_status == 0
    # Without its map, the same restored library cannot read the raw session at all.
    (restored.root / package_map_path(package.session)).unlink()
    with pytest.raises(PromptCoverageProjectionError, match="missing its required take map"):
        prompt_coverage_report(restored, SUBJECT, released_pilot_v2())


def _save_paused_in_its_commit(
    root: str, command: str, now: str, boundary: str, paused: Any, release: Any
) -> None:
    def hook(name: str) -> None:
        if name == boundary:
            paused.set()
            assert release.wait(CHILD_TIMEOUT)

    moment = datetime.fromisoformat(now)
    store = RecordingTakeStore(
        AssetLayout.validate(Path(root)), fault_hook=hook, clock=lambda: moment
    )
    store.save(SaveTakeCommand.model_validate_json(command))


@pytest.mark.parametrize("boundary", ["after-operation", "before-store-head"])
def test_a_save_of_a_listed_take_still_committing_when_the_take_store_ends_is_not_carried(
    native: NativeLibrary, boundary: str
) -> None:
    rig = new_rig(native.layout, native.group_id)
    late = sealed_draft(rig, "late", pilot_prompt(4))
    command = rig.command(late, operation="save-late")
    operation = "provenance/recording-takes/.operations/save-late.json"
    sources = (TakeStoreSource(),)
    classes: tuple[FileClass, ...] = ("records", "recordings")
    quiet, listed = run_pass(native.layout, sources, classes=classes)
    assert quiet.complete
    paths = [read.item.path for read in listed.reads]
    # The last item before the operation journal is listed; the late take was read before it.
    trigger = paths[next(i for i, path in enumerate(paths) if "/.operations/" in path) - 1]
    assert paths.index(take_head(late.reservation.take_id)) < paths.index(trigger)
    paused, release = CONTEXT.Event(), CONTEXT.Event()
    writer = CONTEXT.Process(
        target=_save_paused_in_its_commit,
        args=(
            str(native.layout.root),
            command.model_dump_json(),
            rig.now.isoformat(),
            boundary,
            paused,
            release,
        ),
        daemon=True,
    )

    def on_begin(owner: str, item: ReplicationItem) -> None:
        if item.path == trigger and not writer.is_alive():
            writer.start()
            assert wait_for(paused, writer)

    try:
        result, sink = run_pass(
            native.layout, sources, RecordingSink(on_begin=on_begin), classes=classes
        )
    finally:
        release.set()
        if writer.pid is not None:
            stop(writer)
    takes = result.owner("recording-takes")
    assert (takes.state, takes.problem, takes.problem_path, takes.entries) == (
        "changed",
        "changed",
        "provenance/recording-takes/.pending.json",
        (),
    )
    # The pass read the half-applied save's operation, and counted none of it.
    assert operation in sink.begun() and operation not in {e.path for e in result.entries}
    after, again = run_pass(native.layout, sources, classes=classes)
    assert after.complete and operation in {entry.path for entry in after.entries}
    head = again.carried()[take_head(late.reservation.take_id)]
    assert RecordingTake.model_validate_json(head).decision == "kept"


def lock_files(root: Path) -> list[Path]:
    return sorted(root / path for path in tree_paths(root) if path.endswith((".lock", ".lease")))


def assert_unlocked(paths: list[Path]) -> None:
    """The pass holds no lock: this process can take each one without waiting."""
    for path in paths:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def test_no_save_is_refused_while_a_pass_reads_and_the_pass_holds_no_lock(
    native: NativeLibrary,
) -> None:
    root = native.layout.root
    locks = lock_files(root)
    assert {path.name for path in locks} >= {
        ".metadata.lock",
        ".corpus.lock",
        ".corpus.lease",
        ".index.lock",
        ".index.lease",
        ".owner.lock",
    }
    rig = new_rig(native.layout, native.group_id)
    drafts = [sealed_draft(rig, f"probe-{n}", pilot_prompt(5 + n)) for n in range(3)]
    saved: list[str] = []

    def probe_and_save() -> None:
        assert_unlocked(locks)
        take = drafts[len(saved)]
        saved.append(save_take(rig, take, f"probe-{len(saved)}").reservation.take_id)

    def backup(source: Path, target: Path) -> None:
        probe_and_save()
        backup_database(source, target)
        probe_and_save()

    def on_begin(owner: str, item: ReplicationItem) -> None:
        if owner == "recording-takes" and not saved:
            probe_and_save()

    sources = (*library_sources()[:-1], CorpusDatabaseSource(backup=backup))
    result, sink = run_pass(native.layout, sources, RecordingSink(on_begin=on_begin))
    assert len(saved) == 3
    assert result.owner("recording-takes").state == "changed"
    assert result.owner("prompt-database").state == "complete"
    assert_never_half_copied(sink)
    assert all(not path.name.endswith((".lock", ".lease")) for path in map(Path, sink.begun()))


# The owners each library's pass must reach, so the lock-free checks cover their sources.
REACHED_OWNERS: Mapping[str, set[str]] = {
    "native": {"recording-analysis", "recording-packages", "recording-takes", "prompt-database"},
    "legacy": {"capture-sessions", "fine-tune-job-records", "fine-tune-runs", "prompt-database"},
    "accepted": {"references", "lab-approvals", "raw-packages"},
}


def fixture_library(request: pytest.FixtureRequest, kind: str) -> AssetLayout:
    if kind == "native":
        native: NativeLibrary = request.getfixturevalue("native")
        return native.layout
    libraries: Libraries = request.getfixturevalue("libraries")
    return libraries.legacy if kind == "legacy" else libraries.accepted


def _descriptor_path(descriptor: int) -> bytes:
    """The path an open descriptor names: F_GETPATH on macOS, /proc/self/fd on Linux."""
    if hasattr(fcntl, "F_GETPATH"):
        return fcntl.fcntl(descriptor, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0")
    return os.fsencode(os.readlink(f"/proc/self/fd/{descriptor}"))


@pytest.mark.parametrize("kind", sorted(REACHED_OWNERS))
def test_no_source_opens_a_store_or_a_lock_file(
    request: pytest.FixtureRequest, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import corpus_store_sqlite, recording_index, recording_takes

    layout = fixture_library(request, kind)

    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("a replication source entered a store")

    monkeypatch.setattr(recording_takes.RecordingTakeStore, "_metadata_lock", forbidden)
    monkeypatch.setattr(recording_takes.RecordingTakeStore, "_repair_pending", forbidden)
    monkeypatch.setattr(corpus_store_sqlite.CorpusStore, "__init__", forbidden)
    monkeypatch.setattr(recording_index.RecordingIndex, "__init__", forbidden)
    real_open, real_flock = os.open, fcntl.flock
    locked: list[str] = []

    def open_(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        name = os.path.basename(os.fspath(path))
        if name.endswith((".lock", ".lease")) and name != replication.SCRATCH_LOCK_NAME:
            raise AssertionError(f"a replication source opened {path}")
        return real_open(path, flags, *args, **kwargs)

    def flock(descriptor: int, operation: int) -> None:
        path = _descriptor_path(descriptor)
        locked.append(os.path.basename(os.fsdecode(path)))
        real_flock(descriptor, operation)

    queue_reads: list[str] = []
    real_active = finetune_jobs.active_finetune_job

    def active_job(layout: AssetLayout) -> Any:
        queue_reads.append("active-job")
        return real_active(layout)

    monkeypatch.setattr(finetune_jobs, "active_finetune_job", active_job)
    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(fcntl, "flock", flock)
    result, sink = run_pass(layout)
    monkeypatch.undo()
    assert result.complete and sink.reads
    assert REACHED_OWNERS[kind] <= {entry.owner for entry in result.entries}
    # Only the prompt database's scratch is ever locked, and only the pass's own.
    has_database = (layout.root / CORPUS_DATABASE_PATH).exists()
    assert locked == ([replication.SCRATCH_LOCK_NAME] if has_database else [])
    # The fine-tune source checked the queue (twice: before and after the runs) under the patch.
    assert len(queue_reads) == (2 if kind == "legacy" else 0)
    if kind == "legacy":
        assert any(".rejected/" in entry.path for entry in result.entries)


def _hold_locks(paths: list[str], held: Any, release: Any) -> None:
    descriptors = []
    for path in paths:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        descriptors.append(descriptor)
    held.set()
    assert release.wait(CHILD_TIMEOUT)


@pytest.mark.parametrize("kind", ["legacy", "accepted"])
def test_a_pass_over_other_libraries_completes_while_another_process_holds_every_lock(
    libraries: Libraries, tmp_path: Path, kind: str
) -> None:
    root = tmp_path.resolve() / "assets"
    shutil.copytree(getattr(libraries, kind).root, root, symlinks=True)
    restore_permissions(root)
    extra = ("provenance/capture-desktop/.capture.lock", "provenance/finetune-jobs/.owner.lock")
    locks = sorted({*lock_files(root), *(write_file(root, path, b"") for path in extra)})
    holder_ready, release = CONTEXT.Event(), CONTEXT.Event()
    holder = CONTEXT.Process(
        target=_hold_locks, args=([str(p) for p in locks], holder_ready, release), daemon=True
    )
    holder.start()
    try:
        assert wait_for(holder_ready, holder)
        for path in locks:
            with pytest.raises(BlockingIOError):
                assert_unlocked([path])
        result, sink = run_pass(AssetLayout.validate(root))
    finally:
        release.set()
        stop(holder)
    assert result.complete
    assert REACHED_OWNERS[kind] <= {entry.owner for entry in result.entries}
    assert_never_half_copied(sink)


def attempt_save(rig: Rig, take: RecordingTake, name: str) -> str:
    try:
        save_take(rig, take, name)
    except RecordingMetadataBusyError:
        return "refused: metadata busy"
    return "saved"


@pytest.mark.parametrize(
    ("held", "save_outcome"),
    [
        ("every-writer-lock", "refused: metadata busy"),
        ("every-writer-lock-but-the-take-store's", "saved"),
    ],
)
def test_a_pass_completes_while_another_process_holds_writer_locks(
    native: NativeLibrary, held: str, save_outcome: str
) -> None:
    root = native.layout.root
    extra = (
        "provenance/capture-desktop/.capture.lock",
        "provenance/finetune-jobs/.owner.lock",
        "provenance/recording-edits/native/.render-owner.lock",
    )
    locks = sorted({*lock_files(root), *(write_file(root, path, b"") for path in extra)})
    if held != "every-writer-lock":
        locks = [path for path in locks if path.parent.name != "recording-takes"]
    rig = new_rig(native.layout, native.group_id)
    before, during = (sealed_draft(rig, name, pilot_prompt(6)) for name in ("before", "during"))
    holder_ready, release = CONTEXT.Event(), CONTEXT.Event()
    holder = CONTEXT.Process(
        target=_hold_locks, args=([str(p) for p in locks], holder_ready, release), daemon=True
    )
    holder.start()
    outcomes: list[str] = []
    try:
        assert wait_for(holder_ready, holder)
        for path in locks:
            with pytest.raises(BlockingIOError):
                assert_unlocked([path])
        outcomes.append(attempt_save(rig, before, "before"))
        quiet, _ = run_pass(native.layout)

        def on_begin(owner: str, item: ReplicationItem) -> None:
            if owner == "recording-groups" and len(outcomes) == 1:
                outcomes.append(attempt_save(rig, during, "during"))

        result, sink = run_pass(native.layout, sink=RecordingSink(on_begin=on_begin))
    finally:
        release.set()
        stop(holder)
    assert outcomes == [save_outcome, save_outcome]
    assert quiet.complete and result.complete
    assert_never_half_copied(sink)


# -- the prompt database -------------------------------------------------------------------


def database_dump(path: Path) -> tuple[list[str], int]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        return list(connection.iterdump()), version


def _publish_when_told(
    root: str, go: Any, in_transaction: Any, release: Any, committed: Any
) -> None:
    from cole_voice_lab.corpus_store_sqlite import CorpusStore

    original = CorpusStore._write_snapshot

    def paused(store: CorpusStore, name: str, payload: bytes) -> None:
        original(store, name, payload)
        in_transaction.set()
        assert release.wait(CHILD_TIMEOUT)

    CorpusStore._write_snapshot = paused  # type: ignore[method-assign]
    assert go.wait(CHILD_TIMEOUT)
    with open_catalog_editor(AssetLayout.validate(Path(root))) as store:
        store.edit_prompt(spoken(2), text="A different synthetic sentence.")
        store.publish_version(CATALOG_ID)
    committed.set()


@pytest.mark.parametrize("writer", ["holds-an-open-publish-transaction", "has-committed"])
def test_the_database_copy_is_a_committed_version_while_a_writer_process_publishes(
    tmp_path: Path, writer: str
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)
    live = layout.root / CORPUS_DATABASE_PATH
    go, in_transaction, release, committed = (CONTEXT.Event() for _ in range(4))
    child = CONTEXT.Process(
        target=_publish_when_told,
        args=(str(layout.root), go, in_transaction, release, committed),
        daemon=True,
    )
    child.start()
    expected: list[tuple[list[str], int]] = []

    def backup(source: Path, target: Path) -> None:
        if writer == "has-committed":
            release.set()
        go.set()
        assert wait_for(committed if writer == "has-committed" else in_transaction, child)
        expected.append(database_dump(live))
        backup_database(source, target)

    try:
        result, sink = run_pass(
            layout, (CorpusDatabaseSource(backup=backup),), classes=("prompt-database",)
        )
    finally:
        release.set()
        stop(child)
    assert committed.is_set() and result.complete
    copy = tmp_path / "copy.sqlite3"
    copy.write_bytes(sink.carried()[CORPUS_DATABASE_PATH])
    with closing(sqlite3.connect(copy)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert database_dump(copy) == expected[0]
    after = database_dump(live)
    assert (after == expected[0]) == (writer == "has-committed")
    snapshots = [e for e in result.entries if e.path.startswith("provenance/corpus/snapshots/")]
    assert len(snapshots) == 2
    assert list(layout.directory("tmp").iterdir()) == []


# Run in a child: print the pid of a process holding a POSIX lock on the file (0 for none).
LOCK_PROBE = """
import fcntl, os, struct, sys
descriptor = os.open(sys.argv[1], os.O_RDONLY)
request = struct.pack("qqihh", 0, 0, 0, fcntl.F_WRLCK, os.SEEK_SET)
_, _, pid, kind, _ = struct.unpack("qqihh", fcntl.fcntl(descriptor, fcntl.F_GETLK, request))
print(0 if kind == fcntl.F_UNLCK else pid)
"""


def lock_holder(path: Path) -> int:
    """The process another process sees holding a lock on ``path`` (0 for none)."""
    probe = subprocess.run(
        [sys.executable, "-I", "-B", "-c", LOCK_PROBE, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT,
    )
    return int(probe.stdout)


@pytest.mark.skipif(sys.platform != "darwin", reason="the lock probe packs Darwin's struct flock")
def test_the_database_source_never_opens_the_live_database_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)
    live = layout.root / CORPUS_DATABASE_PATH
    # This process reads the database, as the service does while a pass runs in it; closing
    # any descriptor of the database files here would drop the locks that read holds.
    with closing(sqlite3.connect(live, isolation_level=None)) as service:
        service.execute("BEGIN")
        service.execute("SELECT count(*) FROM sqlite_master").fetchall()
        held = (live, live.with_name(f"{live.name}-shm"))
        assert [lock_holder(path) for path in held] == [os.getpid(), os.getpid()]
        real_os_open, real_open = os.open, builtins.open
        opened: list[str] = []

        def refuse_database(path: Any) -> str:
            name = os.path.basename(os.fspath(path)) if not isinstance(path, int) else ""
            assert not name.startswith("corpus.sqlite3"), f"a pass opened {path}"
            return name

        def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            opened.append(refuse_database(path))
            return real_os_open(path, flags, *args, **kwargs)

        def guarded_open(path: Any, *args: Any, **kwargs: Any) -> Any:
            refuse_database(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", guarded_os_open)
        monkeypatch.setattr(builtins, "open", guarded_open)
        monkeypatch.setattr(io, "open", guarded_open)
        result, sink = run_pass(layout, (CorpusDatabaseSource(),), classes=("prompt-database",))
        monkeypatch.undo()
        assert [lock_holder(path) for path in held] == [os.getpid(), os.getpid()]
        service.execute("COMMIT")
    assert result.complete and replication.DATABASE_BACKUP_NAME in opened
    assert CORPUS_DATABASE_PATH in sink.carried()


def test_a_stale_scratch_is_swept_and_a_live_one_is_kept(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)
    tmp = layout.directory("tmp")
    for name in ("stale", "live", "unlocked"):
        write_file(tmp, f".vault-{name}/{replication.DATABASE_BACKUP_NAME}", b"old copy")
    for name in ("stale", "live"):
        write_file(tmp, f".vault-{name}/{replication.SCRATCH_LOCK_NAME}", b"")
    write_file(tmp, ".vault-file", b"")
    write_file(tmp, ".vault-unreadable/.scratch.lock", b"").parent.chmod(0o000)
    os.symlink(tmp / ".vault-live", tmp / ".vault-link")
    live = os.open(tmp / ".vault-live" / replication.SCRATCH_LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(live, fcntl.LOCK_EX)
        result, _ = run_pass(layout, (CorpusDatabaseSource(),), classes=("prompt-database",))
    finally:
        os.close(live)
    assert result.complete
    assert sorted(path.name for path in tmp.iterdir()) == [
        ".vault-file",
        ".vault-link",
        ".vault-live",
        ".vault-unlocked",
        ".vault-unreadable",
    ]


@pytest.mark.parametrize(
    ("failure", "state", "problem"),
    [
        (sqlite3.OperationalError("database is locked"), "changed", "changed"),
        (sqlite3.DatabaseError("file is not a database"), "refused", "invalid"),
    ],
)
def test_a_failed_backup_is_reported_and_its_scratch_removed(
    tmp_path: Path, failure: sqlite3.Error, state: str, problem: str
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)

    def backup(source: Path, target: Path) -> None:
        raise failure

    result, sink = run_pass(
        layout, (CorpusDatabaseSource(backup=backup),), classes=("prompt-database",)
    )
    owner = result.owner("prompt-database")
    assert (owner.state, owner.problem, owner.problem_path) == (
        state,
        problem,
        CORPUS_DATABASE_PATH,
    )
    assert str(failure) in (owner.reason or "")
    assert sink.reads == [] and list(layout.directory("tmp").iterdir()) == []


def test_a_database_that_is_not_one_fails_its_backup(tmp_path: Path) -> None:
    source, target = tmp_path / "source.sqlite3", tmp_path / "target.sqlite3"
    source.write_bytes(b"not a database, just synthetic bytes" * 200)
    target.touch()
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, target)
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        backup_database(missing, target)


def test_a_backup_that_fails_its_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)
    real_connect = sqlite3.connect

    class Checked:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def execute(self, statement: str) -> Any:
            if "quick_check" in statement:
                return real_connect(":memory:").execute("SELECT 'damaged'")
            return self.connection.execute(statement)

        def backup(self, target: Checked, **kwargs: Any) -> None:
            self.connection.backup(target.connection, **kwargs)

        def close(self) -> None:
            self.connection.close()

    def connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        return Checked(real_connect(database, *args, **kwargs))

    monkeypatch.setattr(replication.sqlite3, "connect", connect)
    target = tmp_path / "target.sqlite3"
    target.touch()
    with pytest.raises(sqlite3.DatabaseError, match="failed its check"):
        backup_database(layout.root / CORPUS_DATABASE_PATH, target)


def test_the_backup_copies_the_database_in_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)
    real_connect = sqlite3.connect
    steps: list[dict[str, Any]] = []

    class Recorded:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def backup(self, target: Recorded, *args: Any, **kwargs: Any) -> None:
            steps.append({"args": args, **kwargs})
            self.connection.backup(target.connection, *args, **kwargs)

    def connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        return Recorded(real_connect(database, *args, **kwargs))

    monkeypatch.setattr(replication.sqlite3, "connect", connect)
    target = tmp_path / "target.sqlite3"
    target.touch()
    backup_database(layout.root / CORPUS_DATABASE_PATH, target)
    # One step, all pages: a stepped backup restarts after every commit by another process.
    assert steps == [{"args": (), "pages": -1}]


def test_a_database_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)
    live = layout.root / CORPUS_DATABASE_PATH
    live.rename(tmp_path / "moved.sqlite3")
    os.symlink(tmp_path / "moved.sqlite3", live)
    result, sink = run_pass(layout, (CorpusDatabaseSource(),), classes=("prompt-database",))
    owner = result.owner("prompt-database")
    assert (owner.state, owner.problem) == ("refused", "unsafe")
    assert sink.reads == []


def test_a_library_without_a_database_still_carries_its_snapshots(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)
    for name in ("corpus.sqlite3", "corpus.sqlite3-wal", "corpus.sqlite3-shm"):
        (layout.root / "provenance/corpus" / name).unlink(missing_ok=True)
    result, _ = run_pass(layout, (CorpusDatabaseSource(),), classes=("prompt-database",))
    assert result.complete
    assert [entry.path.split("/")[2] for entry in result.entries] == ["snapshots"]


def test_a_snapshot_being_published_is_withheld_and_the_database_still_carried(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    publish_corpus(layout)
    (snapshot,) = (layout.root / "provenance/corpus/snapshots").iterdir()
    os.link(snapshot, snapshot.parent / f".{snapshot.name}.tmp-{HEX16}")
    result, sink = run_pass(layout, (CorpusDatabaseSource(),), classes=("prompt-database",))
    owner = result.owner("prompt-database")
    relative = snapshot.relative_to(layout.root).as_posix()
    assert owner.state == "complete"
    assert [entry.path for entry in owner.entries] == [CORPUS_DATABASE_PATH]
    assert owner.withheld == (
        WithheldPath(
            relative,
            "prompt-database",
            "a catalog snapshot is being published; it is carried once its publish ends",
        ),
    )
    assert relative not in sink.begun()
    snapshot.chmod(0o644)
    (snapshot.parent / f".{snapshot.name}.tmp-{HEX16}").unlink()
    result, _ = run_pass(layout, (CorpusDatabaseSource(),), classes=("prompt-database",))
    assert result.owner("prompt-database").problem == "unsafe"


# -- refusals ------------------------------------------------------------------------------


def test_an_unclassified_file_fails_the_pass_before_anything_is_read(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    write_file(layout.root, "provenance/consents/cole-self-v1.json")
    write_file(layout.root, "provenance/stray-notes.txt", b"notes")
    sink = RecordingSink()
    with pytest.raises(StorageError, match="provenance/stray-notes.txt") as caught:
        run_pass(layout, sink=sink)
    assert caught.value.code == "STORAGE.PROTOCOL" and sink.reads == []


def test_a_seal_mismatch_is_refused_and_nothing_the_take_store_offered_counts(
    native: NativeLibrary,
) -> None:
    root = native.layout.root
    original = root / "tmp/recording-takes" / native.edited / "original.wav"
    data = bytearray(original.read_bytes())
    data[-1] ^= 0xFF
    original.write_bytes(bytes(data))
    result, sink = run_pass(native.layout)
    takes = result.owner("recording-takes")
    relative = original.relative_to(root).as_posix()
    assert (takes.state, takes.problem, takes.problem_path, takes.entries) == (
        "refused",
        "mismatch",
        relative,
        (),
    )
    assert "its record names" in (takes.reason or "")
    (failed,) = [read for read in sink.reads if read.item.path == relative]
    assert failed.entry is None and failed.finished
    assert_held_back_only_with(result, "recording-takes")


def test_a_truncated_original_is_refused_before_it_is_read(native: NativeLibrary) -> None:
    original = native.layout.root / "tmp/recording-takes" / native.edited / "original.wav"
    original.write_bytes(original.read_bytes()[:-3])
    result, sink = run_pass(native.layout)
    takes = result.owner("recording-takes")
    assert (takes.state, takes.problem) == ("refused", "mismatch")
    assert "bytes, but its record names" in (takes.reason or "")
    (failed,) = [r for r in sink.reads if r.item.path.endswith(f"{native.edited}/original.wav")]
    assert (failed.chunks, failed.entry) == ([], None)


@pytest.mark.parametrize("link", ["symlinked-original", "hard-linked-record", "symlinked-raw"])
def test_linked_sources_are_refused(native: NativeLibrary, tmp_path: Path, link: str) -> None:
    root = native.layout.root
    if link == "symlinked-original":
        target = root / "tmp/recording-takes" / native.edited / "original.wav"
        target.rename(tmp_path / "elsewhere.wav")
        os.symlink(tmp_path / "elsewhere.wav", target)
        owner, problem = "recording-takes", "unsafe"
    elif link == "hard-linked-record":
        target = sorted((root / "provenance/recording-analysis/jobs").iterdir())[0]
        os.link(target, tmp_path / "second-name.json")
        owner, problem = "recording-analysis", "linked"
    else:
        session = tmp_path / "raw-session"
        write_file(session, "READY")
        target = root / "raw" / SHA
        os.symlink(session, target)
        owner, problem = "raw-packages", "unsafe"
    result, sink = run_pass(native.layout)
    refused = result.owner(owner)
    assert (refused.state, refused.problem, refused.problem_path) == (
        "refused",
        problem,
        target.relative_to(root).as_posix(),
    )
    assert all(read.entry is None for read in sink.reads if read.item.path == refused.problem_path)


def test_a_symlinked_directory_fails_the_pass(libraries: Libraries, tmp_path: Path) -> None:
    root = tmp_path.resolve() / "assets"
    shutil.copytree(libraries.accepted.root, root, symlinks=True)
    restore_permissions(root)
    consents = root / "provenance/consents"
    consents.rename(tmp_path / "consents")
    os.symlink(tmp_path / "consents", consents)
    with pytest.raises(StorageError, match="provenance/consents") as caught:
        run_pass(AssetLayout.validate(root))
    assert caught.value.code == "STORAGE.PROTOCOL"


# -- fine-tune runs ------------------------------------------------------------------------


@pytest.fixture
def runs(tmp_path: Path) -> AssetLayout:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    add_fine_tune_runs(layout, tmp_path.resolve() / "runtime")
    return layout


def run_owner(layout: AssetLayout, sink: RecordingSink | None = None) -> OwnerResult:
    result, _ = run_pass(layout, (FineTuneRunSource(),), sink, classes=("training",))
    return result.owner("fine-tune-runs")


def test_a_removed_checkpoint_is_carried_from_the_generation_its_record_names(
    runs: AssetLayout,
) -> None:
    owner = run_owner(runs)
    path = f"derived/finetune/{RUN_FINISHED}/{REMOVED_CHECKPOINT.path}"

    assert owner.state == "complete"
    assert [item.path for item in owner.carried] == [path]
    carried = owner.carried[0]
    assert (carried.file_class, carried.kind, carried.mode) == ("training", "file", 0o600)
    assert (carried.sha256, carried.size) == (REMOVED_CHECKPOINT.sha256, REMOVED_CHECKPOINT.size)
    assert carried.generation == REMOVED_GENERATION
    assert "restore-run" in carried.reason
    # The file is gone, so nothing was read for it and nothing withheld it.
    assert path not in {entry.path for entry in owner.entries}
    assert path not in {item.path for item in owner.withheld}


def test_a_carried_path_names_its_pin_source_exactly_when_it_is_a_pin() -> None:
    fields: dict[str, Any] = {
        "path": f"derived/finetune/{RUN_FINISHED}/checkpoints/model_9.pt",
        "file_class": "training",
        "mode": 0o600,
        "sha256": "c" * 64,
        "size": 1_024,
        "generation": REMOVED_GENERATION,
        "reason": "removed",
    }
    with pytest.raises(ValueError, match="names its pin source"):
        CarriedPath(**fields, kind="pin")
    with pytest.raises(ValueError, match="names its pin source"):
        CarriedPath(**fields, kind="file", pin_source=PRETRAINED_PIN_SOURCE)

    assert CarriedPath(**fields, kind="file").pin_source is None
    assert CarriedPath(**fields, kind="pin", pin_source=PRETRAINED_PIN_SOURCE).kind == "pin"


def test_a_removal_record_that_names_another_run_or_file_refuses_the_owner(
    runs: AssetLayout,
) -> None:
    record = runs.root / "derived/finetune" / RUN_FINISHED / "local-removal.json"
    removal = FineTuneRunRemoval.model_validate_json(record.read_bytes())
    record.chmod(0o600)
    write_canonical_json(record, removal.model_copy(update={"run_id": "run-other"}))

    refused = run_owner(runs)

    assert (refused.state, refused.problem) == ("refused", "invalid")
    assert refused.problem_path == f"derived/finetune/{RUN_FINISHED}/local-removal.json"
    assert "names another run" in str(refused.reason)

    record.chmod(0o600)
    write_canonical_json(
        record,
        removal.model_copy(
            update={
                "files": (
                    FineTuneArchivedFile(
                        path="checkpoints/model_9.pt", sha256="d" * 64, size=8, kind="pin"
                    ),
                )
            }
        ),
    )

    outside = run_owner(runs)

    assert (outside.state, outside.problem) == ("refused", "invalid")
    assert "not one of its run's files" in str(outside.reason)

    # A record naming anything but a checkpoint -- a summary record every status reply reads,
    # say -- is refused by the record itself, wherever it is read.
    record.chmod(0o600)
    write_canonical_json(
        record,
        removal.model_copy(
            update={
                "files": (
                    FineTuneArchivedFile(path="run.json", sha256="d" * 64, size=8, kind="file"),
                )
            }
        ),
    )

    summary = run_owner(runs)

    assert (summary.state, summary.problem) == ("refused", "invalid")
    assert "is not a valid FineTuneRunRemoval record" in str(summary.reason)


def test_a_removed_file_that_came_back_is_read_again_instead_of_carried(
    runs: AssetLayout,
) -> None:
    checkpoints = runs.root / "derived/finetune" / RUN_FINISHED / "checkpoints"
    write_file(checkpoints, "model_9.pt", b"a checkpoint that was restored", mode=0o600)

    owner = run_owner(runs)

    assert owner.state == "complete" and owner.carried == ()
    assert f"derived/finetune/{RUN_FINISHED}/checkpoints/model_9.pt" in {
        entry.path for entry in owner.entries
    }


def queued_job() -> FineTuneJobHead:
    return FineTuneJobHead(
        job=FineTuneJob(operation_id="finetune-2", request_sha256=SHA, queued_at=utc_now())
    )


@pytest.mark.parametrize(
    ("activity", "expected"),
    [
        ("queued-job", "fine-tune job finetune-2 is queued"),
        ("live-speak-request", "Speak request speak-1 is running"),
        ("stale-speak-request", None),
    ],
)
def test_an_active_fine_tune_job_or_speak_request_holds_back_every_run(
    runs: AssetLayout, activity: str, expected: str | None
) -> None:
    jobs = runs.root / "provenance/finetune-jobs"
    if activity == "queued-job":
        write_recording_evidence(jobs / "head.json", queued_job())
    else:
        pid = os.getpid() if activity == "live-speak-request" else 2**31 - 7
        marker = SpeechActiveMarker(operation_id="speak-1", pid=pid, started_at=utc_now())
        write_recording_evidence(runs.root / SPEECH_ACTIVE_MARKER, marker)
    sink = RecordingSink()
    owner = run_owner(runs, sink)
    if expected is None:
        assert owner.state == "complete" and owner.entries
        return
    assert (owner.state, owner.problem, owner.problem_path) == (
        "changed",
        "changed",
        "derived/finetune",
    )
    assert expected in (owner.reason or "") and sink.reads == []


def test_a_job_that_starts_during_the_pass_holds_back_the_runs(runs: AssetLayout) -> None:
    def on_begin(owner: str, item: ReplicationItem) -> None:
        head = runs.root / "provenance/finetune-jobs/head.json"
        if b"finetune-2" not in head.read_bytes():
            write_recording_evidence(head, queued_job())

    owner = run_owner(runs, RecordingSink(on_begin=on_begin))
    assert (owner.state, owner.problem) == ("changed", "changed")
    assert "is queued" in (owner.reason or "")


def test_an_unreadable_fine_tune_queue_is_refused(runs: AssetLayout) -> None:
    write_file(runs.root, "provenance/finetune-jobs/head.json", b"not canonical")
    owner = run_owner(runs)
    assert (owner.state, owner.problem, owner.problem_path) == (
        "refused",
        "invalid",
        "provenance/finetune-jobs/head.json",
    )


def run_file(layout: AssetLayout, relative: str) -> Path:
    return layout.root / "derived/finetune" / RUN_FINISHED / relative


@pytest.mark.parametrize(
    ("damage", "problem", "path"),
    [
        ("run-names-another-run", "invalid", "run.json"),
        ("run-is-not-a-run", "invalid", "run.json"),
        ("dataset-differs", "mismatch", "dataset/dataset.json"),
        ("clip-differs", "mismatch", f"dataset/wavs/{spoken(1)}.wav"),
        ("unpinned-weights", "invalid", "checkpoints/pretrained_other.safetensors"),
        ("checkpoint-differs", "mismatch", "checkpoints/model_last.pt"),
        ("sample-rebinds-a-checkpoint", "invalid", "samples/base/samples.json"),
        ("sample-named-twice", "invalid", "samples/base/samples.json"),
        ("sample-set-is-not-one", "invalid", "samples/base/samples.json"),
    ],
)
def test_run_files_must_match_what_their_records_name(
    runs: AssetLayout, damage: str, problem: str, path: str
) -> None:
    if damage == "run-names-another-run":
        (runs.root / "derived/finetune" / RUN_FINISHED).rename(
            runs.root / "derived/finetune/run-renamed"
        )
        path = "run.json"
        expected_path = "derived/finetune/run-renamed/run.json"
    else:
        expected_path = f"derived/finetune/{RUN_FINISHED}/{path}"
        target = run_file(runs, path)
        if damage == "run-is-not-a-run":
            write_file(target.parent, target.name, b"{}\n")
        elif damage == "dataset-differs":
            data = target.read_text().replace(
                '"normalization_rule":"synthetic"', '"normalization_rule":"other"'
            )
            target.write_text(data)
        elif damage in ("clip-differs", "checkpoint-differs"):
            target.write_bytes(target.read_bytes()[:-1] + b"!")
        elif damage == "unpinned-weights":
            write_file(target.parent, target.name, RUNTIME_WEIGHTS)
        elif damage == "sample-set-is-not-one":
            write_file(target.parent, target.name, b"{}\n")
        else:
            record = FineTuneSampleSet.model_validate_json(target.read_bytes())
            first, second, third = record.samples
            if damage == "sample-rebinds-a-checkpoint":
                # A listing is not bound by run.json, so it must not rebind a checkpoint.
                checkpoint = run_file(runs, "checkpoints/model_last.pt")
                first = first.model_copy(
                    update={
                        "relative_path": checkpoint.relative_to(runs.root).as_posix(),
                        "sha256": hashlib.sha256(b"other weights").hexdigest(),
                    }
                )
                checkpoint.write_bytes(b"other weights")
            else:
                second = second.model_copy(update={"relative_path": first.relative_path})
            write_canonical_json(
                target, record.model_copy(update={"samples": (first, second, third)})
            )
    owner = run_owner(runs)
    assert (owner.state, owner.problem, owner.problem_path) == ("refused", problem, expected_path)


def test_a_run_that_changes_while_it_is_read_is_held_back(runs: AssetLayout) -> None:
    def on_begin(owner: str, item: ReplicationItem) -> None:
        if item.path.endswith("run.json"):
            write_file(run_file(runs, ""), "samples/again/sample-01.wav", b"RIFF late sample")

    owner = run_owner(runs, RecordingSink(on_begin=on_begin))
    assert (owner.state, owner.problem, owner.problem_path) == (
        "changed",
        "changed",
        f"derived/finetune/{RUN_FINISHED}",
    )


def test_a_sample_rewritten_in_place_during_the_pass_holds_back_the_runs(
    runs: AssetLayout,
) -> None:
    listing = run_file(runs, "samples/finetuned/samples.json")
    samples = FineTuneSampleSet.model_validate_json(listing.read_bytes())
    first = samples.samples[0]
    target = runs.root / first.relative_path
    old = b"RIFF synthetic sample, first synthesis"
    write_file(target.parent, target.name, old)
    bound = first.model_copy(update={"sha256": hashlib.sha256(old).hexdigest()})
    rebound = samples.model_copy(update={"samples": (bound, *samples.samples[1:])})
    write_canonical_json(listing, rebound)
    assert run_owner(runs).state == "complete"

    def on_begin(owner: str, item: ReplicationItem) -> None:
        # `voice-lab finetune synthesize` rewrites the sample in place, with no job marker to
        # announce it, and its listing only afterwards.
        if item.path == listing.relative_to(runs.root).as_posix():
            with target.open("r+b") as stream:
                stream.truncate(0)
                stream.write(b"RIFF synthetic sample, second synthesis")

    owner = run_owner(runs, RecordingSink(on_begin=on_begin))
    assert (owner.state, owner.problem, owner.problem_path) == (
        "changed",
        "changed",
        f"derived/finetune/{RUN_FINISHED}",
    )


SAMPLES_REWRITTEN = "a synthesizer is rewriting this sample set"
SAMPLES_UNRECORDED = "no samples.json records the set yet"


@dataclass
class CommandLineSynthesis:
    """``voice-lab finetune synthesize`` into one sample set of the finished run, step by step.

    The steps keep their real order: ``finetune._synthesize`` writes ``job.json``; the
    synthesizer (``f5_synthesize.py``) creates its work directory, rewrites each sample in
    place and removes the directory when it exits; the command then makes each clip private,
    hashes the checkpoint and only then replaces ``samples.json``.
    """

    layout: AssetLayout
    label: str
    synthesis: str = "second synthesis"

    @property
    def folder(self) -> Path:
        return run_file(self.layout, f"samples/{self.label}")

    def sample(self, index: int) -> Path:
        return self.folder / f"sample-{index:02d}.wav"

    def start(self) -> None:
        write_canonical_json(private_directory(self.folder) / "job.json", {"run": self.synthesis})
        work = self.folder / "synthesis-tmp"
        if work.exists():
            shutil.rmtree(work)  # an interrupted earlier synthesis left it
        write_file(private_directory(work), "tmpreference.wav", b"RIFF converted reference")

    def write_sample(self, index: int) -> None:
        with self.sample(index).open("wb") as stream:  # truncates in place, as sf.write does
            stream.write(f"RIFF {self.label} sample {index}, {self.synthesis}".encode())

    def exit(self) -> None:
        shutil.rmtree(self.folder / "synthesis-tmp")

    def record(self, count: int = 3) -> None:
        samples = []
        for index in range(1, count + 1):
            path = self.sample(index)
            path.chmod(0o600)
            samples.append(
                FineTuneSample(
                    index=index,
                    relative_path=path.relative_to(self.layout.root).as_posix(),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    seconds=4.5,
                    text=f"sentence {index}",
                )
            )
        write_canonical_json(
            self.folder / "samples.json",
            FineTuneSampleSet(
                label=self.label,
                checkpoint_path="/synthetic/model_last.pt",
                checkpoint_sha256="7" * 64,
                use_ema=True,
                nfe_steps=32,
                device="mps",
                reference_prompt_id=spoken(1),
                reference_audio_sha256="9" * 64,
                samples=tuple(samples),
                created_at=utc_now(),
            ),
        )

    def run(self, steps: tuple[str, ...]) -> None:
        for step in steps:
            if step.startswith("sample-"):
                self.write_sample(int(step.removeprefix("sample-")))
            else:
                getattr(self, step)()


def synthesized_earlier(layout: AssetLayout, label: str) -> None:
    """A set a finished earlier synthesis wrote (the fixture's set left no audio behind)."""
    CommandLineSynthesis(layout, label, "first synthesis").run(
        ("start", "sample-1", "sample-2", "sample-3", "exit", "record")
    )


EVERY_SAMPLE = ("sample-1", "sample-2", "sample-3")


@pytest.mark.parametrize(
    ("label", "steps", "outcome"),
    [
        pytest.param("finetuned", ("start",), "first", id="existing-set-synthesizer-loading"),
        pytest.param(
            "finetuned",
            ("start", "sample-1"),
            SAMPLES_REWRITTEN,
            id="existing-set-rewriting-or-interrupted",
        ),
        pytest.param(
            "finetuned",
            ("start", *EVERY_SAMPLE, "exit"),
            SAMPLES_REWRITTEN,
            id="existing-set-hashing-the-checkpoint",
        ),
        pytest.param(
            "finetuned",
            ("start", *EVERY_SAMPLE, "exit", "record"),
            "second",
            id="existing-set-done",
        ),
        pytest.param("again", ("start",), SAMPLES_UNRECORDED, id="new-set-synthesizer-loading"),
        pytest.param(
            "again", ("start", "sample-1"), SAMPLES_UNRECORDED, id="new-set-writing-or-interrupted"
        ),
        pytest.param(
            "again",
            ("start", *EVERY_SAMPLE, "exit"),
            "unbound",
            id="new-set-hashing-the-checkpoint",
        ),
        pytest.param(
            "again", ("start", *EVERY_SAMPLE, "exit", "record"), "second", id="new-set-done"
        ),
    ],
)
def test_a_sample_set_is_carried_only_as_its_record_names_it_at_every_step_of_a_synthesis(
    runs: AssetLayout, label: str, steps: tuple[str, ...], outcome: str
) -> None:
    for existing in ("finetuned", "base"):
        synthesized_earlier(runs, existing)
    first = run_file(runs, f"samples/{label}/samples.json")
    first_record = first.read_bytes() if first.exists() else None
    CommandLineSynthesis(runs, label).run(steps)
    sink = RecordingSink()
    owner = run_owner(runs, sink)
    assert owner.state == "complete", owner
    folder = f"derived/finetune/{RUN_FINISHED}/samples/{label}/"
    served = {p for p in tree_paths(runs.root) if p.startswith(folder) and classify(p).copied}
    carried = {e.path for e in owner.entries if e.path.startswith(folder)}
    withheld = {w.path: w.reason for w in owner.withheld if w.path.startswith(folder)}
    offered = {read.item.path: read.item for read in sink.reads}
    if outcome in (SAMPLES_REWRITTEN, SAMPLES_UNRECORDED):
        assert not carried and set(withheld) == served
        assert all(outcome in reason for reason in withheld.values())
    else:
        assert not withheld and carried == served
        samples = {p for p in served if p.endswith(".wav")}
        assert len(samples) == 3
        if outcome == "unbound":
            assert all(offered[path].expected_sha256 is None for path in samples)
        else:
            record = first_record if outcome == "first" else first.read_bytes()
            assert record is not None
            listing = FineTuneSampleSet.model_validate_json(record)
            assert offered[f"{folder}samples.json"].expected_sha256 == (
                hashlib.sha256(record).hexdigest()
            )
            assert {path: offered[path].expected_sha256 for path in samples} == {
                sample.relative_path: sample.sha256 for sample in listing.samples
            }
    # The other sets are carried either way.
    other = "base" if label == "finetuned" else "finetuned"
    assert f"derived/finetune/{RUN_FINISHED}/samples/{other}/samples.json" in {
        e.path for e in owner.entries
    }


def test_a_sample_that_differs_from_its_record_withholds_its_set_and_refuses_nothing(
    runs: AssetLayout,
) -> None:
    # The fixture's records name samples it never wrote; one that is there must match.
    write_file(run_file(runs, "samples/base"), "sample-01.wav", b"RIFF another sample")
    owner = run_owner(runs)
    assert owner.state == "complete"
    folder = f"derived/finetune/{RUN_FINISHED}/samples/base/"
    assert {w.path for w in owner.withheld if SAMPLES_REWRITTEN in w.reason} == {
        f"{folder}sample-01.wav",
        f"{folder}samples.json",
    }
    assert not any(e.path.startswith(folder) for e in owner.entries)
    # A work directory left in a set whose files match its record changes nothing.
    assert run_file(runs, "samples/finetuned/synthesis-tmp").is_dir()
    assert f"derived/finetune/{RUN_FINISHED}/samples/finetuned/samples.json" in {
        e.path for e in owner.entries
    }


@pytest.mark.parametrize("event", ["synthesis-starts", "leftover-work-directory-removed"])
def test_a_synthesizer_work_directory_that_comes_or_goes_during_the_pass_holds_back_the_runs(
    runs: AssetLayout, event: str
) -> None:
    synthesized_earlier(runs, "base")
    synthesis = CommandLineSynthesis(runs, "base")

    def on_begin(owner: str, item: ReplicationItem) -> None:
        if not item.path.endswith("/run.json"):
            return
        if event == "synthesis-starts":
            # Only the work directory appears: job.json is left as it was.
            write_file(private_directory(synthesis.folder / "synthesis-tmp"), "tmp.wav", b"RIFF")
        else:
            shutil.rmtree(run_file(runs, "samples/finetuned/synthesis-tmp"))

    owner = run_owner(runs, RecordingSink(on_begin=on_begin))
    assert (owner.state, owner.problem, owner.problem_path) == (
        "changed",
        "changed",
        f"derived/finetune/{RUN_FINISHED}",
    )
    assert run_owner(runs).state == "complete"


def test_a_library_without_runs_has_nothing_to_carry(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    assert run_owner(layout) == OwnerResult("fine-tune-runs", "complete", (), ())


# -- take store ----------------------------------------------------------------------------


def takes_owner(layout: AssetLayout, source: TakeStoreSource | None = None) -> OwnerResult:
    result, _ = run_pass(layout, (source or TakeStoreSource(),), classes=("records", "recordings"))
    return result.owner("recording-takes")


def test_the_take_store_pages_its_takes_and_withholds_what_no_head_names(
    native: NativeLibrary,
) -> None:
    root = native.layout.root
    orphan = f"take-{'f' * 64}"
    write_file(root, f"provenance/recording-takes/{orphan}/reservation.json")
    write_file(root, f"provenance/recording-edits/native/{orphan}/head.json")
    write_synthetic_wav(
        private_directory(root / "tmp/recording-takes" / orphan) / "original.wav", 0.01
    )
    write_file(root, f"provenance/recording-takes/.operations/.save-9.json.tmp-{HEX16}")
    # A take reserved a moment ago has no audio yet: nothing to carry or withhold for it.
    new_rig(native.layout, native.group_id).reserve("reserve-bare", pilot_prompt(7))
    paged = takes_owner(native.layout, TakeStoreSource(page_size=1))
    whole = takes_owner(native.layout)
    assert paged == whole and whole.state == "complete"
    assert {item.path: item.reason for item in whole.withheld} == {
        f"provenance/recording-takes/{orphan}/reservation.json": (
            "no committed take head names this file"
        ),
        f"provenance/recording-edits/native/{orphan}/head.json": "no take record names this file",
        f"tmp/recording-takes/{orphan}/original.wav": "no take record names this file",
        f"tmp/recording-takes/{native.draft}/original.wav": (
            "the take is not sealed yet; its audio is carried once its seal names it"
        ),
    }


def test_an_uncommitted_seal_is_withheld(native: NativeLibrary) -> None:
    root = native.layout.root
    seal = root / "provenance/recording-takes" / native.draft / "seal.json"
    write_file(seal.parent, seal.name)
    owner = takes_owner(native.layout)
    assert owner.state == "complete"
    assert (
        WithheldPath(
            seal.relative_to(root).as_posix(), "records", "no committed take head names this file"
        )
        in owner.withheld
    )


def test_a_commit_in_progress_makes_the_take_store_changed(native: NativeLibrary) -> None:
    write_file(native.layout.root, "provenance/recording-takes/.pending.json")
    owner = takes_owner(native.layout)
    assert (owner.state, owner.problem, owner.problem_path) == (
        "changed",
        "changed",
        "provenance/recording-takes/.pending.json",
    )


def test_a_commit_that_starts_between_reads_makes_the_take_store_changed(
    native: NativeLibrary,
) -> None:
    pending = native.layout.root / "provenance/recording-takes/.pending.json"

    def on_begin(owner: str, item: ReplicationItem) -> None:
        if item.path.endswith("/reservation.json") and not pending.exists():
            write_file(pending.parent, pending.name)

    result, _ = run_pass(native.layout, (TakeStoreSource(),), RecordingSink(on_begin=on_begin))
    owner = result.owner("recording-takes")
    assert (owner.state, owner.problem, owner.problem_path) == (
        "changed",
        "changed",
        "provenance/recording-takes/.pending.json",
    )


@pytest.mark.parametrize("damage", ["head-disagrees", "store-refuses-to-open"])
def test_take_records_the_store_refuses_are_refused(native: NativeLibrary, damage: str) -> None:
    root = native.layout.root
    if damage == "head-disagrees":
        head = root / "provenance/recording-takes" / native.draft / "head.json"
        head.write_bytes(head.read_bytes().replace(b'"transport":"ready"', b'"transport":"drafty"'))
        path = head.relative_to(root).as_posix()
    else:
        shutil.rmtree(root / "provenance/recording-groups/.prepared-gates")
        path = "provenance/recording-takes"
    owner = takes_owner(native.layout)
    expected = (
        ("changed", "changed") if damage == "store-refuses-to-open" else ("refused", "invalid")
    )
    assert (owner.state, owner.problem, owner.problem_path) == (*expected, path)


def test_a_library_without_takes_offers_none(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    assert takes_owner(layout) == OwnerResult("recording-takes", "complete", (), ())
    RecordingTakeStore(layout)  # an empty store has no store head yet
    assert (layout.root / "provenance/recording-takes").is_dir()
    assert takes_owner(layout) == OwnerResult("recording-takes", "complete", (), ())


def test_a_take_whose_audio_lies_outside_it_is_refused(
    native: NativeLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = replication._served

    def served(path: str, rules: frozenset[str]) -> Any:
        return None if path.endswith("/original.wav") else real(path, rules)

    monkeypatch.setattr(replication, "_served", served)
    owner = takes_owner(native.layout)
    assert (owner.state, owner.problem) == ("refused", "invalid")
    assert "outside its take" in (owner.reason or "")


# -- legacy capture sessions ---------------------------------------------------------------


@pytest.fixture
def legacy(tmp_path: Path) -> AssetLayout:
    return build_legacy_root(tmp_path.resolve() / "assets").layout


def sessions_owner(layout: AssetLayout, sink: RecordingSink | None = None) -> OwnerResult:
    result, _ = run_pass(layout, (CaptureSessionSource(),), sink, classes=("records", "recordings"))
    return result.owner("capture-sessions")


def session_directory(layout: AssetLayout, *, ready: bool, rejected: bool = False) -> Path:
    tmp = layout.directory("tmp")
    return next(
        path
        for path in sorted(tmp.glob("capture-desktop-*"))
        if path.name.endswith(".rejected") == rejected
        and (rejected or (path / "READY").exists() == ready)
    )


def test_a_desktop_session_record_that_changes_during_the_pass_holds_back_the_sessions(
    legacy: AssetLayout,
) -> None:
    sidecar = min((legacy.root / "provenance/capture-desktop").glob("*.json"))

    def on_begin(owner: str, item: ReplicationItem) -> None:
        if item.path.startswith("tmp/"):
            os.utime(sidecar, ns=(1, 1))

    owner = sessions_owner(legacy, RecordingSink(on_begin=on_begin))
    assert (owner.state, owner.problem, owner.problem_path) == (
        "changed",
        "changed",
        sidecar.relative_to(legacy.root).as_posix(),
    )


def test_session_audio_is_carried_only_as_records_name_it(legacy: AssetLayout) -> None:
    unfinished = session_directory(legacy, ready=False)
    rejected = session_directory(legacy, ready=False, rejected=True)
    root = legacy.root
    write_file(unfinished, "audio/099-extra.wav", b"RIFF")
    write_file(unfinished, "audio/.DS_Store", b"")
    write_file(rejected, "002-reference-warm-01.take-1.wav", b"RIFF")
    write_file(rejected, ".DS_Store", b"")
    orphan = legacy.directory("tmp") / "capture-orphan-session"
    write_file(orphan, "audio/000-room-tone.wav", b"RIFF")
    write_file(orphan, "audio/001-reference-neutral-01.partial.wav", b"RIFF")
    write_file(legacy.directory("tmp"), "tmpab12cd34", b"")
    (legacy.directory("tmp") / "capture-empty").mkdir()
    (legacy.directory("tmp") / "capture-Unclassifiable").mkdir()
    owner = sessions_owner(legacy)
    assert owner.state == "complete"
    held = {item.path for item in owner.withheld}

    def relative(path: Path) -> str:
        return path.relative_to(root).as_posix()

    assert held == {
        relative(unfinished / "audio/099-extra.wav"),
        relative(rejected / "002-reference-warm-01.take-1.wav"),
        relative(orphan / "audio/000-room-tone.wav"),
        relative(orphan / "audio/001-reference-neutral-01.partial.wav"),
    }
    partials = [e for e in owner.entries if e.path.endswith(".partial.wav")]
    assert len(partials) == 1 and partials[0].path.startswith("tmp/capture-desktop-")
    # A capture name that is not a directory (it could appear only after the classification
    # walk) is not a session.
    write_file(legacy.directory("tmp"), "capture-not-a-directory", b"")
    offered = {entry.path for entry in CaptureSessionSource().items(legacy)}
    assert offered == {e.path for e in owner.entries} | held


@pytest.mark.parametrize(
    "damage",
    [
        "sidecar-misnamed",
        "manifest-differs",
        "package-misnamed",
        "rejected-log-names-another-session",
        "sidecar-invalid",
    ],
)
def test_inconsistent_session_records_are_refused(legacy: AssetLayout, damage: str) -> None:
    root = legacy.root
    sidecars = root / "provenance/capture-desktop"
    finalized = session_directory(legacy, ready=True)
    rejected = session_directory(legacy, ready=False, rejected=True)
    if damage == "sidecar-misnamed":
        sidecar = next(sidecars.glob("desktop-*.json"))
        target = sidecars / "desktop-renamed.json"
        sidecar.rename(target)
        expected = ("refused", "invalid", target)
    elif damage == "manifest-differs":
        manifest = finalized / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        expected = ("refused", "mismatch", manifest)
    elif damage == "package-misnamed":
        target = finalized.parent / "capture-renamed"
        finalized.rename(target)
        expected = ("refused", "invalid", target)
    elif damage == "rejected-log-names-another-session":
        target = rejected.parent / "capture-renamed.rejected"
        rejected.rename(target)
        expected = ("refused", "invalid", target / "rejected-takes.json")
    else:
        sidecar = next(sidecars.glob("desktop-*.json"))
        sidecar.write_bytes(b"{}\n")
        expected = ("refused", "invalid", sidecar)
    owner = sessions_owner(legacy)
    assert (owner.state, owner.problem, owner.problem_path) == (
        expected[0],
        expected[1],
        expected[2].relative_to(root).as_posix(),
    )


def test_a_take_rejected_during_the_pass_holds_back_the_sessions(legacy: AssetLayout) -> None:
    rejected = session_directory(legacy, ready=False, rejected=True)
    relative = rejected.relative_to(legacy.root).as_posix()
    log_path = rejected / REJECTED_LOG_NAME
    added = rejected / "009-reference-extra-01.take-1.wav"
    assert sessions_owner(legacy).state == "complete"

    def on_begin(owner: str, item: ReplicationItem) -> None:
        # capture.py's rejection moves the take in, then rewrites the log (a capture console
        # session has no desktop session record to rewrite).
        if item.path.startswith(f"{relative}/") and item.path.endswith(".wav"):
            if not added.exists():
                data = b"RIFF synthetic rejected take"
                write_file(rejected, added.name, data)
                log = RejectedTakeLog.model_validate_json(log_path.read_bytes())
                entry = log.entries[0].model_copy(
                    update={
                        "relative": added.name,
                        "take_number": 1,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data),
                    }
                )
                grown = RejectedTakeLog(session_id=log.session_id, entries=(*log.entries, entry))
                write_canonical_json(log_path, grown)

    owner = sessions_owner(legacy, RecordingSink(on_begin=on_begin))
    assert added.exists()
    assert (owner.state, owner.problem) == ("changed", "changed")
    assert owner.problem_path is not None and owner.problem_path.startswith(f"{relative}/")
    after = sessions_owner(legacy)
    assert after.state == "complete"
    assert added.relative_to(legacy.root).as_posix() in {entry.path for entry in after.entries}


def test_rejected_takes_are_withheld_until_their_log_names_them(legacy: AssetLayout) -> None:
    rejected = session_directory(legacy, ready=False, rejected=True)
    (rejected / "rejected-takes.json").unlink()
    owner = sessions_owner(legacy)
    assert owner.state == "complete"
    assert {item.path for item in owner.withheld} == {
        (rejected / name).relative_to(legacy.root).as_posix() for name in os.listdir(rejected)
    }


# -- raw sessions and references -----------------------------------------------------------


@pytest.fixture
def accepted(libraries: Libraries, tmp_path: Path) -> AssetLayout:
    root = tmp_path.resolve() / "assets"
    shutil.copytree(libraries.accepted.root, root, symlinks=True)
    restore_permissions(root)
    return AssetLayout.validate(root)


@pytest.mark.parametrize(
    "damage",
    [
        "raw-without-ready",
        "receipt-names-another-session",
        "reference-without-manifest",
        "reference-misnamed",
    ],
)
def test_inconsistent_raw_sessions_and_references_are_refused(
    accepted: AssetLayout, damage: str
) -> None:
    root = accepted.root
    (raw,) = (root / "raw").iterdir()
    (reference,) = (root / "references").iterdir()
    if damage == "raw-without-ready":
        (raw / "READY").unlink()
        owner_name, problem, path = "raw-packages", "invalid", raw
    elif damage == "receipt-names-another-session":
        receipt = raw / "intake-receipt.json"
        receipt.write_bytes(receipt.read_bytes().replace(raw.name.encode(), b"0" * 64))
        owner_name, problem, path = "raw-packages", "invalid", receipt
    elif damage == "reference-without-manifest":
        (reference / "manifest.json").unlink()
        owner_name, problem, path = "references", "invalid", reference
    else:
        target = reference.parent / "renamed-reference"
        reference.rename(target)
        owner_name, problem, path = "references", "invalid", target / "manifest.json"
    result, _ = run_pass(accepted, (ReferenceSource(), RawPackageSource()), classes=("recordings",))
    owner = result.owner(owner_name)
    assert (owner.state, owner.problem, owner.problem_path) == (
        "refused",
        problem,
        path.relative_to(root).as_posix(),
    )


def test_stray_files_beside_raw_sessions_and_references_are_carried(accepted: AssetLayout) -> None:
    write_file(accepted.root, "raw/notes.txt", b"synthetic notes", mode=0o400)
    write_file(accepted.root, "references/notes.txt", b"synthetic notes", mode=0o400)
    write_file(accepted.root, "raw/.DS_Store", b"")
    write_file(accepted.root, "references/.DS_Store", b"")
    (reference,) = (path for path in (accepted.root / "references").iterdir() if path.is_dir())
    write_file(reference, ".DS_Store", b"")
    result, _ = run_pass(accepted, (ReferenceSource(), RawPackageSource()), classes=("recordings",))
    assert result.complete
    paths = {entry.path for entry in result.entries}
    assert {"raw/notes.txt", "references/notes.txt"} <= paths
    assert not {"raw/.DS_Store", "references/.DS_Store"} & paths


# -- readers -------------------------------------------------------------------------------


def item_for(
    root: Path,
    relative: str,
    *,
    expected: tuple[str | None, int | None] = (None, None),
    private: bool = True,
) -> ReplicationItem:
    return ReplicationItem(
        path=relative,
        file_class="records",
        kind="file",
        mode=0o600,
        expected_sha256=expected[0],
        expected_size=expected[1],
        root=root,
        source=relative,
        private=private,
    )


def read_all(item: ReplicationItem, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> bytes:
    with item.open(chunk_bytes=chunk_bytes) as reader:
        data = b"".join(reader.chunks())
    assert (reader.sha256, reader.size) == (hashlib.sha256(data).hexdigest(), len(data))
    return data


def refusal(item: ReplicationItem, **options: Any) -> SourceChangedError:
    with pytest.raises(SourceChangedError) as caught:
        read_all(item, **options)
    return caught.value


class HashingSink:
    """Keeps only a digest and the size of the pieces it was handed."""

    def __init__(self) -> None:
        self.largest = 0
        self.pieces = 0
        self.digest = hashlib.sha256()
        self.entries: list[PassEntry | None] = []

    def begin(self, owner: str, item: ReplicationItem) -> None:
        self.digest = hashlib.sha256()

    def chunk(self, data: bytes) -> None:
        self.largest = max(self.largest, len(data))
        self.pieces += 1
        self.digest.update(data)

    def finish(self, entry: PassEntry | None) -> None:
        assert entry is not None and entry.sha256 == self.digest.hexdigest()
        self.entries.append(entry)


def test_streaming_hands_the_sink_only_bounded_chunks(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    write_synthetic_wav(layout.directory("raw") / "long-synthetic.wav", 20.0)
    size = (layout.directory("raw") / "long-synthetic.wav").stat().st_size
    chunk = 64 * 1024
    assert size > 40 * chunk
    sources = (RawPackageSource(),)
    collect_pass(layout, sources, HashingSink(), classes=("recordings",), chunk_bytes=chunk)
    sink = HashingSink()
    tracemalloc.start()
    try:
        result = collect_pass(layout, sources, sink, classes=("recordings",), chunk_bytes=chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    (entry,) = result.entries
    assert (entry.path, entry.size) == ("raw/long-synthetic.wav", size)
    assert sink.largest == chunk and sink.pieces == -(-size // chunk)
    assert peak < size // 4


def test_a_reader_proves_the_file_held_still(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    write_file(root, "a/record.json", b"0123456789")
    item = item_for(root, "a/record.json")
    assert read_all(item, chunk_bytes=3) == b"0123456789"
    with item.open(chunk_bytes=4) as reader:
        assert reader.identity.size == 10
        with pytest.raises(ValueError, match="once verified"):
            _ = reader.sha256
        with pytest.raises(ValueError, match="once verified"):
            _ = reader.size
        pieces = reader.chunks()
        assert next(pieces) == b"0123"
        with pytest.raises(ValueError, match="once"):
            next(reader.chunks())
        assert list(pieces) == [b"4567", b"89"]
    assert reader.sha256 == hashlib.sha256(b"0123456789").hexdigest()
    with pytest.raises(ValueError, match="after its last chunk"):
        with item.open():
            pass


def rewrite_longer(path: Path) -> None:
    path.write_bytes(b"98765432109")


def remove(path: Path) -> None:
    path.unlink()


def append(path: Path) -> None:
    with path.open("ab") as stream:
        stream.write(b"more")


def replace(path: Path) -> None:
    os.replace(write_file(path.parent, "other", b"0123456789"), path)


@pytest.mark.parametrize(
    ("change", "problem", "detail"),
    [
        (rewrite_longer, "changed", "grew while it was being read"),
        (remove, "missing", "was removed while it was being read"),
        (append, "changed", "grew while it was being read"),
        (replace, "changed", "changed while it was being read"),
    ],
)
def test_a_file_that_changes_while_it_is_read_is_not_carried(
    tmp_path: Path, change: Callable[[Path], None], problem: str, detail: str
) -> None:
    root = tmp_path.resolve()
    path = write_file(root, "a/record.json", b"0123456789")
    with pytest.raises(SourceChangedError) as caught:
        with item_for(root, "a/record.json").open(chunk_bytes=4) as reader:
            pieces = reader.chunks()
            next(pieces)
            change(path)
            list(pieces)
    assert (caught.value.problem, caught.value.detail) == (problem, detail)


@pytest.mark.parametrize(
    ("setup", "problem", "detail"),
    [
        ("missing", "missing", "was removed"),
        ("missing-directory", "missing", "was removed"),
        ("symlink", "unsafe", "symbolic link"),
        ("symlinked-directory", "unsafe", "symbolic link"),
        ("fifo", "unsafe", "not a regular file"),
        ("shared", "unsafe", "other users"),
        ("hard-linked", "linked", "2 hard links"),
        ("wrong-size", "mismatch", "record names 11"),
        ("wrong-digest", "mismatch", "record names"),
        ("unreadable", "unsafe", "cannot be opened"),
        ("root-missing", "missing", "was removed"),
    ],
)
def test_a_reader_refuses_unsafe_or_mismatched_files(
    tmp_path: Path, setup: str, problem: str, detail: str
) -> None:
    root = tmp_path.resolve() / "root"
    path = write_file(root, "a/record.json", b"0123456789")
    expected: tuple[str | None, int | None] = (None, None)
    if setup == "missing":
        path.unlink()
    elif setup == "missing-directory":
        shutil.rmtree(root / "a")
    elif setup == "symlink":
        path.rename(root / "real.json")
        os.symlink(root / "real.json", path)
    elif setup == "symlinked-directory":
        (root / "a").rename(root / "b")
        os.symlink(root / "b", root / "a")
    elif setup == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif setup == "shared":
        path.chmod(0o640)
    elif setup == "hard-linked":
        os.link(path, root / "second")
    elif setup == "wrong-size":
        expected = (None, 11)
    elif setup == "wrong-digest":
        expected = ("0" * 64, 10)
    elif setup == "unreadable":
        path.chmod(0o000)
    else:
        root = tmp_path / "nowhere"
    error = refusal(item_for(root, "a/record.json", expected=expected))
    assert (error.path, error.problem) == ("a/record.json", problem)
    assert detail in error.detail and error.refused == (problem not in ("changed", "missing"))
    assert error.code == (
        "REPLICATION.SOURCE_REFUSED" if error.refused else "REPLICATION.SOURCE_CHANGED"
    )
    assert error.retryable != error.refused


def test_a_shared_file_is_read_where_its_owner_does_not_keep_it_private(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    write_file(root, "run/train.log", b"log", mode=0o644)
    assert read_all(item_for(root, "run/train.log", private=False)) == b"log"


def test_a_file_of_another_owner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    write_file(root, "record.json")
    monkeypatch.setattr(replication.os, "getuid", lambda: os.geteuid() + 1)
    error = refusal(item_for(root, "record.json"))
    assert (error.problem, error.detail) == ("unsafe", "is not owned by this user")


def test_a_read_error_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve()
    write_file(root, "record.json")

    def fail(descriptor: int, size: int) -> bytes:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(replication.os, "read", fail)
    error = refusal(item_for(root, "record.json"))
    assert (error.problem, error.detail) == ("unsafe", "cannot be read: Input/output error")


def test_a_read_that_ends_before_the_opened_size_is_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    write_file(root, "record.json", b"0123456789")
    real_read = os.read
    monkeypatch.setattr(
        replication.os, "read", lambda descriptor, size: real_read(descriptor, size)[:5]
    )
    with pytest.raises(SourceChangedError) as caught:
        with item_for(root, "record.json").open(chunk_bytes=5) as reader:
            pieces = reader.chunks()
            next(pieces)
            monkeypatch.setattr(replication.os, "read", lambda descriptor, size: b"")
            list(pieces)
    assert caught.value.problem == "changed"


def test_item_and_chunk_arguments_are_checked(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    pin = ReplicationItem(
        path="derived/finetune/run-1/checkpoints/pretrained_x.safetensors",
        file_class="training",
        kind="pin",
        mode=0o600,
        expected_sha256=SHA,
        expected_size=18,
        root=root,
        source="derived/finetune/run-1/checkpoints/pretrained_x.safetensors",
        pin_source=PRETRAINED_PIN_SOURCE,
    )
    with pytest.raises(ValueError, match="no bytes to read"):
        pin.open()
    for changes in ({"expected_size": None}, {"pin_source": None}, {"kind": "file"}):
        with pytest.raises(ValueError, match="pin item"):
            ReplicationItem(**(pin_fields(pin) | changes))
    with pytest.raises(ValueError, match="chunk"):
        item_for(root, "x").open(chunk_bytes=0)
    with pytest.raises(ValueError, match="chunk"):
        item_for(root, "x").open(chunk_bytes=MAX_CHUNK_BYTES + 1)


def pin_fields(item: ReplicationItem) -> dict[str, Any]:
    return {name: getattr(item, name) for name in item.__slots__}  # type: ignore[attr-defined]


def test_source_errors_pickle_and_name_their_problem() -> None:
    error = SourceChangedError("raw/x", "mismatch", "differs")
    copy = pickle.loads(pickle.dumps(error))
    assert (type(copy), copy.path, copy.problem, copy.detail, str(copy)) == (
        SourceChangedError,
        "raw/x",
        "mismatch",
        "differs",
        "raw/x: differs",
    )
    assert isinstance(error, VoiceLabError) and not isinstance(error, StorageError)
    with pytest.raises(ValueError, match="unknown"):
        SourceChangedError("raw/x", "odd", "x")  # type: ignore[arg-type]


def test_a_huge_record_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve()
    write_file(root, "record.json", b"x" * 11)
    monkeypatch.setattr(replication, "MAX_RECORD_BYTES", 10)
    with pytest.raises(SourceChangedError, match="record limit") as caught:
        replication._read_record(root, "record.json")
    assert caught.value.problem == "invalid"


# -- the pass itself -----------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptedSource:
    """A source that offers what ``script`` yields."""

    name: str
    rules: frozenset[str]
    script: Callable[[AssetLayout], Generator[SourceEntry, None, None]]
    referents: frozenset[str] = frozenset()

    def items(self, layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        return self.script(layout)


def consents(tmp_path: Path) -> AssetLayout:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    write_file(layout.root, "provenance/consents/a.json", b"a")
    write_file(layout.root, "provenance/consents/b.json", b"b")
    return layout


def consent_item(layout: AssetLayout, name: str, **changes: Any) -> ReplicationItem:
    return ReplicationItem(
        **(
            {
                "path": f"provenance/consents/{name}",
                "file_class": "records",
                "kind": "file",
                "mode": 0o600,
                "expected_sha256": None,
                "expected_size": None,
                "root": layout.root,
                "source": f"provenance/consents/{name}",
            }
            | changes
        )
    )


def test_a_source_that_skips_a_failed_item_has_it_withheld(tmp_path: Path) -> None:
    layout = consents(tmp_path)

    def script(layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        try:
            yield consent_item(layout, "a.json", expected_sha256="0" * 64)
        except SourceChangedError:
            pass
        yield consent_item(layout, "b.json")
        try:
            yield consent_item(layout, "missing.json")
        except SourceChangedError:
            return

    source = ScriptedSource("scripted", frozenset({"consents"}), script)
    result, sink = run_pass(layout, (source,), classes=("records",))
    owner = result.owner("scripted")
    assert owner.state == "complete"
    assert [entry.path for entry in owner.entries] == ["provenance/consents/b.json"]
    assert [(item.path, item.reason.split(": ")[0]) for item in owner.withheld] == [
        ("provenance/consents/a.json", "provenance/consents/a.json"),
        ("provenance/consents/missing.json", "provenance/consents/missing.json"),
    ]
    assert [read.entry is not None for read in sink.reads] == [False, True, False]


@pytest.mark.parametrize(
    "offer",
    [
        "another-owner's-file",
        "wrong-class",
        "wrong-mode",
        "file-as-pin",
        "scratch-for-a-file",
        "withheld-of-another-owner",
    ],
)
def test_a_source_that_offers_against_its_classification_fails_the_pass(
    tmp_path: Path, offer: str
) -> None:
    layout = consents(tmp_path)
    write_file(layout.root, "provenance/headphone-reviews/r.json")

    def script(layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        if offer == "another-owner's-file":
            yield consent_item(
                layout,
                "x",
                path="provenance/headphone-reviews/r.json",
                source="provenance/headphone-reviews/r.json",
            )
        elif offer == "wrong-class":
            yield consent_item(layout, "a.json", file_class="recordings")
        elif offer == "wrong-mode":
            yield consent_item(layout, "a.json", mode=0o400)
        elif offer == "file-as-pin":
            yield consent_item(
                layout,
                "a.json",
                kind="pin",
                expected_sha256=SHA,
                expected_size=1,
                pin_source=PRETRAINED_PIN_SOURCE,
            )
        elif offer == "scratch-for-a-file":
            yield consent_item(layout, "a.json", source="provenance/consents/b.json")
        else:
            yield WithheldPath("provenance/headphone-reviews/r.json", "records", "no")

    source = ScriptedSource("scripted", frozenset({"consents"}), script)
    with pytest.raises(StorageError, match="against its classification") as caught:
        run_pass(layout, (source,), classes=("records",))
    assert caught.value.code == "STORAGE.PROTOCOL"


def test_records_written_in_place_are_carried_only_once_their_bytes_are_in(
    tmp_path: Path,
) -> None:
    layout = consents(tmp_path)
    root = layout.root
    history = private_directory(root / "provenance/consents/history")
    history_file = history / consent_history_name(CONSENT_HISTORY_BYTES)
    continuity = private_directory(root / "provenance/capture-continuity/desktop-1")
    written = {
        history_file: CONSENT_HISTORY_BYTES,
        continuity / f"{HEX32}.json": b'{"continuity_checked":true}\n',
    }
    relative = {path: path.relative_to(root).as_posix() for path in written}
    names = {"capture-continuity", "lab-approvals"}
    sources = tuple(source for source in library_sources() if source.name in names)
    classes: tuple[FileClass, ...] = ("records",)
    # consent._preserve_consent_version and capture_native._write_diagnostics create the file,
    # then write its bytes, with no rename; a pass lists it in between.
    descriptors = {
        path: os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600) for path in written
    }
    try:
        result, sink = run_pass(layout, sources, classes=classes)
    finally:
        for path, descriptor in descriptors.items():
            os.write(descriptor, written[path])
            os.close(descriptor)
    assert result.complete
    assert {item.path: item.reason for owner in result.owners for item in owner.withheld} == {
        path: "is still being written; it is carried once its bytes are in"
        for path in relative.values()
    }
    assert not set(relative.values()) & sink.begun()
    after, again = run_pass(layout, sources, classes=classes)
    assert after.complete
    assert {relative[path]: again.carried()[relative[path]] for path in written} == {
        relative[path]: data for path, data in written.items()
    }
    # A history file whose bytes are not the ones its name names is refused.
    write_file(history, history_file.name, b"other synthetic bytes")
    refused, _ = run_pass(layout, sources, classes=classes)
    owner = refused.owner("lab-approvals")
    assert (owner.state, owner.problem, owner.problem_path) == (
        "refused",
        "mismatch",
        relative[history_file],
    )


def test_a_transfer_approval_left_shared_readable_is_carried_and_holds_back_only_plans(
    tmp_path: Path,
) -> None:
    layout = consents(tmp_path)
    approval = write_file(layout.root, "provenance/transfer-approvals/hosted.json", mode=0o644)
    result, _ = run_pass(layout, classes=("records",))
    assert result.complete
    entry = entries_of(result)["provenance/transfer-approvals/hosted.json"]
    assert (entry.owner, entry.mode) == ("transfer-approvals", 0o600)
    # A hard-linked approval is refused, and only the transfer plans (which name approvals) wait.
    os.link(approval, tmp_path / "second-name.json")
    result, _ = run_pass(layout, classes=("records",))
    assert_held_back_only_with(result, "transfer-approvals")
    assert result.owner("transfer-approvals").problem == "linked"
    assert {"provenance/consents/a.json", "provenance/consents/b.json"} <= entries_of(result).keys()


def test_items_and_withheld_paths_of_unwanted_classes_are_skipped(tmp_path: Path) -> None:
    layout = consents(tmp_path)

    def script(layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        yield consent_item(layout, "a.json")
        yield WithheldPath("provenance/consents/b.json", "records", "later")
        yield consent_item(
            layout,
            "x",
            path="tmp/recording-takes/take-" + SHA + "/original.wav",
            source="tmp/recording-takes/take-" + SHA + "/original.wav",
            file_class="recordings",
        )

    source = ScriptedSource("scripted", frozenset({"consents", "native-originals"}), script)
    result, sink = run_pass(layout, (source,), classes=("records",))
    owner = result.owner("scripted")
    assert [e.path for e in owner.entries] == ["provenance/consents/a.json"]
    assert [w.path for w in owner.withheld] == ["provenance/consents/b.json"]
    assert [r.item.path for r in sink.reads] == ["provenance/consents/a.json"]


def test_a_sink_failure_stops_the_pass_and_closes_the_source(tmp_path: Path) -> None:
    layout = consents(tmp_path)
    closed: list[bool] = []

    def script(layout: AssetLayout) -> Generator[SourceEntry, None, None]:
        try:
            yield consent_item(layout, "a.json")
            yield consent_item(layout, "b.json")
        finally:
            closed.append(True)

    class Failing(RecordingSink):
        def chunk(self, data: bytes) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

    source = ScriptedSource("scripted", frozenset({"consents"}), script)
    sink = Failing()
    with pytest.raises(OSError, match="No space"):
        run_pass(layout, (source,), sink, classes=("records",))
    assert closed == [True]
    assert [(read.item.path, read.finished, read.entry) for read in sink.reads] == [
        ("provenance/consents/a.json", True, None)
    ]


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"classes": ()}, "one or more known file classes"),
        ({"classes": ("secrets",)}, "one or more known file classes"),
        ({"chunk_bytes": 0}, "chunk"),
        ({"sources": "duplicate"}, "unique"),
        ({"sources": "unknown-rule"}, "unknown rule"),
        ({"sources": "mac-only-rule"}, "Mac-only rule"),
        ({"sources": "referent-first"}, "raw-packages must be read after it"),
        ({"sources": "referent-of-an-unwanted-class"}, "raw-packages must be read after it"),
        ({"sources": "names-itself"}, "odd must be read after it"),
    ],
)
def test_pass_arguments_are_checked_before_any_read(
    tmp_path: Path, arguments: dict[str, Any], problem: str
) -> None:
    layout = consents(tmp_path)
    sources: tuple[ReplicationSource, ...] = library_sources()
    kind = arguments.pop("sources", None)
    if kind == "duplicate":
        sources = (*sources, sources[0])
    elif kind == "unknown-rule":
        sources = (RecordTreeSource("odd", frozenset({"no-such-rule"}), ()),)
    elif kind == "mac-only-rule":
        sources = (RecordTreeSource("odd", frozenset({"writer-lock"}), ()),)
    elif kind in ("referent-first", "referent-of-an-unwanted-class"):
        # The order is checked whether or not the owners copy a wanted class.
        sources = (RawPackageSource(), ReferenceSource())
        if kind == "referent-of-an-unwanted-class":
            arguments["classes"] = ("records",)
    elif kind == "names-itself":
        consents_rule = frozenset({"consents"})
        sources = (RecordTreeSource("odd", consents_rule, (), referents=frozenset({"odd"})),)
    sink = RecordingSink()
    with pytest.raises(ValueError, match=problem):
        collect_pass(layout, sources, sink, **({"classes": ALL_CLASSES} | arguments))
    assert sink.reads == []


def test_an_unsafe_root_is_refused(tmp_path: Path) -> None:
    layout = consents(tmp_path)
    layout.root.chmod(0o755)
    with pytest.raises(SecurityError):
        run_pass(layout)


def test_the_result_names_every_owner(libraries: Libraries) -> None:
    result, _ = run_pass(libraries.native.layout, classes=("derived-edits",))
    assert [owner.owner for owner in result.owners] == ["derived-edits"]
    assert result.owner("derived-edits").entries
    with pytest.raises(KeyError):
        result.owner("recording-takes")
    assert source_classes(TakeStoreSource()) == {"records", "recordings"}
    # Every copied rule has one source: the library's own, or the memos drop's (which needs its
    # root, so library_sources() leaves it out).
    sources = (*library_sources(), MemosSource(Path("/data/voice-lab/memos/phone")))
    assert {name for source in sources for name in source.rules} == {
        rule.name for rule in CLASSIFICATION_RULES if rule.copied
    }
    served = [name for source in sources for name in source.rules]
    assert len(served) == len(set(served))


def test_every_owner_is_read_before_the_owners_its_files_name() -> None:
    sources = library_sources()
    names = [source.name for source in sources]
    for index, source in enumerate(sources):
        assert source.referents <= set(names[index + 1 :]), source.name
    # The orders the plan's references need (referrer before referent).
    for referrer, referent in (
        ("recording-analysis", "recording-takes"),
        ("recording-packages", "recording-takes"),
        ("capture-sessions", "recording-packages"),
        ("recording-edits", "recording-takes"),
        ("legacy-continuations", "capture-sessions"),
        ("legacy-continuations", "recording-takes"),
        ("recording-takes", "recording-groups"),
        ("recording-groups", "prompt-database"),
        ("fine-tune-job-records", "fine-tune-runs"),
        ("transfer-plans", "datasets"),
        ("datasets", "lab-approvals"),
        ("references", "lab-approvals"),
        ("lab-approvals", "raw-packages"),
        ("raw-packages", "recording-packages"),
        ("recording-edits", "raw-packages"),
    ):
        assert names.index(referrer) < names.index(referent), (referrer, referent)
        assert referrer in referrers_of(referent), (referrer, referent)
    assert referrers_of("raw-packages") >= {"datasets", "references", "lab-approvals"}
    # Raw sessions resolve native takes through package maps, so they fall back with takes.
    assert referrers_of("recording-takes") >= {"raw-packages"} | referrers_of("raw-packages")


def test_records_are_read_in_referrer_order(libraries: Libraries) -> None:
    result, _ = run_pass(libraries.native.layout, classes=("records",))
    packages = [entry.path for entry in result.owner("recording-packages").entries]
    kinds = [
        path.split("/")[2] if path.startswith("provenance/recording-packages/") else "origin"
        for path in packages
    ]
    assert kinds == ["sessions", "operations", "plans", "jobs", "work", "origin"]
    legacy, _ = run_pass(libraries.legacy, classes=("records",))
    edits = [entry.path for entry in legacy.owner("recording-edits").entries]
    assert edits[-1] == "provenance/recording-edits/legacy/.operations/edit-1.json"


# -- walking -------------------------------------------------------------------------------


def test_the_classification_walk_counts_every_file_and_skips_mac_only_trees(
    libraries: Libraries,
) -> None:
    root = libraries.legacy.root
    walked = require_classified_tree(libraries.legacy)
    everything = tree_paths(root)
    skipped = [
        p
        for p in everything
        if any(
            subtree_stays_on_mac("/".join(p.split("/")[:depth]))
            for depth in range(1, p.count("/") + 1)
        )
    ]
    assert walked == len(everything) - len(skipped) and skipped


def test_entries_that_vanish_while_listed_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = consents(tmp_path)
    write_file(layout.root, "provenance/consents/history/c.json")
    real_scandir = os.scandir

    class Vanishing:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.name = entry.name
            self._entry = entry

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            if self.name == "b.json":
                raise FileNotFoundError(self.name)
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class Listing:
        def __init__(self, descriptor: int) -> None:
            self._iterator = real_scandir(descriptor)

        def __enter__(self) -> Iterator[Vanishing]:
            return (Vanishing(entry) for entry in self._iterator)

        def __exit__(self, *_: object) -> None:
            self._iterator.close()

    real_open = os.open

    def open_(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "history":
            raise FileNotFoundError(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(replication.os, "scandir", Listing)
    monkeypatch.setattr(replication.os, "open", open_)
    assert [path for path, _ in replication._scan(layout.root, "provenance/consents")] == [
        "provenance/consents/a.json"
    ]


def test_a_directory_that_turns_into_a_link_while_walked_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = consents(tmp_path)
    write_file(layout.root, "provenance/consents/history/c.json")
    real_open = os.open

    def open_(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "history":
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(replication.os, "open", open_)
    with pytest.raises(SourceChangedError) as caught:
        list(replication._scan(layout.root, "provenance/consents"))
    assert (caught.value.path, caught.value.problem) == ("provenance/consents/history", "unsafe")
    with pytest.raises(SourceChangedError):
        require_classified_tree(layout)


@pytest.mark.parametrize("helper", ["_scan", "_list"])
def test_scans_of_missing_or_linked_directories(tmp_path: Path, helper: str) -> None:
    layout = consents(tmp_path)
    function = getattr(replication, helper)
    assert list(function(layout.root, "provenance/nothing/here")) == []
    assert list(function(layout.root, "provenance/nothing")) == []
    os.symlink(layout.root / "provenance/consents", layout.root / "provenance/linked")
    with pytest.raises(SourceChangedError):
        list(function(layout.root, "provenance/linked/a.json"))
    if helper == "_list":
        with pytest.raises(SourceChangedError):
            function(layout.root, "provenance/linked")
        with pytest.raises(SourceChangedError):
            function(layout.root, "provenance/consents/a.json")
    else:
        assert [p for p, _ in function(layout.root, "provenance/linked")] == ["provenance/linked"]
        assert [p for p, _ in function(layout.root, "provenance/consents/a.json")] == [
            "provenance/consents/a.json"
        ]


def test_a_scan_of_a_directory_that_vanishes_after_its_stat_yields_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = consents(tmp_path)
    real_open = os.open

    def open_(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if (
            path == "consents"
            and "dir_fd" in kwargs
            and flags & os.O_DIRECTORY
            and flags & os.O_NOFOLLOW
        ):
            raise FileNotFoundError(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(replication.os, "open", open_)
    assert list(replication._scan(layout.root, "provenance/consents")) == []
    assert replication._list(layout.root, "provenance/consents") == []


def test_a_scan_of_a_path_that_vanishes_before_its_stat_yields_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = consents(tmp_path)
    real_stat = os.stat

    def stat_(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == "consents":
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(replication.os, "stat", stat_)
    assert list(replication._scan(layout.root, "provenance/consents")) == []

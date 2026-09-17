"""Owned background library discovery; checkpoints certify cache freshness only.

@canonical recording_discovery -- legacy integrity work runs in one child process,
never on the metadata executor. Separate fingerprints prevent native churn from
invalidating the legacy checkpoint. Accepted raw authority remains unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    write_canonical_json,
)
from .catalog_ports import CorpusSnapshotSecurityError
from .corpus_store import resolve_group_catalog
from .errors import ContractError, SecurityError
from .models import ContractModel, Sha256, StableId
from .paths import AssetLayout, confined_path, ensure_private_directory
from .recording_contracts import CatalogSnapshot, RecordingError, RecordingGroup, RecordingTake
from .recording_discovery_contracts import (
    DiscoveryAck,
    DiscoveryAlias,
    DiscoveryBegin,
    DiscoveryEnvelope,
    DiscoveryFinished,
    DiscoveryInvalid,
    DiscoveryLegacy,
    DiscoveryNative,
    DiscoveryNativeInventory,
    DiscoveryPublication,
    DiscoveryRequest,
    DiscoverySpoolRef,
    DiscoveryWorkerStart,
)
from .recording_discovery_spool import (
    append_spool,
    iter_spool,
    read_frame,
    spool_records,
    validate_spool,
    verify_spool,
    write_frame,
)
from .recording_index import RecordingIndex
from .recording_index_backfill import legacy_projection_records, publish_legacy_spool
from .recording_library import RecordingLibrary
from .recording_takes import (
    GROUP_PROVENANCE_ROOT,
    RecordingMetadataBusyError,
    RecordingTakeStore,
    read_recording_evidence,
)

MAX_DISCOVERY_ENTRIES = 200_000
MAX_DISCOVERY_QUEUE = 8
DISCOVERY_RETRY_SECONDS = 30.0
DISCOVERY_METADATA_RETRY_SECONDS = 0.5
DISCOVERY_METADATA_RETRY_INTERVAL = 0.01
DISCOVERY_DEFERRED_RETRY_SECONDS = 0.1
DISCOVERY_DEFERRED_EXIT_CODE = 75


class _DiscoveryCancelled(Exception):
    """The owner stopped; preserve current metadata without recording a source failure."""


class RecordingDiscoveryCheckpoint(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-discovery-checkpoint/v1"] = (
        "cole-voice-lab.recording-discovery-checkpoint/v1"
    )
    subject_id: StableId
    catalog_sha256: Sha256
    index_instance: StableId
    legacy_fingerprint: Sha256
    native_fingerprint: Sha256 | None = None


def _entries(root: Path, *, recursive: bool = True) -> Iterator[Path]:
    if not root.exists():
        if root.is_symlink():
            raise SecurityError("discovery source cannot be a symbolic link")
        return
    yield root
    for child in sorted(root.iterdir()):
        # v1 edits are direct files. Native v2 recipes/derivatives use a separate
        # native/ subtree, so they cannot invalidate legacy audio verification.
        if child.name == "native":
            continue
        yield child
        if recursive and child.is_dir() and not child.is_symlink():
            yield from _descendants(child)


def _descendants(directory: Path) -> Iterator[Path]:
    for child in sorted(directory.iterdir()):
        yield child
        if child.is_dir() and not child.is_symlink():
            yield from _descendants(child)


def _fingerprint(layout: AssetLayout, *, legacy: bool) -> str:
    digest = hashlib.sha256()
    paths: list[Path] = []
    if legacy:
        for relative in (
            "provenance/capture-desktop",
            "provenance/recording-edits",
            "derived/recording-edits",
        ):
            paths.append(confined_path(layout.root, relative))
        paths.extend(sorted(layout.directory("raw").iterdir()))
        paths.extend(
            sorted(
                path
                for path in layout.directory("tmp").iterdir()
                if path.name.startswith("capture-")
            )
        )
    else:
        provenance = confined_path(layout.root, "provenance/recording-takes")
        if provenance.exists():
            paths.extend(
                sorted(path for path in provenance.iterdir() if path.name.startswith("take-"))
            )
        originals = confined_path(layout.root, "tmp/recording-takes")
        if originals.exists():
            paths.extend(sorted(path / "original.wav" for path in originals.iterdir()))
    count = 0
    for root in paths:
        if not root.exists() and not root.is_symlink():
            digest.update(canonical_json_bytes([root.relative_to(layout.root).as_posix(), None]))
            continue
        iterator = _entries(root) if root.is_dir() else iter((root,))
        for path in iterator:
            count += 1
            if count > MAX_DISCOVERY_ENTRIES:
                raise ContractError("recording discovery exceeds its bounded source count")
            confined_path(layout.root, path.relative_to(layout.root).as_posix())
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not (
                stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
            ):
                raise SecurityError("discovery source must be a real regular file or directory")
            if metadata.st_mode & 0o077 or (
                stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1
            ):
                raise SecurityError("discovery source must remain private and unaliased")
            identity = [metadata.st_dev, metadata.st_ino, metadata.st_mode]
            # Directory child names are enumerated; root timestamp changes from
            # excluded native edits cannot invalidate unrelated legacy evidence.
            if not stat.S_ISDIR(metadata.st_mode):
                identity.extend([metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns])
            digest.update(
                canonical_json_bytes([path.relative_to(layout.root).as_posix(), identity])
            )
    return digest.hexdigest()


def _retry_background_metadata[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    """Wait only in the owned background child; foreground commits remain nonblocking."""
    deadline = time.monotonic() + DISCOVERY_METADATA_RETRY_SECONDS
    while True:
        try:
            return operation()
        except RecordingMetadataBusyError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(DISCOVERY_METADATA_RETRY_INTERVAL, remaining))


def _read_group(layout: AssetLayout, group_id: str) -> tuple[RecordingGroup, CatalogSnapshot]:
    """Read one group record and the exact catalog it binds, from files alone.

    Bootstraps the projection before the repairing store exists: immutable group evidence
    and content-addressed snapshots are read without the metadata journal or the corpus
    database, so the read-only worker process can call this too.
    Raises: ``SecurityError`` for a non-canonical record or one naming another group;
    ``ContractError`` for an invalid record; ``CorpusSnapshotSecurityError``
    (``CORPUS.SNAPSHOT_MISSING`` / ``CORPUS.SNAPSHOT_MISMATCH``) when a recording-group/v2's
    snapshot is missing or altered; ``OSError`` when a file cannot be read.
    """
    identifier: str = TypeAdapter(StableId).validate_python(group_id)
    group = read_recording_evidence(
        confined_path(layout.root, f"{GROUP_PROVENANCE_ROOT}/{identifier}.json"), RecordingGroup
    )
    if group.group_id != identifier:
        raise SecurityError("recording group filename does not match its identity")
    return group, resolve_group_catalog(layout, group)


def _take_pages(store: RecordingTakeStore, subject_id: str) -> Iterator[RecordingTake]:
    after = None
    while page := _retry_background_metadata(
        partial(store.iter_takes_for_projection, after_take_id=after)
    ):
        for take in page:
            if take.reservation.group.subject_id == subject_id:
                yield take
        after = page[-1].reservation.take_id


def _checkpoint(path: Path) -> RecordingDiscoveryCheckpoint | None:
    if not path.exists():
        return None
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_mode & 0o077 or metadata.st_nlink != 1 or metadata.st_size > 4096:
            raise SecurityError("discovery checkpoint must be bounded private evidence")
        payload = stream.read(4097)
    checkpoint = RecordingDiscoveryCheckpoint.model_validate_json(payload)
    if payload != canonical_json_bytes(checkpoint) + b"\n":
        raise SecurityError("discovery checkpoint must remain canonical")
    return checkpoint


def _native_inventory(
    index: RecordingIndex, descriptor: int, stop: threading.Event | None = None
) -> DiscoverySpoolRef | None:
    def check_owner() -> None:
        if stop is not None and stop.is_set():
            raise _DiscoveryCancelled

    check_owner()
    first = index.native_take_versions()
    if not first:
        return None

    def records() -> Iterator[DiscoveryNativeInventory]:
        page = first
        while page:
            check_owner()
            for take_id, revision in page:
                yield DiscoveryNativeInventory(take_id=take_id, state_revision=revision)
            page = index.native_take_versions(after_take_id=page[-1][0])

    return spool_records(descriptor, records())


def produce_discovery(
    layout: AssetLayout,
    request: DiscoveryRequest,
    inventory_fd: int,
    inventory: DiscoverySpoolRef | None,
    output_fd: int,
    publish: Callable[[DiscoveryPublication], bool],
) -> DiscoveryFinished:
    """Inspect evidence without opening SQLite; publish only fully prepared typed metadata.

    The group's exact catalog (embedded, or its referenced snapshot file with prompt
    aliases) drives the legacy library scan, so legacy takes recorded against an accepted
    earlier revision are published too.
    Returns: the ``DiscoveryFinished`` summary, ``deferred`` when the metadata journal is
    busy; any other evidence, snapshot or contract failure becomes its retryable
    ``RECORDING.HISTORY_UNKNOWN`` error instead of a raise.
    """
    try:
        group, catalog = _read_group(layout, request.group_id)
        if group.subject_id != request.subject_id or group.catalog_sha256 != request.catalog_sha256:
            raise SecurityError("discovery worker group differs from its parent's exact context")
        legacy = _fingerprint(layout, legacy=True)
        native = _fingerprint(layout, legacy=False)
        legacy_current = request.legacy_fingerprint == legacy
        native_current = request.native_fingerprint == native
        if legacy_current and native_current:
            return DiscoveryFinished(legacy_fingerprint=legacy, native_fingerprint=native)

        def send(publication: DiscoveryPublication) -> None:
            if not publish(publication):
                raise RecordingMetadataBusyError("discovery publication became stale")

        send(DiscoveryBegin())
        store = RecordingTakeStore(layout, read_only=True)

        def publish_take(take: RecordingTake) -> None:
            send(DiscoveryNative(source=spool_records(output_fd, (take,))))

        if not native_current:
            for take in _take_pages(store, group.subject_id):
                publish_take(take)
            if inventory is not None:
                verify_spool(inventory_fd, inventory)
                for payload in iter_spool(inventory_fd, inventory):
                    known = DiscoveryNativeInventory.model_validate_json(payload)
                    try:
                        publish_take(
                            _retry_background_metadata(
                                partial(store.get_take_for_projection, known.take_id)
                            )
                        )
                    except RecordingMetadataBusyError:
                        raise
                    except (ContractError, SecurityError, OSError):
                        send(
                            DiscoveryInvalid(
                                take_id=known.take_id,
                                expected_state_revision=known.state_revision,
                                error=RecordingError(
                                    code="RECORDING.HISTORY_UNKNOWN",
                                    message="Indexed native evidence is missing or unreadable",
                                    retryable=False,
                                ),
                            )
                        )
        if not legacy_current:
            library = RecordingLibrary(layout, catalog, group.subject_id)

            def publish_alias(take: RecordingTake, alias: DiscoveryAlias) -> None:
                publish_take(take)
                send(alias)

            # Native package callbacks stream independently; only finished legacy metadata
            # is copied into the shared spool, so callback records cannot interleave it.
            with tempfile.TemporaryFile(mode="w+b", dir=layout.directory("tmp")) as stage:
                prepared = spool_records(
                    stage.fileno(),
                    legacy_projection_records(library, publish_alias, take_store=store),
                )
                if _fingerprint(layout, legacy=True) != legacy:
                    raise ContractError("legacy evidence changed during background discovery")
                source = append_spool(output_fd, stage.fileno(), prepared)
                send(DiscoveryLegacy(source=source, expected_token=request.legacy_token))
        return DiscoveryFinished(
            legacy_fingerprint=legacy,
            native_fingerprint=native if _fingerprint(layout, legacy=False) == native else None,
            native_reconciled=not native_current,
            legacy_backfilled=not legacy_current,
        )
    except RecordingMetadataBusyError:
        return DiscoveryFinished(deferred=True)
    except Exception:
        return DiscoveryFinished(
            error=RecordingError(
                code="RECORDING.HISTORY_UNKNOWN",
                message="Background library discovery could not verify unchanged evidence",
                retryable=True,
            )
        )


class DiscoveryPublisher:
    """Apply bounded publications through exactly the parent's existing index handle."""

    def __init__(
        self,
        index: RecordingIndex,
        request: DiscoveryRequest,
        descriptor: int,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.index, self.request, self.descriptor = index, request, descriptor
        self.progress = progress
        self.last_end = 0
        self.began = False

    def apply(self, publication: DiscoveryPublication) -> bool:
        if self.index.instance_id != self.request.index_instance:
            return False
        if isinstance(publication, DiscoveryBegin):
            if self.began:
                raise SecurityError("discovery publication repeats its beginning")
            self.index.clear_unknown(self.request.catalog_sha256, "discovery")
            page = self.index.history(self.request.catalog_sha256, page_size=1)
            self.index.set_completeness(
                self.request.catalog_sha256, "unknown" if page.error_count else "building"
            )
            self.began = True
            # The state transition is committed and its index lock is released.
            # Notify before the ACK that lets the child inspect history, so a
            # previously Complete UI can switch its Save action while work runs.
            if self.progress is not None:
                self.progress(self.request.group_id)
            return True
        if not self.began:
            raise SecurityError("discovery metadata arrived before its beginning")
        if isinstance(publication, (DiscoveryNative, DiscoveryLegacy)):
            source = publication.source
            if source.offset != self.last_end:
                raise SecurityError("discovery spool ranges must be contiguous and unreplayed")
            verify_spool(self.descriptor, source)
            self.last_end += source.size_bytes
            if isinstance(publication, DiscoveryNative):
                if source.record_count != 1:
                    raise SecurityError("native publication requires one canonical take")
                take = RecordingTake.model_validate_json(next(iter_spool(self.descriptor, source)))
                self.index.update_take(take)
            else:
                if publication.expected_token != self.request.legacy_token:
                    raise SecurityError("legacy publication token changed in transit")
                return (
                    publish_legacy_spool(
                        self.index,
                        self.request.catalog_sha256,
                        self.descriptor,
                        source,
                        self.request.legacy_token,
                    )
                    is not None
                )
        elif isinstance(publication, DiscoveryAlias):
            self.index.register_alias(
                publication.take_id, publication.path, publication.sha256, publication.alias_kind
            )
        elif isinstance(publication, DiscoveryInvalid):
            return self.index.invalidate_take_if_current(
                publication.take_id, publication.expected_state_revision, publication.error
            )
        else:
            raise ContractError("discovery completion is not an index mutation")
        return True


def _finish_discovery(
    index: RecordingIndex,
    request: DiscoveryRequest,
    publisher: DiscoveryPublisher,
    finished: DiscoveryFinished,
    directory: Path,
    key: str,
) -> dict[str, object]:
    report: dict[str, object] = {
        "legacy_backfilled": finished.legacy_backfilled,
        "native_reconciled": finished.native_reconciled,
        "microphone_opened": False,
    }
    if finished.error is not None:
        index.mark_unknown(request.catalog_sha256, "discovery", finished.error)
    elif finished.deferred:
        if index.history(request.catalog_sha256, page_size=1).completeness != "unknown":
            index.set_completeness(request.catalog_sha256, "building")
        report["deferred"] = True
    elif publisher.began:
        page = index.history(request.catalog_sha256, page_size=1)
        if not page.error_count and finished.native_fingerprint is None:
            # A successful scan always inspects legacy evidence. Without that fingerprint
            # the completion is malformed, not changed native evidence: fail closed to
            # Unknown (as the certified path does) instead of retrying it as deferred.
            if finished.legacy_fingerprint is None:
                raise SecurityError("finished discovery requires its inspected legacy fingerprint")
            # Native evidence changed during this scan (normally an overlapping Save).
            # Only a scan over stable evidence may certify Complete; the next pass must
            # reconcile native evidence again and would demote it. Stay Building and
            # retry promptly as deferred work.
            if page.completeness == "complete":
                index.set_completeness(request.catalog_sha256, "building")
            report["deferred"] = True
            report["native_evidence_changed"] = True
            # Keep verified legacy freshness so retries skip legacy backfill and its
            # interim Complete; the null native fingerprint still forces Begin.
            write_canonical_json(
                directory / f"{key}.json",
                RecordingDiscoveryCheckpoint(
                    subject_id=request.subject_id,
                    catalog_sha256=request.catalog_sha256,
                    index_instance=request.index_instance,
                    legacy_fingerprint=finished.legacy_fingerprint,
                    native_fingerprint=None,
                ),
            )
        elif not page.error_count:
            index.set_completeness(request.catalog_sha256, "complete")
    page = index.history(request.catalog_sha256, page_size=1)
    report["completeness"] = page.completeness
    if finished.error is None and "deferred" not in report and page.completeness == "complete":
        if finished.legacy_fingerprint is None:
            raise SecurityError("completed discovery requires its inspected legacy fingerprint")
        write_canonical_json(
            directory / f"{key}.json",
            RecordingDiscoveryCheckpoint(
                subject_id=request.subject_id,
                catalog_sha256=request.catalog_sha256,
                index_instance=request.index_instance,
                legacy_fingerprint=finished.legacy_fingerprint,
                native_fingerprint=finished.native_fingerprint,
            ),
        )
    write_canonical_json(directory / f"{key}.result.json", report)
    return report


type DiscoveryRunner = Callable[
    [DiscoveryRequest, int, DiscoverySpoolRef | None, int, DiscoveryPublisher], DiscoveryFinished
]


def discover_recording_library(
    layout: AssetLayout,
    group_id: str,
    *,
    index: RecordingIndex | None = None,
    store: RecordingTakeStore | None = None,
    runner: DiscoveryRunner | None = None,
    stop: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Standalone or supervised discovery shares one evidence producer and parent publisher.

    Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) before any index write
    when the group's referenced snapshot is missing or altered; ``SecurityError`` /
    ``ContractError`` for an unreadable group record or a mismatched index;
    ``RecordingIndexError`` when the catalog cannot be registered. Failures after that
    mark the catalog Unknown and are reported in the returned summary, not raised.
    """
    group, catalog = _read_group(layout, group_id)
    directory = ensure_private_directory(
        confined_path(layout.root, "provenance/recording-library-discovery")
    )
    key = canonical_sha256([group.subject_id, group.catalog_sha256])
    with (
        RecordingIndex(layout, subject_id=group.subject_id)
        if index is None
        else nullcontext(index) as owner
    ):
        if owner.layout != layout or owner.subject_id != group.subject_id:
            raise ContractError("discovery index differs from its exact subject/root")
        owner.register_catalog(catalog)
        try:
            # Repair uses the ordinary parent store and its existing projection ordering.
            if store is None:
                store = _retry_background_metadata(
                    lambda: RecordingTakeStore(layout, projection_hook=owner.update_take)
                )
            else:
                _retry_background_metadata(store.recover)
            prior = _checkpoint(directory / f"{key}.json")
            instance = owner.instance_id
            valid = (
                prior is not None
                and prior.subject_id == group.subject_id
                and prior.catalog_sha256 == group.catalog_sha256
                and prior.index_instance == instance
            )
            request = DiscoveryRequest(
                generation=uuid4().hex,
                group_id=group_id,
                subject_id=group.subject_id,
                catalog_sha256=group.catalog_sha256,
                index_instance=instance,
                legacy_token=owner.legacy_publication_token(),
                legacy_fingerprint=prior.legacy_fingerprint
                if valid and prior is not None
                else None,
                native_fingerprint=prior.native_fingerprint
                if valid and prior is not None
                else None,
            )
            with (
                tempfile.TemporaryFile(mode="w+b", dir=layout.directory("tmp")) as inventory_file,
                tempfile.TemporaryFile(mode="w+b", dir=layout.directory("tmp")) as output,
            ):
                inventory = _native_inventory(owner, inventory_file.fileno(), stop)
                publisher = DiscoveryPublisher(owner, request, output.fileno(), progress=progress)
                finished = (
                    produce_discovery(
                        layout,
                        request,
                        inventory_file.fileno(),
                        inventory,
                        output.fileno(),
                        publisher.apply,
                    )
                    if runner is None
                    else runner(
                        request, inventory_file.fileno(), inventory, output.fileno(), publisher
                    )
                )
                return _finish_discovery(owner, request, publisher, finished, directory, key)
        except _DiscoveryCancelled:
            report: dict[str, object] = {
                "legacy_backfilled": False,
                "native_reconciled": False,
                "microphone_opened": False,
                "deferred": True,
                "completeness": owner.history(group.catalog_sha256, page_size=1).completeness,
            }
        except RecordingMetadataBusyError:
            if owner.history(group.catalog_sha256, page_size=1).completeness != "unknown":
                owner.set_completeness(group.catalog_sha256, "building")
            report = {
                "legacy_backfilled": False,
                "native_reconciled": False,
                "microphone_opened": False,
                "deferred": True,
                "completeness": owner.history(group.catalog_sha256, page_size=1).completeness,
            }
        except Exception:
            owner.mark_unknown(
                group.catalog_sha256,
                "discovery",
                RecordingError(
                    code="RECORDING.HISTORY_UNKNOWN",
                    message="Background library discovery could not verify unchanged evidence",
                    retryable=True,
                ),
            )
            report = {
                "legacy_backfilled": False,
                "native_reconciled": False,
                "microphone_opened": False,
                "completeness": "unknown",
            }
        write_canonical_json(directory / f"{key}.result.json", report)
        return report


class RecordingDiscoveryCoordinator:
    """One bounded child process; daemon supervision never performs legacy audio work."""

    def __init__(
        self,
        layout: AssetLayout,
        *,
        completed: Callable[[str], None] | None = None,
        progress: Callable[[str], None] | None = None,
        index: RecordingIndex | None = None,
        store: RecordingTakeStore | None = None,
    ) -> None:
        self.layout = layout
        self.index, self.store = index, store
        self.completed = completed
        self.progress = progress
        self._queue: queue.Queue[str] = queue.Queue(MAX_DISCOVERY_QUEUE)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._scheduled: dict[str, float] = {}
        self._pending: set[str] = set()
        self._process: subprocess.Popen[bytes] | None = None
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="recording-discovery-supervisor"
        )
        self._worker.start()

    def schedule(self, group_id: str) -> None:
        with self._lock:
            previous = self._scheduled.get(group_id)
            if (
                self._stop.is_set()
                or len(self._pending) >= MAX_DISCOVERY_QUEUE
                or group_id in self._pending
                or (previous is not None and time.monotonic() - previous < DISCOVERY_RETRY_SECONDS)
            ):
                return
            now = time.monotonic()
            self._scheduled = {
                key: value
                for key, value in self._scheduled.items()
                if now - value < DISCOVERY_RETRY_SECONDS
            }
            self._scheduled.pop(group_id, None)
            self._scheduled[group_id] = now
            while len(self._scheduled) > MAX_DISCOVERY_QUEUE:
                del self._scheduled[next(iter(self._scheduled))]
            self._pending.add(group_id)
            self._queue.put_nowait(group_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                group = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            deferred = False
            try:
                report = discover_recording_library(
                    self.layout,
                    group,
                    index=self.index,
                    store=self.store,
                    runner=self._run_worker,
                    stop=self._stop,
                    progress=self.progress,
                )
                deferred = report.get("deferred") is True
            except (OSError, CorpusSnapshotSecurityError):
                # No false Complete checkpoint is written. Notify the service so
                # the unchanged Building/Unknown state remains visible and retryable.
                # A missing or altered snapshot fails before any index write; the
                # service's own group read reports it as CORPUS.SNAPSHOT_MISMATCH.
                pass
            finally:
                if deferred:
                    self._stop.wait(DISCOVERY_DEFERRED_RETRY_SECONDS)
                with self._lock:
                    self._process = None
                    if deferred and not self._stop.is_set():
                        # Active plus queued work is capped, so the current group
                        # always retains one slot. Other contexts run first.
                        self._queue.put_nowait(group)
                    else:
                        self._pending.discard(group)
                self._queue.task_done()
            if self.completed is not None and not self._stop.is_set():
                self.completed(group)

    def _run_worker(
        self,
        request: DiscoveryRequest,
        inventory_fd: int,
        inventory: DiscoverySpoolRef | None,
        output_fd: int,
        publisher: DiscoveryPublisher,
    ) -> DiscoveryFinished:
        parent_read, parent_write = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        try:
            with self._lock:
                if self._stop.is_set():
                    raise _DiscoveryCancelled
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "cole_voice_lab.recording_discovery",
                        "--asset-root",
                        str(self.layout.root),
                        "--group-id",
                        request.group_id,
                        "--inventory-fd",
                        str(inventory_fd),
                        "--spool-fd",
                        str(output_fd),
                        "--parent-fd",
                        str(parent_read),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(inventory_fd, output_fd, parent_read),
                )
                self._process = process
            os.close(parent_read)
            parent_read = -1
            assert process.stdin is not None and process.stdout is not None
            write_frame(
                process.stdin.fileno(), DiscoveryWorkerStart(request=request, inventory=inventory)
            )
            sequence = 0
            while not self._stop.is_set():
                envelope = DiscoveryEnvelope.model_validate_json(
                    read_frame(process.stdout.fileno(), self._stop)
                )
                sequence += 1
                if envelope.generation != request.generation or envelope.sequence != sequence:
                    raise SecurityError("discovery publication generation or sequence changed")
                publication = envelope.publication
                if self._stop.is_set():
                    raise _DiscoveryCancelled
                applied = (
                    True
                    if isinstance(publication, DiscoveryFinished)
                    else publisher.apply(publication)
                )
                # Publication has returned and released the index transaction before ACK.
                write_frame(
                    process.stdin.fileno(),
                    DiscoveryAck(generation=request.generation, sequence=sequence, applied=applied),
                )
                if isinstance(publication, DiscoveryFinished):
                    return publication
            raise _DiscoveryCancelled
        except EOFError:
            if self._stop.is_set():
                raise _DiscoveryCancelled from None
            raise
        finally:
            if parent_read >= 0:
                os.close(parent_read)
            os.close(parent_write)
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.5)

    def close(self) -> None:
        self._stop.set()
        self._worker.join(1.5)
        if self._worker.is_alive():
            raise RuntimeError("discovery supervisor did not stop before index owner shutdown")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--inventory-fd", type=int)
    parser.add_argument("--spool-fd", type=int)
    parser.add_argument("--parent-fd", type=int)
    options = parser.parse_args()
    if not stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode):
        raise ValueError("discovery requires its owning service's inherited pipe")
    if options.parent_fd is not None:

        def watch_parent() -> None:
            while os.read(options.parent_fd, 1):
                pass
            os._exit(0)  # Only the live parent may finish publication/checkpoints.

        threading.Thread(
            target=watch_parent, daemon=True, name="recording-discovery-parent"
        ).start()
    try:
        start = DiscoveryWorkerStart.model_validate_json(read_frame(sys.stdin.fileno()))
    except EOFError:
        return 0
    if options.inventory_fd is None or options.spool_fd is None or options.parent_fd is None:
        raise ValueError("discovery worker requires its parent's owned spool descriptors")
    if start.request.group_id != options.group_id:
        raise SecurityError("discovery worker group differs from its command")
    validate_spool(options.inventory_fd)
    validate_spool(options.spool_fd)
    sequence = 0

    def publish(publication: DiscoveryPublication) -> bool:
        nonlocal sequence
        sequence += 1
        write_frame(
            sys.stdout.fileno(),
            DiscoveryEnvelope(
                generation=start.request.generation, sequence=sequence, publication=publication
            ),
        )
        ack = DiscoveryAck.model_validate_json(read_frame(sys.stdin.fileno()))
        if ack.generation != start.request.generation or ack.sequence != sequence:
            raise SecurityError("discovery acknowledgment changed its publication identity")
        return ack.applied

    finished = produce_discovery(
        AssetLayout.validate(options.asset_root),
        start.request,
        options.inventory_fd,
        start.inventory,
        options.spool_fd,
        publish,
    )
    publish(finished)
    return DISCOVERY_DEFERRED_EXIT_CODE if finished.deferred else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Durable bounded package preparation queue and owned, isolated copy/verification process."""

from __future__ import annotations

import argparse
import fcntl
import heapq
import json
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .canonical import canonical_sha256
from .errors import ContractError, SecurityError, VoiceLabError
from .models import ContractModel, Sha256, StableId, utc_now
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    MAX_CONTROL_FRAME_BYTES,
    PackageEvent,
    PackagePreparationEvent,
    PreparePackageCommand,
    RecordingBuildIdentity,
    RecordingError,
    RecordingGroup,
    RecordingPackageMap,
    UtcTimestamp,
    recording_frame_bytes,
)
from .recording_packages import (
    RecordingPackageCommit,
    RecordingPackageError,
    RecordingPackageStore,
    _original_identity,
    load_recording_package_map,
)
from .recording_takes import RecordingMetadataBusyError, RecordingTakeStore, read_recording_evidence

MAX_PACKAGE_BACKLOG = 16
MAX_PACKAGE_ATTEMPTS = 3
PACKAGE_WORKER_SECONDS = 300.0


class RecordingPackageJob(ContractModel):
    operation_id: StableId
    group_id: StableId
    request_sha256: Sha256
    queued_at: UtcTimestamp
    state: Literal["queued", "running", "prepared", "blocked", "failed"] = "queued"
    attempt: int = Field(default=0, ge=0, le=MAX_PACKAGE_ATTEMPTS)
    manifest_sha256: Sha256 | None = None
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _result_binding(self) -> RecordingPackageJob:
        if (self.state == "prepared") != (self.manifest_sha256 is not None):
            raise ValueError("prepared job requires its committed manifest digest")
        if (self.state in {"blocked", "failed"}) != (self.error is not None):
            raise ValueError("blocked/failed job requires a named error")
        if self.state == "running" and self.attempt == 0:
            raise ValueError("running job requires its durable attempt")
        return self


class RecordingPackageQueueHead(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-queue/v1"] = (
        "cole-voice-lab.recording-package-queue/v1"
    )
    jobs: tuple[RecordingPackageJob, ...] = Field(default=(), max_length=MAX_PACKAGE_BACKLOG)

    @model_validator(mode="after")
    def _unique_jobs(self) -> RecordingPackageQueueHead:
        if len({job.operation_id for job in self.jobs}) != len(self.jobs):
            raise ValueError("package queue cannot duplicate an operation")
        if any(job.state not in {"queued", "running"} for job in self.jobs):
            raise ValueError("active package queue contains a terminal job")
        return self


class RecordingPackageIntent(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-intent/v1"] = (
        "cole-voice-lab.recording-package-intent/v1"
    )
    command: PreparePackageCommand
    request_sha256: Sha256
    analyzer_build: RecordingBuildIdentity
    queued_at: UtcTimestamp

    @model_validator(mode="after")
    def _command_binding(self) -> RecordingPackageIntent:
        if self.request_sha256 != canonical_sha256(self.command):
            raise ValueError("package intent must bind its exact explicit command")
        return self


class PackageWorkerRequest(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-worker/v1"] = (
        "cole-voice-lab.recording-package-worker/v1"
    )
    kind: Literal["prepare"] = "prepare"
    generation: StableId
    intent: RecordingPackageIntent
    attempt: int = Field(ge=1, le=MAX_PACKAGE_ATTEMPTS)


class PackageWorkerReply(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-worker/v1"] = (
        "cole-voice-lab.recording-package-worker/v1"
    )
    kind: Literal["result"] = "result"
    generation: StableId
    operation_id: StableId
    request_sha256: Sha256
    attempt: int = Field(ge=1, le=MAX_PACKAGE_ATTEMPTS)
    build: RecordingBuildIdentity
    package: RecordingPackageMap | None = None
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _one_result(self) -> PackageWorkerReply:
        if (self.package is None) == (self.error is None):
            raise ValueError("package worker returns exactly one result or named error")
        return self


type PackageWorkerMessage = Annotated[
    PackageWorkerRequest | PackageWorkerReply, Field(discriminator="kind")
]
_PACKAGE_MESSAGE: TypeAdapter[PackageWorkerMessage] = TypeAdapter(PackageWorkerMessage)


def decode_package_message(payload: bytes) -> PackageWorkerMessage:
    if len(payload) > MAX_CONTROL_FRAME_BYTES:
        raise RecordingPackageError("package worker message exceeds its bounded frame")
    return _PACKAGE_MESSAGE.validate_json(payload)


type PackageJobEvent = PackageEvent | PackagePreparationEvent


class RecordingPackageQueue:
    """Sixteen active metadata jobs in one durable head; unbounded history remains paged on disk."""

    def __init__(self, packages: RecordingPackageStore) -> None:
        self.packages = packages
        self.store = packages.takes
        self.wake = threading.Event()
        self._scanned_empty = False
        self._mutex = threading.RLock()
        self._publishing: set[str] = set()
        self.directory = self.store._directory("provenance/recording-packages/jobs")
        self.store._directory("provenance/recording-packages/jobs/terminal")
        self.store._directory("provenance/recording-packages/jobs/requests")
        self._owner = os.open(
            self.directory / ".owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            _original_identity("package-queue-owner", os.fstat(self._owner))
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self._owner)
            raise
        self._closed = False
        try:
            with self._lock():
                if not self._head_path.exists():
                    self.store._write(self._head_path, RecordingPackageQueueHead())
                self._head()
        except BaseException:
            self.close()
            raise

    @property
    def _head_path(self) -> Path:
        return self.directory / "head.json"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self._mutex:
            if self._closed:
                raise RecordingPackageError("package queue is closed")
            yield

    def _head(self) -> RecordingPackageQueueHead:
        return self.store._read(self._head_path, RecordingPackageQueueHead)

    def _terminal_path(self, operation_id: str) -> Path:
        identifier: str = TypeAdapter(StableId).validate_python(operation_id)
        return self.directory / "terminal" / f"{identifier}.json"

    def _terminal(self, operation_id: str) -> RecordingPackageJob:
        job = self.store._read(self._terminal_path(operation_id), RecordingPackageJob)
        if job.operation_id != operation_id or job.state not in {"prepared", "blocked", "failed"}:
            raise SecurityError("terminal package evidence has an invalid operation/state")
        return job

    def _intent_path(self, operation_id: str) -> Path:
        identifier: str = TypeAdapter(StableId).validate_python(operation_id)
        return self.directory / "requests" / f"{identifier}.json"

    def _intent(self, command: PreparePackageCommand) -> RecordingPackageIntent:
        path = self._intent_path(command.operation_id)
        if path.exists():
            intent = self.store._read(path, RecordingPackageIntent)
            if intent.command != command:
                raise RecordingPackageError("package operation ID already names another selection")
            return intent
        intent = RecordingPackageIntent(
            command=command,
            request_sha256=canonical_sha256(command),
            analyzer_build=self.packages.analyzer_build,
            queued_at=utc_now(),
        )
        self.store._write_immutable(path, intent)
        return intent

    def _replace(self, job: RecordingPackageJob) -> None:
        head = self._head()
        if job.state in {"prepared", "blocked", "failed"}:
            # Terminal evidence sync precedes removing the active pointer. Recovery
            # repairs this boundary without copying or authorizing audio in the parent.
            self.store._write_immutable(self._terminal_path(job.operation_id), job)
            jobs = tuple(item for item in head.jobs if item.operation_id != job.operation_id)
        else:
            jobs = tuple(
                job if item.operation_id == job.operation_id else item for item in head.jobs
            )
        self.store._write(self._head_path, RecordingPackageQueueHead(jobs=jobs))
        self.wake.set()

    def _event(self, job: RecordingPackageJob) -> PackageJobEvent:
        if job.state == "prepared":
            committed = self.store._read(
                self.packages._operation_path(job.operation_id), RecordingPackageCommit
            )
            package = load_recording_package_map(self.store.layout, committed.plan.session_id)
            if package is None or package.manifest_sha256 != job.manifest_sha256:
                raise SecurityError("prepared package job lost its committed map")
            return PackageEvent(package=package)
        return PackagePreparationEvent(
            operation_id=job.operation_id, group_id=job.group_id, state=job.state, error=job.error
        )

    def submit(self, command: PreparePackageCommand) -> PackageJobEvent:
        """Bounded metadata only; no original PCM is read or copied on this service call."""
        request_sha = canonical_sha256(command)
        with self._lock():
            terminal = self._terminal_path(command.operation_id)
            if terminal.exists():
                job = self._terminal(command.operation_id)
                if job.operation_id != command.operation_id or job.request_sha256 != request_sha:
                    raise SecurityError("package job operation was rebound")
                return self._event(job)
            head = self._head()
            existing = next(
                (job for job in head.jobs if job.operation_id == command.operation_id), None
            )
            if existing is not None:
                if existing.request_sha256 != request_sha:
                    raise SecurityError("active package request changed")
                return self._event(existing)
            if len(head.jobs) >= MAX_PACKAGE_BACKLOG:
                raise RecordingPackageError(
                    "package queue is full; wait for preparation to finish", retryable=True
                )
            intent = self._intent(command)
            job = RecordingPackageJob(
                operation_id=command.operation_id,
                group_id=command.group_id,
                request_sha256=intent.request_sha256,
                queued_at=intent.queued_at,
            )
            self.store._write(self._head_path, RecordingPackageQueueHead(jobs=(*head.jobs, job)))
            self._scanned_empty = False
            self.wake.set()
            return self._event(job)

    def recover(self) -> None:
        with self._lock():
            for job in self._head().jobs:
                terminal = self._terminal_path(job.operation_id)
                if terminal.exists():
                    completed = self._terminal(job.operation_id)
                    if completed.request_sha256 != job.request_sha256:
                        raise SecurityError("terminal package operation changed")
                    self._replace(completed)
                elif job.state == "running":
                    self.worker_failed(job)

    def reconcile_requests(self) -> None:
        """Recover synced Prepare intent interrupted before its queue pointer was published.

        This bounded metadata discovery runs on the supervisor, never interactive
        Save/Reserve. Immutable requests and terminal results rebuild the active cache.
        """
        with self._lock():
            head = self._head()
            known = {job.operation_id for job in head.jobs}
            available = MAX_PACKAGE_BACKLOG - len(head.jobs)
            if not available:
                return
            with os.scandir(self.directory / "requests") as candidates:
                paths = heapq.nsmallest(
                    available,
                    (
                        Path(candidate.path)
                        for candidate in candidates
                        if candidate.name.endswith(".json")
                        and candidate.name[:-5] not in known
                        and not self._terminal_path(candidate.name[:-5]).exists()
                    ),
                )
            jobs = list(head.jobs)
            for path in paths:
                intent = self.store._read(path, RecordingPackageIntent)
                if intent.command.operation_id != path.stem:
                    raise SecurityError("package intent filename changed its operation")
                jobs.append(
                    RecordingPackageJob(
                        operation_id=intent.command.operation_id,
                        group_id=intent.command.group_id,
                        request_sha256=intent.request_sha256,
                        queued_at=intent.queued_at,
                    )
                )
            if paths:
                self.store._write(self._head_path, RecordingPackageQueueHead(jobs=tuple(jobs)))
            self._scanned_empty = not paths

    def claim(self) -> tuple[RecordingPackageJob, RecordingPackageIntent] | None:
        with self._lock():
            if not self._head().jobs and not self._scanned_empty:
                self.reconcile_requests()
            jobs = self._head().jobs
            if any(job.state == "running" for job in jobs):
                return None
            if not jobs:
                self.wake.clear()
                return None
            job = min(jobs, key=lambda value: (value.queued_at, value.operation_id))
            intent = self.store._read(self._intent_path(job.operation_id), RecordingPackageIntent)
            if (
                intent.request_sha256 != job.request_sha256
                or intent.command.group_id != job.group_id
            ):
                raise SecurityError("queued package plan binding changed")
            job = job.model_copy(update={"state": "running", "attempt": job.attempt + 1})
            self._replace(job)
            return job, intent

    def _completion_evidence(
        self, job: RecordingPackageJob, reply: PackageWorkerReply
    ) -> tuple[RecordingPackageIntent, RecordingPackageCommit | None, RecordingPackageMap | None]:
        intent = self.store._read(self._intent_path(job.operation_id), RecordingPackageIntent)
        if (
            reply.build != intent.analyzer_build
            or intent.request_sha256 != job.request_sha256
            or reply.operation_id != job.operation_id
            or reply.request_sha256 != job.request_sha256
            or reply.attempt != job.attempt
        ):
            raise SecurityError("package result is stale or bound to another job")
        if reply.package is None:
            return intent, None, None
        committed = self.store._read(
            self.packages._operation_path(job.operation_id), RecordingPackageCommit
        )
        package = load_recording_package_map(self.store.layout, committed.plan.session_id)
        if (
            package != reply.package
            or committed.plan.request_sha256 != job.request_sha256
            or committed.plan.command != intent.command
            or committed.plan.analyzer_build != intent.analyzer_build
        ):
            raise SecurityError("package worker result differs from committed registration")
        return intent, committed, package

    def complete(
        self,
        job: RecordingPackageJob,
        reply: PackageWorkerReply,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> PackageJobEvent:
        """Publish one owned result without putting canonical history behind the queue lock.

        Repeated concurrent completion defers to the existing publisher. Cancellation
        leaves the running job durable; per-entry operations safely replay on restart.
        """
        with self._lock():
            current = next(
                (item for item in self._head().jobs if item.operation_id == job.operation_id), None
            )
            if current != job:
                raise SecurityError("package result is stale or bound to another job")
            if job.operation_id in self._publishing:
                raise RecordingMetadataBusyError("this package result is already being published")
            self._publishing.add(job.operation_id)
        try:
            evidence = self._completion_evidence(job, reply)
            package = evidence[2]
            if package is not None:
                self.packages._project(package, cancelled=cancelled)
            if self._completion_evidence(job, reply) != evidence:
                raise SecurityError("package publication evidence changed before acknowledgment")
            if cancelled():
                raise RecordingPackageError("package publication was stopped", retryable=True)
            with self._lock():
                current = next(
                    (item for item in self._head().jobs if item.operation_id == job.operation_id),
                    None,
                )
                if current != job:
                    raise SecurityError("package result became stale during publication")
                completed = job.model_copy(
                    update={"state": "prepared", "manifest_sha256": package.manifest_sha256}
                    if package is not None
                    else {"state": "blocked", "error": reply.error}
                )
                self._replace(completed)
            return PackageEvent(package=package) if package is not None else self._event(completed)
        finally:
            with self._mutex:
                self._publishing.discard(job.operation_id)

    def worker_failed(self, job: RecordingPackageJob) -> PackageJobEvent:
        with self._lock():
            current = next(
                (item for item in self._head().jobs if item.operation_id == job.operation_id), None
            )
            if current != job:
                raise SecurityError("failed package attempt is stale")
            failed = job.attempt >= MAX_PACKAGE_ATTEMPTS
            job = job.model_copy(
                update={
                    "state": "failed" if failed else "queued",
                    "error": RecordingError(
                        code="RECORDING.WRITE_FAILED",
                        message="package worker stopped before completion; retry preparation",
                        retryable=True,
                    )
                    if failed
                    else None,
                }
            )
            self._replace(job)
            return self._event(job)

    def page(
        self, *, after_operation_id: str | None = None, limit: int = 200
    ) -> tuple[RecordingPackageJob, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("package job page exceeds its bound")
        with self._lock():
            with os.scandir(self.directory / "terminal") as candidates:
                paths = heapq.nsmallest(
                    limit,
                    (
                        Path(candidate.path)
                        for candidate in candidates
                        if candidate.name.endswith(".json")
                        and (after_operation_id is None or candidate.name[:-5] > after_operation_id)
                    ),
                )
            return tuple(self._terminal(path.stem) for path in paths)

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                self._closed = True
                os.close(self._owner)


class RecordingPackageSupervisor:
    """One owned process at a time; full PCM copies, QC and hashes never share the control GIL."""

    def __init__(
        self,
        queue: RecordingPackageQueue,
        *,
        on_event: Callable[[str, PackageJobEvent], None] | None = None,
        on_failure: Callable[[RecordingError], None] | None = None,
        block_fd: int | None = None,
        deadline_seconds: float = PACKAGE_WORKER_SECONDS,
    ) -> None:
        self.queue, self.on_event, self.on_failure = queue, on_event, on_failure
        self.block_fd, self.deadline_seconds = block_fd, deadline_seconds
        self._stop = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self.fatal_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="recording-package-supervisor"
        )

    @property
    def worker_pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def start(self) -> None:
        self._thread.start()

    def _prepare(
        self, job: RecordingPackageJob, intent: RecordingPackageIntent
    ) -> PackageWorkerReply:
        from .recording_service import RecordingFrameDecoder

        generation = f"package-{uuid4().hex}"
        command = [
            sys.executable,
            "-m",
            "cole_voice_lab.recording_package_jobs",
            "--worker",
            "--asset-root",
            str(self.queue.store.layout.root),
        ]
        if self.block_fd is not None:
            command.extend(["--test-block-fd", str(self.block_fd)])
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=() if self.block_fd is None else (self.block_fd,),
        )
        self._process = process
        assert process.stdin is not None and process.stdout is not None
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        pending = memoryview(
            recording_frame_bytes(
                PackageWorkerRequest(generation=generation, intent=intent, attempt=job.attempt)
            )
        )
        decoder = RecordingFrameDecoder()
        deadline = time.monotonic() + self.deadline_seconds
        send_deadline = time.monotonic() + 2.0
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                readable, writable, _ = select.select(
                    [process.stdout], [process.stdin] if pending else [], [], 0.02
                )
                if writable:
                    pending = pending[os.write(process.stdin.fileno(), pending) :]
                if pending and time.monotonic() > send_deadline:
                    raise RecordingPackageError("package request pipe stopped consuming")
                if readable:
                    chunk = os.read(process.stdout.fileno(), 16_384)
                    if not chunk:
                        decoder.finish()
                        raise RecordingPackageError("package worker exited without its result")
                    frames = tuple(decoder.feed(chunk))
                    if len(frames) > 1:
                        raise RecordingPackageError("package worker emitted unsolicited frames")
                    if frames:
                        reply = decode_package_message(frames[0])
                        if (
                            not isinstance(reply, PackageWorkerReply)
                            or reply.generation != generation
                            or reply.build != intent.analyzer_build
                            or reply.operation_id != job.operation_id
                            or reply.request_sha256 != job.request_sha256
                            or reply.attempt != job.attempt
                        ):
                            raise RecordingPackageError("package worker result binding changed")
                        return reply
            raise RecordingPackageError("package preparation exceeded its deadline or was stopped")
        finally:
            process.stdin.close()
            if process.poll() is None:
                try:
                    process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.5)
            process.stdout.close()
            self._process = None

    def _run(self) -> None:
        try:
            self.queue.recover()
            self.queue.reconcile_requests()
            while not self._stop.is_set():
                claimed = self.queue.claim()
                if claimed is None:
                    self.queue.wake.wait(0.1)
                    continue
                job, intent = claimed
                if self.on_event is not None:
                    self.on_event(job.operation_id, self.queue._event(job))
                try:
                    reply = self._prepare(job, intent)
                except (VoiceLabError, ValidationError, OSError, ValueError):
                    if self._stop.is_set():
                        break
                    event = self.queue.worker_failed(job)
                else:
                    while not self._stop.is_set():
                        try:
                            event = self.queue.complete(job, reply, cancelled=self._stop.is_set)
                            break
                        except RecordingMetadataBusyError:
                            self._stop.wait(0.01)
                        except RecordingPackageError:
                            if not self._stop.is_set():
                                raise
                            break
                    else:
                        break
                    if self._stop.is_set():
                        break
                if self.on_event is not None:
                    self.on_event(job.operation_id, event)
        except BaseException as error:
            self.fatal_error = error
            if self.on_failure is not None:
                self.on_failure(
                    RecordingError(
                        code="RECORDING.WRITE_FAILED",
                        message="package preparation stopped; recovery is required",
                        retryable=True,
                    )
                )

    def close(self) -> None:
        self._stop.set()
        self.queue.wake.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        if self._thread.ident is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RecordingPackageError("package supervisor did not stop within its bound")


def prepare_worker_request(
    layout: AssetLayout, request: PackageWorkerRequest, *, metadata_retry_seconds: float = 10.0
) -> PackageWorkerReply:
    from .recording_analysis import analysis_build_identity

    if not 0 < metadata_retry_seconds <= 10.0:
        raise ValueError("package metadata retry window must be in (0, 10] seconds")
    build = analysis_build_identity()
    intent = request.intent
    if build != intent.analyzer_build:
        raise RecordingPackageError("package worker runtime differs from the explicit intent")
    package = None
    error = None
    try:
        persisted = read_recording_evidence(
            confined_path(
                layout.root,
                f"provenance/recording-packages/jobs/requests/{intent.command.operation_id}.json",
                must_exist=True,
            ),
            RecordingPackageIntent,
        )
        if persisted != intent:
            raise SecurityError("package worker received another durable request")
        group = read_recording_evidence(
            confined_path(
                layout.root,
                f"provenance/recording-groups/{intent.command.group_id}.json",
                must_exist=True,
            ),
            RecordingGroup,
        )
        if group.group_id != intent.command.group_id:
            raise SecurityError("package group filename changed its identity")
        deadline = time.monotonic() + metadata_retry_seconds
        while True:
            try:
                store = RecordingTakeStore(layout, read_only=True)
                packages = RecordingPackageStore(
                    layout, take_store=store, analyzer_build=build, defer_projection=True
                )
                package = packages.prepare(intent.command)
                break
            except RecordingMetadataBusyError as busy:
                if time.monotonic() >= deadline:
                    raise RecordingPackageError(
                        "recording metadata is busy; retry package preparation", retryable=True
                    ) from busy
                # Yield only this owned worker; Save/Stop never wait for package planning.
                time.sleep(0.05)
    except (ContractError, SecurityError, OSError) as failure:
        error = (
            failure.recording_error
            if isinstance(failure, RecordingPackageError)
            else RecordingError(
                code="RECORDING.SOURCE_CHANGED"
                if isinstance(failure, SecurityError)
                else "RECORDING.WRITE_FAILED",
                message="package preparation failed strict source or filesystem validation",
                retryable=isinstance(failure, OSError),
            )
        )
    return PackageWorkerReply(
        generation=request.generation,
        operation_id=intent.command.operation_id,
        request_sha256=intent.request_sha256,
        attempt=request.attempt,
        build=build,
        package=package,
        error=error,
    )


def _worker_main(layout: AssetLayout, block_fd: int | None) -> int:
    import queue

    from .recording_service import RecordingFrameDecoder

    input_fd, output_fd = sys.stdin.fileno(), sys.stdout.fileno()
    os.set_blocking(output_fd, False)
    decoder = RecordingFrameDecoder()
    results: queue.Queue[PackageWorkerReply | BaseException] = queue.Queue(1)
    output = bytearray()
    busy = False
    output_deadline = 0.0

    def prepare(request: PackageWorkerRequest) -> None:
        try:
            if block_fd is not None and os.read(block_fd, 1) != b"1":
                raise RecordingPackageError("synthetic package barrier closed")
            results.put_nowait(prepare_worker_request(layout, request))
        except BaseException as error:
            results.put_nowait(error)

    while True:
        if not results.empty():
            result = results.get_nowait()
            if isinstance(result, BaseException):
                raise RecordingPackageError("package worker failed unexpectedly") from result
            output = bytearray(recording_frame_bytes(result))
            output_deadline = time.monotonic() + 2
        readable, writable, _ = select.select([input_fd], [output_fd] if output else [], [], 0.02)
        if readable:
            chunk = os.read(input_fd, 16_384)
            if not chunk:
                decoder.finish()
                return 0
            for payload in decoder.feed(chunk):
                request = decode_package_message(payload)
                if not isinstance(request, PackageWorkerRequest) or busy:
                    raise RecordingPackageError("package worker accepts exactly one request")
                busy = True
                threading.Thread(
                    target=prepare, args=(request,), daemon=True, name="package-copy-and-verify"
                ).start()
        if writable:
            del output[: os.write(output_fd, output)]
            if not output:
                return 0
        if output and time.monotonic() > output_deadline:
            raise RecordingPackageError("package result pipe stopped consuming")


RECORDING_PACKAGE_JOB_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "recording-package-intent": RecordingPackageIntent,
    "recording-package-job": RecordingPackageJob,
    "recording-package-queue": RecordingPackageQueueHead,
    "recording-package-worker-request": PackageWorkerRequest,
    "recording-package-worker-reply": PackageWorkerReply,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--test-block-fd", type=int)
    options = parser.parse_args(argv)
    try:
        return _worker_main(AssetLayout.validate(options.asset_root), options.test_block_fd)
    except (VoiceLabError, ValidationError, OSError, ValueError):
        print(
            json.dumps(
                {
                    "service": "recording-packages",
                    "error_code": "RECORDING.WRITE_FAILED",
                    "message": "package worker stopped",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

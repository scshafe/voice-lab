"""Durable bounded recording QC queue and separately supervised analysis process.

@canonical recording_analysis -- selected-source inspection runs only in the
worker process; canonical job heads and take-operation evidence survive a
rebuildable SQLite queue. No cache result authorizes raw intake or publication.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import select
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .audio import inspect_canonical_wav
from .canonical import canonical_json_bytes, canonical_sha256, open_regular_no_follow
from .errors import ContractError, SecurityError, VoiceLabError
from .models import ContractModel, Sha256, StableId, utc_now
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    AnalysisState,
    CaptureSealReceipt,
    RecordingAnalysisResult,
    RecordingBuildIdentity,
    RecordingError,
    RecordingTake,
    Revision,
    UtcTimestamp,
    recording_frame_bytes,
)
from .recording_index import RecordingIndex
from .recording_takes import (
    AnalysisSourceChangedError,
    RecordingAnalysisPublication,
    RecordingFileIdentity,
    RecordingMetadataBusyError,
    RecordingTakeStore,
    StaleRecordingAnalysisError,
    recording_analysis_job_sha256,
)

ALGORITHM_VERSION = "canonical-pcm-qc-v1"
ANALYSIS_PROTOCOL: Literal["cole-voice-lab.recording-analysis-worker/v1"] = (
    "cole-voice-lab.recording-analysis-worker/v1"
)
MAX_ATTEMPTS = 3
NEW_JOB_BURST = 3
MAX_WORKER_FRAME_BYTES = 65_536
WORKER_START_SECONDS = 10.0
WORKER_JOB_SECONDS = 180.0
_ANALYSIS_ROOT = "provenance/recording-analysis"
type AnalysisPriority = Literal["new", "historical"]
type JobStatus = Literal[
    "pending", "running", "verified", "needs-review", "invalid", "failed", "stale"
]


class RecordingAnalysisError(ContractError):
    """A named queue/worker failure; durable pending work remains inspectable."""


class RecordingAnalysisTicket(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-analysis-job/v1"] = (
        "cole-voice-lab.recording-analysis-job/v1"
    )
    seal: CaptureSealReceipt
    analyzer_build: RecordingBuildIdentity
    algorithm_version: StableId = ALGORITHM_VERSION
    edit_revision: Revision
    context_revision: Revision
    allow_quiet: bool
    queued_at: UtcTimestamp

    @property
    def job_sha256(self) -> str:
        # Time and scheduling priority do not create duplicate PCM work.
        return recording_analysis_job_sha256(
            seal=self.seal,
            analyzer_build=self.analyzer_build,
            algorithm_version=self.algorithm_version,
            edit_revision=self.edit_revision,
            context_revision=self.context_revision,
            allow_quiet=self.allow_quiet,
        )

    def result(self, state: AnalysisState, **fields: object) -> RecordingAnalysisResult:
        return RecordingAnalysisResult.model_validate(
            {
                "take_id": self.seal.take_id,
                "source_sha256": self.seal.original_sha256,
                "algorithm_version": self.algorithm_version,
                "analyzer_build": self.analyzer_build,
                "edit_revision": self.edit_revision,
                "context_revision": self.context_revision,
                "state": state,
                "queued_at": self.queued_at,
                **fields,
            }
        )

    def matches(self, result: RecordingAnalysisResult) -> bool:
        expected = self.result("pending").model_dump(
            exclude={"state", "completed_at", "metrics", "error"}
        )
        return expected == result.model_dump(exclude={"state", "completed_at", "metrics", "error"})


class RecordingAnalysisJob(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-analysis-job-head/v1"] = (
        "cole-voice-lab.recording-analysis-job-head/v1"
    )
    ticket: RecordingAnalysisTicket
    sequence: int = Field(ge=1, le=9_223_372_036_854_775_807)
    priority: AnalysisPriority
    status: JobStatus = "pending"
    attempt: int = Field(ge=0, le=1000)
    publication: RecordingAnalysisPublication | None = None

    @model_validator(mode="after")
    def _bound_result(self) -> RecordingAnalysisJob:
        if self.publication is not None and (
            self.publication.job_sha256 != self.ticket.job_sha256
            or self.publication.attempt != self.attempt
            or not self.ticket.matches(self.publication.result)
            or (self.status != "stale" and self.publication.result.state != self.status)
        ):
            raise ValueError("job head must bind the exact attempt and result")
        return self


class AnalysisWorkerHello(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-analysis-worker/v1"] = ANALYSIS_PROTOCOL
    kind: Literal["hello"] = "hello"
    generation: StableId
    build: RecordingBuildIdentity


class AnalysisWorkerRequest(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-analysis-worker/v1"] = ANALYSIS_PROTOCOL
    kind: Literal["analyze"] = "analyze"
    generation: StableId
    job: RecordingAnalysisJob

    @model_validator(mode="after")
    def _running(self) -> AnalysisWorkerRequest:
        if self.job.status != "running" or self.job.attempt < 1:
            raise ValueError("worker requires a claimed running attempt")
        return self


class AnalysisWorkerReply(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-analysis-worker/v1"] = ANALYSIS_PROTOCOL
    kind: Literal["result"] = "result"
    generation: StableId
    publication: RecordingAnalysisPublication


type AnalysisWorkerMessage = Annotated[
    AnalysisWorkerHello | AnalysisWorkerRequest | AnalysisWorkerReply,
    Field(discriminator="kind"),
]
_WORKER_MESSAGE: TypeAdapter[AnalysisWorkerMessage] = TypeAdapter(AnalysisWorkerMessage)


def decode_analysis_message(payload: bytes) -> AnalysisWorkerMessage:
    """Same bounded canonical framing/JSON rules as recording controls, stricter worker cap."""
    from .recording_service import _invalid_constant, _unique_json_object

    if not 1 <= len(payload) <= MAX_WORKER_FRAME_BYTES:
        raise RecordingAnalysisError("analysis worker payload exceeds its bound")
    try:
        json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_invalid_constant,
        )
        return _WORKER_MESSAGE.validate_json(payload)
    except (ValueError, TypeError, RecursionError) as error:
        raise RecordingAnalysisError("invalid versioned analysis worker message") from error


def analysis_build_identity() -> RecordingBuildIdentity:
    from .recording_service import recording_service_build_identity

    return recording_service_build_identity().model_copy(
        update={"build_id": "python-recording-analysis-v1"}
    )


def _file_identity(relative: str, metadata: os.stat_result) -> RecordingFileIdentity:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o077
    ):
        raise SecurityError("analysis source must be one owned private regular file")
    return RecordingFileIdentity(
        relative_path=relative,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size_bytes=metadata.st_size,
        mode=metadata.st_mode,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def inspect_analysis_job(
    layout: AssetLayout, job: RecordingAnalysisJob, *, clock: Callable[[], datetime] = utc_now
) -> RecordingAnalysisPublication:
    """Worker-only expensive boundary: hash+inspect one stable original using audio.py."""
    ticket, seal = job.ticket, job.ticket.seal
    source_identity = None
    try:
        path = confined_path(layout.root, seal.original_path)
        with open_regular_no_follow(path) as held:
            before = _file_identity(seal.original_path, os.fstat(held.fileno()))
            inspection = inspect_canonical_wav(path)
            after = _file_identity(seal.original_path, os.fstat(held.fileno()))
            final_path = _file_identity(seal.original_path, path.lstat())
            if before != after or before != final_path:
                raise SecurityError("source identity changed during analysis")
            if (
                inspection.sha256 != seal.original_sha256
                or inspection.size_bytes != seal.original_size_bytes
                or inspection.audio != seal.audio
            ):
                raise SecurityError("original no longer matches its exact immutable capture seal")
            source_identity = after
        metrics = inspection.qc_metrics(seal.original_path, allow_quiet=ticket.allow_quiet)
        result = ticket.result(
            "verified" if metrics.passed else "needs-review", completed_at=clock(), metrics=metrics
        )
    except (SecurityError, ContractError) as error:
        result = ticket.result(
            "invalid",
            completed_at=clock(),
            error=RecordingError(
                code="RECORDING.SOURCE_CHANGED",
                message=str(error)[:1024],
                retryable=False,
            ),
        )
    except OSError:
        result = ticket.result(
            "failed",
            completed_at=clock(),
            error=RecordingError(
                code="RECORDING.ANALYSIS_FAILED",
                message="selected source could not be inspected",
                retryable=True,
            ),
        )
    return RecordingAnalysisPublication(
        job_sha256=ticket.job_sha256,
        attempt=job.attempt,
        result=result,
        source_identity=source_identity,
    )


class RecordingAnalysisQueue:
    """Single-owner persistent queue; memory holds only one selected job and bounded pages."""

    def __init__(
        self,
        store: RecordingTakeStore,
        index: RecordingIndex,
        *,
        build: RecordingBuildIdentity | None = None,
        clock: Callable[[], datetime] = utc_now,
        recover: bool = True,
    ) -> None:
        self.store, self.index, self.clock = store, index, clock
        self.build = build or analysis_build_identity()
        self._mutex = threading.RLock()
        self.wake = threading.Event()
        self.directory = store._directory(_ANALYSIS_ROOT)
        self.jobs_directory = store._directory(f"{_ANALYSIS_ROOT}/jobs")
        self._owner_fd = os.open(
            self.directory / ".owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            _file_identity(f"{_ANALYSIS_ROOT}/.owner.lock", os.fstat(self._owner_fd))
            fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._new_count = 0
            self._last_sequence = 0
            self._active_claims: set[tuple[str, int]] = set()
            if recover:
                self.recover()
        except BaseException:
            os.close(self._owner_fd)
            self._owner_fd = -1
            raise

    def close(self) -> None:
        with self._mutex:
            if self._owner_fd >= 0:
                os.close(self._owner_fd)
                self._owner_fd = -1

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self._mutex:
            if self._owner_fd < 0:
                raise RecordingAnalysisError("analysis queue is closed")
            owner_path = self.store._path(f"{_ANALYSIS_ROOT}/.owner.lock")
            held = _file_identity(f"{_ANALYSIS_ROOT}/.owner.lock", os.fstat(self._owner_fd))
            named = _file_identity(f"{_ANALYSIS_ROOT}/.owner.lock", owner_path.lstat())
            if (held.device, held.inode) != (named.device, named.inode):
                raise SecurityError("analysis queue ownership path changed")
            yield

    def _path(self, digest: str) -> Path:
        TypeAdapter(Sha256).validate_python(digest)
        return confined_path(self.store.layout.root, f"{_ANALYSIS_ROOT}/jobs/{digest}.json")

    def _project(self, job: RecordingAnalysisJob) -> None:
        self.index.project_analysis_job(
            job_sha256=job.ticket.job_sha256,
            sequence=job.sequence,
            priority=job.priority,
            status=job.status,
            attempt=job.attempt,
            payload=canonical_json_bytes(job).decode("utf-8"),
        )

    def _save(self, job: RecordingAnalysisJob) -> None:
        self.store._write(self._path(job.ticket.job_sha256), job)
        self._project(job)
        self.wake.set()

    def get(self, digest: str) -> RecordingAnalysisJob:
        with self._lock():
            return self._read_job(digest)

    def _read_job(self, digest: str) -> RecordingAnalysisJob:
        job = self.store._read(self._path(digest), RecordingAnalysisJob)
        if job.ticket.job_sha256 != digest:
            raise SecurityError("analysis job filename differs from canonical job binding")
        return job

    def page(
        self, *, after_sequence: int = 0, limit: int = 200
    ) -> tuple[RecordingAnalysisJob, ...]:
        with self._lock():
            return tuple(
                RecordingAnalysisJob.model_validate_json(payload)
                for _, payload in self.index.analysis_jobs(
                    after_sequence=after_sequence, limit=limit
                )
            )

    def recover(
        self,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        """Prepare history outside locks; publish only an unchanged durable job.

        The supervisor's readiness callback releases new-job claiming after the
        projection reset. Its live claims must never be mistaken for interrupted
        attempts by this concurrent historical scan. Standalone recovery retains
        restart semantics when no readiness callback is supplied.
        """
        with self._lock():
            self.index.reset_analysis_projection()
            self._active_claims.clear()
        if on_ready is not None:
            on_ready()
        with os.scandir(self.jobs_directory) as entries:
            for entry in entries:
                if cancelled():
                    return
                path = Path(entry.path)
                if path.suffix != ".json":
                    raise SecurityError("undeclared file in analysis job evidence")
                while not cancelled():
                    try:
                        self._recover_job(path.stem, preserve_claims=on_ready is not None)
                        break
                    except RecordingMetadataBusyError:
                        time.sleep(0.01)

    def _recover_job(self, digest: str, *, preserve_claims: bool) -> None:
        observed = self._read_job(digest)
        obsolete = (
            observed.ticket.analyzer_build != self.build
            or observed.ticket.algorithm_version != ALGORITHM_VERSION
        )
        published = None if obsolete else self.store.analysis_result_for_projection(digest)
        with self._lock():
            if preserve_claims and (digest, observed.attempt) in self._active_claims:
                return
            if observed != self.get(digest):
                return  # A current-owner transition already projected the newer head.
            job = observed
            self._last_sequence = max(self._last_sequence, job.sequence)
            if obsolete:
                self._save(job.model_copy(update={"status": "stale"}))
            else:
                if (
                    job.status == "running"
                    and published is not None
                    and published.attempt == job.attempt
                    and published.result.completed_at
                ):
                    job = RecordingAnalysisJob.model_validate(
                        job.model_dump()
                        | {
                            "status": published.result.state,
                            "publication": published,
                        }
                    )
                elif job.status == "running":
                    if job.attempt >= MAX_ATTEMPTS:
                        self.worker_failed(job, "analysis process was interrupted repeatedly")
                        return
                    job = job.model_copy(update={"status": "pending", "publication": None})
                if job != observed:
                    self._save(job)
                else:
                    self._project(job)
        if obsolete:
            self.submit(job.ticket.seal.take_id, priority="historical")

    def reconcile_takes(self, *, cancelled: Callable[[], bool] = lambda: False) -> int:
        """Repair the seal-commit/queue-submit crash gap using only paged canonical heads.

        Run on the historical preparer, never the control or worker-claim executor.
        Only identity pages are materialized; canonical heads are read individually
        without the interactive metadata or queue locks.
        """
        count = 0
        cursor = None
        while not cancelled():
            try:
                page = self.store.iter_take_ids_for_projection(after_take_id=cursor, limit=200)
            except RecordingMetadataBusyError:
                time.sleep(0.01)
                continue
            if not page:
                break
            for take_id in page:
                if cancelled():
                    return count
                while not cancelled():
                    try:
                        take = self.store.get_take_for_projection(take_id)
                        if take.seal is not None:
                            if self._submit_prepared(take, priority="historical") is None:
                                continue
                            count += 1
                        break
                    except RecordingMetadataBusyError:
                        time.sleep(0.01)
            cursor = page[-1]
        return count

    def submit(self, take_id: str, *, priority: AnalysisPriority = "new") -> RecordingAnalysisJob:
        """Durably schedule metadata only. Identical source/version/edit/context coalesces."""
        while True:
            take = (
                self.store.get_take_for_projection(take_id)
                if priority == "historical"
                else self.store.get_take(take_id)
            )
            job = self._submit_prepared(take, priority=priority)
            if job is not None:
                return job

    def _submit_prepared(
        self, take: RecordingTake, *, priority: AnalysisPriority
    ) -> RecordingAnalysisJob | None:
        with self._lock():
            if take.seal is None:
                raise RecordingAnalysisError("analysis requires a sealed original")
            ticket = RecordingAnalysisTicket(
                seal=take.seal,
                analyzer_build=self.build,
                edit_revision=take.edit_revision,
                context_revision=take.reservation.group.context_revision,
                allow_quiet=take.reservation.prompt.allows_quiet,
                queued_at=self.clock(),
            )
            if self._path(ticket.job_sha256).exists():
                existing = self.get(ticket.job_sha256)
                if priority == "new" and existing.priority == "historical":
                    existing = existing.model_copy(update={"priority": "new"})
                    self._save(existing)
                else:
                    self._project(existing)
                return existing
            self._last_sequence = max(self._last_sequence + 1, time.time_ns())
            job = RecordingAnalysisJob(
                ticket=ticket, sequence=self._last_sequence, priority=priority, attempt=0
            )
            self._save(job)
            try:
                self._publish(job, ticket.result("pending"))
            except StaleRecordingAnalysisError:
                self._save(job.model_copy(update={"status": "stale"}))
                return None
            return job

    def _publish(
        self,
        job: RecordingAnalysisJob,
        result: RecordingAnalysisResult,
        identity: RecordingFileIdentity | None = None,
    ) -> tuple[str, RecordingTake]:
        publication = RecordingAnalysisPublication(
            job_sha256=job.ticket.job_sha256,
            attempt=job.attempt,
            result=result,
            source_identity=identity,
        )
        operation_id = f"analysis-{canonical_sha256(publication)[:48]}"
        take = self.store.publish_analysis(operation_id=operation_id, publication=publication)
        return operation_id, take

    def claim(self) -> RecordingAnalysisJob | None:
        with self._lock():
            self.wake.clear()
            order: tuple[AnalysisPriority, ...] = (
                ("historical", "new") if self._new_count >= NEW_JOB_BURST else ("new", "historical")
            )
            for priority in order:
                candidates = self.index.analysis_jobs(status="pending", priority=priority, limit=1)
                if not candidates:
                    continue
                cached = RecordingAnalysisJob.model_validate_json(candidates[0][1])
                job = self.get(cached.ticket.job_sha256)
                if job != cached:
                    raise SecurityError("analysis queue projection differs from durable evidence")
                job = job.model_copy(
                    update={"attempt": job.attempt + 1, "status": "running", "publication": None}
                )
                self._save(job)
                try:
                    self._publish(job, job.ticket.result("running"))
                except RecordingMetadataBusyError:
                    self._save(cached)
                    raise
                except StaleRecordingAnalysisError:
                    self._save(job.model_copy(update={"status": "stale"}))
                    return None
                self._new_count = self._new_count + 1 if priority == "new" else 0
                self._active_claims.add((job.ticket.job_sha256, job.attempt))
                return job
            return None

    def complete(
        self, publication: RecordingAnalysisPublication
    ) -> tuple[str, RecordingTake] | None:
        with self._lock():
            job = self.get(publication.job_sha256)
            if (
                job.status != "running"
                or job.attempt != publication.attempt
                or not job.ticket.matches(publication.result)
                or publication.result.completed_at is None
            ):
                raise RecordingAnalysisError("worker returned a stale or unbound result")
            try:
                published = self._publish(job, publication.result, publication.source_identity)
            except AnalysisSourceChangedError:
                changed = job.ticket.result(
                    "invalid",
                    completed_at=self.clock(),
                    error=RecordingError(
                        code="RECORDING.SOURCE_CHANGED",
                        message="source changed before analysis publication",
                        retryable=False,
                    ),
                )
                publication = publication.model_copy(
                    update={"result": changed, "source_identity": None}
                )
                published = self._publish(job, changed)
            except StaleRecordingAnalysisError:
                self._save(job.model_copy(update={"status": "stale", "publication": publication}))
                self._active_claims.discard((job.ticket.job_sha256, job.attempt))
                return None
            self._save(
                RecordingAnalysisJob.model_validate(
                    job.model_dump()
                    | {
                        "status": publication.result.state,
                        "publication": publication,
                    }
                )
            )
            self._active_claims.discard((job.ticket.job_sha256, job.attempt))
            return published

    def worker_failed(
        self, job: RecordingAnalysisJob, message: str
    ) -> tuple[str, RecordingTake] | None:
        publication = RecordingAnalysisPublication(
            job_sha256=job.ticket.job_sha256,
            attempt=job.attempt,
            result=job.ticket.result(
                "failed",
                completed_at=self.clock(),
                error=RecordingError(
                    code="RECORDING.ANALYSIS_FAILED", message=message[:1024], retryable=True
                ),
            ),
        )
        published = self.complete(publication)
        if published is not None and job.attempt < MAX_ATTEMPTS:
            self.retry(job.ticket.job_sha256)
        return published

    def retry(self, digest: str) -> RecordingAnalysisJob:
        with self._lock():
            job = self.get(digest)
            if job.status != "failed" or job.attempt >= 1000:
                raise RecordingAnalysisError("only failed analysis can be retried")
            job = job.model_copy(update={"status": "pending", "publication": None})
            self._save(job)
            return job


class RecordingAnalysisWorker:
    """One owned persistent process, one in-flight request, bounded framed pipes."""

    def __init__(
        self,
        layout: AssetLayout,
        build: RecordingBuildIdentity,
        *,
        block_fd: int | None = None,
        deadline_seconds: float = WORKER_JOB_SECONDS,
    ) -> None:
        from .recording_service import RecordingFrameDecoder

        self.generation = f"analysis-{uuid4().hex}"
        self.deadline_seconds = deadline_seconds
        self._decoder = RecordingFrameDecoder()
        self._decoder.maximum = MAX_WORKER_FRAME_BYTES
        command = [
            sys.executable,
            "-m",
            "cole_voice_lab.recording_analysis",
            "--worker",
            "--asset-root",
            str(layout.root),
            "--generation",
            self.generation,
        ]
        if block_fd is not None:
            command.extend(["--test-block-fd", str(block_fd)])
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=() if block_fd is None else (block_fd,),
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        try:
            hello = self._read(WORKER_START_SECONDS)
            if (
                not isinstance(hello, AnalysisWorkerHello)
                or hello.generation != self.generation
                or hello.build != build
            ):
                raise RecordingAnalysisError("analysis worker runtime identity differs")
        except BaseException:
            self.close()
            raise

    @property
    def pid(self) -> int:
        return self.process.pid

    def _read(self, seconds: float) -> AnalysisWorkerMessage:
        assert self.process.stdout is not None
        descriptor = self.process.stdout.fileno()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            readable, _, _ = select.select(
                [descriptor], [], [], min(0.05, max(0, deadline - time.monotonic()))
            )
            if not readable:
                continue
            chunk = os.read(descriptor, 8192)
            if not chunk:
                raise RecordingAnalysisError("analysis worker exited before a complete reply")
            messages = tuple(self._decoder.feed(chunk))
            if len(messages) > 1:
                raise RecordingAnalysisError("analysis worker returned unsolicited extra frames")
            if messages:
                return decode_analysis_message(messages[0])
        raise RecordingAnalysisError("analysis worker exceeded its bounded response deadline")

    def _send(self, model: ContractModel) -> None:
        assert self.process.stdin is not None
        payload = recording_frame_bytes(model)
        if len(payload) > MAX_WORKER_FRAME_BYTES + 4:
            raise RecordingAnalysisError("analysis request exceeds its frame bound")
        deadline = time.monotonic() + 2.0
        offset = 0
        while offset < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecordingAnalysisError("analysis worker stopped consuming requests")
            _, writable, _ = select.select(
                [], [self.process.stdin.fileno()], [], min(remaining, 0.05)
            )
            if writable:
                offset += os.write(self.process.stdin.fileno(), payload[offset:])

    def analyze(self, job: RecordingAnalysisJob) -> RecordingAnalysisPublication:
        self._send(AnalysisWorkerRequest(generation=self.generation, job=job))
        reply = self._read(self.deadline_seconds)
        if (
            not isinstance(reply, AnalysisWorkerReply)
            or reply.generation != self.generation
            or reply.publication.job_sha256 != job.ticket.job_sha256
            or reply.publication.attempt != job.attempt
            or not job.ticket.matches(reply.publication.result)
        ):
            raise RecordingAnalysisError("analysis worker reply binding changed")
        return reply.publication

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.5)
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()


type AnalysisCompletionHook = Callable[[str, RecordingAnalysisResult], None]


class RecordingAnalysisSupervisor:
    """Worker lifetime and analysis publication run independently of service controls."""

    def __init__(
        self,
        queue: RecordingAnalysisQueue,
        *,
        on_result: AnalysisCompletionHook | None = None,
        on_failure: Callable[[RecordingError], None] | None = None,
        block_fd: int | None = None,
        deadline_seconds: float = WORKER_JOB_SECONDS,
    ) -> None:
        self.queue, self.on_result, self.on_failure = queue, on_result, on_failure
        self.block_fd, self.deadline_seconds = block_fd, deadline_seconds
        self._stop = threading.Event()
        self._history_ready = threading.Event()
        self._failure_lock = threading.Lock()
        self._worker: RecordingAnalysisWorker | None = None
        self.fatal_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name="recording-analysis-supervisor", daemon=True
        )
        self._history_thread = threading.Thread(
            target=self._reconcile, name="recording-analysis-history", daemon=True
        )

    @property
    def worker_pid(self) -> int | None:
        worker = self._worker
        return None if worker is None else worker.pid

    def start(self) -> None:
        self._history_thread.start()
        self._thread.start()

    def _reconcile(self) -> None:
        try:
            self.queue.recover(cancelled=self._stop.is_set, on_ready=self._history_ready.set)
            self.queue.reconcile_takes(cancelled=self._stop.is_set)
        except BaseException as error:
            self._failed(error)
        finally:
            self._history_ready.set()

    def _retry_metadata[T](self, action: Callable[[], T]) -> T | None:
        while not self._stop.is_set():
            try:
                return action()
            except RecordingMetadataBusyError:
                self._stop.wait(0.01)
        return None

    def _run(self) -> None:
        try:
            while not self._history_ready.wait(0.01):
                if self._stop.is_set():
                    return
            while not self._stop.is_set():
                job = self._retry_metadata(self.queue.claim)
                if job is None:
                    self.queue.wake.wait(0.1)
                    continue
                publication: RecordingAnalysisPublication | None = None
                try:
                    if self._worker is None:
                        self._worker = RecordingAnalysisWorker(
                            self.queue.store.layout,
                            self.queue.build,
                            block_fd=self.block_fd,
                            deadline_seconds=self.deadline_seconds,
                        )
                    publication = self._worker.analyze(job)
                except (RecordingAnalysisError, OSError, ValueError) as error:
                    if self._worker is not None:
                        self._worker.close()
                        self._worker = None
                    if self._stop.is_set():
                        break  # Durable running head is requeued by the next owner's recovery.
                    message = f"analysis process failed: {type(error).__name__}"
                    published = self._retry_metadata(
                        partial(self.queue.worker_failed, job, message)
                    )
                    publication = self._retry_metadata(
                        partial(
                            self.queue.store.analysis_result_for_projection,
                            job.ticket.job_sha256,
                        )
                    )
                else:
                    assert publication is not None
                    published = self._retry_metadata(partial(self.queue.complete, publication))
                if published is not None and publication is not None and self.on_result is not None:
                    self.on_result(published[0], publication.result)
        except BaseException as error:
            self._failed(error)
        finally:
            if self._worker is not None:
                self._worker.close()
                self._worker = None

    def _failed(self, error: BaseException) -> None:
        with self._failure_lock:
            if self.fatal_error is not None:
                return
            self.fatal_error = error
            self._stop.set()
            worker = self._worker
            if worker is not None and worker.process.poll() is None:
                worker.process.terminate()
            if self.on_failure is not None:
                try:
                    self.on_failure(
                        RecordingError(
                            code="RECORDING.ANALYSIS_FAILED",
                            message="background recording analysis stopped; recovery is required",
                            retryable=False,
                        )
                    )
                except BaseException as notification_error:
                    self.fatal_error = BaseExceptionGroup(
                        "analysis failure and notification failure", [error, notification_error]
                    )

    def close(self) -> None:
        self._stop.set()
        self.queue.wake.set()
        worker = self._worker
        if worker is not None and worker.process.poll() is None:
            worker.process.terminate()
        deadline = time.monotonic() + 2.0
        for thread in (self._thread, self._history_thread):
            if thread.ident is None:
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                raise RecordingAnalysisError(
                    "analysis supervisor did not reach its shutdown boundary"
                )


def _worker_main(layout: AssetLayout, generation: str, block_fd: int | None) -> int:
    """CPU work uses a daemon thread inside this separate process; pipe EOF stays observable."""
    import queue

    from .recording_service import RecordingFrameDecoder

    build = analysis_build_identity()
    output_fd, input_fd = sys.stdout.fileno(), sys.stdin.fileno()
    os.set_blocking(output_fd, False)
    decoder = RecordingFrameDecoder()
    decoder.maximum = MAX_WORKER_FRAME_BYTES
    results: queue.Queue[RecordingAnalysisPublication | BaseException] = queue.Queue(1)
    output = bytearray(
        recording_frame_bytes(AnalysisWorkerHello(generation=generation, build=build))
    )
    output_deadline = time.monotonic() + 2.0
    busy = False

    def inspect(request: AnalysisWorkerRequest) -> None:
        try:
            if block_fd is not None and os.read(block_fd, 1) != b"1":
                raise RecordingAnalysisError("synthetic test barrier closed")
            results.put_nowait(inspect_analysis_job(layout, request.job))
        except BaseException as error:
            results.put_nowait(error)

    while True:
        if not results.empty():
            result = results.get_nowait()
            if isinstance(result, BaseException):
                raise RecordingAnalysisError("analysis inspection failed unexpectedly") from result
            output = bytearray(
                recording_frame_bytes(
                    AnalysisWorkerReply(generation=generation, publication=result)
                )
            )
            output_deadline = time.monotonic() + 2.0
            busy = False
        readable, writable, _ = select.select([input_fd], [output_fd] if output else [], [], 0.02)
        if readable:
            chunk = os.read(input_fd, 8192)
            if not chunk:
                decoder.finish()
                return 0  # No filesystem deletion; unfinished jobs remain durable in the parent.
            for payload in decoder.feed(chunk):
                request = decode_analysis_message(payload)
                if (
                    not isinstance(request, AnalysisWorkerRequest)
                    or busy
                    or output
                    or request.generation != generation
                    or request.job.ticket.analyzer_build != build
                    or request.job.ticket.algorithm_version != ALGORITHM_VERSION
                ):
                    raise RecordingAnalysisError(
                        "invalid analysis worker request ordering or identity"
                    )
                busy = True
                threading.Thread(
                    target=inspect, args=(request,), daemon=True, name="recording-pcm-inspection"
                ).start()
        if writable and output:
            del output[: os.write(output_fd, output)]
        if output and time.monotonic() > output_deadline:
            raise RecordingAnalysisError("analysis reply pipe stopped consuming")


RECORDING_ANALYSIS_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "recording-analysis-job": RecordingAnalysisTicket,
    "recording-analysis-job-head": RecordingAnalysisJob,
    "recording-analysis-worker-hello": AnalysisWorkerHello,
    "recording-analysis-worker-request": AnalysisWorkerRequest,
    "recording-analysis-worker-reply": AnalysisWorkerReply,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--test-block-fd", type=int)
    options = parser.parse_args(argv)
    try:
        generation: str = TypeAdapter(StableId).validate_python(options.generation)
        return _worker_main(
            AssetLayout.validate(options.asset_root), generation, options.test_block_fd
        )
    except (VoiceLabError, ValidationError, OSError, ValueError):
        print(
            json.dumps(
                {
                    "service": "recording-analysis",
                    "error_code": "RECORDING.ANALYSIS_FAILED",
                    "message": "analysis worker stopped",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Durable one-at-a-time fine-tune jobs, Speak requests, and their owned, cancellable workers.

@canonical finetune_jobs -- the recording service accepts a fine-tune in metadata time
(``FineTuneJobQueue.submit`` checks the runtime and free disk, never the library) and one
worker process, leading its own session and process group, runs
``finetune.run_finetune_pipeline`` on the catalog version the service pinned (the one the app
shows; a group-scoped service without a pin uses the subject's active version). The
supervisor turns the worker's stage frames and the tail of ``train.log`` into
``FineTuneProgressEvent``s (at most one every five seconds), cancels by signalling the whole
group, and marks a job a stopped service left running as failed (``FINETUNE.INTERRUPTED``),
publishing that outcome; a job is never retried.
Speak requests (``SynthesizeCommand``) are the second job kind of the same supervisor: in
memory only, one at a time, sharing the one GPU slot with fine-tuning, each run by the same
worker module (``finetune.resolve_speech_model`` then ``finetune.synthesize_speech``) with a
120 s deadline and cancelled by killing its group. The supervisor keeps the digests the
workers verified (``finetune.FineTuneDigestCache``) and hands them to the next request.
Layout under ``provenance/finetune-jobs/``: ``.owner.lock`` (one owning service),
``head.json`` (the one queued or running job), ``requests/<op>.json`` and
``terminal/<op>.json`` (immutable), and ``speech-active.json`` while a Speak request's worker
runs (command-line training reads it and ``head.json`` to stay off the GPU). Runs are
written to ``derived/finetune/<run id>/``, Speak clips to ``derived/speech/<op>/``; when
supervision starts, request directories a stopped service left without ``speech.json`` are
removed.
Worker protocol (length-prefixed frames, ``recording_service.RecordingFrameDecoder``): one
``FineTuneWorkerRequest`` in, ``FineTuneWorkerStage`` frames then one
``FineTuneWorkerReply`` out; or one ``SpeechWorkerRequest`` in and one ``SpeechWorkerReply``
out. The worker kills its own group when its stdin ends, so neither the trainer nor the
synthesizer outlives the service.
Related: finetune.py (the pipeline), recording_service.py (dispatch and startup),
recording_takes.py (durable evidence writes), recording_contracts.py (wire events),
docs/FINETUNE.md.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter, model_validator

from .canonical import canonical_sha256, open_regular_no_follow
from .catalog_ports import CorpusSnapshotSecurityError, CorpusStoreError
from .corpus_store import read_catalog_snapshot
from .errors import ContractError, SecurityError, VoiceLabError
from .finetune import (
    FINETUNE_OUTPUT_ROOT,
    MAX_TRUSTED_DIGESTS,
    SPEAK_FAMILIES,
    AbsolutePath,
    FineTuneDigestCache,
    FineTuneError,
    FineTunePipelineStep,
    FineTunePretrainedPins,
    FineTuneRuntimeProbe,
    FineTuneSummaryCache,
    FineTuneTrustedDigests,
    FineTuneVerifiedDigest,
    SpeechClip,
    SpeechResolution,
    discard_speech_output,
    discard_unfinished_speech,
    finetune_run_summary,
    finished_finetune_run_ids,
    finished_finetune_runs,
    mark_finetune_run_failed,
    release_finetune_runtime_link,
    resolve_speech_model,
    run_finetune_pipeline,
    synthesize_speech,
)
from .models import F5_FAMILY, ContractModel, Sha256, StableId, utc_now
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    MAX_CONTROL_FRAME_BYTES,
    CancelFineTuneCommand,
    CancelSynthesisCommand,
    CatalogReference,
    ErrorCode,
    FineTuneCommand,
    FineTuneEvent,
    FineTuneProgressEvent,
    FineTuneRunSummary,
    FineTuneStage,
    FineTuneState,
    FineTuneStatusCommand,
    FineTuneStatusEvent,
    RecordingError,
    SynthesisEvent,
    SynthesisState,
    SynthesizeCommand,
    UtcTimestamp,
    recording_frame_bytes,
    speech_directory,
)
from .recording_service import (
    RecordingFrameDecoder,
    RecordingServiceError,
    _active_catalog_failure,
    _bounded_message,
)
from .recording_takes import (
    RecordingMetadataBusyError,
    read_recording_evidence,
    recording_evidence_directory,
    sync_recording_evidence_directory,
    write_recording_evidence,
)
from .storage_binding import open_catalog_reader
from .training import (
    F5TrainingAdapter,
    FineTuneTrainingProgress,
    TrainingAdapter,
    parse_training_progress,
    training_adapter,
)

FINETUNE_JOBS_ROOT: Final = "provenance/finetune-jobs"
# The model family the app's fine-tune trains (D-049): its intent names none, and its worker's
# pretrained-pin test seam is F5-TTS's.
APP_TRAINING_FAMILY: Final = F5_FAMILY
FINETUNE_WORKER_MODULE: Final = "cole_voice_lab.recording_finetune_jobs"
# A run's dataset, packed copy, checkpoints and samples need several GiB.
FINETUNE_MIN_FREE_BYTES: Final = 8 * 1024**3  # 8 GiB
FINETUNE_PROGRESS_INTERVAL_SECONDS: Final = 5.0
FINETUNE_CANCEL_GRACE_SECONDS: Final = 10.0
# No deadline fits training; this only stops a job that can no longer be finishing.
FINETUNE_WATCHDOG_SECONDS: Final = 24 * 60 * 60.0
# A Save committing while the worker opens the take store read-only is retried this long.
FINETUNE_METADATA_RETRY_SECONDS: Final = 10.0
CANCELLED_BY_OPERATOR: Final = "cancelled by the operator"
CANCELLING_MESSAGE: Final = "Cancelling the fine-tune"
INTERRUPTED_BY_RESTART: Final = (
    "the recording service stopped while this fine-tune was running; start it again"
)
# While a restarted service resolves the job its predecessor left running.
RECOVERING_MESSAGE: Final = "Checking the last fine-tune"
# Supervision failed: requests are refused with the first, the failure is reported with the second.
SUPERVISION_STOPPED: Final = "fine-tune supervision stopped; restart the app"
SUPERVISION_FAILURE: Final = "fine-tune supervision stopped; restart the app to recover the job"
_SEND_DEADLINE_SECONDS: Final = 2.0
_REPLY_EXIT_SECONDS: Final = 2.0
_GROUP_STOP_SECONDS: Final = 5.0
# A cancelled or overdue worker group still alive this long after SIGKILL stops supervision.
_KILL_WAIT_SECONDS: Final = 10.0
_LOG_TAIL_BYTES: Final = 65_536
_LOG_CARRY_CHARACTERS: Final = 4_096
_POLL_SECONDS: Final = 0.1
_STABLE_ID: TypeAdapter[StableId] = TypeAdapter(StableId)
# A Speak request's worker may run this long (model load and synthesis included).
SPEECH_DEADLINE_SECONDS: Final = 120.0
# Ended Speak requests kept in memory, so a resent request or cancel names its outcome.
MAX_ENDED_SPEECH: Final = 64

type TerminalFineTuneState = Literal["finished", "failed", "cancelled"]
type FineTuneJobEvent = FineTuneProgressEvent | FineTuneEvent | SynthesisEvent


@dataclass(frozen=True, slots=True)
class _Outcome:
    state: TerminalFineTuneState
    error: RecordingError | None = None
    replied: bool = False
    reply: FineTuneWorkerReply | SpeechWorkerReply | None = None


def _interrupted(message: str) -> RecordingError:
    return RecordingError(
        code="FINETUNE.INTERRUPTED", message=_bounded_message(message), retryable=True
    )


def _group_did_not_stop(pgid: int) -> FineTuneJobError:
    """The fatal failure for a worker group that outlived SIGKILL; supervision then stops."""
    return FineTuneJobError(
        "RECORDING.WRITE_FAILED", f"the fine-tune worker group {pgid} did not stop"
    )


def _duration_text(seconds: float) -> str:
    return f"{seconds / 3600:g} hours" if seconds >= 3600 else f"{seconds:g} seconds"


class FineTuneJobError(VoiceLabError):
    """A refused or impossible fine-tune job request, carrying the wire error to send."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.recording_error = RecordingError(
            code=code, message=_bounded_message(message), retryable=retryable
        )


class FineTuneJobIntent(ContractModel):
    """The exact accepted command, written once before its job enters the queue.

    ``catalog_reference`` is the catalog version the service pinned when it accepted the
    command (the one the app shows); the worker trains on exactly that snapshot. It is absent
    for a service without a pin, whose worker uses the subject's active version.
    """

    schema_version: Literal["cole-voice-lab.finetune-job-intent/v1"] = (
        "cole-voice-lab.finetune-job-intent/v1"
    )
    command: FineTuneCommand
    subject_id: StableId
    request_sha256: Sha256
    queued_at: UtcTimestamp
    catalog_reference: CatalogReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _command_binding(self) -> FineTuneJobIntent:
        if self.request_sha256 != canonical_sha256(self.command):
            raise ValueError("fine-tune intent must bind its exact command")
        return self


class FineTuneJob(ContractModel):
    """One fine-tune job's durable state.

    ``run_id`` is chosen when the job starts; ``worker_pid`` is the worker process, which
    leads its own process group (the group id is the same number); ``runtime_link`` is the
    runtime's ``ckpts/<run id>`` path once the worker has described the runtime.
    ``message`` is a cancelled job's note (its training had finished, so its model exists
    without listening samples); a failed job says so in its error instead.
    """

    schema_version: Literal["cole-voice-lab.finetune-job/v1"] = "cole-voice-lab.finetune-job/v1"
    operation_id: StableId
    request_sha256: Sha256
    queued_at: UtcTimestamp
    state: FineTuneState = "queued"
    run_id: StableId | None = None
    started_at: UtcTimestamp | None = None
    worker_pid: int | None = Field(default=None, ge=2)
    runtime_link: AbsolutePath | None = None
    cancel_requested: bool = False
    finished_at: UtcTimestamp | None = None
    error: RecordingError | None = None
    message: str | None = Field(
        default=None, min_length=1, max_length=1024, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _state_evidence(self) -> FineTuneJob:
        started = self.run_id is not None
        if started != (self.started_at is not None):
            raise ValueError("a started fine-tune job names its run and start time")
        if (self.state == "queued") == started and self.state in {"queued", "running"}:
            raise ValueError("a queued job has not started; a running job has")
        if self.state == "finished" and not started:
            raise ValueError("a finished job names its run")
        if not started and (self.worker_pid is not None or self.runtime_link is not None):
            raise ValueError("only a started job has a worker")
        if (self.state in {"queued", "running"}) != (self.finished_at is None):
            raise ValueError("a terminal job carries its finish time; an active one none")
        if (self.state == "failed") != (self.error is not None):
            raise ValueError("a failed job names its error; other jobs carry none")
        if self.cancel_requested and self.state != "running":
            raise ValueError("only a running job waits for its cancellation")
        if self.message is not None and self.state != "cancelled":
            raise ValueError("only a cancelled job carries a note")
        return self

    @property
    def run_root(self) -> str | None:
        return None if self.run_id is None else f"{FINETUNE_OUTPUT_ROOT}/{self.run_id}"


class FineTuneJobHead(ContractModel):
    """The active job pointer: at most one queued or running job."""

    schema_version: Literal["cole-voice-lab.finetune-job-head/v1"] = (
        "cole-voice-lab.finetune-job-head/v1"
    )
    job: FineTuneJob | None = None

    @model_validator(mode="after")
    def _active_only(self) -> FineTuneJobHead:
        if self.job is not None and self.job.state not in {"queued", "running"}:
            raise ValueError("the fine-tune queue head holds only a queued or running job")
        return self


class FineTuneWorkerRequest(ContractModel):
    schema_version: Literal["cole-voice-lab.finetune-worker/v1"] = (
        "cole-voice-lab.finetune-worker/v1"
    )
    kind: Literal["run"] = "run"
    generation: StableId
    operation_id: StableId
    request_sha256: Sha256
    run_id: StableId
    runtime_root: AbsolutePath
    # Substitute pretrained-file pins (tests only; the service never sends them).
    pretrained_pin: FineTunePretrainedPins | None = None


class FineTuneWorkerStage(ContractModel):
    schema_version: Literal["cole-voice-lab.finetune-worker/v1"] = (
        "cole-voice-lab.finetune-worker/v1"
    )
    kind: Literal["stage"] = "stage"
    generation: StableId
    operation_id: StableId
    stage: FineTuneStage
    runtime_link: AbsolutePath | None = None


class FineTuneWorkerReply(ContractModel):
    """The worker's one result: ``error`` is absent exactly when the run finished."""

    schema_version: Literal["cole-voice-lab.finetune-worker/v1"] = (
        "cole-voice-lab.finetune-worker/v1"
    )
    kind: Literal["result"] = "result"
    generation: StableId
    operation_id: StableId
    request_sha256: Sha256
    run_id: StableId
    error: RecordingError | None = None


class SpeechWorkerRequest(ContractModel):
    """One Speak request for the worker, with the digests earlier requests verified.

    ``trusted`` is reused only for files whose identity is unchanged
    (``finetune.FineTuneDigestCache``).
    """

    schema_version: Literal["cole-voice-lab.finetune-worker/v1"] = (
        "cole-voice-lab.finetune-worker/v1"
    )
    kind: Literal["synthesize"] = "synthesize"
    generation: StableId
    operation_id: StableId
    request_sha256: Sha256
    command: SynthesizeCommand
    runtime_root: AbsolutePath
    # Substitute pretrained-file pins (tests only; the service never sends them).
    pretrained_pin: FineTunePretrainedPins | None = None
    trusted: FineTuneTrustedDigests = FineTuneTrustedDigests()

    @model_validator(mode="after")
    def _command_binding(self) -> SpeechWorkerRequest:
        if (
            self.command.operation_id != self.operation_id
            or canonical_sha256(self.command) != self.request_sha256
        ):
            raise ValueError("a Speak worker request binds its exact command")
        return self


class SpeechWorkerResolution(ContractModel):
    """What the worker resolved: the speaking run (none for base) and the reference."""

    run_id: StableId | None
    reference_run_id: StableId
    reference_prompt_id: StableId


class SpeechWorkerReply(ContractModel):
    """The Speak worker's one result: its clip, or its error (with the resolution, if any).

    ``trusted`` is what the worker verified, for the next request.
    """

    schema_version: Literal["cole-voice-lab.finetune-worker/v1"] = (
        "cole-voice-lab.finetune-worker/v1"
    )
    kind: Literal["synthesis-result"] = "synthesis-result"
    generation: StableId
    operation_id: StableId
    request_sha256: Sha256
    resolution: SpeechWorkerResolution | None = None
    clip: SpeechClip | None = None
    trusted: FineTuneTrustedDigests = FineTuneTrustedDigests()
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _one_result(self) -> SpeechWorkerReply:
        if (self.error is None) == (self.clip is None):
            raise ValueError("a Speak result names its clip or its error")
        clip, resolution = self.clip, self.resolution
        if clip is not None and (
            resolution is None
            or clip.operation_id != self.operation_id
            or clip.model != (resolution.run_id or "base")
            or clip.reference_run_id != resolution.reference_run_id
            or clip.reference_prompt_id != resolution.reference_prompt_id
        ):
            raise ValueError("a Speak clip belongs to its request and its resolution")
        return self


type FineTuneWorkerMessage = Annotated[
    FineTuneWorkerRequest
    | FineTuneWorkerStage
    | FineTuneWorkerReply
    | SpeechWorkerRequest
    | SpeechWorkerReply,
    Field(discriminator="kind"),
]
_WORKER_MESSAGE: TypeAdapter[FineTuneWorkerMessage] = TypeAdapter(FineTuneWorkerMessage)


def decode_finetune_worker_message(payload: bytes) -> FineTuneWorkerMessage:
    """Decode one worker frame.

    Raises: ``FineTuneJobError`` RECORDING.INVALID_MESSAGE for an oversized frame;
    ``pydantic.ValidationError`` for anything that is not a worker message.
    """
    if len(payload) > MAX_CONTROL_FRAME_BYTES:
        raise FineTuneJobError(
            "RECORDING.INVALID_MESSAGE", "fine-tune worker message exceeds its bounded frame"
        )
    return _WORKER_MESSAGE.validate_json(payload)


def finetune_job_event(job: FineTuneJob) -> FineTuneProgressEvent:
    """The durable view of ``job`` as a wire event (no stage or training progress)."""
    return FineTuneProgressEvent(
        operation_id=job.operation_id,
        state=job.state,
        run_id=job.run_id,
        message=CANCELLING_MESSAGE if job.cancel_requested else job.message,
        error=job.error,
    )


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _gibibytes(size: int) -> str:
    return f"{size / 1024**3:.1f} GiB"


class FineTuneJobQueue:
    """The durable fine-tune queue for one service and subject: one active job at a time.

    Every method is short metadata work under one lock, except ``finish`` and ``recover``,
    which rewrite a run record (hashing its checkpoints) before taking the lock.
    """

    def __init__(
        self,
        layout: AssetLayout,
        *,
        subject_id: str,
        runtime_root: Path | None,
        free_bytes: Callable[[Path], int] = _free_bytes,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Params: ``runtime_root`` is the F5 runtime the service was launched with
        (``None``: fine-tuning is not configured); ``free_bytes`` measures free space on the
        asset root's disk (tests substitute it).
        Raises: ``BlockingIOError`` when another service owns the queue; ``SecurityError``
        for unsafe evidence directories or files; ``ContractError`` for invalid evidence.
        """
        self.layout = AssetLayout.validate(layout.root)
        self.subject_id: str = _STABLE_ID.validate_python(subject_id)
        self.runtime_root = runtime_root
        self.wake = threading.Event()
        self._free_bytes = free_bytes
        self._clock = clock
        self._mutex = threading.RLock()
        self._cancelling: set[str] = set()
        self.directory = recording_evidence_directory(self.layout, FINETUNE_JOBS_ROOT)
        recording_evidence_directory(self.layout, f"{FINETUNE_JOBS_ROOT}/requests")
        recording_evidence_directory(self.layout, f"{FINETUNE_JOBS_ROOT}/terminal")
        self._owner = os.open(
            self.directory / ".owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        self._closed = False
        try:
            metadata = os.fstat(self._owner)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                raise SecurityError("the fine-tune queue owner lock must be one private file")
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self._lock():
                if not self._head_path.exists():
                    write_recording_evidence(self._head_path, FineTuneJobHead())
                self._head()
        except BaseException:
            self.close()
            raise

    @property
    def _head_path(self) -> Path:
        return self.directory / "head.json"

    @property
    def runtime_configured(self) -> bool:
        """Whether the service names a runtime and ``<runtime>/bin/python`` exists."""
        return self.runtime_root is not None and (self.runtime_root / "bin" / "python").is_file()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self._mutex:
            if self._closed:
                raise FineTuneJobError("RECORDING.GATE_NOT_READY", "the fine-tune queue is closed")
            yield

    def _head(self) -> FineTuneJobHead:
        return read_recording_evidence(self._head_path, FineTuneJobHead)

    def _write_head(self, job: FineTuneJob | None) -> None:
        write_recording_evidence(self._head_path, FineTuneJobHead(job=job))
        self.wake.set()

    def _terminal_path(self, operation_id: str) -> Path:
        return self.directory / "terminal" / f"{_STABLE_ID.validate_python(operation_id)}.json"

    def _intent_path(self, operation_id: str) -> Path:
        return self.directory / "requests" / f"{_STABLE_ID.validate_python(operation_id)}.json"

    def _terminal(self, operation_id: str) -> FineTuneJob | None:
        path = self._terminal_path(operation_id)
        if not path.exists():
            return None
        job = read_recording_evidence(path, FineTuneJob)
        if job.operation_id != operation_id or job.state in {"queued", "running"}:
            raise SecurityError("terminal fine-tune evidence names another operation or state")
        return job

    def _active(self, operation_id: str) -> FineTuneJob:
        job = self._head().job
        if job is None or job.operation_id != operation_id:
            raise SecurityError(f"fine-tune job {operation_id} is no longer the active job")
        return job

    def intent(self, operation_id: str) -> FineTuneJobIntent:
        """The accepted command of ``operation_id``.

        Raises: ``SecurityError`` when it is missing, unsafe or filed under another operation.
        """
        intent = read_recording_evidence(self._intent_path(operation_id), FineTuneJobIntent)
        if intent.command.operation_id != operation_id:
            raise SecurityError("fine-tune intent filename names another operation")
        return intent

    def _new_intent(
        self,
        command: FineTuneCommand,
        request_sha256: str,
        catalog_reference: CatalogReference | None = None,
    ) -> FineTuneJobIntent:
        path = self._intent_path(command.operation_id)
        if path.exists():
            intent = self.intent(command.operation_id)
            if (
                intent.request_sha256 != request_sha256
                or intent.subject_id != self.subject_id
                or intent.catalog_reference != catalog_reference
            ):
                raise FineTuneJobError(
                    "RECORDING.INVALID_MESSAGE",
                    f"fine-tune operation {command.operation_id} already names another request",
                )
            return intent
        intent = FineTuneJobIntent(
            command=command,
            subject_id=self.subject_id,
            request_sha256=request_sha256,
            queued_at=self._clock(),
            catalog_reference=catalog_reference,
        )
        write_recording_evidence(path, intent, immutable=True)
        return intent

    def require_runtime(self) -> None:
        """Refuse when no runtime is configured or it has no ``bin/python`` (one ``stat``).

        Raises: ``FineTuneJobError`` FINETUNE.RUNTIME_UNAVAILABLE.
        """
        if self.runtime_root is None:
            raise FineTuneJobError(
                "FINETUNE.RUNTIME_UNAVAILABLE",
                "fine-tuning is not set up: this app was built without an F5 runtime "
                "(finetune_runtime_root)",
            )
        python = self.runtime_root / "bin" / "python"
        if not python.is_file():
            raise FineTuneJobError(
                "FINETUNE.RUNTIME_UNAVAILABLE",
                f"no F5 runtime python at {python}; provision it with "
                "`voice-lab finetune provision-runtime`",
            )

    def submit(
        self,
        command: FineTuneCommand,
        *,
        catalog_reference: CatalogReference | None = None,
        admit: Callable[[], None] | None = None,
    ) -> FineTuneJob:
        """Accept ``command`` as the queued job, or replay the job it already names.

        Metadata only: the runtime check is one ``stat`` and the disk check one ``statvfs``;
        the library and the runtime's packages are never read here.
        Params: ``catalog_reference`` is the catalog version the service pinned, recorded in
        the intent so the job trains on exactly it (``None``: the subject's active version
        when the job starts); ``admit`` is called, under the queue's lock, before a new job
        is accepted (not for a replay) and refuses it by raising (the supervisor refuses
        while a Speak request holds the GPU).
        Returns: the new queued job, or the queued, running or terminal job of the same
        operation (an identical resubmit).
        Raises: ``FineTuneJobError`` with RECORDING.INVALID_MESSAGE (the operation already
        names another request), FINETUNE.JOB_ACTIVE (another job is queued or running;
        retryable), FINETUNE.RUNTIME_UNAVAILABLE (no runtime, or no ``bin/python`` in it),
        RECORDING.DISK_SPACE (less than ``FINETUNE_MIN_FREE_BYTES`` free; retryable) or
        RECORDING.GATE_NOT_READY (the queue is closed). Nothing is written for a refusal.
        """
        request_sha256 = canonical_sha256(command)

        def same_request(job: FineTuneJob) -> FineTuneJob:
            if job.request_sha256 != request_sha256:
                raise FineTuneJobError(
                    "RECORDING.INVALID_MESSAGE",
                    f"fine-tune operation {command.operation_id} already names another request",
                )
            return job

        with self._lock():
            terminal = self._terminal(command.operation_id)
            if terminal is not None:
                return same_request(terminal)
            active = self._head().job
            if active is not None:
                if active.operation_id == command.operation_id:
                    return same_request(active)
                raise FineTuneJobError(
                    "FINETUNE.JOB_ACTIVE",
                    f"fine-tune {active.operation_id} is already {active.state}; wait for it "
                    "to finish or cancel it",
                    retryable=True,
                )
            if admit is not None:
                admit()
            self.require_runtime()
            free = self._free_bytes(self.layout.root)
            if free < FINETUNE_MIN_FREE_BYTES:
                raise FineTuneJobError(
                    "RECORDING.DISK_SPACE",
                    f"fine-tuning needs {_gibibytes(FINETUNE_MIN_FREE_BYTES)} free on the "
                    f"library's disk; {_gibibytes(free)} is free",
                    retryable=True,
                )
            intent = self._new_intent(command, request_sha256, catalog_reference)
            job = FineTuneJob(
                operation_id=command.operation_id,
                request_sha256=request_sha256,
                queued_at=intent.queued_at,
            )
            self._write_head(job)
            return job

    def active_job(self) -> FineTuneJob | None:
        """The queued or running job, if any."""
        with self._lock():
            return self._head().job

    def cancel_requested(self, operation_id: str) -> bool:
        """Whether this process has been asked to cancel the running ``operation_id``."""
        with self._mutex:
            return operation_id in self._cancelling

    def request_cancel(self, operation_id: str) -> FineTuneJob:
        """Cancel ``operation_id``: at once when queued; by the supervisor when running.

        Returns: the cancelled job (it was queued), the running job now marked
        ``cancel_requested`` (the supervisor stops its group and publishes the cancelled
        state), or the job's terminal state when it has already ended.
        Raises: ``FineTuneJobError`` RECORDING.INVALID_TRANSITION when no such job exists.
        """
        with self._lock():
            terminal = self._terminal(operation_id)
            if terminal is not None:
                return terminal
            job = self._head().job
            if job is None or job.operation_id != operation_id:
                raise FineTuneJobError(
                    "RECORDING.INVALID_TRANSITION",
                    f"no fine-tune {operation_id} is queued or running",
                )
            if job.state == "queued":
                return self._complete(
                    FineTuneJob.model_validate(
                        job.model_dump() | {"state": "cancelled", "finished_at": self._clock()}
                    )
                )
            if not job.cancel_requested:
                job = FineTuneJob.model_validate(job.model_dump() | {"cancel_requested": True})
                self._write_head(job)
            self._cancelling.add(operation_id)
            self.wake.set()
            return job

    def claim(self) -> tuple[FineTuneJob, FineTuneJobIntent] | None:
        """Start the queued job: choose its run id and mark it running.

        The run id is ``finetune-<UTC time>`` (``-2``, ``-3``, ... when that run directory
        exists). Returns ``None`` when nothing is queued.
        Raises: ``SecurityError`` when the head names a running job (only ``recover`` may
        resolve one) or the queued job's intent changed.
        """
        with self._lock():
            job = self._head().job
            if job is None:
                self.wake.clear()
                return None
            if job.state != "queued":
                raise SecurityError("the fine-tune queue head names a running job no worker owns")
            intent = self.intent(job.operation_id)
            if intent.request_sha256 != job.request_sha256:
                raise SecurityError("the queued fine-tune intent changed")
            started = self._clock()
            base = started.strftime("finetune-%Y%m%dt%H%M%Sz")
            run_id, suffix = base, 1
            while os.path.lexists(self.layout.root / FINETUNE_OUTPUT_ROOT / run_id):
                suffix += 1
                run_id = f"{base}-{suffix}"
            job = FineTuneJob.model_validate(
                job.model_dump() | {"state": "running", "run_id": run_id, "started_at": started}
            )
            self._write_head(job)
            return job, intent

    def _update_running(self, operation_id: str, **changes: object) -> FineTuneJob:
        with self._lock():
            job = self._active(operation_id)
            if job.state != "running":
                raise SecurityError(f"fine-tune job {operation_id} is not running")
            job = FineTuneJob.model_validate(job.model_dump() | changes)
            self._write_head(job)
            return job

    def record_worker(self, operation_id: str, pid: int) -> FineTuneJob:
        """Record the running job's worker process (the leader of its process group)."""
        return self._update_running(operation_id, worker_pid=pid)

    def record_runtime_link(self, operation_id: str, link: str) -> FineTuneJob:
        """Record the runtime's ``ckpts/<run id>`` path the running job may create."""
        return self._update_running(operation_id, runtime_link=link)

    def _complete(self, job: FineTuneJob) -> FineTuneJob:
        # Terminal evidence is synced before the active pointer is cleared; recover()
        # finishes a restart that falls between the two.
        write_recording_evidence(self._terminal_path(job.operation_id), job, immutable=True)
        self._write_head(None)
        self._cancelling.discard(job.operation_id)
        return job

    def finish(
        self,
        operation_id: str,
        state: TerminalFineTuneState,
        error: RecordingError | None = None,
    ) -> FineTuneJob:
        """End the running ``operation_id`` as ``state`` and record it durably.

        A cancelled or failed job's ``run.json`` is rewritten as failed (``"cancelled by the
        operator"`` or the error's message) when it still says running, and the runtime's
        ``ckpts/<run id>`` link is released, even when the rewrite failed; both happen before
        the queue lock is taken. When either cannot be done the job ends failed and its error
        names the first failure. A run whose training had already finished (it was stopped
        while writing samples) keeps its finished ``run.json``: its model exists without
        listening samples, and the job says so (a failed job in its error, a cancelled job in
        its ``message``).
        Returns: the terminal job.
        Raises: ``SecurityError`` when ``operation_id`` is not the running job;
        ``FineTuneJobError`` RECORDING.GATE_NOT_READY when the queue is closed; ``OSError``
        when the terminal record cannot be written.
        """
        with self._lock():
            job = self._active(operation_id)
        if job.state != "running":
            raise SecurityError(f"fine-tune job {operation_id} is not running")
        note: str | None = None
        if state != "finished" and job.run_root is not None:
            reason = CANCELLED_BY_OPERATOR if error is None else error.message
            failure: RecordingError | None = None
            record = None
            try:
                record = mark_finetune_run_failed(
                    self.layout, job.run_root, reason, clock=self._clock
                )
            except FineTuneError as problem:
                failure = _run_record_error(problem.code, problem)
            except (SecurityError, OSError, ValueError) as problem:
                failure = _run_record_error("RECORDING.WRITE_FAILED", problem)
            if job.runtime_link is not None:
                try:
                    release_finetune_runtime_link(
                        Path(job.runtime_link),
                        self.layout.root / job.run_root / "checkpoints",
                    )
                except OSError as problem:
                    if failure is None:
                        failure = RecordingError(
                            code="RECORDING.WRITE_FAILED",
                            message=_bounded_message(
                                f"the fine-tune stopped but the runtime link {job.runtime_link} "
                                f"was not released: {problem}"
                            ),
                            retryable=False,
                        )
            if failure is not None:
                state, error = "failed", failure
            elif record is not None and record.status == "finished":
                trained = (
                    f"the model {job.run_id} was trained, but its listening samples are "
                    "missing or incomplete"
                )
                if error is None:
                    note = f"Fine-tune cancelled after training: {trained}"
                else:
                    error = RecordingError(
                        code=error.code,
                        message=_bounded_message(f"{trained}: {error.message}"),
                        details=error.details,
                        retryable=error.retryable,
                    )
        with self._lock():
            current = self._active(operation_id)
            ended = FineTuneJob.model_validate(
                current.model_dump()
                | {
                    "state": state,
                    "cancel_requested": False,
                    "finished_at": self._clock(),
                    "error": None if error is None else error.model_dump(),
                    "message": note if state == "cancelled" else None,
                }
            )
            return self._complete(ended)

    def recover(
        self,
        stop_worker: Callable[[FineTuneJob], None],
        *,
        recovering: Callable[[FineTuneJob], None] | None = None,
    ) -> FineTuneJob | None:
        """Resolve the job a previous service left behind; a running job is never resumed.

        A job whose terminal record exists leaves the head. A running job is first handed to
        ``recovering`` (the slow part follows, and nothing holds the queue lock meanwhile),
        then its worker group to ``stop_worker`` (which kills it only when it is verifiably
        this job's); the job then ends ``cancelled`` when its cancellation was requested and
        otherwise ``failed`` with ``FINETUNE.INTERRUPTED``, its ``run.json`` rewritten
        (``finish``). A queued job stays queued and starts normally.
        Returns: the job ended here, or ``None``.
        Raises: ``SecurityError`` when a terminal record binds another request; as ``finish``.
        """
        with self._lock():
            job = self._head().job
            if job is None:
                return None
            terminal = self._terminal(job.operation_id)
            if terminal is not None:
                if terminal.request_sha256 != job.request_sha256:
                    raise SecurityError("terminal fine-tune evidence binds another request")
                self._write_head(None)
                return None
            if job.state == "queued":
                return None
        if recovering is not None:
            recovering(job)
        if job.worker_pid is not None:
            stop_worker(job)
        if job.cancel_requested:
            return self.finish(job.operation_id, "cancelled")
        return self.finish(
            job.operation_id,
            "failed",
            RecordingError(
                code="FINETUNE.INTERRUPTED", message=INTERRUPTED_BY_RESTART, retryable=True
            ),
        )

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                self._closed = True
                os.close(self._owner)


def active_finetune_job(layout: AssetLayout) -> FineTuneJob | None:
    """The recording app's queued or running fine-tune job, read without any lock or write.

    ``voice-lab finetune run``, ``train`` and ``synthesize`` use it (with
    ``active_speech_request``) to refuse GPU work while the app's service holds a job, so the
    two never train at once.
    Returns: the job in ``provenance/finetune-jobs/head.json``, or ``None`` when there is no
    head or it names no job.
    Raises: ``SecurityError`` for an unsafe path or head file; ``ContractError`` for an
    invalid head.
    """
    path = confined_path(layout.root, f"{FINETUNE_JOBS_ROOT}/head.json")
    if not os.path.lexists(path):
        return None
    return read_recording_evidence(path, FineTuneJobHead).job


SPEECH_ACTIVE_MARKER: Final = f"{FINETUNE_JOBS_ROOT}/speech-active.json"


class SpeechActiveMarker(ContractModel):
    """The Speak request the recording service is running, for command-line training to see.

    Written (0600) at ``provenance/finetune-jobs/speech-active.json`` just before the
    request's worker starts and removed once the worker's whole process group is gone.
    ``pid`` is the recording service's process: a marker whose service is gone is stale.
    """

    schema_version: Literal["cole-voice-lab.finetune-speech-active/v1"] = (
        "cole-voice-lab.finetune-speech-active/v1"
    )
    operation_id: StableId
    pid: int = Field(ge=1)
    started_at: UtcTimestamp


def _process_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (one owned by another user counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_speech_marker(layout: AssetLayout) -> SpeechActiveMarker | None:
    path = confined_path(layout.root, SPEECH_ACTIVE_MARKER)
    if not os.path.lexists(path):
        return None
    return read_recording_evidence(path, SpeechActiveMarker)


def active_speech_request(layout: AssetLayout) -> SpeechActiveMarker | None:
    """The Speak request the recording app's service is running, read without a lock or write.

    ``voice-lab finetune run``, ``train`` and ``synthesize`` use it, with
    ``active_finetune_job``, to refuse GPU work while the app speaks.
    Returns: the marker when it exists and its service process is alive; ``None`` when there
    is none or it is stale (its service is gone).
    Raises: ``SecurityError`` for an unsafe path or marker file; ``ContractError`` for an
    invalid marker.
    """
    marker = _read_speech_marker(layout)
    if marker is None or not _process_alive(marker.pid):
        return None
    return marker


def _remove_evidence(path: Path) -> None:
    """Remove one evidence file (absent is fine) and sync its directory."""
    path.unlink(missing_ok=True)
    sync_recording_evidence_directory(path.parent)


class FineTuneSupervisionError(FineTuneJobError):
    """Supervision cannot start or continue; its error is published as the supervision failure."""


def _latest_run_error(code: ErrorCode, failure: BaseException) -> RecordingError:
    return RecordingError(
        code=code,
        message=_bounded_message(f"the latest fine-tuned model cannot be read: {failure}"),
        retryable=False,
    )


def _run_record_error(code: ErrorCode, failure: BaseException) -> RecordingError:
    return RecordingError(
        code=code,
        message=_bounded_message(
            f"the fine-tune stopped but its run record was not updated: {failure}"
        ),
        retryable=False,
    )


class FineTuneLogTail:
    """Reads what a run's ``train.log`` gained since the last read, at most its last 64 KiB.

    Only complete fragments are parsed (followed by a separator, or a status that ends with
    ``]``); the unfinished rest is kept for the next read.
    """

    def __init__(
        self,
        layout: AssetLayout,
        run_id: str,
        *,
        parse: Callable[[str], FineTuneTrainingProgress | None] = parse_training_progress,
    ) -> None:
        self.layout = layout
        self.relative_path = f"{FINETUNE_OUTPUT_ROOT}/{run_id}/train.log"
        self.parse = parse
        self._offset = 0
        self._carry = ""

    def read(self) -> FineTuneTrainingProgress | None:
        """Returns: the latest progress in the new text, or ``None``.
        Raises: ``SecurityError`` when the log or a directory above it is a symlink or the
        log is not a regular file.
        """
        path = confined_path(self.layout.root, self.relative_path)
        if not os.path.lexists(path):
            return None
        with open_regular_no_follow(path, nonblocking=True) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size < self._offset:
                self._offset, self._carry = 0, ""
            start = max(self._offset, size - _LOG_TAIL_BYTES)
            if start != self._offset:
                self._carry = ""
            stream.seek(start)
            data = stream.read(size - start)
        self._offset = start + len(data)
        text = self._carry + data.decode("utf-8", errors="replace")
        cut = max(text.rfind("\r"), text.rfind("\n"))
        complete, rest = text[: cut + 1], text[cut + 1 :]
        if rest.rstrip().endswith("]"):
            complete, rest = text, ""
        self._carry = rest[-_LOG_CARRY_CHARACTERS:]
        return self.parse(complete)


class FineTuneProgressTracker:
    """The live progress of one running job and the limit on how often it is published."""

    def __init__(
        self,
        operation_id: str,
        run_id: str,
        epochs: int,
        *,
        interval: float = FINETUNE_PROGRESS_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Params: ``epochs`` is the job's training length, reported as ``1 of epochs`` from
        the start of training until the trainer prints its first status."""
        self.epochs = epochs
        self.interval = interval
        self._clock = clock
        self.current = FineTuneProgressEvent(
            operation_id=operation_id, state="running", run_id=run_id
        )
        self._published: FineTuneProgressEvent | None = None
        self._published_at: float | None = None

    @property
    def training(self) -> bool:
        return self.current.stage == "training"

    def stage(self, stage: FineTuneStage) -> None:
        if stage == self.current.stage:
            return
        training = stage == "training"
        self.current = FineTuneProgressEvent(
            operation_id=self.current.operation_id,
            state="running",
            stage=stage,
            run_id=self.current.run_id,
            epoch=1 if training else None,
            epochs=self.epochs if training else None,
        )

    def training_progress(self, progress: FineTuneTrainingProgress) -> None:
        if not self.training:
            return
        self.current = FineTuneProgressEvent(
            operation_id=self.current.operation_id,
            state="running",
            stage="training",
            run_id=self.current.run_id,
            epoch=progress.epoch,
            epochs=progress.epochs,
            update=progress.update,
            loss=progress.loss,
            eta_seconds=progress.eta_seconds,
        )

    def due(self, *, force: bool = False) -> FineTuneProgressEvent | None:
        """The current progress when it changed and may be published now, else ``None``.

        At most one event per ``interval``; ``force`` publishes a change at once (the first
        event of a job). A change held back is returned by a later call.
        """
        if self.current == self._published:
            return None
        now = self._clock()
        if (
            not force
            and self._published_at is not None
            and now - self._published_at < self.interval
        ):
            return None
        self._published, self._published_at = self.current, now
        return self.current


@dataclass(frozen=True, slots=True)
class ProcessRow:
    pid: int
    pgid: int
    zombie: bool
    command: str


def process_table() -> tuple[ProcessRow, ...]:
    """Every process with its group, zombie state and full command line (``/bin/ps``).

    Raises: ``OSError``/``subprocess.SubprocessError`` when ``ps`` cannot run.
    """
    output = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,pgid=,stat=,command="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    rows: list[ProcessRow] = []
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        rows.append(
            ProcessRow(
                pid=int(parts[0]),
                pgid=int(parts[1]),
                zombie=parts[2].startswith("Z"),
                command=parts[3] if len(parts) == 4 else "",
            )
        )
    return tuple(rows)


def _signal_group(pgid: int, number: signal.Signals) -> None:
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        pass  # gone, or only zombies left: nothing left to signal


def worker_command_marker(layout: AssetLayout, operation_id: str) -> str:
    """The tail of a worker's command line, which recovery requires before killing a group."""
    return (
        f"-m {FINETUNE_WORKER_MODULE} --worker --asset-root {layout.root} "
        f"--operation-id {operation_id}"
    )


@dataclass(frozen=True, slots=True)
class _Conversation:
    """How the supervisor follows one worker: its limits, its frames and its idle work.

    ``worker`` names the worker in failures; ``read`` applies the frames in one chunk and
    returns the worker's result when it came; ``tick`` runs after every poll.
    """

    worker: str
    request: FineTuneWorkerRequest | SpeechWorkerRequest
    cancel_requested: Callable[[], bool]
    limit_seconds: float
    over_limit: Callable[[], RecordingError]
    stop_signal: signal.Signals
    read: Callable[[bytes], FineTuneWorkerReply | SpeechWorkerReply | None]
    tick: Callable[[], None]


@dataclass(slots=True)
class _SpeechJob:
    """One in-memory Speak request: its command and the state last published for it."""

    command: SynthesizeCommand
    request_sha256: str
    event: SynthesisEvent
    cancel_requested: bool = False


def _speech_event(
    command: SynthesizeCommand, state: SynthesisState, **fields: Any
) -> SynthesisEvent:
    return SynthesisEvent(
        operation_id=command.operation_id,
        state=state,
        model=command.model,
        text=command.text,
        **fields,
    )


def _speech_outcome(
    command: SynthesizeCommand, error: RecordingError | None, reply: SpeechWorkerReply | None
) -> SynthesisEvent:
    """A Speak request's final event: finished with ``reply``'s clip, failed with ``error``,
    or cancelled (no error and no clip), naming the resolution the reply carries.

    Raises: ``pydantic.ValidationError`` when the reply contradicts the request.
    """
    resolution = None if reply is None else reply.resolution
    fields: dict[str, Any] = (
        {}
        if resolution is None
        else {
            "run_id": resolution.run_id,
            "reference_run_id": resolution.reference_run_id,
            "reference_prompt_id": resolution.reference_prompt_id,
        }
    )
    clip = None if reply is None else reply.clip
    if error is None and clip is not None:
        return _speech_event(
            command,
            "finished",
            relative_path=clip.relative_path,
            sha256=clip.sha256,
            frames=clip.frames,
            sample_rate_hz=clip.sample_rate_hz,
            seconds=clip.seconds,
            **fields,
        )
    if error is not None:
        return _speech_event(command, "failed", error=error, **fields)
    return _speech_event(command, "cancelled", **fields)


class FineTuneSupervisor:
    """Runs the queue's jobs and Speak requests, one worker process at a time.

    The service calls ``submit``, ``cancel``, ``status``, ``synthesize`` and
    ``cancel_synthesis`` from its metadata executor; the supervisor thread recovers the job
    a previous service left running, then starts, watches and ends jobs and Speak requests.
    Both kinds share one GPU slot: a Speak request is refused while a fine-tune is queued or
    running or another request is active, and a fine-tune while a request is active.
    Progress goes to ``on_event``; a job's end (its run record and final state, a recovered
    job's included) goes to ``on_outcome``; both are keyed by the job's operation id. Speak
    requests live in memory only: every accepted request publishes ``queued``, ``running``
    and exactly one terminal ``SynthesisEvent`` to ``on_event``, keyed by its operation id.
    Once supervision has failed, every request is refused.
    """

    def __init__(
        self,
        queue: FineTuneJobQueue,
        *,
        on_event: Callable[[str, FineTuneJobEvent], None] | None = None,
        on_outcome: Callable[[str, FineTuneJobEvent], None] | None = None,
        on_failure: Callable[[RecordingError], None] | None = None,
        pretrained_pin: FineTunePretrainedPins | None = None,
        progress_interval: float = FINETUNE_PROGRESS_INTERVAL_SECONDS,
        cancel_grace: float = FINETUNE_CANCEL_GRACE_SECONDS,
        watchdog: float = FINETUNE_WATCHDOG_SECONDS,
        synthesis_deadline: float = SPEECH_DEADLINE_SECONDS,
        processes: Callable[[], Sequence[ProcessRow]] = process_table,
    ) -> None:
        """Params: ``on_outcome`` hears each job's run summary and final state (default:
        ``on_event``; the recording service keeps them until the app's handshake);
        ``pretrained_pin`` is a test seam: the pretrained-file pins the worker checks the
        runtime against (the recording service never sets it, so its workers use
        ``finetune_pretrained_pins()``); ``cancel_grace`` is how long a signalled group has
        before it is killed; ``synthesis_deadline`` is how long a Speak worker may run;
        ``processes`` lists processes for recovery's command-line check.
        """
        self.queue = queue
        self.on_event = on_event
        self.on_outcome = on_outcome
        self.on_failure = on_failure
        self.pretrained_pin = pretrained_pin
        self.progress_interval = progress_interval
        self.cancel_grace = cancel_grace
        self.watchdog = watchdog
        self.synthesis_deadline = synthesis_deadline
        self._processes = processes
        self._stop = threading.Event()
        self._mutex = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._tracker: FineTuneProgressTracker | None = None
        self._recovering: str | None = None
        # The GPU slot: the active Speak request, the ended ones (for replay) and the digests
        # Speak workers verified. Taken before the queue's lock, never after it.
        self._speech_lock = threading.Lock()
        self._speech: _SpeechJob | None = None
        self._speech_ended: OrderedDict[str, _SpeechJob] = OrderedDict()
        self._digests: OrderedDict[str, FineTuneVerifiedDigest] = OrderedDict()
        self._probe: FineTuneRuntimeProbe | None = None
        # Status replies reuse the summaries of finished runs whose records are unchanged.
        self._summaries = FineTuneSummaryCache()
        self.fatal_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="recording-finetune-supervisor"
        )

    @property
    def worker_pid(self) -> int | None:
        with self._mutex:
            return None if self._process is None else self._process.pid

    @property
    def trusted_digests(self) -> FineTuneTrustedDigests:
        """The digests and runtime probe the next Speak worker may reuse."""
        with self._speech_lock:
            return FineTuneTrustedDigests(files=tuple(self._digests.values()), probe=self._probe)

    def start(self) -> None:
        self._thread.start()

    # -- service requests (metadata executor) --------------------------------------------

    def _active_event(self, job: FineTuneJob) -> FineTuneProgressEvent:
        with self._mutex:
            tracker = self._tracker
            recovering = self._recovering == job.operation_id
        if recovering:
            event = finetune_job_event(job)
            return FineTuneProgressEvent.model_validate(
                event.model_dump() | {"message": RECOVERING_MESSAGE}
            )
        if tracker is None or tracker.current.operation_id != job.operation_id:
            return finetune_job_event(job)
        live = tracker.current
        if job.cancel_requested:
            return live.model_copy(update={"message": CANCELLING_MESSAGE})
        return live

    def _require_supervising(self) -> None:
        if self.fatal_error is not None:
            raise FineTuneJobError("RECORDING.GATE_NOT_READY", SUPERVISION_STOPPED)

    def _refuse_while_speaking(self) -> None:
        """Called by the queue before it accepts a new job, with ``_speech_lock`` held."""
        if self._speech is not None:
            raise FineTuneJobError(
                "FINETUNE.JOB_ACTIVE",
                f"Speak request {self._speech.command.operation_id} is "
                f"{self._speech.event.state}; fine-tune once it has finished or been cancelled",
                retryable=True,
            )

    def submit(
        self, command: FineTuneCommand, *, catalog_reference: CatalogReference | None = None
    ) -> FineTuneProgressEvent:
        """Queue ``command`` (``FineTuneJobQueue.submit``, with the service's pinned
        ``catalog_reference``) and return its state.

        Raises: ``FineTuneJobError`` RECORDING.GATE_NOT_READY once supervision has stopped;
        FINETUNE.JOB_ACTIVE (retryable) while a Speak request is queued or running; as
        ``FineTuneJobQueue.submit``.
        """
        self._require_supervising()
        with self._speech_lock:
            job = self.queue.submit(
                command, catalog_reference=catalog_reference, admit=self._refuse_while_speaking
            )
        return self._active_event(job) if job.state == "running" else finetune_job_event(job)

    def cancel(self, command: CancelFineTuneCommand) -> FineTuneProgressEvent:
        """Cancel ``command.cancelled_operation_id`` (``FineTuneJobQueue.request_cancel``).

        Returns: the cancelled state for a queued job; for a running job its live progress
        with the message ``Cancelling the fine-tune`` (the ``cancelled`` event follows once
        its process group is gone); an ended job's terminal state.
        Raises: ``FineTuneJobError`` RECORDING.GATE_NOT_READY once supervision has stopped;
        as ``FineTuneJobQueue.request_cancel``.
        """
        self._require_supervising()
        job = self.queue.request_cancel(command.cancelled_operation_id)
        return self._active_event(job) if job.state == "running" else finetune_job_event(job)

    def status(self, command: FineTuneStatusCommand) -> FineTuneStatusEvent:
        """Whether a runtime is configured, the active job's live progress and the finished runs.

        Never waits for recovery: a job being recovered is reported running with the message
        ``Checking the last fine-tune``. Finished runs of the families Speak renders
        (``finetune.SPEAK_FAMILIES``, F5-TTS; ``finished_finetune_runs``, newest first; the
        latest is the first; a run whose records are unchanged since the last reply is not
        read again) that cannot be resolved are reported as
        ``latest_run_error`` (its ``FineTuneError`` code; an unsafe path as
        RECORDING.SOURCE_CHANGED, a file-system failure as RECORDING.WRITE_FAILED), so
        ``active`` is always answered and a new job can still start.
        Raises: ``FineTuneJobError`` RECORDING.GATE_NOT_READY once supervision has stopped;
        RECORDING.GATE_NOT_READY when the queue is closed.
        """
        self._require_supervising()
        job = self.queue.active_job()
        active = None
        if job is not None:
            active = self._active_event(job) if job.state == "running" else finetune_job_event(job)
        runs: tuple[FineTuneRunSummary, ...] = ()
        latest_run_error: RecordingError | None = None
        try:
            # The app's runs are Speak's choices: F5-TTS's, as Speak's latest and base.
            runs = finished_finetune_runs(
                self.queue.layout, summaries=self._summaries, families=SPEAK_FAMILIES
            )
        except FineTuneError as failure:
            latest_run_error = _latest_run_error(failure.code, failure)
        except (SecurityError, ValueError) as failure:
            latest_run_error = _latest_run_error("RECORDING.SOURCE_CHANGED", failure)
        except OSError as failure:
            latest_run_error = _latest_run_error("RECORDING.WRITE_FAILED", failure)
        return FineTuneStatusEvent(
            operation_id=command.operation_id,
            runtime_configured=self.queue.runtime_configured,
            active=active,
            latest_run=runs[0] if runs else None,
            finished_runs=runs,
            latest_run_error=latest_run_error,
        )

    def synthesize(self, command: SynthesizeCommand) -> SynthesisEvent:
        """Accept one Speak request (in memory; the supervisor thread runs it).

        Metadata only: the GPU slot, the runtime's ``bin/python``, whether any run has
        finished (``run.json`` files only) and whether the request's output directory
        already exists. Nothing is probed or hashed here; the worker resolves the model and
        reference. The queued event is also published, keyed by the operation id.
        Returns: the queued state, or the current state of the same request (an identical
        resubmit).
        Raises: ``FineTuneJobError`` RECORDING.GATE_NOT_READY once supervision has stopped or
        the queue is closed; RECORDING.INVALID_MESSAGE when the operation names another
        request; FINETUNE.JOB_ACTIVE (retryable) while a fine-tune is queued or running or
        another Speak request is active; FINETUNE.RUNTIME_UNAVAILABLE; FINETUNE.NO_FINISHED_RUN
        when no run has finished; FINETUNE.RUN_EXISTS when ``derived/speech/<operation>``
        exists. Nothing is written for a refusal.
        """
        self._require_supervising()
        request_sha256 = canonical_sha256(command)
        operation_id = command.operation_id
        with self._speech_lock:
            current = self._speech
            known = (
                current
                if current is not None and current.command.operation_id == operation_id
                else self._speech_ended.get(operation_id)
            )
            if known is not None:
                if known.request_sha256 != request_sha256:
                    raise FineTuneJobError(
                        "RECORDING.INVALID_MESSAGE",
                        f"Speak operation {operation_id} already names another request",
                    )
                return known.event
            if current is not None:
                raise FineTuneJobError(
                    "FINETUNE.JOB_ACTIVE",
                    f"Speak request {current.command.operation_id} is {current.event.state}; "
                    "wait for it to finish or cancel it",
                    retryable=True,
                )
            training = self.queue.active_job()
            if training is not None:
                raise FineTuneJobError(
                    "FINETUNE.JOB_ACTIVE",
                    f"fine-tune {training.operation_id} is {training.state}; Speak is "
                    "available once it has finished or been cancelled",
                    retryable=True,
                )
            self.queue.require_runtime()
            if self._no_finished_run():
                raise FineTuneJobError(
                    "FINETUNE.NO_FINISHED_RUN",
                    "no fine-tuned model has finished yet; fine-tune once before using Speak",
                )
            layout = self.queue.layout
            if os.path.lexists(confined_path(layout.root, speech_directory(operation_id))):
                raise FineTuneJobError(
                    "FINETUNE.RUN_EXISTS",
                    f"{speech_directory(operation_id)} already exists; each Speak request "
                    "needs a new operation id",
                )
            queued = _speech_event(command, "queued")
            self._speech = _SpeechJob(command, request_sha256, queued)
            self.queue.wake.set()
        # The supervisor thread may already have started it; this reply is still "queued".
        self._emit(queued)
        return queued

    def _no_finished_run(self) -> bool:
        """Whether the run records show that no run has finished (a cheap scan).

        Records that cannot be read are left to the worker, which resolves the request
        again and names the failure.
        """
        try:
            return not finished_finetune_run_ids(self.queue.layout, families=SPEAK_FAMILIES)
        except (FineTuneError, SecurityError, OSError):
            return False

    def cancel_synthesis(self, command: CancelSynthesisCommand) -> SynthesisEvent:
        """Cancel the Speak request ``command.cancelled_operation_id``.

        Returns: the cancelled state for a queued request (also published); the running
        state for a running one (its worker's group is killed and the ``cancelled`` event
        follows once it is gone); an ended request's final state.
        Raises: ``FineTuneJobError`` RECORDING.GATE_NOT_READY once supervision has stopped;
        RECORDING.INVALID_TRANSITION when this service has no such request.
        """
        self._require_supervising()
        operation_id = command.cancelled_operation_id
        with self._speech_lock:
            job = self._speech
            if job is None or job.command.operation_id != operation_id:
                ended = self._speech_ended.get(operation_id)
                if ended is None:
                    raise FineTuneJobError(
                        "RECORDING.INVALID_TRANSITION",
                        f"no Speak request {operation_id} is queued or running",
                    )
                return ended.event
            if job.event.state == "running":
                job.cancel_requested = True
                return job.event
            cancelled = _speech_event(job.command, "cancelled")
            self._end_speech_locked(job, cancelled)
        self._emit(cancelled)
        return cancelled

    def _end_speech_locked(self, job: _SpeechJob, event: SynthesisEvent) -> None:
        """Record ``job``'s final ``event`` (``_speech_lock`` held); the slot is free again."""
        job.event = event
        self._speech = None
        self._speech_ended[job.command.operation_id] = job
        while len(self._speech_ended) > MAX_ENDED_SPEECH:
            self._speech_ended.popitem(last=False)

    # -- supervision (supervisor thread) -------------------------------------------------

    def _emit(self, event: FineTuneJobEvent | None) -> None:
        if event is not None and self.on_event is not None:
            self.on_event(event.operation_id, event)

    def _emit_outcome(self, event: FineTuneJobEvent) -> None:
        publish = self.on_outcome or self.on_event
        if publish is not None:
            publish(event.operation_id, event)

    def _note_recovering(self, job: FineTuneJob) -> None:
        with self._mutex:
            self._recovering = job.operation_id

    def _recover(self) -> None:
        """End the job a previous service left running and publish its outcome."""
        try:
            ended = self.queue.recover(
                self._stop_recovered_worker, recovering=self._note_recovering
            )
        finally:
            with self._mutex:
                self._recovering = None
        if ended is not None:
            self._publish_outcome(ended)

    def _prepare_speech(self) -> None:
        """Clear what a stopped service left of its Speak requests, before any request runs.

        A marker whose service process is gone is removed (a live one is replaced by this
        service's next request). Request directories without ``speech.json`` are removed
        (``finetune.discard_unfinished_speech``).
        Raises: ``FineTuneSupervisionError`` RECORDING.SOURCE_CHANGED naming a damaged
        marker or what ``derived/speech`` holds that no Speak request writes;
        RECORDING.WRITE_FAILED when something cannot be read or removed.
        """
        layout = self.queue.layout
        try:
            try:
                marker = _read_speech_marker(layout)
            except (SecurityError, ContractError) as failure:
                raise FineTuneSupervisionError(
                    "RECORDING.SOURCE_CHANGED",
                    f"fine-tune supervision stopped: {SPEECH_ACTIVE_MARKER} is damaged "
                    f"({failure}); remove it, then restart the app",
                ) from failure
            if marker is not None and not _process_alive(marker.pid):
                _remove_evidence(confined_path(layout.root, SPEECH_ACTIVE_MARKER))
            discard_unfinished_speech(layout)
        except (FineTuneError, SecurityError) as failure:
            raise FineTuneSupervisionError(
                "RECORDING.SOURCE_CHANGED",
                f"fine-tune supervision stopped: {failure}, then restart the app",
            ) from failure
        except OSError as failure:
            raise FineTuneSupervisionError(
                "RECORDING.WRITE_FAILED",
                "fine-tune supervision stopped: unfinished Speak output could not be cleared "
                f"({failure}); restart the app",
            ) from failure

    def _run(self) -> None:
        try:
            self._prepare_speech()
            self._recover()
            while not self._stop.is_set():
                claimed = self.queue.claim()
                if claimed is not None:
                    self._run_job(*claimed)
                    continue
                speech = self._start_speech()
                if speech is not None:
                    self._run_speech(speech)
                    continue
                self.queue.wake.wait(_POLL_SECONDS)
        except BaseException as error:
            self.fatal_error = error
            if self.on_failure is not None:
                self.on_failure(
                    error.recording_error
                    if isinstance(error, FineTuneSupervisionError)
                    else RecordingError(
                        code="RECORDING.WRITE_FAILED",
                        message=SUPERVISION_FAILURE,
                        retryable=True,
                    )
                )

    def _live_members(self, pgid: int) -> list[ProcessRow]:
        return [row for row in self._processes() if row.pgid == pgid and not row.zombie]

    def _group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS answers EPERM for a group of zombies; only a live member counts.
            return bool(self._live_members(pgid))
        return True

    def _stop_recovered_worker(self, job: FineTuneJob) -> None:
        """Kill a previous service's worker group, only when it is verifiably this job's.

        A group whose leader (the recorded worker) is alive must have this module's worker
        command line for this asset root and operation; a group whose leader is gone must
        still run a command naming this job's run id (its trainer). Anything else keeps
        running: the pid now belongs to someone else.
        """
        pgid = job.worker_pid
        assert pgid is not None
        members = self._live_members(pgid)
        if not members:
            return
        leader = next((row for row in members if row.pid == pgid), None)
        if leader is not None:
            owned = leader.command.endswith(
                worker_command_marker(self.queue.layout, job.operation_id)
            )
        else:
            owned = job.run_id is not None and any(job.run_id in row.command for row in members)
        if not owned:
            return
        _signal_group(pgid, signal.SIGKILL)
        deadline = time.monotonic() + _GROUP_STOP_SECONDS
        while self._live_members(pgid):
            if time.monotonic() > deadline:
                raise FineTuneJobError(
                    "RECORDING.WRITE_FAILED",
                    f"the previous fine-tune worker group {pgid} did not stop",
                )
            time.sleep(0.05)

    def _worker_command(self, job: FineTuneJob | SynthesizeCommand) -> list[str]:
        return [
            sys.executable,
            "-m",
            FINETUNE_WORKER_MODULE,
            "--worker",
            "--asset-root",
            str(self.queue.layout.root),
            "--operation-id",
            job.operation_id,
        ]

    def _run_job(self, job: FineTuneJob, intent: FineTuneJobIntent) -> None:
        assert job.run_id is not None
        epochs = (
            intent.command.epochs
            or training_adapter(APP_TRAINING_FAMILY).hyperparameters({}).epochs
        )
        tracker = FineTuneProgressTracker(
            job.operation_id, job.run_id, epochs, interval=self.progress_interval
        )
        with self._mutex:
            self._tracker = tracker
        try:
            self._emit(tracker.due(force=True))
            outcome = self._supervise(job, tracker)
            if outcome is None:
                return  # the service is stopping: the job stays running for recovery
            self._end_job(job, outcome.state, outcome.error)
        finally:
            with self._mutex:
                self._tracker = None

    def _end_job(
        self, job: FineTuneJob, state: TerminalFineTuneState, error: RecordingError | None
    ) -> None:
        """Record the job's end, then publish its run (when it has one) and its final state.

        A worker that reports success must leave a finished, summarizable run; otherwise the
        job fails with the summary's error.
        """
        assert job.run_root is not None
        summary: FineTuneRunSummary | None = None
        if state == "finished":
            try:
                summary = finetune_run_summary(self.queue.layout, job.run_root)
                if summary.status != "finished":
                    raise FineTuneError(
                        "FINETUNE.TRAINING_FAILED",
                        f"the fine-tune worker reported success but {job.run_root} is "
                        f"{summary.status}",
                    )
            except FineTuneError as failure:
                state, summary = "failed", None
                error = RecordingError(
                    code=failure.code, message=_bounded_message(str(failure)), retryable=False
                )
        self._publish_outcome(self.queue.finish(job.operation_id, state, error), summary)

    def _publish_outcome(
        self, ended: FineTuneJob, summary: FineTuneRunSummary | None = None
    ) -> None:
        """Publish an ended job's run summary (when its ``run.json`` summarizes) and its state."""
        run_root = ended.run_root
        if (
            summary is None
            and run_root is not None
            and os.path.lexists(self.queue.layout.root / run_root / "run.json")
        ):
            try:
                summary = finetune_run_summary(self.queue.layout, run_root)
            except FineTuneError:
                summary = None  # the final state below still names the job's outcome
        if summary is not None:
            self._emit_outcome(FineTuneEvent(operation_id=ended.operation_id, run=summary))
        self._emit_outcome(finetune_job_event(ended))

    def _supervise(self, job: FineTuneJob, tracker: FineTuneProgressTracker) -> _Outcome | None:
        """Start ``job``'s worker and follow it until it replies, fails or is stopped.

        Returns: the outcome, or ``None`` when the service is stopping (the worker's group
        is killed either way).
        """
        run_id = job.run_id
        assert run_id is not None
        runtime_root = self.queue.runtime_root
        if runtime_root is None:
            return _Outcome(
                "failed",
                RecordingError(
                    code="FINETUNE.RUNTIME_UNAVAILABLE",
                    message="fine-tuning is not set up: this app was built without an F5 runtime",
                    retryable=False,
                ),
            )

        def converse(process: subprocess.Popen[bytes]) -> _Outcome | None:
            recorded = self.queue.record_worker(job.operation_id, process.pid)
            request = FineTuneWorkerRequest(
                generation=f"finetune-{uuid4().hex}",
                operation_id=recorded.operation_id,
                request_sha256=recorded.request_sha256,
                run_id=run_id,
                runtime_root=str(runtime_root),
                pretrained_pin=self.pretrained_pin,
            )
            return self._converse(process, recorded, request, tracker)

        return self._spawn(job, "fine-tune", converse)

    def _spawn(
        self,
        job: FineTuneJob | SynthesizeCommand,
        worker: str,
        converse: Callable[[subprocess.Popen[bytes]], _Outcome | None],
    ) -> _Outcome | None:
        """Start a worker in its own session, follow it with ``converse``, then reap its group.

        Returns: ``converse``'s outcome; a failed one (FINETUNE.INTERRUPTED) when the worker
        cannot start.
        Raises: as ``_reap``.
        """
        try:
            process = subprocess.Popen(
                self._worker_command(job),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as failure:
            return _Outcome(
                "failed", _interrupted(f"the {worker} worker could not start: {failure}")
            )
        with self._mutex:
            self._process = process
        outcome: _Outcome | None = None
        try:
            outcome = converse(process)
            return outcome
        finally:
            try:
                self._reap(process, replied=outcome is not None and outcome.replied)
            finally:
                with self._mutex:
                    self._process = None

    def _reap(self, process: subprocess.Popen[bytes], *, replied: bool) -> None:
        """Close the worker's pipes and make sure its whole process group is gone.

        Raises: ``FineTuneJobError`` RECORDING.WRITE_FAILED when the group is still alive
        ``_GROUP_STOP_SECONDS`` after SIGKILL; supervision then stops, so no second job can
        start while the old group lives.
        """
        assert process.stdin is not None and process.stdout is not None
        try:
            if replied and process.returncode is None:
                # A worker that replied exits by itself; its stdout closes when it does.
                deadline = time.monotonic() + _REPLY_EXIT_SECONDS
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([process.stdout], [], [], _POLL_SECONDS)
                    if readable and not os.read(process.stdout.fileno(), 65_536):
                        break
            process.stdin.close()  # nothing is buffered: the request went out through os.write
            if process.returncode is None:
                # The leader is not reaped yet, so its group id cannot belong to anyone else.
                _signal_group(process.pid, signal.SIGKILL)
                process.wait()
            elif self._group_alive(process.pid):
                _signal_group(process.pid, signal.SIGKILL)
            deadline = time.monotonic() + _GROUP_STOP_SECONDS
            while self._group_alive(process.pid):
                if time.monotonic() > deadline:
                    raise _group_did_not_stop(process.pid)
                time.sleep(0.05)
        finally:
            process.stdout.close()

    def _converse(
        self,
        process: subprocess.Popen[bytes],
        job: FineTuneJob,
        request: FineTuneWorkerRequest,
        tracker: FineTuneProgressTracker,
    ) -> _Outcome | None:
        """Follow a fine-tune worker: stage frames, the training log, cancel and watchdog.

        A cancellation or the watchdog sends SIGTERM to the group and SIGKILL after
        ``cancel_grace``.
        """
        assert job.run_id is not None
        decoder = RecordingFrameDecoder()
        tail = FineTuneLogTail(self.queue.layout, job.run_id)

        def over_limit() -> RecordingError:
            return _interrupted(
                f"the fine-tune ran longer than {_duration_text(self.watchdog)} and was stopped"
            )

        def tick() -> None:
            if tracker.training:
                progress = tail.read()
                if progress is not None:
                    tracker.training_progress(progress)
            self._emit(tracker.due())

        return self._follow(
            process,
            _Conversation(
                worker="fine-tune",
                request=request,
                cancel_requested=lambda: self.queue.cancel_requested(job.operation_id),
                limit_seconds=self.watchdog,
                over_limit=over_limit,
                stop_signal=signal.SIGTERM,
                read=lambda chunk: self._frames(decoder, chunk, job, request, tracker),
                tick=tick,
            ),
        )

    def _follow(
        self, process: subprocess.Popen[bytes], conversation: _Conversation
    ) -> _Outcome | None:
        """Send the request, apply the worker's frames and stop its group on demand.

        A cancellation or the time limit sends ``conversation.stop_signal`` to the group and
        SIGKILL after ``cancel_grace``; a worker that will not take its request or sends an
        invalid frame is killed at once. Once SIGKILL was sent it is repeated until the group
        is gone. The outcome is returned once the group is gone.
        Returns: ``None`` when the service is stopping.
        Raises: ``FineTuneJobError`` RECORDING.WRITE_FAILED (as ``_reap``) when the group is
        still alive ``_KILL_WAIT_SECONDS`` after the first SIGKILL; supervision then stops.
        """
        assert process.stdin is not None and process.stdout is not None
        stdin, stdout = process.stdin.fileno(), process.stdout.fileno()
        os.set_blocking(stdin, False)
        os.set_blocking(stdout, False)
        pending = memoryview(recording_frame_bytes(conversation.request))
        started = time.monotonic()
        ending: _Outcome | None = None
        signalled_at = 0.0
        killed_at: float | None = None

        def stop(outcome: _Outcome, number: signal.Signals) -> None:
            nonlocal ending, signalled_at, killed_at
            ending, signalled_at = outcome, time.monotonic()
            if number == signal.SIGKILL:
                killed_at = signalled_at
            _signal_group(process.pid, number)

        while not self._stop.is_set():
            now = time.monotonic()
            if ending is None:
                if conversation.cancel_requested():
                    stop(_Outcome("cancelled"), conversation.stop_signal)
                elif now - started > conversation.limit_seconds:
                    stop(_Outcome("failed", conversation.over_limit()), conversation.stop_signal)
                elif pending and now - started > _SEND_DEADLINE_SECONDS:
                    message = f"the {conversation.worker} worker did not accept its request"
                    stop(_Outcome("failed", _interrupted(message)), signal.SIGKILL)
            if ending is not None:
                process.poll()
                if not self._group_alive(process.pid):
                    return ending
                if killed_at is None and now - signalled_at > self.cancel_grace:
                    killed_at = now
                if killed_at is not None:
                    if now - killed_at > _KILL_WAIT_SECONDS:
                        raise _group_did_not_stop(process.pid)
                    _signal_group(process.pid, signal.SIGKILL)
                self._stop.wait(0.05)
                continue
            readable, writable, _ = select.select(
                [stdout], [stdin] if pending else [], [], _POLL_SECONDS
            )
            if writable and pending:
                try:
                    pending = pending[os.write(stdin, pending) :]
                except BrokenPipeError:
                    pending = memoryview(b"")  # the worker is gone; its stdout says how
            if readable:
                chunk = os.read(stdout, 65_536)
                if not chunk:
                    status = process.wait()
                    return _Outcome(
                        "failed",
                        _interrupted(
                            f"the {conversation.worker} worker stopped without a result "
                            f"(exit status {status})"
                        ),
                    )
                try:
                    reply = conversation.read(chunk)
                except (VoiceLabError, ValueError) as failure:
                    invalid = RecordingError(
                        code="RECORDING.INVALID_MESSAGE",
                        message=_bounded_message(
                            f"the {conversation.worker} worker sent an invalid message: {failure}"
                        ),
                        retryable=False,
                    )
                    stop(_Outcome("failed", invalid), signal.SIGKILL)
                    continue
                if reply is not None:
                    return _Outcome(
                        "finished" if reply.error is None else "failed",
                        reply.error,
                        replied=True,
                        reply=reply,
                    )
            conversation.tick()
        return None

    def _frames(
        self,
        decoder: RecordingFrameDecoder,
        chunk: bytes,
        job: FineTuneJob,
        request: FineTuneWorkerRequest,
        tracker: FineTuneProgressTracker,
    ) -> FineTuneWorkerReply | None:
        """Apply the stage frames in ``chunk``; return the worker's result when it came.

        Raises: ``FineTuneJobError`` RECORDING.INVALID_MESSAGE for a request frame, a frame
        after the result, or one naming another generation, operation, request or run (or
        of another job kind); decoder and validation errors for malformed frames.
        """
        reply: FineTuneWorkerReply | None = None
        for payload in decoder.feed(chunk):
            message = _worker_frame(payload, request, after_result=reply is not None)
            if isinstance(message, FineTuneWorkerStage):
                tracker.stage(message.stage)
                if message.runtime_link is not None and message.runtime_link != job.runtime_link:
                    job = self.queue.record_runtime_link(job.operation_id, message.runtime_link)
                continue
            if (
                not isinstance(message, FineTuneWorkerReply)
                or message.request_sha256 != request.request_sha256
                or message.run_id != request.run_id
            ):
                raise FineTuneJobError(
                    "RECORDING.INVALID_MESSAGE", "the result names another request or run"
                )
            reply = message
        return reply

    # -- Speak requests (supervisor thread) ----------------------------------------------

    def _start_speech(self) -> _SpeechJob | None:
        """Mark the queued Speak request running and publish that; ``None`` when none waits.

        The request's marker (``SpeechActiveMarker``) is written first; a request whose
        marker cannot be written fails RECORDING.WRITE_FAILED without a worker.
        """
        layout = self.queue.layout
        failed: SynthesisEvent | None = None
        with self._speech_lock:
            job = self._speech
            if job is None or job.event.state != "queued":
                return None
            marker = SpeechActiveMarker(
                operation_id=job.command.operation_id, pid=os.getpid(), started_at=utc_now()
            )
            try:
                write_recording_evidence(confined_path(layout.root, SPEECH_ACTIVE_MARKER), marker)
            except (OSError, SecurityError, ContractError) as failure:
                failed = _speech_event(
                    job.command,
                    "failed",
                    error=RecordingError(
                        code="RECORDING.WRITE_FAILED",
                        message=_bounded_message(
                            f"Speak could not record that it is running: {failure}"
                        ),
                        retryable=True,
                    ),
                )
                self._end_speech_locked(job, failed)
            else:
                job.event = _speech_event(job.command, "running")
        if failed is not None:
            self._emit(failed)
            return None
        self._emit(job.event)
        return job

    def _run_speech(self, job: _SpeechJob) -> None:
        """Run one Speak request's worker and publish how it ended.

        A request that did not finish has its output directory removed (it created it; a
        request refused FINETUNE.RUN_EXISTS did not). When that removal fails the request
        ends failed with RECORDING.WRITE_FAILED. The service stopping ends nothing. Once the
        worker's group is gone the request's marker is removed, before the final event.
        Raises: as ``_spawn`` (the marker then stays, so command-line training keeps
        waiting); ``OSError`` when the marker cannot be removed (supervision stops).
        """
        outcome = self._supervise_speech(job)
        marker = confined_path(self.queue.layout.root, SPEECH_ACTIVE_MARKER)
        if outcome is None:
            _remove_evidence(marker)
            return  # the service is stopping; the request dies with it
        error = outcome.error
        if outcome.state != "finished" and (error is None or error.code != "FINETUNE.RUN_EXISTS"):
            try:
                discard_speech_output(self.queue.layout, job.command.operation_id)
            except (SecurityError, OSError) as failure:
                error = error or RecordingError(
                    code="RECORDING.WRITE_FAILED",
                    message=_bounded_message(
                        f"the Speak request stopped but its partial output was not removed: "
                        f"{failure}"
                    ),
                    retryable=False,
                )
        reply = outcome.reply
        event = _speech_outcome(
            job.command, error, reply if isinstance(reply, SpeechWorkerReply) else None
        )
        with self._speech_lock:
            _remove_evidence(marker)
            self._end_speech_locked(job, event)
        self._emit(event)

    def _supervise_speech(self, job: _SpeechJob) -> _Outcome | None:
        """Start the Speak worker and follow it: one result, a deadline, cancel by SIGKILL."""
        runtime_root = self.queue.runtime_root
        assert runtime_root is not None  # synthesize() refuses a service without a runtime
        command = job.command
        request = SpeechWorkerRequest(
            generation=f"speech-{uuid4().hex}",
            operation_id=command.operation_id,
            request_sha256=job.request_sha256,
            command=command,
            runtime_root=str(runtime_root),
            pretrained_pin=self.pretrained_pin,
            trusted=self.trusted_digests,
        )
        decoder = RecordingFrameDecoder()

        def cancel_requested() -> bool:
            with self._speech_lock:
                return job.cancel_requested

        def over_limit() -> RecordingError:
            return RecordingError(
                code="FINETUNE.SYNTHESIS_DEADLINE",
                message=_bounded_message(
                    f"speech synthesis took longer than {_duration_text(self.synthesis_deadline)}"
                    " and was stopped; try a shorter text"
                ),
                retryable=True,
            )

        def read(chunk: bytes) -> SpeechWorkerReply | None:
            reply = self._speech_frames(decoder, chunk, request)
            if reply is not None:
                clip = reply.clip
                if clip is not None and (
                    clip.text != command.text or clip.nfe_steps != command.nfe_steps
                ):
                    raise FineTuneJobError(
                        "RECORDING.INVALID_MESSAGE", "the clip was spoken for another request"
                    )
                _speech_outcome(command, reply.error, reply)  # refuses an inconsistent result
                self._remember_digests(reply.trusted)
            return reply

        def converse(process: subprocess.Popen[bytes]) -> _Outcome | None:
            return self._follow(
                process,
                _Conversation(
                    worker="speech",
                    request=request,
                    cancel_requested=cancel_requested,
                    limit_seconds=self.synthesis_deadline,
                    over_limit=over_limit,
                    stop_signal=signal.SIGKILL,
                    read=read,
                    tick=lambda: None,
                ),
            )

        return self._spawn(command, "speech", converse)

    def _speech_frames(
        self, decoder: RecordingFrameDecoder, chunk: bytes, request: SpeechWorkerRequest
    ) -> SpeechWorkerReply | None:
        """The Speak worker's result, once ``chunk`` completes it.

        Raises: ``FineTuneJobError`` RECORDING.INVALID_MESSAGE for any other frame, a frame
        after the result, or a result naming another generation, operation or request;
        decoder and validation errors for malformed frames.
        """
        reply: SpeechWorkerReply | None = None
        for payload in decoder.feed(chunk):
            message = _worker_frame(payload, request, after_result=reply is not None)
            if (
                not isinstance(message, SpeechWorkerReply)
                or message.request_sha256 != request.request_sha256
            ):
                raise FineTuneJobError(
                    "RECORDING.INVALID_MESSAGE", "the result names another request"
                )
            reply = message
        return reply

    def _remember_digests(self, trusted: FineTuneTrustedDigests) -> None:
        """Keep what a Speak worker verified; newer entries replace older ones per file."""
        with self._speech_lock:
            for entry in trusted.files:
                self._digests.pop(entry.identity.path, None)
                self._digests[entry.identity.path] = entry
            while len(self._digests) > MAX_TRUSTED_DIGESTS:
                self._digests.popitem(last=False)
            if trusted.probe is not None:
                self._probe = trusted.probe

    def close(self) -> None:
        """Stop supervising: a running worker's group is killed and its job stays running,
        so the next service marks it interrupted (a Speak request is simply lost; its marker
        is removed once its group is gone, and its output when the next service starts).

        Raises: ``FineTuneJobError`` RECORDING.WRITE_FAILED when the thread does not stop.
        """
        self._stop.set()
        self.queue.wake.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                raise FineTuneJobError(
                    "RECORDING.WRITE_FAILED", "the fine-tune supervisor did not stop in time"
                )


def _worker_frame(
    payload: bytes,
    request: FineTuneWorkerRequest | SpeechWorkerRequest,
    *,
    after_result: bool,
) -> FineTuneWorkerStage | FineTuneWorkerReply | SpeechWorkerReply:
    """One frame from ``request``'s worker.

    Raises: ``FineTuneJobError`` RECORDING.INVALID_MESSAGE for a request frame, any frame
    ``after_result``, or a frame naming another generation or operation; as
    ``decode_finetune_worker_message``.
    """
    message = decode_finetune_worker_message(payload)
    if isinstance(message, (FineTuneWorkerRequest, SpeechWorkerRequest)) or after_result:
        raise FineTuneJobError("RECORDING.INVALID_MESSAGE", "the worker sent a frame out of turn")
    if message.generation != request.generation or message.operation_id != request.operation_id:
        raise FineTuneJobError(
            "RECORDING.INVALID_MESSAGE", "the frame belongs to another worker or job"
        )
    return message


def _catalog_failure(failure: CorpusStoreError, subject_id: str) -> RecordingError:
    """A corpus-store failure as the recording service names it.

    A missing or altered snapshot (the active catalog's or a take's group's) is
    CORPUS.SNAPSHOT_MISMATCH with the store's own message, which names the snapshot.
    """
    if isinstance(failure, CorpusSnapshotSecurityError):
        return RecordingError(
            code="CORPUS.SNAPSHOT_MISMATCH",
            message=_bounded_message(
                f"a catalog snapshot fine-tuning needs is unusable: {failure}"
            ),
            retryable=False,
        )
    service_error: RecordingServiceError = _active_catalog_failure(failure, subject_id)
    return service_error.recording_error


def _worker_failure(failure: Exception, what: str, subject_id: str) -> RecordingError:
    """A worker's failure as the recording service names it.

    A ``FineTuneJobError`` keeps its error and a ``FineTuneError`` its code; a corpus-store
    failure is named as the service names it (``CORPUS.*`` or RECORDING.GATE_NOT_READY),
    changed or unsafe evidence is RECORDING.SOURCE_CHANGED, a file-system failure
    RECORDING.WRITE_FAILED (retryable), and anything else RECORDING.WRITE_FAILED.
    Params: ``what`` names the work in the message (``fine-tune``, ``speech``).
    """
    if isinstance(failure, FineTuneJobError):
        return failure.recording_error
    if isinstance(failure, FineTuneError):
        return RecordingError(
            code=failure.code, message=_bounded_message(str(failure)), retryable=False
        )
    if isinstance(failure, CorpusStoreError):
        return _catalog_failure(failure, subject_id)
    if isinstance(failure, (SecurityError, ContractError)):
        return RecordingError(
            code="RECORDING.SOURCE_CHANGED",
            message=_bounded_message(f"{what} evidence changed or is unsafe: {failure}"),
            retryable=False,
        )
    if isinstance(failure, OSError):
        return RecordingError(
            code="RECORDING.WRITE_FAILED",
            message=_bounded_message(f"{what} could not read or write its files: {failure}"),
            retryable=True,
        )
    return RecordingError(
        code="RECORDING.WRITE_FAILED",
        message=_bounded_message(
            f"{what} failed unexpectedly: {type(failure).__name__}: {failure}"
        ),
        retryable=False,
    )


def run_speech_worker_request(
    layout: AssetLayout, request: SpeechWorkerRequest
) -> SpeechWorkerReply:
    """Run one Speak request in this (worker) process and name its outcome.

    Resolves the model and reference (``finetune.resolve_speech_model``), then speaks the
    text (``finetune.synthesize_speech``) into ``derived/speech/<operation>/``, reusing
    ``request.trusted`` digests for unchanged files. Never opens the library's take store
    or corpus store.
    Returns: the reply: the clip, or the error (``_worker_failure``) with the resolution
    when the request got that far; either way what this request verified.
    """
    command = request.command
    digests = FineTuneDigestCache(request.trusted)
    resolution: SpeechResolution | None = None
    clip: SpeechClip | None = None
    error: RecordingError | None = None
    try:
        resolution = resolve_speech_model(
            layout, command.model, reference_prompt_id=command.reference_prompt_id
        )
        clip = synthesize_speech(
            layout,
            resolution,
            Path(request.runtime_root),
            operation_id=command.operation_id,
            text=command.text,
            nfe_steps=command.nfe_steps,
            pretrained_pin=request.pretrained_pin,
            digests=digests,
        )
    except Exception as failure:
        error = _worker_failure(failure, "speech", "")
    return SpeechWorkerReply(
        generation=request.generation,
        operation_id=request.operation_id,
        request_sha256=request.request_sha256,
        resolution=None
        if resolution is None
        else SpeechWorkerResolution(
            run_id=resolution.run_id,
            reference_run_id=resolution.reference_run.run_id,
            reference_prompt_id=resolution.reference.prompt_id,
        ),
        clip=clip,
        trusted=digests.trusted(),
        error=error,
    )


def run_finetune_worker_request(
    layout: AssetLayout,
    request: FineTuneWorkerRequest,
    *,
    emit: Callable[[FineTuneWorkerStage], None],
    metadata_retry_seconds: float = FINETUNE_METADATA_RETRY_SECONDS,
) -> FineTuneWorkerReply:
    """Run one job in this (worker) process and name its outcome.

    Checks the request against the job's durable intent, refuses an existing run directory
    (FINETUNE.RUN_EXISTS), reads the catalog snapshot the intent pins as a file (the corpus
    store is never opened; an intent without a pin reads the subject's active version, the
    store open only for that), then runs ``run_finetune_pipeline`` with the intent's epochs
    and ``allow_missing``. Each stage is
    passed to ``emit``. A selection that overlaps a metadata commit is retried for
    ``metadata_retry_seconds``.
    Returns: the reply; its ``error`` names any failure (``_worker_failure``).
    """
    subject_id = ""
    error: RecordingError | None = None
    try:
        intent = read_recording_evidence(
            confined_path(
                layout.root,
                f"{FINETUNE_JOBS_ROOT}/requests/{request.operation_id}.json",
                must_exist=True,
            ),
            FineTuneJobIntent,
        )
        subject_id = intent.subject_id
        if (
            intent.command.operation_id != request.operation_id
            or intent.request_sha256 != request.request_sha256
        ):
            raise FineTuneJobError(
                "RECORDING.SOURCE_CHANGED",
                "the fine-tune worker received a request that differs from its durable intent",
            )
        run_root = f"{FINETUNE_OUTPUT_ROOT}/{request.run_id}"
        if os.path.lexists(confined_path(layout.root, run_root)):
            raise FineTuneError("FINETUNE.RUN_EXISTS", f"{run_root} already exists")
        reference = intent.catalog_reference
        if reference is None:
            with open_catalog_reader(layout) as store:
                _, reference = store.active_catalog(intent.subject_id)
        snapshot = read_catalog_snapshot(layout, reference)
        # The app trains its family (its intent names none); the adapter makes its settings.
        # The worker's pin seam (tests only) is F5-TTS's, the app's family.
        adapter: TrainingAdapter = (
            training_adapter(APP_TRAINING_FAMILY)
            if request.pretrained_pin is None
            else F5TrainingAdapter(pretrained_pin=request.pretrained_pin)
        )
        hyperparameters = adapter.hyperparameters(
            {} if intent.command.epochs is None else {"epochs": intent.command.epochs}
        )

        def progress(step: FineTunePipelineStep) -> None:
            emit(
                FineTuneWorkerStage(
                    generation=request.generation,
                    operation_id=request.operation_id,
                    stage=step.stage,
                    runtime_link=None if step.runtime_link is None else str(step.runtime_link),
                )
            )

        deadline = time.monotonic() + metadata_retry_seconds
        while True:
            try:
                run_finetune_pipeline(
                    layout,
                    snapshot,
                    intent.subject_id,
                    Path(request.runtime_root),
                    hyperparameters,
                    run_id=request.run_id,
                    allow_missing=intent.command.allow_missing,
                    reference=reference,
                    adapter=adapter,
                    progress=progress,
                )
                break
            except FineTuneError as failure:
                busy = failure.code == "FINETUNE.CORPUS_PROJECTION_FAILED" and isinstance(
                    failure.__cause__, RecordingMetadataBusyError
                )
                if not busy or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)  # a Save is committing; selection wrote nothing yet
    except Exception as failure:
        error = _worker_failure(failure, "fine-tune", subject_id)
    return FineTuneWorkerReply(
        generation=request.generation,
        operation_id=request.operation_id,
        request_sha256=request.request_sha256,
        run_id=request.run_id,
        error=error,
    )


def _kill_own_group() -> None:  # pragma: no cover - kills this process (worker EOF test)
    os.killpg(os.getpgrp(), signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class _FinalFrame:
    payload: bytes


def _worker_main(
    layout: AssetLayout, operation_id: str, *, kill_group: Callable[[], None] = _kill_own_group
) -> int:
    """Serve one request on stdin/stdout; stdin ending means the service is gone.

    Returns: 0 once the reply is written, or 1 after ``kill_group`` returned (only a test's
    substitute returns; the real one kills this process).
    Raises: ``FineTuneJobError`` RECORDING.INVALID_MESSAGE for a second request or one for
    another operation, RECORDING.WRITE_FAILED when the run thread fails unexpectedly or
    the service stops reading; decoder and validation errors for malformed input.
    """
    input_fd, output_fd = sys.stdin.fileno(), sys.stdout.fileno()
    os.set_blocking(output_fd, False)
    decoder = RecordingFrameDecoder()
    outbox: queue.SimpleQueue[bytes | _FinalFrame | BaseException] = queue.SimpleQueue()
    output = bytearray()
    output_deadline = 0.0
    requested = False
    done = False

    def run(request: FineTuneWorkerRequest | SpeechWorkerRequest) -> None:
        try:
            reply: FineTuneWorkerReply | SpeechWorkerReply
            if isinstance(request, SpeechWorkerRequest):
                reply = run_speech_worker_request(layout, request)
            else:
                reply = run_finetune_worker_request(
                    layout, request, emit=lambda stage: outbox.put(recording_frame_bytes(stage))
                )
            outbox.put(_FinalFrame(recording_frame_bytes(reply)))
        except BaseException as failure:
            outbox.put(failure)

    while True:
        while True:
            try:
                item = outbox.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, BaseException):
                raise FineTuneJobError(
                    "RECORDING.WRITE_FAILED", "the fine-tune worker failed unexpectedly"
                ) from item
            if isinstance(item, _FinalFrame):
                done, item = True, item.payload
            if not output:
                output_deadline = time.monotonic() + _SEND_DEADLINE_SECONDS
            output.extend(item)
        if done and not output:
            return 0
        readable, writable, _ = select.select(
            [input_fd], [output_fd] if output else [], [], _POLL_SECONDS / 2
        )
        if readable:
            chunk = os.read(input_fd, 16_384)
            if not chunk:
                kill_group()
                return 1
            for payload in decoder.feed(chunk):
                message = decode_finetune_worker_message(payload)
                if (
                    not isinstance(message, (FineTuneWorkerRequest, SpeechWorkerRequest))
                    or requested
                    or message.operation_id != operation_id
                ):
                    raise FineTuneJobError(
                        "RECORDING.INVALID_MESSAGE",
                        "a fine-tune worker accepts exactly one request for its operation",
                    )
                requested = True
                threading.Thread(
                    target=run, args=(message,), daemon=True, name="finetune-pipeline"
                ).start()
        if writable:
            del output[: os.write(output_fd, output)]
            output_deadline = time.monotonic() + _SEND_DEADLINE_SECONDS
        if output and time.monotonic() > output_deadline:
            raise FineTuneJobError(
                "RECORDING.WRITE_FAILED", "the recording service stopped reading fine-tune results"
            )


RECORDING_FINETUNE_JOB_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "finetune-job": FineTuneJob,
    "finetune-job-head": FineTuneJobHead,
    "finetune-job-intent": FineTuneJobIntent,
    "finetune-speech-active": SpeechActiveMarker,
    "finetune-worker-reply": FineTuneWorkerReply,
    "finetune-worker-request": FineTuneWorkerRequest,
    "finetune-worker-stage": FineTuneWorkerStage,
    "finetune-speech-worker-request": SpeechWorkerRequest,
    "finetune-speech-worker-reply": SpeechWorkerReply,
}


def main(
    argv: Sequence[str] | None = None, *, kill_group: Callable[[], None] = _kill_own_group
) -> int:
    """The worker process: ``--worker --asset-root <root> --operation-id <op>``.

    It must lead its own process group (the supervisor starts it in a new session), because
    it kills that group whenever it stops other than by replying: when the service goes away
    (stdin ends) and after any failure, so no trainer or synthesizer it started outlives it.
    Everything it and its children create is private (umask 077).
    Params: ``kill_group`` stops the whole group, this process included (tests substitute it).
    Returns: 0 after its reply; after a failure it writes one JSON line on stderr naming the
    code (never the request) and, when it leads its group, kills that group; 2 when it does
    not lead its group (that group is its caller's and is left alone) or ``kill_group``
    returned.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    options = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    leader = os.getpgrp() == os.getpid()
    try:
        if not leader:
            raise FineTuneJobError(
                "RECORDING.INVALID_TRANSITION",
                "a fine-tune worker must lead its own process group",
            )
        operation_id: str = _STABLE_ID.validate_python(options.operation_id)
        return _worker_main(
            AssetLayout.validate(options.asset_root), operation_id, kill_group=kill_group
        )
    except BaseException as failure:
        code = (
            failure.recording_error.code
            if isinstance(failure, FineTuneJobError)
            else "RECORDING.WRITE_FAILED"
        )
        print(
            json.dumps(
                {
                    "service": "finetune-jobs",
                    "error_code": code,
                    "message": "fine-tune worker stopped",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        if leader:
            kill_group()
        return 2
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())

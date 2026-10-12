"""The speech jobs on disk, shared by the API and the job runner (docs/SPEECH_API.md §7).

@canonical speech.store -- ``state/speech/`` on the PC, owned by ``voice-lab``; directories
0700, files 0600. The API container writes a job (``jobs/<id>/request.json`` without the
text, ``input.json`` with it, ``status.json``) and then ``queue/<id>``, whose appearance
starts the network-less job runner (a systemd path unit). Ownership of a job is one file,
``jobs/<id>/claim``, created exclusively: the runner claims a job to render it, and a cancel
claims a queued job to end it, so exactly one of them owns it and only the owner writes its
state after that. A cancel that loses the race leaves ``jobs/<id>/cancel`` for the runner.

When a job ends its text is deleted, its state is final, and (with a ``callback_url``) a
webhook event is written to ``outbox/`` for the delivery process. ``sweep`` deletes audio
after its retention (the job then reads ``expired``) and whole job records after theirs.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Final, Literal

from ..api.contracts import ApiErrorCode
from ..canonical import (
    canonical_sha256,
    open_regular_no_follow,
    write_canonical_json,
    write_new_private_file,
)
from ..errors import VoiceLabError
from ..models import ContractModel, StableId
from .config import SpeechCaller, SpeechConfig
from .contracts import (
    JOB_ID_PATTERN,
    TERMINAL_STATES,
    WEBHOOK_EVENT_PREFIX,
    FailureCode,
    JobState,
    RenderedAudio,
    SpeechAudio,
    SpeechEvent,
    SpeechEventData,
    SpeechFailure,
    SpeechJobInput,
    SpeechJobRecord,
    SpeechJobState,
    SpeechJobStatus,
    SpeechLinks,
    SpeechRequest,
    WebhookOutboxEntry,
    event_type,
    milliseconds,
)

AUDIO_NAME: Final = "speech.wav"
PARTIAL_AUDIO_NAME: Final = "render.wav.part"
MAX_RECORD_BYTES: Final = 64 * 1024
# A job directory that never got its state (a crash inside ``submit``) is removed after this.
ABANDONED_SUBMIT: Final = timedelta(hours=1)


class SpeechRefusal(Exception):
    """A refusal the API answers with a stable code (and, for 503, ``Retry-After``)."""

    def __init__(
        self, status: int, code: ApiErrorCode, detail: str, *, retry_after: int | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code: ApiErrorCode = code
        self.detail = detail
        self.retry_after = retry_after


class _IdempotencyEntry(ContractModel):
    job_id: StableId


@dataclass(frozen=True, slots=True)
class Job:
    record: SpeechJobRecord
    state: SpeechJobState


def new_job_id() -> str:
    return f"sp-{secrets.token_hex(12)}"


def _read[ModelT: ContractModel](path: Path, model: type[ModelT]) -> ModelT:
    with open_regular_no_follow(path) as stream:
        return model.model_validate_json(stream.read(MAX_RECORD_BYTES))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SpeechStore:
    """The job directories under one ``state_root``."""

    def __init__(self, config: SpeechConfig) -> None:
        self.config = config
        self.root = Path(config.state_root)
        self.jobs_root = self.root / "jobs"
        self.queue_root = self.root / "queue"
        self.idempotency_root = self.root / "idempotency"
        self.outbox_root = self.root / "outbox"
        self.webhooks_root = self.root / "webhooks"

    def ensure(self) -> None:
        """Create the directories (0700); a link where one belongs is refused."""
        for directory in (
            self.root,
            self.jobs_root,
            self.queue_root,
            self.idempotency_root,
            self.outbox_root,
            self.webhooks_root,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = os.lstat(directory)
            if not stat.S_ISDIR(metadata.st_mode):
                raise SpeechRefusal(500, "internal_error", f"{directory} is not a directory")

    # -- paths -----------------------------------------------------------------------------

    def job_directory(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise SpeechRefusal(404, "speech_not_found", "there is no such speech job")
        return self.jobs_root / job_id

    def audio_path(self, job_id: str) -> Path:
        return self.job_directory(job_id) / AUDIO_NAME

    def partial_audio_path(self, job_id: str) -> Path:
        return self.job_directory(job_id) / PARTIAL_AUDIO_NAME

    # -- reading ---------------------------------------------------------------------------

    def load(self, job_id: str) -> Job | None:
        """The job, or ``None`` when it does not exist (or was never completely submitted)."""
        directory = self.job_directory(job_id)
        try:
            record = _read(directory / "request.json", SpeechJobRecord)
            state = _read(directory / "status.json", SpeechJobState)
        except (OSError, ValueError, VoiceLabError):  # missing, invalid, or a link
            return None
        if record.job_id != job_id or state.job_id != job_id:
            return None
        return Job(record, state)

    def for_caller(self, caller: SpeechCaller, job_id: str) -> Job:
        """The caller's own job; another caller's job is as absent as a missing one."""
        job = self.load(job_id)
        if job is None or job.record.caller_id != caller.caller_id:
            raise SpeechRefusal(404, "speech_not_found", "there is no such speech job")
        return job

    def jobs(self) -> Iterator[Job]:
        try:
            names = sorted(os.listdir(self.jobs_root))
        except FileNotFoundError:
            return
        for name in names:
            if JOB_ID_PATTERN.fullmatch(name):
                job = self.load(name)
                if job is not None:
                    yield job

    def read_input(self, job_id: str) -> SpeechJobInput:
        return _read(self.job_directory(job_id) / "input.json", SpeechJobInput)

    def queued(self) -> list[Job]:
        """Queued jobs, oldest first."""
        jobs = [job for job in self.jobs() if job.state.state == "queued"]
        return sorted(jobs, key=lambda job: (job.record.created_at, job.record.job_id))

    def cancel_requested(self, job_id: str) -> bool:
        return os.path.lexists(self.job_directory(job_id) / "cancel")

    # -- the API's writes --------------------------------------------------------------------

    @contextmanager
    def _submit_lock(self) -> Iterator[None]:
        descriptor = os.open(
            self.root / ".submit.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _idempotency_path(self, caller_id: str, key_sha256: str) -> Path:
        return self.idempotency_root / caller_id / f"{key_sha256}.json"

    def replay(self, caller: SpeechCaller, request: SpeechRequest) -> Job | None:
        """The job an earlier request with the same idempotency key made, if it still exists.

        Raises: ``SpeechRefusal`` 409 ``idempotency_conflict`` when that request differed.
        """
        if request.idempotency_key is None:
            return None
        path = self._idempotency_path(caller.caller_id, _sha256_text(request.idempotency_key))
        if not os.path.lexists(path):
            return None
        existing = self.load(_read(path, _IdempotencyEntry).job_id)
        if existing is None:
            return None
        if existing.record.request_sha256 != canonical_sha256(request):
            raise SpeechRefusal(
                409, "idempotency_conflict", "this idempotency key was used for a different request"
            )
        return existing

    def submit(
        self, caller: SpeechCaller, request: SpeechRequest, *, now: datetime
    ) -> tuple[Job, bool]:
        """Queue ``request`` for ``caller``; return the job and whether it is new.

        An idempotent replay (same key, same request) returns the existing job.
        Raises: ``SpeechRefusal`` 409 ``idempotency_conflict``, 429 ``too_many_jobs`` or
        ``daily_budget_exhausted``, 503 ``queue_full``.
        """
        request_sha256 = canonical_sha256(request)
        key_sha256 = (
            None if request.idempotency_key is None else _sha256_text(request.idempotency_key)
        )
        characters = len(request.text)
        with self._submit_lock():
            existing = self.replay(caller, request)
            if existing is not None:
                return existing, False
            active = 0
            today = 0
            queued = 0
            for job in self.jobs():
                if job.state.state == "queued":
                    queued += 1
                if job.record.caller_id != caller.caller_id:
                    continue
                if job.state.state in {"queued", "running"}:
                    active += 1
                if job.record.created_at.date() == now.date():
                    today += job.record.text_characters
            if active >= caller.max_queued_jobs:
                raise SpeechRefusal(
                    429, "too_many_jobs", f"at most {caller.max_queued_jobs} jobs at once"
                )
            if today + characters > caller.daily_character_budget:
                raise SpeechRefusal(
                    429,
                    "daily_budget_exhausted",
                    f"at most {caller.daily_character_budget} characters per UTC day",
                )
            if queued >= self.config.queue_limit:
                raise SpeechRefusal(503, "queue_full", "the speech queue is full", retry_after=60)
            job_id = new_job_id()
            record = SpeechJobRecord(
                job_id=job_id,
                caller_id=caller.caller_id,
                created_at=now,
                format="wav",
                callback_url=request.callback_url,
                client_ref=request.client_ref,
                idempotency_key_sha256=key_sha256,
                request_sha256=request_sha256,
                text_sha256=_sha256_text(request.text),
                text_characters=characters,
            )
            state = SpeechJobState(job_id=job_id, state="queued")
            directory = self.jobs_root / job_id
            directory.mkdir(mode=0o700)
            write_canonical_json(
                directory / "input.json", SpeechJobInput(job_id=job_id, text=request.text)
            )
            write_canonical_json(directory / "request.json", record)
            write_canonical_json(directory / "status.json", state)
            if key_sha256 is not None:
                path = self._idempotency_path(caller.caller_id, key_sha256)
                path.parent.mkdir(mode=0o700, exist_ok=True)
                write_canonical_json(path, _IdempotencyEntry(job_id=job_id))  # replaces a stale key
            write_new_private_file(self.queue_root / job_id, b"")
            return Job(record, state), True

    def cancel(self, caller: SpeechCaller, job_id: str, *, now: datetime) -> Job:
        """Cancel the caller's job: at once when queued, through the runner when running.

        Raises: ``SpeechRefusal`` 404 for a job that is not the caller's, 409
        ``already_finished`` for one that has ended.
        """
        job = self.for_caller(caller, job_id)
        if job.state.state in TERMINAL_STATES:
            raise SpeechRefusal(409, "already_finished", f"the job has ended: {job.state.state}")
        if self.claim(job_id, "cancel"):
            return self.finish(job, "cancelled", now=now)
        marker = self.job_directory(job_id) / "cancel"
        if not os.path.lexists(marker):
            try:
                write_new_private_file(marker, b"")
            except FileExistsError:
                pass
        return self.load(job_id) or job

    # -- the owner's writes ------------------------------------------------------------------

    def claim(self, job_id: str, owner: Literal["runner", "cancel"]) -> bool:
        """Take ownership of a job; ``False`` when someone already has it."""
        try:
            descriptor = os.open(
                self.job_directory(job_id) / "claim",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except FileExistsError:
            return False
        try:
            os.write(descriptor, owner.encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        (self.queue_root / job_id).unlink(missing_ok=True)
        return True

    def write_state(self, state: SpeechJobState) -> None:
        write_canonical_json(self.job_directory(state.job_id) / "status.json", state)

    def mark_running(self, job: Job, *, now: datetime) -> Job:
        state = SpeechJobState(job_id=job.record.job_id, state="running", started_at=now)
        self.write_state(state)
        return Job(job.record, state)

    def finish(
        self,
        job: Job,
        outcome: Literal["succeeded", "failed", "cancelled"],
        *,
        now: datetime,
        failure: tuple[FailureCode, str] | None = None,
        audio: RenderedAudio | None = None,
    ) -> Job:
        """End ``job`` (its owner only): final state, text deleted, webhook event queued."""
        job_id = job.record.job_id
        state = SpeechJobState(
            job_id=job_id,
            state=outcome,
            started_at=job.state.started_at,
            finished_at=now,
            error=None
            if failure is None
            else SpeechFailure(code=failure[0], detail=failure[1][:2000]),
            audio=audio,
            audio_expires_at=(
                now + timedelta(hours=self.config.audio_retention_hours)
                if audio is not None
                else None
            ),
        )
        self.write_state(state)
        directory = self.job_directory(job_id)
        (directory / "input.json").unlink(missing_ok=True)
        (directory / PARTIAL_AUDIO_NAME).unlink(missing_ok=True)
        (self.queue_root / job_id).unlink(missing_ok=True)
        ended = Job(job.record, state)
        if job.record.callback_url is not None:
            self._emit(ended, outcome, now=now)
        return ended

    def _emit(
        self, job: Job, outcome: Literal["succeeded", "failed", "cancelled"], *, now: datetime
    ) -> None:
        status = to_status(job)
        callback_url = job.record.callback_url
        assert callback_url is not None
        event_id = f"{WEBHOOK_EVENT_PREFIX}{job.record.job_id}"
        entry = WebhookOutboxEntry(
            event_id=event_id,
            caller_id=job.record.caller_id,
            url=callback_url,
            created_at=now,
            event=SpeechEvent(
                type=event_type(outcome),
                timestamp=now,
                data=SpeechEventData(
                    id=status.id,
                    state=outcome,
                    client_ref=status.client_ref,
                    audio=status.audio,
                    error=status.error,
                    links=status.links,
                ),
            ),
        )
        write_canonical_json(self.outbox_root / f"{event_id}.json", entry)

    # -- retention ---------------------------------------------------------------------------

    def sweep(self, *, now: datetime) -> tuple[int, int]:
        """Delete audio past its retention and job records past theirs.

        Returns: how many audio files and how many job records were removed.
        """
        audio_removed = records_removed = 0
        keep = timedelta(days=self.config.record_retention_days)
        try:
            names = sorted(os.listdir(self.jobs_root))
        except FileNotFoundError:
            return 0, 0
        for name in names:
            directory = self.jobs_root / name
            if not JOB_ID_PATTERN.fullmatch(name) or not _real_directory(directory):
                continue
            job = self.load(name)
            if job is None:
                if _older_than(directory, now - ABANDONED_SUBMIT):
                    shutil.rmtree(directory)
                    records_removed += 1
                continue
            state = job.state
            if (
                state.state == "succeeded"
                and state.audio_expires_at is not None
                and state.audio_expires_at <= now
            ):
                (directory / AUDIO_NAME).unlink(missing_ok=True)
                self.write_state(state.model_copy(update={"state": "expired"}))
                audio_removed += 1
            if state.finished_at is not None and state.finished_at + keep <= now:
                (directory / AUDIO_NAME).unlink(missing_ok=True)
                if job.record.idempotency_key_sha256 is not None:
                    self._idempotency_path(
                        job.record.caller_id, job.record.idempotency_key_sha256
                    ).unlink(missing_ok=True)
                shutil.rmtree(directory)
                records_removed += 1
        return audio_removed, records_removed


def _real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _older_than(path: Path, moment: datetime) -> bool:
    return os.lstat(path).st_mtime < moment.timestamp()


def to_status(job: Job) -> SpeechJobStatus:
    """What the caller sees of ``job``."""
    record, state = job.record, job.state
    audio = None
    if state.audio is not None and state.audio_expires_at is not None:
        audio = SpeechAudio(
            sha256=state.audio.sha256,
            bytes=state.audio.bytes,
            duration_ms=round(state.audio.frames * 1000 / state.audio.sample_rate_hz),
            expires_at=state.audio_expires_at,
        )
    queue_ms = (
        milliseconds(record.created_at, state.started_at) if state.started_at is not None else None
    )
    render_ms = (
        milliseconds(state.started_at, state.finished_at)
        if state.started_at is not None and state.finished_at is not None
        else None
    )
    return SpeechJobStatus(
        id=record.job_id,
        state=state.state,
        client_ref=record.client_ref,
        format=record.format,
        text_characters=record.text_characters,
        created_at=record.created_at,
        started_at=state.started_at,
        finished_at=state.finished_at,
        queue_ms=queue_ms,
        render_ms=render_ms,
        audio=audio,
        error=state.error,
        links=SpeechLinks.for_job(record.job_id),
    )


def open_audio(store: SpeechStore, job: Job) -> tuple[BinaryIO, int]:
    """The job's audio, opened (never through a link), and its size.

    Raises: ``SpeechRefusal`` 409 ``audio_not_ready`` before the job succeeded, 410
    ``audio_expired`` after retention.
    """
    state: JobState = job.state.state
    if state == "expired":
        raise SpeechRefusal(410, "audio_expired", "the audio was deleted after its retention")
    if state != "succeeded" or job.state.audio is None:
        raise SpeechRefusal(409, "audio_not_ready", f"the job is {state}")
    try:
        stream = open_regular_no_follow(store.audio_path(job.record.job_id))
    except (OSError, VoiceLabError) as error:  # missing, or not a regular file
        raise SpeechRefusal(410, "audio_expired", "the audio is no longer kept") from error
    return stream, os.fstat(stream.fileno()).st_size

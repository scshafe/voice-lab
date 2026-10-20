"""The speech jobs on disk, shared by the API and the job runner (docs/SPEECH_API.md §7).

@canonical speech.store -- ``state/speech/`` on the PC, owned by ``voice-lab``; directories
0700, files 0600. The API container writes a job (``jobs/<id>/request.json`` without the
text, ``input.json`` with it, ``status.json``) and then ``queue/<id>``, which the
long-running, network-less job runner looks for (``voice-lab-speech.service`` polls the
queue). Ownership of a job is one file, ``jobs/<id>/claim``, created exclusively: the runner
claims a job to render it, and a cancel claims a queued job to end it, so exactly one of them
owns it and only the owner writes its state after that. A cancel that loses the race leaves
``jobs/<id>/cancel`` for the runner. A claim whose owner stopped before the job ended is
found by the next runner pass (``orphaned_claims``): the runner's own claim on a job that
never started is given back to the queue (``requeue``) until the job's queue time is up.

The text and the request are named by keyed digests (HMAC-SHA256 under ``text-digest.key``,
32 random bytes made once per install, 0600, in the state root: never in the asset root, so
never in the vault), never by plain SHA-256; the render ledger keeps the same digest. A key
is made only on a new install (no job record and no render ledger entry yet). A key that is
unusable (not a private regular file of 32 bytes), or missing once anything was named by it,
is never made or replaced automatically: whatever needs it answers ``500 internal_error``
and the log names why, once per change, for the operator's repair (deploy/speech/README.md,
"The text key"; ``voice-lab speech new-text-key``). The API re-checks the key file on every
use, so it never keeps using a key that vanished, and uses a replaced one from then on.
After a new key, earlier jobs' idempotency keys answer ``409 idempotency_key_unverifiable``
(each entry names the key it was made under by ``text_key_id``) and the earlier ledger
entries no longer match a text under it.

When a job ends its text is deleted first, then its state is made final, and (with a
``callback_url``) a webhook event is written to ``outbox/`` for the delivery process.
``sweep`` deletes audio after its retention (the job then reads ``expired``), whole job
records after theirs, and any text an interrupted ending left behind.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import secrets
import shutil
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Final, Literal

from ..api.contracts import ApiErrorCode
from ..canonical import (
    canonical_json_bytes,
    open_regular_no_follow,
    write_canonical_json,
    write_new_private_file,
)
from ..errors import VoiceLabError
from ..models import ContractModel, Sha256, StableId
from .config import SpeechCaller, SpeechConfig
from .contracts import (
    JOB_ID_PATTERN,
    TERMINAL_STATES,
    WEBHOOK_EVENT_PREFIX,
    FailureCode,
    JobState,
    RenderedAudio,
    SegmentAudio,
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
    SpeechSegment,
    WebhookOutboxEntry,
    event_type,
    milliseconds,
    segment_link,
)

AUDIO_NAME: Final = "speech.wav"
SEGMENTS_DIRECTORY: Final = "segments"
PARTIAL_AUDIO_NAME: Final = "render.wav.part"
MAX_RECORD_BYTES: Final = 64 * 1024
# A job directory that never got its state (a crash inside ``submit``) is removed after this.
ABANDONED_SUBMIT: Final = timedelta(hours=1)
# A cancel's claim older than this whose job is still queued: the API stopped in between.
ABANDONED_CANCEL: Final = timedelta(minutes=1)
TEXT_KEY_NAME: Final = "text-digest.key"
# The render ledger's directory in the asset root's provenance (written by the job runner).
LEDGER_DIRECTORY: Final = "speech-renders"
TEXT_KEY_BYTES: Final = 32
# What a key's identifier is the keyed digest of: it names the key without revealing it.
TEXT_KEY_ID_LABEL: Final = b"cole-voice-lab.speech-text-key-id/v1"


def log_to_stderr(message: str) -> None:
    """The speech processes' log line (stderr: the journal, or the API container's log)."""
    print(f"voice-lab speech: {message}", file=sys.stderr, flush=True)


class SpeechRefusal(Exception):
    """A refusal the API answers with a stable code (and, for 503, ``Retry-After``; for 416,
    ``Content-Range``)."""

    def __init__(
        self,
        status: int,
        code: ApiErrorCode,
        detail: str,
        *,
        retry_after: int | None = None,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code: ApiErrorCode = code
        self.detail = detail
        self.retry_after = retry_after
        self.headers = headers


class _IdempotencyEntry(ContractModel):
    job_id: StableId
    # The identifier of the text key the job's request digest was made under (``None`` in an
    # entry written before it was recorded).
    text_key_id: Sha256 | None = None


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


def keyed_digest(key: bytes, data: bytes) -> str:
    """HMAC-SHA256 of ``data`` under the install's text key (hex)."""
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _key_id(key: bytes) -> str:
    return keyed_digest(key, TEXT_KEY_ID_LABEL)


def _lost_key_problem(path: Path, history: tuple[int, int] | None) -> str:
    if history is None:
        named = "the job records or the render ledger cannot be listed"
    else:
        named = f"{history[0]} job record(s) and {history[1]} ledger entr(ies) were named by it"
    return (
        f"{path} is missing and is not made again ({named}): restore it from a backup, or"
        " make a new one with `voice-lab speech new-text-key`"
    )


class TextKeyError(VoiceLabError):
    """The text key cannot be used; the message says why, never what the key is."""


def read_text_key(path: Path) -> bytes:
    """The text key at ``path``: a private regular file of exactly ``TEXT_KEY_BYTES`` bytes.

    Raises: ``TextKeyError`` naming what is wrong (missing, not a regular file, open to
    others, the wrong size, unreadable), never the key's bytes.
    """
    if not os.path.lexists(path):
        raise TextKeyError(f"{path} is missing")
    try:
        with open_regular_no_follow(path) as stream:
            mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
            if mode & 0o077:
                raise TextKeyError(f"{path} is open to group or others (mode {mode:04o}, not 0600)")
            key = stream.read(TEXT_KEY_BYTES + 1)
    except VoiceLabError as error:
        if isinstance(error, TextKeyError):
            raise
        raise TextKeyError(f"{path} is not a regular file (a link or another kind)") from error
    except OSError as error:
        raise TextKeyError(f"{path} cannot be read: {type(error).__name__}") from error
    if len(key) != TEXT_KEY_BYTES:
        size = f"{len(key)} bytes" if len(key) <= TEXT_KEY_BYTES else "longer"
        raise TextKeyError(f"{path} is {size}, not {TEXT_KEY_BYTES} bytes")
    return key


class SpeechStore:
    """The job directories under one ``state_root``."""

    def __init__(self, config: SpeechConfig, *, log: Callable[[str], None] = log_to_stderr) -> None:
        self.config = config
        self.log = log
        self.root = Path(config.state_root)
        self.jobs_root = self.root / "jobs"
        self.queue_root = self.root / "queue"
        self.idempotency_root = self.root / "idempotency"
        self.outbox_root = self.root / "outbox"
        self.webhooks_root = self.root / "webhooks"
        self._text_key: bytes | None = None
        self._key_signature: tuple[int, ...] | None = None
        self._key_problem: str | None = None

    def ensure(self) -> None:
        """Create the directories (0700) and, on a new install, the text key; a link where
        one belongs is refused.

        The key is made only when nothing was ever named by one: no job record and no render
        ledger entry. Otherwise a missing key is a lost key, and it is reported, never made
        again here (``make_text_key`` is the operator's repair). Any problem with the key is
        logged once while it stays the same.
        """
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
                raise SpeechRefusal(500, "internal_error", "the speech state is unavailable")
        key = self.root / TEXT_KEY_NAME
        if not os.path.lexists(key):
            history = self.named_history()
            if history is None or any(history):
                self._note_key_problem(_lost_key_problem(key, history))
                return
            try:
                write_new_private_file(key, secrets.token_bytes(TEXT_KEY_BYTES))
            except FileExistsError:
                pass  # the API and the runner raced; the one that won made it
            self._note_key_problem(None)
            return
        try:
            read_text_key(key)
        except TextKeyError as error:
            self._note_key_problem(str(error))
        else:
            self._note_key_problem(None)

    def named_history(self) -> tuple[int, int] | None:
        """How many job records and render ledger entries exist (each was named by a text
        key); ``None`` when they cannot be counted."""
        try:
            records = sum(
                1 for name in os.listdir(self.jobs_root) if JOB_ID_PATTERN.fullmatch(name)
            )
        except FileNotFoundError:
            records = 0
        except OSError:
            return None
        ledger = Path(self.config.asset_root) / "provenance" / LEDGER_DIRECTORY
        try:
            entries = sum(1 for name in os.listdir(ledger) if name.endswith(".json"))
        except FileNotFoundError:
            entries = 0
        except OSError:
            return None
        return records, entries

    def make_text_key(self) -> tuple[int, int]:
        """Make a new text key where none is (the operator's repair of a lost key).

        Returns: the job records and ledger entries named by the lost key.
        Raises: ``TextKeyError`` when a key file is there (move an unusable one aside first)
        or the history cannot be counted.
        """
        key = self.root / TEXT_KEY_NAME
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.path.lexists(key):
            raise TextKeyError(f"{key} exists: move it aside first if it is unusable")
        history = self.named_history()
        if history is None:
            raise TextKeyError("the job records or the render ledger cannot be listed")
        try:
            write_new_private_file(key, secrets.token_bytes(TEXT_KEY_BYTES))
        except FileExistsError as error:
            raise TextKeyError(f"{key} exists: another process made it") from error
        self.log(
            f"a new {TEXT_KEY_NAME} was made by hand; {history[0]} job record(s) and"
            f" {history[1]} ledger entr(ies) were named by the lost key"
        )
        return history

    def _note_key_problem(self, problem: str | None) -> None:
        """Log a problem with the key once per change (and when it is resolved)."""
        if problem == self._key_problem:
            return
        previous, self._key_problem = self._key_problem, problem
        if problem is not None:
            self.log(
                f"the text key cannot be used: {problem}; speech requests answer 500 until it"
                " is repaired (deploy/speech/README.md, 'The text key')"
            )
        elif previous is not None:
            self.log("the text key is usable again")

    def text_key(self) -> bytes:
        """The install's text key, re-read whenever its file changed (checked by ``lstat``
        on every call), so a key that vanished is never used from memory and a replaced one
        is used, with a log line, from then on.

        Raises: ``SpeechRefusal`` 500 ``internal_error`` when it is missing or unusable (the
        log says why, once per change).
        """
        path = self.root / TEXT_KEY_NAME
        try:
            metadata = os.lstat(path)
            signature: tuple[int, ...] | None = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_mode,
            )
        except OSError:
            signature = None
        if self._text_key is not None and signature == self._key_signature:
            return self._text_key
        try:
            key = read_text_key(path)
        except TextKeyError as error:
            self._text_key, self._key_signature = None, None
            problem = str(error)
            if not os.path.lexists(path):
                problem = _lost_key_problem(path, self.named_history())
            self._note_key_problem(problem)
            raise SpeechRefusal(500, "internal_error", "the speech state is unavailable") from (
                error
            )
        if self._text_key is not None and not hmac.compare_digest(key, self._text_key):
            self.log(
                f"the text key changed on disk (key id {_key_id(self._text_key)[:12]} to"
                f" {_key_id(key)[:12]}); new digests use the new key"
            )
        self._text_key, self._key_signature = key, signature
        self._note_key_problem(None)
        return key

    def text_key_id(self, key: bytes | None = None) -> str:
        """The text key's identifier: a keyed digest of a fixed label, never the key."""
        return _key_id(self.text_key() if key is None else key)

    def text_digest(self, text: str, key: bytes | None = None) -> str:
        """The keyed digest the job record and the ledger name a text by."""
        return keyed_digest(self.text_key() if key is None else key, text.encode("utf-8"))

    def request_digest(self, request: SpeechRequest, key: bytes | None = None) -> str:
        """The keyed digest of a whole request (its text included), for idempotency."""
        return keyed_digest(self.text_key() if key is None else key, canonical_json_bytes(request))

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

    def replay(
        self, caller: SpeechCaller, request: SpeechRequest, *, key: bytes | None = None
    ) -> Job | None:
        """The job an earlier request with the same idempotency key made, if it still exists.

        Raises: ``SpeechRefusal`` 409 ``idempotency_conflict`` when that request differed;
        409 ``idempotency_key_unverifiable`` when the job was made under a text key that has
        since been replaced, so the two requests cannot be compared.
        """
        if request.idempotency_key is None:
            return None
        path = self._idempotency_path(caller.caller_id, _sha256_text(request.idempotency_key))
        if not os.path.lexists(path):
            return None
        entry = _read(path, _IdempotencyEntry)
        existing = self.load(entry.job_id)
        if existing is None:
            return None
        key = self.text_key() if key is None else key
        if entry.text_key_id is not None and not hmac.compare_digest(
            entry.text_key_id, self.text_key_id(key)
        ):
            raise SpeechRefusal(
                409,
                "idempotency_key_unverifiable",
                "this idempotency key's job was made before the server's text key was replaced,"
                " so this request cannot be compared with it; use a new idempotency key",
            )
        digest = self.request_digest(request, key)
        if not hmac.compare_digest(existing.record.request_hmac_sha256, digest):
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
        key = self.text_key()  # one key for the whole request, even if the file changes
        request_digest = self.request_digest(request, key)
        key_sha256 = (
            None if request.idempotency_key is None else _sha256_text(request.idempotency_key)
        )
        characters = len(request.text)
        with self._submit_lock():
            existing = self.replay(caller, request, key=key)
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
                request_hmac_sha256=request_digest,
                text_hmac_sha256=self.text_digest(request.text, key),
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
                entry = _IdempotencyEntry(job_id=job_id, text_key_id=self.text_key_id(key))
                write_canonical_json(path, entry)  # replaces a stale key
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

    def claim_owner(self, job_id: str) -> tuple[str, datetime] | None:
        """Who claimed the job (``runner`` or ``cancel``) and when, or ``None`` when no one
        has (an unreadable claim reads as the runner's, which recovery then re-checks)."""
        path = self.job_directory(job_id) / "claim"
        if not os.path.lexists(path):
            return None
        try:
            with open_regular_no_follow(path) as stream:
                owner = stream.read(16).decode("ascii", "replace")
                modified = os.fstat(stream.fileno()).st_mtime
        except (OSError, VoiceLabError):
            return "runner", datetime.fromtimestamp(0, tz=UTC)
        return ("cancel" if owner == "cancel" else "runner"), datetime.fromtimestamp(
            modified, tz=UTC
        )

    def orphaned_claims(self, *, now: datetime) -> list[tuple[Job, str]]:
        """Queued jobs that someone claimed but never ended, with the claim's owner.

        Only the job runner calls this, under its lock, so a ``runner`` claim on a queued job
        was left by a runner that stopped between its claim and the job's start. A ``cancel``
        claim counts once it is ``ABANDONED_CANCEL`` old (the API ends the job right after it
        claims it).
        """
        found: list[tuple[Job, str]] = []
        for job in self.jobs():
            if job.state.state != "queued":
                continue
            claimed = self.claim_owner(job.record.job_id)
            if claimed is None:
                continue
            owner, at = claimed
            if owner == "cancel" and now - at < ABANDONED_CANCEL:
                continue
            found.append((job, owner))
        return found

    def requeue(self, job_id: str) -> None:
        """Give a claimed job that never started back to the queue (the runner's, under its
        lock): the queue marker first, then the claim is removed."""
        marker = self.queue_root / job_id
        if not os.path.lexists(marker):
            try:
                write_new_private_file(marker, b"")
            except FileExistsError:  # pragma: no cover - only the runner writes it back
                pass
        (self.job_directory(job_id) / "claim").unlink(missing_ok=True)

    def write_state(self, state: SpeechJobState) -> None:
        write_canonical_json(self.job_directory(state.job_id) / "status.json", state)

    def mark_running(self, job: Job, *, now: datetime, segment_count: int) -> Job:
        state = SpeechJobState(
            job_id=job.record.job_id,
            state="running",
            started_at=now,
            segment_count=segment_count,
        )
        self.write_state(state)
        return Job(job.record, state)

    def segment_path(self, job_id: str, index: int) -> Path:
        return self.job_directory(job_id) / SEGMENTS_DIRECTORY / f"{index:04d}.wav"

    def partial_segment_path(self, job_id: str, index: int) -> Path:
        directory = self.job_directory(job_id) / SEGMENTS_DIRECTORY
        directory.mkdir(mode=0o700, exist_ok=True)
        return directory / f".{index:04d}.wav.part"

    def publish_segment(self, job: Job, segment: SegmentAudio) -> Job:
        """Publish one finished sentence (its owner only): the file by rename, then the state
        that lists it, so a listed segment is always complete."""
        job_id = job.record.job_id
        os.replace(
            self.partial_segment_path(job_id, segment.index),
            self.segment_path(job_id, segment.index),
        )
        state = job.state.model_copy(update={"segments": (*job.state.segments, segment)})
        state = SpeechJobState.model_validate(state.model_dump())
        self.write_state(state)
        return Job(job.record, state)

    def withdraw_audio(self, job: Job) -> Job:
        """Unpublish everything ``job`` rendered (its owner only, before ``finish``): first the
        state that lists its segments, then the segment files and the joined audio, which go
        even when that state cannot be written."""
        job_id = job.record.job_id
        state = SpeechJobState.model_validate(job.state.model_dump() | {"segments": ()})
        directory = self.job_directory(job_id)
        try:
            self.write_state(state)
        finally:
            (directory / AUDIO_NAME).unlink(missing_ok=True)
            (directory / PARTIAL_AUDIO_NAME).unlink(missing_ok=True)
            shutil.rmtree(directory / SEGMENTS_DIRECTORY, ignore_errors=True)
        return Job(job.record, state)

    def finish(
        self,
        job: Job,
        outcome: Literal["succeeded", "failed", "cancelled"],
        *,
        now: datetime,
        failure: FailureCode | None = None,
        audio: RenderedAudio | None = None,
    ) -> Job:
        """End ``job`` (its owner only): text deleted, final state, webhook event queued.

        The text goes first, so an ending that is interrupted never leaves it behind a final
        state; the caller sees only ``failure``'s code and its fixed sentence
        (``SpeechFailure.of``), never a path or a reason.
        """
        job_id = job.record.job_id
        directory = self.job_directory(job_id)
        (directory / "input.json").unlink(missing_ok=True)
        state = SpeechJobState(
            job_id=job_id,
            state=outcome,
            started_at=job.state.started_at,
            finished_at=now,
            error=None if failure is None else SpeechFailure.of(failure),
            segment_count=job.state.segment_count,
            segments=job.state.segments,
            audio=audio,
            audio_expires_at=(
                now + timedelta(hours=self.config.audio_retention_hours)
                if audio is not None or job.state.segments
                else None
            ),
        )
        self.write_state(state)
        (directory / PARTIAL_AUDIO_NAME).unlink(missing_ok=True)
        for partial in (directory / SEGMENTS_DIRECTORY).glob(".*.part"):
            partial.unlink(missing_ok=True)
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
                    segment_count=status.segment_count,
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
            if state.state in TERMINAL_STATES:
                (directory / "input.json").unlink(missing_ok=True)  # an interrupted ending's
            if (
                state.audio_expires_at is not None
                and state.audio_expires_at <= now
                and (state.state == "succeeded" or (directory / SEGMENTS_DIRECTORY).exists())
            ):
                (directory / AUDIO_NAME).unlink(missing_ok=True)
                shutil.rmtree(directory / SEGMENTS_DIRECTORY, ignore_errors=True)
                if state.state == "succeeded":
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
    segments = tuple(
        SpeechSegment(
            index=segment.index,
            sha256=segment.sha256,
            bytes=segment.bytes,
            duration_ms=round(segment.frames * 1000 / segment.sample_rate_hz),
            audio=segment_link(record.job_id, segment.index),
            text_start=segment.text_start,
            text_end=segment.text_end,
        )
        for segment in state.segments
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
        segment_count=state.segment_count,
        segments=segments,
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


def open_segment(store: SpeechStore, job: Job, index: int) -> tuple[BinaryIO, int, SegmentAudio]:
    """One published segment, opened (never through a link), its size and its record.

    Raises: ``SpeechRefusal`` 404 ``not_found`` beyond the job's segments, 409
    ``audio_not_ready`` before it is published, 410 ``audio_expired`` after retention.
    """
    count = job.state.segment_count
    if count is not None and not 1 <= index <= count:
        raise SpeechRefusal(404, "not_found", f"the job has {count} segments")
    published = next((item for item in job.state.segments if item.index == index), None)
    if published is None:
        if job.state.state in TERMINAL_STATES:
            raise SpeechRefusal(404, "not_found", "that segment was never rendered")
        raise SpeechRefusal(409, "audio_not_ready", "that segment is not rendered yet")
    try:
        stream = open_regular_no_follow(store.segment_path(job.record.job_id, index))
    except (OSError, VoiceLabError) as error:
        raise SpeechRefusal(410, "audio_expired", "the audio is no longer kept") from error
    return stream, os.fstat(stream.fileno()).st_size, published

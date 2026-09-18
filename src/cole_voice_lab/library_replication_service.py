"""Background library copies owned by the recording service.

@canonical vault_supervisor -- the recording service's vault supervisor and its worker.
Public surface: VaultSupervisor (a thread that decides when to copy and what the app is
told), VaultStatus (what it knows, in memory), VaultWorkerReply (the one frame a pass
returns), vault_status_event (the wire event built from a status), worker_command /
worker_environment (how a pass is started) and main (the ``--worker`` entry point).
Related: storage_binding.py (the composition root the worker opens the vault through),
library_generations.py (push_generation, which the worker runs), recording_service.py (which
starts, nudges and closes the supervisor), tests/test_library_replication_service.py.

The service process never reads a recording and never seals anything: every pass runs in a
``python -I -B -m cole_voice_lab.library_replication_service --worker`` subprocess of its
own session, which opens the vault, pushes one generation and answers with exactly one JSON
frame. The supervisor thread only watches commits, starts passes and reports; it takes no
lock any lab writer takes, and it does nothing at all while the library is bound to no vault.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from .canonical import canonical_json_bytes
from .errors import ContractError, SecurityError, VoiceLabError
from .models import ContractModel, utc_now
from .paths import AssetLayout
from .recording_contracts import (
    MAX_VAULT_WAITING_FILES,
    RecordingError,
    VaultState,
    VaultStatusEvent,
    VaultStorageErrorCode,
)
from .recording_takes import RecordingMetadataBusyError
from .storage_ports import STORAGE_RETRYABLE, StorageError, StorageErrorCode

VAULT_WORKER_MODULE: Final = "cole_voice_lab.library_replication_service"
# The background request id of every vault update; the app matches replies by request id, so
# these two never collide with one of its own.
VAULT_STATUS_REQUEST_ID: Final = "vault-status"
VAULT_SUPERVISOR_REQUEST_ID: Final = "vault-supervisor"
# How long the library must stand still after a commit before a pass starts.
QUIET_PERIOD_SECONDS: Final = 60.0
# A pass runs at least this often anyway: the command line writes evidence this process
# never sees (a catalog publish, a consent, a fine-tune run).
RESCAN_INTERVAL_SECONDS: Final = 900.0
# How long to wait after each consecutive retryable failure, the last value repeating.
BACKOFF_SECONDS: Final[tuple[float, ...]] = (30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)
# At most one background vault update per this many seconds; changes in between coalesce.
PUBLISH_INTERVAL_SECONDS: Final = 30.0
# How long the supervisor thread has to stop once a worker's group has been killed.
CLOSE_JOIN_SECONDS: Final = 15.0
_POLL_SECONDS: Final = 0.05
# The largest reply frame a worker may write (its own message is bounded at 1,024).
MAX_WORKER_REPLY_BYTES: Final = 64 * 1024
# The failures that mean "the vault could not be reached", not "the vault refused".
UNREACHABLE_CODES: Final[frozenset[str]] = frozenset({"STORAGE.UNAVAILABLE", "STORAGE.TIMEOUT"})
SUPERVISION_FAILURE: Final = (
    "the library vault supervisor stopped; copies resume when the app is relaunched"
)
_WORKER_ENVIRONMENT: Final[Mapping[str, str]] = {
    "PATH": "/usr/bin:/bin",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONSAFEPATH": "1",
}


def _bounded(message: str) -> str:
    """A message short enough for the wire (``VaultStatusEvent.message`` is 1,024 at most)."""
    return message if len(message) <= 1024 else message[:1021] + "..."


class VaultWorkerReply(ContractModel):
    """The one frame a pass writes on stdout, and the only thing the service reads from it.

    ``outcome`` is ``pushed`` (a new generation is the vault's head), ``up-to-date`` (the
    vault already held every file this pass read) or ``failed``. ``generation`` is the head's
    generation afterwards (0 when nothing was ever pushed) and ``waiting_files`` counts files
    no generation holds yet. ``code`` is the storage refusal behind a failure (absent when
    the failure was not the vault's), ``retryable`` whether trying again may succeed by
    itself, and ``message`` says what happened (always for a failure; for a success only when
    an owner held files back).
    """

    schema_version: Literal["cole-voice-lab.vault-worker-reply/v1"] = (
        "cole-voice-lab.vault-worker-reply/v1"
    )
    outcome: Literal["pushed", "up-to-date", "failed"]
    generation: int = Field(default=0, ge=0)
    waiting_files: int = Field(default=0, ge=0, le=MAX_VAULT_WAITING_FILES)
    code: VaultStorageErrorCode | None = None
    retryable: bool = False
    message: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _outcome_evidence(self) -> VaultWorkerReply:
        if self.outcome == "failed" and self.message is None:
            raise ValueError("a failed pass says what went wrong")
        if self.outcome != "failed" and (self.code is not None or self.retryable):
            raise ValueError("only a failed pass carries a storage code or asks to be retried")
        if self.code is not None and STORAGE_RETRYABLE[self.code] != self.retryable:
            raise ValueError("a storage code is retryable exactly as its table says")
        return self


VAULT_SUPERVISOR_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "vault-worker-reply": VaultWorkerReply
}


@dataclass(frozen=True, slots=True)
class VaultStatus:
    """Where the vault stands, as the supervisor knows it; it is never read from disk twice.

    The fields are ``VaultStatusEvent``'s, and the same rules hold: ``unreachable_since`` is
    set exactly while ``state`` is ``unreachable``, and ``code`` exactly while the state is
    ``unreachable`` or ``failed``.
    """

    state: VaultState
    waiting_files: int = 0
    unreachable_since: datetime | None = None
    last_pushed_at: datetime | None = None
    generation: int = 0
    code: StorageErrorCode | None = None
    message: str | None = None


OFF_STATUS: Final = VaultStatus(state="off")


def vault_status_event(status: VaultStatus, *, operation_id: str) -> VaultStatusEvent:
    """The wire event for ``status``, answering the request or operation ``operation_id``."""
    return VaultStatusEvent(
        operation_id=operation_id,
        state=status.state,
        waiting_files=status.waiting_files,
        unreachable_since=status.unreachable_since,
        last_pushed_at=status.last_pushed_at,
        generation=status.generation,
        code=status.code,
        message=None if status.message is None else _bounded(status.message),
    )


# -- the worker process ----------------------------------------------------------------------


def worker_command(layout: AssetLayout) -> list[str]:
    """The command one pass runs: an isolated interpreter on this package's worker module."""
    return [
        sys.executable,
        "-I",
        "-B",
        "-m",
        VAULT_WORKER_MODULE,
        "--worker",
        "--asset-root",
        str(layout.root),
    ]


def worker_environment() -> dict[str, str]:
    """The worker's whole environment: a fixed PATH, this user's ``HOME`` and no inheritance.

    The app starts its recording service with no ``HOME`` at all, and ``/usr/bin/security``
    needs one to find the login Keychain, so the home directory comes from the password
    database rather than from the environment this process was given.
    """
    return dict(_WORKER_ENVIRONMENT) | {"HOME": pwd.getpwuid(os.getuid()).pw_dir}


def _kill_own_group() -> None:  # pragma: no cover - kills this process (worker EOF test)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def worker_reply_bytes(reply: VaultWorkerReply) -> bytes:
    """One reply frame: the reply's canonical JSON and a newline, and nothing else."""
    return canonical_json_bytes(reply) + b"\n"


def read_worker_reply(payload: bytes) -> VaultWorkerReply:
    """The reply a worker wrote.

    Raises: ``ContractError`` when the frame is missing, oversized, not one JSON line or not
    a valid ``VaultWorkerReply``.
    """
    if not payload:
        raise ContractError("the vault worker wrote no reply")
    if len(payload) > MAX_WORKER_REPLY_BYTES:
        raise ContractError("the vault worker wrote more than one reply frame")
    try:
        return VaultWorkerReply.model_validate_json(payload.strip())
    except ValidationError as failure:
        raise ContractError(f"the vault worker's reply is not usable: {failure}") from failure


def _failed_reply(
    message: str, *, code: StorageErrorCode | None, retryable: bool
) -> VaultWorkerReply:
    return VaultWorkerReply(
        outcome="failed", code=code, retryable=retryable, message=_bounded(message)
    )


def run_vault_pass(layout: AssetLayout) -> VaultWorkerReply:
    """Copy this library's current files to its vault, and name what happened.

    It opens the vault through the composition root (which is the only place an adapter is
    built, so no argument of this process chooses a destination, a key or a token) and runs
    one ``push_generation``. Every failure it can name becomes a reply rather than a
    traceback: the service reads the frame, not this process's exit status.
    Returns: the ``VaultWorkerReply``; ``failed`` with a retryable flag when the push stopped
    for something that may pass by itself (the vault was unreachable, a source moved, another
    push holds the lock).
    """
    from .library_generations import push_generation
    from .storage_binding import open_library_vault

    try:
        vault = open_library_vault(layout)
        if vault is None:
            return _failed_reply(
                "this library is bound to no vault any more",
                code="STORAGE.NOT_BOUND",
                retryable=False,
            )
        result = push_generation(
            layout,
            blob_store=vault.blob_store,
            ref_store=vault.ref_store,
            library_id=vault.library_id,
            destination=vault.destination,
            classes=vault.classes,
            chunk_bytes=vault.chunk_bytes,
        )
    except StorageError as failure:
        return _failed_reply(str(failure), code=failure.code, retryable=failure.retryable)
    except VoiceLabError as failure:
        # A replication refusal is the library's, not the vault's: it carries no storage code.
        return _failed_reply(
            str(failure), code=None, retryable=getattr(failure, "retryable", False) is True
        )
    except (OSError, ValidationError, ValueError) as failure:
        return _failed_reply(f"the vault pass failed: {failure}", code=None, retryable=True)
    if result.outcome == "source-changed":
        change = result.source_change
        return _failed_reply(
            "the library changed while it was being copied"
            if change is None
            else f"the library changed while it was being copied: {change}",
            code=None,
            retryable=True,
        )
    held = result.waiting_owners
    message: str | None = None
    if held:
        reasons = [outcome.reason for outcome in held if outcome.reason is not None]
        message = "waiting for " + ", ".join(sorted({outcome.owner for outcome in held}))
        if reasons:
            message += f": {reasons[0]}"
    return VaultWorkerReply(
        outcome="pushed" if result.outcome == "pushed" else "up-to-date",
        generation=result.generation,
        waiting_files=len(result.waiting_paths),
        message=None if message is None else _bounded(message),
    )


def _watch_parent(input_fd: int, finished: threading.Event, kill_group: Callable[[], None]) -> None:
    """Kill this worker's whole process group as soon as the service closes its stdin.

    A pass that has already answered is finished, so the stdin the service closes behind the
    reply is not a lost parent.
    """
    while True:
        try:
            chunk = os.read(input_fd, 4096)
        except OSError:
            chunk = b""
        if chunk:
            continue
        if not finished.is_set():
            kill_group()
        return


def _worker_main(layout: AssetLayout, *, kill_group: Callable[[], None] = _kill_own_group) -> int:
    """Run one pass while watching stdin, then write exactly one reply frame on stdout.

    A pass that breaks in a way ``run_vault_pass`` does not name still answers: the frame says
    which exception ended it and asks to be retried, because a crash of one pass is not a
    refusal the vault will repeat, and a supervisor that heard nothing would stop copying.
    Returns: 0 once the reply is written.
    Raises: ``OSError`` when the reply cannot be written.
    """
    finished = threading.Event()
    threading.Thread(
        target=_watch_parent,
        args=(sys.stdin.fileno(), finished, kill_group),
        daemon=True,
        name="vault-worker-parent",
    ).start()
    try:
        reply = run_vault_pass(layout)
    except Exception as failure:
        reply = _failed_reply(
            f"the vault pass stopped with {type(failure).__name__}: {failure}",
            code=None,
            retryable=True,
        )
    payload = worker_reply_bytes(reply)
    output = sys.stdout.buffer
    output.write(payload)
    output.flush()
    finished.set()
    return 0


def main(
    argv: Sequence[str] | None = None, *, kill_group: Callable[[], None] = _kill_own_group
) -> int:
    """The worker process: ``--worker --asset-root <root>``.

    It must lead its own process group (the supervisor starts it in a new session), because
    it kills that group when the service goes away (stdin ends), so nothing it started
    outlives it. Everything it writes is private (umask 077).
    Params: ``kill_group`` stops the whole group, this process included (tests substitute it).
    Returns: 0 after its reply; 2 after a failure it cannot report (one JSON line on stderr
    naming the code) or when it does not lead its group (that group is its caller's).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    options = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    leader = os.getpgrp() == os.getpid()
    try:
        if not leader:
            raise ContractError("a vault worker must lead its own process group")
        return _worker_main(AssetLayout.validate(options.asset_root), kill_group=kill_group)
    except BaseException as failure:
        code = failure.code if isinstance(failure, StorageError) else "STORAGE.PROTOCOL"
        print(
            json.dumps(
                {"service": "vault-worker", "error_code": code, "message": "vault worker stopped"}
            ),
            file=sys.stderr,
            flush=True,
        )
        if leader:
            kill_group()
        return 2
    finally:
        os.umask(previous_umask)


# -- the supervisor --------------------------------------------------------------------------


class VaultPassRunner(Protocol):
    """How one pass is run; the supervisor owns exactly one at a time."""

    def run(self, stop: threading.Event) -> VaultWorkerReply:
        """Run one pass, returning as soon as ``stop`` is set (the reply is then ``failed``)."""
        ...

    def terminate(self) -> None:
        """Stop a running pass now; safe to call from another thread and more than once."""
        ...


class WorkerPassRunner:
    """Runs each pass in a worker process of its own session, and reads its one frame.

    The worker inherits nothing but its three pipes: a sanitized environment, stdin held open
    so the worker learns at once when the service is gone, stdout for the frame, and stderr
    discarded (a worker never writes a library path or a secret where it could be kept).
    """

    def __init__(
        self,
        layout: AssetLayout,
        *,
        command: Callable[[AssetLayout], Sequence[str]] = worker_command,
        environment: Callable[[], Mapping[str, str]] = worker_environment,
    ) -> None:
        """Params: ``command`` and ``environment`` are the test seams for what a pass runs."""
        self.layout = layout
        self._command = command
        self._environment = environment
        self._mutex = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def worker_pid(self) -> int | None:
        with self._mutex:
            return None if self._process is None else self._process.pid

    def run(self, stop: threading.Event) -> VaultWorkerReply:
        try:
            process = subprocess.Popen(
                list(self._command(self.layout)),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(self._environment()),
                close_fds=True,
                start_new_session=True,
            )
        except OSError as failure:
            return _failed_reply(
                f"the vault worker could not start: {failure}", code=None, retryable=True
            )
        with self._mutex:
            self._process = process
        try:
            return self._converse(process, stop)
        finally:
            self._reap(process)
            with self._mutex:
                self._process = None

    def _converse(
        self, process: subprocess.Popen[bytes], stop: threading.Event
    ) -> VaultWorkerReply:
        assert process.stdout is not None
        stopped = "the vault pass was stopped with the service"
        payload = bytearray()
        while not stop.is_set():
            chunk = _read_ready(process.stdout.fileno(), _POLL_SECONDS)
            if chunk is None:
                continue
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_WORKER_REPLY_BYTES:
                _kill_group(process)
                return _failed_reply(
                    "the vault worker wrote more than one reply frame", code=None, retryable=False
                )
        if stop.is_set():
            # The pipe may have ended because ``close`` killed the worker, so whatever is in
            # the buffer is half a frame, not a refusal: the generation is simply not written.
            _kill_group(process)
            return _failed_reply(stopped, code=None, retryable=True)
        frame = bytes(payload)
        if not frame:
            # The worker died before it could answer (it was killed, or its interpreter never
            # reached the module). That is a crash, not a refusal the vault will repeat, so the
            # next pass is still worth starting.
            return _failed_reply("the vault worker wrote no reply", code=None, retryable=True)
        try:
            return read_worker_reply(frame)
        except ContractError as failure:
            # A frame that arrived but is unusable stays non-retryable: the worker answered,
            # and repeating a pass that produced an unreadable answer would loop. Known edge:
            # a worker killed mid-frame (one small write plus flush, with no other writer on
            # its stdout) lands here rather than in the empty-frame branch above, so that rare
            # crash stops copying until the app is relaunched instead of being retried.
            return _failed_reply(str(failure), code=None, retryable=False)

    def _reap(self, process: subprocess.Popen[bytes]) -> None:
        """Close the worker's pipes and make sure its whole group is gone."""
        process.poll()
        _kill_group(process)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()
        process.wait()

    def terminate(self) -> None:
        with self._mutex:
            process = self._process
        if process is not None:
            _kill_group(process)


def _read_ready(descriptor: int, timeout: float) -> bytes | None:
    """The bytes waiting on ``descriptor``, or ``None`` when none arrived within ``timeout``."""
    readable, _, _ = select.select([descriptor], [], [], timeout)
    if not readable:
        return None
    return os.read(descriptor, 16_384)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the worker's whole session; a group that is already gone is not a failure."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


class VaultSupervisor:
    """Copies the library to its vault in the background, and says where that stands.

    It starts a pass once the library has stood still for ``quiet_period`` after a commit
    (``notify_commit``), and at least every ``rescan_interval`` in any case, so evidence the
    command line writes is copied too. It never starts one while a fine-tune job or a Speak
    request is running, and never while a pass is already running. A retryable failure --
    including a worker that died without answering -- is retried after ``backoff``; a refusal
    the vault itself will keep making stops passes until the app is relaunched (the binding is
    read once, in ``start``, so rebinding takes effect only at the next launch, which is what
    ``voice-lab vault bind`` tells the user). Nothing happens at all, and nothing is published,
    while the library is bound to no vault.
    """

    def __init__(
        self,
        layout: AssetLayout,
        *,
        on_status: Callable[[VaultStatus], None] | None = None,
        on_failure: Callable[[RecordingError], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        quiet_period: float = QUIET_PERIOD_SECONDS,
        rescan_interval: float = RESCAN_INTERVAL_SECONDS,
        backoff: Sequence[float] = BACKOFF_SECONDS,
        publish_interval: float = PUBLISH_INTERVAL_SECONDS,
        poll: float = _POLL_SECONDS,
        processes: VaultPassRunner | None = None,
        activity: Callable[[], str | None] | None = None,
    ) -> None:
        """Params: ``on_status`` hears every state the app should see (already coalesced);
        ``on_failure`` hears the supervisor's own end, as the fine-tune supervisor's does;
        ``clock`` dates the status; ``monotonic``, ``quiet_period``, ``rescan_interval``,
        ``backoff``, ``publish_interval`` and ``poll`` are the timing seams; ``processes`` runs each
        pass (default: a ``WorkerPassRunner`` on ``layout``); ``activity`` says why a pass
        must wait (default: the fine-tune queue's own markers).
        """
        self.layout = layout
        self.on_status = on_status
        self.on_failure = on_failure
        self.clock = clock
        self.monotonic = monotonic
        self.quiet_period = quiet_period
        self.rescan_interval = rescan_interval
        self.backoff = tuple(backoff) or BACKOFF_SECONDS
        self.publish_interval = publish_interval
        self.poll = poll
        self.runner: VaultPassRunner = WorkerPassRunner(layout) if processes is None else processes
        self._activity = activity
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._mutex = threading.Lock()
        self._status = OFF_STATUS
        self._published: VaultStatus | None = None
        self._published_at = 0.0
        self._commit_at: float | None = None
        # When the vault first stopped answering; kept across a copying state, which the wire
        # event may not carry it in.
        self._unreachable_since: datetime | None = None
        self._last_pass_at = 0.0
        self._next_pass_at = 0.0
        self._failures = 0
        self._passes = 0
        self.dropped_publications = 0
        self.fatal_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="recording-vault-supervisor"
        )

    # -- what the service asks ----------------------------------------------------------

    @property
    def status(self) -> VaultStatus:
        """Where the vault stands, from memory alone: no disk, no network, no lock."""
        with self._mutex:
            return self._status

    @property
    def passes(self) -> int:
        """How many passes have finished; a test waits on it."""
        with self._mutex:
            return self._passes

    def notify_commit(self) -> None:
        """Note that the library gained durable evidence; never blocks and never does I/O."""
        with self._mutex:
            if self._status.state == "off":
                return
            self._commit_at = self.monotonic()
        self._wake.set()

    def start(self) -> None:
        """Decide whether this library copies anywhere, and start the thread when it does.

        A library with no binding is ``off``: no thread runs, no pass is started and nothing
        is ever published. A replication state without a binding, or a binding that cannot be
        read, is ``failed`` with its storage code (copies stop rather than go somewhere else).
        """
        from .storage_binding import library_vault_binding

        try:
            binding = library_vault_binding(self.layout)
        except StorageError as failure:
            self._set(
                VaultStatus(state="failed", code=failure.code, message=_bounded(str(failure)))
            )
            return
        if binding is None:
            return
        now = self.monotonic()
        with self._mutex:
            self._status = VaultStatus(state="waiting")
            # The service starting is itself a reason to look: the command line may have
            # written evidence while the app was closed.
            self._commit_at = now
            self._last_pass_at = now
        self._thread.start()

    def close(self) -> None:
        """Stop supervising: a running pass is killed and its generation is simply not written.

        It never raises, so the service's other closes always follow it.
        """
        self._stop.set()
        self._wake.set()
        self.runner.terminate()
        if self._thread.ident is not None:
            self._thread.join(timeout=CLOSE_JOIN_SECONDS)

    # -- publication --------------------------------------------------------------------

    def _set(self, status: VaultStatus) -> None:
        with self._mutex:
            self._status = status
        self._flush()

    def _flush(self) -> None:
        """Publish the current status when it differs from the last one and the gap has passed."""
        if self.on_status is None:
            return
        with self._mutex:
            status = self._status
            if status.state == "off" or status == self._published:
                return
            now = self.monotonic()
            if self._published is not None and now - self._published_at < self.publish_interval:
                return
            self._published = status
            self._published_at = now
        self._publish(lambda: None if self.on_status is None else self.on_status(status))

    def _publish(self, action: Callable[[], None]) -> None:
        """Publish through the session, waiting out another process's metadata lock.

        A publication that fails any other way is counted and dropped: the app asks for the
        status again after its handshake, and no background update may stop the service.
        """
        while not self._stop.is_set():
            try:
                action()
                return
            except RecordingMetadataBusyError:
                self._stop.wait(0.01)
            except Exception:
                self.dropped_publications += 1
                return

    # -- the loop -----------------------------------------------------------------------

    def _waiting_for(self) -> str | None:
        """Why a pass must wait, or ``None`` when nothing holds it back."""
        if self._activity is not None:
            return self._activity()
        from .recording_finetune_jobs import active_finetune_job, active_speech_request

        try:
            job = active_finetune_job(self.layout)
            speech = active_speech_request(self.layout)
        except (ContractError, SecurityError) as failure:
            return f"the fine-tune queue cannot be read: {failure}"
        if job is not None:
            return f"fine-tune job {job.operation_id} is {job.state}"
        if speech is not None:
            return f"Speak request {speech.operation_id} is running"
        return None

    def _due(self, now: float) -> bool:
        with self._mutex:
            if self._status.state == "failed" or now < self._next_pass_at:
                return False
            commit_at = self._commit_at
            quiet = commit_at is not None and now - commit_at >= self.quiet_period
            # The rescan is for the writers this process never hears from (the command line).
            # A library the app itself is still committing to waits for its quiet period
            # instead, so no pass reads the disk underneath a recording session.
            rescan = commit_at is None and now - self._last_pass_at >= self.rescan_interval
            return quiet or rescan

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._flush()
                now = self.monotonic()
                if not self._due(now):
                    self._wake.wait(self.poll)
                    self._wake.clear()
                    continue
                waiting = self._waiting_for()
                if waiting is not None:
                    self._hold(now, waiting)
                    continue
                self._pass()
        except BaseException as error:  # the thread's own end, reported like fine-tune's
            self.fatal_error = error
            self._set(
                VaultStatus(state="failed", code="STORAGE.PROTOCOL", message=SUPERVISION_FAILURE)
            )
            if self.on_failure is not None:
                self._publish(
                    lambda: (
                        None
                        if self.on_failure is None
                        else self.on_failure(
                            RecordingError(
                                code="RECORDING.WRITE_FAILED",
                                message=SUPERVISION_FAILURE,
                                retryable=True,
                            )
                        )
                    )
                )

    def _hold(self, now: float, reason: str) -> None:
        """Wait for the GPU to be free again without spending the commit that asked for a pass."""
        with self._mutex:
            self._next_pass_at = now + self.quiet_period
            status = self._status
        self._set(
            replace(status, state="waiting", message=reason, code=None, unreachable_since=None)
        )

    def _pass(self) -> None:
        with self._mutex:
            self._commit_at = None
            status = self._status
        self._set(replace(status, state="copying", message=None, code=None, unreachable_since=None))
        reply = self.runner.run(self._stop)
        now = self.monotonic()
        with self._mutex:
            self._last_pass_at = now
        self._apply(reply, now)
        # Counted only once this pass's status is the one a reader would see, so waiting for
        # the count is waiting for the state it produced.
        with self._mutex:
            self._passes += 1
        self._flush()

    def _apply(self, reply: VaultWorkerReply, now: float) -> None:
        previous = self.status
        if reply.outcome != "failed":
            with self._mutex:
                self._failures = 0
                self._next_pass_at = now
                self._unreachable_since = None
            waiting = reply.waiting_files > 0 or reply.message is not None
            self._set(
                VaultStatus(
                    state="waiting" if waiting else "up-to-date",
                    waiting_files=reply.waiting_files,
                    last_pushed_at=self.clock()
                    if reply.outcome == "pushed"
                    else previous.last_pushed_at,
                    generation=reply.generation,
                    message=reply.message,
                )
            )
            return
        message = reply.message or "the vault pass failed"
        if not reply.retryable:
            # A refusal the vault will keep making: stop, and say so until the app restarts.
            with self._mutex:
                self._unreachable_since = None
            self._set(
                replace(
                    previous,
                    state="failed",
                    code=reply.code or "STORAGE.PROTOCOL",
                    message=message,
                    unreachable_since=None,
                )
            )
            return
        unreachable = reply.code in UNREACHABLE_CODES
        with self._mutex:
            self._failures += 1
            self._next_pass_at = now + self.backoff[min(self._failures - 1, len(self.backoff) - 1)]
            # A retried pass still has a commit to answer: re-arm the quiet period from now.
            self._commit_at = now
            # The vault has been out of reach since the first of these failures, not the last.
            self._unreachable_since = (
                (self._unreachable_since or self.clock()) if unreachable else None
            )
            since = self._unreachable_since
        self._set(
            replace(
                previous,
                state="unreachable" if unreachable else "waiting",
                unreachable_since=since,
                code=reply.code if unreachable else None,
                message=message,
            )
        )


__all__ = [
    "OFF_STATUS",
    "VAULT_STATUS_REQUEST_ID",
    "VAULT_SUPERVISOR_REQUEST_ID",
    "VAULT_SUPERVISOR_SCHEMA_MODELS",
    "VAULT_WORKER_MODULE",
    "VaultPassRunner",
    "VaultStatus",
    "VaultSupervisor",
    "VaultWorkerReply",
    "WorkerPassRunner",
    "main",
    "read_worker_reply",
    "run_vault_pass",
    "vault_status_event",
    "worker_command",
    "worker_environment",
    "worker_reply_bytes",
]


if __name__ == "__main__":
    raise SystemExit(main())

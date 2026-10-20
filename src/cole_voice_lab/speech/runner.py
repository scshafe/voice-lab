"""The speech job runner: the queue, the GPU slot and the resident model, with no network.

@canonical speech.runner -- ``voice-lab speech run`` (the long-running host unit
``voice-lab-speech``, ``PrivateNetwork=yes``) calls ``SpeechRunner.serve``; ``--once`` makes
one pass (``run_once``) for an operator while the unit is stopped. One runner process runs at
a time: ``serve`` holds the instance lock (``.runner.instance.lock`` in the speech state) for
its whole life, and ``run_once`` refuses (``RunnerBusy``) while another holds it, rather than
wait for the GPU slot the resident runner keeps and stall the queue. Under a runner lock a
pass first recovers what a stopped runner (or API) left: a job left ``running`` ends
``cancelled`` when a cancel asked for it, else fails ``internal_error``; a queued job someone
claimed but never ended is cancelled when a cancel asked for it, failed ``queue_timeout`` when
its queue time is up, else given back to the queue. Then it claims each queued job, oldest
first, and for each:

1. ends it ``cancelled`` when a cancel arrived, and fails it ``queue_timeout`` when it waited
   longer than the configuration allows;
2. releases the newest finished run of the pinned subject that passes its checks when it
   finished after the newest release (``release.designate_newest``, D-055; a record it cannot
   write is logged and the current voice speaks); then re-reads the current voice (the newest
   release, else the base voice), refuses a release of another subject and re-hashes what it
   names (``voice_release_changed``); no consent record is read (D-050);
3. holds the GPU slot (``gpu.GpuSlotLease``, kept across jobs while the model is resident;
   a cancel ends the wait) and has the backend load the release (``runtime_unavailable``
   without a runtime);
4. splits the text into segments no longer than the release's family takes in one pass
   (``segments``), and renders them one after another, each with its own seed derived from
   the job's; before each it checks that the release is still current, so a new designation
   stops a running render at its next sentence; checks each WAV and
   publishes it at once as a segment of the job; a cancel is honoured between sentences;
5. joins the segments into the job's audio, writes the render ledger entry
   (``provenance/speech-renders/<job id>.json``: never the text), ends the job and queues
   its webhook event.

Any other error ends the job ``failed`` with a code (``internal_error`` when nothing more
precise fits); the particulars go to the runner's log (stderr, the journal), never to the
caller. Between jobs ``serve`` unloads the model and gives the slot back after the idle time,
or at once when a long job wants the slot and no speech is queued; it sweeps retention every
few minutes. A job that started rendering has a ledger entry, whatever its outcome, unless
the runner itself was killed mid-render. Each sentence is published (listed and served) as
soon as it is rendered, before the job's ledger entry exists; that is inherent in serving
sentences during the render. Once a job has ended, though, nothing of it is served without
its entry: when the entry cannot be written the job fails ``internal_error`` and its
segments and audio are unlisted and deleted, and a job a killed runner left ``running``
without an entry has them withdrawn when the next pass recovers it.

``serve``'s stop (SIGTERM) is honoured between jobs, while a job waits for the GPU slot (the
job goes back to the queue) and between sentences (the job ends ``failed internal_error``
with its ledger entry), so ``systemctl stop`` waits for at most the sentence being rendered
(the unit's ``TimeoutStopSec``, 150 s, exceeds the default ``segment_deadline_seconds``).

A deploy replaces the release the host unit runs from (``/opt/voice-lab/current``) but does
not restart the unit. Given ``code_check`` (``release_code_check``), the runner checks at the
start of every pass, before it claims each job and before each model load, whether a model is
loaded or not, that its own code is still in the active release. When it is not, the job in
hand (if any) has already ended, so nothing stops mid-sentence: it unloads the model, gives
back the GPU slot (and a job it claimed but had not started goes back to the queue) and raises
``RunnerOutdated``, and ``speech run`` exits ``EXIT_OUTDATED`` for systemd to start the new
code. A busy runner therefore restarts after the job it is rendering, not after its idle time.
A release link that is missing, dangling or unreadable is not a deploy: the runner gives back
what it holds the same way, raises ``RunnerMisconfigured`` and exits ``EXIT_MISCONFIGURED``
(78), which the unit's ``RestartPreventExitStatus=78`` does not restart.
"""

from __future__ import annotations

import fcntl
import os
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Final

from ..canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    sha256_stream,
    write_new_private_file,
)
from ..errors import VoiceLabError
from ..finetune import FineTuneDigestCache
from ..models import utc_now
from ..paths import AssetLayout
from .backend import SpeechBackend, SpeechBackendError
from .config import SpeechConfig
from .contracts import (
    API_SAMPLE_RATE_HZ,
    FailureCode,
    GpuSlotHolder,
    RenderedAudio,
    RenderFingerprint,
    SegmentAudio,
    SpeechRenderRecord,
    VoiceReleaseRef,
    milliseconds,
)
from .gpu import GpuSlotAbandoned, GpuSlotLease, GpuSlotTimeout, slot_wanted
from .release import (
    VerifiedRelease,
    VoiceReleaseError,
    current_voice,
    designate_newest,
    verify_voice,
)
from .segments import job_seed, join_wavs, segment_seed, split_segments
from .store import AUDIO_NAME, LEDGER_DIRECTORY, Job, SpeechStore, log_to_stderr

MAX_AUDIO_FRAMES: Final = API_SAMPLE_RATE_HZ * 60 * 30  # 30 minutes: far beyond 2,000 characters
SWEEP_SECONDS: Final = 300.0
# ``speech run``'s exit status when its code is no longer the active release's (EX_TEMPFAIL):
# not success, so the unit's ``Restart=on-failure`` starts the new code.
EXIT_OUTDATED: Final = 75
# ... and when the release link is missing or unreadable (EX_CONFIG): the unit's
# ``RestartPreventExitStatus=78`` keeps systemd from restarting it every ten seconds.
EXIT_MISCONFIGURED: Final = 78
# A family's segment limit is never taken below this (a hard cut needs room for a character).
MIN_SEGMENT_BYTES: Final = 16
# Held by the one runner process (``serve`` for its life, ``run_once`` for its pass).
INSTANCE_LOCK_NAME: Final = ".runner.instance.lock"


class RunnerOutdated(Exception):
    """This runner's code is no longer in the active release; it must be restarted."""


class RunnerMisconfigured(RunnerOutdated):
    """The release link is missing or unreadable: a restart cannot fix it (exit 78)."""


class RunnerBusy(VoiceLabError):
    """Another runner process (the ``voice-lab-speech`` unit) is running."""


@dataclass(frozen=True, slots=True)
class CodeProblem:
    """Why the runner's code is not the active release's, and whether a restart can help: a
    link moved to another release can (exit 75), a link that is missing or unreadable cannot
    (exit 78)."""

    reason: str
    restartable: bool


type CodeCheck = Callable[[], str | CodeProblem | None]


def release_code_check(link: Path) -> CodeCheck:
    """A check that this process's code is still the release ``link`` points at.

    The package's directory is resolved now, at start (``link`` is resolved again at each
    check), so a deploy that moves ``link`` or prunes the old release is noticed.
    Returns: a function answering ``None`` while the code is current, why it is not (a
    string: a restart starts the new code), or a ``CodeProblem`` that a restart cannot fix
    (``link`` is missing, dangling or unreadable).
    """
    package = Path(__file__).resolve().parents[1]

    def check() -> str | CodeProblem | None:
        try:
            active = link.resolve(strict=True)
        except (OSError, RuntimeError):
            return CodeProblem(f"{link} does not resolve", restartable=False)
        if not active.is_dir():
            return CodeProblem(f"{link} is not a release directory", restartable=False)
        if not package.is_dir():
            return f"this runner's code ({package}) is gone"
        if not package.is_relative_to(active):
            return f"this runner's code ({package}) is not in the active release ({active})"
        return None

    return check


@dataclass
class PassReport:
    """What one pass did (printed by the CLI as JSON; job ids and outcomes only)."""

    outcomes: dict[str, str] = field(default_factory=dict)
    audio_removed: int = 0
    records_removed: int = 0


def check_wav(path: Path) -> RenderedAudio:
    """``path``'s digest, size and frames once it is complete 24 kHz mono 16-bit PCM.

    Raises: ``SpeechBackendError`` ``render_failed`` naming what is wrong.
    """
    try:
        with open_regular_no_follow(path) as stream:
            with wave.open(stream, "rb") as handle:
                shape = (handle.getframerate(), handle.getnchannels(), handle.getsampwidth())
                frames = handle.getnframes()
                data_bytes = len(handle.readframes(frames))
            stream.seek(0)
            digest, size = sha256_stream(stream)
    except (wave.Error, EOFError, OSError, VoiceLabError) as error:
        raise SpeechBackendError(
            "render_failed", f"the render is not a PCM WAV file: {error}"
        ) from (error)
    if shape != (API_SAMPLE_RATE_HZ, 1, 2):
        raise SpeechBackendError(
            "render_failed",
            f"the render is {shape[0]} Hz, {shape[1]} channel(s), {8 * shape[2]}-bit",
        )
    if not 1 <= frames <= MAX_AUDIO_FRAMES or data_bytes != frames * 2:
        raise SpeechBackendError("render_failed", "the render is empty or truncated")
    return RenderedAudio(sha256=digest, bytes=size, frames=frames)


class SpeechRunner:
    """One host's job runner over one configuration.

    ``code_check`` (``release_code_check``) is given by ``speech run`` on the host; ``log``
    receives the particulars of every failure (never a text).
    """

    def __init__(
        self,
        config: SpeechConfig,
        backend: SpeechBackend,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        digests: FineTuneDigestCache | None = None,
        slot_poll_seconds: float = 0.5,
        code_check: CodeCheck | None = None,
        log: Callable[[str], None] = log_to_stderr,
    ) -> None:
        self.config = config
        self.backend = backend
        self.clock = clock
        self.monotonic = monotonic
        self.store = SpeechStore(config, log=log)
        self.layout = AssetLayout.validate(Path(config.asset_root))
        self.digests = FineTuneDigestCache() if digests is None else digests
        self.slot_poll_seconds = slot_poll_seconds
        self.lease = GpuSlotLease(Path(config.gpu_slot_root))
        self.code_check = code_check
        self.log = log
        self.last_job = monotonic()
        self._last_sweep: float | None = None
        self._prepared: str | None = None
        # What the last automatic release logged, so a run that keeps failing its checks (or a
        # release directory the runner cannot write) is logged once per change.
        self._release_logged: str | None = None
        # ``serve``'s stop: honoured between jobs, between sentences and while a job waits for
        # the GPU slot, so SIGTERM never waits for a whole job.
        self._stopping: Callable[[], bool] = lambda: False

    @contextmanager
    def _runner_lock(self) -> Iterator[None]:
        descriptor = os.open(
            self.store.root / ".runner.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _instance_lock(
        self, *, stop: Callable[[], bool] | None = None, poll_seconds: float = 0.25
    ) -> int | None:
        """Take the instance lock: at once, or (with ``stop``) waiting until it is free.

        Returns: its descriptor, or ``None`` when ``stop()`` turned true while it waited.
        Raises: ``RunnerBusy`` without ``stop`` when another runner holds it.
        """
        self.store.ensure()
        descriptor = os.open(
            self.store.root / INSTANCE_LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        waiting = False
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if stop is not None and not waiting:
                    waiting = True
                    self.log("waiting for a pass by hand (speech run --once) to end")
                if stop is None:
                    os.close(descriptor)
                    raise RunnerBusy(
                        "another speech runner is running (the voice-lab-speech unit): stop it"
                        " before a pass by hand, or let it render the queue"
                    ) from None
            except BaseException:
                os.close(descriptor)
                raise
            if stop():
                os.close(descriptor)
                return None
            time.sleep(poll_seconds)

    # -- the loop ------------------------------------------------------------------------------

    def serve(self, *, stop: Callable[[], bool], poll_seconds: float = 0.25) -> None:
        """Run passes until ``stop()``; keep the model resident between jobs; unload at the end.

        Holds the instance lock throughout (waiting first while a pass by hand holds it).
        Raises: ``RunnerOutdated`` when its code is no longer the active release's (checked
        every pass and between jobs, never mid-job).
        """
        descriptor = self._instance_lock(stop=stop, poll_seconds=poll_seconds)
        if descriptor is None:
            return
        self._stopping = stop
        try:
            while not stop():
                report = self.run_pending(sweep=self._sweep_due())
                if not report.outcomes:
                    self.yield_if_idle()
                    time.sleep(poll_seconds)
        finally:
            self._stopping = lambda: False
            self.unload()
            os.close(descriptor)

    def run_once(self) -> PassReport:
        """One pass by hand (``speech run --once``), then unload.

        Raises: ``RunnerBusy`` at once, before anything is claimed, while another runner
        (the host unit) runs; ``RunnerOutdated`` as ``run_pending``.
        """
        descriptor = self._instance_lock()
        assert descriptor is not None
        try:
            return self.run_pending()
        finally:
            self.unload()
            os.close(descriptor)

    def _sweep_due(self) -> bool:
        now = self.monotonic()
        if self._last_sweep is None or now - self._last_sweep >= SWEEP_SECONDS:
            self._last_sweep = now
            return True
        return False

    def _require_current_code(self) -> None:
        """Between jobs: unload, give the slot back and raise when the code is outdated.

        Raises: ``RunnerOutdated`` (a restart starts the new code), or its
        ``RunnerMisconfigured`` when the release link is missing or unreadable.
        """
        problem = None if self.code_check is None else self.code_check()
        if problem is None:
            return
        self.unload()
        if isinstance(problem, CodeProblem) and not problem.restartable:
            self.log(
                f"stopping, not restarting: {problem.reason}; fix the release link, then"
                " start voice-lab-speech.service"
            )
            raise RunnerMisconfigured(problem.reason)
        reason = problem.reason if isinstance(problem, CodeProblem) else problem
        self.log(f"restarting: {reason}")
        raise RunnerOutdated(reason)

    def unload(self) -> None:
        self.backend.unload()
        self.lease.release()
        self._prepared = None

    def yield_if_idle(self) -> bool:
        """Unload and give the slot back when idle long enough, or when a long job wants the
        slot and no speech is queued. Returns whether it did."""
        if not (self.backend.loaded or self.lease.held):
            return False
        idle = self.monotonic() - self.last_job >= self.config.idle_unload_seconds
        wanted = slot_wanted(Path(self.config.gpu_slot_root)) and not self.store.queued()
        if idle or wanted or not self.backend.loaded:
            self.unload()
            return True
        return False

    def run_pending(self, *, sweep: bool = True) -> PassReport:
        """One pass: what stopped runners left, then every queued job, then (with ``sweep``)
        retention.

        Raises: ``RunnerOutdated`` when its code is outdated: at the start of the pass, or
        after a job ends and before the next is claimed (never while a job renders).
        """
        self.store.ensure()
        report = PassReport()
        with self._runner_lock():
            self._require_current_code()
            self._recover(report)
            for job in self.store.queued():
                if self._stopping():
                    break
                self._require_current_code()
                if not self.store.claim(job.record.job_id, "runner"):
                    continue
                ended = self._run_safely(job)
                self.last_job = self.monotonic()
                report.outcomes[job.record.job_id] = ended.state.state
            if sweep:
                report.audio_removed, report.records_removed = self.store.sweep(now=self.clock())
        return report

    def _recover(self, report: PassReport) -> None:
        """End or requeue the jobs that a stopped runner or API left owned but unfinished."""
        now = self.clock()
        for job in self.store.jobs():
            if job.state.state != "running":
                continue
            self._safely(report, partial(self._end_unfinished, job), job)
        for job, owner in self.store.orphaned_claims(now=now):
            job_id = job.record.job_id
            waited = (now - job.record.created_at).total_seconds()
            if owner == "cancel" or self.store.cancel_requested(job_id):
                self._safely(report, partial(self._cancel, job), job)
            elif waited >= self.config.queue_timeout_seconds:
                detail = "claimed by a runner that stopped, and its queue time is up"
                self._safely(report, partial(self._fail, job, "queue_timeout", detail), job)
            else:
                try:
                    self.store.requeue(job_id)
                    self.log(f"{job_id}: claimed by a runner that stopped; queued again")
                except Exception as error:  # noqa: BLE001 - the next pass tries again
                    self.log(f"{job_id}: could not be queued again: {type(error).__name__}")

    def _end_unfinished(self, job: Job) -> Job:
        """End a job a stopped runner left ``running``: ``cancelled`` when a cancel asked for
        it, else ``failed internal_error``. Without its ledger entry (the runner stopped before
        writing it) its published sentences are withdrawn first: nothing is served without
        one."""
        job_id = job.record.job_id
        if not ledger_path(self.layout, job_id).exists():
            job = self.store.withdraw_audio(job)
        if self.store.cancel_requested(job_id):
            self.log(f"{job_id}: the job runner stopped while it rendered; cancelled")
            return self._cancel(job)
        return self._fail(job, "internal_error", "the job runner stopped while it rendered")

    def _safely(self, report: PassReport, end: Callable[[], Job], job: Job) -> None:
        try:
            report.outcomes[job.record.job_id] = end().state.state
        except Exception as error:  # noqa: BLE001 - the next pass tries again
            self.log(f"{job.record.job_id}: could not be ended: {type(error).__name__}: {error}")

    # -- one job -------------------------------------------------------------------------------

    def _fail(self, job: Job, code: FailureCode, detail: str) -> Job:
        self.log(f"{job.record.job_id}: failed {code}: {detail}")
        return self.store.finish(job, "failed", now=self.clock(), failure=code)

    def _cancel(self, job: Job) -> Job:
        return self.store.finish(job, "cancelled", now=self.clock())

    def _run_safely(self, job: Job) -> Job:
        """``_run``, with every error ending the job (``internal_error`` when nothing more
        precise fits). Should even that fail, the job keeps its claim and the next pass
        recovers it."""
        try:
            return self._run(job)
        except RunnerOutdated:
            raise
        except Exception as error:  # noqa: BLE001 - a job never wedges the queue
            if not isinstance(error, VoiceLabError | OSError | ValueError):
                self.unload()
            try:
                return self._fail(job, "internal_error", f"{type(error).__name__}: {error}")
            except Exception as again:  # noqa: BLE001
                self.log(f"{job.record.job_id}: could not be ended: {type(again).__name__}")
                return job

    def _release_newest(self) -> None:
        """Release the newest finished run when it is newer than the newest release (D-055).

        Never fails the job: a run that fails its checks, a run record that cannot be read or a
        release that cannot be written is logged (once per change) and the current voice speaks.
        """
        refusals: list[str] = []
        try:
            written = designate_newest(
                self.layout,
                now=self.clock(),
                digests=self.digests,
                refused=lambda run_id, why: refusals.append(f"run {run_id} is not released: {why}"),
            )
        except Exception as error:  # noqa: BLE001 - the current voice speaks; the log says why
            refusals.append(
                f"the newest finished run cannot be released: {type(error).__name__}: {error}"
            )
            written = None
        if written is not None:
            release, _ = written
            self.log(f"released run {release.run_id} automatically as {release.release_id}")
        message = "; ".join(refusals) or None
        if message is not None and message != self._release_logged:
            self.log(message)
        self._release_logged = message

    def _verified(self) -> VerifiedRelease:
        """The current voice re-hashed: the newest release (after releasing a newer finished
        run, ``_release_newest``), of the pinned subject, or the base voice.

        Raises: ``VoiceReleaseError`` (any other error reading the voice is
        ``release_changed``).
        """
        self._release_newest()
        try:
            voice, digest = current_voice(self.layout, self.config)
            verified = verify_voice(
                self.layout, voice, digest, config=self.config, digests=self.digests
            )
        except VoiceReleaseError:
            raise
        except (VoiceLabError, OSError, ValueError) as error:
            raise VoiceReleaseError("release_changed", f"{type(error).__name__}: {error}") from (
                error
            )
        return verified

    def _still_current(self, verified: VerifiedRelease) -> None:
        """Before each sentence: the same release is still current.

        Raises: ``VoiceReleaseError``.
        """
        try:
            _, digest = current_voice(self.layout, self.config)
        except VoiceReleaseError:
            raise
        except (VoiceLabError, OSError, ValueError) as error:
            raise VoiceReleaseError("release_changed", f"{type(error).__name__}: {error}") from (
                error
            )
        if digest != verified.digest:
            raise VoiceReleaseError("release_changed", "another release was designated")

    def _run(self, job: Job) -> Job:
        now = self.clock()
        job_id = job.record.job_id
        if self.store.cancel_requested(job_id):
            return self._cancel(job)
        waited = (now - job.record.created_at).total_seconds()
        remaining = self.config.queue_timeout_seconds - waited
        if remaining <= 0:
            return self._fail(job, "queue_timeout", "the job waited longer than the queue allows")
        try:
            verified = self._verified()
        except VoiceReleaseError as error:
            return self._fail(job, "voice_release_changed", str(error))
        holder = GpuSlotHolder(kind="speech", job_id=job_id, started_at=now)
        try:
            self.lease.acquire(
                holder,
                timeout_seconds=remaining,
                poll_seconds=self.slot_poll_seconds,
                stop=lambda: self._stopping() or self.store.cancel_requested(job_id),
            )
        except GpuSlotTimeout:
            return self._fail(job, "queue_timeout", "the GPU slot stayed busy past the queue limit")
        except GpuSlotAbandoned:
            if self.store.cancel_requested(job_id):
                return self._cancel(job)
            self.store.requeue(job_id)  # the runner is stopping: the job waits for the next
            return job
        if self._stopping():
            self.store.requeue(job_id)
            return job
        try:
            self._require_current_code()  # a deploy while it waited for the slot
        except RunnerOutdated:
            self.store.requeue(job_id)
            raise
        try:
            fingerprint = self.backend.prepare(verified)
        except SpeechBackendError as error:
            self.unload()
            code = "internal_error" if error.code == "cancelled" else error.code
            return self._fail(job, code, error.detail)
        except (VoiceLabError, OSError, ValueError) as error:
            self.unload()
            return self._fail(job, "runtime_unavailable", f"{type(error).__name__}: {error}")
        self._prepared = verified.digest
        return self._render(job, verified, fingerprint)

    def _render(self, job: Job, verified: VerifiedRelease, fingerprint: RenderFingerprint) -> Job:
        job_id = job.record.job_id
        text = self.store.read_input(job_id).text
        sentences = split_segments(
            text, max_bytes=max(verified.max_segment_bytes, MIN_SEGMENT_BYTES)
        )
        seed = job_seed(
            job.record.text_hmac_sha256, verified.digest, canonical_sha256(fingerprint.settings)
        )
        running = self.store.mark_running(job, now=self.clock(), segment_count=len(sentences))
        outcome = "succeeded"
        failure: FailureCode | None = None
        audio: RenderedAudio | None = None
        try:
            for index, sentence in enumerate(sentences, start=1):
                if self.store.cancel_requested(job_id):
                    outcome = "cancelled"
                    break
                if self._stopping():
                    outcome, failure = "failed", "internal_error"
                    self.log(f"{job_id}: the job runner was stopped between sentences")
                    break
                self._still_current(verified)
                partial = self.store.partial_segment_path(job_id, index)
                partial.unlink(missing_ok=True)
                self.backend.render_segment(
                    sentence.text,
                    seed=segment_seed(seed, index),
                    output=partial,
                    deadline_seconds=float(self.config.segment_deadline_seconds),
                )
                checked = check_wav(partial)
                running = self.store.publish_segment(
                    running,
                    SegmentAudio(
                        index=index,
                        text_start=sentence.start,
                        text_end=sentence.end,
                        **checked.model_dump(),
                    ),
                )
            if outcome == "succeeded":
                joined = self.store.partial_audio_path(job_id)
                joined.unlink(missing_ok=True)
                join_wavs(
                    [
                        self.store.segment_path(job_id, index)
                        for index in range(1, len(sentences) + 1)
                    ],
                    joined,
                )
                audio = check_wav(joined)
                os.replace(joined, self.store.audio_path(job_id))
        except SpeechBackendError as error:
            if error.code == "cancelled":
                outcome = "cancelled"
            else:
                outcome, failure = "failed", error.code
                self.log(f"{job_id}: failed {error.code}: {error.detail}")
        except VoiceReleaseError as error:
            outcome, failure = "failed", "voice_release_changed"
            self.log(f"{job_id}: stopped, {failure}: {error}")
        except Exception as error:  # noqa: BLE001 - the job ends, with its ledger entry
            outcome, failure = "failed", "internal_error"
            self.log(f"{job_id}: failed internal_error: {type(error).__name__}: {error}")
        finished = self.clock()
        try:
            self._ledger(running, verified, finished, outcome, failure, fingerprint, seed, audio)
        except Exception as error:  # noqa: BLE001 - nothing is served without its ledger entry
            self.log(f"{job_id}: the ledger entry could not be written: {type(error).__name__}")
            outcome, failure, audio = "failed", "internal_error", None
            running = self.store.withdraw_audio(running)
        if outcome == "failed":
            assert failure is not None
            return self.store.finish(running, "failed", now=finished, failure=failure)
        if outcome == "cancelled":
            return self.store.finish(running, "cancelled", now=finished)
        return self.store.finish(running, "succeeded", now=finished, audio=audio)

    def _ledger(
        self,
        job: Job,
        verified: VerifiedRelease,
        finished: datetime,
        outcome: str,
        failure: FailureCode | None,
        fingerprint: RenderFingerprint,
        seed: int,
        audio: RenderedAudio | None,
    ) -> None:
        started = job.state.started_at
        assert started is not None and job.state.segment_count is not None
        release = verified.release
        record = SpeechRenderRecord.model_validate(
            {
                "job_id": job.record.job_id,
                "caller_id": job.record.caller_id,
                "text_hmac_sha256": job.record.text_hmac_sha256,
                "text_characters": job.record.text_characters,
                "release": VoiceReleaseRef(
                    release_id=release.release_id, release_sha256=verified.digest
                ),
                "model_family": release.model_family,
                "outcome": outcome,
                "failure": failure,
                "fingerprint": fingerprint,
                "seed": seed,
                "segment_count": job.state.segment_count,
                "segment_sha256": tuple(segment.sha256 for segment in job.state.segments),
                "output": audio,
                "created_at": job.record.created_at,
                "started_at": started,
                "finished_at": finished,
                "queue_ms": milliseconds(job.record.created_at, started),
                "render_ms": milliseconds(started, finished),
                "audio_ms": (
                    None if audio is None else round(audio.frames * 1000 / API_SAMPLE_RATE_HZ)
                ),
            }
        )
        directory = self.layout.directory("provenance") / LEDGER_DIRECTORY
        directory.mkdir(mode=0o700, exist_ok=True)
        write_new_private_file(
            directory / f"{record.job_id}.json", canonical_json_bytes(record) + b"\n", mode=0o400
        )


def ledger_path(layout: AssetLayout, job_id: str) -> Path:
    return layout.directory("provenance") / LEDGER_DIRECTORY / f"{job_id}.json"


__all__ = [
    "AUDIO_NAME",
    "EXIT_OUTDATED",
    "PassReport",
    "CodeProblem",
    "EXIT_MISCONFIGURED",
    "RunnerMisconfigured",
    "RunnerOutdated",
    "SpeechRunner",
    "check_wav",
    "ledger_path",
    "release_code_check",
]

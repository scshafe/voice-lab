"""The speech job runner: the queue, the GPU slot and the resident model, with no network.

@canonical speech.runner -- ``voice-lab speech run`` (the long-running host unit
``voice-lab-speech``, ``PrivateNetwork=yes``) calls ``SpeechRunner.serve``; ``--once`` makes
one pass (``run_pending``). Under a runner lock a pass first recovers what a stopped runner
(or API) left: a job left ``running`` fails ``internal_error``; a queued job someone claimed
but never ended is cancelled when a cancel asked for it, failed ``queue_timeout`` when its
queue time is up, else given back to the queue. Then it claims each queued job, oldest first,
and for each:

1. ends it ``cancelled`` when a cancel arrived, and fails it ``queue_timeout`` when it waited
   longer than the configuration allows;
2. re-reads the current voice release, refuses one of another subject, re-hashes what it
   names (``voice_release_changed``) and re-checks its consent record (``consent_changed``);
3. holds the GPU slot (``gpu.GpuSlotLease``, kept across jobs while the model is resident;
   a cancel ends the wait) and has the backend load the release (``runtime_unavailable``
   without a runtime);
4. splits the text into segments no longer than the release's family takes in one pass
   (``segments``), and renders them one after another, each with its own seed derived from
   the job's; before each it checks that the release is still current and its consent still
   valid, so a revocation stops a running render at its next sentence; checks each WAV and
   publishes it at once as a segment of the job; a cancel is honoured between sentences;
5. joins the segments into the job's audio, writes the render ledger entry
   (``provenance/speech-renders/<job id>.json``: never the text), ends the job and queues
   its webhook event.

Any other error ends the job ``failed`` with a code (``internal_error`` when nothing more
precise fits); the particulars go to the runner's log (stderr, the journal), never to the
caller. Between jobs ``serve`` unloads the model and gives the slot back after the idle time,
or at once when a long job wants the slot and no speech is queued; it sweeps retention every
few minutes. A job that started rendering has a ledger entry, whatever its outcome, unless
the runner itself was stopped mid-render.

A deploy replaces the release the host unit runs from (``/opt/voice-lab/current``) but does
not restart the unit. Given ``code_check`` (``release_code_check``), the runner checks before
every model load, and whenever it is idle without a model, that its own code is still in the
active release; when it is not it gives back what it holds and raises ``RunnerOutdated``, and
``speech run`` exits ``EXIT_OUTDATED`` for systemd to start the new code.
"""

from __future__ import annotations

import fcntl
import os
import sys
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
    current_release,
    require_release_consent,
    require_subject,
    verify_release,
)
from .segments import job_seed, join_wavs, segment_seed, split_sentences
from .store import AUDIO_NAME, Job, SpeechStore

LEDGER_DIRECTORY: Final = "speech-renders"
MAX_AUDIO_FRAMES: Final = API_SAMPLE_RATE_HZ * 60 * 30  # 30 minutes: far beyond 2,000 characters
SWEEP_SECONDS: Final = 300.0
# ``speech run``'s exit status when its code is no longer the active release's (EX_TEMPFAIL):
# not success, so the unit's ``Restart=on-failure`` starts the new code.
EXIT_OUTDATED: Final = 75
# A family's segment limit is never taken below this (a hard cut needs room for a character).
MIN_SEGMENT_BYTES: Final = 16


class RunnerOutdated(Exception):
    """This runner's code is no longer in the active release; it must be restarted."""


def release_code_check(link: Path) -> Callable[[], str | None]:
    """A check that this process's code is still the release ``link`` points at.

    The package's directory is resolved now, at start (``link`` is resolved again at each
    check), so a deploy that moves ``link`` or prunes the old release is noticed.
    Returns: a function answering ``None`` while the code is current, else why it is not.
    """
    package = Path(__file__).resolve().parents[1]

    def check() -> str | None:
        try:
            active = link.resolve(strict=True)
        except OSError:
            return f"{link} does not resolve"
        if not package.is_dir():
            return f"this runner's code ({package}) is gone"
        if not package.is_relative_to(active):
            return f"this runner's code ({package}) is not in the active release ({active})"
        return None

    return check


def _stderr(message: str) -> None:
    print(f"voice-lab speech: {message}", file=sys.stderr, flush=True)


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


def _release_failure(error: VoiceReleaseError) -> FailureCode:
    return "consent_changed" if error.code == "consent_changed" else "voice_release_changed"


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
        code_check: Callable[[], str | None] | None = None,
        log: Callable[[str], None] = _stderr,
    ) -> None:
        self.config = config
        self.backend = backend
        self.clock = clock
        self.monotonic = monotonic
        self.store = SpeechStore(config)
        self.layout = AssetLayout.validate(Path(config.asset_root))
        self.digests = FineTuneDigestCache() if digests is None else digests
        self.slot_poll_seconds = slot_poll_seconds
        self.lease = GpuSlotLease(Path(config.gpu_slot_root))
        self.code_check = code_check
        self.log = log
        self.last_job = monotonic()
        self._last_sweep: float | None = None
        self._prepared: str | None = None

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

    # -- the loop ------------------------------------------------------------------------------

    def serve(self, *, stop: Callable[[], bool], poll_seconds: float = 0.25) -> None:
        """Run passes until ``stop()``; keep the model resident between jobs; unload at the end.

        Raises: ``RunnerOutdated`` when, idle and without a model, its code is no longer the
        active release's.
        """
        try:
            while not stop():
                report = self.run_pending(sweep=self._sweep_due())
                if not report.outcomes:
                    self.yield_if_idle()
                    if not self.backend.loaded:
                        self._require_current_code()
                    time.sleep(poll_seconds)
        finally:
            self.unload()

    def _sweep_due(self) -> bool:
        now = self.monotonic()
        if self._last_sweep is None or now - self._last_sweep >= SWEEP_SECONDS:
            self._last_sweep = now
            return True
        return False

    def _require_current_code(self) -> None:
        problem = None if self.code_check is None else self.code_check()
        if problem is not None:
            self.log(f"restarting: {problem}")
            raise RunnerOutdated(problem)

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

        Raises: ``RunnerOutdated`` (before it claims anything) when its code is outdated.
        """
        self.store.ensure()
        report = PassReport()
        with self._runner_lock():
            self._recover(report)
            for job in self.store.queued():
                if not self.backend.loaded:
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
            if job.state.state == "running":
                self._safely(
                    report,
                    partial(
                        self._fail,
                        job,
                        "internal_error",
                        "the job runner stopped while it rendered",
                    ),
                    job,
                )
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

    def _verified(self, at: datetime) -> VerifiedRelease:
        """The current release, of the pinned subject, re-hashed, with its consent re-checked.

        Raises: ``VoiceReleaseError`` (any other error reading the release is
        ``release_changed``).
        """
        try:
            release, digest = current_release(self.layout)
            require_subject(release, self.config.subject_id)
            verified = verify_release(self.layout, release, digest, digests=self.digests)
        except VoiceReleaseError:
            raise
        except (VoiceLabError, OSError, ValueError) as error:
            raise VoiceReleaseError("release_changed", f"{type(error).__name__}: {error}") from (
                error
            )
        require_release_consent(self.layout, release, at=at, subject_id=self.config.subject_id)
        return verified

    def _still_current(self, verified: VerifiedRelease) -> None:
        """Before each sentence: the same release is current and its consent still valid.

        Raises: ``VoiceReleaseError``.
        """
        try:
            release, digest = current_release(self.layout)
        except VoiceReleaseError:
            raise
        except (VoiceLabError, OSError, ValueError) as error:
            raise VoiceReleaseError("release_changed", f"{type(error).__name__}: {error}") from (
                error
            )
        if digest != verified.digest:
            raise VoiceReleaseError("release_changed", "another release was designated")
        require_release_consent(
            self.layout, release, at=self.clock(), subject_id=self.config.subject_id
        )

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
            verified = self._verified(now)
        except VoiceReleaseError as error:
            return self._fail(job, _release_failure(error), str(error))
        holder = GpuSlotHolder(kind="speech", job_id=job_id, started_at=now)
        try:
            self.lease.acquire(
                holder,
                timeout_seconds=remaining,
                poll_seconds=self.slot_poll_seconds,
                stop=lambda: self.store.cancel_requested(job_id),
            )
        except GpuSlotTimeout:
            return self._fail(job, "queue_timeout", "the GPU slot stayed busy past the queue limit")
        except GpuSlotAbandoned:
            return self._cancel(job)
        if not self.backend.loaded or self._prepared != verified.digest:
            try:
                self._require_current_code()
            except RunnerOutdated:
                self.unload()
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
        sentences = split_sentences(
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
                self._still_current(verified)
                partial = self.store.partial_segment_path(job_id, index)
                partial.unlink(missing_ok=True)
                self.backend.render_segment(
                    sentence,
                    seed=segment_seed(seed, index),
                    output=partial,
                    deadline_seconds=float(self.config.segment_deadline_seconds),
                )
                checked = check_wav(partial)
                running = self.store.publish_segment(
                    running, SegmentAudio(index=index, **checked.model_dump())
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
            outcome, failure = "failed", _release_failure(error)
            self.log(f"{job_id}: stopped, {failure}: {error}")
        except Exception as error:  # noqa: BLE001 - the job ends, with its ledger entry
            outcome, failure = "failed", "internal_error"
            self.log(f"{job_id}: failed internal_error: {type(error).__name__}: {error}")
        finished = self.clock()
        try:
            self._ledger(running, verified, finished, outcome, failure, fingerprint, seed, audio)
        except Exception as error:  # noqa: BLE001 - nothing is served without its ledger entry
            self.log(f"{job_id}: the ledger entry could not be written: {type(error).__name__}")
            self.store.audio_path(job_id).unlink(missing_ok=True)
            outcome, failure, audio = "failed", "internal_error", None
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
                "consent_id": release.consent_id,
                "consent_sha256": release.consent_sha256,
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
    "RunnerOutdated",
    "SpeechRunner",
    "check_wav",
    "ledger_path",
    "release_code_check",
]

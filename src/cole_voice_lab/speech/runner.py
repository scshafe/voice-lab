"""The speech job runner: the queue, the GPU slot and the resident model, with no network.

@canonical speech.runner -- ``voice-lab speech run`` (the long-running host unit
``voice-lab-speech``, ``PrivateNetwork=yes``) calls ``SpeechRunner.serve``; ``--once`` makes
one pass (``run_pending``). Under a runner lock a pass first fails any job a stopped runner
left ``running``, then claims each queued job, oldest first, and for each:

1. fails it ``queue_timeout`` when it waited longer than the configuration allows, and ends
   it ``cancelled`` when a cancel arrived;
2. re-reads the current voice release and re-hashes what it names (``voice_release_changed``)
   and re-checks its consent record (``consent_changed``);
3. holds the GPU slot (``gpu.GpuSlotLease``, kept across jobs while the model is resident)
   and has the backend load the release (``runtime_unavailable`` without a runtime);
4. splits the text into sentences (``segments``), and renders them one after another, each
   with its own seed derived from the job's; checks each WAV and publishes it at once as a
   segment of the job; a cancel is honoured between sentences;
5. joins the segments into the job's audio, writes the render ledger entry
   (``provenance/speech-renders/<job id>.json``: never the text), ends the job and queues
   its webhook event.

Between jobs ``serve`` unloads the model and gives the slot back after the idle time, or at
once when a long job wants the slot and no speech is queued; it sweeps retention every few
minutes. A job that started rendering has a ledger entry, whatever its outcome, unless the
runner itself was stopped mid-render: the next pass then fails that job ``internal_error``.
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
from pathlib import Path
from typing import Final

from ..canonical import (
    canonical_json_bytes,
    open_regular_no_follow,
    sha256_stream,
    write_new_private_file,
)
from ..errors import VoiceLabError
from ..finetune import FineTuneDigestCache
from ..models import utc_now
from ..paths import AssetLayout
from ..recording_contracts import SPEECH_DEFAULT_NFE_STEPS
from .backend import SpeechBackend, SpeechBackendError
from .config import SpeechConfig
from .contracts import (
    FailureCode,
    GpuSlotHolder,
    RenderedAudio,
    RenderFingerprint,
    SegmentAudio,
    SpeechRenderRecord,
    VoiceReleaseRef,
    milliseconds,
)
from .gpu import GpuSlotLease, GpuSlotTimeout, slot_wanted
from .release import (
    VerifiedRelease,
    VoiceReleaseError,
    current_release,
    require_release_consent,
    verify_release,
)
from .segments import job_seed, join_wavs, max_segment_characters, segment_seed, split_sentences
from .store import AUDIO_NAME, Job, SpeechStore

LEDGER_DIRECTORY: Final = "speech-renders"
MAX_AUDIO_FRAMES: Final = 24_000 * 60 * 30  # 30 minutes: far beyond any 2,000-character text
SWEEP_SECONDS: Final = 300.0


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
    if shape != (24_000, 1, 2):
        raise SpeechBackendError(
            "render_failed",
            f"the render is {shape[0]} Hz, {shape[1]} channel(s), {8 * shape[2]}-bit",
        )
    if not 1 <= frames <= MAX_AUDIO_FRAMES or data_bytes != frames * 2:
        raise SpeechBackendError("render_failed", "the render is empty or truncated")
    return RenderedAudio(sha256=digest, bytes=size, frames=frames)


class SpeechRunner:
    """One host's job runner over one configuration."""

    def __init__(
        self,
        config: SpeechConfig,
        backend: SpeechBackend,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        digests: FineTuneDigestCache | None = None,
        slot_poll_seconds: float = 0.5,
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
        self.last_job = monotonic()
        self._last_sweep: float | None = None

    @property
    def nfe_steps(self) -> int:
        runtime = self.config.runtime
        return SPEECH_DEFAULT_NFE_STEPS if runtime is None else runtime.nfe_steps

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
        """Run passes until ``stop()``; keep the model resident between jobs; unload at the end."""
        try:
            while not stop():
                report = self.run_pending(sweep=self._sweep_due())
                if not report.outcomes:
                    self.yield_if_idle()
                    time.sleep(poll_seconds)
        finally:
            self.unload()

    def _sweep_due(self) -> bool:
        now = self.monotonic()
        if self._last_sweep is None or now - self._last_sweep >= SWEEP_SECONDS:
            self._last_sweep = now
            return True
        return False

    def unload(self) -> None:
        self.backend.unload()
        self.lease.release()

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
        """One pass: orphans, then every queued job, then (with ``sweep``) retention."""
        self.store.ensure()
        report = PassReport()
        with self._runner_lock():
            for job in self.store.jobs():
                if job.state.state == "running":
                    ended = self.store.finish(
                        job,
                        "failed",
                        now=self.clock(),
                        failure=("internal_error", "the job runner stopped while it rendered"),
                    )
                    report.outcomes[job.record.job_id] = ended.state.state
            for job in self.store.queued():
                if not self.store.claim(job.record.job_id, "runner"):
                    continue
                ended = self._run(job)
                self.last_job = self.monotonic()
                report.outcomes[job.record.job_id] = ended.state.state
            if sweep:
                report.audio_removed, report.records_removed = self.store.sweep(now=self.clock())
        return report

    # -- one job -------------------------------------------------------------------------------

    def _fail(self, job: Job, code: FailureCode, detail: str) -> Job:
        return self.store.finish(job, "failed", now=self.clock(), failure=(code, detail))

    def _run(self, job: Job) -> Job:
        now = self.clock()
        job_id = job.record.job_id
        if self.store.cancel_requested(job_id):
            return self.store.finish(job, "cancelled", now=now)
        waited = (now - job.record.created_at).total_seconds()
        remaining = self.config.queue_timeout_seconds - waited
        if remaining <= 0:
            return self._fail(job, "queue_timeout", "the job waited longer than the queue allows")
        try:
            release, digest = current_release(self.layout)
            verified = verify_release(self.layout, release, digest, digests=self.digests)
            require_release_consent(self.layout, release, at=now)
        except VoiceReleaseError as error:
            code: FailureCode = (
                "consent_changed" if error.code == "consent_changed" else "voice_release_changed"
            )
            return self._fail(job, code, str(error))
        holder = GpuSlotHolder(kind="speech", job_id=job_id, started_at=now)
        try:
            self.lease.acquire(
                holder, timeout_seconds=remaining, poll_seconds=self.slot_poll_seconds
            )
        except GpuSlotTimeout:
            return self._fail(job, "queue_timeout", "the GPU slot stayed busy past the queue limit")
        try:
            fingerprint = self.backend.prepare(verified)
        except SpeechBackendError as error:
            self.unload()
            code = "internal_error" if error.code == "cancelled" else error.code
            return self._fail(job, code, error.detail)
        return self._render(job, verified, fingerprint)

    def _render(self, job: Job, verified: VerifiedRelease, fingerprint: RenderFingerprint) -> Job:
        job_id = job.record.job_id
        text = self.store.read_input(job_id).text
        reference = verified.resolution.reference
        sentences = split_sentences(
            text, max_characters=max_segment_characters(reference.text, reference.frames)
        )
        seed = job_seed(job.record.text_sha256, verified.digest, fingerprint.nfe_steps)
        running = self.store.mark_running(job, now=self.clock(), segment_count=len(sentences))
        outcome = "succeeded"
        failure: tuple[FailureCode, str] | None = None
        audio: RenderedAudio | None = None
        try:
            for index, sentence in enumerate(sentences, start=1):
                if self.store.cancel_requested(job_id):
                    outcome = "cancelled"
                    break
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
                outcome = "failed"
                failure = (error.code, error.detail)
        except (OSError, ValueError, wave.Error) as error:
            outcome = "failed"
            failure = ("internal_error", f"{type(error).__name__}")
        finished = self.clock()
        self._ledger(running, verified, finished, outcome, failure, fingerprint, seed, audio)
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
        failure: tuple[FailureCode, str] | None,
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
                "text_sha256": job.record.text_sha256,
                "text_characters": job.record.text_characters,
                "release": VoiceReleaseRef(
                    release_id=release.release_id, release_sha256=verified.digest
                ),
                "consent_id": release.consent_id,
                "consent_sha256": release.consent_sha256,
                "outcome": outcome,
                "failure": None if failure is None else failure[0],
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
                "audio_ms": None if audio is None else round(audio.frames * 1000 / 24_000),
            }
        )
        directory = self.layout.directory("provenance") / LEDGER_DIRECTORY
        directory.mkdir(mode=0o700, exist_ok=True)
        write_new_private_file(
            directory / f"{record.job_id}.json", canonical_json_bytes(record) + b"\n", mode=0o400
        )


def ledger_path(layout: AssetLayout, job_id: str) -> Path:
    return layout.directory("provenance") / LEDGER_DIRECTORY / f"{job_id}.json"


__all__ = ["AUDIO_NAME", "PassReport", "SpeechRunner", "check_wav", "ledger_path"]

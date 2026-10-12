"""The speech job runner: one pass over the queue, in the GPU slot, with no network.

@canonical speech.runner -- ``voice-lab speech run`` (the host unit ``voice-lab-speech``,
``PrivateNetwork=yes``) calls ``run_pending``. Under a runner lock it first fails any job a
stopped runner left ``running``, then claims each queued job, oldest first, and for each:

1. fails it ``queue_timeout`` when it waited longer than the configuration allows, and ends
   it ``cancelled`` when a cancel arrived;
2. re-reads the current voice release and re-hashes what it names (``voice_release_changed``)
   and re-checks its consent record (``consent_changed``);
3. holds the GPU slot (``gpu.hold_slot``) and renders through the ``SpeechBackend`` port,
   which polls for a cancel and the deadline;
4. checks the WAV (24 kHz, mono, 16-bit, complete), publishes it as the job's audio, writes
   the render ledger entry (``provenance/speech-renders/<job id>.json``: never the text),
   ends the job and queues its webhook event.

Then it sweeps retention. A job that started rendering has a ledger entry, whatever its
outcome, unless the runner itself was stopped mid-render: the next pass then fails that job
``internal_error``.
"""

from __future__ import annotations

import fcntl
import os
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from .backend import RenderRequest, SpeechBackend, SpeechBackendError, discard_render_scratch
from .config import SpeechConfig
from .contracts import (
    FailureCode,
    GpuSlotHolder,
    RenderedAudio,
    RenderFingerprint,
    SpeechRenderRecord,
    VoiceReleaseRef,
    milliseconds,
)
from .gpu import GpuSlotTimeout, hold_slot
from .release import (
    VerifiedRelease,
    VoiceReleaseError,
    current_release,
    require_release_consent,
    verify_release,
)
from .store import AUDIO_NAME, Job, SpeechStore

LEDGER_DIRECTORY: Final = "speech-renders"
MAX_AUDIO_FRAMES: Final = 24_000 * 60 * 30  # 30 minutes: far beyond any 2,000-character text


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
        digests: FineTuneDigestCache | None = None,
        slot_poll_seconds: float = 0.5,
    ) -> None:
        self.config = config
        self.backend = backend
        self.clock = clock
        self.store = SpeechStore(config)
        self.layout = AssetLayout.validate(Path(config.asset_root))
        self.digests = FineTuneDigestCache() if digests is None else digests
        self.slot_poll_seconds = slot_poll_seconds

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

    def run_pending(self) -> PassReport:
        """One pass: orphans, then every queued job, then retention."""
        self.store.ensure()
        report = PassReport()
        with self._runner_lock():
            for job in self.store.jobs():
                if job.state.state == "running":
                    discard_render_scratch(self.layout.root, job.record.job_id)
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
                report.outcomes[job.record.job_id] = ended.state.state
            report.audio_removed, report.records_removed = self.store.sweep(now=self.clock())
        return report

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
        holder = GpuSlotHolder(
            kind="speech",
            job_id=job_id,
            started_at=now,
            expected_end_at=now + timedelta(seconds=self.config.render_deadline_seconds),
        )
        try:
            with hold_slot(
                Path(self.config.gpu_slot_root),
                holder,
                timeout_seconds=remaining,
                poll_seconds=self.slot_poll_seconds,
            ):
                return self._render(job, verified)
        except GpuSlotTimeout:
            return self._fail(job, "queue_timeout", "the GPU slot stayed busy past the queue limit")

    def _render(self, job: Job, verified: VerifiedRelease) -> Job:
        job_id = job.record.job_id
        running = self.store.mark_running(job, now=self.clock())
        started = running.state.started_at
        assert started is not None
        partial = self.store.partial_audio_path(job_id)
        partial.unlink(missing_ok=True)
        request = RenderRequest(
            job_id=job_id,
            job_directory=self.store.job_directory(job_id),
            asset_root=self.layout.root,
            release=verified,
            output=partial,
            deadline_seconds=float(self.config.render_deadline_seconds),
        )
        fingerprint: RenderFingerprint | None = None
        audio: RenderedAudio | None = None
        try:
            fingerprint = self.backend.render(
                request, cancelled=lambda: self.store.cancel_requested(job_id)
            )
            audio = check_wav(partial)
            os.replace(partial, self.store.audio_path(job_id))
        except SpeechBackendError as error:
            finished = self.clock()
            outcome = "cancelled" if error.code == "cancelled" else "failed"
            failure: FailureCode | None = None if error.code == "cancelled" else error.code
            self._ledger(running, verified, finished, outcome, failure, fingerprint, None)
            if failure is None:
                return self.store.finish(running, "cancelled", now=finished)
            return self.store.finish(
                running, "failed", now=finished, failure=(failure, error.detail)
            )
        finished = self.clock()
        self._ledger(running, verified, finished, "succeeded", None, fingerprint, audio)
        return self.store.finish(running, "succeeded", now=finished, audio=audio)

    def _ledger(
        self,
        job: Job,
        verified: VerifiedRelease,
        finished: datetime,
        outcome: str,
        failure: FailureCode | None,
        fingerprint: RenderFingerprint | None,
        audio: RenderedAudio | None,
    ) -> None:
        started = job.state.started_at
        assert started is not None
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
                "failure": failure,
                "fingerprint": fingerprint,
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

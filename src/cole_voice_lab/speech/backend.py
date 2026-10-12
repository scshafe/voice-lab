"""The port a render goes through, and the F5-TTS backend behind it (docs/SPEECH_API.md §7).

@canonical speech.backend -- the job runner knows only ``SpeechBackend``: given a job's text
(through its ``input.json``) and a verified voice release, write a 24 kHz mono 16-bit WAV to
the given path, or fail with a stable code. Tests use a fake that writes synthetic audio.

``F5SpeechBackend`` runs ``speech.worker`` as a subprocess in a session of its own, which
renders through the existing Speak seam (``finetune.synthesize_speech``, the pinned
``f5_synthesize.py`` in the runtime's interpreter). The backend polls for a cancel and for
the deadline and kills the worker's whole process group for either. Without a configured
runtime, or with a runtime directory that is not there, it fails ``runtime_unavailable``
before starting anything: the PC's ROCm runtime is phase 5's (spike S-2).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import Field, ValidationError

from ..canonical import open_regular_no_follow, write_canonical_json
from ..errors import VoiceLabError
from ..finetune import FineTunePretrainedPins, FineTuneTrustedDigests, discard_speech_output
from ..models import ContractModel, Sha256, StableId
from ..paths import AssetLayout
from .config import AbsolutePath, SpeechRuntime
from .contracts import FailureCode, RenderFingerprint
from .release import VerifiedRelease

WORKER_MODULE: Final = "cole_voice_lab.speech.worker"
WORKER_REQUEST_NAME: Final = "worker-request.json"
WORKER_RESULT_NAME: Final = "worker-result.json"
MAX_RESULT_BYTES: Final = 256 * 1024

type BackendFailure = FailureCode | Literal["cancelled"]


class SpeechBackendError(VoiceLabError):
    """A render that did not produce audio, with its stable code."""

    def __init__(self, code: BackendFailure, detail: str) -> None:
        super().__init__(detail)
        self.code: BackendFailure = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """One render: the job, its directory (holding ``input.json``), the verified release,
    where the WAV goes, and how long it may take."""

    job_id: str
    job_directory: Path
    asset_root: Path
    release: VerifiedRelease
    output: Path
    deadline_seconds: float


class SpeechBackend(Protocol):
    def render(self, request: RenderRequest, *, cancelled: Callable[[], bool]) -> RenderFingerprint:
        """Write ``request.output``; return what spoke.

        Raises: ``SpeechBackendError`` (``cancelled`` when ``cancelled()`` became true).
        """
        ...


class SpeechWorkerRequest(ContractModel):
    """What the worker subprocess renders (the text stays in the job's ``input.json``)."""

    schema_version: Literal["cole-voice-lab.speech-worker-request/v1"] = (
        "cole-voice-lab.speech-worker-request/v1"
    )
    job_id: StableId
    asset_root: AbsolutePath
    job_directory: AbsolutePath
    output: AbsolutePath
    run_id: StableId
    reference_prompt_id: StableId
    checkpoint_sha256: Sha256
    reference_sha256: Sha256
    runtime_root: AbsolutePath
    device: Literal["cuda", "cpu"]
    nfe_steps: int = Field(ge=1, le=256)
    pretrained_pin: FineTunePretrainedPins | None = None
    trusted: FineTuneTrustedDigests = FineTuneTrustedDigests()


class SpeechWorkerResult(ContractModel):
    schema_version: Literal["cole-voice-lab.speech-worker-result/v1"] = (
        "cole-voice-lab.speech-worker-result/v1"
    )
    job_id: StableId
    ok: bool
    code: FailureCode | None = None
    detail: str = Field(default="", max_length=2000)
    fingerprint: RenderFingerprint | None = None
    trusted: FineTuneTrustedDigests = FineTuneTrustedDigests()


def render_operation_id(job_id: str) -> str:
    """The Speak operation a job's worker renders under (``derived/speech/api-<job id>``)."""
    return f"api-{job_id}"


def discard_render_scratch(asset_root: Path, job_id: str) -> None:
    """Remove what a killed or crashed worker left under ``derived/speech/api-<job id>``
    (its synthesizer job file holds the text, its work directory a reference copy)."""
    try:
        discard_speech_output(AssetLayout.validate(asset_root), render_operation_id(job_id))
    except (OSError, VoiceLabError):  # pragma: no cover - a link there is left for the operator
        pass


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class F5SpeechBackend:
    """Renders with the F5-TTS runtime through ``speech.worker`` (one process group per job).

    ``trusted_digests`` names a file where verified digests are kept between renders, so the
    checkpoint is hashed again only when it changes (``finetune.FineTuneDigestCache``).
    ``pretrained_pin`` is a test seam, as for Speak; the shipped runner never passes it.
    """

    def __init__(
        self,
        runtime: SpeechRuntime | None,
        *,
        trusted_digests: Path | None = None,
        pretrained_pin: FineTunePretrainedPins | None = None,
        python: str = sys.executable,
        poll_seconds: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime = runtime
        self.trusted_digests = trusted_digests
        self.pretrained_pin = pretrained_pin
        self.python = python
        self.poll_seconds = poll_seconds
        self.clock = clock

    def _trusted(self) -> FineTuneTrustedDigests:
        if self.trusted_digests is None or not self.trusted_digests.exists():
            return FineTuneTrustedDigests()
        try:
            with open_regular_no_follow(self.trusted_digests) as stream:
                return FineTuneTrustedDigests.model_validate_json(stream.read(MAX_RESULT_BYTES))
        except (ValueError, VoiceLabError):
            return FineTuneTrustedDigests()

    def render(self, request: RenderRequest, *, cancelled: Callable[[], bool]) -> RenderFingerprint:
        runtime = self.runtime
        if runtime is None:
            raise SpeechBackendError(
                "runtime_unavailable",
                "no speech runtime is configured on this host (design phase 5, spike S-2)",
            )
        if not (Path(runtime.runtime_root) / "bin" / "python").is_file():
            raise SpeechBackendError(
                "runtime_unavailable", f"there is no F5-TTS runtime at {runtime.runtime_root}"
            )
        release = request.release.release
        worker_request = SpeechWorkerRequest(
            job_id=request.job_id,
            asset_root=str(request.asset_root),
            job_directory=str(request.job_directory),
            output=str(request.output),
            run_id=release.run_id,
            reference_prompt_id=release.reference_prompt_id,
            checkpoint_sha256=release.checkpoint_sha256,
            reference_sha256=release.reference_sha256,
            runtime_root=runtime.runtime_root,
            device=runtime.device,
            nfe_steps=runtime.nfe_steps,
            pretrained_pin=self.pretrained_pin,
            trusted=self._trusted(),
        )
        request_path = request.job_directory / WORKER_REQUEST_NAME
        result_path = request.job_directory / WORKER_RESULT_NAME
        result_path.unlink(missing_ok=True)
        write_canonical_json(request_path, worker_request)
        try:
            process = subprocess.Popen(
                [self.python, "-I", "-B", "-m", WORKER_MODULE, str(request_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=request.job_directory,
            )
        except OSError as error:
            raise SpeechBackendError(
                "runtime_unavailable", f"the speech worker could not start: {error}"
            ) from error
        try:
            deadline = self.clock() + request.deadline_seconds
            while process.poll() is None:
                if cancelled():
                    _kill_group(process)
                    raise SpeechBackendError("cancelled", "the job was cancelled while it rendered")
                if self.clock() >= deadline:
                    _kill_group(process)
                    raise SpeechBackendError(
                        "render_timeout",
                        f"the render took longer than {request.deadline_seconds:.0f} seconds",
                    )
                time.sleep(self.poll_seconds)
            _kill_group(process)  # whatever the worker left running in its group
            result = self._result(result_path, request.job_id)
        finally:
            request_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)
            discard_render_scratch(request.asset_root, request.job_id)
        if self.trusted_digests is not None:
            write_canonical_json(self.trusted_digests, result.trusted)
        if not result.ok or result.fingerprint is None:
            raise SpeechBackendError(result.code or "render_failed", result.detail)
        return result.fingerprint

    @staticmethod
    def _result(path: Path, job_id: str) -> SpeechWorkerResult:
        try:
            with open_regular_no_follow(path) as stream:
                result = SpeechWorkerResult.model_validate_json(stream.read(MAX_RESULT_BYTES))
        except (ValidationError, ValueError, VoiceLabError) as error:
            raise SpeechBackendError(
                "render_failed", "the speech worker ended without a readable result"
            ) from error
        if result.job_id != job_id:
            raise SpeechBackendError("render_failed", "the speech worker answered another job")
        return result

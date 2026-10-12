"""``python -m cole_voice_lab.speech.worker <request>``: one render, in its own process group.

@canonical speech.worker -- started by ``F5SpeechBackend``. It reads its request
(``SpeechWorkerRequest``) and the job's text, renders through Speak's seam
(``finetune.resolve_speech_model`` and ``finetune.synthesize_speech``) with the release's
run and reference clip into a private ``derived/speech/api-<job>/`` directory, checks that
what spoke is what the release names, copies the WAV to the job's partial audio file and
removes the Speak directory, then writes ``worker-result.json`` beside its request. A
failure is a result with a stable code, never a traceback to the caller.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

from .. import __version__
from ..canonical import open_regular_no_follow, write_canonical_json
from ..errors import VoiceLabError
from ..finetune import (
    FineTuneDigestCache,
    FineTuneError,
    describe_finetune_runtime,
    discard_speech_output,
    resolve_speech_model,
    synthesize_speech,
)
from ..paths import AssetLayout, confined_path
from .backend import (
    WORKER_RESULT_NAME,
    SpeechWorkerRequest,
    SpeechWorkerResult,
    render_operation_id,
)
from .contracts import FailureCode, RenderFingerprint, SpeechJobInput

MAX_REQUEST_BYTES: Final = 256 * 1024
_RELEASE_CHANGED: Final = frozenset(
    {
        "FINETUNE.SOURCE_CHANGED",
        "FINETUNE.CHECKPOINT_MISSING",
        "FINETUNE.RUN_ARCHIVED",
        "FINETUNE.NO_FINISHED_RUN",
        "FINETUNE.REFERENCE_MISSING",
    }
)
_RUNTIME_UNAVAILABLE: Final = frozenset(
    {"FINETUNE.RUNTIME_UNAVAILABLE", "FINETUNE.PRETRAINED_MISMATCH"}
)


def failure_code(error: FineTuneError) -> FailureCode:
    """The speech API's code for a Speak seam failure."""
    if error.code in _RELEASE_CHANGED:
        return "voice_release_changed"
    if error.code in _RUNTIME_UNAVAILABLE:
        return "runtime_unavailable"
    return "render_failed"


def _copy_private(source: Path, destination: Path) -> None:
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    with os.fdopen(descriptor, "wb") as target, open_regular_no_follow(source) as stream:
        while chunk := stream.read(1 << 20):
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())


def render(request: SpeechWorkerRequest) -> SpeechWorkerResult:
    """Render ``request``; every failure becomes a result with its code."""
    digests = FineTuneDigestCache(request.trusted)
    layout = AssetLayout.validate(Path(request.asset_root))
    operation_id = render_operation_id(request.job_id)

    def failed(code: FailureCode, detail: str) -> SpeechWorkerResult:
        return SpeechWorkerResult(
            job_id=request.job_id,
            ok=False,
            code=code,
            detail=detail[:2000],
            trusted=digests.trusted(),
        )

    try:
        with open_regular_no_follow(Path(request.job_directory) / "input.json") as stream:
            job_input = SpeechJobInput.model_validate_json(stream.read(MAX_REQUEST_BYTES))
        if job_input.job_id != request.job_id:
            return failed("internal_error", "the job's input names another job")
        resolution = resolve_speech_model(
            layout, request.run_id, reference_prompt_id=request.reference_prompt_id
        )
        clip = synthesize_speech(
            layout,
            resolution,
            Path(request.runtime_root),
            operation_id=operation_id,
            text=job_input.text,
            nfe_steps=request.nfe_steps,
            device=request.device,
            pretrained_pin=request.pretrained_pin,
            digests=digests,
        )
        if (clip.model, clip.checkpoint_sha256, clip.reference_audio_sha256) != (
            request.run_id,
            request.checkpoint_sha256,
            request.reference_sha256,
        ):
            return failed("voice_release_changed", "what spoke is not what the release names")
        runtime = describe_finetune_runtime(
            Path(request.runtime_root), pretrained_pin=request.pretrained_pin, digests=digests
        )
        _copy_private(confined_path(layout.root, clip.relative_path), Path(request.output))
        fingerprint = RenderFingerprint(
            checkpoint_sha256=clip.checkpoint_sha256,
            reference_sha256=clip.reference_audio_sha256,
            backend="f5-tts",
            runtime={
                "f5_tts_version": runtime.identity.f5_tts_version,
                "torch_version": runtime.identity.torch_version,
                "python_sha256": runtime.identity.python_sha256,
                "pretrained_sha256": runtime.identity.pretrained_sha256,
            },
            device=request.device,
            nfe_steps=request.nfe_steps,
            voice_lab_version=__version__,
        )
        return SpeechWorkerResult(
            job_id=request.job_id, ok=True, fingerprint=fingerprint, trusted=digests.trusted()
        )
    except FineTuneError as error:
        return failed(failure_code(error), str(error))
    except (OSError, ValueError, VoiceLabError) as error:
        return failed("internal_error", f"{type(error).__name__}: {error}")
    finally:
        try:
            discard_speech_output(layout, operation_id)
        except (OSError, VoiceLabError):  # pragma: no cover - nothing to remove, or refused
            pass


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        sys.stderr.write("usage: python -m cole_voice_lab.speech.worker <request.json>\n")
        return 2
    request_path = Path(arguments[0])
    with open_regular_no_follow(request_path) as stream:
        request = SpeechWorkerRequest.model_validate_json(stream.read(MAX_REQUEST_BYTES))
    result = render(request)
    write_canonical_json(request_path.parent / WORKER_RESULT_NAME, result)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

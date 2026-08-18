"""MLX Chatterbox Turbo one-request worker."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

from ..errors import ContractError
from ..models import ExperimentRequest
from .common import (
    WorkerContext,
    WorkerDefinition,
    float_setting,
    integer_setting,
    local_huggingface_overrides,
    worker_main,
)

DEFINITION = WorkerDefinition(
    candidate_id="chatterbox-turbo",
    track="direct-tts",
    code_revision="65fb3ccbecbbbb8b32fd625326fb0bc85d2ad03b",
    model_revisions={
        "mlx-community/chatterbox-turbo-fp16": ("b2d0a13aa7cfff0a06d9acb247ae91c8f19a6d75"),
        "mlx-community/chatterbox-turbo-8bit": ("2f2e21a03863f86a1274d1060dcc188e7cde77e1"),
    },
    auxiliary_revisions={"mlx-community/S3TokenizerV2": "e0c9886f0e1c35ae85b1f27277416fb19fc72bec"},
    allowed_settings=frozenset(
        {"max_tokens", "temperature", "top_k", "top_p", "repetition_penalty", "stream"}
    ),
    package_name="mlx-audio",
    package_version="0.5.0",
    # The official PyTorch path embeds Perth; mlx-audio 0.5.0 invokes no watermark API.
    watermarked=False,
)


def load_model(request: ExperimentRequest, context: WorkerContext) -> object:  # pragma: no cover
    del request
    module = cast(Any, importlib.import_module("mlx_audio.tts.utils"))
    with local_huggingface_overrides(context.auxiliary_paths):
        return module.load_model(context.model_path)


def synthesize(
    model: object,
    request: ExperimentRequest,
    context: WorkerContext,
    output_path: Path | None,
) -> int | None:  # pragma: no cover
    del context
    mx = cast(Any, importlib.import_module("mlx.core"))
    audio_io = cast(Any, importlib.import_module("mlx_audio.audio_io"))

    mx.random.seed(request.seed)
    typed_model = cast(Any, model)
    results = list(
        typed_model.generate(
            text=request.text,
            ref_audio=request.reference.path,
            max_tokens=integer_setting(request, "max_tokens", 800, minimum=1),
            temperature=float_setting(request, "temperature", 0.8),
            top_k=integer_setting(request, "top_k", 1000, minimum=1),
            top_p=float_setting(request, "top_p", 0.95),
            repetition_penalty=float_setting(request, "repetition_penalty", 1.2),
            stream=False,
        )
    )
    if not results:
        raise ContractError("Chatterbox Turbo generated no audio")
    if output_path is not None:
        chunks = [result.audio.reshape(-1) for result in results]
        audio = chunks[0] if len(chunks) == 1 else mx.concatenate(chunks)
        audio_io.write(str(output_path), audio, results[0].sample_rate, format="wav")
    return request.seed


def main() -> int:  # pragma: no cover - isolated process entrypoint
    return worker_main(DEFINITION, load=load_model, synthesize=synthesize)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

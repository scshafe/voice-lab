"""MLX Qwen3-TTS 1.7B Base one-request worker."""

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
    worker_main,
)

DEFINITION = WorkerDefinition(
    candidate_id="qwen3-tts-base",
    track="direct-tts",
    code_revision="65fb3ccbecbbbb8b32fd625326fb0bc85d2ad03b",
    model_revisions={
        "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16": ("a6eb4f68e4b056f1215157bb696209bc82a6db48"),
        "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit": ("e7dd0585652209fa0d7783659aad4e8a324de11c"),
    },
    auxiliary_revisions={},
    allowed_settings=frozenset(
        {
            "max_tokens",
            "temperature",
            "top_k",
            "top_p",
            "repetition_penalty",
            "stream",
        }
    ),
    package_name="mlx-audio",
    package_version="0.5.0",
)

_QWEN_LANGUAGES = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "it": "Italian",
}


def load_model(request: ExperimentRequest, context: WorkerContext) -> object:  # pragma: no cover
    del request
    module = cast(Any, importlib.import_module("mlx_audio.tts.utils"))
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

    if not request.reference.transcript.strip():
        raise ContractError("Qwen Base requires the exact reference transcript")
    language_key = request.language.split("-", maxsplit=1)[0].lower()
    try:
        qwen_language = _QWEN_LANGUAGES[language_key]
    except KeyError as error:
        raise ContractError(f"unsupported Qwen language: {request.language}") from error
    mx.random.seed(request.seed)
    typed_model = cast(Any, model)
    results = list(
        typed_model.generate(
            text=request.text,
            ref_audio=request.reference.path,
            ref_text=request.reference.transcript,
            lang_code=qwen_language,
            max_tokens=integer_setting(request, "max_tokens", 4096, minimum=1),
            temperature=float_setting(request, "temperature", 0.9),
            top_k=integer_setting(request, "top_k", 50, minimum=1),
            top_p=float_setting(request, "top_p", 1.0),
            repetition_penalty=max(1.5, float_setting(request, "repetition_penalty", 1.5)),
            stream=False,
            verbose=False,
        )
    )
    if not results:
        raise ContractError("Qwen generated no audio")
    if output_path is not None:
        chunks = [result.audio.reshape(-1) for result in results]
        audio = chunks[0] if len(chunks) == 1 else mx.concatenate(chunks)
        audio_io.write(str(output_path), audio, results[0].sample_rate, format="wav")
    return request.seed


def main() -> int:  # pragma: no cover - isolated process entrypoint
    return worker_main(DEFINITION, load=load_model, synthesize=synthesize)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

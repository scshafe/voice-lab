"""Official F5-TTS 1.1.22 MPS one-request worker."""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from ..errors import ContractError, RuntimeUnavailableError
from ..models import ExperimentRequest
from .common import (
    WorkerContext,
    WorkerDefinition,
    float_setting,
    integer_setting,
    worker_main,
)

DEFINITION = WorkerDefinition(
    candidate_id="f5-tts",
    track="direct-tts",
    code_revision="9c614e9657089213efc6a7421b30630be138a3f5",
    model_revisions={"SWivid/F5-TTS": "84e5a410d9cead4de2f847e7c9369a6440bdfaca"},
    auxiliary_revisions={"charactr/vocos-mel-24khz": "0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21"},
    allowed_settings=frozenset(
        {
            "nfe_step",
            "cfg_strength",
            "sway_sampling_coef",
            "speed",
            "cross_fade_duration",
        }
    ),
    package_name="f5-tts",
    package_version="1.1.22",
)


def _no_progress[T](iterable: Iterable[T], **kwargs: object) -> Iterable[T]:
    del kwargs
    return iterable


def load_model(request: ExperimentRequest, context: WorkerContext) -> object:  # pragma: no cover
    del request
    torch = cast(Any, importlib.import_module("torch"))
    f5_api = cast(Any, importlib.import_module("f5_tts.api"))

    if not torch.backends.mps.is_available():
        raise RuntimeUnavailableError("F5 worker requires an available Apple MPS device")
    checkpoint = context.model_path / "F5TTS_v1_Base/model_1250000.safetensors"
    vocabulary = context.model_path / "F5TTS_v1_Base/vocab.txt"
    vocoder = context.auxiliary_paths["charactr/vocos-mel-24khz"]
    if not checkpoint.is_file() or not vocabulary.is_file():
        raise RuntimeUnavailableError("curated F5 checkpoint or vocabulary is missing")
    return f5_api.F5TTS(
        model="F5TTS_v1_Base",
        ckpt_file=str(checkpoint),
        vocab_file=str(vocabulary),
        vocoder_local_path=str(vocoder),
        device="mps",
    )


def synthesize(
    model: object,
    request: ExperimentRequest,
    context: WorkerContext,
    output_path: Path | None,
) -> int | None:  # pragma: no cover
    del context
    if not request.reference.transcript.strip():
        raise ContractError("F5 requires reference text to avoid an implicit ASR download")
    typed_model = cast(Any, model)
    typed_model.infer(
        ref_file=request.reference.path,
        ref_text=request.reference.transcript,
        gen_text=request.text,
        show_info=lambda _message: None,
        progress=_no_progress,
        cross_fade_duration=float_setting(request, "cross_fade_duration", 0.15),
        sway_sampling_coef=float_setting(request, "sway_sampling_coef", -1.0),
        cfg_strength=float_setting(request, "cfg_strength", 2.0),
        nfe_step=integer_setting(request, "nfe_step", 32, minimum=1),
        speed=float_setting(request, "speed", 1.0),
        remove_silence=False,
        file_wave=str(output_path) if output_path is not None else None,
        seed=request.seed,
    )
    return request.seed


def main() -> int:  # pragma: no cover - isolated process entrypoint
    return worker_main(DEFINITION, load=load_model, synthesize=synthesize)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Pinned Seed-VC MPS one-request worker for the separate VC track."""

from __future__ import annotations

import argparse
import importlib
import os
import random
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from ..errors import ContractError, RuntimeUnavailableError
from ..models import ExperimentRequest
from .common import (
    WorkerContext,
    WorkerDefinition,
    bool_setting,
    float_setting,
    integer_setting,
    worker_main,
)

DEFINITION = WorkerDefinition(
    candidate_id="seed-vc",
    track="voice-conversion",
    code_revision="51383efd921027683c89e5348211d93ff12ac2a8",
    model_revisions={"Plachta/Seed-VC": "257283f9f41585055e8f858fba4fd044e5caed6e"},
    auxiliary_revisions={
        "openai/whisper-small": "973afd24965f72e36ca33b3055d56a652f456b4d",
        "funasr/campplus": "e4b6ede7ce16997aff4ae69fbca1f0175e2afede",
        "nvidia/bigvgan_v2_22khz_80band_256x": ("633ff708ed5b74903e86ff1298cf4a98e921c513"),
    },
    allowed_settings=frozenset(
        {
            "diffusion_steps",
            "length_adjust",
            "inference_cfg_rate",
            "f0_condition",
            "auto_f0_adjust",
            "semi_tone_shift",
            "fp16",
        }
    ),
    package_name="torch",
    package_version="2.6.0",
)


def load_model(request: ExperimentRequest, context: WorkerContext) -> object:  # pragma: no cover
    del request
    if context.code_path is None or context.code_checkout_integrity_sha256 is None:
        raise RuntimeUnavailableError("Seed-VC requires its clean, parent-verified checkout")
    source = context.code_path / "inference.py"
    if source.is_symlink() or not source.is_file():
        raise RuntimeUnavailableError("Seed-VC pinned checkout has no regular inference.py")
    code_root = str(context.code_path)
    if code_root not in sys.path:
        sys.path.insert(0, code_root)
    module = importlib.import_module("inference")
    if Path(cast(Any, module).__file__).resolve(strict=True) != source.resolve(strict=True):
        raise RuntimeUnavailableError("Seed-VC imported inference from outside its pinned checkout")
    if str(cast(Any, module).device) != "mps":
        raise RuntimeUnavailableError("Seed-VC worker requires an available Apple MPS device")
    return module


def _local_model_resolver(
    context: WorkerContext,
    repository: str,
    filename: str,
    config_filename: str | None = None,
) -> str | tuple[str, str]:
    del config_filename
    try:
        root = context.auxiliary_paths[repository]
    except KeyError as error:
        raise RuntimeUnavailableError(
            f"Seed-VC requested an unprovisioned model: {repository}"
        ) from error
    model_file = root / filename
    if not model_file.is_file():
        raise RuntimeUnavailableError(f"Seed-VC auxiliary file is missing: {model_file}")
    return str(model_file)


def synthesize(
    model: object,
    request: ExperimentRequest,
    context: WorkerContext,
    output_path: Path | None,
) -> int | None:  # pragma: no cover
    if output_path is None or request.source_audio_path is None:
        raise ContractError("Seed-VC requires cold timing, a source performance, and an output")
    if bool_setting(request, "f0_condition", False):
        raise ContractError("the curated Seed-VC profile is the locked non-F0 checkpoint")
    np = cast(Any, importlib.import_module("numpy"))
    torch = cast(Any, importlib.import_module("torch"))
    yaml = cast(Any, importlib.import_module("yaml"))

    random.seed(request.seed)
    np.random.seed(request.seed % (2**32))
    torch.manual_seed(request.seed)
    inference = cast(ModuleType, model)
    config_source = context.model_path / "config_dit_mel_seed_uvit_whisper_small_wavenet.yml"
    checkpoint = context.model_path / "DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth"
    configuration = yaml.safe_load(config_source.read_text(encoding="utf-8"))
    configuration["model_params"]["speech_tokenizer"]["name"] = str(
        context.auxiliary_paths["openai/whisper-small"]
    )
    configuration["model_params"]["vocoder"]["name"] = str(
        context.auxiliary_paths["nvidia/bigvgan_v2_22khz_80band_256x"]
    )
    with tempfile.TemporaryDirectory(prefix="seed-vc-", dir=output_path.parent) as temporary:
        temporary_path = Path(temporary)
        derived_config = temporary_path / "offline-config.yml"
        derived_config.write_text(yaml.safe_dump(configuration), encoding="utf-8")
        generated_directory = temporary_path / "generated"
        arguments = argparse.Namespace(
            source=request.source_audio_path,
            target=request.reference.path,
            output=str(generated_directory),
            diffusion_steps=integer_setting(request, "diffusion_steps", 25, minimum=1),
            length_adjust=float_setting(request, "length_adjust", 1.0),
            inference_cfg_rate=float_setting(request, "inference_cfg_rate", 0.7),
            f0_condition=False,
            auto_f0_adjust=bool_setting(request, "auto_f0_adjust", False),
            semi_tone_shift=integer_setting(request, "semi_tone_shift", 0),
            checkpoint=str(checkpoint),
            config=str(derived_config),
            fp16=bool_setting(request, "fp16", True),
        )
        original_resolver = cast(Any, inference).load_custom_model_from_hf
        cast(Any, inference).load_custom_model_from_hf = (
            lambda repository, filename, config_filename=None: _local_model_resolver(
                context, repository, filename, config_filename
            )
        )
        try:
            cast(Any, inference).main(arguments)
        finally:
            cast(Any, inference).load_custom_model_from_hf = original_resolver
        generated = tuple(generated_directory.glob("*.wav"))
        if len(generated) != 1:
            raise ContractError("Seed-VC did not produce exactly one native WAV")
        os.replace(generated[0], output_path)
    return request.seed


def main() -> int:  # pragma: no cover - isolated process entrypoint
    return worker_main(DEFINITION, load=load_model, synthesize=synthesize)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

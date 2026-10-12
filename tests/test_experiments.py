from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.errors import ContractError
from cole_voice_lab.experiments import plan_bakeoff, timing_semantics
from cole_voice_lab.models import Prompt, ReferenceEvidence
from cole_voice_lab.registry import load_registry

ROOT = Path(__file__).resolve().parents[1]


def registry():
    return load_registry(ROOT / "config/candidates.toml", ROOT / "config/model-sources.lock.json")


def held_out() -> Prompt:
    return Prompt(
        prompt_id="heldout-neutral-1",
        text="This sentence is held out for a fair comparison.",
        category="neutral",
        style="Neutral and conversational.",
        holdout=True,
        expected_seconds=4.0,
    )


def reference(tmp_path: Path) -> ReferenceEvidence:
    return ReferenceEvidence(
        path=str((tmp_path / "reference.wav").resolve()),
        sha256="a" * 64,
        transcript="A clean and exact reference transcript.",
        subject_id="cole-self",
    )


def performed_source(tmp_path: Path, name: str = "source.wav") -> Path:
    path = (tmp_path / name).resolve()
    path.write_bytes(b"checksum-bound performed source fixture")
    return path


def test_plan_uses_identical_direct_matrix_and_separate_seed_track(tmp_path: Path) -> None:
    prompt = held_out()
    source = performed_source(tmp_path, "performed-source.wav")
    plan = plan_bakeoff(
        registry(),
        experiment_id="bakeoff-001",
        prompts=(prompt,),
        references=(reference(tmp_path),),
        seeds=(7, 11),
        output_root=tmp_path / "experiment",
        source_audio_by_prompt={prompt.prompt_id: source},
        profile="smoke",
    )
    assert len(plan.direct_tts) == 4 * 1 * 1 * 2 * 2
    assert len(plan.voice_conversion) == 1 * 1 * 1 * 2
    plan.assert_fair()
    direct_candidates = {trial.request.candidate_id for trial in plan.direct_tts}
    assert direct_candidates == {
        "qwen3-tts-base",
        "chatterbox-turbo",
        "chatterbox-v3",
        "f5-tts",
    }
    assert {trial.timing_mode for trial in plan.direct_tts} == {"cold", "warm"}
    assert {trial.timing_mode for trial in plan.voice_conversion} == {"cold"}
    assert all(trial.request.source_audio_path is None for trial in plan.direct_tts)
    assert all(trial.request.source_audio_path == str(source) for trial in plan.voice_conversion)
    assert all(
        trial.request.source_audio_sha256 == sha256_file(source)[0]
        and trial.request.source_audio_size_bytes == source.stat().st_size
        for trial in plan.voice_conversion
    )
    assert all(
        trial.native_output_path != trial.normalized_output_path for trial in plan.direct_tts
    )
    assert not (tmp_path / "experiment").exists()


def test_smoke_profile_selects_quantized_mlx_and_low_step_diffusion(tmp_path: Path) -> None:
    prompt = held_out()
    plan = plan_bakeoff(
        registry(),
        experiment_id="bakeoff-smoke",
        prompts=(prompt,),
        references=(reference(tmp_path),),
        seeds=(1,),
        output_root=tmp_path / "experiment",
        source_audio_by_prompt={prompt.prompt_id: performed_source(tmp_path)},
        profile="smoke",
        direct_timing_modes=("cold",),
    )
    by_candidate = {trial.request.candidate_id: trial for trial in plan.direct_tts}
    assert by_candidate["qwen3-tts-base"].request.model_id.endswith("-8bit")
    assert by_candidate["chatterbox-turbo"].request.model_id.endswith("-8bit")
    assert by_candidate["f5-tts"].request.settings["nfe_step"] == 8
    assert plan.voice_conversion[0].request.settings["diffusion_steps"] == 4


def test_planner_requires_heldout_inputs_and_performed_seed_sources(tmp_path: Path) -> None:
    prompt = held_out()
    with pytest.raises(ContractError, match="performed sources"):
        plan_bakeoff(
            registry(),
            experiment_id="bakeoff-1",
            prompts=(prompt,),
            references=(reference(tmp_path),),
            seeds=(1,),
            output_root=tmp_path,
        )
    not_held_out = prompt.model_copy(update={"holdout": False})
    with pytest.raises(ContractError, match="held-out"):
        plan_bakeoff(
            registry(),
            experiment_id="bakeoff-1",
            prompts=(not_held_out,),
            references=(reference(tmp_path),),
            seeds=(1,),
            output_root=tmp_path,
        )
    with pytest.raises(ContractError, match="seeds"):
        plan_bakeoff(
            registry(),
            experiment_id="bakeoff-1",
            prompts=(prompt,),
            references=(reference(tmp_path),),
            seeds=(1, 1),
            output_root=tmp_path,
        )
    with pytest.raises(ContractError, match="cannot checksum performed source"):
        plan_bakeoff(
            registry(),
            experiment_id="bakeoff-missing-source",
            prompts=(prompt,),
            references=(reference(tmp_path),),
            seeds=(1,),
            output_root=tmp_path,
            source_audio_by_prompt={prompt.prompt_id: tmp_path / "missing.wav"},
        )


def test_timing_semantics_are_explicit() -> None:
    assert "model load" in timing_semantics("cold")
    assert "warm-up" in timing_semantics("warm")


def test_planner_rejects_duplicate_cells_and_invalid_timing_modes(tmp_path: Path) -> None:
    prompt = held_out()
    ref = reference(tmp_path)
    shared = {
        "registry": registry(),
        "experiment_id": "bakeoff-invalid",
        "references": (ref,),
        "seeds": (1,),
        "output_root": tmp_path,
        "source_audio_by_prompt": {prompt.prompt_id: performed_source(tmp_path)},
    }
    with pytest.raises(ContractError, match="prompt IDs"):
        plan_bakeoff(prompts=(prompt, prompt), **shared)
    with pytest.raises(ContractError, match="reference digests"):
        plan_bakeoff(
            registry(),
            experiment_id="bakeoff-invalid",
            prompts=(prompt,),
            references=(ref, ref),
            seeds=(1,),
            output_root=tmp_path,
            source_audio_by_prompt={prompt.prompt_id: performed_source(tmp_path)},
        )
    with pytest.raises(ContractError, match="timing modes"):
        plan_bakeoff(prompts=(prompt,), direct_timing_modes=(), **shared)
    with pytest.raises(ContractError, match="unsupported timing"):
        plan_bakeoff(
            prompts=(prompt,),
            direct_timing_modes=("cold", "invalid"),  # type: ignore[arg-type]
            **shared,
        )


def test_plan_fairness_guard_detects_missing_direct_cell(tmp_path: Path) -> None:
    prompt = held_out()
    plan = plan_bakeoff(
        registry(),
        experiment_id="bakeoff-fairness",
        prompts=(prompt,),
        references=(reference(tmp_path),),
        seeds=(1,),
        output_root=tmp_path,
        source_audio_by_prompt={prompt.prompt_id: performed_source(tmp_path)},
    )
    unfair = replace(plan, direct_tts=plan.direct_tts[1:])
    with pytest.raises(ContractError, match="matrix differs"):
        unfair.assert_fair()
    warm_conversion = replace(
        plan,
        voice_conversion=(replace(plan.voice_conversion[0], timing_mode="warm"),),
    )
    with pytest.raises(ContractError, match="cold-only"):
        warm_conversion.assert_fair()

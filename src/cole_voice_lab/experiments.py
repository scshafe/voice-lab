"""Fair, deterministic bake-off planning across isolated candidate workers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .canonical import canonical_json_bytes, sha256_file
from .errors import ContractError
from .models import ExperimentRequest, Prompt, ReferenceEvidence
from .registry import CandidateRegistry, ProfileName

TimingMode = Literal["cold", "warm"]


@dataclass(frozen=True, slots=True)
class ExperimentTrial:
    request: ExperimentRequest
    profile: ProfileName
    timing_mode: TimingMode
    prompt_id: str
    reference_sha256: str
    native_output_path: Path
    normalized_output_path: Path

    @property
    def fairness_key(self) -> tuple[str, str, int, TimingMode]:
        return (
            self.prompt_id,
            self.reference_sha256,
            self.request.seed,
            self.timing_mode,
        )


@dataclass(frozen=True, slots=True)
class BakeoffPlan:
    experiment_id: str
    profile: ProfileName
    direct_tts: tuple[ExperimentTrial, ...]
    voice_conversion: tuple[ExperimentTrial, ...]

    def assert_fair(self) -> None:
        by_candidate: dict[str, set[tuple[str, str, int, TimingMode]]] = {}
        for trial in self.direct_tts:
            by_candidate.setdefault(trial.request.candidate_id, set()).add(trial.fairness_key)
        if by_candidate:
            expected = next(iter(by_candidate.values()))
            for candidate_id, keys in by_candidate.items():
                if keys != expected:
                    raise ContractError(
                        f"direct-TTS held-out matrix differs for candidate {candidate_id}"
                    )
        if any(trial.timing_mode != "cold" for trial in self.voice_conversion):
            raise ContractError("voice-conversion trials must remain in their cold-only track")


def timing_semantics(mode: TimingMode) -> str:
    if mode == "cold":
        return "fresh process; measured interval includes immutable model load and synthesis"
    return "fresh process; one unmeasured same-request warm-up, then measured synthesis"


def _trial_id(
    *,
    experiment_id: str,
    candidate_id: str,
    prompt_id: str,
    reference_sha256: str,
    seed: int,
    timing_mode: TimingMode,
) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "experiment_id": experiment_id,
                "candidate_id": candidate_id,
                "prompt_id": prompt_id,
                "reference_sha256": reference_sha256,
                "seed": seed,
                "timing_mode": timing_mode,
            }
        )
    ).hexdigest()[:20]
    return f"{candidate_id}-{digest}"


def _validate_matrix_inputs(
    prompts: Sequence[Prompt], references: Sequence[ReferenceEvidence], seeds: Sequence[int]
) -> None:
    if not prompts or any(not prompt.holdout for prompt in prompts):
        raise ContractError("bake-offs require a non-empty held-out prompt set")
    prompt_ids = [prompt.prompt_id for prompt in prompts]
    if len(prompt_ids) != len(set(prompt_ids)):
        raise ContractError("held-out prompt IDs must be unique")
    if not references:
        raise ContractError("bake-offs require at least one checksum-bound reference")
    reference_hashes = [reference.sha256 for reference in references]
    if len(reference_hashes) != len(set(reference_hashes)):
        raise ContractError("reference digests must be unique")
    if not seeds or len(seeds) != len(set(seeds)):
        raise ContractError("bake-off seeds must be non-empty and unique")
    if any(seed < 0 or seed > (2**63) - 1 for seed in seeds):
        raise ContractError("bake-off seeds must fit the worker contract")


def _make_trial(
    *,
    registry: CandidateRegistry,
    candidate_id: str,
    experiment_id: str,
    prompt: Prompt,
    reference: ReferenceEvidence,
    seed: int,
    timing_mode: TimingMode,
    output_root: Path,
    profile: ProfileName,
    source_audio_path: Path | None,
    source_audio_sha256: str | None,
    source_audio_size_bytes: int | None,
) -> ExperimentTrial:
    resolved = registry.resolve(candidate_id, profile)
    trial_id = _trial_id(
        experiment_id=experiment_id,
        candidate_id=candidate_id,
        prompt_id=prompt.prompt_id,
        reference_sha256=reference.sha256,
        seed=seed,
        timing_mode=timing_mode,
    )
    track = resolved.candidate.track
    native = output_root / "native" / track / candidate_id / f"{trial_id}.wav"
    normalized = output_root / "normalized" / track / candidate_id / f"{trial_id}.wav"
    settings = dict(resolved.settings)
    settings.update(
        {
            "profile": profile,
            "timing_mode": timing_mode,
            "timing_semantics": timing_semantics(timing_mode),
            "prompt_id": prompt.prompt_id,
        }
    )
    request = ExperimentRequest(
        experiment_id=experiment_id,
        trial_id=trial_id,
        candidate_id=candidate_id,
        track=track,
        text=prompt.require_text(),
        language="en",
        output_path=str(native),
        reference=reference,
        source_audio_path=str(source_audio_path) if source_audio_path is not None else None,
        source_audio_sha256=source_audio_sha256,
        source_audio_size_bytes=source_audio_size_bytes,
        seed=seed if resolved.candidate.supports_seed else 0,
        model_id=resolved.model_id,
        model_revision=resolved.model_revision,
        code_revision=resolved.code_revision,
        settings=settings,
    )
    return ExperimentTrial(
        request=request,
        profile=profile,
        timing_mode=timing_mode,
        prompt_id=prompt.prompt_id,
        reference_sha256=reference.sha256,
        native_output_path=native,
        normalized_output_path=normalized,
    )


def plan_bakeoff(
    registry: CandidateRegistry,
    *,
    experiment_id: str,
    prompts: Sequence[Prompt],
    references: Sequence[ReferenceEvidence],
    seeds: Sequence[int],
    output_root: Path,
    source_audio_by_prompt: Mapping[str, Path] | None = None,
    profile: ProfileName = "quality",
    direct_timing_modes: Sequence[TimingMode] = ("cold", "warm"),
) -> BakeoffPlan:
    """Plan identical direct-TTS cells and a separately labeled Seed-VC track.

    Planning is pure: it creates no directories, downloads, or processes.
    """

    _validate_matrix_inputs(prompts, references, seeds)
    if not direct_timing_modes or len(direct_timing_modes) != len(set(direct_timing_modes)):
        raise ContractError("direct timing modes must be non-empty and unique")
    if any(mode not in {"cold", "warm"} for mode in direct_timing_modes):
        raise ContractError("unsupported timing mode")
    output_root = output_root.expanduser().resolve(strict=False)

    direct_trials = tuple(
        _make_trial(
            registry=registry,
            candidate_id=candidate.candidate_id,
            experiment_id=experiment_id,
            prompt=prompt,
            reference=reference,
            seed=seed,
            timing_mode=mode,
            output_root=output_root,
            profile=profile,
            source_audio_path=None,
            source_audio_sha256=None,
            source_audio_size_bytes=None,
        )
        for candidate in registry.candidates(track="direct-tts")
        if candidate.enabled
        for prompt in prompts
        for reference in references
        for seed in seeds
        for mode in direct_timing_modes
    )

    conversion_candidates = tuple(
        candidate
        for candidate in registry.candidates(track="voice-conversion")
        if candidate.enabled
    )
    sources = source_audio_by_prompt or {}
    if conversion_candidates:
        missing = sorted(prompt.prompt_id for prompt in prompts if prompt.prompt_id not in sources)
        if missing:
            raise ContractError(
                f"voice-conversion track requires performed sources for held-out prompts: {missing}"
            )
    source_evidence: dict[str, tuple[Path, str, int]] = {}
    for prompt_id, source in sources.items():
        try:
            source_path = source.expanduser().resolve(strict=True)
            digest, size_bytes = sha256_file(source_path)
        except (OSError, ValueError) as error:
            raise ContractError(
                f"cannot checksum performed source for {prompt_id}: {source}"
            ) from error
        if size_bytes < 1:
            raise ContractError(f"performed source must not be empty: {source_path}")
        source_evidence[prompt_id] = (source_path, digest, size_bytes)
    conversion_trials = tuple(
        _make_trial(
            registry=registry,
            candidate_id=candidate.candidate_id,
            experiment_id=experiment_id,
            prompt=prompt,
            reference=reference,
            seed=seed,
            timing_mode="cold",
            output_root=output_root,
            profile=profile,
            source_audio_path=source_evidence[prompt.prompt_id][0],
            source_audio_sha256=source_evidence[prompt.prompt_id][1],
            source_audio_size_bytes=source_evidence[prompt.prompt_id][2],
        )
        for candidate in conversion_candidates
        for prompt in prompts
        for reference in references
        for seed in seeds
    )
    plan = BakeoffPlan(experiment_id, profile, direct_trials, conversion_trials)
    plan.assert_fair()
    return plan

"""Durable offline audition planning, preparation, execution, and resume."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, ValidationError, field_validator, model_validator

from .audio import inspect_pcm_wav
from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .errors import ContractError
from .experiments import BakeoffPlan, TimingMode, plan_bakeoff
from .models import (
    AudioSpec,
    ContractModel,
    ExecutedCodeEvidence,
    ExperimentRequest,
    ExperimentResult,
    Prompt,
    PromptSuite,
    ReferenceEvidence,
    WorkerModelEvidence,
    utc_now,
)
from .normalization import (
    NormalizationEvidence,
    build_normalization_plan,
    execute_normalization,
)
from .paths import (
    AssetLayout,
    confined_path,
    reject_symlink_components,
    validate_relative_path,
)
from .registry import CandidateRegistry, ProfileName, ResolvedCandidate, SourceLock
from .runtime import (
    SOURCE_MANIFEST_NAME,
    LocalSourceManifest,
    SourceVerification,
    WorkerInvocation,
    build_runtime_install_plan,
    build_source_provision_plan,
    exclusive_worker_slot,
    load_persisted_runtime_receipt,
    run_worker,
    source_paths_by_repository,
    verify_local_source,
    verify_runtime_install_receipt,
)

AUDITION_PLAN_SCHEMA: Literal["cole-voice-lab.audition-plan/v1"] = "cole-voice-lab.audition-plan/v1"
AUDITION_RESULT_SCHEMA: Literal["cole-voice-lab.audition-trial-result/v1"] = (
    "cole-voice-lab.audition-trial-result/v1"
)
AUDITION_RUN_SCHEMA: Literal["cole-voice-lab.audition-run/v1"] = "cole-voice-lab.audition-run/v1"
_MAX_PERSISTED_EVIDENCE_BYTES = 64 * 1024 * 1024


def _absolute_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("audition execution paths must be normalized absolute paths")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("audition timestamps require an explicit timezone")
    return value.astimezone(UTC)


class CommandPlan(ContractModel):
    argv: tuple[str, ...] = Field(min_length=1)


class CandidatePreparationPlan(ContractModel):
    candidate_id: str
    profile: ProfileName
    runtime_id: str
    runtime_path: str
    runtime_commands: tuple[CommandPlan, ...]
    model_source_id: str
    model_path: str
    source_commands: tuple[CommandPlan, ...]
    auxiliary_paths: dict[str, str]
    code_source_id: str
    code_checkout_path: str | None
    code_checkout_commands: tuple[CommandPlan, ...]

    @field_validator("runtime_path", "model_path")
    @classmethod
    def _absolute_primary_paths(cls, value: str) -> str:
        return _absolute_path(value)

    @field_validator("code_checkout_path")
    @classmethod
    def _absolute_optional_code_path(cls, value: str | None) -> str | None:
        return _absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def _code_checkout_shape(self) -> CandidatePreparationPlan:
        if (self.code_checkout_path is None) != (not self.code_checkout_commands):
            raise ValueError("code checkout path and commands must be present together")
        return self

    @field_validator("auxiliary_paths")
    @classmethod
    def _absolute_auxiliary_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value.values():
            _absolute_path(path)
        return value


class AuditionTrial(ContractModel):
    trial_id: str
    candidate_id: str
    track: Literal["direct-tts", "voice-conversion"]
    profile: ProfileName
    timing_mode: TimingMode
    runtime_id: str
    runtime_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: ExperimentRequest
    normalized_output_path: str

    @field_validator("normalized_output_path")
    @classmethod
    def _absolute_normalized_path(cls, value: str) -> str:
        return _absolute_path(value)

    @model_validator(mode="after")
    def _request_is_bound(self) -> AuditionTrial:
        if self.request_sha256 != canonical_sha256(self.request):
            raise ValueError("audition trial request digest does not match request")
        if (self.trial_id, self.candidate_id, self.track) != (
            self.request.trial_id,
            self.request.candidate_id,
            self.request.track,
        ):
            raise ValueError("audition trial identity does not match request")
        if self.request.settings.get("profile") != self.profile:
            raise ValueError("audition trial profile does not match request settings")
        if self.request.settings.get("timing_mode") != self.timing_mode:
            raise ValueError("audition trial timing does not match request settings")
        return self


class AuditionPlan(ContractModel):
    schema_version: Literal["cole-voice-lab.audition-plan/v1"] = AUDITION_PLAN_SCHEMA
    experiment_id: str
    profile: ProfileName
    created_at: datetime
    prompt_suite: PromptSuite
    prompt_suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_prompt_ids: tuple[str, ...] = Field(min_length=1)
    held_out_assertion: Literal["suite-purpose-and-prompt-flags-validated"] = (
        "suite-purpose-and-prompt-flags-validated"
    )
    trials: tuple[AuditionTrial, ...] = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def _created_at_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _unique_consistent_trials(self) -> AuditionPlan:
        if self.prompt_suite.purpose != "held-out-evaluation":
            raise ValueError("audition plans require a held-out evaluation prompt suite")
        if self.prompt_suite_sha256 != canonical_sha256(self.prompt_suite):
            raise ValueError("audition prompt-suite digest does not match embedded evidence")
        if len(self.selected_prompt_ids) != len(set(self.selected_prompt_ids)):
            raise ValueError("selected held-out prompt IDs must be unique")
        suite_prompts = {prompt.prompt_id: prompt for prompt in self.prompt_suite.prompts}
        if not set(self.selected_prompt_ids).issubset(suite_prompts):
            raise ValueError("selected prompts are not present in the bound prompt suite")
        trial_ids = [trial.trial_id for trial in self.trials]
        output_paths = [trial.request.output_path for trial in self.trials]
        normalized_paths = [trial.normalized_output_path for trial in self.trials]
        if len(trial_ids) != len(set(trial_ids)):
            raise ValueError("audition trial IDs must be unique")
        if len(output_paths) != len(set(output_paths)):
            raise ValueError("audition native output paths must be unique")
        if len(normalized_paths) != len(set(normalized_paths)):
            raise ValueError("audition normalized output paths must be unique")
        if any(trial.request.experiment_id != self.experiment_id for trial in self.trials):
            raise ValueError("audition trial experiment IDs must match the plan")
        if any(trial.profile != self.profile for trial in self.trials):
            raise ValueError("audition trial profiles must match the plan")
        cells_by_track_candidate: dict[
            tuple[str, str], list[tuple[str, str, str, int, str, str | None]]
        ] = {}
        for trial in self.trials:
            prompt_id = trial.request.settings.get("prompt_id")
            if not isinstance(prompt_id, str) or prompt_id not in self.selected_prompt_ids:
                raise ValueError("audition trial does not name a selected held-out prompt")
            if trial.request.text != suite_prompts[prompt_id].text:
                raise ValueError("audition trial text differs from the bound held-out prompt")
            if trial.track == "voice-conversion" and trial.timing_mode != "cold":
                raise ValueError("voice-conversion audition trials must remain cold-only")
            cells_by_track_candidate.setdefault((trial.track, trial.candidate_id), []).append(
                (
                    prompt_id,
                    canonical_sha256(trial.request.reference),
                    trial.request.language,
                    trial.request.seed,
                    trial.timing_mode,
                    trial.request.source_audio_sha256,
                )
            )
        for track in ("direct-tts", "voice-conversion"):
            matrices = {
                candidate_id: cells
                for (candidate_track, candidate_id), cells in cells_by_track_candidate.items()
                if candidate_track == track
            }
            expected: set[tuple[str, str, str, int, str, str | None]] | None = None
            for candidate_id, cells in matrices.items():
                matrix = set(cells)
                if len(matrix) != len(cells):
                    raise ValueError(f"{track} candidate {candidate_id} has duplicate matrix cells")
                if {cell[0] for cell in matrix} != set(self.selected_prompt_ids):
                    raise ValueError(f"{track} candidate {candidate_id} omits held-out prompts")
                if expected is None:
                    expected = matrix
                elif matrix != expected:
                    raise ValueError(
                        f"{track} held-out matrix differs for candidate {candidate_id}"
                    )
        return self


class NormalizedOutputEvidence(ContractModel):
    normalization_id: Literal["cole-voice-lab-common-v1"] = "cole-voice-lab-common-v1"
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audio: AudioSpec

    @field_validator("path")
    @classmethod
    def _absolute_output_path(cls, value: str) -> str:
        return _absolute_path(value)


class PersistedSourceEvidence(ContractModel):
    """Portable integrity preimage for one source after its resident bytes are evicted."""

    source_id: str = Field(min_length=1, max_length=128)
    kind: Literal["git", "huggingface"]
    repository: str = Field(min_length=1, max_length=1_024)
    revision: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    manifest: LocalSourceManifest | None = None
    files: dict[str, str] = Field(min_length=1)
    integrity_record: dict[str, object]
    integrity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("files")
    @classmethod
    def _portable_file_digests(cls, value: dict[str, str]) -> dict[str, str]:
        for path, digest in value.items():
            validate_relative_path(path)
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("persisted source files require lowercase SHA-256 digests")
        return value

    @model_validator(mode="after")
    def _bound_integrity_preimage(self) -> PersistedSourceEvidence:
        if canonical_sha256(self.integrity_record) != self.integrity_sha256:
            raise ValueError("persisted source integrity preimage does not match its digest")
        if self.kind == "huggingface":
            if self.manifest is None or self.manifest_sha256 is None:
                raise ValueError("Hugging Face evidence requires its local manifest preimage")
            manifest_digest = hashlib.sha256(
                canonical_json_bytes(self.manifest) + b"\n"
            ).hexdigest()
            if manifest_digest != self.manifest_sha256:
                raise ValueError("persisted local source manifest preimage has the wrong digest")
            if (
                self.manifest.source_id,
                self.manifest.kind,
                self.manifest.repository,
                self.manifest.revision,
                self.manifest.files,
            ) != (
                self.source_id,
                self.kind,
                self.repository,
                self.revision,
                self.files,
            ):
                raise ValueError("persisted local source manifest changes source identity or files")
            expected_record: dict[str, object] = {
                "schema_version": "cole-voice-lab.snapshot-integrity/v1",
                "source_id": self.source_id,
                "repository": self.repository,
                "revision": self.revision,
                "manifest_sha256": self.manifest_sha256,
                "files": self.files,
            }
        else:
            if self.manifest is not None or self.manifest_sha256 is not None:
                raise ValueError("Git evidence cannot claim a local snapshot manifest")
            tree_id = self.integrity_record.get("tree_id")
            if (
                not isinstance(tree_id, str)
                or not tree_id
                or any(character not in "0123456789abcdef" for character in tree_id)
            ):
                raise ValueError("Git evidence requires its immutable tree identifier")
            expected_record = {
                "schema_version": "cole-voice-lab.git-checkout-integrity/v1",
                "revision": self.revision,
                "tree_id": tree_id,
                "files": self.files,
            }
        if canonical_json_bytes(self.integrity_record) != canonical_json_bytes(expected_record):
            raise ValueError("persisted source integrity record has unexpected fields or values")
        return self


class PersistedTrialSourceEvidence(ContractModel):
    schema_version: Literal["cole-voice-lab.audition-source-evidence/v1"] = (
        "cole-voice-lab.audition-source-evidence/v1"
    )
    trial_id: str
    candidate_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    primary: PersistedSourceEvidence
    auxiliary: dict[str, PersistedSourceEvidence]
    code: PersistedSourceEvidence | None = None

    @model_validator(mode="after")
    def _unique_sources(self) -> PersistedTrialSourceEvidence:
        identifiers = [self.primary.source_id]
        identifiers.extend(source.source_id for source in self.auxiliary.values())
        if self.code is not None:
            identifiers.append(self.code.source_id)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("persisted trial source evidence repeats a source")
        if any(repository != source.repository for repository, source in self.auxiliary.items()):
            raise ValueError("persisted auxiliary source keys must be repository identities")
        return self


class PersistedTrialResult(ContractModel):
    schema_version: Literal["cole-voice-lab.audition-trial-result/v1"] = AUDITION_RESULT_SCHEMA
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    outcome: Literal["success", "worker-failed", "normalization-failed"]
    worker_result: ExperimentResult
    runtime_receipt_path: str | None = None
    runtime_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_evidence_path: str | None = None
    source_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    normalized: NormalizedOutputEvidence | None = None
    error_code: str | None = None
    error_message: str | None = None

    @field_validator("completed_at")
    @classmethod
    def _completed_at_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("runtime_receipt_path", "source_evidence_path")
    @classmethod
    def _portable_runtime_receipt(cls, value: str | None) -> str | None:
        return validate_relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def _outcome_shape(self) -> PersistedTrialResult:
        if (self.runtime_receipt_path is None) != (self.runtime_receipt_sha256 is None):
            raise ValueError("runtime receipt path and digest must appear together")
        if (self.source_evidence_path is None) != (self.source_evidence_sha256 is None):
            raise ValueError("source evidence path and digest must appear together")
        if self.outcome == "success":
            if not self.worker_result.ok or self.normalized is None:
                raise ValueError(
                    "successful audition result requires worker and normalization evidence"
                )
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful audition result cannot carry an error")
            if self.runtime_receipt_path is None:
                raise ValueError("successful audition result requires sealed runtime evidence")
            if self.source_evidence_path is None:
                raise ValueError("successful audition result requires portable source evidence")
        elif self.outcome == "worker-failed":
            if self.worker_result.ok or self.normalized is not None:
                raise ValueError("worker failure must contain only a failed worker result")
        elif not self.worker_result.ok or self.normalized is not None:
            raise ValueError("normalization failure requires successful native worker output only")
        if self.outcome != "success" and (self.error_code is None or self.error_message is None):
            raise ValueError("failed audition result requires a structured error")
        return self


class RunResultPointer(ContractModel):
    trial_id: str
    candidate_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["success", "worker-failed", "normalization-failed"]
    result_path: str
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("result_path")
    @classmethod
    def _portable_result_path(cls, value: str) -> str:
        return validate_relative_path(value)


class AuditionRunManifest(ContractModel):
    schema_version: Literal["cole-voice-lab.audition-run/v1"] = AUDITION_RUN_SCHEMA
    experiment_id: str
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime
    status: Literal["in-progress", "complete", "complete-with-failures"]
    direct_candidate_ids: tuple[str, ...]
    voice_conversion_candidate_ids: tuple[str, ...]
    results: tuple[RunResultPointer, ...]

    @field_validator("updated_at")
    @classmethod
    def _updated_at_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _unique_manifest_entries(self) -> AuditionRunManifest:
        if len(self.direct_candidate_ids) != len(set(self.direct_candidate_ids)):
            raise ValueError("direct candidate selection must be unique")
        if len(self.voice_conversion_candidate_ids) != len(
            set(self.voice_conversion_candidate_ids)
        ):
            raise ValueError("voice-conversion candidate selection must be unique")
        ids = [result.trial_id for result in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("run manifest trial results must be unique")
        return self


@dataclass(frozen=True, slots=True)
class AuditionPaths:
    experiment_directory: Path
    plan_path: Path
    run_manifest_path: Path
    results_directory: Path


def audition_paths(layout: AssetLayout, experiment_id: str) -> AuditionPaths:
    try:
        validated_id = validate_relative_path(experiment_id)
    except ValueError as error:
        raise ContractError("experiment ID cannot escape the experiments directory") from error
    if "/" in validated_id:
        raise ContractError("experiment ID must be one path component")
    directory = layout.directory("experiments") / experiment_id
    return AuditionPaths(
        experiment_directory=directory,
        plan_path=directory / "plan.json",
        run_manifest_path=directory / "run-manifest.json",
        results_directory=directory / "results",
    )


def runtime_path(layout: AssetLayout, runtime_id: str) -> Path:
    return layout.directory("runtimes") / runtime_id


def source_path(layout: AssetLayout, source_id: str) -> Path:
    return layout.directory("models") / "sources" / source_id


def checkout_path(layout: AssetLayout, source_id: str) -> Path:
    return layout.directory("runtimes") / "source-checkouts" / source_id


def _require_confined_path(
    layout: AssetLayout,
    value: str,
    boundary: Path,
    *,
    label: str,
) -> None:
    path = Path(value)
    if not path.is_absolute() or str(path) != value:
        raise ContractError(f"{label} must be a normalized absolute path")
    try:
        path.relative_to(boundary)
    except ValueError as error:
        raise ContractError(f"{label} must remain under {boundary}") from error
    reject_symlink_components(layout.root, path)


def validate_audition_plan_paths(layout: AssetLayout, plan: AuditionPlan) -> None:
    """Confine every worker input/output before a plan may be persisted or run."""

    paths = audition_paths(layout, plan.experiment_id)
    reference_root = layout.directory("references")
    # A plan written before D-050 may name a consent record; it is never read, only confined.
    consent_root = layout.directory("provenance") / "consents"
    for trial in plan.trials:
        expected_native = (
            paths.experiment_directory
            / "native"
            / trial.track
            / trial.candidate_id
            / f"{trial.trial_id}.wav"
        )
        expected_normalized = (
            paths.experiment_directory
            / "normalized"
            / trial.track
            / trial.candidate_id
            / f"{trial.trial_id}.wav"
        )
        native = Path(trial.request.output_path)
        normalized = Path(trial.normalized_output_path)
        if native != expected_native:
            raise ContractError("native output path does not match the experiment convention")
        if normalized != expected_normalized:
            raise ContractError("normalized output path does not match the experiment convention")
        _require_confined_path(
            layout,
            trial.request.output_path,
            paths.experiment_directory,
            label="native output",
        )
        _require_confined_path(
            layout,
            trial.normalized_output_path,
            paths.experiment_directory,
            label="normalized output",
        )
        _require_confined_path(
            layout,
            trial.request.reference.path,
            reference_root,
            label="reference audio",
        )
        if trial.request.reference.consent_path is not None:
            _require_confined_path(
                layout,
                trial.request.reference.consent_path,
                consent_root,
                label="consent evidence",
            )
        if trial.request.source_audio_path is not None:
            _require_confined_path(
                layout,
                trial.request.source_audio_path,
                layout.root,
                label="performed source audio",
            )


def build_candidate_preparation_plan(
    registry: CandidateRegistry,
    layout: AssetLayout,
    *,
    candidate_id: str,
    profile: ProfileName,
    repository_root: Path,
    base_python: Path,
) -> CandidatePreparationPlan:
    """Build install/download argv only; this function never executes them."""

    resolved = registry.resolve(candidate_id, profile)
    environment = runtime_path(layout, resolved.runtime.runtime_id)
    runtime_plan = build_runtime_install_plan(
        resolved.runtime,
        environment,
        base_python=base_python,
        repository_root=repository_root,
    )
    runtime_python = environment / "bin/python"
    material_sources = (resolved.model_source, *resolved.auxiliary_sources)
    source_plans = tuple(
        build_source_provision_plan(
            source,
            source_path(layout, source.source_id),
            runtime_python=runtime_python,
        )
        for source in material_sources
    )
    code_plan = (
        build_source_provision_plan(
            resolved.code_source,
            checkout_path(layout, resolved.code_source.source_id),
            runtime_python=runtime_python,
        )
        if resolved.candidate.engine == "seed-vc"
        else None
    )
    return CandidatePreparationPlan(
        candidate_id=candidate_id,
        profile=profile,
        runtime_id=resolved.runtime.runtime_id,
        runtime_path=str(environment),
        runtime_commands=tuple(CommandPlan(argv=command) for command in runtime_plan.commands),
        model_source_id=resolved.model_source.source_id,
        model_path=str(source_path(layout, resolved.model_source.source_id)),
        source_commands=tuple(
            CommandPlan(argv=command) for plan in source_plans for command in plan.commands
        ),
        auxiliary_paths={
            source.repository: str(source_path(layout, source.source_id))
            for source in resolved.auxiliary_sources
        },
        code_source_id=resolved.code_source.source_id,
        code_checkout_path=(
            str(checkout_path(layout, resolved.code_source.source_id))
            if code_plan is not None
            else None
        ),
        code_checkout_commands=(
            tuple(CommandPlan(argv=command) for command in code_plan.commands)
            if code_plan is not None
            else ()
        ),
    )


def build_preparation_plans(
    registry: CandidateRegistry,
    layout: AssetLayout,
    *,
    candidate_ids: Sequence[str],
    profile: ProfileName,
    repository_root: Path,
    base_python: Path,
) -> tuple[CandidatePreparationPlan, ...]:
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ContractError("candidate preparation selection must be unique")
    return tuple(
        build_candidate_preparation_plan(
            registry,
            layout,
            candidate_id=candidate_id,
            profile=profile,
            repository_root=repository_root,
            base_python=base_python,
        )
        for candidate_id in candidate_ids
    )


def _from_bakeoff(
    bakeoff: BakeoffPlan,
    *,
    registry: CandidateRegistry,
    prompt_suite: PromptSuite,
    selected_prompt_ids: tuple[str, ...],
    created_at: datetime,
) -> AuditionPlan:
    trials = tuple(
        AuditionTrial(
            trial_id=trial.request.trial_id,
            candidate_id=trial.request.candidate_id,
            track=trial.request.track,
            profile=trial.profile,
            timing_mode=trial.timing_mode,
            runtime_id=(
                resolved := registry.resolve(trial.request.candidate_id, trial.profile)
            ).runtime.runtime_id,
            runtime_lock_sha256=canonical_sha256(resolved.runtime),
            request_sha256=canonical_sha256(trial.request),
            request=trial.request,
            normalized_output_path=str(trial.normalized_output_path),
        )
        for trial in (*bakeoff.direct_tts, *bakeoff.voice_conversion)
    )
    return AuditionPlan(
        experiment_id=bakeoff.experiment_id,
        profile=bakeoff.profile,
        created_at=created_at,
        prompt_suite=prompt_suite,
        prompt_suite_sha256=canonical_sha256(prompt_suite),
        selected_prompt_ids=selected_prompt_ids,
        trials=trials,
    )


def build_audition_plan(
    registry: CandidateRegistry,
    layout: AssetLayout,
    *,
    experiment_id: str,
    prompt_suite: PromptSuite,
    prompts: Sequence[Prompt],
    references: Sequence[ReferenceEvidence],
    seeds: Sequence[int],
    source_audio_by_prompt: Mapping[str, Path] | None = None,
    profile: ProfileName = "quality",
    direct_timing_modes: Sequence[TimingMode] = ("cold", "warm"),
    created_at: datetime | None = None,
) -> AuditionPlan:
    selected_prompt_ids = tuple(prompt.prompt_id for prompt in prompts)
    suite_prompts = {prompt.prompt_id: prompt for prompt in prompt_suite.prompts}
    if prompt_suite.purpose != "held-out-evaluation":
        raise ContractError("audition planning requires a held-out evaluation prompt suite")
    if len(selected_prompt_ids) != len(set(selected_prompt_ids)):
        raise ContractError("selected held-out prompt IDs must be unique")
    if any(suite_prompts.get(prompt.prompt_id) != prompt for prompt in prompts):
        raise ContractError("selected prompts must be exact members of the bound prompt suite")
    paths = audition_paths(layout, experiment_id)
    bakeoff = plan_bakeoff(
        registry,
        experiment_id=experiment_id,
        prompts=prompts,
        references=references,
        seeds=seeds,
        output_root=paths.experiment_directory,
        source_audio_by_prompt=source_audio_by_prompt,
        profile=profile,
        direct_timing_modes=direct_timing_modes,
    )
    return _from_bakeoff(
        bakeoff,
        registry=registry,
        prompt_suite=prompt_suite,
        selected_prompt_ids=selected_prompt_ids,
        created_at=created_at or utc_now(),
    )


def persist_audition_plan(layout: AssetLayout, plan: AuditionPlan) -> tuple[Path, str]:
    validate_audition_plan_paths(layout, plan)
    paths = audition_paths(layout, plan.experiment_id)
    reject_symlink_components(layout.root, paths.plan_path)
    expected = canonical_json_bytes(plan) + b"\n"
    if paths.plan_path.exists():
        with open_regular_no_follow(paths.plan_path) as stream:
            actual = stream.read()
        if actual != expected:
            raise ContractError("persisted audition plan differs; use a new experiment ID")
        digest, _ = sha256_file(paths.plan_path)
        return paths.plan_path, digest
    digest = write_canonical_json(paths.plan_path, plan)
    return paths.plan_path, digest


def _normalized_record(
    trial: AuditionTrial,
    worker_result: ExperimentResult,
    evidence: NormalizationEvidence,
) -> NormalizedOutputEvidence:
    if evidence.native_path != Path(trial.request.output_path):
        raise ContractError("normalization native path does not match the worker request")
    if evidence.normalized_path != Path(trial.normalized_output_path):
        raise ContractError("normalization output path does not match the audition plan")
    if evidence.native_sha256 != worker_result.output_sha256:
        raise ContractError("normalization input digest does not match worker output evidence")
    return NormalizedOutputEvidence(
        path=str(evidence.normalized_path),
        sha256=evidence.normalized_sha256,
        size_bytes=evidence.normalized_bytes,
        settings_sha256=evidence.settings_sha256,
        audio=evidence.inspection.audio,
    )


def _failed_worker_result(trial: AuditionTrial, error: Exception) -> ExperimentResult:
    return ExperimentResult(
        trial_id=trial.trial_id,
        candidate_id=trial.candidate_id,
        ok=False,
        error_code="orchestration_error",
        error_message=(str(error) or error.__class__.__name__)[:4_000],
    )


def _assert_worker_binding(trial: AuditionTrial, result: ExperimentResult) -> None:
    if (result.trial_id, result.candidate_id) != (trial.trial_id, trial.candidate_id):
        raise ContractError("worker result identity does not match the audition trial")
    if result.ok:
        if result.output_path != trial.request.output_path:
            raise ContractError("worker output path does not match the audition request")
        if result.realized_seed != trial.request.seed:
            raise ContractError("worker realized seed does not match the audition request")


def _evidence_path(output: Path, relative: str, *, label: str) -> Path:
    try:
        validate_relative_path(relative)
        return confined_path(output.parent, relative, must_exist=True)
    except (OSError, ValueError) as error:
        raise ContractError(f"{label} path is missing or escapes the trial directory") from error


def _metadata_string(result: ExperimentResult, name: str) -> str:
    value = result.metadata.get(name)
    if not isinstance(value, str):
        raise ContractError(f"successful worker result lacks {name} evidence")
    return value


def _metadata_value(result: ExperimentResult, name: str) -> object:
    if name not in result.metadata:
        raise ContractError(f"successful worker result lacks {name} evidence")
    return result.metadata[name]


def _assert_json_binding(
    label: str,
    observed: object,
    expected: object,
    *,
    evidence_label: str = "model evidence",
) -> None:
    try:
        matches = canonical_json_bytes(observed) == canonical_json_bytes(expected)
    except (TypeError, ValueError) as error:
        raise ContractError(
            f"persisted {evidence_label} contains invalid JSON for {label}"
        ) from error
    if not matches:
        raise ContractError(f"persisted {evidence_label} changed binding for {label}")


def _assert_file_digest(path: Path, expected_digest: str, expected_size: int | None = None) -> None:
    digest, size = sha256_file(path)
    if digest != expected_digest or (expected_size is not None and size != expected_size):
        raise ContractError(f"persisted evidence digest or size changed: {path}")


def _load_digest_bound_evidence[EvidenceModel: ContractModel](
    path: Path,
    expected_digest: str,
    model: type[EvidenceModel],
    *,
    label: str,
) -> EvidenceModel:
    with open_regular_no_follow(path) as stream:
        payload = stream.read(_MAX_PERSISTED_EVIDENCE_BYTES + 1)
    if len(payload) > _MAX_PERSISTED_EVIDENCE_BYTES:
        raise ContractError(f"persisted {label} exceeds the evidence size limit")
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise ContractError(f"persisted evidence digest or size changed: {path}")
    try:
        return model.model_validate_json(payload)
    except ValidationError as error:
        raise ContractError(f"persisted {label} violates its strict evidence contract") from error


def _portable_source_evidence(
    source: SourceLock,
    verification: SourceVerification,
) -> PersistedSourceEvidence:
    if verification.integrity_record is None:
        raise ContractError("source verification omitted its durable integrity preimage")
    manifest: LocalSourceManifest | None = None
    if source.kind == "huggingface":
        manifest_path = verification.path / SOURCE_MANIFEST_NAME
        with open_regular_no_follow(manifest_path) as stream:
            payload = stream.read(_MAX_PERSISTED_EVIDENCE_BYTES + 1)
        if len(payload) > _MAX_PERSISTED_EVIDENCE_BYTES:
            raise ContractError("local source manifest exceeds the evidence size limit")
        if hashlib.sha256(payload).hexdigest() != verification.manifest_sha256:
            raise ContractError("local source manifest changed before evidence persistence")
        manifest = LocalSourceManifest.model_validate_json(payload)
        if payload != canonical_json_bytes(manifest) + b"\n":
            raise ContractError("local source manifest is not canonical")
    return PersistedSourceEvidence(
        source_id=source.source_id,
        kind=source.kind,
        repository=source.repository,
        revision=source.revision,
        manifest_sha256=verification.manifest_sha256,
        manifest=manifest,
        files=verification.file_sha256,
        integrity_record=dict(verification.integrity_record),
        integrity_sha256=verification.integrity_sha256,
    )


def _load_trial_source_evidence(
    layout: AssetLayout,
    trial: AuditionTrial,
    record: PersistedTrialResult,
) -> PersistedTrialSourceEvidence | None:
    if record.source_evidence_path is None:
        return None
    path = confined_path(layout.root, record.source_evidence_path, must_exist=True)
    evidence = _load_digest_bound_evidence(
        path,
        cast(str, record.source_evidence_sha256),
        PersistedTrialSourceEvidence,
        label="trial-source evidence",
    )
    if (
        evidence.trial_id,
        evidence.candidate_id,
        evidence.request_sha256,
    ) != (trial.trial_id, trial.candidate_id, trial.request_sha256):
        raise ContractError("persisted source evidence differs from its audition trial")
    return evidence


def _assert_source_evidence_against_registry(
    resolved: ResolvedCandidate,
    evidence: PersistedTrialSourceEvidence,
) -> None:
    def identity(source: SourceLock) -> tuple[str, str, str, str]:
        return (source.source_id, source.kind, source.repository, source.revision)

    def observed(source: PersistedSourceEvidence) -> tuple[str, str, str, str]:
        return (source.source_id, source.kind, source.repository, source.revision)

    def locked_files(source: SourceLock, persisted: PersistedSourceEvidence) -> None:
        if source.kind != "huggingface":
            return
        expected = {item.path: item.sha256 for item in source.files}
        if set(persisted.files) != set(expected):
            raise ContractError("persisted source file membership differs from the registry lock")
        for path, digest in expected.items():
            if digest is not None and persisted.files[path] != digest:
                raise ContractError("persisted source artifact digest differs from the registry")

    if observed(evidence.primary) != identity(resolved.model_source):
        raise ContractError("persisted primary source evidence differs from the registry lock")
    locked_files(resolved.model_source, evidence.primary)
    expected_auxiliary = {source.repository: source for source in resolved.auxiliary_sources}
    if set(evidence.auxiliary) != set(expected_auxiliary):
        raise ContractError("persisted auxiliary source evidence differs from the registry lock")
    for repository, source in expected_auxiliary.items():
        if observed(evidence.auxiliary[repository]) != identity(source):
            raise ContractError("persisted auxiliary source identity differs from the registry")
        locked_files(source, evidence.auxiliary[repository])
    expected_code = resolved.code_source if resolved.candidate.engine == "seed-vc" else None
    if (evidence.code is None) != (expected_code is None):
        raise ContractError("persisted executable source evidence differs from the registry")
    if expected_code is not None and evidence.code is not None:
        if observed(evidence.code) != identity(expected_code):
            raise ContractError("persisted executable source identity differs from the registry")


def _verify_current_model_sources(
    registry: CandidateRegistry,
    layout: AssetLayout,
    plan: AuditionPlan,
    trial: AuditionTrial,
    result: ExperimentResult,
) -> None:
    invocation = _invocation(registry, layout, plan, trial)
    model = verify_local_source(invocation.resolved.model_source, invocation.model_path)
    auxiliary = {
        source.repository: verify_local_source(
            source, invocation.auxiliary_paths[source.repository]
        )
        for source in invocation.resolved.auxiliary_sources
    }
    code = (
        verify_local_source(invocation.resolved.code_source, invocation.code_path)
        if invocation.code_path is not None
        else None
    )
    if result.metadata.get("model_manifest_sha256") != model.manifest_sha256:
        raise ContractError("persisted result model manifest no longer matches local evidence")
    if result.metadata.get("auxiliary_manifest_sha256") != {
        repository: verification.manifest_sha256 for repository, verification in auxiliary.items()
    }:
        raise ContractError("persisted result auxiliary manifests no longer match local evidence")
    if result.metadata.get("code_checkout_integrity_sha256") != (
        code.integrity_sha256 if code is not None else None
    ):
        raise ContractError("persisted result executable checkout no longer matches local evidence")


def validate_persisted_trial_evidence(
    layout: AssetLayout,
    trial: AuditionTrial,
    record: PersistedTrialResult,
) -> None:
    """Revalidate the portable plan/result/evidence chain without a registry."""

    result = record.worker_result
    if record.request_sha256 != trial.request_sha256:
        raise ContractError(f"trial {trial.trial_id} was run with a different request")
    _assert_worker_binding(trial, result)
    if record.runtime_receipt_path is not None:
        receipt_path = confined_path(
            layout.root,
            record.runtime_receipt_path,
            must_exist=True,
        )
        load_persisted_runtime_receipt(
            receipt_path,
            expected_sha256=cast(str, record.runtime_receipt_sha256),
            runtime_id=trial.runtime_id,
            runtime_lock_sha256=trial.runtime_lock_sha256,
        )
        for name, receipt_expected in {
            "runtime_receipt_path": record.runtime_receipt_path,
            "runtime_receipt_sha256": record.runtime_receipt_sha256,
            "runtime_lock_sha256": trial.runtime_lock_sha256,
        }.items():
            if name in result.metadata:
                _assert_json_binding(name, _metadata_value(result, name), receipt_expected)
    source_evidence = _load_trial_source_evidence(layout, trial, record)
    if source_evidence is not None and result.ok:
        for name, source_expected in {
            "source_evidence_path": record.source_evidence_path,
            "source_evidence_sha256": record.source_evidence_sha256,
        }.items():
            _assert_json_binding(name, _metadata_value(result, name), source_expected)
    if record.normalized is not None and record.normalized.path != trial.normalized_output_path:
        raise ContractError("persisted normalized output path does not match the audition plan")
    reference = Path(trial.request.reference.path)
    _assert_file_digest(reference, trial.request.reference.sha256)
    if trial.request.source_audio_path is not None:
        _assert_file_digest(
            Path(trial.request.source_audio_path),
            cast(str, trial.request.source_audio_sha256),
            trial.request.source_audio_size_bytes,
        )
    if not result.ok:
        return

    output = Path(cast(str, result.output_path))
    _assert_file_digest(output, cast(str, result.output_sha256), result.output_bytes)
    if inspect_pcm_wav(output).audio != result.audio:
        raise ContractError("persisted native audio format differs from worker evidence")
    if result.environment is None:
        raise ContractError("successful persisted worker result lacks environment evidence")
    dependency = _evidence_path(
        output,
        result.environment.dependency_evidence_path,
        label="dependency evidence",
    )
    _assert_file_digest(dependency, result.environment.dependency_fingerprint_sha256)

    model_evidence_relative = _metadata_string(result, "model_evidence_path")
    model_evidence_sha256 = _metadata_string(result, "model_evidence_sha256")
    model_evidence_path = _evidence_path(output, model_evidence_relative, label="model evidence")
    model_evidence = _load_digest_bound_evidence(
        model_evidence_path,
        model_evidence_sha256,
        WorkerModelEvidence,
        label="worker-model evidence",
    )
    if source_evidence is None:
        raise ContractError("successful persisted trial omits portable source evidence")
    _assert_json_binding(
        "model_manifest_sha256",
        model_evidence.model_manifest_sha256,
        source_evidence.primary.manifest_sha256,
        evidence_label="model evidence",
    )
    _assert_json_binding(
        "auxiliary_manifest_sha256",
        model_evidence.auxiliary_manifest_sha256,
        {
            repository: evidence.manifest_sha256
            for repository, evidence in source_evidence.auxiliary.items()
        },
        evidence_label="model evidence",
    )
    _assert_json_binding(
        "code_checkout_integrity_sha256",
        model_evidence.code_checkout_integrity_sha256,
        source_evidence.code.integrity_sha256 if source_evidence.code is not None else None,
        evidence_label="model evidence",
    )

    code_relative = _metadata_string(result, "executed_code_evidence_path")
    code_sha256 = _metadata_string(result, "executed_code_evidence_sha256")
    code_path = _evidence_path(output, code_relative, label="executed-code evidence")
    code_evidence = _load_digest_bound_evidence(
        code_path,
        code_sha256,
        ExecutedCodeEvidence,
        label="executed-code evidence",
    )

    request_bindings: dict[str, object] = {
        "candidate_id": trial.candidate_id,
        "model_id": trial.request.model_id,
        "model_revision": trial.request.model_revision,
        "code_revision": trial.request.code_revision,
        "reference_sha256": trial.request.reference.sha256,
        "consent_sha256": trial.request.reference.consent_sha256,
        "source_audio_sha256": trial.request.source_audio_sha256,
        "source_audio_size_bytes": trial.request.source_audio_size_bytes,
        "settings": trial.request.settings,
    }
    for name, binding_expected in request_bindings.items():
        _assert_json_binding(name, getattr(model_evidence, name), binding_expected)

    result_bindings: dict[str, object] = {
        "model_manifest_sha256": model_evidence.model_manifest_sha256,
        "auxiliary_manifest_sha256": model_evidence.auxiliary_manifest_sha256,
        "code_checkout_integrity_sha256": model_evidence.code_checkout_integrity_sha256,
        "executed_code_evidence_path": model_evidence.executed_code_evidence_path,
        "executed_code_evidence_sha256": model_evidence.executed_code_evidence_sha256,
        "reference_sha256": model_evidence.reference_sha256,
        "source_audio_sha256": model_evidence.source_audio_sha256,
        "source_audio_size_bytes": model_evidence.source_audio_size_bytes,
        "runtime_package": model_evidence.runtime_package,
        "runtime_package_version": model_evidence.runtime_package_version,
    }
    for name, binding_expected in result_bindings.items():
        _assert_json_binding(name, _metadata_value(result, name), binding_expected)

    _assert_json_binding(
        "executed_code_evidence_path",
        model_evidence.executed_code_evidence_path,
        code_relative,
    )
    _assert_json_binding(
        "executed_code_evidence_sha256",
        model_evidence.executed_code_evidence_sha256,
        code_sha256,
    )
    distribution = code_evidence.distributions.get(model_evidence.runtime_package)
    if distribution is None:
        raise ContractError("executed-code evidence omits the candidate runtime distribution")
    _assert_json_binding(
        "runtime_package_version",
        distribution.version,
        model_evidence.runtime_package_version,
        evidence_label="executed-code evidence",
    )
    # A consent digest is evidence of a trial run before D-050 only: present in the model
    # evidence and the result together, or in neither.
    if model_evidence.consent_sha256 is None:
        if "consent_sha256" in result.metadata:
            raise ContractError("worker result names a consent record its evidence does not")
    else:
        _assert_json_binding(
            "consent_sha256",
            _metadata_value(result, "consent_sha256"),
            model_evidence.consent_sha256,
        )
    if model_evidence.watermarked is None:
        if "watermarked" in result.metadata:
            raise ContractError("worker result claims watermark evidence that is not applicable")
    else:
        _assert_json_binding(
            "watermarked",
            _metadata_value(result, "watermarked"),
            model_evidence.watermarked,
        )

    if record.normalized is not None:
        normalized = Path(record.normalized.path)
        _assert_file_digest(normalized, record.normalized.sha256, record.normalized.size_bytes)
        if inspect_pcm_wav(normalized).audio != record.normalized.audio:
            raise ContractError("persisted normalized audio format differs from evidence")


def _verify_persisted_trial_evidence(
    registry: CandidateRegistry,
    layout: AssetLayout,
    plan: AuditionPlan,
    trial: AuditionTrial,
    record: PersistedTrialResult,
    *,
    repository_root: Path,
    require_resident_sources: bool = False,
) -> None:
    validate_persisted_trial_evidence(layout, trial, record)
    resolved = registry.resolve(trial.candidate_id, plan.profile)
    source_evidence = _load_trial_source_evidence(layout, trial, record)
    if source_evidence is not None:
        _assert_source_evidence_against_registry(resolved, source_evidence)
    if record.runtime_receipt_sha256 is not None:
        if (
            trial.runtime_id != resolved.runtime.runtime_id
            or trial.runtime_lock_sha256 != canonical_sha256(resolved.runtime)
        ):
            raise ContractError("persisted trial runtime binding differs from the registry")
        receipt_path = confined_path(
            layout.root,
            cast(str, record.runtime_receipt_path),
            must_exist=True,
        )
        load_persisted_runtime_receipt(
            receipt_path,
            expected_sha256=record.runtime_receipt_sha256,
            runtime_id=trial.runtime_id,
            runtime_lock_sha256=trial.runtime_lock_sha256,
            runtime=resolved.runtime,
            repository_root=repository_root,
        )
    if require_resident_sources and record.worker_result.ok:
        _verify_current_model_sources(registry, layout, plan, trial, record.worker_result)


def _load_trial_result(
    path: Path,
    trial: AuditionTrial,
    *,
    registry: CandidateRegistry,
    layout: AssetLayout,
    plan: AuditionPlan,
    repository_root: Path,
) -> PersistedTrialResult:
    with open_regular_no_follow(path) as stream:
        existing = PersistedTrialResult.model_validate_json(stream.read())
    _verify_persisted_trial_evidence(
        registry,
        layout,
        plan,
        trial,
        existing,
        repository_root=repository_root,
    )
    return existing


def _persist_trial_result(
    registry: CandidateRegistry,
    layout: AssetLayout,
    paths: AuditionPaths,
    plan: AuditionPlan,
    trial: AuditionTrial,
    record: PersistedTrialResult,
    *,
    repository_root: Path,
) -> tuple[Path, PersistedTrialResult]:
    path = paths.results_directory / f"{trial.trial_id}.json"
    reject_symlink_components(layout.root, path)
    if path.exists():
        existing = _load_trial_result(
            path,
            trial,
            registry=registry,
            layout=layout,
            plan=plan,
            repository_root=repository_root,
        )
        return path, existing
    write_canonical_json(path, record)
    path.relative_to(layout.root)
    _verify_persisted_trial_evidence(
        registry,
        layout,
        plan,
        trial,
        record,
        repository_root=repository_root,
    )
    return path, record


def _pointer(layout: AssetLayout, path: Path, record: PersistedTrialResult) -> RunResultPointer:
    result_sha256, _ = sha256_file(path)
    return RunResultPointer(
        trial_id=record.worker_result.trial_id,
        candidate_id=record.worker_result.candidate_id,
        request_sha256=record.request_sha256,
        outcome=record.outcome,
        result_path=path.relative_to(layout.root).as_posix(),
        result_sha256=result_sha256,
    )


def _status(
    plan: AuditionPlan,
    direct_ids: set[str],
    voice_conversion_ids: set[str],
    pointers: Mapping[str, RunResultPointer],
) -> Literal["in-progress", "complete", "complete-with-failures"]:
    selected = direct_ids | voice_conversion_ids
    expected = {trial.trial_id for trial in plan.trials if trial.candidate_id in selected}
    if not expected.issubset(pointers):
        return "in-progress"
    if any(pointers[trial_id].outcome != "success" for trial_id in expected):
        return "complete-with-failures"
    return "complete"


def _write_run_manifest(
    layout: AssetLayout,
    paths: AuditionPaths,
    plan: AuditionPlan,
    plan_sha256: str,
    direct_ids: set[str],
    voice_conversion_ids: set[str],
    pointers: Mapping[str, RunResultPointer],
    *,
    now: datetime,
) -> AuditionRunManifest:
    manifest = AuditionRunManifest(
        experiment_id=plan.experiment_id,
        plan_sha256=plan_sha256,
        updated_at=now,
        status=_status(plan, direct_ids, voice_conversion_ids, pointers),
        direct_candidate_ids=tuple(sorted(direct_ids)),
        voice_conversion_candidate_ids=tuple(sorted(voice_conversion_ids)),
        results=tuple(sorted(pointers.values(), key=lambda item: item.trial_id)),
    )
    reject_symlink_components(layout.root, paths.run_manifest_path)
    write_canonical_json(paths.run_manifest_path, manifest)
    paths.run_manifest_path.relative_to(layout.root)
    return manifest


def _validate_selection(
    plan: AuditionPlan,
    direct_candidate_ids: Sequence[str] | None,
    voice_conversion_candidate_ids: Sequence[str],
) -> tuple[set[str], set[str]]:
    available_direct = {trial.candidate_id for trial in plan.trials if trial.track == "direct-tts"}
    available_vc = {
        trial.candidate_id for trial in plan.trials if trial.track == "voice-conversion"
    }
    direct = available_direct if direct_candidate_ids is None else set(direct_candidate_ids)
    vc = set(voice_conversion_candidate_ids)
    if len(direct) != len(direct_candidate_ids or direct):
        raise ContractError("direct candidate selection must be unique")
    if len(vc) != len(voice_conversion_candidate_ids):
        raise ContractError("voice-conversion candidate selection must be unique")
    if not direct.issubset(available_direct):
        raise ContractError("direct candidate selection is not present in the audition plan")
    if not vc.issubset(available_vc):
        raise ContractError("voice-conversion selection is not present in the audition plan")
    if not direct and not vc:
        raise ContractError("audition execution requires at least one selected candidate")
    return direct, vc


def load_validated_audition_run(
    registry: CandidateRegistry,
    layout: AssetLayout,
    plan: AuditionPlan,
    *,
    repository_root: Path | None = None,
) -> AuditionRunManifest:
    """Load a run only after rehashing every result and referenced evidence file."""

    repository_root = (
        (repository_root or Path(__file__).resolve().parents[2]).expanduser().resolve(strict=True)
    )
    validate_audition_plan_paths(layout, plan)
    paths = audition_paths(layout, plan.experiment_id)
    reject_symlink_components(layout.root, paths.run_manifest_path)
    with open_regular_no_follow(paths.run_manifest_path) as stream:
        manifest = AuditionRunManifest.model_validate_json(stream.read())
    if manifest.experiment_id != plan.experiment_id:
        raise ContractError("run manifest experiment ID does not match the audition plan")
    plan_sha256, _ = sha256_file(paths.plan_path)
    if manifest.plan_sha256 != plan_sha256:
        raise ContractError("run manifest is bound to a different audition plan")
    direct, vc = _validate_selection(
        plan,
        manifest.direct_candidate_ids,
        manifest.voice_conversion_candidate_ids,
    )
    selected = direct | vc
    trials = {trial.trial_id: trial for trial in plan.trials}
    verified_pointers: dict[str, RunResultPointer] = {}
    for pointer in manifest.results:
        try:
            trial = trials[pointer.trial_id]
        except KeyError as error:
            raise ContractError("run manifest references a trial absent from its plan") from error
        if trial.candidate_id not in selected:
            raise ContractError("run manifest result is outside its selected candidates")
        expected_path = paths.results_directory / f"{trial.trial_id}.json"
        if pointer.result_path != expected_path.relative_to(layout.root).as_posix():
            raise ContractError("run manifest result path differs from the plan convention")
        _assert_file_digest(expected_path, pointer.result_sha256)
        record = _load_trial_result(
            expected_path,
            trial,
            registry=registry,
            layout=layout,
            plan=plan,
            repository_root=repository_root,
        )
        verified = _pointer(layout, expected_path, record)
        if verified != pointer:
            raise ContractError("run manifest pointer differs from revalidated trial evidence")
        verified_pointers[trial.trial_id] = verified
    expected_status = _status(plan, direct, vc, verified_pointers)
    if manifest.status != expected_status:
        raise ContractError("run manifest status differs from revalidated trial evidence")
    return manifest


def _invocation(
    registry: CandidateRegistry,
    layout: AssetLayout,
    plan: AuditionPlan,
    trial: AuditionTrial,
) -> WorkerInvocation:
    resolved: ResolvedCandidate = registry.resolve(trial.candidate_id, plan.profile)
    if (
        trial.runtime_id != resolved.runtime.runtime_id
        or trial.runtime_lock_sha256 != canonical_sha256(resolved.runtime)
    ):
        raise ContractError("audition trial runtime binding differs from the registry lock")
    material_paths = {
        source.source_id: source_path(layout, source.source_id)
        for source in resolved.auxiliary_sources
    }
    code = (
        checkout_path(layout, resolved.code_source.source_id)
        if resolved.candidate.engine == "seed-vc"
        else None
    )
    return WorkerInvocation(
        request=trial.request,
        resolved=resolved,
        runtime_python=runtime_path(layout, resolved.runtime.runtime_id) / "bin/python",
        model_path=source_path(layout, resolved.model_source.source_id),
        auxiliary_paths=source_paths_by_repository(resolved.auxiliary_sources, material_paths),
        worker_lock_path=layout.directory("runtimes") / "worker.lock",
        runtime_root=layout.directory("runtimes"),
        code_path=code,
    )


def _persist_trial_runtime_receipt(
    registry: CandidateRegistry,
    layout: AssetLayout,
    paths: AuditionPaths,
    plan: AuditionPlan,
    trial: AuditionTrial,
    *,
    repository_root: Path,
    expected_receipt_sha256: str | None,
) -> tuple[str, str]:
    if expected_receipt_sha256 is None:
        raise ContractError(
            f"new execution requires an approved runtime receipt digest for {trial.runtime_id}"
        )
    invocation = _invocation(registry, layout, plan, trial)
    receipt, receipt_sha256 = verify_runtime_install_receipt(
        invocation.resolved.runtime,
        runtime_path(layout, trial.runtime_id),
        repository_root=repository_root,
        expected_receipt_sha256=expected_receipt_sha256,
    )
    directory = paths.experiment_directory / "evidence/runtime-receipts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / f"{trial.trial_id}.json"
    reject_symlink_components(layout.root, destination)
    expected = canonical_json_bytes(receipt) + b"\n"
    if destination.exists():
        with open_regular_no_follow(destination) as stream:
            if stream.read() != expected:
                raise ContractError(
                    "persisted trial runtime receipt differs from resident evidence"
                )
    else:
        written_sha256 = write_canonical_json(destination, receipt, mode=0o600)
        if written_sha256 != receipt_sha256:
            raise ContractError("copied runtime receipt digest changed during persistence")
    return destination.relative_to(layout.root).as_posix(), receipt_sha256


def _persist_trial_source_evidence(
    registry: CandidateRegistry,
    layout: AssetLayout,
    paths: AuditionPaths,
    plan: AuditionPlan,
    trial: AuditionTrial,
) -> tuple[str, str]:
    """Copy all source-manifest/integrity preimages before resident caches may be evicted."""

    invocation = _invocation(registry, layout, plan, trial)
    primary_verification = verify_local_source(
        invocation.resolved.model_source,
        invocation.model_path,
    )
    auxiliary_verification = {
        source.repository: verify_local_source(
            source,
            invocation.auxiliary_paths[source.repository],
        )
        for source in invocation.resolved.auxiliary_sources
    }
    code_verification = (
        verify_local_source(invocation.resolved.code_source, invocation.code_path)
        if invocation.code_path is not None
        else None
    )
    evidence = PersistedTrialSourceEvidence(
        trial_id=trial.trial_id,
        candidate_id=trial.candidate_id,
        request_sha256=trial.request_sha256,
        primary=_portable_source_evidence(
            invocation.resolved.model_source,
            primary_verification,
        ),
        auxiliary={
            source.repository: _portable_source_evidence(
                source,
                auxiliary_verification[source.repository],
            )
            for source in invocation.resolved.auxiliary_sources
        },
        code=(
            _portable_source_evidence(invocation.resolved.code_source, code_verification)
            if code_verification is not None
            else None
        ),
    )
    directory = paths.experiment_directory / "evidence/source-manifests"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / f"{trial.trial_id}.json"
    reject_symlink_components(layout.root, destination)
    expected = canonical_json_bytes(evidence) + b"\n"
    if destination.exists():
        with open_regular_no_follow(destination) as stream:
            if stream.read() != expected:
                raise ContractError("persisted trial source evidence changed between executions")
        digest = hashlib.sha256(expected).hexdigest()
    else:
        digest = write_canonical_json(destination, evidence, mode=0o600)
    return destination.relative_to(layout.root).as_posix(), digest


def _run_offline_audition_locked(
    registry: CandidateRegistry,
    layout: AssetLayout,
    plan: AuditionPlan,
    *,
    direct_candidate_ids: Sequence[str] | None = None,
    voice_conversion_candidate_ids: Sequence[str] = (),
    trial_ids: Sequence[str] | None = None,
    now: datetime | None = None,
    repository_root: Path,
    expected_runtime_receipt_sha256: Mapping[str, str],
) -> AuditionRunManifest:
    """Run prepared trials sequentially; this API never provisions or downloads."""

    paths = audition_paths(layout, plan.experiment_id)
    _, plan_sha256 = persist_audition_plan(layout, plan)
    direct, vc = _validate_selection(plan, direct_candidate_ids, voice_conversion_candidate_ids)
    selected_candidates = direct | vc
    selected_trial_ids: set[str] | None = None
    if trial_ids is not None:
        selected_trial_ids = set(trial_ids)
        if len(selected_trial_ids) != len(trial_ids):
            raise ContractError("trial selection must be unique")
    candidate_trials = tuple(
        trial for trial in plan.trials if trial.candidate_id in selected_candidates
    )
    known_trial_ids = {trial.trial_id for trial in candidate_trials}
    if selected_trial_ids is not None and not selected_trial_ids.issubset(known_trial_ids):
        raise ContractError("trial selection is not present for the selected candidates")

    existing_manifest: AuditionRunManifest | None = None
    pointers: dict[str, RunResultPointer] = {}
    if paths.run_manifest_path.exists():
        existing_manifest = load_validated_audition_run(
            registry,
            layout,
            plan,
            repository_root=repository_root,
        )
        pointers = {pointer.trial_id: pointer for pointer in existing_manifest.results}
        direct.update(existing_manifest.direct_candidate_ids)
        vc.update(existing_manifest.voice_conversion_candidate_ids)

    execution_time = _aware_utc(now or utc_now())
    changed = existing_manifest is None or (
        tuple(sorted(direct)) != existing_manifest.direct_candidate_ids
        or tuple(sorted(vc)) != existing_manifest.voice_conversion_candidate_ids
    )
    for trial in candidate_trials:
        if selected_trial_ids is not None and trial.trial_id not in selected_trial_ids:
            continue
        result_path = paths.results_directory / f"{trial.trial_id}.json"
        if result_path.exists():
            reject_symlink_components(layout.root, result_path)
            record = _load_trial_result(
                result_path,
                trial,
                registry=registry,
                layout=layout,
                plan=plan,
                repository_root=repository_root,
            )
            pointer = _pointer(layout, result_path, record)
            if pointers.get(trial.trial_id) != pointer:
                changed = True
            pointers[trial.trial_id] = pointer
            continue

        runtime_receipt_evidence: tuple[str, str] | None = None
        source_evidence: tuple[str, str] | None = None
        try:
            runtime_receipt_evidence = _persist_trial_runtime_receipt(
                registry,
                layout,
                paths,
                plan,
                trial,
                repository_root=repository_root,
                expected_receipt_sha256=expected_runtime_receipt_sha256.get(trial.runtime_id),
            )
            source_evidence = _persist_trial_source_evidence(
                registry,
                layout,
                paths,
                plan,
                trial,
            )
            invocation = _invocation(registry, layout, plan, trial)
            worker_result = run_worker(invocation)
            verify_runtime_install_receipt(
                invocation.resolved.runtime,
                runtime_path(layout, trial.runtime_id),
                repository_root=repository_root,
                expected_receipt_sha256=runtime_receipt_evidence[1],
            )
            _assert_worker_binding(trial, worker_result)
        except Exception as error:
            worker_result = _failed_worker_result(trial, error)
        runtime_receipt_path = (
            runtime_receipt_evidence[0] if runtime_receipt_evidence is not None else None
        )
        runtime_receipt_sha256 = (
            runtime_receipt_evidence[1] if runtime_receipt_evidence is not None else None
        )
        source_evidence_path = source_evidence[0] if source_evidence is not None else None
        source_evidence_sha256 = source_evidence[1] if source_evidence is not None else None
        if runtime_receipt_evidence is not None:
            worker_result = worker_result.model_copy(
                update={
                    "metadata": {
                        **worker_result.metadata,
                        "runtime_receipt_path": runtime_receipt_path,
                        "runtime_receipt_sha256": runtime_receipt_sha256,
                        "runtime_lock_sha256": trial.runtime_lock_sha256,
                        **(
                            {
                                "source_evidence_path": source_evidence_path,
                                "source_evidence_sha256": source_evidence_sha256,
                            }
                            if source_evidence is not None
                            else {}
                        ),
                    }
                }
            )
        if not worker_result.ok:
            record = PersistedTrialResult(
                request_sha256=trial.request_sha256,
                completed_at=execution_time,
                outcome="worker-failed",
                worker_result=worker_result,
                runtime_receipt_path=runtime_receipt_path,
                runtime_receipt_sha256=runtime_receipt_sha256,
                source_evidence_path=source_evidence_path,
                source_evidence_sha256=source_evidence_sha256,
                error_code=worker_result.error_code or "worker_failed",
                error_message=worker_result.error_message or "worker failed without details",
            )
        else:
            try:
                normalized = execute_normalization(
                    build_normalization_plan(
                        Path(trial.request.output_path), Path(trial.normalized_output_path)
                    )
                )
                normalized_record = _normalized_record(trial, worker_result, normalized)
            except Exception as error:
                record = PersistedTrialResult(
                    request_sha256=trial.request_sha256,
                    completed_at=execution_time,
                    outcome="normalization-failed",
                    worker_result=worker_result,
                    runtime_receipt_path=runtime_receipt_path,
                    runtime_receipt_sha256=runtime_receipt_sha256,
                    source_evidence_path=source_evidence_path,
                    source_evidence_sha256=source_evidence_sha256,
                    error_code="normalization_failed",
                    error_message=(str(error) or error.__class__.__name__)[:4_000],
                )
            else:
                record = PersistedTrialResult(
                    request_sha256=trial.request_sha256,
                    completed_at=execution_time,
                    outcome="success",
                    worker_result=worker_result,
                    runtime_receipt_path=runtime_receipt_path,
                    runtime_receipt_sha256=runtime_receipt_sha256,
                    source_evidence_path=source_evidence_path,
                    source_evidence_sha256=source_evidence_sha256,
                    normalized=normalized_record,
                )
        result_path, persisted = _persist_trial_result(
            registry,
            layout,
            paths,
            plan,
            trial,
            record,
            repository_root=repository_root,
        )
        pointers[trial.trial_id] = _pointer(layout, result_path, persisted)
        changed = True
        _write_run_manifest(
            layout,
            paths,
            plan,
            plan_sha256,
            direct,
            vc,
            pointers,
            now=execution_time,
        )

    if not changed and existing_manifest is not None:
        return existing_manifest
    return _write_run_manifest(
        layout,
        paths,
        plan,
        plan_sha256,
        direct,
        vc,
        pointers,
        now=execution_time,
    )


def run_offline_audition(
    registry: CandidateRegistry,
    layout: AssetLayout,
    plan: AuditionPlan,
    *,
    direct_candidate_ids: Sequence[str] | None = None,
    voice_conversion_candidate_ids: Sequence[str] = (),
    trial_ids: Sequence[str] | None = None,
    now: datetime | None = None,
    repository_root: Path | None = None,
    expected_runtime_receipt_sha256: Mapping[str, str] | None = None,
) -> AuditionRunManifest:
    """Run prepared trials sequentially; this API never provisions or downloads."""

    repository_root = (
        (repository_root or Path(__file__).resolve().parents[2]).expanduser().resolve(strict=True)
    )
    runtime_root = layout.directory("runtimes")
    lock_path = runtime_root / "audition-orchestrator.lock"
    with exclusive_worker_slot(lock_path, boundary=runtime_root):
        return _run_offline_audition_locked(
            registry,
            layout,
            plan,
            direct_candidate_ids=direct_candidate_ids,
            voice_conversion_candidate_ids=voice_conversion_candidate_ids,
            trial_ids=trial_ids,
            now=now,
            repository_root=repository_root,
            expected_runtime_receipt_sha256=expected_runtime_receipt_sha256 or {},
        )

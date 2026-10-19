"""The records of the Mac's zero-shot bake-off, kept so that blind review can read them.

An audition was one run of the bake-off's candidate matrix: its plan, each trial's persisted
result with its worker, runtime and source evidence, and the run's manifest. The bake-off and
its runtimes were retired on 2026-10-19 (D-057), so nothing writes these records any more;
an audition run already on disk still validates here, byte for byte, without a registry, and
blind review (``blind.py``) builds, verifies and aggregates from it.
"""

from __future__ import annotations

import hashlib
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
)
from .errors import ContractError
from .models import (
    AudioSpec,
    ContractModel,
    ExecutedCodeEvidence,
    ExperimentRequest,
    ExperimentResult,
    PromptSuite,
    WorkerModelEvidence,
)
from .paths import (
    AssetLayout,
    confined_path,
    reject_symlink_components,
    validate_relative_path,
)
from .runtime import LocalSourceManifest, load_persisted_runtime_receipt

AUDITION_PLAN_SCHEMA: Literal["cole-voice-lab.audition-plan/v1"] = "cole-voice-lab.audition-plan/v1"
AUDITION_RESULT_SCHEMA: Literal["cole-voice-lab.audition-trial-result/v1"] = (
    "cole-voice-lab.audition-trial-result/v1"
)
AUDITION_RUN_SCHEMA: Literal["cole-voice-lab.audition-run/v1"] = "cole-voice-lab.audition-run/v1"
_MAX_PERSISTED_EVIDENCE_BYTES = 64 * 1024 * 1024

# A candidate's weight profile and a trial's timing, as the bake-off recorded them.
ProfileName = Literal["smoke", "quality"]
TimingMode = Literal["cold", "warm"]


def _absolute_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("audition execution paths must be normalized absolute paths")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("audition timestamps require an explicit timezone")
    return value.astimezone(UTC)


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

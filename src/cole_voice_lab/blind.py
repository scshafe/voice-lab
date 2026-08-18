"""Deterministic private blinding and listening-score aggregation."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import stat
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .audio import inspect_pcm_wav
from .audition import (
    AuditionPlan,
    AuditionRunManifest,
    PersistedTrialResult,
    audition_paths,
    validate_audition_plan_paths,
    validate_persisted_trial_evidence,
)
from .canonical import (
    canonical_json_bytes,
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .errors import ContractError, SecurityError
from .models import CandidateId, Sha256, StableId, utc_now
from .paths import AssetLayout, confined_path, validate_relative_path
from .resources import resource_path

type BlindId = Annotated[str, StringConstraints(pattern=r"^sample-[0-9a-f]{16}$")]
type Dimension = Literal[
    "likeness", "naturalness", "prosody", "intelligibility", "artifact_freedom"
]

DIMENSIONS: tuple[Dimension, ...] = (
    "likeness",
    "naturalness",
    "prosody",
    "intelligibility",
    "artifact_freedom",
)
_STABLE_ID: TypeAdapter[StableId] = TypeAdapter(StableId)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require an explicit timezone")
    return value.astimezone(UTC)


class BlindModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BlindInput(BlindModel):
    """A minimal pointer to immutable audition evidence.

    Candidate, prompt, seed, track, and audio labels are deliberately absent:
    package construction derives them from the committed plan/run/result chain.
    """

    experiment_id: StableId
    trial_id: StableId
    plan_sha256: Sha256
    run_manifest_sha256: Sha256
    result_sha256: Sha256


class ReviewSample(BlindModel):
    blind_id: BlindId
    track: Literal["direct-tts", "voice-conversion"]
    prompt_id: StableId
    prompt_text: str
    audio_path: str
    audio_sha256: Sha256
    order: int = Field(ge=0)

    @field_validator("audio_path")
    @classmethod
    def _relative_audio(cls, value: str) -> str:
        return validate_relative_path(value)


class ReviewIndex(BlindModel):
    schema_version: Literal["cole-voice-lab.blind-review/v1"] = "cole-voice-lab.blind-review/v1"
    review_id: StableId
    created_at: datetime
    key_commitment_sha256: Sha256
    dimensions: tuple[Dimension, ...] = DIMENSIONS
    samples: tuple[ReviewSample, ...] = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _unique_blind_ids(self) -> ReviewIndex:
        ids = [sample.blind_id for sample in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("blind sample IDs must be unique")
        if self.dimensions != DIMENSIONS:
            raise ValueError("blind review dimensions must be the fixed complete dimension set")
        if tuple(sample.order for sample in self.samples) != tuple(range(len(self.samples))):
            raise ValueError("blind samples must be stored in contiguous review order")
        return self


class RevealEntry(BlindModel):
    blind_id: BlindId
    experiment_id: StableId
    trial_id: StableId
    candidate_id: CandidateId
    track: Literal["direct-tts", "voice-conversion"]
    prompt_id: StableId
    output_sha256: Sha256
    seed: int
    timing_mode: Literal["cold", "warm"]
    reference_sha256: Sha256
    source_audio_sha256: Sha256 | None = None
    request_sha256: Sha256
    plan_sha256: Sha256
    run_manifest_sha256: Sha256
    result_sha256: Sha256


class RevealMap(BlindModel):
    schema_version: Literal["cole-voice-lab.blind-reveal/v1"] = "cole-voice-lab.blind-reveal/v1"
    review_id: StableId
    key_hex: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    entries: tuple[RevealEntry, ...] = Field(min_length=1)


class ListeningScore(BlindModel):
    blind_id: BlindId
    likeness: int = Field(ge=1, le=5)
    naturalness: int = Field(ge=1, le=5)
    prosody: int = Field(ge=1, le=5)
    intelligibility: int = Field(ge=1, le=5)
    artifact_freedom: int = Field(ge=1, le=5)
    notes: str = Field(default="", max_length=2_000)


class ScoreSheet(BlindModel):
    schema_version: Literal["cole-voice-lab.listening-scores/v2"] = (
        "cole-voice-lab.listening-scores/v2"
    )
    review_id: StableId
    review_index_sha256: Sha256
    public_manifest_sha256: Sha256
    reviewer_id: StableId
    scored_at: datetime
    headphones_used: Literal[True]
    scores: tuple[ListeningScore, ...] = Field(min_length=1)

    @field_validator("scored_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class PackageFile(BlindModel):
    path: str = Field(min_length=1, max_length=1_024)
    sha256: Sha256
    size_bytes: int = Field(gt=0)

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return validate_relative_path(value)


class ReviewPackageManifest(BlindModel):
    schema_version: Literal["cole-voice-lab.blind-package/v1"] = "cole-voice-lab.blind-package/v1"
    review_id: StableId
    created_at: datetime
    review_index_sha256: Sha256
    public_manifest_sha256: Sha256
    reveal_sha256: Sha256
    files: tuple[PackageFile, ...] = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _unique_paths(self) -> ReviewPackageManifest:
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("blind-package file paths must be unique")
        return self


class PublicReviewManifest(BlindModel):
    """Reviewer-safe commitments that can be served from ``public/`` alone."""

    schema_version: Literal["cole-voice-lab.blind-public-package/v1"] = (
        "cole-voice-lab.blind-public-package/v1"
    )
    review_id: StableId
    created_at: datetime
    review_index_sha256: Sha256
    files: tuple[PackageFile, ...] = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _unique_paths(self) -> PublicReviewManifest:
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("public blind-package file paths must be unique")
        if "review-manifest.json" in paths:
            raise ValueError("public manifest cannot recursively commit itself")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedReviewPackage:
    root: Path
    index: ReviewIndex
    reveal: RevealMap
    public_manifest: PublicReviewManifest
    manifest: ReviewPackageManifest
    review_index_sha256: str
    public_manifest_sha256: str
    package_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _ResolvedBlindInput:
    binding: BlindInput
    candidate_id: str
    track: Literal["direct-tts", "voice-conversion"]
    prompt_id: str
    prompt_text: str
    audio_path: Path
    output_sha256: str
    output_size_bytes: int
    seed: int
    timing_mode: Literal["cold", "warm"]
    reference_sha256: str
    source_audio_sha256: str | None
    request_sha256: str

    @property
    def fairness_key(self) -> tuple[str, str, int, str, str | None]:
        return (
            self.prompt_id,
            self.reference_sha256,
            self.seed,
            self.timing_mode,
            self.source_audio_sha256,
        )


def _blind_id_parts(
    key: bytes,
    *,
    trial_id: str,
    candidate_id: str,
    prompt_id: str,
    output_sha256: str,
    seed: int,
    request_sha256: str,
    plan_sha256: str,
    run_manifest_sha256: str,
    result_sha256: str,
) -> str:
    message = "\x00".join(
        (
            trial_id,
            candidate_id,
            prompt_id,
            output_sha256,
            str(seed),
            request_sha256,
            plan_sha256,
            run_manifest_sha256,
            result_sha256,
        )
    ).encode()
    return f"sample-{hmac.new(key, message, hashlib.sha256).hexdigest()[:16]}"


def _blind_id(key: bytes, item: _ResolvedBlindInput) -> str:
    return _blind_id_parts(
        key,
        trial_id=item.binding.trial_id,
        candidate_id=item.candidate_id,
        prompt_id=item.prompt_id,
        output_sha256=item.output_sha256,
        seed=item.seed,
        request_sha256=item.request_sha256,
        plan_sha256=item.binding.plan_sha256,
        run_manifest_sha256=item.binding.run_manifest_sha256,
        result_sha256=item.binding.result_sha256,
    )


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    actual_sha256, _ = sha256_file(source)
    if actual_sha256 != expected_sha256:
        raise SecurityError(f"blind-review source digest mismatch: {source}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            with open_regular_no_follow(source) as input_stream:
                shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    copied_sha256, _ = sha256_file(destination)
    if copied_sha256 != expected_sha256:
        destination.unlink(missing_ok=True)
        raise SecurityError("blind-review copy failed post-copy digest verification")


def _read_canonical_contract[ModelT: BaseModel](
    path: Path, model: type[ModelT]
) -> tuple[ModelT, str]:
    with open_regular_no_follow(path) as stream:
        payload = stream.read(16 * 1024 * 1024 + 1)
    if len(payload) > 16 * 1024 * 1024:
        raise ContractError(f"audition evidence exceeds the size limit: {path}")
    parsed = model.model_validate_json(payload)
    if payload != canonical_json_bytes(parsed) + b"\n":
        raise ContractError(f"audition evidence is not canonical JSON: {path}")
    return parsed, hashlib.sha256(payload).hexdigest()


def _rehash_bound_file(
    layout: AssetLayout,
    path: Path,
    *,
    boundary: Path,
    expected_sha256: str,
    expected_size: int | None,
    label: str,
) -> None:
    try:
        relative = path.relative_to(boundary).as_posix()
    except ValueError as error:
        raise SecurityError(f"{label} escapes its audition boundary") from error
    canonical = confined_path(boundary, relative, must_exist=True)
    if canonical != path:
        raise SecurityError(f"{label} path is not canonical")
    metadata = canonical.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SecurityError(f"{label} must be a single-link regular file")
    digest, size = sha256_file(canonical)
    if digest != expected_sha256 or (expected_size is not None and size != expected_size):
        raise SecurityError(f"{label} digest or size differs from audition evidence")
    canonical.relative_to(layout.root)


def _resolve_blind_input(layout: AssetLayout, item: BlindInput) -> _ResolvedBlindInput:
    paths = audition_paths(layout, item.experiment_id)
    plan, plan_digest = _read_canonical_contract(paths.plan_path, AuditionPlan)
    if plan_digest != item.plan_sha256 or plan.experiment_id != item.experiment_id:
        raise ContractError("blind input does not bind the exact audition plan")
    validate_audition_plan_paths(layout, plan)
    run, run_digest = _read_canonical_contract(paths.run_manifest_path, AuditionRunManifest)
    if (
        run_digest != item.run_manifest_sha256
        or run.experiment_id != item.experiment_id
        or run.plan_sha256 != plan_digest
    ):
        raise ContractError("blind input does not bind the exact audition run manifest")
    trials = [trial for trial in plan.trials if trial.trial_id == item.trial_id]
    pointers = [pointer for pointer in run.results if pointer.trial_id == item.trial_id]
    if len(trials) != 1 or len(pointers) != 1:
        raise ContractError("blind trial must occur exactly once in its plan and run")
    trial = trials[0]
    pointer = pointers[0]
    expected_result = paths.results_directory / f"{item.trial_id}.json"
    expected_relative = expected_result.relative_to(layout.root).as_posix()
    if (
        pointer.candidate_id != trial.candidate_id
        or pointer.request_sha256 != trial.request_sha256
        or pointer.outcome != "success"
        or pointer.result_path != expected_relative
        or pointer.result_sha256 != item.result_sha256
    ):
        raise ContractError("blind trial pointer disagrees with its audition plan")
    result, result_digest = _read_canonical_contract(expected_result, PersistedTrialResult)
    if result_digest != item.result_sha256:
        raise ContractError("blind input does not bind the exact persisted trial result")
    if (
        result.outcome != "success"
        or result.request_sha256 != trial.request_sha256
        or result.normalized is None
        or not result.worker_result.ok
        or result.worker_result.trial_id != trial.trial_id
        or result.worker_result.candidate_id != trial.candidate_id
        or result.worker_result.output_path != trial.request.output_path
        or result.normalized.path != trial.normalized_output_path
    ):
        raise ContractError("blind trial result disagrees with its plan binding")
    validate_persisted_trial_evidence(layout, trial, result)

    experiment_root = paths.experiment_directory
    native_path = Path(trial.request.output_path)
    normalized_path = Path(trial.normalized_output_path)
    native_sha256 = result.worker_result.output_sha256
    native_size = result.worker_result.output_bytes
    native_audio = result.worker_result.audio
    if native_sha256 is None or native_size is None or native_audio is None:
        raise ContractError("successful blind trial lacks complete native evidence")
    _rehash_bound_file(
        layout,
        native_path,
        boundary=experiment_root,
        expected_sha256=native_sha256,
        expected_size=native_size,
        label="native audition audio",
    )
    _rehash_bound_file(
        layout,
        normalized_path,
        boundary=experiment_root,
        expected_sha256=result.normalized.sha256,
        expected_size=result.normalized.size_bytes,
        label="normalized audition audio",
    )
    if inspect_pcm_wav(native_path).audio != native_audio:
        raise SecurityError("native audition WAV structure differs from result evidence")
    if inspect_pcm_wav(normalized_path).audio != result.normalized.audio:
        raise SecurityError("normalized audition WAV structure differs from result evidence")

    environment = result.worker_result.environment
    if environment is None:
        raise ContractError("blind trial lacks dependency evidence")
    dependency_path = native_path.parent / environment.dependency_evidence_path
    _rehash_bound_file(
        layout,
        dependency_path,
        boundary=experiment_root,
        expected_sha256=environment.dependency_fingerprint_sha256,
        expected_size=None,
        label="dependency evidence",
    )
    model_path = result.worker_result.metadata.get("model_evidence_path")
    model_digest = result.worker_result.metadata.get("model_evidence_sha256")
    if not isinstance(model_path, str) or not isinstance(model_digest, str):
        raise ContractError("blind trial lacks checksum-bound model evidence")
    _rehash_bound_file(
        layout,
        native_path.parent / validate_relative_path(model_path),
        boundary=experiment_root,
        expected_sha256=model_digest,
        expected_size=None,
        label="model evidence",
    )

    prompt_id = trial.request.settings.get("prompt_id")
    if not isinstance(prompt_id, str):
        raise ContractError("audition request lacks a prompt ID")
    prompt_id = _STABLE_ID.validate_python(prompt_id, strict=True)
    return _ResolvedBlindInput(
        binding=item,
        candidate_id=trial.candidate_id,
        track=trial.track,
        prompt_id=prompt_id,
        prompt_text=trial.request.text,
        audio_path=normalized_path,
        output_sha256=result.normalized.sha256,
        output_size_bytes=result.normalized.size_bytes,
        seed=trial.request.seed,
        timing_mode=trial.timing_mode,
        reference_sha256=trial.request.reference.sha256,
        source_audio_sha256=trial.request.source_audio_sha256,
        request_sha256=trial.request_sha256,
    )


def _validate_complete_fair_matrix(
    layout: AssetLayout, resolved: tuple[_ResolvedBlindInput, ...]
) -> None:
    bindings = {item.binding for item in resolved}
    if (
        len({item.experiment_id for item in bindings}) != 1
        or len({item.plan_sha256 for item in bindings}) != 1
        or len({item.run_manifest_sha256 for item in bindings}) != 1
    ):
        raise ContractError("one blind review must bind one exact audition run")
    if len({item.binding.trial_id for item in resolved}) != len(resolved):
        raise ContractError("blind-review trial inputs must be unique")

    first = resolved[0].binding
    paths = audition_paths(layout, first.experiment_id)
    plan, _ = _read_canonical_contract(paths.plan_path, AuditionPlan)
    validate_audition_plan_paths(layout, plan)
    run, _ = _read_canonical_contract(paths.run_manifest_path, AuditionRunManifest)
    if run.status != "complete":
        raise ContractError("blind review requires a complete failure-free audition run")
    included_tracks = {item.track for item in resolved}
    selected_by_track = {
        "direct-tts": set(run.direct_candidate_ids),
        "voice-conversion": set(run.voice_conversion_candidate_ids),
    }
    planned_by_track = {
        track: {trial.candidate_id for trial in plan.trials if trial.track == track}
        for track in ("direct-tts", "voice-conversion")
    }
    for track in included_tracks:
        if selected_by_track[track] != planned_by_track[track]:
            raise ContractError(
                f"blind review requires every planned {track} candidate to be completed"
            )
        actual_candidates = {item.candidate_id for item in resolved if item.track == track}
        if actual_candidates != planned_by_track[track]:
            raise ContractError(f"blind review must include every selected {track} candidate")
        if track == "direct-tts" and len(actual_candidates) < 2:
            raise ContractError("direct-TTS blind review requires at least two candidates")

    expected_trials = {
        trial.trial_id
        for trial in plan.trials
        if trial.track in included_tracks and trial.candidate_id in selected_by_track[trial.track]
    }
    actual_trials = {item.binding.trial_id for item in resolved}
    if actual_trials != expected_trials:
        raise ContractError("blind review must include every selected audition cell exactly")

    for track in included_tracks:
        by_candidate: dict[str, set[tuple[str, str, int, str, str | None]]] = {}
        counts: dict[str, int] = {}
        for item in resolved:
            if item.track != track:
                continue
            by_candidate.setdefault(item.candidate_id, set()).add(item.fairness_key)
            counts[item.candidate_id] = counts.get(item.candidate_id, 0) + 1
        if any(len(by_candidate[candidate]) != count for candidate, count in counts.items()):
            raise ContractError("a candidate contains duplicate blind-review cells")
        expected = next(iter(by_candidate.values()))
        if any(cells != expected for cells in by_candidate.values()):
            raise ContractError(f"{track} blind-review matrices are not balanced")


def blind_inputs_from_audition(
    layout: AssetLayout,
    *,
    experiment_id: str,
    tracks: tuple[Literal["direct-tts", "voice-conversion"], ...] = ("direct-tts",),
) -> tuple[BlindInput, ...]:
    """Derive review inputs without accepting any caller-supplied attribution."""

    if not tracks or len(tracks) != len(set(tracks)):
        raise ContractError("blind-review track selection must be non-empty and unique")
    paths = audition_paths(layout, experiment_id)
    plan, plan_sha256 = _read_canonical_contract(paths.plan_path, AuditionPlan)
    validate_audition_plan_paths(layout, plan)
    run, run_sha256 = _read_canonical_contract(paths.run_manifest_path, AuditionRunManifest)
    by_trial = {trial.trial_id: trial for trial in plan.trials}
    if any(pointer.trial_id not in by_trial for pointer in run.results):
        raise ContractError("audition run references a trial absent from its bound plan")
    inputs = tuple(
        BlindInput(
            experiment_id=experiment_id,
            trial_id=pointer.trial_id,
            plan_sha256=plan_sha256,
            run_manifest_sha256=run_sha256,
            result_sha256=pointer.result_sha256,
        )
        for pointer in sorted(run.results, key=lambda item: item.trial_id)
        if pointer.trial_id in by_trial and by_trial[pointer.trial_id].track in tracks
    )
    if not inputs:
        raise ContractError("audition run has no results in the selected review tracks")
    resolved = tuple(_resolve_blind_input(layout, item) for item in inputs)
    _validate_complete_fair_matrix(layout, resolved)
    return inputs


def build_review_package(
    layout: AssetLayout,
    *,
    review_id: str,
    inputs: tuple[BlindInput, ...],
    key_hex: str | None = None,
) -> VerifiedReviewPackage:
    if not inputs:
        raise ContractError("blind review requires at least one input")
    AssetLayout.validate(layout.root)
    review_id = _STABLE_ID.validate_python(review_id, strict=True)
    resolved = tuple(_resolve_blind_input(layout, item) for item in inputs)
    _validate_complete_fair_matrix(layout, resolved)

    output_parent = layout.directory("evaluations") / "blind-reviews"
    output_parent.mkdir(mode=0o700, exist_ok=True)
    if output_parent.is_symlink() or not output_parent.is_dir():
        raise SecurityError("blind-review output parent is unsafe")
    output_root = output_parent / review_id
    if output_root.exists():
        raise FileExistsError(output_root)
    staging = layout.directory("tmp") / f"blind-{review_id}-{secrets.token_hex(8)}"
    staging.mkdir(mode=0o700)
    try:
        public = staging / "public"
        private = staging / "private"
        public.mkdir(mode=0o700)
        private.mkdir(mode=0o700)
        key = bytes.fromhex(key_hex) if key_hex is not None else secrets.token_bytes(32)
        if len(key) != 32:
            raise ValueError("blind-review key must contain exactly 32 bytes")

        staged: list[tuple[str, _ResolvedBlindInput]] = [
            (_blind_id(key, item), item) for item in resolved
        ]
        if len({blind_id for blind_id, _ in staged}) != len(staged):
            raise ContractError("blind identifier collision")
        # HMAC-derived ordering is reproducible and reveals no candidate label without the key.
        staged.sort(key=lambda pair: hmac.new(key, pair[0].encode(), hashlib.sha256).digest())

        samples: list[ReviewSample] = []
        reveals: list[RevealEntry] = []
        for order, (blind_id, item) in enumerate(staged):
            relative_audio = f"audio/{blind_id}.wav"
            _copy_verified(item.audio_path, public / relative_audio, item.output_sha256)
            samples.append(
                ReviewSample(
                    blind_id=blind_id,
                    track=item.track,
                    prompt_id=item.prompt_id,
                    prompt_text=item.prompt_text,
                    audio_path=relative_audio,
                    audio_sha256=item.output_sha256,
                    order=order,
                )
            )
            reveals.append(
                RevealEntry(
                    blind_id=blind_id,
                    experiment_id=item.binding.experiment_id,
                    trial_id=item.binding.trial_id,
                    candidate_id=item.candidate_id,
                    track=item.track,
                    prompt_id=item.prompt_id,
                    output_sha256=item.output_sha256,
                    seed=item.seed,
                    timing_mode=item.timing_mode,
                    reference_sha256=item.reference_sha256,
                    source_audio_sha256=item.source_audio_sha256,
                    request_sha256=item.request_sha256,
                    plan_sha256=item.binding.plan_sha256,
                    run_manifest_sha256=item.binding.run_manifest_sha256,
                    result_sha256=item.binding.result_sha256,
                )
            )

        index = ReviewIndex(
            review_id=review_id,
            created_at=utc_now(),
            key_commitment_sha256=hashlib.sha256(key).hexdigest(),
            samples=tuple(samples),
        )
        reveal = RevealMap(review_id=review_id, key_hex=key.hex(), entries=tuple(reveals))
        index_sha256 = write_canonical_json(public / "review-index.json", index)
        reveal_sha256 = write_canonical_json(private / "reveal.json", reveal)
        html = resource_path("blind-review.html")
        with (
            open_regular_no_follow(html) as html_stream,
            (public / "index.html").open("xb") as target,
        ):
            shutil.copyfileobj(html_stream, target)
        os.chmod(public / "index.html", 0o600)

        public_files: list[PackageFile] = []
        for candidate in sorted(public.rglob("*")):
            if candidate.is_dir():
                if candidate.is_symlink():
                    raise SecurityError("public blind package cannot contain symlink directories")
                continue
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SecurityError("public blind package can contain only regular files")
            digest, size = sha256_file(candidate)
            public_files.append(
                PackageFile(
                    path=candidate.relative_to(public).as_posix(),
                    sha256=digest,
                    size_bytes=size,
                )
            )
        public_manifest = PublicReviewManifest(
            review_id=review_id,
            created_at=index.created_at,
            review_index_sha256=index_sha256,
            files=tuple(public_files),
        )
        public_manifest_sha256 = write_canonical_json(
            public / "review-manifest.json", public_manifest
        )

        files: list[PackageFile] = []
        for candidate in sorted(staging.rglob("*")):
            if candidate.is_dir():
                if candidate.is_symlink():
                    raise SecurityError("blind package cannot contain symlink directories")
                continue
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SecurityError("blind package can contain only regular files")
            digest, size = sha256_file(candidate)
            files.append(
                PackageFile(
                    path=candidate.relative_to(staging).as_posix(),
                    sha256=digest,
                    size_bytes=size,
                )
            )
        manifest = ReviewPackageManifest(
            review_id=review_id,
            created_at=index.created_at,
            review_index_sha256=index_sha256,
            public_manifest_sha256=public_manifest_sha256,
            reveal_sha256=reveal_sha256,
            files=tuple(files),
        )
        write_canonical_json(staging / "package-manifest.json", manifest)
        for candidate in staging.rglob("*"):
            if candidate.is_file():
                candidate.chmod(0o400)
        os.replace(staging, output_root)
        return verify_review_package(layout, review_id=review_id)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _read_canonical_model[ModelT: BlindModel](
    path: Path, model: type[ModelT]
) -> tuple[ModelT, str]:
    with open_regular_no_follow(path) as stream:
        payload = stream.read(16 * 1024 * 1024 + 1)
    if len(payload) > 16 * 1024 * 1024:
        raise ContractError(f"blind evidence exceeds the size limit: {path}")
    parsed = model.model_validate_json(payload)
    if payload != canonical_json_bytes(parsed) + b"\n":
        raise ContractError(f"blind evidence is not canonical JSON: {path}")
    return parsed, hashlib.sha256(payload).hexdigest()


def verify_review_package(layout: AssetLayout, *, review_id: str) -> VerifiedReviewPackage:
    """Re-hash the complete sealed package before any score aggregation."""

    AssetLayout.validate(layout.root)
    review_id = _STABLE_ID.validate_python(review_id, strict=True)
    relative_root = f"evaluations/blind-reviews/{review_id}"
    root = confined_path(layout.root, relative_root, must_exist=True)
    if root.is_symlink() or not root.is_dir():
        raise SecurityError("blind-review package root is unsafe")
    manifest_path = root / "package-manifest.json"
    manifest, manifest_digest = _read_canonical_model(manifest_path, ReviewPackageManifest)
    if manifest.review_id != review_id:
        raise ContractError("blind-package manifest review ID differs from its path")
    expected = {entry.path for entry in manifest.files} | {"package-manifest.json"}
    actual: set[str] = set()
    for candidate in root.rglob("*"):
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(f"blind package contains a symlink: {candidate}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SecurityError(f"blind package contains a non-file: {candidate}")
        if metadata.st_nlink != 1:
            raise SecurityError(f"blind package contains a hard-linked file: {candidate}")
        if stat.S_IMODE(metadata.st_mode) & 0o222:
            raise SecurityError(f"finalized blind evidence remains writable: {candidate}")
        actual.add(candidate.relative_to(root).as_posix())
    if actual != expected:
        raise ContractError(
            f"blind-package file set differs; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    by_path = {entry.path: entry for entry in manifest.files}
    for relative, entry in by_path.items():
        path = confined_path(root, relative, must_exist=True)
        digest, size = sha256_file(path)
        if digest != entry.sha256 or size != entry.size_bytes:
            raise SecurityError(f"blind-package file digest changed: {relative}")
    index, index_digest = _read_canonical_model(root / "public" / "review-index.json", ReviewIndex)
    public_manifest, public_manifest_digest = _read_canonical_model(
        root / "public" / "review-manifest.json", PublicReviewManifest
    )
    reveal, reveal_digest = _read_canonical_model(root / "private" / "reveal.json", RevealMap)
    if (
        index.review_id != review_id
        or public_manifest.review_id != review_id
        or reveal.review_id != review_id
    ):
        raise ContractError("blind-package component review IDs differ")
    if (
        index_digest != manifest.review_index_sha256
        or public_manifest_digest != manifest.public_manifest_sha256
        or reveal_digest != manifest.reveal_sha256
        or public_manifest.review_index_sha256 != index_digest
    ):
        raise SecurityError("blind-package component commitment differs from its manifest")
    expected_public = {
        relative.removeprefix("public/")
        for relative in by_path
        if relative.startswith("public/") and relative != "public/review-manifest.json"
    }
    public_by_path = {entry.path: entry for entry in public_manifest.files}
    if set(public_by_path) != expected_public:
        raise ContractError("public blind-package manifest has an incomplete file set")
    for relative, entry in public_by_path.items():
        full_entry = by_path[f"public/{relative}"]
        if entry != full_entry.model_copy(update={"path": relative}):
            raise SecurityError("public file commitment differs from the sealed package")
    for sample in index.samples:
        sample_file = by_path.get(f"public/{sample.audio_path}")
        if sample_file is None or sample_file.sha256 != sample.audio_sha256:
            raise SecurityError("public sample audio does not match its committed bytes")

    resolved: list[_ResolvedBlindInput] = []
    samples_by_id = {sample.blind_id: sample for sample in index.samples}
    for reveal_entry in reveal.entries:
        item = _resolve_blind_input(
            layout,
            BlindInput(
                experiment_id=reveal_entry.experiment_id,
                trial_id=reveal_entry.trial_id,
                plan_sha256=reveal_entry.plan_sha256,
                run_manifest_sha256=reveal_entry.run_manifest_sha256,
                result_sha256=reveal_entry.result_sha256,
            ),
        )
        source_sample = samples_by_id.get(reveal_entry.blind_id)
        if source_sample is None:
            raise ContractError("reveal names a blind ID absent from the public index")
        if (
            item.candidate_id != reveal_entry.candidate_id
            or item.track != reveal_entry.track
            or item.prompt_id != reveal_entry.prompt_id
            or item.output_sha256 != reveal_entry.output_sha256
            or item.seed != reveal_entry.seed
            or item.timing_mode != reveal_entry.timing_mode
            or item.reference_sha256 != reveal_entry.reference_sha256
            or item.source_audio_sha256 != reveal_entry.source_audio_sha256
            or item.request_sha256 != reveal_entry.request_sha256
            or source_sample.audio_sha256 != reveal_entry.output_sha256
        ):
            raise ContractError("reveal attribution differs from immutable audition evidence")
        resolved.append(item)
    _validate_complete_fair_matrix(layout, tuple(resolved))
    return VerifiedReviewPackage(
        root=root,
        index=index,
        reveal=reveal,
        public_manifest=public_manifest,
        manifest=manifest,
        review_index_sha256=index_digest,
        public_manifest_sha256=public_manifest_digest,
        package_manifest_sha256=manifest_digest,
    )


def aggregate_scores(
    layout: AssetLayout,
    sheet: ScoreSheet,
) -> dict[str, object]:
    package = verify_review_package(layout, review_id=sheet.review_id)
    index = package.index
    reveal = package.reveal
    if not (index.review_id == reveal.review_id == sheet.review_id):
        raise ContractError("review, reveal, and score-sheet IDs differ")
    if sheet.review_index_sha256 != package.review_index_sha256:
        raise ContractError("score sheet does not bind the exact review index")
    if sheet.public_manifest_sha256 != package.public_manifest_sha256:
        raise ContractError("score sheet does not bind the exact public review package")
    key = bytes.fromhex(reveal.key_hex)
    if not hmac.compare_digest(hashlib.sha256(key).hexdigest(), index.key_commitment_sha256):
        raise ContractError("reveal key does not match the public key commitment")
    expected = {sample.blind_id for sample in index.samples}
    reveal_by_id = {entry.blind_id: entry for entry in reveal.entries}
    score_by_id = {score.blind_id: score for score in sheet.scores}
    if len(reveal_by_id) != len(reveal.entries):
        raise ContractError("reveal contains duplicate blind IDs")
    if len(score_by_id) != len(sheet.scores):
        raise ContractError("score sheet contains duplicate blind IDs")
    if set(reveal_by_id) != expected or set(score_by_id) != expected:
        raise ContractError("reveal and score sheets must cover every blinded sample exactly")

    samples_by_id = {sample.blind_id: sample for sample in index.samples}
    for blind_id, entry in reveal_by_id.items():
        sample = samples_by_id[blind_id]
        if entry.track != sample.track or entry.prompt_id != sample.prompt_id:
            raise ContractError("reveal entry disagrees with the public sample binding")
        recomputed = _blind_id_parts(
            key,
            trial_id=entry.trial_id,
            candidate_id=entry.candidate_id,
            prompt_id=entry.prompt_id,
            output_sha256=entry.output_sha256,
            seed=entry.seed,
            request_sha256=entry.request_sha256,
            plan_sha256=entry.plan_sha256,
            run_manifest_sha256=entry.run_manifest_sha256,
            result_sha256=entry.result_sha256,
        )
        if not hmac.compare_digest(recomputed, blind_id):
            raise ContractError("reveal entry does not reproduce its blinded identifier")

    dimensions = index.dimensions
    totals: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    tracks: dict[str, str] = {}
    for blind_id in expected:
        entry = reveal_by_id[blind_id]
        score = score_by_id[blind_id]
        existing_track = tracks.setdefault(entry.candidate_id, entry.track)
        if existing_track != entry.track:
            raise ContractError("a candidate cannot appear in more than one evaluation track")
        for dimension in dimensions:
            totals[entry.candidate_id][dimension].append(int(getattr(score, dimension)))

    candidates: list[dict[str, object]] = []
    for candidate_id, values in sorted(totals.items()):
        means = {
            dimension: sum(scores) / len(scores) for dimension, scores in sorted(values.items())
        }
        candidates.append(
            {
                "candidate_id": candidate_id,
                "track": tracks[candidate_id],
                "sample_count": len(next(iter(values.values()))),
                "means": means,
                "overall_mean": sum(means.values()) / len(means),
            }
        )
    candidates_by_track: dict[str, list[dict[str, object]]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_track[str(candidate["track"])].append(candidate)
    for track_candidates in candidates_by_track.values():
        track_candidates.sort(
            key=lambda candidate: float(cast(float, candidate["overall_mean"])), reverse=True
        )
    return {
        "schema_version": "cole-voice-lab.listening-summary/v1",
        "review_id": index.review_id,
        "tracks": {
            track: {"candidates": track_candidates}
            for track, track_candidates in sorted(candidates_by_track.items())
        },
    }

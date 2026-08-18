"""One-way, evidence-bound promotion into ChloeSpeech's public contracts.

This module deliberately owns a lab-side contract.  It does not import
ChloeSpeech, copy biometric assets, or mutate a consumer.  A successful call
only proves that the private asset root still contains the exact evidence named
by a promotion manifest and returns a JSON-ready handoff specification.
"""

from __future__ import annotations

import hashlib
import math
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from .audio import inspect_pcm_wav
from .audition import (
    AuditionPlan,
    AuditionRunManifest,
    AuditionTrial,
    PersistedTrialResult,
    audition_paths,
    validate_audition_plan_paths,
    validate_persisted_trial_evidence,
)
from .blind import (
    DIMENSIONS,
    RevealEntry,
    ReviewPackageManifest,
    ScoreSheet,
    aggregate_scores,
    verify_review_package,
)
from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    sha256_file,
)
from .errors import ContractError, SecurityError
from .models import (
    CandidateId,
    ConsentRecord,
    ContractModel,
    ExecutedCodeEvidence,
    ModelLockEntry,
    Sha256,
    StableId,
    WorkerModelEvidence,
)
from .paths import AssetLayout, confined_path, validate_relative_path
from .runtime import SOURCE_MANIFEST_NAME, LocalSourceManifest

PROMOTION_SCHEMA_VERSION: Literal["cole-voice-lab.promotion/v2"] = "cole-voice-lab.promotion/v2"
HANDOFF_SCHEMA_VERSION: Literal["cole-voice-lab.chloespeech-handoff/v1"] = (
    "cole-voice-lab.chloespeech-handoff/v1"
)
PROMOTION_PURPOSE: Literal["private-chloe-speech-rendering"] = "private-chloe-speech-rendering"
_MAX_JSON_EVIDENCE_BYTES = 16 * 1024 * 1024
_CHATTERBOX_REPOSITORIES = {
    "turbo": "ResembleAI/chatterbox-turbo",
    "v3": "ResembleAI/chatterbox",
    "original": "ResembleAI/chatterbox",
}
_PROVIDER_PROJECTION_ID = "cole-voice-lab.worker-provider-projection/v1"
_OPERATIONAL_REQUEST_SETTINGS = frozenset(
    {"profile", "timing_mode", "timing_semantics", "prompt_id"}
)
_PROVIDER_PROJECTIONS: dict[str, tuple[str, str]] = {
    "qwen3-tts-base": ("mlx-audio", "mlx-audio"),
    "chatterbox-turbo": ("mlx-audio", "mlx-audio"),
    "chatterbox-v3": ("mlx-audio", "mlx-audio"),
    "f5-tts": ("f5-tts", "f5-tts"),
}

type ProviderId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=64),
]
type NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type LanguageTag = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$", max_length=64),
]


class PromotionValidationError(ContractError):
    """Promotion evidence is stale, inconsistent, unsafe, or incomplete."""


def _portable_path(value: str) -> str:
    return validate_relative_path(value)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require an explicit timezone")
    return value.astimezone(UTC)


class AssetFileBinding(ContractModel):
    """An asset-root-relative, byte-for-byte file binding."""

    path: str = Field(min_length=1, max_length=4_096)
    sha256: Sha256
    size_bytes: int = Field(gt=0, le=8 * 1024 * 1024 * 1024)
    media_type: str = Field(min_length=3, max_length=200)

    @field_validator("path")
    @classmethod
    def _path_is_portable(cls, value: str) -> str:
        return _portable_path(value)


class JsonEvidenceFile(ContractModel):
    path: str = Field(min_length=1, max_length=4_096)
    sha256: Sha256
    size_bytes: int = Field(gt=0, le=_MAX_JSON_EVIDENCE_BYTES)
    media_type: Literal["application/json"] = "application/json"

    @field_validator("path")
    @classmethod
    def _path_is_portable(cls, value: str) -> str:
        return _portable_path(value)


class HumanReferenceBinding(ContractModel):
    path: str = Field(min_length=1, max_length=4_096)
    sha256: Sha256
    size_bytes: int = Field(gt=0, le=8 * 1024 * 1024 * 1024)
    media_type: Literal["audio/wav"] = "audio/wav"
    human_subject: Literal[True] = True
    subject_id: StableId

    @field_validator("path")
    @classmethod
    def _path_is_portable(cls, value: str) -> str:
        return _portable_path(value)


class HumanReferenceProvenanceRecord(ContractModel):
    """Strict contents expected in the separately stored provenance document."""

    schema_version: Literal["cole-voice-lab.human-reference-provenance/v1"] = (
        "cole-voice-lab.human-reference-provenance/v1"
    )
    provenance_id: StableId
    subject_id: StableId
    human_subject: Literal[True] = True
    recognizable_human: Literal[True] = True
    purpose: Literal["private-chloe-speech-rendering"] = PROMOTION_PURPOSE
    reference_path: str = Field(min_length=1, max_length=4_096)
    reference_sha256: Sha256
    consent_id: StableId
    consent_evidence_sha256: Sha256
    created_at: datetime
    created_by: NonBlank = Field(max_length=256)
    statement: NonBlank = Field(max_length=4_096)

    @field_validator("reference_path")
    @classmethod
    def _path_is_portable(cls, value: str) -> str:
        return _portable_path(value)

    @field_validator("created_at")
    @classmethod
    def _timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class WinningRunRecord(ContractModel):
    """Identity and output evidence for the exact run selected for promotion."""

    schema_version: Literal["cole-voice-lab.promotion-run/v2"] = "cole-voice-lab.promotion-run/v2"
    experiment_id: StableId
    trial_id: StableId
    candidate_id: CandidateId
    track: Literal["direct-tts"] = "direct-tts"
    status: Literal["complete"] = "complete"
    plan_sha256: Sha256
    run_manifest_sha256: Sha256
    result_sha256: Sha256
    request_sha256: Sha256
    provider_projection_id: Literal["cole-voice-lab.worker-provider-projection/v1"] = (
        "cole-voice-lab.worker-provider-projection/v1"
    )
    provider_id: ProviderId
    provider_voice: NonBlank = Field(max_length=512)
    model_id: NonBlank = Field(max_length=500)
    model_revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
    code_revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
    reference_sha256: Sha256
    consent_evidence_sha256: Sha256
    language: LanguageTag
    audition_settings_sha256: Sha256
    provider_settings_sha256: Sha256
    environment_fingerprint_sha256: Sha256
    output: AssetFileBinding
    watermarked: bool
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def _timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class LabVerdictRecord(ContractModel):
    """The blind-review decision that names the sole promotable run."""

    schema_version: Literal["cole-voice-lab.promotion-verdict/v2"] = (
        "cole-voice-lab.promotion-verdict/v2"
    )
    verdict_id: StableId
    experiment_id: StableId
    decision: Literal["promote"] = "promote"
    approved: Literal[True] = True
    selection_policy: Literal["highest-direct-tts-overall-mean-no-ties"] = (
        "highest-direct-tts-overall-mean-no-ties"
    )
    trial_policy: Literal["explicit-reviewed-winning-candidate-cell"] = (
        "explicit-reviewed-winning-candidate-cell"
    )
    review_id: StableId
    winning_candidate_id: CandidateId
    winning_trial_id: StableId
    reference_sha256: Sha256
    winning_run_evidence_sha256: Sha256
    model_lock_evidence_sha256: Sha256
    model_source_manifest_sha256: Sha256
    auxiliary_source_manifest_sha256: dict[str, Sha256]
    worker_model_evidence_sha256: Sha256
    blind_package_manifest_sha256: Sha256
    score_sheet_evidence_sha256: Sha256
    listening_summary_evidence_sha256: Sha256
    winning_reveal_entry: RevealEntry
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def _timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class ListeningDimensionMeans(ContractModel):
    """The complete fixed listening rubric for one candidate."""

    likeness: float = Field(ge=1.0, le=5.0, allow_inf_nan=False)
    naturalness: float = Field(ge=1.0, le=5.0, allow_inf_nan=False)
    prosody: float = Field(ge=1.0, le=5.0, allow_inf_nan=False)
    intelligibility: float = Field(ge=1.0, le=5.0, allow_inf_nan=False)
    artifact_freedom: float = Field(ge=1.0, le=5.0, allow_inf_nan=False)


class ListeningCandidateSummary(ContractModel):
    candidate_id: CandidateId
    track: Literal["direct-tts", "voice-conversion"]
    sample_count: int = Field(gt=0)
    means: ListeningDimensionMeans
    overall_mean: float = Field(ge=1.0, le=5.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _overall_is_derived(self) -> ListeningCandidateSummary:
        values = tuple(float(getattr(self.means, dimension)) for dimension in DIMENSIONS)
        if not math.isclose(self.overall_mean, sum(values) / len(values), abs_tol=1e-12):
            raise ValueError("candidate overall mean must be derived from the fixed rubric")
        return self


class ListeningTrackSummary(ContractModel):
    candidates: tuple[ListeningCandidateSummary, ...] = Field(min_length=1)

    @field_validator("candidates", mode="before")
    @classmethod
    def _canonicalize_aggregate_list(cls, value: object) -> object:
        # ``aggregate_scores`` deliberately emits JSON-native lists.  Store the
        # validated boundary immutably after accepting that one exact shape.
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _ordered_unique_track(self) -> ListeningTrackSummary:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("listening-summary candidates must be unique")
        tracks = {candidate.track for candidate in self.candidates}
        if len(tracks) != 1:
            raise ValueError("one listening-summary track cannot mix evaluation tracks")
        scores = [candidate.overall_mean for candidate in self.candidates]
        if scores != sorted(scores, reverse=True):
            raise ValueError("listening-summary candidates must be score ordered")
        return self


class ListeningSummaryAggregate(ContractModel):
    schema_version: Literal["cole-voice-lab.listening-summary/v1"] = (
        "cole-voice-lab.listening-summary/v1"
    )
    review_id: StableId
    tracks: dict[Literal["direct-tts", "voice-conversion"], ListeningTrackSummary] = Field(
        min_length=1
    )

    @field_validator("tracks")
    @classmethod
    def _track_keys_match_entries(
        cls,
        value: dict[Literal["direct-tts", "voice-conversion"], ListeningTrackSummary],
    ) -> dict[Literal["direct-tts", "voice-conversion"], ListeningTrackSummary]:
        for track, summary in value.items():
            if any(candidate.track != track for candidate in summary.candidates):
                raise ValueError("listening-summary track key and candidates disagree")
        return value


class PromotionListeningSummaryRecord(ContractModel):
    """Digest-bound aggregate whose inputs are named explicitly."""

    schema_version: Literal["cole-voice-lab.promotion-listening-summary/v1"] = (
        "cole-voice-lab.promotion-listening-summary/v1"
    )
    review_id: StableId
    blind_package_manifest_sha256: Sha256
    score_sheet_sha256: Sha256
    aggregate: ListeningSummaryAggregate

    @model_validator(mode="after")
    def _review_is_consistent(self) -> PromotionListeningSummaryRecord:
        if self.aggregate.review_id != self.review_id:
            raise ValueError("promotion listening summary review IDs must match")
        return self


class BoundConsentEvidence(ContractModel):
    file: JsonEvidenceFile
    record: ConsentRecord


class BoundProvenanceEvidence(ContractModel):
    file: JsonEvidenceFile
    record: HumanReferenceProvenanceRecord


class BoundSourceManifestEvidence(ContractModel):
    file: JsonEvidenceFile
    record: LocalSourceManifest

    @model_validator(mode="after")
    def _canonical_source_path(self) -> BoundSourceManifestEvidence:
        expected = f"models/sources/{self.record.source_id}/{SOURCE_MANIFEST_NAME}"
        if self.file.path != expected:
            raise ValueError("source manifest must use its canonical source-ID path")
        if self.record.kind != "huggingface" or not self.record.files:
            raise ValueError("promotion requires a nonempty Hugging Face source manifest")
        return self


class BoundModelLockEvidence(ContractModel):
    file: JsonEvidenceFile
    record: ModelLockEntry
    model_source: BoundSourceManifestEvidence
    auxiliary_sources: tuple[BoundSourceManifestEvidence, ...] = ()
    worker_model_evidence: JsonEvidenceFile
    worker_model_record: WorkerModelEvidence

    @model_validator(mode="after")
    def _complete_source_set(self) -> BoundModelLockEvidence:
        sources = (self.model_source, *self.auxiliary_sources)
        repositories = [source.record.repository for source in sources]
        source_ids = [source.record.source_id for source in sources]
        if len(repositories) != len(set(repositories)) or len(source_ids) != len(set(source_ids)):
            raise ValueError("promotion model sources must be unique")
        worker = self.worker_model_record
        if self.model_source.file.sha256 != worker.model_manifest_sha256:
            raise ValueError("model source manifest must match immutable worker evidence")
        expected_auxiliary = {
            source.record.repository: source.file.sha256 for source in self.auxiliary_sources
        }
        if expected_auxiliary != worker.auxiliary_manifest_sha256:
            raise ValueError("auxiliary source manifests must match immutable worker evidence")
        if (
            self.record.candidate_id != worker.candidate_id
            or self.record.model_id != worker.model_id
            or self.record.model_revision != worker.model_revision
            or self.record.code_revision != worker.code_revision
            or self.model_source.record.repository != worker.model_id
            or self.model_source.record.revision != worker.model_revision
        ):
            raise ValueError("model lock identity must match worker and source evidence")
        expected_files: dict[str, str] = {}
        for source in sources:
            source_root = Path(source.file.path).parent
            for relative, digest in source.record.files.items():
                path = (source_root / relative).as_posix()
                if path in expected_files:
                    raise ValueError("model source artifact paths must be unique")
                expected_files[path] = digest
        if self.record.files != expected_files:
            raise ValueError("model lock files must exactly equal all source-manifest artifacts")
        return self


class BoundWinningRunEvidence(ContractModel):
    file: JsonEvidenceFile
    record: WinningRunRecord


class BoundBlindPackageEvidence(ContractModel):
    file: JsonEvidenceFile
    record: ReviewPackageManifest


class BoundScoreSheetEvidence(ContractModel):
    file: JsonEvidenceFile
    record: ScoreSheet


class BoundListeningSummaryEvidence(ContractModel):
    file: JsonEvidenceFile
    record: PromotionListeningSummaryRecord


class BoundVerdictEvidence(ContractModel):
    file: JsonEvidenceFile
    record: LabVerdictRecord


class PromotionDisclosure(ContractModel):
    spoken: NonBlank = Field(max_length=4_096)
    persistent: NonBlank = Field(max_length=4_096)
    cue_id: StableId = "synthetic-voice-disclosure"
    speaker_id: StableId = "narrator"
    pause_after_ms: int = Field(default=500, ge=0, le=60_000)


class PromotionLicense(ContractModel):
    name: NonBlank = Field(max_length=256)
    spdx_id: str | None = Field(default=None, min_length=1, max_length=128)
    uri: str | None = Field(default=None, min_length=1, max_length=2_048)


class PromotionManifest(ContractModel):
    """Lab authority for promoting exactly one winning human-reference voice."""

    schema_version: Literal["cole-voice-lab.promotion/v2"] = PROMOTION_SCHEMA_VERSION
    promotion_id: StableId
    created_at: datetime
    subject_id: StableId
    purpose: Literal["private-chloe-speech-rendering"] = PROMOTION_PURPOSE
    experiment_id: StableId
    winning_trial_id: StableId
    candidate_id: CandidateId
    voice_id: StableId
    voice_name: NonBlank = Field(max_length=256)
    provider_id: ProviderId
    provider_voice: NonBlank = Field(max_length=512)
    language: LanguageTag
    identity_kind: Literal["human_authorized"] = "human_authorized"
    recognizable_human: Literal[True] = True
    reference: HumanReferenceBinding
    consent: BoundConsentEvidence
    provenance: BoundProvenanceEvidence
    model_lock: BoundModelLockEvidence
    winning_run: BoundWinningRunEvidence
    blind_package: BoundBlindPackageEvidence
    score_sheet: BoundScoreSheetEvidence
    listening_summary: BoundListeningSummaryEvidence
    verdict: BoundVerdictEvidence
    license: PromotionLicense
    disclosure: PromotionDisclosure
    clearance_scope: NonBlank = Field(max_length=4_096)
    provider_settings: dict[str, JsonValue] = Field(default_factory=dict)
    watermarked: bool

    @field_validator("created_at")
    @classmethod
    def _timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _evidence_chain_is_consistent(self) -> PromotionManifest:
        consent = self.consent.record
        provenance = self.provenance.record
        lock = self.model_lock.record
        run = self.winning_run.record
        verdict = self.verdict.record

        subjects = {
            self.subject_id,
            self.reference.subject_id,
            consent.subject_id,
            provenance.subject_id,
        }
        if len(subjects) != 1:
            raise ValueError("promotion, reference, consent, and provenance subjects must match")
        if self.provider_id == "chatterbox":
            expected_voice = f"reference-sha256:{self.reference.sha256}"
            if self.provider_voice != expected_voice:
                raise ValueError("Chatterbox provider_voice must be bound to the reference SHA-256")
        if not consent.approved or PROMOTION_PURPOSE not in consent.allowed_purposes:
            raise ValueError("consent must approve private ChloeSpeech rendering")
        consent_timestamps = (consent.effective_at, consent.expires_at, consent.revoked_at)
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in consent_timestamps
        ):
            raise ValueError("consent timestamps require explicit timezones")
        if "tailnet-local-host" not in consent.allowed_execution_boundaries:
            raise ValueError("consent must allow the ChloeSpeech execution boundary")
        if provenance.reference_path != self.reference.path:
            raise ValueError("provenance and promotion reference paths must match")
        if provenance.reference_sha256 != self.reference.sha256:
            raise ValueError("provenance and promotion reference digests must match")
        if provenance.consent_id != consent.consent_id:
            raise ValueError("provenance and consent identifiers must match")
        if provenance.consent_evidence_sha256 != self.consent.file.sha256:
            raise ValueError("provenance must bind the exact consent evidence file")

        if lock.candidate_id != self.candidate_id:
            raise ValueError("model lock candidate does not match the promoted candidate")
        if not lock.files:
            raise ValueError("promotion requires checksum-bound model files")
        for model_path in lock.files:
            _portable_path(model_path)

        common_identity = (
            self.experiment_id,
            self.winning_trial_id,
            self.candidate_id,
            self.reference.sha256,
        )
        run_identity = (
            run.experiment_id,
            run.trial_id,
            run.candidate_id,
            run.reference_sha256,
        )
        if run_identity != common_identity:
            raise ValueError("winning run identity does not match the promotion")
        verdict_identity = (
            verdict.experiment_id,
            verdict.winning_trial_id,
            verdict.winning_candidate_id,
            verdict.reference_sha256,
        )
        if verdict_identity != common_identity:
            raise ValueError("verdict identity does not match the promotion")
        if verdict.winning_run_evidence_sha256 != self.winning_run.file.sha256:
            raise ValueError("verdict must bind the exact winning-run evidence")
        if verdict.model_lock_evidence_sha256 != self.model_lock.file.sha256:
            raise ValueError("verdict must bind the exact model-lock evidence")
        expected_auxiliary_manifests = {
            source.record.repository: source.file.sha256
            for source in self.model_lock.auxiliary_sources
        }
        if (
            verdict.model_source_manifest_sha256 != self.model_lock.model_source.file.sha256
            or verdict.auxiliary_source_manifest_sha256 != expected_auxiliary_manifests
            or verdict.worker_model_evidence_sha256 != self.model_lock.worker_model_evidence.sha256
        ):
            raise ValueError("verdict must bind the exact worker and model-source evidence")

        package = self.blind_package.record
        sheet = self.score_sheet.record
        summary = self.listening_summary.record
        expected_package_path = (
            f"evaluations/blind-reviews/{package.review_id}/package-manifest.json"
        )
        if self.blind_package.file.path != expected_package_path:
            raise ValueError("blind-package evidence must use its canonical package path")
        if not (verdict.review_id == package.review_id == sheet.review_id == summary.review_id):
            raise ValueError("promotion blind-review IDs must match")
        if (
            sheet.review_index_sha256 != package.review_index_sha256
            or sheet.public_manifest_sha256 != package.public_manifest_sha256
        ):
            raise ValueError("score sheet must bind the exact blind package")
        blind_file_bindings = (
            (
                verdict.blind_package_manifest_sha256,
                self.blind_package.file.sha256,
                summary.blind_package_manifest_sha256,
            ),
            (
                verdict.score_sheet_evidence_sha256,
                self.score_sheet.file.sha256,
                summary.score_sheet_sha256,
            ),
        )
        if any(len(set(bindings)) != 1 for bindings in blind_file_bindings):
            raise ValueError("verdict and summary must bind the exact blind-review evidence")
        if verdict.listening_summary_evidence_sha256 != self.listening_summary.file.sha256:
            raise ValueError("verdict must bind the exact listening summary")

        reveal = verdict.winning_reveal_entry
        reveal_identity = (
            reveal.experiment_id,
            reveal.trial_id,
            reveal.candidate_id,
            reveal.reference_sha256,
        )
        if reveal_identity != common_identity or reveal.track != "direct-tts":
            raise ValueError("winning reveal entry must identify the promoted direct-TTS trial")
        run_bindings = (
            (run.plan_sha256, reveal.plan_sha256),
            (run.run_manifest_sha256, reveal.run_manifest_sha256),
            (run.result_sha256, reveal.result_sha256),
            (run.request_sha256, reveal.request_sha256),
            (run.output.sha256, reveal.output_sha256),
        )
        if any(left != right for left, right in run_bindings):
            raise ValueError("winning run must bind the exact revealed audition evidence")

        direct = summary.aggregate.tracks.get("direct-tts")
        if direct is None or len(direct.candidates) < 2:
            raise ValueError("promotion requires a competitive direct-TTS listening summary")
        best_score = max(candidate.overall_mean for candidate in direct.candidates)
        winners = [
            candidate for candidate in direct.candidates if candidate.overall_mean == best_score
        ]
        if len(winners) != 1:
            raise ValueError("tied direct-TTS listening results fail closed")
        if winners[0].candidate_id != self.candidate_id:
            raise ValueError("promoted candidate is not the direct-TTS listening winner")

        realized = (
            run.provider_id,
            run.provider_voice,
            run.model_id,
            run.model_revision,
            run.code_revision,
            run.language,
        )
        promoted = (
            self.provider_id,
            self.provider_voice,
            lock.model_id,
            lock.model_revision,
            lock.code_revision,
            self.language,
        )
        if realized != promoted:
            raise ValueError("winning-run provider/model identity does not match the promotion")
        if run.provider_settings_sha256 != canonical_sha256(self.provider_settings):
            raise ValueError("winning-run provider settings do not match the promotion")
        if run.consent_evidence_sha256 != self.consent.file.sha256:
            raise ValueError("winning run must bind the promoted consent evidence")
        if run.watermarked != self.watermarked:
            raise ValueError("declared and realized watermark state must agree")

        if consent.effective_at is None or consent.effective_at > run.completed_at:
            raise ValueError("consent must be effective when the winning run completes")
        if consent.expires_at is not None and consent.expires_at <= run.completed_at:
            raise ValueError("consent must remain valid through the winning run")
        if consent.effective_at > provenance.created_at:
            raise ValueError("reference provenance cannot predate effective consent")
        if consent.expires_at is not None and consent.expires_at <= self.created_at:
            raise ValueError("consent must remain valid through promotion creation")
        if lock.resolved_at.tzinfo is None or lock.resolved_at.utcoffset() is None:
            raise ValueError("model-lock resolved_at requires an explicit timezone")
        if provenance.created_at > run.completed_at:
            raise ValueError("reference provenance cannot postdate the winning run")
        if lock.resolved_at > run.completed_at:
            raise ValueError("model lock cannot postdate the winning run")
        if (
            run.completed_at > package.created_at
            or package.created_at > sheet.scored_at
            or sheet.scored_at > verdict.decided_at
            or verdict.decided_at > self.created_at
        ):
            raise ValueError(
                "run, package, score, verdict, and promotion timestamps are out of order"
            )

        paths = [
            self.reference.path,
            self.consent.file.path,
            self.provenance.file.path,
            self.model_lock.file.path,
            self.model_lock.model_source.file.path,
            self.model_lock.worker_model_evidence.path,
            *(source.file.path for source in self.model_lock.auxiliary_sources),
            self.winning_run.file.path,
            self.winning_run.record.output.path,
            self.blind_package.file.path,
            self.score_sheet.file.path,
            self.listening_summary.file.path,
            self.verdict.file.path,
            *lock.files,
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("promotion evidence paths must be distinct")

        if self.provider_id == "chatterbox":
            variant = self.provider_settings.get("variant")
            if not isinstance(variant, str) or variant not in _CHATTERBOX_REPOSITORIES:
                raise ValueError("Chatterbox promotion requires an explicit supported variant")
            if lock.model_id != _CHATTERBOX_REPOSITORIES[variant]:
                raise ValueError("Chatterbox model ID does not match the selected variant")
            if not self.watermarked:
                raise ValueError("Chatterbox promotion must retain its realized watermark")
            ineffective = {"exaggeration", "cfg_weight", "cfgWeight"}.intersection(
                self.provider_settings
            )
            if variant != "original" and ineffective:
                raise ValueError(
                    "Chatterbox exaggeration/CFG settings are only realized by the original variant"
                )
        return self


class PromotionValidation(ContractModel):
    schema_version: Literal["cole-voice-lab.promotion-validation/v1"] = (
        "cole-voice-lab.promotion-validation/v1"
    )
    promotion_sha256: Sha256
    evaluated_at: datetime
    verified_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("evaluated_at")
    @classmethod
    def _timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class ChloeSpeechEvidenceExport(ContractModel):
    schema_version: Literal[1] = 1
    kind: StableId
    uri: None = None
    path: str
    sha256: Sha256
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _path_is_portable(cls, value: str) -> str:
        return _portable_path(value)


class ChloeSpeechClearanceExport(ContractModel):
    schema_version: Literal[1] = 1
    status: Literal["approved"] = "approved"
    allowed_purposes: tuple[StableId, ...]
    review_by: date | None = None
    scope: NonBlank = Field(max_length=4_096)
    evidence: tuple[ChloeSpeechEvidenceExport, ...] = Field(min_length=1)


class ChloeSpeechDisclosureExport(ContractModel):
    schema_version: Literal[1] = 1
    spoken: NonBlank = Field(max_length=4_096)
    persistent: NonBlank = Field(max_length=4_096)


class ChloeSpeechProvenanceExport(ContractModel):
    schema_version: Literal[1] = 1
    source_kind: Literal["human_recording"] = "human_recording"
    created_by: NonBlank = Field(max_length=256)
    clearance: ChloeSpeechClearanceExport
    disclosure: ChloeSpeechDisclosureExport


class ChloeSpeechReferenceAudioExport(ContractModel):
    schema_version: Literal[1] = 1
    path: str
    sha256: Sha256
    size_bytes: int = Field(gt=0)
    media_type: Literal["audio/wav"] = "audio/wav"
    human_subject: Literal[True] = True
    subject_id: StableId
    origin_voice_id: None = None
    provenance_path: str
    provenance_sha256: Sha256

    @field_validator("path", "provenance_path")
    @classmethod
    def _paths_are_portable(cls, value: str) -> str:
        return _portable_path(value)


class ChloeSpeechModelExport(ContractModel):
    schema_version: Literal[1] = 1
    id: NonBlank = Field(max_length=512)
    revision: NonBlank = Field(max_length=512)
    artifacts: tuple[()] = ()


class ChloeSpeechSubjectExport(ContractModel):
    schema_version: Literal[1] = 1
    id: StableId
    display_name: None = None


class ChloeSpeechLicenseExport(ContractModel):
    schema_version: Literal[1] = 1
    name: NonBlank = Field(max_length=256)
    spdx_id: str | None = Field(default=None, min_length=1, max_length=128)
    uri: str | None = Field(default=None, min_length=1, max_length=2_048)


class ChloeSpeechDeliveryExport(ContractModel):
    rate: Annotated[float, Field(ge=1.0, le=1.0)] = 1.0
    pitch_semitones: Annotated[float, Field(ge=0.0, le=0.0)] = 0.0
    volume_db: Annotated[float, Field(ge=0.0, le=0.0)] = 0.0
    style: None = None
    style_strength: None = None
    provider_options: dict[str, JsonValue] = Field(default_factory=dict)


class ChloeSpeechVoiceManifestExport(ContractModel):
    schema_version: Literal[1] = 1
    id: StableId
    name: NonBlank = Field(max_length=256)
    provider_id: ProviderId
    provider_voice: NonBlank = Field(max_length=512)
    model: ChloeSpeechModelExport
    language: LanguageTag
    identity_kind: Literal["human_authorized"] = "human_authorized"
    recognizable_human: Literal[True] = True
    subject: ChloeSpeechSubjectExport
    license: ChloeSpeechLicenseExport
    provenance: ChloeSpeechProvenanceExport
    reference_audio: ChloeSpeechReferenceAudioExport
    provider_settings: dict[str, JsonValue] = Field(default_factory=dict)
    defaults: ChloeSpeechDeliveryExport = Field(default_factory=ChloeSpeechDeliveryExport)
    watermarked: bool


class ChloeSpeechDisclosureCueExport(ContractModel):
    """An actual public Cue-shaped segment, not merely a disclosure promise."""

    schema_version: Literal[1] = 1
    id: StableId
    speaker_id: StableId
    text: NonBlank = Field(max_length=1_000_000)
    voice_id: StableId
    delivery: dict[str, JsonValue] = Field(default_factory=dict)
    pause_after_ms: int = Field(ge=0, le=60_000)


class ChloeSpeechEvidenceIndexEntry(ContractModel):
    kind: StableId
    path: str
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def _path_is_portable(cls, value: str) -> str:
        return _portable_path(value)


class ChloeSpeechHandoff(ContractModel):
    """JSON-ready, public-contract-shaped data for an explicit consumer import."""

    schema_version: Literal["cole-voice-lab.chloespeech-handoff/v1"] = HANDOFF_SCHEMA_VERSION
    promotion_id: StableId
    promotion_sha256: Sha256
    purpose: Literal["private-chloe-speech-rendering"] = PROMOTION_PURPOSE
    asset_paths_are_relative_to_caller_root: Literal[True] = True
    copies_assets: Literal[False] = False
    mutates_consumer: Literal[False] = False
    voice_manifest: ChloeSpeechVoiceManifestExport
    required_disclosure_cues: tuple[ChloeSpeechDisclosureCueExport, ...] = Field(min_length=1)
    lab_evidence: tuple[ChloeSpeechEvidenceIndexEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _actual_disclosure_matches_manifest(self) -> ChloeSpeechHandoff:
        spoken = self.voice_manifest.provenance.disclosure.spoken
        if not any(
            cue.text == spoken and cue.voice_id == self.voice_manifest.id
            for cue in self.required_disclosure_cues
        ):
            raise ValueError("handoff requires an actual cue that speaks the declared disclosure")
        return self


def _root(asset_root: Path) -> Path:
    expanded = asset_root.expanduser()
    if expanded.is_symlink():
        raise PromotionValidationError("asset root must not be a symlink")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as error:
        raise PromotionValidationError(f"asset root is unavailable: {error}") from error
    if not resolved.is_dir():
        raise PromotionValidationError("asset root must be a directory")
    try:
        return AssetLayout.validate(resolved).root
    except (OSError, SecurityError) as error:
        raise PromotionValidationError(
            f"asset root is not a private lab layout: {error}"
        ) from error


def _bound_path(root: Path, relative: str) -> Path:
    try:
        return confined_path(root, relative, must_exist=True)
    except (FileNotFoundError, SecurityError, ValueError) as error:
        raise PromotionValidationError(
            f"unsafe or missing promotion evidence {relative!r}: {error}"
        ) from error


def _reject_hardlink(path: Path, relative: str) -> None:
    try:
        links = path.lstat().st_nlink
    except OSError as error:
        raise PromotionValidationError(
            f"cannot inspect promotion evidence links {relative!r}: {error}"
        ) from error
    if links != 1:
        raise PromotionValidationError(f"promotion evidence must not be hardlinked: {relative}")


def _verify_asset(root: Path, binding: AssetFileBinding | HumanReferenceBinding) -> str:
    path = _bound_path(root, binding.path)
    _reject_hardlink(path, binding.path)
    try:
        if binding.media_type == "audio/wav":
            inspection = inspect_pcm_wav(path)
            digest, size = inspection.sha256, inspection.size_bytes
        else:
            digest, size = sha256_file(path)
    except (ContractError, OSError, SecurityError) as error:
        raise PromotionValidationError(
            f"cannot verify promotion asset {binding.path!r}: {error}"
        ) from error
    _reject_hardlink(path, binding.path)
    if size != binding.size_bytes:
        raise PromotionValidationError(f"promotion asset size changed: {binding.path}")
    if digest != binding.sha256:
        raise PromotionValidationError(f"promotion asset checksum changed: {binding.path}")
    return binding.path


def _verify_json_record[RecordT: BaseModel](
    root: Path,
    binding: JsonEvidenceFile,
    expected: RecordT,
    model_type: type[RecordT],
) -> str:
    path = _bound_path(root, binding.path)
    _reject_hardlink(path, binding.path)
    try:
        with open_regular_no_follow(path) as stream:
            payload = stream.read(_MAX_JSON_EVIDENCE_BYTES + 1)
    except (OSError, SecurityError) as error:
        raise PromotionValidationError(
            f"cannot read promotion evidence {binding.path!r}: {error}"
        ) from error
    _reject_hardlink(path, binding.path)
    if len(payload) > _MAX_JSON_EVIDENCE_BYTES:
        raise PromotionValidationError(f"promotion evidence is too large: {binding.path}")
    if len(payload) != binding.size_bytes:
        raise PromotionValidationError(f"promotion evidence size changed: {binding.path}")
    if hashlib.sha256(payload).hexdigest() != binding.sha256:
        raise PromotionValidationError(f"promotion evidence checksum changed: {binding.path}")
    try:
        actual = model_type.model_validate_json(payload)
    except ValueError as error:
        raise PromotionValidationError(
            f"promotion evidence has invalid contents {binding.path!r}: {error}"
        ) from error
    if actual != expected:
        raise PromotionValidationError(
            f"promotion evidence contents do not match the manifest: {binding.path}"
        )
    return binding.path


def _verify_source_snapshot(
    root: Path,
    evidence: BoundSourceManifestEvidence,
) -> tuple[str, ...]:
    source_relative = Path(evidence.file.path).parent.as_posix()
    source_root = _bound_path(root, source_relative)
    if source_root.is_symlink() or not source_root.is_dir():
        raise PromotionValidationError(f"model source root is unsafe: {source_relative}")
    expected = set(evidence.record.files) | {SOURCE_MANIFEST_NAME}
    actual: set[str] = set()
    for candidate in source_root.rglob("*"):
        metadata = candidate.lstat()
        relative = candidate.relative_to(source_root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise PromotionValidationError(f"model source contains a symlink: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PromotionValidationError(
                f"model source must contain only regular files: {relative}"
            )
        if metadata.st_nlink != 1:
            raise PromotionValidationError(f"model source must not be hardlinked: {relative}")
        if relative.startswith(".cache/"):
            continue
        actual.add(relative)
    if actual != expected:
        raise PromotionValidationError(
            "model source file set differs from its manifest; "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )
    verified: list[str] = []
    for relative, expected_sha256 in sorted(evidence.record.files.items()):
        asset_relative = (Path(source_relative) / relative).as_posix()
        path = _bound_path(root, asset_relative)
        _reject_hardlink(path, asset_relative)
        try:
            digest, _ = sha256_file(path)
        except (OSError, SecurityError) as error:
            raise PromotionValidationError(
                f"cannot verify source artifact {asset_relative!r}: {error}"
            ) from error
        _reject_hardlink(path, asset_relative)
        if digest != expected_sha256:
            raise PromotionValidationError(f"source artifact checksum changed: {asset_relative}")
        verified.append(asset_relative)
    return tuple(verified)


def _validate_current_consent(consent: ConsentRecord, *, evaluated_at: datetime) -> None:
    timestamps = (consent.effective_at, consent.expires_at, consent.revoked_at)
    if any(
        value is not None and (value.tzinfo is None or value.utcoffset() is None)
        for value in timestamps
    ):
        raise PromotionValidationError("consent timestamps require explicit timezones")
    if not consent.approved or consent.revoked_at is not None:
        raise PromotionValidationError("consent is not currently approved")
    if consent.effective_at is None or consent.effective_at.astimezone(UTC) > evaluated_at:
        raise PromotionValidationError("consent is not yet effective")
    if consent.expires_at is not None and consent.expires_at.astimezone(UTC) <= evaluated_at:
        raise PromotionValidationError("consent has expired")
    if PROMOTION_PURPOSE not in consent.allowed_purposes:
        raise PromotionValidationError("consent does not allow private ChloeSpeech rendering")
    if "tailnet-local-host" not in consent.allowed_execution_boundaries:
        raise PromotionValidationError("consent does not allow the ChloeSpeech execution boundary")
    if consent.hosted_transfer_allowed:
        raise PromotionValidationError("promotion cannot rely on hosted human-audio transfer")


def _read_audition_contract[RecordT: BaseModel](
    root: Path,
    path: Path,
    model_type: type[RecordT],
    *,
    expected_sha256: str,
    label: str,
) -> RecordT:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as error:
        raise PromotionValidationError(f"{label} escapes the private asset root") from error
    canonical = _bound_path(root, relative)
    _reject_hardlink(canonical, relative)
    try:
        with open_regular_no_follow(canonical) as stream:
            payload = stream.read(_MAX_JSON_EVIDENCE_BYTES + 1)
    except (OSError, SecurityError) as error:
        raise PromotionValidationError(f"cannot read {label}: {error}") from error
    _reject_hardlink(canonical, relative)
    if len(payload) > _MAX_JSON_EVIDENCE_BYTES:
        raise PromotionValidationError(f"{label} exceeds the evidence size limit")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise PromotionValidationError(f"{label} digest differs from blind-review evidence")
    try:
        record = model_type.model_validate_json(payload)
    except ValueError as error:
        raise PromotionValidationError(f"{label} violates its strict contract: {error}") from error
    if payload != canonical_json_bytes(record) + b"\n":
        raise PromotionValidationError(f"{label} must be canonical JSON")
    return record


def _verify_indirect_file(
    layout: AssetLayout,
    path: Path,
    *,
    expected_sha256: str,
    label: str,
) -> str:
    try:
        relative = path.relative_to(layout.root).as_posix()
    except ValueError as error:
        raise PromotionValidationError(f"{label} escapes the private asset root") from error
    canonical = _bound_path(layout.root, relative)
    _reject_hardlink(canonical, relative)
    try:
        digest, _ = sha256_file(canonical)
    except (OSError, SecurityError) as error:
        raise PromotionValidationError(f"cannot verify {label}: {error}") from error
    _reject_hardlink(canonical, relative)
    if digest != expected_sha256:
        raise PromotionValidationError(f"{label} digest differs from audition evidence")
    return relative


def _provider_projection(
    trial: AuditionTrial,
    worker_model: WorkerModelEvidence,
) -> tuple[str, str, dict[str, JsonValue]]:
    try:
        expected_runtime, provider_id = _PROVIDER_PROJECTIONS[trial.candidate_id]
    except KeyError as error:
        raise PromotionValidationError(
            f"candidate has no reviewed ChloeSpeech provider projection: {trial.candidate_id}"
        ) from error
    if worker_model.runtime_package != expected_runtime:
        raise PromotionValidationError(
            "worker runtime does not match the reviewed provider projection"
        )
    provider_voice = f"reference-sha256:{trial.request.reference.sha256}"
    provider_settings = {
        key: value
        for key, value in trial.request.settings.items()
        if key not in _OPERATIONAL_REQUEST_SETTINGS
    }
    return provider_id, provider_voice, provider_settings


def _verify_winning_audition_binding(
    layout: AssetLayout,
    manifest: PromotionManifest,
    reveal: RevealEntry,
) -> tuple[str, ...]:
    """Rebind the promotion record to the exact validated audition cell."""

    paths = audition_paths(layout, reveal.experiment_id)
    plan = _read_audition_contract(
        layout.root,
        paths.plan_path,
        AuditionPlan,
        expected_sha256=reveal.plan_sha256,
        label="winning audition plan",
    )
    try:
        validate_audition_plan_paths(layout, plan)
    except (ContractError, OSError, SecurityError, ValueError) as error:
        raise PromotionValidationError(f"winning audition plan is unsafe: {error}") from error
    run_manifest = _read_audition_contract(
        layout.root,
        paths.run_manifest_path,
        AuditionRunManifest,
        expected_sha256=reveal.run_manifest_sha256,
        label="winning audition run manifest",
    )
    if (
        run_manifest.experiment_id != reveal.experiment_id
        or run_manifest.plan_sha256 != reveal.plan_sha256
        or run_manifest.status != "complete"
    ):
        raise PromotionValidationError("winning audition run is not the bound complete run")
    trials = [trial for trial in plan.trials if trial.trial_id == reveal.trial_id]
    pointers = [pointer for pointer in run_manifest.results if pointer.trial_id == reveal.trial_id]
    if len(trials) != 1 or len(pointers) != 1:
        raise PromotionValidationError("winning trial must occur exactly once in plan and run")
    trial = trials[0]
    pointer = pointers[0]
    result_path = paths.results_directory / f"{reveal.trial_id}.json"
    if (
        pointer.candidate_id != reveal.candidate_id
        or pointer.request_sha256 != reveal.request_sha256
        or pointer.result_sha256 != reveal.result_sha256
        or pointer.outcome != "success"
        or pointer.result_path != result_path.relative_to(layout.root).as_posix()
    ):
        raise PromotionValidationError("winning run pointer differs from the reveal entry")
    result = _read_audition_contract(
        layout.root,
        result_path,
        PersistedTrialResult,
        expected_sha256=reveal.result_sha256,
        label="winning audition trial result",
    )
    try:
        validate_persisted_trial_evidence(layout, trial, result)
    except (ContractError, OSError, SecurityError, ValueError) as error:
        raise PromotionValidationError(f"winning audition evidence is invalid: {error}") from error
    normalized = result.normalized
    environment = result.worker_result.environment
    runtime_receipt_relative = result.runtime_receipt_path
    if (
        result.outcome != "success"
        or normalized is None
        or environment is None
        or runtime_receipt_relative is None
    ):
        raise PromotionValidationError("winning audition trial lacks complete success evidence")
    native_path = Path(result.worker_result.output_path or "")
    native_sha256 = result.worker_result.output_sha256
    if not native_path.is_absolute() or native_sha256 is None:
        raise PromotionValidationError("winning native output lacks complete evidence")
    verified_indirect = [
        _verify_indirect_file(
            layout,
            native_path,
            expected_sha256=native_sha256,
            label="winning native output",
        ),
        _verify_indirect_file(
            layout,
            Path(normalized.path),
            expected_sha256=normalized.sha256,
            label="winning normalized output",
        ),
        _verify_indirect_file(
            layout,
            native_path.parent / environment.dependency_evidence_path,
            expected_sha256=environment.dependency_fingerprint_sha256,
            label="winning dependency evidence",
        ),
    ]
    metadata = result.worker_result.metadata
    model_relative = metadata.get("model_evidence_path")
    model_sha256 = metadata.get("model_evidence_sha256")
    executed_relative = metadata.get("executed_code_evidence_path")
    executed_sha256 = metadata.get("executed_code_evidence_sha256")
    if not all(
        isinstance(value, str)
        for value in (model_relative, model_sha256, executed_relative, executed_sha256)
    ):
        raise PromotionValidationError("winning result lacks bound worker/code evidence")
    model_path = native_path.parent / validate_relative_path(cast(str, model_relative))
    executed_path = native_path.parent / validate_relative_path(cast(str, executed_relative))
    worker_model = _read_audition_contract(
        layout.root,
        model_path,
        WorkerModelEvidence,
        expected_sha256=cast(str, model_sha256),
        label="winning worker-model evidence",
    )
    _read_audition_contract(
        layout.root,
        executed_path,
        ExecutedCodeEvidence,
        expected_sha256=cast(str, executed_sha256),
        label="winning executed-code evidence",
    )
    verified_indirect.extend(
        (
            model_path.relative_to(layout.root).as_posix(),
            executed_path.relative_to(layout.root).as_posix(),
        )
    )
    model_binding = manifest.model_lock.worker_model_evidence
    if (
        model_path.relative_to(layout.root).as_posix() != model_binding.path
        or model_binding.sha256 != model_sha256
        or manifest.model_lock.worker_model_record != worker_model
        or worker_model.executed_code_evidence_path != executed_relative
        or worker_model.executed_code_evidence_sha256 != executed_sha256
    ):
        raise PromotionValidationError(
            "promotion model lock differs from immutable worker-model evidence"
        )
    try:
        normalized_relative = Path(normalized.path).relative_to(layout.root).as_posix()
        reference_relative = Path(trial.request.reference.path).relative_to(layout.root).as_posix()
        consent_relative = (
            Path(trial.request.reference.consent_path).relative_to(layout.root).as_posix()
        )
    except ValueError as error:
        raise PromotionValidationError(
            "winning human or audio evidence escapes the asset root"
        ) from error
    run = manifest.winning_run.record
    request = trial.request
    provider_id, provider_voice, provider_settings = _provider_projection(trial, worker_model)
    exact_bindings: tuple[tuple[object, object, str], ...] = (
        (trial.track, "direct-tts", "track"),
        (trial.candidate_id, reveal.candidate_id, "candidate"),
        (trial.request_sha256, reveal.request_sha256, "request digest"),
        (reference_relative, manifest.reference.path, "reference path"),
        (request.reference.sha256, manifest.reference.sha256, "reference digest"),
        (consent_relative, manifest.consent.file.path, "consent path"),
        (request.reference.consent_sha256, manifest.consent.file.sha256, "consent digest"),
        (request.model_id, run.model_id, "model ID"),
        (request.model_revision, run.model_revision, "model revision"),
        (request.code_revision, run.code_revision, "code revision"),
        (request.language, run.language, "language"),
        (canonical_sha256(request.settings), run.audition_settings_sha256, "audition settings"),
        (
            environment.dependency_fingerprint_sha256,
            run.environment_fingerprint_sha256,
            "environment fingerprint",
        ),
        (result.completed_at, run.completed_at, "completion timestamp"),
        (normalized_relative, run.output.path, "normalized output path"),
        (normalized.sha256, run.output.sha256, "normalized output digest"),
        (normalized.size_bytes, run.output.size_bytes, "normalized output size"),
        (_PROVIDER_PROJECTION_ID, run.provider_projection_id, "provider projection"),
        (provider_id, run.provider_id, "provider ID"),
        (provider_id, manifest.provider_id, "promoted provider ID"),
        (provider_voice, run.provider_voice, "provider voice"),
        (provider_voice, manifest.provider_voice, "promoted provider voice"),
        (provider_settings, manifest.provider_settings, "provider settings"),
        (canonical_sha256(provider_settings), run.provider_settings_sha256, "provider settings"),
    )
    for actual, declared, label in exact_bindings:
        if actual != declared:
            raise PromotionValidationError(f"winning-run {label} differs from audition evidence")
    realized_watermark = result.worker_result.metadata.get("watermarked")
    if not isinstance(realized_watermark, bool) or realized_watermark != run.watermarked:
        raise PromotionValidationError(
            "winning-run watermark state lacks matching realized worker evidence"
        )
    if not (
        plan.created_at
        <= result.completed_at
        <= run_manifest.updated_at
        <= manifest.blind_package.record.created_at
    ):
        raise PromotionValidationError(
            "audition plan, result, run, and blind-package chronology is invalid"
        )
    return (
        paths.plan_path.relative_to(layout.root).as_posix(),
        paths.run_manifest_path.relative_to(layout.root).as_posix(),
        result_path.relative_to(layout.root).as_posix(),
        runtime_receipt_relative,
        reference_relative,
        consent_relative,
        *verified_indirect,
    )


def _promotion_snapshot(manifest: PromotionManifest) -> PromotionManifest:
    if not isinstance(manifest, PromotionManifest):
        raise TypeError("manifest must be a validated PromotionManifest")
    try:
        return PromotionManifest.model_validate(manifest.model_dump(mode="python"))
    except (TypeError, ValidationError, ValueError) as error:
        raise PromotionValidationError(
            f"promotion object no longer satisfies its strict contract: {error}"
        ) from error


def _validate_promotion_snapshot(
    manifest: PromotionManifest,
    *,
    asset_root: Path,
    as_of: datetime | None = None,
) -> PromotionValidation:
    evaluated_at = as_of or datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise PromotionValidationError("as_of requires an explicit timezone")
    evaluated_at = evaluated_at.astimezone(UTC)
    if evaluated_at < manifest.created_at:
        raise PromotionValidationError("promotion cannot be validated before it was created")
    root = _root(asset_root)

    verified = [
        _verify_asset(root, manifest.reference),
        _verify_json_record(root, manifest.consent.file, manifest.consent.record, ConsentRecord),
        _verify_json_record(
            root,
            manifest.provenance.file,
            manifest.provenance.record,
            HumanReferenceProvenanceRecord,
        ),
        _verify_json_record(
            root,
            manifest.model_lock.file,
            manifest.model_lock.record,
            ModelLockEntry,
        ),
        _verify_json_record(
            root,
            manifest.model_lock.model_source.file,
            manifest.model_lock.model_source.record,
            LocalSourceManifest,
        ),
        _verify_json_record(
            root,
            manifest.model_lock.worker_model_evidence,
            manifest.model_lock.worker_model_record,
            WorkerModelEvidence,
        ),
        _verify_json_record(
            root,
            manifest.winning_run.file,
            manifest.winning_run.record,
            WinningRunRecord,
        ),
        _verify_asset(root, manifest.winning_run.record.output),
        _verify_json_record(
            root,
            manifest.blind_package.file,
            manifest.blind_package.record,
            ReviewPackageManifest,
        ),
        _verify_json_record(
            root,
            manifest.score_sheet.file,
            manifest.score_sheet.record,
            ScoreSheet,
        ),
        _verify_json_record(
            root,
            manifest.listening_summary.file,
            manifest.listening_summary.record,
            PromotionListeningSummaryRecord,
        ),
        _verify_json_record(
            root,
            manifest.verdict.file,
            manifest.verdict.record,
            LabVerdictRecord,
        ),
    ]
    for source in manifest.model_lock.auxiliary_sources:
        verified.append(
            _verify_json_record(
                root,
                source.file,
                source.record,
                LocalSourceManifest,
            )
        )
    for source in (
        manifest.model_lock.model_source,
        *manifest.model_lock.auxiliary_sources,
    ):
        verified.extend(_verify_source_snapshot(root, source))
    for model_path, expected_digest in sorted(manifest.model_lock.record.files.items()):
        path = _bound_path(root, model_path)
        _reject_hardlink(path, model_path)
        try:
            digest, _ = sha256_file(path)
        except (OSError, SecurityError) as error:
            raise PromotionValidationError(
                f"cannot verify model artifact {model_path!r}: {error}"
            ) from error
        _reject_hardlink(path, model_path)
        if digest != expected_digest:
            raise PromotionValidationError(f"model artifact checksum changed: {model_path}")
        verified.append(model_path)

    layout = AssetLayout.validate(root)
    review_id = manifest.blind_package.record.review_id
    try:
        package = verify_review_package(layout, review_id=review_id)
    except (ContractError, OSError, SecurityError, ValueError) as error:
        raise PromotionValidationError(
            f"blind-review package validation failed: {error}"
        ) from error
    if (
        package.manifest != manifest.blind_package.record
        or package.package_manifest_sha256 != manifest.blind_package.file.sha256
        or package.review_index_sha256 != manifest.score_sheet.record.review_index_sha256
        or package.public_manifest_sha256 != manifest.score_sheet.record.public_manifest_sha256
    ):
        raise PromotionValidationError("blind-review package differs from promotion evidence")
    if not (
        package.manifest.created_at
        == package.index.created_at
        == package.public_manifest.created_at
    ):
        raise PromotionValidationError(
            "blind package, index, and public manifest timestamps must match"
        )
    package_relative = package.root.relative_to(root)
    verified.extend((package_relative / entry.path).as_posix() for entry in package.manifest.files)

    try:
        computed_payload = aggregate_scores(layout, manifest.score_sheet.record)
        computed_summary = ListeningSummaryAggregate.model_validate(computed_payload)
    except (ContractError, OSError, SecurityError, ValueError) as error:
        raise PromotionValidationError(f"score aggregation validation failed: {error}") from error
    if computed_summary != manifest.listening_summary.record.aggregate:
        raise PromotionValidationError("listening summary differs from the recomputed score sheet")
    winning_entry = manifest.verdict.record.winning_reveal_entry
    matching_entries = [entry for entry in package.reveal.entries if entry == winning_entry]
    if len(matching_entries) != 1:
        raise PromotionValidationError(
            "winning reveal entry does not occur exactly once in the blind package"
        )
    verified.extend(_verify_winning_audition_binding(layout, manifest, winning_entry))

    _validate_current_consent(manifest.consent.record, evaluated_at=evaluated_at)
    return PromotionValidation(
        promotion_sha256=canonical_sha256(manifest),
        evaluated_at=evaluated_at,
        verified_paths=tuple(sorted(set(verified))),
    )


def validate_promotion(
    manifest: PromotionManifest,
    *,
    asset_root: Path,
    as_of: datetime | None = None,
) -> PromotionValidation:
    """Deep-snapshot and revalidate every promotion binding without writing."""

    snapshot = _promotion_snapshot(manifest)
    return _validate_promotion_snapshot(snapshot, asset_root=asset_root, as_of=as_of)


def build_chloespeech_handoff(
    manifest: PromotionManifest,
    *,
    asset_root: Path,
    as_of: datetime | None = None,
) -> ChloeSpeechHandoff:
    """Validate a promotion and return JSON-ready public ChloeSpeech-shaped data."""

    manifest = _promotion_snapshot(manifest)
    validation = _validate_promotion_snapshot(manifest, asset_root=asset_root, as_of=as_of)
    consent = manifest.consent.record
    # ChloeSpeech's public clearance is day-granular. Round down so that it can
    # never extend a timestamp-granular consent interval.
    review_by = (
        consent.expires_at.astimezone(UTC).date() - timedelta(days=1)
        if consent.expires_at is not None
        else None
    )
    evidence: tuple[ChloeSpeechEvidenceExport, ...] = (
        ChloeSpeechEvidenceExport(
            kind="subject_consent",
            path=manifest.consent.file.path,
            sha256=manifest.consent.file.sha256,
            details={"consent_id": consent.consent_id},
        ),
        ChloeSpeechEvidenceExport(
            kind="reference_provenance",
            path=manifest.provenance.file.path,
            sha256=manifest.provenance.file.sha256,
            details={"provenance_id": manifest.provenance.record.provenance_id},
        ),
        ChloeSpeechEvidenceExport(
            kind="model_lock",
            path=manifest.model_lock.file.path,
            sha256=manifest.model_lock.file.sha256,
            details={
                "candidate_id": manifest.candidate_id,
                "code_revision": manifest.model_lock.record.code_revision,
                "model_revision": manifest.model_lock.record.model_revision,
            },
        ),
        ChloeSpeechEvidenceExport(
            kind="model_source_manifest",
            path=manifest.model_lock.model_source.file.path,
            sha256=manifest.model_lock.model_source.file.sha256,
            details={
                "repository": manifest.model_lock.model_source.record.repository,
                "source_id": manifest.model_lock.model_source.record.source_id,
            },
        ),
        ChloeSpeechEvidenceExport(
            kind="worker_model_evidence",
            path=manifest.model_lock.worker_model_evidence.path,
            sha256=manifest.model_lock.worker_model_evidence.sha256,
            details={"provider_projection_id": manifest.winning_run.record.provider_projection_id},
        ),
        ChloeSpeechEvidenceExport(
            kind="winning_run",
            path=manifest.winning_run.file.path,
            sha256=manifest.winning_run.file.sha256,
            details={"trial_id": manifest.winning_trial_id},
        ),
        ChloeSpeechEvidenceExport(
            kind="blind_package",
            path=manifest.blind_package.file.path,
            sha256=manifest.blind_package.file.sha256,
            details={"review_id": manifest.blind_package.record.review_id},
        ),
        ChloeSpeechEvidenceExport(
            kind="listening_scores",
            path=manifest.score_sheet.file.path,
            sha256=manifest.score_sheet.file.sha256,
            details={"reviewer_id": manifest.score_sheet.record.reviewer_id},
        ),
        ChloeSpeechEvidenceExport(
            kind="listening_summary",
            path=manifest.listening_summary.file.path,
            sha256=manifest.listening_summary.file.sha256,
            details={
                "selection_policy": manifest.verdict.record.selection_policy,
                "winning_blind_id": manifest.verdict.record.winning_reveal_entry.blind_id,
            },
        ),
        ChloeSpeechEvidenceExport(
            kind="lab_verdict",
            path=manifest.verdict.file.path,
            sha256=manifest.verdict.file.sha256,
            details={"verdict_id": manifest.verdict.record.verdict_id},
        ),
    )
    evidence += tuple(
        ChloeSpeechEvidenceExport(
            kind="auxiliary_source_manifest",
            path=source.file.path,
            sha256=source.file.sha256,
            details={"repository": source.record.repository, "source_id": source.record.source_id},
        )
        for source in manifest.model_lock.auxiliary_sources
    )
    voice = ChloeSpeechVoiceManifestExport(
        id=manifest.voice_id,
        name=manifest.voice_name,
        provider_id=manifest.provider_id,
        provider_voice=manifest.provider_voice,
        model=ChloeSpeechModelExport(
            id=manifest.model_lock.record.model_id,
            revision=manifest.model_lock.record.model_revision,
        ),
        language=manifest.language,
        subject=ChloeSpeechSubjectExport(id=manifest.subject_id),
        license=ChloeSpeechLicenseExport(**manifest.license.model_dump(mode="python")),
        provenance=ChloeSpeechProvenanceExport(
            created_by=f"cole-voice-lab promotion {manifest.promotion_id}",
            clearance=ChloeSpeechClearanceExport(
                allowed_purposes=(manifest.purpose,),
                review_by=review_by,
                scope=manifest.clearance_scope,
                evidence=evidence,
            ),
            disclosure=ChloeSpeechDisclosureExport(
                spoken=manifest.disclosure.spoken,
                persistent=manifest.disclosure.persistent,
            ),
        ),
        reference_audio=ChloeSpeechReferenceAudioExport(
            path=manifest.reference.path,
            sha256=manifest.reference.sha256,
            size_bytes=manifest.reference.size_bytes,
            subject_id=manifest.subject_id,
            provenance_path=manifest.provenance.file.path,
            provenance_sha256=manifest.provenance.file.sha256,
        ),
        provider_settings=dict(manifest.provider_settings),
        watermarked=manifest.watermarked,
    )
    cue = ChloeSpeechDisclosureCueExport(
        id=manifest.disclosure.cue_id,
        speaker_id=manifest.disclosure.speaker_id,
        text=manifest.disclosure.spoken,
        voice_id=manifest.voice_id,
        pause_after_ms=manifest.disclosure.pause_after_ms,
    )
    index = tuple(
        ChloeSpeechEvidenceIndexEntry(kind=item.kind, path=item.path, sha256=item.sha256)
        for item in evidence
    )
    return ChloeSpeechHandoff(
        promotion_id=manifest.promotion_id,
        promotion_sha256=validation.promotion_sha256,
        voice_manifest=voice,
        required_disclosure_cues=(cue,),
        lab_evidence=index,
    )


__all__ = [
    "AssetFileBinding",
    "BoundBlindPackageEvidence",
    "BoundConsentEvidence",
    "BoundListeningSummaryEvidence",
    "BoundModelLockEvidence",
    "BoundProvenanceEvidence",
    "BoundScoreSheetEvidence",
    "BoundSourceManifestEvidence",
    "BoundVerdictEvidence",
    "BoundWinningRunEvidence",
    "ChloeSpeechHandoff",
    "HANDOFF_SCHEMA_VERSION",
    "HumanReferenceBinding",
    "HumanReferenceProvenanceRecord",
    "JsonEvidenceFile",
    "LabVerdictRecord",
    "ListeningCandidateSummary",
    "ListeningDimensionMeans",
    "ListeningSummaryAggregate",
    "ListeningTrackSummary",
    "PROMOTION_PURPOSE",
    "PROMOTION_SCHEMA_VERSION",
    "PromotionDisclosure",
    "PromotionLicense",
    "PromotionListeningSummaryRecord",
    "PromotionManifest",
    "PromotionValidation",
    "PromotionValidationError",
    "WinningRunRecord",
    "build_chloespeech_handoff",
    "validate_promotion",
]

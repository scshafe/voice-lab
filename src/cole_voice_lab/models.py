"""Strict versioned contracts for capture, evidence, consent, and experiments."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from .errors import ContractError
from .paths import validate_relative_path

type Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type StableId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$", max_length=128),
]
# The first model family's id (D-048, D-049): F5-TTS. The one definition; the training
# adapter (``training``), the speech family (``speech.f5``) and their contracts import it.
F5_FAMILY: Final = "f5-tts"
type CandidateId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=80),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AudioSpec(ContractModel):
    sample_rate_hz: int = Field(ge=8_000, le=384_000)
    channels: int = Field(ge=1, le=32)
    sample_width_bytes: Literal[2, 3, 4]
    encoding: Literal["pcm_s16le", "pcm_s24le", "pcm_s32le"]
    frames: int = Field(ge=1)
    duration_seconds: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _encoding_matches_width(self) -> AudioSpec:
        width = {"pcm_s16le": 2, "pcm_s24le": 3, "pcm_s32le": 4}[self.encoding]
        if width != self.sample_width_bytes:
            raise ValueError("PCM encoding and sample width disagree")
        expected = self.frames / self.sample_rate_hz
        if abs(expected - self.duration_seconds) > max(0.001, expected * 0.0001):
            raise ValueError("duration does not agree with frame and sample-rate evidence")
        return self


type PromptLabel = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", min_length=2, max_length=32),
]

PROMPT_SUITE_V1: Final = "cole-voice-lab.prompt-suite/v1"
PROMPT_SUITE_V2: Final = "cole-voice-lab.prompt-suite/v2"
# A plain alias (not a PEP 695 ``type``) so the generated schema keeps its inline enum.
PromptSuitePurpose = Literal[
    "capture-pilot", "capture-corpus", "held-out-evaluation", "physical-test"
]
PHYSICAL_TEST_SUITE_PREFIX: Final = "cole-physical-test-"
SILENT_PROMPT_CATEGORIES: Final = frozenset({"room-tone", "procedure"})
# Labels are presentation and filter tokens only; none may impersonate a
# category, a derived flag, a status/filter word or a script section heading.
RESERVED_PROMPT_LABELS: Final = frozenset(
    {
        "room-tone",
        "neutral",
        "conversational",
        "warm-narration",
        "technical",
        "numbers",
        "expressive",
        "reference",
        "procedure",
        "reference-candidate",
        "holdout",
        "all",
        "attention",
        "missing",
        "recorded",
        "verified",
        "draft",
        "needs-review",
        "saved-checking",
        "pending",
        "running",
        "invalid",
        "failed",
        "unknown",
        "removed",
        "rejected",
        "kept",
        "prompt",
        "instructions",
        "guidance",
        "read-aloud",
        "silent",
        "do-not-speak",
    }
)


class Prompt(ContractModel):
    """One catalog entry. Optional v2 fields are omitted when absent so v1 bytes never change."""

    prompt_id: StableId
    text: str | None = Field(
        default=None, min_length=1, max_length=2_000, exclude_if=lambda value: value is None
    )
    category: Literal[
        "room-tone",
        "neutral",
        "conversational",
        "warm-narration",
        "technical",
        "numbers",
        "expressive",
        "reference",
        "procedure",
    ]
    style: str = Field(min_length=1, max_length=160)
    reference_candidate: bool = False
    holdout: bool = False
    expected_seconds: float = Field(gt=0.0, le=120.0, allow_inf_nan=False)
    labels: tuple[PromptLabel, ...] = Field(
        default=(), max_length=8, exclude_if=lambda value: not value
    )
    instructions: str | None = Field(
        default=None, min_length=1, max_length=2_000, exclude_if=lambda value: value is None
    )

    @field_validator("text", "style", "instructions")
    @classmethod
    def _non_whitespace(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text fields must contain a non-whitespace character")
        return value

    @model_validator(mode="after")
    def _silent_content_and_labels(self) -> Prompt:
        if self.text is None and self.category not in SILENT_PROMPT_CATEGORIES:
            raise ValueError("only room tone and procedure prompts may omit spoken text")
        if self.category == "procedure" and (self.text is not None or self.instructions is None):
            raise ValueError("procedure prompts require instructions and no spoken text")
        if len(self.labels) != len(set(self.labels)):
            raise ValueError("prompt labels must be unique")
        if RESERVED_PROMPT_LABELS.intersection(self.labels):
            raise ValueError("prompt labels cannot use reserved category, status or section words")
        return self

    @property
    def spoken(self) -> bool:
        """Behavior keys on category only (D-013); a v1 room tone has text but is silent."""
        return self.category not in SILENT_PROMPT_CATEGORIES

    @property
    def allows_quiet(self) -> bool:
        return not self.spoken

    @property
    def content(self) -> str:
        value = self.text if self.spoken else (self.instructions or self.text)
        if value is None:
            raise ContractError(f"prompt {self.prompt_id} has no content")
        return value

    def require_text(self) -> str:
        """Return spoken text for consumers that cannot represent a text-less prompt."""
        if self.text is None:
            raise ContractError(f"prompt {self.prompt_id} has no spoken text")
        return self.text


def has_hidden_control(value: str) -> bool:
    """True when ``value`` carries a format (Cf) or control (Cc, newline excepted) character.

    This is the prompt-suite/v2 text hygiene rule; the corpus store applies it at add and
    edit time so no stored prompt can later refuse to publish.
    """
    return any(
        (category := unicodedata.category(character)) == "Cf"
        or (category == "Cc" and character != "\n")
        for character in value
    )


class PromptSuite(ContractModel):
    schema_version: Literal["cole-voice-lab.prompt-suite/v1", "cole-voice-lab.prompt-suite/v2"] = (
        PROMPT_SUITE_V1
    )
    suite_id: StableId
    purpose: PromptSuitePurpose
    prompts: tuple[Prompt, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids_and_holdout_policy(self) -> PromptSuite:
        ids = [prompt.prompt_id for prompt in self.prompts]
        if len(ids) != len(set(ids)):
            raise ValueError("prompt IDs must be unique")
        expected_holdout = self.purpose == "held-out-evaluation"
        if any(prompt.holdout != expected_holdout for prompt in self.prompts):
            raise ValueError("prompt holdout flags must agree with suite purpose")
        if self.schema_version == PROMPT_SUITE_V1:
            self._v1_exactness()
        else:
            self._v2_rules()
        return self

    def _v1_exactness(self) -> None:
        if self.purpose == "physical-test":
            raise ValueError("prompt-suite/v1 catalogs cannot declare the physical-test purpose")
        for prompt in self.prompts:
            if prompt.text is None or prompt.labels or prompt.instructions is not None:
                raise ValueError("prompt-suite/v1 prompts require text and forbid v2 fields")
            if prompt.category == "procedure":
                raise ValueError("prompt-suite/v1 catalogs cannot contain procedure prompts")

    def _v2_rules(self) -> None:
        physical_test = self.purpose == "physical-test"
        if physical_test != self.suite_id.startswith(PHYSICAL_TEST_SUITE_PREFIX):
            raise ValueError("physical-test purpose and suite ID prefix must agree")
        for prompt in self.prompts:
            if prompt.category == "room-tone" and (
                prompt.text is not None or prompt.instructions is None
            ):
                raise ValueError("prompt-suite/v2 room tone requires instructions and no text")
            if prompt.category not in SILENT_PROMPT_CATEGORIES and prompt.text is None:
                raise ValueError("prompt-suite/v2 spoken prompts require text")
            if physical_test and ("test" not in prompt.labels or prompt.reference_candidate):
                raise ValueError("physical-test prompts require the test label and no references")
            if not physical_test and ("test" in prompt.labels or prompt.category == "procedure"):
                raise ValueError("only physical-test catalogs may use the test label or procedures")
            if any(
                value is not None and has_hidden_control(value)
                for value in (prompt.text, prompt.instructions, prompt.style)
            ):
                raise ValueError("prompt-suite/v2 text cannot contain format or control characters")


class AudioFileEvidence(ContractModel):
    path: str = Field(min_length=1, max_length=1_024)
    kind: Literal["utterance", "room-tone", "calibration"]
    sha256: Sha256
    size_bytes: int = Field(gt=0)
    audio: AudioSpec | None = None
    prompt_id: StableId | None = None
    transcript: str | None = Field(default=None, min_length=1, max_length=2_000)

    @field_validator("path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def _kind_fields(self) -> AudioFileEvidence:
        if self.kind in {"utterance", "room-tone"} and self.audio is None:
            raise ValueError("recorded audio requires audio-format evidence")
        if self.kind == "utterance" and (self.prompt_id is None or self.transcript is None):
            raise ValueError("utterances require a prompt ID and exact transcript")
        if self.kind != "utterance" and (self.prompt_id is not None or self.transcript is not None):
            raise ValueError("only utterances can bind prompts and transcripts")
        return self


class CaptureDevice(ContractModel):
    # Preserve historical evidence defaults; native capture opts in explicitly. "portaudio"
    # is the desktop app's capture worker (voice-lab-desktop, D-039): its continuity evidence
    # is its own contract, never D-015's Audio Queue evidence, and native groups still
    # require "coreaudio".
    backend: Literal["avfoundation", "coreaudio", "portaudio"] = "avfoundation"
    device_id: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=500)
    microphone_serial: str | None = Field(default=None, min_length=1, max_length=500)
    calibration_sha256: Sha256 | None = None


class CaptureManifest(ContractModel):
    schema_version: Literal["cole-voice-lab.capture-manifest/v1"] = (
        "cole-voice-lab.capture-manifest/v1"
    )
    session_id: StableId
    subject_id: StableId
    prompt_suite_id: StableId
    capture_host: str = Field(min_length=1, max_length=255)
    captured_at: datetime
    finalized_at: datetime
    capture_device: CaptureDevice
    intended_format: Literal["mono-48000-pcm-s24le"] = "mono-48000-pcm-s24le"
    processing_applied: tuple[str, ...] = ()
    files: tuple[AudioFileEvidence, ...] = Field(min_length=1)

    @field_validator("captured_at", "finalized_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps require an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _manifest_consistency(self) -> CaptureManifest:
        if self.finalized_at < self.captured_at:
            raise ValueError("finalization cannot precede capture")
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest file paths must be unique")
        prompt_ids = [entry.prompt_id for entry in self.files if entry.prompt_id is not None]
        if len(prompt_ids) != len(set(prompt_ids)):
            raise ValueError("a capture manifest cannot contain duplicate prompt recordings")
        if not any(entry.kind == "room-tone" for entry in self.files):
            raise ValueError("capture session requires room tone")
        if self.processing_applied:
            raise ValueError("raw capture manifests cannot declare signal processing")
        return self


class AudioQcMetrics(ContractModel):
    path: str
    duration_seconds: float = Field(gt=0.0, allow_inf_nan=False)
    peak_dbfs: float = Field(le=0.0, allow_inf_nan=False)
    rms_dbfs: float = Field(le=0.0, allow_inf_nan=False)
    dc_offset: float = Field(ge=-1.0, le=1.0, allow_inf_nan=False)
    clipped_samples: int = Field(ge=0)
    longest_zero_run_ms: float = Field(ge=0.0, allow_inf_nan=False)
    signal_to_noise_db: float | None = Field(default=None, allow_inf_nan=False)
    passed: bool
    findings: tuple[str, ...] = ()

    @field_validator("path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)


class QcReport(ContractModel):
    schema_version: Literal["cole-voice-lab.qc-report/v1"] = "cole-voice-lab.qc-report/v1"
    session_id: StableId
    manifest_sha256: Sha256
    generated_at: datetime
    status: Literal["pass", "review", "fail"]
    metrics: tuple[AudioQcMetrics, ...] = Field(min_length=1)
    headphone_review_required: Literal[True] = True

    @field_validator("generated_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps require an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _status_matches_unique_metrics(self) -> QcReport:
        paths = [metric.path for metric in self.metrics]
        if len(paths) != len(set(paths)):
            raise ValueError("QC metric paths must be unique")
        all_passed = all(metric.passed for metric in self.metrics)
        if self.status == "pass" and not all_passed:
            raise ValueError("passing QC status requires every metric to pass")
        if self.status == "fail" and all_passed:
            raise ValueError("failed QC status requires at least one failed metric")
        return self


class ConsentRecord(ContractModel):
    schema_version: Literal["cole-voice-lab.consent/v1"] = "cole-voice-lab.consent/v1"
    consent_id: StableId
    subject_id: StableId
    approved: bool
    attestation: Literal[
        "self-recorded-and-self-authorized",
        "pending-subject-attestation",
    ]
    # ``private-chloe-speech-rendering`` is retired (D-044, D-047): records written before
    # 2026-10-11 (every pending record ``assets init`` installed) still parse, so they can be
    # rotated into approval or revoked, but no check asks for it and nothing grants it.
    allowed_purposes: tuple[
        Literal[
            "private-zero-shot-evaluation",
            "private-fine-tuning",
            "private-home-host-synthesis",
            "private-assistant-speech",
            "private-chloe-speech-rendering",
        ],
        ...,
    ]
    allowed_execution_boundaries: tuple[
        Literal["capture-host", "tailnet-local-host", "home-host"], ...
    ]
    hosted_transfer_allowed: Literal[False] = False
    disclosure_required: Literal[True] = True
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("effective_at", "expires_at", "revoked_at")
    @classmethod
    def _optional_timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("consent timestamps require an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _approval_is_attested(self) -> ConsentRecord:
        if self.approved:
            if self.attestation != "self-recorded-and-self-authorized":
                raise ValueError("approved consent requires subject attestation")
            if self.effective_at is None:
                raise ValueError("approved consent requires an effective timestamp")
            if not self.allowed_purposes or not self.allowed_execution_boundaries:
                raise ValueError("approved consent requires non-empty scope")
            if self.expires_at is None:
                raise ValueError("approved consent requires an explicit expiry")
        if len(self.allowed_purposes) != len(set(self.allowed_purposes)):
            raise ValueError("consent purposes must be unique")
        if len(self.allowed_execution_boundaries) != len(set(self.allowed_execution_boundaries)):
            raise ValueError("consent execution boundaries must be unique")
        if self.revoked_at is not None and self.approved:
            raise ValueError("revoked consent cannot remain approved")
        if self.expires_at and self.effective_at and self.expires_at <= self.effective_at:
            raise ValueError("consent expiry must follow its effective time")
        if self.revoked_at and self.effective_at and self.revoked_at < self.effective_at:
            raise ValueError("consent revocation cannot precede its effective time")
        return self


class CandidateSpec(ContractModel):
    candidate_id: CandidateId
    display_name: str = Field(min_length=1, max_length=120)
    track: Literal["direct-tts", "voice-conversion"]
    engine: Literal["mlx-audio", "chatterbox", "f5-tts", "seed-vc"]
    runtime_id: StableId
    worker_module: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]+$")
    model_id: str = Field(min_length=1, max_length=500)
    model_source: Literal["huggingface", "git"]
    code_license: str = Field(min_length=1, max_length=200)
    weight_license: str = Field(min_length=1, max_length=200)
    commercial_use_allowed: bool
    requires_reference_text: bool
    supports_seed: bool
    default_settings: dict[str, JsonValue] = Field(default_factory=dict)
    estimated_disk_gib: float = Field(gt=0.0, allow_inf_nan=False)
    enabled: bool = True


class ModelLockEntry(ContractModel):
    candidate_id: CandidateId
    model_id: str = Field(min_length=1, max_length=500)
    model_revision: Sha256 | Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    code_revision: Sha256 | Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    resolved_at: datetime
    files: dict[str, Sha256] = Field(default_factory=dict)

    @field_validator("resolved_at")
    @classmethod
    def _aware_resolution(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("model resolution timestamp requires an explicit timezone")
        return value.astimezone(UTC)

    @field_validator("files")
    @classmethod
    def _portable_artifact_paths(cls, value: dict[str, Sha256]) -> dict[str, Sha256]:
        for path in value:
            validate_relative_path(path)
        return value


class ReferenceEvidence(ContractModel):
    path: str = Field(min_length=1, max_length=1_024)
    sha256: Sha256
    transcript: str = Field(min_length=1, max_length=2_000)
    subject_id: StableId
    consent_path: str = Field(min_length=1, max_length=1_024)
    consent_sha256: Sha256

    @field_validator("path", "consent_path")
    @classmethod
    def _absolute_worker_input(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("worker input paths must be normalized absolute paths")
        return value


class ExperimentRequest(ContractModel):
    schema_version: Literal["cole-voice-lab.worker-request/v1"] = "cole-voice-lab.worker-request/v1"
    experiment_id: StableId
    trial_id: StableId
    candidate_id: CandidateId
    track: Literal["direct-tts", "voice-conversion"]
    text: str = Field(min_length=1, max_length=20_000)
    language: str = Field(default="en", min_length=2, max_length=64)
    output_path: str = Field(min_length=1, max_length=4_096)
    reference: ReferenceEvidence
    source_audio_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    source_audio_sha256: Sha256 | None = None
    source_audio_size_bytes: int | None = Field(default=None, gt=0)
    seed: int = Field(default=0, ge=0, le=(2**63) - 1)
    model_id: str = Field(min_length=1, max_length=500)
    model_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    code_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    settings: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("output_path", "source_audio_path")
    @classmethod
    def _absolute_worker_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("worker execution paths must be normalized absolute paths")
        return value

    @model_validator(mode="after")
    def _track_inputs(self) -> ExperimentRequest:
        source_binding = (
            self.source_audio_path,
            self.source_audio_sha256,
            self.source_audio_size_bytes,
        )
        if self.track == "voice-conversion" and any(value is None for value in source_binding):
            raise ValueError("voice conversion requires checksum-bound performed source audio")
        if self.track == "direct-tts" and any(value is not None for value in source_binding):
            raise ValueError("direct TTS cannot receive performed source audio evidence")
        return self


class EnvironmentEvidence(ContractModel):
    python_version: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=500)
    machine: str = Field(min_length=1, max_length=200)
    dependency_fingerprint_sha256: Sha256
    dependency_evidence_path: str = Field(min_length=1, max_length=4_096)

    @field_validator("dependency_evidence_path")
    @classmethod
    def _portable_dependency_path(cls, value: str) -> str:
        return validate_relative_path(value)


class FileByteEvidence(ContractModel):
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class WorkerPackageByteEvidence(ContractModel):
    root_name: Literal["cole_voice_lab"] = "cole_voice_lab"
    files: dict[str, FileByteEvidence] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def _safe_names(cls, value: dict[str, FileByteEvidence]) -> dict[str, FileByteEvidence]:
        for path in value:
            validate_relative_path(path)
        if "workers/common.py" not in value:
            raise ValueError("executed worker evidence must include workers/common.py")
        return value


class DistributionByteEvidence(ContractModel):
    version: str = Field(min_length=1, max_length=200)
    files: dict[str, FileByteEvidence] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def _non_whitespace_version(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("distribution versions must be printable and non-whitespace")
        return value

    @field_validator("files")
    @classmethod
    def _safe_names(cls, value: dict[str, FileByteEvidence]) -> dict[str, FileByteEvidence]:
        if any(
            not path
            or len(path) > 4_096
            or "\x00" in path
            or any(ord(character) < 32 for character in path)
            for path in value
        ):
            raise ValueError("distribution file names must be bounded, non-empty, and printable")
        return value


class ExecutedCodeEvidence(ContractModel):
    schema_version: Literal["cole-voice-lab.executed-code-evidence/v1"] = (
        "cole-voice-lab.executed-code-evidence/v1"
    )
    worker_package: WorkerPackageByteEvidence
    distributions: dict[str, DistributionByteEvidence] = Field(min_length=1)
    integrity_sha256: Sha256

    @field_validator("distributions")
    @classmethod
    def _safe_distribution_names(
        cls, value: dict[str, DistributionByteEvidence]
    ) -> dict[str, DistributionByteEvidence]:
        if any(
            not name
            or len(name) > 200
            or "\x00" in name
            or any(ord(character) < 32 for character in name)
            for name in value
        ):
            raise ValueError("distribution names must be bounded, non-empty, and printable")
        if "cole-voice-lab" not in value:
            raise ValueError("executed-code evidence must include the cole-voice-lab distribution")
        return value

    @model_validator(mode="after")
    def _valid_integrity(self) -> ExecutedCodeEvidence:
        from .canonical import canonical_sha256

        payload = self.model_dump(mode="json", exclude={"integrity_sha256"})
        if self.integrity_sha256 != canonical_sha256(payload):
            raise ValueError("executed-code evidence integrity digest is invalid")
        return self


class WorkerModelEvidence(ContractModel):
    schema_version: Literal["cole-voice-lab.worker-model-evidence/v1"] = (
        "cole-voice-lab.worker-model-evidence/v1"
    )
    candidate_id: CandidateId
    model_id: str = Field(min_length=1, max_length=500)
    model_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    code_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    model_manifest_sha256: Sha256
    auxiliary_manifest_sha256: dict[str, Sha256]
    code_checkout_integrity_sha256: Sha256 | None
    executed_code_evidence_path: str = Field(min_length=1, max_length=4_096)
    executed_code_evidence_sha256: Sha256
    reference_sha256: Sha256
    consent_sha256: Sha256
    source_audio_sha256: Sha256 | None
    source_audio_size_bytes: int | None = Field(gt=0)
    settings: dict[str, JsonValue]
    runtime_package: str = Field(min_length=1, max_length=200)
    runtime_package_version: str = Field(min_length=1, max_length=200)
    watermarked: bool | None = None

    @field_validator("executed_code_evidence_path")
    @classmethod
    def _portable_code_evidence_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("auxiliary_manifest_sha256")
    @classmethod
    def _safe_auxiliary_names(cls, value: dict[str, Sha256]) -> dict[str, Sha256]:
        if any(
            not name
            or len(name) > 500
            or "\x00" in name
            or any(ord(character) < 32 for character in name)
            for name in value
        ):
            raise ValueError("auxiliary repository names must be bounded and printable")
        return value

    @field_validator("runtime_package", "runtime_package_version")
    @classmethod
    def _printable_runtime_identity(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("runtime package identity must be printable and non-whitespace")
        return value

    @model_validator(mode="after")
    def _source_pair(self) -> WorkerModelEvidence:
        if (self.source_audio_sha256 is None) != (self.source_audio_size_bytes is None):
            raise ValueError("source audio digest and size must be present together")
        if self.watermarked is None and "watermarked" in self.model_fields_set:
            raise ValueError("watermarked must be omitted when it is not applicable")
        return self


class ExperimentResult(ContractModel):
    schema_version: Literal["cole-voice-lab.worker-result/v1"] = "cole-voice-lab.worker-result/v1"
    trial_id: StableId
    candidate_id: CandidateId
    ok: bool
    output_path: str | None = None
    output_sha256: Sha256 | None = None
    output_bytes: int | None = Field(default=None, gt=0)
    audio: AudioSpec | None = None
    generation_seconds: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    realized_seed: int | None = Field(default=None, ge=0, le=(2**63) - 1)
    environment: EnvironmentEvidence | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    error_message: str | None = Field(default=None, min_length=1, max_length=4_000)

    @field_validator("output_path")
    @classmethod
    def _absolute_output_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("worker output paths must be normalized absolute paths")
        return value

    @model_validator(mode="after")
    def _exact_outcome(self) -> ExperimentResult:
        success = (
            self.output_path,
            self.output_sha256,
            self.output_bytes,
            self.audio,
            self.generation_seconds,
            self.realized_seed,
            self.environment,
        )
        if self.ok and (
            any(value is None for value in success)
            or self.error_code is not None
            or self.error_message is not None
        ):
            raise ValueError("successful results require complete evidence and no error")
        if not self.ok:
            if self.error_code is None or self.error_message is None:
                raise ValueError("failed results require a structured error")
            if any(value is not None for value in success):
                raise ValueError("failed results cannot carry successful output evidence")
        return self


def utc_now() -> datetime:
    return datetime.now(UTC)

"""Versioned, no-copy dataset and fine-tuning transfer-plan contracts."""

from __future__ import annotations

import os
import secrets
import shutil
import stat
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, cast

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .errors import ContractError, SecurityError
from .intake import validate_capture_package
from .models import ContractModel, Prompt, PromptSuite, Sha256, StableId, utc_now
from .paths import AssetLayout, confined_path, validate_relative_path
from .references import (
    AcceptedUtteranceBinding,
    BoundHeadphoneReview,
    ConsentEvidenceBinding,
    JsonEvidenceBinding,
    load_accepted_utterance,
    load_consent_binding,
    load_headphone_review,
)

DATASET_MANIFEST_NAME = "manifest.json"
TRANSFER_PLAN_NAME = "manifest.json"
DEFAULT_MINIMUM_HOLDOUT_SECONDS = 600.0
MAX_DATASET_EVIDENCE_BYTES = 16 * 1024 * 1024

type NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type DatasetSplit = Literal["train", "validation", "permanent-holdout"]
type DatasetCategory = Literal[
    "neutral",
    "conversational",
    "warm-narration",
    "technical",
    "numbers",
    "expressive",
    "reference",
]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require an explicit timezone")
    return value.astimezone(UTC)


class DatasetSelection(ContractModel):
    """A human-selected accepted utterance and its split/review evidence."""

    raw_manifest_sha256: Sha256
    prompt_id: StableId
    split: DatasetSplit
    headphone_review_path: str = Field(min_length=1, max_length=4_096)
    headphone_review_sha256: Sha256

    @field_validator("headphone_review_path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)


class DatasetSelectionDocument(ContractModel):
    """Bounded, versioned operator input for explicit dataset membership."""

    schema_version: Literal["cole-voice-lab.dataset-selection/v1"] = (
        "cole-voice-lab.dataset-selection/v1"
    )
    selections: tuple[DatasetSelection, ...] = Field(min_length=1, max_length=100_000)


class PilotGateApproval(ContractModel):
    """Separate human evidence authorizing work beyond the capture pilot."""

    schema_version: Literal["cole-voice-lab.pilot-gate/v1"] = "cole-voice-lab.pilot-gate/v1"
    gate_id: StableId
    pilot_manifest_sha256s: tuple[Sha256, ...] = Field(min_length=1)
    approved: Literal[True]
    headphone_review_completed: Literal[True]
    full_corpus_capture_allowed: Literal[True]
    approved_at: datetime
    approved_by: NonBlank = Field(max_length=256)
    statement: NonBlank = Field(max_length=4_096)

    @field_validator("approved_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _unique_pilot_manifests(self) -> PilotGateApproval:
        if len(self.pilot_manifest_sha256s) != len(set(self.pilot_manifest_sha256s)):
            raise ValueError("pilot gate manifest digests must be unique")
        return self


class BoundPilotGate(ContractModel):
    file: JsonEvidenceBinding
    record: PilotGateApproval


class DatasetEntry(ContractModel):
    split: DatasetSplit
    source: AcceptedUtteranceBinding
    prompt_suite_sha256: Sha256
    prompt_suite_purpose: Literal["capture-pilot", "capture-corpus", "held-out-evaluation"]
    prompt_suite: PromptSuite
    prompt: Prompt
    headphone_review: BoundHeadphoneReview

    @model_validator(mode="after")
    def _source_prompt_and_review(self) -> DatasetEntry:
        if self.prompt_suite.suite_id != self.source.prompt_suite_id:
            raise ValueError("dataset prompt suite does not match accepted source")
        if canonical_sha256(self.prompt_suite) != self.prompt_suite_sha256:
            raise ValueError("dataset prompt-suite digest does not match embedded evidence")
        if self.prompt_suite.purpose != self.prompt_suite_purpose:
            raise ValueError("dataset prompt-suite purpose is inconsistent")
        suite_prompts = [
            prompt
            for prompt in self.prompt_suite.prompts
            if prompt.prompt_id == self.source.prompt_id
        ]
        if len(suite_prompts) != 1 or suite_prompts[0] != self.prompt:
            raise ValueError("dataset prompt is not the exact embedded prompt-suite entry")
        if self.prompt.text != self.source.transcript:
            raise ValueError("dataset transcript must exactly match accepted source")
        source_identity = (
            self.source.session_id,
            self.source.prompt_id,
            self.source.raw_audio_sha256,
        )
        review_identity = (
            self.headphone_review.record.session_id,
            self.headphone_review.record.prompt_id,
            self.headphone_review.record.source_sha256,
        )
        if source_identity != review_identity:
            raise ValueError("dataset headphone review does not bind its source")
        if self.split == "permanent-holdout":
            if not self.prompt.holdout or self.prompt_suite_purpose != "held-out-evaluation":
                raise ValueError("permanent holdout requires held-out prompt-suite evidence")
        elif self.prompt.holdout or self.prompt_suite_purpose == "held-out-evaluation":
            raise ValueError("held-out prompts cannot enter train or validation")
        if self.prompt.category == "room-tone":
            raise ValueError("room tone cannot be a dataset utterance")
        return self


class CategoryAccounting(ContractModel):
    category: DatasetCategory
    train_count: int = Field(ge=0)
    validation_count: int = Field(ge=0)
    permanent_holdout_count: int = Field(ge=0)
    total_count: int = Field(gt=0)

    @model_validator(mode="after")
    def _sum_matches(self) -> CategoryAccounting:
        if (
            self.train_count + self.validation_count + self.permanent_holdout_count
            != self.total_count
        ):
            raise ValueError("category split counts must sum to total_count")
        return self


class HoldoutGate(ContractModel):
    minimum_duration_seconds: float = Field(gt=0.0)
    actual_duration_seconds: float = Field(gt=0.0)
    utterance_count: int = Field(gt=0)
    passed: Literal[True] = True

    @model_validator(mode="after")
    def _minimum_met(self) -> HoldoutGate:
        if self.actual_duration_seconds + 1e-9 < self.minimum_duration_seconds:
            raise ValueError("permanent holdout is below the required duration")
        return self


class DatasetManifest(ContractModel):
    schema_version: Literal["cole-voice-lab.dataset-manifest/v1"] = (
        "cole-voice-lab.dataset-manifest/v1"
    )
    dataset_id: StableId
    corpus_stage: Literal["pilot", "full-corpus"]
    created_at: datetime
    created_by: NonBlank = Field(max_length=256)
    purpose: Literal["private-fine-tuning"] = "private-fine-tuning"
    subject_id: StableId
    consent: ConsentEvidenceBinding
    pilot_gate: BoundPilotGate | None
    entries: tuple[DatasetEntry, ...] = Field(min_length=3)
    category_accounting: tuple[CategoryAccounting, ...] = Field(min_length=1)
    holdout_gate: HoldoutGate
    raw_audio_copied: Literal[False] = False

    @field_validator("created_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _split_and_accounting_invariants(self) -> DatasetManifest:
        if self.consent.purpose != self.purpose:
            raise ValueError("dataset consent must authorize private fine-tuning")
        if self.corpus_stage == "full-corpus" and self.pilot_gate is None:
            raise ValueError("full-corpus datasets require a separately bound pilot gate")
        if self.pilot_gate is not None and self.pilot_gate.record.approved_at > self.created_at:
            raise ValueError("dataset cannot predate its pilot-gate approval")
        if any(entry.source.subject_id != self.subject_id for entry in self.entries):
            raise ValueError("all dataset entries must match the consented subject")
        if any(
            entry.headphone_review.record.reviewed_at > self.created_at for entry in self.entries
        ):
            raise ValueError("dataset cannot predate headphone review")

        splits = {entry.split for entry in self.entries}
        expected_splits: set[DatasetSplit] = {
            "train",
            "validation",
            "permanent-holdout",
        }
        if splits != expected_splits:
            raise ValueError("train, validation, and permanent-holdout must all be non-empty")
        source_keys = [
            (entry.source.raw_manifest_sha256, entry.source.prompt_id) for entry in self.entries
        ]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("dataset cannot contain a duplicate accepted utterance")

        split_sets: dict[str, set[DatasetSplit]] = defaultdict(set)
        for entry in self.entries:
            split_sets[f"session:{entry.source.session_id}"].add(entry.split)
            split_sets[f"prompt:{entry.source.prompt_id}"].add(entry.split)
            split_sets[f"transcript:{entry.source.transcript}"].add(entry.split)
        overlap = sorted(key for key, values in split_sets.items() if len(values) > 1)
        if overlap:
            raise ValueError(f"session, prompt, or transcript overlap across splits: {overlap}")

        expected_counts: Counter[tuple[str, DatasetSplit]] = Counter(
            (entry.prompt.category, entry.split) for entry in self.entries
        )
        realized_categories = {row.category for row in self.category_accounting}
        expected_categories = {entry.prompt.category for entry in self.entries}
        if len(realized_categories) != len(self.category_accounting):
            raise ValueError("category accounting rows must be unique")
        if realized_categories != expected_categories:
            raise ValueError("category accounting must cover every realized category exactly")
        for row in self.category_accounting:
            expected = (
                expected_counts[(row.category, "train")],
                expected_counts[(row.category, "validation")],
                expected_counts[(row.category, "permanent-holdout")],
            )
            if expected != (
                row.train_count,
                row.validation_count,
                row.permanent_holdout_count,
            ):
                raise ValueError(f"category accounting is wrong for {row.category}")

        holdout = [entry for entry in self.entries if entry.split == "permanent-holdout"]
        holdout_seconds = sum(entry.source.audio.duration_seconds for entry in holdout)
        if len(holdout) != self.holdout_gate.utterance_count:
            raise ValueError("holdout gate utterance count does not match entries")
        if abs(holdout_seconds - self.holdout_gate.actual_duration_seconds) > 0.001:
            raise ValueError("holdout gate duration does not match entries")
        return self


class PayloadFileBinding(ContractModel):
    path: str = Field(min_length=1, max_length=4_096)
    sha256: Sha256
    size_bytes: int = Field(gt=0)
    media_type: Literal["audio/wav", "application/json"]

    @field_validator("path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)


class HostedTransferApproval(ContractModel):
    """A separate human authorization; this module never creates one implicitly."""

    schema_version: Literal["cole-voice-lab.hosted-transfer-approval/v1"] = (
        "cole-voice-lab.hosted-transfer-approval/v1"
    )
    approval_id: StableId
    approved: Literal[True]
    purpose: Literal["private-fine-tuning"] = "private-fine-tuning"
    destination: NonBlank = Field(max_length=2_048)
    dataset_manifest_sha256: Sha256
    payload_set_sha256: Sha256
    approved_at: datetime
    expires_at: datetime
    approved_by: NonBlank = Field(max_length=256)
    statement: NonBlank = Field(max_length=4_096)

    @field_validator("approved_at", "expires_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _expiry_follows_approval(self) -> HostedTransferApproval:
        if self.expires_at <= self.approved_at:
            raise ValueError("hosted-transfer approval expiry must follow approval")
        return self


class BoundHostedTransferApproval(ContractModel):
    file: JsonEvidenceBinding
    record: HostedTransferApproval


def _payload_digest(payload: Sequence[PayloadFileBinding]) -> str:
    return canonical_sha256([item.model_dump(mode="json") for item in payload])


class FineTuningTransferPlan(ContractModel):
    schema_version: Literal["cole-voice-lab.fine-tuning-transfer-plan/v1"] = (
        "cole-voice-lab.fine-tuning-transfer-plan/v1"
    )
    plan_id: StableId
    dataset_id: StableId
    dataset_manifest: JsonEvidenceBinding
    created_at: datetime
    created_by: NonBlank = Field(max_length=256)
    payload: tuple[PayloadFileBinding, ...] = Field(min_length=2)
    payload_set_sha256: Sha256
    hosted_transfer_status: Literal["prohibited", "approved-not-executed"]
    destination: str | None = Field(default=None, min_length=1, max_length=2_048)
    approval: BoundHostedTransferApproval | None = None
    execution_performed: Literal[False] = False

    @field_validator("created_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _payload_and_authority(self) -> FineTuningTransferPlan:
        paths = [item.path for item in self.payload]
        if len(paths) != len(set(paths)):
            raise ValueError("transfer payload paths must be unique")
        if self.payload_set_sha256 != _payload_digest(self.payload):
            raise ValueError("payload-set digest does not match exact payload bindings")
        dataset_members = [item for item in self.payload if item.path == self.dataset_manifest.path]
        if len(dataset_members) != 1:
            raise ValueError("transfer payload must include its exact dataset manifest")
        dataset_member = dataset_members[0]
        if (
            dataset_member.sha256 != self.dataset_manifest.sha256
            or dataset_member.size_bytes != self.dataset_manifest.size_bytes
            or dataset_member.media_type != "application/json"
        ):
            raise ValueError("dataset payload member does not match dataset binding")
        if self.hosted_transfer_status == "prohibited":
            if self.destination is not None or self.approval is not None:
                raise ValueError("prohibited hosted transfer cannot name approval or destination")
        else:
            if self.destination is None or self.approval is None:
                raise ValueError("hosted transfer requires separate destination-bound approval")
            record = self.approval.record
            if record.destination != self.destination:
                raise ValueError("transfer destination does not match approval")
            if record.payload_set_sha256 != self.payload_set_sha256:
                raise ValueError("transfer payload does not match approval")
            if record.dataset_manifest_sha256 != self.dataset_manifest.sha256:
                raise ValueError("transfer dataset manifest does not match approval")
            if record.approved_at > self.created_at or record.expires_at <= self.created_at:
                raise ValueError("hosted-transfer approval is not effective for this plan")
        return self


@dataclass(frozen=True, slots=True)
class PublishedDataset:
    directory: Path
    manifest: DatasetManifest
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class PublishedTransferPlan:
    directory: Path
    plan: FineTuningTransferPlan
    plan_sha256: str


def _safe_file(path: Path, *, max_bytes: int | None = None) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SecurityError(f"evidence must be a regular non-link file: {path}")
    if metadata.st_nlink != 1:
        raise SecurityError(f"hard-linked evidence is forbidden: {path}")
    if max_bytes is not None and metadata.st_size > max_bytes:
        raise ContractError(f"evidence exceeds {max_bytes} bytes: {path}")
    return metadata


def _read_canonical_model[ContractT: ContractModel](
    path: Path, model_type: type[ContractT]
) -> ContractT:
    _safe_file(path, max_bytes=MAX_DATASET_EVIDENCE_BYTES)
    try:
        with open_regular_no_follow(path) as stream:
            payload = stream.read(MAX_DATASET_EVIDENCE_BYTES + 1)
        record = model_type.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"invalid canonical evidence {path}: {error}") from error
    if payload != canonical_json_bytes(record) + b"\n":
        raise ContractError(f"JSON evidence is not canonical: {path}")
    return record


def _require_prefix(relative: str, prefix: tuple[str, ...], label: str) -> str:
    validated = validate_relative_path(relative)
    parts = PurePosixPath(validated).parts
    if parts[: len(prefix)] != prefix:
        raise SecurityError(f"{label} must be beneath {'/'.join(prefix)}/")
    return validated


def _json_binding(path: Path, relative: str, expected_sha256: str) -> JsonEvidenceBinding:
    metadata = _safe_file(path, max_bytes=MAX_DATASET_EVIDENCE_BYTES)
    digest, size_bytes = sha256_file(path)
    if digest != expected_sha256 or size_bytes != metadata.st_size:
        raise SecurityError(f"JSON evidence does not match its binding: {relative}")
    return JsonEvidenceBinding(path=relative, sha256=digest, size_bytes=size_bytes)


def load_pilot_gate(
    layout: AssetLayout, *, relative_path: str, expected_sha256: str
) -> BoundPilotGate:
    relative = _require_prefix(relative_path, ("provenance", "pilot-gates"), "pilot gate")
    path = confined_path(layout.root, relative, must_exist=True)
    binding = _json_binding(path, relative, expected_sha256)
    record = _read_canonical_model(path, PilotGateApproval)
    digest_after, size_after = sha256_file(path)
    if digest_after != binding.sha256 or size_after != binding.size_bytes:
        raise SecurityError("pilot-gate evidence changed during validation")
    for manifest_sha256 in record.pilot_manifest_sha256s:
        raw_session = confined_path(
            layout.root,
            f"raw/{manifest_sha256}",
            must_exist=True,
        )
        package = validate_capture_package(raw_session, allow_intake_receipt=True)
        if package.manifest_sha256 != manifest_sha256:
            raise SecurityError("pilot gate names a non-content-addressed raw session")
        if package.qc_report.status != "pass":
            raise ContractError("pilot-gate source capture QC status must be pass")
    return BoundPilotGate(file=binding, record=record)


def _prompt_for_source(suite: PromptSuite, prompt_id: str) -> Prompt:
    matches = [prompt for prompt in suite.prompts if prompt.prompt_id == prompt_id]
    if len(matches) != 1:
        raise ContractError(f"prompt suite must contain {prompt_id!r} exactly once")
    return matches[0]


def _category_accounting(entries: Sequence[DatasetEntry]) -> tuple[CategoryAccounting, ...]:
    counts: Counter[tuple[str, str]] = Counter(
        (entry.prompt.category, entry.split) for entry in entries
    )
    categories = sorted({entry.prompt.category for entry in entries})
    return tuple(
        CategoryAccounting(
            category=cast(DatasetCategory, category),
            train_count=counts[(category, "train")],
            validation_count=counts[(category, "validation")],
            permanent_holdout_count=counts[(category, "permanent-holdout")],
            total_count=sum(
                counts[(category, split)] for split in ("train", "validation", "permanent-holdout")
            ),
        )
        for category in categories
    )


def build_dataset_manifest(
    layout: AssetLayout,
    *,
    dataset_id: str,
    corpus_stage: Literal["pilot", "full-corpus"],
    selections: Sequence[DatasetSelection],
    prompt_suites: Mapping[str, PromptSuite],
    consent_path: str,
    consent_sha256: str,
    created_by: str,
    minimum_holdout_seconds: float = DEFAULT_MINIMUM_HOLDOUT_SECONDS,
    pilot_gate_path: str | None = None,
    pilot_gate_sha256: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> DatasetManifest:
    """Revalidate sources and build a no-copy three-way dataset manifest."""

    if minimum_holdout_seconds <= 0:
        raise ValueError("minimum holdout duration must be positive")
    now = _aware_utc(clock())
    if not selections:
        raise ContractError("dataset selections cannot be empty")
    if len({suite.suite_id for suite in prompt_suites.values()}) != len(prompt_suites):
        raise ContractError("prompt-suite mapping contains duplicate suite IDs")

    entries: list[DatasetEntry] = []
    for selection in selections:
        accepted = load_accepted_utterance(
            layout,
            raw_manifest_sha256=selection.raw_manifest_sha256,
            prompt_id=selection.prompt_id,
        )
        try:
            suite = prompt_suites[accepted.binding.prompt_suite_id]
        except KeyError as error:
            raise ContractError(
                f"missing prompt suite {accepted.binding.prompt_suite_id!r}"
            ) from error
        if suite.suite_id != accepted.binding.prompt_suite_id:
            raise ContractError("prompt-suite mapping key and suite ID disagree")
        prompt = _prompt_for_source(suite, accepted.binding.prompt_id)
        if prompt.text != accepted.binding.transcript:
            raise ContractError("accepted transcript does not exactly match prompt-suite text")
        review = load_headphone_review(
            layout,
            relative_path=selection.headphone_review_path,
            expected_sha256=selection.headphone_review_sha256,
            source=accepted.binding,
        )
        if review.record.reviewed_at < accepted.package.qc_report.generated_at:
            raise ContractError("dataset headphone review cannot predate source QC")
        entries.append(
            DatasetEntry(
                split=selection.split,
                source=accepted.binding,
                prompt_suite_sha256=canonical_sha256(suite),
                prompt_suite_purpose=suite.purpose,
                prompt_suite=suite,
                prompt=prompt,
                headphone_review=review,
            )
        )

    subject_ids = {entry.source.subject_id for entry in entries}
    if len(subject_ids) != 1:
        raise ContractError("one fine-tuning dataset cannot mix voice subjects")
    subject_id = next(iter(subject_ids))
    consent, _ = load_consent_binding(
        layout,
        relative_path=consent_path,
        expected_sha256=consent_sha256,
        subject_id=subject_id,
        purpose="private-fine-tuning",
        at=now,
    )

    gate_supplied = pilot_gate_path is not None or pilot_gate_sha256 is not None
    if gate_supplied and (pilot_gate_path is None or pilot_gate_sha256 is None):
        raise SecurityError("pilot-gate path and digest must be supplied together")
    gate = (
        load_pilot_gate(
            layout,
            relative_path=pilot_gate_path,
            expected_sha256=pilot_gate_sha256,
        )
        if pilot_gate_path is not None and pilot_gate_sha256 is not None
        else None
    )
    if corpus_stage == "full-corpus" and gate is None:
        raise SecurityError("full-corpus dataset work is prohibited before the pilot gate")
    if gate is not None and gate.record.approved_at > now:
        raise SecurityError("pilot gate is not yet effective")

    holdout_entries = [entry for entry in entries if entry.split == "permanent-holdout"]
    holdout_seconds = sum(entry.source.audio.duration_seconds for entry in holdout_entries)
    if holdout_seconds + 1e-9 < minimum_holdout_seconds:
        raise ContractError(
            f"permanent holdout is {holdout_seconds:.3f}s; "
            f"minimum is {minimum_holdout_seconds:.3f}s"
        )
    return DatasetManifest(
        dataset_id=dataset_id,
        corpus_stage=corpus_stage,
        created_at=now,
        created_by=created_by,
        subject_id=subject_id,
        consent=consent,
        pilot_gate=gate,
        entries=tuple(entries),
        category_accounting=_category_accounting(entries),
        holdout_gate=HoldoutGate(
            minimum_duration_seconds=minimum_holdout_seconds,
            actual_duration_seconds=holdout_seconds,
            utterance_count=len(holdout_entries),
        ),
    )


def validate_dataset_sources(
    layout: AssetLayout, manifest: DatasetManifest, *, at: datetime | None = None
) -> None:
    """Recheck accepted bytes, reviews, consent, and optional pilot-gate evidence."""

    when = _aware_utc(at or utc_now())
    consent, _ = load_consent_binding(
        layout,
        relative_path=manifest.consent.file.path,
        expected_sha256=manifest.consent.file.sha256,
        subject_id=manifest.subject_id,
        purpose="private-fine-tuning",
        at=when,
    )
    if consent != manifest.consent:
        raise SecurityError("dataset consent binding is stale")
    if manifest.pilot_gate is not None:
        gate = load_pilot_gate(
            layout,
            relative_path=manifest.pilot_gate.file.path,
            expected_sha256=manifest.pilot_gate.file.sha256,
        )
        if gate != manifest.pilot_gate:
            raise SecurityError("dataset pilot-gate binding is stale")
    for entry in manifest.entries:
        accepted = load_accepted_utterance(
            layout,
            raw_manifest_sha256=entry.source.raw_manifest_sha256,
            prompt_id=entry.source.prompt_id,
        )
        if accepted.binding != entry.source:
            raise SecurityError("dataset accepted-source binding is stale")
        review = load_headphone_review(
            layout,
            relative_path=entry.headphone_review.file.path,
            expected_sha256=entry.headphone_review.file.sha256,
            source=entry.source,
        )
        if review != entry.headphone_review:
            raise SecurityError("dataset headphone-review binding is stale")
        if review.record.reviewed_at < accepted.package.qc_report.generated_at:
            raise ContractError("dataset headphone review cannot predate source QC")


def _publication_directory_files(directory: Path) -> None:
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"publication must be a real directory: {directory}")
    members = list(directory.iterdir())
    if [member.name for member in members] != [DATASET_MANIFEST_NAME]:
        names = {member.name for member in members}
        raise SecurityError(f"publication has undeclared members: {sorted(names)}")
    _safe_file(members[0], max_bytes=MAX_DATASET_EVIDENCE_BYTES)


def _private_child_directory(parent: Path, name: str) -> Path:
    directory = parent / name
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"derived directory must be a real directory: {directory}")
    os.chmod(directory, 0o700)
    return directory


def publish_dataset_manifest(
    layout: AssetLayout, manifest: DatasetManifest, *, at: datetime | None = None
) -> PublishedDataset:
    """Atomically publish only the manifest; accepted audio remains in raw/."""

    validation_time = _aware_utc(at or utc_now())
    validate_dataset_sources(layout, manifest, at=validation_time)
    datasets_root = _private_child_directory(layout.directory("derived"), "datasets")
    destination = datasets_root / manifest.dataset_id
    if destination.exists() or destination.is_symlink():
        existing = validate_published_dataset(layout, destination, at=validation_time)
        if existing.manifest != manifest:
            raise SecurityError(f"dataset ID collision: {manifest.dataset_id}")
        return existing

    stage = layout.directory("tmp") / f".dataset-{manifest.dataset_id}-{secrets.token_hex(8)}"
    stage.mkdir(mode=0o700)
    published = False
    try:
        write_canonical_json(stage / DATASET_MANIFEST_NAME, manifest, mode=0o400)
        stage_descriptor = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(stage_descriptor)
        finally:
            os.close(stage_descriptor)
        os.rename(stage, destination)
        os.chmod(destination, 0o500)
        published = True
        root_descriptor = os.open(datasets_root, os.O_RDONLY)
        try:
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
        return validate_published_dataset(layout, destination, at=validation_time)
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


def validate_published_dataset(
    layout: AssetLayout, directory: Path, *, at: datetime | None = None
) -> PublishedDataset:
    datasets_root = (layout.directory("derived") / "datasets").resolve(strict=True)
    candidate = directory.expanduser()
    try:
        if candidate.parent.resolve(strict=True) != datasets_root:
            raise SecurityError("dataset publication must be a direct child of derived/datasets")
    except FileNotFoundError as error:
        raise SecurityError(f"dataset publication is unavailable: {candidate}") from error
    _publication_directory_files(candidate)
    manifest_path = candidate / DATASET_MANIFEST_NAME
    manifest = _read_canonical_model(manifest_path, DatasetManifest)
    if manifest.dataset_id != candidate.name:
        raise ContractError("dataset directory name does not match manifest ID")
    validate_dataset_sources(layout, manifest, at=at)
    digest, _ = sha256_file(manifest_path)
    return PublishedDataset(candidate, manifest, digest)


def _hosted_approval(
    layout: AssetLayout,
    *,
    relative_path: str,
    expected_sha256: str,
) -> BoundHostedTransferApproval:
    relative = _require_prefix(
        relative_path,
        ("provenance", "transfer-approvals"),
        "hosted-transfer approval",
    )
    path = confined_path(layout.root, relative, must_exist=True)
    binding = _json_binding(path, relative, expected_sha256)
    record = _read_canonical_model(path, HostedTransferApproval)
    digest_after, size_after = sha256_file(path)
    if digest_after != binding.sha256 or size_after != binding.size_bytes:
        raise SecurityError("hosted-transfer approval changed during validation")
    return BoundHostedTransferApproval(file=binding, record=record)


def _transfer_payload(published: PublishedDataset) -> tuple[PayloadFileBinding, ...]:
    dataset_id = published.manifest.dataset_id
    dataset_relative = f"derived/datasets/{dataset_id}/{DATASET_MANIFEST_NAME}"
    _, dataset_size = sha256_file(published.directory / DATASET_MANIFEST_NAME)
    payload_by_path: dict[str, PayloadFileBinding] = {
        dataset_relative: PayloadFileBinding(
            path=dataset_relative,
            sha256=published.manifest_sha256,
            size_bytes=dataset_size,
            media_type="application/json",
        )
    }
    for entry in published.manifest.entries:
        payload_by_path[entry.source.raw_audio_path] = PayloadFileBinding(
            path=entry.source.raw_audio_path,
            sha256=entry.source.raw_audio_sha256,
            size_bytes=entry.source.raw_audio_size_bytes,
            media_type="audio/wav",
        )
    return tuple(payload_by_path[path] for path in sorted(payload_by_path))


def validate_transfer_plan(
    layout: AssetLayout,
    plan: FineTuningTransferPlan,
    *,
    at: datetime | None = None,
) -> None:
    """Revalidate every planned payload byte and any still-effective approval."""

    when = _aware_utc(at or utc_now())
    published = validate_published_dataset(
        layout,
        layout.directory("derived") / "datasets" / plan.dataset_id,
        at=when,
    )
    dataset_relative = f"derived/datasets/{plan.dataset_id}/{DATASET_MANIFEST_NAME}"
    dataset_path = published.directory / DATASET_MANIFEST_NAME
    _, dataset_size = sha256_file(dataset_path)
    expected_dataset = JsonEvidenceBinding(
        path=dataset_relative,
        sha256=published.manifest_sha256,
        size_bytes=dataset_size,
    )
    if plan.dataset_manifest != expected_dataset:
        raise SecurityError("transfer plan dataset binding is stale")
    expected_payload = _transfer_payload(published)
    if plan.payload != expected_payload:
        raise SecurityError("transfer plan payload is not the exact current dataset payload")
    for item in plan.payload:
        path = confined_path(layout.root, item.path, must_exist=True)
        metadata = _safe_file(path)
        digest, size_bytes = sha256_file(path)
        if (
            digest != item.sha256
            or size_bytes != item.size_bytes
            or metadata.st_size != item.size_bytes
        ):
            raise SecurityError(f"transfer payload changed: {item.path}")
    if plan.approval is not None:
        approval = _hosted_approval(
            layout,
            relative_path=plan.approval.file.path,
            expected_sha256=plan.approval.file.sha256,
        )
        if approval != plan.approval:
            raise SecurityError("transfer-plan approval binding is stale")
        if approval.record.approved_at > when or approval.record.expires_at <= when:
            raise SecurityError("hosted-transfer approval is not currently effective")


def build_transfer_plan(
    layout: AssetLayout,
    *,
    plan_id: str,
    dataset_id: str,
    created_by: str,
    destination: str | None = None,
    approval_path: str | None = None,
    approval_sha256: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FineTuningTransferPlan:
    """Build an exact no-execution plan; hosted transfer is prohibited by default."""

    now = _aware_utc(clock())
    dataset_directory = layout.directory("derived") / "datasets" / dataset_id
    published = validate_published_dataset(layout, dataset_directory, at=now)
    dataset_relative = f"derived/datasets/{dataset_id}/{DATASET_MANIFEST_NAME}"
    dataset_path = published.directory / DATASET_MANIFEST_NAME
    _, dataset_size = sha256_file(dataset_path)
    dataset_binding = JsonEvidenceBinding(
        path=dataset_relative,
        sha256=published.manifest_sha256,
        size_bytes=dataset_size,
    )
    payload = _transfer_payload(published)
    payload_set_sha256 = _payload_digest(payload)

    supplied = (destination is not None, approval_path is not None, approval_sha256 is not None)
    if any(supplied) and not all(supplied):
        raise SecurityError(
            "hosted transfer requires destination, separate approval path, and approval digest"
        )
    approval = (
        _hosted_approval(
            layout,
            relative_path=approval_path,
            expected_sha256=approval_sha256,
        )
        if approval_path is not None and approval_sha256 is not None
        else None
    )
    if approval is not None:
        record = approval.record
        if record.destination != destination:
            raise SecurityError("hosted destination does not match separate approval")
        if record.dataset_manifest_sha256 != published.manifest_sha256:
            raise SecurityError("hosted approval does not bind the exact dataset manifest")
        if record.payload_set_sha256 != payload_set_sha256:
            raise SecurityError("hosted approval does not bind the exact transfer payload")
        if record.approved_at > now or record.expires_at <= now:
            raise SecurityError("hosted-transfer approval is not currently effective")

    return FineTuningTransferPlan(
        plan_id=plan_id,
        dataset_id=dataset_id,
        dataset_manifest=dataset_binding,
        created_at=now,
        created_by=created_by,
        payload=payload,
        payload_set_sha256=payload_set_sha256,
        hosted_transfer_status=("approved-not-executed" if approval is not None else "prohibited"),
        destination=destination,
        approval=approval,
    )


def publish_transfer_plan(
    layout: AssetLayout,
    plan: FineTuningTransferPlan,
    *,
    at: datetime | None = None,
) -> PublishedTransferPlan:
    """Atomically persist plan evidence.  This function has no network behavior."""

    validate_transfer_plan(layout, plan, at=at)
    plans_root = _private_child_directory(layout.directory("derived"), "transfer-plans")
    destination = plans_root / plan.plan_id
    if destination.exists() or destination.is_symlink():
        _publication_directory_files(destination)
        existing = _read_canonical_model(destination / TRANSFER_PLAN_NAME, FineTuningTransferPlan)
        if existing != plan:
            raise SecurityError(f"transfer-plan ID collision: {plan.plan_id}")
        digest, _ = sha256_file(destination / TRANSFER_PLAN_NAME)
        return PublishedTransferPlan(destination, existing, digest)
    stage = layout.directory("tmp") / f".transfer-plan-{plan.plan_id}-{secrets.token_hex(8)}"
    stage.mkdir(mode=0o700)
    published = False
    try:
        digest = write_canonical_json(stage / TRANSFER_PLAN_NAME, plan, mode=0o400)
        os.rename(stage, destination)
        os.chmod(destination, 0o500)
        published = True
        return PublishedTransferPlan(destination, plan, digest)
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


__all__ = [
    "BoundHostedTransferApproval",
    "BoundPilotGate",
    "CategoryAccounting",
    "DatasetEntry",
    "DatasetManifest",
    "DatasetSelection",
    "DatasetSelectionDocument",
    "FineTuningTransferPlan",
    "HoldoutGate",
    "HostedTransferApproval",
    "PayloadFileBinding",
    "PilotGateApproval",
    "PublishedDataset",
    "PublishedTransferPlan",
    "build_dataset_manifest",
    "build_transfer_plan",
    "load_pilot_gate",
    "publish_dataset_manifest",
    "publish_transfer_plan",
    "validate_dataset_sources",
    "validate_published_dataset",
    "validate_transfer_plan",
]

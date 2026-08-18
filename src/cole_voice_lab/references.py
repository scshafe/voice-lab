"""Checksum-bound, byte-for-byte human reference publication.

This module is intentionally a derived-data boundary.  It reads accepted raw
sessions, approved consent, and human review evidence; it never writes beneath
``raw/`` and never transforms the selected waveform.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from .audio import inspect_canonical_wav
from .canonical import (
    canonical_json_bytes,
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .consent import load_valid_consent
from .errors import ContractError, SecurityError
from .intake import ValidatedPackage, validate_capture_package
from .models import (
    AudioFileEvidence,
    AudioQcMetrics,
    AudioSpec,
    ConsentRecord,
    ContractModel,
    Sha256,
    StableId,
    utc_now,
)
from .paths import AssetLayout, confined_path, validate_relative_path

REFERENCE_MANIFEST_NAME = "manifest.json"
REFERENCE_AUDIO_NAME = "reference.wav"
MAX_EVIDENCE_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

type NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require an explicit timezone")
    return value.astimezone(UTC)


class JsonEvidenceBinding(ContractModel):
    """An exact, asset-root-relative canonical JSON file."""

    path: str = Field(min_length=1, max_length=4_096)
    sha256: Sha256
    size_bytes: int = Field(gt=0, le=MAX_EVIDENCE_BYTES)

    @field_validator("path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)


class HeadphoneReviewEvidence(ContractModel):
    """A human-authored pass for one exact accepted utterance."""

    schema_version: Literal["cole-voice-lab.headphone-review/v1"] = (
        "cole-voice-lab.headphone-review/v1"
    )
    review_id: StableId
    reviewer_id: StableId
    session_id: StableId
    prompt_id: StableId
    source_sha256: Sha256
    reviewed_at: datetime
    headphones_used: Literal[True]
    transcript_matches: Literal[True]
    audio_quality_passed: Literal[True]
    findings: tuple[str, ...] = ()

    @field_validator("reviewed_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("findings")
    @classmethod
    def _nonblank_findings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not finding.strip() for finding in value):
            raise ValueError("headphone-review findings cannot be blank")
        return value


class BoundHeadphoneReview(ContractModel):
    file: JsonEvidenceBinding
    record: HeadphoneReviewEvidence


class ConsentEvidenceBinding(ContractModel):
    file: JsonEvidenceBinding
    consent_id: StableId
    purpose: Literal["private-zero-shot-evaluation", "private-fine-tuning"]


class AcceptedUtteranceBinding(ContractModel):
    """Immutable coordinates for one utterance in the accepted raw store."""

    raw_session_path: str = Field(min_length=1, max_length=4_096)
    raw_manifest_sha256: Sha256
    raw_audio_path: str = Field(min_length=1, max_length=4_096)
    raw_audio_sha256: Sha256
    raw_audio_size_bytes: int = Field(gt=0)
    audio: AudioSpec
    session_id: StableId
    subject_id: StableId
    prompt_suite_id: StableId
    prompt_id: StableId
    transcript: str = Field(min_length=1, max_length=2_000)
    source_qc_report_sha256: Sha256
    source_qc_passed: Literal[True] = True

    @field_validator("raw_session_path", "raw_audio_path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def _content_addressed_source(self) -> AcceptedUtteranceBinding:
        expected_root = f"raw/{self.raw_manifest_sha256}"
        if self.raw_session_path != expected_root:
            raise ValueError("raw session path must be content-addressed by its manifest digest")
        prefix = f"{expected_root}/"
        if not self.raw_audio_path.startswith(prefix):
            raise ValueError("raw audio path must be beneath its bound raw session")
        return self


class ReferenceFileBinding(ContractModel):
    path: str = Field(min_length=1, max_length=4_096)
    sha256: Sha256
    size_bytes: int = Field(gt=0)
    audio: AudioSpec
    media_type: Literal["audio/wav"] = "audio/wav"

    @field_validator("path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)


class ReferenceManifest(ContractModel):
    schema_version: Literal["cole-voice-lab.reference-manifest/v1"] = (
        "cole-voice-lab.reference-manifest/v1"
    )
    reference_id: StableId
    created_at: datetime
    created_by: NonBlank = Field(max_length=256)
    purpose: Literal["private-zero-shot-evaluation"] = "private-zero-shot-evaluation"
    transformation: Literal["none-byte-for-byte-copy"] = "none-byte-for-byte-copy"
    source: AcceptedUtteranceBinding
    output: ReferenceFileBinding
    consent: ConsentEvidenceBinding
    headphone_review: BoundHeadphoneReview
    provenance_statement: NonBlank = Field(max_length=4_096)

    @field_validator("created_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def _evidence_chain(self) -> ReferenceManifest:
        if self.output.path != f"references/{self.reference_id}/{REFERENCE_AUDIO_NAME}":
            raise ValueError("reference output path does not match its stable reference ID")
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
            raise ValueError("headphone review does not bind the selected utterance")
        if self.output.sha256 != self.source.raw_audio_sha256:
            raise ValueError("a reference must be an exact byte-for-byte raw copy")
        if self.output.size_bytes != self.source.raw_audio_size_bytes:
            raise ValueError("reference and source byte counts must match")
        if self.output.audio != self.source.audio:
            raise ValueError("reference and source audio specifications must match")
        if self.consent.purpose != self.purpose:
            raise ValueError("reference consent purpose must match the reference purpose")
        if self.created_at < self.headphone_review.record.reviewed_at:
            raise ValueError("reference publication cannot predate headphone review")
        return self


@dataclass(frozen=True, slots=True)
class AcceptedUtterance:
    binding: AcceptedUtteranceBinding
    package: ValidatedPackage
    evidence: AudioFileEvidence
    metric: AudioQcMetrics
    path: Path


@dataclass(frozen=True, slots=True)
class PublishedReference:
    directory: Path
    manifest: ReferenceManifest
    manifest_sha256: str


def _require_prefix(relative: str, prefix: tuple[str, ...], label: str) -> str:
    validated = validate_relative_path(relative)
    parts = PurePosixPath(validated).parts
    if parts[: len(prefix)] != prefix:
        raise SecurityError(f"{label} must be beneath {'/'.join(prefix)}/")
    return validated


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
    _safe_file(path, max_bytes=MAX_EVIDENCE_BYTES)
    try:
        with open_regular_no_follow(path) as stream:
            payload = stream.read(MAX_EVIDENCE_BYTES + 1)
        record = model_type.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"invalid evidence file {path}: {error}") from error
    if payload != canonical_json_bytes(record) + b"\n":
        raise ContractError(f"JSON evidence is not canonical: {path}")
    return record


def _json_binding(path: Path, relative: str, expected_sha256: str) -> JsonEvidenceBinding:
    metadata = _safe_file(path, max_bytes=MAX_EVIDENCE_BYTES)
    digest, size_bytes = sha256_file(path)
    if digest != expected_sha256:
        raise SecurityError(f"evidence digest does not match bound value: {relative}")
    if metadata.st_size != size_bytes:
        raise SecurityError(f"evidence changed while hashing: {relative}")
    return JsonEvidenceBinding(path=relative, sha256=digest, size_bytes=size_bytes)


def load_accepted_utterance(
    layout: AssetLayout, *, raw_manifest_sha256: str, prompt_id: str
) -> AcceptedUtterance:
    """Revalidate and resolve one utterance from a content-addressed raw session."""

    if not _SHA256.fullmatch(raw_manifest_sha256):
        raise ContractError("raw manifest SHA-256 must be 64 lowercase hexadecimal characters")
    raw_relative = f"raw/{raw_manifest_sha256}"
    raw_session = confined_path(layout.root, raw_relative, must_exist=True)
    if raw_session.parent != layout.directory("raw"):
        raise SecurityError("accepted raw sessions must be direct children of raw/")
    package = validate_capture_package(raw_session, allow_intake_receipt=True)
    if package.manifest_sha256 != raw_manifest_sha256:
        raise SecurityError("raw directory name does not match its canonical manifest digest")
    if package.qc_report.status != "pass":
        raise ContractError("source capture QC status must be pass")

    matches = [
        evidence
        for evidence in package.manifest.files
        if evidence.kind == "utterance" and evidence.prompt_id == prompt_id
    ]
    if len(matches) != 1:
        raise ContractError(f"accepted session must contain prompt {prompt_id!r} exactly once")
    evidence = matches[0]
    metrics = [metric for metric in package.qc_report.metrics if metric.path == evidence.path]
    if len(metrics) != 1 or not metrics[0].passed:
        raise ContractError("selected utterance requires one passing source QC metric")
    if evidence.audio is None or evidence.transcript is None or evidence.prompt_id is None:
        raise ContractError("selected utterance lacks exact audio, prompt, or transcript evidence")

    audio_path = confined_path(raw_session, evidence.path, must_exist=True)
    inspection = inspect_canonical_wav(audio_path)
    if (
        inspection.sha256 != evidence.sha256
        or inspection.size_bytes != evidence.size_bytes
        or inspection.audio != evidence.audio
    ):
        raise SecurityError("selected raw utterance no longer matches accepted evidence")
    binding = AcceptedUtteranceBinding(
        raw_session_path=raw_relative,
        raw_manifest_sha256=package.manifest_sha256,
        raw_audio_path=f"{raw_relative}/{evidence.path}",
        raw_audio_sha256=evidence.sha256,
        raw_audio_size_bytes=evidence.size_bytes,
        audio=evidence.audio,
        session_id=package.manifest.session_id,
        subject_id=package.manifest.subject_id,
        prompt_suite_id=package.manifest.prompt_suite_id,
        prompt_id=evidence.prompt_id,
        transcript=evidence.transcript,
        source_qc_report_sha256=package.qc_report_sha256,
    )
    return AcceptedUtterance(
        binding=binding,
        package=package,
        evidence=evidence,
        metric=metrics[0],
        path=audio_path,
    )


def load_headphone_review(
    layout: AssetLayout,
    *,
    relative_path: str,
    expected_sha256: str,
    source: AcceptedUtteranceBinding,
) -> BoundHeadphoneReview:
    relative = _require_prefix(
        relative_path, ("provenance", "headphone-reviews"), "headphone review"
    )
    path = confined_path(layout.root, relative, must_exist=True)
    binding = _json_binding(path, relative, expected_sha256)
    record = _read_canonical_model(path, HeadphoneReviewEvidence)
    digest_after, size_after = sha256_file(path)
    if digest_after != binding.sha256 or size_after != binding.size_bytes:
        raise SecurityError("headphone-review evidence changed during validation")
    expected_identity = (source.session_id, source.prompt_id, source.raw_audio_sha256)
    actual_identity = (record.session_id, record.prompt_id, record.source_sha256)
    if actual_identity != expected_identity:
        raise SecurityError("headphone review does not bind the selected accepted utterance")
    return BoundHeadphoneReview(file=binding, record=record)


def load_consent_binding(
    layout: AssetLayout,
    *,
    relative_path: str,
    expected_sha256: str,
    subject_id: str,
    purpose: Literal["private-zero-shot-evaluation", "private-fine-tuning"],
    at: datetime,
) -> tuple[ConsentEvidenceBinding, ConsentRecord]:
    relative = _require_prefix(relative_path, ("provenance", "consents"), "consent")
    path = confined_path(layout.root, relative, must_exist=True)
    binding = _json_binding(path, relative, expected_sha256)
    record = load_valid_consent(
        path,
        expected_sha256=expected_sha256,
        subject_id=subject_id,
        purpose=purpose,
        execution_boundary="tailnet-local-host",
        at=at,
    )
    digest_after, size_after = sha256_file(path)
    if digest_after != binding.sha256 or size_after != binding.size_bytes:
        raise SecurityError("consent evidence changed while it was being validated")
    return (
        ConsentEvidenceBinding(
            file=binding,
            consent_id=record.consent_id,
            purpose=purpose,
        ),
        record,
    )


def _copy_exact(source: Path, destination: Path, expected_sha256: str) -> int:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_descriptor = os.open(source, source_flags)
    destination_descriptor: int | None = None
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o400,
        )
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SecurityError("raw source must remain a regular, singly linked file")
        with os.fdopen(source_descriptor, "rb", closefd=False) as source_stream:
            with os.fdopen(destination_descriptor, "wb", closefd=False) as destination_stream:
                while chunk := source_stream.read(1024 * 1024):
                    destination_stream.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        after = os.fstat(source_descriptor)
        stable = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable or size_bytes != before.st_size:
            raise SecurityError("raw source changed while the reference was copied")
        if digest.hexdigest() != expected_sha256:
            raise SecurityError("copied reference digest does not match accepted raw evidence")
    finally:
        os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
    return size_bytes


def _reference_directory_files(directory: Path) -> set[str]:
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"reference publication must be a real directory: {directory}")
    names: set[str] = set()
    for child in directory.iterdir():
        _safe_file(child)
        names.add(child.name)
    expected = {REFERENCE_AUDIO_NAME, REFERENCE_MANIFEST_NAME}
    if names != expected:
        raise SecurityError(
            f"reference publication files differ from contract; "
            f"missing={sorted(expected - names)}, undeclared={sorted(names - expected)}"
        )
    return names


def validate_reference_publication(
    layout: AssetLayout,
    directory: Path,
    *,
    at: datetime | None = None,
) -> PublishedReference:
    """Revalidate every source and copied byte behind a published reference."""

    references_root = layout.directory("references").resolve(strict=True)
    candidate = directory.expanduser()
    try:
        if candidate.parent.resolve(strict=True) != references_root:
            raise SecurityError("reference publications must be direct children of references/")
    except FileNotFoundError as error:
        raise SecurityError(f"reference publication is unavailable: {candidate}") from error
    _reference_directory_files(candidate)
    manifest_path = candidate / REFERENCE_MANIFEST_NAME
    manifest = _read_canonical_model(manifest_path, ReferenceManifest)
    if candidate.name != manifest.reference_id:
        raise ContractError("reference directory name does not match its manifest ID")

    source = load_accepted_utterance(
        layout,
        raw_manifest_sha256=manifest.source.raw_manifest_sha256,
        prompt_id=manifest.source.prompt_id,
    )
    if source.binding != manifest.source:
        raise SecurityError("reference source binding is stale or inconsistent")
    output_path = candidate / REFERENCE_AUDIO_NAME
    inspection = inspect_canonical_wav(output_path)
    realized_output = ReferenceFileBinding(
        path=f"references/{manifest.reference_id}/{REFERENCE_AUDIO_NAME}",
        sha256=inspection.sha256,
        size_bytes=inspection.size_bytes,
        audio=inspection.audio,
    )
    if realized_output != manifest.output:
        raise SecurityError("published reference audio does not match its manifest")

    review = load_headphone_review(
        layout,
        relative_path=manifest.headphone_review.file.path,
        expected_sha256=manifest.headphone_review.file.sha256,
        source=manifest.source,
    )
    if review != manifest.headphone_review:
        raise SecurityError("published reference headphone-review binding is stale")
    if review.record.reviewed_at < source.package.qc_report.generated_at:
        raise ContractError("headphone review cannot predate source QC")
    validation_time = _aware_utc(at or utc_now())
    consent, record = load_consent_binding(
        layout,
        relative_path=manifest.consent.file.path,
        expected_sha256=manifest.consent.file.sha256,
        subject_id=manifest.source.subject_id,
        purpose="private-zero-shot-evaluation",
        at=validation_time,
    )
    if consent != manifest.consent:
        raise SecurityError("published reference consent binding is stale")
    if record.effective_at is None or record.effective_at > manifest.created_at:
        raise ContractError("consent was not effective when the reference was published")

    manifest_sha256, _ = sha256_file(manifest_path)
    return PublishedReference(
        directory=candidate,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )


def publish_reference(
    layout: AssetLayout,
    *,
    reference_id: str,
    raw_manifest_sha256: str,
    prompt_id: str,
    consent_path: str,
    consent_sha256: str,
    headphone_review_path: str,
    headphone_review_sha256: str,
    created_by: str,
    provenance_statement: str,
    clock: Callable[[], datetime] = utc_now,
) -> PublishedReference:
    """Atomically publish one exact accepted utterance as a zero-shot reference."""

    now = _aware_utc(clock())
    source = load_accepted_utterance(
        layout,
        raw_manifest_sha256=raw_manifest_sha256,
        prompt_id=prompt_id,
    )
    review = load_headphone_review(
        layout,
        relative_path=headphone_review_path,
        expected_sha256=headphone_review_sha256,
        source=source.binding,
    )
    if review.record.reviewed_at < source.package.qc_report.generated_at:
        raise ContractError("headphone review cannot predate source QC")
    consent, record = load_consent_binding(
        layout,
        relative_path=consent_path,
        expected_sha256=consent_sha256,
        subject_id=source.binding.subject_id,
        purpose="private-zero-shot-evaluation",
        at=now,
    )
    if record.effective_at is None or record.effective_at > now:
        raise SecurityError("zero-shot consent is not effective for this publication")

    output = ReferenceFileBinding(
        path=f"references/{reference_id}/{REFERENCE_AUDIO_NAME}",
        sha256=source.binding.raw_audio_sha256,
        size_bytes=source.binding.raw_audio_size_bytes,
        audio=source.binding.audio,
    )
    desired = ReferenceManifest(
        reference_id=reference_id,
        created_at=now,
        created_by=created_by,
        source=source.binding,
        output=output,
        consent=consent,
        headphone_review=review,
        provenance_statement=provenance_statement,
    )
    destination = layout.directory("references") / desired.reference_id
    if destination.exists() or destination.is_symlink():
        existing = validate_reference_publication(layout, destination, at=now)
        comparable = desired.model_copy(update={"created_at": existing.manifest.created_at})
        if comparable != existing.manifest:
            raise SecurityError(f"reference ID collision: {desired.reference_id}")
        return existing

    tmp_root = layout.directory("tmp")
    stage = tmp_root / f".reference-{desired.reference_id}-{secrets.token_hex(8)}"
    stage.mkdir(mode=0o700)
    published = False
    try:
        copied_size = _copy_exact(
            source.path,
            stage / REFERENCE_AUDIO_NAME,
            source.binding.raw_audio_sha256,
        )
        if copied_size != source.binding.raw_audio_size_bytes:
            raise SecurityError("reference copy byte count disagrees with accepted raw evidence")
        inspection = inspect_canonical_wav(stage / REFERENCE_AUDIO_NAME)
        if (
            inspection.sha256 != desired.output.sha256
            or inspection.size_bytes != desired.output.size_bytes
            or inspection.audio != desired.output.audio
        ):
            raise SecurityError("staged reference failed byte-for-byte audio verification")
        write_canonical_json(stage / REFERENCE_MANIFEST_NAME, desired, mode=0o400)
        os.chmod(stage / REFERENCE_AUDIO_NAME, 0o400)
        directory_descriptor = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        try:
            os.rename(stage, destination)
        except OSError as error:
            if not destination.exists():
                raise
            existing = validate_reference_publication(layout, destination, at=now)
            comparable = desired.model_copy(update={"created_at": existing.manifest.created_at})
            if comparable != existing.manifest:
                raise SecurityError(f"reference ID collision: {desired.reference_id}") from error
            return existing
        os.chmod(destination, 0o500)
        published = True
        references_descriptor = os.open(layout.directory("references"), os.O_RDONLY)
        try:
            os.fsync(references_descriptor)
        finally:
            os.close(references_descriptor)
        return validate_reference_publication(layout, destination, at=now)
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


__all__ = [
    "AcceptedUtterance",
    "AcceptedUtteranceBinding",
    "BoundHeadphoneReview",
    "ConsentEvidenceBinding",
    "HeadphoneReviewEvidence",
    "JsonEvidenceBinding",
    "PublishedReference",
    "ReferenceFileBinding",
    "ReferenceManifest",
    "load_accepted_utterance",
    "load_consent_binding",
    "load_headphone_review",
    "publish_reference",
    "validate_reference_publication",
]

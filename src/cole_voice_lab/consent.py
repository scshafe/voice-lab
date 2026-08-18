"""Fail-closed, checksum-bound self-consent records."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from .canonical import open_regular_no_follow, sha256_file, write_canonical_json
from .errors import SecurityError
from .models import ConsentRecord, StableId, utc_now
from .paths import AssetLayout
from .resources import resource_path

Purpose = Literal[
    "private-zero-shot-evaluation",
    "private-fine-tuning",
    "private-chloe-speech-rendering",
]
ExecutionBoundary = Literal["capture-host", "tailnet-local-host"]

_STABLE_ID: TypeAdapter[StableId] = TypeAdapter(StableId)


def consent_record_path(layout: AssetLayout, consent_id: str) -> Path:
    """Return a confined consent path after validating its single-component ID."""

    validated = _STABLE_ID.validate_python(consent_id, strict=True)
    return consent_directory(layout) / f"{validated}.json"


def _read_consent(path: Path) -> tuple[ConsentRecord, bytes, str]:
    with open_regular_no_follow(path) as stream:
        payload = stream.read()
    digest = hashlib.sha256(payload).hexdigest()
    return ConsentRecord.model_validate_json(payload), payload, digest


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _preserve_consent_version(path: Path, digest: str, payload: bytes) -> Path:
    """Copy the exact current bytes into append-only history before a rotation."""

    if hashlib.sha256(payload).hexdigest() != digest:
        raise SecurityError("consent record changed while preserving history")
    history = path.parent / "history"
    history.mkdir(mode=0o700, exist_ok=True)
    if history.is_symlink() or not history.is_dir():
        raise SecurityError(f"consent history is not a real directory: {history}")
    history.chmod(0o700)
    destination = history / f"{path.stem}.{digest}.json"
    if destination.exists():
        archived_digest, _ = sha256_file(destination)
        if archived_digest != digest:
            raise SecurityError("consent history collision")
        return destination
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def consent_directory(layout: AssetLayout) -> Path:
    directory = layout.directory("provenance") / "consents"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise SecurityError(f"consent directory is not a real directory: {directory}")
    directory.chmod(0o700)
    return directory


def pending_template() -> ConsentRecord:
    template = resource_path("consent.pending.json")
    return ConsentRecord.model_validate_json(template.read_bytes())


def install_pending_consent(layout: AssetLayout) -> tuple[Path, str]:
    record = pending_template()
    path = consent_record_path(layout, record.consent_id)
    if path.exists():
        existing, _, digest = _read_consent(path)
        if existing != record:
            raise SecurityError(
                "refusing to replace an existing consent record; rotate with bound evidence"
            )
        return path, digest
    digest = write_canonical_json(path, record)
    return path, digest


def approve_self_consent(
    layout: AssetLayout,
    *,
    acknowledgement: bool,
    purposes: tuple[Purpose, ...],
    execution_boundaries: tuple[ExecutionBoundary, ...],
    expires_at: datetime,
    subject_id: str = "cole-self",
    consent_id: str = "cole-self-v1",
    expected_current_sha256: str | None = None,
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Create the explicit machine-readable authorization; never infer the attestation."""

    if not acknowledgement:
        raise SecurityError(
            "self-consent requires the explicit --i-am-the-subject-and-authorize flag"
        )
    effective = now or utc_now()
    effective = _aware_utc(effective, label="consent timestamp")
    expiry = _aware_utc(expires_at, label="consent expiry")
    record = ConsentRecord(
        consent_id=consent_id,
        subject_id=subject_id,
        approved=True,
        attestation="self-recorded-and-self-authorized",
        allowed_purposes=purposes,
        allowed_execution_boundaries=execution_boundaries,
        hosted_transfer_allowed=False,
        disclosure_required=True,
        effective_at=effective,
        expires_at=expiry,
        revoked_at=None,
    )
    path = consent_record_path(layout, record.consent_id)
    if path.exists():
        current, current_payload, current_digest = _read_consent(path)
        if expected_current_sha256 is None or current_digest != expected_current_sha256:
            raise SecurityError(
                "replacing pending consent requires its exact --expected-current-sha256"
            )
        if current.approved or current.revoked_at is not None:
            raise SecurityError("approved or revoked consent cannot be replaced")
        if current.attestation != "pending-subject-attestation":
            raise SecurityError("only a pending consent record may be rotated into approval")
        _preserve_consent_version(path, current_digest, current_payload)
    elif expected_current_sha256 is not None:
        raise SecurityError("expected consent digest was supplied but no current record exists")
    digest = write_canonical_json(path, record)
    return path, digest


def load_valid_consent(
    path: Path,
    *,
    expected_sha256: str | None,
    subject_id: str,
    purpose: Purpose,
    execution_boundary: ExecutionBoundary,
    at: datetime | None = None,
) -> ConsentRecord:
    record, _, digest = _read_consent(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise SecurityError("consent record digest does not match bound evidence")
    when = _aware_utc(at or utc_now(), label="consent evaluation timestamp")
    if not record.approved or record.attestation != "self-recorded-and-self-authorized":
        raise SecurityError("consent is not approved and attested")
    if record.subject_id != subject_id:
        raise SecurityError("consent subject does not match the reference subject")
    if purpose not in record.allowed_purposes:
        raise SecurityError(f"consent does not allow purpose {purpose}")
    if execution_boundary not in record.allowed_execution_boundaries:
        raise SecurityError(f"consent does not allow execution boundary {execution_boundary}")
    if record.effective_at is None or record.effective_at > when:
        raise SecurityError("consent is not yet effective")
    if record.expires_at is not None and record.expires_at <= when:
        raise SecurityError("consent has expired")
    if record.revoked_at is not None and record.revoked_at <= when:
        raise SecurityError("consent has been revoked")
    return record


def revoke_consent(
    path: Path,
    *,
    expected_sha256: str,
    at: datetime | None = None,
) -> tuple[ConsentRecord, str]:
    existing, payload, digest = _read_consent(path)
    if digest != expected_sha256:
        raise SecurityError("consent record digest does not match the requested revocation")
    if not existing.approved or existing.revoked_at is not None:
        raise SecurityError("only a currently approved consent record can be revoked")
    when = _aware_utc(at or utc_now(), label="consent revocation timestamp")
    revoked = ConsentRecord.model_validate(
        existing.model_dump(mode="python") | {"approved": False, "revoked_at": when}
    )
    _preserve_consent_version(path, digest, payload)
    digest = write_canonical_json(path, revoked)
    return revoked, digest

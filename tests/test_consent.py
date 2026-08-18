from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import cole_voice_lab.consent as consent
from cole_voice_lab.canonical import sha256_file, write_canonical_json
from cole_voice_lab.consent import (
    approve_self_consent,
    consent_directory,
    install_pending_consent,
    load_valid_consent,
    pending_template,
    revoke_consent,
)
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.paths import AssetLayout


def _approve(
    layout: AssetLayout,
    *,
    now: datetime | None = None,
    expected_current_sha256: str | None = None,
) -> tuple[Path, str]:
    effective = now or datetime(2026, 8, 17, 12, tzinfo=UTC)
    return approve_self_consent(
        layout,
        acknowledgement=True,
        purposes=("private-zero-shot-evaluation",),
        execution_boundaries=("tailnet-local-host",),
        expires_at=effective + timedelta(days=30),
        expected_current_sha256=expected_current_sha256,
        now=effective,
    )


def test_consent_requires_explicit_acknowledgement(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")

    with pytest.raises(SecurityError, match="explicit"):
        approve_self_consent(
            layout,
            acknowledgement=False,
            purposes=("private-zero-shot-evaluation",),
            execution_boundaries=("tailnet-local-host",),
            expires_at=datetime(2026, 9, 17, tzinfo=UTC),
        )


def test_pending_consent_is_not_authorization(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    path, digest = install_pending_consent(layout)

    with pytest.raises(SecurityError, match="not approved"):
        load_valid_consent(
            path,
            expected_sha256=digest,
            subject_id="cole-self",
            purpose="private-zero-shot-evaluation",
            execution_boundary="tailnet-local-host",
        )


def test_pending_consent_install_is_idempotent(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    first_path, first_digest = install_pending_consent(layout)
    second_path, second_digest = install_pending_consent(layout)

    assert (second_path, second_digest) == (first_path, first_digest)


def test_approved_consent_is_digest_and_scope_bound(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    pending_path, pending_digest = install_pending_consent(layout)
    path, digest = _approve(layout, expected_current_sha256=pending_digest, now=now)
    assert path == pending_path
    assert (path.parent / "history" / f"{path.stem}.{pending_digest}.json").exists()

    loaded = load_valid_consent(
        path,
        expected_sha256=digest,
        subject_id="cole-self",
        purpose="private-zero-shot-evaluation",
        execution_boundary="tailnet-local-host",
        at=now + timedelta(seconds=1),
    )
    assert loaded.approved

    with pytest.raises(SecurityError, match="digest"):
        load_valid_consent(
            path,
            expected_sha256="0" * 64,
            subject_id="cole-self",
            purpose="private-zero-shot-evaluation",
            execution_boundary="tailnet-local-host",
            at=now + timedelta(seconds=1),
        )
    with pytest.raises(SecurityError, match="subject"):
        load_valid_consent(
            path,
            expected_sha256=digest,
            subject_id="someone-else",
            purpose="private-zero-shot-evaluation",
            execution_boundary="tailnet-local-host",
            at=now + timedelta(seconds=1),
        )

    with pytest.raises(SecurityError, match="execution boundary"):
        load_valid_consent(
            path,
            expected_sha256=digest,
            subject_id="cole-self",
            purpose="private-zero-shot-evaluation",
            execution_boundary="capture-host",
            at=now + timedelta(seconds=1),
        )


def test_revocation_fails_closed(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    path, approved_digest = _approve(layout, now=now)
    _, digest = revoke_consent(
        path,
        expected_sha256=approved_digest,
        at=now + timedelta(minutes=1),
    )
    assert (path.parent / "history" / f"{path.stem}.{approved_digest}.json").read_bytes()

    with pytest.raises(SecurityError, match="not approved"):
        load_valid_consent(
            path,
            expected_sha256=digest,
            subject_id="cole-self",
            purpose="private-zero-shot-evaluation",
            execution_boundary="tailnet-local-host",
            at=now + timedelta(minutes=2),
        )


def test_consent_rotations_require_bound_current_digest(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    path, pending_digest = install_pending_consent(layout)

    with pytest.raises(SecurityError, match="expected-current"):
        _approve(layout)

    path, approved_digest = _approve(layout, expected_current_sha256=pending_digest)
    with pytest.raises(SecurityError, match="approved or revoked"):
        _approve(layout, expected_current_sha256=approved_digest)
    with pytest.raises(SecurityError, match="digest"):
        revoke_consent(path, expected_sha256="0" * 64)


def test_approval_rejects_expected_digest_without_current_record(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    with pytest.raises(SecurityError, match="no current record"):
        _approve(layout, expected_current_sha256="0" * 64)


def test_approval_rejects_nonpending_attestation(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    record = pending_template().model_copy(
        update={"attestation": "self-recorded-and-self-authorized"}
    )
    path = consent.consent_record_path(layout, record.consent_id)
    digest = write_canonical_json(path, record)

    with pytest.raises(SecurityError, match="only a pending consent"):
        _approve(layout, expected_current_sha256=digest)


def test_pending_install_never_replaces_approved_consent(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    path, pending_digest = install_pending_consent(layout)
    _approve(layout, expected_current_sha256=pending_digest)
    with pytest.raises(SecurityError, match="refusing to replace"):
        install_pending_consent(layout)
    assert path.exists()


def test_consent_requires_narrow_expiring_scope(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    with pytest.raises(ValidationError, match="non-empty scope"):
        approve_self_consent(
            layout,
            acknowledgement=True,
            purposes=(),
            execution_boundaries=(),
            expires_at=now + timedelta(days=1),
            now=now,
        )
    with pytest.raises(ValidationError, match="expiry"):
        approve_self_consent(
            layout,
            acknowledgement=True,
            purposes=("private-zero-shot-evaluation",),
            execution_boundaries=("tailnet-local-host",),
            expires_at=now,
            now=now,
        )


def test_naive_consent_times_are_rejected(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    with pytest.raises(ValueError, match="timezone"):
        approve_self_consent(
            layout,
            acknowledgement=True,
            purposes=("private-zero-shot-evaluation",),
            execution_boundaries=("tailnet-local-host",),
            expires_at=datetime(2026, 9, 17),
        )
    path, digest = _approve(layout)
    with pytest.raises(ValueError, match="timezone"):
        load_valid_consent(
            path,
            expected_sha256=digest,
            subject_id="cole-self",
            purpose="private-zero-shot-evaluation",
            execution_boundary="tailnet-local-host",
            at=datetime(2026, 8, 18),
        )
    with pytest.raises(ValidationError, match="cannot precede"):
        revoke_consent(
            path,
            expected_sha256=digest,
            at=datetime(2026, 8, 16, tzinfo=UTC),
        )


def test_valid_consent_rejects_wrong_purpose_and_time_windows(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    path, digest = _approve(layout, now=now)

    with pytest.raises(SecurityError, match="does not allow purpose"):
        load_valid_consent(
            path,
            expected_sha256=digest,
            subject_id="cole-self",
            purpose="private-fine-tuning",
            execution_boundary="tailnet-local-host",
            at=now + timedelta(seconds=1),
        )
    with pytest.raises(SecurityError, match="not yet effective"):
        load_valid_consent(
            path,
            expected_sha256=None,
            subject_id="cole-self",
            purpose="private-zero-shot-evaluation",
            execution_boundary="tailnet-local-host",
            at=now - timedelta(seconds=1),
        )
    with pytest.raises(SecurityError, match="expired"):
        load_valid_consent(
            path,
            expected_sha256=digest,
            subject_id="cole-self",
            purpose="private-zero-shot-evaluation",
            execution_boundary="tailnet-local-host",
            at=now + timedelta(days=30),
        )


def test_valid_consent_defensively_rejects_revocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    record = pending_template().model_construct(
        consent_id="cole-self-v1",
        subject_id="cole-self",
        approved=True,
        attestation="self-recorded-and-self-authorized",
        allowed_purposes=("private-zero-shot-evaluation",),
        allowed_execution_boundaries=("tailnet-local-host",),
        hosted_transfer_allowed=False,
        disclosure_required=True,
        effective_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
        revoked_at=now - timedelta(seconds=1),
    )
    monkeypatch.setattr(consent, "_read_consent", lambda _path: (record, b"fixture", "a" * 64))

    with pytest.raises(SecurityError, match="revoked"):
        load_valid_consent(
            tmp_path / "consent.json",
            expected_sha256=None,
            subject_id="cole-self",
            purpose="private-zero-shot-evaluation",
            execution_boundary="tailnet-local-host",
            at=now,
        )


def test_revocation_rejects_pending_and_already_revoked_records(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    pending_path, pending_digest = install_pending_consent(layout)
    with pytest.raises(SecurityError, match="currently approved"):
        revoke_consent(pending_path, expected_sha256=pending_digest)

    pending_path.unlink()
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    path, approved_digest = _approve(layout, now=now)
    _, revoked_digest = revoke_consent(
        path, expected_sha256=approved_digest, at=now + timedelta(seconds=1)
    )
    with pytest.raises(SecurityError, match="currently approved"):
        revoke_consent(path, expected_sha256=revoked_digest, at=now + timedelta(seconds=2))


def test_consent_history_is_idempotent_and_collision_safe(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    path, digest = install_pending_consent(layout)
    payload = path.read_bytes()

    archived = consent._preserve_consent_version(path, digest, payload)
    assert consent._preserve_consent_version(path, digest, payload) == archived

    archived.write_bytes(b"collision")
    with pytest.raises(SecurityError, match="history collision"):
        consent._preserve_consent_version(path, digest, payload)
    with pytest.raises(SecurityError, match="changed while preserving"):
        consent._preserve_consent_version(path, digest, b"changed")


def test_consent_history_write_failure_removes_partial_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    path, digest = install_pending_consent(layout)
    payload = path.read_bytes()

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(consent.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        consent._preserve_consent_version(path, digest, payload)

    history = path.parent / "history"
    assert not tuple(history.iterdir())


def test_consent_directories_reject_symlinks(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = layout.directory("provenance") / "consents"
    directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SecurityError, match="not a real directory"):
        consent_directory(layout)


def test_consent_history_rejects_symlink_directory(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    path, digest = install_pending_consent(layout)
    outside = tmp_path / "outside"
    outside.mkdir()
    history = path.parent / "history"
    history.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SecurityError, match="history is not a real directory"):
        consent._preserve_consent_version(path, digest, path.read_bytes())

    assert sha256_file(path)[0] == digest

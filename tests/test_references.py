from __future__ import annotations

import hashlib
import json
import os
import stat
import wave
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import cole_voice_lab.references as references_module
from cole_voice_lab.canonical import sha256_file, write_canonical_json
from cole_voice_lab.capture import expected_recording_paths, finalize_capture_session
from cole_voice_lab.consent import approve_self_consent
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake import MIN_FREE_BYTES, IntakeReceipt, accept_capture_session
from cole_voice_lab.models import AudioSpec, CaptureDevice, ConsentRecord, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.references import (
    MAX_EVIDENCE_BYTES,
    AcceptedUtteranceBinding,
    HeadphoneReviewEvidence,
    PublishedReference,
    ReferenceFileBinding,
    ReferenceManifest,
    load_accepted_utterance,
    load_headphone_review,
    publish_reference,
    validate_reference_publication,
)

NOW = datetime(2026, 8, 17, 18, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_test_permissions(tmp_path: Path) -> Iterator[None]:
    yield
    for root, directories, files in os.walk(tmp_path):
        for name in directories:
            os.chmod(Path(root) / name, 0o700)
        for name in files:
            os.chmod(Path(root) / name, 0o600)


@dataclass(frozen=True)
class AcceptedFixture:
    layout: AssetLayout
    receipt: IntakeReceipt
    suite: PromptSuite


def _write_wav(path: Path, *, amplitude: int = 1_000_000) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    samples = [amplitude, -amplitude] * 480
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(3)
        writer.setframerate(48_000)
        writer.writeframes(b"".join(value.to_bytes(3, "little", signed=True) for value in samples))


def _suite(suite_id: str = "pilot-reference-v1") -> PromptSuite:
    return PromptSuite(
        suite_id=suite_id,
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="room-tone-01",
                text="Remain silent.",
                category="room-tone",
                style="Silent room tone.",
                expected_seconds=1.0,
            ),
            Prompt(
                prompt_id="reference-neutral-01",
                text="This is the exact reference sentence.",
                category="reference",
                style="Neutral reference.",
                reference_candidate=True,
                expected_seconds=1.0,
            ),
        ),
    )


def _accepted(
    tmp_path: Path,
    *,
    session_id: str = "pilot-reference-001",
    quiet_utterance: bool = False,
) -> AcceptedFixture:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    suite = _suite()
    incoming = layout.directory("incoming") / session_id
    incoming.mkdir(mode=0o700)
    paths = expected_recording_paths(suite)
    _write_wav(incoming / paths[0])
    _write_wav(incoming / paths[1], amplitude=1 if quiet_utterance else 1_000_000)
    finalize_capture_session(
        incoming,
        session_id=session_id,
        subject_id="cole",
        prompt_suite=suite,
        device=CaptureDevice(device_id="4", display_name="miniDSP UMIK-1"),
        captured_at=NOW,
        finalized_at=NOW,
        capture_host="coles-macbook-pro",
    )
    receipt = accept_capture_session(
        layout,
        incoming,
        available_bytes=MIN_FREE_BYTES,
        clock=lambda: NOW,
    )
    return AcceptedFixture(layout, receipt, suite)


def _consent(fixture: AcceptedFixture) -> tuple[str, str]:
    existing = fixture.layout.directory("provenance") / "consents" / "cole-self-v1.json"
    if existing.exists():
        digest, _ = sha256_file(existing)
        return existing.relative_to(fixture.layout.root).as_posix(), digest
    path, digest = approve_self_consent(
        fixture.layout,
        acknowledgement=True,
        purposes=("private-zero-shot-evaluation", "private-fine-tuning"),
        execution_boundaries=("capture-host", "tailnet-local-host"),
        expires_at=NOW + timedelta(days=365),
        subject_id="cole",
        consent_id="cole-self-v1",
        now=NOW,
    )
    return path.relative_to(fixture.layout.root).as_posix(), digest


def _review(
    fixture: AcceptedFixture,
    *,
    prompt_id: str = "reference-neutral-01",
    source_sha256: str | None = None,
    reviewed_at: datetime = NOW + timedelta(seconds=1),
    canonical: bool = True,
) -> tuple[str, str]:
    source = load_accepted_utterance(
        fixture.layout,
        raw_manifest_sha256=fixture.receipt.manifest_sha256,
        prompt_id="reference-neutral-01",
    )
    record = HeadphoneReviewEvidence(
        review_id=f"review-{fixture.receipt.session_id}",
        reviewer_id="cole",
        session_id=fixture.receipt.session_id,
        prompt_id=prompt_id,
        source_sha256=source_sha256 or source.binding.raw_audio_sha256,
        reviewed_at=reviewed_at,
        headphones_used=True,
        transcript_matches=True,
        audio_quality_passed=True,
        findings=("Clean pacing and stable room tone.",),
    )
    directory = fixture.layout.directory("provenance") / "headphone-reviews"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{record.review_id}.json"
    if canonical:
        digest = write_canonical_json(path, record)
    else:
        payload = json.dumps(record.model_dump(mode="json"), indent=2).encode() + b"\n"
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
    return path.relative_to(fixture.layout.root).as_posix(), digest


def _publish(
    fixture: AcceptedFixture, *, reference_id: str = "cole-neutral-v1"
) -> PublishedReference:
    consent_path, consent_sha256 = _consent(fixture)
    review_path, review_sha256 = _review(fixture)
    return publish_reference(
        fixture.layout,
        reference_id=reference_id,
        raw_manifest_sha256=fixture.receipt.manifest_sha256,
        prompt_id="reference-neutral-01",
        consent_path=consent_path,
        consent_sha256=consent_sha256,
        headphone_review_path=review_path,
        headphone_review_sha256=review_sha256,
        created_by="cole",
        provenance_statement="Selected after checksum-bound headphone review.",
        clock=lambda: NOW + timedelta(seconds=2),
    )


def test_publish_reference_is_exact_atomic_read_only_and_idempotent(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    source = load_accepted_utterance(
        fixture.layout,
        raw_manifest_sha256=fixture.receipt.manifest_sha256,
        prompt_id="reference-neutral-01",
    )
    raw_digest_before, raw_size_before = sha256_file(source.path)
    raw_mode_before = stat.S_IMODE(source.path.stat().st_mode)

    first = _publish(fixture)
    second = _publish(fixture)

    reference_audio = first.directory / "reference.wav"
    assert first == second
    assert reference_audio.read_bytes() == source.path.read_bytes()
    assert first.manifest.source.transcript == "This is the exact reference sentence."
    assert first.manifest.source.raw_session_path == f"raw/{fixture.receipt.manifest_sha256}"
    assert first.manifest.output.sha256 == raw_digest_before
    assert first.manifest.headphone_review.record.audio_quality_passed is True
    assert first.manifest.consent.purpose == "private-zero-shot-evaluation"
    assert stat.S_IMODE(first.directory.stat().st_mode) == 0o500
    assert stat.S_IMODE(reference_audio.stat().st_mode) == 0o400
    assert not any(fixture.layout.directory("tmp").iterdir())
    assert sha256_file(source.path) == (raw_digest_before, raw_size_before)
    assert stat.S_IMODE(source.path.stat().st_mode) == raw_mode_before


def test_reference_validation_rechecks_copy_raw_review_and_consent(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    published = _publish(fixture)

    assert (
        validate_reference_publication(
            fixture.layout,
            published.directory,
            at=NOW + timedelta(seconds=3),
        ).manifest_sha256
        == published.manifest_sha256
    )

    os.chmod(published.directory / "reference.wav", 0o600)
    (published.directory / "reference.wav").write_bytes(b"tampered")
    with pytest.raises((ContractError, SecurityError)):
        validate_reference_publication(fixture.layout, published.directory)


def test_reference_rejects_nonpassing_source_qc(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path, quiet_utterance=True)
    consent_path, consent_digest = _consent(fixture)
    with pytest.raises(ContractError, match="QC status must be pass"):
        publish_reference(
            fixture.layout,
            reference_id="bad-qc",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=consent_path,
            consent_sha256=consent_digest,
            headphone_review_path="provenance/headphone-reviews/missing.json",
            headphone_review_sha256="0" * 64,
            created_by="cole",
            provenance_statement="Must not publish.",
        )


@pytest.mark.parametrize("mismatch", ["prompt", "audio"])
def test_reference_rejects_headphone_review_for_other_source(tmp_path: Path, mismatch: str) -> None:
    fixture = _accepted(tmp_path)
    consent_path, consent_digest = _consent(fixture)
    review_path, review_digest = _review(
        fixture,
        prompt_id=("other-prompt" if mismatch == "prompt" else "reference-neutral-01"),
        source_sha256=("f" * 64 if mismatch == "audio" else None),
    )
    with pytest.raises(SecurityError, match="does not bind"):
        publish_reference(
            fixture.layout,
            reference_id="mismatched-review",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=consent_path,
            consent_sha256=consent_digest,
            headphone_review_path=review_path,
            headphone_review_sha256=review_digest,
            created_by="cole",
            provenance_statement="Must not publish.",
        )


def test_reference_rejects_noncanonical_or_predating_review(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    consent_path, consent_digest = _consent(fixture)
    review_path, review_digest = _review(fixture, canonical=False)
    with pytest.raises(ContractError, match="not canonical"):
        publish_reference(
            fixture.layout,
            reference_id="noncanonical-review",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=consent_path,
            consent_sha256=consent_digest,
            headphone_review_path=review_path,
            headphone_review_sha256=review_digest,
            created_by="cole",
            provenance_statement="Must not publish.",
        )

    review_path, review_digest = _review(
        fixture,
        reviewed_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(ContractError, match="predate source QC"):
        publish_reference(
            fixture.layout,
            reference_id="early-review",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=consent_path,
            consent_sha256=consent_digest,
            headphone_review_path=review_path,
            headphone_review_sha256=review_digest,
            created_by="cole",
            provenance_statement="Must not publish.",
        )


def test_reference_requires_approved_zero_shot_consent_and_exact_digest(
    tmp_path: Path,
) -> None:
    fixture = _accepted(tmp_path)
    review_path, review_digest = _review(fixture)
    directory = fixture.layout.directory("provenance") / "consents"
    directory.mkdir(mode=0o700, exist_ok=True)
    pending = ConsentRecord(
        consent_id="pending",
        subject_id="cole",
        approved=False,
        attestation="pending-subject-attestation",
        allowed_purposes=("private-fine-tuning",),
        allowed_execution_boundaries=("tailnet-local-host",),
    )
    path = directory / "pending.json"
    digest = write_canonical_json(path, pending)
    relative = path.relative_to(fixture.layout.root).as_posix()

    with pytest.raises(SecurityError, match="not approved"):
        publish_reference(
            fixture.layout,
            reference_id="pending-consent",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=relative,
            consent_sha256=digest,
            headphone_review_path=review_path,
            headphone_review_sha256=review_digest,
            created_by="cole",
            provenance_statement="Must not publish.",
        )
    with pytest.raises(SecurityError, match="digest"):
        publish_reference(
            fixture.layout,
            reference_id="wrong-consent-digest",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=relative,
            consent_sha256="0" * 64,
            headphone_review_path=review_path,
            headphone_review_sha256=review_digest,
            created_by="cole",
            provenance_statement="Must not publish.",
        )


def test_reference_rejects_linked_review_and_raw_tamper(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    consent_path, consent_digest = _consent(fixture)
    review_path, review_digest = _review(fixture)
    real_review = fixture.layout.root / review_path
    linked_review = real_review.with_name("linked.json")
    linked_review.symlink_to(real_review)
    with pytest.raises(SecurityError, match="(?:non-link|symlink)"):
        publish_reference(
            fixture.layout,
            reference_id="linked-review",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=consent_path,
            consent_sha256=consent_digest,
            headphone_review_path=linked_review.relative_to(fixture.layout.root).as_posix(),
            headphone_review_sha256=review_digest,
            created_by="cole",
            provenance_statement="Must not publish.",
        )

    source = load_accepted_utterance(
        fixture.layout,
        raw_manifest_sha256=fixture.receipt.manifest_sha256,
        prompt_id="reference-neutral-01",
    ).path
    os.chmod(source, 0o600)
    source.write_bytes(source.read_bytes() + b"tamper")
    with pytest.raises(SecurityError, match="tampered"):
        publish_reference(
            fixture.layout,
            reference_id="tampered-raw",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=consent_path,
            consent_sha256=consent_digest,
            headphone_review_path=review_path,
            headphone_review_sha256=review_digest,
            created_by="cole",
            provenance_statement="Must not publish.",
        )


def test_reference_id_collision_fails_closed(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    _publish(fixture, reference_id="stable-reference")

    with pytest.raises(SecurityError, match="collision"):
        consent_path, consent_digest = _consent(fixture)
        review_path, review_digest = _review(fixture)
        publish_reference(
            fixture.layout,
            reference_id="stable-reference",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=consent_path,
            consent_sha256=consent_digest,
            headphone_review_path=review_path,
            headphone_review_sha256=review_digest,
            created_by="someone-else",
            provenance_statement="Different provenance must collide.",
            clock=lambda: NOW + timedelta(seconds=3),
        )


def test_reference_contract_rejects_transformation_or_hash_mismatch(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    published = _publish(fixture)
    data = published.manifest.model_dump(mode="python")
    data["transformation"] = "normalized"
    with pytest.raises(ValidationError):
        ReferenceManifest.model_validate(data)

    source = published.manifest.source
    wrong_output = ReferenceFileBinding(
        path=published.manifest.output.path,
        sha256="0" * 64,
        size_bytes=source.raw_audio_size_bytes,
        audio=source.audio,
    )
    with pytest.raises(ValidationError, match="byte-for-byte"):
        ReferenceManifest.model_validate(
            {**published.manifest.model_dump(mode="python"), "output": wrong_output}
        )


def test_accepted_binding_requires_content_addressed_raw_path() -> None:
    with pytest.raises(ValidationError, match="content-addressed"):
        AcceptedUtteranceBinding(
            raw_session_path="raw/not-the-hash",
            raw_manifest_sha256="a" * 64,
            raw_audio_path="raw/not-the-hash/audio/x.wav",
            raw_audio_sha256="b" * 64,
            raw_audio_size_bytes=100,
            audio=AudioSpec(
                sample_rate_hz=48_000,
                channels=1,
                sample_width_bytes=3,
                encoding="pcm_s24le",
                frames=1,
                duration_seconds=1 / 48_000,
            ),
            session_id="session",
            subject_id="cole",
            prompt_suite_id="suite",
            prompt_id="prompt",
            transcript="Exact.",
            source_qc_report_sha256="c" * 64,
        )


def test_reference_contract_cross_field_validation_matrix(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    base = _publish(fixture).manifest.model_dump(mode="python")
    cases: list[tuple[dict[str, Any], str]] = []

    wrong_path = deepcopy(base)
    wrong_path["output"]["path"] = "references/other/reference.wav"
    cases.append((wrong_path, "output path"))

    wrong_review = deepcopy(base)
    wrong_review["headphone_review"]["record"]["source_sha256"] = "f" * 64
    cases.append((wrong_review, "review does not bind"))

    wrong_size = deepcopy(base)
    wrong_size["output"]["size_bytes"] += 1
    cases.append((wrong_size, "byte counts"))

    wrong_audio = deepcopy(base)
    wrong_audio["output"]["audio"]["frames"] += 1
    wrong_audio["output"]["audio"]["duration_seconds"] = (
        wrong_audio["output"]["audio"]["frames"] / 48_000
    )
    cases.append((wrong_audio, "audio specifications"))

    wrong_purpose = deepcopy(base)
    wrong_purpose["consent"]["purpose"] = "private-fine-tuning"
    cases.append((wrong_purpose, "consent purpose"))

    early = deepcopy(base)
    early["created_at"] = NOW
    early["headphone_review"]["record"]["reviewed_at"] = NOW + timedelta(seconds=1)
    cases.append((early, "cannot predate"))

    for data, message in cases:
        with pytest.raises(ValidationError, match=message):
            ReferenceManifest.model_validate(data)

    with pytest.raises(ValidationError, match="timestamps require"):
        ReferenceManifest.model_validate({**base, "created_at": datetime(2026, 8, 17)})
    review = deepcopy(base["headphone_review"]["record"])
    review["findings"] = ("",)
    with pytest.raises(ValidationError, match="findings cannot be blank"):
        HeadphoneReviewEvidence.model_validate(review)


def test_reference_evidence_boundaries_reject_wrong_prefix_hardlink_and_size(
    tmp_path: Path,
) -> None:
    fixture = _accepted(tmp_path)
    source = load_accepted_utterance(
        fixture.layout,
        raw_manifest_sha256=fixture.receipt.manifest_sha256,
        prompt_id="reference-neutral-01",
    )
    review_path, review_digest = _review(fixture)
    with pytest.raises(SecurityError, match="headphone review must be beneath"):
        load_headphone_review(
            fixture.layout,
            relative_path="provenance/not-headphone-reviews/review.json",
            expected_sha256=review_digest,
            source=source.binding,
        )

    review = fixture.layout.root / review_path
    linked = review.with_name("hardlinked.json")
    os.link(review, linked)
    with pytest.raises(SecurityError, match="hard-linked"):
        load_headphone_review(
            fixture.layout,
            relative_path=linked.relative_to(fixture.layout.root).as_posix(),
            expected_sha256=review_digest,
            source=source.binding,
        )
    linked.unlink()

    oversized = review.with_name("oversized.json")
    with oversized.open("wb") as stream:
        stream.truncate(MAX_EVIDENCE_BYTES + 1)
    oversized_digest, _ = sha256_file(oversized)
    with pytest.raises(ContractError, match="exceeds"):
        load_headphone_review(
            fixture.layout,
            relative_path=oversized.relative_to(fixture.layout.root).as_posix(),
            expected_sha256=oversized_digest,
            source=source.binding,
        )


def test_accepted_utterance_rejects_invalid_digest_and_missing_prompt(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    with pytest.raises(ContractError, match="64 lowercase"):
        load_accepted_utterance(
            fixture.layout,
            raw_manifest_sha256="not-a-digest",
            prompt_id="reference-neutral-01",
        )
    with pytest.raises(ContractError, match="exactly once"):
        load_accepted_utterance(
            fixture.layout,
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="missing-prompt",
        )


def test_reference_publication_directory_is_fail_closed(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    published = _publish(fixture)
    os.chmod(published.directory, 0o700)
    extra = published.directory / "undeclared.txt"
    extra.write_text("undeclared")
    with pytest.raises(SecurityError, match="undeclared"):
        validate_reference_publication(fixture.layout, published.directory)
    extra.unlink()

    renamed = published.directory.with_name("different-reference-id")
    published.directory.rename(renamed)
    with pytest.raises(ContractError, match="directory name"):
        validate_reference_publication(fixture.layout, renamed)
    renamed.rename(published.directory)

    with pytest.raises(SecurityError, match="direct children"):
        validate_reference_publication(fixture.layout, fixture.layout.directory("derived"))
    with pytest.raises(SecurityError, match="unavailable"):
        validate_reference_publication(
            fixture.layout,
            fixture.layout.directory("references") / "missing-parent" / "missing",
        )
    linked = fixture.layout.directory("references") / "linked-publication"
    linked.symlink_to(published.directory, target_is_directory=True)
    with pytest.raises(SecurityError, match="real directory"):
        validate_reference_publication(fixture.layout, linked)


def test_exact_copy_helper_rejects_hardlink_and_wrong_digest(tmp_path: Path) -> None:
    fixture = _accepted(tmp_path)
    source = load_accepted_utterance(
        fixture.layout,
        raw_manifest_sha256=fixture.receipt.manifest_sha256,
        prompt_id="reference-neutral-01",
    ).path
    with pytest.raises(SecurityError, match="digest"):
        references_module._copy_exact(source, tmp_path / "wrong-digest.wav", "0" * 64)

    linked = tmp_path / "source-hardlink.wav"
    os.link(source, linked)
    with pytest.raises(SecurityError, match="singly linked"):
        references_module._copy_exact(
            source, tmp_path / "hardlink-copy.wav", sha256_file(source)[0]
        )


def test_reference_stage_cleanup_and_rename_race_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _accepted(tmp_path)
    consent_path, consent_digest = _consent(fixture)
    review_path, review_digest = _review(fixture)
    source = load_accepted_utterance(
        fixture.layout,
        raw_manifest_sha256=fixture.receipt.manifest_sha256,
        prompt_id="reference-neutral-01",
    )
    monkeypatch.setattr(
        references_module,
        "_copy_exact",
        lambda *_args: source.binding.raw_audio_size_bytes + 1,
    )
    with pytest.raises(SecurityError, match="byte count"):
        publish_reference(
            fixture.layout,
            reference_id="failed-stage",
            raw_manifest_sha256=fixture.receipt.manifest_sha256,
            prompt_id="reference-neutral-01",
            consent_path=consent_path,
            consent_sha256=consent_digest,
            headphone_review_path=review_path,
            headphone_review_sha256=review_digest,
            created_by="cole",
            provenance_statement="Exercise safe staging cleanup.",
            clock=lambda: NOW + timedelta(seconds=2),
        )
    assert not any(fixture.layout.directory("tmp").iterdir())

    monkeypatch.undo()
    real_rename = os.rename

    def _winning_race(source_path: Path, destination_path: Path) -> None:
        real_rename(source_path, destination_path)
        raise OSError("simulated concurrent winner")

    monkeypatch.setattr(os, "rename", _winning_race)
    raced = publish_reference(
        fixture.layout,
        reference_id="race-safe",
        raw_manifest_sha256=fixture.receipt.manifest_sha256,
        prompt_id="reference-neutral-01",
        consent_path=consent_path,
        consent_sha256=consent_digest,
        headphone_review_path=review_path,
        headphone_review_sha256=review_digest,
        created_by="cole",
        provenance_statement="Exercise an idempotent publication race.",
        clock=lambda: NOW + timedelta(seconds=2),
    )
    assert raced.directory.name == "race-safe"

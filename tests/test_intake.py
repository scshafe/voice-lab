from __future__ import annotations

import json
import os
import stat
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.canonical import sha256_file, write_canonical_json
from cole_voice_lab.capture import ReadyMarker, expected_recording_paths, finalize_capture_session
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake import (
    INTAKE_QC_REPORT_NAME,
    MAX_AUDIO_FILE_BYTES,
    MAX_CONTROL_FILE_BYTES,
    MIN_FREE_BYTES,
    IntakeReceipt,
    accept_capture_session,
    validate_capture_package,
)
from cole_voice_lab.models import CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout

NOW = datetime(2026, 8, 17, 19, 0, tzinfo=UTC)


def _write_wav(path: Path, *, amplitude: int = 1_000_000) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    samples = [amplitude, -amplitude] * 480
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(3)
        writer.setframerate(48_000)
        writer.writeframes(b"".join(value.to_bytes(3, "little", signed=True) for value in samples))


def _suite(*, text: str = "This is the exact intake sentence.") -> PromptSuite:
    return PromptSuite(
        suite_id="pilot-v1",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="room-tone-01",
                text="Remain silent.",
                category="room-tone",
                style="Silent",
                expected_seconds=10.0,
            ),
            Prompt(
                prompt_id="neutral-01",
                text=text,
                category="neutral",
                style="Neutral",
                expected_seconds=10.0,
            ),
        ),
    )


def _device() -> CaptureDevice:
    return CaptureDevice(device_id="4", display_name="miniDSP UMIK-1")


def _package(
    layout: AssetLayout, session_id: str, *, text: str = "This is the exact intake sentence."
) -> Path:
    suite = _suite(text=text)
    session = layout.directory("incoming") / session_id
    session.mkdir(mode=0o700)
    for relative in expected_recording_paths(suite):
        _write_wav(session / relative)
    finalize_capture_session(
        session,
        session_id=session_id,
        subject_id="cole",
        prompt_suite=suite,
        device=_device(),
        captured_at=NOW,
        finalized_at=NOW,
        capture_host="coles-macbook-pro",
    )
    return session


def _rewrite_and_rebind(
    package: Path,
    *,
    manifest_change: Callable[[dict[str, object]], None] | None = None,
    qc_change: Callable[[dict[str, object]], None] | None = None,
) -> None:
    manifest = json.loads((package / "manifest.json").read_text())
    if manifest_change is not None:
        manifest_change(manifest)
    manifest_digest = write_canonical_json(package / "manifest.json", manifest)
    qc = json.loads((package / "qc.json").read_text())
    qc["manifest_sha256"] = manifest_digest
    if qc_change is not None:
        qc_change(qc)
    qc_digest = write_canonical_json(package / "qc.json", qc)
    marker = ReadyMarker.model_validate_json((package / "READY").read_bytes())
    write_canonical_json(
        package / "READY",
        marker.model_copy(
            update={
                "manifest_sha256": manifest_digest,
                "qc_report_sha256": qc_digest,
            }
        ),
    )


def test_intake_verifies_and_atomically_publishes_read_only_raw(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    incoming = _package(layout, "pilot-001")

    receipt = accept_capture_session(layout, incoming, available_bytes=MIN_FREE_BYTES)

    raw = layout.root / receipt.raw_path
    assert not incoming.exists()
    assert raw.name == receipt.manifest_sha256
    assert (
        validate_capture_package(raw, allow_intake_receipt=True).manifest.session_id == "pilot-001"
    )
    assert not any(layout.directory("derived").iterdir())
    assert stat.S_IMODE(raw.stat().st_mode) == 0o500
    for root, directories, files in os.walk(raw):
        for name in files:
            assert stat.S_IMODE((Path(root) / name).stat().st_mode) == 0o400
        for name in directories:
            assert stat.S_IMODE((Path(root) / name).stat().st_mode) == 0o500


@pytest.mark.parametrize(
    ("amplitude", "finding"),
    [
        (8_388_607, "clipped-samples"),
        (1, "peak-too-quiet"),
    ],
)
def test_intake_persists_independent_qc_and_ignores_forged_source_pass(
    tmp_path: Path,
    amplitude: int,
    finding: str,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    incoming = _package(layout, f"pilot-forged-{finding}")
    relative = expected_recording_paths(_suite())[1]
    recording = incoming / relative
    _write_wav(recording, amplitude=amplitude)
    inspection = inspect_canonical_wav(recording)

    def bind_changed_audio(manifest: dict[str, object]) -> None:
        files = manifest["files"]
        assert isinstance(files, list)
        matches = [
            entry for entry in files if isinstance(entry, dict) and entry["path"] == relative
        ]
        assert len(matches) == 1
        matches[0]["sha256"] = inspection.sha256
        matches[0]["size_bytes"] = inspection.size_bytes
        matches[0]["audio"] = inspection.audio.model_dump(mode="json")

    # Rebind every capture-side digest while deliberately retaining the old,
    # passing source metrics. Intake must not treat that report as authoritative.
    _rewrite_and_rebind(incoming, manifest_change=bind_changed_audio)
    before = validate_capture_package(incoming)
    assert before.source_qc_report.status == "pass"
    assert before.qc_report.status == "review"
    assert (
        finding
        in next(metric for metric in before.qc_report.metrics if metric.path == relative).findings
    )

    receipt = accept_capture_session(layout, incoming, available_bytes=MIN_FREE_BYTES)
    raw = layout.root / receipt.raw_path
    accepted = validate_capture_package(raw, allow_intake_receipt=True)
    assert accepted.source_qc_report.status == "pass"
    assert accepted.qc_report.status == "review"
    assert receipt.source_qc_report_sha256 == accepted.source_qc_report_sha256
    assert receipt.intake_qc_report_sha256 == accepted.qc_report_sha256
    assert sha256_file(raw / INTAKE_QC_REPORT_NAME)[0] == accepted.qc_report_sha256


def test_intake_fresh_copy_breaks_preexisting_writable_descriptors(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    incoming = _package(layout, "pilot-stale-descriptor")
    relative = expected_recording_paths(_suite())[1]
    descriptor = os.open(incoming / relative, os.O_RDWR)
    try:
        source_inode = os.fstat(descriptor).st_ino
        receipt = accept_capture_session(layout, incoming, available_bytes=MIN_FREE_BYTES)
        raw_audio = layout.root / receipt.raw_path / relative
        accepted_digest = sha256_file(raw_audio)[0]
        assert raw_audio.stat().st_ino != source_inode

        os.lseek(descriptor, 64, os.SEEK_SET)
        os.write(descriptor, b"\xff\xff\xff")
        os.fsync(descriptor)
        assert sha256_file(raw_audio)[0] == accepted_digest
        validate_capture_package(layout.root / receipt.raw_path, allow_intake_receipt=True)
    finally:
        os.close(descriptor)


def test_identical_manifest_is_idempotent_and_returns_same_receipt(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    first = accept_capture_session(
        layout,
        _package(layout, "pilot-002"),
        available_bytes=MIN_FREE_BYTES,
        clock=lambda: NOW,
    )
    duplicate = _package(layout, "pilot-002")

    second = accept_capture_session(
        layout,
        duplicate,
        available_bytes=MIN_FREE_BYTES,
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert second == first
    assert not duplicate.exists()
    assert len(tuple(layout.directory("raw").iterdir())) == 1


def test_same_session_id_with_different_manifest_is_collision(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    accept_capture_session(
        layout,
        _package(layout, "pilot-003"),
        available_bytes=MIN_FREE_BYTES,
    )
    collision = _package(layout, "pilot-003", text="A different exact sentence.")

    with pytest.raises(SecurityError, match="collision"):
        accept_capture_session(layout, collision, available_bytes=MIN_FREE_BYTES)

    assert collision.exists()


def test_audio_tamper_is_rejected_and_incoming_is_restored(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    incoming = _package(layout, "pilot-004")
    recording = incoming / expected_recording_paths(_suite())[1]
    recording.write_bytes(recording.read_bytes() + b"tamper")

    with pytest.raises(SecurityError, match="tampered"):
        accept_capture_session(layout, incoming, available_bytes=MIN_FREE_BYTES)

    assert incoming.exists()
    assert not any(layout.directory("raw").iterdir())


def test_undeclared_file_and_symlink_are_rejected(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    undeclared = _package(layout, "pilot-005")
    (undeclared / "surprise.txt").write_text("not declared")

    with pytest.raises(SecurityError, match="undeclared"):
        accept_capture_session(layout, undeclared, available_bytes=MIN_FREE_BYTES)

    (undeclared / "surprise.txt").unlink()
    os.symlink(undeclared / "manifest.json", undeclared / "manifest-link.json")
    with pytest.raises(SecurityError, match="regular file"):
        accept_capture_session(layout, undeclared, available_bytes=MIN_FREE_BYTES)


def test_manifest_traversal_is_rejected_even_with_rebound_ready(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    incoming = _package(layout, "pilot-006")
    manifest_path = incoming / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["path"] = "../escape.wav"
    manifest_digest = write_canonical_json(manifest_path, manifest)
    ready = ReadyMarker(
        session_id="pilot-006",
        manifest_sha256=manifest_digest,
        qc_report_sha256=json.loads((incoming / "READY").read_text())["qc_report_sha256"],
        finalized_at=NOW,
    )
    write_canonical_json(incoming / "READY", ready)

    with pytest.raises(ContractError, match="paths must|parent path"):
        accept_capture_session(layout, incoming, available_bytes=MIN_FREE_BYTES)


def test_ten_gib_disk_floor_fails_before_claim(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    incoming = _package(layout, "pilot-007")

    with pytest.raises(SecurityError, match="disk floor"):
        accept_capture_session(layout, incoming, available_bytes=MIN_FREE_BYTES - 1)

    assert incoming.exists()


def test_partial_noncanonical_and_unexpected_directory_fail_closed(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    partial = _package(layout, "pilot-008")
    (partial / "READY").unlink()
    with pytest.raises(ContractError, match="no READY"):
        accept_capture_session(layout, partial, available_bytes=MIN_FREE_BYTES)
    assert partial.exists()

    noncanonical = _package(layout, "pilot-009")
    ready = noncanonical / "READY"
    ready.write_bytes(ready.read_bytes() + b" ")
    with pytest.raises(ContractError, match="canonical form"):
        accept_capture_session(layout, noncanonical, available_bytes=MIN_FREE_BYTES)

    unexpected_directory = _package(layout, "pilot-010")
    (unexpected_directory / "empty-extra").mkdir()
    with pytest.raises(SecurityError, match="directories differ"):
        accept_capture_session(
            layout,
            unexpected_directory,
            available_bytes=MIN_FREE_BYTES,
        )


def test_intake_requires_direct_real_incoming_child_and_matching_name(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    outside = tmp_path / "private" / "outside" / "pilot-outside"
    outside.mkdir(mode=0o700, parents=True)
    with pytest.raises(SecurityError, match="direct children"):
        accept_capture_session(layout, outside, available_bytes=MIN_FREE_BYTES)

    package = _package(layout, "pilot-011")
    mismatched = package.with_name("different-name")
    package.rename(mismatched)
    with pytest.raises(ContractError, match="directory name"):
        accept_capture_session(layout, mismatched, available_bytes=MIN_FREE_BYTES)
    assert mismatched.exists()

    real = _package(layout, "pilot-012")
    link = layout.directory("incoming") / "pilot-link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SecurityError, match="real directory"):
        accept_capture_session(layout, link, available_bytes=MIN_FREE_BYTES)


def test_negative_floor_and_unsafe_raw_store_member_are_rejected(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    package = _package(layout, "pilot-013")
    with pytest.raises(ValueError, match="cannot be negative"):
        accept_capture_session(layout, package, min_free_bytes=-1)

    (layout.directory("raw") / "unexpected.txt").write_text("unsafe")
    with pytest.raises(SecurityError, match="unsafe member"):
        accept_capture_session(layout, package, available_bytes=MIN_FREE_BYTES)
    assert package.exists()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("manifest_sha256", "manifest digest"),
        ("qc_report_sha256", "QC digest"),
        ("session_id", "session IDs disagree"),
    ],
)
def test_ready_bindings_are_independently_checked(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    package = _package(layout, f"pilot-binding-{field.replace('_', '-')}")
    marker = ReadyMarker.model_validate_json((package / "READY").read_bytes())
    replacement = "different-session" if field == "session_id" else "f" * 64
    write_canonical_json(package / "READY", marker.model_copy(update={field: replacement}))

    expected_error = ContractError if field == "session_id" else SecurityError
    with pytest.raises(expected_error, match=message):
        accept_capture_session(layout, package, available_bytes=MIN_FREE_BYTES)


@pytest.mark.parametrize("mutation", ["umik", "metrics", "size", "format"])
def test_rebound_but_false_contract_evidence_is_rejected(
    tmp_path: Path,
    mutation: str,
) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    package = _package(layout, f"pilot-false-{mutation}")

    def change_manifest(manifest: dict[str, object]) -> None:
        if mutation == "umik":
            device = manifest["capture_device"]
            assert isinstance(device, dict)
            device["display_name"] = "MacBook Pro Microphone"
        elif mutation == "size":
            files = manifest["files"]
            assert isinstance(files, list) and isinstance(files[0], dict)
            files[0]["size_bytes"] = MAX_AUDIO_FILE_BYTES + 1
        elif mutation == "format":
            files = manifest["files"]
            assert isinstance(files, list) and isinstance(files[0], dict)
            audio = files[0]["audio"]
            assert isinstance(audio, dict)
            audio["sample_rate_hz"] = 96_000
            audio["duration_seconds"] = audio["frames"] / 96_000

    def change_qc(qc: dict[str, object]) -> None:
        if mutation == "metrics":
            metrics = qc["metrics"]
            assert isinstance(metrics, list)
            qc["metrics"] = metrics[:1]

    _rewrite_and_rebind(package, manifest_change=change_manifest, qc_change=change_qc)
    messages = {
        "umik": "UMIK-1",
        "metrics": "cover every",
        "size": "size limit",
        "format": "format evidence disagrees",
    }
    error = ContractError
    with pytest.raises(error, match=messages[mutation]):
        accept_capture_session(layout, package, available_bytes=MIN_FREE_BYTES)


def test_control_size_directory_link_and_hardlink_are_rejected(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    oversized = _package(layout, "pilot-oversized-control")
    (oversized / "READY").write_bytes(b" " * (MAX_CONTROL_FILE_BYTES + 1))
    with pytest.raises(ContractError, match="control file exceeds"):
        accept_capture_session(layout, oversized, available_bytes=MIN_FREE_BYTES)

    linked_directory = _package(layout, "pilot-linked-directory")
    (linked_directory / "linked-audio").symlink_to(
        linked_directory / "audio", target_is_directory=True
    )
    with pytest.raises(SecurityError, match="(?:not be a link|not a regular file)"):
        accept_capture_session(layout, linked_directory, available_bytes=MIN_FREE_BYTES)

    hardlinked = _package(layout, "pilot-hardlink")
    source = hardlinked / expected_recording_paths(_suite())[0]
    os.link(source, hardlinked / "audio" / "hardlink.wav")
    with pytest.raises(SecurityError, match="hard-linked"):
        accept_capture_session(layout, hardlinked, available_bytes=MIN_FREE_BYTES)


def test_receipt_mismatch_and_staging_collision_fail_closed(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    receipt = accept_capture_session(
        layout,
        _package(layout, "pilot-receipt"),
        available_bytes=MIN_FREE_BYTES,
        clock=lambda: NOW,
    )
    raw = layout.root / receipt.raw_path
    os.chmod(raw, 0o700)
    receipt_path = raw / "intake-receipt.json"
    os.chmod(receipt_path, 0o600)
    write_canonical_json(
        receipt_path,
        receipt.model_copy(update={"manifest_sha256": "f" * 64}),
        mode=0o400,
    )
    duplicate = _package(layout, "pilot-receipt")
    with pytest.raises(SecurityError, match="receipt disagrees"):
        accept_capture_session(layout, duplicate, available_bytes=MIN_FREE_BYTES)

    other = _package(layout, "pilot-stage-collision")
    stage = layout.directory("tmp") / f".intake-{other.name}-{os.getpid()}"
    stage.mkdir()
    with pytest.raises(SecurityError, match="staging path already exists"):
        accept_capture_session(layout, other, available_bytes=MIN_FREE_BYTES)


def test_receipt_requires_timezone_and_missing_parent_is_reported(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="timezone"):
        IntakeReceipt(
            session_id="pilot",
            manifest_sha256="0" * 64,
            source_qc_report_sha256="1" * 64,
            intake_qc_report_sha256="2" * 64,
            accepted_at=datetime(2026, 8, 17),
            raw_path="raw/" + "0" * 64,
        )
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    missing = layout.directory("incoming") / "missing-parent" / "pilot"
    with pytest.raises(SecurityError, match="unavailable"):
        accept_capture_session(layout, missing, available_bytes=MIN_FREE_BYTES)


def test_published_receipt_tamper_is_detected_by_raw_revalidation(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    receipt = accept_capture_session(
        layout,
        _package(layout, "pilot-raw-revalidate"),
        available_bytes=MIN_FREE_BYTES,
    )
    raw = layout.root / receipt.raw_path
    os.chmod(raw, 0o700)
    receipt_path = raw / "intake-receipt.json"
    os.chmod(receipt_path, 0o600)
    wrong = receipt.model_copy(update={"session_id": "some-other-session"})
    write_canonical_json(receipt_path, wrong, mode=0o400)

    with pytest.raises(SecurityError, match="receipt disagrees"):
        validate_capture_package(raw, allow_intake_receipt=True)

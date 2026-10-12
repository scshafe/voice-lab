from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from cole_voice_lab.audition import AuditionPlan, AuditionTrial
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.capture import CaptureContinuityEvidence
from cole_voice_lab.datasets import DatasetEntry
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import (
    RESERVED_PROMPT_LABELS,
    AudioFileEvidence,
    AudioQcMetrics,
    AudioSpec,
    CaptureDevice,
    CaptureManifest,
    DistributionByteEvidence,
    EnvironmentEvidence,
    ExecutedCodeEvidence,
    ExperimentRequest,
    ExperimentResult,
    FileByteEvidence,
    ModelLockEntry,
    Prompt,
    PromptSuite,
    QcReport,
    ReferenceEvidence,
    WorkerModelEvidence,
    WorkerPackageByteEvidence,
    require_cole_subject,
)
from cole_voice_lab.recording_contracts import (
    CaptureReservation,
    CaptureSealReceipt,
    PreparedCaptureGate,
    RecordingBuildIdentity,
    RecordingFrameRange,
    RecordingGroup,
    RecordingPackageEntry,
    RecordingPackageMap,
    RecordingTake,
)
from cole_voice_lab.recording_library import DesktopSessionEvidence, DesktopTakeEvidence
from cole_voice_lab.references import (
    AcceptedUtteranceBinding,
    BoundHeadphoneReview,
    HeadphoneReviewEvidence,
    JsonEvidenceBinding,
)
from cole_voice_lab.resources import resource_path


def audio_spec() -> AudioSpec:
    return AudioSpec(
        sample_rate_hz=48_000,
        channels=1,
        sample_width_bytes=3,
        encoding="pcm_s24le",
        frames=48_000,
        duration_seconds=1.0,
    )


def test_audio_spec_binds_encoding_duration_and_width() -> None:
    assert audio_spec().duration_seconds == 1.0
    with pytest.raises(ValidationError, match="sample width"):
        AudioSpec(
            sample_rate_hz=48_000,
            channels=1,
            sample_width_bytes=2,
            encoding="pcm_s24le",
            frames=48_000,
            duration_seconds=1.0,
        )
    with pytest.raises(ValidationError, match="duration"):
        AudioSpec(
            sample_rate_hz=48_000,
            channels=1,
            sample_width_bytes=3,
            encoding="pcm_s24le",
            frames=48_000,
            duration_seconds=2.0,
        )


def test_capture_device_preserves_legacy_avfoundation_default_and_evidence() -> None:
    legacy_payload = {
        "backend": "avfoundation",
        "device_id": "1",
        "display_name": "UMIK-1",
        "microphone_serial": None,
        "calibration_sha256": None,
    }
    implicit = CaptureDevice(device_id="1", display_name="UMIK-1")
    explicit = CaptureDevice.model_validate(legacy_payload)
    assert implicit == explicit
    assert explicit.model_dump(mode="json") == legacy_payload
    assert canonical_sha256(explicit) == canonical_sha256(legacy_payload)


def test_capture_device_accepts_coreaudio_uid_without_changing_metadata() -> None:
    payload = {
        "backend": "coreaudio",
        "device_id": "AppleUSBAudioEngine:miniDSP:UMIK-1:synthetic-uid:1,0",
        "display_name": "UMIK-1",
        "microphone_serial": "synthetic-serial",
        "calibration_sha256": "a" * 64,
    }
    device = CaptureDevice.model_validate(payload)
    assert device.model_dump(mode="json") == payload
    assert CaptureDevice.model_validate_json(device.model_dump_json()) == device


@pytest.mark.parametrize("backend", ["unknown", "CoreAudio", "", None, 1])
def test_capture_device_rejects_unknown_or_coerced_backend(backend: object) -> None:
    with pytest.raises(ValidationError):
        CaptureDevice.model_validate(
            {
                "backend": backend,
                "device_id": "synthetic-uid",
                "display_name": "UMIK-1",
            }
        )


def test_prompt_text_and_suite_policy_are_strict() -> None:
    fields = {
        "prompt_id": "prompt-1",
        "text": "Readable text.",
        "category": "neutral",
        "style": "neutral",
        "expected_seconds": 2.0,
    }
    with pytest.raises(ValidationError, match="non-whitespace"):
        Prompt(**(fields | {"text": " \t "}))
    with pytest.raises(ValidationError, match="non-whitespace"):
        Prompt(**(fields | {"style": " \n "}))

    prompt = Prompt(**fields)
    duplicate = prompt.model_copy()
    with pytest.raises(ValidationError, match="prompt IDs must be unique"):
        PromptSuite(
            suite_id="suite-1",
            purpose="capture-pilot",
            prompts=(prompt, duplicate),
        )
    with pytest.raises(ValidationError, match="holdout flags"):
        PromptSuite(
            suite_id="suite-1",
            purpose="held-out-evaluation",
            prompts=(prompt,),
        )
    held_out = prompt.model_copy(update={"holdout": True})
    assert PromptSuite(
        suite_id="suite-1",
        purpose="held-out-evaluation",
        prompts=(held_out,),
    ).prompts == (held_out,)


def test_audio_file_kind_specific_fields_are_strict() -> None:
    shared = {"path": "audio/example.wav", "sha256": "a" * 64, "size_bytes": 100}
    with pytest.raises(ValidationError, match="audio-format evidence"):
        AudioFileEvidence(kind="room-tone", **shared)
    with pytest.raises(ValidationError, match="prompt ID"):
        AudioFileEvidence(kind="utterance", audio=audio_spec(), **shared)
    with pytest.raises(ValidationError, match="only utterances"):
        AudioFileEvidence(
            kind="calibration",
            prompt_id="prompt-1",
            transcript="Not allowed.",
            **shared,
        )
    calibration = AudioFileEvidence(kind="calibration", **shared)
    assert calibration.audio is None


def test_capture_manifest_requires_room_tone_and_unique_prompts() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    utterance = AudioFileEvidence(
        path="audio/neutral-01.wav",
        kind="utterance",
        sha256="a" * 64,
        size_bytes=100,
        audio=audio_spec(),
        prompt_id="neutral-01",
        transcript="A safe example.",
    )
    fields = {
        "session_id": "pilot-001",
        "subject_id": "cole-self",
        "prompt_suite_id": "cole-pilot-v1",
        "capture_host": "capture-mac",
        "captured_at": now,
        "finalized_at": now,
        "capture_device": CaptureDevice(device_id="UMIK-1", display_name="UMIK-1"),
        "files": (utterance,),
    }
    with pytest.raises(ValidationError, match="room tone"):
        CaptureManifest(**fields)

    room = AudioFileEvidence(
        path="audio/room.wav",
        kind="room-tone",
        sha256="b" * 64,
        size_bytes=100,
        audio=audio_spec(),
    )
    manifest = CaptureManifest(**(fields | {"files": (room, utterance)}))
    assert manifest.session_id == "pilot-001"

    duplicate = utterance.model_copy(update={"path": "audio/neutral-01-retake.wav"})
    with pytest.raises(ValidationError, match="duplicate prompt"):
        CaptureManifest(**(fields | {"files": (room, utterance, duplicate)}))

    with pytest.raises(ValidationError, match="finalization"):
        CaptureManifest(
            **(
                fields
                | {
                    "captured_at": now,
                    "finalized_at": datetime(2026, 8, 16, tzinfo=UTC),
                    "files": (room,),
                }
            )
        )
    with pytest.raises(ValidationError, match="file paths must be unique"):
        CaptureManifest(**(fields | {"files": (room, room)}))
    with pytest.raises(ValidationError, match="signal processing"):
        CaptureManifest(**(fields | {"files": (room,), "processing_applied": ("normalize",)}))
    with pytest.raises(ValidationError, match="explicit timezone"):
        CaptureManifest(**(fields | {"captured_at": datetime(2026, 8, 17)}))


def test_only_coles_subject_passes_the_pin() -> None:
    require_cole_subject("cole-self", what="the reference")
    for other in ("someone-else", "cole", "cole-physical-diagnostic-1"):
        with pytest.raises(SecurityError, match="not 'cole-self'"):
            require_cole_subject(other, what="the reference")


def test_reference_evidence_names_no_consent_but_reads_one_written_before() -> None:
    fields: dict[str, object] = {
        "path": "/private/references/ref.wav",
        "sha256": "a" * 64,
        "transcript": "Words.",
        "subject_id": "cole-self",
    }
    new = ReferenceEvidence.model_validate(fields)
    assert new.consent_path is None and "consent_path" not in new.model_dump(mode="json")
    stored = fields | {"consent_path": "/private/consent.json", "consent_sha256": "b" * 64}
    old = ReferenceEvidence.model_validate(stored)
    assert old.model_dump(mode="json") == stored
    with pytest.raises(ValidationError, match="present together"):
        ReferenceEvidence.model_validate(fields | {"consent_path": "/private/consent.json"})
    with pytest.raises(ValidationError, match="never null"):
        ReferenceEvidence.model_validate(fields | {"consent_path": None, "consent_sha256": None})
    with pytest.raises(ValidationError, match="absolute"):
        ReferenceEvidence.model_validate(stored | {"consent_path": "consent.json"})


def test_voice_conversion_and_direct_tts_inputs_are_distinct() -> None:
    reference = ReferenceEvidence(
        path="/private/references/ref.wav",
        sha256="a" * 64,
        transcript="Reference words.",
        subject_id="cole-self",
    )
    shared = {
        "experiment_id": "experiment-1",
        "trial_id": "trial-1",
        "candidate_id": "seed-vc",
        "text": "Held out text.",
        "output_path": "/tmp/output.wav",
        "reference": reference,
        "model_id": "model/repo",
        "model_revision": "c" * 40,
        "code_revision": "d" * 40,
    }
    with pytest.raises(ValidationError, match="performed source"):
        ExperimentRequest(track="voice-conversion", **shared)
    with pytest.raises(ValidationError, match="cannot receive"):
        ExperimentRequest(
            track="direct-tts",
            source_audio_path="/tmp/source.wav",
            **shared,
        )
    with pytest.raises(ValidationError, match="checksum-bound"):
        ExperimentRequest(
            track="voice-conversion",
            source_audio_path="/tmp/source.wav",
            **shared,
        )
    ExperimentRequest(
        track="voice-conversion",
        source_audio_path="/tmp/source.wav",
        source_audio_sha256="e" * 64,
        source_audio_size_bytes=123,
        **shared,
    )


def test_qc_evidence_is_finite_unique_and_status_consistent() -> None:
    metric = AudioQcMetrics(
        path="audio/example.wav",
        duration_seconds=1.0,
        peak_dbfs=-6.0,
        rms_dbfs=-18.0,
        dc_offset=0.0,
        clipped_samples=0,
        longest_zero_run_ms=0.0,
        passed=True,
    )
    now = datetime(2026, 8, 17, tzinfo=UTC)
    QcReport(
        session_id="session-1",
        manifest_sha256="a" * 64,
        generated_at=now,
        status="pass",
        metrics=(metric,),
    )
    with pytest.raises(ValidationError, match="finite"):
        AudioQcMetrics.model_validate(metric.model_dump() | {"rms_dbfs": float("nan")})
    with pytest.raises(ValidationError, match="unique"):
        QcReport(
            session_id="session-1",
            manifest_sha256="a" * 64,
            generated_at=now,
            status="pass",
            metrics=(metric, metric),
        )
    with pytest.raises(ValidationError, match="every metric"):
        QcReport(
            session_id="session-1",
            manifest_sha256="a" * 64,
            generated_at=now,
            status="pass",
            metrics=(metric.model_copy(update={"passed": False}),),
        )
    with pytest.raises(ValidationError, match="at least one failed"):
        QcReport(
            session_id="session-1",
            manifest_sha256="a" * 64,
            generated_at=now,
            status="fail",
            metrics=(metric,),
        )
    with pytest.raises(ValidationError, match="explicit timezone"):
        QcReport(
            session_id="session-1",
            manifest_sha256="a" * 64,
            generated_at=datetime(2026, 8, 17),
            status="pass",
            metrics=(metric,),
        )


def test_model_lock_and_worker_paths_have_explicit_semantics() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ModelLockEntry(
            candidate_id="qwen-base-bf16",
            model_id="model/repo",
            model_revision="a" * 40,
            code_revision="b" * 40,
            resolved_at=datetime(2026, 8, 17),
            files={"model.safetensors": "c" * 64},
        )
    with pytest.raises(ValidationError, match="parent"):
        ModelLockEntry(
            candidate_id="qwen-base-bf16",
            model_id="model/repo",
            model_revision="a" * 40,
            code_revision="b" * 40,
            resolved_at=datetime(2026, 8, 17, tzinfo=UTC),
            files={"../model.safetensors": "c" * 64},
        )
    with pytest.raises(ValidationError, match="absolute"):
        ReferenceEvidence(
            path="references/ref.wav",
            sha256="a" * 64,
            transcript="Words.",
            subject_id="cole-self",
        )

    reference = ReferenceEvidence(
        path="/private/references/ref.wav",
        sha256="a" * 64,
        transcript="Words.",
        subject_id="cole-self",
    )
    with pytest.raises(ValidationError, match="execution paths"):
        ExperimentRequest(
            experiment_id="experiment-1",
            trial_id="trial-1",
            candidate_id="qwen-base-bf16",
            track="direct-tts",
            text="Text.",
            output_path="relative/output.wav",
            reference=reference,
            model_id="model/repo",
            model_revision="c" * 40,
            code_revision="d" * 40,
        )


def test_experiment_results_are_exact_success_or_failure() -> None:
    audio = audio_spec().model_copy(
        update={
            "sample_rate_hz": 24_000,
            "sample_width_bytes": 2,
            "encoding": "pcm_s16le",
            "frames": 24_000,
        }
    )
    environment = EnvironmentEvidence(
        python_version="3.12",
        platform="macOS",
        machine="arm64",
        dependency_fingerprint_sha256="d" * 64,
        dependency_evidence_path="evidence/trial.pip-freeze.txt",
    )
    success = {
        "trial_id": "trial-1",
        "candidate_id": "qwen-base-bf16",
        "ok": True,
        "output_path": "/private/output.wav",
        "output_sha256": "a" * 64,
        "output_bytes": 100,
        "audio": audio,
        "generation_seconds": 1.0,
        "realized_seed": 7,
        "environment": environment,
    }
    ExperimentResult(**success)
    with pytest.raises(ValidationError, match="no error"):
        ExperimentResult(**(success | {"error_message": "not actually successful"}))
    with pytest.raises(ValidationError, match="cannot carry"):
        ExperimentResult(
            **(
                success
                | {
                    "ok": False,
                    "error_code": "inference_failed",
                    "error_message": "failed",
                }
            )
        )

    failure = ExperimentResult(
        trial_id="trial-1",
        candidate_id="qwen-base-bf16",
        ok=False,
        output_path=None,
        error_code="inference_failed",
        error_message="failed",
    )
    assert failure.output_path is None
    with pytest.raises(ValidationError, match="structured error"):
        ExperimentResult(trial_id="trial-1", candidate_id="qwen-base-bf16", ok=False)
    with pytest.raises(ValidationError, match="output paths"):
        ExperimentResult(**(success | {"output_path": "relative/output.wav"}))


def _file_evidence() -> FileByteEvidence:
    return FileByteEvidence(sha256="a" * 64, size_bytes=1)


def _distribution_evidence() -> DistributionByteEvidence:
    return DistributionByteEvidence(version="1.0", files={"package/module.py": _file_evidence()})


def _executed_code_payload() -> dict[str, object]:
    return {
        "worker_package": WorkerPackageByteEvidence(files={"workers/common.py": _file_evidence()}),
        "distributions": {"cole-voice-lab": _distribution_evidence()},
    }


def test_executed_code_evidence_requires_safe_complete_names_and_integrity() -> None:
    with pytest.raises(ValidationError, match="workers/common.py"):
        WorkerPackageByteEvidence(files={"workers/qwen.py": _file_evidence()})
    with pytest.raises(ValidationError, match="printable"):
        DistributionByteEvidence(version=" \t", files={"module.py": _file_evidence()})
    with pytest.raises(ValidationError, match="file names"):
        DistributionByteEvidence(version="1.0", files={"bad\x00name": _file_evidence()})

    payload = _executed_code_payload()
    with pytest.raises(ValidationError, match="distribution names"):
        ExecutedCodeEvidence(
            **(
                payload
                | {
                    "distributions": {"bad\x00name": _distribution_evidence()},
                    "integrity_sha256": "a" * 64,
                }
            )
        )
    with pytest.raises(ValidationError, match="cole-voice-lab distribution"):
        ExecutedCodeEvidence(
            **(
                payload
                | {
                    "distributions": {"other": _distribution_evidence()},
                    "integrity_sha256": "a" * 64,
                }
            )
        )
    with pytest.raises(ValidationError, match="integrity digest"):
        ExecutedCodeEvidence(**(payload | {"integrity_sha256": "a" * 64}))

    serialized = {
        "schema_version": "cole-voice-lab.executed-code-evidence/v1",
        "worker_package": payload["worker_package"].model_dump(mode="json"),  # type: ignore[union-attr]
        "distributions": {"cole-voice-lab": _distribution_evidence().model_dump(mode="json")},
    }
    evidence = ExecutedCodeEvidence(
        **(payload | {"integrity_sha256": canonical_sha256(serialized)})
    )
    assert evidence.integrity_sha256 == canonical_sha256(serialized)


def _worker_model_evidence_fields() -> dict[str, object]:
    return {
        "candidate_id": "qwen-base-bf16",
        "model_id": "model/repo",
        "model_revision": "a" * 40,
        "code_revision": "b" * 40,
        "model_manifest_sha256": "c" * 64,
        "auxiliary_manifest_sha256": {},
        "code_checkout_integrity_sha256": None,
        "executed_code_evidence_path": "evidence/executed-code.json",
        "executed_code_evidence_sha256": "d" * 64,
        "reference_sha256": "e" * 64,
        "source_audio_sha256": None,
        "source_audio_size_bytes": None,
        "settings": {},
        "runtime_package": "mlx-audio",
        "runtime_package_version": "0.5.0",
    }


def test_worker_model_evidence_rejects_unsafe_or_partial_identity() -> None:
    fields = _worker_model_evidence_fields()
    WorkerModelEvidence(**fields)
    with pytest.raises(ValidationError, match="repository names"):
        WorkerModelEvidence(**(fields | {"auxiliary_manifest_sha256": {"bad\n": "a" * 64}}))
    with pytest.raises(ValidationError, match="runtime package identity"):
        WorkerModelEvidence(**(fields | {"runtime_package": " \t"}))
    with pytest.raises(ValidationError, match="present together"):
        WorkerModelEvidence(**(fields | {"source_audio_sha256": "a" * 64}))
    with pytest.raises(ValidationError, match="must be omitted"):
        WorkerModelEvidence(**(fields | {"watermarked": None}))
    # A consent digest is evidence written before D-050 only: present, or omitted, never null.
    assert "consent_sha256" not in WorkerModelEvidence(**fields).model_dump(mode="json")
    stored = WorkerModelEvidence(**(fields | {"consent_sha256": "f" * 64}))
    assert stored.model_dump(mode="json")["consent_sha256"] == "f" * 64
    with pytest.raises(ValidationError, match="never null"):
        WorkerModelEvidence(**(fields | {"consent_sha256": None}))


# TM1: goldens from docs/PROMPT_CATALOG_V2.md §2.5, and canonical digests of synthetic
# v1 evidence captured on the tree before prompt-suite/v2 fields existed.
_RESOURCE_GOLDENS = {
    "pilot-v1.json": (
        "ed188aad76a9f0741acbb93b654bca24574b37b90bfa5b4836f4158cdf155b93",
        "eac118a995a1e184d5ed20b9ee23c3e0924c272b28d87308df2abeee1f8772d0",
    ),
    "evaluation-v1.json": (
        "559898769afd88ba749f78c880f9914440b643783bded09b3ebbb1dd7e267a1b",
        "2854decad263e9e82a131f130782ff7c846d4ffd14c21eab87cab8cca2e6caf2",
    ),
}
_PILOT_V1_ROOM_TONE_SHA256 = {
    "room-tone-start": "b59836ab0b8c1e2ede111471c9f69571d38fc1270d88faa7138e32ea7c5c5664",
    "room-tone-end": "6bb832ddefda164d24fe2414e8e7e1e0ece9e8413e51e0cca1539351e6401798",
}
_PRE_V2_EVIDENCE_SHA256 = {
    "audition-plan": "d0fafcabbe675e82041b4a35d31e33e2bfb5f16de12967e894de2cc4b8bf245e",
    "capture-reservation": "a33cb2a6b21780d31d697a3c4827f997fc9c27d6dad38397daceaaec0f4a2693",
    "dataset-entry": "6347d12353d848b46095eac9a28f5e4c6e8496b15bdc9f9336ed69bba8140c2d",
    "desktop-session-sidecar": "a3923ac87643e23668e062ab22f52528914b5e8b9aefdf6380212b97f35c40ee",
    "recording-group": "5f8469d6788ee9db0e6727fe243ad2094861331406c206e72ea7cd02614c1bbc",
    "recording-package-map": "c6be22ebbc046880051e03062563c12a4552e422898de45b539400f0be9bed22",
    "recording-take": "b20d132dcee23262a93b4b3413ab4b1b01aaa97e3b3d91b99a588e58f2e5cda1",
}


def _hash_stability_instances() -> dict[str, BaseModel]:
    """Deterministic v1 evidence; digests below were captured before prompt-suite/v2 existed."""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    spoken = Prompt(
        prompt_id="stable-spoken",
        text="A deterministic synthetic sentence.",
        category="neutral",
        style="Even, relaxed delivery.",
        reference_candidate=True,
        expected_seconds=4.0,
    )
    room = Prompt(
        prompt_id="stable-room",
        text="Remain still and silent.",
        category="room-tone",
        style="Silence; keep the recording position unchanged.",
        expected_seconds=15.0,
    )
    catalog = PromptSuite(suite_id="stable-pilot", purpose="capture-pilot", prompts=(room, spoken))
    device = CaptureDevice(backend="coreaudio", device_id="stable-uid", display_name="Stable")
    group = RecordingGroup(
        group_id="stable-group",
        context_revision=0,
        subject_id="stable-subject",
        catalog=catalog,
        catalog_sha256=canonical_sha256(catalog),
        capture_device=device,
        capture_host="stable-host",
        opened_at=now,
        reason="app-start",
        gate=PreparedCaptureGate(state="not-required"),
        room_tone_take_ids=("stable-room-take",),
    )
    reservation = CaptureReservation(
        take_id="stable-take",
        operation_id="stable-reserve",
        process_generation="stable-gen",
        group=group,
        prompt=spoken,
        requested_at=now,
        partial_path="tmp/recording-takes/stable-take/capture.partial.wav",
        original_path="tmp/recording-takes/stable-take/original.wav",
    )
    audio = AudioSpec(
        sample_rate_hz=48_000,
        channels=1,
        sample_width_bytes=3,
        encoding="pcm_s24le",
        frames=48_000,
        duration_seconds=1.0,
    )
    seal = CaptureSealReceipt(
        take_id=reservation.take_id,
        reservation_sha256=canonical_sha256(reservation),
        process_generation=reservation.process_generation,
        recorder_build=RecordingBuildIdentity(
            build_id="stable-build",
            source_sha256="1" * 64,
            executable_sha256="2" * 64,
            environment_sha256="3" * 64,
        ),
        original_path=reservation.original_path,
        original_sha256="4" * 64,
        original_size_bytes=144_044,
        audio=audio,
        requested_at=now,
        first_sample_at=now,
        last_sample_at=now + timedelta(seconds=1),
        sealed_at=now + timedelta(seconds=2),
        continuity=CaptureContinuityEvidence(
            schema_version="cole-voice-lab.capture-continuity/v1",
            backend="coreaudio-audioqueue",
            device_uid=device.device_id,
            output_name="original.wav",
            wav_sha256="4" * 64,
            wav_size_bytes=144_044,
            frames=48_000,
            received_frames=48_000,
            callback_count=12,
            first_sample_time=100.0,
            last_sample_end=48_100.0,
            success=True,
            continuity_checked=True,
            stop_reason="requested",
        ),
    )
    take = RecordingTake(
        reservation=reservation,
        seal=seal,
        transport="draft",
        decision="draft",
        decision_revision=0,
        analysis="pending",
        export="unprepared",
        edit_revision=0,
        frame_range=RecordingFrameRange(source_frames=48_000, start_frame=0, end_frame=48_000),
        state_revision=2,
    )
    package = RecordingPackageMap(
        session_id="stable-session",
        manifest_sha256="5" * 64,
        package_path="tmp/capture/stable-session",
        group=group,
        selected_suite=catalog,
        entries=(
            RecordingPackageEntry(
                take_id="stable-room-take",
                group_id=group.group_id,
                subject_id=group.subject_id,
                catalog_sha256=group.catalog_sha256,
                prompt=room,
                package_relative_path="room-tone-start.wav",
                original_sha256="3" * 64,
                recorded_at=now,
                edit_identity_sha256="2" * 64,
            ),
            RecordingPackageEntry(
                take_id=reservation.take_id,
                group_id=group.group_id,
                subject_id=group.subject_id,
                catalog_sha256=group.catalog_sha256,
                prompt=spoken,
                package_relative_path="stable-spoken.wav",
                original_sha256=seal.original_sha256,
                recorded_at=now,
                edit_identity_sha256="6" * 64,
            ),
        ),
        registered_at=now + timedelta(seconds=3),
    )
    sidecar = DesktopSessionEvidence(
        session_id="stable-desktop-session",
        subject_id="stable-subject",
        catalog_suite=catalog,
        catalog_sha256=canonical_sha256(catalog),
        selected_suite=catalog,
        session_started_at=now,
        capture_device=CaptureDevice(device_id="1", display_name="Stable"),
        capture_host="stable-host",
        takes=(
            DesktopTakeEvidence(
                prompt_id=spoken.prompt_id,
                take_started_at=now + timedelta(seconds=1),
                capture_device=CaptureDevice(device_id="1", display_name="Stable"),
                audio_sha256="7" * 64,
                accepted=True,
            ),
        ),
    )
    manifest_sha256 = "8" * 64
    source = AcceptedUtteranceBinding(
        raw_session_path=f"raw/{manifest_sha256}",
        raw_manifest_sha256=manifest_sha256,
        raw_audio_path=f"raw/{manifest_sha256}/stable-spoken.wav",
        raw_audio_sha256="9" * 64,
        raw_audio_size_bytes=144_044,
        audio=audio,
        session_id="stable-session",
        subject_id="stable-subject",
        prompt_suite_id=catalog.suite_id,
        prompt_id=spoken.prompt_id,
        transcript=spoken.text,
        source_qc_report_sha256="a" * 64,
    )
    dataset_entry = DatasetEntry(
        split="train",
        source=source,
        prompt_suite_sha256=canonical_sha256(catalog),
        prompt_suite_purpose="capture-pilot",
        prompt_suite=catalog,
        prompt=spoken,
        headphone_review=BoundHeadphoneReview(
            file=JsonEvidenceBinding(
                path="provenance/headphone-reviews/stable.json", sha256="b" * 64, size_bytes=512
            ),
            record=HeadphoneReviewEvidence(
                review_id="stable-review",
                reviewer_id="stable-reviewer",
                session_id=source.session_id,
                prompt_id=source.prompt_id,
                source_sha256=source.raw_audio_sha256,
                reviewed_at=now,
                headphones_used=True,
                transcript_matches=True,
                audio_quality_passed=True,
            ),
        ),
    )
    held_out = Prompt(
        prompt_id="stable-held-out",
        text="Held out synthetic words.",
        category="technical",
        style="Neutral held-out delivery.",
        holdout=True,
        expected_seconds=2.0,
    )
    evaluation = PromptSuite(
        suite_id="stable-held-out", purpose="held-out-evaluation", prompts=(held_out,)
    )
    request = ExperimentRequest(
        experiment_id="stable-experiment",
        trial_id="stable-trial",
        candidate_id="stable-candidate",
        track="direct-tts",
        text=held_out.text,
        output_path="/private/stable/native/stable-trial.wav",
        reference=ReferenceEvidence(
            path="/private/stable/reference.wav",
            sha256="c" * 64,
            transcript="Reference words.",
            subject_id="stable-subject",
            consent_path="/private/stable/consent.json",
            consent_sha256="d" * 64,
        ),
        seed=7,
        model_id="stable/model",
        model_revision="e" * 40,
        code_revision="f" * 40,
        settings={"profile": "smoke", "timing_mode": "cold", "prompt_id": held_out.prompt_id},
    )
    audition_plan = AuditionPlan(
        experiment_id="stable-experiment",
        profile="smoke",
        created_at=now,
        prompt_suite=evaluation,
        prompt_suite_sha256=canonical_sha256(evaluation),
        selected_prompt_ids=(held_out.prompt_id,),
        trials=(
            AuditionTrial(
                trial_id="stable-trial",
                candidate_id="stable-candidate",
                track="direct-tts",
                profile="smoke",
                timing_mode="cold",
                runtime_id="stable-runtime",
                runtime_lock_sha256="0" * 64,
                request_sha256=canonical_sha256(request),
                request=request,
                normalized_output_path="/private/stable/normalized/stable-trial.wav",
            ),
        ),
    )
    return {
        "recording-group": group,
        "capture-reservation": reservation,
        "recording-take": take,
        "recording-package-map": package,
        "desktop-session-sidecar": sidecar,
        "dataset-entry": dataset_entry,
        "audition-plan": audition_plan,
    }


def test_v1_resource_and_evidence_bytes_are_unchanged_by_prompt_suite_v2() -> None:
    for name, (file_sha256, suite_sha256) in _RESOURCE_GOLDENS.items():
        raw = resource_path(name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == file_sha256
        suite = PromptSuite.model_validate_json(raw)
        assert canonical_sha256(suite) == suite_sha256
        assert suite.schema_version == "cole-voice-lab.prompt-suite/v1"
    pilot = PromptSuite.model_validate_json(resource_path("pilot-v1.json").read_bytes())
    assert {
        prompt.prompt_id: canonical_sha256(prompt)
        for prompt in pilot.prompts
        if prompt.category == "room-tone"
    } == _PILOT_V1_ROOM_TONE_SHA256
    instances = _hash_stability_instances()
    assert {name: canonical_sha256(value) for name, value in instances.items()} == (
        _PRE_V2_EVIDENCE_SHA256
    )
    for value in instances.values():
        encoded = canonical_json_bytes(value)
        assert b'"labels"' not in encoded and b'"instructions"' not in encoded
        assert canonical_json_bytes(type(value).model_validate_json(encoded)) == encoded


def _v2_prompt(**changes: object) -> dict[str, object]:
    return {
        "prompt_id": "spoken",
        "text": "Read these synthetic words.",
        "category": "neutral",
        "style": "Natural pace",
        "expected_seconds": 4.0,
    } | changes


def _v2_room(**changes: object) -> dict[str, object]:
    return {
        "prompt_id": "room",
        "instructions": "Remain still and silent.",
        "category": "room-tone",
        "style": "Silence",
        "expected_seconds": 15.0,
    } | changes


def _v2_step(**changes: object) -> dict[str, object]:
    return {
        "prompt_id": "step",
        "instructions": "Follow the synthetic step.",
        "category": "procedure",
        "style": "Follow the steps",
        "expected_seconds": 120.0,
        "labels": ["test"],
    } | changes


def _catalog(
    *prompts: dict[str, object],
    version: str = "v2",
    suite_id: str = "synthetic-v2",
    purpose: str = "capture-pilot",
) -> PromptSuite:
    return PromptSuite.model_validate_json(
        json.dumps(
            {
                "schema_version": f"cole-voice-lab.prompt-suite/{version}",
                "suite_id": suite_id,
                "purpose": purpose,
                "prompts": list(prompts),
            }
        )
    )


def _physical(*prompts: dict[str, object]) -> PromptSuite:
    return _catalog(*prompts, suite_id="cole-physical-test-synthetic", purpose="physical-test")


@pytest.mark.parametrize(
    "prompt,purpose",
    [
        (_v2_prompt(labels=["warm-up"]), "capture-pilot"),
        (_v2_prompt(instructions="Extra"), "capture-pilot"),
        (_v2_room(), "capture-pilot"),
        (_v2_step(labels=[]), "capture-pilot"),
        (_v2_prompt(), "physical-test"),
    ],
    ids=["labels", "instructions", "absent-text", "procedure", "physical-test"],
)
def test_prompt_suite_v1_remains_exact(prompt: dict[str, object], purpose: str) -> None:
    """TM2."""
    with pytest.raises(ValidationError):
        _catalog(prompt, version="v1", suite_id="cole-physical-test-v1", purpose=purpose)
    assert _catalog(_v2_prompt(labels=[], text="Kept.", instructions=None), version="v1")


def test_absent_v2_fields_parse_and_serialize_as_absent() -> None:
    explicit = _catalog(_v2_prompt(labels=[], instructions=None), _v2_room(text=None, labels=[]))
    implicit = _catalog(_v2_prompt(), _v2_room())
    assert explicit == implicit
    assert canonical_json_bytes(explicit) == canonical_json_bytes(implicit)
    assert b"null" not in canonical_json_bytes(explicit)
    assert b'"labels"' not in canonical_json_bytes(explicit)
    reparsed = PromptSuite.model_validate_json(canonical_json_bytes(explicit))
    assert canonical_json_bytes(reparsed) == canonical_json_bytes(explicit)
    assert canonical_json_bytes(explicit.prompts[0].model_dump(mode="python")) == (
        canonical_json_bytes(explicit.prompts[0])
    )


def test_prompt_helpers_key_behavior_on_category_only() -> None:
    catalog = _physical(_v2_room(labels=["test"]), _v2_step(), _v2_prompt(labels=["test"]))
    room, step, spoken = catalog.prompts
    legacy_room = Prompt(
        prompt_id="legacy-room",
        text="Remain silent.",
        category="room-tone",
        style="Silence",
        expected_seconds=15.0,
    )
    assert [p.spoken for p in (room, step, spoken, legacy_room)] == [False, False, True, False]
    assert [p.allows_quiet for p in (room, step, spoken, legacy_room)] == [True, True, False, True]
    assert [p.content for p in (room, step, spoken, legacy_room)] == [
        "Remain still and silent.",
        "Follow the synthetic step.",
        "Read these synthetic words.",
        "Remain silent.",
    ]
    assert legacy_room.require_text() == "Remain silent."
    for silent in (room, step):
        with pytest.raises(ContractError, match="no spoken text"):
            silent.require_text()
    with pytest.raises(ContractError, match="no content"):
        assert not Prompt(
            prompt_id="bare", category="room-tone", style="Silence", expected_seconds=1.0
        ).content


@pytest.mark.parametrize("token", sorted(RESERVED_PROMPT_LABELS))
def test_every_reserved_label_is_refused(token: str) -> None:
    """TM3: P4 reserved tokens."""
    with pytest.raises(ValidationError, match="reserved"):
        _catalog(_v2_prompt(labels=[token]))


@pytest.mark.parametrize(
    "build,message",
    [
        (lambda: _catalog(_v2_prompt(instructions=" \n ")), "non-whitespace"),
        (lambda: _catalog(_v2_prompt(text=None)), "may omit spoken text"),
        (lambda: _physical(_v2_step(text="Words")), "procedure prompts require"),
        (lambda: _physical(_v2_step(instructions=None)), "procedure prompts require"),
        (lambda: _catalog(_v2_prompt(labels=["warm-up", "warm-up"])), "unique"),
        (lambda: _catalog(_v2_room(text="Remain silent.")), "room tone requires"),
        (lambda: _catalog(_v2_room(instructions=None)), "room tone requires"),
        (lambda: _catalog(_v2_step(labels=[]), purpose="physical-test"), "prefix must agree"),
        (lambda: _catalog(_v2_prompt(), suite_id="cole-physical-test-x"), "prefix must agree"),
        (lambda: _physical(_v2_step(labels=["recovery"])), "test label"),
        (
            lambda: _physical(_v2_prompt(labels=["test"], reference_candidate=True)),
            "test label",
        ),
        (lambda: _catalog(_v2_prompt(labels=["test"])), "only physical-test"),
        (lambda: _catalog(_v2_room(), _v2_step(labels=[])), "only physical-test"),
        (lambda: _catalog(_v2_prompt(), _v2_prompt()), "prompt IDs must be unique"),
        (lambda: _physical(_v2_step(holdout=True)), "holdout flags"),
        (lambda: _catalog(_v2_prompt(text="Read\u200b this.")), "format or control"),
        (lambda: _catalog(_v2_room(instructions="Stay\u202e silent.")), "format or control"),
        (lambda: _catalog(_v2_prompt(style="Natural\tpace")), "format or control"),
        (lambda: _catalog(_v2_prompt(labels=["x"])), "at least 2"),
        (lambda: _catalog(_v2_prompt(labels=["a" * 33])), "at most 32"),
        (lambda: _catalog(_v2_prompt(labels=["Warm-up"])), "pattern"),
        (lambda: _catalog(_v2_prompt(labels=["warm-"])), "pattern"),
        (lambda: _catalog(_v2_prompt(labels=[f"label{i}" for i in range(9)])), "at most 8"),
        (lambda: _catalog(_v2_prompt(instructions="x" * 2001)), "at most 2000"),
        (lambda: _catalog(_v2_prompt(text="")), "at least 1"),
    ],
)
def test_prompt_suite_v2_rules_fail_closed(build: Any, message: str) -> None:
    """TM3: P1-P4, S2-S5, S7 and field bounds."""
    with pytest.raises(ValidationError, match=message):
        build()


def test_prompt_suite_v2_accepts_its_boundaries() -> None:
    catalog = _catalog(
        _v2_prompt(
            text="First line.\nSecond line.",
            labels=["ab", "a" * 32, *[f"label{i}" for i in range(6)]],
            instructions="x" * 2000,
        ),
        _v2_room(labels=["warm-up"]),
    )
    assert len(catalog.prompts[0].labels) == 8
    physical = _physical(_v2_room(labels=["test"]), _v2_step(labels=["test", "recovery"]))
    assert physical.purpose == "physical-test"
    assert _catalog(_v2_prompt(text="Legacy\u200b bytes."), version="v1")

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.models import (
    AudioFileEvidence,
    AudioQcMetrics,
    AudioSpec,
    CaptureDevice,
    CaptureManifest,
    ConsentRecord,
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
)


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


def test_consent_cannot_be_approved_without_attestation() -> None:
    with pytest.raises(ValidationError, match="attestation"):
        ConsentRecord(
            consent_id="consent-1",
            subject_id="cole-self",
            approved=True,
            attestation="pending-subject-attestation",
            allowed_purposes=("private-zero-shot-evaluation",),
            allowed_execution_boundaries=("tailnet-local-host",),
            hosted_transfer_allowed=False,
            disclosure_required=True,
            effective_at=datetime(2026, 8, 17, tzinfo=UTC),
        )


def test_consent_contract_rejects_incomplete_duplicate_and_revoked_approval() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    base = {
        "consent_id": "consent-1",
        "subject_id": "cole-self",
        "approved": True,
        "attestation": "self-recorded-and-self-authorized",
        "allowed_purposes": ("private-zero-shot-evaluation",),
        "allowed_execution_boundaries": ("tailnet-local-host",),
        "effective_at": now,
        "expires_at": datetime(2026, 9, 17, tzinfo=UTC),
    }
    with pytest.raises(ValidationError, match="effective timestamp"):
        ConsentRecord(**(base | {"effective_at": None}))
    with pytest.raises(ValidationError, match="explicit expiry"):
        ConsentRecord(**(base | {"expires_at": None}))
    with pytest.raises(ValidationError, match="purposes must be unique"):
        ConsentRecord(
            **(
                base
                | {
                    "approved": False,
                    "allowed_purposes": (
                        "private-zero-shot-evaluation",
                        "private-zero-shot-evaluation",
                    ),
                }
            )
        )
    with pytest.raises(ValidationError, match="boundaries must be unique"):
        ConsentRecord(
            **(
                base
                | {
                    "approved": False,
                    "allowed_execution_boundaries": (
                        "tailnet-local-host",
                        "tailnet-local-host",
                    ),
                }
            )
        )
    with pytest.raises(ValidationError, match="cannot remain approved"):
        ConsentRecord(**(base | {"revoked_at": now}))
    with pytest.raises(ValidationError, match="explicit timezone"):
        ConsentRecord(**(base | {"effective_at": datetime(2026, 8, 17)}))


def test_voice_conversion_and_direct_tts_inputs_are_distinct() -> None:
    reference = ReferenceEvidence(
        path="/private/references/ref.wav",
        sha256="a" * 64,
        transcript="Reference words.",
        subject_id="cole-self",
        consent_path="/private/provenance/consent.json",
        consent_sha256="b" * 64,
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
            consent_path="/private/consent.json",
            consent_sha256="b" * 64,
        )

    reference = ReferenceEvidence(
        path="/private/references/ref.wav",
        sha256="a" * 64,
        transcript="Words.",
        subject_id="cole-self",
        consent_path="/private/consent.json",
        consent_sha256="b" * 64,
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
        "consent_sha256": "f" * 64,
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

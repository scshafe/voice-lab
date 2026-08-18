from __future__ import annotations

import io
import json
import os
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_blind import _audition_inputs

from cole_voice_lab.audition import (
    AuditionPlan,
    AuditionRunManifest,
    PersistedSourceEvidence,
    PersistedTrialResult,
    PersistedTrialSourceEvidence,
    RunResultPointer,
    audition_paths,
)
from cole_voice_lab.blind import (
    ListeningScore,
    PublicReviewManifest,
    ReviewIndex,
    ScoreSheet,
    aggregate_scores,
    blind_inputs_from_audition,
    build_review_package,
)
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
    write_canonical_json,
)
from cole_voice_lab.cli import main
from cole_voice_lab.models import ConsentRecord, ModelLockEntry, WorkerModelEvidence
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.promotion import (
    AssetFileBinding,
    BoundBlindPackageEvidence,
    BoundConsentEvidence,
    BoundListeningSummaryEvidence,
    BoundModelLockEvidence,
    BoundProvenanceEvidence,
    BoundScoreSheetEvidence,
    BoundSourceManifestEvidence,
    BoundVerdictEvidence,
    BoundWinningRunEvidence,
    ChloeSpeechHandoff,
    HumanReferenceBinding,
    HumanReferenceProvenanceRecord,
    JsonEvidenceFile,
    LabVerdictRecord,
    ListeningSummaryAggregate,
    PromotionDisclosure,
    PromotionLicense,
    PromotionListeningSummaryRecord,
    PromotionManifest,
    PromotionValidationError,
    WinningRunRecord,
    build_chloespeech_handoff,
    validate_promotion,
)
from cole_voice_lab.runtime import SOURCE_MANIFEST_NAME, LocalSourceManifest

OUTPUT_SHA256_PLACEHOLDER = "d" * 64
BASE_TIME = datetime(2026, 8, 17, tzinfo=UTC)
AS_OF = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _wav_payload(*, sample: int = 1_000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24_000)
        writer.writeframes(sample.to_bytes(2, "little", signed=True) * 2_400)
    return stream.getvalue()


def _json_binding(root: Path, relative: str, record: object) -> JsonEvidenceFile:
    path = root / relative
    digest = write_canonical_json(path, record)
    return JsonEvidenceFile(path=relative, sha256=digest, size_bytes=path.stat().st_size)


def _existing_json_binding(root: Path, relative: str) -> JsonEvidenceFile:
    path = root / relative
    digest, size = sha256_file(path)
    return JsonEvidenceFile(path=relative, sha256=digest, size_bytes=size)


def _asset_binding(root: Path, relative: str, payload: bytes) -> AssetFileBinding:
    path = root / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    digest, size = sha256_file(path)
    return AssetFileBinding(
        path=relative,
        sha256=digest,
        size_bytes=size,
        media_type="audio/wav",
    )


def _rebind_audition_consent(
    layout: AssetLayout,
    consent_file: JsonEvidenceFile,
    *,
    winning_candidate_id: str,
    winning_source_record: LocalSourceManifest,
    winning_model_manifest_sha256: str,
) -> None:
    paths = audition_paths(layout, "blind-audition")
    plan = AuditionPlan.model_validate_json(paths.plan_path.read_bytes())
    rebound_trials = []
    for trial in plan.trials:
        reference = trial.request.reference.model_copy(
            update={
                "consent_path": str(layout.root / consent_file.path),
                "consent_sha256": consent_file.sha256,
            }
        )
        request = trial.request.model_copy(update={"reference": reference})
        rebound_trials.append(
            trial.model_copy(
                update={"request": request, "request_sha256": canonical_sha256(request)}
            )
        )
    rebound_plan = plan.model_copy(update={"trials": tuple(rebound_trials)})
    plan_sha256 = write_canonical_json(paths.plan_path, rebound_plan)

    old_run = AuditionRunManifest.model_validate_json(paths.run_manifest_path.read_bytes())
    pointers: list[RunResultPointer] = []
    for trial in rebound_plan.trials:
        result_path = paths.results_directory / f"{trial.trial_id}.json"
        result = PersistedTrialResult.model_validate_json(result_path.read_bytes())
        output = Path(trial.request.output_path)
        model_path = output.parent / f"evidence/{trial.trial_id}.model-evidence.json"
        model_payload = json.loads(model_path.read_text(encoding="utf-8"))
        model_payload["consent_sha256"] = consent_file.sha256
        model_payload["watermarked"] = False
        if trial.candidate_id == winning_candidate_id:
            model_payload["model_manifest_sha256"] = winning_model_manifest_sha256
        model_sha256 = write_canonical_json(model_path, model_payload)
        metadata = dict(result.worker_result.metadata)
        metadata["consent_sha256"] = consent_file.sha256
        metadata["model_evidence_sha256"] = model_sha256
        metadata["watermarked"] = False
        if trial.candidate_id == winning_candidate_id:
            metadata["model_manifest_sha256"] = winning_model_manifest_sha256
        source_path = layout.root / str(result.source_evidence_path)
        source_evidence = PersistedTrialSourceEvidence.model_validate_json(source_path.read_bytes())
        primary = source_evidence.primary
        if trial.candidate_id == winning_candidate_id:
            integrity_record = {
                "schema_version": "cole-voice-lab.snapshot-integrity/v1",
                "source_id": winning_source_record.source_id,
                "repository": winning_source_record.repository,
                "revision": winning_source_record.revision,
                "manifest_sha256": winning_model_manifest_sha256,
                "files": winning_source_record.files,
            }
            primary = PersistedSourceEvidence(
                source_id=winning_source_record.source_id,
                kind="huggingface",
                repository=winning_source_record.repository,
                revision=winning_source_record.revision,
                manifest_sha256=winning_model_manifest_sha256,
                manifest=winning_source_record,
                files=winning_source_record.files,
                integrity_record=integrity_record,
                integrity_sha256=canonical_sha256(integrity_record),
            )
        source_evidence = source_evidence.model_copy(
            update={"request_sha256": trial.request_sha256, "primary": primary}
        )
        source_sha256 = write_canonical_json(source_path, source_evidence)
        metadata["source_evidence_sha256"] = source_sha256
        worker = result.worker_result.model_copy(update={"metadata": metadata})
        rebound = result.model_copy(
            update={
                "request_sha256": trial.request_sha256,
                "worker_result": worker,
                "source_evidence_sha256": source_sha256,
            }
        )
        result_sha256 = write_canonical_json(result_path, rebound)
        pointers.append(
            RunResultPointer(
                trial_id=trial.trial_id,
                candidate_id=trial.candidate_id,
                request_sha256=trial.request_sha256,
                outcome="success",
                result_path=result_path.relative_to(layout.root).as_posix(),
                result_sha256=result_sha256,
            )
        )
    write_canonical_json(
        paths.run_manifest_path,
        old_run.model_copy(update={"plan_sha256": plan_sha256, "results": tuple(pointers)}),
    )


def promotion_fixture(
    tmp_path: Path,
    *,
    expires_at: datetime | None = datetime(2027, 1, 1, tzinfo=UTC),
    reference_payload: bytes | None = None,
    output_payload: bytes | None = None,
) -> tuple[PromotionManifest, dict[str, Path]]:
    layout = AssetLayout.initialize(tmp_path)
    consent_record = ConsentRecord(
        consent_id="cole-private-rendering-v1",
        subject_id="cole-self",
        approved=True,
        attestation="self-recorded-and-self-authorized",
        allowed_purposes=("private-chloe-speech-rendering",),
        allowed_execution_boundaries=("capture-host", "tailnet-local-host"),
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=expires_at,
    )
    consent_file = _json_binding(
        tmp_path,
        "provenance/consents/cole-private-rendering-v1.json",
        consent_record,
    )
    consent = BoundConsentEvidence(file=consent_file, record=consent_record)

    winner_candidate_id = "qwen3-tts-base"
    _audition_inputs(layout, direct_candidates=(winner_candidate_id, "f5-tts"))
    initial_paths = audition_paths(layout, "blind-audition")
    initial_plan = AuditionPlan.model_validate_json(initial_paths.plan_path.read_bytes())
    initial_winner = next(
        trial for trial in initial_plan.trials if trial.candidate_id == winner_candidate_id
    )
    source_root = tmp_path / "models/sources/qwen3-tts-base-fixture"
    source_root.mkdir(mode=0o700, parents=True)
    model_file = source_root / "model.safetensors"
    model_file.write_bytes(b"model fixture")
    model_sha256, _ = sha256_file(model_file)
    source_record = LocalSourceManifest(
        source_id="qwen3-tts-base-fixture",
        kind="huggingface",
        repository=initial_winner.request.model_id,
        revision=initial_winner.request.model_revision,
        files={"model.safetensors": model_sha256},
    )
    source_relative = source_root.relative_to(tmp_path).as_posix()
    source_file = _json_binding(
        tmp_path,
        f"{source_relative}/{SOURCE_MANIFEST_NAME}",
        source_record,
    )
    model_source = BoundSourceManifestEvidence(file=source_file, record=source_record)
    _rebind_audition_consent(
        layout,
        consent_file,
        winning_candidate_id=winner_candidate_id,
        winning_source_record=source_record,
        winning_model_manifest_sha256=source_file.sha256,
    )
    inputs = blind_inputs_from_audition(layout, experiment_id="blind-audition")
    package = build_review_package(
        layout,
        review_id="blind-audition-review",
        inputs=inputs,
        key_hex="ab" * 32,
    )
    package_relative = package.root.relative_to(tmp_path).as_posix()
    package_file = _existing_json_binding(
        tmp_path,
        f"{package_relative}/package-manifest.json",
    )
    blind_package = BoundBlindPackageEvidence(file=package_file, record=package.manifest)
    reveal_by_id = {entry.blind_id: entry for entry in package.reveal.entries}
    score_record = ScoreSheet(
        review_id=package.index.review_id,
        review_index_sha256=package.review_index_sha256,
        public_manifest_sha256=package.public_manifest_sha256,
        reviewer_id="cole",
        scored_at=package.manifest.created_at + timedelta(minutes=1),
        headphones_used=True,
        scores=tuple(
            ListeningScore(
                blind_id=sample.blind_id,
                likeness=5
                if reveal_by_id[sample.blind_id].candidate_id == winner_candidate_id
                else 3,
                naturalness=5
                if reveal_by_id[sample.blind_id].candidate_id == winner_candidate_id
                else 3,
                prosody=5
                if reveal_by_id[sample.blind_id].candidate_id == winner_candidate_id
                else 3,
                intelligibility=5
                if reveal_by_id[sample.blind_id].candidate_id == winner_candidate_id
                else 3,
                artifact_freedom=5
                if reveal_by_id[sample.blind_id].candidate_id == winner_candidate_id
                else 3,
            )
            for sample in package.index.samples
        ),
    )
    score_file = _json_binding(
        tmp_path,
        "evaluations/promotion-evidence/blind-audition-review.scores.json",
        score_record,
    )
    score_sheet = BoundScoreSheetEvidence(file=score_file, record=score_record)
    aggregate = ListeningSummaryAggregate.model_validate(aggregate_scores(layout, score_record))
    summary_record = PromotionListeningSummaryRecord(
        review_id=package.index.review_id,
        blind_package_manifest_sha256=package_file.sha256,
        score_sheet_sha256=score_file.sha256,
        aggregate=aggregate,
    )
    summary_file = _json_binding(
        tmp_path,
        "evaluations/promotion-evidence/blind-audition-review.summary.json",
        summary_record,
    )
    listening_summary = BoundListeningSummaryEvidence(
        file=summary_file,
        record=summary_record,
    )
    winning_reveal = next(
        entry for entry in package.reveal.entries if entry.candidate_id == winner_candidate_id
    )

    audition = audition_paths(layout, "blind-audition")
    plan = AuditionPlan.model_validate_json(audition.plan_path.read_bytes())
    winning_trial = next(
        trial for trial in plan.trials if trial.trial_id == winning_reveal.trial_id
    )
    result_path = audition.results_directory / f"{winning_trial.trial_id}.json"
    result = PersistedTrialResult.model_validate_json(result_path.read_bytes())
    assert result.normalized is not None
    assert result.worker_result.environment is not None
    reference_path = Path(winning_trial.request.reference.path)
    reference_sha256, reference_size = sha256_file(reference_path)
    reference_relative = reference_path.relative_to(tmp_path).as_posix()
    reference = HumanReferenceBinding(
        path=reference_relative,
        sha256=reference_sha256,
        size_bytes=reference_size,
        subject_id="cole-self",
    )

    model_evidence_relative = result.worker_result.metadata["model_evidence_path"]
    assert isinstance(model_evidence_relative, str)
    worker_model_path = (
        Path(result.worker_result.output_path or "").parent / model_evidence_relative
    )
    executed_code_relative = result.worker_result.metadata["executed_code_evidence_path"]
    assert isinstance(executed_code_relative, str)
    executed_code_path = (
        Path(result.worker_result.output_path or "").parent / executed_code_relative
    )
    dependency_path = (
        Path(result.worker_result.output_path or "").parent
        / result.worker_result.environment.dependency_evidence_path
    )
    assert result.runtime_receipt_path is not None
    runtime_receipt_path = tmp_path / result.runtime_receipt_path
    worker_model_record = WorkerModelEvidence.model_validate_json(worker_model_path.read_bytes())
    worker_model_file = _existing_json_binding(
        tmp_path,
        worker_model_path.relative_to(tmp_path).as_posix(),
    )
    model_lock_record = ModelLockEntry(
        candidate_id=winning_trial.candidate_id,
        model_id=winning_trial.request.model_id,
        model_revision=winning_trial.request.model_revision,
        code_revision=winning_trial.request.code_revision,
        resolved_at=BASE_TIME,
        files={f"{source_relative}/model.safetensors": model_sha256},
    )
    model_lock_file = _json_binding(
        tmp_path,
        "models/qwen3-tts-base/model-lock.json",
        model_lock_record,
    )
    model_lock = BoundModelLockEvidence(
        file=model_lock_file,
        record=model_lock_record,
        model_source=model_source,
        worker_model_evidence=worker_model_file,
        worker_model_record=worker_model_record,
    )

    provenance_record = HumanReferenceProvenanceRecord(
        provenance_id="cole-reference-v1",
        subject_id="cole-self",
        reference_path=reference.path,
        reference_sha256=reference.sha256,
        consent_id=consent_record.consent_id,
        consent_evidence_sha256=consent_file.sha256,
        created_at=BASE_TIME,
        created_by="Cole Voice Lab test fixture",
        statement="Cole recorded and authorized this human-reference fixture.",
    )
    provenance_file = _json_binding(
        tmp_path,
        "provenance/cole-reference-v1.json",
        provenance_record,
    )
    provenance = BoundProvenanceEvidence(file=provenance_file, record=provenance_record)

    normalized_path = Path(result.normalized.path)
    output = AssetFileBinding(
        path=normalized_path.relative_to(tmp_path).as_posix(),
        sha256=result.normalized.sha256,
        size_bytes=result.normalized.size_bytes,
        media_type="audio/wav",
    )
    provider_voice = f"reference-sha256:{reference.sha256}"
    provider_settings: dict[str, Any] = {}
    run_record = WinningRunRecord(
        experiment_id=winning_reveal.experiment_id,
        trial_id=winning_reveal.trial_id,
        candidate_id=winning_reveal.candidate_id,
        plan_sha256=winning_reveal.plan_sha256,
        run_manifest_sha256=winning_reveal.run_manifest_sha256,
        result_sha256=winning_reveal.result_sha256,
        request_sha256=winning_reveal.request_sha256,
        provider_id="mlx-audio",
        provider_voice=provider_voice,
        model_id=model_lock_record.model_id,
        model_revision=model_lock_record.model_revision,
        code_revision=model_lock_record.code_revision,
        reference_sha256=reference.sha256,
        consent_evidence_sha256=consent_file.sha256,
        language=winning_trial.request.language,
        audition_settings_sha256=canonical_sha256(winning_trial.request.settings),
        provider_settings_sha256=canonical_sha256(provider_settings),
        environment_fingerprint_sha256=(
            result.worker_result.environment.dependency_fingerprint_sha256
        ),
        output=output,
        watermarked=False,
        completed_at=result.completed_at,
    )
    run_file = _json_binding(
        tmp_path,
        "evaluations/promotion-evidence/qwen-bf16-winning-run.json",
        run_record,
    )
    winning_run = BoundWinningRunEvidence(file=run_file, record=run_record)

    verdict_record = LabVerdictRecord(
        verdict_id="blind-audition-verdict",
        experiment_id=winning_reveal.experiment_id,
        review_id=package.index.review_id,
        winning_candidate_id=winning_reveal.candidate_id,
        winning_trial_id=winning_reveal.trial_id,
        reference_sha256=reference.sha256,
        winning_run_evidence_sha256=run_file.sha256,
        model_lock_evidence_sha256=model_lock_file.sha256,
        model_source_manifest_sha256=source_file.sha256,
        auxiliary_source_manifest_sha256={},
        worker_model_evidence_sha256=worker_model_file.sha256,
        blind_package_manifest_sha256=package_file.sha256,
        score_sheet_evidence_sha256=score_file.sha256,
        listening_summary_evidence_sha256=summary_file.sha256,
        winning_reveal_entry=winning_reveal,
        decided_at=package.manifest.created_at + timedelta(minutes=2),
    )
    verdict_file = _json_binding(
        tmp_path,
        "evaluations/promotion-evidence/verdict.json",
        verdict_record,
    )
    verdict = BoundVerdictEvidence(file=verdict_file, record=verdict_record)

    manifest = PromotionManifest(
        promotion_id="cole-qwen-bf16-v2",
        created_at=package.manifest.created_at + timedelta(minutes=3),
        subject_id="cole-self",
        experiment_id=winning_reveal.experiment_id,
        winning_trial_id=winning_reveal.trial_id,
        candidate_id=winning_reveal.candidate_id,
        voice_id="cole-qwen-bf16",
        voice_name="Cole authorized Qwen BF16",
        provider_id="mlx-audio",
        provider_voice=provider_voice,
        language=winning_trial.request.language,
        reference=reference,
        consent=consent,
        provenance=provenance,
        model_lock=model_lock,
        winning_run=winning_run,
        blind_package=blind_package,
        score_sheet=score_sheet,
        listening_summary=listening_summary,
        verdict=verdict,
        license=PromotionLicense(name="Private authorized use"),
        disclosure=PromotionDisclosure(
            spoken="This narration uses an authorized synthetic voice.",
            persistent="Synthetic voice generated from Cole's authorized recording.",
        ),
        clearance_scope="Private ChloeSpeech rendering on Cole-controlled local hosts.",
        provider_settings=provider_settings,
        watermarked=False,
    )
    if reference_payload is not None:
        reference_path.write_bytes(reference_payload)
    if output_payload is not None:
        normalized_path.write_bytes(output_payload)
    return manifest, {
        "reference": reference_path,
        "consent": tmp_path / consent_file.path,
        "provenance": tmp_path / provenance_file.path,
        "model": model_file,
        "model_source": tmp_path / source_file.path,
        "worker_model": tmp_path / worker_model_file.path,
        "executed_code": executed_code_path,
        "dependency": dependency_path,
        "runtime_receipt": runtime_receipt_path,
        "model_lock": tmp_path / model_lock_file.path,
        "output": tmp_path / output.path,
        "run": tmp_path / run_file.path,
        "blind_package": tmp_path / package_file.path,
        "score_sheet": tmp_path / score_file.path,
        "listening_summary": tmp_path / summary_file.path,
        "verdict": tmp_path / verdict_file.path,
    }


def _chatterbox_payload(manifest: PromotionManifest) -> dict[str, Any]:
    payload = manifest.model_dump(mode="python")
    expected_voice = f"reference-sha256:{manifest.reference.sha256}"
    settings = {"variant": "turbo", "device": "mps"}
    payload["provider_id"] = "chatterbox"
    payload["provider_voice"] = expected_voice
    payload["provider_settings"] = settings
    payload["watermarked"] = True
    payload["model_lock"]["record"]["model_id"] = "ResembleAI/chatterbox-turbo"
    payload["model_lock"]["model_source"]["record"]["repository"] = "ResembleAI/chatterbox-turbo"
    payload["model_lock"]["worker_model_record"]["model_id"] = "ResembleAI/chatterbox-turbo"
    payload["winning_run"]["record"]["provider_id"] = "chatterbox"
    payload["winning_run"]["record"]["provider_voice"] = expected_voice
    payload["winning_run"]["record"]["model_id"] = "ResembleAI/chatterbox-turbo"
    payload["winning_run"]["record"]["provider_settings_sha256"] = canonical_sha256(settings)
    payload["winning_run"]["record"]["watermarked"] = True
    return payload


def test_valid_promotion_emits_public_contract_shape_and_actual_disclosure_cue(
    tmp_path: Path,
) -> None:
    manifest, paths = promotion_fixture(tmp_path)

    validation = validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)
    handoff = build_chloespeech_handoff(manifest, asset_root=tmp_path, as_of=AS_OF)
    payload = handoff.model_dump(mode="json")

    assert validation.promotion_sha256 == canonical_sha256(manifest)
    assert {
        paths[name].relative_to(tmp_path).as_posix()
        for name in (
            "model",
            "model_source",
            "worker_model",
            "executed_code",
            "dependency",
            "runtime_receipt",
        )
    }.issubset(validation.verified_paths)
    assert handoff.copies_assets is False
    assert handoff.mutates_consumer is False
    assert payload["voice_manifest"]["identity_kind"] == "human_authorized"
    assert payload["voice_manifest"]["subject"] == {
        "schema_version": 1,
        "id": "cole-self",
        "display_name": None,
    }
    assert payload["voice_manifest"]["provider_voice"] == (
        f"reference-sha256:{manifest.reference.sha256}"
    )
    assert payload["voice_manifest"]["reference_audio"]["path"] == manifest.reference.path
    assert payload["voice_manifest"]["provenance"]["disclosure"] == {
        "schema_version": 1,
        "spoken": manifest.disclosure.spoken,
        "persistent": manifest.disclosure.persistent,
    }
    assert payload["required_disclosure_cues"] == [
        {
            "schema_version": 1,
            "id": manifest.disclosure.cue_id,
            "speaker_id": manifest.disclosure.speaker_id,
            "text": manifest.disclosure.spoken,
            "voice_id": manifest.voice_id,
            "delivery": {},
            "pause_after_ms": manifest.disclosure.pause_after_ms,
        }
    ]
    # The result is genuinely JSON-ready and contains no absolute private root.
    encoded = json.dumps(payload, sort_keys=True)
    assert str(tmp_path) not in encoded


def test_reference_tamper_fails_closed(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    paths["reference"].write_bytes(b"changed reference")

    with pytest.raises(
        PromotionValidationError,
        match="cannot verify|size changed|checksum changed",
    ):
        validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)


@pytest.mark.parametrize("malformed", ["reference", "output"])
def test_promotion_rejects_checksum_bound_non_wav_audio(
    tmp_path: Path,
    malformed: str,
) -> None:
    kwargs = {f"{malformed}_payload": b"not actually a PCM WAV"}
    manifest, _ = promotion_fixture(tmp_path, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(PromotionValidationError, match="cannot verify promotion asset"):
        validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)


@pytest.mark.parametrize(
    "evidence",
    [
        "reference",
        "consent",
        "model",
        "model_source",
        "worker_model",
        "executed_code",
        "dependency",
        "runtime_receipt",
        "output",
        "blind_package",
        "score_sheet",
        "listening_summary",
    ],
)
def test_promotion_rejects_hardlinked_assets_and_evidence(
    tmp_path: Path,
    evidence: str,
) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    alias = tmp_path / "tmp" / f"{evidence}-hardlink"
    os.link(paths[evidence], alias)

    with pytest.raises(
        PromotionValidationError,
        match="must (?:not be hardlinked|be a single-link)|unsafe link count",
    ):
        validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)


def test_json_evidence_tamper_fails_closed(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    paths["consent"].write_text('{"approved":false}\n', encoding="utf-8")

    with pytest.raises(PromotionValidationError, match="size changed|checksum changed"):
        validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)


def test_model_artifact_is_rehashed_at_export_time(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    paths["model"].write_bytes(b"substituted model")

    with pytest.raises(
        PromotionValidationError, match="(?:model|source) artifact checksum changed"
    ):
        validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)


def test_chatterbox_provider_voice_must_be_reference_digest(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    payload = _chatterbox_payload(manifest)
    payload["provider_voice"] = "friendly-voice-name"

    with pytest.raises(ValidationError, match="provider_voice must be bound"):
        PromotionManifest.model_validate(payload)


def test_subject_watermark_and_both_disclosures_fail_closed(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)

    subject_payload = manifest.model_dump(mode="python")
    subject_payload["reference"]["subject_id"] = "someone-else"
    with pytest.raises(ValidationError, match="subjects must match"):
        PromotionManifest.model_validate(subject_payload)

    watermark_payload = manifest.model_dump(mode="python")
    watermark_payload["watermarked"] = True
    with pytest.raises(ValidationError, match="watermark state must agree"):
        PromotionManifest.model_validate(watermark_payload)

    with pytest.raises(ValidationError):
        PromotionDisclosure(spoken=" ", persistent="Persistent disclosure")
    with pytest.raises(ValidationError):
        PromotionDisclosure(spoken="Spoken disclosure", persistent=" ")


def test_expired_consent_blocks_handoff(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(
        tmp_path,
        expires_at=datetime(2026, 8, 18, 11, tzinfo=UTC),
    )

    with pytest.raises(PromotionValidationError, match="consent has expired"):
        build_chloespeech_handoff(manifest, asset_root=tmp_path, as_of=AS_OF)


def test_absolute_and_symlink_reference_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        HumanReferenceBinding(
            path=str(tmp_path / "absolute.wav"),
            sha256=OUTPUT_SHA256_PLACEHOLDER,
            size_bytes=1,
            subject_id="cole",
        )

    manifest, paths = promotion_fixture(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.wav"
    outside.write_bytes(paths["reference"].read_bytes())
    paths["reference"].unlink()
    paths["reference"].symlink_to(outside)
    try:
        with pytest.raises(PromotionValidationError, match="unsafe or missing|cannot verify"):
            validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)
    finally:
        outside.unlink(missing_ok=True)


def test_non_original_chatterbox_cannot_claim_ignored_style_controls(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    payload = _chatterbox_payload(manifest)
    settings = dict(payload["provider_settings"])
    settings["exaggeration"] = 1.2
    payload["provider_settings"] = settings
    payload["winning_run"]["record"]["provider_settings_sha256"] = canonical_sha256(settings)

    with pytest.raises(ValidationError, match="only realized by the original"):
        PromotionManifest.model_validate(payload)


def _replace(payload: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    cursor: dict[str, Any] = payload
    for part in path[:-1]:
        nested = cursor[part]
        assert isinstance(nested, dict)
        cursor = nested
    cursor[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            ("consent", "record", "allowed_purposes"),
            ("private-zero-shot-evaluation",),
            "consent must approve",
        ),
        (
            ("consent", "record", "allowed_execution_boundaries"),
            ("capture-host",),
            "execution boundary",
        ),
        (
            ("provenance", "record", "reference_path"),
            "references/other.wav",
            "reference paths must match",
        ),
        (
            ("provenance", "record", "reference_sha256"),
            OUTPUT_SHA256_PLACEHOLDER,
            "reference digests must match",
        ),
        (
            ("provenance", "record", "consent_id"),
            "different-consent",
            "consent identifiers must match",
        ),
        (
            ("provenance", "record", "consent_evidence_sha256"),
            OUTPUT_SHA256_PLACEHOLDER,
            "exact consent evidence",
        ),
        (
            ("model_lock", "record", "candidate_id"),
            "different-candidate",
            "model lock identity",
        ),
        (("model_lock", "record", "files"), {}, "model lock files"),
        (
            ("winning_run", "record", "trial_id"),
            "different-trial",
            "winning run identity",
        ),
        (
            ("verdict", "record", "winning_trial_id"),
            "different-trial",
            "verdict identity",
        ),
        (
            ("verdict", "record", "winning_run_evidence_sha256"),
            OUTPUT_SHA256_PLACEHOLDER,
            "exact winning-run evidence",
        ),
        (
            ("verdict", "record", "model_lock_evidence_sha256"),
            OUTPUT_SHA256_PLACEHOLDER,
            "exact model-lock evidence",
        ),
        (
            ("winning_run", "record", "model_revision"),
            "e" * 40,
            "provider/model identity",
        ),
        (
            ("winning_run", "record", "provider_settings_sha256"),
            OUTPUT_SHA256_PLACEHOLDER,
            "provider settings",
        ),
        (
            ("provenance", "record", "created_at"),
            datetime(2026, 8, 17, 13, 30, tzinfo=UTC),
            "provenance cannot postdate",
        ),
        (
            ("model_lock", "record", "resolved_at"),
            datetime(2026, 8, 17, 13, 30, tzinfo=UTC),
            "model lock cannot postdate",
        ),
        (
            ("verdict", "file", "path"),
            "provenance/consents/cole-private-rendering-v1.json",
            "paths must be distinct",
        ),
    ],
)
def test_manifest_rejects_broken_evidence_links(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
    message: str,
) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    payload = manifest.model_dump(mode="python")
    _replace(payload, path, value)

    with pytest.raises(ValidationError, match=message):
        PromotionManifest.model_validate(payload)


def test_consent_must_cover_the_winning_run_and_use_aware_timestamps(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)

    after_run = manifest.model_dump(mode="python")
    after_run["consent"]["record"]["effective_at"] = datetime(2026, 8, 17, 14, tzinfo=UTC)
    with pytest.raises(ValidationError, match="effective when the winning run completes"):
        PromotionManifest.model_validate(after_run)

    before_run = manifest.model_dump(mode="python")
    before_run["consent"]["record"]["expires_at"] = datetime(2026, 8, 17, 0, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="remain valid through the winning run"):
        PromotionManifest.model_validate(before_run)

    before_provenance = manifest.model_dump(mode="python")
    before_provenance["provenance"]["record"]["created_at"] = datetime(2025, 12, 31, tzinfo=UTC)
    with pytest.raises(ValidationError, match="provenance cannot predate effective consent"):
        PromotionManifest.model_validate(before_provenance)

    before_promotion = manifest.model_dump(mode="python")
    before_promotion["consent"]["record"]["expires_at"] = datetime(2026, 8, 17, 14, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="remain valid through promotion creation"):
        PromotionManifest.model_validate(before_promotion)

    naive = manifest.model_dump(mode="python")
    naive["consent"]["record"]["effective_at"] = datetime(2026, 1, 1)
    naive["consent"]["record"]["expires_at"] = None
    with pytest.raises(ValidationError, match="explicit timezone"):
        PromotionManifest.model_validate(naive)

    naive_lock = manifest.model_dump(mode="python")
    naive_lock["model_lock"]["record"]["resolved_at"] = datetime(2026, 8, 17, 12)
    with pytest.raises(ValidationError, match="timestamp requires an explicit timezone"):
        PromotionManifest.model_validate(naive_lock)


def test_chatterbox_variant_model_and_watermark_must_match_reality(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)

    unsupported = _chatterbox_payload(manifest)
    unsupported_settings = {"variant": "future", "device": "mps"}
    unsupported["provider_settings"] = unsupported_settings
    unsupported["winning_run"]["record"]["provider_settings_sha256"] = canonical_sha256(
        unsupported_settings
    )
    with pytest.raises(ValidationError, match="explicit supported variant"):
        PromotionManifest.model_validate(unsupported)

    wrong_model = _chatterbox_payload(manifest)
    wrong_model_settings = {"variant": "v3", "device": "mps"}
    wrong_model["provider_settings"] = wrong_model_settings
    wrong_model["winning_run"]["record"]["provider_settings_sha256"] = canonical_sha256(
        wrong_model_settings
    )
    with pytest.raises(ValidationError, match="model ID does not match"):
        PromotionManifest.model_validate(wrong_model)

    unwatermarked = _chatterbox_payload(manifest)
    unwatermarked["watermarked"] = False
    unwatermarked["winning_run"]["record"]["watermarked"] = False
    with pytest.raises(ValidationError, match="must retain its realized watermark"):
        PromotionManifest.model_validate(unwatermarked)

    original = _chatterbox_payload(manifest)
    original_settings = {"variant": "original", "device": "mps", "exaggeration": 1.2}
    original["provider_settings"] = original_settings
    original["model_lock"]["record"]["model_id"] = "ResembleAI/chatterbox"
    original["model_lock"]["model_source"]["record"]["repository"] = "ResembleAI/chatterbox"
    original["model_lock"]["worker_model_record"]["model_id"] = "ResembleAI/chatterbox"
    original["winning_run"]["record"]["model_id"] = "ResembleAI/chatterbox"
    original["winning_run"]["record"]["provider_settings_sha256"] = canonical_sha256(
        original_settings
    )
    PromotionManifest.model_validate(original)


def test_handoff_rejects_promised_but_missing_actual_disclosure(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    handoff = build_chloespeech_handoff(manifest, asset_root=tmp_path, as_of=AS_OF)
    payload = handoff.model_dump(mode="python")
    payload["required_disclosure_cues"][0]["text"] = "Different text."

    with pytest.raises(ValidationError, match="actual cue"):
        ChloeSpeechHandoff.model_validate(payload)


def test_handoff_contract_rejects_absolute_export_paths(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    handoff = build_chloespeech_handoff(manifest, asset_root=tmp_path, as_of=AS_OF)
    payload = handoff.model_dump(mode="python")
    payload["voice_manifest"]["reference_audio"]["path"] = "/private/reference.wav"

    with pytest.raises(ValidationError):
        ChloeSpeechHandoff.model_validate(payload)


def test_validation_rejects_bad_root_argument_and_missing_evidence(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)

    with pytest.raises(TypeError, match="validated PromotionManifest"):
        validate_promotion(object(), asset_root=tmp_path, as_of=AS_OF)  # type: ignore[arg-type]
    with pytest.raises(PromotionValidationError, match="explicit timezone"):
        validate_promotion(manifest, asset_root=tmp_path, as_of=datetime(2026, 8, 18))
    with pytest.raises(PromotionValidationError, match="before it was created"):
        validate_promotion(
            manifest,
            asset_root=tmp_path,
            as_of=datetime(2026, 8, 17, 14, 59, tzinfo=UTC),
        )
    with pytest.raises(PromotionValidationError, match="asset root is unavailable"):
        validate_promotion(manifest, asset_root=tmp_path / "missing", as_of=AS_OF)

    root_file = tmp_path.parent / f"{tmp_path.name}-root-file"
    root_file.write_bytes(b"not a directory")
    try:
        with pytest.raises(PromotionValidationError, match="must be a directory"):
            validate_promotion(manifest, asset_root=root_file, as_of=AS_OF)
    finally:
        root_file.unlink(missing_ok=True)

    paths["reference"].unlink()
    with pytest.raises(PromotionValidationError, match="unsafe or missing"):
        validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)


def test_asset_root_symlink_is_rejected(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    linked_root = tmp_path.parent / f"{tmp_path.name}-root-link"
    linked_root.symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises(PromotionValidationError, match="root must not be a symlink"):
            validate_promotion(manifest, asset_root=linked_root, as_of=AS_OF)
    finally:
        linked_root.unlink(missing_ok=True)


def test_asset_root_must_remain_private(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    tmp_path.chmod(0o755)

    with pytest.raises(PromotionValidationError, match="not a private lab layout"):
        validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)


def test_bound_json_is_parsed_and_must_equal_embedded_record(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)

    different = manifest.consent.record.model_copy(
        update={"allowed_purposes": ("private-zero-shot-evaluation",)}
    )
    different_digest = write_canonical_json(paths["consent"], different)
    different_payload = manifest.model_dump(mode="python")
    different_payload["consent"]["file"]["sha256"] = different_digest
    different_payload["consent"]["file"]["size_bytes"] = paths["consent"].stat().st_size
    different_payload["provenance"]["record"]["consent_evidence_sha256"] = different_digest
    different_payload["winning_run"]["record"]["consent_evidence_sha256"] = different_digest
    different_manifest = PromotionManifest.model_validate(different_payload)

    with pytest.raises(PromotionValidationError, match="contents do not match"):
        validate_promotion(different_manifest, asset_root=tmp_path, as_of=AS_OF)

    invalid_bytes = b"not valid json"
    paths["consent"].write_bytes(invalid_bytes)
    invalid_digest, invalid_size = sha256_file(paths["consent"])
    invalid_payload = manifest.model_dump(mode="python")
    invalid_payload["consent"]["file"]["sha256"] = invalid_digest
    invalid_payload["consent"]["file"]["size_bytes"] = invalid_size
    invalid_payload["provenance"]["record"]["consent_evidence_sha256"] = invalid_digest
    invalid_payload["winning_run"]["record"]["consent_evidence_sha256"] = invalid_digest
    invalid_manifest = PromotionManifest.model_validate(invalid_payload)

    with pytest.raises(PromotionValidationError, match="invalid contents"):
        validate_promotion(invalid_manifest, asset_root=tmp_path, as_of=AS_OF)


def test_day_granular_review_never_extends_timestamp_consent(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(
        tmp_path,
        expires_at=datetime(2027, 1, 1, 23, 59, tzinfo=UTC),
    )

    handoff = build_chloespeech_handoff(manifest, asset_root=tmp_path, as_of=AS_OF)

    assert (
        handoff.voice_manifest.provenance.clearance.review_by
        == datetime(2026, 12, 31, tzinfo=UTC).date()
    )


def test_promotion_recomputes_the_bound_listening_summary(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    summary_payload = manifest.listening_summary.record.model_dump(mode="python")
    candidates = summary_payload["aggregate"]["tracks"]["direct-tts"]["candidates"]
    winner = next(item for item in candidates if item["candidate_id"] == manifest.candidate_id)
    winner["means"]["likeness"] = 4.0
    winner["overall_mean"] = 4.8
    changed_summary = PromotionListeningSummaryRecord.model_validate(summary_payload)
    summary_digest = write_canonical_json(paths["listening_summary"], changed_summary)
    changed_verdict = manifest.verdict.record.model_copy(
        update={"listening_summary_evidence_sha256": summary_digest}
    )
    verdict_digest = write_canonical_json(paths["verdict"], changed_verdict)

    payload = manifest.model_dump(mode="python")
    payload["listening_summary"]["record"] = changed_summary.model_dump(mode="python")
    payload["listening_summary"]["file"]["sha256"] = summary_digest
    payload["listening_summary"]["file"]["size_bytes"] = paths["listening_summary"].stat().st_size
    payload["verdict"]["record"] = changed_verdict.model_dump(mode="python")
    payload["verdict"]["file"]["sha256"] = verdict_digest
    payload["verdict"]["file"]["size_bytes"] = paths["verdict"].stat().st_size
    rebound = PromotionManifest.model_validate(payload)

    with pytest.raises(PromotionValidationError, match="differs from the recomputed"):
        validate_promotion(rebound, asset_root=tmp_path, as_of=AS_OF)


def test_assertion_only_v1_promotion_is_rejected(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    payload = manifest.model_dump(mode="python")
    payload["schema_version"] = "cole-voice-lab.promotion/v1"
    payload.pop("blind_package")
    payload.pop("score_sheet")
    payload.pop("listening_summary")
    payload["verdict"]["record"]["schema_version"] = "cole-voice-lab.promotion-verdict/v1"
    payload["verdict"]["record"]["blind_review_completed"] = True

    with pytest.raises(ValidationError):
        PromotionManifest.model_validate(payload)


def test_tied_or_losing_direct_tts_result_cannot_be_promoted(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    tied = manifest.model_dump(mode="python")
    candidates = tied["listening_summary"]["record"]["aggregate"]["tracks"]["direct-tts"][
        "candidates"
    ]
    loser = next(item for item in candidates if item["candidate_id"] != manifest.candidate_id)
    loser["means"] = {
        "likeness": 5.0,
        "naturalness": 5.0,
        "prosody": 5.0,
        "intelligibility": 5.0,
        "artifact_freedom": 5.0,
    }
    loser["overall_mean"] = 5.0
    with pytest.raises(ValidationError, match="tied direct-TTS"):
        PromotionManifest.model_validate(tied)

    losing = manifest.model_dump(mode="python")
    losing_candidates = losing["listening_summary"]["record"]["aggregate"]["tracks"]["direct-tts"][
        "candidates"
    ]
    promoted = next(
        item for item in losing_candidates if item["candidate_id"] == manifest.candidate_id
    )
    actual_winner = next(
        item for item in losing_candidates if item["candidate_id"] != manifest.candidate_id
    )
    promoted["means"] = {
        "likeness": 2.0,
        "naturalness": 2.0,
        "prosody": 2.0,
        "intelligibility": 2.0,
        "artifact_freedom": 2.0,
    }
    promoted["overall_mean"] = 2.0
    actual_winner["means"] = {
        "likeness": 5.0,
        "naturalness": 5.0,
        "prosody": 5.0,
        "intelligibility": 5.0,
        "artifact_freedom": 5.0,
    }
    actual_winner["overall_mean"] = 5.0
    losing["listening_summary"]["record"]["aggregate"]["tracks"]["direct-tts"]["candidates"] = (
        actual_winner,
        promoted,
    )
    with pytest.raises(ValidationError, match="not the direct-TTS listening winner"):
        PromotionManifest.model_validate(losing)


def test_winning_reveal_entry_must_exist_in_validated_package(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    forged_entry = manifest.verdict.record.winning_reveal_entry.model_copy(
        update={"blind_id": "sample-0000000000000000"}
    )
    changed_verdict = manifest.verdict.record.model_copy(
        update={"winning_reveal_entry": forged_entry}
    )
    verdict_digest = write_canonical_json(paths["verdict"], changed_verdict)
    payload = manifest.model_dump(mode="python")
    payload["verdict"]["record"] = changed_verdict.model_dump(mode="python")
    payload["verdict"]["file"]["sha256"] = verdict_digest
    payload["verdict"]["file"]["size_bytes"] = paths["verdict"].stat().st_size
    rebound = PromotionManifest.model_validate(payload)

    with pytest.raises(PromotionValidationError, match="does not occur exactly once"):
        validate_promotion(rebound, asset_root=tmp_path, as_of=AS_OF)


def test_winning_run_claims_are_rebound_to_audition_request(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    changed_run = manifest.winning_run.record.model_copy(
        update={"audition_settings_sha256": "f" * 64}
    )
    run_digest = write_canonical_json(paths["run"], changed_run)
    changed_verdict = manifest.verdict.record.model_copy(
        update={"winning_run_evidence_sha256": run_digest}
    )
    verdict_digest = write_canonical_json(paths["verdict"], changed_verdict)
    payload = manifest.model_dump(mode="python")
    payload["winning_run"]["record"] = changed_run.model_dump(mode="python")
    payload["winning_run"]["file"]["sha256"] = run_digest
    payload["winning_run"]["file"]["size_bytes"] = paths["run"].stat().st_size
    payload["verdict"]["record"] = changed_verdict.model_dump(mode="python")
    payload["verdict"]["file"]["sha256"] = verdict_digest
    payload["verdict"]["file"]["size_bytes"] = paths["verdict"].stat().st_size
    rebound = PromotionManifest.model_validate(payload)

    with pytest.raises(PromotionValidationError, match="audition settings differs"):
        validate_promotion(rebound, asset_root=tmp_path, as_of=AS_OF)


def test_coherent_model_lock_substitution_cannot_escape_worker_evidence(
    tmp_path: Path,
) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    paths["model"].write_bytes(b"attacker-selected model")
    substituted_sha256, _ = sha256_file(paths["model"])
    changed_source = manifest.model_lock.model_source.record.model_copy(
        update={"files": {"model.safetensors": substituted_sha256}}
    )
    source_sha256 = write_canonical_json(paths["model_source"], changed_source)
    changed_worker = manifest.model_lock.worker_model_record.model_copy(
        update={"model_manifest_sha256": source_sha256}
    )
    worker_sha256 = write_canonical_json(paths["worker_model"], changed_worker)
    model_path = next(iter(manifest.model_lock.record.files))
    changed_lock = manifest.model_lock.record.model_copy(
        update={"files": {model_path: substituted_sha256}}
    )
    lock_sha256 = write_canonical_json(paths["model_lock"], changed_lock)
    changed_verdict = manifest.verdict.record.model_copy(
        update={
            "model_lock_evidence_sha256": lock_sha256,
            "model_source_manifest_sha256": source_sha256,
            "worker_model_evidence_sha256": worker_sha256,
        }
    )
    verdict_sha256 = write_canonical_json(paths["verdict"], changed_verdict)

    payload = manifest.model_dump(mode="python")
    payload["model_lock"]["record"] = changed_lock.model_dump(mode="python")
    payload["model_lock"]["file"].update(
        sha256=lock_sha256,
        size_bytes=paths["model_lock"].stat().st_size,
    )
    payload["model_lock"]["model_source"]["record"] = changed_source.model_dump(mode="python")
    payload["model_lock"]["model_source"]["file"].update(
        sha256=source_sha256,
        size_bytes=paths["model_source"].stat().st_size,
    )
    payload["model_lock"]["worker_model_record"] = changed_worker.model_dump(mode="python")
    payload["model_lock"]["worker_model_evidence"].update(
        sha256=worker_sha256,
        size_bytes=paths["worker_model"].stat().st_size,
    )
    payload["verdict"]["record"] = changed_verdict.model_dump(mode="python")
    payload["verdict"]["file"].update(
        sha256=verdict_sha256,
        size_bytes=paths["verdict"].stat().st_size,
    )
    rebound = PromotionManifest.model_validate(payload)

    with pytest.raises(
        PromotionValidationError,
        match=(
            "blind-review package validation failed|"
            "winning audition evidence is invalid|model lock differs"
        ),
    ):
        validate_promotion(rebound, asset_root=tmp_path, as_of=AS_OF)


def test_coherent_provider_rebinding_is_rejected_by_worker_projection(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    fake_settings: dict[str, Any] = {"backend": "never-executed", "network": True}
    changed_run = manifest.winning_run.record.model_copy(
        update={
            "provider_id": "fake-provider",
            "provider_voice": "unrealized-voice",
            "provider_settings_sha256": canonical_sha256(fake_settings),
        }
    )
    run_sha256 = write_canonical_json(paths["run"], changed_run)
    changed_verdict = manifest.verdict.record.model_copy(
        update={"winning_run_evidence_sha256": run_sha256}
    )
    verdict_sha256 = write_canonical_json(paths["verdict"], changed_verdict)
    payload = manifest.model_dump(mode="python")
    payload["provider_id"] = "fake-provider"
    payload["provider_voice"] = "unrealized-voice"
    payload["provider_settings"] = fake_settings
    payload["winning_run"]["record"] = changed_run.model_dump(mode="python")
    payload["winning_run"]["file"].update(
        sha256=run_sha256,
        size_bytes=paths["run"].stat().st_size,
    )
    payload["verdict"]["record"] = changed_verdict.model_dump(mode="python")
    payload["verdict"]["file"].update(
        sha256=verdict_sha256,
        size_bytes=paths["verdict"].stat().st_size,
    )
    rebound = PromotionManifest.model_validate(payload)

    with pytest.raises(PromotionValidationError, match="provider ID differs"):
        validate_promotion(rebound, asset_root=tmp_path, as_of=AS_OF)


def test_promotion_chronology_cannot_predate_blind_review(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    payload = manifest.model_dump(mode="python")
    payload["score_sheet"]["record"]["scored_at"] = (
        manifest.blind_package.record.created_at - timedelta(seconds=1)
    )

    with pytest.raises(ValidationError, match="timestamps are out of order"):
        PromotionManifest.model_validate(payload)


def test_coherently_rebound_blind_package_timestamps_must_match(tmp_path: Path) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    package_root = paths["blind_package"].parent
    index_path = package_root / "public/review-index.json"
    public_manifest_path = package_root / "public/review-manifest.json"
    index = ReviewIndex.model_validate_json(index_path.read_bytes())
    public_manifest = PublicReviewManifest.model_validate_json(public_manifest_path.read_bytes())

    changed_index = index.model_copy(update={"created_at": index.created_at + timedelta(seconds=1)})
    index_sha256 = write_canonical_json(index_path, changed_index, mode=0o400)
    changed_public_manifest = public_manifest.model_copy(
        update={
            "review_index_sha256": index_sha256,
            "files": tuple(
                entry.model_copy(
                    update={
                        "sha256": index_sha256,
                        "size_bytes": index_path.stat().st_size,
                    }
                )
                if entry.path == "review-index.json"
                else entry
                for entry in public_manifest.files
            ),
        }
    )
    public_manifest_sha256 = write_canonical_json(
        public_manifest_path,
        changed_public_manifest,
        mode=0o400,
    )
    changed_package = manifest.blind_package.record.model_copy(
        update={
            "review_index_sha256": index_sha256,
            "public_manifest_sha256": public_manifest_sha256,
            "files": tuple(
                entry.model_copy(
                    update={
                        "sha256": (
                            index_sha256
                            if entry.path == "public/review-index.json"
                            else public_manifest_sha256
                        ),
                        "size_bytes": (
                            index_path.stat().st_size
                            if entry.path == "public/review-index.json"
                            else public_manifest_path.stat().st_size
                        ),
                    }
                )
                if entry.path in {"public/review-index.json", "public/review-manifest.json"}
                else entry
                for entry in manifest.blind_package.record.files
            ),
        }
    )
    package_sha256 = write_canonical_json(paths["blind_package"], changed_package, mode=0o400)

    changed_score = manifest.score_sheet.record.model_copy(
        update={
            "review_index_sha256": index_sha256,
            "public_manifest_sha256": public_manifest_sha256,
        }
    )
    score_sha256 = write_canonical_json(paths["score_sheet"], changed_score)
    changed_summary = manifest.listening_summary.record.model_copy(
        update={
            "blind_package_manifest_sha256": package_sha256,
            "score_sheet_sha256": score_sha256,
        }
    )
    summary_sha256 = write_canonical_json(paths["listening_summary"], changed_summary)
    changed_verdict = manifest.verdict.record.model_copy(
        update={
            "blind_package_manifest_sha256": package_sha256,
            "score_sheet_evidence_sha256": score_sha256,
            "listening_summary_evidence_sha256": summary_sha256,
        }
    )
    verdict_sha256 = write_canonical_json(paths["verdict"], changed_verdict)

    payload = manifest.model_dump(mode="python")
    for field, record, digest, path in (
        ("blind_package", changed_package, package_sha256, paths["blind_package"]),
        ("score_sheet", changed_score, score_sha256, paths["score_sheet"]),
        (
            "listening_summary",
            changed_summary,
            summary_sha256,
            paths["listening_summary"],
        ),
        ("verdict", changed_verdict, verdict_sha256, paths["verdict"]),
    ):
        payload[field]["record"] = record.model_dump(mode="python")
        payload[field]["file"].update(sha256=digest, size_bytes=path.stat().st_size)
    rebound = PromotionManifest.model_validate(payload)

    with pytest.raises(PromotionValidationError, match="timestamps must match"):
        validate_promotion(rebound, asset_root=tmp_path, as_of=AS_OF)


def test_public_boundaries_deep_revalidate_shallow_frozen_containers(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    manifest.provider_settings.update({"backend": "mutated-after-validation", "network": True})

    with pytest.raises(PromotionValidationError, match="strict contract"):
        validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)
    with pytest.raises(PromotionValidationError, match="strict contract"):
        build_chloespeech_handoff(manifest, asset_root=tmp_path, as_of=AS_OF)

    nested, _ = promotion_fixture(tmp_path / "nested")
    nested.model_lock.record.files.clear()
    with pytest.raises(PromotionValidationError, match="strict contract"):
        validate_promotion(nested, asset_root=tmp_path / "nested", as_of=AS_OF)


def test_public_boundaries_reject_unvalidated_model_copy(tmp_path: Path) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    bypass = manifest.model_copy(
        update={
            "provider_id": "chatterbox",
            "provider_voice": "not-a-reference",
            "watermarked": True,
        }
    )

    with pytest.raises(PromotionValidationError, match="strict contract"):
        validate_promotion(bypass, asset_root=tmp_path, as_of=AS_OF)
    with pytest.raises(PromotionValidationError, match="strict contract"):
        build_chloespeech_handoff(bypass, asset_root=tmp_path, as_of=AS_OF)


def test_reviewed_huggingface_cache_metadata_does_not_change_source_artifacts(
    tmp_path: Path,
) -> None:
    manifest, paths = promotion_fixture(tmp_path)
    cache_file = paths["model"].parent / ".cache/huggingface/download-state.json"
    cache_file.parent.mkdir(mode=0o700, parents=True)
    cache_file.write_text("reviewed downloader cache\n", encoding="utf-8")

    validate_promotion(manifest, asset_root=tmp_path, as_of=AS_OF)


def test_promotion_cli_validates_and_writes_one_new_private_handoff(
    tmp_path: Path,
    capsys: object,
) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    manifest_path = tmp_path / "evaluations/promotion-evidence/promotion.json"
    write_canonical_json(manifest_path, manifest)
    common = [
        "--asset-root",
        str(tmp_path),
        "--manifest",
        str(manifest_path),
        "--as-of",
        AS_OF.isoformat(),
    ]

    assert main(["promotion", "validate", *common]) == 0
    validation = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert validation["status"] == "pass"
    assert validation["promotion_sha256"] == canonical_sha256(manifest)
    assert validation["evaluated_at"] == AS_OF.isoformat().replace("+00:00", "Z")

    output_parent = tmp_path / "evaluations/handoff-exports"
    output_parent.mkdir(mode=0o700)
    output_path = output_parent / "chloespeech-handoff.json"
    assert main(["promotion", "handoff", *common, "--output", str(output_path)]) == 0
    result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    payload = output_path.read_bytes()
    handoff = ChloeSpeechHandoff.model_validate_json(payload)
    assert payload == canonical_json_bytes(handoff) + b"\n"
    assert result == {
        "output_path": str(output_path),
        "output_sha256": sha256_file(output_path)[0],
        "promotion_sha256": canonical_sha256(manifest),
    }
    assert output_path.stat().st_mode & 0o777 == 0o600

    original = payload
    assert main(["promotion", "handoff", *common, "--output", str(output_path)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert output_path.read_bytes() == original


def test_promotion_cli_rejects_linked_input_and_output(
    tmp_path: Path,
    capsys: object,
) -> None:
    manifest, _ = promotion_fixture(tmp_path)
    manifest_path = tmp_path / "evaluations/promotion-evidence/promotion.json"
    write_canonical_json(manifest_path, manifest)
    linked_manifest = manifest_path.with_name("promotion-link.json")
    linked_manifest.symlink_to(manifest_path)
    assert (
        main(
            [
                "promotion",
                "validate",
                "--asset-root",
                str(tmp_path),
                "--manifest",
                str(linked_manifest),
                "--as-of",
                AS_OF.isoformat(),
            ]
        )
        == 2
    )
    assert "safely open" in capsys.readouterr().err  # type: ignore[attr-defined]

    output_parent = tmp_path / "evaluations/handoff-exports"
    output_parent.mkdir(mode=0o700)
    target = output_parent / "existing.json"
    target.write_text("preserve\n", encoding="utf-8")
    linked_output = output_parent / "handoff.json"
    linked_output.symlink_to(target)
    assert (
        main(
            [
                "promotion",
                "handoff",
                "--asset-root",
                str(tmp_path),
                "--manifest",
                str(manifest_path),
                "--output",
                str(linked_output),
                "--as-of",
                AS_OF.isoformat(),
            ]
        )
        == 2
    )
    assert "refusing to overwrite" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert target.read_text(encoding="utf-8") == "preserve\n"

    accessible_parent = tmp_path / "evaluations/group-readable-export"
    accessible_parent.mkdir(mode=0o700)
    accessible_parent.chmod(0o750)
    accessible_output = accessible_parent / "handoff.json"
    assert (
        main(
            [
                "promotion",
                "handoff",
                "--asset-root",
                str(tmp_path),
                "--manifest",
                str(manifest_path),
                "--output",
                str(accessible_output),
                "--as-of",
                AS_OF.isoformat(),
            ]
        )
        == 2
    )
    assert "inaccessible to group and other" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert not accessible_output.exists()

"""The stored audition records blind review reads, and their validation without a registry.

The bake-off that wrote them is retired (D-057); these tests check that a run already on disk
still validates, and that every digest-coherent rewrite of its evidence is refused.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from audition_fixtures import write_audition_run
from pydantic import ValidationError

from cole_voice_lab.audition import (
    AuditionPlan,
    AuditionRunManifest,
    AuditionTrial,
    PersistedTrialResult,
    audition_paths,
    validate_audition_plan_paths,
    validate_persisted_trial_evidence,
)
from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import ExperimentResult
from cole_voice_lab.paths import AssetLayout

EXPERIMENT = "blind-audition"


def _layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "private" / "assets")


def _stored(
    layout: AssetLayout,
) -> tuple[AuditionPlan, AuditionRunManifest, dict[str, PersistedTrialResult]]:
    paths = audition_paths(layout, EXPERIMENT)
    plan = AuditionPlan.model_validate_json(paths.plan_path.read_bytes())
    run = AuditionRunManifest.model_validate_json(paths.run_manifest_path.read_bytes())
    records = {
        pointer.trial_id: PersistedTrialResult.model_validate_json(
            (layout.root / pointer.result_path).read_bytes()
        )
        for pointer in run.results
    }
    return plan, run, records


def _run(
    tmp_path: Path, *, include_seed: bool = False, historical: bool = False
) -> tuple[AssetLayout, AuditionPlan, AuditionRunManifest, dict[str, PersistedTrialResult]]:
    layout = _layout(tmp_path)
    write_audition_run(
        layout,
        direct_candidates=("qwen-bf16", "f5-tts"),
        include_seed=include_seed,
        historical=historical,
    )
    return layout, *_stored(layout)


def _first(
    tmp_path: Path, *, historical: bool = False
) -> tuple[AssetLayout, AuditionTrial, PersistedTrialResult]:
    layout, plan, _, records = _run(tmp_path, historical=historical)
    trial = plan.trials[0]
    return layout, trial, records[trial.trial_id]


def test_a_stored_run_validates_without_a_registry(tmp_path: Path) -> None:
    layout, plan, run, records = _run(tmp_path, include_seed=True)
    validate_audition_plan_paths(layout, plan)
    assert run.status == "complete"
    assert {trial.track for trial in plan.trials} == {"direct-tts", "voice-conversion"}
    for trial in plan.trials:
        validate_persisted_trial_evidence(layout, trial, records[trial.trial_id])


def test_a_trial_run_under_a_consent_record_before_d050_still_validates(
    tmp_path: Path,
) -> None:
    # Its request, model evidence and result all named the consent record's digest; they
    # still agree, and the record itself is not read (here it does not exist).
    layout, plan, _, records = _run(tmp_path, historical=True)
    validate_audition_plan_paths(layout, plan)
    for trial in plan.trials:
        record = records[trial.trial_id]
        assert record.worker_result.metadata["consent_sha256"] == "c" * 64
        validate_persisted_trial_evidence(layout, trial, record)
    assert not (layout.root / "provenance/consents").exists()


def test_strict_nested_contract_invariants(tmp_path: Path) -> None:
    _, audition, _, _ = _run(tmp_path)
    first, second = audition.trials[:2]

    with pytest.raises(ValidationError, match="normalized absolute"):
        AuditionTrial(
            **{
                **first.model_dump(mode="python"),
                "normalized_output_path": "relative.wav",
            }
        )
    with pytest.raises(ValidationError, match="timezone"):
        AuditionPlan.model_validate(
            audition.model_dump(mode="python")
            | {"created_at": datetime(2026, 8, 17), "trials": (first,)}
        )
    with pytest.raises(ValidationError, match="identity"):
        AuditionTrial(**{**first.model_dump(mode="python"), "trial_id": "different-trial"})
    with pytest.raises(ValidationError, match="profile"):
        AuditionTrial(**{**first.model_dump(mode="python"), "profile": "quality"})
    with pytest.raises(ValidationError, match="timing"):
        AuditionTrial(**{**first.model_dump(mode="python"), "timing_mode": "warm"})

    duplicate_native_request = second.request.model_copy(
        update={"output_path": first.request.output_path}
    )
    duplicate_native = AuditionTrial(
        **{
            **second.model_dump(mode="python"),
            "request": duplicate_native_request,
            "request_sha256": canonical_sha256(duplicate_native_request),
        }
    )
    with pytest.raises(ValidationError, match="native output paths"):
        AuditionPlan.model_validate(
            audition.model_dump(mode="python") | {"trials": (first, duplicate_native)}
        )
    with pytest.raises(ValidationError, match="normalized output paths"):
        AuditionPlan.model_validate(
            audition.model_dump(mode="python")
            | {
                "trials": (
                    first,
                    second.model_copy(
                        update={"normalized_output_path": first.normalized_output_path}
                    ),
                )
            }
        )
    with pytest.raises(ValidationError, match="experiment IDs"):
        AuditionPlan.model_validate(
            audition.model_dump(mode="python")
            | {"experiment_id": "other-experiment", "trials": (first,)}
        )
    with pytest.raises(ValidationError, match="profiles"):
        AuditionPlan.model_validate(
            audition.model_dump(mode="python") | {"profile": "quality", "trials": (first,)}
        )
    with pytest.raises(ValidationError, match="prompt-suite digest"):
        AuditionPlan.model_validate(
            audition.model_dump(mode="python") | {"prompt_suite_sha256": "0" * 64}
        )


def test_the_direct_matrix_must_be_equal_for_every_candidate(tmp_path: Path) -> None:
    _, audition, _, _ = _run(tmp_path)
    qwen = next(trial for trial in audition.trials if trial.candidate_id == "qwen-bf16")
    trial_id = "qwen-bf16-warm-trial"
    directory = Path(qwen.request.output_path).parents[3]
    request = qwen.request.model_copy(
        update={
            "trial_id": trial_id,
            "output_path": str(directory / "native/direct-tts/qwen-bf16" / f"{trial_id}.wav"),
            "settings": {**qwen.request.settings, "timing_mode": "warm"},
        }
    )
    warm = qwen.model_copy(
        update={
            "trial_id": trial_id,
            "timing_mode": "warm",
            "request": request,
            "request_sha256": canonical_sha256(request),
            "normalized_output_path": str(
                directory / "normalized/direct-tts/qwen-bf16" / f"{trial_id}.wav"
            ),
        }
    )
    with pytest.raises(ValidationError, match="held-out matrix differs"):
        AuditionPlan.model_validate(
            audition.model_dump(mode="python") | {"trials": (*audition.trials, warm)}
        )


def test_result_and_manifest_contracts_reject_inconsistent_shapes(tmp_path: Path) -> None:
    _, _, manifest, records = _run(tmp_path)
    successful = records[manifest.results[0].trial_id]
    failed_worker = ExperimentResult(
        trial_id=successful.worker_result.trial_id,
        candidate_id=successful.worker_result.candidate_id,
        ok=False,
        error_code="runtime_unavailable",
        error_message="fixture failure",
    )

    invalid_records = (
        {"outcome": "success", "worker_result": failed_worker, "normalized": successful.normalized},
        {"outcome": "success", "error_code": "unexpected", "error_message": "unexpected"},
        {"outcome": "worker-failed", "error_code": "failed", "error_message": "failed"},
        {
            "outcome": "normalization-failed",
            "worker_result": failed_worker,
            "normalized": None,
            "error_code": "failed",
            "error_message": "failed",
        },
        {
            "outcome": "worker-failed",
            "worker_result": failed_worker,
            "normalized": None,
            "error_code": None,
            "error_message": None,
        },
    )
    base = successful.model_dump(mode="python")
    for updates in invalid_records:
        with pytest.raises(ValidationError):
            PersistedTrialResult(**{**base, **updates})

    manifest_base = manifest.model_dump(mode="python")
    invalid_manifests = (
        {"direct_candidate_ids": ("qwen-bf16", "qwen-bf16")},
        {"voice_conversion_candidate_ids": ("seed-vc", "seed-vc")},
        {"results": (manifest.results[0], manifest.results[0])},
    )
    for updates in invalid_manifests:
        with pytest.raises(ValidationError, match="unique"):
            AuditionRunManifest(**{**manifest_base, **updates})


def test_stored_plans_confine_every_worker_path(tmp_path: Path) -> None:
    layout, audition, _, _ = _run(tmp_path, include_seed=True)
    direct = next(trial for trial in audition.trials if trial.track == "direct-tts")
    conversion = next(trial for trial in audition.trials if trial.track == "voice-conversion")

    def with_trial(original: AuditionTrial, replacement: AuditionTrial) -> AuditionPlan:
        return audition.model_copy(
            update={
                "trials": tuple(
                    replacement if trial.trial_id == original.trial_id else trial
                    for trial in audition.trials
                )
            }
        )

    def with_request(original: AuditionTrial, request) -> AuditionTrial:
        return original.model_copy(
            update={"request": request, "request_sha256": canonical_sha256(request)}
        )

    outside_native = direct.request.model_copy(
        update={"output_path": str((tmp_path / "outside-native.wav").resolve())}
    )
    with pytest.raises(ContractError, match="native output path"):
        validate_audition_plan_paths(
            layout, with_trial(direct, with_request(direct, outside_native))
        )

    outside_normalized = direct.model_copy(
        update={"normalized_output_path": str((tmp_path / "outside-normalized.wav").resolve())}
    )
    with pytest.raises(ContractError, match="normalized output path"):
        validate_audition_plan_paths(layout, with_trial(direct, outside_normalized))

    outside_reference = direct.request.reference.model_copy(
        update={"path": str((tmp_path / "outside-reference.wav").resolve())}
    )
    reference_request = direct.request.model_copy(update={"reference": outside_reference})
    with pytest.raises(ContractError, match="reference audio must remain"):
        validate_audition_plan_paths(
            layout, with_trial(direct, with_request(direct, reference_request))
        )

    # A plan written before D-050 may name a consent record: never read, still confined.
    outside_consent = direct.request.reference.model_copy(
        update={
            "consent_path": str((tmp_path / "outside-consent.json").resolve()),
            "consent_sha256": "c" * 64,
        }
    )
    consent_request = direct.request.model_copy(update={"reference": outside_consent})
    with pytest.raises(ContractError, match="consent evidence must remain"):
        validate_audition_plan_paths(
            layout, with_trial(direct, with_request(direct, consent_request))
        )

    outside_source_request = conversion.request.model_copy(
        update={"source_audio_path": str((tmp_path / "outside-source.wav").resolve())}
    )
    with pytest.raises(ContractError, match="performed source audio must remain"):
        validate_audition_plan_paths(
            layout,
            with_trial(conversion, with_request(conversion, outside_source_request)),
        )

    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    reference_link = layout.directory("references") / "linked"
    reference_link.symlink_to(outside_directory, target_is_directory=True)
    linked_reference = direct.request.reference.model_copy(
        update={"path": str(reference_link / "reference.wav")}
    )
    linked_request = direct.request.model_copy(update={"reference": linked_reference})
    with pytest.raises(SecurityError, match="symlink component"):
        validate_audition_plan_paths(
            layout, with_trial(direct, with_request(direct, linked_request))
        )


def test_a_result_bound_to_another_request_is_refused(tmp_path: Path) -> None:
    layout, trial, record = _first(tmp_path)
    changed = record.model_copy(update={"request_sha256": "f" * 64})
    with pytest.raises(ContractError, match="different request"):
        validate_persisted_trial_evidence(layout, trial, changed)


@pytest.mark.parametrize(
    ("tamper", "error"),
    (
        ("empty-worker", "strict evidence contract"),
        ("empty-distribution", "strict evidence contract"),
        ("missing-runtime", "omits the candidate runtime distribution"),
        ("invalid-integrity", "strict evidence contract"),
    ),
)
def test_persisted_executed_code_contract_rejects_digest_coherent_false_evidence(
    tmp_path: Path, tamper: str, error: str
) -> None:
    layout, trial, record = _first(tmp_path)
    result = record.worker_result
    assert result.output_path is not None
    code_path = Path(result.output_path).parent / str(
        result.metadata["executed_code_evidence_path"]
    )
    code_payload = json.loads(code_path.read_bytes())
    runtime_package = str(result.metadata["runtime_package"])
    if tamper == "empty-worker":
        code_payload["worker_package"]["files"] = {}
    elif tamper == "empty-distribution":
        code_payload["distributions"][runtime_package]["files"] = {}
    elif tamper == "missing-runtime":
        del code_payload["distributions"][runtime_package]
    else:
        code_payload["integrity_sha256"] = "0" * 64
    if tamper != "invalid-integrity":
        code_payload_without_integrity = {
            key: value for key, value in code_payload.items() if key != "integrity_sha256"
        }
        code_payload["integrity_sha256"] = canonical_sha256(code_payload_without_integrity)
    code_sha256 = write_canonical_json(code_path, code_payload)

    model_path = Path(result.output_path).parent / str(result.metadata["model_evidence_path"])
    model_payload = json.loads(model_path.read_bytes())
    model_payload["executed_code_evidence_sha256"] = code_sha256
    model_sha256 = write_canonical_json(model_path, model_payload)
    metadata = {
        **result.metadata,
        "executed_code_evidence_sha256": code_sha256,
        "model_evidence_sha256": model_sha256,
    }
    changed = record.model_copy(
        update={"worker_result": result.model_copy(update={"metadata": metadata})}
    )
    with pytest.raises(ContractError, match=error):
        validate_persisted_trial_evidence(layout, trial, changed)


@pytest.mark.parametrize(
    ("tamper", "error"),
    (
        ("candidate", "model evidence changed binding for candidate_id"),
        ("model-id", "model evidence changed binding for model_id"),
        ("model-revision", "model evidence changed binding for model_revision"),
        ("code-revision", "model evidence changed binding for code_revision"),
        ("reference", "model evidence changed binding for reference_sha256"),
        ("consent", "model evidence changed binding for consent_sha256"),
        ("consent-result", "names a consent record its evidence does not"),
        ("source", "model evidence changed binding for source_audio_sha256"),
        ("settings", "model evidence changed binding for settings"),
        ("model-manifest", "model evidence changed binding for model_manifest_sha256"),
        ("runtime", "executed-code evidence changed binding for runtime_package_version"),
        ("watermark-result", "claims watermark evidence that is not applicable"),
    ),
)
def test_persisted_model_evidence_rejects_digest_coherent_rebinding(
    tmp_path: Path, tamper: str, error: str
) -> None:
    layout, trial, record = _first(tmp_path)
    result = record.worker_result
    assert result.output_path is not None
    model_path = Path(result.output_path).parent / str(result.metadata["model_evidence_path"])
    model_payload = json.loads(model_path.read_bytes())
    metadata = dict(result.metadata)
    if tamper == "candidate":
        model_payload["candidate_id"] = "f5-tts"
    elif tamper == "model-id":
        model_payload["model_id"] = "attacker/model"
    elif tamper == "model-revision":
        model_payload["model_revision"] = "f" * 40
    elif tamper == "code-revision":
        model_payload["code_revision"] = "f" * 40
    elif tamper == "reference":
        model_payload["reference_sha256"] = "f" * 64
        metadata["reference_sha256"] = "f" * 64
    elif tamper == "consent":
        model_payload["consent_sha256"] = "f" * 64
        metadata["consent_sha256"] = "f" * 64
    elif tamper == "consent-result":
        metadata["consent_sha256"] = "f" * 64
    elif tamper == "source":
        model_payload["source_audio_sha256"] = "f" * 64
        model_payload["source_audio_size_bytes"] = 123
        metadata["source_audio_sha256"] = "f" * 64
        metadata["source_audio_size_bytes"] = 123
    elif tamper == "settings":
        model_payload["settings"] = {**model_payload["settings"], "temperature": 99.0}
    elif tamper == "model-manifest":
        model_payload["model_manifest_sha256"] = "f" * 64
    elif tamper == "runtime":
        model_payload["runtime_package_version"] = "9.9.9"
        metadata["runtime_package_version"] = "9.9.9"
    else:
        metadata["watermarked"] = False
    metadata["model_evidence_sha256"] = write_canonical_json(model_path, model_payload)
    changed = record.model_copy(
        update={"worker_result": result.model_copy(update={"metadata": metadata})}
    )
    with pytest.raises(ContractError, match=error):
        validate_persisted_trial_evidence(layout, trial, changed)

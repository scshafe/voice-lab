from __future__ import annotations

import hashlib
import json
import math
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from runtime_fixtures import synthetic_python_runtime

from cole_voice_lab.audio import inspect_pcm_wav
from cole_voice_lab.audition import (
    AuditionPlan,
    AuditionRunManifest,
    AuditionTrial,
    PersistedSourceEvidence,
    PersistedTrialResult,
    PersistedTrialSourceEvidence,
    audition_paths,
    build_audition_plan,
    build_candidate_preparation_plan,
    build_preparation_plans,
    checkout_path,
    load_validated_audition_run,
    persist_audition_plan,
    run_offline_audition,
    runtime_path,
    source_path,
    validate_audition_plan_paths,
    validate_persisted_trial_evidence,
)
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
    write_canonical_json,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import (
    EnvironmentEvidence,
    ExperimentResult,
    Prompt,
    PromptSuite,
    ReferenceEvidence,
)
from cole_voice_lab.normalization import NormalizationEvidence
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.registry import load_registry
from cole_voice_lab.runtime import LocalSourceManifest, RuntimeInstallReceipt, RuntimeTreeEntry

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 17, 20, tzinfo=UTC)


def registry():
    return load_registry(ROOT / "config/candidates.toml", ROOT / "config/model-sources.lock.json")


def fixture_source_evidence(source) -> PersistedSourceEvidence:
    if source.kind == "huggingface":
        files = {
            item.path: item.sha256 or hashlib.sha256(item.path.encode()).hexdigest()
            for item in source.files
        }
        manifest = LocalSourceManifest(
            source_id=source.source_id,
            kind=source.kind,
            repository=source.repository,
            revision=source.revision,
            files=files,
        )
        manifest_sha256 = hashlib.sha256(canonical_json_bytes(manifest) + b"\n").hexdigest()
        integrity_record = {
            "schema_version": "cole-voice-lab.snapshot-integrity/v1",
            "source_id": source.source_id,
            "repository": source.repository,
            "revision": source.revision,
            "manifest_sha256": manifest_sha256,
            "files": files,
        }
    else:
        files = {"fixture.py": hashlib.sha256(source.revision.encode()).hexdigest()}
        manifest = None
        manifest_sha256 = None
        integrity_record = {
            "schema_version": "cole-voice-lab.git-checkout-integrity/v1",
            "revision": source.revision,
            "tree_id": "a" * 40,
            "files": files,
        }
    return PersistedSourceEvidence(
        source_id=source.source_id,
        kind=source.kind,
        repository=source.repository,
        revision=source.revision,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
        files=files,
        integrity_record=integrity_record,
        integrity_sha256=canonical_sha256(integrity_record),
    )


def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "private-assets")


@pytest.fixture(autouse=True)
def _sealed_runtime_receipt_fixture(monkeypatch) -> None:
    def persist(_registry, asset_layout, paths, _plan, trial, **_kwargs):
        tree = (
            RuntimeTreeEntry(
                path="bin/python",
                kind="file",
                mode=0o500,
                size_bytes=1,
                sha256="d" * 64,
            ),
        )
        receipt = RuntimeInstallReceipt(
            runtime_id=trial.runtime_id,
            runtime_lock_sha256=trial.runtime_lock_sha256,
            requirements_sha256="1" * 64,
            artifact_lock_sha256="2" * 64,
            reviewed_wheel_sha256={},
            lab_wheel_sha256="3" * 64,
            lab_distribution_version="0.1.0.dev0",
            runtime_python_sha256="4" * 64,
            python_runtime_lock_sha256="5" * 64,
            sanitized_files=(),
            installed_distributions={"cole-voice-lab": "0.1.0.dev0"},
            tree=tree,
            tree_sha256=canonical_sha256([entry.model_dump(mode="json") for entry in tree]),
        )
        destination = (
            paths.experiment_directory / "evidence/runtime-receipts" / f"{trial.trial_id}.json"
        )
        digest = write_canonical_json(destination, receipt)
        return destination.relative_to(asset_layout.root).as_posix(), digest

    monkeypatch.setattr("cole_voice_lab.audition._persist_trial_runtime_receipt", persist)

    def persist_sources(registry_, asset_layout, paths, plan_, trial):
        resolved = registry_.resolve(trial.candidate_id, plan_.profile)
        evidence = PersistedTrialSourceEvidence(
            trial_id=trial.trial_id,
            candidate_id=trial.candidate_id,
            request_sha256=trial.request_sha256,
            primary=fixture_source_evidence(resolved.model_source),
            auxiliary={
                source.repository: fixture_source_evidence(source)
                for source in resolved.auxiliary_sources
            },
            code=(
                fixture_source_evidence(resolved.code_source)
                if resolved.candidate.engine == "seed-vc"
                else None
            ),
        )
        destination = (
            paths.experiment_directory / "evidence/source-manifests" / f"{trial.trial_id}.json"
        )
        digest = write_canonical_json(destination, evidence)
        return destination.relative_to(asset_layout.root).as_posix(), digest

    monkeypatch.setattr("cole_voice_lab.audition._persist_trial_source_evidence", persist_sources)
    monkeypatch.setattr(
        "cole_voice_lab.audition.verify_runtime_install_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "cole_voice_lab.audition.load_persisted_runtime_receipt",
        lambda *_args, **_kwargs: None,
    )


def prompt() -> Prompt:
    return Prompt(
        prompt_id="heldout-1",
        text="A held-out audition sentence.",
        category="neutral",
        style="Neutral.",
        holdout=True,
        expected_seconds=3.0,
    )


def prompt_suite(*prompts: Prompt) -> PromptSuite:
    return PromptSuite(
        suite_id="heldout-fixture-v1",
        purpose="held-out-evaluation",
        prompts=prompts,
    )


def reference(assets: AssetLayout) -> ReferenceEvidence:
    reference_path = assets.directory("references") / "reference.wav"
    reference_path.write_bytes(b"checksum-bound reference fixture")
    reference_digest, _ = sha256_file(reference_path)
    consent_path = assets.directory("provenance") / "consents" / "consent.json"
    consent_path.parent.mkdir(mode=0o700, exist_ok=True)
    consent_path.write_bytes(b"checksum-bound consent fixture")
    consent_digest, _ = sha256_file(consent_path)
    return ReferenceEvidence(
        path=str(reference_path),
        sha256=reference_digest,
        transcript="Exact reference transcript.",
        subject_id="cole-self",
        consent_path=str(consent_path),
        consent_sha256=consent_digest,
    )


def performed_source(assets: AssetLayout) -> Path:
    path = assets.directory("derived") / "source.wav"
    path.write_bytes(b"checksum-bound performed source fixture")
    return path


def plan(tmp_path: Path, assets: AssetLayout, *, experiment_id: str = "audition-1") -> AuditionPlan:
    heldout = prompt()
    return build_audition_plan(
        registry(),
        assets,
        experiment_id=experiment_id,
        prompt_suite=prompt_suite(heldout),
        prompts=(heldout,),
        references=(reference(assets),),
        seeds=(7,),
        source_audio_by_prompt={heldout.prompt_id: performed_source(assets)},
        profile="smoke",
        direct_timing_modes=("cold",),
        created_at=NOW,
    )


def write_tone(path: Path, *, sample_rate: int = 24_000) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(
            b"".join(
                struct.pack("<h", int(2_000 * math.sin(2 * math.pi * 220 * i / sample_rate)))
                for i in range(2_400)
            )
        )


def successful_worker(invocation) -> ExperimentResult:
    output = Path(invocation.request.output_path)
    write_tone(output)
    inspection = inspect_pcm_wav(output)
    digest, size = sha256_file(output)
    evidence = output.parent / "evidence"
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    dependency = evidence / f"{invocation.request.trial_id}.pip-freeze.txt"
    dependency.write_text("fixture==1.0\n", encoding="utf-8")
    dependency_sha256, _ = sha256_file(dependency)
    runtime_package, runtime_package_version = {
        "f5-tts": ("f5-tts", "1.1.22"),
        "seed-vc": ("torch", "2.6.0"),
    }.get(invocation.request.candidate_id, ("mlx-audio", "0.5.0"))
    primary_source = fixture_source_evidence(invocation.resolved.model_source)
    auxiliary_manifests = {
        source.repository: fixture_source_evidence(source).manifest_sha256
        for source in invocation.resolved.auxiliary_sources
    }
    code_integrity = (
        fixture_source_evidence(invocation.resolved.code_source).integrity_sha256
        if invocation.resolved.candidate.engine == "seed-vc"
        else None
    )
    code_record = {
        "schema_version": "cole-voice-lab.executed-code-evidence/v1",
        "worker_package": {
            "root_name": "cole_voice_lab",
            "files": {"workers/common.py": {"sha256": "a" * 64, "size_bytes": 1}},
        },
        "distributions": {
            "cole-voice-lab": {
                "version": "0.1.0.dev0",
                "files": {"cole_voice_lab/__init__.py": {"sha256": "b" * 64, "size_bytes": 1}},
            },
            runtime_package: {
                "version": runtime_package_version,
                "files": {"runtime.py": {"sha256": "c" * 64, "size_bytes": 1}},
            },
        },
    }
    code_record["integrity_sha256"] = canonical_sha256(code_record)
    code_path = evidence / f"{invocation.request.trial_id}.executed-code.json"
    code_sha256 = write_canonical_json(code_path, code_record)
    model_record = {
        "schema_version": "cole-voice-lab.worker-model-evidence/v1",
        "candidate_id": invocation.request.candidate_id,
        "model_id": invocation.request.model_id,
        "model_revision": invocation.request.model_revision,
        "code_revision": invocation.request.code_revision,
        "model_manifest_sha256": primary_source.manifest_sha256,
        "auxiliary_manifest_sha256": auxiliary_manifests,
        "code_checkout_integrity_sha256": code_integrity,
        "executed_code_evidence_path": f"evidence/{invocation.request.trial_id}.executed-code.json",
        "executed_code_evidence_sha256": code_sha256,
        "reference_sha256": invocation.request.reference.sha256,
        "consent_sha256": invocation.request.reference.consent_sha256,
        "source_audio_sha256": invocation.request.source_audio_sha256,
        "source_audio_size_bytes": invocation.request.source_audio_size_bytes,
        "settings": invocation.request.settings,
        "runtime_package": runtime_package,
        "runtime_package_version": runtime_package_version,
    }
    model_path = evidence / f"{invocation.request.trial_id}.model-evidence.json"
    model_sha256 = write_canonical_json(model_path, model_record)
    return ExperimentResult(
        trial_id=invocation.request.trial_id,
        candidate_id=invocation.request.candidate_id,
        ok=True,
        output_path=str(output),
        output_sha256=digest,
        output_bytes=size,
        audio=inspection.audio,
        generation_seconds=0.1,
        realized_seed=invocation.request.seed,
        environment=EnvironmentEvidence(
            python_version="3.12.12",
            platform="test",
            machine="arm64",
            dependency_fingerprint_sha256=dependency_sha256,
            dependency_evidence_path=(f"evidence/{invocation.request.trial_id}.pip-freeze.txt"),
        ),
        metadata={
            "model_manifest_sha256": primary_source.manifest_sha256,
            "auxiliary_manifest_sha256": auxiliary_manifests,
            "code_checkout_integrity_sha256": code_integrity,
            "model_evidence_path": (f"evidence/{invocation.request.trial_id}.model-evidence.json"),
            "model_evidence_sha256": model_sha256,
            "executed_code_evidence_path": (
                f"evidence/{invocation.request.trial_id}.executed-code.json"
            ),
            "executed_code_evidence_sha256": code_sha256,
            "reference_sha256": invocation.request.reference.sha256,
            "consent_sha256": invocation.request.reference.consent_sha256,
            "source_audio_sha256": invocation.request.source_audio_sha256,
            "source_audio_size_bytes": invocation.request.source_audio_size_bytes,
            "runtime_package": runtime_package,
            "runtime_package_version": runtime_package_version,
        },
    )


def successful_normalization(normalization_plan) -> NormalizationEvidence:
    write_tone(normalization_plan.normalized_path)
    native_digest, _ = sha256_file(normalization_plan.native_path)
    normalized_digest, normalized_size = sha256_file(normalization_plan.normalized_path)
    return NormalizationEvidence(
        normalization_id="cole-voice-lab-common-v1",
        native_path=normalization_plan.native_path,
        native_sha256=native_digest,
        normalized_path=normalization_plan.normalized_path,
        normalized_sha256=normalized_digest,
        normalized_bytes=normalized_size,
        settings_sha256=normalization_plan.settings_sha256,
        inspection=inspect_pcm_wav(normalization_plan.normalized_path),
    )


def completed_record(
    tmp_path: Path, monkeypatch, *, experiment_id: str
) -> tuple[AssetLayout, AuditionTrial, PersistedTrialResult]:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id=experiment_id)
    monkeypatch.setattr("cole_voice_lab.audition.run_worker", successful_worker)
    monkeypatch.setattr("cole_voice_lab.audition.execute_normalization", successful_normalization)
    manifest = run_offline_audition(
        registry(), assets, audition, direct_candidate_ids=("qwen3-tts-base",), now=NOW
    )
    pointer = manifest.results[0]
    trial = next(item for item in audition.trials if item.trial_id == pointer.trial_id)
    record = PersistedTrialResult.model_validate_json(
        (assets.root / pointer.result_path).read_bytes()
    )
    return assets, trial, record


def test_historical_result_survives_runtime_eviction_and_different_reinstall(
    tmp_path: Path,
    monkeypatch,
) -> None:
    assets, completed_trial, _record = completed_record(
        tmp_path,
        monkeypatch,
        experiment_id="audition-evicted-history",
    )
    paths = audition_paths(assets, "audition-evicted-history")
    audition = AuditionPlan.model_validate_json(paths.plan_path.read_bytes())

    # Simulate exact eviction followed by a differently sealed resident tree at
    # the same runtime ID. Historical validation must use the copied receipt.
    resident = runtime_path(assets, completed_trial.runtime_id)
    resident.mkdir(mode=0o700, parents=True)
    (resident / "different-install").write_text("new receipt/tree", encoding="utf-8")
    monkeypatch.setattr(
        "cole_voice_lab.audition.verify_runtime_install_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("historical validation consulted the resident runtime")
        ),
    )
    historical = load_validated_audition_run(
        registry(),
        assets,
        audition,
        repository_root=ROOT,
    )
    assert {pointer.candidate_id for pointer in historical.results} == {"qwen3-tts-base"}

    monkeypatch.setattr(
        "cole_voice_lab.audition.verify_runtime_install_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("cole_voice_lab.audition.run_worker", successful_worker)
    monkeypatch.setattr(
        "cole_voice_lab.audition.execute_normalization",
        successful_normalization,
    )
    next_trial = next(trial for trial in audition.trials if trial.candidate_id == "f5-tts")
    resumed = run_offline_audition(
        registry(),
        assets,
        audition,
        direct_candidate_ids=("f5-tts",),
        repository_root=ROOT,
        expected_runtime_receipt_sha256={next_trial.runtime_id: "a" * 64},
        now=NOW,
    )
    assert {pointer.candidate_id for pointer in resumed.results} == {
        "qwen3-tts-base",
        "f5-tts",
    }


def test_plan_persistence_is_canonical_and_conflict_safe(tmp_path: Path) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets)
    path, digest = persist_audition_plan(assets, audition)
    assert path == audition_paths(assets, "audition-1").plan_path
    assert digest == sha256_file(path)[0]
    assert persist_audition_plan(assets, audition) == (path, digest)
    changed = audition.model_copy(update={"created_at": datetime(2026, 8, 18, tzinfo=UTC)})
    with pytest.raises(ContractError, match="differs"):
        persist_audition_plan(assets, changed)


def test_plan_persistence_confines_all_worker_paths(tmp_path: Path) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id="audition-confined")
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
        persist_audition_plan(assets, with_trial(direct, with_request(direct, outside_native)))

    outside_normalized = direct.model_copy(
        update={"normalized_output_path": str((tmp_path / "outside-normalized.wav").resolve())}
    )
    with pytest.raises(ContractError, match="normalized output path"):
        validate_audition_plan_paths(assets, with_trial(direct, outside_normalized))

    outside_reference = direct.request.reference.model_copy(
        update={"path": str((tmp_path / "outside-reference.wav").resolve())}
    )
    reference_request = direct.request.model_copy(update={"reference": outside_reference})
    with pytest.raises(ContractError, match="reference audio must remain"):
        validate_audition_plan_paths(
            assets, with_trial(direct, with_request(direct, reference_request))
        )

    outside_consent = direct.request.reference.model_copy(
        update={"consent_path": str((tmp_path / "outside-consent.json").resolve())}
    )
    consent_request = direct.request.model_copy(update={"reference": outside_consent})
    with pytest.raises(ContractError, match="consent evidence must remain"):
        validate_audition_plan_paths(
            assets, with_trial(direct, with_request(direct, consent_request))
        )

    outside_source_request = conversion.request.model_copy(
        update={"source_audio_path": str((tmp_path / "outside-source.wav").resolve())}
    )
    with pytest.raises(ContractError, match="performed source audio must remain"):
        validate_audition_plan_paths(
            assets,
            with_trial(conversion, with_request(conversion, outside_source_request)),
        )

    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    reference_link = assets.directory("references") / "linked"
    reference_link.symlink_to(outside_directory, target_is_directory=True)
    linked_reference = direct.request.reference.model_copy(
        update={"path": str(reference_link / "reference.wav")}
    )
    linked_request = direct.request.model_copy(update={"reference": linked_reference})
    with pytest.raises(SecurityError, match="symlink component"):
        validate_audition_plan_paths(
            assets, with_trial(direct, with_request(direct, linked_request))
        )


def test_standard_paths_and_preparation_are_pure(tmp_path: Path, monkeypatch) -> None:
    assets = layout(tmp_path)
    host = synthetic_python_runtime(tmp_path, monkeypatch)
    loaded = host.registry
    preparation = build_candidate_preparation_plan(
        loaded,
        assets,
        candidate_id="chatterbox-turbo",
        profile="smoke",
        repository_root=ROOT,
        base_python=host.python,
    )
    assert preparation.runtime_path == str(runtime_path(assets, "mlx-audio-0.5.0"))
    assert preparation.model_path == str(source_path(assets, "chatterbox-turbo-8bit"))
    assert preparation.code_source_id == "mlx-audio-code"
    assert preparation.code_checkout_path is None
    assert preparation.code_checkout_commands == ()
    assert preparation.runtime_commands[0].argv[1:6] == ("-I", "-P", "-S", "-m", "venv")
    assert any(
        "S3TokenizerV2" in item for command in preparation.source_commands for item in command.argv
    )
    assert not Path(preparation.model_path).exists()

    plans = build_preparation_plans(
        loaded,
        assets,
        candidate_ids=("qwen3-tts-base", "seed-vc"),
        profile="smoke",
        repository_root=ROOT,
        base_python=host.python,
    )
    assert [item.candidate_id for item in plans] == ["qwen3-tts-base", "seed-vc"]
    assert plans[0].code_checkout_path is None
    assert plans[1].code_checkout_path == str(checkout_path(assets, "seed-vc-code"))
    with pytest.raises(ContractError, match="unique"):
        build_preparation_plans(
            loaded,
            assets,
            candidate_ids=("seed-vc", "seed-vc"),
            profile="smoke",
            repository_root=ROOT,
            base_python=host.python,
        )


def test_offline_run_is_sequential_persisted_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets)
    calls: list[str] = []

    def worker(invocation):
        calls.append(invocation.request.candidate_id)
        return successful_worker(invocation)

    monkeypatch.setattr("cole_voice_lab.audition.run_worker", worker)
    monkeypatch.setattr("cole_voice_lab.audition.execute_normalization", successful_normalization)
    monkeypatch.setattr(
        "cole_voice_lab.audition._verify_current_model_sources",
        lambda *_args: None,
    )
    manifest = run_offline_audition(
        registry(),
        assets,
        audition,
        direct_candidate_ids=("qwen3-tts-base",),
        now=NOW,
    )
    assert calls == ["qwen3-tts-base"]
    assert manifest.status == "complete"
    assert len(manifest.results) == 1
    result_path = assets.root / manifest.results[0].result_path
    record = PersistedTrialResult.model_validate_json(result_path.read_bytes())
    assert record.outcome == "success"
    assert record.normalized is not None
    assert record.request_sha256 == next(
        trial.request_sha256 for trial in audition.trials if trial.candidate_id == "qwen3-tts-base"
    )

    resumed = run_offline_audition(
        registry(),
        assets,
        audition,
        direct_candidate_ids=("qwen3-tts-base",),
        now=datetime(2026, 8, 18, tzinfo=UTC),
    )
    assert resumed == manifest
    assert calls == ["qwen3-tts-base"]


def test_direct_and_vc_selection_are_independent_and_failures_are_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id="audition-failures")
    calls: list[str] = []

    def worker(invocation):
        candidate = invocation.request.candidate_id
        calls.append(candidate)
        if candidate == "qwen3-tts-base":
            return ExperimentResult(
                trial_id=invocation.request.trial_id,
                candidate_id=candidate,
                ok=False,
                error_code="runtime_unavailable",
                error_message="prepared runtime missing in fixture",
            )
        return successful_worker(invocation)

    monkeypatch.setattr("cole_voice_lab.audition.run_worker", worker)
    monkeypatch.setattr("cole_voice_lab.audition.execute_normalization", successful_normalization)
    manifest = run_offline_audition(
        registry(),
        assets,
        audition,
        direct_candidate_ids=("qwen3-tts-base",),
        voice_conversion_candidate_ids=("seed-vc",),
        now=NOW,
    )
    assert calls == ["qwen3-tts-base", "seed-vc"]
    assert manifest.direct_candidate_ids == ("qwen3-tts-base",)
    assert manifest.voice_conversion_candidate_ids == ("seed-vc",)
    assert manifest.status == "complete-with-failures"
    assert {pointer.outcome for pointer in manifest.results} == {"success", "worker-failed"}


def test_resume_rejects_result_bound_to_a_different_request(tmp_path: Path, monkeypatch) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id="audition-tamper")
    monkeypatch.setattr("cole_voice_lab.audition.run_worker", successful_worker)
    monkeypatch.setattr("cole_voice_lab.audition.execute_normalization", successful_normalization)
    monkeypatch.setattr(
        "cole_voice_lab.audition._verify_current_model_sources",
        lambda *_args: None,
    )
    manifest = run_offline_audition(
        registry(),
        assets,
        audition,
        direct_candidate_ids=("f5-tts",),
        now=NOW,
    )
    result_path = assets.root / manifest.results[0].result_path
    record = PersistedTrialResult.model_validate_json(result_path.read_bytes())
    write_canonical_json(result_path, record.model_copy(update={"request_sha256": "f" * 64}))
    with pytest.raises(ContractError, match="digest or size changed"):
        run_offline_audition(
            registry(),
            assets,
            audition,
            direct_candidate_ids=("f5-tts",),
            now=NOW,
        )


def test_selection_and_plan_contracts_fail_closed(tmp_path: Path) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id="audition-invalid")
    with pytest.raises(ContractError, match="at least one"):
        run_offline_audition(registry(), assets, audition, direct_candidate_ids=(), now=NOW)
    with pytest.raises(ContractError, match="not present"):
        run_offline_audition(
            registry(),
            assets,
            audition,
            direct_candidate_ids=("seed-vc",),
            now=NOW,
        )
    with pytest.raises(ContractError, match="direct candidate selection must be unique"):
        run_offline_audition(
            registry(),
            assets,
            audition,
            direct_candidate_ids=("qwen3-tts-base", "qwen3-tts-base"),
            now=NOW,
        )
    with pytest.raises(ContractError, match="voice-conversion candidate selection must be unique"):
        run_offline_audition(
            registry(),
            assets,
            audition,
            direct_candidate_ids=(),
            voice_conversion_candidate_ids=("seed-vc", "seed-vc"),
            now=NOW,
        )
    with pytest.raises(ContractError, match="voice-conversion selection is not present"):
        run_offline_audition(
            registry(),
            assets,
            audition,
            direct_candidate_ids=(),
            voice_conversion_candidate_ids=("qwen3-tts-base",),
            now=NOW,
        )
    with pytest.raises(ContractError, match="trial selection"):
        run_offline_audition(
            registry(),
            assets,
            audition,
            direct_candidate_ids=("qwen3-tts-base",),
            trial_ids=("missing-trial",),
            now=NOW,
        )
    selected_trial = next(
        trial.trial_id for trial in audition.trials if trial.candidate_id == "qwen3-tts-base"
    )
    with pytest.raises(ContractError, match="trial selection must be unique"):
        run_offline_audition(
            registry(),
            assets,
            audition,
            direct_candidate_ids=("qwen3-tts-base",),
            trial_ids=(selected_trial, selected_trial),
            now=NOW,
        )

    with pytest.raises(ContractError, match="cannot escape"):
        audition_paths(assets, "../escaped")
    with pytest.raises(ContractError, match="one path component"):
        audition_paths(assets, "nested/experiment")

    first = audition.trials[0]
    with pytest.raises(ValidationError, match="digest"):
        AuditionTrial(
            trial_id=first.trial_id,
            candidate_id=first.candidate_id,
            track=first.track,
            profile=first.profile,
            timing_mode=first.timing_mode,
            runtime_id=first.runtime_id,
            runtime_lock_sha256=first.runtime_lock_sha256,
            request_sha256="0" * 64,
            request=first.request,
            normalized_output_path=first.normalized_output_path,
        )
    with pytest.raises(ValidationError, match="unique"):
        AuditionPlan.model_validate(audition.model_dump(mode="python") | {"trials": (first, first)})
    assert canonical_sha256(first.request) == first.request_sha256


def test_strict_nested_contract_invariants(tmp_path: Path) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id="audition-contracts")
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


def test_result_and_manifest_contracts_reject_inconsistent_shapes(
    tmp_path: Path, monkeypatch
) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id="audition-result-contracts")
    monkeypatch.setattr("cole_voice_lab.audition.run_worker", successful_worker)
    monkeypatch.setattr("cole_voice_lab.audition.execute_normalization", successful_normalization)
    monkeypatch.setattr(
        "cole_voice_lab.audition._verify_current_model_sources",
        lambda *_args: None,
    )
    manifest = run_offline_audition(
        registry(), assets, audition, direct_candidate_ids=("qwen3-tts-base",), now=NOW
    )
    persisted_path = assets.root / manifest.results[0].result_path
    successful = PersistedTrialResult.model_validate_json(persisted_path.read_bytes())
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
        {"direct_candidate_ids": ("qwen3-tts-base", "qwen3-tts-base")},
        {"voice_conversion_candidate_ids": ("seed-vc", "seed-vc")},
        {"results": (manifest.results[0], manifest.results[0])},
    )
    for updates in invalid_manifests:
        with pytest.raises(ValidationError, match="unique"):
            AuditionRunManifest(**{**manifest_base, **updates})


@pytest.mark.parametrize("failure", ("worker", "normalization"))
def test_orchestration_exceptions_are_durable_failures(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id=f"audition-{failure}-exception")
    if failure == "worker":

        def exploding_worker(_invocation):
            raise RuntimeError("worker exploded")

        monkeypatch.setattr("cole_voice_lab.audition.run_worker", exploding_worker)
    else:
        monkeypatch.setattr("cole_voice_lab.audition.run_worker", successful_worker)

        def exploding_normalization(_plan):
            raise RuntimeError("normalization exploded")

        monkeypatch.setattr(
            "cole_voice_lab.audition.execute_normalization", exploding_normalization
        )

    manifest = run_offline_audition(
        registry(), assets, audition, direct_candidate_ids=("qwen3-tts-base",), now=NOW
    )
    result = PersistedTrialResult.model_validate_json(
        (assets.root / manifest.results[0].result_path).read_bytes()
    )
    assert result.outcome == f"{failure}-failed"
    assert result.error_message == f"{failure} exploded"


@pytest.mark.parametrize("mismatch", ("identity", "path", "seed"))
def test_worker_evidence_is_rebound_before_persistence(
    tmp_path: Path, monkeypatch, mismatch: str
) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id=f"audition-worker-{mismatch}")

    def mismatched_worker(invocation):
        result = successful_worker(invocation)
        if mismatch == "identity":
            return result.model_copy(update={"candidate_id": "f5-tts"})
        if mismatch == "path":
            return result.model_copy(
                update={"output_path": str((tmp_path / "other.wav").resolve())}
            )
        return result.model_copy(update={"realized_seed": result.realized_seed + 1})

    monkeypatch.setattr("cole_voice_lab.audition.run_worker", mismatched_worker)
    manifest = run_offline_audition(
        registry(), assets, audition, direct_candidate_ids=("qwen3-tts-base",), now=NOW
    )
    record = PersistedTrialResult.model_validate_json(
        (assets.root / manifest.results[0].result_path).read_bytes()
    )
    assert record.outcome == "worker-failed"
    assert record.error_code == "orchestration_error"


def test_partial_selection_and_manifest_repair_are_resumable(tmp_path: Path, monkeypatch) -> None:
    assets = layout(tmp_path)
    heldout = prompt()
    audition = build_audition_plan(
        registry(),
        assets,
        experiment_id="audition-partial",
        prompt_suite=prompt_suite(heldout),
        prompts=(heldout,),
        references=(reference(assets),),
        seeds=(7,),
        source_audio_by_prompt={heldout.prompt_id: performed_source(assets)},
        profile="smoke",
        direct_timing_modes=("cold", "warm"),
        created_at=NOW,
    )
    selected = next(
        trial.trial_id
        for trial in audition.trials
        if trial.candidate_id == "qwen3-tts-base" and trial.timing_mode == "cold"
    )
    monkeypatch.setattr("cole_voice_lab.audition.run_worker", successful_worker)
    monkeypatch.setattr("cole_voice_lab.audition.execute_normalization", successful_normalization)
    monkeypatch.setattr(
        "cole_voice_lab.audition._verify_current_model_sources",
        lambda *_args: None,
    )
    manifest = run_offline_audition(
        registry(),
        assets,
        audition,
        direct_candidate_ids=("qwen3-tts-base",),
        trial_ids=(selected,),
        now=NOW,
    )
    assert manifest.status == "in-progress"
    assert len(manifest.results) == 1

    manifest_path = audition_paths(assets, audition.experiment_id).run_manifest_path
    write_canonical_json(manifest_path, manifest.model_copy(update={"results": ()}))
    repaired = run_offline_audition(
        registry(),
        assets,
        audition,
        direct_candidate_ids=("qwen3-tts-base",),
        trial_ids=(selected,),
        now=NOW,
    )
    assert repaired.results == manifest.results


def test_run_manifest_digest_tamper_is_rejected(tmp_path: Path, monkeypatch) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id="audition-manifest-tamper")
    monkeypatch.setattr("cole_voice_lab.audition.run_worker", successful_worker)
    monkeypatch.setattr("cole_voice_lab.audition.execute_normalization", successful_normalization)
    manifest = run_offline_audition(
        registry(), assets, audition, direct_candidate_ids=("qwen3-tts-base",), now=NOW
    )
    manifest_path = audition_paths(assets, audition.experiment_id).run_manifest_path
    write_canonical_json(manifest_path, manifest.model_copy(update={"plan_sha256": "f" * 64}))
    with pytest.raises(ContractError, match="different audition plan"):
        run_offline_audition(
            registry(), assets, audition, direct_candidate_ids=("qwen3-tts-base",), now=NOW
        )


@pytest.mark.parametrize(
    "artifact",
    ("native", "normalized", "dependency", "model-evidence", "executed-code"),
)
def test_resume_rehashes_every_success_artifact(tmp_path: Path, monkeypatch, artifact: str) -> None:
    assets = layout(tmp_path)
    audition = plan(tmp_path, assets, experiment_id=f"audition-rehash-{artifact}")
    monkeypatch.setattr("cole_voice_lab.audition.run_worker", successful_worker)
    monkeypatch.setattr("cole_voice_lab.audition.execute_normalization", successful_normalization)
    monkeypatch.setattr(
        "cole_voice_lab.audition._verify_current_model_sources",
        lambda *_args: None,
    )
    manifest = run_offline_audition(
        registry(), assets, audition, direct_candidate_ids=("qwen3-tts-base",), now=NOW
    )
    record_path = assets.root / manifest.results[0].result_path
    record = PersistedTrialResult.model_validate_json(record_path.read_bytes())
    assert record.normalized is not None
    result = record.worker_result
    assert result.output_path is not None and result.environment is not None
    paths = {
        "native": Path(result.output_path),
        "normalized": Path(record.normalized.path),
        "dependency": Path(result.output_path).parent / result.environment.dependency_evidence_path,
        "model-evidence": Path(result.output_path).parent
        / str(result.metadata["model_evidence_path"]),
        "executed-code": Path(result.output_path).parent
        / str(result.metadata["executed_code_evidence_path"]),
    }
    with paths[artifact].open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ContractError, match="digest or size changed"):
        run_offline_audition(
            registry(),
            assets,
            audition,
            direct_candidate_ids=("qwen3-tts-base",),
            now=NOW,
        )


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
    tmp_path: Path, monkeypatch, tamper: str, error: str
) -> None:
    assets, trial, record = completed_record(
        tmp_path, monkeypatch, experiment_id=f"audition-code-{tamper}"
    )
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
        validate_persisted_trial_evidence(assets, trial, changed)


@pytest.mark.parametrize(
    ("tamper", "error"),
    (
        ("candidate", "model evidence changed binding for candidate_id"),
        ("model-id", "model evidence changed binding for model_id"),
        ("model-revision", "model evidence changed binding for model_revision"),
        ("code-revision", "model evidence changed binding for code_revision"),
        ("reference", "model evidence changed binding for reference_sha256"),
        ("consent", "model evidence changed binding for consent_sha256"),
        ("source", "model evidence changed binding for source_audio_sha256"),
        ("settings", "model evidence changed binding for settings"),
        ("model-manifest", "model evidence changed binding for model_manifest_sha256"),
        ("runtime", "executed-code evidence changed binding for runtime_package_version"),
        ("watermark", "lacks watermarked evidence"),
        ("watermark-result", "claims watermark evidence that is not applicable"),
    ),
)
def test_persisted_model_evidence_rejects_digest_coherent_rebinding(
    tmp_path: Path, monkeypatch, tamper: str, error: str
) -> None:
    assets, trial, record = completed_record(
        tmp_path, monkeypatch, experiment_id=f"audition-model-{tamper}"
    )
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
    elif tamper == "watermark":
        model_payload["watermarked"] = False
    else:
        metadata["watermarked"] = False
    metadata["model_evidence_sha256"] = write_canonical_json(model_path, model_payload)
    changed = record.model_copy(
        update={"worker_result": result.model_copy(update={"metadata": metadata})}
    )
    with pytest.raises(ContractError, match=error):
        validate_persisted_trial_evidence(assets, trial, changed)


def test_plan_revalidates_prompt_suite_digest_and_equal_direct_matrix(tmp_path: Path) -> None:
    assets = layout(tmp_path)
    heldout = prompt()
    audition = build_audition_plan(
        registry(),
        assets,
        experiment_id="audition-fairness-reload",
        prompt_suite=prompt_suite(heldout),
        prompts=(heldout,),
        references=(reference(assets),),
        seeds=(7,),
        source_audio_by_prompt={heldout.prompt_id: performed_source(assets)},
        profile="smoke",
        direct_timing_modes=("cold", "warm"),
        created_at=NOW,
    )
    with pytest.raises(ValidationError, match="prompt-suite digest"):
        AuditionPlan.model_validate(
            audition.model_dump(mode="python") | {"prompt_suite_sha256": "0" * 64}
        )
    qwen = [trial for trial in audition.trials if trial.candidate_id == "qwen3-tts-base"]
    missing_cell = tuple(trial for trial in audition.trials if trial.trial_id != qwen[0].trial_id)
    with pytest.raises(ValidationError, match="held-out matrix differs"):
        AuditionPlan.model_validate(audition.model_dump(mode="python") | {"trials": missing_cell})

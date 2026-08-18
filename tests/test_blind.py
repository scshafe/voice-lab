from __future__ import annotations

import hashlib
import json
import os
import shutil
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import cole_voice_lab.blind as blind_module
from cole_voice_lab.audio import inspect_pcm_wav
from cole_voice_lab.audition import (
    AuditionPlan,
    AuditionRunManifest,
    AuditionTrial,
    NormalizedOutputEvidence,
    PersistedSourceEvidence,
    PersistedTrialResult,
    PersistedTrialSourceEvidence,
    RunResultPointer,
    audition_paths,
)
from cole_voice_lab.blind import (
    DIMENSIONS,
    BlindInput,
    ListeningScore,
    ReviewIndex,
    ReviewPackageManifest,
    ScoreSheet,
    VerifiedReviewPackage,
    aggregate_scores,
    blind_inputs_from_audition,
    build_review_package,
    verify_review_package,
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
    ExperimentRequest,
    ExperimentResult,
    Prompt,
    PromptSuite,
    ReferenceEvidence,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.runtime import LocalSourceManifest, RuntimeInstallReceipt, RuntimeTreeEntry


def _wav(path: Path, sample: int) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(sample.to_bytes(2, "little", signed=True) * 2_400)
    return sha256_file(path)[0]


def _layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "private" / "assets")


def _inputs(layout: AssetLayout) -> tuple[BlindInput, ...]:
    return _audition_inputs(layout, direct_candidates=("qwen-bf16", "f5-tts"))


def _audition_inputs(
    layout: AssetLayout,
    *,
    direct_candidates: tuple[str, ...],
    include_seed: bool = False,
) -> tuple[BlindInput, ...]:
    experiment_id = "blind-audition"
    paths = audition_paths(layout, experiment_id)
    reference_path = layout.directory("references") / "reference.wav"
    reference_sha256 = _wav(reference_path, 50)
    consent_path = layout.directory("provenance") / "consents" / "consent.json"
    consent_path.parent.mkdir(mode=0o700, exist_ok=True)
    consent_path.write_bytes(b"bound consent fixture")
    consent_sha256, _ = sha256_file(consent_path)
    reference = ReferenceEvidence(
        path=str(reference_path),
        sha256=reference_sha256,
        transcript="Exact held-out reference transcript.",
        subject_id="cole-self",
        consent_path=str(consent_path),
        consent_sha256=consent_sha256,
    )
    source_path = layout.directory("derived") / "performed-source.wav"
    source_sha256 = _wav(source_path, 75)
    _, source_size = sha256_file(source_path)

    identities = tuple((candidate, "direct-tts") for candidate in direct_candidates)
    if include_seed:
        identities = (*identities, ("seed-vc", "voice-conversion"))
    trials: list[AuditionTrial] = []
    for index, (candidate_id, track) in enumerate(identities, start=1):
        trial_id = f"{candidate_id}-trial"
        native = paths.experiment_directory / "native" / track / candidate_id / f"{trial_id}.wav"
        normalized = (
            paths.experiment_directory / "normalized" / track / candidate_id / f"{trial_id}.wav"
        )
        request = ExperimentRequest(
            experiment_id=experiment_id,
            trial_id=trial_id,
            candidate_id=candidate_id,
            track=track,
            text="Held out words.",
            output_path=str(native),
            reference=reference,
            source_audio_path=str(source_path) if track == "voice-conversion" else None,
            source_audio_sha256=source_sha256 if track == "voice-conversion" else None,
            source_audio_size_bytes=source_size if track == "voice-conversion" else None,
            seed=7,
            model_id=f"test/{candidate_id}",
            model_revision=str(index) * 40,
            code_revision=str(index + 3) * 40,
            settings={"profile": "smoke", "timing_mode": "cold", "prompt_id": "prompt-1"},
        )
        trials.append(
            AuditionTrial(
                trial_id=trial_id,
                candidate_id=candidate_id,
                track=track,
                profile="smoke",
                timing_mode="cold",
                runtime_id=f"runtime-{candidate_id}",
                runtime_lock_sha256=canonical_sha256(
                    {"candidate_id": candidate_id, "fixture": "runtime-lock"}
                ),
                request_sha256=canonical_sha256(request),
                request=request,
                normalized_output_path=str(normalized),
            )
        )
    prompt_suite = PromptSuite(
        suite_id="blind-held-out",
        purpose="held-out-evaluation",
        prompts=(
            Prompt(
                prompt_id="prompt-1",
                text="Held out words.",
                category="neutral",
                style="Neutral held-out delivery.",
                holdout=True,
                expected_seconds=2.0,
            ),
        ),
    )
    plan = AuditionPlan(
        experiment_id=experiment_id,
        profile="smoke",
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
        prompt_suite=prompt_suite,
        prompt_suite_sha256=canonical_sha256(prompt_suite),
        selected_prompt_ids=("prompt-1",),
        trials=tuple(trials),
    )
    plan_sha256 = write_canonical_json(paths.plan_path, plan)

    pointers: list[RunResultPointer] = []
    result_digests: dict[str, str] = {}
    for index, trial in enumerate(trials, start=1):
        native = Path(trial.request.output_path)
        normalized = Path(trial.normalized_output_path)
        native_sha256 = _wav(native, 100 * index)
        normalized_sha256 = _wav(normalized, 200 * index)
        _, native_size = sha256_file(native)
        _, normalized_size = sha256_file(normalized)
        evidence_dir = native.parent / "evidence"
        evidence_dir.mkdir(mode=0o700, exist_ok=True)
        dependency_path = evidence_dir / f"{trial.trial_id}.pip-freeze.txt"
        dependency_path.write_text("fixture==1.0\n")
        dependency_sha256, _ = sha256_file(dependency_path)
        source_files = {"weights.bin": hashlib.sha256(trial.request.model_id.encode()).hexdigest()}
        source_manifest = LocalSourceManifest(
            source_id=f"{trial.candidate_id}-model",
            kind="huggingface",
            repository=trial.request.model_id,
            revision=trial.request.model_revision,
            files=source_files,
        )
        model_manifest_sha256 = hashlib.sha256(
            canonical_json_bytes(source_manifest) + b"\n"
        ).hexdigest()
        source_integrity_record = {
            "schema_version": "cole-voice-lab.snapshot-integrity/v1",
            "source_id": source_manifest.source_id,
            "repository": source_manifest.repository,
            "revision": source_manifest.revision,
            "manifest_sha256": model_manifest_sha256,
            "files": source_files,
        }
        primary_source_evidence = PersistedSourceEvidence(
            source_id=source_manifest.source_id,
            kind="huggingface",
            repository=source_manifest.repository,
            revision=source_manifest.revision,
            manifest_sha256=model_manifest_sha256,
            manifest=source_manifest,
            files=source_files,
            integrity_record=source_integrity_record,
            integrity_sha256=canonical_sha256(source_integrity_record),
        )
        auxiliary_manifest_sha256: dict[str, str] = {}
        code_source_evidence = None
        if trial.track == "voice-conversion":
            code_files = {
                "inference.py": hashlib.sha256(trial.request.code_revision.encode()).hexdigest()
            }
            code_integrity_record = {
                "schema_version": "cole-voice-lab.git-checkout-integrity/v1",
                "revision": trial.request.code_revision,
                "tree_id": "a" * 40,
                "files": code_files,
            }
            code_source_evidence = PersistedSourceEvidence(
                source_id=f"{trial.candidate_id}-code",
                kind="git",
                repository=f"https://example.invalid/{trial.candidate_id}.git",
                revision=trial.request.code_revision,
                files=code_files,
                integrity_record=code_integrity_record,
                integrity_sha256=canonical_sha256(code_integrity_record),
            )
        code_checkout_integrity_sha256 = (
            code_source_evidence.integrity_sha256 if code_source_evidence is not None else None
        )
        runtime_package, runtime_package_version = {
            "f5-tts": ("f5-tts", "1.1.22"),
            "seed-vc": ("torch", "2.6.0"),
        }.get(trial.candidate_id, ("mlx-audio", "0.5.0"))
        code_path = evidence_dir / f"{trial.trial_id}.executed-code.json"
        code_evidence: dict[str, object] = {
            "schema_version": "cole-voice-lab.executed-code-evidence/v1",
            "worker_package": {
                "root_name": "cole_voice_lab",
                "files": {"workers/common.py": {"sha256": "a" * 64, "size_bytes": 1}},
            },
            "distributions": {
                "cole-voice-lab": {
                    "version": "0.1.0.dev0",
                    "files": {
                        "cole_voice_lab/__init__.py": {
                            "sha256": "b" * 64,
                            "size_bytes": 1,
                        }
                    },
                },
                runtime_package: {
                    "version": runtime_package_version,
                    "files": {"runtime.py": {"sha256": "c" * 64, "size_bytes": 1}},
                },
            },
        }
        code_evidence["integrity_sha256"] = canonical_sha256(code_evidence)
        code_sha256 = write_canonical_json(code_path, code_evidence)
        model_path = evidence_dir / f"{trial.trial_id}.model-evidence.json"
        model_sha256 = write_canonical_json(
            model_path,
            {
                "schema_version": "cole-voice-lab.worker-model-evidence/v1",
                "candidate_id": trial.candidate_id,
                "model_id": trial.request.model_id,
                "model_revision": trial.request.model_revision,
                "code_revision": trial.request.code_revision,
                "model_manifest_sha256": model_manifest_sha256,
                "auxiliary_manifest_sha256": auxiliary_manifest_sha256,
                "code_checkout_integrity_sha256": code_checkout_integrity_sha256,
                "executed_code_evidence_path": (f"evidence/{trial.trial_id}.executed-code.json"),
                "executed_code_evidence_sha256": code_sha256,
                "reference_sha256": trial.request.reference.sha256,
                "consent_sha256": trial.request.reference.consent_sha256,
                "source_audio_sha256": trial.request.source_audio_sha256,
                "source_audio_size_bytes": trial.request.source_audio_size_bytes,
                "settings": trial.request.settings,
                "runtime_package": runtime_package,
                "runtime_package_version": runtime_package_version,
            },
        )
        runtime_tree = (
            RuntimeTreeEntry(
                path="bin/python",
                kind="file",
                mode=0o500,
                size_bytes=1,
                sha256="d" * 64,
            ),
        )
        runtime_receipt = RuntimeInstallReceipt(
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
            installed_distributions={
                "cole-voice-lab": "0.1.0.dev0",
                runtime_package: runtime_package_version,
            },
            tree=runtime_tree,
            tree_sha256=canonical_sha256([entry.model_dump(mode="json") for entry in runtime_tree]),
        )
        runtime_receipt_path = (
            paths.experiment_directory / "evidence/runtime-receipts" / f"{trial.trial_id}.json"
        )
        runtime_receipt_sha256 = write_canonical_json(runtime_receipt_path, runtime_receipt)
        runtime_receipt_relative = runtime_receipt_path.relative_to(layout.root).as_posix()
        source_evidence = PersistedTrialSourceEvidence(
            trial_id=trial.trial_id,
            candidate_id=trial.candidate_id,
            request_sha256=trial.request_sha256,
            primary=primary_source_evidence,
            auxiliary={},
            code=code_source_evidence,
        )
        source_evidence_path = (
            paths.experiment_directory / "evidence/source-manifests" / f"{trial.trial_id}.json"
        )
        source_evidence_sha256 = write_canonical_json(source_evidence_path, source_evidence)
        source_evidence_relative = source_evidence_path.relative_to(layout.root).as_posix()
        worker_result = ExperimentResult(
            trial_id=trial.trial_id,
            candidate_id=trial.candidate_id,
            ok=True,
            output_path=str(native),
            output_sha256=native_sha256,
            output_bytes=native_size,
            audio=inspect_pcm_wav(native).audio,
            generation_seconds=0.1,
            realized_seed=trial.request.seed,
            environment=EnvironmentEvidence(
                python_version="3.12.12",
                platform="test",
                machine="arm64",
                dependency_fingerprint_sha256=dependency_sha256,
                dependency_evidence_path=f"evidence/{trial.trial_id}.pip-freeze.txt",
            ),
            metadata={
                "model_evidence_path": f"evidence/{trial.trial_id}.model-evidence.json",
                "model_evidence_sha256": model_sha256,
                "model_manifest_sha256": model_manifest_sha256,
                "auxiliary_manifest_sha256": auxiliary_manifest_sha256,
                "code_checkout_integrity_sha256": code_checkout_integrity_sha256,
                "executed_code_evidence_path": (f"evidence/{trial.trial_id}.executed-code.json"),
                "executed_code_evidence_sha256": code_sha256,
                "reference_sha256": trial.request.reference.sha256,
                "consent_sha256": trial.request.reference.consent_sha256,
                "source_audio_sha256": trial.request.source_audio_sha256,
                "source_audio_size_bytes": trial.request.source_audio_size_bytes,
                "runtime_package": runtime_package,
                "runtime_package_version": runtime_package_version,
                "runtime_receipt_path": runtime_receipt_relative,
                "runtime_receipt_sha256": runtime_receipt_sha256,
                "runtime_lock_sha256": trial.runtime_lock_sha256,
                "source_evidence_path": source_evidence_relative,
                "source_evidence_sha256": source_evidence_sha256,
            },
        )
        record = PersistedTrialResult(
            request_sha256=trial.request_sha256,
            completed_at=datetime(2026, 8, 17, 1, tzinfo=UTC),
            outcome="success",
            worker_result=worker_result,
            runtime_receipt_path=runtime_receipt_relative,
            runtime_receipt_sha256=runtime_receipt_sha256,
            source_evidence_path=source_evidence_relative,
            source_evidence_sha256=source_evidence_sha256,
            normalized=NormalizedOutputEvidence(
                path=str(normalized),
                sha256=normalized_sha256,
                size_bytes=normalized_size,
                settings_sha256="a" * 64,
                audio=inspect_pcm_wav(normalized).audio,
            ),
        )
        result_path = paths.results_directory / f"{trial.trial_id}.json"
        result_digest = write_canonical_json(result_path, record)
        result_digests[trial.trial_id] = result_digest
        pointers.append(
            RunResultPointer(
                trial_id=trial.trial_id,
                candidate_id=trial.candidate_id,
                request_sha256=trial.request_sha256,
                outcome="success",
                result_path=result_path.relative_to(layout.root).as_posix(),
                result_sha256=result_digest,
            )
        )
    run = AuditionRunManifest(
        experiment_id=experiment_id,
        plan_sha256=plan_sha256,
        updated_at=datetime(2026, 8, 17, 2, tzinfo=UTC),
        status="complete",
        direct_candidate_ids=direct_candidates,
        voice_conversion_candidate_ids=("seed-vc",) if include_seed else (),
        results=tuple(pointers),
    )
    run_sha256 = write_canonical_json(paths.run_manifest_path, run)
    return tuple(
        BlindInput(
            experiment_id=experiment_id,
            trial_id=trial.trial_id,
            plan_sha256=plan_sha256,
            run_manifest_sha256=run_sha256,
            result_sha256=result_digests[trial.trial_id],
        )
        for trial in sorted(trials, key=lambda item: item.trial_id)
    )


def _rebind_plan_human_input(
    layout: AssetLayout,
    *,
    target: str,
) -> str:
    """Write a digest-coherent plan whose human input escapes its boundary."""

    paths = audition_paths(layout, "blind-audition")
    plan = AuditionPlan.model_validate_json(paths.plan_path.read_bytes())
    outside = layout.root.parent / f"outside-{target}.wav"
    if target == "reference":
        digest = _wav(outside, 123)
    elif target == "consent":
        outside = outside.with_suffix(".json")
        outside.write_bytes(b"digest-coherent outside consent")
        digest, _ = sha256_file(outside)
    elif target == "source":
        digest = _wav(outside, 321)
        _, size = sha256_file(outside)
    else:  # pragma: no cover - test helper contract
        raise AssertionError(f"unsupported rebound target: {target}")
    changed_trials: list[AuditionTrial] = []
    for trial in plan.trials:
        request = trial.request
        if target == "reference":
            reference = request.reference.model_copy(
                update={"path": str(outside), "sha256": digest}
            )
            request = request.model_copy(update={"reference": reference})
        elif target == "consent":
            reference = request.reference.model_copy(
                update={"consent_path": str(outside), "consent_sha256": digest}
            )
            request = request.model_copy(update={"reference": reference})
        elif trial.track == "voice-conversion":
            request = request.model_copy(
                update={
                    "source_audio_path": str(outside),
                    "source_audio_sha256": digest,
                    "source_audio_size_bytes": size,
                }
            )
        changed_trials.append(
            trial.model_copy(
                update={"request": request, "request_sha256": canonical_sha256(request)}
            )
        )
    return write_canonical_json(
        paths.plan_path,
        plan.model_copy(update={"trials": tuple(changed_trials)}),
    )


def _sheet(package: object, scores: tuple[ListeningScore, ...]) -> ScoreSheet:
    return ScoreSheet(
        review_id="review-1",
        review_index_sha256=package.review_index_sha256,  # type: ignore[attr-defined]
        public_manifest_sha256=package.public_manifest_sha256,  # type: ignore[attr-defined]
        reviewer_id="cole-self",
        scored_at=datetime(2026, 8, 17, tzinfo=UTC),
        headphones_used=True,
        scores=scores,
    )


def _scores(package: VerifiedReviewPackage) -> tuple[ListeningScore, ...]:
    return tuple(
        ListeningScore(
            blind_id=entry.blind_id,
            likeness=3,
            naturalness=3,
            prosody=3,
            intelligibility=3,
            artifact_freedom=3,
        )
        for entry in package.reveal.entries
    )


def _reseal(
    layout: AssetLayout,
    package: VerifiedReviewPackage,
    *,
    index: ReviewIndex | None = None,
    reveal: blind_module.RevealMap | None = None,
) -> VerifiedReviewPackage:
    manifest = package.manifest
    files = manifest.files
    updates: dict[str, object] = {}

    def update_file(relative: str, digest: str, size: int) -> None:
        nonlocal files
        files = tuple(
            entry.model_copy(update={"sha256": digest, "size_bytes": size})
            if entry.path == relative
            else entry
            for entry in files
        )

    if index is not None:
        index_path = package.root / "public/review-index.json"
        index_digest = write_canonical_json(index_path, index, mode=0o400)
        _, index_size = sha256_file(index_path)
        update_file("public/review-index.json", index_digest, index_size)
        public_files = tuple(
            entry.model_copy(update={"sha256": index_digest, "size_bytes": index_size})
            if entry.path == "review-index.json"
            else entry
            for entry in package.public_manifest.files
        )
        public_manifest = package.public_manifest.model_copy(
            update={"review_index_sha256": index_digest, "files": public_files}
        )
        public_path = package.root / "public/review-manifest.json"
        public_digest = write_canonical_json(public_path, public_manifest, mode=0o400)
        _, public_size = sha256_file(public_path)
        update_file("public/review-manifest.json", public_digest, public_size)
        updates.update(
            {
                "review_index_sha256": index_digest,
                "public_manifest_sha256": public_digest,
            }
        )
    if reveal is not None:
        reveal_path = package.root / "private/reveal.json"
        reveal_digest = write_canonical_json(reveal_path, reveal, mode=0o400)
        _, reveal_size = sha256_file(reveal_path)
        update_file("private/reveal.json", reveal_digest, reveal_size)
        updates["reveal_sha256"] = reveal_digest
    updates["files"] = files
    write_canonical_json(
        package.root / "package-manifest.json",
        manifest.model_copy(update=updates),
        mode=0o400,
    )
    return verify_review_package(layout, review_id=package.index.review_id)


def test_blind_derivation_survives_source_runtime_eviction_and_reinstall(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _audition_inputs(layout, direct_candidates=("qwen-bf16", "f5-tts"))
    source_cache = layout.directory("models") / "sources/evicted-model"
    old_runtime = layout.directory("runtimes") / "runtime-qwen-bf16"
    source_cache.mkdir(mode=0o700, parents=True)
    old_runtime.mkdir(mode=0o700, parents=True)
    (source_cache / "weights.bin").write_bytes(b"resident cache")
    (old_runtime / "old-receipt").write_bytes(b"resident runtime")
    shutil.rmtree(source_cache)
    shutil.rmtree(old_runtime)

    old_runtime.mkdir(mode=0o700)
    (old_runtime / "different-receipt").write_bytes(b"new runtime at same ID")
    inputs = blind_inputs_from_audition(layout, experiment_id="blind-audition")
    assert {item.trial_id for item in inputs} == {"qwen-bf16-trial", "f5-tts-trial"}


def test_blind_input_derivation_rejects_outside_reference_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    _inputs(layout)
    _rebind_plan_human_input(layout, target="reference")

    def unexpected_resolution(_layout: AssetLayout, _item: BlindInput) -> None:
        raise AssertionError("path confinement must run before trial evidence resolution")

    monkeypatch.setattr(blind_module, "_resolve_blind_input", unexpected_resolution)
    with pytest.raises(ContractError, match="reference audio must remain under"):
        blind_inputs_from_audition(layout, experiment_id="blind-audition")


def test_review_build_rejects_outside_consent_before_run_binding(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    inputs = _inputs(layout)
    changed_plan_sha256 = _rebind_plan_human_input(layout, target="consent")
    changed_inputs = tuple(
        item.model_copy(update={"plan_sha256": changed_plan_sha256}) for item in inputs
    )

    with pytest.raises(ContractError, match="consent evidence must remain under"):
        build_review_package(
            layout,
            review_id="outside-consent-review",
            inputs=changed_inputs,
            key_hex="10" * 32,
        )


def test_review_verification_rejects_seed_source_path_rebound(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_audition_inputs(
            layout,
            direct_candidates=("qwen-bf16", "f5-tts"),
            include_seed=True,
        ),
        key_hex="12" * 32,
    )
    changed_plan_sha256 = _rebind_plan_human_input(layout, target="source")
    changed_reveal = package.reveal.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"plan_sha256": changed_plan_sha256})
                for entry in package.reveal.entries
            )
        }
    )

    with pytest.raises(ContractError, match="performed source audio must remain under"):
        _reseal(layout, package, reveal=changed_reveal)


def test_review_is_bound_sealed_and_public_index_is_blind(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    inputs = _inputs(layout)
    assert blind_inputs_from_audition(layout, experiment_id="blind-audition") == inputs
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=inputs,
        key_hex="11" * 32,
    )

    public = (package.root / "public" / "review-index.json").read_text()
    assert "qwen" not in public
    assert "f5-tts" not in public
    assert (package.root / "public" / "index.html").exists()
    assert "../package-manifest.json" not in (package.root / "public" / "index.html").read_text()
    assert not any("private/" in entry.path for entry in package.public_manifest.files)
    assert {entry.candidate_id for entry in package.reveal.entries} == {
        "qwen-bf16",
        "f5-tts",
    }
    assert all(
        (package.root / "public" / sample.audio_path).exists() for sample in package.index.samples
    )
    assert all(
        not path.stat().st_mode & 0o222 for path in package.root.rglob("*") if path.is_file()
    )
    assert verify_review_package(layout, review_id="review-1") == package


def test_review_rejects_outside_or_tampered_input(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    inputs = list(_inputs(layout))
    inputs[0] = inputs[0].model_copy(update={"result_sha256": "0" * 64})
    with pytest.raises(ContractError, match="trial pointer"):
        build_review_package(
            layout,
            review_id="review-1",
            inputs=tuple(inputs),
            key_hex="22" * 32,
        )
    inputs = list(_inputs(layout))
    inputs[0] = inputs[0].model_copy(update={"plan_sha256": "0" * 64})
    with pytest.raises(ContractError, match="audition plan"):
        build_review_package(
            layout,
            review_id="review-2",
            inputs=tuple(inputs),
            key_hex="22" * 32,
        )


@pytest.mark.parametrize("target", ["normalized", "dependency"])
def test_review_rehashes_audition_artifacts_before_copy(tmp_path: Path, target: str) -> None:
    layout = _layout(tmp_path)
    inputs = _inputs(layout)
    experiment = audition_paths(layout, "blind-audition").experiment_directory
    if target == "normalized":
        path = next((experiment / "normalized").rglob("*.wav"))
    else:
        path = next(experiment.rglob("*.pip-freeze.txt"))
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ContractError, match="digest or size"):
        build_review_package(
            layout,
            review_id=f"tamper-{target}",
            inputs=inputs,
            key_hex="23" * 32,
        )


def test_review_rejects_realized_seed_and_semantic_model_drift(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    inputs = list(_inputs(layout))
    target = inputs[0]
    paths = audition_paths(layout, target.experiment_id)
    result_path = paths.results_directory / f"{target.trial_id}.json"
    result = PersistedTrialResult.model_validate_json(result_path.read_bytes())
    changed_worker = result.worker_result.model_copy(update={"realized_seed": 999})
    changed_result_sha256 = write_canonical_json(
        result_path, result.model_copy(update={"worker_result": changed_worker})
    )
    run = AuditionRunManifest.model_validate_json(paths.run_manifest_path.read_bytes())
    changed_pointers = tuple(
        pointer.model_copy(update={"result_sha256": changed_result_sha256})
        if pointer.trial_id == target.trial_id
        else pointer
        for pointer in run.results
    )
    changed_run_sha256 = write_canonical_json(
        paths.run_manifest_path, run.model_copy(update={"results": changed_pointers})
    )
    changed_inputs = tuple(
        item.model_copy(
            update={
                "run_manifest_sha256": changed_run_sha256,
                "result_sha256": (
                    changed_result_sha256
                    if item.trial_id == target.trial_id
                    else item.result_sha256
                ),
            }
        )
        for item in inputs
    )
    with pytest.raises(ContractError, match="realized seed"):
        build_review_package(
            layout,
            review_id="seed-drift-review",
            inputs=changed_inputs,
            key_hex="24" * 32,
        )


def test_review_parses_model_and_executed_code_evidence(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    inputs = list(_inputs(layout))
    target = inputs[0]
    paths = audition_paths(layout, target.experiment_id)
    result_path = paths.results_directory / f"{target.trial_id}.json"
    result = PersistedTrialResult.model_validate_json(result_path.read_bytes())
    model_relative = result.worker_result.metadata["model_evidence_path"]
    assert isinstance(model_relative, str)
    assert result.worker_result.output_path is not None
    model_path = Path(result.worker_result.output_path).parent / model_relative
    model_payload = json.loads(model_path.read_bytes())
    model_payload["candidate_id"] = "attacker-choice"
    changed_model_sha256 = write_canonical_json(model_path, model_payload)
    changed_metadata = {
        **result.worker_result.metadata,
        "model_evidence_sha256": changed_model_sha256,
    }
    changed_worker = result.worker_result.model_copy(update={"metadata": changed_metadata})
    changed_result_sha256 = write_canonical_json(
        result_path, result.model_copy(update={"worker_result": changed_worker})
    )
    run = AuditionRunManifest.model_validate_json(paths.run_manifest_path.read_bytes())
    changed_pointers = tuple(
        pointer.model_copy(update={"result_sha256": changed_result_sha256})
        if pointer.trial_id == target.trial_id
        else pointer
        for pointer in run.results
    )
    changed_run_sha256 = write_canonical_json(
        paths.run_manifest_path, run.model_copy(update={"results": changed_pointers})
    )
    changed_inputs = tuple(
        item.model_copy(
            update={
                "run_manifest_sha256": changed_run_sha256,
                "result_sha256": (
                    changed_result_sha256
                    if item.trial_id == target.trial_id
                    else item.result_sha256
                ),
            }
        )
        for item in inputs
    )
    with pytest.raises(ContractError, match="model evidence changed binding"):
        build_review_package(
            layout,
            review_id="model-drift-review",
            inputs=changed_inputs,
            key_hex="25" * 32,
        )


def test_review_rejects_hardlinked_audition_audio(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    inputs = _inputs(layout)
    normalized = next(
        (audition_paths(layout, "blind-audition").experiment_directory / "normalized").rglob(
            "*.wav"
        )
    )
    os.link(normalized, normalized.with_suffix(".alias.wav"))
    with pytest.raises(SecurityError, match="single-link"):
        build_review_package(
            layout,
            review_id="hardlink-review",
            inputs=inputs,
            key_hex="24" * 32,
        )


def test_review_requires_complete_run_and_two_direct_candidates(tmp_path: Path) -> None:
    layout = _layout(tmp_path / "incomplete")
    inputs = _inputs(layout)
    run_path = audition_paths(layout, "blind-audition").run_manifest_path
    run = AuditionRunManifest.model_validate_json(run_path.read_bytes())
    changed_digest = write_canonical_json(
        run_path, run.model_copy(update={"status": "in-progress"})
    )
    changed_inputs = tuple(
        item.model_copy(update={"run_manifest_sha256": changed_digest}) for item in inputs
    )
    with pytest.raises(ContractError, match="complete failure-free"):
        build_review_package(
            layout,
            review_id="incomplete-review",
            inputs=changed_inputs,
            key_hex="25" * 32,
        )

    layout = _layout(tmp_path / "single")
    single = _audition_inputs(layout, direct_candidates=("qwen-bf16",))
    with pytest.raises(ContractError, match="at least two"):
        build_review_package(
            layout,
            review_id="single-review",
            inputs=single,
            key_hex="26" * 32,
        )


def test_scores_require_exact_package_and_complete_coverage(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="33" * 32,
    )
    scores = tuple(
        ListeningScore(
            blind_id=entry.blind_id,
            likeness=5 if entry.candidate_id == "qwen-bf16" else 2,
            naturalness=5 if entry.candidate_id == "qwen-bf16" else 2,
            prosody=5 if entry.candidate_id == "qwen-bf16" else 2,
            intelligibility=5 if entry.candidate_id == "qwen-bf16" else 2,
            artifact_freedom=5 if entry.candidate_id == "qwen-bf16" else 2,
        )
        for entry in package.reveal.entries
    )
    sheet = _sheet(package, scores)
    summary = aggregate_scores(layout, sheet)
    direct = summary["tracks"]["direct-tts"]["candidates"]  # type: ignore[index]
    assert direct[0]["candidate_id"] == "qwen-bf16"

    with pytest.raises(ContractError, match="cover"):
        aggregate_scores(layout, sheet.model_copy(update={"scores": (scores[0],)}))
    with pytest.raises(ContractError, match="exact review index"):
        aggregate_scores(
            layout,
            sheet.model_copy(update={"review_index_sha256": "0" * 64}),
        )


def test_package_tamper_is_detected_before_reveal_use(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="44" * 32,
    )
    reveal_path = package.root / "private" / "reveal.json"
    reveal_path.chmod(0o600)
    changed = package.reveal.entries[0].model_copy(update={"candidate_id": "attacker-choice"})
    write_canonical_json(
        reveal_path,
        package.reveal.model_copy(update={"entries": (changed, *package.reveal.entries[1:])}),
        mode=0o400,
    )
    with pytest.raises(SecurityError, match="digest changed"):
        verify_review_package(layout, review_id="review-1")


def test_ranking_is_partitioned_by_track(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    inputs = _audition_inputs(
        layout,
        direct_candidates=("qwen-bf16", "f5-tts"),
        include_seed=True,
    )
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=inputs,
        key_hex="66" * 32,
    )
    scores = tuple(
        ListeningScore(
            blind_id=entry.blind_id,
            likeness=5,
            naturalness=5,
            prosody=5,
            intelligibility=5,
            artifact_freedom=5,
        )
        for entry in package.reveal.entries
    )
    summary = aggregate_scores(layout, _sheet(package, scores))
    assert set(summary["tracks"]) == {"direct-tts", "voice-conversion"}  # type: ignore[arg-type]


def test_blind_contracts_reject_ambiguous_evidence(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    inputs = _inputs(layout)
    with pytest.raises(ValidationError, match="Extra inputs"):
        BlindInput.model_validate({**inputs[0].model_dump(), "candidate_id": "forged-label"})

    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=inputs,
        key_hex="77" * 32,
    )
    with pytest.raises(ValidationError, match="explicit timezone"):
        ScoreSheet(
            review_id="review-1",
            review_index_sha256=package.review_index_sha256,
            public_manifest_sha256=package.public_manifest_sha256,
            reviewer_id="cole-self",
            scored_at=datetime(2026, 8, 17),
            headphones_used=True,
            scores=_scores(package),
        )

    first, second = package.index.samples
    with pytest.raises(ValidationError, match="IDs must be unique"):
        ReviewIndex(
            review_id="review-1",
            created_at=package.index.created_at,
            key_commitment_sha256=package.index.key_commitment_sha256,
            samples=(first, first),
        )
    with pytest.raises(ValidationError, match="fixed complete"):
        ReviewIndex(
            review_id="review-1",
            created_at=package.index.created_at,
            key_commitment_sha256=package.index.key_commitment_sha256,
            dimensions=tuple(reversed(DIMENSIONS)),
            samples=(first, second),
        )
    with pytest.raises(ValidationError, match="contiguous"):
        ReviewIndex(
            review_id="review-1",
            created_at=package.index.created_at,
            key_commitment_sha256=package.index.key_commitment_sha256,
            samples=(first.model_copy(update={"order": 2}), second),
        )
    with pytest.raises(ValidationError, match="paths must be unique"):
        ReviewPackageManifest(
            review_id="review-1",
            created_at=package.manifest.created_at,
            review_index_sha256=package.manifest.review_index_sha256,
            public_manifest_sha256=package.manifest.public_manifest_sha256,
            reveal_sha256=package.manifest.reveal_sha256,
            files=(package.manifest.files[0], package.manifest.files[0]),
        )


def test_build_rejects_empty_duplicate_bad_key_and_blind_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(ContractError, match="at least one"):
        build_review_package(layout, review_id="empty-review", inputs=())
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        build_review_package(
            layout,
            review_id="short-key",
            inputs=_inputs(layout),
            key_hex="11",
        )

    build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="88" * 32,
    )
    with pytest.raises(FileExistsError):
        build_review_package(
            layout,
            review_id="review-1",
            inputs=_inputs(layout),
            key_hex="88" * 32,
        )

    monkeypatch.setattr(blind_module, "_blind_id", lambda _key, _item: "sample-0000000000000000")
    with pytest.raises(ContractError, match="collision"):
        build_review_package(
            layout,
            review_id="collision-review",
            inputs=_inputs(layout),
            key_hex="99" * 32,
        )


def test_build_rejects_symlinked_output_parent(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    evaluations = layout.directory("evaluations")
    target = evaluations / "other"
    target.mkdir()
    (evaluations / "blind-reviews").symlink_to(target, target_is_directory=True)
    with pytest.raises(SecurityError, match="output parent"):
        build_review_package(
            layout,
            review_id="review-1",
            inputs=_inputs(layout),
            key_hex="aa" * 32,
        )


def test_verify_rejects_noncanonical_and_oversized_evidence(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="ab" * 32,
    )
    manifest_path = package.root / "package-manifest.json"
    payload = manifest_path.read_bytes()
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(b" " + payload)
    manifest_path.chmod(0o400)
    with pytest.raises(ContractError, match="not canonical JSON"):
        verify_review_package(layout, review_id="review-1")

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (16 * 1024 * 1024))
    with pytest.raises(ContractError, match="size limit"):
        blind_module._read_canonical_model(oversized, ReviewPackageManifest)


def test_verify_rejects_manifest_and_component_binding_tamper(tmp_path: Path) -> None:
    layout = _layout(tmp_path / "manifest-id")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="ac" * 32,
    )
    write_canonical_json(
        package.root / "package-manifest.json",
        package.manifest.model_copy(update={"review_id": "review-other"}),
        mode=0o400,
    )
    with pytest.raises(ContractError, match="differs from its path"):
        verify_review_package(layout, review_id="review-1")

    layout = _layout(tmp_path / "component-id")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="ad" * 32,
    )
    with pytest.raises(ContractError, match="component review IDs"):
        _reseal(
            layout,
            package,
            reveal=package.reveal.model_copy(update={"review_id": "review-other"}),
        )

    layout = _layout(tmp_path / "commitment")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="ae" * 32,
    )
    write_canonical_json(
        package.root / "package-manifest.json",
        package.manifest.model_copy(update={"review_index_sha256": "0" * 64}),
        mode=0o400,
    )
    with pytest.raises(SecurityError, match="component commitment"):
        verify_review_package(layout, review_id="review-1")

    layout = _layout(tmp_path / "sample-binding")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="af" * 32,
    )
    bad_sample = package.index.samples[0].model_copy(update={"audio_sha256": "0" * 64})
    with pytest.raises(SecurityError, match="sample audio"):
        _reseal(
            layout,
            package,
            index=package.index.model_copy(
                update={"samples": (bad_sample, *package.index.samples[1:])}
            ),
        )


def test_verify_rejects_file_set_symlink_and_writable_tamper(tmp_path: Path) -> None:
    layout = _layout(tmp_path / "extra")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b0" * 32,
    )
    extra = package.root / "public" / "extra.txt"
    extra.write_text("unexpected")
    extra.chmod(0o400)
    with pytest.raises(ContractError, match="file set differs"):
        verify_review_package(layout, review_id="review-1")

    layout = _layout(tmp_path / "symlink")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b1" * 32,
    )
    (package.root / "public" / "linked").symlink_to(package.root / "public" / "index.html")
    with pytest.raises(SecurityError, match="contains a symlink"):
        verify_review_package(layout, review_id="review-1")

    layout = _layout(tmp_path / "writable")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b2" * 32,
    )
    (package.root / "public" / "index.html").chmod(0o600)
    with pytest.raises(SecurityError, match="remains writable"):
        verify_review_package(layout, review_id="review-1")


def test_aggregate_rejects_score_and_reveal_integrity_failures(tmp_path: Path) -> None:
    layout = _layout(tmp_path / "package-digest")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b3" * 32,
    )
    with pytest.raises(ContractError, match="exact public review package"):
        aggregate_scores(
            layout,
            _sheet(package, _scores(package)).model_copy(
                update={"public_manifest_sha256": "0" * 64}
            ),
        )

    layout = _layout(tmp_path / "key")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b4" * 32,
    )
    package = _reseal(
        layout,
        package,
        index=package.index.model_copy(update={"key_commitment_sha256": "0" * 64}),
    )
    with pytest.raises(ContractError, match="public key commitment"):
        aggregate_scores(layout, _sheet(package, _scores(package)))

    layout = _layout(tmp_path / "duplicate-reveal")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b5" * 32,
    )
    duplicate = package.reveal.entries[0]
    with pytest.raises(ContractError, match="unique|exactly|absent"):
        _reseal(
            layout,
            package,
            reveal=package.reveal.model_copy(update={"entries": (duplicate, duplicate)}),
        )

    layout = _layout(tmp_path / "duplicate-scores")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b6" * 32,
    )
    score = _scores(package)[0]
    with pytest.raises(ContractError, match="score sheet contains duplicate"):
        aggregate_scores(layout, _sheet(package, (score, score)))


def test_aggregate_rejects_reveal_binding_and_cross_track_candidate(tmp_path: Path) -> None:
    layout = _layout(tmp_path / "public-binding")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b7" * 32,
    )
    first = package.reveal.entries[0]
    wrong_track = "voice-conversion" if first.track == "direct-tts" else "direct-tts"
    with pytest.raises(ContractError, match="attribution"):
        _reseal(
            layout,
            package,
            reveal=package.reveal.model_copy(
                update={
                    "entries": (
                        first.model_copy(update={"track": wrong_track}),
                        *package.reveal.entries[1:],
                    )
                }
            ),
        )

    layout = _layout(tmp_path / "hmac-binding")
    package = build_review_package(
        layout,
        review_id="review-1",
        inputs=_inputs(layout),
        key_hex="b8" * 32,
    )
    first = package.reveal.entries[0]
    with pytest.raises(ContractError, match="attribution"):
        _reseal(
            layout,
            package,
            reveal=package.reveal.model_copy(
                update={
                    "entries": (
                        first.model_copy(update={"candidate_id": "attacker-choice"}),
                        *package.reveal.entries[1:],
                    )
                }
            ),
        )

    layout = _layout(tmp_path / "cross-track")
    inputs = _inputs(layout)
    with pytest.raises(ContractError, match="every selected|every selected audition cell"):
        build_review_package(
            layout,
            review_id="review-1",
            inputs=(inputs[0],),
            key_hex="b9" * 32,
        )

"""Stored audition runs, as the retired bake-off wrote them (D-057), for blind review's tests.

Nothing writes an audition run any more; blind review reads the runs already on disk, and
these fixtures write one by hand, with every record and piece of evidence its validator reads.
"""

from __future__ import annotations

import hashlib
import wave
from datetime import UTC, datetime
from pathlib import Path

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
from cole_voice_lab.blind import BlindInput
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
    write_canonical_json,
)
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


def wav(path: Path, sample: int) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(sample.to_bytes(2, "little", signed=True) * 2_400)
    return sha256_file(path)[0]


def write_audition_run(
    layout: AssetLayout,
    *,
    direct_candidates: tuple[str, ...],
    include_seed: bool = False,
    historical: bool = False,
) -> tuple[BlindInput, ...]:
    """Write a complete audition run, as the bake-off left one, and return its blind inputs.

    ``historical`` makes it a run before D-050: its reference, model evidence and results name
    a consent record's digest, and the record itself does not exist (nothing reads it).
    """
    experiment_id = "blind-audition"
    paths = audition_paths(layout, experiment_id)
    reference_path = layout.directory("references") / "reference.wav"
    reference_sha256 = wav(reference_path, 50)
    consent: dict[str, str] = {}
    if historical:
        consent = {
            "consent_path": str(layout.root / "provenance/consents/cole-self-v1.json"),
            "consent_sha256": "c" * 64,
        }
    reference = ReferenceEvidence(
        path=str(reference_path),
        sha256=reference_sha256,
        transcript="Exact held-out reference transcript.",
        subject_id="cole-self",
        **consent,
    )
    source_path = layout.directory("derived") / "performed-source.wav"
    source_sha256 = wav(source_path, 75)
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
        native_sha256 = wav(native, 100 * index)
        normalized_sha256 = wav(normalized, 200 * index)
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
                "source_audio_sha256": trial.request.source_audio_sha256,
                "source_audio_size_bytes": trial.request.source_audio_size_bytes,
                "settings": trial.request.settings,
                "runtime_package": runtime_package,
                "runtime_package_version": runtime_package_version,
                **({"consent_sha256": "c" * 64} if historical else {}),
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
                "source_audio_sha256": trial.request.source_audio_sha256,
                "source_audio_size_bytes": trial.request.source_audio_size_bytes,
                "runtime_package": runtime_package,
                "runtime_package_version": runtime_package_version,
                "runtime_receipt_path": runtime_receipt_relative,
                "runtime_receipt_sha256": runtime_receipt_sha256,
                "runtime_lock_sha256": trial.runtime_lock_sha256,
                "source_evidence_path": source_evidence_relative,
                "source_evidence_sha256": source_evidence_sha256,
                **({"consent_sha256": "c" * 64} if historical else {}),
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

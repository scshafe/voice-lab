from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from audition_fixtures import wav, write_audition_run
from pydantic import ValidationError

import cole_voice_lab.blind as blind_module
from cole_voice_lab.audition import (
    AuditionPlan,
    AuditionRunManifest,
    AuditionTrial,
    PersistedTrialResult,
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
    canonical_sha256,
    sha256_file,
    write_canonical_json,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.paths import AssetLayout


def _layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "private" / "assets")


def _inputs(layout: AssetLayout) -> tuple[BlindInput, ...]:
    return write_audition_run(layout, direct_candidates=("qwen-bf16", "f5-tts"))


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
        digest = wav(outside, 123)
    elif target == "consent":
        outside = outside.with_suffix(".json")
        outside.write_bytes(b"digest-coherent outside consent")
        digest, _ = sha256_file(outside)
    elif target == "source":
        digest = wav(outside, 321)
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
    write_audition_run(layout, direct_candidates=("qwen-bf16", "f5-tts"))
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
    # A plan written before D-050 may name a consent record: never read, still confined.
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
        inputs=write_audition_run(
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
    single = write_audition_run(layout, direct_candidates=("qwen-bf16",))
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
    inputs = write_audition_run(
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

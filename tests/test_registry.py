from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.errors import ContractError
from cole_voice_lab.registry import (
    CandidateConfiguration,
    CandidateProfileLock,
    ModelSourcesLock,
    RuntimeLock,
    SourceFileLock,
    SourceLock,
    is_immutable_revision,
    load_registry,
    verify_runtime_requirements,
)

ROOT = Path(__file__).resolve().parents[1]


def registry():
    return load_registry(ROOT / "config/candidates.toml", ROOT / "config/model-sources.lock.json")


def test_registry_resolves_exact_quality_and_smoke_profiles() -> None:
    loaded = registry()
    loaded.verify_requirements(ROOT)
    assert {candidate.candidate_id for candidate in loaded.candidates()} == {
        "qwen3-tts-base",
        "chatterbox-turbo",
        "chatterbox-v3",
        "f5-tts",
        "seed-vc",
    }

    qwen_quality = loaded.resolve("qwen3-tts-base", "quality")
    qwen_smoke = loaded.resolve("qwen3-tts-base", "smoke")
    turbo_quality = loaded.resolve("chatterbox-turbo", "quality")
    turbo_smoke = loaded.resolve("chatterbox-turbo", "smoke")
    assert qwen_quality.model_id.endswith("-bf16")
    assert qwen_smoke.model_id.endswith("-8bit")
    assert turbo_quality.model_id.endswith("-fp16")
    assert turbo_smoke.model_id.endswith("-8bit")
    assert qwen_quality.runtime.runtime_id == turbo_quality.runtime.runtime_id
    assert loaded.resolve("chatterbox-v3", "quality").runtime.runtime_id == ("mlx-audio-0.5.0")
    assert loaded.resolve("f5-tts", "quality").runtime.runtime_id != (
        qwen_quality.runtime.runtime_id
    )
    assert loaded.resolve("seed-vc", "quality").candidate.track == "voice-conversion"


def test_registry_locks_exact_files_and_revisions() -> None:
    loaded = registry()
    for candidate in loaded.candidates():
        for profile in ("smoke", "quality"):
            resolved = loaded.resolve(candidate.candidate_id, profile)
            assert is_immutable_revision(resolved.model_revision)
            assert is_immutable_revision(resolved.code_revision)
            assert resolved.model_source.allow_patterns
            assert all("*" not in path for path in resolved.model_source.allow_patterns)
            assert {item.path for item in resolved.model_source.files} == set(
                resolved.model_source.allow_patterns
            )
    assert not is_immutable_revision("main")
    assert not is_immutable_revision("abc123")


def test_registry_rejects_mutable_or_globbed_model_sources() -> None:
    payload = json.loads((ROOT / "config/model-sources.lock.json").read_text())
    payload["sources"][5]["revision"] = "main"
    with pytest.raises(ValidationError, match="revision"):
        ModelSourcesLock.model_validate_json(json.dumps(payload))

    payload = json.loads((ROOT / "config/model-sources.lock.json").read_text())
    payload["sources"][5]["allow_patterns"][0] = "*.json"
    payload["sources"][5]["files"][0]["path"] = "*.json"
    with pytest.raises(ValidationError, match="exact paths|POSIX"):
        ModelSourcesLock.model_validate_json(json.dumps(payload))


def test_registry_unknown_keys_fail_closed() -> None:
    loaded = registry()
    with pytest.raises(ContractError, match="unknown candidate"):
        loaded.candidate("not-a-candidate")
    with pytest.raises(ContractError, match="unknown runtime"):
        loaded.runtime("not-a-runtime")
    with pytest.raises(ContractError, match="unknown locked source"):
        loaded.source("not-a-source")


def test_requirements_digest_drift_is_detected(tmp_path: Path) -> None:
    loaded = registry()
    for runtime in loaded.lock.runtimes:
        path = tmp_path / runtime.requirements_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("drift\n")
    with pytest.raises(ContractError, match="requirements digest mismatch"):
        loaded.verify_requirements(tmp_path)


def test_complete_artifact_lock_rejects_unhashed_and_missing_roots(tmp_path: Path) -> None:
    runtime = registry().runtime("mlx-audio-0.5.0")
    requirements = tmp_path / runtime.requirements_path
    artifact_lock = tmp_path / runtime.artifact_lock_path
    requirements.parent.mkdir(parents=True)
    requirements.write_bytes((ROOT / runtime.requirements_path).read_bytes())
    artifact_lock.write_text("mlx-audio==0.5.0\n", encoding="utf-8")
    requirements_sha256, _ = sha256_file(requirements)
    artifact_sha256, _ = sha256_file(artifact_lock)
    unsafe = runtime.model_copy(
        update={
            "requirements_sha256": requirements_sha256,
            "artifact_lock_sha256": artifact_sha256,
        }
    )
    with pytest.raises(ContractError, match="unsafe or unpinned"):
        verify_runtime_requirements(unsafe, tmp_path)

    artifact_lock.write_text(
        "pip==26.0 --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    artifact_sha256, _ = sha256_file(artifact_lock)
    missing = unsafe.model_copy(update={"artifact_lock_sha256": artifact_sha256})
    with pytest.raises(ContractError, match="does not bind root mlx-audio"):
        verify_runtime_requirements(missing, tmp_path)


def test_lock_models_reject_ambiguous_and_duplicate_entries() -> None:
    loaded = registry()
    duplicate_candidate = loaded.configuration.candidates[0]
    with pytest.raises(ValidationError, match="unique"):
        CandidateConfiguration(candidates=(duplicate_candidate, duplicate_candidate))
    with pytest.raises(ValidationError, match="exact paths"):
        SourceLock(
            source_id="bad-source",
            kind="huggingface",
            repository="repo/model",
            revision="a" * 40,
            license="MIT",
            allow_patterns=("*.bin",),
            files=(SourceFileLock(path="weights.bin", size_bytes=1),),
        )
    with pytest.raises(ValidationError, match="curated file allow-list"):
        SourceLock(
            source_id="empty-source",
            kind="huggingface",
            repository="repo/model",
            revision="a" * 40,
            license="MIT",
        )
    with pytest.raises(ValidationError, match="do not use"):
        SourceLock(
            source_id="git-source",
            kind="git",
            repository="https://example.invalid/repo.git",
            revision="a" * 40,
            license="MIT",
            allow_patterns=("weights.bin",),
            files=(SourceFileLock(path="weights.bin", size_bytes=1),),
        )
    with pytest.raises(ValidationError, match="exact versions"):
        RuntimeLock(
            runtime_id="bad-runtime",
            python_version="3.12",
            target_platform="cpython-3.12-macos-arm64",
            requirements_path="requirements/bad.txt",
            requirements_sha256="a" * 64,
            artifact_lock_path="requirements/bad.artifacts.txt",
            artifact_lock_sha256="b" * 64,
            estimated_install_bytes=1,
            packages={"package": ">=1"},
            reviewed_build_toolchain=None,
            python_runtime=loaded.runtime("mlx-audio-0.5.0").python_runtime,
            code_source_id="code",
        )
    f5_runtime = loaded.runtime("f5-tts-1.1.22-mps")
    with pytest.raises(ValidationError, match="hash-locked build toolchain"):
        RuntimeLock.model_validate(
            f5_runtime.model_dump(mode="python") | {"reviewed_build_toolchain": None}
        )
    with pytest.raises(ValidationError, match="auxiliary source IDs"):
        CandidateProfileLock(
            candidate_id="candidate",
            profile="smoke",
            model_source_id="model",
            auxiliary_source_ids=("aux", "aux"),
        )


def test_registry_requires_every_candidate_profile() -> None:
    loaded = registry()
    shortened = loaded.lock.model_copy(update={"profiles": loaded.lock.profiles[:-1]})
    with pytest.raises(ContractError, match="profile locks disagree"):
        type(loaded)(loaded.configuration, shortened)

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.errors import ContractError
from cole_voice_lab.registry import (
    CandidateProfileLock,
    CandidateRegistry,
    ModelSourcesLock,
    ReviewedBuildToolchainLock,
    ReviewedBuiltWheelLock,
    RuntimeLock,
    SourceLock,
    _read_runtime_requirement_entries,
    canonical_package_name,
    load_registry,
    verify_runtime_requirements,
)

ROOT = Path(__file__).resolve().parents[1]


def registry() -> CandidateRegistry:
    return load_registry(ROOT / "config/candidates.toml", ROOT / "config/model-sources.lock.json")


def _validation(model: type, payload: object, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        model.model_validate(payload)


def test_source_lock_duplicate_and_mismatched_paths_fail_closed() -> None:
    base = {
        "source_id": "fixture",
        "kind": "huggingface",
        "repository": "example/fixture",
        "revision": "a" * 40,
        "license": "MIT",
        "allow_patterns": ("weights.bin",),
        "files": ({"path": "weights.bin", "size_bytes": 1},),
    }
    _validation(
        SourceLock,
        base | {"allow_patterns": ("weights.bin", "weights.bin")},
        "allow-patterns must be unique",
    )
    _validation(
        SourceLock,
        base
        | {
            "allow_patterns": ("weights.bin", "other.bin"),
            "files": (
                {"path": "weights.bin", "size_bytes": 1},
                {"path": "weights.bin", "size_bytes": 1},
            ),
        },
        "file locks must have unique paths",
    )
    _validation(
        SourceLock,
        base | {"allow_patterns": ("weights.bin", "other.bin")},
        "every allowed Hugging Face file",
    )


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"source_filename": "../source.tar.gz"}, "one path component"),
        ({"source_filename": "source.whl"}, "sdist archive"),
        ({"wheel_filename": "output.tar.gz"}, "output must be a wheel"),
        ({"compiler": None, "sdk_version": "15.0"}, "SDK version requires"),
        ({"compiler": "clang", "sdk_version": None}, "require SDK evidence"),
        (
            {
                "build_environment": {
                    "LC_ALL": "C",
                    "PYTHONHASHSEED": "0",
                    "SOURCE_DATE_EPOCH": "315532800",
                    "UNRELATED": "1",
                }
            },
            "environment is incomplete",
        ),
        (
            {
                "build_environment": {
                    "LC_ALL": "C",
                    "PYTHONHASHSEED": "0",
                    "SOURCE_DATE_EPOCH": "315532801",
                    "TZ": "UTC",
                }
            },
            "epoch disagrees",
        ),
    ),
)
def test_reviewed_wheel_build_contract_rejects_ambiguous_recipe(
    updates: dict[str, object],
    message: str,
) -> None:
    evidence = registry().runtime("f5-tts-1.1.22-mps").reviewed_built_wheels[0]
    _validation(
        ReviewedBuiltWheelLock,
        evidence.model_dump(mode="python") | updates,
        message,
    )


def test_reviewed_build_toolchain_requires_exact_unique_tools() -> None:
    toolchain = registry().runtime("f5-tts-1.1.22-mps").reviewed_build_toolchain
    assert toolchain is not None
    tool = toolchain.tool_wheels[0]
    _validation(
        type(tool),
        tool.model_dump(mode="python") | {"filename": "../packaging.whl"},
        "one wheel filename",
    )
    _validation(
        ReviewedBuildToolchainLock,
        toolchain.model_dump(mode="python") | {"tool_wheels": (tool, tool, tool)},
        "unique",
    )
    _validation(
        ReviewedBuildToolchainLock,
        toolchain.model_dump(mode="python") | {"tool_wheels": (tool,)},
        "packaging, setuptools, and wheel",
    )


def test_runtime_and_profile_contracts_reject_duplicate_bindings() -> None:
    loaded = registry()
    f5 = loaded.runtime("f5-tts-1.1.22-mps")
    first, second, third = f5.reviewed_built_wheels
    _validation(
        RuntimeLock,
        f5.model_dump(mode="python") | {"python_version": "3.10"},
        "CPython 3.12",
    )
    duplicate_package = second.model_copy(update={"package": first.package})
    _validation(
        RuntimeLock,
        f5.model_dump(mode="python") | {"reviewed_built_wheels": (first, duplicate_package, third)},
        "packages must be unique",
    )
    duplicate_filename = second.model_copy(update={"wheel_filename": first.wheel_filename})
    _validation(
        RuntimeLock,
        f5.model_dump(mode="python")
        | {"reviewed_built_wheels": (first, duplicate_filename, third)},
        "filenames must be unique",
    )
    profile = loaded.lock.profiles[0]
    _validation(
        CandidateProfileLock,
        profile.model_dump(mode="python") | {"provenance_source_ids": ("a", "a")},
        "provenance source IDs must be unique",
    )


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("sources", "source IDs must be unique"),
        ("runtimes", "runtime IDs must be unique"),
        ("profiles", "candidate/profile lock keys must be unique"),
    ),
)
def test_model_lock_rejects_duplicate_keys(field: str, message: str) -> None:
    lock = registry().lock
    value = getattr(lock, field)
    _validation(
        ModelSourcesLock,
        lock.model_dump(mode="python") | {field: (value[0], value[0])},
        message,
    )


def test_requirement_parser_rejects_io_size_duplicates_and_version_drift(
    tmp_path: Path,
) -> None:
    runtime = registry().runtime("mlx-audio-0.5.0")
    with pytest.raises(ContractError, match="cannot read runtime requirement lock"):
        _read_runtime_requirement_entries(tmp_path / "missing.txt", artifact_lock=False)

    requirements = tmp_path / "root.txt"
    artifacts = tmp_path / "artifacts.txt"
    requirements.write_text("mlx-audio==0.5.0\n", encoding="utf-8")
    artifacts.write_text(
        "mlx-audio==0.5.0 --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )

    def bound() -> RuntimeLock:
        root_digest, _ = sha256_file(requirements)
        artifact_digest, _ = sha256_file(artifacts)
        return runtime.model_copy(
            update={
                "requirements_path": "root.txt",
                "requirements_sha256": root_digest,
                "artifact_lock_path": "artifacts.txt",
                "artifact_lock_sha256": artifact_digest,
                "packages": {"mlx-audio": "0.5.0"},
            }
        )

    requirements.write_bytes(b"#" + b"x" * 2_000_000)
    oversized = bound()
    with pytest.raises(ContractError, match="unexpectedly large"):
        verify_runtime_requirements(oversized, tmp_path)

    requirements.write_text("mlx-audio==0.5.0\nmlx_audio==0.5.0\n", encoding="utf-8")
    duplicate = bound()
    with pytest.raises(ContractError, match="repeats package"):
        verify_runtime_requirements(duplicate, tmp_path)

    requirements.write_text("# no entries\n", encoding="utf-8")
    empty = bound()
    with pytest.raises(ContractError, match="lock is empty"):
        verify_runtime_requirements(empty, tmp_path)

    requirements.write_text("mlx-audio==0.5.0\n", encoding="utf-8")
    artifacts.write_text(
        "mlx-audio==0.4.0 --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    drifted = bound()
    with pytest.raises(ContractError, match="does not bind root"):
        verify_runtime_requirements(drifted, tmp_path)

    assert canonical_package_name("MLX_Audio") == "mlx-audio"
    with pytest.raises(ContractError, match="invalid package name"):
        canonical_package_name("bad package")


def test_reviewed_wheel_must_match_complete_artifact_lock(tmp_path: Path) -> None:
    runtime = registry().runtime("f5-tts-1.1.22-mps")
    requirements = tmp_path / runtime.requirements_path
    artifacts = tmp_path / runtime.artifact_lock_path
    requirements.parent.mkdir(parents=True)
    requirements.write_bytes((ROOT / runtime.requirements_path).read_bytes())
    artifacts.write_bytes((ROOT / runtime.artifact_lock_path).read_bytes())
    payload = artifacts.read_text(encoding="utf-8")
    reviewed = runtime.reviewed_built_wheels[0]
    payload = payload.replace(reviewed.wheel_sha256, "f" * 64)
    artifacts.write_text(payload, encoding="utf-8")
    requirements_sha256, _ = sha256_file(requirements)
    artifact_sha256, _ = sha256_file(artifacts)
    rebound = runtime.model_copy(
        update={
            "requirements_sha256": requirements_sha256,
            "artifact_lock_sha256": artifact_sha256,
        }
    )
    with pytest.raises(ContractError, match="reviewed wheel evidence disagrees"):
        verify_runtime_requirements(rebound, tmp_path)


def _replace_candidate(loaded: CandidateRegistry, candidate: object) -> CandidateRegistry:
    candidates = (candidate, *loaded.configuration.candidates[1:])
    configuration = loaded.configuration.model_copy(update={"candidates": candidates})
    return CandidateRegistry(configuration, loaded.lock)


def test_registry_link_validation_rejects_rebound_policy() -> None:
    loaded = registry()
    candidate = loaded.configuration.candidates[0]
    with pytest.raises(ContractError, match="unknown runtime"):
        _replace_candidate(loaded, candidate.model_copy(update={"runtime_id": "missing-runtime"}))

    runtime = loaded.runtime(candidate.runtime_id)
    code = loaded.source(runtime.code_source_id)
    rebound_code = code.model_copy(update={"kind": "huggingface"})
    lock = loaded.lock.model_copy(
        update={
            "sources": tuple(
                rebound_code if source.source_id == code.source_id else source
                for source in loaded.lock.sources
            )
        }
    )
    with pytest.raises(ContractError, match="code source must be Git"):
        CandidateRegistry(loaded.configuration, lock)

    profile = next(
        item
        for item in loaded.lock.profiles
        if item.candidate_id == candidate.candidate_id and item.profile == "quality"
    )
    model = loaded.source(profile.model_source_id)
    rebound_model = model.model_copy(update={"kind": "git"})
    lock = loaded.lock.model_copy(
        update={
            "sources": tuple(
                rebound_model if source.source_id == model.source_id else source
                for source in loaded.lock.sources
            )
        }
    )
    with pytest.raises(ContractError, match="model source kind disagrees"):
        CandidateRegistry(loaded.configuration, lock)

    with pytest.raises(ContractError, match="model_id must name"):
        _replace_candidate(loaded, candidate.model_copy(update={"model_id": "wrong/model"}))
    with pytest.raises(ContractError, match="workers must live"):
        _replace_candidate(loaded, candidate.model_copy(update={"worker_module": "outside.worker"}))


def test_load_registry_wraps_io_and_rejects_non_array(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="cannot load candidate registry"):
        load_registry(tmp_path / "missing.toml", tmp_path / "missing.json")
    candidates = tmp_path / "candidates.toml"
    lock = tmp_path / "lock.json"
    candidates.write_text('schema_version = "cole-voice-lab.candidates/v1"\n', encoding="utf-8")
    lock.write_text(json.dumps(registry().lock.model_dump(mode="json")), encoding="utf-8")
    with pytest.raises(ContractError, match="candidates array"):
        load_registry(candidates, lock)

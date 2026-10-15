from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from runtime_fixtures import synthetic_python_runtime

from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.errors import (
    ContractError,
    ExternalToolError,
    RuntimeUnavailableError,
    SecurityError,
)
from cole_voice_lab.models import ExperimentRequest, ExperimentResult, ReferenceEvidence
from cole_voice_lab.registry import SourceFileLock, SourceLock, load_registry
from cole_voice_lab.runtime import (
    LocalSourceManifest,
    RuntimeInstallPlan,
    RuntimeInstallReceipt,
    RuntimeTreeEntry,
    SourceEvictionPlan,
    SourceVerification,
    WorkerInvocation,
    _assert_successful_result_evidence,
    _confined_symlink_resolution,
    _distribution_identity,
    _expand_loader_reference,
    _expected_runtime_distributions,
    _installed_distributions,
    _lab_wheel_version,
    _macho_rpaths,
    _macho_system_references,
    _normalized_real_directory,
    _parse_git_record,
    _read_runtime_receipt,
    _reject_unsafe_source_root,
    _required_git_output,
    _runtime_receipt,
    _runtime_tree_entries,
    _tree_size_without_following,
    _validate_invocation,
    _validate_runtime_pth,
    _verify_lab_wheel,
    _verify_opt_alias,
    _verify_reviewed_wheel,
    assert_disk_floor,
    build_runtime_eviction_plan,
    build_runtime_install_plan,
    build_source_eviction_plan,
    build_source_provision_plan,
    execute_provision_plan,
    execute_runtime_eviction,
    execute_runtime_install_plan,
    execute_source_eviction,
    load_persisted_runtime_receipt,
    run_worker,
    verify_local_source,
    verify_runtime_install_receipt,
    write_runtime_install_receipt,
)

ROOT = Path(__file__).resolve().parents[1]


def registry():
    return load_registry(ROOT / "config/candidates.toml", ROOT / "config/model-sources.lock.json")


def tiny_source(payload: bytes = b"locked") -> SourceLock:
    return SourceLock(
        source_id="fixture-source",
        kind="huggingface",
        repository="example/fixture",
        revision="a" * 40,
        license="MIT",
        allow_patterns=("weights.bin",),
        files=(
            SourceFileLock(
                path="weights.bin",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )


def lab_wheel(tmp_path: Path, *, name: str = "cole-voice-lab") -> tuple[Path, str]:
    path = tmp_path / "cole_voice_lab-0.1.0.dev0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "cole_voice_lab-0.1.0.dev0.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: 0.1.0.dev0\n",
        )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_plan(tmp_path: Path, monkeypatch) -> RuntimeInstallPlan:
    host = synthetic_python_runtime(tmp_path, monkeypatch)
    runtime = host.registry.runtime("mlx-audio-0.5.0")
    environment = tmp_path / "environment"
    site_packages = host.create_environment(environment)
    for name, version in (("fixture", "1.0"), ("cole-voice-lab", "0.1.0.dev0")):
        metadata = site_packages / f"{name}-{version}.dist-info/METADATA"
        metadata.parent.mkdir()
        metadata.write_text(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )
    wheel, digest = lab_wheel(tmp_path)
    return RuntimeInstallPlan(
        runtime=runtime,
        repository_root=ROOT,
        environment_path=environment,
        reviewed_wheelhouse=None,
        lab_wheel=wheel,
        lab_wheel_sha256=digest,
        lab_distribution_version="0.1.0.dev0",
        artifact_versions={"fixture": "1.0"},
        commands=(),
    )


def test_runtime_contract_models_reject_incomplete_shapes() -> None:
    with pytest.raises(ValidationError, match="lowercase SHA-256"):
        LocalSourceManifest(
            source_id="source",
            kind="huggingface",
            repository="repo/model",
            revision="a" * 40,
            files={"weights.bin": "A" * 64},
        )
    with pytest.raises(ValidationError, match="require only a SHA-256"):
        RuntimeTreeEntry(path="file", kind="file", mode=0o600, size_bytes=1)
    with pytest.raises(ValidationError, match="target text"):
        RuntimeTreeEntry(
            path="alias",
            kind="symlink",
            mode=0o777,
            size_bytes=1,
            resolved_sha256="a" * 64,
        )
    with pytest.raises(ValidationError, match="cannot carry"):
        RuntimeTreeEntry(
            path="directory",
            kind="directory",
            mode=0o700,
            size_bytes=1,
            sha256="a" * 64,
        )

    entries = (
        RuntimeTreeEntry(
            path="b",
            kind="file",
            mode=0o600,
            size_bytes=1,
            sha256="b" * 64,
        ),
        RuntimeTreeEntry(
            path="a",
            kind="file",
            mode=0o600,
            size_bytes=1,
            sha256="a" * 64,
        ),
    )
    base = {
        "runtime_id": "runtime",
        "runtime_lock_sha256": "a" * 64,
        "requirements_sha256": "b" * 64,
        "artifact_lock_sha256": "c" * 64,
        "reviewed_wheel_sha256": {},
        "lab_wheel_sha256": "d" * 64,
        "lab_distribution_version": "1.0",
        "runtime_python_sha256": "e" * 64,
        "python_runtime_lock_sha256": "f" * 64,
        "sanitized_files": (),
        "installed_distributions": {"package": "1.0"},
        "tree": entries,
        "tree_sha256": canonical_sha256([entry.model_dump(mode="json") for entry in entries]),
    }
    with pytest.raises(ValidationError, match="sorted and unique"):
        RuntimeInstallReceipt.model_validate(base)
    with pytest.raises(ValidationError, match="digest does not match"):
        RuntimeInstallReceipt.model_validate(base | {"tree": tuple(reversed(entries))})


def test_source_and_git_low_level_checks_fail_closed(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="positive"):
        assert_disk_floor(tmp_path, minimum_free_bytes=0)
    plain_file = tmp_path / "plain"
    plain_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SecurityError, match="real directory"):
        _reject_unsafe_source_root(plain_file)

    monkeypatch.setattr(
        "cole_voice_lab.runtime._git_command",
        lambda *_args: subprocess.CompletedProcess((), 2, b"", b"broken git"),
    )
    with pytest.raises(RuntimeUnavailableError, match="broken git"):
        _required_git_output(tmp_path, "status")
    with pytest.raises(SecurityError, match="malformed tree"):
        _parse_git_record(b"no-tab", label="tree")
    with pytest.raises(SecurityError, match="non-portable index"):
        _parse_git_record(b"100644 object 0\t\xff", label="index")


def test_snapshot_manifest_identity_digest_and_file_types_are_rechecked(tmp_path: Path) -> None:
    source = tiny_source()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"locked")
    (snapshot / "weights.bin").symlink_to(outside)
    with pytest.raises(SecurityError, match="forbidden symlink"):
        verify_local_source(source, snapshot, require_manifest=False)
    (snapshot / "weights.bin").unlink()
    (snapshot / "weights.bin").write_bytes(b"locked")

    wrong_identity = LocalSourceManifest(
        source_id="wrong-source",
        kind=source.kind,
        repository=source.repository,
        revision=source.revision,
        files={"weights.bin": source.files[0].sha256 or ""},
    )
    write_canonical_json(snapshot / ".cole-voice-lab-source.json", wrong_identity)
    with pytest.raises(ContractError, match="identity mismatch"):
        verify_local_source(source, snapshot)

    wrong_digest = wrong_identity.model_copy(
        update={"source_id": source.source_id, "files": {"weights.bin": "f" * 64}}
    )
    write_canonical_json(snapshot / ".cole-voice-lab-source.json", wrong_digest)
    with pytest.raises(ContractError, match="manifest digest mismatch"):
        verify_local_source(source, snapshot)

    unsafe_tree = tmp_path / "unsafe-tree"
    unsafe_tree.mkdir()
    os.mkfifo(unsafe_tree / "fifo")
    with pytest.raises(SecurityError, match="unsafe file type"):
        _tree_size_without_following(unsafe_tree)


def test_source_plan_git_branch_and_eviction_revalidation(tmp_path: Path, monkeypatch) -> None:
    source = SourceLock(
        source_id="git-source",
        kind="git",
        repository="https://example.invalid/repository.git",
        revision="a" * 40,
        license="MIT",
    )
    plan = build_source_provision_plan(
        source,
        tmp_path / "checkout",
        runtime_python=Path("/python"),
    )
    assert [command[1] for command in plan.commands] == ["clone", "-C", "-C"]

    model = tiny_source()
    boundary = tmp_path / "models"
    candidate = boundary / model.source_id
    candidate.mkdir(parents=True)
    verification = SourceVerification(model.source_id, candidate, None, {}, "a" * 64)
    monkeypatch.setattr(
        "cole_voice_lab.runtime.verify_local_source", lambda *_a, **_k: verification
    )
    with pytest.raises(ContractError, match="checksum-bound"):
        build_source_eviction_plan(model, candidate, boundary=boundary)

    eviction = SourceEvictionPlan(model.source_id, candidate, "a" * 64, 1)
    monkeypatch.setattr(
        "cole_voice_lab.runtime.build_source_eviction_plan",
        lambda *_a, **_k: eviction.__class__(model.source_id, candidate, "a" * 64, 2),
    )
    with pytest.raises(SecurityError, match="changed after"):
        execute_source_eviction(
            model,
            eviction,
            boundary=boundary,
            expected_manifest_sha256="a" * 64,
            confirmed=True,
        )


def test_provision_rejects_existing_destination(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    plan = build_source_provision_plan(
        tiny_source(),
        destination,
        runtime_python=Path("/python"),
    )
    monkeypatch.setattr("cole_voice_lab.runtime.assert_disk_floor", lambda *_a, **_k: 1)
    with pytest.raises(ContractError, match="existing path"):
        execute_provision_plan(plan, allow_downloads=True)


@pytest.mark.serial
def test_real_directory_and_wheel_artifact_validation(tmp_path: Path) -> None:
    with pytest.raises(RuntimeUnavailableError, match="cannot inspect"):
        _normalized_real_directory(tmp_path / "missing", label="fixture")
    plain = tmp_path / "plain"
    plain.write_text("plain", encoding="utf-8")
    with pytest.raises(SecurityError, match="not a real directory"):
        _normalized_real_directory(plain, label="fixture")
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(SecurityError, match="symlink component"):
        _normalized_real_directory(alias, label="fixture")

    runtime = registry().runtime("f5-tts-1.1.22-mps")
    expected = runtime.reviewed_built_wheels[0]
    with pytest.raises(RuntimeUnavailableError, match="missing"):
        _verify_reviewed_wheel(tmp_path / expected.wheel_filename, expected)
    wrong = tmp_path / expected.wheel_filename
    wrong.write_bytes(b"wrong")
    with pytest.raises(SecurityError, match="bytes do not match"):
        _verify_reviewed_wheel(wrong, expected)

    with pytest.raises(ContractError, match="lowercase SHA-256"):
        _verify_lab_wheel(wrong, "invalid")
    with pytest.raises(ContractError, match="must be a wheel"):
        _verify_lab_wheel(plain, "a" * 64)
    missing_wheel = tmp_path / "missing.whl"
    with pytest.raises(RuntimeUnavailableError, match="missing"):
        _verify_lab_wheel(missing_wheel, "a" * 64)
    wheel, digest = lab_wheel(tmp_path)
    alias_wheel = tmp_path / "alias.whl"
    alias_wheel.symlink_to(wheel)
    with pytest.raises(SecurityError, match="not a regular file"):
        _verify_lab_wheel(alias_wheel, digest)
    second = tmp_path / "second.whl"
    os.link(wheel, second)
    with pytest.raises(SecurityError, match="exactly one hard link"):
        _verify_lab_wheel(wheel, digest)
    second.unlink()
    with pytest.raises(SecurityError, match="digest does not match"):
        _verify_lab_wheel(wheel, "f" * 64)


def test_distribution_and_wheel_metadata_are_unambiguous(tmp_path: Path) -> None:
    with pytest.raises(SecurityError, match="size bound"):
        _distribution_identity(b"x" * (2 * 1024 * 1024 + 1), label="fixture")
    with pytest.raises(ContractError, match="ambiguous"):
        _distribution_identity(b"Name: package\n", label="fixture")

    empty = tmp_path / "empty.whl"
    with zipfile.ZipFile(empty, "w"):
        pass
    with pytest.raises(ContractError, match="one distribution METADATA"):
        _lab_wheel_version(empty)
    huge = tmp_path / "huge.whl"
    with zipfile.ZipFile(huge, "w") as archive:
        archive.writestr("package-1.0.dist-info/METADATA", b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(SecurityError, match="size bound"):
        _lab_wheel_version(huge)
    broken = tmp_path / "broken.whl"
    broken.write_bytes(b"not a zip")
    with pytest.raises(ContractError, match="cannot inspect"):
        _lab_wheel_version(broken)
    wrong, _ = lab_wheel(tmp_path, name="different-package")
    with pytest.raises(ContractError, match="does not contain"):
        _lab_wheel_version(wrong)


def test_runtime_tree_rejects_hardlinks_symlinks_and_divergent_python_alias(
    tmp_path: Path, monkeypatch
) -> None:
    plan = runtime_plan(tmp_path, monkeypatch)
    site_packages = plan.environment_path / "lib/python3.12/site-packages"
    metadata = next(site_packages.glob("fixture-*.dist-info/METADATA"))
    hardlink = site_packages / "metadata-hardlink"
    os.link(metadata, hardlink)
    with pytest.raises(SecurityError, match="exactly one hard link"):
        _runtime_tree_entries(plan.environment_path)
    hardlink.unlink()

    outside = tmp_path / "outside"
    outside.mkdir()
    injected = site_packages / "injected"
    injected.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecurityError, match="forbidden symlink"):
        _runtime_tree_entries(plan.environment_path)
    injected.unlink()

    other_python = plan.environment_path / "bin/other-python"
    other_python.write_bytes(b"different interpreter")
    (plan.environment_path / "bin/python3").unlink()
    (plan.environment_path / "bin/python3").symlink_to("other-python")
    with pytest.raises(SecurityError, match="one bound interpreter"):
        write_runtime_install_receipt(plan)


def test_distribution_graph_and_receipt_bindings_fail_closed(tmp_path: Path, monkeypatch) -> None:
    empty = tmp_path / "empty-runtime"
    empty.mkdir()
    tree = _runtime_tree_entries(empty)
    with pytest.raises(ContractError, match="no distribution metadata"):
        _installed_distributions(empty, tree)

    plan = runtime_plan(tmp_path / "valid", monkeypatch)
    site_packages = plan.environment_path / "lib/python3.12/site-packages"
    duplicate = site_packages / "fixture-copy-1.0.dist-info/METADATA"
    duplicate.parent.mkdir()
    duplicate.write_text("Name: fixture\nVersion: 1.0\n", encoding="utf-8")
    with pytest.raises(ContractError, match="repeats distribution"):
        _installed_distributions(
            plan.environment_path, _runtime_tree_entries(plan.environment_path)
        )
    duplicate.unlink()
    duplicate.parent.rmdir()

    with pytest.raises(RuntimeUnavailableError, match="no reviewed lab"):
        _expected_runtime_distributions(
            plan.__class__(
                runtime=plan.runtime,
                repository_root=plan.repository_root,
                environment_path=plan.environment_path,
                reviewed_wheelhouse=None,
                lab_wheel=None,
                lab_wheel_sha256=None,
                lab_distribution_version=None,
                artifact_versions={},
                commands=(),
            )
        )
    conflict = plan.__class__(
        runtime=plan.runtime,
        repository_root=plan.repository_root,
        environment_path=plan.environment_path,
        reviewed_wheelhouse=None,
        lab_wheel=plan.lab_wheel,
        lab_wheel_sha256=plan.lab_wheel_sha256,
        lab_distribution_version=plan.lab_distribution_version,
        artifact_versions={"cole-voice-lab": "9.9"},
        commands=(),
    )
    with pytest.raises(ContractError, match="must not replace"):
        _expected_runtime_distributions(conflict)
    mismatch = plan.__class__(
        runtime=plan.runtime,
        repository_root=plan.repository_root,
        environment_path=plan.environment_path,
        reviewed_wheelhouse=None,
        lab_wheel=plan.lab_wheel,
        lab_wheel_sha256=plan.lab_wheel_sha256,
        lab_distribution_version=plan.lab_distribution_version,
        artifact_versions={"missing": "1.0"},
        commands=(),
    )
    with pytest.raises(ContractError, match="distribution graph differs"):
        _runtime_receipt(mismatch, _runtime_tree_entries(plan.environment_path))


def test_runtime_receipt_canonical_identity_and_stale_binding(tmp_path: Path, monkeypatch) -> None:
    plan = runtime_plan(tmp_path, monkeypatch)
    receipt_path, digest = write_runtime_install_receipt(plan)
    with pytest.raises(ContractError, match="trial binding"):
        load_persisted_runtime_receipt(
            receipt_path,
            expected_sha256=digest,
            runtime_id="different-runtime",
            runtime_lock_sha256=canonical_sha256(plan.runtime),
        )
    payload = receipt_path.read_bytes()
    receipt_path.write_bytes(payload + b" ")
    with pytest.raises(SecurityError, match="not canonical"):
        _read_runtime_receipt(receipt_path)

    receipt_path.write_bytes(payload)
    stale = plan.runtime.model_copy(update={"requirements_sha256": "f" * 64})
    with pytest.raises(ContractError, match="requirements digest mismatch"):
        verify_runtime_install_receipt(
            stale,
            plan.environment_path,
            repository_root=ROOT,
        )


def test_confined_symlink_resolution_rejects_each_escape_shape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "target").write_text("locked", encoding="utf-8")

    with pytest.raises(SecurityError, match="starts outside"):
        _confined_symlink_resolution(
            outside,
            root,
            label="fixture tree",
            relative="outside",
        )

    (root / "dangling").symlink_to("missing")
    with pytest.raises(SecurityError, match="dangling"):
        _confined_symlink_resolution(
            root / "dangling",
            root,
            label="fixture tree",
            relative="dangling",
        )

    (root / "escape").symlink_to("../outside")
    with pytest.raises(SecurityError, match="escapes"):
        _confined_symlink_resolution(
            root / "escape",
            root,
            label="fixture tree",
            relative="escape",
        )

    (root / "absolute").symlink_to(root / "target")
    assert (
        _confined_symlink_resolution(
            root / "absolute",
            root,
            label="fixture tree",
            relative="absolute",
        )
        == root / "target"
    )

    (root / "file").write_text("not a directory", encoding="utf-8")
    (root / "through-file").symlink_to("file/child")
    with pytest.raises(SecurityError, match="non-directory"):
        _confined_symlink_resolution(
            root / "through-file",
            root,
            label="fixture tree",
            relative="through-file",
        )

    for index in range(65):
        target = f"hop-{index + 1}" if index < 64 else "target"
        (root / f"hop-{index}").symlink_to(target)
    with pytest.raises(SecurityError, match="hop bound"):
        _confined_symlink_resolution(
            root / "hop-0",
            root,
            label="fixture tree",
            relative="hop-0",
        )


def test_macho_helpers_cover_rpaths_tcb_and_escape_rejection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "bin" / "candidate"
    candidate.parent.mkdir()
    candidate.write_bytes(b"\xcf\xfa\xed\xfe")
    library = tmp_path / "lib" / "locked.dylib"
    library.parent.mkdir()
    library.write_bytes(b"\xcf\xfa\xed\xfe")

    rpath_output = """Load command 1
          cmd LC_RPATH
      cmdsize 48
         path @loader_path/../lib (offset 12)
"""
    monkeypatch.setattr(
        "cole_voice_lab.runtime.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, rpath_output, ""),
    )
    assert _macho_rpaths(candidate) == ("@loader_path/../lib",)
    assert _expand_loader_reference("@loader_path", candidate) == candidate.parent
    assert _expand_loader_reference("@loader_path/../lib", candidate) == candidate.parent / "../lib"
    assert _expand_loader_reference(str(library), candidate) == library
    with pytest.raises(SecurityError, match="context-dependent"):
        _expand_loader_reference("@executable_path/libbad.dylib", candidate)

    monkeypatch.setattr(
        "cole_voice_lab.runtime.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 1, "", "failed"),
    )
    with pytest.raises(RuntimeUnavailableError, match="rpaths"):
        _macho_rpaths(candidate)

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    binary = allowed / "binary"
    binary.write_bytes(b"\xcf\xfa\xed\xfepayload")
    dependency = allowed / "dependency.dylib"
    dependency.write_bytes(b"plain")
    closure_output = (
        f"{binary}:\n"
        "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        f"\t{dependency} (compatibility version 1.0.0)\n"
    )
    monkeypatch.setattr(
        "cole_voice_lab.runtime.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, closure_output, ""),
    )
    assert _macho_system_references((allowed,)) == ("/usr/lib/libSystem.B.dylib",)

    escaped = tmp_path / "escaped.dylib"
    escaped.write_bytes(b"plain")
    escape_output = f"{binary}:\n\t{escaped} (compatibility version 1.0.0)\n"
    monkeypatch.setattr(
        "cole_voice_lab.runtime.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, escape_output, ""),
    )
    with pytest.raises(SecurityError, match="escapes the locked runtime closure"):
        _macho_system_references((allowed,))

    monkeypatch.setattr(
        "cole_voice_lab.runtime.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 1, "", "failed"),
    )
    with pytest.raises(RuntimeUnavailableError, match="Mach-O closure"):
        _macho_system_references((allowed,))


def test_runtime_pth_and_opt_alias_validation_edges(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    site_packages = environment / "lib/python3.12/site-packages"
    internal = site_packages / "internal"
    internal.mkdir(parents=True)
    pth = site_packages / "safe.pth"
    pth.write_text("# comment\n\ninternal\nmissing\n", encoding="utf-8")
    _validate_runtime_pth(environment)

    pth.write_bytes(b"\xff")
    with pytest.raises(SecurityError, match="not UTF-8"):
        _validate_runtime_pth(environment)

    pth.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(SecurityError, match="size bound"):
        _validate_runtime_pth(environment)

    pth.write_text("internal\n", encoding="utf-8")
    alias = tmp_path / "safe-hardlink.pth"
    os.link(pth, alias)
    with pytest.raises(SecurityError, match="single-link"):
        _validate_runtime_pth(environment)
    alias.unlink()

    missing_alias = tmp_path / "missing-alias"
    with pytest.raises(RuntimeUnavailableError, match="alias is missing"):
        _verify_opt_alias(str(missing_alias), "target", tmp_path / "target")
    missing_alias.write_text("not a link", encoding="utf-8")
    with pytest.raises(SecurityError, match="alias changed"):
        _verify_opt_alias(str(missing_alias), "target", tmp_path / "target")
    missing_alias.unlink()
    target = tmp_path / "target"
    target.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    missing_alias.symlink_to("target")
    with pytest.raises(SecurityError, match="resolves outside"):
        _verify_opt_alias(str(missing_alias), "target", other)


def test_runtime_install_plan_requires_paired_artifacts_and_clean_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    host = synthetic_python_runtime(tmp_path, monkeypatch)
    runtime = host.registry.runtime("mlx-audio-0.5.0")
    wheel, digest = lab_wheel(tmp_path)
    with pytest.raises(ContractError, match="supplied together"):
        build_runtime_install_plan(
            runtime,
            tmp_path / "runtime",
            base_python=host.python,
            repository_root=ROOT,
            lab_wheel=wheel,
        )
    plan = build_runtime_install_plan(
        runtime,
        tmp_path / "runtime",
        base_python=host.python,
        repository_root=ROOT,
        lab_wheel=wheel,
        lab_wheel_sha256=digest,
    )
    plan.environment_path.mkdir()
    monkeypatch.setattr("cole_voice_lab.runtime.assert_disk_floor", lambda *_a, **_k: 1)
    with pytest.raises(ContractError, match="existing runtime"):
        execute_runtime_install_plan(plan, allow_downloads=True)

    preview = build_runtime_install_plan(
        runtime,
        tmp_path / "preview",
        base_python=host.python,
        repository_root=ROOT,
    )
    monkeypatch.setattr("cole_voice_lab.runtime.assert_disk_floor", lambda *_a, **_k: 1)
    with pytest.raises(RuntimeUnavailableError, match="exact reviewed"):
        execute_runtime_install_plan(preview, allow_downloads=True)


def test_runtime_eviction_rejects_wrong_target_lock_and_changed_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = registry().runtime("mlx-audio-0.5.0")
    boundary = tmp_path / "runtimes"
    boundary.mkdir(mode=0o700)
    wrong = boundary / "wrong-name"
    wrong.mkdir(mode=0o700)
    with pytest.raises(SecurityError, match="direct runtime-ID"):
        build_runtime_eviction_plan(runtime, wrong, boundary=boundary, repository_root=ROOT)

    environment = boundary / runtime.runtime_id
    environment.mkdir(mode=0o700)
    monkeypatch.setattr(
        "cole_voice_lab.runtime.verify_runtime_install_receipt",
        lambda *_a, **_k: (None, "a" * 64),
    )
    plan = build_runtime_eviction_plan(
        runtime, environment, boundary=boundary, repository_root=ROOT
    )
    with pytest.raises(SecurityError, match="fixed audition"):
        execute_runtime_eviction(
            runtime,
            plan,
            boundary=boundary,
            repository_root=ROOT,
            expected_receipt_sha256=plan.receipt_sha256,
            orchestration_lock_path=tmp_path / "wrong.lock",
            confirmed=True,
        )
    monkeypatch.setattr(
        "cole_voice_lab.runtime.build_runtime_eviction_plan",
        lambda *_a, **_k: plan.__class__(plan.runtime_id, plan.path, plan.receipt_sha256, 999),
    )
    with pytest.raises(SecurityError, match="changed after"):
        execute_runtime_eviction(
            runtime,
            plan,
            boundary=boundary,
            repository_root=ROOT,
            expected_receipt_sha256=plan.receipt_sha256,
            orchestration_lock_path=boundary / "audition-orchestrator.lock",
            confirmed=True,
        )


def worker_invocation(tmp_path: Path) -> WorkerInvocation:
    resolved = registry().resolve("qwen3-tts-base", "smoke")
    runtime_python = tmp_path / "runtime/bin/python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("stub", encoding="utf-8")
    request = ExperimentRequest(
        experiment_id="experiment",
        trial_id="trial",
        candidate_id=resolved.candidate.candidate_id,
        track=resolved.candidate.track,
        text="Held-out text.",
        output_path=str((tmp_path / "output.wav").resolve()),
        reference=ReferenceEvidence(
            path=str((tmp_path / "reference.wav").resolve()),
            sha256="a" * 64,
            transcript="Reference transcript.",
            subject_id="cole-self",
            consent_path=str((tmp_path / "consent.json").resolve()),
            consent_sha256="b" * 64,
        ),
        model_id=resolved.model_id,
        model_revision=resolved.model_revision,
        code_revision=resolved.code_revision,
    )
    return WorkerInvocation(
        request=request,
        resolved=resolved,
        runtime_python=runtime_python,
        model_path=tmp_path / "model",
        auxiliary_paths={},
        worker_lock_path=tmp_path / "worker.lock",
        runtime_root=tmp_path,
    )


@pytest.mark.parametrize(
    ("request_update", "auxiliary", "message"),
    (
        ({"candidate_id": "chatterbox-v3"}, {}, "candidate does not match"),
        ({"track": "voice-conversion"}, {}, "track does not match"),
        ({"model_revision": "f" * 40}, {}, "immutable model/code"),
        ({}, {"unexpected": Path("/tmp")}, "auxiliary paths"),
    ),
)
def test_worker_invocation_binding_errors(
    tmp_path: Path,
    request_update: dict[str, object],
    auxiliary: dict[str, Path],
    message: str,
) -> None:
    invocation = worker_invocation(tmp_path)
    request = invocation.request.model_copy(update=request_update)
    rebound = invocation.__class__(
        request=request,
        resolved=invocation.resolved,
        runtime_python=invocation.runtime_python,
        model_path=invocation.model_path,
        auxiliary_paths=auxiliary,
        worker_lock_path=invocation.worker_lock_path,
        runtime_root=invocation.runtime_root,
    )
    with pytest.raises(ContractError, match=message):
        _validate_invocation(rebound)


def test_worker_process_failures_and_source_mutation_are_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    invocation = worker_invocation(tmp_path)
    monkeypatch.setattr("cole_voice_lab.runtime.assert_disk_floor", lambda *_a, **_k: 1)
    verification = SourceVerification(
        invocation.resolved.model_source.source_id,
        invocation.model_path,
        "a" * 64,
        {},
        "b" * 64,
    )
    monkeypatch.setattr(
        "cole_voice_lab.runtime._verify_invocation_sources",
        lambda _invocation: (verification, {}, None),
    )
    monkeypatch.setattr(
        "cole_voice_lab.runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 9, "", "worker failed"),
    )
    with pytest.raises(ExternalToolError, match="exited 9"):
        run_worker(invocation)

    monkeypatch.setattr(
        "cole_voice_lab.runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "not-json", ""),
    )
    with pytest.raises(ExternalToolError, match="valid result"):
        run_worker(invocation)

    failed = ExperimentResult(
        trial_id="other-trial",
        candidate_id=invocation.request.candidate_id,
        ok=False,
        error_code="fixture",
        error_message="fixture",
    )
    monkeypatch.setattr(
        "cole_voice_lab.runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, failed.model_dump_json(), ""
        ),
    )
    with pytest.raises(ExternalToolError, match="identity"):
        run_worker(invocation)

    changed = SourceVerification(
        verification.source_id,
        verification.path,
        verification.manifest_sha256,
        {},
        "c" * 64,
    )
    calls = iter(((verification, {}, None), (changed, {}, None)))
    monkeypatch.setattr(
        "cole_voice_lab.runtime._verify_invocation_sources", lambda _invocation: next(calls)
    )
    with pytest.raises(SecurityError, match="changed during"):
        run_worker(invocation)


def test_worker_receives_only_the_canonical_verified_source_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    invocation = worker_invocation(tmp_path)
    canonical_model = tmp_path / "verified-model"
    canonical_model.mkdir()
    verification = SourceVerification(
        invocation.resolved.model_source.source_id,
        canonical_model,
        "a" * 64,
        {},
        "b" * 64,
    )
    monkeypatch.setattr("cole_voice_lab.runtime.assert_disk_floor", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        "cole_voice_lab.runtime._verify_invocation_sources",
        lambda _invocation: (verification, {}, None),
    )
    failed = ExperimentResult(
        trial_id=invocation.request.trial_id,
        candidate_id=invocation.request.candidate_id,
        ok=False,
        error_code="fixture",
        error_message="fixture",
    )

    def execute(_command, **kwargs):
        assert kwargs["env"]["COLE_VOICE_LAB_MODEL_PATH"] == str(canonical_model)
        return subprocess.CompletedProcess((), 0, failed.model_dump_json(), "")

    monkeypatch.setattr("cole_voice_lab.runtime.subprocess.run", execute)
    assert run_worker(invocation).error_code == "fixture"


def test_success_evidence_checks_each_bound_source() -> None:
    model = SourceVerification("model", Path("/model"), "a" * 64, {}, "b" * 64)
    auxiliary = {"repo/aux": SourceVerification("aux", Path("/aux"), "c" * 64, {}, "d" * 64)}
    code = SourceVerification("code", Path("/code"), None, {}, "e" * 64)
    failed = SimpleNamespace(ok=False, metadata={})
    _assert_successful_result_evidence(failed, model, auxiliary, code)
    with pytest.raises(SecurityError, match="model manifest"):
        _assert_successful_result_evidence(SimpleNamespace(ok=True, metadata={}), model, {}, None)
    with pytest.raises(SecurityError, match="auxiliary manifests"):
        _assert_successful_result_evidence(
            SimpleNamespace(ok=True, metadata={"model_manifest_sha256": "a" * 64}),
            model,
            auxiliary,
            code,
        )
    with pytest.raises(SecurityError, match="executable checkout"):
        _assert_successful_result_evidence(
            SimpleNamespace(
                ok=True,
                metadata={
                    "model_manifest_sha256": "a" * 64,
                    "auxiliary_manifest_sha256": {"repo/aux": "c" * 64},
                },
            ),
            model,
            auxiliary,
            code,
        )

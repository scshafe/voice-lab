"""The runtime records blind review still reads, and the recording runtime's tree helpers.

The bake-off's provisioning and workers were retired with their runtimes (D-057); what stays
is the contracts of the receipts and source manifests its audition runs copied, the bounded
receipt reader, and the helpers the native recording service's runtime receipt is made with.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.errors import ContractError, RuntimeUnavailableError, SecurityError
from cole_voice_lab.runtime import (
    LocalSourceManifest,
    RuntimeInstallReceipt,
    RuntimeSanitizedFileLock,
    RuntimeTreeEntry,
    _confined_symlink_resolution,
    _expand_loader_reference,
    _immutable_tree_sha256,
    _macho_rpaths,
    _normalized_real_directory,
    _read_runtime_receipt,
    canonical_package_name,
    load_persisted_runtime_receipt,
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


def _receipt() -> RuntimeInstallReceipt:
    entry = RuntimeTreeEntry(
        path="bin/python", kind="file", mode=0o755, size_bytes=1, sha256="a" * 64
    )
    return RuntimeInstallReceipt(
        runtime_id="f5-tts-1.1.22-mps",
        runtime_lock_sha256="a" * 64,
        requirements_sha256="b" * 64,
        artifact_lock_sha256="c" * 64,
        reviewed_wheel_sha256={},
        lab_wheel_sha256="d" * 64,
        lab_distribution_version="0.1.0.dev0",
        runtime_python_sha256="e" * 64,
        python_runtime_lock_sha256="f" * 64,
        sanitized_files=(
            RuntimeSanitizedFileLock(
                path="lib/python3.12/site-packages/distutils-precedence.pth",
                sha256="a" * 64,
                size_bytes=151,
            ),
        ),
        installed_distributions={"f5-tts": "1.1.22"},
        tree=(entry,),
        tree_sha256=canonical_sha256([entry.model_dump(mode="json")]),
    )


def test_the_sanitizer_names_only_a_site_packages_pth_file() -> None:
    with pytest.raises(ValidationError, match="exact site-packages .pth"):
        RuntimeSanitizedFileLock(path="bin/startup.pth", sha256="a" * 64, size_bytes=1)
    with pytest.raises(ValidationError, match="exact site-packages .pth"):
        RuntimeSanitizedFileLock(
            path="lib/python3.12/site-packages/module.py", sha256="a" * 64, size_bytes=1
        )


def test_package_names_are_canonical_and_strict() -> None:
    assert canonical_package_name("MLX_Audio") == "mlx-audio"
    assert canonical_package_name("ruamel.yaml") == "ruamel-yaml"
    with pytest.raises(ContractError, match="invalid package name"):
        canonical_package_name("bad package")


def test_a_copied_receipt_is_read_bounded_canonical_and_bound_to_its_trial(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-receipt.json"
    digest = write_canonical_json(path, _receipt())
    receipt = load_persisted_runtime_receipt(
        path,
        expected_sha256=digest,
        runtime_id="f5-tts-1.1.22-mps",
        runtime_lock_sha256="a" * 64,
    )
    assert receipt == _receipt()
    with pytest.raises(ContractError, match="trial binding"):
        load_persisted_runtime_receipt(
            path,
            expected_sha256=digest,
            runtime_id="different-runtime",
            runtime_lock_sha256="a" * 64,
        )
    with pytest.raises(ContractError, match="trial binding"):
        load_persisted_runtime_receipt(
            path,
            expected_sha256=digest,
            runtime_id="f5-tts-1.1.22-mps",
            runtime_lock_sha256="b" * 64,
        )
    with pytest.raises(SecurityError, match="differs from approved evidence"):
        load_persisted_runtime_receipt(
            path,
            expected_sha256="f" * 64,
            runtime_id="f5-tts-1.1.22-mps",
            runtime_lock_sha256="a" * 64,
        )

    payload = path.read_bytes()
    path.write_bytes(payload + b" ")
    with pytest.raises(SecurityError, match="not canonical"):
        _read_runtime_receipt(path)
    path.write_bytes(payload)
    os.link(path, tmp_path / "second-name.json")
    with pytest.raises(SecurityError, match="unsafe link count"):
        _read_runtime_receipt(path)


def test_real_directories_are_required(tmp_path: Path) -> None:
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
    assert _normalized_real_directory(real, label="fixture") == real


def test_runtime_tree_rejects_external_symlink_bridge_that_reenters_root(tmp_path: Path) -> None:
    root = tmp_path / "reviewed-keg"
    outside = tmp_path / "mutable-bridge"
    root.mkdir()
    outside.mkdir()
    (root / "a").write_bytes(b"locked-a")
    (root / "b").write_bytes(b"locked-b")
    (outside / "bridge").symlink_to(root / "a")
    (root / "reenter").symlink_to(outside / "bridge")

    with pytest.raises(SecurityError, match="escapes its reviewed root"):
        _immutable_tree_sha256(root, label="fixture keg")
    (outside / "bridge").unlink()
    (outside / "bridge").symlink_to(root / "b")
    with pytest.raises(SecurityError, match="escapes its reviewed root"):
        _immutable_tree_sha256(root, label="fixture keg")

    (root / "reenter").unlink()
    (root / "internal").symlink_to("a")
    assert len(_immutable_tree_sha256(root, label="fixture keg")) == 64


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


def test_macho_helpers_cover_rpaths_and_escape_rejection(
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

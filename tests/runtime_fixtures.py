"""Temporary locked Python closures for tests that must not depend on a host install."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.registry import CandidateRegistry, PythonRuntimeLock, load_registry
from cole_voice_lab.runtime import _immutable_tree_sha256

ROOT = Path(__file__).resolve().parents[1]
MACHO_PAYLOAD = b"\xcf\xfa\xed\xfefixture interpreter or shared library\n"
SYSTEM_LIBRARY = "/usr/lib/libSystem.B.dylib"


@dataclass
class PythonRuntimeFixture:
    """Real file closure and controllable responses at the OS process boundary."""

    registry: CandidateRegistry
    python: Path
    lock: PythonRuntimeLock
    macos_product_version: str
    macos_build_version: str

    def create_environment(self, environment: Path) -> Path:
        """Create only the venv files needed for receipt and integrity verification."""

        binaries = environment / "bin"
        binaries.mkdir(parents=True)
        (binaries / "python3.12").symlink_to(self.python)
        (binaries / "python3").symlink_to("python3.12")
        (binaries / "python").symlink_to("python3.12")
        (environment / "pyvenv.cfg").write_text(
            f"home = {self.python.parent}\ninclude-system-site-packages = false\n",
            encoding="utf-8",
        )
        site_packages = environment / "lib/python3.12/site-packages"
        site_packages.mkdir(parents=True)
        return site_packages


def synthetic_python_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> PythonRuntimeFixture:
    """@canonical fixture Python closure: hash real files; fake only subprocess evidence."""

    loaded = load_registry(ROOT / "config/candidates.toml", ROOT / "config/model-sources.lock.json")
    original = loaded.runtime("mlx-audio-0.5.0").python_runtime
    fixture_root = (tmp_path / "fixture-homebrew").resolve()
    prefix = fixture_root / "Cellar/python@3.12/fixture"
    python = prefix / "bin/python3.12"
    python.parent.mkdir(parents=True)
    python.write_bytes(MACHO_PAYLOAD)
    python.chmod(0o700)
    stdlib = prefix / "lib/python3.12"
    stdlib.mkdir(parents=True)
    (stdlib / "fixture.py").write_text("# locked standard-library fixture\n", encoding="utf-8")
    opt = fixture_root / "opt"
    opt.mkdir()
    python_alias = opt / "python@3.12"
    python_target = "../Cellar/python@3.12/fixture"
    python_alias.symlink_to(python_target)
    dependencies = []
    for dependency in original.dependency_prefixes:
        dependency_prefix = fixture_root / "Cellar" / dependency.formula / "fixture"
        dependency_prefix.mkdir(parents=True)
        (dependency_prefix / "library.dylib").write_bytes(MACHO_PAYLOAD)
        dependency_target = f"../Cellar/{dependency.formula}/fixture"
        dependency_alias = opt / dependency.formula
        dependency_alias.symlink_to(dependency_target)
        dependencies.append(
            dependency.model_copy(
                update={
                    "prefix_path": str(dependency_prefix),
                    "opt_alias_path": str(dependency_alias),
                    "opt_alias_target": dependency_target,
                    "tree_sha256": _immutable_tree_sha256(dependency_prefix, label="fixture"),
                }
            )
        )
    # Production schemas intentionally constrain paths to /opt/homebrew. The test
    # copies relocate those fields under tmp_path without weakening their validator.
    lock = original.model_copy(
        update={
            "prefix_path": str(prefix),
            "opt_alias_path": str(python_alias),
            "opt_alias_target": python_target,
            "executable_sha256": hashlib.sha256(MACHO_PAYLOAD).hexdigest(),
            "prefix_tree_sha256": _immutable_tree_sha256(prefix, label="fixture"),
            "dependency_prefixes": tuple(dependencies),
            "system_library_references": (SYSTEM_LIBRARY,),
        }
    )
    registry = CandidateRegistry(
        loaded.configuration,
        loaded.lock.model_copy(
            update={
                "runtimes": tuple(
                    runtime.model_copy(update={"python_runtime": lock})
                    for runtime in loaded.lock.runtimes
                )
            }
        ),
    )
    fixture = PythonRuntimeFixture(
        registry, python, lock, lock.macos_product_version, lock.macos_build_version
    )
    original_run = subprocess.run

    def run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if tuple(command) == ("/usr/bin/sw_vers", "-productVersion"):
            return subprocess.CompletedProcess(command, 0, fixture.macos_product_version + "\n")
        if tuple(command) == ("/usr/bin/sw_vers", "-buildVersion"):
            return subprocess.CompletedProcess(command, 0, fixture.macos_build_version + "\n")
        if tuple(command[:2]) == ("/usr/bin/otool", "-L") and Path(command[2]).is_relative_to(
            fixture_root
        ):
            return subprocess.CompletedProcess(command, 0, f"{command[2]}:\n {SYSTEM_LIBRARY}\n")
        if len(command) > 2 and command[-2] == "-c" and Path(command[0]).resolve() == python:
            assert command[1:3] == ("-I", "-P")
            assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
            probe = {
                "implementation": lock.implementation,
                "version": lock.version,
                "build": lock.build,
                "machine": lock.machine,
                "base_prefix": str(prefix),
                "stdlib": str(stdlib),
                "path": [str(stdlib)],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(probe))
        return original_run(command, **kwargs)

    monkeypatch.setattr("cole_voice_lab.runtime.subprocess.run", run)
    return fixture

"""Ordinary synthetic host launches retain isolation across fixture process boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from finetune_stub_runtime import stub_finetune_runtime, write_stub_mode
from recording_fixtures import REPOSITORY_ROOT
from vault_binding_fixtures import restore_permissions

sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import native_finetune_fixture as finetune_fixture  # noqa: E402
import verify_native_bridge as bridge  # noqa: E402
import verify_native_workspace as workspace  # noqa: E402


@pytest.fixture(autouse=True)
def private_fixture_permissions(tmp_path: Path) -> Iterator[None]:
    original = os.umask(0o077)
    try:
        yield
    finally:
        restore_permissions(tmp_path)
        os.umask(original)


def test_copied_runtime_identity_and_wrapper_share_explicit_imports_without_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    environment = workspace.fixture_environment()
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import json, sys\nfrom cole_voice_lab import recording_service\n"
        "print(json.dumps({'isolated': sys.flags.isolated, "
        "'no_bytecode': sys.dont_write_bytecode, 'module': recording_service.__file__, "
        "'arguments': sys.argv[1:], "
        "'build': recording_service.recording_service_build_identity().model_dump(mode='json')}))\n"
    )
    root = tmp_path / "fixture"
    identity = workspace.isolated_service_runtime(root, wrapper, "_fixture_probe", environment)
    tail = ["--test-barriers", str(root), "--native-workspace", "--subject-id", "synthetic"]
    result = subprocess.run(
        [sys.executable, *workspace.copied_service_arguments(root, "_fixture_probe"), *tail],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["isolated"] == 1 and report["no_bytecode"] is True
    assert Path(report["module"]).is_relative_to(root / "python-runtime")
    assert report["arguments"] == tail
    assert report["build"] == identity.model_dump(mode="json")
    assert not list(root.rglob("__pycache__"))


def test_fake_f5_host_is_isolated_and_preserves_fake_runtime_arguments(tmp_path: Path) -> None:
    root, _ = stub_finetune_runtime(tmp_path / "f5", {"host_probe": True})
    arguments = ["-B", "-c", "fake runtime probe", "argument with spaces"]
    result = subprocess.run(
        [str(root / "bin/python"), *arguments], capture_output=True, text=True, check=True
    )
    report = json.loads(result.stdout)
    assert report["isolated"] == 1 and report["no_bytecode"] is True
    assert report["arguments"] == arguments
    assert report["executable"] == sys.executable
    # The shell exec must keep the fake's ordinary probe emulation, including caller -B.
    write_stub_mode(root, {})
    probe = subprocess.run(
        [str(root / "bin/python"), "-B", "-c", "fake runtime probe"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(probe.stdout)["f5_tts_version"] == "1.1.22-stub"
    assert not list(root.rglob("__pycache__"))


class ReachedNative(Exception):
    """Stop a fixture at its native launch boundary, before executing any native product."""


@pytest.fixture
def builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> workspace.NativeBuilds:
    identity = workspace.RecordingBuildIdentity(
        build_id="synthetic-fixture",
        source_sha256="a" * 64,
        executable_sha256="b" * 64,
        environment_sha256="c" * 64,
    )
    value = workspace.NativeBuilds(
        helper=tmp_path / "ColeVoiceRecorderHelper",
        harness=tmp_path / "RecordingHarness",
        recorder=identity,
        client=identity,
    )
    monkeypatch.setattr(workspace, "native_builds", lambda *_: value)
    monkeypatch.setattr(finetune_fixture, "native_builds", lambda *_: value)
    return value


@pytest.mark.parametrize(
    ("suite", "delayed"),
    [("review", False), ("transport", False), ("recovery", False), ("transport", True)],
)
def test_workspace_configuration_preserves_service_flags_and_delayed_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builds: workspace.NativeBuilds,
    suite: str,
    delayed: bool,
) -> None:
    root = tmp_path / "fixture"
    original_run = subprocess.run

    def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command[0] == str(builds.harness):
            raise ReachedNative
        assert command[1:3] == ["-I", "-B"]
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fixture",
            "--suite",
            suite,
            "--package",
            str(REPOSITORY_ROOT / "apps/macos"),
            "--binary-directory",
            str(tmp_path),
            "--output",
            str(root),
            *(["--delayed"] if delayed else []),
        ],
    )
    with pytest.raises(ReachedNative):
        workspace.main()
    service = json.loads((root / "recording-application.json").read_text())["service"]
    expected = (
        workspace.copied_service_arguments(root, "_synthetic_workspace_fixture")
        + ["--test-barriers", str(root)]
        if delayed
        else list(workspace.SERVICE_MODULE_ARGUMENTS)
    )
    assert service["arguments"] == expected + [
        "--native-workspace",
        "--subject-id",
        workspace.SUBJECT,
    ]
    assert service["arguments"][:2] == ["-I", "-B"]
    assert service["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not list(root.rglob("__pycache__"))


@pytest.mark.parametrize("configured", [False, True])
def test_finetune_configuration_uses_isolated_service_and_preserves_fixture_options(
    tmp_path: Path, builds: workspace.NativeBuilds, configured: bool
) -> None:
    fixture = finetune_fixture.prepare_finetune_fixture(
        tmp_path / "fixture",
        package=REPOSITORY_ROOT / "apps/macos",
        binary_directory=tmp_path,
        runtime_configured=configured,
        synthesis_deadline=10 if configured else None,
    )
    service = json.loads(fixture.configuration.read_text())["service"]
    arguments = service["arguments"]
    assert arguments[:2] == ["-I", "-B"]
    assert service["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    if configured:
        assert arguments[:6] == workspace.copied_service_arguments(
            fixture.root, finetune_fixture.SERVICE_WRAPPER_MODULE
        )
        assert arguments[arguments.index("--test-pretrained-pins") + 1]
        assert arguments[arguments.index("--test-synthesis-deadline") + 1] == "10"
    else:
        assert arguments[:4] == list(workspace.SERVICE_MODULE_ARGUMENTS)
    assert arguments[-2:] == ["--finetune-runtime-root", str(fixture.runtime_root)]


def test_capture_owner_host_command_is_isolated_before_any_child_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[list[str], dict[str, str]]] = []

    def popen(command: list[str], *, env: dict[str, str]) -> None:
        observed.append((command, env))
        raise ReachedNative

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(ReachedNative):
        workspace.run_with_live_capture_owner(
            tmp_path, tmp_path / "assets", tmp_path / "harness", tmp_path / "config"
        )
    command, environment = observed[0]
    assert command[:3] == [sys.executable, "-I", "-B"]
    assert Path(command[3]).name == "native_recovery_holder.py"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_bridge_manifest_passes_isolated_service_arguments_to_swift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fixture"
    binary = tmp_path / "binaries"
    binary.mkdir()
    for name in ("ColeVoiceRecorderHelper", "RecordingBridgeHarness"):
        (binary / name).write_bytes(b"synthetic executable placeholder")

    def run(*args: Any, **kwargs: Any) -> None:
        raise ReachedNative

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(bridge.platform, "platform", lambda: "synthetic")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fixture",
            "--package",
            str(REPOSITORY_ROOT / "apps/macos"),
            "--binary-directory",
            str(binary),
            "--output",
            str(root),
        ],
    )
    with pytest.raises(ReachedNative):
        bridge.main()
    manifest = json.loads((root / "bridge.json").read_text())
    assert manifest["service_arguments"][:4] == list(workspace.SERVICE_MODULE_ARGUMENTS)
    assert manifest["service_environment"]["PYTHONDONTWRITEBYTECODE"] == "1"

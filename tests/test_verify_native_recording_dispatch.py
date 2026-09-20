"""Verifier dispatch uses fresh evidence and runs acceptance's full native prerequisite suite."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from recording_fixtures import REPOSITORY_ROOT

sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import verify_native_recording as verifier  # noqa: E402


@pytest.fixture(autouse=True)
def preserve_umask() -> Iterator[None]:
    original = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(original)


def no_tools(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("invalid verifier request reached build or runtime tools")


def test_acceptance_requires_its_benchmark_before_any_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verifier, "output", no_tools)
    monkeypatch.setattr(verifier, "run", no_tools)
    with pytest.raises(SystemExit) as caught:
        verifier.main(["--suite", "acceptance"])
    assert caught.value.code == 2


@pytest.mark.parametrize("suite", ["acceptance", "all", "recovery"])
@pytest.mark.parametrize("existing", ["verification.json", "shell/geometry.json"])
def test_verifier_preserves_all_existing_evidence_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suite: str, existing: str
) -> None:
    root = tmp_path / "existing"
    artifact = root / existing
    artifact.parent.mkdir(mode=0o700, parents=True)
    artifact.write_bytes(b"previous verification evidence\n")
    monkeypatch.setattr(verifier, "output", no_tools)
    monkeypatch.setattr(verifier, "run", no_tools)
    arguments = ["--suite", suite, "--output", str(root)]
    if suite == "acceptance":
        (tmp_path / "report.json").write_text("{}")
        arguments += ["--benchmark-report", str(tmp_path / "report.json")]
    with pytest.raises(SystemExit) as caught:
        verifier.main(arguments)
    assert caught.value.code == 2
    assert artifact.read_bytes() == b"previous verification evidence\n"
    assert set(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()) == {
        existing
    }


@pytest.mark.parametrize("suite", ["acceptance", "all", "recovery", "vault"])
@pytest.mark.parametrize("external_scratch", [False, True])
def test_fresh_dispatch_reaches_full_suite_or_recovery_inventory_without_running_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suite: str, external_scratch: bool
) -> None:
    if external_scratch:
        scratch = tmp_path.resolve() / "scratch"
        scratch.mkdir(mode=0o700)
        monkeypatch.setenv("COLE_VOICE_NATIVE_SCRATCH_ROOT", str(scratch))
    else:
        scratch = REPOSITORY_ROOT / "build"
        monkeypatch.delenv("COLE_VOICE_NATIVE_SCRATCH_ROOT", raising=False)
    developer = tmp_path / "developer"
    (developer / "Library/Developer/Frameworks/Testing.framework").mkdir(parents=True)
    root = tmp_path / "fresh"
    root.mkdir(mode=0o700)
    commands: list[list[str]] = []

    class ReachedSuite(Exception):
        pass

    def output(command: list[str], **kwargs: Any) -> str:
        if command == ["xcode-select", "-p"]:
            return str(developer)
        if "--show-bin-path" in command:
            return str(tmp_path / "binaries")
        raise AssertionError(f"unexpected command before selected suite: {command}")

    def run(command: list[str], **kwargs: Any) -> None:
        commands.append(command)
        if "test" in command:
            raise ReachedSuite

    monkeypatch.setattr(verifier, "output", output)
    monkeypatch.setattr(verifier, "run", run)
    arguments = ["--suite", suite, "--output", str(root)]
    if suite == "acceptance":
        (tmp_path / "report.json").write_text("{}")
        arguments += ["--benchmark-report", str(tmp_path / "report.json")]
    with pytest.raises(ReachedSuite):
        verifier.main(arguments)
    assert commands[0][:3] == ["xcrun", "swift", "build"]
    assert commands[0][commands[0].index("--scratch-path") + 1] == str(scratch / "native-recording")
    expected = (
        "RecordingVaultTests"
        if suite == "vault"
        else "RecordingRecoveryTests|RecordingRecoveryInventoryTests"
        if suite == "recovery"
        else "RecordingReviewTests|RecordingPlaybackTests|RecordingWaveformTests|"
        "NativeCaptureTests|SyntheticCaptureBackendTests|RecordingHarnessTests"
    )
    assert commands[-1][-2:] == ["--filter", expected]
    if suite not in {"recovery", "vault"}:
        assert commands[-1][3:5] == ["--sanitize", "address"]
    assert commands[-1][commands[-1].index("--scratch-path") + 1] == str(
        scratch
        / (
            "native-recording-tests"
            if suite in {"recovery", "vault"}
            else "native-recording-review-address"
        )
    )
    assert not (root / "verification.json").exists()


@pytest.mark.parametrize("unsafe_child", [False, True])
def test_capture_sanitizer_scratch_uses_the_validated_external_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_child: bool
) -> None:
    scratch = tmp_path.resolve() / "scratch"
    scratch.mkdir(mode=0o700)
    if unsafe_child:
        (scratch / "native-recording-tests-thread").symlink_to(
            REPOSITORY_ROOT / "build", target_is_directory=True
        )
    monkeypatch.setenv("COLE_VOICE_NATIVE_SCRATCH_ROOT", str(scratch))
    developer = tmp_path / "developer"
    (developer / "Library/Developer/Frameworks/Testing.framework").mkdir(parents=True)
    root = tmp_path / "fresh"
    root.mkdir(mode=0o700)
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in (
        "ColeVoiceRecorderPreview",
        "ColeVoiceRecorderHelper",
        "RecordingHarness",
        "RecordingBridgeHarness",
    ):
        (binaries / name).write_bytes(b"synthetic executable placeholder")
    scratch_paths: list[tuple[str, str]] = []

    class ReachedSuite(Exception):
        pass

    def output(command: list[str], **kwargs: Any) -> str:
        if command == ["xcode-select", "-p"]:
            return str(developer)
        if "--show-bin-path" in command:
            return str(tmp_path / "binaries")
        # Identity metadata only; no tool runs after the build-path probes.
        return "synthetic identity minos 13.0"

    def run(command: list[str], **kwargs: Any) -> None:
        if command[:3] == ["xcrun", "swift", "test"]:
            variant = command[command.index("--sanitize") + 1]
            scratch_paths.append((variant, command[command.index("--scratch-path") + 1]))
            if variant == "thread":
                raise ReachedSuite

    monkeypatch.setattr(verifier, "output", output)
    monkeypatch.setattr(verifier, "run", run)
    arguments = ["--suite", "capture-sanitizers", "--output", str(root)]
    if unsafe_child:
        with pytest.raises(ValueError, match="child must not be a symbolic link"):
            verifier.main(arguments)
        assert scratch_paths == [("address", str(scratch / "native-recording-tests-address"))]
    else:
        with pytest.raises(ReachedSuite):
            verifier.main(arguments)
        assert scratch_paths == [
            ("address", str(scratch / "native-recording-tests-address")),
            ("thread", str(scratch / "native-recording-tests-thread")),
        ]


@pytest.mark.parametrize("suite", ["review", "transport", "finetune", "speak", "vault", "recovery"])
def test_workspace_routes_use_isolated_python_without_running_native_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suite: str
) -> None:
    developer = tmp_path / "developer"
    (developer / "Library/Developer/Frameworks/Testing.framework").mkdir(parents=True)
    commands: list[list[str]] = []

    class ReachedPython(Exception):
        pass

    def output(command: list[str], **kwargs: Any) -> str:
        return str(developer if command == ["xcode-select", "-p"] else tmp_path / "binaries")

    def run(command: list[str], **kwargs: Any) -> None:
        if command[0] == str(REPOSITORY_ROOT / ".venv/bin/python"):
            commands.append(command)
            raise ReachedPython

    monkeypatch.setattr(verifier, "output", output)
    monkeypatch.setattr(verifier, "run", run)
    with pytest.raises(ReachedPython):
        verifier.main(["--suite", suite, "--output", str(tmp_path / "evidence")])
    command = commands[0]
    assert command[1:4] == ["-I", "-B", "-c"]
    assert command[5:8] == [
        str(REPOSITORY_ROOT / "scripts"),
        str(REPOSITORY_ROOT / "tests"),
        "verify_native_workspace",
    ]
    if suite == "transport":
        assert "--suite" not in command
    else:
        assert command[command.index("--suite") + 1] == suite


def test_isolated_module_imports_explicit_fixtures_and_ignores_ambient_path(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fixtures = tmp_path / "tests"
    fixtures.mkdir()
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "sitecustomize.py").write_text("raise RuntimeError('ambient path executed')\n")
    (fixtures / "probe_fixture.py").write_text("VALUE = 'explicit fixture'\n")
    (scripts / "probe_verifier.py").write_text(
        "import json, sys\nfrom probe_fixture import VALUE\n"
        "print(json.dumps([sys.flags.isolated, sys.dont_write_bytecode, VALUE, sys.argv[1:]]))\n"
    )
    command = verifier.isolated_script(tmp_path, "probe_verifier") + ["synthetic argument"]
    command[0] = str(REPOSITORY_ROOT / ".venv/bin/python")
    result = subprocess.run(
        command,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ambient)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [1, True, "explicit fixture", ["synthetic argument"]]
    assert not list(tmp_path.rglob("__pycache__"))


@pytest.mark.parametrize("rollback", ["unset", "present", "missing"])
def test_packaged_dispatch_never_discovers_real_rollback_and_honors_explicit_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback: str
) -> None:
    developer = tmp_path / "developer"
    (developer / "Library/Developer/Frameworks/Testing.framework").mkdir(parents=True)
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in (
        "ColeVoiceRecorderPreview",
        "ColeVoiceRecorderHelper",
        "RecordingHarness",
        "RecordingBridgeHarness",
    ):
        (binaries / name).write_bytes(b"synthetic executable placeholder")
    explicit = tmp_path / "explicit-release"
    if rollback == "unset":
        monkeypatch.delenv("COLE_VOICE_ROLLBACK_RELEASE", raising=False)
    else:
        monkeypatch.setenv("COLE_VOICE_ROLLBACK_RELEASE", str(explicit))
        if rollback == "present":
            explicit.mkdir()
    original_stat = Path.stat

    def guarded_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        assert "Application Support" not in str(path), "default real-root probe"
        return original_stat(path, *args, **kwargs)

    captured: list[tuple[list[str], dict[str, str]]] = []

    def output(command: list[str], **kwargs: Any) -> str:
        if command == ["xcode-select", "-p"]:
            return str(developer)
        if "--show-bin-path" in command:
            return str(binaries)
        return "synthetic identity minos 13.0"

    def run(command: list[str], **kwargs: Any) -> None:
        if "pytest" in command:
            captured.append((command, kwargs["env"]))

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(verifier, "output", output)
    monkeypatch.setattr(verifier, "run", run)
    root = tmp_path / "evidence"
    assert verifier.main(["--suite", "packaged-app", "--output", str(root)]) == 0
    command, environment = captured[0]
    assert command[:5] == [str(REPOSITORY_ROOT / ".venv/bin/python"), "-I", "-B", "-m", "pytest"]
    assert environment.get("COLE_VOICE_ROLLBACK_RELEASE") == (
        None if rollback == "unset" else str(explicit)
    )
    identity = json.loads((root / "verification.json").read_text())
    assert identity["packaged_app_rollback_release_present"] is (rollback == "present")

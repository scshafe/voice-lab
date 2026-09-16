"""Verifier dispatch uses fresh evidence and runs acceptance's full native prerequisite suite."""

from __future__ import annotations

import os
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


@pytest.mark.parametrize("suite", ["acceptance", "all", "recovery"])
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
        "RecordingRecoveryTests|RecordingRecoveryInventoryTests"
        if suite == "recovery"
        else "RecordingReviewTests|RecordingPlaybackTests|RecordingWaveformTests|"
        "NativeCaptureTests|SyntheticCaptureBackendTests|RecordingHarnessTests"
    )
    assert commands[-1][-2:] == ["--filter", expected]
    if suite != "recovery":
        assert commands[-1][3:5] == ["--sanitize", "address"]
    assert commands[-1][commands[-1].index("--scratch-path") + 1] == str(
        scratch
        / ("native-recording-tests" if suite == "recovery" else "native-recording-review-address")
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

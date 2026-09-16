"""External Swift scratch overrides fail before any build or existing-path mutation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from recording_fixtures import REPOSITORY_ROOT

sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
from recording_native_scratch import (  # noqa: E402
    SCRATCH_ROOT_ENVIRONMENT,
    native_recording_scratch_path,
)

from cole_voice_lab.paths import AssetLayout  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_scratch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SCRATCH_ROOT_ENVIRONMENT, raising=False)


def test_unset_override_preserves_existing_default_without_creating_it(tmp_path: Path) -> None:
    default = tmp_path / "repository/build/native-recording"
    assert native_recording_scratch_path(default, repository=tmp_path / "repository") == default
    assert not default.exists()


@pytest.mark.parametrize(
    "name",
    [
        "native-recording",
        "native-recording-tests",
        "native-recording-tests-address",
        "native-recording-tests-thread",
        "native-recording-review-address",
        "native-recording-review-thread",
        "native-recording-release",
    ],
)
def test_override_preserves_separate_build_kinds_without_creating_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    scratch = tmp_path.resolve() / "scratch with spaces"
    scratch.mkdir(mode=0o700)
    monkeypatch.setenv(SCRATCH_ROOT_ENVIRONMENT, str(scratch))
    assert (
        native_recording_scratch_path(REPOSITORY_ROOT / "build" / name, repository=REPOSITORY_ROOT)
        == scratch / name
    )
    assert not tuple(scratch.iterdir())


@pytest.mark.parametrize("kind", ["empty", "relative", "missing", "public", "file", "alias"])
def test_invalid_root_is_rejected_without_changing_permissions_or_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path.resolve() / "scratch"
    configured = str(root)
    if kind in {"public", "alias"}:
        root.mkdir(mode=0o700)
    if kind == "public":
        root.chmod(0o755)
    elif kind == "file":
        root.write_text("preserved scratch collision")
    elif kind == "empty":
        configured = ""
    elif kind == "relative":
        configured = "relative-scratch"
    elif kind == "alias":
        alias = root.with_name("alias")
        alias.symlink_to(root, target_is_directory=True)
        configured = str(alias)
    before = root.stat() if root.exists() else None
    monkeypatch.setenv(SCRATCH_ROOT_ENVIRONMENT, configured)
    with pytest.raises(ValueError, match=SCRATCH_ROOT_ENVIRONMENT):
        native_recording_scratch_path(
            REPOSITORY_ROOT / "build/native-recording", repository=REPOSITORY_ROOT
        )
    assert (root.stat() if root.exists() else None) == before
    if kind == "file":
        assert root.read_text() == "preserved scratch collision"


@pytest.mark.parametrize("relationship", ["inside", "same", "ancestor"])
def test_explicit_repository_overlap_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relationship: str
) -> None:
    repository = tmp_path.resolve() / "repository"
    repository.mkdir(mode=0o700)
    root = {
        "inside": repository / "build",
        "same": repository,
        "ancestor": repository.parent,
    }[relationship]
    monkeypatch.setenv(SCRATCH_ROOT_ENVIRONMENT, str(root))
    with pytest.raises(ValueError, match="overlap the repository"):
        native_recording_scratch_path(repository / "build/native-recording", repository=repository)


@pytest.mark.parametrize("location", ["root", "descendant", "build-child"])
def test_asset_layouts_are_never_used_as_swift_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    root = tmp_path.resolve() / "private"
    root.mkdir(mode=0o700)
    assets = AssetLayout.initialize(root / "native-recording")
    selected = {"root": assets.root, "descendant": assets.root / "tmp", "build-child": root}[
        location
    ]
    monkeypatch.setenv(SCRATCH_ROOT_ENVIRONMENT, str(selected))
    with pytest.raises(ValueError, match="asset layout"):
        native_recording_scratch_path(
            REPOSITORY_ROOT / "build/native-recording", repository=REPOSITORY_ROOT
        )


@pytest.mark.parametrize("kind", ["alias", "file", "public"])
def test_existing_unsafe_build_child_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path.resolve()
    child = root / "native-recording"
    if kind == "alias":
        child.symlink_to(root, target_is_directory=True)
    elif kind == "file":
        child.write_text("preserved")
    else:
        child.mkdir(mode=0o755)
        child.chmod(0o755)
    monkeypatch.setenv(SCRATCH_ROOT_ENVIRONMENT, str(root))
    with pytest.raises(ValueError, match="child must"):
        native_recording_scratch_path(
            REPOSITORY_ROOT / "build/native-recording", repository=REPOSITORY_ROOT
        )


def test_benchmark_build_and_identity_use_the_same_external_release_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import recording_native_benchmark as benchmark

    root = tmp_path.resolve()
    expected = root / "native-recording-release"
    binary = expected / "arm64-apple-macosx/release"
    expected.mkdir(mode=0o700)
    binary.parent.mkdir(mode=0o700)
    binary.mkdir(mode=0o700)
    for name in ("RecordingHarness", "ColeVoiceRecorderHelper"):
        (binary / name).write_bytes(b"synthetic executable identity")
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> Any:
        from subprocess import CompletedProcess

        commands.append(command)
        result = str(binary) if "--show-bin-path" in command else "synthetic tool identity"
        return CompletedProcess(command, 0, stdout=result)

    monkeypatch.setenv(SCRATCH_ROOT_ENVIRONMENT, str(root))
    monkeypatch.setattr(benchmark.subprocess, "run", run)
    identity = benchmark.release_binaries()
    assert identity["swift_scratch_path"] == str(expected)
    assert identity["swift_build_command"] == commands[0]
    assert all(
        command[command.index("--scratch-path") + 1] == str(expected)
        for command in commands
        if "--scratch-path" in command
    )

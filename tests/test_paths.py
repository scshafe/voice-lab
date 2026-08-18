from __future__ import annotations

import stat
from pathlib import Path

import pytest

import cole_voice_lab.paths as paths
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.paths import (
    ASSET_DIRECTORIES,
    AssetLayout,
    confined_path,
    ensure_private_directory,
    reject_symlink_components,
    validate_relative_path,
)


@pytest.mark.parametrize(
    "value",
    ("", "/absolute", "../escape", "a/../escape", "a\\b", "a/./b", "a\x00b", "a\nb"),
)
def test_relative_path_rejects_unsafe_forms(value: str) -> None:
    with pytest.raises(ValueError):
        validate_relative_path(value)


def test_relative_path_preserves_canonical_posix_value() -> None:
    assert validate_relative_path("audio/prompt-001.wav") == "audio/prompt-001.wav"


def test_relative_path_rejects_noncanonical_redundant_separators() -> None:
    with pytest.raises(ValueError, match="canonical"):
        validate_relative_path("audio//prompt-001.wav")


def test_asset_layout_is_private_and_complete(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    validated = AssetLayout.validate(layout.root)

    assert stat.S_IMODE(validated.root.stat().st_mode) == 0o700
    assert all(validated.directory(name).is_dir() for name in ASSET_DIRECTORIES)


def test_asset_layout_rejects_broad_root() -> None:
    with pytest.raises(SecurityError, match="broad"):
        AssetLayout.initialize(Path.home())


def test_asset_layout_rejects_insufficiently_specific_root() -> None:
    with pytest.raises(SecurityError, match="not specific enough"):
        paths._reject_dangerously_broad_root(Path("/voice-assets"))


def test_confined_path_rejects_symlink_component(tmp_path: Path) -> None:
    root = tmp_path / "private" / "root"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SecurityError, match="symlink"):
        confined_path(root, "link/private.wav")


def test_path_confinement_rejects_outside_and_missing_required_paths(tmp_path: Path) -> None:
    root = tmp_path / "private" / "root"
    root.mkdir(parents=True)

    with pytest.raises(SecurityError, match="outside root"):
        reject_symlink_components(root, tmp_path / "outside.wav")
    with pytest.raises(FileNotFoundError):
        confined_path(root, "missing/file.wav", must_exist=True)

    existing = root / "existing"
    existing.mkdir()
    candidate = confined_path(root, "existing/new.wav")
    assert candidate == existing / "new.wav"


def test_ensure_private_directory_rejects_symlink_in_existing_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "private" / "linked"
    linked.parent.mkdir()
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SecurityError, match="symlink component"):
        ensure_private_directory(linked / "voice-assets")


def test_asset_layout_rejects_symlink_root(tmp_path: Path) -> None:
    real_root = tmp_path / "private" / "real-assets"
    AssetLayout.initialize(real_root)
    linked_root = tmp_path / "private" / "linked-assets"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(SecurityError, match="symlink"):
        AssetLayout.validate(linked_root)


def test_asset_layout_initialize_rejects_symlinked_member(tmp_path: Path) -> None:
    root = tmp_path / "private" / "voice-assets"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (root / ASSET_DIRECTORIES[0]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(SecurityError, match="not a real directory"):
        AssetLayout.initialize(root)


def test_asset_layout_validation_rejects_public_root_and_members(tmp_path: Path) -> None:
    root = tmp_path / "private" / "voice-assets"
    layout = AssetLayout.initialize(root)
    root.chmod(0o755)
    with pytest.raises(SecurityError, match="asset root"):
        AssetLayout.validate(root)

    root.chmod(0o700)
    member = layout.directory(ASSET_DIRECTORIES[0])
    member.chmod(0o755)
    with pytest.raises(SecurityError, match="layout member must be private"):
        AssetLayout.validate(root)


def test_asset_layout_validation_rejects_missing_member_and_unknown_name(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    layout.directory(ASSET_DIRECTORIES[0]).rmdir()

    with pytest.raises(SecurityError, match="missing or unsafe"):
        AssetLayout.validate(layout.root)
    with pytest.raises(KeyError):
        layout.directory("not-a-layout-member")

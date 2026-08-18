"""Private asset layout and path-confinement rules."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import SecurityError

ASSET_DIRECTORIES = (
    "incoming",
    "raw",
    "derived",
    "references",
    "provenance",
    "experiments",
    "evaluations",
    "models",
    "runtimes",
    "tmp",
)


def validate_relative_path(value: str) -> str:
    """Return a canonical portable relative path or fail closed."""

    if not value or "\\" in value or "\x00" in value:
        raise ValueError("paths must be non-empty POSIX relative paths")
    if any(ord(character) < 32 for character in value):
        raise ValueError("paths cannot contain control characters")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("absolute, empty, dot, and parent path components are forbidden")
    canonical = path.as_posix()
    if canonical != value:
        raise ValueError("path must already be in canonical POSIX form")
    return canonical


def _reject_dangerously_broad_root(root: Path) -> None:
    home = Path.home().resolve()
    forbidden = {Path("/").resolve(), home}
    if root in forbidden:
        raise SecurityError(f"refusing dangerously broad asset root: {root}")
    if len(root.parts) < 4:
        raise SecurityError(f"asset root is not specific enough: {root}")


def _reject_existing_symlink_components(path: Path) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor /= component
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(f"symlink component is forbidden: {cursor}")
    return absolute


def ensure_private_directory(path: Path) -> Path:
    absolute = _reject_existing_symlink_components(path)
    resolved = absolute.resolve(strict=False)
    _reject_dangerously_broad_root(resolved)
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = resolved.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"private directory is not a real directory: {resolved}")
    os.chmod(resolved, 0o700)
    return resolved


def reject_symlink_components(root: Path, path: Path) -> None:
    """Reject symlinks in every existing component below root."""

    resolved_root = root.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise SecurityError(f"path is outside root {root}: {path}") from error
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(f"symlink component is forbidden: {cursor}")
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise SecurityError(f"resolved path escapes root {root}: {path}") from error


def confined_path(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    validated = validate_relative_path(relative)
    candidate = root.joinpath(*PurePosixPath(validated).parts)
    reject_symlink_components(root, candidate)
    if must_exist and not candidate.exists():
        raise FileNotFoundError(candidate)
    return candidate


@dataclass(frozen=True, slots=True)
class AssetLayout:
    root: Path

    @classmethod
    def initialize(cls, root: Path) -> AssetLayout:
        private_root = ensure_private_directory(root)
        for name in ASSET_DIRECTORIES:
            child = private_root / name
            child.mkdir(mode=0o700, exist_ok=True)
            if child.is_symlink() or not child.is_dir():
                raise SecurityError(f"asset layout member is not a real directory: {child}")
            os.chmod(child, 0o700)
        return cls(private_root)

    @classmethod
    def validate(cls, root: Path) -> AssetLayout:
        absolute = _reject_existing_symlink_components(root)
        private_root = absolute.resolve(strict=True)
        _reject_dangerously_broad_root(private_root)
        mode = stat.S_IMODE(private_root.stat().st_mode)
        if mode & 0o077:
            raise SecurityError(f"asset root must not be group/world accessible: {private_root}")
        for name in ASSET_DIRECTORIES:
            child = private_root / name
            if child.is_symlink() or not child.is_dir():
                raise SecurityError(f"asset layout member is missing or unsafe: {child}")
            if stat.S_IMODE(child.stat().st_mode) & 0o077:
                raise SecurityError(f"asset layout member must be private: {child}")
        return cls(private_root)

    def directory(self, name: str) -> Path:
        if name not in ASSET_DIRECTORIES:
            raise KeyError(name)
        return self.root / name

"""Resolve optional private SwiftPM scratch paths for recording verification.

@canonical recording_native_scratch -- benchmark, native verifier and scripts/verify.
COLE_VOICE_NATIVE_SCRATCH_ROOT is an existing resolved private external directory.
This resolver only inspects paths; it never creates, clears or changes their permissions.
"""

from __future__ import annotations

import os
from pathlib import Path

from cole_voice_lab.paths import ASSET_DIRECTORIES

SCRATCH_ROOT_ENVIRONMENT = "COLE_VOICE_NATIVE_SCRATCH_ROOT"


def native_recording_scratch_path(default: Path, *, repository: Path) -> Path:
    """Return the default or its named child under the validated external override.

    Defaults retain their existing behavior. An explicit override must already be an
    absolute, resolved, private directory outside the repository and any asset layout.
    Unsafe overrides/children raise ValueError before a compiler can write into them.
    """

    configured = os.environ.get(SCRATCH_ROOT_ENVIRONMENT)
    if configured is None:
        return default
    root = Path(configured)
    if not configured or not root.is_absolute() or root != root.resolve():
        raise ValueError(f"{SCRATCH_ROOT_ENVIRONMENT} must be an absolute resolved directory")
    repository = repository.resolve()
    if root == repository or root.is_relative_to(repository) or repository.is_relative_to(root):
        raise ValueError(f"{SCRATCH_ROOT_ENVIRONMENT} must not overlap the repository")
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError(f"{SCRATCH_ROOT_ENVIRONMENT} must be an existing private directory")
    scratch = root / default.name
    if any(
        all((parent / name).is_dir() for name in ASSET_DIRECTORIES)
        for parent in (scratch, root, *root.parents)
    ):
        raise ValueError(f"{SCRATCH_ROOT_ENVIRONMENT} must not be inside an asset layout")
    if scratch.is_symlink() or scratch != scratch.resolve():
        raise ValueError(f"{SCRATCH_ROOT_ENVIRONMENT} child must not be a symbolic link")
    if scratch.exists() and (not scratch.is_dir() or scratch.stat().st_mode & 0o077):
        raise ValueError(f"{SCRATCH_ROOT_ENVIRONMENT} child must be a private directory")
    return scratch

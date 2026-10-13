"""Private directories for the corpus client's files (0700; records are 0600)."""

from __future__ import annotations

import os
from pathlib import Path


def private_dir(path: Path) -> Path:
    """``path`` as a directory of mode 0700, with every directory it had to create 0700 too
    (``mkdir -p`` would give those the default mode)."""
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path

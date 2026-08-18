"""Explicitly invoked curated Hugging Face snapshot downloader."""

from __future__ import annotations

import argparse
import importlib
import re
from pathlib import Path

from ..errors import ContractError
from ..paths import validate_relative_path

_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - network opt-in
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--allow-file", action="append", required=True)
    options = parser.parse_args(argv)
    if _REVISION.fullmatch(options.revision) is None:
        raise ContractError("snapshot revision must be an immutable full hash")
    allow_files = tuple(options.allow_file)
    if len(allow_files) != len(set(allow_files)):
        raise ContractError("snapshot allow-list contains duplicates")
    for value in allow_files:
        validate_relative_path(value)
        if any(character in value for character in "*?["):
            raise ContractError("snapshot allow-list must contain exact paths")
    destination = options.destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise ContractError(f"refusing to download over existing path: {destination}")

    huggingface_hub = importlib.import_module("huggingface_hub")

    resolved = huggingface_hub.snapshot_download(
        repo_id=options.repository,
        revision=options.revision,
        allow_patterns=list(allow_files),
        local_dir=str(destination),
    )
    if Path(resolved).resolve() != destination:
        raise ContractError("snapshot downloader returned an unexpected destination")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())

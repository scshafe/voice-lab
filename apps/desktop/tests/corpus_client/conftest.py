"""The corpus client's fixtures: a PC (the backend's API and intake publisher over a seeded
synthetic lab, ``corpus_rig.PC``) and this machine's library with the approval bound."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pytest  # noqa: E402
from corpus_rig import PC, bind_library  # noqa: E402

from cole_voice_lab.paths import AssetLayout  # noqa: E402


@pytest.fixture
def pc(tmp_path: Path) -> Iterator[PC]:
    """The PC, up."""
    machine = PC.create(tmp_path / "pc").start()
    try:
        yield machine
    finally:
        machine.stop()


@pytest.fixture
def library(data_dir: Path) -> AssetLayout:
    return bind_library(data_dir)

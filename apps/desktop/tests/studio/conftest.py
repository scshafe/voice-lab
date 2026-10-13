"""The prompt studio's fixtures: the PC (``corpus_rig.PC``: the backend's API and intake
publisher over a synthetic lab whose catalog is seeded), the app in studio mode over it with
its corpus client made as the corpus client's tests make it, and a stand-in recorder that
leaves the worker's files. Offscreen; no device, no network beyond 127.0.0.1."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE.parent / "corpus_client"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pytest  # noqa: E402
from conftest import AppRig, RecordingOutput, Window, open_window  # noqa: E402
from corpus_rig import PC, bind_library, client  # noqa: E402
from studio_fakes import StudioRecorder  # noqa: E402
from voice_lab_desktop.config import Config  # noqa: E402

from cole_voice_lab.paths import AssetLayout  # noqa: E402


@pytest.fixture
def pc(tmp_path: Path) -> Iterator[PC]:
    """The PC, up, with cole-corpus version 1 (pilot-v2 without its room tones) active."""
    machine = PC.create(tmp_path / "pc").start()
    try:
        yield machine
    finally:
        machine.stop()


@pytest.fixture
def library(data_dir: Path) -> AssetLayout:
    return bind_library(data_dir)


@pytest.fixture
def recorder() -> StudioRecorder:
    return StudioRecorder()


def studio_app(
    data_dir: Path,
    tmp_path: Path,
    pc: PC,
    recorder: Any,
    *,
    enabled: bool | Callable[[], bool] = False,
    **config: Any,
) -> Any:
    """The app on Record → Prompts in studio mode, its corpus client toward ``pc``."""
    from voice_lab_desktop.app import AppModel
    from voice_lab_desktop.measurement.simulated import SimulatedBackend, SimulatedLevelReader
    from voice_lab_desktop.net import new_http
    from voice_lab_desktop.tokens import MemoryStore

    settings = Config(
        server_url=pc.url, data_dir=data_dir, file=tmp_path / "config.toml", **config
    ).validated()
    return AppModel(
        settings,
        MemoryStore(None),
        http=new_http(),
        output=RecordingOutput(),
        open_url=lambda _url: None,
        measurement_backend=SimulatedBackend(speed=200, block_frames=4800),
        level_reader=SimulatedLevelReader(),
        recorder_factory=lambda: recorder,
        start_screen="prompts",
        corpus_factory=lambda context: client(data_dir, pc, enabled=enabled, tasks=context.tasks),
    )


def open_studio(qtbot: Any, model: Any, tmp_path: Path) -> Any:
    """The main window over ``model`` (a context manager yielding the ``Window``)."""
    return open_window(AppRig(model, model.player._output, [], None, tmp_path))


@pytest.fixture
def studio_window(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> Iterator[Window]:
    """The main window on the prompt studio, once the PC's catalog is pinned and the
    microphone (the stand-in) checked."""
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            qtbot.waitExposed(window.root)
            ready(qtbot, model)
            yield window
    finally:
        model.shutdown()


def ready(qtbot: Any, model: Any) -> None:
    """The studio has a catalog, has read the PC's library, and its microphone answered."""
    studio = model.studio
    qtbot.waitUntil(lambda: studio.phase == "ready" and studio.deviceOk, timeout=10000)
    qtbot.waitUntil(lambda: studio.client.library.latest is not None, timeout=10000)


def wait_phase(qtbot: Any, studio: Any, phase: str) -> None:
    qtbot.waitUntil(lambda: studio.phase == phase, timeout=10000)

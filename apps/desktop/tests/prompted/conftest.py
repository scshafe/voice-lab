"""Record: prompts' fixtures: the fake recorder, a store with recorded takes, and the app
opened on the Prompts screen in its session flow (``prompts_mode = "sessions"``; offscreen;
no device, no server)."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pytest  # noqa: E402
from conftest import AppRig, RecordingOutput, Window, open_window  # noqa: E402
from recorder_fakes import FakeRecorder, write_take_wav  # noqa: E402
from voice_lab_desktop.capture.types import EVIDENCE_NAME, ORIGINAL_NAME, CapturedTake  # noqa: E402
from voice_lab_desktop.config import Config  # noqa: E402
from voice_lab_desktop.prompted import catalog  # noqa: E402
from voice_lab_desktop.prompted.store import PromptedStore, SessionRecord, TakeRecord  # noqa: E402

from cole_voice_lab.canonical import sha256_file  # noqa: E402
from cole_voice_lab.models import utc_now  # noqa: E402


@pytest.fixture
def recorder() -> FakeRecorder:
    return FakeRecorder()


@pytest.fixture
def store(data_dir: Path) -> PromptedStore:
    return PromptedStore(data_dir)


def pilot(
    selected: tuple[str, ...] = ("reference-neutral-01", "expressive-02"),
) -> tuple[catalog.Catalog, tuple[str, ...]]:
    loaded = catalog.load(catalog.DEFAULT_SOURCE, catalog_file=None)
    return loaded, catalog.batch_ids(loaded.suite, selected)


def new_session(
    store: PromptedStore, selected: tuple[str, ...] = ("reference-neutral-01",)
) -> SessionRecord:
    loaded, ids = pilot(selected)
    return store.create(
        catalog_source=loaded.source,
        catalog_label=loaded.label,
        catalog=loaded.suite,
        catalog_sha256=loaded.sha256,
        prompt_ids=ids,
        device="Umik-1  Gain: 18dB [Core Audio]",
    )


def add_take(
    store: PromptedStore,
    session: SessionRecord,
    prompt_id: str,
    *,
    frames: int = 4800,
    keep: bool = True,
    device: str = "Umik-1  Gain: 18dB [Core Audio]",
) -> TakeRecord:
    """A recorded take, as the recorder would have published it."""
    number, take_dir = store.new_take_dir(session, prompt_id)
    write_take_wav(take_dir / ORIGINAL_NAME, frames, hz=180.0 + 20 * number)
    (take_dir / EVIDENCE_NAME).write_text("{}")
    sha, size = sha256_file(take_dir / ORIGINAL_NAME)
    captured = CapturedTake(
        path=take_dir / ORIGINAL_NAME,
        evidence_path=take_dir / EVIDENCE_NAME,
        sha256=sha,
        size_bytes=size,
        frames=frames,
        requested_at=utc_now(),
        device=device,
    )
    record = store.record_take(session, prompt_id, number, captured)
    if keep:
        record = store.decide(session, prompt_id, number, "kept")
    return record


def make_app(
    data_dir: Path, tmp_path: Path, recorder: FakeRecorder, *, tokens: Any = None, **config: Any
) -> Any:
    from voice_lab_desktop.app import AppModel
    from voice_lab_desktop.measurement.simulated import SimulatedBackend, SimulatedLevelReader
    from voice_lab_desktop.net import new_http
    from voice_lab_desktop.tokens import MemoryStore

    # Record → Prompts' session flow (phase 4c): the studio is the default (D-051).
    config.setdefault("prompts_mode", "sessions")
    settings = Config(data_dir=data_dir, file=tmp_path / "config.toml", **config).validated()
    return AppModel(
        settings,
        tokens or MemoryStore(None),
        http=new_http(),
        output=RecordingOutput(),
        open_url=lambda _url: None,
        measurement_backend=SimulatedBackend(speed=200, block_frames=4800),
        level_reader=SimulatedLevelReader(),
        recorder_factory=lambda: recorder,
        start_screen="prompts",
    )


@pytest.fixture
def prompts_window(
    qtbot: Any, data_dir: Path, tmp_path: Path, recorder: FakeRecorder
) -> Iterator[Window]:
    """The main window on Record → Prompts, over the fake recorder."""
    model = make_app(data_dir, tmp_path, recorder)
    output = model.player._output
    try:
        with open_window(AppRig(model, output, [], None, tmp_path)) as window:
            qtbot.waitExposed(window.root)
            window.root.requestActivate()
            qtbot.waitUntil(lambda: model.prompts.deviceOk, timeout=5000)
            yield window
    finally:
        model.shutdown()


def wait_phase(qtbot: Any, model: Any, phase: str) -> None:
    qtbot.waitUntil(lambda: model.prompts.phase == phase, timeout=5000)


Waiter = Callable[[Any, Any, str], None]

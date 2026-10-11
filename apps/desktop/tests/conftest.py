"""Shared fixtures: offscreen Qt, temp homes, the fake Pocket ID and intake, a simulated
UMIK-1, and a real /api/v1 server over synthetic manifests.

Nothing here needs a microphone, a speaker, the network or the real server. The app's
sources and the backend's test fixtures (``tests/home_host_fixtures.py``,
``tests/journey_browser_fixtures.py``) are put on ``sys.path``: scripts/verify runs this
suite with ``python -I``, which ignores ``PYTHONPATH``.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[2]
for _path in (HERE / "measurement", REPOSITORY / "tests", HERE.parent / "src", HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Before any Qt import creates the application.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
# Never pick up the person's real settings or data.
for _var in (
    "VOICE_LAB_DESKTOP_SERVER",
    "VOICE_LAB_DESKTOP_ISSUER",
    "VOICE_LAB_DESKTOP_CLIENT_ID",
    "VOICE_LAB_DESKTOP_CONFIG",
    "VOICE_LAB_DESKTOP_DATA",
):
    os.environ.pop(_var, None)

import pytest  # noqa: E402
from fakes import FakeIntake, FakeIssuer, fake_jwt  # noqa: E402
from voice_lab_desktop.config import Config  # noqa: E402
from voice_lab_desktop.measurement.pin import StateStore  # noqa: E402
from voice_lab_desktop.measurement.simulated import (  # noqa: E402
    UMIK,
    SimulatedBackend,
    SimulatedLevelReader,
)
from voice_lab_desktop.measurement.store import SessionStore  # noqa: E402
from voice_lab_desktop.playback import Clip  # noqa: E402
from voice_lab_desktop.tokens import MemoryStore, Tokens  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """XDG dirs and HOME point into the test's tmp dir: no test touches real settings or data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))


# ---- the measurement capture (voice-journey-capture's fixtures) -----------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def measurement_dir(data_dir: Path) -> Path:
    """Where the measurement capture keeps its sessions, pin and cal file
    (``Config.measurement_dir`` for ``data_dir``)."""
    return data_dir / "measurement"


@pytest.fixture
def intake(tmp_path: Path) -> Iterator[FakeIntake]:
    fake = FakeIntake(tmp_path / "server").start()
    yield fake
    fake.stop()


@pytest.fixture
def issuer(intake: FakeIntake) -> Iterator[FakeIssuer]:
    fake = FakeIssuer(client_id="voice-lab-desktop").start()
    fake.on_access_token = intake.valid_tokens.add  # what the issuer mints, the door accepts
    yield fake
    fake.stop()


@pytest.fixture
def config(intake: FakeIntake, issuer: FakeIssuer, data_dir: Path, tmp_path: Path) -> Config:
    return Config(
        server_url=intake.url,
        issuer=issuer.issuer,
        client_id="voice-lab-desktop",
        data_dir=data_dir,
        file=tmp_path / "config.toml",
    ).validated()


def mint(issuer: FakeIssuer, *, expires_in: float = 3600, refresh: bool = True) -> Tokens:
    """Tokens as if a sign-in had happened (the access token is valid at the fake door)."""
    now = time.time()
    claims = {
        "iss": issuer.issuer,
        "aud": "voice-lab-desktop",
        "sub": "sub-owner",
        "exp": now + expires_in,
    }
    access = fake_jwt(claims)
    issuer.on_access_token(access)
    rt = None
    if refresh:
        rt = "rt-seed"
        issuer.refresh_tokens[rt] = True
    return Tokens(
        access_token=access,
        id_token=fake_jwt({**claims, "email": "owner@example.test"}),
        expires_at=now + expires_in,
        refresh_token=rt,
    )


@pytest.fixture
def signed_in(issuer: FakeIssuer) -> MemoryStore:
    return MemoryStore(mint(issuer))


@pytest.fixture
def backend() -> SimulatedBackend:
    return SimulatedBackend(speed=200, block_frames=4800)


@pytest.fixture
def levels() -> SimulatedLevelReader:
    return SimulatedLevelReader()


@pytest.fixture
def state(measurement_dir: Path) -> StateStore:
    return StateStore(measurement_dir)


@pytest.fixture
def sessions(measurement_dir: Path) -> SessionStore:
    return SessionStore(measurement_dir)


@pytest.fixture
def pinned(state: StateStore, levels: SimulatedLevelReader) -> StateStore:
    state.set_pin(levels.read(UMIK), UMIK.name)
    return state


# ---- the app over a real /api/v1 on synthetic manifests -------------------------------------


@dataclass
class RecordingOutput:
    """The audio device in tests: keeps what would have played; never opens a device."""

    played: list[Clip] = field(default_factory=list)
    frame: int = 0
    on_done: Callable[[], None] | None = None
    running: bool = False

    def start(self, clip: Clip, frame: int, on_done: Callable[[], None]) -> None:
        self.played.append(clip)
        self.frame = frame
        self.on_done = on_done
        self.running = True

    def stop(self) -> None:
        self.running = False

    def finish(self) -> None:
        """The device played the take to its end."""
        assert self.on_done is not None
        self.frame = self.played[-1].samples.shape[0]
        self.running = False
        self.on_done()


def app_tokens() -> Tokens:
    """A sign-in the synthetic server accepts (the backend's test key, the allowed subject)."""
    from synthetic import mint as mint_jwt

    now = time.time()
    access = mint_jwt({"exp": int(now) + 3600})
    return Tokens(
        access_token=access,
        id_token=mint_jwt({"exp": int(now) + 3600, "email": "owner@example.test"}),
        expires_at=now + 3600,
        refresh_token=None,
    )


@dataclass
class AppRig:
    """A signed-in app model over a synthetic server, and what it did outside."""

    model: Any
    output: RecordingOutput
    opened: list[str]
    served: Any
    tmp: Path


@pytest.fixture
def synthetic_root(tmp_path: Path) -> Path:
    from synthetic import data_root

    return data_root(tmp_path / "journey")


@contextmanager
def make_rig(
    tmp_path: Path, journey_config: Any, *, signed_in: bool = True, configured: bool = True
) -> Iterator[AppRig]:
    from synthetic import ISSUER, serve
    from voice_lab_desktop.app import AppModel
    from voice_lab_desktop.net import new_http

    with serve(tmp_path / "server", journey_config) as served:
        output = RecordingOutput()
        opened: list[str] = []
        config = Config(
            server_url=served.url if configured else "",
            issuer=ISSUER if configured else "",
            client_id="voice-lab-desktop",
            data_dir=tmp_path / "app-data",
            file=tmp_path / "app-config.toml",
        ).validated()
        tokens = MemoryStore(app_tokens() if signed_in else None)
        model = AppModel(
            config,
            tokens,
            http=new_http(),
            output=output,
            open_url=opened.append,
            open_browser=opened.append,
        )
        try:
            yield AppRig(model, output, opened, served, tmp_path)
        finally:
            model.shutdown()


@pytest.fixture
def rig(tmp_path: Path, synthetic_root: Path) -> Iterator[AppRig]:
    """The app, signed in, over the synthetic corpus with every gate open."""
    from synthetic import audio_root, config_for

    corpus = audio_root(tmp_path / "journey")
    with make_rig(tmp_path, config_for(synthetic_root, corpus=corpus)) as made:
        yield made


@dataclass
class Window:
    rig: AppRig
    engine: Any
    root: Any

    @property
    def model(self) -> Any:
        return self.rig.model

    def find(self, name: str) -> Any:
        from PySide6.QtCore import QObject

        found = self.root.findChild(QObject, name)
        assert found is not None, f"no object named {name!r}"
        return found

    def find_all(self, name: str) -> list[Any]:
        from PySide6.QtCore import QObject

        return [o for o in self.root.findChildren(QObject) if o.objectName() == name]


@contextmanager
def open_window(rig: AppRig) -> Iterator[Window]:
    from voice_lab_desktop import cli

    engine = cli.load_main_window(rig.model)
    roots = engine.rootObjects()
    assert roots, "Main.qml did not load"
    rig.model.start()
    try:
        yield Window(rig, engine, roots[0])
    finally:
        cli.teardown(engine, lambda: None)


@pytest.fixture
def window(qtbot: Any, rig: AppRig) -> Iterator[Window]:
    """The main window, offscreen, over ``rig``."""
    with open_window(rig) as opened:
        yield opened

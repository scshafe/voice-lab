"""Shared fixtures: offscreen Qt, the fake Pocket ID and intake, a simulated UMIK-1, temp data dirs.

Nothing here needs a microphone, the network or the real server.
"""

from __future__ import annotations

import os
import time

# Before any Qt import creates the application.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
# Never pick up the person's real settings or data.
for _var in ("VOICE_JOURNEY_CAPTURE_SERVER", "VOICE_JOURNEY_CAPTURE_ISSUER", "VOICE_JOURNEY_CAPTURE_CLIENT_ID",
             "VOICE_JOURNEY_CAPTURE_CONFIG", "VOICE_JOURNEY_CAPTURE_DATA"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402

from fakes import FakeIntake, FakeIssuer, fake_jwt  # noqa: E402
from voice_journey_capture.config import Config  # noqa: E402
from voice_journey_capture.pin import StateStore  # noqa: E402
from voice_journey_capture.simulated import UMIK, SimulatedBackend, SimulatedLevelReader  # noqa: E402
from voice_journey_capture.store import SessionStore  # noqa: E402
from voice_journey_capture.tokens import MemoryStore, Tokens  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """XDG dirs and HOME point into the test's tmp dir: no test touches real settings or data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def intake(tmp_path):
    fake = FakeIntake(tmp_path / "server").start()
    yield fake
    fake.stop()


@pytest.fixture
def issuer(intake):
    fake = FakeIssuer().start()
    fake.on_access_token = intake.valid_tokens.add  # what the issuer mints, the door accepts
    yield fake
    fake.stop()


@pytest.fixture
def config(intake, issuer, data_dir, tmp_path):
    return Config(server_url=intake.url, issuer=issuer.issuer, client_id="voice-journey-capture",
                  data_dir=data_dir, file=tmp_path / "config.toml").validated()


def mint(issuer: FakeIssuer, *, expires_in: float = 3600, refresh: bool = True) -> Tokens:
    """Tokens as if a sign-in had happened (the access token is valid at the fake door)."""
    now = time.time()
    claims = {"iss": issuer.issuer, "aud": "voice-journey-capture", "sub": "sub-owner", "exp": now + expires_in}
    access = fake_jwt(claims)
    issuer.on_access_token(access)
    rt = None
    if refresh:
        rt = "rt-seed"
        issuer.refresh_tokens[rt] = True
    return Tokens(access_token=access, id_token=fake_jwt({**claims, "email": "owner@example.test"}), expires_at=now + expires_in, refresh_token=rt)


@pytest.fixture
def signed_in(issuer):
    return MemoryStore(mint(issuer))


@pytest.fixture
def backend():
    return SimulatedBackend(speed=200, block_frames=4800)


@pytest.fixture
def levels():
    return SimulatedLevelReader()


@pytest.fixture
def state(data_dir):
    return StateStore(data_dir)


@pytest.fixture
def sessions(data_dir):
    return SessionStore(data_dir)


@pytest.fixture
def pinned(state, levels):
    state.set_pin(levels.read(UMIK), UMIK.name)
    return state

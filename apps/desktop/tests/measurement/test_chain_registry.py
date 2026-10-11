"""The Measurement screen against the server's chain registry (``GET /api/v1/chains``, #31).

The chain is fixed in ``measurement/chain.py``; the screen reads the registry the server
serves and refuses a new session when it describes the chain differently. A registry it
cannot read (signed out, a server without the route) leaves the fixed chain in use.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import Window, make_rig, open_window
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from synthetic import config_for
from voice_lab_desktop.measurement import chain
from voice_lab_desktop.measurement.pin import StateStore
from voice_lab_desktop.measurement.simulated import UMIK, SimulatedLevelReader

from cole_voice_lab.api.chains import ClientChain, read_chain_registry, registry_view
from cole_voice_lab.journey.paths import packaged_chains


def packaged() -> tuple[ClientChain, ...]:
    projection = read_chain_registry(packaged_chains())
    assert projection is not None
    return registry_view(projection).chains


def registry(path: Path, **umik: Any) -> Path:
    """The packaged registry with the ``umik1`` entry changed (a key set to None is removed)."""
    document = json.loads(packaged_chains().read_text(encoding="utf-8"))
    for entry in document["chains"]:
        if entry["chainId"] == "umik1":
            for key, value in umik.items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_packaged_registry_agrees_with_the_fixed_chain() -> None:
    assert chain.registry_disagreement(packaged()) is None


def test_a_registry_that_states_another_format_or_device_disagrees(tmp_path: Path) -> None:
    def chains(**umik: Any) -> tuple[ClientChain, ...]:
        projection = read_chain_registry(registry(tmp_path / "chains.json", **umik))
        assert projection is not None
        return registry_view(projection).chains

    message = chain.registry_disagreement(chains(sampleRate=44100, usbId="2752:0011"))
    assert message is not None
    assert "sample rate 44100 (this app: 48000)" in message
    assert "USB id '2752:0011' (this app: '2752:0007')" in message
    assert "refused" in message
    # What the registry leaves out contradicts nothing; the USB id compares case-blind.
    assert chain.registry_disagreement(chains(bitDepth=None, usbId="2752:0007".upper())) is None
    without = tuple(entry for entry in packaged() if entry.id != chain.CHAIN_ID)
    assert chain.registry_disagreement(without) == "the server's chain registry has no chain umik1"


@contextmanager
def measuring(qtbot: Any, tmp_path: Path, journey: Any, **rig: Any) -> Iterator[Window]:
    levels = SimulatedLevelReader()
    with make_rig(tmp_path, journey, level_reader=levels, **rig) as made:
        state = StateStore(made.model.context.config.measurement_dir)
        state.set_pin(levels.read(UMIK), UMIK.name)
        with open_window(made) as window:
            qtbot.waitExposed(window.root)
            made.model.navigate("measurement")
            yield window


def test_a_matching_registry_lets_a_session_start(
    qtbot: Any, tmp_path: Path, synthetic_root: Path
) -> None:
    with measuring(qtbot, tmp_path, config_for(synthetic_root)) as window:
        controller = window.model.measurement
        qtbot.waitUntil(lambda: controller.chainState == "agrees", timeout=10000)
        qtbot.waitUntil(lambda: controller.canStart, timeout=10000)
        assert window.find("chainText").property("text") == (
            "Chain umik1 matches the server's chain registry."
        )


def test_a_registry_that_differs_refuses_a_new_session(
    qtbot: Any, tmp_path: Path, synthetic_root: Path
) -> None:
    chains = registry(tmp_path / "chains.json", sampleRate=44100)
    with measuring(qtbot, tmp_path, config_for(synthetic_root, chains=chains)) as window:
        controller = window.model.measurement
        qtbot.waitUntil(lambda: controller.chainState == "differs", timeout=10000)
        qtbot.waitUntil(lambda: controller.deviceOk, timeout=10000)
        assert not controller.canStart
        assert controller.startBlocker.startswith("The server's chain registry describes umik1")
        assert "sample rate 44100" in window.find("chainText").property("text")
        controller.startSession()
        assert controller.phase == "setup"


@pytest.mark.parametrize("signed_in", [True, False])
def test_an_unreadable_registry_leaves_the_fixed_chain_in_use(
    qtbot: Any, tmp_path: Path, synthetic_root: Path, signed_in: bool
) -> None:
    missing = tmp_path / "no-such-chains.json"  # the server answers 503 chain_registry_unavailable
    journey = config_for(synthetic_root, chains=missing)
    with measuring(qtbot, tmp_path, journey, signed_in=signed_in) as window:
        controller = window.model.measurement
        qtbot.waitUntil(lambda: controller.chainState == "unavailable", timeout=10000)
        qtbot.waitUntil(lambda: controller.canStart, timeout=10000)
        text = window.find("chainText").property("text")
        expected = "the chain registry file is missing" if signed_in else "signed out"
        assert expected in text and "this app's chain" in text
        if signed_in:
            # Shown again, the screen asks again (the server may have been fixed meanwhile).
            window.model.navigate("status")
            missing.write_text(packaged_chains().read_text(encoding="utf-8"), encoding="utf-8")
            window.model.navigate("measurement")
            qtbot.waitUntil(lambda: controller.chainState == "agrees", timeout=10000)


def test_a_server_without_the_route_is_said_so(
    qtbot: Any, tmp_path: Path, synthetic_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.api import journey_routes

    routes = dict(journey_routes._V1_ROUTES)
    routes.pop("/api/v1/chains")
    monkeypatch.setattr(journey_routes, "_V1_ROUTES", routes)
    with measuring(qtbot, tmp_path, config_for(synthetic_root)) as window:
        controller = window.model.measurement
        qtbot.waitUntil(lambda: controller.chainState == "unavailable", timeout=10000)
        assert "does not serve the chain registry yet" in controller.chainText
        qtbot.waitUntil(lambda: controller.canStart, timeout=10000)

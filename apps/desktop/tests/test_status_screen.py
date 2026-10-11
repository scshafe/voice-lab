"""Sign-in and status (checklist S-01 to S-05, A-02)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import AppRig, Window, make_rig, open_window
from fakes import FakeIssuer
from synthetic import config_for, mint
from voice_lab_desktop.config import Config, load_config
from voice_lab_desktop.screens.status import client_problem, watch_lines


def browser(url: str) -> None:
    """The person's browser: follows the authorization redirect back to the app."""
    threading.Thread(target=lambda: httpx.get(url, follow_redirects=True), daemon=True).start()


@pytest.fixture
def signed_out_rig(tmp_path: Path, synthetic_root: Path) -> Iterator[AppRig]:
    with make_rig(tmp_path, config_for(synthetic_root), signed_in=False) as made:
        yield made


@pytest.mark.checklist("S-02", "S-03")
def test_status_shows_the_server(qtbot: Any, window: Window) -> None:
    status = window.model.status
    window.model.navigate("status")
    qtbot.waitUntil(lambda: status.state == "loaded", timeout=10000)
    assert status.serverBuild == "synthetic-build"
    assert status.apiVersions == "v1"
    assert status.minimumClientVersion == "0.1.0"
    assert status.clientProblem == ""
    assert (status.playbackEnabled, status.transcriptTextEnabled, status.featureDetailEnabled) == (
        True,
        True,
        True,
    )
    assert status.watchLines == ["Not running (watch_not_enabled)."]
    assert window.find("serverBuild").property("text") == "synthetic-build"
    assert window.find("gatePlayback").property("text") == "Playback: on"
    assert window.find("serverAddress").property("text") == window.rig.served.url


@pytest.mark.checklist("S-02")
def test_an_older_app_is_told_to_update() -> None:
    from cole_voice_lab.api.contracts import ApiMeta

    old = ApiMeta(server_build="b", minimum_client_version="0.2.0")
    assert "older than the server's minimum client version (0.2.0)" in client_problem(old, "0.1.0")
    assert client_problem(old, "0.2.0") == ""
    other = ApiMeta(server_build="b", minimum_client_version="0.1.0", api_versions=("v2",))
    assert "does not serve /api/v1" in client_problem(other)


@pytest.mark.checklist("S-03")
def test_watch_lines_summarize_the_job_runner() -> None:
    from cole_voice_lab.api.journey_contracts import WatchStatus

    state = {
        "lastRun": {"at": "2026-10-11T00:00:00Z", "status": "ok", "newCount": 2},
        "nextDigestAt": "2026-10-12T08:00:00Z",
    }
    lines = watch_lines(WatchStatus(enabled=True, state=state))
    assert lines == [
        "Last run: ok at 2026-10-11T00:00:00Z, 2 new.",
        "Next digest: 2026-10-12T08:00:00Z.",
    ]
    assert watch_lines(WatchStatus(enabled=True, state={})) == ["No run recorded yet."]


@pytest.mark.checklist("S-01")
def test_status_sign_in_and_out(qtbot: Any, signed_out_rig: AppRig, tmp_path: Path) -> None:
    fake = FakeIssuer(client_id="voice-lab-desktop").start()
    fake.access_factory = mint
    try:
        model = signed_out_rig.model
        model.open_browser = browser
        assert model.saveSettings(signed_out_rig.served.url, fake.issuer, "voice-lab-desktop")
        assert not model.signedIn
        with open_window(signed_out_rig) as window:
            assert window.find("accountText").property("text") == "Not signed in."
            model.signIn()
            qtbot.waitUntil(lambda: model.signedIn, timeout=10000)
            assert model.account == "owner@example.test"
            text = window.find("accountText").property("text")
            assert text == "Signed in as owner@example.test"
        params = fake.authorize_params[-1]
        assert params["client_id"] == "voice-lab-desktop"
        assert params["code_challenge_method"] == "S256"
        assert params["redirect_uri"].startswith("http://127.0.0.1:")
        qtbot.waitUntil(lambda: model.status.state == "loaded", timeout=10000)
        model.signOut()
        assert not model.signedIn and model.account == ""
        assert model.session.store.load() is None
    finally:
        fake.stop()


@pytest.mark.checklist("S-01")
def test_sign_in_needs_the_settings(qtbot: Any, tmp_path: Path, synthetic_root: Path) -> None:
    config = config_for(synthetic_root)
    with make_rig(tmp_path, config, signed_in=False, configured=False) as rig:
        toasts: list[tuple[str, str]] = []
        rig.model.toast.connect(lambda text, tone: toasts.append((text, tone)))
        assert rig.model.screen == "status" and not rig.model.configured
        rig.model.signIn()
        assert toasts and toasts[-1][1] == "warn" and "no server is set" in toasts[-1][0]


@pytest.mark.checklist("S-04")
def test_status_shows_the_spool(qtbot: Any, window: Window) -> None:
    window.model.navigate("status")
    assert window.model.status.spoolText == "No local sessions."
    assert window.find("spoolText").property("text") == "No local sessions."


@pytest.mark.checklist("S-05")
def test_status_settings(qtbot: Any, signed_out_rig: AppRig, tmp_path: Path) -> None:
    model = signed_out_rig.model
    toasts: list[tuple[str, str]] = []
    model.toast.connect(lambda text, tone: toasts.append((text, tone)))
    assert not model.saveSettings("ftp://nope", "https://id.example.invalid", "voice-lab-desktop")
    assert toasts[-1][1] == "danger" and "server_url must be https" in toasts[-1][0]
    assert model.saveSettings("https://voice-lab.example.ts.net", "https://id.example.ts.net", "c1")
    saved = load_config(tmp_path / "app-config.toml", env={})
    assert (saved.server_url, saved.issuer, saved.client_id) == (
        "https://voice-lab.example.ts.net",
        "https://id.example.ts.net",
        "c1",
    )
    assert model.server == "https://voice-lab.example.ts.net" and model.clientId == "c1"


@pytest.mark.checklist("S-05")
def test_nothing_in_the_code_names_a_server() -> None:
    config = Config()
    assert (config.server_url, config.issuer) == ("", "")
    assert not config.configured


@pytest.mark.checklist("A-02")
def test_status_offers_the_evidence_report(qtbot: Any, window: Window) -> None:
    window.find("openReport").clicked.emit()
    assert window.rig.opened == [f"{window.rig.served.url}/view/report"]

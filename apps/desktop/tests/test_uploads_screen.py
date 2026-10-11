"""Uploads (checklist U-01 to U-04): the spool, progress, resume, retry and receipts, over
/api/v1/intake with the D-021 ledger (phase 4b), and whether this machine can upload."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import AppRig, RecordingOutput, Window, open_window
from helpers import new_ritual, record_segment, record_session
from home_rig import SERVER_URL, HomeRig, home_rig
from synthetic import ISSUER, mint
from voice_lab_desktop.app import AppModel
from voice_lab_desktop.config import Config
from voice_lab_desktop.net import new_http
from voice_lab_desktop.tokens import MemoryStore, Tokens


def signed_tokens() -> MemoryStore:
    now = time.time()
    access = mint({"exp": int(now) + 3600})
    return MemoryStore(Tokens(access, mint({"email": "owner@example.test"}), now + 3600, None))


@pytest.fixture
def home(tmp_path: Path, data_dir: Path) -> Iterator[HomeRig]:
    with home_rig(tmp_path / "rig", data_dir / "library") as rig:
        yield rig


@pytest.fixture
def uploads_rig(data_dir: Path, tmp_path: Path, home: HomeRig) -> Iterator[AppRig]:
    """The app, signed in, its server the approved home host (a loopback stand-in)."""
    config = Config(
        server_url=SERVER_URL, issuer=ISSUER, data_dir=data_dir, file=tmp_path / "c.toml"
    ).validated()
    output = RecordingOutput()
    opened: list[str] = []
    model = AppModel(
        config, signed_tokens(), http=new_http(), output=output, open_url=opened.append
    )
    model.uploads.client_factory = home.client_factory()
    model.uploads.sleep = home.publish
    try:
        yield AppRig(model, output, opened, home, tmp_path)
    finally:
        model.shutdown()


@pytest.fixture
def uploads_window(qtbot: Any, uploads_rig: AppRig) -> Iterator[Window]:
    with open_window(uploads_rig) as window:
        window.model.navigate("uploads")
        yield window


def states(model: Any) -> dict[str, str]:
    return {row["sessionId"]: row["state"] for row in model.uploads.rows}


@pytest.mark.checklist("U-01")
def test_uploads_lists_sessions(
    qtbot: Any, uploads_window: Window, backend: Any, levels: Any, pinned: Any, sessions: Any
) -> None:
    done = record_session(backend, levels, pinned, sessions, skip_free=True)
    later = datetime.now().astimezone() + timedelta(seconds=5)
    aborted = new_ritual(backend, levels, pinned, sessions, now=lambda: later)
    aborted.begin()
    record_segment(aborted)
    aborted.abort("stopped by you")
    model = uploads_window.model
    model.uploads.refresh()
    assert states(model) == {done.session_id: "pending", aborted.session_id: "not-uploadable"}
    rows = {row["sessionId"]: row for row in model.uploads.rows}
    assert rows[done.session_id]["stateLabel"] == "waiting"
    assert rows[done.session_id]["detail"] == "Not sent yet."
    assert rows[aborted.session_id]["detail"] == "Aborted: kept here, never uploaded."
    assert model.uploads.pendingCount == 1
    assert uploads_window.find("uploadsSummary").property("text") == "1 waiting · 0 refused"
    assert model.status.pendingCount == 1
    assert [e["count"] for e in model.sidebar if e["key"] == "uploads"] == [1]


@pytest.mark.checklist("U-01")
def test_uploads_say_when_this_machine_cannot_upload(
    qtbot: Any, uploads_window: Window, home: HomeRig, data_dir: Path
) -> None:
    uploads = uploads_window.model.uploads
    assert uploads.ready and "pc-home-host" in uploads.readiness
    assert "voice-lab.example.ts.net" in uploads_window.find("uploadsReadiness").property("text")
    # Unbound: nothing can be sent, and the screen says how to approve the home host.
    binding = data_dir / "library" / "provenance" / "home-host" / "binding.json"
    binding.chmod(0o600)
    binding.unlink()
    uploads.activate()
    assert not uploads.ready and "no home-host approval is bound" in uploads.readiness
    assert "voice-lab home-host approve --asset-root" in uploads.readiness
    assert uploads_window.find("uploadsNotReady").property("visible")
    assert not uploads_window.find("uploadAll").property("enabled")


@pytest.mark.checklist("U-02", "U-04")
def test_uploads_progress_and_receipt(
    qtbot: Any, uploads_window: Window, backend: Any, levels: Any, pinned: Any, sessions: Any
) -> None:
    recorded = record_session(backend, levels, pinned, sessions, skip_free=True)
    uploads = uploads_window.model.uploads
    seen: list[tuple[float, str, str]] = []
    uploads.progressChanged.connect(
        lambda: seen.append((uploads.progressValue, uploads.progressLabel, uploads.progressDetail))
    )
    in_flight: list[str] = []
    uploads.changed.connect(lambda: in_flight.append(uploads.uploading))
    uploads.activate()
    uploads.uploadAll()
    accepted = lambda: states(uploads_window.model)[recorded.session_id] == "accepted"  # noqa: E731
    qtbot.waitUntil(lambda: not uploads.busy and accepted(), timeout=30000)
    assert recorded.session_id in in_flight
    shown = [label for _value, label, _detail in seen if label]
    assert any(
        label.startswith(f"Uploading {recorded.session_id}: ") and label.endswith(".wav")
        for label in shown
    )
    assert any(value == 1.0 and detail.endswith(" MB") for value, _label, detail in seen)
    row = uploads.rows[0]
    assert row["stateLabel"] == "accepted" and row["tone"] == "ok"
    assert row["detail"].startswith("Accepted at ") and SERVER_URL in row["detail"]
    assert f"Package vj-{recorded.session_id}, manifest " in row["detail"]


@pytest.mark.checklist("U-03")
def test_uploads_resume_and_retry(
    qtbot: Any,
    uploads_window: Window,
    backend: Any,
    levels: Any,
    pinned: Any,
    sessions: Any,
    home: HomeRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cole_voice_lab.intake_client as intake_client
    from cole_voice_lab.intake_client import HomeHostClient, UploadError

    monkeypatch.setattr(intake_client, "CHUNK_BYTES", 256 * 1024)
    recorded = record_session(backend, levels, pinned, sessions, skip_free=True)
    uploads = uploads_window.model.uploads
    starts: list[int] = []

    class Dropping(HomeHostClient):
        def request(self, method: str, path: str, **kwargs: Any) -> Any:
            if method == "PUT":
                span = dict(kwargs.get("headers") or {})["Content-Range"][6:].split("/")[0]
                starts.append(int(span.split("-")[0]))
                if len(starts) == 2:
                    raise UploadError("the home host is unreachable (ConnectionResetError)")
            return super().request(method, path, **kwargs)

    uploads.client_factory = lambda _a, token: Dropping(
        endpoint="127.0.0.1", resolver=None, bearer=token, loopback_port=home.port
    )
    uploads.activate()
    uploads.uploadAll()
    qtbot.waitUntil(lambda: not uploads.busy and len(starts) == 2, timeout=30000)
    row = uploads.rows[0]
    assert row["state"] == "pending"
    assert "1 attempt(s)" in row["detail"] and "It resumes where it stopped." in row["detail"]
    uploads.client_factory = home.client_factory()
    uploads.uploadAll()
    qtbot.waitUntil(
        lambda: not uploads.busy and uploads.rows[0]["state"] == "accepted", timeout=30000
    )

    # A refused session goes back to waiting on Retry.
    sessions.mark_failed(recorded.session_dir, "the server refused it (test)")
    uploads.refresh()
    assert uploads.rows[0]["state"] == "failed" and uploads.rows[0]["canRetry"]
    assert uploads.failedCount == 1
    uploads.retry(recorded.session_id)
    assert uploads.rows[0]["state"] == "pending"

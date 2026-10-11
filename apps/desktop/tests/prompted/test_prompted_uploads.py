"""Prompted packages in Uploads (D-042): held on this machine until prompted uploads are on;
then sent as ``capture`` through the real ``/api/v1/intake`` with the D-021 ledger, landing
in the PC's ``raw/`` labelled ``portaudio``."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from home_rig import SERVER_URL, HomeRig, home_rig
from synthetic import ISSUER, mint
from voice_lab_desktop.app import AppModel
from voice_lab_desktop.config import Config
from voice_lab_desktop.net import new_http
from voice_lab_desktop.prompted import package
from voice_lab_desktop.prompted import uploads as prompted_uploads
from voice_lab_desktop.prompted.store import PromptedStore
from voice_lab_desktop.tokens import MemoryStore, Tokens

from cole_voice_lab.home_host import HomeHostLedger
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.models import utc_now

from .conftest import add_take, new_session
from .recorder_fakes import FakeRecorder

PROMPTS = ("room-tone-start", "reference-neutral-01", "room-tone-end")


def signed_tokens() -> MemoryStore:
    now = time.time()
    access = mint({"exp": int(now) + 3600})
    return MemoryStore(Tokens(access, mint({"email": "owner@example.test"}), now + 3600, None))


def packaged(store: PromptedStore) -> str:
    session = new_session(store)
    for prompt_id in PROMPTS:
        add_take(store, session, prompt_id, frames=2400)
    session = store.update(session, state="finished", finished_at=utc_now())
    _path, digest = package.build(store, session)
    store.update(session, state="packaged", package_manifest_sha256=digest)
    return session.session_id


def app(data_dir: Path, tmp_path: Path, home: HomeRig, *, prompted_uploads: bool) -> AppModel:
    config = Config(
        server_url=SERVER_URL,
        issuer=ISSUER,
        data_dir=data_dir,
        file=tmp_path / "c.toml",
        prompted_uploads=prompted_uploads,
    ).validated()
    model = AppModel(
        config,
        signed_tokens(),
        http=new_http(),
        open_url=lambda _url: None,
        recorder_factory=lambda: FakeRecorder(),
    )
    model.uploads.client_factory = home.client_factory()
    model.uploads.sleep = home.publish
    return model


@pytest.fixture
def home(tmp_path: Path, data_dir: Path) -> Iterator[HomeRig]:
    with home_rig(tmp_path / "rig", data_dir / "library") as rig:
        yield rig


def rows(model: AppModel) -> dict[str, dict[str, Any]]:
    return {row["sessionId"]: row for row in model.uploads.rows}


def test_prompted_packages_are_held_until_prompted_uploads_are_on(
    qtbot: Any, data_dir: Path, tmp_path: Path, home: HomeRig
) -> None:
    store = PromptedStore(data_dir)
    session_id = packaged(store)
    model = app(data_dir, tmp_path, home, prompted_uploads=False)
    try:
        uploads = model.uploads
        uploads.activate()
        row = rows(model)[session_id]
        assert row["state"] == "held" and row["stateLabel"] == "held here"
        assert "until spike S-1 passes" in row["detail"] and "prompted_uploads" in row["detail"]
        assert uploads.heldCount == 1 and uploads.pendingCount == 0
        # Status counts it apart from what waits for a receipt.
        status = model.status
        assert status.spoolText == "Sessions: 1 prompted, held here until spike S-1 passes."
        assert status.pendingCount == 0
        assert (
            "covering prompted sessions (capture packages), measurement sessions"
            in uploads.readiness
        )
        uploads.uploadAll()
        qtbot.waitUntil(lambda: not uploads.busy, timeout=10000)
        assert home.accepted() == []
        assert list(HomeHostLedger(home.library).entries()) == []
        assert store.upload_state(session_id) == {}
    finally:
        model.shutdown()


def test_a_prompted_package_lands_in_raw_labelled_portaudio(
    qtbot: Any, data_dir: Path, tmp_path: Path, home: HomeRig
) -> None:
    store = PromptedStore(data_dir)
    session_id = packaged(store)
    model = app(data_dir, tmp_path, home, prompted_uploads=True)
    try:
        uploads = model.uploads
        uploads.activate()
        assert rows(model)[session_id]["state"] == "pending" and uploads.pendingCount == 1
        assert model.status.spoolText == "Sessions: 1 waiting for a receipt."
        uploads.uploadAll()
        qtbot.waitUntil(
            lambda: not uploads.busy and rows(model)[session_id]["state"] == "accepted",
            timeout=30000,
        )
        (raw,) = home.accepted()
        manifest = validate_capture_package(raw, allow_intake_receipt=True).manifest
        assert manifest.session_id == session_id
        assert manifest.capture_device.backend == "portaudio"
        state = store.upload_state(session_id)
        assert state["receipt"]["packageSessionId"] == session_id
        assert state["receipt"]["approvalId"] == "pc-home-host"
        assert f"Package {session_id}, manifest " in rows(model)[session_id]["detail"]
        assert model.status.spoolText == "Every local session has been accepted by the server."
        events = [entry.event for entry, _ in HomeHostLedger(home.library).entries()]
        assert events == ["upload-started", "upload-completed"]
        # The package itself is untouched: its receipt lives beside it.
        assert validate_capture_package(store.package_dir(session_id)).manifest == manifest
        assert json.loads(store.upload_file(session_id).read_text())["state"] == "accepted"
    finally:
        model.shutdown()


def test_an_approval_without_capture_holds_prompted_packages(
    qtbot: Any, data_dir: Path, tmp_path: Path
) -> None:
    store = PromptedStore(data_dir)
    session_id = packaged(store)
    with home_rig(tmp_path / "rig", data_dir / "library", classes=("measurement",)) as home:
        model = app(data_dir, tmp_path, home, prompted_uploads=True)
        try:
            uploads = model.uploads
            uploads.activate()
            row = rows(model)[session_id]
            assert row["state"] == "held"
            assert "does not cover prompted sessions (capture packages)" in row["detail"]
            assert uploads.ready  # measurement sessions could still go
            # Straight through the uploader, the class is checked before anything is sent.
            from voice_lab_desktop.measurement import home_host

            with pytest.raises(home_host.HomeHostNotReady, match="does not cover prompted"):
                prompted_uploads.upload_pending(
                    store,
                    library=home.library,
                    server_url=SERVER_URL,
                    bearer=lambda: "token",
                    client_factory=home.client_factory(),
                )
            assert home.accepted() == []
        finally:
            model.shutdown()


def test_refusals_lost_sign_ins_and_interruptions(
    data_dir: Path, home: HomeRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voice_lab_desktop.auth import SignInRequired

    from cole_voice_lab.intake_client import HomeHostClient, UploadError, UploadRefused

    store = PromptedStore(data_dir)
    session_id = packaged(store)
    seen: list[tuple[str, str]] = []

    def send(**changes: Any) -> Any:
        options: dict[str, Any] = {
            "library": home.library,
            "server_url": SERVER_URL,
            "bearer": lambda: "token",
            "client_factory": home.client_factory(),
            "on_session": lambda sid, state: seen.append((sid, state)),
            "sleep": home.publish,
        }
        return prompted_uploads.upload_pending(store, **{**options, **changes})

    # A token the server refuses: sign in again; the package keeps waiting.
    bad = lambda _approval, token: HomeHostClient.loopback_for_tests(home.port, token)  # noqa: E731
    with pytest.raises(SignInRequired, match="sign in again"):
        send(client_factory=bad, bearer=lambda: "not-a-token")
    assert store.upload_state(session_id)["lastError"] == "sign-in required"

    def raising(error: BaseException) -> Any:
        def upload(*_args: Any, **_kwargs: Any) -> Any:
            raise error

        return upload

    monkeypatch.setattr(prompted_uploads, "upload_session", raising(UploadError("unreachable")))
    summary = send()
    assert (summary.pending, summary.last_error) == (1, "unreachable")
    assert store.upload_state(session_id)["attempts"] == 2
    monkeypatch.setattr(
        prompted_uploads, "upload_session", raising(SignInRequired("the sign-in is gone"))
    )
    with pytest.raises(SignInRequired):
        send()
    monkeypatch.setattr(
        prompted_uploads,
        "upload_session",
        raising(UploadRefused("refused_by_intake", "a file differs from its declaration")),
    )
    summary = send()
    assert summary.failed == 1 and "refused_by_intake" in str(summary.last_error)
    assert store.upload_state(session_id)["state"] == "failed"
    assert seen[-1] == (session_id, "failed")
    assert prompted_uploads.pending(store) == []


def test_a_refused_prompted_package_can_be_retried(
    qtbot: Any, data_dir: Path, tmp_path: Path, home: HomeRig
) -> None:
    store = PromptedStore(data_dir)
    session_id = packaged(store)
    store.write_upload_state(session_id, {"state": "failed", "failedAt": "now", "lastError": "x"})
    model = app(data_dir, tmp_path, home, prompted_uploads=True)
    try:
        uploads = model.uploads
        uploads.activate()
        row = rows(model)[session_id]
        assert row["state"] == "failed" and row["canRetry"] and uploads.failedCount == 1
        assert "Refused at now: x" in row["detail"]
        uploads.retry(session_id)
        assert rows(model)[session_id]["state"] == "pending"
        prompted_uploads.record_attempt(store, session_id, "the home host is unreachable")
        uploads.refresh()
        assert (
            "1 attempt(s); the last: the home host is unreachable"
            in (rows(model)[session_id]["detail"])
        )
    finally:
        model.shutdown()

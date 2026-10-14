"""Approve uploads to my PC in the window (D-021): Uploads and Status offer it while nothing
stands in this app's library; one dialog shows what Approve writes (the endpoint, the node,
the classes, the expiry, who approves); pressing Approve writes and binds it and sends what
waits at once; a lookup Tailscale refuses says why, with Close only, and writes nothing; a
second press writes nothing more; who approves comes from the sign-in. The home host is the
loopback stand-in (``home_rig``), Tailscale recorded answers (``tailscale_fakes``)."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import AppRig, RecordingOutput, Window, offline_corpus, open_window
from helpers import record_session
from home_host_fixtures import ENDPOINT, NODE
from home_rig import SERVER_URL, HomeRig, home_rig
from PySide6.QtCore import QPointF, Qt
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from PySide6.QtTest import QTest
from synthetic import ISSUER, mint
from tailscale_fakes import NOT_RUNNING, FakeTailscale, again, completed, status, whois
from voice_lab_desktop import approval as logic
from voice_lab_desktop.app import AppModel
from voice_lab_desktop.config import Config
from voice_lab_desktop.net import new_http
from voice_lab_desktop.tokens import MemoryStore, Tokens

from cole_voice_lab.home_host import HomeHostLedger, load_approval
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout


def signed_tokens() -> MemoryStore:
    """A sign-in the synthetic home host accepts; its ID token names no one."""
    now = time.time()
    access = mint({"exp": int(now) + 3600})
    return MemoryStore(Tokens(access, mint({"email": "owner@example.test"}), now + 3600, None))


def states(model: Any) -> dict[str, str]:
    return {row["sessionId"]: row["state"] for row in model.uploads.rows}


@pytest.fixture
def home(tmp_path: Path) -> Iterator[HomeRig]:
    """The home host; its fixture's approval is bound elsewhere, not in this app's library."""
    with home_rig(tmp_path / "rig", tmp_path / "another-library") as rig:
        yield rig


@pytest.fixture
def tailscale() -> FakeTailscale:
    return FakeTailscale()


@pytest.fixture
def approve_window(
    qtbot: Any, data_dir: Path, tmp_path: Path, home: HomeRig, tailscale: FakeTailscale
) -> Iterator[Window]:
    """The app, signed in, its server the home host, with nothing approved in its library."""
    config = Config(
        server_url=SERVER_URL, issuer=ISSUER, data_dir=data_dir, file=tmp_path / "c.toml"
    ).validated()
    output = RecordingOutput()
    model = AppModel(
        config,
        signed_tokens(),
        http=new_http(),
        output=output,
        open_url=lambda _url: None,
        corpus_factory=offline_corpus,  # Uploads and Status, not the studio
        start_screen="status",
    )
    model.uploads.client_factory = home.client_factory()
    model.uploads.sleep = home.publish
    model.approval.resolver_factory = tailscale.factory
    try:
        with open_window(AppRig(model, output, [], home, tmp_path)) as window:
            qtbot.waitExposed(window.root)
            yield window
    finally:
        model.shutdown()


def click(window: Window, name: str) -> None:
    """A left click in the middle of the visible item named ``name``."""
    (item,) = [found for found in window.items(name) if found.isVisible()]
    assert item.isEnabled(), name
    centre = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window.root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)


def dialog_open(qtbot: Any, window: Window) -> Any:
    dialog = window.find("approveDialog")
    qtbot.waitUntil(lambda: dialog.property("visible") and dialog.property("opacity") == 1)
    return dialog


def press(qtbot: Any, window: Window, button: str) -> None:
    """The dialog is open; press ``button``, and it closes."""
    dialog = dialog_open(qtbot, window)
    window.items(button)[0].clicked.emit()
    qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=10000)
    assert not window.model.approval.dialogOpen


def text(window: Window, name: str) -> str:
    return str(window.items(name)[0].property("text"))


def approval_files(data_dir: Path) -> list[Path]:
    folder = data_dir / "library" / "provenance" / "home-host" / "approvals"
    return sorted(folder.iterdir()) if folder.exists() else []


def toasts(window: Window) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    window.model.toast.connect(lambda message, tone: seen.append((message, tone)))
    return seen


def test_uploads_offer_approve_and_send_what_waits(
    qtbot: Any,
    approve_window: Window,
    backend: Any,
    levels: Any,
    pinned: Any,
    sessions: Any,
    data_dir: Path,
) -> None:
    recorded = record_session(backend, levels, pinned, sessions, skip_free=True)
    model = approve_window.model
    model.navigate("uploads")
    uploads, approval = model.uploads, model.approval
    uploads.refresh()
    assert not uploads.ready and states(model)[recorded.session_id] == "pending"
    assert approval.needed and not approval.expiringSoon
    assert approval.summary == "Uploads need your OK: there is no approval here yet."
    assert approve_window.find("uploadsApproval").property("visible")
    assert text(approve_window, "uploadsApprovalText") == approval.summary
    seen = toasts(approve_window)
    # One press: Tailscale is asked, and the dialog shows exactly what Approve writes.
    click(approve_window, "uploadsApprove")
    dialog_open(qtbot, approve_window)
    assert approval.canApprove and approval.dialogTitle == "Approve uploads to my PC?"
    assert text(approve_window, "approveEndpoint") == ENDPOINT
    assert text(approve_window, "approveNode") == NODE
    assert text(approve_window, "approveClasses").startswith("capture (takes, prompted")
    until = logic.local_date(utc_now() + timedelta(days=365))  # in local time
    assert text(approve_window, "approveExpires") == f"{until} (365 days)"
    assert text(approve_window, "approveBy") == "Cole"  # the sign-in names no one
    assert approve_window.items("approveConfirm")[0].isVisible()
    assert approval_files(data_dir) == []  # nothing is written before Approve
    # Approve: written and bound in this app's library, and the waiting session goes at once.
    press(qtbot, approve_window, "approveConfirm")
    qtbot.waitUntil(lambda: states(model)[recorded.session_id] == "accepted", timeout=30000)
    layout = AssetLayout.validate(data_dir / "library")
    stored, _digest = load_approval(layout, now=utc_now())
    assert (stored.endpoint, stored.node_stable_id, stored.classes) == (
        ENDPOINT,
        NODE,
        ("capture", "measurement"),
    )
    events = [entry.event for entry, _digest in HomeHostLedger(layout).entries()]
    assert events == ["upload-started", "upload-completed"]
    assert uploads.ready and not approval.needed
    qtbot.waitUntil(lambda: not approve_window.find("uploadsApproval").property("visible"))
    assert (
        f"Uploads to {ENDPOINT} are approved until {until} (approval {stored.approval_id}).",
        "ok",
    ) in seen


def test_status_offers_approve_and_says_why_not(
    qtbot: Any, approve_window: Window, tailscale: FakeTailscale, data_dir: Path
) -> None:
    model = approve_window.model
    approval = model.approval
    assert model.screen == "status"
    assert text(approve_window, "approvalSummary") == approval.summary
    assert approval.summary.startswith("Uploads need your OK")
    assert approve_window.find("statusApprove").property("variant") == "primary"
    # Tailscale is not running: the dialog says so, with Close only; nothing is written.
    tailscale.answers[:] = [NOT_RUNNING]
    click(approve_window, "statusApprove")
    dialog_open(qtbot, approve_window)
    assert not approval.canApprove and approval.dialogTitle == "Nothing was approved"
    assert approval.dialogText.startswith("Tailscale is not running on this machine")
    assert not approve_window.items("approveConfirm")[0].isVisible()
    assert not approve_window.items("approveFields")[0].isVisible()
    assert text(approve_window, "approveCancel") == "Close"
    press(qtbot, approve_window, "approveCancel")
    # A node without tag:voice-lab is never approved (Escape closes the dialog too).
    tailscale.answer(completed(status()), completed(whois(Tags=["tag:other"])))
    click(approve_window, "statusApprove")
    dialog = dialog_open(qtbot, approve_window)
    assert "does not carry tag:voice-lab" in approval.dialogText
    dialog.reject()
    qtbot.waitUntil(lambda: not approval.dialogOpen and not dialog.property("visible"))
    assert approval_files(data_dir) == []
    # Cancel on what Approve would write: nothing is written.
    again(tailscale)
    click(approve_window, "statusApprove")
    dialog_open(qtbot, approve_window)
    assert approval.canApprove and text(approve_window, "approveCancel") == "Cancel"
    press(qtbot, approve_window, "approveCancel")
    assert approval_files(data_dir) == [] and approval.needed
    # Approve, then press again: one stands for this endpoint and node, nothing more is
    # written, and a toast says so.
    again(tailscale)
    click(approve_window, "statusApprove")
    press(qtbot, approve_window, "approveConfirm")
    qtbot.waitUntil(lambda: not approval.needed and not approval.busy)
    assert approval.summary.startswith(f"Uploads to {ENDPOINT} are approved until ")
    assert approve_window.find("statusApprove").property("variant") == "secondary"
    seen = toasts(approve_window)
    again(tailscale)
    click(approve_window, "statusApprove")
    qtbot.waitUntil(lambda: bool(seen) and not approval.busy, timeout=10000)
    message, tone = seen[-1]
    assert tone == "info" and message.startswith(f"Uploads to {ENDPOINT} are approved already")
    assert message.endswith("nothing was written.")
    assert not approval.dialogOpen and len(approval_files(data_dir)) == 1


def test_the_approval_model_at_its_edges(
    qtbot: Any, approve_window: Window, tailscale: FakeTailscale, data_dir: Path
) -> None:
    model = approve_window.model
    approval, session = model.approval, model.session
    # Who approves: the sign-in's name when it has one; else Cole (no name, or no JWT).
    now = time.time()
    access = mint({"exp": int(now) + 3600})
    session.adopt(Tokens(access, mint({"name": "Cole Example"}), now + 3600, None))
    assert approval._approver() == "Cole Example"
    session.adopt(Tokens(access, "not-a-jwt", now + 3600, None))
    assert approval._approver() == "Cole"
    # Approve with no dialog open does nothing; a second press while Tailscale is asked waits.
    approval.confirm()
    approval.begin()
    assert approval.busy
    approval.begin()
    dialog_open(qtbot, approve_window)
    assert tailscale.calls == [["status", "--json"], ["whois", "--json", "100.64.0.7"]]
    # Approved elsewhere meanwhile (the CLI, another window): Approve writes nothing more,
    # and what waits is sent now all the same.
    again(tailscale)
    library = data_dir / "library"
    elsewhere = logic.propose(
        library, SERVER_URL, now=utc_now(), resolver_factory=tailscale.factory
    )
    assert logic.confirm(
        elsewhere, server_url=SERVER_URL, library_dir=library, now=utc_now()
    ).written
    seen = toasts(approve_window)
    fired: list[str] = []
    approval.approved.connect(lambda: fired.append("approved"))
    press(qtbot, approve_window, "approveConfirm")
    qtbot.waitUntil(lambda: bool(seen), timeout=10000)
    assert seen[-1][0].endswith("nothing was written.") and len(approval_files(data_dir)) == 1
    assert fired == ["approved"]
    # The library stopped being private between the dialog and Approve: the dialog says why.
    other = data_dir.parent / "other-library"
    model.context.set_config(  # another data dir, the same server
        Config(
            server_url=SERVER_URL, issuer=ISSUER, data_dir=other, file=data_dir.parent / "o.toml"
        ).validated()
    )
    assert approval.needed  # nothing is approved in that library
    again(tailscale)
    approval.begin()
    dialog_open(qtbot, approve_window)
    unsafe = other / "library"
    unsafe.mkdir(parents=True, mode=0o755)
    unsafe.chmod(0o755)
    approve_window.items("approveConfirm")[0].clicked.emit()
    qtbot.waitUntil(lambda: not approval.busy and not approval.canApprove, timeout=10000)
    assert approval.dialogOpen and approval.dialogText.startswith("This app's library cannot")
    press(qtbot, approve_window, "approveCancel")


def test_an_unexpected_failure_closes_approve_and_is_raised(
    qtbot: Any, approve_window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval = approve_window.model.approval
    approval.begin()
    dialog_open(qtbot, approve_window)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a bug")

    monkeypatch.setattr(logic, "confirm", broken)
    with qtbot.captureExceptions() as raised:
        approve_window.items("approveConfirm")[0].clicked.emit()
        qtbot.waitUntil(lambda: not approval.busy, timeout=10000)
    assert [type(error) for _kind, error, _trace in raised] == [RuntimeError]
    # The dialog says something failed, with Close only: no Approve is left that does nothing.
    assert approval.dialogOpen and not approval.canApprove
    assert approval.dialogTitle == "Nothing was approved"
    assert approval.dialogText == "Something failed: RuntimeError."
    assert not approve_window.items("approveConfirm")[0].isVisible()
    press(qtbot, approve_window, "approveCancel")


def test_settings_saved_meanwhile_drop_the_proposal(
    qtbot: Any, approve_window: Window, tailscale: FakeTailscale, data_dir: Path
) -> None:
    """A proposal is for the server and the library it was made for: saved Settings drop one
    being made or shown, and Approve refuses one the settings no longer match."""
    model = approve_window.model
    approval = model.approval
    seen = toasts(approve_window)
    home_server = model.context.config
    other_server = dataclasses.replace(home_server, server_url="https://other-pc.example.ts.net")
    # Saved while Tailscale is asked: its answer is for the old server; no dialog opens.
    approval.begin()
    model.context.set_config(other_server)
    qtbot.waitUntil(lambda: not approval.busy, timeout=10000)
    assert not approval.dialogOpen and seen[-1] == (logic.STALE, "info")
    # Saved while the dialog shows the old server: it closes; nothing is written.
    model.context.set_config(home_server)
    again(tailscale)
    approval.begin()
    dialog = dialog_open(qtbot, approve_window)
    assert approval.endpoint == ENDPOINT
    model.context.set_config(other_server)
    qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=10000)
    assert not approval.dialogOpen and seen[-1] == (logic.STALE, "info")
    # Saved while a refusal shows: it closes too (nothing was proposed, so nothing to say).
    shown = len(seen)
    tailscale.answer(NOT_RUNNING)
    approval.begin()
    dialog = dialog_open(qtbot, approve_window)
    assert not approval.canApprove
    model.context.set_config(other_server)
    qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=10000)
    assert len(seen) == shown
    # The settings moved without a save being heard: Approve refuses, and says why.
    model.context.set_config(home_server)
    again(tailscale)
    approval.begin()
    dialog_open(qtbot, approve_window)
    approval.server = lambda: "https://other-pc.example.ts.net"
    approve_window.items("approveConfirm")[0].clicked.emit()
    qtbot.waitUntil(lambda: not approval.busy and not approval.canApprove, timeout=10000)
    assert approval.dialogOpen and approval.dialogText == logic.STALE
    press(qtbot, approve_window, "approveCancel")
    assert approval_files(data_dir) == []

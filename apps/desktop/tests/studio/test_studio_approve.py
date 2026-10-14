"""The studio says plainly when uploads wait for the home-host approval (D-021): "Uploads need
your OK", with Approve beside it (the app's approval dialog), instead of a take that only
looks queued; Approve writes and binds the approval in this app's library and syncs at once,
so the take goes. Sync now runs a pass while operations wait. Neither works while a take is
being recorded. The PC is the loopback stand-in (``corpus_rig``), Tailscale recorded answers
(``tailscale_fakes``)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from corpus_rig import PC, SERVER_URL
from home_host_fixtures import ENDPOINT, NODE
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from studio_fakes import StudioRecorder
from tailscale_fakes import FakeTailscale

from cole_voice_lab.home_host import load_approval
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout

from .conftest import open_studio, ready, studio_app, wait_phase
from .test_studio_screen import click, drawn


def test_uploads_need_your_ok_then_approve_sends_the_take(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC
) -> None:
    recorder = StudioRecorder(simulated=False)
    model = studio_app(data_dir, tmp_path, pc, recorder, enabled=True)
    tailscale = FakeTailscale()
    model.approval.resolver_factory = tailscale.factory
    # The approval is for the name the loopback PC stands for (as the uploader's, corpus_rig).
    model.approval.server = lambda: SERVER_URL
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            qtbot.waitExposed(window.root)
            ready(qtbot, model)
            studio = model.studio
            # Nothing is approved in this app's library: the line says so, with Approve in
            # Details' place.
            qtbot.waitUntil(lambda: studio.syncAction == "approve", timeout=10000)
            assert (studio.syncText, studio.syncTone) == ("Uploads need your OK", "warn")
            approve = window.find("studioApprove")
            assert approve.property("visible") and approve.property("enabled")
            assert not window.find("studioDetails").property("visible")
            assert not window.find("studioSyncNow").property("visible")
            # While a take is recorded, Approve waits.
            click(window, "studioRecord")
            qtbot.waitUntil(lambda: len(recorder.takes) == 1, timeout=5000)
            recorder.last.advance(12_000)
            wait_phase(qtbot, studio, "recording")
            assert approve.property("visible") and not approve.property("enabled")
            click(window, "studioRecord")
            wait_phase(qtbot, studio, "review")
            click(window, "studioSave")
            wait_phase(qtbot, studio, "ready")
            assert approve.property("enabled")
            # The take and its Save wait, and the line says why (not just "queued").
            qtbot.waitUntil(
                lambda: studio.syncText == "Uploads need your OK · 2 queued", timeout=10000
            )
            sync = window.find("studioSyncText")
            qtbot.waitUntil(lambda: sync.property("text") == studio.syncText, timeout=5000)
            assert not sync.property("truncated")
            calls: list[str] = []
            sync_now = studio.client.sync_now

            def counted() -> None:
                calls.append("sync")
                sync_now()

            studio.client.sync_now = counted  # type: ignore[method-assign]
            # Approve: the dialog shows what is written; pressing Approve writes it, and the
            # app syncs at once, so the take goes.
            click(window, "studioApprove")
            dialog = window.find("approveDialog")
            qtbot.waitUntil(lambda: dialog.property("visible"), timeout=10000)
            assert model.approval.canApprove
            # The studio's keys wait while the dialog is open (R, Space and Return are its).
            screen = window.find("studioScreen")
            assert not screen.property("keysEnabled")
            assert window.items("approveEndpoint")[0].property("text") == ENDPOINT
            assert window.items("approveNode")[0].property("text") == NODE
            window.items("approveConfirm")[0].clicked.emit()
            qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=10000)
            assert calls == ["sync"]
            qtbot.waitUntil(lambda: screen.property("keysEnabled"), timeout=5000)
            qtbot.waitUntil(lambda: studio.client.status().sent == 2, timeout=30000)
            qtbot.waitUntil(lambda: studio.syncText == "Up to date · 2 sent", timeout=30000)
            assert studio.syncAction == "" and not approve.property("visible")
            stored, _digest = load_approval(
                AssetLayout.validate(data_dir / "library"), now=utc_now()
            )
            assert (stored.endpoint, stored.node_stable_id) == (ENDPOINT, NODE)
            # Sync now, beside what waits for the PC: a pass now, through the corpus client;
            # it too waits while a take is recorded.
            waiting = dataclasses.replace(studio.client.status(), offline=True, queued=1)
            studio.client.status = lambda **_: waiting  # type: ignore[method-assign]
            studio._client_changed()
            qtbot.waitUntil(lambda: window.find("studioSyncNow").property("visible"))
            drawn(qtbot, window)  # laid out beside the line
            click(window, "studioSyncNow")
            assert calls == ["sync", "sync"]
            click(window, "studioRecord")
            qtbot.waitUntil(lambda: len(recorder.takes) == 2, timeout=5000)
            recorder.last.advance(4_800)
            wait_phase(qtbot, studio, "recording")
            assert not window.find("studioSyncNow").property("enabled")
            click(window, "studioRecord")
            wait_phase(qtbot, studio, "review")
    finally:
        model.shutdown()

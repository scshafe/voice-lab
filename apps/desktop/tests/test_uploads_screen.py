"""Uploads (checklist U-01 to U-04): the spool, progress, resume, retry and receipts."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import AppRig, RecordingOutput, Window, open_window
from helpers import new_ritual, record_segment, record_session
from voice_lab_desktop.app import AppModel
from voice_lab_desktop.net import new_http


@pytest.fixture
def uploads_rig(config: Any, signed_in: Any, intake: Any, tmp_path: Any) -> Iterator[AppRig]:
    """The app against the fake intake (voice-journey-capture's protocol, 4a's transport)."""
    output = RecordingOutput()
    opened: list[str] = []
    model = AppModel(config, signed_in, http=new_http(), output=output, open_url=opened.append)
    try:
        yield AppRig(model, output, opened, intake, tmp_path)
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
    uploads.uploadAll()
    accepted = lambda: states(uploads_window.model)[recorded.session_id] == "accepted"  # noqa: E731
    qtbot.waitUntil(lambda: not uploads.busy and accepted(), timeout=20000)
    assert recorded.session_id in in_flight
    files = [entry["name"] for entry in recorded.manifest["files"]]
    shown = [label for _value, label, _detail in seen if label]
    assert f"Uploading {recorded.session_id}: {files[0]}" in shown
    assert any(value == 1.0 and detail.endswith(" MB") for value, _label, detail in seen)
    row = uploads.rows[0]
    assert row["stateLabel"] == "accepted" and row["tone"] == "ok"
    assert (
        row["detail"].startswith("Accepted at ") and uploads_window.rig.served.url in row["detail"]
    )


@pytest.mark.checklist("U-03")
def test_uploads_resume_and_retry(
    qtbot: Any,
    uploads_window: Window,
    backend: Any,
    levels: Any,
    pinned: Any,
    sessions: Any,
    intake: Any,
) -> None:
    recorded = record_session(backend, levels, pinned, sessions, skip_free=True)
    uploads = uploads_window.model.uploads
    uploads.chunk_bytes = 256 * 1024  # several PUTs a file, so one can break in the middle
    intake.drop_after, intake.drop_min_start = 5000, 1
    uploads.uploadAll()
    qtbot.waitUntil(lambda: not uploads.busy, timeout=20000)
    row = uploads.rows[0]
    assert row["state"] == "pending"
    assert "1 attempt(s)" in row["detail"] and "It resumes where it stopped." in row["detail"]
    puts_before = [r for r in intake.requests if r.method == "PUT"]
    uploads.uploadAll()
    qtbot.waitUntil(lambda: not uploads.busy, timeout=20000)
    assert uploads.rows[0]["state"] == "accepted"
    resumed = [r for r in intake.requests if r.method == "PUT"][len(puts_before) :]
    start = int(resumed[0].headers["content-range"].split()[1].split("-")[0])
    assert start == 256 * 1024 + 5000  # from the server's receivedBytes, not from zero

    # A refused session goes back to waiting on Retry.
    sessions.mark_failed(recorded.session_dir, "the server refused it (test)")
    uploads.refresh()
    assert uploads.rows[0]["state"] == "failed" and uploads.rows[0]["canRetry"]
    assert uploads.failedCount == 1
    uploads.retry(recorded.session_id)
    assert uploads.rows[0]["state"] == "pending"

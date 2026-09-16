"""Discovery publishes coverage regression before an actual child inspects history."""

from __future__ import annotations

import os
import select
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from test_recording_discovery_publication import error
from test_recording_discovery_publication import request as discovery_request
from test_recording_navigation import NavigationRig
from test_recording_navigation import nav as nav
from test_recording_service import await_responses, build_identity, prepared, request

from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import RecordingHistoryPage, SaveEvent
from cole_voice_lab.recording_discovery import DiscoveryPublisher, RecordingDiscoveryCoordinator
from cole_voice_lab.recording_discovery_contracts import DiscoveryBegin
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_service import RecordingServiceSession, RecordingWorkspaceBackend


@pytest.mark.parametrize("unknown", [False, True])
def test_begin_notification_observes_committed_state_without_holding_index_lock(
    tmp_path: Path, unknown: bool
) -> None:
    layout = AssetLayout.initialize(tmp_path / "assets")
    with (
        RecordingIndex(layout) as index,
        tempfile.TemporaryFile(dir=layout.directory("tmp")) as out,
    ):
        command = discovery_request(index)
        index.set_completeness(command.catalog_sha256, "complete")
        if unknown:
            index.mark_unknown(command.catalog_sha256, "preserved-source", error())
        before = index.revision
        notifications = []

        def progress(group_id: str) -> None:
            assert group_id == command.group_id
            assert not index.connection.in_transaction
            with ThreadPoolExecutor() as executor:
                # An independent reader must acquire the lock before notification returns.
                page = executor.submit(index.history, command.catalog_sha256).result(timeout=1)
            notifications.append(page)

        publisher = DiscoveryPublisher(index, command, out.fileno(), progress=progress)
        assert publisher.apply(DiscoveryBegin())
        assert len(notifications) == 1
        assert notifications[0].completeness == ("unknown" if unknown else "building")
        assert notifications[0].index_revision > before
        assert notifications[0].error_count == int(unknown)


def test_wire_receives_building_before_held_child_scan_and_plain_save_stays_durable(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    take = nav.capture()
    digest = take.reservation.group.catalog_sha256
    assert nav.index.history(digest).completeness == "complete"
    entered_read, entered_write = os.pipe()
    release_read, release_write = os.pipe()
    original_popen = subprocess.Popen
    original_schedule = RecordingDiscoveryCoordinator.schedule
    child = Path(__file__).with_name("native_discovery_barrier_fixture.py")

    def launch(arguments: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert arguments[1:3] == ["-m", "cole_voice_lab.recording_discovery"]
        command = [
            arguments[0],
            "-I",
            "-B",
            str(child),
            "--test-entered-fd",
            str(entered_write),
            "--test-release-fd",
            str(release_read),
            *arguments[3:],
        ]
        kwargs["pass_fds"] = (*kwargs["pass_fds"], entered_write, release_read)
        return original_popen(command, **kwargs)

    monkeypatch.setattr("cole_voice_lab.recording_discovery.subprocess.Popen", launch)
    # Bootstrap the real publisher/handshake before releasing the normal startup schedule.
    monkeypatch.setattr(RecordingDiscoveryCoordinator, "schedule", lambda *_: None)
    backend = RecordingWorkspaceBackend(
        nav.rig.layout, "synthetic-subject", digest, store=nav.rig.store, index=nav.index
    )
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=backend,
    )
    assert backend.discovery is not None
    try:
        prepared(session)
        monkeypatch.setattr(RecordingDiscoveryCoordinator, "schedule", original_schedule)
        backend.discovery.schedule(nav.cursor.group_id)
        assert select.select([entered_read], [], [], 2)[0], "child did not reach held history scan"
        assert os.read(entered_read, 1) == b"1"
        notification = await_responses(session)[0]
        assert notification.error is None and isinstance(notification.data, RecordingHistoryPage)
        assert notification.data.completeness == "building"
        assert notification.meta.request_id.startswith("history-")
        assert backend.discovery._worker.is_alive()

        selection = nav.cursor.selection
        advance = nav.rig.command(take, "save-next-during-scan", selection=selection, advance=True)
        session.submit(request(advance, request_id="save-next", take_id=take.reservation.take_id))
        rejected = await_responses(session)[0]
        assert rejected.error is not None and rejected.error.code == "RECORDING.HISTORY_UNKNOWN"
        assert "complete catalog" in rejected.error.message
        assert nav.rig.store.get_take(take.reservation.take_id).decision == "draft"

        plain = nav.rig.command(take, "save-during-scan", selection=selection, advance=False)
        session.submit(request(plain, request_id="save", take_id=take.reservation.take_id))
        saved = await_responses(session)[0]
        assert saved.error is None and isinstance(saved.data, SaveEvent)
        assert saved.data.take.decision == "kept" and not saved.data.operation.advance
        assert (
            saved.data.cursor is not None
            and saved.data.cursor.selection.prompt_id == selection.prompt_id
        )
        assert nav.index.history(digest).completeness == "building"
        assert nav.rig.store.get_take(take.reservation.take_id).decision == "kept"

        os.write(release_write, b"1")
        # The Save changed native evidence during that scan, so it cannot certify
        # Complete. Its prompt deferred retry rescans and certifies stable evidence.
        deferred = await_responses(session)[0]
        assert deferred.error is None and isinstance(deferred.data, RecordingHistoryPage)
        assert deferred.data.completeness == "building"
        assert select.select([entered_read], [], [], 2)[0], "deferred retry did not rescan"
        assert os.read(entered_read, 1) == b"1"
        retry = await_responses(session)[0]
        assert isinstance(retry.data, RecordingHistoryPage)
        assert retry.data.completeness == "building"
        os.write(release_write, b"1")
        finished = await_responses(session)[0]
        assert finished.error is None and isinstance(finished.data, RecordingHistoryPage)
        assert finished.data.completeness == "complete"
        assert finished.data.index_revision > notification.data.index_revision
    finally:
        os.write(release_write, b"1")
        backend.discovery.close()
        session.close()
        for descriptor in (entered_read, entered_write, release_read, release_write):
            os.close(descriptor)

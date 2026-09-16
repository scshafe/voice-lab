"""Parent package publication must never hold up the ordinary interactive service lane."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from test_recording_packages import analyzer_build as analyzer_build
from test_recording_packages import full_selection
from test_recording_packages import package_rig as package_rig
from test_recording_service import await_responses, build_identity, prepared, request

from cole_voice_lab.recording_contracts import (
    PackageEvent,
    PackagePreparationEvent,
    RecordingHistoryPage,
    SnapshotCommand,
)
from cole_voice_lab.recording_package_jobs import (
    PackageWorkerRequest,
    RecordingPackageQueue,
    prepare_worker_request,
)
from cole_voice_lab.recording_service import RecordingServiceSession, RecordingTakesBackend


def test_held_parent_publication_allows_second_prepare_and_ordinary_service_command(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import recording_analysis

    rig, analysis, packages = package_rig
    takes, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    backend = RecordingTakesBackend(rig.store, rig.group_id)
    backend.package_queue = queue
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=backend,
    )
    entered = threading.Event()
    release = threading.Event()
    original_project = packages._project

    def held(package: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(10), "test did not release package publication"
        original_project(package, **kwargs)

    try:
        prepared(session)
        queue.submit(command)
        claimed = queue.claim()
        assert claimed is not None
        job, intent = claimed
        monkeypatch.setattr(recording_analysis, "analysis_build_identity", lambda: analysis.build)
        reply = prepare_worker_request(
            rig.layout,
            PackageWorkerRequest(generation="held-parent-publication", intent=intent, attempt=1),
        )
        assert reply.package is not None and reply.error is None
        monkeypatch.setattr(packages, "_project", held)
        with ThreadPoolExecutor(max_workers=1) as executor:
            publication = executor.submit(queue.complete, job, reply)
            try:
                assert entered.wait(2)
                assert not publication.done()
                assert not queue._terminal_path(command.operation_id).exists()
                assert all(
                    rig.store.get_take(take.reservation.take_id).export == "unprepared"
                    for take in takes
                )
                second = command.model_copy(update={"operation_id": "prepare-second"})
                assert session.submit(request(second, request_id="second-prepare")) is None
                assert (
                    session.submit(request(SnapshotCommand(), request_id="ordinary-after-prepare"))
                    is None
                )
                responses = await_responses(session, 2)
                assert [response.meta.request_id for response in responses] == [
                    "second-prepare",
                    "ordinary-after-prepare",
                ]
                assert all(response.error is None for response in responses)
                assert isinstance(responses[0].data, PackagePreparationEvent)
                assert responses[0].data.state == "queued"
                assert isinstance(responses[1].data, RecordingHistoryPage)
                assert not publication.done()
                assert not queue._terminal_path(command.operation_id).exists()
            finally:
                release.set()
            assert isinstance(publication.result(timeout=5), PackageEvent)
            assert queue._terminal_path(command.operation_id).exists()
            assert all(
                rig.store.get_take(take.reservation.take_id).export == "prepared" for take in takes
            )
    finally:
        release.set()
        session.close()
        queue.close()


def test_parent_alias_publication_retains_only_one_catalog_bearing_take_and_replays_partial_work(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import recording_analysis

    rig, analysis, packages = package_rig
    takes, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    try:
        queue.submit(command)
        claimed = queue.claim()
        assert claimed is not None
        job, intent = claimed
        monkeypatch.setattr(recording_analysis, "analysis_build_identity", lambda: analysis.build)
        reply = prepare_worker_request(
            rig.layout,
            PackageWorkerRequest(generation="stream-parent-publication", intent=intent, attempt=1),
        )
        assert reply.package is not None and reply.error is None
        assert packages.index is analysis.index
        register = analysis.index.register_package_aliases
        seen: list[str] = []

        def bounded(current: Any, aliases: Any) -> int:
            # Three independent takes distinguish streaming from a library-sized
            # collection without constructing or retaining a large audio fixture.
            assert len(current) == 1, "parent retained multiple catalog-bearing take models"
            assert len(aliases) == 2
            take_id = current[0].reservation.take_id
            assert {alias[0] for alias in aliases} == {take_id}
            seen.append(take_id)
            if len(seen) == 2:
                raise OSError("synthetic partial parent alias publication interruption")
            return register(current, aliases)

        monkeypatch.setattr(analysis.index, "register_package_aliases", bounded)
        with pytest.raises(OSError, match="partial parent alias"):
            queue.complete(job, reply)
        assert not queue._terminal_path(command.operation_id).exists()
        assert queue._head().jobs == (job,)
        first = rig.store.get_take(seen[0])
        assert first.export == "prepared"
        monkeypatch.setattr(analysis.index, "register_package_aliases", register)
        assert isinstance(queue.complete(job, reply), PackageEvent)
        assert rig.store.get_take(first.reservation.take_id) == first
        assert all(
            rig.store.get_take(take.reservation.take_id).export == "prepared" for take in takes
        )
        assert queue._head().jobs == ()
        assert queue._terminal_path(command.operation_id).exists()
    finally:
        queue.close()

"""Parent package publication streams one catalog-bearing take and replays partial work."""

from __future__ import annotations

from typing import Any

import pytest
from test_recording_packages import analyzer_build as analyzer_build
from test_recording_packages import full_selection
from test_recording_packages import package_rig as package_rig

from cole_voice_lab.recording_contracts import PackageEvent
from cole_voice_lab.recording_package_jobs import (
    PackageWorkerRequest,
    RecordingPackageQueue,
    prepare_worker_request,
)


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

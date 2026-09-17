from __future__ import annotations

import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_recording_packages import (
    analyzer_build as analyzer_build,
)
from test_recording_packages import (
    full_selection,
)
from test_recording_packages import (
    package_rig as package_rig,
)

from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.models import utc_now
from cole_voice_lab.recording_contracts import PackageEvent
from cole_voice_lab.recording_package_jobs import (
    MAX_PACKAGE_ATTEMPTS,
    MAX_PACKAGE_BACKLOG,
    PackageWorkerReply,
    PackageWorkerRequest,
    RecordingPackageQueue,
    RecordingPackageQueueHead,
    RecordingPackageSupervisor,
    decode_package_message,
    prepare_worker_request,
)
from cole_voice_lab.recording_packages import RecordingPackageError


def wait_until(predicate: Any, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("synthetic package process deadline exceeded")
        time.sleep(0.01)


def test_queue_metadata_submit_replay_bounded_backlog_and_exclusive_owner(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, analysis, packages = package_rig
    _, command = full_selection(rig, analysis)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("queue metadata attempted PCM work")

    monkeypatch.setattr(packages, "prepare", forbidden)
    monkeypatch.setattr(packages, "plan", forbidden)
    queue = RecordingPackageQueue(packages)
    try:
        with pytest.raises(BlockingIOError):
            RecordingPackageQueue(packages)
        initial = queue.submit(command)
        assert initial.state == "queued" and queue.submit(command) == initial
        for number in range(1, MAX_PACKAGE_BACKLOG):
            queue.submit(command.model_copy(update={"operation_id": f"prepare-{number}"}))
        with pytest.raises(RecordingPackageError, match="full"):
            queue.submit(command.model_copy(update={"operation_id": "prepare-overflow"}))
        assert len(queue._head().jobs) == MAX_PACKAGE_BACKLOG
        assert len(canonical_json_bytes(queue._head())) < 16384
    finally:
        queue.close()
    with pytest.raises(RecordingPackageError, match="closed"):
        queue.claim()


def test_queue_worker_retry_and_restart_is_bounded_and_durable(package_rig: Any) -> None:
    rig, analysis, packages = package_rig
    _, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    queue.submit(command)
    job, _ = queue.claim()
    queue.close()
    queue = RecordingPackageQueue(packages)
    try:
        queue.recover()
        assert queue._head().jobs[0].state == "queued"
        for attempt in range(2, MAX_PACKAGE_ATTEMPTS + 1):
            job, _ = queue.claim()
            assert job.attempt == attempt
            event = queue.worker_failed(job)
        assert event.state == "failed" and event.error.retryable
        assert queue.claim() is None and queue.submit(command) == event
        assert len(queue.page(limit=1)) == 1
        assert queue.page(after_operation_id=command.operation_id) == ()
        with pytest.raises(ValueError):
            queue.page(limit=201)
        with pytest.raises(SecurityError, match="stale"):
            queue.worker_failed(job)
    finally:
        queue.close()


def test_direct_worker_strict_result_and_terminal_pointer_recovery(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, analysis, packages = package_rig
    takes, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    try:
        queue.submit(command)
        job, intent = queue.claim()
        request = PackageWorkerRequest(
            generation="synthetic-worker", intent=intent, attempt=job.attempt
        )
        assert decode_package_message(canonical_json_bytes(request)) == request
        result = prepare_worker_request(rig.layout, request)
        assert isinstance(result, PackageWorkerReply) and result.error is None
        assert all(rig.store.get_take(t.reservation.take_id).export == "unprepared" for t in takes)
        with pytest.raises(SecurityError, match="stale"):
            queue.complete(job, result.model_copy(update={"attempt": job.attempt + 1}))
        original_write = queue.store._write

        def fail_head(path: Path, payload: Any) -> None:
            if path == queue._head_path:
                raise OSError("synthetic queue head interruption")
            original_write(path, payload)

        monkeypatch.setattr(queue.store, "_write", fail_head)
        with pytest.raises(OSError):
            queue.complete(job, result)
        assert all(rig.store.get_take(t.reservation.take_id).export == "prepared" for t in takes)
        monkeypatch.setattr(queue.store, "_write", original_write)
        queue.recover()
        assert queue.claim() is None
        assert isinstance(queue.submit(command), PackageEvent)
        with pytest.raises(ValidationError):
            decode_package_message(
                canonical_json_bytes(request.model_copy(update={"kind": "unknown"}))
            )
        with pytest.raises(RecordingPackageError, match="bound"):
            decode_package_message(b"x" * (1024 * 1024 + 1))
        with pytest.raises(ValidationError):
            RecordingPackageQueueHead(jobs=(job, job))
    finally:
        queue.close()


def test_worker_publishes_only_package_evidence_until_same_owner_completes(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import recording_analysis, recording_index
    from cole_voice_lab.recording_packages import RecordingPackageStore
    from cole_voice_lab.recording_takes import RecordingTakeStore

    rig, analysis, packages = package_rig
    takes, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    try:
        queue.submit(command)
        job, intent = queue.claim()
        revision = rig.store.state_revision

        def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("package producer acquired service-owned publication authority")

        with monkeypatch.context() as worker_patch:
            # This in-process boundary test has an explicitly fixed worker build;
            # process identity validation remains covered by the frozen real-worker suite.
            worker_patch.setattr(
                recording_analysis, "analysis_build_identity", lambda: analysis.build
            )
            worker_patch.setattr(recording_index, "RecordingIndex", forbidden)
            worker_patch.setattr(RecordingTakeStore, "_repair_pending", forbidden)
            worker_patch.setattr(RecordingPackageStore, "_project", forbidden)
            reply = prepare_worker_request(
                rig.layout,
                PackageWorkerRequest(generation="owned-publication", intent=intent, attempt=1),
            )
        assert reply.package is not None and reply.error is None
        assert rig.store.state_revision == revision
        assert all(rig.store.get_take(t.reservation.take_id).export == "unprepared" for t in takes)
        assert queue._head().jobs == (job,)
        event = queue.complete(job, reply)
        assert isinstance(event, PackageEvent)
        assert all(rig.store.get_take(t.reservation.take_id).export == "prepared" for t in takes)
        assert packages.index is analysis.index
        assert queue.page()[0].state == "prepared"
    finally:
        queue.close()


def test_parent_projection_failure_keeps_package_job_running_and_replayable(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import recording_analysis

    rig, analysis, packages = package_rig
    takes, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    try:
        queue.submit(command)
        job, intent = queue.claim()
        monkeypatch.setattr(recording_analysis, "analysis_build_identity", lambda: analysis.build)
        reply = prepare_worker_request(
            rig.layout,
            PackageWorkerRequest(generation="projection-retry", intent=intent, attempt=1),
        )
        assert reply.package is not None
        project = packages._project

        def fail(_package: Any, **_kwargs: Any) -> None:
            raise OSError("synthetic parent projection interruption")

        monkeypatch.setattr(packages, "_project", fail)
        with pytest.raises(OSError, match="projection interruption"):
            queue.complete(job, reply)
        assert queue._head().jobs == (job,) and queue.page() == ()
        assert all(rig.store.get_take(t.reservation.take_id).export == "unprepared" for t in takes)
        monkeypatch.setattr(packages, "_project", project)
        assert isinstance(queue.complete(job, reply), PackageEvent)
        assert queue._head().jobs == ()
    finally:
        queue.close()


def test_completion_has_one_publisher_and_cancellation_remains_replayable(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import recording_analysis
    from cole_voice_lab.recording_takes import RecordingMetadataBusyError

    rig, analysis, packages = package_rig
    _, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    try:
        queue.submit(command)
        job, intent = queue.claim()
        monkeypatch.setattr(recording_analysis, "analysis_build_identity", lambda: analysis.build)
        reply = prepare_worker_request(
            rig.layout,
            PackageWorkerRequest(generation="one-publisher", intent=intent, attempt=1),
        )
        assert reply.package is not None
        project = packages._project

        def held(package: Any, **kwargs: Any) -> None:
            entered.set()
            assert release.wait(3)
            project(package, **kwargs)

        monkeypatch.setattr(packages, "_project", held)
        with ThreadPoolExecutor(max_workers=1) as pool:
            completion = pool.submit(queue.complete, job, reply, cancelled=cancelled.is_set)
            try:
                assert entered.wait(2)
                with pytest.raises(RecordingMetadataBusyError, match="already being published"):
                    queue.complete(job, reply)
                assert queue._head().jobs == (job,) and queue.page() == ()
                cancelled.set()
            finally:
                release.set()
            with pytest.raises(RecordingPackageError, match="stopped"):
                completion.result(timeout=3)
        assert queue._head().jobs == (job,) and queue.page() == ()
        monkeypatch.setattr(packages, "_project", project)
        assert isinstance(queue.complete(job, reply), PackageEvent)
        with pytest.raises(SecurityError, match="stale"):
            queue.complete(job, reply)
        assert queue._head().jobs == ()
    finally:
        release.set()
        queue.close()


def test_actual_worker_isolated_copy_and_kill_restart_keep_metadata_reachable(
    package_rig: Any,
) -> None:
    rig, analysis, packages = package_rig
    _, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    events: list[Any] = []
    reader, writer = os.pipe()
    supervisor = RecordingPackageSupervisor(
        queue, on_event=lambda op, event: events.append(event), block_fd=reader
    )
    try:
        queue.submit(command)
        supervisor.start()
        wait_until(lambda: supervisor.worker_pid is not None)
        first_pid = supervisor.worker_pid
        assert first_pid is not None and first_pid != os.getpid()
        # The blocked child's first preparation cannot hold TakeStore's metadata lock.
        started = time.monotonic()
        rig.now = utc_now() - timedelta(seconds=5)
        rig.store.clock = lambda: rig.now
        reservation = rig.reserve("reserve-while-package-blocked", "synthetic-0001")
        take = rig.seal(reservation, "seal-while-package-blocked")
        rig.store.clock = utc_now
        saved = rig.store.save(rig.command(take, "save-while-package-blocked"))
        elapsed = time.monotonic() - started
        assert saved.take.decision == "kept" and elapsed < 2
        os.kill(first_pid, signal.SIGKILL)
        wait_until(lambda: supervisor.worker_pid is not None and supervisor.worker_pid != first_pid)
        os.write(writer, b"1")
        wait_until(lambda: any(isinstance(event, PackageEvent) for event in events), timeout=30)
        assert supervisor.fatal_error is None
        assert queue.page()[0].attempt == 2
        assert isinstance(queue.submit(command), PackageEvent)
    finally:
        supervisor.close()
        queue.close()
        os.close(reader)
        os.close(writer)


def test_supervisor_stop_preserves_running_job_for_new_owner(package_rig: Any) -> None:
    rig, analysis, packages = package_rig
    _, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    reader, writer = os.pipe()
    supervisor = RecordingPackageSupervisor(queue, block_fd=reader)
    try:
        queue.submit(command)
        supervisor.start()
        wait_until(lambda: supervisor.worker_pid is not None)
        supervisor.close()
        assert queue._head().jobs[0].state == "running"
        queue.close()
        queue = RecordingPackageQueue(packages)
        queue.recover()
        assert queue._head().jobs[0].state == "queued"
    finally:
        supervisor.close()
        queue.close()
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize(
    "fault", ["none", "bad-build", "copy-error", "duplicate", "partial-eof", "parent-eof"]
)
def test_worker_main_strict_framing_and_parent_eof_with_real_pipes(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import queue as threaded_queue
    import select
    import threading
    from types import SimpleNamespace

    from cole_voice_lab import recording_package_jobs as module
    from cole_voice_lab.recording_contracts import recording_frame_bytes
    from cole_voice_lab.recording_service import RecordingFrameDecoder

    rig, analysis, packages = package_rig
    _, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    intent = queue._intent(command)
    queue.close()
    if fault == "bad-build":
        intent = intent.model_copy(
            update={
                "analyzer_build": intent.analyzer_build.model_copy(
                    update={"source_sha256": "f" * 64}
                )
            }
        )
    request = PackageWorkerRequest(generation="synthetic-pipe", intent=intent, attempt=1)
    parent_read, worker_write = os.pipe()
    worker_read, parent_write = os.pipe()
    barrier_read, barrier_write = os.pipe()
    worker_input = os.fdopen(worker_read, "rb", buffering=0)
    worker_output = os.fdopen(worker_write, "wb", buffering=0)
    monkeypatch.setattr(module, "sys", SimpleNamespace(stdin=worker_input, stdout=worker_output))
    if fault == "copy-error":

        def fail(*args: Any) -> Any:
            raise OSError("synthetic worker failure")

        monkeypatch.setattr(module, "prepare_worker_request", fail)
    stopped: threaded_queue.Queue[Any] = threaded_queue.Queue(1)

    def run() -> None:
        try:
            stopped.put_nowait(
                module._worker_main(rig.layout, barrier_read if fault == "parent-eof" else None)
            )
        except BaseException as error:
            stopped.put_nowait(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        if fault == "partial-eof":
            os.write(parent_write, b"\x00\x00")
        else:
            payload = recording_frame_bytes(request)
            os.write(parent_write, payload + (payload if fault == "duplicate" else b""))
        if fault in {"partial-eof", "parent-eof"}:
            os.close(parent_write)
            parent_write = -1
        if fault == "none":
            decoder = RecordingFrameDecoder()
            reply = None
            deadline = time.monotonic() + 10
            while reply is None and time.monotonic() < deadline:
                if select.select([parent_read], [], [], 0.05)[0]:
                    for payload in decoder.feed(os.read(parent_read, 8192)):
                        reply = decode_package_message(payload)
            assert isinstance(reply, PackageWorkerReply) and reply.package is not None
        result = stopped.get(timeout=10)
        assert result == 0 if fault in {"none", "parent-eof"} else isinstance(result, BaseException)
    finally:
        if parent_write >= 0:
            os.close(parent_write)
        os.close(barrier_write)
        thread.join(timeout=2)
        worker_input.close()
        worker_output.close()
        os.close(parent_read)
        os.close(barrier_read)


def test_synced_prepare_intent_recovers_if_service_dies_before_queue_publication(
    package_rig: Any,
) -> None:
    rig, analysis, packages = package_rig
    _, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    intent = queue._intent(command)
    try:
        queue.recover()
        queue.reconcile_requests()
        job, recovered = queue.claim()
        assert recovered == intent and job.operation_id == command.operation_id
    finally:
        queue.close()


def test_prerequisites_are_checked_in_worker_after_metadata_only_ack(package_rig: Any) -> None:
    rig, analysis, packages = package_rig
    takes, command = full_selection(rig, analysis)
    command = command.model_copy(
        update={
            "take_ids": (takes[1].reservation.take_id,),
            "room_tone_take_ids": (takes[1].reservation.take_id,),
        }
    )
    queue = RecordingPackageQueue(packages)
    try:
        assert queue.submit(command).state == "queued"
        assert not packages._plan_path(command.operation_id).exists()
        job, intent = queue.claim()
        reply = prepare_worker_request(
            rig.layout,
            PackageWorkerRequest(
                generation="blocked-prerequisite", intent=intent, attempt=job.attempt
            ),
        )
        assert (
            reply.package is None and reply.error is not None and "room-tone" in reply.error.message
        )
        event = queue.complete(job, reply)
        assert event.state == "blocked" and queue.submit(command) == event
        assert rig.store.get_take(takes[1].reservation.take_id).decision == "kept"
        queue.store._write(queue._terminal_path(command.operation_id), job)
        with pytest.raises(SecurityError, match="terminal"):
            queue.submit(command)
    finally:
        queue.close()


def test_queue_corruption_during_initialization_closes_owner_descriptor(package_rig: Any) -> None:
    _, _, packages = package_rig
    queue = RecordingPackageQueue(packages)
    head = queue._head_path
    queue.close()
    head.write_bytes(b"not canonical queue evidence")
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        with pytest.raises(Exception, match="invalid versioned"):
            RecordingPackageQueue(packages)
    assert len(os.listdir("/dev/fd")) == before


def test_worker_cli_rejects_invalid_root_without_disclosing_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cole_voice_lab.recording_package_jobs import main

    assert main(["--worker", "--asset-root", str(tmp_path / "missing-private-root")]) == 2
    error = capsys.readouterr().err
    assert '"error_code": "RECORDING.WRITE_FAILED"' in error
    assert str(tmp_path) not in error


def test_corrupt_intent_stops_supervision_with_named_error_and_retains_original(
    package_rig: Any,
) -> None:
    rig, analysis, packages = package_rig
    takes, command = full_selection(rig, analysis)
    queue = RecordingPackageQueue(packages)
    failures: list[Any] = []
    supervisor = RecordingPackageSupervisor(queue, on_failure=failures.append)
    try:
        queue.submit(command)
        queue._intent_path(command.operation_id).write_bytes(b"corrupt synthetic command intent")
        supervisor.start()
        wait_until(lambda: bool(failures))
        assert supervisor.fatal_error is not None and supervisor.worker_pid is None
        assert failures[0].code == "RECORDING.WRITE_FAILED"
        assert all((rig.layout.root / take.seal.original_path).exists() for take in takes)
        assert queue._head().jobs[0].state == "queued"
    finally:
        supervisor.close()
        queue.close()


def test_worker_yields_to_live_metadata_commit_and_reports_retryable_timeout(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    from cole_voice_lab import recording_analysis

    rig, analysis, packages = package_rig
    _, command = full_selection(rig, analysis)
    monkeypatch.setattr(recording_analysis, "analysis_build_identity", lambda: analysis.build)
    queue = RecordingPackageQueue(packages)
    try:
        queue.submit(command)
        job, intent = queue.claim()
        request = PackageWorkerRequest(
            generation="busy-metadata", intent=intent, attempt=job.attempt
        )
        descriptor = os.open(
            rig.store._path("provenance/recording-takes/.metadata.lock"), os.O_RDWR
        )
        pending = rig.store._path("provenance/recording-takes/.pending.json")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # The producer observes a journal overlap without acquiring or repairing
            # the parent's metadata authority. The held flock alone is not a read dependency.
            pending.write_bytes(b"synthetic pending commit marker")
            pending.chmod(0o600)
            result = prepare_worker_request(rig.layout, request, metadata_retry_seconds=0.02)
            assert (
                result.package is None and result.error.retryable and "busy" in result.error.message
            )
            pending.unlink()
            result = prepare_worker_request(rig.layout, request)
            assert result.package is not None and result.error is None
        finally:
            pending.unlink(missing_ok=True)
            os.close(descriptor)
    finally:
        queue.close()

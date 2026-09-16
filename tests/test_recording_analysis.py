from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_recording_takes import Rig
from test_recording_takes import rig as rig

from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256, write_canonical_json
from cole_voice_lab.errors import ContractError, SecurityError, VoiceLabError
from cole_voice_lab.models import utc_now
from cole_voice_lab.recording_analysis import (
    MAX_ATTEMPTS,
    NEW_JOB_BURST,
    AnalysisWorkerRequest,
    RecordingAnalysisError,
    RecordingAnalysisJob,
    RecordingAnalysisQueue,
    RecordingAnalysisSupervisor,
    RecordingAnalysisWorker,
    analysis_build_identity,
    decode_analysis_message,
    inspect_analysis_job,
)
from cole_voice_lab.recording_contracts import RecordingFrameRange
from cole_voice_lab.recording_index import RecordingIndex, recording_index_migration
from cole_voice_lab.recording_takes import (
    RecordingAnalysisPublication,
    RecordingMetadataBusyError,
    RecordingTakeOperation,
    RecordingTakeStore,
)


@pytest.fixture(scope="session")
def analyzer_build() -> Any:
    return analysis_build_identity()


@pytest.fixture
def analysis_queue(rig: Rig, analyzer_build: Any) -> Iterator[RecordingAnalysisQueue]:
    index = RecordingIndex(rig.layout, subject_id="synthetic-subject")
    index.register_catalog(rig.store.get_group(rig.group_id).catalog)
    rig.store.projection_hook = index.update_take
    # Durable fixture capture times remain ordered; asynchronous publication uses real UTC.
    queue = RecordingAnalysisQueue(rig.store, index, build=analyzer_build)
    try:
        yield queue
    finally:
        queue.close()
        index.close()


def sealed(rig: Rig, number: int = 0) -> Any:
    rig.now = utc_now() - timedelta(seconds=5)
    rig.store.clock = lambda: rig.now
    take = rig.seal(
        rig.reserve(f"reserve-{number}", f"synthetic-{number % 3:04d}"), f"seal-{number}"
    )
    rig.store.clock = utc_now
    return take


def queued(rig: Rig, queue: RecordingAnalysisQueue) -> RecordingAnalysisJob:
    take = sealed(rig)
    # Rig writes seal timestamp one second after requested_at; move store/worker clock safely.
    rig.store.clock = lambda: max(utc_now(), rig.now)
    queue.clock = rig.store.clock
    return queue.submit(take.reservation.take_id)


def wait_until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("asynchronous condition timed out")
        time.sleep(0.01)


def test_real_inspection_and_durable_result_preserve_saved_decision(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    job = queued(rig, analysis_queue)
    take = rig.store.get_take(job.ticket.seal.take_id)
    saved = rig.store.save(rig.command(take))
    claimed = analysis_queue.claim()
    assert claimed is not None
    publication = inspect_analysis_job(rig.layout, claimed, clock=analysis_queue.clock)
    assert publication.result.state == "verified"
    completed = analysis_queue.complete(publication)
    assert completed is not None
    assert completed[1].decision == "kept" and completed[1].analysis == "verified"
    assert completed[1].decision_revision == saved.take.decision_revision
    assert completed[1].export == "unprepared"
    assert analysis_queue.get(job.ticket.job_sha256).publication == publication
    assert rig.store.analysis_result(job.ticket.job_sha256) == publication
    assert analysis_queue.submit(job.ticket.seal.take_id).ticket == job.ticket
    assert analysis_queue.claim() is None


@pytest.mark.parametrize(
    "mutation",
    [
        "digest",
        "header",
        "replace",
        "public",
        "link",
        "missing",
        "during-inspection",
        "after-inspection",
    ],
)
def test_changed_original_is_invalid_even_when_size_and_mtime_are_preserved(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = queued(rig, analysis_queue)
    claimed = analysis_queue.claim()
    assert claimed is not None
    path = rig.layout.root / job.ticket.seal.original_path
    old = path.stat()
    original_bytes = path.read_bytes()

    def mutate() -> None:
        changed = bytearray(original_bytes)
        changed[60] ^= 127
        if mutation == "header":
            changed[0] = 0
        if mutation == "replace":
            path.unlink()
        if mutation == "public":
            path.chmod(0o644)
        elif mutation == "link":
            os.link(path, path.with_suffix(".other"))
        elif mutation == "missing":
            path.unlink()
            return
        else:
            path.write_bytes(changed)
            path.chmod(0o600)
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))

    if mutation == "during-inspection":
        from cole_voice_lab import recording_analysis as module

        real = module.inspect_canonical_wav

        def inspect_then_change(source: Path) -> Any:
            result = real(source)
            mutate()
            return result

        monkeypatch.setattr(module, "inspect_canonical_wav", inspect_then_change)
    elif mutation != "after-inspection":
        mutate()
    result = inspect_analysis_job(rig.layout, claimed, clock=analysis_queue.clock)
    if mutation == "after-inspection":
        assert result.result.state == "verified"
        mutate()
    committed = analysis_queue.complete(result)
    assert committed is not None and committed[1].analysis == "invalid"
    assert not committed[1].satisfies_coverage
    if path.exists():
        assert path.stat().st_mtime_ns == old.st_mtime_ns


def test_informational_findings_are_reviewable_not_invalid(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    reservation = rig.reserve(prompt="synthetic-0001")
    receipt = rig.receipt(reservation)
    path = rig.layout.root / receipt.original_path
    data = bytearray(path.read_bytes())
    data[44:] = bytes(len(data) - 44)
    path.write_bytes(data)
    receipt = rig.receipt(reservation)
    take = rig.store.register_seal(operation_id="seal", receipt=receipt)
    rig.store.clock = lambda: max(utc_now(), rig.now)
    analysis_queue.clock = rig.store.clock
    analysis_queue.submit(take.reservation.take_id)
    claimed = analysis_queue.claim()
    assert claimed is not None
    result = inspect_analysis_job(rig.layout, claimed, clock=analysis_queue.clock)
    assert result.result.state == "needs-review" and result.result.metrics is not None
    assert "peak-too-quiet" in result.result.metrics.findings
    analysis_queue.complete(result)
    saved = rig.store.save(rig.command(rig.store.get_take(take.reservation.take_id)))
    assert saved.take.satisfies_coverage


def test_edit_makes_old_result_stale_and_new_job_independent(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    job = queued(rig, analysis_queue)
    claimed = analysis_queue.claim()
    assert claimed is not None
    result = inspect_analysis_job(rig.layout, claimed, clock=analysis_queue.clock)
    take = rig.store.get_take(job.ticket.seal.take_id)
    saved = rig.store.save(
        rig.command(
            take, frame_range=RecordingFrameRange(source_frames=480, start_frame=1, end_frame=479)
        )
    )
    assert analysis_queue.complete(result) is None
    assert analysis_queue.get(job.ticket.job_sha256).status == "stale"
    assert rig.store.get_take(take.reservation.take_id).edit_revision == 1
    new = analysis_queue.submit(saved.take.reservation.take_id)
    assert new.ticket.job_sha256 != job.ticket.job_sha256
    assert new.ticket.edit_revision == 1


@pytest.mark.parametrize(
    "field",
    [
        "source_sha256",
        "edit_revision",
        "context_revision",
        "algorithm_version",
        "analyzer_build",
        "queued_at",
        "attempt",
    ],
)
def test_unbound_results_cannot_change_take(
    rig: Rig, analysis_queue: RecordingAnalysisQueue, field: str
) -> None:
    job = queued(rig, analysis_queue)
    claimed = analysis_queue.claim()
    assert claimed is not None
    result = inspect_analysis_job(rig.layout, claimed, clock=analysis_queue.clock)
    changes: dict[str, Any] = {}
    if field == "attempt":
        result = result.model_copy(update={"attempt": 2})
    else:
        changes[field] = {
            "source_sha256": "f" * 64,
            "edit_revision": 99,
            "context_revision": 99,
            "algorithm_version": "wrong",
            "analyzer_build": result.result.analyzer_build.model_copy(update={"build_id": "wrong"}),
            "queued_at": result.result.queued_at - timedelta(seconds=1),
        }[field]
        result = result.model_copy(update={"result": result.result.model_copy(update=changes)})
    revision = rig.store.state_revision
    with pytest.raises(RecordingAnalysisError):
        analysis_queue.complete(result)
    assert rig.store.state_revision == revision
    assert analysis_queue.get(job.ticket.job_sha256).status == "running"


@pytest.mark.parametrize(
    "boundary",
    [
        "before-intent",
        "after-intent",
        "before-operation",
        "after-operation",
        "before-take-head",
        "after-take-head",
        "before-analysis-head",
        "after-analysis-head",
        "before-store-head",
        "after-store-head",
        "before-projection",
        "after-projection",
        "before-completion",
        "after-completion",
        "before-ack",
    ],
)
def test_analysis_publication_faults_repair_without_false_keep(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    boundary: str,
) -> None:
    job = queued(rig, analysis_queue)
    claimed = analysis_queue.claim()
    assert claimed is not None
    publication = inspect_analysis_job(rig.layout, claimed, clock=analysis_queue.clock)
    fired = False

    def fault(where: str) -> None:
        nonlocal fired
        if where == boundary and not fired:
            fired = True
            raise OSError("synthetic interrupted sync")

    rig.store.fault_hook = fault
    with pytest.raises(OSError):
        analysis_queue.complete(publication)
    rig.store.fault_hook = None
    rig.store.recover()
    operation_id = f"analysis-{canonical_sha256(publication)[:48]}"
    result = rig.store.publish_analysis(operation_id=operation_id, publication=publication)
    assert result.analysis == "verified" and result.decision == "draft"
    assert rig.store.analysis_result(job.ticket.job_sha256) == publication
    analysis_queue.recover()
    assert analysis_queue.get(job.ticket.job_sha256).status == "verified"


def test_fair_priority_and_bounded_pages(rig: Rig, analysis_queue: RecordingAnalysisQueue) -> None:
    jobs = []
    for number in range(10):
        take = sealed(rig, number)
        rig.store.clock = lambda: max(utc_now(), rig.now)
        analysis_queue.clock = rig.store.clock
        jobs.append(
            analysis_queue.submit(
                take.reservation.take_id, priority="historical" if number < 2 else "new"
            )
        )
    assert len(analysis_queue.page(limit=3)) == 3
    assert len(analysis_queue.page(after_sequence=jobs[2].sequence, limit=3)) == 3
    for limit in (0, 201):
        with pytest.raises(ContractError):
            analysis_queue.page(limit=limit)
    priorities = []
    for _ in range(8):
        job = analysis_queue.claim()
        assert job is not None
        priorities.append(job.priority)
    assert priorities[NEW_JOB_BURST] == "historical"
    assert priorities[-1] == "historical"


def test_queue_rebuild_restart_retry_and_failed_attempt_bound(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    job = queued(rig, analysis_queue)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claimed = analysis_queue.claim()
        assert claimed is not None and claimed.attempt == attempt
        analysis_queue.worker_failed(claimed, "synthetic crash")
        analysis_queue.recover()
    assert analysis_queue.get(job.ticket.job_sha256).status == "failed"
    assert analysis_queue.claim() is None
    analysis_queue.retry(job.ticket.job_sha256)
    analysis_queue.recover()
    next_job = analysis_queue.claim()
    assert next_job is not None and next_job.attempt == MAX_ATTEMPTS + 1
    analysis_queue.index.reset_analysis_projection()
    assert analysis_queue.page() == ()
    analysis_queue.recover()
    assert analysis_queue.get(job.ticket.job_sha256).status == "failed"
    assert len(analysis_queue.page()) == 1


def test_private_single_owner_queue_and_corrupt_projection_fail_closed(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    job = queued(rig, analysis_queue)
    with pytest.raises(BlockingIOError):
        RecordingAnalysisQueue(rig.store, analysis_queue.index, build=analysis_queue.build)
    analysis_queue.index.connection.execute(
        "UPDATE recording_analysis_jobs SET recording_analysis_job_json='{}'"
    )
    with pytest.raises(ValidationError):
        analysis_queue.claim()
    analysis_queue.recover()
    path = analysis_queue._path(job.ticket.job_sha256)
    path.chmod(0o644)
    with pytest.raises(SecurityError):
        analysis_queue.get(job.ticket.job_sha256)
    path.chmod(0o600)


def test_migrations_up_down_preserve_library(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    connection = analysis_queue.index.connection
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    count = connection.execute("SELECT COUNT(*) FROM recording_catalogs").fetchone()[0]
    connection.executescript(recording_index_migration("down", version=2))
    connection.executescript(recording_index_migration("down", version=2))
    connection.executescript(recording_index_migration(version=2))
    connection.executescript(recording_index_migration(version=2))
    assert connection.execute("SELECT COUNT(*) FROM recording_catalogs").fetchone()[0] == count


def test_old_operation_canonical_bytes_survive_optional_analysis_extension(rig: Rig) -> None:
    reservation = rig.reserve()
    path = rig.store._operation_path("reserve-1")
    raw = json.loads(path.read_bytes())
    assert "analysis" not in raw
    assert (
        canonical_json_bytes(RecordingTakeOperation.model_validate_json(path.read_bytes())) + b"\n"
        == path.read_bytes()
    )
    reopened = RecordingTakeStore(rig.layout, clock=lambda: rig.now)
    assert reopened.get_take(reservation.take_id).reservation == reservation


@pytest.mark.parametrize(
    "payload", [b"{}", b"[]", b"{", b'{"x":1,"x":2}', b'{"x":NaN}', b"x" * 65537, b"\xff"]
)
def test_worker_contract_rejects_malformed_bounded_messages(payload: bytes) -> None:
    with pytest.raises(RecordingAnalysisError):
        decode_analysis_message(payload)


def test_persistent_process_inspects_multiple_real_synthetic_files(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    queued(rig, analysis_queue)
    worker = RecordingAnalysisWorker(rig.layout, analysis_queue.build)
    try:
        pid = worker.pid
        first = analysis_queue.claim()
        assert first is not None
        first_result = worker.analyze(first)
        assert first_result.result.state == "verified"
        analysis_queue.complete(first_result)
        take = sealed(rig, 1)
        analysis_queue.submit(take.reservation.take_id)
        second = analysis_queue.claim()
        assert second is not None
        assert worker.analyze(second).result.state == "verified"
        assert worker.pid == pid and pid != os.getpid()
    finally:
        worker.close()


def test_blocked_worker_does_not_block_real_reserve_seal_save_and_restarts(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    job = queued(rig, analysis_queue)
    reader, writer = os.pipe()
    completed: list[str] = []
    supervisor = RecordingAnalysisSupervisor(
        analysis_queue,
        block_fd=reader,
        on_result=lambda operation, result: completed.append(result.state),
    )
    supervisor.start()
    try:
        wait_until(lambda: supervisor.worker_pid is not None)
        first_pid = supervisor.worker_pid
        started = time.monotonic()
        next_take = sealed(rig, 1)
        saved = rig.store.save(rig.command(next_take))
        assert saved.take.decision == "kept"
        assert time.monotonic() - started < 1.0
        assert analysis_queue.get(job.ticket.job_sha256).status == "running"
        assert first_pid is not None
        os.kill(first_pid, signal.SIGKILL)
        wait_until(lambda: supervisor.worker_pid is not None and supervisor.worker_pid != first_pid)
        os.write(writer, b"1")
        wait_until(lambda: analysis_queue.get(job.ticket.job_sha256).status == "verified")
        assert analysis_queue.get(job.ticket.job_sha256).attempt == 2
        assert supervisor.fatal_error is None
        assert "failed" in completed and "verified" in completed
    finally:
        supervisor.close()
        os.close(reader)
        os.close(writer)


def test_worker_eof_while_inspection_blocked_exits_without_removing_source(
    rig: Rig, analysis_queue: RecordingAnalysisQueue
) -> None:
    job = queued(rig, analysis_queue)
    claimed = analysis_queue.claim()
    assert claimed is not None
    reader, writer = os.pipe()
    worker = RecordingAnalysisWorker(rig.layout, analysis_queue.build, block_fd=reader)
    try:
        worker._send(AnalysisWorkerRequest(generation=worker.generation, job=claimed))
        assert worker.process.stdin is not None
        worker.process.stdin.close()
        assert worker.process.wait(timeout=2) == 0
        assert (rig.layout.root / job.ticket.seal.original_path).is_file()
    finally:
        worker.close()
        os.close(reader)
        os.close(writer)


def test_actual_service_and_analysis_processes_keep_save_reachable(
    rig: Rig,
    tmp_path: Path,
) -> None:
    from test_recording_service import build_identity, handshake, read_response, request

    from cole_voice_lab.recording_contracts import (
        AnalysisEvent,
        RecordingSelection,
        RegisterSealCommand,
        ReservationEvent,
        ReserveTakeCommand,
        SaveEvent,
        ShutdownCommand,
        TakeEvent,
        recording_frame_bytes,
    )
    from cole_voice_lab.recording_service import recording_service_build_identity

    build = recording_service_build_identity()
    own_path, client_path = tmp_path / "service-build.json", tmp_path / "client-build.json"
    write_canonical_json(own_path, build)
    write_canonical_json(client_path, build_identity())
    group = rig.store.get_group(rig.group_id)
    block_reader, block_writer = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cole_voice_lab.recording_service",
            "--asset-root",
            str(rig.layout.root),
            "--catalog-sha256",
            group.catalog_sha256,
            "--recording-group-id",
            group.group_id,
            "--process-generation",
            "generation-1",
            "--build-identity",
            str(own_path),
            "--expected-client-build",
            str(client_path),
            "--analysis-test-block-fd",
            str(block_reader),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(block_reader,),
    )
    assert process.stdin is not None and process.stdout is not None
    events: list[Any] = []

    def send(command: Any) -> Any:
        process.stdin.write(recording_frame_bytes(command))
        process.stdin.flush()
        while True:
            response = read_response(process.stdout.fileno(), timeout=15)
            events.append(response)
            assert response.error is None, response.error
            if response.meta.request_id == command.meta.request_id:
                return response.data

    try:
        send(handshake(service=build))
        selection = RecordingSelection(
            catalog_sha256=group.catalog_sha256,
            prompt_id="synthetic-0001",
            take_id=None,
            selection_revision=0,
            index_revision=0,
        )
        reserved = send(
            request(
                ReserveTakeCommand(
                    operation_id="service-reserve",
                    group_id=group.group_id,
                    recorder_generation="synthetic-helper",
                    selection=selection,
                ),
                request_id="reserve",
            )
        )
        assert isinstance(reserved, ReservationEvent)
        reservation = reserved.take.reservation
        receipt = rig.receipt(reservation)
        wait_until(lambda: utc_now() >= receipt.last_sample_at)
        receipt = receipt.model_copy(update={"sealed_at": utc_now()})
        draft = send(
            request(
                RegisterSealCommand(operation_id="service-seal", receipt=receipt),
                request_id="seal",
                take_id=reservation.take_id,
            )
        )
        assert isinstance(draft, TakeEvent)

        # Both queued/running heads are metadata; no inspection or sample read here.
        def running() -> bool:
            heads = tuple((rig.layout.root / "provenance/recording-analysis/jobs").glob("*.json"))
            return bool(heads) and json.loads(heads[0].read_bytes())["status"] == "running"

        wait_until(running)
        start = time.monotonic()
        saved = send(
            request(
                rig.command(draft.take, operation="service-save"),
                request_id="save",
                take_id=reservation.take_id,
            )
        )
        save_seconds = time.monotonic() - start
        assert isinstance(saved, SaveEvent) and saved.take.decision == "kept"
        assert save_seconds < 1.0 and running()
        # The blocked inspector is a real child of the actual service process.
        process_rows = subprocess.check_output(
            ["ps", "-axo", "pid,ppid,comm"], text=True
        ).splitlines()[1:]
        child_pids = [
            int(row.split(None, 2)[0])
            for row in process_rows
            if len(row.split(None, 2)) == 3 and int(row.split(None, 2)[1]) == process.pid
        ]
        assert len(child_pids) == 1
        os.write(block_writer, b"1")
        while not any(isinstance(event.data, AnalysisEvent) for event in events):
            events.append(read_response(process.stdout.fileno(), timeout=15))
        event = next(event for event in events if isinstance(event.data, AnalysisEvent))
        assert event.data.result.state == "verified"
        assert event.meta.take_id == reservation.take_id
        assert event.meta.request_id.startswith("analysis-")
        assert rig.store.get_take(reservation.take_id).decision == "kept"
        evidence = {
            "service_pid": process.pid,
            "worker_pid": child_pids[0],
            "save_seconds_while_worker_blocked": save_seconds,
            "source_sha256": receipt.original_sha256,
            "result": event.data.result.model_dump(mode="json"),
            "synthetic_only": True,
        }
        write_canonical_json(tmp_path / "actual-service-worker-evidence.json", evidence)
        send(request(ShutdownCommand(), request_id="shutdown"))
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()
        assert process.stderr is not None
        process.stderr.close()
        os.close(block_reader)
        os.close(block_writer)


def test_running_restart_is_bounded_and_build_upgrade_requeues(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
) -> None:
    job = queued(rig, analysis_queue)
    for _ in range(MAX_ATTEMPTS):
        assert analysis_queue.claim() is not None
        analysis_queue.recover()
    assert analysis_queue.get(job.ticket.job_sha256).status == "failed"
    prior = analysis_queue.build
    analysis_queue.build = prior.model_copy(update={"build_id": "new-analyzer-build"})
    analysis_queue.recover()
    jobs = analysis_queue.page()
    assert len(jobs) == 2
    assert jobs[0].status == "stale"
    assert jobs[1].ticket.analyzer_build != prior
    assert jobs[1].status == "pending" and jobs[1].priority == "historical"
    promoted = analysis_queue.submit(job.ticket.seal.take_id)
    assert promoted.priority == "new"


def test_invalid_job_model_and_journal_job_binding_are_rejected(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
) -> None:
    job = queued(rig, analysis_queue)
    with pytest.raises(ValidationError):
        AnalysisWorkerRequest(generation="test", job=job)
    with pytest.raises(RecordingAnalysisError):
        analysis_queue.retry(job.ticket.job_sha256)
    with pytest.raises(RecordingAnalysisError):
        analysis_queue.submit(rig.reserve("unsealed").take_id)
    running = analysis_queue.claim()
    assert running is not None
    result = inspect_analysis_job(rig.layout, running, clock=analysis_queue.clock)
    with pytest.raises(ValidationError):
        RecordingAnalysisPublication.model_validate(result.model_dump() | {"source_identity": None})
    with pytest.raises(ValidationError):
        RecordingAnalysisJob.model_validate(job.model_dump() | {"publication": result})
    wrong = result.model_copy(update={"job_sha256": "f" * 64})
    with pytest.raises(ValidationError):
        rig.store.publish_analysis(operation_id="wrong-binding", publication=wrong)
    # Claim also checks a stale edit before sending bytes to the worker.
    take = rig.store.get_take(job.ticket.seal.take_id)
    rig.store.save(
        rig.command(
            take, frame_range=RecordingFrameRange(source_frames=480, start_frame=2, end_frame=479)
        )
    )
    with pytest.raises(ContractError):
        rig.store.publish_analysis(operation_id="stale-edit", publication=result)


def test_source_disappearance_after_inspection_publishes_invalid(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
) -> None:
    job = queued(rig, analysis_queue)
    running = analysis_queue.claim()
    assert running is not None
    result = inspect_analysis_job(rig.layout, running, clock=analysis_queue.clock)
    (rig.layout.root / job.ticket.seal.original_path).unlink()
    committed = analysis_queue.complete(result)
    assert committed is not None and committed[1].analysis == "invalid"


def test_transient_inspector_io_failure_is_independently_retryable(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cole_voice_lab import recording_analysis as module

    queued(rig, analysis_queue)
    running = analysis_queue.claim()
    assert running is not None

    def fail(_path: Path) -> Any:
        raise OSError("synthetic transient read failure")

    monkeypatch.setattr(module, "inspect_canonical_wav", fail)
    result = inspect_analysis_job(rig.layout, running, clock=analysis_queue.clock)
    assert result.result.state == "failed" and result.result.error is not None
    assert result.result.error.retryable
    analysis_queue.complete(result)
    assert analysis_queue.retry(running.ticket.job_sha256).status == "pending"


def test_worker_runtime_mismatch_and_timeout_retain_original(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
) -> None:
    job = queued(rig, analysis_queue)
    wrong = analysis_queue.build.model_copy(update={"source_sha256": "f" * 64})
    with pytest.raises(RecordingAnalysisError, match="runtime identity"):
        RecordingAnalysisWorker(rig.layout, wrong)
    reader, writer = os.pipe()
    worker = RecordingAnalysisWorker(
        rig.layout, analysis_queue.build, block_fd=reader, deadline_seconds=0.05
    )
    try:
        running = analysis_queue.claim()
        assert running is not None
        with pytest.raises(RecordingAnalysisError, match="deadline"):
            worker.analyze(running)
        assert (rig.layout.root / job.ticket.seal.original_path).exists()
    finally:
        worker.close()
        os.close(reader)
        os.close(writer)


def test_ownership_substitution_and_undeclared_evidence_fail_closed(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
) -> None:
    queued(rig, analysis_queue)
    other = analysis_queue.jobs_directory / "unexpected"
    other.write_text("invalid", encoding="utf-8")
    with pytest.raises(SecurityError, match="undeclared"):
        analysis_queue.recover()
    other.unlink()
    owner = analysis_queue.directory / ".owner.lock"
    moved = owner.with_name(".old-owner.lock")
    owner.rename(moved)
    owner.touch(mode=0o600)
    with pytest.raises(SecurityError, match="ownership"):
        analysis_queue.page()
    owner.unlink()
    moved.rename(owner)
    analysis_queue.close()
    with pytest.raises(RecordingAnalysisError, match="closed"):
        analysis_queue.page()


def test_backlog_memory_is_paged_and_status_queries_open_zero_wavs(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tracemalloc

    from cole_voice_lab import recording_analysis as module

    # Every job uses actual durable take/seal and real synthetic PCM; only scheduling runs here.
    for number in range(250):
        take = sealed(rig, number)
        analysis_queue.submit(take.reservation.take_id, priority="historical")
    wav_opens = 0

    def forbidden(_path: Path) -> Any:
        nonlocal wav_opens
        wav_opens += 1
        raise AssertionError("metadata status must not inspect WAV")

    monkeypatch.setattr(module, "inspect_canonical_wav", forbidden)
    tracemalloc.start()
    try:
        cursor = 0
        total = 0
        while page := analysis_queue.page(after_sequence=cursor, limit=20):
            assert len(page) <= 20
            total += len(page)
            cursor = page[-1].sequence
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert total == 250 and wav_opens == 0
    write_canonical_json(
        rig.layout.root.parent / "queue-memory-evidence.json",
        {
            "synthetic_sealed_takes": total,
            "page_limit": 20,
            "historical_wav_opens": wav_opens,
            "metadata_paging_peak_tracemalloc_bytes": peak,
        },
    )
    assert peak < 8 * 1024 * 1024
    assert len(analysis_queue.index.analysis_jobs(status="pending", limit=1)) == 1


@pytest.mark.parametrize(
    "fault", ["none", "generation", "build", "algorithm", "inspection", "wrong-kind", "partial-eof"]
)
def test_worker_main_protocol_and_eof_boundaries_with_real_pipes(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import queue as thread_queue
    import select
    import threading
    from types import SimpleNamespace

    from cole_voice_lab import recording_analysis as module
    from cole_voice_lab.recording_contracts import recording_frame_bytes
    from cole_voice_lab.recording_service import RecordingFrameDecoder

    queued(rig, analysis_queue)
    job = analysis_queue.claim()
    assert job is not None
    parent_read, worker_write = os.pipe()
    worker_read, parent_write = os.pipe()
    worker_input = os.fdopen(worker_read, "rb", buffering=0)
    worker_output = os.fdopen(worker_write, "wb", buffering=0)
    monkeypatch.setattr(module, "sys", SimpleNamespace(stdin=worker_input, stdout=worker_output))
    monkeypatch.setattr(module, "analysis_build_identity", lambda: analysis_queue.build)
    if fault == "inspection":

        def fail(*_arguments: Any) -> Any:
            raise RuntimeError("synthetic inspector fault")

        monkeypatch.setattr(module, "inspect_analysis_job", fail)
    stopped: thread_queue.Queue[Any] = thread_queue.Queue(1)

    def run() -> None:
        try:
            stopped.put_nowait(module._worker_main(rig.layout, "in-process-harness", None))
        except BaseException as error:
            stopped.put_nowait(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def read() -> Any:
        decoder = RecordingFrameDecoder()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if select.select([parent_read], [], [], 0.05)[0]:
                for payload in decoder.feed(os.read(parent_read, 8192)):
                    return decode_analysis_message(payload)
        raise AssertionError("worker harness reply timed out")

    try:
        hello = read()
        assert isinstance(hello, module.AnalysisWorkerHello)
        generation = "wrong" if fault == "generation" else "in-process-harness"
        if fault in {"build", "algorithm"}:
            ticket = job.ticket.model_copy(
                update={
                    "analyzer_build": analysis_queue.build.model_copy(update={"build_id": "wrong"})
                }
                if fault == "build"
                else {"algorithm_version": "wrong"}
            )
            job = job.model_copy(update={"ticket": ticket})
        request = module.AnalysisWorkerRequest(generation=generation, job=job)
        frame = recording_frame_bytes(hello if fault == "wrong-kind" else request)
        if fault == "partial-eof":
            os.write(parent_write, frame[:2])
            os.close(parent_write)
            parent_write = -1
        else:
            os.write(parent_write, frame)
        if fault == "none":
            result = read()
            assert isinstance(result, module.AnalysisWorkerReply)
            assert result.publication.result.state == "verified"
            os.close(parent_write)
            parent_write = -1
            assert stopped.get(timeout=3) == 0
        else:
            assert isinstance(stopped.get(timeout=3), VoiceLabError)
    finally:
        if parent_write >= 0:
            os.close(parent_write)
        thread.join(timeout=3)
        assert not thread.is_alive()
        worker_input.close()
        worker_output.close()
        os.close(parent_read)


def test_worker_cli_named_failure_and_normal_exit(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cole_voice_lab import recording_analysis as module

    def fail(*_arguments: Any) -> int:
        raise ContractError("private fixture path must not appear in diagnostics")

    monkeypatch.setattr(module, "_worker_main", fail)
    arguments = ["--worker", "--asset-root", str(rig.layout.root), "--generation", "test"]
    assert module.main(arguments) == 2
    output = json.loads(capsys.readouterr().err)
    assert output["error_code"] == "RECORDING.ANALYSIS_FAILED"
    assert "private fixture" not in output["message"]
    monkeypatch.setattr(module, "_worker_main", lambda *_args: 0)
    assert module.main(arguments) == 0


def test_supervisor_metadata_failure_surfaces_named_background_error(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_recording_service import build_identity, handshake

    from cole_voice_lab.recording_service import RecordingServiceSession, RecordingTakesBackend

    queued(rig, analysis_queue)
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=RecordingTakesBackend(rig.store, rig.group_id),
    )
    session.submit(handshake())
    list(session.completed())

    def fail() -> Any:
        raise SecurityError("synthetic job head was replaced")

    monkeypatch.setattr(analysis_queue, "claim", fail)
    supervisor = RecordingAnalysisSupervisor(
        analysis_queue, on_failure=lambda error: session.publish_error("analysis-supervisor", error)
    )
    supervisor.start()
    try:
        wait_until(lambda: supervisor.fatal_error is not None)
        wait_until(lambda: session.has_responses)
        response = tuple(session.completed())[0]
        assert response.meta.request_id == "analysis-supervisor"
        assert response.error is not None and response.error.code == "RECORDING.ANALYSIS_FAILED"
        assert response.data is None
        assert "replaced" not in response.error.message
    finally:
        supervisor.close()
        session.close()


def test_new_analysis_sql_bytes_are_part_of_runtime_build_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cole_voice_lab import recording_index as module
    from cole_voice_lab.recording_service import recording_service_build_identity

    resource = tmp_path / module._ANALYSIS_MIGRATION_NAME
    resource.write_bytes(module._migration_bytes(module._ANALYSIS_MIGRATION_NAME))
    resource.chmod(0o600)
    original = module._migration_resource
    monkeypatch.setattr(
        module,
        "_migration_resource",
        lambda name: resource if name == module._ANALYSIS_MIGRATION_NAME else original(name),
    )
    before = recording_service_build_identity()
    resource.write_bytes(resource.read_bytes() + b"\n-- Synthetic queue schema identity change.\n")
    after = recording_service_build_identity()
    assert before.source_sha256 != after.source_sha256
    assert before.executable_sha256 == after.executable_sha256
    assert len(module.recording_index_migration_resources()) == 3


def test_startup_reconciles_seal_commit_without_job_and_can_be_cancelled(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
) -> None:
    # Simulate service death after seal evidence sync and before queue.submit.
    take = sealed(rig)
    rig.reserve("unsealed-gap")
    assert analysis_queue.page() == ()
    assert analysis_queue.reconcile_takes(cancelled=lambda: True) == 0
    assert analysis_queue.reconcile_takes() == 1
    assert analysis_queue.page()[0].ticket.seal == take.seal
    supervisor = RecordingAnalysisSupervisor(analysis_queue)
    supervisor.start()
    try:
        wait_until(lambda: rig.store.get_take(take.reservation.take_id).analysis == "verified")
        assert supervisor.fatal_error is None
    finally:
        supervisor.close()


def test_held_historical_publication_does_not_block_new_submit_claim_or_metadata(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical = queued(rig, analysis_queue)
    new_take = sealed(rig, 1)
    entered, release = threading.Event(), threading.Event()
    read = rig.store.analysis_result_for_projection

    def held(digest: str) -> Any:
        if digest == historical.ticket.job_sha256:
            entered.set()
            assert release.wait(3)
        return read(digest)

    monkeypatch.setattr(rig.store, "analysis_result_for_projection", held)
    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery = executor.submit(analysis_queue.recover, on_ready=lambda: None)
        try:
            assert entered.wait(2)
            submitted = executor.submit(analysis_queue.submit, new_take.reservation.take_id).result(
                timeout=1
            )
            claimed = executor.submit(analysis_queue.claim).result(timeout=1)
            assert claimed is not None and claimed.ticket == submitted.ticket
            assert (
                executor.submit(rig.store.get_take, new_take.reservation.take_id)
                .result(timeout=1)
                .seal
                == new_take.seal
            )
        finally:
            release.set()
        recovery.result(timeout=2)
    assert analysis_queue.get(claimed.ticket.job_sha256).status == "running"


def test_historical_compare_before_apply_preserves_a_newer_claim(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = queued(rig, analysis_queue)
    entered, release = threading.Event(), threading.Event()
    read = rig.store.analysis_result_for_projection

    def held(digest: str) -> Any:
        publication = read(digest)
        entered.set()
        assert release.wait(3)
        return publication

    monkeypatch.setattr(rig.store, "analysis_result_for_projection", held)
    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery = executor.submit(analysis_queue.recover, on_ready=lambda: None)
        try:
            assert entered.wait(2)
            analysis_queue.submit(job.ticket.seal.take_id)
            claimed = analysis_queue.claim()
            assert claimed is not None
        finally:
            release.set()
        recovery.result(timeout=2)
    assert analysis_queue.page() == (claimed,)
    assert analysis_queue.get(job.ticket.job_sha256) == claimed


def test_historical_take_read_is_outside_interactive_locks(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    take = sealed(rig)
    entered, release = threading.Event(), threading.Event()
    read = rig.store.get_take_for_projection

    def held(take_id: str) -> Any:
        entered.set()
        assert release.wait(3)
        return read(take_id)

    def forbidden(**_arguments: Any) -> Any:
        raise AssertionError("historical reconciliation must not decode a locked take page")

    monkeypatch.setattr(rig.store, "get_take_for_projection", held)
    monkeypatch.setattr(rig.store, "iter_takes", forbidden)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reconciliation = executor.submit(analysis_queue.reconcile_takes)
        try:
            assert entered.wait(2)
            assert (
                executor.submit(rig.store.get_take, take.reservation.take_id).result(timeout=1).seal
                == take.seal
            )
            assert executor.submit(analysis_queue.page).result(timeout=1) == ()
        finally:
            release.set()
        assert reconciliation.result(timeout=2) == 1


def test_historical_submission_rechecks_an_edit_made_after_its_source_read(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    take = sealed(rig)
    read = rig.store.get_take_for_projection
    edited = False

    def edit_after_read(take_id: str) -> Any:
        nonlocal edited
        snapshot = read(take_id)
        if not edited:
            edited = True
            rig.store.save(
                rig.command(
                    snapshot,
                    frame_range=RecordingFrameRange(
                        source_frames=480, start_frame=1, end_frame=479
                    ),
                )
            )
        return snapshot

    monkeypatch.setattr(rig.store, "get_take_for_projection", edit_after_read)
    assert analysis_queue.reconcile_takes() == 1
    jobs = analysis_queue.page()
    assert [(job.ticket.edit_revision, job.status) for job in jobs] == [
        (0, "stale"),
        (1, "pending"),
    ]
    assert rig.store.get_take(take.reservation.take_id).edit_revision == 1


def test_busy_claim_preserves_pending_job_without_consuming_an_attempt(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = queued(rig, analysis_queue)
    publish = analysis_queue._publish

    def busy(*_args: Any, **_kwargs: Any) -> Any:
        raise RecordingMetadataBusyError("synthetic canonical publication overlap")

    monkeypatch.setattr(analysis_queue, "_publish", busy)
    with pytest.raises(RecordingMetadataBusyError):
        analysis_queue.claim()
    assert analysis_queue.page() == (job,)
    assert analysis_queue.get(job.ticket.job_sha256) == job
    monkeypatch.setattr(analysis_queue, "_publish", publish)
    claimed = analysis_queue.claim()
    assert claimed is not None and claimed.attempt == 1


def test_supervisor_processes_new_job_while_historical_read_is_held_and_retries_busy(
    rig: Rig,
    analysis_queue: RecordingAnalysisQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from cole_voice_lab import recording_analysis as module

    historical = queued(rig, analysis_queue)
    new_take = sealed(rig, 1)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    read = rig.store.analysis_result_for_projection
    busy = 0

    def held(digest: str) -> Any:
        nonlocal busy
        if digest == historical.ticket.job_sha256:
            busy += 1
            if busy <= 2:
                raise RecordingMetadataBusyError("synthetic overlapping metadata commit")
            entered.set()
            assert release.wait(4)
        return read(digest)

    class Worker:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.process = SimpleNamespace(poll=lambda: 0)

        def analyze(self, job: RecordingAnalysisJob) -> RecordingAnalysisPublication:
            return inspect_analysis_job(rig.layout, job)

        def close(self) -> None:
            pass

    monkeypatch.setattr(rig.store, "analysis_result_for_projection", held)
    monkeypatch.setattr(module, "RecordingAnalysisWorker", Worker)
    supervisor = RecordingAnalysisSupervisor(
        analysis_queue,
        on_result=lambda _operation, result: (
            completed.set() if result.take_id == new_take.reservation.take_id else None
        ),
    )
    supervisor.start()
    try:
        assert entered.wait(2)
        analysis_queue.submit(new_take.reservation.take_id)
        assert completed.wait(2), "new analysis waited for unrelated historical input"
        assert not release.is_set() and supervisor.fatal_error is None
        assert busy == 3
    finally:
        release.set()
        supervisor.close()


def test_analysis_contracts_are_exported_as_strict_schemas() -> None:
    from cole_voice_lab.recording_analysis import RECORDING_ANALYSIS_SCHEMA_MODELS
    from cole_voice_lab.schema import SCHEMA_MODELS

    assert len(RECORDING_ANALYSIS_SCHEMA_MODELS) == 5
    for name, model in RECORDING_ANALYSIS_SCHEMA_MODELS.items():
        assert SCHEMA_MODELS[name] is model
        assert model.model_json_schema()["additionalProperties"] is False

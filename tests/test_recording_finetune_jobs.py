"""Fine-tune jobs in the recording service (recording_finetune_jobs.py).

The worker runs the real pipeline against the stub F5 runtime from test_finetune.py. The stub's
pretrained files cannot match the pinned F5-TTS v1 Base weights and Vocos vocoder, so tests
that train pass the stub's pins through ``FineTuneSupervisor(pretrained_pin=...)``, a
constructor seam the recording service never uses; the real-service tests stay on paths that
never check the pins (a slow runtime probe, a job recovered at startup).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_finetune import FFMPEG, _stub_runtime, _write_run
from test_pilot_v2_coverage import SUBJECT, NativeCoverage, released_pilot_v2
from test_pilot_v2_coverage import native_coverage as native_coverage
from test_recording_packages import analyzer_build as analyzer_build
from test_recording_packages import package_rig as package_rig
from test_recording_service import build_identity, handshake, read_response, request

import cole_voice_lab.recording_finetune_jobs as jobs_module
from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.corpus_store import CorpusStoreError, open_corpus_store
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.finetune import FineTuneError, FineTunePretrainedPins, FineTuneRun
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    CancelFineTuneCommand,
    FineTuneCommand,
    FineTuneEvent,
    FineTuneProgressEvent,
    FineTuneStatusCommand,
    FineTuneStatusEvent,
    RecordingResponse,
    SaveEvent,
    ShutdownCommand,
    recording_frame_bytes,
)
from cole_voice_lab.recording_finetune_jobs import (
    CANCELLED_BY_OPERATOR,
    CANCELLING_MESSAGE,
    FINETUNE_MIN_FREE_BYTES,
    FINETUNE_WORKER_MODULE,
    INTERRUPTED_BY_RESTART,
    RECOVERING_MESSAGE,
    SUPERVISION_FAILURE,
    SUPERVISION_STOPPED,
    FineTuneJob,
    FineTuneJobError,
    FineTuneJobHead,
    FineTuneJobQueue,
    FineTuneLogTail,
    FineTuneProgressTracker,
    FineTuneSupervisor,
    FineTuneTrainingProgress,
    FineTuneWorkerReply,
    FineTuneWorkerRequest,
    FineTuneWorkerStage,
    ProcessRow,
    active_finetune_job,
    decode_finetune_worker_message,
    finetune_job_event,
    parse_training_progress,
    process_table,
    run_finetune_worker_request,
    worker_command_marker,
)
from cole_voice_lab.recording_service import (
    RecordingCancellation,
    RecordingServiceError,
    RecordingTakesBackend,
    RecordingWorkspaceBackend,
    recording_service_build_identity,
)
from cole_voice_lab.recording_takes import (
    RecordingMetadataBusyError,
    read_recording_evidence,
    write_recording_evidence,
)

needs_ffmpeg = pytest.mark.skipif(not FFMPEG.exists(), reason="ffmpeg is not installed")
CATALOG_ID = "cole-corpus"


@dataclass(frozen=True)
class Library:
    """A cole-self library with one kept take of 34 spoken prompts, and a stub runtime."""

    coverage: NativeCoverage
    runtime: Path
    pin: FineTunePretrainedPins

    @property
    def layout(self) -> AssetLayout:
        return self.coverage.rig.layout

    def mode(self, **mode: Any) -> None:
        (self.runtime / "stub-mode.json").write_text(json.dumps(mode))


@pytest.fixture
def library(native_coverage: NativeCoverage, tmp_path: Path) -> Library:
    layout = native_coverage.rig.layout
    with open_corpus_store(layout, create=True) as corpus:
        corpus.import_prompt_suite(released_pilot_v2(), source="pilot-v2")
        corpus.create_catalog(CATALOG_ID, purpose="capture-pilot")
        corpus.activate(SUBJECT, corpus.publish_version(CATALOG_ID))
    runtime, pin = _stub_runtime(tmp_path.resolve() / "runtime")
    return Library(native_coverage, runtime, pin)


class Events:
    """Collects published events with their arrival time; tests wait on them."""

    def __init__(self) -> None:
        self.items: list[tuple[float, str, Any]] = []
        self._condition = threading.Condition()

    def __call__(self, request_id: str, event: Any) -> None:
        with self._condition:
            self.items.append((time.monotonic(), request_id, event))
            self._condition.notify_all()

    @property
    def events(self) -> list[Any]:
        with self._condition:
            return [event for _, _, event in self.items]

    def wait_for(self, predicate: Callable[[Any], bool], timeout: float = 60) -> Any:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for _, _, event in self.items:
                    if predicate(event):
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"no matching fine-tune event in {self.items}")
                self._condition.wait(remaining)


def progress(state: str, **fields: Any) -> Callable[[Any], bool]:
    def matches(event: Any) -> bool:
        return (
            isinstance(event, FineTuneProgressEvent)
            and event.state == state
            and all(getattr(event, name) == value for name, value in fields.items())
        )

    return matches


def wait_until(predicate: Callable[[], bool], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("synthetic fine-tune deadline exceeded")
        time.sleep(0.02)


def live_group(pgid: int) -> list[ProcessRow]:
    return [row for row in process_table() if row.pgid == pgid and not row.zombie]


def plenty(_path: Path) -> int:
    return 4 * FINETUNE_MIN_FREE_BYTES


def queue_for(library: Library, **options: Any) -> FineTuneJobQueue:
    """A queue on the library; free disk space is injected unless a test names it."""
    options.setdefault("runtime_root", library.runtime)
    options.setdefault("free_bytes", plenty)
    return FineTuneJobQueue(library.layout, subject_id=SUBJECT, **options)


@pytest.fixture
def jobs(library: Library) -> Iterator[tuple[FineTuneJobQueue, Events, list[FineTuneSupervisor]]]:
    """A queue and an event sink; supervisors a test starts are closed afterwards."""
    queue = queue_for(library)
    supervisors: list[FineTuneSupervisor] = []
    try:
        yield queue, Events(), supervisors
    finally:
        for supervisor in supervisors:
            supervisor.close()
        queue.close()


def supervise(
    library: Library,
    queue: FineTuneJobQueue,
    events: Events,
    supervisors: list[FineTuneSupervisor],
    **options: Any,
) -> FineTuneSupervisor:
    options.setdefault("pretrained_pin", library.pin)
    options.setdefault("progress_interval", 0.3)
    options.setdefault("cancel_grace", 1.0)
    supervisor = FineTuneSupervisor(queue, on_event=events, **options)
    supervisors.append(supervisor)
    return supervisor


def command(operation_id: str = "tune-1", **changes: Any) -> FineTuneCommand:
    return FineTuneCommand.model_validate(
        {"operation_id": operation_id, "allow_missing": True} | changes
    )


def cancel(operation_id: str = "tune-1") -> CancelFineTuneCommand:
    return CancelFineTuneCommand(operation_id="cancel-1", cancelled_operation_id=operation_id)


def status(supervisor: FineTuneSupervisor) -> FineTuneStatusEvent:
    return supervisor.status(FineTuneStatusCommand(operation_id="status-1"))


def run_record(library: Library, run_id: str) -> FineTuneRun:
    path = library.layout.root / "derived/finetune" / run_id / "run.json"
    return FineTuneRun.model_validate_json(path.read_bytes())


def runtime_link(library: Library, run_id: str) -> Path:
    return library.runtime / "lib" / "python3.12" / "ckpts" / run_id


def terminal(queue: FineTuneJobQueue, operation_id: str) -> FineTuneJob:
    return read_recording_evidence(
        queue.directory / "terminal" / f"{operation_id}.json", FineTuneJob
    )


# -- the whole job -------------------------------------------------------------------------


@needs_ffmpeg
def test_a_job_runs_from_queued_to_finished_with_rate_limited_progress(
    library: Library, jobs: Any
) -> None:
    queue, events, supervisors = jobs
    library.mode(step_seconds=0.25)
    supervisor = supervise(library, queue, events, supervisors)
    before = status(supervisor)
    assert before.runtime_configured and before.active is None and before.latest_run is None
    queued = supervisor.submit(command(epochs=3))
    assert (queued.state, queued.run_id, queued.stage) == ("queued", None, None)
    assert supervisor.submit(command(epochs=3)) == queued  # an identical resubmit replays
    assert status(supervisor).active == queued
    supervisor.start()
    training = events.wait_for(
        lambda event: progress("running", stage="training")(event) and event.epoch >= 2
    )
    during = status(supervisor)
    assert during.active is not None and during.active.state == "running"
    assert during.active.run_id == training.run_id and during.latest_run is None
    finished = events.wait_for(progress("finished"))
    run_id = finished.run_id
    assert run_id is not None and run_id.startswith("finetune-")
    items = list(events.items)
    assert {request_id for _, request_id, _ in items} == {"tune-1"}
    running = [(at, event) for at, _, event in items if progress("running")(event)]
    # The first running event is published at once; later ones at most once per interval.
    assert running[0][1].stage is None and running[0][1].run_id == run_id
    assert all(
        later - earlier >= 0.28
        for (earlier, _), (later, _) in zip(running, running[1:], strict=False)
    )
    stages = [event.stage for _, event in running]
    order = [None, "selecting", "dataset", "training", "sampling"]
    assert all(order.index(a) <= order.index(b) for a, b in zip(stages, stages[1:], strict=False))
    trained = [event for _, event in running if event.stage == "training"]
    assert trained[0].epochs == 3
    parsed = [event for event in trained if event.update is not None]
    assert parsed, "no progress was parsed from train.log"
    assert all(event.loss is not None and event.eta_seconds is not None for event in parsed)
    assert all(event.epoch <= event.epochs == 3 for event in parsed)
    # The run record comes first, then the job's final state.
    kinds = [type(event) for _, _, event in items[-2:]]
    assert kinds == [FineTuneEvent, FineTuneProgressEvent]
    summary = items[-2][2].run
    assert (summary.run_id, summary.status, summary.epochs, summary.prompt_count) == (
        run_id,
        "finished",
        3,
        1,
    )
    assert len(summary.finetuned_sample_paths) == 3 and len(summary.base_sample_paths) == 3
    assert run_record(library, run_id).status == "finished"
    assert runtime_link(library, run_id).is_symlink()  # a finished run keeps its link
    after = status(supervisor)
    assert after.active is None and after.latest_run == summary
    assert terminal(queue, "tune-1").state == "finished"
    assert supervisor.submit(command(epochs=3)) == finished
    assert supervisor.submit(command("tune-2")).state == "queued"


def test_an_incomplete_corpus_fails_the_job_by_name_before_any_run_is_written(
    library: Library, jobs: Any
) -> None:
    queue, events, supervisors = jobs
    supervisor = supervise(library, queue, events, supervisors)
    supervisor.submit(command(allow_missing=False))
    supervisor.start()
    failed = events.wait_for(progress("failed"))
    assert failed.error.code == "FINETUNE.CORPUS_INCOMPLETE"
    assert failed.error.message.startswith(
        "the corpus is incomplete: 33 of 34 spoken prompts have no kept take"
    )
    assert not any(isinstance(event, FineTuneEvent) for event in events.events)
    assert not (library.layout.root / "derived/finetune" / failed.run_id).exists()
    assert terminal(queue, "tune-1").error == failed.error


# -- refusals -------------------------------------------------------------------------------


def test_submit_refuses_by_name_and_writes_nothing(library: Library, tmp_path: Path) -> None:
    requests = library.layout.root / "provenance/finetune-jobs/requests"

    def refused(queue: FineTuneJobQueue, code: str, text: str, *, retryable: bool) -> None:
        try:
            with pytest.raises(FineTuneJobError) as refusal:
                queue.submit(command())
            error = refusal.value.recording_error
            assert (error.code, error.retryable) == (code, retryable)
            assert text in error.message
            assert queue.active_job() is None and not list(requests.iterdir())
        finally:
            queue.close()

    unconfigured = queue_for(library, runtime_root=None)
    assert not unconfigured.runtime_configured
    refused(unconfigured, "FINETUNE.RUNTIME_UNAVAILABLE", "finetune_runtime_root", retryable=False)
    empty = tmp_path / "empty-runtime"
    empty.mkdir()
    missing_python = queue_for(library, runtime_root=empty)
    assert not missing_python.runtime_configured
    refused(
        missing_python,
        "FINETUNE.RUNTIME_UNAVAILABLE",
        f"no F5 runtime python at {empty / 'bin' / 'python'}",
        retryable=False,
    )
    full_disk = queue_for(library, free_bytes=lambda _path: 1024**3)
    refused(
        full_disk,
        "RECORDING.DISK_SPACE",
        "fine-tuning needs 8.0 GiB free on the library's disk; 1.0 GiB is free",
        retryable=True,
    )
    queue = queue_for(library, free_bytes=lambda _path: FINETUNE_MIN_FREE_BYTES)
    try:
        assert queue.runtime_configured
        assert queue.submit(command()).state == "queued"
        with pytest.raises(FineTuneJobError) as busy:
            queue.submit(command("tune-2"))
        assert busy.value.recording_error.code == "FINETUNE.JOB_ACTIVE"
        assert busy.value.recording_error.retryable
        with pytest.raises(FineTuneJobError) as rebound:
            queue.submit(command(epochs=5))
        assert rebound.value.recording_error.code == "RECORDING.INVALID_MESSAGE"
        with pytest.raises(BlockingIOError):
            queue_for(library)  # one owning service
    finally:
        queue.close()
    with pytest.raises(FineTuneJobError) as closed:
        queue.submit(command())
    assert closed.value.recording_error.code == "RECORDING.GATE_NOT_READY"


def test_a_queued_job_is_cancelled_at_once(library: Library, jobs: Any) -> None:
    queue, events, supervisors = jobs
    supervisor = supervise(library, queue, events, supervisors)
    supervisor.submit(command())
    cancelled = supervisor.cancel(cancel())
    assert (cancelled.state, cancelled.error, cancelled.run_id) == ("cancelled", None, None)
    assert supervisor.cancel(cancel()) == cancelled
    assert supervisor.submit(command()) == cancelled
    assert queue.active_job() is None and queue.claim() is None
    assert terminal(queue, "tune-1").state == "cancelled"
    with pytest.raises(FineTuneJobError) as unknown:
        supervisor.cancel(cancel("tune-unknown"))
    assert unknown.value.recording_error.code == "RECORDING.INVALID_TRANSITION"
    assert events.events == []


# -- cancel, crash, watchdog, restart ------------------------------------------------------


def start_training(library: Library, supervisor: FineTuneSupervisor, events: Events) -> int:
    """Submit a long job and return its worker's group once the trainer runs in it."""
    supervisor.submit(command(epochs=3))
    supervisor.start()
    # A parsed status line: the stub trainer prints it after its signal setup.
    events.wait_for(
        lambda event: progress("running", stage="training")(event) and event.update is not None
    )
    pgid = supervisor.worker_pid
    assert pgid is not None
    wait_until(lambda: any("f5_finetune_launcher.py" in row.command for row in live_group(pgid)))
    return pgid


@needs_ffmpeg
@pytest.mark.parametrize("stubborn", [False, True])
def test_cancel_stops_the_whole_group_and_records_the_run_as_cancelled(
    library: Library, jobs: Any, stubborn: bool
) -> None:
    queue, events, supervisors = jobs
    library.mode(step_seconds=5, ignore_sigterm=stubborn)
    supervisor = supervise(library, queue, events, supervisors)
    pgid = start_training(library, supervisor, events)
    job = queue.active_job()
    assert job is not None and job.run_id is not None and job.worker_pid == pgid
    assert job.runtime_link == str(runtime_link(library, job.run_id))
    asked = time.monotonic()
    reply = supervisor.cancel(cancel())
    assert (reply.state, reply.message, reply.run_id) == ("running", CANCELLING_MESSAGE, job.run_id)
    cancelled = events.wait_for(progress("cancelled"))
    waited = time.monotonic() - asked
    assert cancelled.error is None and cancelled.run_id == job.run_id
    assert live_group(pgid) == []
    if stubborn:
        assert waited >= 1.0  # SIGTERM was ignored; SIGKILL followed after the grace period
    record = run_record(library, job.run_id)
    assert record.status == "failed" and record.error == CANCELLED_BY_OPERATOR
    assert not os.path.lexists(runtime_link(library, job.run_id))
    summary = next(event for event in events.events if isinstance(event, FineTuneEvent))
    assert summary.run.status == "failed" and summary.run.error == CANCELLED_BY_OPERATOR
    assert terminal(queue, "tune-1").state == "cancelled"
    assert status(supervisor).active is None and status(supervisor).latest_run is None
    assert supervisor.cancel(cancel()) == cancelled


@needs_ffmpeg
def test_a_crashed_worker_fails_its_job_and_leaves_no_trainer(library: Library, jobs: Any) -> None:
    queue, events, supervisors = jobs
    library.mode(step_seconds=5)
    supervisor = supervise(library, queue, events, supervisors)
    pgid = start_training(library, supervisor, events)
    job = queue.active_job()
    assert job is not None and job.run_id is not None
    os.kill(pgid, signal.SIGKILL)  # the worker alone; its trainer keeps running
    failed = events.wait_for(progress("failed"))
    assert failed.error.code == "FINETUNE.INTERRUPTED" and failed.error.retryable
    assert "stopped without a result" in failed.error.message
    wait_until(lambda: live_group(pgid) == [], timeout=10)
    record = run_record(library, job.run_id)
    assert record.status == "failed" and record.error == failed.error.message
    assert not os.path.lexists(runtime_link(library, job.run_id))


def test_the_watchdog_stops_a_job_that_runs_too_long(library: Library, jobs: Any) -> None:
    queue, events, supervisors = jobs
    library.mode(probe_seconds=60)
    supervisor = supervise(library, queue, events, supervisors, watchdog=1.0)
    supervisor.submit(command())
    supervisor.start()
    failed = events.wait_for(progress("failed"))
    assert failed.error.code == "FINETUNE.INTERRUPTED"
    assert "ran longer than 1 seconds" in failed.error.message
    assert jobs_module._duration_text(24 * 3600.0) == "24 hours"


def test_a_stopped_supervisor_kills_its_worker_and_the_next_one_marks_the_job_interrupted(
    library: Library,
) -> None:
    library.mode(probe_seconds=60)
    queue = queue_for(library)
    supervisor = FineTuneSupervisor(queue, progress_interval=0.1)
    try:
        supervisor.submit(command())
        supervisor.start()
        wait_until(lambda: supervisor.worker_pid is not None)
        pgid = supervisor.worker_pid
        assert pgid is not None
        probe = str(library.runtime / "bin" / "python")
        wait_until(lambda: any(probe in row.command for row in live_group(pgid)))
        supervisor.close()
        assert live_group(pgid) == []
        job = queue.active_job()
        assert job is not None and job.state == "running"  # left for the next service
    finally:
        supervisor.close()
        queue.close()
    queue = queue_for(library)
    events, outcomes = Events(), Events()
    supervisor = FineTuneSupervisor(queue, on_event=events, on_outcome=outcomes)
    try:
        supervisor.start()
        wait_until(lambda: queue.active_job() is None)
        ended = terminal(queue, "tune-1")
        assert ended.state == "failed" and ended.error is not None
        assert (ended.error.code, ended.error.message) == (
            "FINETUNE.INTERRUPTED",
            INTERRUPTED_BY_RESTART,
        )
        assert supervisor.submit(command()) == finetune_job_event(ended)
        # The job never wrote a run record, so its outcome is its final state alone, published
        # as an outcome (the service keeps it for the app's handshake) keyed by the job.
        outcomes.wait_for(progress("failed"))
        assert [(request_id, event) for _, request_id, event in outcomes.items] == [
            ("tune-1", finetune_job_event(ended))
        ]
        assert events.events == []
    finally:
        supervisor.close()
        queue.close()


@pytest.mark.parametrize("case", ["leader", "leaderless", "stranger", "cancel-requested"])
def test_restart_recovery_kills_only_a_group_that_is_verifiably_this_jobs(
    library: Library, case: str
) -> None:
    layout = library.layout
    queue = queue_for(library)
    started: list[subprocess.Popen[bytes]] = []
    pid: int | None = None
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None
        job = claimed[0]
        assert job.run_id is not None
        _write_run(layout, job.run_id, status="running", finished_minute=None)
        checkpoints = layout.root / "derived/finetune" / job.run_id / "checkpoints"
        checkpoints.mkdir()
        link = runtime_link(library, job.run_id)
        link.parent.mkdir(parents=True)
        os.symlink(checkpoints.resolve(), link)
        queue.record_runtime_link("tune-1", str(link))
        worker_tail = worker_command_marker(layout, "tune-1").split(" ")
        if case == "leaderless":
            # The worker died alone; its trainer (naming the run) still runs in the group.
            leader = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(60)', sys.argv[1]]); print(flush=True)",
                    job.run_id,
                ],
                stdout=subprocess.PIPE,
                start_new_session=True,
            )
            assert leader.stdout is not None
            leader.stdout.readline()
            leader.wait()
            leader.stdout.close()
            pid = leader.pid
            wait_until(lambda: len(live_group(leader.pid)) == 1)
        else:
            tail = worker_tail if case != "stranger" else ["--operation-id", "someone-else"]
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)", *tail],
                start_new_session=True,
            )
            started.append(process)
            pid = process.pid
            wait_until(lambda: len(live_group(process.pid)) == 1)
        queue.record_worker("tune-1", pid)
        if case == "cancel-requested":
            assert queue.request_cancel("tune-1").cancel_requested
    finally:
        queue.close()
    queue = queue_for(library)
    events = Events()
    supervisor = FineTuneSupervisor(queue, on_event=events)
    try:
        supervisor.start()
        wait_until(lambda: queue.active_job() is None)
        ended = terminal(queue, "tune-1")
        record = run_record(library, job.run_id)
        assert record.status == "failed"
        assert not os.path.lexists(link)
        final = events.wait_for(lambda event: isinstance(event, FineTuneProgressEvent))
        summary, published = events.events
        assert isinstance(summary, FineTuneEvent) and summary.run.status == "failed"
        assert summary.run.error == record.error and published == final
        assert final == finetune_job_event(ended) and final.state == ended.state
        if case == "cancel-requested":
            assert ended.state == "cancelled" and record.error == CANCELLED_BY_OPERATOR
        else:
            assert ended.state == "failed" and record.error == INTERRUPTED_BY_RESTART
        assert pid is not None
        group = pid
        if case == "stranger":
            assert [row.pid for row in live_group(group)] == [group]  # not ours: still running
        else:
            wait_until(lambda: live_group(group) == [], timeout=10)
    finally:
        supervisor.close()
        queue.close()
        for process in started:
            if process.poll() is None:
                process.kill()
            process.wait()
        if pid is not None:
            for row in live_group(pid):
                os.kill(row.pid, signal.SIGKILL)


def test_recovery_refuses_to_kill_a_group_that_will_not_stop(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = queue_for(library)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        queue.submit(command())
        queue.claim()
        job = queue.record_worker("tune-1", process.pid)
        marker = worker_command_marker(library.layout, "tune-1")
        ghost = ProcessRow(pid=process.pid, pgid=process.pid, zombie=False, command=marker)
        supervisor = FineTuneSupervisor(queue, processes=lambda: (ghost,))
        monkeypatch.setattr(jobs_module, "_GROUP_STOP_SECONDS", 0.1)
        with pytest.raises(FineTuneJobError, match="did not stop"):
            supervisor._stop_recovered_worker(job)
        assert process.wait(timeout=5) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        queue.close()


def test_a_group_of_zombies_is_not_alive(library: Library) -> None:
    queue = queue_for(library)
    try:
        supervisor = FineTuneSupervisor(queue)
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)

        def zombie() -> bool:
            rows = [row for row in process_table() if row.pid == process.pid]
            return bool(rows) and all(row.zombie for row in rows)

        wait_until(zombie)
        assert supervisor._group_alive(process.pid) is False  # EPERM: only a zombie is left
        process.wait()
        assert supervisor._group_alive(process.pid) is False
        assert supervisor._group_alive(os.getpgrp()) is True
    finally:
        queue.close()


def test_the_worker_kills_its_whole_group_when_the_service_goes_away(library: Library) -> None:
    library.mode(probe_seconds=60)
    queue = queue_for(library)
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None
        job = claimed[0]
        assert job.run_id is not None
    finally:
        queue.close()
    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            FINETUNE_WORKER_MODULE,
            "--worker",
            "--asset-root",
            str(library.layout.root),
            "--operation-id",
            "tune-1",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert worker.stdin is not None and worker.stdout is not None
    try:
        worker.stdin.write(
            recording_frame_bytes(
                FineTuneWorkerRequest(
                    generation="eof-generation",
                    operation_id="tune-1",
                    request_sha256=job.request_sha256,
                    run_id=job.run_id,
                    runtime_root=str(library.runtime),
                )
            )
        )
        worker.stdin.flush()
        prefix = worker.stdout.read(4)
        stage = decode_finetune_worker_message(worker.stdout.read(int.from_bytes(prefix, "big")))
        assert isinstance(stage, FineTuneWorkerStage) and stage.stage == "selecting"
        probe = str(library.runtime / "bin" / "python")
        wait_until(lambda: any(probe in row.command for row in live_group(worker.pid)))
        worker.stdin.close()
        assert worker.wait(timeout=10) == -signal.SIGKILL
        wait_until(lambda: live_group(worker.pid) == [], timeout=10)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        worker.stdout.close()


def test_the_worker_refuses_to_start_outside_its_own_group(tmp_path: Path) -> None:
    missing = tmp_path / "missing-private-root"
    arguments = ["--worker", "--asset-root", str(missing), "--operation-id", "tune-1"]
    base = [sys.executable, "-m", FINETUNE_WORKER_MODULE, *arguments]
    shared = subprocess.run(base, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert shared.returncode == 2
    report = json.loads(shared.stderr)
    assert report["error_code"] == "RECORDING.INVALID_TRANSITION"
    # A leader that fails reports why, then kills its own group (itself included).
    leader = subprocess.run(
        base, capture_output=True, text=True, stdin=subprocess.DEVNULL, start_new_session=True
    )
    assert leader.returncode == -signal.SIGKILL
    assert json.loads(leader.stderr)["error_code"] == "RECORDING.WRITE_FAILED"
    assert str(missing) not in leader.stderr


# -- the supervisor's side of the protocol ---------------------------------------------------

FAKE_WORKER = r"""
import json, os, sys, time
mode = sys.argv[1]
def read_exact(count):
    data = b""
    while len(data) < count:
        chunk = os.read(0, count - len(data))
        if not chunk:
            sys.exit(5)
        data += chunk
    return data
request = json.loads(read_exact(int.from_bytes(read_exact(4), "big")))
def frame(value):
    payload = json.dumps(value, separators=(",", ":")).encode()
    return len(payload).to_bytes(4, "big") + payload
base = {"schema_version": "cole-voice-lab.finetune-worker/v1",
        "generation": request["generation"], "operation_id": request["operation_id"]}
result = dict(base, kind="result", request_sha256=request["request_sha256"],
              run_id=request["run_id"])
frames = {
    "exit": b"",
    "garbage": (5).to_bytes(4, "big") + b"nope!",
    "foreign-stage": frame(dict(base, kind="stage", stage="dataset", generation="other")),
    "foreign-result": frame(dict(result, run_id="another-run")),
    "request-echo": frame(request),
    "success-without-run": frame(dict(base, kind="stage", stage="selecting")) + frame(result),
}[mode]
if mode == "exit":
    sys.exit(3)
os.write(1, frames)
time.sleep(60)
"""


@pytest.mark.parametrize(
    ("mode", "code", "text"),
    [
        ("exit", "FINETUNE.INTERRUPTED", "stopped without a result (exit status 3)"),
        ("garbage", "RECORDING.INVALID_MESSAGE", "invalid message"),
        ("foreign-stage", "RECORDING.INVALID_MESSAGE", "another worker or job"),
        ("foreign-result", "RECORDING.INVALID_MESSAGE", "another request or run"),
        ("request-echo", "RECORDING.INVALID_MESSAGE", "out of turn"),
        ("success-without-run", "FINETUNE.SOURCE_CHANGED", "missing, unreadable or invalid"),
    ],
)
def test_the_supervisor_fails_a_job_whose_worker_breaks_the_protocol(
    library: Library,
    jobs: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    code: str,
    text: str,
) -> None:
    queue, events, supervisors = jobs
    monkeypatch.setattr(
        FineTuneSupervisor,
        "_worker_command",
        lambda self, job: [sys.executable, "-c", FAKE_WORKER, mode],
    )
    supervisor = supervise(library, queue, events, supervisors)
    supervisor.submit(command())
    supervisor.start()
    failed = events.wait_for(progress("failed"), timeout=20)
    assert failed.error.code == code and text in failed.error.message
    job = terminal(queue, "tune-1")
    assert job.worker_pid is not None
    wait_until(lambda: live_group(job.worker_pid) == [], timeout=10)
    assert supervisor.fatal_error is None


def test_a_worker_that_cannot_start_fails_its_job(
    library: Library, jobs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, events, supervisors = jobs
    missing = str(library.layout.root / "no-such-python")
    monkeypatch.setattr(FineTuneSupervisor, "_worker_command", lambda self, job: [missing])
    supervisor = supervise(library, queue, events, supervisors)
    supervisor.submit(command())
    supervisor.start()
    failed = events.wait_for(progress("failed"))
    assert failed.error.code == "FINETUNE.INTERRUPTED"
    assert "could not start" in failed.error.message


def test_a_job_left_queued_without_a_runtime_fails_by_name(library: Library) -> None:
    queue = queue_for(library)
    try:
        queue.submit(command())
    finally:
        queue.close()
    queue = queue_for(library, runtime_root=None)
    events = Events()
    supervisor = FineTuneSupervisor(queue, on_event=events)
    try:
        assert status(supervisor).runtime_configured is False
        supervisor.start()
        failed = events.wait_for(progress("failed"))
        assert failed.error.code == "FINETUNE.RUNTIME_UNAVAILABLE"
    finally:
        supervisor.close()
        queue.close()


def test_supervision_stops_with_a_named_failure_when_its_evidence_is_damaged(
    library: Library,
) -> None:
    queue = queue_for(library)
    failures: list[Any] = []
    supervisor = FineTuneSupervisor(queue, on_failure=failures.append)
    try:
        supervisor.submit(command())
        (queue.directory / "requests" / "tune-1.json").write_bytes(b"damaged")
        supervisor.start()
        wait_until(lambda: bool(failures))
        assert failures[0].code == "RECORDING.WRITE_FAILED" and failures[0].retryable
        assert supervisor.fatal_error is not None and supervisor.worker_pid is None
        job = queue.active_job()
        assert job is not None and job.state == "queued"
    finally:
        supervisor.close()
        queue.close()


def test_a_supervisor_that_does_not_stop_is_reported(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    queue = queue_for(library)
    supervisor = FineTuneSupervisor(queue)
    stuck = SimpleNamespace(ident=1, join=lambda timeout: None, is_alive=lambda: True)
    monkeypatch.setattr(supervisor, "_thread", stuck)
    try:
        with pytest.raises(FineTuneJobError, match="did not stop"):
            supervisor.close()
        assert supervisor._stop.is_set() and queue.wake.is_set()
    finally:
        queue.close()


# -- the queue's evidence ---------------------------------------------------------------------


def test_queue_evidence_is_strict_about_its_own_state(library: Library) -> None:
    queue = queue_for(library)
    try:
        with pytest.raises(SecurityError, match="no longer the active job"):
            queue.record_worker("tune-1", 5)
        queue.submit(command())
        with pytest.raises(SecurityError, match="not running"):
            queue.record_worker("tune-1", 5)
        with pytest.raises(SecurityError, match="not running"):
            queue.finish("tune-1", "failed")
        claimed = queue.claim()
        assert claimed is not None
        with pytest.raises(SecurityError, match="no worker owns"):
            queue.claim()
        # An intent filed under another operation is refused.
        intent_bytes = (queue.directory / "requests" / "tune-1.json").read_bytes()
        (queue.directory / "requests" / "tune-9.json").write_bytes(intent_bytes)
        (queue.directory / "requests" / "tune-9.json").chmod(0o600)
        with pytest.raises(SecurityError, match="another operation"):
            queue.intent("tune-9")
    finally:
        queue.close()


def test_a_changed_intent_or_terminal_record_is_refused(library: Library) -> None:
    queue = queue_for(library)
    try:
        queue.submit(command())
        other = jobs_module.FineTuneJobIntent(
            command=command(epochs=7),
            subject_id=SUBJECT,
            request_sha256=canonical_sha256(command(epochs=7)),
            queued_at=utc_now(),
        )
        write_recording_evidence(queue.directory / "requests" / "tune-1.json", other)
        with pytest.raises(SecurityError, match="intent changed"):
            queue.claim()
        active = queue.active_job()
        assert active is not None
        # A terminal record that is not terminal is refused.
        write_recording_evidence(queue.directory / "terminal" / "tune-1.json", active)
        with pytest.raises(SecurityError, match="another operation or state"):
            queue.submit(command())
    finally:
        queue.close()


def test_an_orphan_intent_is_reused_only_for_the_same_request(library: Library) -> None:
    queue = queue_for(library)
    try:
        request_sha256 = canonical_sha256(command())
        orphan = queue._new_intent(command(), request_sha256)  # written, then the service died
        with pytest.raises(FineTuneJobError) as rebound:
            queue.submit(command(epochs=9))
        assert rebound.value.recording_error.code == "RECORDING.INVALID_MESSAGE"
        job = queue.submit(command())
        assert job.queued_at == orphan.queued_at
    finally:
        queue.close()


def test_recovery_finishes_a_restart_that_fell_between_terminal_and_head(
    library: Library,
) -> None:
    queue = queue_for(library)
    try:
        assert queue.recover(lambda job: None) is None  # nothing to recover
        queued = queue.submit(command())
        assert queue.recover(lambda job: None) is None  # a queued job simply starts later
        ended = FineTuneJob.model_validate(
            queued.model_dump() | {"state": "cancelled", "finished_at": utc_now()}
        )
        path = queue.directory / "terminal" / "tune-1.json"
        write_recording_evidence(path, ended)
        assert queue.recover(lambda job: None) is None
        assert queue.active_job() is None
        # The same boundary with a terminal record of another request is refused.
        queue.submit(command("tune-2"))
        forged = ended.model_copy(update={"operation_id": "tune-2", "request_sha256": "0" * 64})
        write_recording_evidence(queue.directory / "terminal" / "tune-2.json", forged)
        with pytest.raises(SecurityError, match="binds another request"):
            queue.recover(lambda job: None)
    finally:
        queue.close()


@pytest.mark.parametrize("damage", ["corrupt-run", "stuck-link", "both"])
def test_a_run_record_that_cannot_be_updated_fails_the_job_by_name(
    library: Library, damage: str
) -> None:
    layout = library.layout
    queue = queue_for(library)
    link_parent: Path | None = None
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None
        run_id = claimed[0].run_id
        assert run_id is not None
        run = _write_run(layout, run_id, status="running", finished_minute=None)
        link = runtime_link(library, run_id)
        link.parent.mkdir(parents=True)
        os.symlink((layout.root / run.run_root / "checkpoints").resolve(), link)
        queue.record_runtime_link("tune-1", str(link))
        if damage in {"corrupt-run", "both"}:
            (layout.root / run.run_root / "run.json").write_text("{damaged")
        if damage in {"stuck-link", "both"}:
            link_parent = link.parent
            link_parent.chmod(0o500)  # the link cannot be removed
        ended = queue.finish("tune-1", "cancelled")
        assert ended.state == "failed" and ended.error is not None and ended.message is None
        if damage == "stuck-link":
            assert ended.error.code == "RECORDING.WRITE_FAILED"
            assert ended.error.message.startswith(
                f"the fine-tune stopped but the runtime link {link} was not released"
            )
            assert run_record(library, run_id).status == "failed"  # rewritten all the same
        else:
            # The first failure is reported; the link is released even so, unless it cannot be.
            assert ended.error.code == "FINETUNE.SOURCE_CHANGED"
            assert ended.error.message.startswith(
                "the fine-tune stopped but its run record was not updated"
            )
            assert os.path.lexists(link) == (damage == "both")
    finally:
        if link_parent is not None:
            link_parent.chmod(0o700)
        queue.close()


def test_queue_construction_fails_closed_and_releases_its_lock(library: Library) -> None:
    queue = queue_for(library)
    queue.close()
    head = library.layout.root / "provenance/finetune-jobs/head.json"
    head.write_bytes(b"not canonical evidence")
    before = len(os.listdir("/dev/fd"))
    for _ in range(3):
        with pytest.raises(Exception, match="invalid versioned"):
            queue_for(library)
    assert len(os.listdir("/dev/fd")) == before
    head.unlink()
    owner = library.layout.root / "provenance/finetune-jobs/.owner.lock"
    owner.chmod(0o644)
    with pytest.raises(SecurityError, match="owner lock"):
        queue_for(library)


def test_job_and_head_contracts_hold_their_invariants() -> None:
    now = utc_now()
    base: dict[str, Any] = {
        "operation_id": "tune-1",
        "request_sha256": "0" * 64,
        "queued_at": now,
    }
    running = base | {"state": "running", "run_id": "run-1", "started_at": now}
    for invalid in (
        base | {"run_id": "run-1"},  # a run without a start time
        base | {"run_id": "run-1", "started_at": now},  # queued but started
        base | {"state": "running"},  # running but never started
        base | {"state": "finished", "finished_at": now},  # finished without a run
        base | {"worker_pid": 7},  # a worker before the job started
        running | {"finished_at": now},  # an active job with a finish time
        running | {"state": "failed", "finished_at": now},  # failed without an error
        base | {"cancel_requested": True},  # only a running job waits for cancellation
        base | {"message": "A note"},  # only a cancelled job carries a note
        running
        | {
            "state": "failed",
            "finished_at": now,
            "error": {"code": "FINETUNE.INTERRUPTED", "message": "x", "retryable": True},
            "message": "A note",
        },
    ):
        with pytest.raises(ValueError):
            FineTuneJob.model_validate(invalid)
    with pytest.raises(ValueError, match="queued or running"):
        FineTuneJobHead(
            job=FineTuneJob.model_validate(base | {"state": "cancelled", "finished_at": now})
        )
    with pytest.raises(ValueError, match="exact command"):
        jobs_module.FineTuneJobIntent(
            command=command(), subject_id=SUBJECT, request_sha256="0" * 64, queued_at=now
        )
    job = FineTuneJob.model_validate(running | {"cancel_requested": True})
    assert job.run_root == "derived/finetune/run-1"
    assert finetune_job_event(job).message == CANCELLING_MESSAGE
    with pytest.raises(FineTuneJobError, match="bounded frame"):
        decode_finetune_worker_message(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        decode_finetune_worker_message(b'{"kind": "unknown"}')


# -- progress ----------------------------------------------------------------------------------


def test_training_progress_is_the_last_complete_trainer_status() -> None:
    log = (
        "lab launcher: workers=2\n"
        "Epoch 1/120:   0%|          | 0/9 [00:00<?, ?update/s]"
        "\rEpoch 1/120:  11%|█         | 1/9 [00:06<00:48,  6.03s/update, loss=0.9, update=1]"
        "\x1b[A\rEpoch 3/120:  89%|████████▉ | 8/9 [00:50<00:06,  6.03s/update, loss=0.566, "
        "update=1079]\rSaved last checkpoint at update 1080\n"
    )
    assert parse_training_progress(log) == FineTuneTrainingProgress(
        epoch=3, epochs=120, update=1079, loss=0.566, eta_seconds=6 + 117 * 56
    )
    assert parse_training_progress("Epoch 2/3: [1:00:00<0:30:00, loss=nan]") == (
        FineTuneTrainingProgress(epoch=2, epochs=3, update=None, loss=None, eta_seconds=1800 + 5400)
    )
    assert parse_training_progress("Epoch 2/3: loss=fast") == FineTuneTrainingProgress(
        epoch=2, epochs=3, update=None, loss=None, eta_seconds=None
    )
    # A pass outside its total is ignored in favour of the last sensible status.
    assert parse_training_progress("Epoch 1/2: loss=inf\rEpoch 3/2: loss=0.1") == (
        FineTuneTrainingProgress(epoch=1, epochs=2, update=None, loss=None, eta_seconds=None)
    )
    assert parse_training_progress("Epoch 1/2: [00:01<00:01]") == FineTuneTrainingProgress(
        epoch=1, epochs=2, update=None, loss=None, eta_seconds=1 + 1 * 2
    )
    assert parse_training_progress("packed 34 rows\n") is None
    assert parse_training_progress("Epoch 0/2") is None


def test_the_log_tail_parses_only_new_complete_fragments(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    tail = FineTuneLogTail(layout, "run-1")
    assert tail.read() is None  # no log yet
    log = layout.root / "derived/finetune/run-1/train.log"
    log.parent.mkdir(parents=True)
    log.write_text("packed 3 rows\n")
    assert tail.read() is None
    with log.open("a") as stream:
        stream.write("\rEpoch 1/4: [00:01<00:01, loss=0.5, update=1]\rEpoch 2/4: [00:0")
    assert tail.read() == FineTuneTrainingProgress(1, 4, 1, 0.5, 1 + 3 * 2)
    assert tail.read() is None  # nothing new
    with log.open("a") as stream:
        stream.write("1<00:01, loss=0.4, update=3]")
    assert tail.read() == FineTuneTrainingProgress(2, 4, 3, 0.4, 1 + 2 * 2)
    log.write_text("\rEpoch 4/4: [00:02<00:00, loss=0.1, update=8]\n")  # rotated: shorter
    assert tail.read() == FineTuneTrainingProgress(4, 4, 8, 0.1, 0)
    with log.open("a") as stream:
        stream.write("x" * 70_000 + "\rEpoch 3/4: loss=0.2]")
    assert tail.read() == FineTuneTrainingProgress(3, 4, None, 0.2, None)
    log.unlink()
    log.symlink_to(layout.root / "elsewhere.log")
    with pytest.raises(SecurityError):
        tail.read()


def test_progress_is_published_at_most_once_per_interval() -> None:
    now = [100.0]
    tracker = FineTuneProgressTracker("tune-1", "run-1", 120, interval=5.0, clock=lambda: now[0])
    first = tracker.due(force=True)
    assert first is not None and first.stage is None and first.run_id == "run-1"
    assert tracker.due(force=True) is None  # unchanged
    tracker.training_progress(FineTuneTrainingProgress(2, 120, 5, 0.5, 60))
    assert not tracker.training and tracker.due() is None  # not training yet: ignored
    tracker.stage("selecting")
    tracker.stage("selecting")
    now[0] += 1
    assert tracker.due() is None  # held back
    tracker.stage("training")
    now[0] += 4
    held = tracker.due()
    assert held is not None and (held.stage, held.epoch, held.epochs) == ("training", 1, 120)
    tracker.training_progress(FineTuneTrainingProgress(2, 120, 5, 0.5, 60))
    now[0] += 4
    assert tracker.due() is None
    now[0] += 1
    latest = tracker.due()
    assert latest is not None
    assert (latest.epoch, latest.update, latest.loss, latest.eta_seconds) == (2, 5, 0.5, 60)
    tracker.stage("sampling")
    assert tracker.current.epoch is None and tracker.current.epochs is None


# -- the worker in this process ----------------------------------------------------------------


def claimed_request(queue: FineTuneJobQueue, runtime: Path, **changes: Any) -> Any:
    """Queue and start a job the way the supervisor does; return its worker request."""
    queue.submit(command(**changes))
    claimed = queue.claim()
    assert claimed is not None
    job = claimed[0]
    assert job.run_id is not None
    return FineTuneWorkerRequest(
        generation="worker-generation",
        operation_id=job.operation_id,
        request_sha256=job.request_sha256,
        run_id=job.run_id,
        runtime_root=str(runtime),
    )


def test_the_worker_names_every_failure(
    library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = library.layout
    queue = queue_for(library)
    stages: list[FineTuneWorkerStage] = []
    try:
        request = claimed_request(queue, tmp_path / "no-runtime")

        def run(value: FineTuneWorkerRequest = request, **options: Any) -> FineTuneWorkerReply:
            return run_finetune_worker_request(layout, value, emit=stages.append, **options)

        reply = run()
        assert reply.error is not None and reply.error.code == "FINETUNE.RUNTIME_UNAVAILABLE"
        assert [stage.stage for stage in stages] == ["selecting"]
        assert (reply.generation, reply.run_id) == ("worker-generation", request.run_id)
        changed = run(request.model_copy(update={"request_sha256": "0" * 64}))
        assert changed.error is not None and changed.error.code == "RECORDING.SOURCE_CHANGED"
        missing = run(request.model_copy(update={"operation_id": "tune-unknown"}))
        assert missing.error is not None and missing.error.code == "RECORDING.WRITE_FAILED"
        assert missing.error.retryable
        (layout.root / "derived/finetune" / request.run_id).mkdir(parents=True)
        exists = run()
        assert exists.error is not None and exists.error.code == "FINETUNE.RUN_EXISTS"
        (layout.root / "derived/finetune" / request.run_id).rmdir()

        def failing(error: BaseException) -> None:
            def raise_error(*args: Any, **kwargs: Any) -> None:
                raise error

            monkeypatch.setattr(jobs_module, "run_finetune_pipeline", raise_error)

        failing(SecurityError("synthetic unsafe path"))
        assert run().error.code == "RECORDING.SOURCE_CHANGED"
        failing(RuntimeError("synthetic bug"))
        unexpected = run().error
        assert unexpected.code == "RECORDING.WRITE_FAILED"
        assert "unexpectedly: RuntimeError: synthetic bug" in unexpected.message
        monkeypatch.undo()
        with open_corpus_store(layout, create=False) as corpus:
            snapshot_path = layout.root / corpus.active_catalog(SUBJECT)[1].snapshot_path
        original = snapshot_path.read_bytes()
        snapshot_path.chmod(0o600)
        snapshot_path.write_bytes(original.replace(b"pilot", b"pilox", 1))
        altered = run().error
        assert altered.code == "CORPUS.SNAPSHOT_MISMATCH"
        assert altered.message.startswith("a catalog snapshot fine-tuning needs is unusable")
        snapshot_path.write_bytes(original)

        def unavailable(*args: Any, **kwargs: Any) -> None:
            raise CorpusStoreError("CORPUS.STORE_UNAVAILABLE", "synthetic store failure")

        monkeypatch.setattr(jobs_module, "open_corpus_store", unavailable)
        assert run().error.code == "RECORDING.GATE_NOT_READY"
        monkeypatch.undo()
        queue.finish("tune-1", "failed", reply.error)
    finally:
        queue.close()
    stranger = FineTuneJobQueue(
        layout, subject_id="nobody", runtime_root=library.runtime, free_bytes=plenty
    )
    try:
        reply = run_finetune_worker_request(
            layout,
            claimed_request(stranger, library.runtime, operation_id="tune-2"),
            emit=stages.append,
        )
        assert reply.error is not None and reply.error.code == "CORPUS.NO_ACTIVE_CATALOG"
    finally:
        stranger.close()
    bare = AssetLayout.initialize(tmp_path.resolve() / "bare")
    bare_queue = FineTuneJobQueue(
        bare, subject_id=SUBJECT, runtime_root=library.runtime, free_bytes=plenty
    )
    try:
        reply = run_finetune_worker_request(
            bare, claimed_request(bare_queue, library.runtime), emit=stages.append
        )
        assert reply.error is not None and reply.error.code == "CORPUS.STORE_MISSING"
    finally:
        bare_queue.close()


def test_the_worker_retries_a_selection_that_overlaps_a_save(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = queue_for(library)
    calls: list[int] = []

    def busy_twice(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) <= 2:
            try:
                raise RecordingMetadataBusyError("synthetic commit in progress")
            except RecordingMetadataBusyError as busy:
                raise FineTuneError(
                    "FINETUNE.CORPUS_PROJECTION_FAILED", "synthetic busy selection"
                ) from busy

    try:
        request = claimed_request(queue, library.runtime, epochs=4)
        monkeypatch.setattr(jobs_module, "run_finetune_pipeline", busy_twice)
        reply = run_finetune_worker_request(library.layout, request, emit=lambda stage: None)
        assert reply.error is None and len(calls) == 3
        calls.clear()
        reply = run_finetune_worker_request(
            library.layout, request, emit=lambda stage: None, metadata_retry_seconds=0
        )
        assert reply.error is not None
        assert reply.error.code == "FINETUNE.CORPUS_PROJECTION_FAILED" and len(calls) == 1
    finally:
        queue.close()


@pytest.mark.parametrize(
    "fault", ["reply", "eof", "second-request", "foreign", "thread-failure", "stalled"]
)
def test_worker_main_serves_exactly_one_request_and_treats_eof_as_parent_loss(
    library: Library, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import queue as threaded_queue
    import select
    from types import SimpleNamespace

    from cole_voice_lab.recording_service import RecordingFrameDecoder

    queue = queue_for(library)
    try:
        request = claimed_request(queue, library.runtime)
    finally:
        queue.close()
    request = request.model_copy(update={"request_sha256": "0" * 64})  # fails fast
    parent_read, worker_write = os.pipe()
    worker_read, parent_write = os.pipe()
    worker_input = os.fdopen(worker_read, "rb", buffering=0)
    worker_output = os.fdopen(worker_write, "wb", buffering=0)
    monkeypatch.setattr(
        jobs_module, "sys", SimpleNamespace(stdin=worker_input, stdout=worker_output)
    )
    if fault == "stalled":
        # The service stopped reading: the pipe is full before the reply is due.
        os.set_blocking(worker_write, False)
        try:
            while True:
                os.write(worker_write, b"x" * 4096)
        except BlockingIOError:
            pass
        monkeypatch.setattr(jobs_module, "_SEND_DEADLINE_SECONDS", 0.2)
    if fault == "thread-failure":

        def explode(*args: Any, **kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(jobs_module, "run_finetune_worker_request", explode)
    killed = threading.Event()
    stopped: threaded_queue.Queue[Any] = threaded_queue.Queue(1)

    def run() -> None:
        try:
            stopped.put_nowait(
                jobs_module._worker_main(library.layout, "tune-1", kill_group=killed.set)
            )
        except BaseException as error:
            stopped.put_nowait(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        payload = recording_frame_bytes(request)
        if fault == "foreign":
            payload = recording_frame_bytes(request.model_copy(update={"operation_id": "other"}))
        if fault == "second-request":
            payload += payload
        if fault != "eof":
            os.write(parent_write, payload)
        if fault == "reply":
            decoder = RecordingFrameDecoder()
            reply = None
            deadline = time.monotonic() + 10
            while reply is None and time.monotonic() < deadline:
                if select.select([parent_read], [], [], 0.05)[0]:
                    for frame in decoder.feed(os.read(parent_read, 8192)):
                        reply = decode_finetune_worker_message(frame)
            assert isinstance(reply, FineTuneWorkerReply)
            assert reply.error is not None and reply.error.code == "RECORDING.SOURCE_CHANGED"
        if fault == "eof":
            os.close(parent_write)
            parent_write = -1
        result = stopped.get(timeout=10)
        if fault == "reply":
            assert result == 0
        elif fault == "eof":
            assert result == 1 and killed.is_set()
        else:
            assert isinstance(result, FineTuneJobError) and not killed.is_set()
    finally:
        if parent_write >= 0:
            os.close(parent_write)
        thread.join(timeout=2)
        worker_input.close()
        worker_output.close()
        os.close(parent_read)


def test_frames_after_a_result_are_refused(library: Library) -> None:
    from cole_voice_lab.recording_service import RecordingFrameDecoder

    queue = queue_for(library)
    try:
        request = claimed_request(queue, library.runtime)
        job = queue.active_job()
        assert job is not None
        supervisor = FineTuneSupervisor(queue)
        tracker = FineTuneProgressTracker("tune-1", request.run_id, 3)
        result = FineTuneWorkerReply(
            generation=request.generation,
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
            run_id=request.run_id,
        )
        stage = FineTuneWorkerStage(
            generation=request.generation,
            operation_id=request.operation_id,
            stage="dataset",
            runtime_link=str(runtime_link(library, request.run_id)),
        )
        decoder = RecordingFrameDecoder()
        chunk = recording_frame_bytes(stage) + recording_frame_bytes(result)
        assert supervisor._frames(decoder, chunk, job, request, tracker) == result
        assert tracker.current.stage == "dataset"
        recorded = queue.active_job()
        assert recorded is not None and recorded.runtime_link == stage.runtime_link
        # The same link again is not rewritten; a second result in one read is refused.
        assert (
            supervisor._frames(decoder, recording_frame_bytes(stage), recorded, request, tracker)
            is None
        )
        with pytest.raises(FineTuneJobError, match="out of turn"):
            supervisor._frames(
                decoder, recording_frame_bytes(result) * 2, recorded, request, tracker
            )
    finally:
        queue.close()


def test_queue_bookkeeping_edges(library: Library) -> None:
    from datetime import UTC, datetime

    fixed = datetime(2026, 9, 16, 12, tzinfo=UTC)
    base = "finetune-20260916t120000z"
    (library.layout.root / "derived/finetune" / base).mkdir(parents=True)
    (library.layout.root / "derived/finetune" / f"{base}-2").mkdir()
    queue = queue_for(library, clock=lambda: fixed)
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None and claimed[0].run_id == f"{base}-3"
        first = queue.request_cancel("tune-1")
        assert first.cancel_requested and queue.cancel_requested("tune-1")
        assert queue.request_cancel("tune-1") == first  # asked twice: written once
    finally:
        queue.close()
        queue.close()  # closing twice is harmless
    stopped: list[FineTuneJob] = []
    queue = queue_for(library)
    try:
        # A new owner learns of the cancellation from the durable record alone.
        assert not queue.cancel_requested("tune-1")
        ended = queue.recover(stopped.append)
        assert ended is not None and ended.state == "cancelled"
        assert stopped == []  # no worker was recorded, so there was nothing to stop
    finally:
        queue.close()


def test_process_table_reads_every_row_and_skips_what_it_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert any(row.pid == os.getpid() and not row.zombie for row in process_table())

    def fake_ps(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args, 0, stdout="  7   7 Z\n  8\n  9   7 S  sleep 5\n")

    monkeypatch.setattr(jobs_module.subprocess, "run", fake_ps)
    assert process_table() == (
        ProcessRow(pid=7, pgid=7, zombie=True, command=""),
        ProcessRow(pid=9, pgid=7, zombie=False, command="sleep 5"),
    )


def test_live_progress_is_used_only_for_its_own_job(library: Library) -> None:
    queue = queue_for(library)
    try:
        queue.submit(command())
        queue.claim()
        job = queue.active_job()
        assert job is not None and job.run_id is not None
        supervisor = FineTuneSupervisor(queue)
        supervisor._tracker = FineTuneProgressTracker("tune-other", "run-other", 3)
        assert supervisor._active_event(job) == finetune_job_event(job)
        supervisor._tracker = FineTuneProgressTracker("tune-1", job.run_id, 3)
        supervisor._tracker.stage("dataset")
        assert supervisor._active_event(job).stage == "dataset"
    finally:
        queue.close()


def test_ending_a_job_publishes_what_its_run_record_supports(library: Library) -> None:
    layout = library.layout
    queue = queue_for(library)
    events = Events()
    supervisor = FineTuneSupervisor(queue, on_event=events)
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None
        job = claimed[0]
        assert job.run_id is not None
        # The worker said it finished, but its run record still says running.
        _write_run(layout, job.run_id, status="running", finished_minute=None)
        supervisor._end_job(job, "finished", None)
        summary, final = events.events
        assert final.state == "failed" and final.error.code == "FINETUNE.TRAINING_FAILED"
        assert "reported success but" in final.error.message
        assert isinstance(summary, FineTuneEvent) and summary.run.status == "failed"
        # A failed job whose run cannot be summarized still publishes its final state.
        queue.submit(command("tune-2"))
        claimed = queue.claim()
        assert claimed is not None
        second = claimed[0]
        assert second.run_id is not None and second.run_id != job.run_id
        run = _write_run(layout, second.run_id, status="running", finished_minute=None)
        (layout.root / run.run_root / "dataset" / "dataset.json").write_text("{damaged")
        failure = jobs_module._interrupted("synthetic failure")
        supervisor._end_job(second, "failed", failure)
        assert [type(event) for event in events.events[2:]] == [FineTuneProgressEvent]
        assert events.events[-1].error == failure
    finally:
        queue.close()


def test_a_worker_gone_before_its_request_is_a_failed_job(library: Library) -> None:
    from types import SimpleNamespace

    queue = queue_for(library)
    try:
        request = claimed_request(queue, library.runtime)
        job = queue.active_job()
        assert job is not None
    finally:
        queue.close()
    supervisor = FineTuneSupervisor(queue)
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    os.close(stdin_read)  # the worker never reads its request
    os.close(stdout_write)  # and exits without a result
    process = SimpleNamespace(
        stdin=os.fdopen(stdin_write, "wb", buffering=0),
        stdout=os.fdopen(stdout_read, "rb", buffering=0),
        pid=os.getpid(),
        poll=lambda: 7,
        wait=lambda: 7,
    )
    try:
        tracker = FineTuneProgressTracker("tune-1", request.run_id, 3)
        outcome = supervisor._converse(process, job, request, tracker)  # type: ignore[arg-type]
        assert outcome is not None and outcome.state == "failed"
        assert outcome.error.message.endswith("stopped without a result (exit status 7)")
    finally:
        process.stdin.close()
        process.stdout.close()


def test_a_worker_that_will_not_take_its_request_is_killed(
    library: Library, jobs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, events, supervisors = jobs
    monkeypatch.setattr(jobs_module, "_SEND_DEADLINE_SECONDS", -1.0)
    monkeypatch.setattr(
        FineTuneSupervisor,
        "_worker_command",
        lambda self, job: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    supervisor = supervise(library, queue, events, supervisors)
    supervisor.submit(command())
    supervisor.start()
    failed = events.wait_for(progress("failed"), timeout=20)
    assert failed.error.message == "the fine-tune worker did not accept its request"


def test_supervision_without_a_failure_listener_still_records_why_it_stopped(
    library: Library,
) -> None:
    queue = queue_for(library)
    supervisor = FineTuneSupervisor(queue)
    try:
        supervisor.submit(command())
        (queue.directory / "requests" / "tune-1.json").write_bytes(b"damaged")
        supervisor.start()
        wait_until(lambda: supervisor.fatal_error is not None)
    finally:
        supervisor.close()
        queue.close()


# -- proofread fixes: recovery, supervision, sampling, pinned catalog ----------------------------


def leave_running_job(library: Library, *, run_record_written: bool = True) -> FineTuneJob:
    """A job a stopped service left running: claimed, its worker recorded as a gone process."""
    queue = queue_for(library)
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None
        job = claimed[0]
        assert job.run_id is not None
        if run_record_written:
            _write_run(library.layout, job.run_id, status="running", finished_minute=None)
        gone = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        gone.wait()
        return queue.record_worker("tune-1", gone.pid)
    finally:
        queue.close()


def test_status_never_waits_for_recovery_and_names_the_job_being_checked(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = leave_running_job(library)
    entered, release = threading.Event(), threading.Event()
    original = jobs_module.mark_finetune_run_failed

    def slow_rewrite(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(30)  # hashing a real run's checkpoints takes seconds
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs_module, "mark_finetune_run_failed", slow_rewrite)
    queue = queue_for(library)
    outcomes = Events()
    supervisor = FineTuneSupervisor(queue, on_outcome=outcomes)
    try:
        supervisor.start()
        assert entered.wait(30)
        asked = time.monotonic()
        during = status(supervisor)
        assert time.monotonic() - asked < 2
        assert during.active is not None
        assert (during.active.operation_id, during.active.state, during.active.message) == (
            "tune-1",
            "running",
            RECOVERING_MESSAGE,
        )
        assert during.active.run_id == job.run_id and outcomes.events == []
        release.set()
        final = outcomes.wait_for(progress("failed"))
        assert final.error.code == "FINETUNE.INTERRUPTED"
        summary = outcomes.events[0]
        assert isinstance(summary, FineTuneEvent) and summary.run.run_id == job.run_id
        assert [request_id for _, request_id, _ in outcomes.items] == ["tune-1", "tune-1"]
        after = status(supervisor)
        assert after.active is None and after.latest_run is None
    finally:
        release.set()
        supervisor.close()
        queue.close()


def test_a_job_recovered_while_writing_samples_is_a_model_without_samples(
    library: Library,
) -> None:
    queue = queue_for(library)
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None
        run_id = claimed[0].run_id
        assert run_id is not None
        _write_run(library.layout, run_id, samples=False)  # training finished, no samples yet
    finally:
        queue.close()
    queue = queue_for(library)
    outcomes = Events()
    supervisor = FineTuneSupervisor(queue, on_outcome=outcomes)
    try:
        supervisor.start()
        final = outcomes.wait_for(progress("failed"))
        assert final.error.code == "FINETUNE.INTERRUPTED"
        assert final.error.message == (
            f"the model {run_id} was trained, but its listening samples are missing or "
            f"incomplete: {INTERRUPTED_BY_RESTART}"
        )
        summary = outcomes.events[0]
        assert isinstance(summary, FineTuneEvent) and summary.run.status == "finished"
        assert summary.run.finetuned_sample_paths == () == summary.run.base_sample_paths
        assert run_record(library, run_id).status == "finished"  # a usable model
        assert status(supervisor).latest_run == summary.run
    finally:
        supervisor.close()
        queue.close()


@needs_ffmpeg
@pytest.mark.parametrize("ending", ["cancelled", "failed"])
def test_a_job_stopped_while_writing_samples_keeps_its_trained_model(
    library: Library, jobs: Any, ending: str
) -> None:
    queue, events, supervisors = jobs
    if ending == "cancelled":
        library.mode(sample_seconds=60)
    else:
        library.mode(fail_sampling=True)
    supervisor = supervise(library, queue, events, supervisors)
    supervisor.submit(command(epochs=1))
    supervisor.start()
    if ending == "cancelled":
        events.wait_for(progress("running", stage="sampling"))
        assert supervisor.cancel(cancel()).message == CANCELLING_MESSAGE
    final = events.wait_for(progress(ending))
    run_id = final.run_id
    assert run_id is not None
    trained = f"the model {run_id} was trained, but its listening samples are missing or incomplete"
    if ending == "cancelled":
        assert final.error is None
        assert final.message == f"Fine-tune cancelled after training: {trained}"
        assert terminal(queue, "tune-1").message == final.message
        assert supervisor.cancel(cancel()) == final  # the note is part of the durable outcome
    else:
        assert final.error.code == "FINETUNE.RUNTIME_FAILED"
        assert final.error.message.startswith(f"{trained}: sample synthesis for finetuned failed")
    summary = next(event for event in events.events if isinstance(event, FineTuneEvent))
    assert summary.run.status == "finished" and summary.run.finetuned_sample_paths == ()
    assert run_record(library, run_id).status == "finished"
    assert not os.path.lexists(runtime_link(library, run_id))  # released all the same
    assert status(supervisor).latest_run == summary.run


def test_failed_supervision_refuses_every_request_by_name(library: Library) -> None:
    queue = queue_for(library)
    failures: list[Any] = []
    supervisor = FineTuneSupervisor(queue, on_failure=failures.append)
    try:
        supervisor.submit(command())
        (queue.directory / "requests" / "tune-1.json").write_bytes(b"damaged")
        supervisor.start()
        wait_until(lambda: bool(failures))
        assert failures[0].message == SUPERVISION_FAILURE
        for call in (
            lambda: supervisor.submit(command("tune-2")),
            lambda: supervisor.cancel(cancel()),
            lambda: status(supervisor),
        ):
            with pytest.raises(FineTuneJobError) as refused:
                call()
            error = refused.value.recording_error
            assert (error.code, error.message) == ("RECORDING.GATE_NOT_READY", SUPERVISION_STOPPED)
        assert queue.active_job() is not None and queue.active_job().operation_id == "tune-1"
    finally:
        supervisor.close()
        queue.close()


def test_a_worker_group_that_will_not_stop_stops_supervision(
    library: Library, jobs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, events, supervisors = jobs
    failures: list[Any] = []
    monkeypatch.setattr(
        FineTuneSupervisor,
        "_worker_command",
        lambda self, job: [sys.executable, "-c", FAKE_WORKER, "exit"],
    )
    monkeypatch.setattr(jobs_module, "_GROUP_STOP_SECONDS", 0.2)
    monkeypatch.setattr(FineTuneSupervisor, "_group_alive", lambda self, pgid: True)
    supervisor = supervise(library, queue, events, supervisors, on_failure=failures.append)
    supervisor.submit(command())
    supervisor.start()
    wait_until(lambda: bool(failures), timeout=20)
    assert isinstance(supervisor.fatal_error, FineTuneJobError)
    assert "did not stop" in str(supervisor.fatal_error)
    assert supervisor.worker_pid is None
    job = queue.active_job()
    assert job is not None and job.state == "running"  # left for the next service
    assert events.wait_for(progress("running")).stage is None
    assert not any(progress("failed")(event) for event in events.events)
    with pytest.raises(FineTuneJobError, match="supervision stopped"):
        supervisor.submit(command("tune-2"))  # no second job while the old group lives


def test_a_failing_worker_kills_its_own_group_only_when_it_leads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    killed: list[bool] = []
    arguments = ["--worker", "--asset-root", str(tmp_path / "missing"), "--operation-id", "t-1"]
    monkeypatch.setattr(jobs_module.os, "getpgrp", lambda: os.getpid())
    assert jobs_module.main(arguments, kill_group=lambda: killed.append(True)) == 2
    assert killed == [True]
    assert json.loads(capsys.readouterr().err)["error_code"] == "RECORDING.WRITE_FAILED"

    def broken(*args: Any, **kwargs: Any) -> int:
        raise KeyboardInterrupt  # any failure at all, not only named ones

    monkeypatch.setattr(jobs_module, "_worker_main", broken)
    monkeypatch.setattr(jobs_module.AssetLayout, "validate", lambda root: root)
    assert jobs_module.main(arguments, kill_group=lambda: killed.append(True)) == 2
    assert killed == [True, True]
    capsys.readouterr()
    monkeypatch.setattr(jobs_module.os, "getpgrp", lambda: os.getpid() + 1)
    assert jobs_module.main(arguments, kill_group=lambda: killed.append(True)) == 2
    assert killed == [True, True]  # a worker in its caller's group never kills that group
    assert json.loads(capsys.readouterr().err)["error_code"] == "RECORDING.INVALID_TRANSITION"


def test_the_worker_trains_on_the_pinned_catalog_version_without_opening_the_store(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = library.layout
    with open_corpus_store(layout, create=False) as corpus:
        _, pinned = corpus.active_catalog(SUBJECT)
        corpus.retire_prompt(released_pilot_v2().prompts[-1].prompt_id)
        newer = corpus.publish_version(CATALOG_ID)
        corpus.activate(SUBJECT, newer)
    assert newer != pinned
    queue = queue_for(library)
    selected: list[Any] = []
    try:
        queue.submit(command(), catalog_reference=pinned)
        claimed = queue.claim()
        assert claimed is not None
        job = claimed[0]
        assert job.run_id is not None and queue.intent("tune-1").catalog_reference == pinned
        request = FineTuneWorkerRequest(
            generation="pinned-generation",
            operation_id="tune-1",
            request_sha256=job.request_sha256,
            run_id=job.run_id,
            runtime_root=str(library.runtime),
        )

        def never(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("a pinned job never opens the corpus store")

        def pipeline(layout: Any, snapshot: Any, subject: str, *args: Any, **kwargs: Any) -> None:
            selected.append((snapshot, kwargs["reference"]))

        monkeypatch.setattr(jobs_module, "open_corpus_store", never)
        monkeypatch.setattr(jobs_module, "run_finetune_pipeline", pipeline)
        reply = run_finetune_worker_request(layout, request, emit=lambda stage: None)
        assert reply.error is None
        snapshot, reference = selected[0]
        assert reference == pinned and snapshot.catalog_sha256 == pinned.catalog_sha256
        # A pinned snapshot that is gone fails the job by name.
        (layout.root / pinned.snapshot_path).unlink()
        missing = run_finetune_worker_request(layout, request, emit=lambda stage: None)
        assert missing.error is not None and missing.error.code == "CORPUS.SNAPSHOT_MISMATCH"
        # The same operation cannot be resubmitted with another pin.
        with pytest.raises(FineTuneJobError) as rebound:
            queue._new_intent(command(), job.request_sha256, newer)
        assert rebound.value.recording_error.code == "RECORDING.INVALID_MESSAGE"
    finally:
        queue.close()


def test_a_recovered_job_ends_even_when_nobody_listens(library: Library) -> None:
    job = leave_running_job(library, run_record_written=False)
    queue = queue_for(library)
    supervisor = FineTuneSupervisor(queue)
    try:
        supervisor.start()
        wait_until(lambda: queue.active_job() is None)
        ended = terminal(queue, "tune-1")
        assert ended.state == "failed" and ended.run_id == job.run_id
        assert supervisor.fatal_error is None
    finally:
        supervisor.close()
        queue.close()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (SecurityError("synthetic unsafe output root"), "RECORDING.SOURCE_CHANGED"),
        (ValueError("synthetic escaping output root"), "RECORDING.SOURCE_CHANGED"),
        (OSError("synthetic unreadable output root"), "RECORDING.WRITE_FAILED"),
    ],
)
def test_an_unresolvable_latest_run_is_named_in_the_status(
    library: Library, monkeypatch: pytest.MonkeyPatch, failure: Exception, code: str
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(jobs_module, "latest_finished_finetune_run", broken)
    queue = queue_for(library)
    try:
        answer = status(FineTuneSupervisor(queue))
        assert answer.latest_run is None and answer.latest_run_error is not None
        assert answer.latest_run_error.code == code
        assert answer.latest_run_error.message.endswith(str(failure))
    finally:
        queue.close()


def test_an_unsafe_run_record_still_releases_the_link(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsafe(*args: Any, **kwargs: Any) -> None:
        raise SecurityError("synthetic unsafe run directory")

    monkeypatch.setattr(jobs_module, "mark_finetune_run_failed", unsafe)
    queue = queue_for(library)
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None
        run_id = claimed[0].run_id
        assert run_id is not None
        run = _write_run(library.layout, run_id, status="running", finished_minute=None)
        link = runtime_link(library, run_id)
        link.parent.mkdir(parents=True)
        os.symlink((library.layout.root / run.run_root / "checkpoints").resolve(), link)
        queue.record_runtime_link("tune-1", str(link))
        ended = queue.finish("tune-1", "failed", jobs_module._interrupted("synthetic"))
        assert ended.error is not None and ended.error.code == "RECORDING.WRITE_FAILED"
        assert "synthetic unsafe run directory" in ended.error.message
        assert not os.path.lexists(link)
    finally:
        queue.close()


def test_the_app_job_head_is_read_without_writing(library: Library) -> None:
    layout = library.layout
    assert active_finetune_job(layout) is None
    queue = queue_for(library)
    try:
        assert active_finetune_job(layout) is None
        queued = queue.submit(command())
        assert active_finetune_job(layout) == queued
    finally:
        queue.close()
    head = layout.root / "provenance/finetune-jobs/head.json"
    head.chmod(0o644)
    with pytest.raises(SecurityError):
        active_finetune_job(layout)


def test_trainer_times_are_bounded() -> None:
    long = "9" * 7
    assert parse_training_progress(f"Epoch 1/2: [{long}:00<00:01, loss=0.5]") == (
        FineTuneTrainingProgress(epoch=1, epochs=2, update=None, loss=0.5, eta_seconds=None)
    )
    assert parse_training_progress("Epoch 1/2: [999999:00<0:00:01, loss=0.5]") == (
        FineTuneTrainingProgress(
            epoch=1, epochs=2, update=None, loss=0.5, eta_seconds=1 + 999999 * 60 + 1
        )
    )


# -- the recording service ---------------------------------------------------------------------


def test_both_backends_answer_fine_tune_commands_without_an_open_context(
    library: Library,
) -> None:
    layout = library.layout
    queue = queue_for(library)
    supervisor = FineTuneSupervisor(queue)
    workspace = RecordingWorkspaceBackend(layout, SUBJECT, discover=False)
    rig = library.coverage.rig
    takes = RecordingTakesBackend(rig.store, rig.group_id)

    def execute(backend: Any, data: Any) -> Any:
        return backend.execute(request(data), RecordingCancellation())

    def refused(backend: Any, data: Any, code: str) -> RecordingServiceError:
        with pytest.raises(RecordingServiceError) as refusal:
            execute(backend, data)
        assert refusal.value.recording_error.code == code
        return refusal.value

    try:
        assert workspace.store.get_cursor(SUBJECT) is None  # no context is open
        for backend in (workspace, takes):
            refused(backend, FineTuneStatusCommand(operation_id="s"), "RECORDING.GATE_NOT_READY")
            refused(backend, command(), "RECORDING.GATE_NOT_READY")
            backend.finetune_jobs = supervisor
        answer = execute(workspace, FineTuneStatusCommand(operation_id="s"))
        assert isinstance(answer, FineTuneStatusEvent) and answer.runtime_configured
        assert answer.active is None and answer.latest_run is None
        assert answer.latest_run_error is None
        queued = execute(workspace, command())
        assert isinstance(queued, FineTuneProgressEvent) and queued.state == "queued"
        # The workspace's job trains on the catalog version this service pinned.
        pinned = workspace.pinned_catalog().reference
        assert queue.intent("tune-1").catalog_reference == pinned
        busy = refused(takes, command("tune-2"), "FINETUNE.JOB_ACTIVE")
        assert busy.recording_error.retryable
        assert execute(takes, FineTuneStatusCommand(operation_id="s")).active == queued
        assert execute(takes, cancel()).state == "cancelled"
        refused(workspace, cancel("tune-unknown"), "RECORDING.INVALID_TRANSITION")
        # A group-scoped service pins nothing: its job uses the active version when it starts.
        assert execute(takes, command("tune-4")).state == "queued"
        assert queue.intent("tune-4").catalog_reference is None
        assert execute(takes, cancel("tune-4")).state == "cancelled"
        # A damaged run record no longer fails the reply: the active job is still reported,
        # the latest run is named unreadable, and a new job can start.
        run = _write_run(layout, "finetune-damaged")
        (layout.root / run.run_root / "run.json").write_text("{damaged")
        assert execute(workspace, command("tune-5")).state == "queued"
        damaged = execute(workspace, FineTuneStatusCommand(operation_id="s"))
        assert isinstance(damaged, FineTuneStatusEvent) and damaged.latest_run is None
        assert damaged.active is not None and damaged.active.operation_id == "tune-5"
        error = damaged.latest_run_error
        assert error is not None and error.code == "FINETUNE.SOURCE_CHANGED"
        assert error.message.startswith("the latest fine-tuned model cannot be read: ")
        assert f"{run.run_root}/run.json" in error.message and not error.retryable
        assert execute(workspace, cancel("tune-5")).state == "cancelled"
        cancelled = RecordingCancellation()
        cancelled.cancel()
        with pytest.raises(RecordingServiceError) as stopped:
            workspace.execute(request(command("tune-3")), cancelled)
        assert stopped.value.recording_error.code == "RECORDING.CANCELLED"
    finally:
        supervisor.close()
        queue.close()
        workspace.index.close()


def test_a_real_service_keeps_save_fast_during_a_job_and_cancel_leaves_no_process(
    package_rig: Any, tmp_path: Path
) -> None:
    from test_recording_takes import publish_catalog, store_suite

    rig, analysis, _packages = package_rig
    layout = rig.layout
    reference, _snapshot = publish_catalog(layout, store_suite(2))
    with open_corpus_store(layout, create=False) as corpus:
        corpus.activate("synthetic-subject", reference)
    runtime, _pin = _stub_runtime(tmp_path.resolve() / "runtime", {"probe_seconds": 120})
    # A separate unsaved draft makes the concurrent Save a real durable mutation.
    rig.now = utc_now() - timedelta(seconds=5)
    rig.store.clock = lambda: rig.now
    draft = rig.seal(rig.reserve("service-draft", "synthetic-0001"), "service-draft-seal")
    rig.store.clock = utc_now
    analysis.close()
    build = recording_service_build_identity()
    own, client = tmp_path / "service-build.json", tmp_path / "client-build.json"
    write_canonical_json(own, build)
    write_canonical_json(client, build_identity())
    group = rig.store.get_group(rig.group_id)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cole_voice_lab.recording_service",
            "--asset-root",
            str(layout.root),
            "--catalog-sha256",
            group.catalog_sha256,
            "--recording-group-id",
            group.group_id,
            "--process-generation",
            "generation-1",
            "--build-identity",
            str(own),
            "--expected-client-build",
            str(client),
            "--finetune-runtime-root",
            str(runtime),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    responses: list[RecordingResponse] = []

    def send(message: Any) -> Any:
        process.stdin.write(recording_frame_bytes(message))
        process.stdin.flush()
        while True:
            response = read_response(process.stdout.fileno(), timeout=20)
            responses.append(response)
            assert response.error is None, response.error
            if response.meta.request_id == message.meta.request_id:
                return response.data

    pgid: int | None = None
    try:
        send(handshake(service=build))
        before = send(request(FineTuneStatusCommand(operation_id="status-1"), request_id="s-1"))
        assert before.runtime_configured and before.active is None and before.latest_run is None
        queued = send(request(command(), request_id="fine-tune-request"))
        assert isinstance(queued, FineTuneProgressEvent) and queued.state == "queued"
        head = layout.root / "provenance/finetune-jobs/head.json"

        def worker_pid() -> int | None:
            job = FineTuneJobHead.model_validate_json(head.read_bytes()).job
            return None if job is None else job.worker_pid

        wait_until(lambda: worker_pid() is not None)
        pgid = worker_pid()
        assert pgid is not None
        probe = str(runtime / "bin" / "python")
        wait_until(lambda: any(probe in row.command for row in live_group(pgid)))
        started = time.monotonic()
        saved = send(
            request(
                rig.command(draft, "service-fast-save"),
                request_id="save",
                take_id=draft.reservation.take_id,
            )
        )
        elapsed = time.monotonic() - started
        assert isinstance(saved, SaveEvent) and saved.take.decision == "kept" and elapsed < 2
        rows = process_table()
        children = [
            row for row in rows if FINETUNE_WORKER_MODULE in row.command and row.pgid == pgid
        ]
        assert [row.pid for row in children] == [pgid]
        during = send(request(FineTuneStatusCommand(operation_id="status-2"), request_id="s-2"))
        assert during.active is not None and during.active.state == "running"
        reply = send(request(cancel(), request_id="cancel-request"))
        assert (reply.state, reply.message) == ("running", CANCELLING_MESSAGE)
        while not any(
            isinstance(response.data, FineTuneProgressEvent) and response.data.state == "cancelled"
            for response in responses
        ):
            response = read_response(process.stdout.fileno(), timeout=30)
            assert response.error is None, response.error
            responses.append(response)
        cancelled = next(
            response
            for response in responses
            if isinstance(response.data, FineTuneProgressEvent)
            and response.data.state == "cancelled"
        )
        assert cancelled.meta.request_id == "tune-1" and cancelled.meta.take_id is None
        assert live_group(pgid) == []
        after = send(request(FineTuneStatusCommand(operation_id="status-3"), request_id="s-3"))
        assert after.active is None
        write_canonical_json(
            tmp_path / "actual-service-finetune-evidence.json",
            {
                "synthetic_only": True,
                "service_pid": process.pid,
                "finetune_worker_pid": pgid,
                "save_seconds_while_finetune_worker_ran": elapsed,
            },
        )
        send(request(ShutdownCommand(), request_id="shutdown"))
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        assert process.stderr is not None
        process.stderr.close()
        if pgid is not None:
            for row in live_group(pgid):
                os.kill(row.pid, signal.SIGKILL)


def test_a_real_service_reports_a_job_it_recovered_before_the_handshake(
    package_rig: Any, tmp_path: Path
) -> None:
    rig, analysis, _packages = package_rig
    layout = rig.layout
    analysis.close()
    runtime, _pin = _stub_runtime(tmp_path.resolve() / "runtime")
    group = rig.store.get_group(rig.group_id)
    queue = FineTuneJobQueue(
        layout, subject_id=group.subject_id, runtime_root=runtime, free_bytes=plenty
    )
    try:
        queue.submit(command())
        claimed = queue.claim()
        assert claimed is not None
        run_id = claimed[0].run_id
        assert run_id is not None
        _write_run(layout, run_id, status="running", finished_minute=None)
    finally:
        queue.close()
    build = recording_service_build_identity()
    own, client = tmp_path / "service-build.json", tmp_path / "client-build.json"
    write_canonical_json(own, build)
    write_canonical_json(client, build_identity())
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cole_voice_lab.recording_service",
            "--asset-root",
            str(layout.root),
            "--catalog-sha256",
            group.catalog_sha256,
            "--recording-group-id",
            group.group_id,
            "--process-generation",
            "generation-1",
            "--build-identity",
            str(own),
            "--expected-client-build",
            str(client),
            "--finetune-runtime-root",
            str(runtime),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        head = layout.root / "provenance/finetune-jobs/head.json"
        wait_until(lambda: FineTuneJobHead.model_validate_json(head.read_bytes()).job is None)
        time.sleep(0.5)  # the outcome is published just after the head clears
        process.stdin.write(recording_frame_bytes(handshake(service=build)))
        process.stdin.flush()
        responses = [read_response(process.stdout.fileno(), timeout=20) for _ in range(3)]
        assert [response.meta.request_id for response in responses] == [
            "handshake",
            "tune-1",
            "tune-1",
        ]
        assert all(response.error is None for response in responses)
        summary, final = responses[1].data, responses[2].data
        assert isinstance(summary, FineTuneEvent) and summary.run.run_id == run_id
        assert isinstance(final, FineTuneProgressEvent) and final.state == "failed"
        assert final.error is not None and final.error.code == "FINETUNE.INTERRUPTED"
        assert responses[1].meta.event_sequence < responses[2].meta.event_sequence
        process.stdin.write(recording_frame_bytes(request(ShutdownCommand(), request_id="bye")))
        process.stdin.flush()
        assert read_response(process.stdout.fileno(), timeout=20).meta.request_id == "bye"
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        assert process.stderr is not None
        process.stderr.close()

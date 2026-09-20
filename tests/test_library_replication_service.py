"""Background library copies owned by the recording service (Step 10).

Scenario tests for ``src/cole_voice_lab/library_replication_service.py``: when the supervisor
decides to copy, what it tells the app, how it backs off and when it stops; and the one reply
frame a pass writes. The supervisor's state machine runs against a fake clock and a fake pass
runner; the worker's frames come from real pushes into a local-directory vault whose key is a
dictionary. One test starts a real recording service whose uploads are stalled and proves Save
stays fast and unrefused while a pass sits on them. Synthetic audio only; nothing here touches
the login Keychain, a tailnet peer, a network or the real library.
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import vault_service_fixture
from test_recording_packages import analyzer_build as analyzer_build
from test_recording_packages import package_rig as package_rig
from test_recording_service import build_identity, handshake, read_response, request
from vault_binding_fixtures import (
    COPIED_CLASSES,
    ENDPOINT,
    LIBRARY_ID,
    NAMESPACE,
    add_library_records,
    approval_record,
    initialized_library,
    install_vault_seams,
    store_library_key,
    write_approval,
    write_library_file,
)

import cole_voice_lab.library_replication_service as service_module
from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.errors import ContractError
from cole_voice_lab.library_generations import OwnerOutcome, PushResult, VaultRecords
from cole_voice_lab.library_replication_service import (
    BACKOFF_SECONDS,
    OFF_STATUS,
    VAULT_STATUS_REQUEST_ID,
    VAULT_SUPERVISOR_REQUEST_ID,
    VaultStatus,
    VaultSupervisor,
    VaultWorkerReply,
    WorkerPassRunner,
    read_worker_reply,
    run_vault_pass,
    vault_status_event,
    worker_command,
    worker_environment,
    worker_reply_bytes,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    SaveEvent,
    ShutdownCommand,
    VaultStatusCommand,
    VaultStatusEvent,
    recording_frame_bytes,
)
from cole_voice_lab.recording_service import recording_service_build_identity
from cole_voice_lab.recording_takes import RecordingMetadataBusyError
from cole_voice_lab.storage_binding import (
    bind_library,
    binding_path,
    library_vault_binding,
    open_library_vault,
)
from cole_voice_lab.storage_contracts import library_head_ref
from cole_voice_lab.storage_ports import storage_error

FIXTURE = Path(vault_service_fixture.__file__).resolve()
UNUSED_BINDING_SHA256 = "0" * 64


# -- helpers ----------------------------------------------------------------------------------


def wait_until(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was never reached")
        time.sleep(0.01)


def settle(seconds: float = 0.1) -> None:
    """Give the supervisor thread several turns of its poll loop."""
    time.sleep(seconds)


def binding_digest(layout: AssetLayout) -> str:
    """The startup pin for a directly invoked synthetic worker pass."""
    return canonical_sha256(library_vault_binding(layout))


class FakeClock:
    """One clock for both seams: a monotonic counter and the wall time it implies."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self.origin = datetime(2026, 9, 17, 12, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return self.origin + timedelta(seconds=self.seconds)

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


def pushed(generation: int = 1, **changes: Any) -> VaultWorkerReply:
    return VaultWorkerReply(outcome="pushed", generation=generation, **changes)


def failed(message: str, **changes: Any) -> VaultWorkerReply:
    return VaultWorkerReply(outcome="failed", message=message, **changes)


class FakeRunner:
    """A pass runner that answers from a script instead of starting a process."""

    def __init__(self, replies: Sequence[VaultWorkerReply]) -> None:
        self.replies = list(replies)
        self.runs = 0
        self.terminated = 0
        self.running = threading.Event()
        self.gate: threading.Event | None = None

    def run(self, stop: threading.Event, *, binding_sha256: str) -> VaultWorkerReply:
        self.runs += 1
        self.running.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=20)
        return self.replies.pop(0) if self.replies else pushed(generation=self.runs)

    def terminate(self) -> None:
        self.terminated += 1
        if self.gate is not None:
            self.gate.set()


class Recorder:
    """Every status the supervisor published, in order."""

    def __init__(self, failures: int = 0, failure: Exception | None = None) -> None:
        self.statuses: list[VaultStatus] = []
        self.errors: list[Any] = []
        self.remaining = failures
        self.failure = failure or RecordingMetadataBusyError("metadata is busy")
        self.attempts = 0

    def status(self, status: VaultStatus) -> None:
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.failure
        self.statuses.append(status)

    def error(self, error: Any) -> None:
        self.errors.append(error)

    @property
    def states(self) -> list[str]:
        return [status.state for status in self.statuses]


@pytest.fixture
def library(tmp_path: Path) -> AssetLayout:
    return initialized_library(tmp_path.resolve() / "assets")


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    directory = tmp_path.resolve() / "outside"
    directory.mkdir(mode=0o700)
    return directory


def bind_local(
    layout: AssetLayout, endpoint: Path, classes: Sequence[str] = COPIED_CLASSES
) -> None:
    bind_library(
        layout,
        backend="local-directory",
        endpoint=str(endpoint),
        namespace=NAMESPACE,
        classes=list(classes),
    )


@pytest.fixture
def supervised(library: AssetLayout, elsewhere: Path) -> Iterator[Callable[..., Any]]:
    """Build a supervisor over a bound library, and close it however the test ends."""
    bind_local(library, elsewhere)
    built: list[VaultSupervisor] = []

    def build(
        runner: FakeRunner | WorkerPassRunner,
        recorder: Recorder,
        clock: FakeClock,
        **changes: Any,
    ) -> VaultSupervisor:
        arguments: dict[str, Any] = {
            "on_status": recorder.status,
            "on_failure": recorder.error,
            "clock": clock.now,
            "monotonic": clock.monotonic,
            "publish_interval": 0.0,
            "poll": 0.005,
            "processes": runner,
            "activity": lambda: None,
        }
        supervisor = VaultSupervisor(library, **(arguments | changes))
        built.append(supervisor)
        return supervisor

    try:
        yield build
    finally:
        for supervisor in built:
            supervisor.close()


# -- off, and failing closed -------------------------------------------------------------------


def test_a_library_bound_to_no_vault_starts_no_thread_and_publishes_nothing(
    library: AssetLayout,
) -> None:
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([])
    supervisor = VaultSupervisor(
        library,
        on_status=recorder.status,
        on_failure=recorder.error,
        clock=clock.now,
        monotonic=clock.monotonic,
        processes=runner,
        activity=lambda: None,
    )

    supervisor.start()
    supervisor.notify_commit()
    clock.advance(10_000)
    settle()

    assert supervisor.status == OFF_STATUS
    assert (recorder.statuses, recorder.errors, runner.runs) == ([], [], 0)
    assert vault_status_event(supervisor.status, operation_id="op-vault").state == "off"
    supervisor.close()


def test_copies_recorded_without_a_binding_fail_closed_before_any_pass(
    library: AssetLayout, elsewhere: Path
) -> None:
    bind_local(library, elsewhere)
    records = VaultRecords(library)
    records.make_directory()
    binding_path(library).unlink()
    write_canonical_json(
        records.directory(None) / "state.json" if records.directory(None) else Path("x"),
        {"unreadable": True},
    )
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([])
    supervisor = VaultSupervisor(
        library,
        on_status=recorder.status,
        on_failure=recorder.error,
        clock=clock.now,
        monotonic=clock.monotonic,
        publish_interval=0.0,
        processes=runner,
        activity=lambda: None,
    )

    supervisor.start()
    clock.advance(10_000)
    settle()

    status = supervisor.status
    assert (status.state, status.code) == ("failed", "STORAGE.NOT_BOUND")
    assert status.message is not None and "bind the vault again" in status.message
    assert runner.runs == 0
    assert recorder.states == ["failed"]
    assert vault_status_event(status, operation_id="op-vault").code == "STORAGE.NOT_BOUND"
    supervisor.close()


# -- when a pass runs ---------------------------------------------------------------------------


def test_a_pass_waits_until_the_library_has_stood_still_for_the_quiet_period(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([pushed()])
    supervisor = supervised(runner, recorder, clock, quiet_period=60.0, rescan_interval=900.0)

    supervisor.start()
    assert supervisor.status.state == "waiting"
    clock.advance(59.0)
    settle()
    assert runner.runs == 0

    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)

    assert supervisor.status.state == "up-to-date"
    assert supervisor.status.generation == 1
    assert supervisor.status.last_pushed_at == clock.now()
    assert recorder.states == ["waiting", "copying", "up-to-date"]
    # One pass, then nothing until a commit or the rescan interval.
    clock.advance(800.0)
    settle()
    assert runner.runs == 1


def test_a_rescan_copies_what_the_command_line_wrote_while_the_app_was_quiet(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([pushed(), pushed(2)])
    supervisor = supervised(runner, recorder, clock, quiet_period=60.0, rescan_interval=900.0)

    supervisor.start()
    clock.advance(61.0)
    wait_until(lambda: supervisor.passes == 1)
    clock.advance(899.0)
    settle()
    assert runner.runs == 1

    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 2)

    assert supervisor.status.generation == 2


def test_a_rescan_never_starts_a_pass_while_the_app_is_still_committing(
    supervised: Callable[..., Any],
) -> None:
    """A recording session keeps saving takes; no pass reads the disk underneath it."""
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([])
    supervisor = supervised(runner, recorder, clock, quiet_period=60.0, rescan_interval=900.0)

    supervisor.start()
    # Twenty minutes of Saves, one every half a quiet period: the rescan interval passes twice.
    for _ in range(40):
        supervisor.notify_commit()
        clock.advance(30.0)
        settle(0.02)

    assert runner.runs == 0
    assert supervisor.status.state == "waiting"

    # The session ends; the quiet period then runs out and the waiting commit is answered.
    clock.advance(61.0)
    wait_until(lambda: supervisor.passes == 1)


def test_a_commit_during_a_pass_asks_for_the_next_one(supervised: Callable[..., Any]) -> None:
    recorder, clock = Recorder(), FakeClock()
    runner = FakeRunner([pushed(), pushed(2)])
    runner.gate = threading.Event()
    supervisor = supervised(runner, recorder, clock, quiet_period=60.0, rescan_interval=900.0)

    supervisor.start()
    clock.advance(61.0)
    wait_until(runner.running.is_set)
    assert supervisor.status.state == "copying"
    supervisor.notify_commit()
    runner.gate.set()
    wait_until(lambda: supervisor.passes == 1)
    runner.gate.clear()
    runner.running.clear()

    # The commit that arrived mid-pass still has to be answered, one quiet period later.
    settle()
    assert runner.runs == 1
    clock.advance(61.0)
    runner.gate.set()
    wait_until(lambda: supervisor.passes == 2)

    assert supervisor.status.generation == 2


def test_a_pass_waits_while_a_fine_tune_job_or_a_speak_request_runs(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([pushed()])
    reasons = ["fine-tune job op-1 is running", "Speak request op-2 is running", None]
    supervisor = supervised(
        runner,
        recorder,
        clock,
        quiet_period=60.0,
        rescan_interval=900.0,
        activity=lambda: reasons.pop(0) if len(reasons) > 1 else reasons[0],
    )

    supervisor.start()
    clock.advance(61.0)
    wait_until(lambda: supervisor.status.message == "fine-tune job op-1 is running")
    assert (supervisor.status.state, runner.runs) == ("waiting", 0)

    clock.advance(61.0)
    wait_until(lambda: supervisor.status.message == "Speak request op-2 is running")
    assert runner.runs == 0

    clock.advance(61.0)
    wait_until(lambda: supervisor.passes == 1)
    assert supervisor.status.state == "up-to-date"


def test_an_unreadable_fine_tune_queue_holds_the_pass_and_says_so(
    supervised: Callable[..., Any], library: AssetLayout
) -> None:
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([pushed()])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0, activity=None)
    head = library.root / "provenance/finetune-jobs"
    head.mkdir(mode=0o700, parents=True)
    (head / "head.json").write_bytes(b"{}\n")
    (head / "head.json").chmod(0o600)

    supervisor.start()
    clock.advance(2.0)
    wait_until(
        lambda: supervisor.status.state == "waiting" and supervisor.status.message is not None
    )

    assert supervisor.status.message is not None
    assert "fine-tune queue cannot be read" in supervisor.status.message
    assert runner.runs == 0


# -- what the app is told -----------------------------------------------------------------------


def test_status_updates_coalesce_to_one_every_thirty_seconds(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock = Recorder(), FakeClock()
    runner = FakeRunner([pushed(), pushed(2, waiting_files=4)])
    supervisor = supervised(
        runner, recorder, clock, quiet_period=1.0, rescan_interval=900.0, publish_interval=30.0
    )

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)
    # "waiting" reached the app at once; "copying" and "up-to-date" fall inside the gap.
    assert recorder.states == ["waiting"]

    supervisor.notify_commit()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 2)
    settle()
    assert recorder.states == ["waiting"]

    clock.advance(31.0)
    wait_until(lambda: len(recorder.statuses) == 2)

    assert recorder.states == ["waiting", "waiting"]
    assert recorder.statuses[-1].waiting_files == 4
    assert supervisor.status.waiting_files == 4


def test_a_publication_another_process_blocks_is_retried_until_it_lands(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock, runner = Recorder(failures=3), FakeClock(), FakeRunner([pushed()])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0)

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)
    wait_until(lambda: recorder.states == ["waiting", "copying", "up-to-date"])

    # The same status is offered again until the lock clears: three refusals, then three states.
    assert recorder.attempts == 6
    assert supervisor.dropped_publications == 0


def test_a_publication_that_fails_another_way_is_dropped_not_fatal(
    supervised: Callable[..., Any],
) -> None:
    recorder = Recorder(failures=1, failure=ValueError("the response lane is full"))
    clock, runner = FakeClock(), FakeRunner([pushed()])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0)

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)
    wait_until(lambda: supervisor.dropped_publications == 1)

    assert supervisor.fatal_error is None
    assert supervisor.status.state == "up-to-date"


def test_a_supervisor_whose_thread_dies_reports_it_on_its_own_error_lane(
    supervised: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0)

    def explode(stop: threading.Event, *, binding_sha256: str) -> VaultWorkerReply:
        raise MemoryError("no room for a pass")

    monkeypatch.setattr(runner, "run", explode)
    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: recorder.errors != [])

    assert isinstance(supervisor.fatal_error, MemoryError)
    assert recorder.errors[0].code == "RECORDING.WRITE_FAILED"
    assert recorder.errors[0].retryable
    assert supervisor.status.state == "failed"
    assert supervisor.status.code == "STORAGE.PROTOCOL"


# -- failure, backoff and stopping ---------------------------------------------------------------


def test_a_vault_that_cannot_be_reached_is_unreachable_since_its_first_silence(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock = Recorder(), FakeClock()
    silent = failed("the vault did not answer", code="STORAGE.UNAVAILABLE", retryable=True)
    runner = FakeRunner([silent, silent, pushed()])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0, backoff=(30.0, 60.0, 120.0))

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)
    first = supervisor.status
    assert (first.state, first.code) == ("unreachable", "STORAGE.UNAVAILABLE")
    assert first.unreachable_since == clock.now()

    clock.advance(31.0)
    wait_until(lambda: supervisor.passes == 2)
    assert supervisor.status.unreachable_since == first.unreachable_since

    clock.advance(61.0)
    wait_until(lambda: supervisor.passes == 3)

    assert supervisor.status.state == "up-to-date"
    assert supervisor.status.unreachable_since is None


def test_each_retryable_failure_waits_longer_than_the_one_before(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock = Recorder(), FakeClock()
    moved = failed("the library changed while it was being copied", retryable=True)
    runner = FakeRunner([moved, moved, moved])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0, backoff=(30.0, 60.0))

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)
    # A source that moved is not the vault's fault: no code, and the app waits rather than fails.
    assert (supervisor.status.state, supervisor.status.code) == ("waiting", None)

    clock.advance(29.0)
    settle()
    assert runner.runs == 1
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 2)

    clock.advance(59.0)
    settle()
    assert runner.runs == 2
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 3)

    assert BACKOFF_SECONDS[-1] == 1800.0


def test_a_refusal_the_vault_will_repeat_stops_every_later_pass(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock = Recorder(), FakeClock()
    refusal = failed("the destination approval expired", code="STORAGE.NOT_APPROVED")
    runner = FakeRunner([refusal, pushed()])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0, rescan_interval=5.0)

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)

    assert supervisor.status.state == "failed"
    assert supervisor.status.code == "STORAGE.NOT_APPROVED"
    supervisor.notify_commit()
    clock.advance(10_000.0)
    settle()

    assert runner.runs == 1
    assert recorder.states[-1] == "failed"


def test_a_pass_whose_owner_held_files_back_leaves_the_vault_waiting(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock = Recorder(), FakeClock()
    held = VaultWorkerReply(
        outcome="pushed",
        generation=4,
        waiting_files=2,
        message="waiting for fine-tune-runs: a job is active",
    )
    runner = FakeRunner([held])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0)

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)

    status = supervisor.status
    assert (status.state, status.waiting_files, status.generation) == ("waiting", 2, 4)
    event = vault_status_event(status, operation_id="op-vault")
    assert event.message == "waiting for fine-tune-runs: a job is active"
    assert event.code is None


# -- closing ------------------------------------------------------------------------------------


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_closing_kills_the_whole_process_group_of_a_running_pass(
    library: AssetLayout, elsewhere: Path, tmp_path: Path
) -> None:
    bind_local(library, elsewhere)
    child = tmp_path / "child.pid"
    script = tmp_path / "worker.sh"
    script.write_text(f"#!/bin/sh\nsleep 120 &\necho $! > {child}\nsleep 120\n")
    script.chmod(0o700)
    runner = WorkerPassRunner(
        library,
        command=lambda _layout: ["/bin/sh", str(script)],
        environment=lambda: {"PATH": "/usr/bin:/bin"},
    )
    recorder, clock = Recorder(), FakeClock()
    supervisor = VaultSupervisor(
        library,
        on_status=recorder.status,
        on_failure=recorder.error,
        clock=clock.now,
        monotonic=clock.monotonic,
        quiet_period=1.0,
        publish_interval=0.0,
        poll=0.005,
        processes=runner,
        activity=lambda: None,
    )

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: child.exists() and runner.worker_pid is not None)
    pgid = runner.worker_pid
    assert pgid is not None
    grandchild = int(child.read_text().strip())
    assert group_alive(pgid)

    supervisor.close()

    wait_until(lambda: not group_alive(pgid))
    wait_until(lambda: not pid_alive(grandchild))
    # A stopped pass is retryable, never a refusal that would stop every later one.
    assert supervisor.status.state != "failed"
    supervisor.close()  # closing twice is safe


def test_closing_never_raises_so_the_services_later_closes_still_run(
    library: AssetLayout,
) -> None:
    supervisor = VaultSupervisor(library, activity=lambda: None)

    supervisor.start()  # no binding: no thread
    supervisor.close()
    supervisor.close()

    assert supervisor.status == OFF_STATUS


# -- the worker's own reply frames ---------------------------------------------------------------


@pytest.fixture
def bound(library: AssetLayout, elsewhere: Path, monkeypatch: pytest.MonkeyPatch) -> AssetLayout:
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    bind_local(library, elsewhere)
    add_library_records(library)
    return library


@pytest.mark.parametrize("replacement", ["destination", "classes"])
@pytest.mark.parametrize("replace_before_worker_open", [False, True])
def test_rebinding_cannot_reach_the_replacement_until_the_supervisor_restarts(
    bound: AssetLayout,
    elsewhere: Path,
    tmp_path: Path,
    replacement: str,
    replace_before_worker_open: bool,
) -> None:
    """Real subprocess passes observe destination heads, including a swap during dispatch."""
    destination = elsewhere if replacement == "classes" else tmp_path.resolve() / "vault-b"
    stall_directory = tmp_path / "unstalled"
    replace_during_dispatch = False

    def rebind() -> None:
        bind_library(
            bound,
            backend="local-directory",
            endpoint=str(destination),
            namespace=NAMESPACE,
            classes=["records"] if replacement == "classes" else COPIED_CLASSES,
            replace=True,
            reset_state=True,
        )

    def command(layout: AssetLayout) -> list[str]:
        nonlocal replace_during_dispatch
        if replace_during_dispatch:
            replace_during_dispatch = False
            rebind()
        return vault_service_fixture.worker_command(layout.root, stall_directory)

    def supervisor() -> VaultSupervisor:
        return VaultSupervisor(
            bound,
            quiet_period=0.01,
            rescan_interval=3600,
            poll=0.005,
            activity=lambda: None,
            processes=WorkerPassRunner(bound, command=command),
        )

    original = supervisor()
    original.start()
    try:
        wait_until(lambda: original.passes == 1)
        assert original.status.state == "up-to-date"
        first = open_library_vault(bound)
        assert first is not None
        first_head = first.ref_store.read(library_head_ref(LIBRARY_ID))
        assert first_head is not None
        write_library_file(
            bound.root, "provenance/headphone-reviews/review-2.json", b'{"review":2}\n'
        )
        if replace_before_worker_open:
            replace_during_dispatch = True
        else:
            rebind()
        original.notify_commit()
        wait_until(lambda: original.passes == 2)

        assert (original.status.state, original.status.code) == ("failed", "STORAGE.NOT_BOUND")
        assert "relaunch the app" in (original.status.message or "")
        replacement_vault = open_library_vault(bound)
        assert replacement_vault is not None
        unchanged = first_head if replacement == "classes" else None
        assert replacement_vault.ref_store.read(library_head_ref(LIBRARY_ID)) == unchanged
        original.notify_commit()
        settle()
        assert original.passes == 2
    finally:
        original.close()

    restarted = supervisor()
    restarted.start()
    try:
        wait_until(lambda: restarted.passes == 1)
        assert restarted.status.state == "up-to-date"
        assert replacement_vault.ref_store.read(library_head_ref(LIBRARY_ID)) != unchanged
    finally:
        restarted.close()


def test_worker_checks_the_opened_binding_even_when_the_file_changes_again(
    bound: AssetLayout, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.storage_binding as bindings

    pin = binding_digest(bound)
    destination = tmp_path.resolve() / "vault-b"
    opened: list[Any] = []

    def replace_open_and_restore(layout: AssetLayout) -> Any:
        bind_library(
            layout,
            backend="local-directory",
            endpoint=str(destination),
            namespace=NAMESPACE,
            classes=COPIED_CLASSES,
            replace=True,
        )
        vault = open_library_vault(layout)
        opened.append(vault)
        # Put the exact startup record back after opening B. Comparing a second disk read
        # would approve A's pin while pushing through B's already-opened stores.
        binding_path(layout).write_bytes(startup_record)
        return vault

    startup_record = binding_path(bound).read_bytes()
    monkeypatch.setattr(bindings, "open_library_vault", replace_open_and_restore)

    reply = run_vault_pass(bound, binding_sha256=pin)

    assert (reply.outcome, reply.code, reply.retryable) == ("failed", "STORAGE.NOT_BOUND", False)
    assert opened[0].ref_store.read(library_head_ref(LIBRARY_ID)) is None


def test_same_destination_approval_replacement_needs_a_restarted_supervisor(
    bound: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.storage_binding as bindings

    local = open_library_vault(bound)
    assert local is not None
    # Keep real approval validation and real encrypted storage, with no tailnet connection.
    monkeypatch.setattr(bindings, "_open_stores", lambda *_args, **_kwargs: local.stores)

    def approve_and_bind(approval_id: str) -> None:
        approval = approval_record(approval_id=approval_id)
        _, digest = write_approval(bound, approval)
        bind_library(
            bound,
            backend="tailnet-vault",
            endpoint=ENDPOINT,
            namespace=NAMESPACE,
            classes=COPIED_CLASSES,
            approval_id=approval_id,
            approval_sha256=digest,
            replace=True,
        )

    class InProcessRunner:
        def run(self, stop: threading.Event, *, binding_sha256: str) -> VaultWorkerReply:
            return run_vault_pass(bound, binding_sha256=binding_sha256)

        def terminate(self) -> None:
            pass

    def supervisor(clock: FakeClock) -> VaultSupervisor:
        return VaultSupervisor(
            bound,
            monotonic=clock.monotonic,
            quiet_period=1,
            poll=0.005,
            activity=lambda: None,
            processes=InProcessRunner(),
        )

    approve_and_bind("approval-a")
    clock = FakeClock()
    original = supervisor(clock)
    original.start()
    try:
        approve_and_bind("approval-b")
        clock.advance(2)
        wait_until(lambda: original.passes == 1)
        assert (original.status.state, original.status.code) == ("failed", "STORAGE.NOT_BOUND")
        assert local.ref_store.read(library_head_ref(LIBRARY_ID)) is None
    finally:
        original.close()

    restarted = supervisor(clock)
    restarted.start()
    try:
        clock.advance(2)
        wait_until(lambda: restarted.passes == 1)
        assert restarted.status.state == "up-to-date"
        assert local.ref_store.read(library_head_ref(LIBRARY_ID)) is not None
    finally:
        restarted.close()


@pytest.mark.parametrize("problem", ["expired", "edited"])
def test_a_pinned_worker_still_checks_approval_expiry_and_integrity_each_pass(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    import cole_voice_lab.storage_binding as bindings

    approval = approval_record()
    path, digest = write_approval(library, approval)
    bind_library(
        library,
        backend="tailnet-vault",
        endpoint=ENDPOINT,
        namespace=NAMESPACE,
        classes=COPIED_CLASSES,
        approval_id=approval.approval_id,
        approval_sha256=digest,
    )
    pin = binding_digest(library)
    if problem == "expired":
        monkeypatch.setattr(bindings, "utc_now", lambda: approval.expires_at + timedelta(seconds=1))
    else:
        path.chmod(0o600)
        path.write_bytes(path.read_bytes().replace(b'"Cole"', b'"Someone else"'))

    reply = run_vault_pass(library, binding_sha256=pin)

    assert (reply.outcome, reply.code, reply.retryable) == ("failed", "STORAGE.NOT_APPROVED", False)
    assert ("expired" if problem == "expired" else "approval changed") in (reply.message or "")


def test_a_pass_answers_one_frame_naming_the_generation_it_pushed(bound: AssetLayout) -> None:
    reply = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (reply.outcome, reply.generation, reply.waiting_files) == ("pushed", 1, 0)
    assert (reply.code, reply.retryable, reply.message) == (None, False, None)
    frame = worker_reply_bytes(reply)
    assert frame.endswith(b"\n") and frame.count(b"\n") == 1
    assert read_worker_reply(frame) == reply

    again = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (again.outcome, again.generation) == ("up-to-date", 1)


def test_a_pass_the_vault_refuses_answers_with_its_storage_code(
    bound: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.library_generations as generations

    monkeypatch.setattr(
        generations,
        "push_generation",
        lambda *_a, **_k: (_ for _ in ()).throw(
            storage_error("STORAGE.NOT_APPROVED", "the destination approval expired")
        ),
    )

    reply = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (reply.outcome, reply.code, reply.retryable) == ("failed", "STORAGE.NOT_APPROVED", False)
    assert reply.message == "the destination approval expired"


def test_a_vault_that_did_not_answer_asks_for_another_pass(
    bound: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.library_generations as generations

    monkeypatch.setattr(
        generations,
        "push_generation",
        lambda *_a, **_k: (_ for _ in ()).throw(
            storage_error("STORAGE.TIMEOUT", "the vault did not answer in time")
        ),
    )

    reply = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (reply.outcome, reply.code, reply.retryable) == ("failed", "STORAGE.TIMEOUT", True)


def test_a_source_that_moved_during_a_pass_asks_for_another_without_a_storage_code(
    bound: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.library_generations as generations
    from cole_voice_lab.library_replication import SourceChangedError

    change = SourceChangedError("raw/one.wav", "changed", "the file moved")
    result = PushResult(
        outcome="source-changed",
        library_id=LIBRARY_ID,
        generation=0,
        manifest_sha256=None,
        head=None,
        owners=(),
        waiting_paths=(),
        uploaded_files=0,
        reused_files=0,
        sent_blobs=0,
        sent_bytes=0,
        source_change=change,
    )
    monkeypatch.setattr(generations, "push_generation", lambda *_a, **_k: result)

    reply = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (reply.outcome, reply.code, reply.retryable) == ("failed", None, True)
    assert reply.message is not None and "raw/one.wav" in reply.message


def test_an_owner_that_held_its_files_back_is_named_in_the_reply(
    bound: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.library_generations as generations

    result = PushResult(
        outcome="pushed",
        library_id=LIBRARY_ID,
        generation=3,
        manifest_sha256="a" * 64,
        head=None,
        owners=(
            OwnerOutcome(
                owner="fine-tune-runs", state="changed", carried=2, reason="a job is active"
            ),
            OwnerOutcome(owner="records", state="complete", carried=0),
        ),
        waiting_paths=("derived/finetune/run-1/run.json",),
        uploaded_files=1,
        reused_files=0,
        sent_blobs=1,
        sent_bytes=10,
    )
    monkeypatch.setattr(generations, "push_generation", lambda *_a, **_k: result)

    reply = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (reply.outcome, reply.generation, reply.waiting_files) == ("pushed", 3, 1)
    assert reply.message == "waiting for fine-tune-runs: a job is active"


def test_a_library_no_longer_bound_refuses_its_pass(library: AssetLayout) -> None:
    reply = run_vault_pass(library, binding_sha256=binding_digest(library))

    assert (reply.outcome, reply.code, reply.retryable) == ("failed", "STORAGE.NOT_BOUND", False)


def test_an_unusable_reply_frame_is_a_refusal_not_a_traceback() -> None:
    for payload, message in (
        (b"", "wrote no reply"),
        (b"x" * (64 * 1024 + 1), "more than one reply frame"),
        (b"{}\n", "not usable"),
        (b'{"outcome": "pushed", "generation": -1}\n', "not usable"),
    ):
        with pytest.raises(ContractError, match=message):
            read_worker_reply(payload)


def test_a_reply_frame_binds_its_outcome_to_its_evidence() -> None:
    for fields, message in (
        ({"outcome": "failed"}, "says what went wrong"),
        ({"outcome": "pushed", "retryable": True}, "only a failed pass"),
        ({"outcome": "pushed", "code": "STORAGE.TIMEOUT"}, "only a failed pass"),
        (
            {"outcome": "failed", "message": "gone", "code": "STORAGE.TIMEOUT"},
            "retryable exactly as its table says",
        ),
    ):
        with pytest.raises(Exception, match=message):
            VaultWorkerReply(**fields)


# -- how a pass is started -------------------------------------------------------------------------


def test_a_pass_runs_an_isolated_interpreter_on_the_worker_module(library: AssetLayout) -> None:
    command = worker_command(library)

    assert command[:4] == [sys.executable, "-I", "-B", "-m"]
    assert command[4] == "cole_voice_lab.library_replication_service"
    assert command[5:] == ["--worker", "--asset-root", str(library.root)]


def test_the_worker_environment_carries_a_home_the_keychain_can_use() -> None:
    import pwd

    environment = worker_environment()

    assert environment["HOME"] == pwd.getpwuid(os.getuid()).pw_dir
    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"
    # Nothing the service inherited reaches the worker.
    assert set(environment) == {
        "PATH",
        "HOME",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONSAFEPATH",
    }


def test_a_worker_that_does_not_lead_its_group_refuses_without_killing_its_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[bool] = []
    # However this test process was started, it stands in for a worker whose group is its
    # caller's: the refusal must leave that group alone.
    monkeypatch.setattr(service_module.os, "getpgrp", lambda: os.getpid() + 1)

    status = service_module.main(
        [
            "--worker",
            "--asset-root",
            "/private/tmp/unused-synthetic-vault-root",
            "--binding-sha256",
            UNUSED_BINDING_SHA256,
        ],
        kill_group=lambda: killed.append(True),
    )

    assert status == 2
    assert killed == []


def test_a_worker_kills_its_own_group_when_the_service_closes_its_stdin(
    library: AssetLayout, tmp_path: Path
) -> None:
    child = tmp_path / "child.pid"
    program = (
        "import os, subprocess, sys, time\n"
        "from cole_voice_lab import library_replication_service as vault\n"
        f"held = subprocess.Popen(['/bin/sh', '-c', 'sleep 120'])\n"
        f"open({str(child)!r}, 'w').write(str(held.pid))\n"
        "vault._watch_parent(sys.stdin.fileno(), __import__('threading').Event(), "
        "lambda: os.killpg(os.getpgrp(), 9))\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_until(lambda: child.exists() and child.read_text().strip() != "")
        held = int(child.read_text().strip())
        assert process.stdin is not None
        process.stdin.close()

        assert process.wait(timeout=20) == -signal.SIGKILL
        wait_until(lambda: not pid_alive(held))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


# -- the real service ------------------------------------------------------------------------------


def test_a_real_service_keeps_save_fast_and_unrefused_while_an_upload_is_stalled(
    package_rig: Any, tmp_path: Path
) -> None:
    rig, analysis, _packages = package_rig
    layout = rig.layout
    analysis.close()
    destination = tmp_path.resolve() / "vault"
    destination.mkdir(mode=0o700)
    stall_directory = tmp_path.resolve() / "stall"
    initialized_library(layout.root)
    bind_local(layout, destination, classes=("records", "recordings"))
    vault_service_fixture.stall(stall_directory)

    rig.now = datetime.now(UTC) - timedelta(seconds=20)
    rig.store.clock = lambda: rig.now
    drafts = [
        rig.seal(
            rig.reserve(f"vault-draft-{number}", f"synthetic-000{number}"), f"vault-seal-{number}"
        )
        for number in range(3)
    ]
    rig.store.clock = lambda: datetime.now(UTC)

    build = recording_service_build_identity()
    own, client = tmp_path / "service-build.json", tmp_path / "client-build.json"
    write_canonical_json(own, build)
    write_canonical_json(client, build_identity())
    group = rig.store.get_group(rig.group_id)
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            str(FIXTURE),
            "--test-vault-service",
            "--test-stall-directory",
            str(stall_directory),
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
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None

    # Every envelope the service sent that no request asked for: the background lane.
    unsolicited: list[Any] = []

    def send(message: Any) -> Any:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(recording_frame_bytes(message))
        process.stdin.flush()
        while True:
            response = read_response(process.stdout.fileno(), timeout=30)
            assert response.error is None, response.error
            if response.meta.request_id == message.meta.request_id:
                return response.data
            unsolicited.append(response)

    asked = itertools.count()

    def status() -> VaultStatusEvent:
        # A fresh request id every time: the service replays a repeated one from its cache.
        number = next(asked)
        reply = send(
            request(VaultStatusCommand(operation_id=f"vault-{number}"), request_id=f"vs-{number}")
        )
        assert isinstance(reply, VaultStatusEvent)
        return reply

    latencies: list[float] = []
    try:
        send(handshake(service=build))
        assert status().state in {"waiting", "copying"}
        # A pass has actually reached an upload and is sitting on it.
        wait_until(lambda: vault_service_fixture.stalled(stall_directory), timeout=60)
        assert status().state == "copying"

        for number, draft in enumerate(drafts):
            started = time.monotonic()
            saved = send(
                request(
                    rig.command(draft, f"vault-save-{number}"),
                    request_id=f"save-{number}",
                    take_id=draft.reservation.take_id,
                )
            )
            latencies.append(time.monotonic() - started)
            assert isinstance(saved, SaveEvent)
            assert saved.take.decision == "kept"
        assert max(latencies) < 2.0
        assert vault_service_fixture.stalled(stall_directory)

        vault_service_fixture.release(stall_directory)
        deadline = time.monotonic() + 90
        while True:
            reached = status()
            if reached.state == "up-to-date":
                break
            assert reached.state in {"waiting", "copying"}, reached
            assert time.monotonic() < deadline, reached
            time.sleep(0.2)

        assert reached.generation >= 1
        assert (destination / "refs").exists()
        # The supervisor's own updates reached the app without being asked for, on the one
        # background request id the app routes them by, carrying no take.
        background = [
            response
            for response in unsolicited
            if response.meta.request_id == VAULT_STATUS_REQUEST_ID
        ]
        assert background, "no background vault status reached the app"
        for response in background:
            assert isinstance(response.data, VaultStatusEvent), response
            assert response.data.operation_id == VAULT_STATUS_REQUEST_ID
            assert response.data.state in {"waiting", "copying", "up-to-date"}
            assert response.meta.take_id is None
        write_canonical_json(
            tmp_path / "actual-service-vault-evidence.json",
            {
                "synthetic_only": True,
                "service_pid": process.pid,
                "save_seconds_while_the_upload_stalled": latencies,
                "generation": reached.generation,
            },
        )
        send(request(ShutdownCommand(), request_id="shutdown"))
        assert process.wait(timeout=20) == 0
    finally:
        vault_service_fixture.release(stall_directory)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        assert process.stderr is not None
        stderr = process.stderr.read()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        assert b"error_code" not in stderr, stderr
        json.dumps({"stderr_bytes": len(stderr)})


def test_the_background_request_ids_never_collide_with_the_apps_own() -> None:
    assert VAULT_STATUS_REQUEST_ID == "vault-status"
    assert VAULT_SUPERVISOR_REQUEST_ID == "vault-supervisor"


# -- the worker process's own life ------------------------------------------------------------


class _Pipes:
    """Stand-ins for the worker's inherited stdin and stdout."""

    def __init__(self) -> None:
        self.stdin_read, self.stdin_write = os.pipe()
        self.stdout_read, self.stdout_write = os.pipe()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pipes = self

        class Stdin:
            def fileno(self) -> int:
                return pipes.stdin_read

        class Stdout:
            def __init__(self) -> None:
                self.buffer = os.fdopen(pipes.stdout_write, "wb")

        monkeypatch.setattr(sys, "stdin", Stdin())
        monkeypatch.setattr(sys, "stdout", Stdout())

    def frame(self) -> bytes:
        return os.read(self.stdout_read, 64 * 1024)

    def close(self) -> None:
        for descriptor in (self.stdin_read, self.stdin_write, self.stdout_read):
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_a_worker_writes_one_frame_and_its_closed_stdin_afterwards_kills_nothing(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipes = _Pipes()
    pipes.install(monkeypatch)
    monkeypatch.setattr(service_module, "run_vault_pass", lambda _layout, **_kw: pushed(7))
    monkeypatch.setattr(os, "getpgrp", os.getpid)
    killed: list[bool] = []

    try:
        status = service_module.main(
            [
                "--worker",
                "--asset-root",
                str(library.root),
                "--binding-sha256",
                UNUSED_BINDING_SHA256,
            ],
            kill_group=lambda: killed.append(True),
        )

        assert status == 0
        assert read_worker_reply(pipes.frame()).generation == 7
        os.close(pipes.stdin_write)
        settle()
        assert killed == []
    finally:
        pipes.close()


def test_a_worker_whose_service_goes_away_mid_pass_kills_its_own_group(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipes = _Pipes()
    pipes.install(monkeypatch)
    gate = threading.Event()
    monkeypatch.setattr(
        service_module, "run_vault_pass", lambda _layout, **_kw: (gate.wait(20), pushed())[1]
    )
    killed: list[bool] = []
    done: list[int] = []

    try:
        thread = threading.Thread(
            target=lambda: done.append(
                service_module._worker_main(
                    library,
                    binding_sha256=UNUSED_BINDING_SHA256,
                    kill_group=lambda: killed.append(True),
                )
            ),
            daemon=True,
        )
        thread.start()
        os.write(pipes.stdin_write, b"the service is still there")
        settle()
        assert killed == []

        os.close(pipes.stdin_write)
        wait_until(lambda: killed == [True])
        gate.set()
        thread.join(timeout=10)

        assert done == [0]
    finally:
        gate.set()
        pipes.close()


def test_a_worker_that_cannot_finish_names_its_code_on_stderr_and_kills_its_group(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(_layout: AssetLayout, **_k: Any) -> int:
        raise storage_error("STORAGE.INTEGRITY", "the vault head does not chain")

    pipes = _Pipes()
    pipes.install(monkeypatch)
    # A failure before a frame can be written at all (the pass itself never started).
    monkeypatch.setattr(service_module, "_worker_main", explode)
    monkeypatch.setattr(os, "getpgrp", os.getpid)
    killed: list[bool] = []

    try:
        status = service_module.main(
            [
                "--worker",
                "--asset-root",
                str(library.root),
                "--binding-sha256",
                UNUSED_BINDING_SHA256,
            ],
            kill_group=lambda: killed.append(True),
        )
    finally:
        pipes.close()

    assert (status, killed != []) == (2, True)
    reported = json.loads(capsys.readouterr().err.strip())
    # The code travels; the vault head, the destination and the library never do.
    assert reported == {
        "service": "vault-worker",
        "error_code": "STORAGE.INTEGRITY",
        "message": "vault worker stopped",
    }


def test_a_pass_that_breaks_in_a_way_no_frame_expected_still_answers_one(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside the pass is still a frame; a supervisor that heard nothing would stop."""

    def explode(_layout: AssetLayout, **_kw: Any) -> VaultWorkerReply:
        raise KeyError("recording-takes")

    pipes = _Pipes()
    pipes.install(monkeypatch)
    monkeypatch.setattr(service_module, "run_vault_pass", explode)
    monkeypatch.setattr(os, "getpgrp", os.getpid)
    killed: list[bool] = []

    try:
        status = service_module.main(
            [
                "--worker",
                "--asset-root",
                str(library.root),
                "--binding-sha256",
                UNUSED_BINDING_SHA256,
            ],
            kill_group=lambda: killed.append(True),
        )
        reply = read_worker_reply(pipes.frame())
    finally:
        pipes.close()

    assert (status, killed) == (0, [])
    assert (reply.outcome, reply.code, reply.retryable) == ("failed", None, True)
    assert reply.message is not None and "KeyError" in reply.message


def test_a_pass_that_breaks_in_a_way_no_code_names_asks_to_be_retried(
    bound: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.library_generations as generations

    def explode(*_a: Any, **_k: Any) -> Any:
        raise OSError("the vault directory disappeared")

    monkeypatch.setattr(generations, "push_generation", explode)

    reply = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (reply.outcome, reply.code, reply.retryable) == ("failed", None, True)
    assert reply.message is not None and "the vault pass failed" in reply.message


def test_an_owner_that_gave_no_reason_is_still_named(
    bound: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.library_generations as generations

    result = PushResult(
        outcome="up-to-date",
        library_id=LIBRARY_ID,
        generation=2,
        manifest_sha256="b" * 64,
        head=None,
        owners=(OwnerOutcome(owner="take-store", state="changed", carried=1),),
        waiting_paths=(),
        uploaded_files=0,
        reused_files=0,
        sent_blobs=0,
        sent_bytes=0,
    )
    monkeypatch.setattr(generations, "push_generation", lambda *_a, **_k: result)

    reply = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (reply.outcome, reply.message) == ("up-to-date", "waiting for take-store")


# -- a pass that never answers properly ----------------------------------------------------------


def one_pass(library: AssetLayout, command: list[str]) -> VaultWorkerReply:
    runner = WorkerPassRunner(
        library,
        command=lambda _layout: command,
        environment=lambda: {"PATH": "/usr/bin:/bin"},
    )
    return runner.run(threading.Event(), binding_sha256=UNUSED_BINDING_SHA256)


def test_a_worker_that_cannot_start_asks_for_another_pass(library: AssetLayout) -> None:
    reply = one_pass(library, ["/nonexistent/vault-worker"])

    assert (reply.outcome, reply.retryable) == ("failed", True)
    assert reply.message is not None and "could not start" in reply.message


def test_a_worker_killed_before_it_answers_asks_for_another_pass(library: AssetLayout) -> None:
    """A dead worker is a crash, not a storage refusal: copying must not stop for the session."""
    for command in (["/bin/sh", "-c", "kill -9 $$"], ["/bin/sh", "-c", "exit 3"]):
        reply = one_pass(library, command)

        assert (reply.outcome, reply.code, reply.retryable) == ("failed", None, True)
        assert reply.message is not None and "wrote no reply" in reply.message


def test_a_pass_whose_worker_died_is_followed_by_another_one(
    supervised: Callable[..., Any],
) -> None:
    recorder, clock = Recorder(), FakeClock()
    dead = failed("the vault worker wrote no reply", retryable=True)
    runner = FakeRunner([dead, pushed(2)])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0, backoff=(30.0,))

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)
    assert supervisor.status.state != "failed"

    clock.advance(31.0)
    wait_until(lambda: supervisor.passes == 2)

    assert (supervisor.status.state, supervisor.status.generation) == ("up-to-date", 2)


def test_a_spawned_worker_inherits_nothing_but_the_environment_the_module_builds(
    library: AssetLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default seam, not a test's override, is what reaches a real worker process."""
    written = tmp_path / "worker-environment.json"
    program = f"import json, os; open({str(written)!r}, 'w').write(json.dumps(dict(os.environ)))"
    monkeypatch.setenv("COLE_VOICE_LAB_SYNTHETIC_LEAK", "must not reach the worker")
    runner = WorkerPassRunner(
        library, command=lambda _layout: [sys.executable, "-I", "-B", "-c", program]
    )

    reply = runner.run(threading.Event(), binding_sha256=UNUSED_BINDING_SHA256)

    assert reply.outcome == "failed"  # the stand-in writes no frame
    given = json.loads(written.read_text())
    expected = worker_environment()
    assert {name: given.get(name) for name in expected} == expected
    # Only what the interpreter itself adds on this platform may join them; nothing inherited.
    assert set(given) - set(expected) <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert "COLE_VOICE_LAB_SYNTHETIC_LEAK" not in given


def test_a_worker_that_writes_more_than_one_frame_is_refused(library: AssetLayout) -> None:
    reply = one_pass(library, ["/bin/sh", "-c", "yes vault | head -c 200000"])

    assert (reply.outcome, reply.retryable) == ("failed", False)
    assert reply.message is not None and "more than one reply frame" in reply.message


def test_a_worker_that_writes_something_else_is_refused(library: AssetLayout) -> None:
    reply = one_pass(library, ["/bin/echo", "not a reply frame"])

    assert (reply.outcome, reply.retryable) == ("failed", False)
    assert reply.message is not None and "not usable" in reply.message


# -- the seams a service without callbacks uses ----------------------------------------------------


def test_a_supervisor_without_callbacks_still_copies_and_keeps_its_status(
    library: AssetLayout, elsewhere: Path
) -> None:
    bind_local(library, elsewhere)
    clock, runner = FakeClock(), FakeRunner([pushed(5)])
    supervisor = VaultSupervisor(
        library,
        clock=clock.now,
        monotonic=clock.monotonic,
        quiet_period=1.0,
        poll=0.005,
        processes=runner,
        activity=lambda: None,
    )

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.passes == 1)

    assert supervisor.status.generation == 5
    supervisor.close()


def test_a_supervisor_without_a_failure_lane_still_records_its_own_end(
    library: AssetLayout, elsewhere: Path
) -> None:
    bind_local(library, elsewhere)
    clock, runner = FakeClock(), FakeRunner([])

    def explode(stop: threading.Event, *, binding_sha256: str) -> VaultWorkerReply:
        raise MemoryError("no room for a pass")

    runner.run = explode  # type: ignore[method-assign]
    supervisor = VaultSupervisor(
        library,
        clock=clock.now,
        monotonic=clock.monotonic,
        quiet_period=1.0,
        poll=0.005,
        processes=runner,
        activity=lambda: None,
    )

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.fatal_error is not None)

    assert supervisor.status.state == "failed"
    supervisor.close()


def test_the_fine_tune_queue_itself_says_when_a_pass_must_wait(
    supervised: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_finetune_jobs as jobs

    class Job:
        operation_id = "op-fine-tune"
        state = "running"

    class Speech:
        operation_id = "op-speak"

    job: list[Any] = [Job()]
    speech: list[Any] = [None, Speech()]
    monkeypatch.setattr(jobs, "active_finetune_job", lambda _layout: job[0])
    monkeypatch.setattr(jobs, "active_speech_request", lambda _layout: speech[0])
    recorder, clock, runner = Recorder(), FakeClock(), FakeRunner([pushed()])
    supervisor = supervised(runner, recorder, clock, quiet_period=1.0, activity=None)

    supervisor.start()
    clock.advance(2.0)
    wait_until(lambda: supervisor.status.message == "fine-tune job op-fine-tune is running")

    job[0] = None
    speech.pop(0)
    clock.advance(2.0)
    wait_until(lambda: supervisor.status.message == "Speak request op-speak is running")

    assert runner.runs == 0


def test_a_push_another_process_already_holds_asks_for_another_pass(
    bound: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.library_generations as generations

    def busy(*_a: Any, **_k: Any) -> Any:
        raise generations.PushRunningError("a push of this library is already running")

    monkeypatch.setattr(generations, "push_generation", busy)

    reply = run_vault_pass(bound, binding_sha256=binding_digest(bound))

    assert (reply.outcome, reply.code, reply.retryable) == ("failed", None, True)
    assert reply.message == "a push of this library is already running"

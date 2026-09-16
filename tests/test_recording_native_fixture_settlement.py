"""Idle native fixtures must prove settlement and retain failure/shutdown evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import recording_latency_benchmark as benchmark
import recording_native_benchmark as native


@pytest.fixture
def settlement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    state = SimpleNamespace(
        events=[],
        clock=0.0,
        jobs=0,
        unanalyzed=0,
        fatal=None,
        fatal_on_start=None,
        fatal_on_close=None,
        close_error=None,
        fatal_on_page=None,
        jobs_on_close=None,
        page_error=None,
    )

    def pages(*, after_sequence: int, limit: int) -> list[tuple[int, str]]:
        state.events.append("page")
        if state.page_error is not None:
            raise state.page_error
        if state.fatal_on_page is not None:
            state.fatal = state.fatal_on_page
        return [(1, json.dumps({"status": "pending"}))] * state.jobs if not after_sequence else []

    index = SimpleNamespace(analysis_jobs=pages, close=lambda: state.events.append("index-close"))
    queue = SimpleNamespace(index=index, close=lambda: state.events.append("queue-close"))

    class Supervisor:
        @property
        def fatal_error(self) -> BaseException | None:
            return state.fatal

        def start(self) -> None:
            state.events.append("start")
            state.fatal = state.fatal_on_start

        def close(self) -> None:
            state.events.append("supervisor-close")
            state.fatal = state.fatal_on_close or state.fatal
            if state.jobs_on_close is not None:
                state.jobs = state.jobs_on_close
            if state.close_error is not None:
                raise state.close_error

    def pause(seconds: float) -> None:
        state.events.append("sleep")
        state.clock += seconds

    monkeypatch.setattr(native, "RecordingTakeStore", lambda _: object())
    monkeypatch.setattr(native, "RecordingIndex", lambda *_, **__: index)
    monkeypatch.setattr(native, "RecordingAnalysisQueue", lambda *_: queue)
    monkeypatch.setattr(native, "RecordingAnalysisSupervisor", lambda _: Supervisor())
    monkeypatch.setattr(native, "unanalyzed_take_count", lambda _: state.unanalyzed)
    monkeypatch.setattr(native, "time", SimpleNamespace(monotonic=lambda: state.clock, sleep=pause))
    state.layout = native.AssetLayout(root=tmp_path)
    return state


def test_idle_fixture_closes_only_after_join_and_confirms_empty_queue(
    settlement: SimpleNamespace,
) -> None:
    assert native.settle_analysis(settlement.layout, drain=True) == 0
    assert settlement.events == [
        "start",
        "page",
        "supervisor-close",
        "page",
        "queue-close",
        "index-close",
    ]


def test_supervisor_fatal_error_is_immediate_not_a_nine_hundred_second_timeout(
    settlement: SimpleNamespace,
) -> None:
    failure = AttributeError("index publication failed")
    settlement.fatal_on_start = failure
    with pytest.raises(AttributeError) as observed:
        native.settle_analysis(settlement.layout, drain=True)
    assert observed.value is failure
    assert "sleep" not in settlement.events and "page" not in settlement.events
    assert settlement.events[-2:] == ["queue-close", "index-close"]


def test_worker_failure_during_a_queue_page_aborts_before_next_page_or_sleep(
    settlement: SimpleNamespace,
) -> None:
    failure = RuntimeError("worker failed while metadata was read")
    settlement.jobs, settlement.fatal_on_page = 1, failure
    with pytest.raises(RuntimeError) as observed:
        native.settle_analysis(settlement.layout, drain=True)
    assert observed.value is failure
    assert settlement.events.count("page") == 1
    assert "sleep" not in settlement.events


def test_expired_deadline_is_not_relabelled_as_settled_even_with_empty_counts(
    settlement: SimpleNamespace,
) -> None:
    with pytest.raises(RuntimeError, match="exceeded its analysis deadline"):
        native.settle_analysis(settlement.layout, drain=True, timeout=0.0)


@pytest.mark.parametrize(("jobs", "unanalyzed"), [(1, 0), (0, 1), (1, 1)])
def test_nonzero_queue_or_unanalyzed_backlog_is_a_fixture_deadline_failure(
    settlement: SimpleNamespace,
    jobs: int,
    unanalyzed: int,
) -> None:
    settlement.jobs, settlement.unanalyzed = jobs, unanalyzed
    with pytest.raises(RuntimeError, match="exceeded its analysis deadline"):
        native.settle_analysis(settlement.layout, drain=True, timeout=0.5)
    assert settlement.clock == 0.5
    assert settlement.events[-3:] == ["supervisor-close", "queue-close", "index-close"]


def test_failure_first_observed_during_shutdown_cannot_become_idle_success(
    settlement: SimpleNamespace,
) -> None:
    failure = RuntimeError("late worker failure")
    settlement.fatal_on_close = failure
    with pytest.raises(RuntimeError) as observed:
        native.settle_analysis(settlement.layout, drain=True)
    assert observed.value is failure
    assert settlement.events[-2:] == ["queue-close", "index-close"]


def test_new_pending_work_at_shutdown_cannot_become_idle_success(
    settlement: SimpleNamespace,
) -> None:
    settlement.jobs_on_close = 1
    with pytest.raises(RuntimeError, match="retained work after analysis shutdown"):
        native.settle_analysis(settlement.layout, drain=True)
    assert settlement.events[-2:] == ["queue-close", "index-close"]


def test_primary_and_join_failures_are_retained_without_closing_live_worker_handles(
    settlement: SimpleNamespace,
) -> None:
    primary, shutdown = ValueError("original metadata failure"), RuntimeError("worker did not join")
    settlement.page_error, settlement.close_error = primary, shutdown
    with pytest.raises(native.NativeBenchmarkUnsafeShutdown) as observed:
        native.settle_analysis(settlement.layout, drain=True)
    assert observed.value.failures == (primary, shutdown)
    assert isinstance(observed.value.__cause__, BaseExceptionGroup)
    assert observed.value.__cause__.exceptions == (primary, shutdown)
    assert "queue-close" not in settlement.events and "index-close" not in settlement.events


def test_late_fatal_and_join_error_are_both_retained_without_closing_handles(
    settlement: SimpleNamespace,
) -> None:
    fatal, shutdown = ValueError("late history failure"), RuntimeError("worker did not join")
    settlement.fatal_on_close, settlement.close_error = fatal, shutdown
    with pytest.raises(native.NativeBenchmarkUnsafeShutdown) as observed:
        native.settle_analysis(settlement.layout, drain=True)
    assert observed.value.failures == (shutdown, fatal)
    assert "queue-close" not in settlement.events and "index-close" not in settlement.events


def test_competing_fixture_reports_backlog_without_starting_a_supervisor(
    settlement: SimpleNamespace,
) -> None:
    settlement.unanalyzed = 1000
    assert native.settle_analysis(settlement.layout, drain=False) == 1000
    assert settlement.events == ["queue-close", "index-close"]


def test_dispatched_native_navigation_failure_retains_partial_report_and_no_samples(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "native"
    destination.mkdir()
    partial = {
        "cycles": 100,
        "failure": "RECORDING.HISTORY_UNKNOWN",
        "timer_frame_agreement": [{"cycle": 0, "exact": True}],
        "samples": [{"operation": "plain_save_durable_acknowledgment", "cycle": 0}],
        "navigation_availability": [{"command_dispatched": True, "outcome": "dispatched-failed"}],
    }
    encoded = json.dumps(partial).encode()
    report = destination / "benchmark-report.json"
    report.write_bytes(encoded)
    monkeypatch.setattr(
        native,
        "build_native_fixture",
        lambda *_, **__: {
            "harness": tmp_path / "fake-harness",
            "configuration": tmp_path / "fake-config.json",
        },
    )

    def failed_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(native.subprocess, "run", failed_run)
    sink: list[dict] = []
    with pytest.raises(RuntimeError, match="partial evidence"):
        native.run_native_case(
            tmp_path,
            binaries={},
            history_count=0,
            take_seconds=1.0,
            cycles=100,
            sample_sink=sink,
        )
    assert report.read_bytes() == encoded
    assert sink == []
    assert (tmp_path / "harness-stdout.log").exists()
    assert (tmp_path / "harness-stderr.log").exists()


@pytest.mark.parametrize("unsafe_shutdown", [False, True])
def test_runner_stops_after_unjoined_worker_but_continues_clean_case_failures(
    settlement: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unsafe_shutdown: bool,
) -> None:
    report_root = tmp_path / "aggregate"
    report_root.mkdir(mode=0o700)
    cases = tuple(
        {
            "history_count": count,
            "cycles": 1,
            "take_seconds": 1.0,
            "signal": "sine",
            "workload": "idle",
        }
        for count in (0, 4, 36)
    )
    monkeypatch.setattr(native, "NATIVE_SMOKE", cases)
    monkeypatch.setattr(native, "release_binaries", lambda: {})
    monkeypatch.setattr(native, "merge_observations", lambda _: {})
    monkeypatch.setattr(benchmark, "host_identity", lambda: {})
    monkeypatch.setattr(
        benchmark, "tempfile", SimpleNamespace(mkdtemp=lambda **_: str(report_root))
    )
    monkeypatch.setattr(
        benchmark,
        "shutil",
        SimpleNamespace(disk_usage=lambda _: SimpleNamespace(free=10 * 1024**3)),
    )
    settlement.page_error = ValueError("initiating fixture metadata failure")
    if unsafe_shutdown:
        settlement.close_error = RuntimeError("fixture shutdown could not join worker")
    attempted: list[int] = []

    def run_case(root: Path, **kwargs: object) -> dict:
        count = kwargs["history_count"]
        assert isinstance(count, int)
        attempted.append(count)
        if count == 4:
            native.settle_analysis(settlement.layout, drain=True)
            raise AssertionError("The failing fixture unexpectedly settled")
        sink = kwargs["sample_sink"]
        assert isinstance(sink, list)
        sink.append(
            {
                "operation": "next_missing_lookup",
                "configured_history_level": count,
                "durable_take_count_at_start": count,
                "take_seconds": 1.0,
                "signal": "sine",
                "workload": "idle",
                "duration_ns": 1_000,
                "status": "ok",
                "wav_open_count": 0,
                "wav_inspection_count": 0,
                "historical_wav_open_count": 0,
            }
        )
        return {
            "save_action_counts": {"completed_cycles": 1},
            "history_count": count,
            "unanalyzed_takes_at_start": 0,
        }

    monkeypatch.setattr(native, "run_native_case", run_case)
    arguments = argparse.Namespace(profile="smoke", repetitions=2, disk_budget_mib=8192)
    assert benchmark.run_native_backend(arguments, argparse.ArgumentParser()) == 1
    report = json.loads((report_root / "report.json").read_bytes())
    assert report["release_acceptance_passed"] is False
    failures = report["failure"]["failed_cases"]
    assert [failure["repeat"] for failure in failures] == ([0] if unsafe_shutdown else [0, 1])
    assert all(failure["case_number"] == 1 for failure in failures)
    assert [failure["artifact_root"] for failure in failures] == [
        str(report_root / f"repeat-{repeat:03d}-case-001")
        for repeat in ([0] if unsafe_shutdown else [0, 1])
    ]
    assert (report_root / "trace.jsonl").read_text().count("\n") == len(report["samples"])
    if unsafe_shutdown:
        assert attempted == [0, 4]
        assert report["aborted_remaining_cases"] is True
        assert len(report["samples"]) == 1
        assert report["failure"]["failed_cases"][0]["unsafe_shutdown_failures"] == [
            {"error_code": "ValueError", "detail": "initiating fixture metadata failure"},
            {"error_code": "RuntimeError", "detail": "fixture shutdown could not join worker"},
        ]
        assert "queue-close" not in settlement.events and "index-close" not in settlement.events
    else:
        assert attempted == [0, 4, 36, 0, 4, 36]
        assert "aborted_remaining_cases" not in report
        assert len(report["samples"]) == 4
        assert len(report["failure"]["failed_cases"]) == 2

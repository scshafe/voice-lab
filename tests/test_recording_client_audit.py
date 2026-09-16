"""Only actual, correlated source work can establish the native inspection verdict."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from recording_native_audit_fixtures import (
    HISTORICAL_SOURCE,
    add_client_span,
    native_audit_fixture,
)
from recording_native_benchmark import native_case_result
from recording_native_client_audit import audited_native_samples


def historical_read(native: dict, *, offset: int = 0, inode: int = 99) -> str:
    return add_client_span(
        native,
        name="private_file.read",
        source=HISTORICAL_SOURCE,
        started=1620 + offset,
        ended=1630 + offset,
        parent_span_id="retained-lease-from-before-command",
        byte_count=96_044,
        device=1,
        inode=inode,
    )


def remove_events(native: dict, predicate) -> None:
    diagnostic = native["diagnostics"]
    diagnostic["events"] = [event for event in diagnostic["events"] if not predicate(event)]
    diagnostic["retained_event_count"] = len(diagnostic["events"])


@pytest.mark.parametrize("cache_hit", [False, True])
def test_full_playback_and_actual_waveform_branch_controls_are_valid(cache_hit: bool) -> None:
    native, audit = native_audit_fixture(cycles=2, waveform_cache_hit=cache_hit)
    original = deepcopy(native)
    samples = audited_native_samples(native, audit)
    assert len(samples) == 11
    assert all(sample["client_opened_historical_paths"] == [] for sample in samples)
    assert native == original


def test_changed_descriptor_holdings_never_become_an_open_event() -> None:
    native, audit = native_audit_fixture()
    sample = native["samples"][0]
    sample["client_held_wav_paths_before"] = []
    sample["client_held_wav_paths_after"] = [HISTORICAL_SOURCE["source_path"]]
    # Even a stale older report's inferred-open field cannot override actual spans.
    sample["client_opened_historical_paths"] = [HISTORICAL_SOURCE["source_path"]]
    result = audited_native_samples(native, audit)[0]
    assert result["client_inspection_events"] == []
    assert result["client_opened_historical_paths"] == []


def test_retained_lease_repeated_reads_count_without_a_new_open() -> None:
    native, audit = native_audit_fixture()
    first = historical_read(native)
    second = historical_read(native, offset=20)
    result = audited_native_samples(native, audit)[0]
    assert {event["span_id"] for event in result["client_inspection_events"]} == {first, second}
    assert len(result["client_historical_inspection_events"]) == 2
    assert result["client_opened_historical_paths"] == [HISTORICAL_SOURCE["source_path"]]
    assert all(event["name"] == "private_file.read" for event in result["client_inspection_events"])


def test_issue_time_selected_exemption_survives_later_selection_and_fd_changes() -> None:
    native, audit = native_audit_fixture()
    historical_read(native)
    sample = native["samples"][0]
    sample["source_exemptions"] = [HISTORICAL_SOURCE.copy()]
    sample["client_held_wav_paths_before"] = [HISTORICAL_SOURCE["source_path"]]
    sample["client_held_wav_paths_after"] = []
    native["selected_review_at_end"] = native["captured_sources"][0]
    result = audited_native_samples(native, audit)[0]
    assert len(result["client_inspection_events"]) == 1
    assert result["client_historical_inspection_events"] == []


@pytest.mark.parametrize("field,value", [("source_sha256", "c" * 64), ("inode", 1000)])
def test_wrong_digest_or_inode_rejects_the_selected_exemption(field: str, value) -> None:
    native, audit = native_audit_fixture()
    historical_read(native)
    native["samples"][0]["source_exemptions"] = [HISTORICAL_SOURCE | {field: value}]
    result = audited_native_samples(native, audit)[0]
    assert len(result["client_historical_inspection_events"]) == 1
    assert result["client_opened_historical_paths"] == [HISTORICAL_SOURCE["source_path"]]


@pytest.mark.parametrize(
    "missing",
    [
        "playback.prepare",
        "playback.av_audio_file_open",
        "private_file.read",
        "private_file.sha256",
        "private_file.validate_identity",
        "waveform.prepare",
        "waveform.cache_load",
        "waveform.decode",
    ],
)
def test_missing_reader_or_waveform_branch_positive_control_invalidates(missing: str) -> None:
    native, audit = native_audit_fixture()
    remove_events(native, lambda event: event["name"] == missing)
    with pytest.raises(RuntimeError, match="positive control"):
        audited_native_samples(native, audit)


def test_unpaired_success_marker_cannot_fake_a_reader_control() -> None:
    native, audit = native_audit_fixture()
    remove_events(
        native,
        lambda event: event["name"] == "playback.av_audio_file_open" and event["phase"] == "begin",
    )
    with pytest.raises(RuntimeError, match="positive control"):
        audited_native_samples(native, audit)


@pytest.mark.parametrize("field,value", [("source_sha256", "f" * 64), ("inode", 999)])
def test_control_from_a_different_source_identity_cannot_satisfy_capture(field: str, value) -> None:
    native, audit = native_audit_fixture()
    native["captured_sources"][0][field] = value
    with pytest.raises(RuntimeError, match="positive control"):
        audited_native_samples(native, audit)


@pytest.mark.parametrize("fault", ["overflow", "count-mismatch", "missing-observer-footprint"])
def test_invalid_observer_capacity_or_accounting_rejects_the_report(fault: str) -> None:
    native, audit = native_audit_fixture()
    diagnostic = native["diagnostics"]
    if fault == "overflow":
        diagnostic["dropped_event_count"] = 1
    elif fault == "count-mismatch":
        diagnostic["retained_event_count"] += 1
    else:
        diagnostic["footprint_includes_observer_storage"] = False
    with pytest.raises(RuntimeError):
        audited_native_samples(native, audit)


@pytest.mark.parametrize("missing_role", ["service", "worker"])
def test_each_process_role_needs_its_startup_positive_control(missing_role: str) -> None:
    native, audit = native_audit_fixture()
    audit = [row for row in audit if not (row["control"] and row["role"] == missing_role)]
    with pytest.raises(RuntimeError, match="startup control"):
        audited_native_samples(native, audit)


def test_genuine_worker_read_and_startup_controls_are_not_interactive_service_reads() -> None:
    native, audit = native_audit_fixture()
    for row in audit:
        if row["control"]:
            row["uptime_ns"] = 1650  # Controls are explicit, even inside a measured window.
    result = audited_native_samples(native, audit)[0]
    assert result["service_open_events"] == []
    assert result["service_historical_inspection_events"] == []
    assert any(not row["control"] and row["role"] == "worker" for row in audit)


def test_actual_service_open_keeps_pid_role_and_command_window_evidence() -> None:
    native, audit = native_audit_fixture()
    inspected = {
        "uptime_ns": 1650,
        "pid": 11,
        "role": "service",
        "control": False,
        "path": HISTORICAL_SOURCE["source_path"],
    }
    audit.append(inspected)
    result = audited_native_samples(native, audit)[0]
    assert result["service_open_events"] == [inspected]
    assert result["service_historical_inspection_events"] == [inspected]


def test_unknown_process_cannot_borrow_another_workers_control() -> None:
    native, audit = native_audit_fixture()
    audit[-1]["pid"] = 999
    with pytest.raises(RuntimeError, match="process startup control"):
        audited_native_samples(native, audit)


@pytest.mark.parametrize("fault", ["wrong-request", "unapplied", "missing-request", "out-of-order"])
def test_metadata_requires_actual_request_and_successful_correlated_application(fault: str) -> None:
    native, audit = native_audit_fixture()
    application = next(
        event
        for event in native["diagnostics"]["events"]
        if event["name"] == "history.application" and event["phase"] == "end"
    )
    if fault == "wrong-request":
        application["context"] = application["context"] | {"request_id": "another-request"}
    elif fault == "unapplied":
        application["outcome"] = "superseded"
    elif fault == "missing-request":
        remove_events(native, lambda event: event["name"] == "service.request.history")
    else:
        application["uptime_nanoseconds"] = 2040
    with pytest.raises(RuntimeError, match="metadata request"):
        audited_native_samples(native, audit)


def test_missing_inspection_end_cannot_turn_a_real_attempt_into_zero_work() -> None:
    native, audit = native_audit_fixture()
    span = historical_read(native)
    remove_events(native, lambda event: event["span_id"] == span and event["phase"] == "end")
    with pytest.raises(RuntimeError, match="completion"):
        audited_native_samples(native, audit)


def test_invalid_diagnostics_cannot_publish_any_samples_to_aggregate(tmp_path: Path) -> None:
    native, audit = native_audit_fixture()
    native["diagnostics"]["dropped_event_count"] = 1
    sentinel = {"trace_id": "earlier-valid-case"}
    sink = [sentinel]
    with pytest.raises(RuntimeError):
        native_case_result(
            native=native,
            audit=audit,
            root=tmp_path,
            history_count=1000,
            durable_take_count=1000,
            unanalyzed_takes_at_start=0,
            take_seconds=1.0,
            signal="sine",
            workload="idle",
            sample_sink=sink,
            requested_cycles=1,
        )
    assert sink == [sentinel]


@pytest.mark.parametrize(
    "operation",
    [
        "record_to_first_synthetic_confirmed_frame",
        "capture_poll_window",
        "stop_model_transition",
        "stop_to_sealed_review",
    ],
)
def test_missing_capture_or_stop_window_cannot_publish_a_zero_inspection_case(
    tmp_path: Path, operation: str
) -> None:
    native, audit = native_audit_fixture(cycles=2)
    native["samples"] = [
        sample
        for sample in native["samples"]
        if not (sample["operation"] == operation and sample["cycle"] == 1)
    ]
    sink = [{"trace_id": "earlier-valid-case"}]
    with pytest.raises(RuntimeError):
        native_case_result(
            native=native,
            audit=audit,
            root=tmp_path,
            history_count=1000,
            durable_take_count=1000,
            unanalyzed_takes_at_start=0,
            take_seconds=1.0,
            signal="sine",
            workload="idle",
            sample_sink=sink,
            requested_cycles=2,
        )
    assert sink == [{"trace_id": "earlier-valid-case"}]


@pytest.mark.parametrize("fault", ["reversed", "duration-mismatch", "noninteger-clock"])
def test_invalid_command_bounds_cannot_hide_real_source_work(tmp_path: Path, fault: str) -> None:
    native, audit = native_audit_fixture()
    sample = next(
        sample for sample in native["samples"] if sample["operation"] == "stop_to_sealed_review"
    )
    if fault == "reversed":
        sample["started_uptime_ns"] = sample["ended_uptime_ns"] + 1
        sample["duration_ns"] = -1
    elif fault == "duration-mismatch":
        sample["duration_ns"] += 1
    else:
        sample["started_uptime_ns"] = float(sample["started_uptime_ns"])
    sink = []
    with pytest.raises(RuntimeError):
        native_case_result(
            native=native,
            audit=audit,
            root=tmp_path,
            history_count=1000,
            durable_take_count=1000,
            unanalyzed_takes_at_start=0,
            take_seconds=1.0,
            signal="sine",
            workload="idle",
            sample_sink=sink,
            requested_cycles=1,
        )
    assert sink == []

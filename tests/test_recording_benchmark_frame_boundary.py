"""Frame targets and terminal Stop evidence cannot infer completed captures."""

from copy import deepcopy
from pathlib import Path

import pytest
from recording_latency_benchmark import ACCEPTANCE_ROWS, acceptance_verdicts, summarize
from recording_native_audit_fixtures import native_audit_fixture, set_capture_evidence
from recording_native_benchmark import MAXIMUM_DURATION_LIMIT, NATIVE_ACCEPTANCE, native_case_result


def result(
    native: dict,
    audit: list,
    root: Path,
    sink: list,
    *,
    seconds: float = 1.0,
    policy: str = "exact-frames",
) -> dict:
    return native_case_result(
        native=native,
        audit=audit,
        root=root,
        history_count=1000,
        durable_take_count=1000,
        unanalyzed_takes_at_start=0,
        take_seconds=seconds,
        completion_policy=policy,
        signal="sine",
        workload="idle",
        requested_cycles=1,
        sample_sink=sink,
    )


def test_full_target_keeps_exact_original_observations(tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    observed = result(native, audit, tmp_path, [])
    assert observed["short_take_cycles"] == 0
    assert observed["observations"]["timer_frame_agreement"]["exact"] is True


def test_early_terminal_cannot_claim_target_with_a_true_flag(tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    set_capture_evidence(
        native,
        0,
        expected=28_800_000,
        frames=28_798_080,
        before_frames=28_793_280,
        policy=MAXIMUM_DURATION_LIMIT,
    )
    native["timer_frame_agreement"][0]["reached_configured_frames"] = True
    sink: list = []
    with pytest.raises(RuntimeError, match="completion contradicts sealed frames"):
        result(native, audit, tmp_path, sink, seconds=600, policy=MAXIMUM_DURATION_LIMIT)
    assert sink == []
    assert native["timer_frame_agreement"][0]["timer_frames"] == 28_793_280
    assert native["timer_frame_agreement"][0]["sealed_frames"] == 28_798_080


def test_truthful_short_take_cannot_supply_an_exact_frame_design_point(tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    set_capture_evidence(native, 0, frames=47_520)
    sink: list = []
    with pytest.raises(RuntimeError, match="Exact-frame capture sealed"):
        result(native, audit, tmp_path, sink)
    assert sink == []


@pytest.mark.parametrize("field", ["expected_frames", "timer_frames", "sealed_frames"])
def test_missing_counts_cannot_default_to_complete(field: str, tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    del native["timer_frame_agreement"][0][field]
    sink: list = []
    with pytest.raises(RuntimeError, match="explicit nonnegative frame counts"):
        result(native, audit, tmp_path, sink)
    assert sink == []


@pytest.mark.parametrize("flag", [None, 1, "true"])
def test_completion_flag_is_explicit_and_strict(flag: object, tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    native["timer_frame_agreement"][0]["reached_configured_frames"] = flag
    sink: list = []
    with pytest.raises(RuntimeError, match="explicit completion"):
        result(native, audit, tmp_path, sink)
    assert sink == []


def test_fabricated_exact_flag_cannot_hide_real_disagreement(tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    native["timer_frame_agreement"][0]["final_timer_frames"] = 47_520
    sink: list = []
    with pytest.raises(RuntimeError, match="timer agreement contradicts"):
        result(native, audit, tmp_path, sink)
    assert sink == []


def test_model_stop_stays_diagnostic_even_with_a_claimed_controller_group(tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    observed = result(native, audit, tmp_path, [])
    local = [
        sample for sample in observed["samples"] if sample["operation"] == "stop_model_transition"
    ]
    assert len(local) == 1
    summaries = summarize(observed["samples"])
    # A second group cannot promote known substitute evidence into controller proof.
    claimed = summarize([local[0] | {"operation": "stop_reaches_controller"}])
    for data in (summaries, summaries + claimed):
        row = acceptance_verdicts(data, backend="native")["stop_reaches_controller"]
        assert row["verdict"] == "not-measured"
        assert row["substitute_metric_name"] == "stop_model_transition"
        assert row["substitute_measured"] is True
        assert row["boundary_is_row_definition"] is False
    assert ACCEPTANCE_ROWS["stop_reaches_controller"]["thresholds_ms"] == {"p95": 50.0}


def test_a_report_cannot_relabel_local_transition_as_controller_ack(tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    for sample in native["samples"]:
        if sample["operation"] == "stop_model_transition":
            sample["operation"] = "stop_reaches_controller"
    sink: list = []
    with pytest.raises(RuntimeError, match="local Stop evidence cannot claim controller"):
        result(native, audit, tmp_path, sink)
    assert sink == []


def maximum_case(frames: int = 28_798_080) -> tuple[dict, list]:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    set_capture_evidence(
        native,
        0,
        expected=28_800_000,
        frames=frames,
        before_frames=frames - 480,
        policy=MAXIMUM_DURATION_LIMIT,
    )
    native["samples"] = [
        sample
        for sample in native["samples"]
        if sample["operation"] not in ("stop_model_transition", "stop_to_sealed_review")
    ]
    return native, audit


def test_live_pre_stop_timer_can_lag_without_replacing_final_duration(tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    set_capture_evidence(native, 0, before_frames=47_520)
    observed = result(native, audit, tmp_path, [])
    assert observed["observations"]["timer_frame_agreement"]["exact"] is True
    assert native["timer_frame_agreement"][0]["timer_frames"] == 47_520
    assert native["timer_frame_agreement"][0]["final_timer_frames"] == 48_000


@pytest.mark.parametrize("frames", [28_798_080, 28_800_000])
def test_explicit_maximum_limit_records_actual_frames_without_stop_samples(
    frames: int, tmp_path: Path
) -> None:
    native, audit = maximum_case(frames)
    observed = result(native, audit, tmp_path, [], seconds=600, policy=MAXIMUM_DURATION_LIMIT)
    assert observed["short_take_cycles"] == (frames != 28_800_000)
    assert observed["permitted_maximum_duration_limit_shortfall_cycles"] == (frames != 28_800_000)
    assert native["timer_frame_agreement"][0]["reached_exact_configured_frames"] == (
        frames == 28_800_000
    )
    assert observed["observations"]["timer_frame_agreement"]["exact"] is True
    assert not any(sample["operation"].startswith("stop_") for sample in observed["samples"])


def test_only_the_six_hundred_second_case_declares_maximum_limit() -> None:
    assert [
        (i, case["take_seconds"])
        for i, case in enumerate(NATIVE_ACCEPTANCE)
        if case.get("completion_policy") == MAXIMUM_DURATION_LIMIT
    ] == [(9, 600.0)]


@pytest.mark.parametrize(
    "field",
    [
        "final_timer_frames",
        "final_waveform_frames",
        "selected_source_frames",
        "final_timer_observed_uptime_ns",
        "final_timer_mode",
        "selected_range_end_frame",
    ],
)
def test_missing_final_measurements_cannot_inherit_pre_stop_values(
    field: str, tmp_path: Path
) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    del native["timer_frame_agreement"][0][field]
    sink: list = []
    with pytest.raises(RuntimeError):
        result(native, audit, tmp_path, sink)
    assert sink == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("final_timer_mode", "capture-written-frames"),
        ("final_timer_source", "pre-stop-snapshot"),
        ("selected_range_start_frame", 480),
        ("selected_source_frames", 47_520),
        ("selected_take_id", "other"),
        ("final_waveform_generation", "other"),
        ("final_waveform_take_id", "other"),
        ("final_timer_observed_uptime_ns", 0),
        ("final_timer_observed_uptime_ns", 1),
    ],
)
def test_final_timer_requires_its_original_full_review_source(
    field: str, value: object, tmp_path: Path
) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    native["timer_frame_agreement"][0][field] = value
    with pytest.raises(RuntimeError):
        result(native, audit, tmp_path, [])


@pytest.mark.parametrize(
    "defect",
    [
        "wrong-reason",
        "timeout",
        "called-stop",
        "issued-clock",
        "still-active",
        "duplicate-boundary",
        "missing-boundary",
        "wrong-digest",
        "wrong-generation",
        "wrong-count",
        "failed-continuity",
        "wrong-maximum",
        "stop-model-sample",
        "stop-review-sample",
        "old-version",
    ],
)
def test_automatic_limit_exceptions_fail_closed(defect: str, tmp_path: Path) -> None:
    native, audit = maximum_case()
    boundary = native["capture_boundaries"][0]
    if defect == "wrong-reason":
        boundary["terminal_reason"] = boundary["seal"]["continuity"]["stop_reason"] = "requested"
    elif defect == "timeout":
        boundary["wait_timed_out"] = True
    elif defect == "called-stop":
        boundary["stop_attempt"]["model_stop_called"] = True
    elif defect == "issued-clock":
        boundary["stop_attempt"]["issued_uptime_ns"] = 900
    elif defect == "still-active":
        boundary["stop_attempt"]["before"]["can_stop"] = True
    elif defect == "duplicate-boundary":
        native["capture_boundaries"].append(deepcopy(boundary))
    elif defect == "missing-boundary":
        native["capture_boundaries"] = []
    elif defect == "wrong-digest":
        boundary["seal"]["reservation_sha256"] = "f" * 64
    elif defect == "wrong-generation":
        boundary["seal"]["process_generation"] = "other"
    elif defect == "wrong-count":
        boundary["seal"]["continuity"]["received_frames"] -= 480
    elif defect == "failed-continuity":
        boundary["seal"]["continuity"]["success"] = False
    elif defect == "wrong-maximum":
        boundary["reservation"]["maximum_frames"] = 28_700_000
    elif defect in ("stop-model-sample", "stop-review-sample"):
        native["samples"].append(
            native["samples"][0]
            | {
                "operation": "stop_model_transition"
                if defect == "stop-model-sample"
                else "stop_to_sealed_review"
            }
        )
    else:
        native["capture_evidence_version"] = "cole-voice-lab.recording-benchmark-capture/v1"
    sink: list = []
    with pytest.raises(RuntimeError):
        result(native, audit, tmp_path, sink, seconds=600, policy=MAXIMUM_DURATION_LIMIT)
    assert sink == []


def test_legacy_frame_report_does_not_acquire_new_final_timer_meaning(tmp_path: Path) -> None:
    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    del native["capture_evidence_version"]
    original = deepcopy(native)
    with pytest.raises(RuntimeError, match="explicit v2"):
        result(native, audit, tmp_path, [])
    assert native == original

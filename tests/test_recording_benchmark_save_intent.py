"""Adaptive native capture cannot substitute plain Save for advancing evidence."""

from pathlib import Path

import pytest
from recording_latency_benchmark import acceptance_verdicts, summarize
from recording_native_audit_fixtures import native_audit_fixture
from recording_native_benchmark import historical_inspections, native_case_result


@pytest.fixture
def required_cases() -> list[dict]:
    return [
        {
            "history_count": count,
            "cycles": 100,
            "take_seconds": 1.0,
            "signal": "sine",
            "workload": "idle",
        }
        for count in (0, 1000)
    ]


def samples(operation: str, count: int, history: int) -> list[dict]:
    return [
        {
            "operation": operation,
            "configured_history_level": history,
            "durable_take_count_at_start": history,
            "take_seconds": 1.0,
            "signal": "sine",
            "workload": "idle",
            "duration_ns": 1_000_000,
            "started_uptime_ns": 100,
            "ended_uptime_ns": 1_000_100,
            "status": "ok",
            "cycle": cycle,
            "wav_open_count": 0,
            "wav_inspection_count": 0,
            "historical_wav_open_count": 0,
            "client_opened_historical_paths": [],
            "take_original_path": "/synthetic/current.wav",
            "advance_intent": operation == "save_next_durable_acknowledgment",
        }
        for cycle in range(count)
    ]


@pytest.mark.parametrize("large_advancing_count", [0, 99])
def test_a_powered_small_library_cannot_hide_missing_or_thin_large_save_next(
    required_cases: list[dict], large_advancing_count: int
) -> None:
    data = samples("save_next_durable_acknowledgment", 100, 0)
    data += samples("save_next_durable_acknowledgment", large_advancing_count, 1000)
    data += samples("plain_save_durable_acknowledgment", 100 - large_advancing_count, 1000)
    row = acceptance_verdicts(summarize(data), backend="native", required_cases=required_cases)[
        "save_next_durable_acknowledgment"
    ]
    assert row["verdict"] == "underpowered"
    required_large = next(
        point
        for point in row["required_design_points"]
        if point["dimensions"]["configured_history_level"] == 1000
    )
    assert required_large["sample_count"] == large_advancing_count
    assert required_large["required_sample_count"] == 100


@pytest.mark.serial
def test_real_powered_advancing_saves_at_every_required_point_can_pass(
    required_cases: list[dict],
) -> None:
    data = samples("save_next_durable_acknowledgment", 100, 0)
    data += samples("save_next_durable_acknowledgment", 100, 1000)
    row = acceptance_verdicts(summarize(data), backend="native", required_cases=required_cases)[
        "save_next_durable_acknowledgment"
    ]
    assert row["verdict"] == "pass"
    assert row["missing_required_design_points"] == []
    assert row["underpowered_required_design_points"] == []


def test_only_plain_save_leaves_the_advancing_row_unmeasured(required_cases: list[dict]) -> None:
    data = samples("plain_save_durable_acknowledgment", 100, 0)
    data += samples("plain_save_durable_acknowledgment", 100, 1000)
    row = acceptance_verdicts(summarize(data), backend="native", required_cases=required_cases)[
        "save_next_durable_acknowledgment"
    ]
    assert row["verdict"] == "not-measured"
    assert len(row["missing_required_design_points"]) == 2


def test_powered_snapshots_do_not_replace_unavailable_next_missing(
    required_cases: list[dict],
) -> None:
    data = samples("metadata_snapshot_at_1000", 100, 1000)
    row = acceptance_verdicts(summarize(data), backend="native", required_cases=required_cases)[
        "metadata_snapshot_at_1000"
    ]
    assert row["verdict"] == "underpowered"
    assert [
        point["dimensions"]["operation"] for point in row["missing_required_design_points"]
    ] == ["next_missing_lookup"]


def test_plain_save_remains_an_audited_interactive_command() -> None:
    data = samples("plain_save_durable_acknowledgment", 1, 1000)
    audit = [{"uptime_ns": 200, "pid": 1, "role": "service", "path": "/synthetic/history.wav"}]
    assert historical_inspections(data, audit)["value_count"] == 1
    audit[0]["role"] = "worker"
    assert historical_inspections(data, audit)["value_count"] == 0


def result(native: dict, root: Path, *, sink: list | None = None) -> dict:
    return native_case_result(
        native=native,
        audit=native["_test_audit"],
        root=root,
        history_count=1000,
        durable_take_count=1000,
        unanalyzed_takes_at_start=0,
        take_seconds=1.0,
        signal="sine",
        workload="idle",
        requested_cycles=100,
        sample_sink=sink,
    )


@pytest.fixture
def plain_native_report() -> dict:
    native, audit = native_audit_fixture(cycles=100, include_metadata=False)
    return native | {"_test_audit": audit}


def test_one_hundred_plain_saves_preserve_memory_and_frame_evidence(
    plain_native_report: dict, tmp_path: Path
) -> None:
    observed = result(plain_native_report, tmp_path)
    assert observed["observations"]["timer_frame_agreement"]["sample_count"] == 100
    assert observed["observations"]["recorder_memory_growth"]["value_bytes"] == 0
    assert observed["save_action_counts"]["plain_save_acknowledged"] == 100
    assert (
        sum(
            item["operation"] == "plain_save_durable_acknowledgment" for item in observed["samples"]
        )
        == 100
    )
    assert not any(
        item["operation"] == "save_next_durable_acknowledgment" for item in observed["samples"]
    )


def test_failure_after_all_captures_still_cannot_feed_the_sample_sink(
    plain_native_report: dict, tmp_path: Path
) -> None:
    plain_native_report["failure"] = "navigation failed after all captures"
    sink: list = []
    with pytest.raises(RuntimeError, match="Failed native case"):
        result(plain_native_report, tmp_path, sink=sink)
    assert sink == []


def test_incomplete_durable_cycles_cannot_emit_passing_memory_evidence(
    plain_native_report: dict, tmp_path: Path
) -> None:
    plain_native_report["save_action_counts"]["completed_cycles"] = 99
    with pytest.raises(RuntimeError, match="every declared capture"):
        result(plain_native_report, tmp_path)


def test_a_mislabeled_plain_save_cannot_supply_an_advancing_sample(
    plain_native_report: dict, tmp_path: Path
) -> None:
    plain_native_report["samples"][0]["operation"] = "save_next_durable_acknowledgment"
    plain_native_report["save_action_counts"]["save_next_acknowledged"] = 1
    plain_native_report["save_action_counts"]["plain_save_acknowledged"] = 99
    with pytest.raises(RuntimeError, match="actual advancing command intent"):
        result(plain_native_report, tmp_path)


def test_a_short_report_cannot_replace_the_requested_hundred_cycle_run(
    plain_native_report: dict, tmp_path: Path
) -> None:
    plain_native_report["cycles"] = 3
    plain_native_report["save_action_counts"]["completed_cycles"] = 3
    plain_native_report["save_action_counts"]["plain_save_acknowledged"] = 3
    plain_native_report["samples"] = plain_native_report["samples"][:3]
    plain_native_report["timer_frame_agreement"] = plain_native_report["timer_frame_agreement"][:3]
    with pytest.raises(RuntimeError, match="requested design point"):
        result(plain_native_report, tmp_path)


@pytest.mark.parametrize("field", ["samples", "timer_frame_agreement"])
def test_duplicate_cycles_cannot_hide_a_missing_command_or_frame_window(
    plain_native_report: dict, tmp_path: Path, field: str
) -> None:
    plain_native_report[field][-1]["cycle"] = 0
    with pytest.raises(RuntimeError, match="missing or duplicates"):
        result(plain_native_report, tmp_path)


def test_action_counters_must_match_the_actual_audited_sample_partition(
    plain_native_report: dict, tmp_path: Path
) -> None:
    plain_native_report["save_action_counts"]["save_next_acknowledged"] = 1
    plain_native_report["save_action_counts"]["plain_save_acknowledged"] = 99
    with pytest.raises(RuntimeError, match="actual command windows"):
        result(plain_native_report, tmp_path)


def test_late_scalar_failure_leaves_the_shared_sample_sink_unchanged(
    plain_native_report: dict, tmp_path: Path
) -> None:
    del plain_native_report["peak_footprint"]
    sink: list = []
    with pytest.raises(KeyError, match="peak_footprint"):
        result(plain_native_report, tmp_path, sink=sink)
    assert sink == []


@pytest.mark.parametrize(
    "operation", ["stop_reaches_controller", "save_next_durable_acknowledgment"]
)
def test_supplemental_long_takes_are_required_only_when_the_profile_powers_them(
    required_cases: list[dict], operation: str
) -> None:
    data = samples(operation, 100, 0) + samples(operation, 100, 1000)
    for seconds, cycles in ((120.0, 3), (600.0, 1)):
        required_cases.append(
            {
                "history_count": 36,
                "cycles": cycles,
                "take_seconds": seconds,
                "signal": "sine",
                "workload": "idle",
            }
        )
        data.extend(sample | {"take_seconds": seconds} for sample in samples(operation, cycles, 36))
    row = acceptance_verdicts(summarize(data), backend="native", required_cases=required_cases)[
        operation
    ]
    assert row["verdict"] == "pass"
    assert len(row["required_design_points"]) == 2
    repeated = acceptance_verdicts(
        summarize(data), backend="native", required_cases=required_cases, repetitions=100
    )[operation]
    assert repeated["verdict"] == "underpowered"
    assert len(repeated["underpowered_required_design_points"]) == 2


def test_a_supplemental_long_take_bound_breach_still_fails(required_cases: list[dict]) -> None:
    data = samples("stop_reaches_controller", 100, 0) + samples(
        "stop_reaches_controller", 100, 1000
    )
    data.extend(
        sample | {"take_seconds": 600.0, "duration_ns": 100_000_000}
        for sample in samples("stop_reaches_controller", 1, 36)
    )
    required_cases.append(
        {
            "history_count": 36,
            "cycles": 1,
            "take_seconds": 600.0,
            "signal": "sine",
            "workload": "idle",
        }
    )
    row = acceptance_verdicts(summarize(data), backend="native", required_cases=required_cases)[
        "stop_reaches_controller"
    ]
    assert row["verdict"] == "fail"
    assert row["observed_ms"]["p95"] == 100.0

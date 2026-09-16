"""The native acceptance benchmark's attribution rules, which decide gate verdicts.

No process is started here. These are the pure rules that turn a harness report and a
cross-process audit into a claim, and a wrong rule would quietly retire a real gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from recording_native_benchmark import (
    NATIVE_ACCEPTANCE,
    historical_inspections,
    indexed_footprint,
    merge_observations,
    native_case_bytes,
    native_pcm,
)

OWN = "/root/assets/takes/current/original.wav"
OTHER = "/root/assets/takes/older/original.wav"


def sample(operation: str, start: int, end: int, **changes: object) -> dict:
    return {
        "operation": operation,
        "started_uptime_ns": start,
        "ended_uptime_ns": end,
        "cycle": 0,
        "take_original_path": OWN,
        "client_opened_historical_paths": [],
    } | changes


def opened(uptime: int, role: str = "service", path: str = OTHER) -> dict:
    return {"uptime_ns": uptime, "pid": 1, "role": role, "path": path}


def test_a_background_worker_read_is_not_charged_to_the_command_it_overlaps() -> None:
    """The design allows a reconciliation job to inspect history; the row does not."""

    samples = [sample("save_next_durable_acknowledgment", 100, 200)]
    result = historical_inspections(samples, [opened(150, role="worker")])
    assert result["value_count"] == 0
    assert result["audited_worker_opens"] == 1
    assert result["audited_service_opens"] == 0


def test_a_service_read_inside_a_command_window_is_a_violation() -> None:
    samples = [sample("save_next_durable_acknowledgment", 100, 200)]
    result = historical_inspections(samples, [opened(150)])
    assert result["value_count"] == 1
    assert result["offenders"][0] == {
        "operation": "save_next_durable_acknowledgment",
        "path": OTHER,
        "process": "service",
    }


def test_the_current_take_and_reads_outside_every_window_are_not_violations() -> None:
    samples = [sample("stop_to_sealed_review", 100, 200)]
    assert historical_inspections(samples, [opened(150, path=OWN)])["value_count"] == 0
    assert historical_inspections(samples, [opened(500)])["value_count"] == 0
    # A window belonging to no measured command cannot absolve a read either.
    unmeasured = [sample("capture_idle", 100, 200)]
    assert historical_inspections(unmeasured, [opened(150)])["value_count"] == 0


def test_a_client_descriptor_opened_across_a_window_counts_as_an_inspection() -> None:
    samples = [sample("metadata_snapshot_at_1000", 1, 2, client_opened_historical_paths=[OTHER])]
    result = historical_inspections(samples, [])
    assert result["value_count"] == 1
    assert result["offenders"][0]["process"] == "client"


def case_result(
    history: int,
    *,
    inspections: int = 0,
    points: float = 0.0,
    client: int = 0,
    workload: str = "idle",
):
    return {
        "case": {"history_count": history, "workload": workload},
        "warm_idle_footprint": {"client_bytes": client, "service_bytes": 0},
        "observations": {
            "historical_audio_inspections": {
                "value_count": inspections,
                "sample_count": 4,
                "offenders": [],
                "audited_service_opens": inspections,
                "audited_worker_opens": 0,
            },
            "timer_frame_agreement": {
                "measured_boundary": "timer",
                "exact": True,
                "sample_count": 2,
                "disagreements": [],
            },
            "stable_layout": {
                "measured_boundary": "layout",
                "value_points": points,
                "sample_count": 3,
            },
            "recorder_memory_growth": {
                "measured_boundary": "memory",
                "value_bytes": 1,
                "sample_count": 5,
            },
        },
    }


def test_merged_inspections_add_rather_than_average_across_cases() -> None:
    """One inspection in one case is a violation; another case's zero cannot cancel it."""

    merged = merge_observations([case_result(4, inspections=1), case_result(36)])
    assert merged["historical_audio_inspections"]["value_count"] == 1
    assert merged["stable_layout"]["value_points"] == 0.0


def test_merged_layout_drift_takes_the_worst_case_not_the_last() -> None:
    merged = merge_observations([case_result(4, points=3.0), case_result(36, points=0.0)])
    assert merged["stable_layout"]["value_points"] == 3.0


def test_the_indexed_history_row_needs_both_library_sizes_to_exist_at_all() -> None:
    """A single library size cannot produce a difference between two library sizes."""

    assert "history_memory_growth" not in merge_observations([case_result(1000, client=100)])
    assert "history_memory_growth" not in merge_observations([case_result(4, client=10)])
    merged = merge_observations([case_result(4, client=10), case_result(1000, client=100)])
    assert merged["history_memory_growth"]["value_bytes"] == 90
    assert merged["history_memory_growth"]["small_library_take_count"] == 4


def test_the_declared_profile_measures_both_library_sizes_and_a_competing_job() -> None:
    """The document requires an idle run and a run with a real competing QC job."""

    levels = {case["history_count"] for case in NATIVE_ACCEPTANCE}
    assert {0, 4, 36, 1000} <= levels
    assert any(case["workload"] == "competing-analysis" for case in NATIVE_ACCEPTANCE)
    assert any(case["cycles"] >= 100 for case in NATIVE_ACCEPTANCE)
    assert {case["signal"] for case in NATIVE_ACCEPTANCE} == {"sine", "silence"}
    assert {case["take_seconds"] for case in NATIVE_ACCEPTANCE} >= {1.0, 10.0, 30.0, 120.0, 600.0}


def test_declared_fixture_size_grows_with_both_cycles_and_take_length() -> None:
    base = {"cycles": 10, "take_seconds": 1.0, "history_count": 0}
    assert native_case_bytes(base | {"cycles": 20}) > native_case_bytes(base)
    assert native_case_bytes(base | {"take_seconds": 2.0}) > native_case_bytes(base)
    assert native_case_bytes(base | {"history_count": 1000}) > native_case_bytes(base)


def test_indexed_footprint_excludes_the_recorder_and_background_workers() -> None:
    result = {"warm_idle_footprint": {"client_bytes": 7, "service_bytes": 11}}
    assert indexed_footprint(result) == 18


def test_generated_pcm_is_bounded_repeatable_and_silent_when_asked() -> None:
    assert native_pcm("silence", 8) == bytes(24)
    first = native_pcm("sine")
    assert first == native_pcm("sine") and len(first) == 1440
    assert (
        max(
            abs(int.from_bytes(first[index : index + 3], "little", signed=True))
            for index in range(0, len(first), 3)
        )
        <= 2_000_000
    )
    with pytest.raises(ValueError, match="bounded sine"):
        native_pcm("white-noise")


def test_the_indexed_history_row_ignores_a_competing_workload_case() -> None:
    """A backlog run is a different measurement and must not decide the idle comparison."""

    merged = merge_observations(
        [
            case_result(4, client=10),
            case_result(1000, client=900, workload="competing-analysis"),
            case_result(1000, client=100),
        ]
    )
    assert merged["history_memory_growth"]["value_bytes"] == 90
    assert merged["history_memory_growth"]["large_library_bytes"] == 100


def test_a_repeated_design_point_can_only_widen_the_reported_growth() -> None:
    """Repetition must not let iteration order pick the flattering pair."""

    merged = merge_observations(
        [
            case_result(4, client=30),
            case_result(4, client=10),
            case_result(1000, client=100),
            case_result(1000, client=140),
        ]
    )
    growth = merged["history_memory_growth"]
    assert growth["large_library_bytes"] == 140
    assert growth["small_library_bytes"] == 10
    assert growth["value_bytes"] == 130
    assert growth["sample_count"] == 4


def test_the_audit_reports_whether_it_could_place_an_open_at_all() -> None:
    """A zero violation count means nothing if no open ever landed inside a window."""

    samples = [sample("stop_to_sealed_review", 100, 200)]
    live = historical_inspections(samples, [opened(150, role="worker"), opened(900)])
    assert live["value_count"] == 0
    assert live["audited_opens_inside_windows"] == 1
    assert live["command_windows"] == 1
    # Clocks that never line up look exactly like success without this control.
    skewed = historical_inspections(samples, [opened(10**12, role="worker")])
    assert skewed["value_count"] == 0
    assert skewed["audited_opens_inside_windows"] == 0


def take_library(tmp_path: Path, states: tuple[object, ...]) -> Path:
    """A durable take library whose head records carry the given analysis states."""

    root = tmp_path / "assets"
    takes = root / "provenance/recording-takes"
    takes.mkdir(parents=True)
    for ordinal, state in enumerate(states):
        directory = takes / f"take-{ordinal:04d}"
        directory.mkdir()
        head = directory / "head.json"
        if state is None:
            head.write_text("{not json", encoding="utf-8")
        else:
            head.write_text(json.dumps({"analysis": state}), encoding="utf-8")
    # Neither a lock file nor a directory without a head is a take awaiting analysis.
    (takes / ".metadata.lock").write_bytes(b"")
    return root


def test_unanalyzed_takes_are_counted_from_durable_heads_not_from_the_queue(
    tmp_path: Path,
) -> None:
    """Saving a take enqueues nothing, so the queue would report an empty backlog."""

    from recording_native_benchmark import unanalyzed_take_count

    from cole_voice_lab.paths import AssetLayout

    root = take_library(
        tmp_path, ("verified", "needs-review", "invalid", "pending", "running", None)
    )
    layout = AssetLayout(root)
    # pending, running and an unreadable head are all work the service still has to do.
    assert unanalyzed_take_count(layout) == 3
    assert unanalyzed_take_count(AssetLayout(take_library(tmp_path / "b", ("verified",)))) == 0

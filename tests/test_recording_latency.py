"""Functional benchmark verification uses counters/barriers, never speed thresholds."""

from __future__ import annotations

import hashlib
import json
import os
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest
from recording_fixtures import (
    REPOSITORY_ROOT,
    InertCaptureProcess,
    require_external_private_root,
    write_synthetic_wav,
)
from recording_latency_benchmark import (
    ACCEPTANCE_ROWS,
    LEGACY_GAPS,
    NATIVE_NOT_MEASURED,
    SCALAR_SAMPLE_REQUIREMENT,
    RecordingTrace,
    acceptance_passed,
    acceptance_verdicts,
    host_identity,
    main,
    run_legacy_case,
    summarize,
)

import cole_voice_lab.audio as audio_module
import cole_voice_lab.capture as capture_module
from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.errors import ContractError


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = (tmp_path / "synthetic-recording-benchmark").resolve()
    root.mkdir(mode=0o700)
    return root


def test_generated_pcm_is_exact_private_repeatable_and_bounded(private_root: Path) -> None:
    first = private_root / "first.wav"
    second = private_root / "second.wav"
    assert write_synthetic_wav(first, 0.0125) == 600
    write_synthetic_wav(second, 0.0125)
    assert first.read_bytes() == second.read_bytes()
    assert first.stat().st_mode & 0o777 == 0o600
    with wave.open(str(first), "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (
            1,
            3,
            48_000,
        )
        assert reader.getnframes() == 600
    assert inspect_canonical_wav(first).audio.frames == 600


def test_generated_silence_contains_only_zero_pcm(private_root: Path) -> None:
    path = private_root / "silence.wav"
    write_synthetic_wav(path, 0.02, "silence")
    with wave.open(str(path), "rb") as reader:
        assert reader.readframes(960) == bytes(960 * 3)


@pytest.mark.parametrize("seconds", [0.0, -1.0, float("nan"), float("inf"), 600.01, 0.000001])
def test_invalid_fixture_durations_fail_before_writing(private_root: Path, seconds: float) -> None:
    path = private_root / "invalid.wav"
    with pytest.raises(ValueError, match="duration"):
        write_synthetic_wav(path, seconds)
    assert not path.exists()


def test_fixtures_reject_repository_world_readable_and_symlink_roots(private_root: Path) -> None:
    with pytest.raises(ValueError, match="repository"):
        require_external_private_root(REPOSITORY_ROOT)
    public = private_root / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        require_external_private_root(public)
    alias = private_root / "alias"
    alias.symlink_to(private_root, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        require_external_private_root(alias)


@pytest.mark.serial
def test_real_legacy_calls_reproduce_history_scans_without_opening_device(
    private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden_device = Mock(side_effect=AssertionError("real device must never be opened"))
    monkeypatch.setattr(capture_module, "_default_process_factory", forbidden_device)
    samples = run_legacy_case(
        private_root, history_count=2, take_seconds=0.02, history_seconds=0.02
    )
    by_operation = {sample["operation"]: sample for sample in samples}
    assert by_operation["current_before_record"]["historical_wav_inspection_count"] == 4
    assert by_operation["poll_during_record"]["historical_wav_inspection_count"] == 4
    assert by_operation["library_rows_and_sessions"]["historical_wav_inspection_count"] == 12
    assert by_operation["save_and_next_model_ready"]["historical_wav_inspection_count"] > 12
    assert all(sample["wav_open_count"] >= sample["wav_inspection_count"] for sample in samples)
    assert all(sample["status"] == "ok" for sample in samples)
    assert forbidden_device.call_count == 0
    assert "Synthetic benchmark instructions." not in json.dumps(samples)


def test_stop_and_save_trace_real_sync_boundaries_separately(private_root: Path) -> None:
    samples = run_legacy_case(
        private_root, history_count=0, take_seconds=0.02, history_seconds=0.02
    )
    stop = next(sample for sample in samples if sample["operation"] == "stop_to_review")
    save = next(sample for sample in samples if sample["operation"] == "save_and_next_model_ready")
    stop_events = {event["event"]: event["elapsed_ns"] for event in stop["events"]}
    assert (
        0
        <= stop_events["stop_delivered_to_controller"]
        < stop_events["file_sealed_after_publication_sync"]
        <= stop["duration_ns"]
    )
    assert save["events"][0]["event"] == "save_evidence_committed_after_sync"
    assert save["events"][0]["elapsed_ns"] <= save["duration_ns"]
    assert stop["wav_inspection_count"] > 0
    assert len({sample["trace_id"] for sample in samples}) == len(samples)


def test_rapid_cycles_keep_prior_takes_and_attribute_history(private_root: Path) -> None:
    samples = run_legacy_case(
        private_root, history_count=0, take_seconds=0.02, history_seconds=0.02, cycles=3
    )
    polls = [sample for sample in samples if sample["operation"] == "poll_during_record"]
    assert [sample["historical_wav_inspection_count"] for sample in polls] == [0, 2, 4]
    assert len(tuple((private_root / "assets/tmp").rglob("*.wav"))) == 3


def test_inspection_instrumentation_retains_failure_and_restores_original(
    private_root: Path,
) -> None:
    bad_path = private_root / "bad.wav"
    bad_path.write_bytes(b"malformed synthetic WAV")
    original = audio_module.inspect_pcm_wav
    trace = RecordingTrace({bad_path})
    with trace.instrument(), pytest.raises(ContractError):
        trace.measure("invalid_inspection", lambda: inspect_canonical_wav(bad_path), {})
    assert audio_module.inspect_pcm_wav is original
    assert trace.samples[0]["status"] == "failed"
    assert trace.samples[0]["wav_inspection_count"] == 1
    assert trace.samples[0]["historical_wav_open_count"] == 1


def test_inert_capture_requires_explicit_frames_before_stop(private_root: Path) -> None:
    process = InertCaptureProcess(["unused", str(private_root / "partial.wav")], lambda event: None)
    with pytest.raises(ValueError, match="not fed"):
        process.signal_quit()
    source = private_root / "generated.wav"
    frames = write_synthetic_wav(source, 0.01)
    process.feed(source, frames)
    with pytest.raises(ValueError, match="only once"):
        process.feed(source, frames)
    process.signal_quit()
    assert process.captured_frames == frames


def test_quantiles_are_empirical_and_include_sample_count(private_root: Path) -> None:
    samples = run_legacy_case(
        private_root, history_count=0, take_seconds=0.01, history_seconds=0.01
    )
    summaries = summarize(samples)
    assert len(summaries) == len(samples)
    assert all(summary["sample_count"] == 1 for summary in summaries)
    assert all(
        summary["latency_ms"]["p50"]["value_ms"] == summary["latency_ms"]["p99"]["value_ms"]
        for summary in summaries
    )
    # A single-sample quantile is the maximum, and the report has to say so itself
    # rather than letting one observation read as an established tail.
    assert all(
        summary["latency_ms"][label]["is_sample_maximum"]
        and summary["latency_ms"][label]["underpowered"]
        for summary in summaries
        for label in ("p50", "p95", "p99")
    )


def test_unmeasured_device_ui_and_background_metrics_are_explicit() -> None:
    assert {
        "visible_command_acknowledgment",
        "device_first_confirmed_frame",
        "next_prompt_render",
        "background_queue_latency",
        "visible_frame_age",
        "competing_background_job",
    } <= LEGACY_GAPS.keys()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--backend", "legacy", "--profile", "smoke"],
        ["--synthetic", "--backend", "legacy", "--profile", "acceptance", "--disk-budget-mib", "1"],
    ],
)
def test_cli_refuses_unsafe_unimplemented_or_over_budget_run(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as failure:
        main(arguments)
    assert failure.value.code == 2


def test_exact_build_and_host_identity_records_source_hashes() -> None:
    identity = host_identity()
    capture_path = REPOSITORY_ROOT / "src/cole_voice_lab/capture.py"
    assert (
        identity["source_sha256"]["src/cole_voice_lab/capture.py"]
        == hashlib.sha256(capture_path.read_bytes()).hexdigest()
    )
    assert identity["process_id"] == os.getpid()
    assert len(identity["python_executable_sha256"]) == 64


def test_every_acceptance_row_is_encoded_with_its_document_line() -> None:
    """The report cannot quietly omit a gate; all fifteen rows are named in one place."""

    assert len(ACCEPTANCE_ROWS) == 15
    lines = (REPOSITORY_ROOT / "docs/NATIVE_RECORDING_UX.md").read_text().splitlines()
    start = next(number for number, line in enumerate(lines) if line.startswith("## 12. "))
    end = next(number for number, line in enumerate(lines) if line.startswith("## 13. "))
    metric_lines = {
        number + 1
        for number in range(start, end)
        if lines[number].startswith("| ") and not lines[number].startswith(("| Metric |", "| ---"))
    }
    assert {row["doc_line"] for row in ACCEPTANCE_ROWS.values()} == metric_lines
    assert all(row["doc_anchor"].startswith("12-") for row in ACCEPTANCE_ROWS.values())
    assert all(row["row"] for row in ACCEPTANCE_ROWS.values())


def test_a_substitute_boundary_can_never_report_a_measured_gate() -> None:
    """Rows needing a display, a device or a person are declared, not approximated."""

    assert NATIVE_NOT_MEASURED.keys() <= ACCEPTANCE_ROWS.keys()
    for row_id, gap in NATIVE_NOT_MEASURED.items():
        assert gap["reason"] and gap["owner"]
        # A substitute must never be named after the row it stands in for.
        assert gap["substitute_metric_name"] != row_id


def test_acceptance_cannot_pass_while_any_row_is_unmeasured() -> None:
    """The pass flag is computed and defaults to false; it is never assigned by hand."""

    passing = {
        row_id: {"verdict": "pass", "boundary_is_row_definition": True}
        for row_id in ACCEPTANCE_ROWS
    }
    release = {"swift_configuration": "release", "sanitizers": []}
    assert acceptance_passed(
        {"acceptance_rows": passing, "samples": [], "host": release, "failure": None}
    )
    for broken in (
        {"acceptance_rows": passing | {"stable_layout": {"verdict": "not-measured"}}},
        {"acceptance_rows": passing | {"stable_layout": {"verdict": "fail"}}},
        {"acceptance_rows": passing | {"stable_layout": {"verdict": "underpowered"}}},
        {"acceptance_rows": {k: v for k, v in list(passing.items())[:14]}},
        {"acceptance_rows": passing, "samples": [{"status": "error"}]},
        {"acceptance_rows": passing, "failure": {"error_code": "RuntimeError"}},
        {"acceptance_rows": passing, "host": {"swift_configuration": "debug"}},
        {"acceptance_rows": passing, "host": release | {"sanitizers": ["address"]}},
        # A run whose takes came up short measured something other than what it declares.
        {"acceptance_rows": passing, "short_take_cycles": 1},
    ):
        report = {"samples": [], "host": release, "failure": None} | broken
        assert not acceptance_passed(report), broken


def test_acceptance_requires_full_validated_evidence_for_each_permitted_limit_shortfall(
    tmp_path,
) -> None:
    from copy import deepcopy

    from recording_native_audit_fixtures import native_audit_fixture, set_capture_evidence
    from recording_native_benchmark import CAPTURE_EVIDENCE_VERSION, native_case_result

    native, audit = native_audit_fixture(cycles=1, include_metadata=False)
    set_capture_evidence(
        native, 0, expected=28_800_000, frames=28_798_080, policy="maximum-duration-limit"
    )
    native["samples"] = [
        sample for sample in native["samples"] if not sample["operation"].startswith("stop_")
    ]
    validated = native_case_result(
        native=native,
        audit=audit,
        root=tmp_path,
        history_count=0,
        durable_take_count=0,
        unanalyzed_takes_at_start=0,
        take_seconds=600,
        signal="sine",
        workload="idle",
        completion_policy="maximum-duration-limit",
    )
    report = {
        "acceptance_rows": {row_id: {"verdict": "pass"} for row_id in ACCEPTANCE_ROWS},
        "host": {"swift_configuration": "release", "sanitizers": []},
        "samples": [],
        "capture_evidence_version": CAPTURE_EVIDENCE_VERSION,
        "short_take_cycles": validated["short_take_cycles"],
        "permitted_maximum_duration_limit_shortfall_cycles": 1,
        "maximum_duration_limit_evidence": validated["maximum_duration_limit_evidence"],
    }
    assert acceptance_passed(report)
    for defect in (
        "missing-proof",
        "old-version",
        "free-count",
        "wrong-reason",
        "duplicate",
        "nonlimit-shortfall",
    ):
        changed = deepcopy(report)
        if defect == "missing-proof":
            changed["maximum_duration_limit_evidence"] = []
        elif defect == "old-version":
            del changed["capture_evidence_version"]
        elif defect == "free-count":
            changed["permitted_maximum_duration_limit_shortfall_cycles"] = 2
        elif defect == "wrong-reason":
            changed["maximum_duration_limit_evidence"][0]["boundary"]["terminal_reason"] = (
                "requested"
            )
        elif defect == "duplicate":
            changed["short_take_cycles"] = changed[
                "permitted_maximum_duration_limit_shortfall_cycles"
            ] = 2
            changed["maximum_duration_limit_evidence"] *= 2
        else:
            changed["short_take_cycles"] = 2
        assert not acceptance_passed(changed), defect


SCALAR_ROWS = {row_id for row_id, row in ACCEPTANCE_ROWS.items() if "thresholds_ms" not in row}


def scalar_summary(row_id: str) -> dict[str, object]:
    """A latency-shaped group named after a scalar row, which must not satisfy it."""

    quantile = {
        "value_ms": 0.0,
        "sample_count": 500,
        "required_sample_count": 20,
        "is_sample_maximum": False,
        "underpowered": False,
    }
    return {
        "operation": row_id,
        "sample_count": 500,
        "latency_ms": {label: dict(quantile) for label in ("p50", "p95", "p99")},
    }


def test_a_scalar_bound_is_never_satisfied_by_a_timing_sample_alone() -> None:
    """Memory, count, exactness and layout rows are bounds, not quantiles."""

    assert SCALAR_ROWS == {
        "recorder_memory_growth",
        "history_memory_growth",
        "historical_audio_inspections",
        "timer_frame_agreement",
        "stable_layout",
    }
    verdicts = acceptance_verdicts(
        [scalar_summary(row_id) for row_id in SCALAR_ROWS], backend="native"
    )
    for row_id in SCALAR_ROWS:
        assert verdicts[row_id]["verdict"] == "not-measured", row_id
        assert "no observation" in verdicts[row_id]["reason"]


@pytest.mark.parametrize(
    ("observation", "verdict"),
    [
        ({"value_bytes": 64 * 1024 * 1024, "sample_count": 3}, "pass"),
        ({"value_bytes": 64 * 1024 * 1024 + 1, "sample_count": 3}, "fail"),
        # One reading under a bound is one run's luck, not an established bound.
        ({"value_bytes": 0, "sample_count": SCALAR_SAMPLE_REQUIREMENT - 1}, "underpowered"),
        # A bound cannot be retired by an observation carrying no value of its kind.
        ({"value_ms": 0.1, "sample_count": 500}, "not-measured"),
        ({"value_bytes": True, "sample_count": 500}, "not-measured"),
    ],
)
def test_a_scalar_observation_is_judged_against_its_declared_bound(
    observation: dict[str, object], verdict: str
) -> None:
    verdicts = acceptance_verdicts(
        [],
        backend="native",
        observations={
            "recorder_memory_growth": {"measured_boundary": "resident growth over warmed idle"}
            | observation
        },
    )
    assert verdicts["recorder_memory_growth"]["verdict"] == verdict


def test_an_observation_without_its_own_boundary_is_refused() -> None:
    """A bare number with no stated boundary is not evidence for a row."""

    verdicts = acceptance_verdicts(
        [],
        backend="native",
        observations={"stable_layout": {"value_points": 0.0, "sample_count": 500}},
    )
    assert verdicts["stable_layout"]["verdict"] == "not-measured"
    assert verdicts["stable_layout"]["boundary_is_row_definition"] is True


def latency_group(operation: str, value_ms: float, count: int, **dimensions: object) -> dict:
    """A summary group shaped exactly as summarize() emits one."""

    quantile = {
        "value_ms": value_ms,
        "sample_count": count,
        "required_sample_count": 20,
        "is_sample_maximum": False,
        "underpowered": count < 20,
    }
    return {
        "operation": operation,
        "configured_history_level": 36,
        "durable_take_count_at_start": 36,
        "take_seconds": 1.0,
        "signal": "sine",
        "workload": "idle",
        "sample_count": count,
        "latency_ms": {label: dict(quantile) for label in ("p50", "p95", "p99")},
    } | dimensions


def test_a_row_is_judged_only_at_the_library_size_it_names() -> None:
    """A fast snapshot over a small history must not retire the thousand-take row."""

    small = latency_group(
        "metadata_snapshot_at_1000",
        1.0,
        500,
        configured_history_level=4,
        durable_take_count_at_start=4,
    )
    verdicts = acceptance_verdicts([small], backend="native")
    entry = verdicts["metadata_snapshot_at_1000"]
    assert entry["verdict"] == "not-measured"
    assert "condition" in entry["reason"]
    assert entry["reported_separately"] == [
        {
            "operation": "metadata_snapshot_at_1000",
            "configured_history_level": 4,
            "durable_take_count_at_start": 4,
            "take_seconds": 1.0,
            "signal": "sine",
            "workload": "idle",
            "sample_count": 500,
        }
    ]
    # A case that declared a thousand takes but built fewer is still out of scope.
    understocked = latency_group(
        "metadata_snapshot_at_1000",
        1.0,
        500,
        configured_history_level=1000,
        durable_take_count_at_start=999,
    )
    assert (
        acceptance_verdicts([understocked], backend="native")["metadata_snapshot_at_1000"][
            "verdict"
        ]
        == "not-measured"
    )
    at_scale = latency_group(
        "metadata_snapshot_at_1000",
        30.0,
        500,
        configured_history_level=1000,
        durable_take_count_at_start=1000,
    )
    assert (
        acceptance_verdicts([small, at_scale], backend="native")["metadata_snapshot_at_1000"][
            "verdict"
        ]
        == "pass"
    )


def test_both_halves_of_the_snapshot_row_are_judged_together() -> None:
    """The row names a snapshot and a next-missing lookup; the worse one decides it."""

    snapshot = latency_group(
        "metadata_snapshot_at_1000",
        30.0,
        500,
        configured_history_level=1000,
        durable_take_count_at_start=1000,
    )
    lookup = latency_group(
        "next_missing_lookup",
        73.0,
        500,
        configured_history_level=1000,
        durable_take_count_at_start=1000,
    )
    entry = acceptance_verdicts([snapshot, lookup], backend="native")["metadata_snapshot_at_1000"]
    assert entry["verdict"] == "fail"
    assert entry["observed_ms"]["p95"] == 73.0


def test_a_longer_take_is_reported_separately_not_judged() -> None:
    """The document scopes the sealing row to takes of at most thirty seconds."""

    short = latency_group("stop_to_sealed_review", 120.0, 100)
    long_take = latency_group("stop_to_sealed_review", 4000.0, 1, take_seconds=600.0)
    entry = acceptance_verdicts([short, long_take], backend="native")["stop_to_sealed_review"]
    assert entry["verdict"] == "pass"
    assert entry["observed_ms"]["p95"] == 120.0
    assert [group["take_seconds"] for group in entry["reported_separately"]] == [600.0]


def test_an_underpowered_group_can_fail_a_bound_but_never_establish_one() -> None:
    thin = latency_group("stop_reaches_controller", 10.0, 3)
    entry = acceptance_verdicts([thin], backend="native")["stop_reaches_controller"]
    assert entry["verdict"] == "underpowered"
    assert entry["powered_group_count"] == 0
    assert entry["underpowered_groups"][0]["sample_count"] == 3
    # The same thin group over the bound is still a real failure.
    over = latency_group("stop_reaches_controller", 80.0, 3)
    assert (
        acceptance_verdicts([over], backend="native")["stop_reaches_controller"]["verdict"]
        == "fail"
    )
    # And one powered group alongside it does not launder that failure into a pass.
    powered = latency_group("stop_reaches_controller", 10.0, 100, take_seconds=2.0)
    assert (
        acceptance_verdicts([over, powered], backend="native")["stop_reaches_controller"]["verdict"]
        == "fail"
    )


def test_a_declared_substitute_is_only_reported_as_measured_when_it_exists() -> None:
    """Naming a stand-in metric must not imply this run produced one."""

    row_id = "record_to_first_confirmed_frames"
    substitute = NATIVE_NOT_MEASURED[row_id]["substitute_metric_name"]
    absent = acceptance_verdicts([], backend="native")[row_id]
    assert absent["verdict"] == "not-measured" and absent["substitute_measured"] is False
    present = acceptance_verdicts([latency_group(substitute, 120.0, 100)], backend="native")[row_id]
    assert present["verdict"] == "not-measured" and present["substitute_measured"] is True

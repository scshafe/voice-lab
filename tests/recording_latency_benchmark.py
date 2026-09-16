"""Maintained synthetic recording benchmark; production behavior remains unmodified.

@canonical recording_latency_benchmark -- scripts/benchmark-recording entry point.
All timing claims name their real observation boundary; missing UI/device metrics
are explicit gaps rather than surrogate successes. COLE_VOICE_NATIVE_SCRATCH_ROOT
optionally selects an existing resolved private external SwiftPM scratch directory.
"""

from __future__ import annotations

import argparse
import builtins
import hashlib
import io
import json
import math
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from recording_fixtures import (
    REPOSITORY_ROOT,
    SAMPLE_RATE_HZ,
    LegacyRecordingFixture,
    Signal,
    require_external_private_root,
    write_synthetic_wav,
)

import cole_voice_lab.audio as audio_module
import cole_voice_lab.capture as capture_module
import cole_voice_lab.recording_library as library_module

sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
from recording_acceptance_matrix import DOCUMENT_PATH, document_reference  # noqa: E402

# Every acceptance row in docs/NATIVE_RECORDING_UX.md section 12, encoded so a
# report can never imply a gate it did not measure. A row whose measured boundary is not the
# row's own definition is forced to "not-measured" no matter how good its numbers look.
ACCEPTANCE_ROWS: dict[str, dict[str, Any]] = {
    "visible_command_acknowledgment": {
        "row": "Click/key to visible acknowledged state",
        "thresholds_ms": {"p95": 50.0, "p99": 100.0},
    },
    "record_to_first_confirmed_frames": {
        "row": "Warm Record to first confirmed frames",
        "thresholds_ms": {"p95": 250.0},
    },
    "cold_device_start": {
        "row": "Cold device start after permission already granted",
        "thresholds_ms": {"p95": 750.0},
    },
    "stop_reaches_controller": {
        "row": "Stop command reaches capture controller",
        "thresholds_ms": {"p95": 50.0},
    },
    "stop_to_sealed_review": {
        "row": "Stop to sealed review for a take of at most thirty seconds",
        "thresholds_ms": {"p95": 300.0},
        # The row is defined for takes of at most thirty seconds; the document says longer
        # takes are reported separately, so they are reported and never judged here.
        "scope": {"take_seconds": {"max": 30.0}},
    },
    "save_next_durable_acknowledgment": {
        "row": "Save and Next to durable acknowledgment and displayed next prompt",
        "thresholds_ms": {"p95": 200.0, "p99": 400.0},
    },
    "captured_sample_visible_age": {
        "row": "Captured-sample to visible waveform age",
        "thresholds_ms": {"p95": 100.0},
    },
    "waveform_render_work": {
        "row": "Foreground waveform drawing render work",
        "thresholds_ms": {"p95": 16.7},
    },
    "warm_launch_to_useful": {
        "row": "Warm indexed launch to useful library and recorder",
        "thresholds_ms": {"p95": 1000.0},
    },
    "metadata_snapshot_at_1000": {
        "row": "Metadata snapshot and next-missing lookup at one thousand takes",
        "thresholds_ms": {"p95": 50.0},
        # Both halves of the row, and only at the library size the row names. A snapshot
        # over a five-take history is fast for reasons this gate is not about. The scope
        # reads the library this run actually built, never the size it meant to build.
        "operations": ("metadata_snapshot_at_1000", "next_missing_lookup"),
        "scope": {"durable_take_count_at_start": {"min": 1000}},
    },
    "recorder_memory_growth": {
        "row": "Added recorder and waveform working memory over warmed idle",
        "thresholds_bytes": {"maximum": 64 * 1024 * 1024},
    },
    "history_memory_growth": {
        "row": "Added indexed-history and cache memory at one thousand takes",
        "thresholds_bytes": {"maximum": 64 * 1024 * 1024},
    },
    "historical_audio_inspections": {
        "row": "Historical audio inspections requested by start, poll, stop and save",
        "thresholds_count": {"maximum": 0},
    },
    "timer_frame_agreement": {
        "row": "Timer and final audio duration agree exactly in frames",
        "thresholds_exact": True,
    },
    "stable_layout": {
        "row": "Stable layout during transport and background processing",
        "thresholds_points": {"maximum": 1.0},
    },
}

# Resolve only citation metadata. Metric boundaries, scopes, targets and verdict
# evaluation remain the benchmark's own fixed acceptance definitions above.
_acceptance_document_lines = DOCUMENT_PATH.read_text(encoding="utf-8").splitlines()
for _acceptance_row_id, _acceptance_row in ACCEPTANCE_ROWS.items():
    _acceptance_row.update(
        document_reference(_acceptance_document_lines, "12", metric_id=_acceptance_row_id)
    )

# Rows a headless synthetic benchmark cannot establish, with the reason and who owns them.
# A substitute metric may be measured, but it is never reported under the row's own label.
NATIVE_NOT_MEASURED: dict[str, dict[str, str]] = {
    "visible_command_acknowledgment": {
        "reason": (
            "No frame is ever presented: the harness sets a prohibited activation policy, no "
            "window is ordered on screen, and the only pixels come from an offscreen bitmap."
        ),
        "substitute_metric_name": "command_acknowledgment_model_publish_ms",
        "owner": "physical gate",
    },
    "record_to_first_confirmed_frames": {
        "reason": (
            "Synthetic capture emits its first chunk from a fixture-chosen timer with no "
            "CoreAudio start cost, so the device half of this row is not exercised."
        ),
        "substitute_metric_name": "record_to_first_synthetic_confirmed_frame",
        "owner": "physical gate",
    },
    "cold_device_start": {
        "reason": "No microphone is opened; device validation is skipped for synthetic capture.",
        "substitute_metric_name": "starting_state_visible_in_model",
        "owner": "physical gate",
    },
    "captured_sample_visible_age": {
        "reason": (
            "Only render-completion ticks exist without a display driver, and offscreen "
            "redraw cadence is an artifact of harness polling rather than presentation."
        ),
        "substitute_metric_name": "captured_sample_to_render_completion_age_ms",
        "owner": "physical gate",
    },
    "warm_launch_to_useful": {
        "reason": (
            "Useful is stamped at model readiness before any window, layout or presented "
            "frame exists, and the clock starts after the dynamic loader has run."
        ),
        "substitute_metric_name": "release_spawn_to_useful_model_ms",
        "owner": "physical gate",
    },
    "waveform_render_work": {
        "reason": (
            "Render work per frame is measurable, but the sixty hertz cadence and the "
            "energy-saving mode depend on a display driver this stack does not have."
        ),
        "substitute_metric_name": "waveform_render_work_ms_offscreen",
        "owner": "physical gate",
    },
}


LEGACY_GAPS = {
    "visible_command_acknowledgment": (
        "No GUI is hosted; method-return latency is measured separately."
    ),
    "device_first_confirmed_frame": "Inert capture injects generated PCM; no device is opened.",
    "cold_device_start": "No microphone or permission interaction is authorized by this benchmark.",
    "next_prompt_render": "The returned selected prompt is measured, without a Qt paint event.",
    "background_queue_latency": "Legacy QC is synchronous; no separate background queue exists.",
    "visible_frame_age": "Legacy live waveform is absent and this benchmark does not host a UI.",
    "waveform_render_work": "No waveform/UI renderer is active.",
    "callback_continuity": "The inert process has no hardware callback or sample timestamps.",
    "memory_working_set_delta": (
        "Process high-water RSS is recorded; it is not a working-set delta."
    ),
    "competing_background_job": "Legacy baseline runs idle; native acceptance adds competing QC.",
}


class RecordingTrace:
    """Attribute real WAV inspections/opens and capture/save boundaries to each command."""

    def __init__(self, historical_paths: set[Path]) -> None:
        self.historical_paths = historical_paths
        self.samples: list[dict[str, Any]] = []
        self.active: dict[str, Any] | None = None
        self.started_ns = 0

    def mark(self, event: str) -> None:
        """Record a monotonic event only while a measured command owns the trace."""
        if self.active is not None:
            self.active["events"].append(
                {"event": event, "elapsed_ns": time.perf_counter_ns() - self.started_ns}
            )

    def _opened(self, path: object) -> None:
        if self.active is None or not isinstance(path, (str, bytes, os.PathLike)):
            return
        candidate = Path(os.fsdecode(path))
        if candidate.suffix == ".wav":
            self.active["wav_open_count"] += 1
            self.active["historical_wav_open_count"] += candidate in self.historical_paths

    @contextmanager
    def instrument(self) -> Iterator[None]:
        """Wrap actual production functions without caching, bypassing or replacing checks."""
        inspect = audio_module.inspect_pcm_wav
        publish = capture_module._publish_completed_capture
        persist = library_module.write_canonical_json

        def inspected(path: Path, **kwargs: Any) -> Any:
            if self.active is not None:
                self.active["wav_inspection_count"] += 1
                self.active["historical_wav_inspection_count"] += path in self.historical_paths
            result = inspect(path, **kwargs)
            if self.active is not None:
                self.active["inspected_audio_seconds"] += result.audio.duration_seconds
            return result

        def published(**kwargs: Any) -> Any:
            result = publish(**kwargs)
            self.mark("file_sealed_after_publication_sync")
            return result

        def persisted(path: Path, value: object, **kwargs: Any) -> str:
            result = persist(path, value, **kwargs)
            if (
                self.active is not None
                and self.active["operation"] == "save_and_next_model_ready"
                and isinstance(value, library_module.DesktopSessionEvidence)
            ):
                self.mark("save_evidence_committed_after_sync")
            return result

        def observe_open(original: Callable[..., Any]) -> Callable[..., Any]:
            def opened(file: object, *args: Any, **kwargs: Any) -> Any:
                result = original(file, *args, **kwargs)
                self._opened(file)
                return result

            return opened

        with ExitStack() as stack:
            stack.enter_context(patch.object(audio_module, "inspect_pcm_wav", inspected))
            stack.enter_context(
                patch.object(capture_module, "_publish_completed_capture", published)
            )
            stack.enter_context(patch.object(library_module, "write_canonical_json", persisted))
            for module, name in ((os, "open"), (builtins, "open"), (io, "open")):
                stack.enter_context(patch.object(module, name, observe_open(getattr(module, name))))
            yield

    def measure(
        self, operation: str, action: Callable[[], object], context: dict[str, Any]
    ) -> object:
        """Run one actual operation; retain failure evidence and propagate its exception."""
        if self.active is not None:
            raise RuntimeError("nested benchmark commands would double-count work")
        self.active = {
            **context,
            "operation": operation,
            "trace_id": uuid4().hex,
            "timestamp": datetime.now(UTC).isoformat(),
            "level": "info",
            "message": "synthetic recording command measurement",
            "service": "cole_voice_lab.recording_latency_benchmark",
            "wav_open_count": 0,
            "historical_wav_open_count": 0,
            "wav_inspection_count": 0,
            "historical_wav_inspection_count": 0,
            "inspected_audio_seconds": 0.0,
            "events": [],
            "status": "ok",
        }
        self.started_ns = time.perf_counter_ns()
        try:
            return action()
        except BaseException as error:
            self.active["status"] = "failed"
            self.active["error_code"] = type(error).__name__
            raise
        finally:
            self.active["duration_ns"] = time.perf_counter_ns() - self.started_ns
            usage = resource.getrusage(resource.RUSAGE_SELF)
            self.active["process_high_water_rss_bytes"] = int(
                usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
            )
            self.samples.append(self.active)
            self.active = None


def run_legacy_case(
    root: Path,
    *,
    history_count: int,
    take_seconds: float,
    history_seconds: float = 10.0,
    signal: Signal = "sine",
    cycles: int = 1,
    sample_sink: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Measure an isolated legacy workload using generated audio and real integrity code."""
    require_external_private_root(root)
    if history_count < 0 or cycles < 1:
        raise ValueError("history must be nonnegative and cycles must be positive")
    template = root / "generated-take.wav"
    frames = write_synthetic_wav(template, take_seconds, signal)
    history_template = root / "generated-history.wav"
    write_synthetic_wav(history_template, history_seconds)
    trace = RecordingTrace(set())
    fixture = LegacyRecordingFixture(
        root,
        history_count=history_count,
        history_wav=history_template,
        mark=trace.mark,
        future_takes=cycles,
    )
    trace.historical_paths = fixture.historical_paths
    try:
        with trace.instrument():
            for cycle in range(cycles):
                context = {
                    "historical_take_count": history_count + cycle,
                    "configured_history_level": history_count,
                    "take_seconds": take_seconds,
                    "history_seconds": history_seconds,
                    "signal": signal,
                    "cycle": cycle,
                    "workload": "idle",
                    "capture_adapter": "inert_generated_pcm",
                    "backend": "legacy",
                }
                trace.measure("current_before_record", fixture.session.current, context)
                trace.measure(
                    "library_rows_and_sessions",
                    lambda: (fixture.library.rows(), fixture.library.sessions()),
                    context,
                )
                trace.measure("record_method_acknowledged", fixture.session.start_take, context)
                trace.measure(
                    "inject_synthetic_confirmed_frames",
                    lambda: fixture.processes[-1].feed(template, frames),
                    context,
                )
                state = trace.measure("poll_during_record", fixture.session.current, context)
                if not isinstance(state, capture_module.CaptureState):
                    raise AssertionError("legacy capture returned an unexpected state")
                if state.elapsed_seconds != frames / SAMPLE_RATE_HZ:
                    raise AssertionError("confirmed sample duration disagrees with injected frames")
                stopped = trace.measure("stop_to_review", fixture.session.stop_take, context)
                if (
                    not isinstance(stopped, capture_module.CaptureState)
                    or stopped.metrics is None
                    or stopped.metrics.duration_seconds != frames / SAMPLE_RATE_HZ
                ):
                    raise AssertionError("sealed duration disagrees with injected frames")
                trace.measure("save_and_next_model_ready", fixture.save_and_select_next, context)
                trace.historical_paths.add(
                    fixture.processes[-1].path.with_suffix("").with_suffix(".wav")
                )
    finally:
        if sample_sink is not None:
            sample_sink.extend(trace.samples)
        fixture.close()
    return trace.samples


def summarize(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report nearest-rank empirical quantiles, always retaining their sample count."""
    groups: dict[tuple[object, ...], list[dict[str, Any]]] = defaultdict(list)
    # The configured design point groups samples; the per-sample history depth does not.
    # Grouping on a value that changes every cycle would split a hundred-cycle case into a
    # hundred single-sample groups whose p50, p95 and p99 are all the same observation.
    dimensions = (
        "operation",
        "configured_history_level",
        "durable_take_count_at_start",
        "take_seconds",
        "signal",
        "workload",
    )
    for sample in samples:
        groups[tuple(sample.get(key) for key in dimensions)].append(sample)
    summaries = []
    for key, group in groups.items():
        durations = sorted(item["duration_ns"] / 1_000_000 for item in group)
        summaries.append(
            {
                **dict(zip(dimensions, key, strict=True)),
                "sample_count": len(group),
                "failure_count": sum(item["status"] != "ok" for item in group),
                "latency_ms": {
                    label: {
                        "value_ms": durations[max(0, math.ceil(fraction * len(durations)) - 1)],
                        "sample_count": len(durations),
                        "required_sample_count": group_requirement(label),
                        # A nearest-rank quantile of too few samples is just the maximum.
                        "is_sample_maximum": math.ceil(fraction * len(durations)) >= len(durations),
                        "underpowered": len(durations) < group_requirement(label),
                    }
                    for label, fraction in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
                },
                "wav_open_count": sum(item["wav_open_count"] for item in group),
                "wav_inspection_count": sum(item["wav_inspection_count"] for item in group),
                "historical_wav_open_count": sum(
                    item["historical_wav_open_count"] for item in group
                ),
            }
        )
    return summaries


# A bound claimed from fewer than this many independent observations is reported as
# underpowered rather than passed. A single memory or layout reading is one run's luck.
SCALAR_SAMPLE_REQUIREMENT = 3


def scalar_verdict(row: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Judge one non-latency row against the single threshold family it declares.

    A row whose threshold family has no evaluator here, or whose observation carries no
    value of the declared kind, is not-measured. Silence is never assent: the absence of
    an evaluator must not read as a satisfied bound.
    """

    families = {
        "thresholds_bytes": "value_bytes",
        "thresholds_count": "value_count",
        "thresholds_points": "value_points",
    }
    for name, key in families.items():
        if name not in row:
            continue
        limit = float(row[name]["maximum"])
        value = observation.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return {
                "verdict": "not-measured",
                "reason": f"The observation for this row declares no {key}.",
            }
        return {
            "observed": {key: float(value)},
            "thresholds": {"maximum": limit},
            "verdict": "fail" if float(value) > limit else "pass",
        }
    if row.get("thresholds_exact"):
        agreed = observation.get("exact")
        if not isinstance(agreed, bool):
            return {
                "verdict": "not-measured",
                "reason": "The observation for this row declares no exact result.",
            }
        return {
            "observed": {"exact": agreed},
            "thresholds": {"exact": True},
            "verdict": "pass" if agreed else "fail",
        }
    return {
        "verdict": "not-measured",
        "reason": "No evaluator exists for this row's declared thresholds.",
    }


def acceptance_verdicts(
    summaries: list[dict[str, Any]],
    *,
    backend: str,
    observations: dict[str, dict[str, Any]] | None = None,
    required_cases: list[dict[str, Any]] | None = None,
    repetitions: int = 1,
) -> dict[str, dict[str, Any]]:
    """Judge every section 12 row, defaulting to not-measured rather than to success.

    A row is only eligible for "pass" when this run measured the row's own boundary. A
    substitute boundary is forced to "not-measured" however good its numbers look, which
    is the difference between evidence and a surrogate that reads like evidence.
    """

    measured: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for summary in summaries:
        measured[str(summary["operation"])].append(summary)
    readings = observations or {}
    verdicts: dict[str, dict[str, Any]] = {}
    for row_id, row in ACCEPTANCE_ROWS.items():
        gap = NATIVE_NOT_MEASURED.get(row_id) if backend == "native" else None
        if (
            backend == "native"
            and row_id == "stop_reaches_controller"
            and "stop_model_transition" in measured
        ):
            gap = {
                "reason": (
                    "The native Stop observation ends at local stopping-state publication; "
                    "no correlated helper/controller acknowledgment was measured."
                ),
                "substitute_metric_name": "stop_model_transition",
                "owner": "native recorder receipt measurement",
            }
        entry: dict[str, Any] = {
            "doc_line": row["doc_line"],
            "doc_anchor": row["doc_anchor"],
            "row": row["row"],
            "boundary_is_row_definition": gap is None,
        }
        if gap is not None:
            # Naming a substitute is not the same as having measured one. A row whose
            # declared substitute this run never emitted says so, rather than leaving the
            # name to imply a number exists somewhere.
            entry.update(
                verdict="not-measured",
                substitute_measured=gap["substitute_metric_name"] in measured,
                **gap,
            )
            verdicts[row_id] = entry
            continue
        if "thresholds_ms" not in row:
            # Memory, count, exactness and layout rows are scalar bounds, not quantiles.
            # They are judged only from an observation that names its own boundary.
            observation = readings.get(row_id)
            if not isinstance(observation, dict) or not observation.get("measured_boundary"):
                entry.update(
                    verdict="not-measured",
                    reason="This run produced no observation naming the row's own boundary.",
                    owner="benchmark",
                    substitute_metric_name="none",
                )
                verdicts[row_id] = entry
                continue
            count = observation.get("sample_count")
            entry.update(
                measured_boundary=str(observation["measured_boundary"]),
                sample_count=count if isinstance(count, int) and not isinstance(count, bool) else 0,
                required_sample_count=SCALAR_SAMPLE_REQUIREMENT,
            )
            entry.update(scalar_verdict(row, observation))
            if entry["verdict"] == "pass" and entry["sample_count"] < SCALAR_SAMPLE_REQUIREMENT:
                entry["verdict"] = "underpowered"
            if entry["verdict"] == "not-measured":
                entry.setdefault("owner", "benchmark")
                entry.setdefault("substitute_metric_name", "none")
            verdicts[row_id] = entry
            continue
        groups = [
            group
            for operation in row.get("operations", (row_id,))
            for group in measured.get(operation, [])
        ]
        scoped = [group for group in groups if in_scope(row, group)]
        entry["reported_separately"] = [
            describe_group(group) for group in groups if group not in scoped
        ]
        requirements = required_latency_design_points(
            row_id, row, required_cases or [], repetitions
        )
        for point in requirements:
            matches = [
                group
                for group in scoped
                if all(group.get(key) == value for key, value in point["dimensions"].items())
            ]
            point["sample_count"] = max(
                (int(group["sample_count"]) for group in matches), default=0
            )
        entry["required_design_points"] = requirements
        entry["missing_required_design_points"] = [
            point for point in requirements if point["sample_count"] == 0
        ]
        entry["underpowered_required_design_points"] = [
            point
            for point in requirements
            if 0 < point["sample_count"] < point["required_sample_count"]
        ]
        required_points_powered = all(
            point["sample_count"] >= point["required_sample_count"] for point in requirements
        )
        if not scoped:
            entry.update(
                verdict="not-measured",
                reason=(
                    "This run produced no sample at the row's own boundary and condition."
                    if groups
                    else "This run produced no sample for the row's own boundary."
                ),
                owner="benchmark",
                substitute_metric_name="none",
            )
            verdicts[row_id] = entry
            continue
        thresholds = row.get("thresholds_ms", {})
        required = max(group_requirement(label) for label in thresholds) if thresholds else 0
        # A group with too few samples can still fail a bound, which is real evidence, but
        # it can never be what establishes one. At least one powered group is required.
        powered = [group for group in scoped if int(group["sample_count"]) >= required]
        observed: dict[str, float] = {}
        failed = False
        for label, limit in thresholds.items():
            worst = max(float(group["latency_ms"][label]["value_ms"]) for group in scoped)
            observed[label] = worst
            failed = failed or worst > limit
        entry.update(
            observed_ms=observed,
            thresholds_ms=thresholds,
            required_sample_count=required,
            powered_group_count=len(powered),
            underpowered_groups=[describe_group(group) for group in scoped if group not in powered],
            sample_count=min(int(group["sample_count"]) for group in scoped),
            verdict="fail"
            if failed
            else ("pass" if powered and required_points_powered else "underpowered"),
        )
        verdicts[row_id] = entry
    return verdicts


def required_latency_design_points(
    row_id: str, row: dict[str, Any], cases: list[dict[str, Any]], repetitions: int
) -> list[dict[str, Any]]:
    """Required powered groups come from the declared profile, never observed availability.

    Short exploratory/long-take groups remain visible in quantile summaries. A profile
    that allocates enough observations to establish a row must actually deliver them at
    that exact operation/library/duration/signal/workload point. Plain Save never supplies
    observations for the advancing operation, and the two metadata operations stay distinct.
    """

    required = max(group_requirement(label) for label in row["thresholds_ms"])
    points: dict[tuple[object, ...], dict[str, Any]] = {}
    for case in cases:
        for operation in row.get("operations", (row_id,)):
            dimensions = {
                "operation": operation,
                "configured_history_level": case["history_count"],
                "durable_take_count_at_start": case["history_count"],
                "take_seconds": case["take_seconds"],
                "signal": case["signal"],
                "workload": case["workload"],
            }
            if not in_scope(row, dimensions):
                continue
            planned = (
                max(20, case["cycles"])
                if operation in ("metadata_snapshot_at_1000", "next_missing_lookup")
                else case["cycles"]
            )
            key = tuple(dimensions.values())
            point = points.setdefault(
                key,
                {
                    "dimensions": dimensions,
                    "planned_sample_count": 0,
                    "required_sample_count": required,
                },
            )
            point["planned_sample_count"] += planned * repetitions
    return [point for point in points.values() if point["planned_sample_count"] >= required]


def in_scope(row: dict[str, Any], group: dict[str, Any]) -> bool:
    """True when this group was measured at the condition the row is defined at."""

    for dimension, bounds in row.get("scope", {}).items():
        value = group.get(dimension)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        if "min" in bounds and value < bounds["min"]:
            return False
        if "max" in bounds and value > bounds["max"]:
            return False
    return True


def describe_group(group: dict[str, Any]) -> dict[str, Any]:
    """Name a group by its design point, so an excluded group stays visible."""

    return {
        key: group.get(key)
        for key in (
            "operation",
            "configured_history_level",
            "durable_take_count_at_start",
            "take_seconds",
            "signal",
            "workload",
            "sample_count",
        )
    }


def group_requirement(label: str) -> int:
    """Smallest sample count at which a nearest-rank quantile is not just the maximum."""

    return {"p50": 20, "p95": 20, "p99": 100}[label]


def acceptance_passed(report: dict[str, Any]) -> bool:
    """True only when every row was measured at its own boundary and every one passed.

    An unmeasured row keeps this false by construction. That is the point: a headless
    synthetic run cannot retire rows that need a display, a device or a person, and this
    flag must never be the thing that makes a physical gate look satisfied.
    """

    verdicts = report.get("acceptance_rows")
    if not isinstance(verdicts, dict) or set(verdicts) != set(ACCEPTANCE_ROWS):
        return False
    if report.get("failure") is not None:
        return False
    if any(sample.get("status") != "ok" for sample in report.get("samples", [])):
        return False
    identity = report.get("host", {})
    if identity.get("swift_configuration") != "release" or identity.get("sanitizers"):
        return False
    # Preserve every shortfall. Only new, fully validated hard-limit evidence may
    # explain one; an old true completion flag or a free-standing allowance cannot.
    short = report.get("short_take_cycles", 0)
    permitted = report.get("permitted_maximum_duration_limit_shortfall_cycles", 0)
    if type(short) is not int or type(permitted) is not int or short < 0 or permitted < 0:
        return False
    if short or permitted or report.get("maximum_duration_limit_evidence"):
        from recording_native_benchmark import (
            CAPTURE_EVIDENCE_VERSION,
            MAXIMUM_DURATION_LIMIT,
            validate_capture_cycle,
        )

        evidence = report.get("maximum_duration_limit_evidence")
        if (
            report.get("capture_evidence_version") != CAPTURE_EVIDENCE_VERSION
            or not isinstance(evidence, list)
            or len(evidence) != permitted
            or short != permitted
        ):
            return False
        identities = set()
        try:
            for record in evidence:
                if (
                    not isinstance(record, dict)
                    or record.get("capture_evidence_version") != CAPTURE_EVIDENCE_VERSION
                ):
                    return False
                if not validate_capture_cycle(
                    record["agreement"],
                    record["boundary"],
                    completion_policy=MAXIMUM_DURATION_LIMIT,
                    expected_frames=28_800_000,
                    recorder_build=record["recorder_build"],
                ):
                    return False
                identity = (
                    record["agreement"]["selected_take_id"],
                    record["agreement"]["final_waveform_generation"],
                )
                if identity in identities:
                    return False
                identities.add(identity)
        except (KeyError, TypeError, ValueError, RuntimeError):
            return False
    return all(entry.get("verdict") == "pass" for entry in verdicts.values())


def host_identity() -> dict[str, Any]:
    """Record exact code/runtime/host configuration without hostname or user audio paths."""

    def command(arguments: list[str]) -> str:
        completed = subprocess.run(
            arguments, cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False
        )
        return completed.stdout.strip() if completed.returncode == 0 else "unavailable"

    source_paths = [
        *sorted((REPOSITORY_ROOT / "src/cole_voice_lab").rglob("*.py")),
        Path(__file__),
        Path(__file__).with_name("recording_fixtures.py"),
        Path(__file__).with_name("recording_native_benchmark.py"),
        REPOSITORY_ROOT / "scripts/recording_native_scratch.py",
    ]
    files = {}
    for path in source_paths:
        with path.open("rb") as stream:
            files[str(path.relative_to(REPOSITORY_ROOT))] = hashlib.file_digest(
                stream, "sha256"
            ).hexdigest()
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_version": sys.version,
        "python_executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "cpu_count": os.cpu_count(),
        "process_id": os.getpid(),
        "git_revision": command(["git", "rev-parse", "HEAD"]),
        "git_worktree_status": command(["git", "status", "--porcelain"]),
        "macos_build": command(["sw_vers", "-buildVersion"]) if sys.platform == "darwin" else None,
        "hardware_model": command(["sysctl", "-n", "hw.model"])
        if sys.platform == "darwin"
        else None,
        "physical_memory_bytes": command(["sysctl", "-n", "hw.memsize"])
        if sys.platform == "darwin"
        else None,
        "source_sha256": files,
        "build_kind": "python_source_with_inert_capture",
    }


def run_native_backend(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Measure the real release native stack, declaring what a headless run cannot reach."""

    from recording_native_benchmark import (
        NATIVE_ACCEPTANCE,
        NATIVE_SMOKE,
        NativeBenchmarkUnsafeShutdown,
        merge_observations,
        native_case_bytes,
        release_binaries,
        run_native_case,
    )

    cases = list(NATIVE_SMOKE if arguments.profile == "smoke" else NATIVE_ACCEPTANCE)
    estimated_bytes = arguments.repetitions * sum(native_case_bytes(case) for case in cases)
    if estimated_bytes > arguments.disk_budget_mib * 1024 * 1024:
        parser.error(
            f"estimated fixture size {estimated_bytes} bytes exceeds the declared disk budget"
        )
    root = Path(tempfile.mkdtemp(prefix="cole-recording-benchmark-")).resolve()
    require_external_private_root(root)
    if shutil.disk_usage(root).free < estimated_bytes + 256 * 1024 * 1024:
        parser.error("insufficient free space for declared synthetic fixtures and 256 MiB reserve")
    # Building the release products before the report is stamped keeps the recorded
    # identity and the binaries that produced the numbers the same thing.
    binaries = release_binaries()
    report: dict[str, Any] = {
        "schema_version": "cole-voice-lab.recording-benchmark/v1",
        "synthetic_only": True,
        "backend": "native",
        "profile": arguments.profile,
        "created_at": datetime.now(UTC).isoformat(),
        "host": host_identity() | binaries | {"build_kind": "release_native_three_process_stack"},
        "disk_budget_bytes": arguments.disk_budget_mib * 1024 * 1024,
        "estimated_fixture_bytes": estimated_bytes,
        "repetitions": arguments.repetitions,
        "cases": cases,
        "case_save_action_counts": [],
        "unsupported_metrics": NATIVE_NOT_MEASURED,
        "quantile_interpretation": (
            "Empirical nearest-rank; small samples do not establish tail latency."
        ),
        "samples": [],
    }
    status = 0
    results: list[dict[str, Any]] = []
    failed_cases: list[dict[str, Any]] = []
    print(json.dumps({"event": "benchmark_started", "report_directory": str(root)}), flush=True)
    try:
        for repeat in range(arguments.repetitions):
            for case_number, case in enumerate(cases):
                case_root = root / f"repeat-{repeat:03d}-case-{case_number:03d}"
                case_root.mkdir(mode=0o700)
                # One case that cannot run must not discard the evidence from the others.
                # It is recorded as a failure, which keeps the acceptance flag false.
                try:
                    result = run_native_case(
                        case_root, binaries=binaries, sample_sink=report["samples"], **case
                    )
                except Exception as error:  # noqa: BLE001 - every case failure is reportable
                    failed_case: dict[str, Any] = {
                        "repeat": repeat,
                        "case_number": case_number,
                        "artifact_root": str(case_root),
                        "design_point": case,
                        "error_code": type(error).__name__,
                        "detail": str(error)[:2000],
                        # A terse message is not enough to fix a case that failed
                        # during a run nobody was watching.
                        "traceback": "".join(
                            traceback.format_exception(type(error), error, error.__traceback__)
                        )[-4000:],
                    }
                    if isinstance(error, NativeBenchmarkUnsafeShutdown):
                        failed_case["unsafe_shutdown_failures"] = [
                            {"error_code": type(failure).__name__, "detail": str(failure)[:2000]}
                            for failure in error.failures
                        ]
                    failed_cases.append(failed_case)
                    print(
                        json.dumps({"event": "case_failed", "case": case_number, **case}),
                        flush=True,
                    )
                    if isinstance(error, NativeBenchmarkUnsafeShutdown):
                        # Keep the partial aggregate, but never measure alongside a
                        # fixture worker whose shutdown could not be confirmed.
                        raise
                    continue
                results.append(result | {"case": case})
                report["case_save_action_counts"].append(
                    {
                        "repeat": repeat,
                        "design_point": case,
                        "counts": result["save_action_counts"],
                    }
                )
                print(
                    json.dumps(
                        {
                            "event": "case_completed",
                            "repeat": repeat,
                            "case": case_number,
                            "design_point": case,
                            "history_count": result["history_count"],
                            "unanalyzed_takes_at_start": result["unanalyzed_takes_at_start"],
                        }
                    ),
                    flush=True,
                )
    except NativeBenchmarkUnsafeShutdown:
        report["aborted_remaining_cases"] = True
    except BaseException as error:
        failed_cases.append({"error_code": type(error).__name__, "detail": str(error)[:2000]})
    finally:
        if failed_cases:
            report["failure"] = {"failed_cases": failed_cases}
            status = 1
        report["short_take_cycles"] = sum(result.get("short_take_cycles", 0) for result in results)
        report["capture_evidence_version"] = "cole-voice-lab.recording-benchmark-capture/v2"
        report["maximum_duration_limit_evidence"] = [
            evidence
            for result in results
            for evidence in result.get("maximum_duration_limit_evidence", [])
        ]
        report["permitted_maximum_duration_limit_shortfall_cycles"] = sum(
            result.get("permitted_maximum_duration_limit_shortfall_cycles", 0) for result in results
        )
        report["observations"] = merge_observations(results)
        finish_report(report, root, status, observations=report["observations"])
    return status


def finish_report(
    report: dict[str, Any],
    root: Path,
    status: int,
    *,
    observations: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Summarize, judge and retain one benchmark report and its own raw trace."""

    report["summaries"] = summarize(report["samples"])
    report["acceptance_rows"] = acceptance_verdicts(
        report["summaries"],
        backend=report["backend"],
        observations=observations,
        required_cases=report.get("cases") if report["backend"] == "native" else None,
        repetitions=report.get("repetitions", 1),
    )
    report["rows_not_measured"] = sorted(
        row_id
        for row_id, entry in report["acceptance_rows"].items()
        if entry["verdict"] == "not-measured"
    )
    # Computed last and never assigned True by hand. An unmeasured row keeps it false.
    report["release_acceptance_passed"] = acceptance_passed(report)
    report["completed_at"] = datetime.now(UTC).isoformat()
    report_path = root / "report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    report_path.chmod(0o600)
    trace_path = root / "trace.jsonl"
    trace_path.write_text(
        "".join(json.dumps(sample, default=str) + "\n" for sample in report["samples"]),
        encoding="utf-8",
    )
    trace_path.chmod(0o600)
    print(json.dumps({"event": "benchmark_finished", "report": str(report_path), "status": status}))
    return report_path


def main(argv: list[str] | None = None) -> int:
    """Write private retained reports/fixtures; refuse unsynthetic or unavailable backends."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true", required=True)
    parser.add_argument("--backend", choices=("legacy", "native"), required=True)
    parser.add_argument("--profile", choices=("smoke", "acceptance"), required=True)
    parser.add_argument("--disk-budget-mib", type=int, default=8192)
    parser.add_argument("--repetitions", type=int, default=1)
    arguments = parser.parse_args(argv)
    # Every retained fixture, report and trace here is private by construction.
    os.umask(0o077)
    if arguments.repetitions < 1 or arguments.disk_budget_mib < 1:
        parser.error("repetitions and disk budget must be positive")
    if arguments.backend == "native":
        return run_native_backend(arguments, parser)
    cases = (
        [(history, 10.0, "sine", 1, 10.0) for history in (0, 4)]
        if arguments.profile == "smoke"
        else [
            (history, seconds, signal, 1, 1.0)
            for history in (0, 4, 36, 1000)
            for seconds in (1.0, 10.0, 30.0, 120.0, 600.0)
            for signal in ("sine", "silence")
        ]
        + [(0, 1.0, "sine", 100, 1.0)]
    )
    # Retain all evidence; do not delete earlier cases to hide actual disk requirements.
    estimated_bytes = arguments.repetitions * sum(
        int((history * history_seconds + (cycles + 1) * seconds + history_seconds) * 144_000)
        + (history + cycles + 2) * 4096
        for history, seconds, _signal, cycles, history_seconds in cases
    )
    if estimated_bytes > arguments.disk_budget_mib * 1024 * 1024:
        parser.error(
            f"estimated fixture size {estimated_bytes} bytes exceeds the declared disk budget"
        )
    root = Path(tempfile.mkdtemp(prefix="cole-recording-benchmark-")).resolve()
    require_external_private_root(root)
    if shutil.disk_usage(root).free < estimated_bytes + 256 * 1024 * 1024:
        parser.error("insufficient free space for declared synthetic fixtures and 256 MiB reserve")
    report: dict[str, Any] = {
        "schema_version": "cole-voice-lab.recording-benchmark/v1",
        "synthetic_only": True,
        "backend": arguments.backend,
        "profile": arguments.profile,
        "created_at": datetime.now(UTC).isoformat(),
        "host": host_identity(),
        "disk_budget_bytes": arguments.disk_budget_mib * 1024 * 1024,
        "estimated_fixture_bytes": estimated_bytes,
        "repetitions": arguments.repetitions,
        "unsupported_metrics": LEGACY_GAPS
        if arguments.backend == "legacy"
        else NATIVE_NOT_MEASURED,
        "quantile_interpretation": (
            "Empirical nearest-rank; small samples do not establish tail latency."
        ),
        "samples": [],
    }
    status = 0
    print(json.dumps({"event": "benchmark_started", "report_directory": str(root)}), flush=True)
    try:
        for repeat in range(arguments.repetitions):
            for case_number, (history, seconds, signal, cycles, history_seconds) in enumerate(
                cases
            ):
                case_root = root / f"repeat-{repeat:03d}-case-{case_number:03d}"
                case_root.mkdir(mode=0o700)
                run_legacy_case(
                    case_root,
                    history_count=history,
                    take_seconds=seconds,
                    history_seconds=history_seconds,
                    signal=signal,  # type: ignore[arg-type]
                    cycles=cycles,
                    sample_sink=report["samples"],
                )
                print(
                    json.dumps({"event": "case_completed", "repeat": repeat, "case": case_number}),
                    flush=True,
                )
    except Exception as error:
        report["failure"] = {"error_code": type(error).__name__}
        status = 1
    finally:
        finish_report(report, root, status)
    return status


if __name__ == "__main__":
    raise SystemExit(main())

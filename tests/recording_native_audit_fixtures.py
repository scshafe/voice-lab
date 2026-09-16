"""Inert report factories matching native paired-span diagnostic serialization."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import cache
from typing import Any

from test_recording_contracts import synthetic_contracts

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.recording_contracts import CaptureReservation

HISTORICAL_SOURCE = {
    "kind": "selected_at_issue",
    "take_id": "take-historical",
    "source_path": "/synthetic/assets/history/original.wav",
    "source_sha256": "b" * 64,
    "operation_id": None,
    "device": 1,
    "inode": 99,
}


@cache
def _capture_templates() -> dict[str, Any]:
    return {
        key: value.model_dump(mode="json")
        for key, value in synthetic_contracts().items()
        if key in ("capture-reservation", "capture-seal")
    }


def set_capture_evidence(
    native: dict[str, Any],
    cycle: int,
    *,
    expected: int = 48_000,
    frames: int = 48_000,
    before_frames: int | None = None,
    policy: str = "exact-frames",
) -> None:
    """Construct independent invented reservation/seal/final-review observations in memory."""
    source = native["captured_sources"][cycle]
    before_frames = frames if before_frames is None else before_frames
    start = 1000 + cycle * 1000
    reservation = deepcopy(_capture_templates()["capture-reservation"])
    take_id = source["take_id"]
    reservation.update(
        take_id=take_id,
        operation_id=f"reserve-{cycle}",
        partial_path=f"tmp/recording-takes/{take_id}/capture.partial.wav",
        original_path=f"tmp/recording-takes/{take_id}/original.wav",
    )
    bound = CaptureReservation.model_validate_json(json.dumps(reservation))
    seal = deepcopy(_capture_templates()["capture-seal"])
    seal.update(
        take_id=take_id,
        reservation_sha256=canonical_sha256(bound),
        original_path=reservation["original_path"],
        original_sha256=source["source_sha256"],
        original_size_bytes=44 + frames * 3,
    )
    seal["audio"].update(frames=frames, duration_seconds=frames / 48_000)
    seal["continuity"].update(
        frames=frames,
        received_frames=frames,
        last_sample_end=seal["continuity"]["first_sample_time"] + frames,
        wav_sha256=source["source_sha256"],
        wav_size_bytes=44 + frames * 3,
        stop_reason="maximum-duration" if policy == "maximum-duration-limit" else "requested",
    )
    state = {
        "take_id": take_id,
        "generation": reservation["process_generation"],
        "phase": "recording",
        "can_stop": True,
        "written_frames": before_frames,
    }
    automatic = policy == "maximum-duration-limit"
    stop = {
        "issued_uptime_ns": None if automatic else start - 90,
        "before": state | ({"phase": "stopping", "can_stop": False} if automatic else {}),
        "at_issue": None if automatic else state.copy(),
        "after_call": None if automatic else state | {"phase": "stopping", "can_stop": False},
        "model_stop_called": not automatic,
        "model_stop_accepted": not automatic,
        "reason": "maximum_duration_policy_no_stop" if automatic else "model_stop_accepted",
        "controller_acknowledgment_proven": False,
    }
    boundary = {
        "cycle": cycle,
        "expected_frames": expected,
        "completion_policy": policy,
        "reservation": reservation,
        "reservation_sha256": seal["reservation_sha256"],
        "seal": seal,
        "sealed_frames": frames,
        "final_waveform_frames": frames,
        "terminal_reason": seal["continuity"]["stop_reason"],
        "capture_started_uptime_ns": start - 400,
        "terminal_observed_uptime_ns": start - 100,
        "wait_timed_out": False,
        "wait_exit": "terminal_before_target"
        if before_frames < expected
        else "frame_target_observed",
        "timer_frames_before_issue": before_frames,
        "stop_attempt": stop,
    }
    agreement = {
        "cycle": cycle,
        "expected_frames": expected,
        "timer_frames": before_frames,
        "timer_frames_before_issue": before_frames,
        "final_timer_frames": frames,
        "final_timer_mode": "initial-review-retained-duration",
        "final_timer_source": "RecordingMediaTimer.durationFrames",
        "final_timer_observed_uptime_ns": start + 300,
        "final_waveform_frames": frames,
        "final_waveform_take_id": take_id,
        "final_waveform_generation": reservation["process_generation"],
        "selected_source_frames": frames,
        "selected_take_id": take_id,
        "selected_source_sha256": source["source_sha256"],
        "selected_source_path": reservation["original_path"],
        "selected_range_start_frame": 0,
        "selected_range_end_frame": frames,
        "sealed_frames": frames,
        "exact": True,
        "reached_configured_frames": frames == expected,
        "reached_exact_configured_frames": frames == expected,
    }
    native["recorder_build"] = seal["recorder_build"]
    native["completion_policy"] = policy
    native.setdefault("capture_boundaries", [])
    for key, record in (("capture_boundaries", boundary), ("timer_frame_agreement", agreement)):
        entries = native[key]
        existing = next((index for index, row in enumerate(entries) if row["cycle"] == cycle), None)
        if existing is None:
            entries.append(record)
        else:
            entries[existing] = record


def add_client_span(
    native: dict[str, Any],
    *,
    name: str,
    source: dict[str, Any] | None,
    started: int,
    ended: int,
    parent_span_id: str | None = None,
    outcome: str = "success",
    trace_id: str | None = None,
    request_id: str | None = None,
    ending_request_id: str | None = None,
    byte_count: int | None = None,
    device: int | None = None,
    inode: int | None = None,
) -> str:
    """Add one actual begin/end pair; optional response IDs appear only at end."""
    events = native["diagnostics"]["events"]
    span_id = f"span-{len(events) // 2:05d}"
    source = source or {}
    context = {
        "trace_id": trace_id or f"trace-{source.get('take_id', 'metadata')}",
        "operation_id": source.get("operation_id"),
        "request_id": request_id,
        "preparation_id": f"prepare-{source['take_id']}" if source.get("take_id") else None,
        "take_id": source.get("take_id"),
        "source_path": source.get("source_path"),
        "source_sha256": source.get("source_sha256"),
        "parent_span_id": parent_span_id,
    }
    for phase, uptime in (("begin", started), ("end", ended)):
        events.append(
            {
                "span_id": span_id,
                "name": name,
                "phase": phase,
                "uptime_nanoseconds": uptime,
                "context": context
                | (
                    {"request_id": ending_request_id}
                    if phase == "end" and ending_request_id
                    else {}
                ),
                "outcome": outcome if phase == "end" else None,
                "error_code": None,
                "byte_count": byte_count if phase == "end" else None,
                "device": device if phase == "end" else None,
                "inode": inode if phase == "end" else None,
            }
        )
    native["diagnostics"]["retained_event_count"] = len(events)
    return span_id


def _branch(
    native: dict[str, Any], source: dict[str, Any], *, start: int, waveform: bool, cache_hit: bool
) -> None:
    prefix = "waveform" if waveform else "playback"
    parent = add_client_span(
        native, name=f"{prefix}.prepare", source=source, started=start, ended=start + 100
    )
    opened = add_client_span(
        native,
        name="private_file.open",
        source=source,
        started=start + 2,
        ended=start + 8,
        parent_span_id=parent,
        byte_count=96_044,
        device=source["device"],
        inode=source["inode"],
    )
    add_client_span(
        native,
        name="private_file.validate_identity",
        source=source,
        started=start + 10,
        ended=start + 12,
        parent_span_id=opened,
        device=source["device"],
        inode=source["inode"],
    )
    if waveform:
        add_client_span(
            native,
            name="waveform.cache_load",
            source=source,
            started=start + 14,
            ended=start + 16,
            parent_span_id=parent,
            outcome="cache_hit" if cache_hit else "cache_miss",
        )
        native["diagnostics"][f"waveform_cache_{'hit' if cache_hit else 'miss'}_count"] += 1
    if not waveform or not cache_hit:
        for ordinal, name in enumerate(("private_file.read", "private_file.sha256")):
            add_client_span(
                native,
                name=name,
                source=source,
                started=start + 20 + ordinal * 10,
                ended=start + 25 + ordinal * 10,
                parent_span_id=opened,
                byte_count=96_044,
                device=source["device"],
                inode=source["inode"],
            )
        add_client_span(
            native,
            name=f"{prefix}.av_audio_file_open",
            source=source,
            started=start + 40,
            ended=start + 45,
            parent_span_id=parent,
        )
        if waveform:
            add_client_span(
                native,
                name="waveform.decode",
                source=source,
                started=start + 50,
                ended=start + 90,
                parent_span_id=parent,
                byte_count=96_000,
            )


def native_audit_fixture(
    *,
    cycles: int = 1,
    save_operation: str = "plain_save_durable_acknowledgment",
    waveform_cache_hit: bool = False,
    include_metadata: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a complete synthetic report/audit; this function never opens any source."""
    assert cycles > 0
    assert save_operation in (
        "plain_save_durable_acknowledgment",
        "save_next_durable_acknowledgment",
    )
    advancing = save_operation == "save_next_durable_acknowledgment"
    footprint = {"client_bytes": 10, "recorder_bytes": 10, "service_bytes": 10, "other_bytes": 0}
    native: dict[str, Any] = {
        "cycles": cycles,
        "capture_evidence_version": "cole-voice-lab.recording-benchmark-capture/v2",
        "clock": "mach_uptime_ns",
        "save_action_counts": {
            "completed_cycles": cycles,
            "save_next_acknowledged": cycles if advancing else 0,
            "plain_save_acknowledged": 0 if advancing else cycles,
        },
        "samples": [],
        "captured_sources": [],
        "diagnostics": {
            "schema_version": "cole-voice-lab.recording-benchmark-diagnostics/v1",
            "clock": "mach_uptime_ns",
            "event_capacity": 32_768,
            "retained_event_count": 0,
            "dropped_event_count": 0,
            "footprint_includes_observer_storage": True,
            "events": [],
            "waveform_cache_hit_count": 0,
            "waveform_cache_miss_count": 0,
        },
        "timer_frame_agreement": [
            {
                "cycle": cycle,
                "exact": True,
                "reached_configured_frames": True,
                "expected_frames": 48_000,
                "timer_frames": 48_000,
                "sealed_frames": 48_000,
            }
            for cycle in range(cycles)
        ],
        "warm_idle_footprint": footprint.copy(),
        "peak_footprint": footprint.copy(),
        "footprint_sample_count": cycles,
        "layout_max_drift_points": 0,
        "layout_measurement_count": 4,
        "history_count": 1000 + cycles,
    }
    for cycle in range(cycles):
        start = 1000 + cycle * 1000
        source = {
            "kind": "captured_take",
            "take_id": f"take-captured-{cycle}",
            "source_path": (
                f"/synthetic/assets/tmp/recording-takes/take-captured-{cycle}/original.wav"
            ),
            "source_sha256": f"{cycle + 1:064x}",
            "operation_id": None,
            "device": 1,
            "inode": 100 + cycle,
        }
        native["captured_sources"].append(source)
        set_capture_evidence(native, cycle)
        _branch(native, source, start=start, waveform=False, cache_hit=False)
        _branch(native, source, start=start + 120, waveform=True, cache_hit=waveform_cache_hit)
        native["samples"].append(
            {
                "operation": save_operation,
                "cycle": cycle,
                "started_uptime_ns": start + 600,
                "ended_uptime_ns": start + 700,
                "duration_ns": 100,
                "status": "ok",
                "take_original_path": source["source_path"],
                "advance_intent": advancing,
                "source_exemptions": [source | {"kind": "selected_at_issue"}],
                "client_held_wav_paths_before": [source["source_path"]],
                "client_held_wav_paths_after": [source["source_path"]],
            }
        )
        reserved = {
            "kind": "reserved_take",
            "take_id": source["take_id"],
            "source_path": source["source_path"],
            "source_sha256": None,
            "operation_id": f"reserve-{cycle}",
            "device": None,
            "inode": None,
        }
        # Keep Save first for intent-specific fixture consumers; timestamps retain
        # actual issue ordering, including both reader branches inside Stop→review.
        for operation, near, far in (
            ("record_to_first_synthetic_confirmed_frame", start - 400, start - 300),
            ("capture_poll_window", start - 400, start - 100),
            ("stop_model_transition", start - 90, start - 80),
            ("stop_to_sealed_review", start - 90, start + 300),
        ):
            native["samples"].append(
                {
                    "operation": operation,
                    "cycle": cycle,
                    "started_uptime_ns": near,
                    "ended_uptime_ns": far,
                    "duration_ns": far - near,
                    "status": "ok",
                    "take_original_path": source["source_path"],
                    "advance_intent": None,
                    "source_exemptions": [reserved.copy()],
                    "client_held_wav_paths_before": [],
                    "client_held_wav_paths_after": [source["source_path"]],
                }
            )
    if include_metadata:
        start = 1000 + cycles * 1000
        trace, request_id = "metadata-trace", "history-request"
        refresh = add_client_span(
            native,
            name="history.refresh",
            source=None,
            started=start + 1,
            ended=start + 90,
            trace_id=trace,
            ending_request_id=request_id,
        )
        add_client_span(
            native,
            name="service.request.history",
            source=None,
            started=start + 3,
            ended=start + 50,
            parent_span_id=refresh,
            trace_id=trace,
            ending_request_id=request_id,
        )
        add_client_span(
            native,
            name="history.application",
            source=None,
            started=start + 55,
            ended=start + 80,
            parent_span_id=refresh,
            trace_id=trace,
            request_id=request_id,
        )
        native["samples"].append(
            {
                "operation": "metadata_snapshot_at_1000",
                "cycle": 0,
                "started_uptime_ns": start,
                "ended_uptime_ns": start + 100,
                "duration_ns": 100,
                "status": "ok",
                "take_original_path": None,
                "source_exemptions": [],
                "client_held_wav_paths_before": [],
                "client_held_wav_paths_after": [],
                "request_id": request_id,
                "diagnostic_trace_id": trace,
            }
        )
    audit = [
        {
            "uptime_ns": 10,
            "pid": 11,
            "role": "service",
            "control": True,
            "path": "/synthetic/assets/service-audit-control.wav",
        },
        {
            "uptime_ns": 11,
            "pid": 12,
            "role": "worker",
            "control": True,
            "path": "/synthetic/assets/worker-audit-control.wav",
        },
        {
            "uptime_ns": 1650,
            "pid": 12,
            "role": "worker",
            "control": False,
            "path": HISTORICAL_SOURCE["source_path"],
        },
    ]
    return native, audit

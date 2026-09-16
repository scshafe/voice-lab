"""Validate native reader controls and attribute actual source work to command windows.

Descriptor holdings are lifetime evidence only. A canonical acceptance case must carry
the actual event stream and positive controls for both selected-source reader branches.
"""

from __future__ import annotations

from typing import Any

INSPECTION_NAMES = {
    "private_file.open",
    "private_file.read",
    "playback.av_audio_file_open",
    "waveform.av_audio_file_open",
    "waveform.decode",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError("Native source audit: " + message)


def _source_matches(context: dict[str, Any], source: dict[str, Any]) -> bool:
    return all(
        context.get(key) == source.get(key) for key in ("take_id", "source_path", "source_sha256")
    )


def _validated_events(native: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostic = native.get("diagnostics")
    _require(isinstance(diagnostic, dict), "missing actual client diagnostic events")
    _require(native.get("diagnostic_failure") is None, "harness reported invalid diagnostics")
    _require(
        diagnostic.get("schema_version") == "cole-voice-lab.recording-benchmark-diagnostics/v1"
        and diagnostic.get("clock") == "mach_uptime_ns"
        and diagnostic.get("event_capacity") == 32_768
        and diagnostic.get("dropped_event_count") == 0
        and diagnostic.get("footprint_includes_observer_storage") is True,
        "missing, overflowing or incompatible observer boundary",
    )
    events = diagnostic.get("events")
    _require(
        isinstance(events, list)
        and len(events) == diagnostic.get("retained_event_count")
        and 0 < len(events) <= diagnostic["event_capacity"],
        "event count differs from retained bounded evidence",
    )
    phases: set[tuple[str, str]] = set()
    for event in events:
        _require(isinstance(event, dict), "malformed event")
        context = event.get("context")
        _require(
            isinstance(event.get("span_id"), str)
            and isinstance(event.get("name"), str)
            and event.get("phase") in ("begin", "end")
            and type(event.get("uptime_nanoseconds")) is int
            and event["uptime_nanoseconds"] >= 0
            and isinstance(context, dict)
            and isinstance(context.get("trace_id"), str),
            "event lacks span, clock or attribution",
        )
        key = (event["span_id"], event["phase"])
        _require(key not in phases, "duplicate span phase")
        phases.add(key)
    sources = native.get("captured_sources")
    _require(
        isinstance(sources, list) and len(sources) == native["cycles"],
        "captured source controls do not cover every cycle",
    )
    _require(
        len({source["take_id"] for source in sources}) == len(sources), "duplicate captured source"
    )
    begins = {event["span_id"]: event for event in events if event["phase"] == "begin"}

    def completed(
        name: str, source: dict, parent: str | None = None, outcome: str = "success"
    ) -> list[dict]:
        result = []
        for event in events:
            begin = begins.get(event["span_id"])
            if (
                event["name"] == name
                and event["phase"] == "end"
                and event.get("outcome") == outcome
                and _source_matches(event["context"], source)
                and (parent is None or event["context"].get("parent_span_id") == parent)
                and begin is not None
                and begin["name"] == name
                and begin["context"] == event["context"]
                and begin["uptime_nanoseconds"] <= event["uptime_nanoseconds"]
            ):
                result.append(event)
        return result

    def lease(parent: str, source: dict, contents: bool) -> bool:
        for opened in completed("private_file.open", source, parent):
            if (
                type(opened.get("device")) is not int
                or type(opened.get("inode")) is not int
                or not isinstance(opened.get("byte_count"), int)
                or opened["byte_count"] <= 0
            ):
                continue
            if any(
                source.get(key) is not None and source[key] != opened[key]
                for key in ("device", "inode")
            ):
                continue

            def same_identity(event: dict, expected: dict = opened) -> bool:
                return all(event.get(key) == expected[key] for key in ("device", "inode"))

            if not any(
                same_identity(event)
                for event in completed("private_file.validate_identity", source, opened["span_id"])
            ):
                continue
            if not contents or all(
                any(
                    same_identity(event) and event.get("byte_count") == opened["byte_count"]
                    for event in completed(name, source, opened["span_id"])
                )
                for name in ("private_file.read", "private_file.sha256")
            ):
                return True
        return False

    for source in sources:
        _require(
            isinstance(source.get("take_id"), str)
            and isinstance(source.get("source_path"), str)
            and source["source_path"].startswith("/")
            and source["source_path"].endswith(".wav")
            and isinstance(source.get("source_sha256"), str)
            and len(source["source_sha256"]) == 64,
            "captured source lacks exact take, path or digest",
        )
        playback = any(
            lease(branch["span_id"], source, True)
            and completed("playback.av_audio_file_open", source, branch["span_id"])
            for branch in completed("playback.prepare", source)
        )
        waveform = False
        for branch in completed("waveform.prepare", source):
            parent = branch["span_id"]
            if not lease(parent, source, False):
                continue
            if completed("waveform.cache_load", source, parent, "cache_hit"):
                waveform = True
            elif (
                completed("waveform.cache_load", source, parent, "cache_miss")
                and lease(parent, source, True)
                and completed("waveform.av_audio_file_open", source, parent)
                and any(
                    event.get("byte_count", 0) > 0
                    for event in completed("waveform.decode", source, parent)
                )
            ):
                waveform = True
        _require(playback and waveform, "missing playback or waveform branch positive control")
    return events


def _exempt(event: dict, exemptions: list[dict], *, client: bool) -> bool:
    context = event["context"] if client else {"source_path": event["path"]}
    for source in exemptions:
        if context.get("source_path") != source["source_path"]:
            continue
        if client:
            if context.get("take_id") != source["take_id"]:
                continue
            if (
                source.get("source_sha256") is not None
                and context.get("source_sha256") != source["source_sha256"]
            ):
                continue
            if any(
                event.get(key) is not None
                and source.get(key) is not None
                and event[key] != source[key]
                for key in ("device", "inode")
            ):
                continue
        return True
    return False


def audited_native_samples(
    native: dict[str, Any], audit: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return attributed copies only after all controls pass; never mutate a shared sink."""
    events = _validated_events(native)
    _require(all(row.get("role") in ("service", "worker") for row in audit), "unknown process role")
    controls = [row for row in audit if row.get("control") is True]
    _require(
        {row["role"] for row in controls} == {"service", "worker"},
        "missing service or worker startup control",
    )
    controlled = {(row["pid"], row["role"]) for row in controls}
    _require(
        all((row["pid"], row["role"]) in controlled for row in audit),
        "source open lacks its process startup control",
    )
    service = [row for row in audit if row["role"] == "service" and not row.get("control", False)]
    ends = {event["span_id"]: event for event in events if event["phase"] == "end"}
    reservation_operations: dict[int, str] = {}
    result = []
    for sample in native["samples"]:
        start, end = sample["started_uptime_ns"], sample["ended_uptime_ns"]
        _require(
            type(start) is int
            and type(end) is int
            and start >= 0
            and end >= start
            and type(sample.get("duration_ns")) is int
            and sample["duration_ns"] == end - start,
            "command window has reversed or inconsistent clock bounds",
        )
        exemptions = sample.get("source_exemptions")
        _require(isinstance(exemptions, list), "command lacks its immutable source exemptions")
        for source in exemptions:
            _require(
                source.get("kind") in ("selected_at_issue", "reserved_take")
                and isinstance(source.get("take_id"), str)
                and isinstance(source.get("source_path"), str),
                "invalid command source exemption",
            )
            _require(
                (
                    source["kind"] == "selected_at_issue"
                    and isinstance(source.get("source_sha256"), str)
                )
                or (
                    source["kind"] == "reserved_take"
                    and isinstance(source.get("operation_id"), str)
                ),
                "source exemption lacks selection digest or reservation operation",
            )
            if source["kind"] == "reserved_take":
                cycle = sample["cycle"]
                _require(
                    type(cycle) is int and 0 <= cycle < len(native["captured_sources"]),
                    "reserved source has no corresponding capture cycle",
                )
                captured = native["captured_sources"][cycle]
                _require(
                    source["take_id"] == captured["take_id"]
                    and source["source_path"] == captured["source_path"]
                    and sample.get("take_original_path") == captured["source_path"],
                    "reserved exemption names a different capture source",
                )
                previous_operation = reservation_operations.setdefault(
                    cycle, source["operation_id"]
                )
                _require(
                    previous_operation == source["operation_id"],
                    "reservation operation changed across one capture cycle",
                )
        inspected = []
        offenders = []
        for event in events:
            if (
                event["phase"] != "begin"
                or event["name"] not in INSPECTION_NAMES
                or not start <= event["uptime_nanoseconds"] <= end
            ):
                continue
            context = event["context"]
            path = context.get("source_path")
            _require(isinstance(path, str), "inspection attempt lacks a source path")
            if not path.endswith(".wav"):
                continue
            finished = ends.get(event["span_id"])
            _require(
                finished is not None
                and finished["name"] == event["name"]
                and finished["context"] == context
                and finished["uptime_nanoseconds"] >= event["uptime_nanoseconds"],
                "inspection attempt lacks its completion",
            )
            evidence = event | {
                "device": finished.get("device"),
                "inode": finished.get("inode"),
                "outcome": finished.get("outcome"),
                "ended_uptime_ns": finished["uptime_nanoseconds"],
            }
            inspected.append(evidence)
            if not _exempt(evidence, exemptions, client=True):
                offenders.append(evidence)
        if sample["operation"] == "metadata_snapshot_at_1000":
            request_id, trace_id = sample.get("request_id"), sample.get("diagnostic_trace_id")
            _require(
                isinstance(request_id, str) and isinstance(trace_id, str),
                "metadata lacks actual request attribution",
            )
            correlated = [event for event in events if event["context"].get("trace_id") == trace_id]
            previous = start
            for name in ("service.request.history", "history.application", "history.refresh"):
                completed = [
                    event
                    for event in correlated
                    if event["name"] == name and event["phase"] == "end"
                ]
                _require(
                    len(completed) == 1
                    and completed[0].get("outcome") == "success"
                    and completed[0]["context"].get("request_id") == request_id
                    and previous <= completed[0]["uptime_nanoseconds"] <= end,
                    "metadata request lacks its successful applied correlated response",
                )
                previous = completed[0]["uptime_nanoseconds"]
                _require(
                    any(
                        event["name"] == name
                        and event["span_id"] == completed[0]["span_id"]
                        and event["phase"] == "begin"
                        and start <= event["uptime_nanoseconds"] <= previous
                        for event in correlated
                    ),
                    "metadata span lacks its issued boundary",
                )
        service_opens = [row for row in service if start <= row["uptime_ns"] <= end]
        result.append(
            sample
            | {
                "client_inspection_events": inspected,
                "client_historical_inspection_events": offenders,
                "client_opened_historical_paths": sorted(
                    {event["context"]["source_path"] for event in offenders}
                ),
                "service_open_events": service_opens,
                "service_historical_inspection_events": [
                    row for row in service_opens if not _exempt(row, exemptions, client=False)
                ],
            }
        )
    return result

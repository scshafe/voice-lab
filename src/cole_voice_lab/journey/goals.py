"""Goals as pre-registered n=1 experiments (port of voice-journey's ``goals.mjs``).

A goal freezes its full spec at creation: metric, direction, target, sample size,
scope. The verdict is decided by the FIRST N qualifying takes recorded after creation,
in chronological order: no optional stopping, no peeking, no "just one more take" bias.
Status is derived from the manifests on every read (the first N takes are the first N
takes, so recomputation is stable); only the spec and an explicit abandonment are ever
stored. Values that flow out are numeric aggregates only.

The goals state file keeps its format (``voice-journey.goals-state.v1``: ``version``
and ``goals``, each goal with the keys and the 16-hex ``goalId`` the JavaScript made;
the file is read and written by the API, not here). Differences from the JavaScript:
a metric name that is only an ``Object.prototype`` property (``"toString"``,
``"constructor"``, ...) is refused as ``unknown_metric`` where the JavaScript accepted
it; times are epoch milliseconds. Ties between takes at the same instant order
recording ids with ``jscompat.locale_compare_key``: ``localeCompare`` (ICU root
collation) exactly on ASCII ids, an approximation beyond ASCII.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any, NamedTuple

from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey.coach import cluster_rollup, median

Json = dict[str, Any]

GOALS_STATE_SCHEMA_VERSION = "voice-journey.goals-state.v1"


class GoalMetric(NamedTuple):
    label: str
    pick: Callable[[Any], Any]


def _picker(*keys: str) -> Callable[[Any], Any]:
    def pick(features: Any) -> Any:
        value = features
        for key in keys:
            value = js.get(value, key)
        return value

    return pick


GOAL_METRICS: Mapping[str, GoalMetric] = {
    "vibratoRateHz": GoalMetric("vibrato rate (Hz)", _picker("vibrato", "meanRateHz")),
    "vibratoExtentCents": GoalMetric(
        "vibrato extent (cents)", _picker("vibrato", "meanExtentCents")
    ),
    "cpps": GoalMetric("clarity — CPPS (dB)", _picker("quality", "cpps")),
    "longestSustainSeconds": GoalMetric(
        "longest sustain (s)", _picker("phrasing", "longestSustainedSeconds")
    ),
    "pitchP95Hz": GoalMetric("top of range — p95 (Hz)", _picker("pitch", "f0Hz", "p95")),
    "tuningMedianCentError": GoalMetric(
        "tuning error (cents)", _picker("tuning", "medianAbsCentError")
    ),
}

GOAL_DIRECTIONS = ("at_least", "at_most")
TAKES_REQUIRED_MIN = 3
TAKES_REQUIRED_MAX = 50
TAKES_REQUIRED_DEFAULT = 10


class GoalError(ValueError):
    """A refused goal spec; the API answers it with ``status_code`` (400)."""

    status_code = 400


def validate_goal_spec(body: Any = None) -> Json:
    if body is None:
        body = {}
    metric = js.to_string(js.coalesce(js.get(body, "metric"), ""))
    if metric not in GOAL_METRICS:
        raise GoalError(f"unknown_metric: expected one of {', '.join(GOAL_METRICS)}")
    direction = js.to_string(js.coalesce(js.get(body, "direction"), ""))
    if direction not in GOAL_DIRECTIONS:
        raise GoalError("invalid_direction: expected at_least or at_most")
    target = js.to_number(js.get(body, "target"))
    if not js.is_finite(target):
        raise GoalError("invalid_target: expected a finite number")
    raw_takes = js.get(body, "takesRequired")
    takes = TAKES_REQUIRED_DEFAULT if raw_takes is js.MISSING else js.to_number(raw_takes)
    if (
        not js.is_finite(takes)
        or not float(takes).is_integer()
        or takes < TAKES_REQUIRED_MIN
        or takes > TAKES_REQUIRED_MAX
    ):
        raise GoalError(
            f"invalid_takes_required: expected an integer {TAKES_REQUIRED_MIN}–{TAKES_REQUIRED_MAX}"
        )
    cluster = js.get(body, "clusterId")
    raw_title = js.get(body, "title")
    title = js.slice_utf16(js.trim(js.to_string(raw_title)), 120) if js.truthy(raw_title) else None
    return {
        "metric": metric,
        "direction": direction,
        "target": target,
        "takesRequired": int(takes),
        "clusterId": js.to_string(cluster) if js.truthy(cluster) else None,
        "title": title,
    }


def create_goal(spec: Json, *, now: float) -> Json:
    created_at = js.iso_from_ms(now)
    metric_label = GOAL_METRICS[spec["metric"]].label
    title = spec.get("title")
    if title is None:
        sign = "≥" if spec.get("direction") == "at_least" else "≤"
        cluster = spec.get("clusterId")
        scope = f" on {js.to_string(cluster)}" if js.truthy(cluster) else ""
        title = f"{metric_label} {sign} {js.to_string(spec.get('target', js.MISSING))}{scope}"
    digest = hashlib.sha256(f"{js.stringify_compact(spec)}:{created_at}".encode()).hexdigest()
    return {
        "goalId": digest[:16],
        "createdAt": created_at,
        "status": "open",
        **spec,
        "title": title,
    }


@dataclass
class GoalContext:
    """What the routes hand in: per-take feature rows, the recording to cluster map and
    the privacy-generic cluster options."""

    feature_rows: list[Any]
    cluster_by_recording: dict[Any, Any]
    cluster_options: list[Json] = field(default_factory=list)


def goal_context(rows: list[Json] | None, features_manifest: Any) -> GoalContext:
    cluster_by_recording: dict[Any, Any] = {}
    for row in rows or []:
        cluster_id = js.get(row, "lyricMatchCluster")
        if js.truthy(cluster_id):
            cluster_by_recording[js.get(row, "recordingId")] = cluster_id
    rollup = sorted(cluster_rollup(rows or []), key=lambda cluster: -cluster["takeCount"])
    options = [
        {
            "clusterId": cluster["clusterId"],
            "label": cluster["label"] if cluster["label"] is not None else cluster["clusterId"],
            "takeCount": cluster["takeCount"],
        }
        for cluster in rollup[:30]
    ]
    feature_rows = js.coalesce(js.get(features_manifest, "results"), [])
    return GoalContext(feature_rows, cluster_by_recording, options)


def _missing_pick(features: Any) -> Any:
    return js.MISSING


def _slice_end(end: Any, length: int) -> int:
    """The end index ``Array.prototype.slice(0, end)`` uses (undefined is the length,
    a negative end counts from the back)."""

    if end is js.MISSING:
        return length
    number = js.to_number(end)
    if number != number or number == -math.inf:
        return 0
    if number == math.inf:
        return length
    whole = math.trunc(number)
    return max(length + whole, 0) if whole < 0 else min(whole, length)


def evaluate_goal(goal: Json, context: GoalContext) -> Json:
    created_at_ms = js.date_parse(js.get(goal, "createdAt"))
    metric = GOAL_METRICS.get(js.to_string(js.get(goal, "metric")))
    pick = metric.pick if metric is not None else _missing_pick
    goal_cluster = js.get(goal, "clusterId")
    qualifying: list[tuple[float, str, Json]] = []
    for row in context.feature_rows or []:
        features = js.get(row, "features")
        captured_at = js.get(row, "capturedAt")
        if js.get(row, "status") != "completed" or not js.truthy(features):
            continue
        if not js.truthy(captured_at):
            continue
        at = js.date_parse(captured_at)
        if at is None or (created_at_ms is not None and at <= created_at_ms):
            continue
        recording_id = js.get(row, "recordingId")
        if (
            js.truthy(goal_cluster)
            and context.cluster_by_recording.get(recording_id, js.MISSING) != goal_cluster
        ):
            continue
        value = pick(features)
        if not js.is_finite(value):
            continue
        take: Json = {"recordingId": recording_id, "capturedAt": captured_at, "value": value}
        if recording_id is js.MISSING:
            del take["recordingId"]
        qualifying.append((at, js.to_string(recording_id), take))
    qualifying.sort(key=lambda item: (item[0], js.locale_compare_key(item[1])))
    # Pre-registration: only the first N qualifying takes ever count.
    takes_required = js.get(goal, "takesRequired")
    end = _slice_end(takes_required, len(qualifying))
    counted = [take for _, _, take in qualifying[:end]]
    running_median = median([take["value"] for take in counted])
    complete = len(counted) >= js.to_number(takes_required)
    verdict = None
    if complete:
        observed = js.to_number(running_median)
        target = js.to_number(js.get(goal, "target"))
        met = observed >= target if js.get(goal, "direction") == "at_least" else observed <= target
        verdict = "achieved" if met else "missed"
    progress: Json = {"takesCounted": len(counted)}
    if takes_required is not js.MISSING:
        progress["takesRequired"] = takes_required
    return {
        **progress,
        "runningMedian": running_median,
        "latestValue": counted[-1]["value"] if counted else None,
        "verdict": verdict,
        "perTake": counted,
    }


def _by_created_desc(left: Json, right: Json) -> int:
    left_at = js.date_parse(js.get(left, "createdAt"))
    right_at = js.date_parse(js.get(right, "createdAt"))
    if left_at is None or right_at is None:
        return 0
    return (right_at > left_at) - (right_at < left_at)


def goals_payload(state: Any, context: GoalContext) -> Json:
    goals = []
    for goal in sorted(js.coalesce(js.get(state, "goals"), []), key=cmp_to_key(_by_created_desc)):
        abandoned = js.get(goal, "status") == "abandoned"
        progress = None if abandoned else evaluate_goal(goal, context)
        metric_name = js.get(goal, "metric")
        metric = GOAL_METRICS.get(metric_name) if isinstance(metric_name, str) else None
        entry = {**goal}
        label = metric.label if metric is not None else metric_name
        if label is not js.MISSING:
            entry["metricLabel"] = label
        else:
            entry.pop("metricLabel", None)
        entry["progress"] = progress
        verdict = None if progress is None else progress["verdict"]
        entry["derivedStatus"] = "abandoned" if abandoned else js.coalesce(verdict, "active")
        goals.append(entry)
    return {
        "goals": goals,
        "metrics": [{"key": key, "label": metric.label} for key, metric in GOAL_METRICS.items()],
        "clusterOptions": context.cluster_options or [],
        "readScope": {
            "committedManifestsOnly": True,
            "countsAndAggregatesOnly": True,
            "transcriptText": False,
            "audioBytes": False,
        },
    }

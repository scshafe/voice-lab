"""goals.test.mjs, translated (design §5.3), then the cases that hold the port's
remaining branches."""

from __future__ import annotations

import re
from typing import Any

import pytest

from cole_voice_lab.journey import jscompat
from cole_voice_lab.journey.goals import (
    GOAL_METRICS,
    GOALS_STATE_SCHEMA_VERSION,
    GoalContext,
    GoalError,
    create_goal,
    evaluate_goal,
    goal_context,
    goals_payload,
    validate_goal_spec,
)

CREATED = "2026-08-01T00:00:00Z"


def created_ms() -> float:
    parsed = jscompat.date_parse(CREATED)
    assert parsed is not None
    return parsed


def vibrato_take(recording_id: str, captured_at: str, mean_rate_hz: float) -> dict[str, Any]:
    return {
        "recordingId": recording_id,
        "capturedAt": captured_at,
        "status": "completed",
        "features": {"vibrato": {"meanRateHz": mean_rate_hz}},
    }


def goal_of(**overrides: Any) -> dict[str, Any]:
    return {
        "goalId": "g",
        "createdAt": CREATED,
        "status": "open",
        "metric": "vibratoRateHz",
        "direction": "at_least",
        "target": 5.5,
        "takesRequired": 3,
        "clusterId": None,
        "title": "t",
        **overrides,
    }


def test_validate_goal_spec_rejects_bad_specs_with_400s_and_applies_defaults() -> None:
    with pytest.raises(GoalError, match="unknown_metric") as unknown:
        validate_goal_spec({"metric": "nope", "direction": "at_least", "target": 1})
    assert unknown.value.status_code == 400
    with pytest.raises(GoalError, match="invalid_direction"):
        validate_goal_spec({"metric": "cpps", "direction": "sideways", "target": 1})
    with pytest.raises(GoalError, match="invalid_target"):
        validate_goal_spec({"metric": "cpps", "direction": "at_most", "target": "wat"})
    with pytest.raises(GoalError, match="invalid_takes_required"):
        validate_goal_spec(
            {"metric": "cpps", "direction": "at_most", "target": 1, "takesRequired": 2}
        )
    spec = validate_goal_spec(
        {"metric": "cpps", "direction": "at_most", "target": "11.5", "title": "  padded  "}
    )
    assert spec["takesRequired"] == 10
    assert spec["target"] == 11.5
    assert spec["title"] == "padded"
    assert spec["clusterId"] is None


def test_create_goal_freezes_the_spec_with_a_generated_title_and_id() -> None:
    goal = create_goal(
        validate_goal_spec(
            {
                "metric": "vibratoRateHz",
                "direction": "at_least",
                "target": 5.5,
                "clusterId": "lyric-9",
            }
        ),
        now=created_ms(),
    )
    assert re.fullmatch(r"[0-9a-f]{16}", goal["goalId"])
    assert goal["createdAt"] == jscompat.iso_from_ms(created_ms())
    assert goal["status"] == "open"
    assert goal["title"] == "vibrato rate (Hz) ≥ 5.5 on lyric-9"


def test_evaluation_counts_only_qualifying_takes_after_creation_in_order() -> None:
    feature_rows = [
        vibrato_take("r-late", "2026-08-20T00:00:00Z", 5.7),
        vibrato_take("r-before", "2026-07-01T00:00:00Z", 9.9),
        vibrato_take("r-early", "2026-08-05T00:00:00Z", 5.6),
        vibrato_take("r-mid", "2026-08-10T00:00:00Z", 5.4),
        {
            "recordingId": "r-failed",
            "capturedAt": "2026-08-06T00:00:00Z",
            "status": "failed",
            "features": {"vibrato": {"meanRateHz": 1}},
        },
    ]
    progress = evaluate_goal(goal_of(), GoalContext(feature_rows, {}))
    assert [take["recordingId"] for take in progress["perTake"]] == ["r-early", "r-mid", "r-late"]
    assert progress["takesCounted"] == 3
    assert progress["runningMedian"] == 5.6
    assert progress["verdict"] == "achieved"


def test_pre_registration_means_takes_beyond_the_first_n_never_count() -> None:
    feature_rows = [
        vibrato_take("a", "2026-08-02T00:00:00Z", 4.0),
        vibrato_take("b", "2026-08-03T00:00:00Z", 4.1),
        vibrato_take("c", "2026-08-04T00:00:00Z", 4.2),
        vibrato_take("d", "2026-08-05T00:00:00Z", 6.5),
        vibrato_take("e", "2026-08-06T00:00:00Z", 6.6),
    ]
    progress = evaluate_goal(goal_of(), GoalContext(feature_rows, {}))
    assert progress["takesCounted"] == 3
    assert progress["verdict"] == "missed"
    assert [take["recordingId"] for take in progress["perTake"]] == ["a", "b", "c"]


def test_direction_at_most_incomplete_goals_and_cluster_scoping_behave() -> None:
    feature_rows = [
        vibrato_take("x1", "2026-08-02T00:00:00Z", 4.0),
        vibrato_take("x2", "2026-08-03T00:00:00Z", 4.4),
        vibrato_take("y1", "2026-08-04T00:00:00Z", 9.0),
    ]
    context = GoalContext(feature_rows, {"x1": "song-x", "x2": "song-x", "y1": "song-y"})
    scoped = evaluate_goal(
        goal_of(direction="at_most", target=4.5, clusterId="song-x", takesRequired=3), context
    )
    assert scoped["takesCounted"] == 2, "other clusters never qualify"
    assert scoped["verdict"] is None, "incomplete goals hold no verdict"
    assert scoped["runningMedian"] == 4.0
    complete = evaluate_goal(
        goal_of(direction="at_most", target=4.5, clusterId="song-x", takesRequired=2), context
    )
    assert complete["verdict"] == "achieved"


def test_goals_payload_derives_statuses_keeps_abandoned_frozen_and_aggregate_only() -> None:
    state = {
        "goals": [
            goal_of(goalId="g-old", createdAt="2026-07-01T00:00:00Z"),
            goal_of(
                goalId="g-gone",
                createdAt="2026-07-15T00:00:00Z",
                status="abandoned",
                abandonedAt="2026-08-01T00:00:00Z",
            ),
        ]
    }
    rows = [
        {
            "recordingId": f"x{i}",
            "lyricMatchCluster": "song-x",
            "lyricMatchClusterLabel": "song x label",
            "capturedAt": f"2026-08-0{i + 1}T00:00:00Z",
            "bucket": "clean_singing",
        }
        for i in range(1, 4)
    ]
    features_manifest = {
        "results": [
            vibrato_take(row["recordingId"], row["capturedAt"], 5.6 + i * 0.1)
            for i, row in enumerate(rows)
        ]
    }
    payload = goals_payload(state, goal_context(rows, features_manifest))
    assert [goal["goalId"] for goal in payload["goals"]] == ["g-gone", "g-old"], "newest first"
    gone = payload["goals"][0]
    assert gone["derivedStatus"] == "abandoned"
    assert gone["progress"] is None
    active = payload["goals"][1]
    assert active["derivedStatus"] == "achieved"
    assert active["metricLabel"] == GOAL_METRICS["vibratoRateHz"].label
    assert any(metric["key"] == "cpps" for metric in payload["metrics"])
    assert payload["clusterOptions"] == [
        {"clusterId": "song-x", "label": "song x label", "takeCount": 3}
    ]
    assert payload["readScope"]["transcriptText"] is False


# The port's own cases: JavaScript's coercions and the state file's format.


def test_goal_ids_and_spec_coercions_match_the_javascript() -> None:
    spec = validate_goal_spec(
        {"metric": ["cpps"], "direction": "at_most", "target": " 0x10 ", "clusterId": 7}
    )
    assert spec == {
        "metric": "cpps",
        "direction": "at_most",
        "target": 16.0,
        "takesRequired": 10,
        "clusterId": "7",
        "title": None,
    }
    goal = create_goal(spec, now=created_ms())
    assert goal == {
        "goalId": "270b4df50eaa7110",
        "createdAt": "2026-08-01T00:00:00.000Z",
        "status": "open",
        "metric": "cpps",
        "direction": "at_most",
        "target": 16.0,
        "takesRequired": 10,
        "clusterId": "7",
        "title": "clarity — CPPS (dB) ≤ 16 on 7",
    }
    assert GOALS_STATE_SCHEMA_VERSION == "voice-journey.goals-state.v1"
    long_title = validate_goal_spec(
        {"metric": "cpps", "direction": "at_least", "target": None, "title": "\u3000" + "😀" * 61}
    )
    assert long_title["target"] == 0.0
    assert len(long_title["title"].encode("utf-16-le", "surrogatepass")) == 240
    assert (
        validate_goal_spec({"metric": "cpps", "direction": "at_least", "target": 1, "title": ""})[
            "title"
        ]
        is None
    )
    for takes in (None, 3.5, "51", float("inf")):
        with pytest.raises(GoalError, match="invalid_takes_required"):
            validate_goal_spec(
                {"metric": "cpps", "direction": "at_least", "target": 1, "takesRequired": takes}
            )
    with pytest.raises(GoalError, match="unknown_metric"):
        validate_goal_spec()
    with pytest.raises(GoalError, match="unknown_metric"):
        validate_goal_spec({"metric": "toString", "direction": "at_least", "target": 1})


def test_evaluation_edges_follow_the_javascript() -> None:
    rows = [
        vibrato_take("b", "2026-08-02T00:00:00Z", 5.0),
        vibrato_take("A", "2026-08-02T00:00:00Z", 6.0),
        vibrato_take("a", "2026-08-02T00:00:00Z", 7.0),
        {"recordingId": "n", "capturedAt": "", "status": "completed", "features": {"x": 1}},
        {"recordingId": "u", "capturedAt": "soon", "status": "completed", "features": {"x": 1}},
        {
            "capturedAt": "2026-08-03T00:00:00Z",
            "status": "completed",
            "features": {"vibrato": {"meanRateHz": 1}},
        },
    ]
    context = GoalContext(rows, {})
    unparsed = evaluate_goal(goal_of(createdAt="never", takesRequired=None), context)
    assert [take.get("recordingId") for take in unparsed["perTake"]] == []
    assert unparsed["verdict"] == "missed"
    everything = goal_of()
    del everything["takesRequired"]
    progress = evaluate_goal(everything, context)
    assert "takesRequired" not in progress
    assert [take.get("recordingId") for take in progress["perTake"]] == ["a", "A", "b", None]
    assert progress["verdict"] is None
    negative = evaluate_goal(goal_of(takesRequired=-1), context)
    assert negative["takesCounted"] == 3
    unknown = evaluate_goal(goal_of(metric="nope"), context)
    assert unknown["perTake"] == []
    endless = evaluate_goal(goal_of(takesRequired=float("inf")), context)
    assert endless["takesCounted"] == 4
    assert evaluate_goal(goal_of(takesRequired=float("-inf")), context)["takesCounted"] == 0


def test_payload_drops_an_undefined_metric_label_and_tolerates_bad_dates() -> None:
    no_metric = goal_of(goalId="m", metricLabel="stale")
    del no_metric["metric"]
    state = {
        "goals": [
            goal_of(goalId="bad", createdAt="never", metric=5),
            no_metric,
            goal_of(goalId="late", createdAt="2026-09-01T00:00:00Z"),
        ]
    }
    payload = goals_payload(state, GoalContext([], {}))
    assert [goal["goalId"] for goal in payload["goals"]] == ["bad", "late", "m"]
    by_id = {goal["goalId"]: goal for goal in payload["goals"]}
    assert "metricLabel" not in by_id["m"]
    assert by_id["bad"]["metricLabel"] == 5
    assert by_id["late"]["derivedStatus"] == "active"
    assert payload["clusterOptions"] == []
    assert goals_payload(None, GoalContext([], {}))["goals"] == []
    context = goal_context(None, None)
    assert (context.feature_rows, context.cluster_by_recording, context.cluster_options) == (
        [],
        {},
        [],
    )
    unlabeled = goal_context([{"recordingId": "r", "lyricMatchCluster": "c"}], None)
    assert unlabeled.cluster_options == [{"clusterId": "c", "label": "c", "takeCount": 1}]

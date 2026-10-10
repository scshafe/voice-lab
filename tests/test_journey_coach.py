"""coach.test.mjs, translated (design §5.3), then the cases that hold the port's
remaining branches."""

from __future__ import annotations

import re
from typing import Any

from cole_voice_lab.journey import jscompat
from cole_voice_lab.journey.coach import (
    DUE_AFTER_DAYS,
    build_coach_payload,
    build_due_songs,
    build_frontier,
    cluster_rollup,
)

NOW = jscompat.date_parse("2026-08-12T00:00:00Z") or 0.0


def take(
    recording_id: str, cluster: str | None, captured_at: str, **overrides: Any
) -> dict[str, Any]:
    return {
        "recordingId": recording_id,
        "lyricMatchCluster": cluster,
        "lyricMatchClusterLabel": f"{cluster} label" if cluster else None,
        "capturedAt": captured_at,
        "bucket": "clean_singing",
        **overrides,
    }


def feat_row(recording_id: str, captured_at: str, features: dict[str, Any]) -> dict[str, Any]:
    return {
        "recordingId": recording_id,
        "capturedAt": captured_at,
        "status": "completed",
        "features": features,
    }


def test_cluster_rollup_groups_rows_by_cluster_with_privacy_generic_labels_only() -> None:
    rollup = cluster_rollup(
        [
            take("r1", "song-a", "2020-01-01T00:00:00Z"),
            take("r2", "song-a", "2021-01-01T00:00:00Z", bucket="uncertain_manual_review"),
            take("r3", None, "2021-01-01T00:00:00Z"),
        ]
    )
    assert len(rollup) == 1
    assert rollup[0]["clusterId"] == "song-a"
    assert rollup[0]["label"] == "song-a label"
    assert rollup[0]["takeCount"] == 2
    assert rollup[0]["cleanCount"] == 1


def test_due_songs_respect_the_interval_and_the_recurring_minimum() -> None:
    rows = [
        *(take(f"a{i}", "stale", f"202{min(i, 4)}-0{i + 1}-01T00:00:00Z") for i in range(6)),
        take("b1", "fresh", "2026-06-01T00:00:00Z"),
        take("b2", "fresh", "2026-07-01T00:00:00Z"),
        take("b3", "fresh", "2026-08-01T00:00:00Z"),
        take("c1", "tiny", "2020-01-01T00:00:00Z"),
        take("c2", "tiny", "2020-02-01T00:00:00Z"),
    ]
    due = build_due_songs(rows=rows, journeys_manifest=None, now=NOW)
    assert [entry["clusterId"] for entry in due] == ["stale"]
    assert due[0]["label"] == "stale label"
    assert re.fullmatch(r"last sung \d+ d ago", due[0]["why"][0])
    assert due[0]["daysSinceLast"] >= DUE_AFTER_DAYS


def test_due_ranking_favors_history_marks_same_song_index_and_flags_near_eligibility() -> None:
    rows = [
        *(take(f"big{i}", "big", f"{2020 + (i % 5)}-03-01T00:00:00Z") for i in range(24)),
        *(take(f"sm{i}", "small", f"2020-0{i + 1}-01T00:00:00Z") for i in range(3)),
        take("n1", "near", "2020-01-01T00:00:00Z"),
        take("n2", "near", "2021-01-01T00:00:00Z"),
        take("n3", "near", "2022-01-01T00:00:00Z"),
        take("n4", "near", "2024-01-01T00:00:00Z"),
    ]
    journeys_manifest = {"journeys": [{"clusterId": "big", "takesUsed": 20}]}
    due = build_due_songs(rows=rows, journeys_manifest=journeys_manifest, now=NOW)
    assert due[0]["clusterId"] in ("big", "near"), "deep or boosted clusters lead"
    by_id = {entry["clusterId"]: entry for entry in due}
    big = by_id["big"]
    assert big["inSameSongIndex"] is True
    assert any("same-song index" in reason for reason in big["why"])
    near = by_id["near"]
    assert near["inSameSongIndex"] is False
    assert any("short of same-song eligibility" in reason for reason in near["why"])
    small = by_id["small"]
    assert big["score"] > small["score"], "history depth outranks a shallow thread"


def test_frontier_rules_fire_with_the_numbers_that_argue_them() -> None:
    rows = [
        take("f1", "fx", "2025-01-01T00:00:00Z"),
        take("f2", "fx", "2025-02-01T00:00:00Z"),
        take("f3", "fx", "2025-03-01T00:00:00Z"),
        take("pb1", "other", "2024-01-01T00:00:00Z"),
    ]
    features_manifest = {
        "results": [
            feat_row(
                "f1",
                "2025-01-01T00:00:00Z",
                {
                    "pitch": {"f0Hz": {"p95": 350}},
                    "vibrato": {"meanRateHz": 4.8, "vibratoSegmentCount": 3},
                    "phrasing": {"longestSustainedSeconds": 9.0},
                },
            ),
            feat_row(
                "f2",
                "2025-02-01T00:00:00Z",
                {
                    "pitch": {"f0Hz": {"p95": 348}},
                    "vibrato": {"meanRateHz": 4.9, "vibratoSegmentCount": 2},
                    "phrasing": {"longestSustainedSeconds": 5.0},
                },
            ),
            feat_row(
                "f3",
                "2025-03-01T00:00:00Z",
                {
                    "pitch": {"f0Hz": {"p95": 352}},
                    "vibrato": {"meanRateHz": 3.9, "vibratoSegmentCount": 4},
                    "phrasing": {"longestSustainedSeconds": 6.0},
                },
            ),
            feat_row(
                "pb1",
                "2024-01-01T00:00:00Z",
                {
                    "pitch": {"f0Hz": {"p95": 200}},
                    "vibrato": {"meanRateHz": 4.0, "vibratoSegmentCount": 1},
                    "phrasing": {"longestSustainedSeconds": 10.0},
                },
            ),
        ]
    }
    journeys_manifest = {
        "journeys": [
            {
                "clusterId": "fx",
                "takesUsed": 8,
                "perYear": {"2023": {"noteCoreCentError": 21}, "2024": {"noteCoreCentError": 28}},
                "slopesPerYear": {"cpps": 0.4},
            }
        ]
    }
    frontier = build_frontier(
        rows=rows,
        features_manifest=features_manifest,
        journeys_manifest=journeys_manifest,
        working_top_hz=392,
    )
    assert len(frontier) == 1, "only the flagged cluster appears (the PB donor has too few)"
    entry = frontier[0]
    assert entry["clusterId"] == "fx"
    assert entry["label"] == "fx label"
    rules = sorted(flag["rule"] for flag in entry["flags"])
    assert rules == [
        "clarity-thin",
        "range-stretch",
        "sustain-pb",
        "tuning-near",
        "vibrato-settling",
    ]
    by_rule = {flag["rule"]: flag["detail"] for flag in entry["flags"]}
    assert "st under your working top" in by_rule["range-stretch"]
    assert "Hz" in by_rule["vibrato-settling"]
    assert re.search(r"of your 10\.0 s best", by_rule["sustain-pb"])
    assert "28 c in 2024" in by_rule["tuning-near"]
    assert "add data" in by_rule["clarity-thin"]


def test_quiet_clusters_produce_no_frontier_entries() -> None:
    rows = [
        take("q1", "quiet", "2025-01-01T00:00:00Z"),
        take("q2", "quiet", "2025-02-01T00:00:00Z"),
        take("q3", "quiet", "2025-03-01T00:00:00Z"),
    ]
    features_manifest = {
        "results": [
            feat_row(
                recording_id,
                f"2025-0{index + 1}-01T00:00:00Z",
                {
                    "pitch": {"f0Hz": {"p95": 200}},
                    "vibrato": {"meanRateHz": 4.0, "vibratoSegmentCount": 2},
                    "phrasing": {"longestSustainedSeconds": 3.0},
                },
            )
            for index, recording_id in enumerate(["q1", "q2", "q3"])
        ]
    }
    frontier = build_frontier(
        rows=rows, features_manifest=features_manifest, journeys_manifest=None, working_top_hz=392
    )
    assert frontier == []


def test_coach_payload_carries_aggregates_only_with_a_fixed_entry_shape() -> None:
    rows = [take(f"a{i}", "song-a", f"202{i}-01-01T00:00:00Z") for i in range(4)]
    payload = build_coach_payload(
        rows=rows,
        features_manifest=None,
        journeys_manifest={"available": True, "journeys": []},
        voice_trends={"headline": {"workingTop": {"peakTopHz": 392, "gainSemitones": 6.7}}},
        now=NOW,
    )
    assert payload["workingTop"]["peakTopHz"] == 392
    assert payload["readScope"]["transcriptText"] is False
    assert payload["readScope"]["audioBytes"] is False
    assert isinstance(payload["frontier"], list)
    assert len(payload["dueSongs"]) >= 1
    assert sorted(payload["dueSongs"][0]) == [
        "clusterId",
        "daysSinceLast",
        "firstYear",
        "inSameSongIndex",
        "label",
        "lastYear",
        "score",
        "takeCount",
        "why",
    ]


# The port's own cases: the exact strings and the edges of the rules.


def test_due_song_text_and_numbers_match_the_javascript() -> None:
    rows = [take(f"a{i}", "song", f"202{i}-01-01T00:00:00Z") for i in range(3)]
    rows.append(take("bad", "song", "not a date"))
    journeys = {"journeys": [{"clusterId": "song"}]}
    (entry,) = build_due_songs(rows=rows, journeys_manifest=journeys, now=NOW)
    assert entry == {
        "clusterId": "song",
        "label": "song label",
        "score": 7.3,
        "daysSinceLast": 1684.0,
        "takeCount": 4,
        "firstYear": "2020",
        "lastYear": "2022",
        "inSameSongIndex": True,
        "why": [
            "last sung 1684 d ago",
            "4 takes, 2020–2022",
            "in the same-song index (undefined takes used) — a take today extends its span 4.6 y",
        ],
    }
    one_short = [take(f"n{i}", "near", f"202{i}-01-01T00:00:00Z") for i in range(5)]
    (near,) = build_due_songs(rows=one_short, now=NOW)
    assert near["why"][-1] == "1 clean take short of same-song eligibility"
    assert near["label"] == "near label"
    undated = [take(f"u{i}", "u", "") for i in range(3)]
    assert build_due_songs(rows=undated, now=NOW) == []
    unlabeled = [take(f"x{i}", "x", f"202{i}-01-01T00:00:00Z") for i in range(3)]
    for row in unlabeled:
        row["lyricMatchClusterLabel"] = None
    assert build_due_songs(rows=unlabeled, now=NOW)[0]["label"] == "x"


def test_frontier_edges_follow_the_javascript() -> None:
    rows = [take(f"t{i}", "song", f"2025-0{i + 1}-01T00:00:00Z") for i in range(3)]
    rows.append(take("none", None, "2025-01-01T00:00:00Z"))
    for row in rows:
        row["lyricMatchClusterLabel"] = None
    features = {
        "results": [
            feat_row(
                f"t{i}",
                "" if i == 0 else f"2025-0{i + 1}-01T00:00:00Z",
                {
                    "pitch": {"f0Hz": {"p95": None}},
                    "vibrato": {"meanRateHz": 5.0, "vibratoSegmentCount": "2"},
                    "phrasing": {"longestSustainedSeconds": 9.5 if i else 10.0},
                },
            )
            for i in range(3)
        ]
        + [{"recordingId": "none", "status": "completed", "features": {}}]
    }
    journeys = {
        "journeys": [
            {"clusterId": "song", "perYear": {"2024": None}, "slopesPerYear": {"cpps": 0.2}}
        ]
    }
    (entry,) = build_frontier(
        rows=rows, features_manifest=features, journeys_manifest=journeys, working_top_hz=392
    )
    assert entry["label"] == "song"
    assert [flag["rule"] for flag in entry["flags"]] == ["vibrato-settling"]
    assert entry["flags"][0]["detail"] == (
        "100% of recent takes at 5.0 Hz — one nudge from the settled 5.5 Hz zone"
    )
    journeys["journeys"][0]["takesUsed"] = None
    journeys["journeys"][0]["perYear"] = {}
    flagged = build_frontier(
        rows=rows, features_manifest=features, journeys_manifest=journeys, working_top_hz="392"
    )
    assert [flag["rule"] for flag in flagged[0]["flags"]] == ["vibrato-settling", "clarity-thin"]
    assert flagged[0]["flags"][1]["detail"] == (
        "clarity trending +0.2/yr on only null takes — promising but evidence-thin; add data"
    )
    pb_rows = [take(f"p{i}", "pb", f"2025-0{i + 1}-01T00:00:00Z") for i in range(3)]
    pb_features = {
        "results": [
            feat_row(
                row["recordingId"], row["capturedAt"], {"phrasing": {"longestSustainedSeconds": s}}
            )
            for row, s in zip(pb_rows, [9.99, 9.0, 8.0], strict=True)
        ]
        + [feat_row("donor", "2020-01-01T00:00:00Z", {"phrasing": {"longestSustainedSeconds": 10}})]
    }
    (pb,) = build_frontier(rows=pb_rows, features_manifest=pb_features)
    assert (
        pb["flags"][0]["detail"] == "longest recent sustain 10.0 s — within 1% of your 10.0 s best"
    )


def test_payload_defaults_to_now_and_no_working_top() -> None:
    payload = build_coach_payload()
    assert payload["generatedAt"].endswith("Z")
    assert payload["workingTop"] is None
    assert payload["params"]["vibratoSettlingBandHz"] == [4.5, 5.2]
    assert payload["dueSongs"] == []
    assert payload["frontier"] == []

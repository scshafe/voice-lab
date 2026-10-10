"""journeys.mjs's tests, case for case, plus the JavaScript-parity checks of the port."""

from __future__ import annotations

import io
import json
import math
import random
import shutil
import subprocess
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.journey import journeys
from cole_voice_lab.journey.journeys import (
    JourneysOptions,
    analyze_journeys,
    bootstrap_ci,
    main,
    note_core_tuning,
    segment_notes,
    theil_sen_slope,
)
from cole_voice_lab.journey.jsonio import to_fixed

REPO = Path(__file__).resolve().parents[1]
IMPORT_COMMIT = "1f236b5"
NODE = shutil.which("node")
ANALYZED_AT = "2026-07-31T00:00:00.000Z"


def contour_for(segments: list[dict[str, Any]]) -> list[list[Any]]:
    points: list[list[Any]] = []
    t = 0.0
    for segment in segments:
        for _ in range(segment["points"]):
            points.append([to_fixed(t, 2), segment["hz"]])
            t += 0.05
        t += segment.get("gap", 0)
    return points


def cents_above_a4(cents: float) -> float:
    return 440 * math.pow(2, cents / 1200)


def journey_fixture() -> tuple[list[dict[str, Any]], dict[str, list[str]], dict[str, Any]]:
    feature_rows: list[dict[str, Any]] = []
    cluster_ids: dict[str, list[str]] = {"improving": [], "flat": []}
    detail_files: dict[str, Any] = {}

    def add_take(year: int, month: int, cluster_key: str, spread: float) -> None:
        recording_id = f"take-{len(feature_rows) + 1}"
        improving = cluster_key == "improving"
        feature_rows.append(
            {
                "recordingId": recording_id,
                "capturedAt": f"{year}-{month:02d}-10T00:00:00Z",
                "year": str(year),
                "status": "completed",
                "features": {
                    "durationSeconds": 40,
                    "voicing": {
                        "voicedShare": 0.7,
                        "frameCount": 4000,
                        "rejectedOutlierShare": 0.01,
                        "rejectedGlobalOutlierShare": 0,
                    },
                    "pitch": {
                        "f0Hz": {"p05": 110, "p25": 150, "p50": 200, "p75": 260, "p95": 330},
                        "rangeSemitonesP05P95": 12,
                        "madSemitones": 1,
                    },
                    "tuning": {
                        "offsetCents": 0,
                        "medianAbsCentError": 22,
                        "within25CentsShare": 0.5,
                    },
                    "vibrato": {
                        "sustainedSegmentCount": 3,
                        "vibratoSegmentCount": 2,
                        "meanRateHz": 4,
                        "meanExtentCents": 45,
                        "vibratoTimeShare": 0.3,
                    },
                    "quality": {
                        "jitterLocal": 0.01,
                        "shimmerLocal": 0.07,
                        "meanHnrDb": 15,
                        "cpps": 10 + (year - 2019) * (0.6 if improving else 0),
                        "spectral": None,
                    },
                    "phrasing": {
                        "longestSustainedSeconds": 2 + (year - 2019) * (0.3 if improving else 0),
                        "meanVoicedSegmentSeconds": 1.5,
                        "pauseRatePerMinute": 8,
                    },
                },
            }
        )
        cluster_ids[cluster_key].append(recording_id)
        notes = [
            {
                "hz": cents_above_a4(-1200 + index * 200 + (spread if index % 2 == 0 else -spread)),
                "points": 8,
                "gap": 0.3,
            }
            for index in range(6)
        ]
        detail_files[recording_id] = {
            "recordingId": recording_id,
            "methodId": "voice-journey.feature-extract.v1",
            "summary": {},
            "detail": {"f0ContourVoiced50ms": contour_for(notes), "vibratoSegments": []},
        }

    for year in (2019, 2020, 2021, 2022):
        for month in (2, 8):
            add_take(year, month, "improving", max(4, 36 - (year - 2019) * 10))
            add_take(year, month, "flat", 18)
    return feature_rows, cluster_ids, detail_files


# --- journeys.test.mjs ------------------------------------------------------------


def test_segment_notes_splits_on_jumps_and_gaps_and_drops_short_transitions() -> None:
    contour = contour_for(
        [
            {"hz": 220, "points": 10},
            {"hz": 330, "points": 8, "gap": 0},
            {"hz": 262, "points": 2, "gap": 0.3},
            {"hz": 440, "points": 6, "gap": 0.3},
        ]
    )
    notes = segment_notes(contour)
    assert len(notes) == 3
    assert abs(notes[0]["medianCents"] - (-1200)) < 5
    assert abs(notes[1]["medianCents"] - (-498)) < 8
    assert abs(notes[2]["medianCents"] - 0) < 5
    assert notes[0]["points"] == 10


def test_note_core_tuning_infers_the_takes_own_offset_and_scores_deviations() -> None:
    offsets = [20, 18, 22, 20, 19, 21]
    notes = [
        {
            "startSeconds": index,
            "durationSeconds": 0.5,
            "medianCents": -1200 + index * 100 + cents,
            "points": 10,
        }
        for index, cents in enumerate(offsets)
    ]
    tuning = note_core_tuning(notes)
    assert abs(tuning["tuningOffsetCents"] - 20) < 1.5, tuning["tuningOffsetCents"]
    assert tuning["centErrorMedian"] <= 2, tuning["centErrorMedian"]
    assert tuning["inTuneShare"] == 1
    assert tuning["notes"] == 6

    too_few = note_core_tuning(notes[:3])
    assert too_few["centErrorMedian"] is None


def test_theil_sen_slope_recovers_a_clean_linear_trend_and_bootstrap_ci_is_deterministic() -> None:
    points = [(2019 + x, 30 - 2 * x) for x in range(5)]
    assert theil_sen_slope(points) == -2
    ci_a = bootstrap_ci([-2.1, -1.9, -2.4, -1.7, -2.2], 7)
    ci_b = bootstrap_ci([-2.1, -1.9, -2.4, -1.7, -2.2], 7)
    assert ci_a == ci_b
    assert ci_a is not None and ci_a[1] is not None and ci_a[1] < 0


def test_analyze_journeys_builds_journeys_note_core_tuning_and_an_improvement_index(
    tmp_path: Path,
) -> None:
    feature_rows, cluster_ids, detail_files = journey_fixture()
    features_manifest = {
        "schemaVersion": "voice-journey.local-voice-features.v1",
        "generatedAt": ANALYZED_AT,
        "results": feature_rows,
    }
    lyrics_manifest = {
        "schemaVersion": "voice-journey.local-lyric-indicators.v1",
        "clusters": [
            {
                "clusterId": "cluster-improving",
                "label": "recurring_lyric_cluster_A",
                "recordingIds": cluster_ids["improving"],
            },
            {
                "clusterId": "cluster-flat",
                "label": "recurring_lyric_cluster_B",
                "recordingIds": cluster_ids["flat"],
            },
            {
                "clusterId": "cluster-tiny",
                "label": "recurring_lyric_cluster_C",
                "recordingIds": cluster_ids["flat"][:2],
            },
        ],
        "results": [],
    }
    out_path = tmp_path / "song-journeys.json"
    options = JourneysOptions(
        feature_dir=str(tmp_path), out=str(out_path), analyzed_at=ANALYZED_AT, seed=2026
    )
    manifest = analyze_journeys(features_manifest, lyrics_manifest, options, detail_files.get)

    assert manifest["totals"]["reliableTakes"] == 16
    assert manifest["totals"]["takesWithNoteCore"] == 16
    assert manifest["totals"]["clustersEligible"] == 2
    by_id = {journey["clusterId"]: journey for journey in manifest["journeys"]}
    improving = by_id["cluster-improving"]
    assert improving["slopesPerYear"]["noteCoreCentError"] < -5
    assert improving["slopesPerYear"]["cpps"] > 0.4
    assert abs(by_id["cluster-flat"]["slopesPerYear"]["noteCoreCentError"]) < 2
    cent_dimension = next(
        item for item in manifest["improvementIndex"] if item["key"] == "noteCoreCentError"
    )
    assert cent_dimension["clustersUsed"] == 2
    written = out_path.read_text(encoding="utf-8")
    assert "f0ContourVoiced50ms" not in written
    assert len(json.loads(written)["noteCore"]["takes"]) == 16

    out_path2 = tmp_path / "song-journeys-2.json"
    options2 = JourneysOptions(
        feature_dir=str(tmp_path), out=str(out_path2), analyzed_at=ANALYZED_AT, seed=2026
    )
    analyze_journeys(features_manifest, lyrics_manifest, options2, detail_files.get)
    assert out_path2.read_text(encoding="utf-8") == written


def test_journeys_dry_run_reads_only_committed_manifests_and_reports_eligibility(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feature_rows, cluster_ids, _ = journey_fixture()
    features_path = tmp_path / "features.json"
    lyrics_path = tmp_path / "lyrics.json"
    features_path.write_text(
        json.dumps(
            {"schemaVersion": "voice-journey.local-voice-features.v1", "results": feature_rows}
        )
    )
    lyrics_path.write_text(
        json.dumps(
            {
                "schemaVersion": "voice-journey.local-lyric-indicators.v1",
                "clusters": [
                    {
                        "clusterId": "cluster-improving",
                        "label": "recurring_lyric_cluster_A",
                        "recordingIds": cluster_ids["improving"],
                    }
                ],
                "results": [],
            }
        )
    )
    code = main(
        [
            "analyze",
            "--features",
            str(features_path),
            "--lyrics",
            str(lyrics_path),
            "--feature-dir",
            str(tmp_path / "missing"),
            "--dry-run",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["operation"] == "same-song-journey-analysis"
    assert payload["scope"]["reliableTakes"] == 16
    assert payload["scope"]["clustersEligible"] == 1
    assert payload["readScope"]["audioBytes"] is False


# --- the command line -------------------------------------------------------------


def test_help_and_refusals(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert capsys.readouterr().out == journeys.HELP
    assert main(["help"]) == 0
    assert main(["bogus", "--help"]) == 0
    capsys.readouterr()
    for argv, message in [
        (["--nope"], "unsupported argument: --nope"),
        ([""], "unsupported argument: "),
        (["analyze", "--out"], "--out requires a value"),
        (["analyze", "--out", "--dry-run"], "--out requires a value"),
        (["analyze", "--seed", "-1"], "--seed must be a non-negative integer"),
        (["analyze", "--seed", "1.5"], "--seed must be a non-negative integer"),
        (["analyze", "--seed", "abc"], "--seed must be a non-negative integer"),
        (["bogus"], "unsupported command: bogus"),
    ]:
        assert main(argv) == 1
        assert capsys.readouterr().err == f"error: {message}\n"
    missing = "/nonexistent/features.json"
    assert main(["analyze", "--features", missing]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_options_parse_every_flag_and_javascript_numbers() -> None:
    options = journeys.parse_args(
        [
            "--analyzed-at",
            ANALYZED_AT,
            "--dry-run",
            "--feature-dir",
            "fd",
            "--features",
            "f.json",
            "--lyrics",
            "l.json",
            "--out",
            "o.json",
            "--seed",
            " 0x10 ",
        ]
    )
    assert options == JourneysOptions(
        features="f.json",
        lyrics="l.json",
        feature_dir="fd",
        out="o.json",
        seed=16,
        analyzed_at=ANALYZED_AT,
        dry_run=True,
    )
    assert journeys.parse_args(["--seed", "-0"]).seed == 0
    assert journeys.parse_args(["--seed", "1e21"]).seed == 1e21
    assert journeys.parse_args(["--seed", "   "]).seed == 0


def test_main_writes_the_manifest_and_announces_the_path_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("f.json").write_text(json.dumps({"results": []}))
    Path("l.json").write_text(json.dumps({"clusters": []}))
    argv = ["analyze", "--features", "f.json", "--lyrics", "l.json", "--out", "./out/x.json"]
    assert main(argv) == 0
    assert capsys.readouterr().out == "./out/x.json\n"
    manifest = json.loads(Path("out/x.json").read_text())
    assert manifest["totals"] == {
        "reliableTakes": 0,
        "takesWithNoteCore": 0,
        "missingDetailFiles": 0,
        "clustersEligible": 0,
    }
    assert [item["verdict"] for item in manifest["improvementIndex"]] == ["insufficient_data"] * 6
    assert manifest["sources"]["features"]["recordingCount"] == 0
    assert manifest["generatedAt"].endswith("Z")
    assert main([*argv[:-1], "-"]) == 0
    assert json.loads(capsys.readouterr().out)["schemaVersion"] == "voice-journey.song-journeys.v1"


def test_default_detail_reader_reads_the_store_and_treats_missing_as_absent(
    tmp_path: Path,
) -> None:
    feature_rows, cluster_ids, detail_files = journey_fixture()
    store = tmp_path / "store"
    store.mkdir()
    for recording_id, detail in list(detail_files.items())[:-2]:
        (store / f"{recording_id}.json").write_text(json.dumps(detail))
    options = JourneysOptions(feature_dir=str(store), out=str(tmp_path / "o.json"), analyzed_at="x")
    clusters = {"clusters": [{"clusterId": "c", "recordingIds": cluster_ids["flat"]}]}
    manifest = analyze_journeys({"results": feature_rows}, clusters, options)
    assert manifest["totals"]["missingDetailFiles"] == 2
    assert manifest["journeys"][0]["label"] == "c"
    (store / "take-16.json").mkdir()
    with pytest.raises(IsADirectoryError):
        analyze_journeys({"results": feature_rows}, clusters, options)


# --- JavaScript value rules -------------------------------------------------------


def test_rows_follow_javascript_truthiness_and_coercion(tmp_path: Path) -> None:
    def row(recording_id: Any, **extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "recordingId": recording_id,
            "status": "completed",
            "capturedAt": "2020-05-01T00:00:00Z",
            "features": {"voicing": {}},
        }
        base.update(extra)
        return base

    rows: list[Any] = [
        7,
        row("a"),
        row("b", status="failed"),
        row("c", features={}),
        row("d", features=[]),
        row("e", features="x"),
        row("f", features=0),
        row("g", capturedAt=""),
        row("h", features={"voicing": {"rejectedOutlierShare": "0.05"}}),
        row("i", features={"voicing": {"rejectedOutlierShare": "0.5"}}),
        row("j", features={"voicing": {"rejectedOutlierShare": [0.01]}}),
        row(
            "k",
            features={
                "voicing": {"rejectedOutlierShare": 0.06, "rejectedGlobalOutlierShare": 0.05}
            },
        ),
        row("l", features={"voicing": 3}),
    ]
    ids = [r["recordingId"] for r in journeys.reliable_feature_takes({"results": rows})]
    assert ids == ["a", "c", "d", "e", "h", "j", "l"]
    with pytest.raises(TypeError, match="reading 'status'"):
        journeys.reliable_feature_takes({"results": [None]})
    with pytest.raises(TypeError, match="not an array"):
        journeys.reliable_feature_takes({"results": "abc"})
    with pytest.raises(TypeError, match="reading 'results'"):
        journeys.reliable_feature_takes(None)
    assert journeys.reliable_feature_takes(5) == []


def test_value_helpers_match_javascript() -> None:
    undefined = journeys._UNDEFINED
    assert repr(undefined) == "undefined"
    assert [journeys._to_string(v) for v in [undefined, None, True, False, 2026.0, math.nan]] == [
        "undefined",
        "null",
        "true",
        "false",
        "2026",
        "NaN",
    ]
    assert journeys._to_string(-math.inf) == "-Infinity"
    assert journeys._to_string(math.inf) == "Infinity"
    assert journeys._to_string([1, None, "a", [2, 3]]) == "1,,a,2,3"
    assert journeys._to_string({}) == "[object Object]"
    assert journeys._to_number(undefined) != journeys._to_number(undefined)
    assert journeys._to_number(None) == 0
    assert journeys._to_number(True) == 1
    assert journeys._to_number(False) == 0
    assert journeys._to_number(10**400) == math.inf
    assert journeys._to_number([" 7 "]) == 7
    assert math.isnan(journeys._to_number({}))
    assert journeys._add("0.01", 0) == "0.010"
    assert journeys._add(True, 1) == 2
    assert journeys._add({}, 1) == "[object Object]1"
    assert journeys._div(1.0, 0.0) == math.inf
    assert journeys._div(-1.0, -0.0) == math.inf
    assert math.isnan(journeys._div(0.0, 0.0))
    assert journeys._div(3.0, 2.0) == 1.5
    keys = {journeys._map_key(v) for v in [1, 1.0, "1", True, None, undefined]}
    assert len(keys) == 5
    assert journeys._map_key([]) != journeys._map_key([])
    assert journeys._object_key_order(["b", "2019", "999", "a", "4294967295", "01"]) == [
        "999",
        "2019",
        "b",
        "a",
        "4294967295",
        "01",
    ]
    assert journeys._path_join("", "") == "."
    assert journeys._path_join("//dir/../x", "id.json") == "/x/id.json"
    assert journeys._path_join("store", "/abs.json") == "store/abs.json"
    assert journeys._length("abc") is undefined
    assert journeys._truthy([]) and journeys._truthy({}) and not journeys._truthy(math.nan)
    assert not journeys._truthy(0) and not journeys._truthy("") and journeys._truthy("0")
    with pytest.raises(TypeError, match="reading 'x'"):
        journeys._prop(undefined, "x")


def test_segment_notes_takes_javascript_inputs() -> None:
    assert segment_notes(None) == []
    assert segment_notes("abcd") == []
    with pytest.raises(TypeError):
        segment_notes({"a": 1})
    noisy = [[0, 220], [0.05, 0], [0.1, "220"], [0.1], "x", [0.1, 220], [0.15, 220], [0.2, 220]]
    notes = segment_notes(noisy)
    assert [note["points"] for note in notes] == [4]
    assert notes[0]["durationSeconds"] == 0.25
    stringly = segment_notes([[None, 220], [None, 220], [None, 220], [None, 220]])
    assert stringly[0]["startSeconds"] is None and stringly[0]["durationSeconds"] == 0.05
    assert segment_notes([["a", 220]] * 6) == []


def test_note_core_tuning_coerces_null_durations_and_zero_weight() -> None:
    notes = [{"medianCents": 10.0 * i, "durationSeconds": None} for i in range(5)]
    tuning = note_core_tuning(notes)
    assert tuning["noteTimeSeconds"] == 0
    assert tuning["inTuneShare"] is None
    assert tuning["longestNoteSeconds"] == 0
    assert note_core_tuning(notes[:2])["noteTimeSeconds"] == 0
    tail = [{"medianCents": 0.0, "durationSeconds": 0.5}] * 4 + [
        {"medianCents": 40.0, "durationSeconds": 3.0}
    ]
    assert note_core_tuning(tail)["centErrorMedian"] is not None


def test_slopes_bootstrap_and_verdicts() -> None:
    assert theil_sen_slope([(2020.0, 1), (2020.2, 2)]) is None
    assert theil_sen_slope([(2020.0, "1"), (2021.0, "3")]) == 2
    assert bootstrap_ci([1.0], 1) is None
    assert journeys.median_of([]) is None
    assert journeys.quantile_of([math.nan], 0.5) is None
    for direction, ci, verdict in [
        ("down_good", [-2, -1], "improving"),
        ("down_good", [1, 2], "declining"),
        ("down_good", [-1, 1], "flat"),
        ("up_good", [1, 2], "improving"),
        ("up_good", [-2, -1], "declining"),
        ("up_good", [-1, 1], "flat"),
        ("neutral", [1, 2], "trending_up"),
        ("neutral", [-2, -1], "trending_down"),
        ("neutral", [-1, 1], "flat"),
        ("neutral", None, "insufficient_data"),
    ]:
        assert journeys.verdict_for(direction, ci) == verdict
    assert journeys._to_uint32(math.nan) == 0
    assert journeys._to_uint32(-1) == 2**32 - 1


def test_per_year_keys_follow_object_order_and_refuse_prototype_names() -> None:
    def take(year: Any) -> dict[str, Any]:
        return {
            "year": year,
            "metrics": dict.fromkeys(
                ["noteCoreCentError", "inTuneShare", "rangeSemitones", "cpps", "longestSustain"],
                1.0,
            ),
        }

    rollup = journeys._per_year_rollup([take("unknown"), take(2020.0), take("999"), take("2020")])
    assert list(rollup) == ["999", "2020", "unknown"]
    assert rollup["2020"]["n"] == 2
    with pytest.raises(TypeError, match="push is not a function"):
        journeys._per_year_rollup([take("constructor")])


def test_analyze_handles_odd_rows_like_the_javascript(tmp_path: Path) -> None:
    rows = [
        {
            "status": "completed",
            "capturedAt": f"{2018 + i}-0{1 + i}-01T00:00:00Z",
            "recordingId": f"r{i}",
            "features": {"vibrato": None, "quality": {"cpps": i}},
        }
        for i in range(6)
    ]
    rows.append({"status": "completed", "capturedAt": "2020-01-01", "features": {"x": 1}})
    rows.append({"status": "completed", "capturedAt": "garbage", "recordingId": "g", "features": 1})
    clusters = [
        {"recordingIds": [f"r{i}" for i in range(6)], "label": None},
        3,
        {"clusterId": "x", "recordingIds": ["g"]},
    ]
    seen: list[Any] = []

    def reader(recording_id: Any) -> Any:
        seen.append(recording_id)
        return {"detail": {"f0ContourVoiced50ms": []}} if recording_id in ("r0", None) else 5

    options = JourneysOptions(out=str(tmp_path / "o.json"), analyzed_at="t")
    manifest = analyze_journeys({"results": rows}, {"clusters": clusters}, options, reader)
    assert seen[-2:] == [None, "g"]
    assert manifest["totals"]["takesWithNoteCore"] == 2
    written = json.loads((tmp_path / "o.json").read_text())["noteCore"]["takes"]
    assert written[0]["quarter"] == "2018-Q1" and written[0]["notes"] == 0
    assert "id" not in written[1] and written[1]["year"] == "2020"
    journey = manifest["journeys"][0]
    assert "clusterId" not in journey and "label" not in journey
    assert journey["firstYear"] == "2018" and journey["lastYear"] == "2023"
    assert 0.9 < journey["slopesPerYear"]["cpps"] < 1
    bad = [{"status": "completed", "capturedAt": 2020, "features": {}, "recordingId": "n"}]
    with pytest.raises(TypeError, match="slice"):
        analyze_journeys({"results": bad}, {}, options, reader)
    odd = [{**rows[0], "capturedAt": "2020-xx-01"}, {**rows[1], "year": 2019}]
    manifest = analyze_journeys({"results": odd}, {}, options, reader)
    assert [take["quarter"] for take in manifest["noteCore"]["takes"]] == ["2020-QNaN"]
    assert manifest["sources"]["lyrics"]["clusterCount"] is None


def test_date_parse_iso_forms() -> None:
    parse = journeys._date_parse
    assert parse("2019-02-10T00:00:00Z") == 1549756800000
    assert parse("2019-02-10T00:00:00.1234Z") == 1549756800123
    assert parse("+275760-09-13T00:00:00.001Z") is None
    assert parse("-000000-01-01") is None
    assert parse("2019-02-10T00:00:00+24:00") is None
    assert parse("2019-02-10T24:00:01Z") is None
    assert parse("2019-13-01") is None
    assert parse("2019/02/10") is None
    assert parse("2019-02-10T00:00:00-01:00") == 1549760400000
    assert journeys._utc_offset_ms(10**20) == 0
    assert journeys.year_fraction_of(None) is None


# --- parity with the original JavaScript ------------------------------------------

JS_DATES = [
    "2019-02-10T00:00:00Z",
    "2019-02-10T00:00:00.123456Z",
    "2019-02-10t00:00:00z",
    "2019-02-10T00:00Z",
    "2019-02-10T00Z",
    "2019-02-10",
    "2019-02",
    "2019",
    "2019Z",
    "2019T10:00Z",
    "2019-02-10Z",
    "2019-02-10T00:00:00",
    "2019-02-10T10:00:00.5",
    "2019-02-10 00:00:00",
    "2019-02-10 00:00:00Z",
    "2019-02-10T00:00:00+01:00",
    "2019-02-10T00:00:00+0100",
    "2019-02-10T00:00:00+23:59",
    "2019-02-10T00:00:00+24:00",
    "2019-02-10T00:00:00+01:60",
    "2019-02-10T10:00+01",
    "2019-02-10T24:00:00Z",
    "2019-02-10T24:00:00.001Z",
    "2019-02-10T25:00:00Z",
    "2019-02-10T00:60:00Z",
    "2019-02-10T00:00:60Z",
    "+002019-02-10T00:00:00Z",
    "-000001-01-01T00:00:00Z",
    "-000000-01-01T00:00:00Z",
    "+275760-09-13T00:00:00Z",
    "+275760-09-13T00:00:00.001Z",
    "2019-02-30T00:00:00Z",
    "2019-04-31",
    "2019-02-32",
    "2019-00-10",
    "2019-02-00",
    "2019-13-01",
    "20190210",
    "2019-02-10T00:00:00.Z",
    "2019-02-10T00:00:00Z ",
    "1900-01-01T00:00:00",
    "2019-03-10T02:30:00",
    "2019-11-03T01:30:00",
    "garbage",
    "",
]
JS_NUMBERS = [
    "",
    "  ",
    "12",
    " 12 ",
    "+5",
    "-5",
    "1e3",
    ".5",
    "5.",
    "0x1F",
    "0o17",
    "0b101",
    "-0x10",
    "Infinity",
    "-Infinity",
    "infinity",
    "1_000",
    "nan",
    "1e400",
    "0x",
    " 12﻿",
    "\x1c12",
    "07",
    "1.5e-3",
    "e5",
    "--1",
]


def _node(script: str, **env: str) -> str:
    assert NODE is not None
    return subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
    ).stdout


@pytest.fixture
def chicago(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.usefixtures("chicago")
def test_date_parse_and_number_match_node() -> None:
    script = (
        f"const d={json.dumps(JS_DATES)};const n={json.dumps(JS_NUMBERS)};"
        "console.log(JSON.stringify({d:d.map(x=>{const v=Date.parse(x);"
        "return Number.isNaN(v)?null:v}),n:n.map(x=>String(Number(x)))}))"
    )
    node = json.loads(_node(script, TZ="America/Chicago"))
    assert [journeys._date_parse(text) for text in JS_DATES] == node["d"]
    assert [journeys._to_string(journeys._number_from_text(t)) for t in JS_NUMBERS] == node["n"]


@pytest.fixture(scope="module")
def legacy_src(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """voice-journey's ``src/`` as imported (commit 1f236b5), extracted from git."""

    if NODE is None:
        pytest.skip("node is not installed")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    archive = subprocess.run(
        [git, "-C", str(REPO), "archive", IMPORT_COMMIT, "legacy/voice-journey/src"],
        capture_output=True,
    )
    if archive.returncode != 0:
        pytest.skip(f"commit {IMPORT_COMMIT} is not available (shallow checkout?)")
    root = tmp_path_factory.mktemp("legacy")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(root, filter="data")
    return root / "legacy" / "voice-journey" / "src"


def test_bootstrap_matches_node_for_any_seed(legacy_src: Path) -> None:
    values = [-2.1, -1.9, -2.4, -1.7, -2.2, 0.3, -0.5]
    seeds = [0, 7, 2026, 2**31, 2**32 + 5, 2**53, 1e21]
    module = (legacy_src / "journeys.mjs").as_uri()
    script = (
        f"const {{bootstrapCi,theilSenSlope}}=await import({json.dumps(module)});"
        f"const v={json.dumps(values)};"
        f"console.log(JSON.stringify([{','.join(f'bootstrapCi(v,{s!r})' for s in seeds)}]))"
    )
    assert json.loads(_node(script)) == [bootstrap_ci(values, seed) for seed in seeds]
    random_ = journeys.mulberry32(2026)
    assert [random_() for _ in range(3)] == _first_draws(legacy_src)


def _first_draws(legacy_src: Path) -> list[float]:
    source = (legacy_src / "journeys.mjs").read_text(encoding="utf-8")
    start = source.index("function mulberry32")
    end = source.index("function segmentNotes")
    script = (
        source[start:end] + "const r=mulberry32(2026);console.log(JSON.stringify([r(),r(),r()]))"
    )
    return list(json.loads(_node(script)))


def _parity_fixture(root: Path) -> tuple[Path, Path, Path]:
    """A synthetic corpus with every branch the analysis takes: noisy contours, missing
    and short details, unreliable rows, odd dates, years as numbers, clusters too small,
    too short or unlabeled."""

    rng = random.Random(7)
    store = root / "store"
    store.mkdir()
    rows: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    for cluster_index in range(16):
        ids: list[str] = []
        drift = rng.uniform(-6, 6)
        for take_index in range(rng.randint(5, 14)):
            recording_id = f"c{cluster_index}-t{take_index}"
            ids.append(recording_id)
            year = rng.randint(2018, 2023)
            stamp = f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
            captured = rng.choice(
                [
                    f"{stamp}T10:11:12Z",
                    f"{stamp}T10:11:12.345Z",
                    f"{stamp}T23:30:00+01:00",
                    f"{stamp}T08:00:00",
                    stamp,
                ]
            )
            row: dict[str, Any] = {
                "recordingId": recording_id,
                "capturedAt": captured,
                "status": "completed" if rng.random() > 0.05 else "failed",
                "features": {
                    "voicing": {
                        "rejectedOutlierShare": round(rng.uniform(0, 0.08), 3),
                        "rejectedGlobalOutlierShare": round(rng.uniform(0, 0.04), 3),
                    },
                    "pitch": {"rangeSemitonesP05P95": round(rng.uniform(8, 20), 2)},
                    "quality": {
                        "cpps": round(10 + (year - 2018) * drift * 0.1 + rng.gauss(0, 1), 3)
                    },
                    "phrasing": {"longestSustainedSeconds": round(rng.uniform(1, 5), 2)},
                },
            }
            if rng.random() > 0.2:
                row["features"]["vibrato"] = {"meanRateHz": round(rng.uniform(3.5, 7), 2)}
            if rng.random() > 0.3:
                row["year"] = rng.choice([str(year), year])
            rows.append(row)
            if rng.random() < 0.1:
                continue
            spread = max(2.0, 30 - (year - 2018) * drift)
            contour: list[list[float]] = []
            t = 0.0
            for note in range(rng.randint(3, 12)):
                base = -1200 + note * rng.choice([100, 200, 300]) + rng.uniform(-spread, spread)
                for _ in range(rng.randint(2, 14)):
                    contour.append(
                        [to_fixed(t, 2) or 0.0, 440 * 2 ** ((base + rng.gauss(0, 6)) / 1200)]
                    )
                    t += 0.05
                t += rng.choice([0, 0.05, 0.2, 0.4])
            detail = {"recordingId": recording_id, "detail": {"f0ContourVoiced50ms": contour}}
            (store / f"{recording_id}.json").write_text(json.dumps(detail))
        cluster: dict[str, Any] = {"clusterId": f"cluster-{cluster_index}", "recordingIds": ids}
        if cluster_index != 3:
            cluster["label"] = f"recurring_lyric_cluster_{chr(65 + cluster_index)}"
        clusters.append(cluster)
    clusters.append({"clusterId": "cluster-tiny", "recordingIds": ["c0-t0", "missing"]})
    features = root / "features.json"
    features.write_text(
        json.dumps(
            {
                "schemaVersion": "voice-journey.local-voice-features.v1",
                "generatedAt": ANALYZED_AT,
                "results": rows,
            }
        )
    )
    lyrics = root / "lyrics.json"
    lyrics.write_text(
        json.dumps(
            {"schemaVersion": "voice-journey.local-lyric-indicators.v1", "clusters": clusters}
        )
    )
    return features, lyrics, store


@pytest.mark.usefixtures("chicago")
def test_manifest_matches_the_javascript_byte_for_byte(
    legacy_src: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    features, lyrics, store = _parity_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    for name in ("VOICE_JOURNEY_DATA", "VOICE_JOURNEY_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    common = [
        "analyze",
        "--features",
        str(features),
        "--lyrics",
        str(lyrics),
        "--feature-dir",
        str(store),
        "--analyzed-at",
        ANALYZED_AT,
        "--seed",
        "99",
    ]
    env = {"PATH": "/usr/bin:/bin", "TZ": "America/Chicago"}
    script = str(legacy_src / "journeys.mjs")
    assert NODE is not None
    for argv in (
        [*common, "--out", "js.json"],
        [*common, "--dry-run"],
        ["analyze", "--features", str(features), "--lyrics", str(lyrics), "--dry-run"],
        ["--help"],
    ):
        node = subprocess.run(
            [NODE, script, *argv], capture_output=True, text=True, env=env, cwd=tmp_path, check=True
        )
        python_argv = [("py.json" if arg == "js.json" else arg) for arg in argv]
        assert main(python_argv) == 0
        python_out = capsys.readouterr().out
        assert python_out == node.stdout.replace("js.json", "py.json")
    js = (tmp_path / "js.json").read_bytes()
    assert (tmp_path / "py.json").read_bytes() == js
    manifest = json.loads(js)
    assert manifest["totals"]["clustersEligible"] >= 10
    assert manifest["totals"]["missingDetailFiles"] >= 1
    assert any(item["ci95"] for item in manifest["improvementIndex"])

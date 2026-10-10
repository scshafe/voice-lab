"""corpus-browser.test.mjs, translated (design §5.3): the data layer's cases (the HTTP
cases are in tests/test_api_journey.py), then the cases that hold the port's remaining
branches."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from journey_browser_fixtures import (
    fake_features,
    fake_index,
    fake_lyrics,
    fake_results,
    fake_state,
    fake_transcripts,
    journey_config,
    referee_lyrics,
    write_manifests,
)

from cole_voice_lab.journey import browser
from cole_voice_lab.journey.browser import (
    BrowserError,
    CorpusAccessDenied,
    JourneyConfig,
    JourneyStore,
    SeamMismatch,
    bradley_terry_by_year,
    build_corpus_rows,
    build_referee_pairs,
    build_voice_trends,
    compare_values,
    filter_rows,
    loudness_match_gains_db,
    merge_verdicts_into_results,
    mulberry32,
    open_recording_audio,
    paginate_rows,
    parse_range,
    project_rows,
    query_rows,
    read_goals_state,
    read_referee_state,
    read_state,
    read_watch_status,
    select_review_queue,
    sort_rows,
)
from cole_voice_lab.journey.corpus import recording_id
from cole_voice_lab.journey.jsonio import MISSING, read_json, stringify
from cole_voice_lab.journey.paths import resolve_paths


def _params(query: str) -> list[tuple[str, str]]:
    from urllib.parse import parse_qsl

    return parse_qsl(query, keep_blank_values=True)


def _row(rows: list[dict[str, Any]], recording: str) -> dict[str, Any]:
    return next(row for row in rows if row["recordingId"] == recording)


# ---- corpus-browser.test.mjs ----------------------------------------------------------


def test_build_corpus_rows_joins_manifests_and_overlays_local_verdict_state() -> None:
    payload = build_corpus_rows(fake_index(), fake_results(), state=fake_state(), queue_size=5)
    assert payload["schemaVersion"] == "voice-journey.corpus-browser-rows.v1"
    assert len(payload["rows"]) == 7
    assert payload["source"]["readScope"]["audioBytes"] is False
    assert payload["source"]["readScope"]["verdictWrites"] is True
    assert _row(payload["rows"], "b")["spotCheckStatus"] == "reviewed"
    assert _row(payload["rows"], "b")["humanVerdict"] == "clean_singing"
    assert len([row for row in payload["rows"] if row["reviewQueueSelected"]]) == 5


def test_build_corpus_rows_tolerates_absent_transcript_and_lyric_manifests() -> None:
    payload = build_corpus_rows(fake_index(), fake_results(), queue_size=5)
    assert payload["source"]["transcripts"]["available"] is False
    assert payload["source"]["lyrics"]["available"] is False
    assert _row(payload["rows"], "a")["transcriptStatus"] == "not_available"
    assert _row(payload["rows"], "a")["lyricMatchStatus"] == "not_available"


def test_build_corpus_rows_joins_partial_and_complete_transcript_and_lyric_manifests() -> None:
    payload = build_corpus_rows(
        fake_index(),
        fake_results(),
        queue_size=5,
        transcripts_manifest=fake_transcripts(),
        lyrics_manifest=fake_lyrics(),
    )
    completed = _row(payload["rows"], "a")
    assert payload["source"]["transcripts"]["available"] is True
    assert payload["source"]["lyrics"]["available"] is True
    assert completed["transcriptStatus"] == "completed"
    assert completed["transcriptLanguage"] == "en"
    assert completed["transcriptWordCount"] == 42
    assert completed["transcriptTextLocalOnly"] is True
    assert completed["lyricMatchStatus"] == "marked_indicator"
    assert completed["lyricMatchCluster"] == "lyric-cluster-001"
    assert completed["lyricMatchClusterLabel"] == "recurring_lyric_cluster_001"
    assert completed["wordlessVocaliseMarked"] is False
    assert _row(payload["rows"], "b")["wordlessVocaliseMarked"] is True
    missing = _row(payload["rows"], "g")
    assert missing["transcriptStatus"] == "not_available"
    assert missing["lyricMatchStatus"] == "not_available"


def test_select_review_queue_includes_uncertain_rows_and_bucket_controls() -> None:
    rows = build_corpus_rows(fake_index(), fake_results(), queue_size=7)["rows"]
    queue = [row["bucket"] for row, _ in select_review_queue(rows, 56)]
    for bucket in (
        "uncertain_manual_review",
        "clean_singing",
        "noise_contaminated_singing",
        "non_singing",
    ):
        assert bucket in queue


def test_filter_rows_and_sort_rows_support_browser_table_axes() -> None:
    rows = build_corpus_rows(fake_index(), fake_results(), queue_size=5)["rows"]

    def ids(found: list[dict[str, Any]]) -> list[str]:
        return [row["recordingId"] for row in found]

    assert ids(filter_rows(rows, {"bucket": "uncertain_manual_review"})) == ["b", "d"]
    assert ids(filter_rows(rows, {"year": "2020", "spotCheckStatus": "reviewed"})) == ["c"]
    assert filter_rows(rows, {"reviewQueue": "selected"})
    assert ids(sort_rows(rows, "confidence", "desc")[:2]) == ["a", "g"]


def test_paginate_rows_slices_only_with_paging_params_and_clamps_bad_input() -> None:
    rows = build_corpus_rows(fake_index(), fake_results(), queue_size=5)["rows"]
    unpaged, page = paginate_rows(rows, _params(""))
    assert page is None and len(unpaged) == 7
    paged, page = paginate_rows(rows, _params("limit=2&offset=1"))
    assert page == {"offset": 1, "limit": 2, "returned": 2, "totalFiltered": 7}
    assert [row["recordingId"] for row in paged] == ["b", "c"]
    _, page = paginate_rows(rows, _params("limit=5&offset=5"))
    assert page == {"offset": 5, "limit": 5, "returned": 2, "totalFiltered": 7}
    _, page = paginate_rows(rows, _params("limit=9999&offset=-3"))
    assert page is not None and page["limit"] == 500 and page["offset"] == 0
    _, page = paginate_rows(rows, _params("offset=abc"))
    assert page == {"offset": 0, "limit": 100, "returned": 7, "totalFiltered": 7}


def test_project_rows_lean_list_drops_heavy_fields_and_rejects_unknown() -> None:
    rows = build_corpus_rows(fake_index(), fake_results(), queue_size=5)["rows"]
    assert project_rows(rows, None)[0]["rationale"] == "clean_singing rationale"
    lean = project_rows(rows, "list")
    assert len(lean) == 7
    assert lean[0]["recordingId"] == "a" and lean[0]["bucket"] == "clean_singing"
    assert "rationale" not in lean[0] and "sizeBytes" not in lean[0]
    with pytest.raises(BrowserError) as refused:
        project_rows(rows, "bogus")
    assert refused.value.status_code == 400


def test_loudness_match_gains_are_symmetric_capped_and_default_to_zero() -> None:
    assert loudness_match_gains_db(-25, -31) == {"aGainDb": -3, "bGainDb": 3}
    assert loudness_match_gains_db(-10, -60) == {"aGainDb": -12, "bGainDb": 12}
    assert loudness_match_gains_db(None, -20) == {"aGainDb": 0, "bGainDb": 0}
    lyrics = {"clusters": [{"clusterId": "c1", "label": "c1", "recordingIds": ["x", "y"]}]}
    features = {
        "results": [
            {
                "recordingId": "x",
                "capturedAt": "2020-01-01T00:00:00Z",
                "year": "2020",
                "status": "completed",
                "features": {"voicing": {}, "dynamics": {"voicedRmsDbP50": -20}},
            },
            {
                "recordingId": "y",
                "capturedAt": "2021-01-01T00:00:00Z",
                "year": "2021",
                "status": "completed",
                "features": {"voicing": {}, "dynamics": {"voicedRmsDbP50": -26}},
            },
        ]
    }
    pool = build_referee_pairs(lyrics, features, seed=7)
    assert len(pool) == 1
    assert pool[0]["aGainDb"] + pool[0]["bGainDb"] == 0
    assert abs(pool[0]["aGainDb"]) == 3


def test_merge_verdicts_into_results_writes_repo_safe_human_spot_check_metadata() -> None:
    merged = merge_verdicts_into_results(fake_results(), fake_state())
    entry = _row(merged["results"], "b")
    assert merged["humanSpotCheckMerge"]["mergedCount"] == 1
    assert entry["humanSpotCheck"]["status"] == "reviewed"
    assert entry["humanSpotCheck"]["verdict"] == "clean_singing"
    assert entry["humanSpotCheck"]["notes"] == "fixture"


def test_merge_verdicts_command_writes_output_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results_path, state_path, out_path = (
        tmp_path / "results.json",
        tmp_path / "state.json",
        tmp_path / "merged.json",
    )
    results_path.write_text(json.dumps(fake_results()), encoding="utf-8")
    state_path.write_text(json.dumps(fake_state()), encoding="utf-8")
    code = browser.main(
        [
            "merge-verdicts",
            "--results",
            str(results_path),
            "--state",
            str(state_path),
            "--out",
            str(out_path),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["mergedCount"] == 1
    merged = read_json(out_path)
    assert _row(merged["results"], "b")["humanSpotCheck"]["verdict"] == "clean_singing"


def test_build_voice_trends_aggregates_quarters_and_the_working_top_headline() -> None:
    rows = [
        {"capturedAt": "2019-01-05T00:00:00Z"},
        {"capturedAt": "2019-01-20T00:00:00Z"},
        {"capturedAt": "2021-02-10T00:00:00Z"},
    ]
    trends = build_voice_trends(fake_features(), rows)
    assert trends["available"] is True
    headline = trends["headline"]
    assert (headline["takesAnalyzed"], headline["reliableTakes"]) == (25, 24)
    assert headline["flaggedTakes"] == 1
    assert headline["workingTop"]["baselineYear"] == "2019"
    assert headline["workingTop"]["peakYear"] == "2021"
    assert headline["workingTop"]["gainSemitones"] == 7
    q1_2019 = next(item for item in trends["quarters"] if item["quarter"] == "2019-Q1")
    assert q1_2019["nReliable"] == 6
    assert q1_2019["envelope"]["hiHz"] == 260
    assert q1_2019["registerShares"]["low"] == 1
    q1_2021 = next(item for item in trends["quarters"] if item["quarter"] == "2021-Q1")
    assert q1_2021["registerShares"]["high"] == 1
    assert trends["monthlyCadence"] == [
        {"month": "2019-01", "count": 2},
        {"month": "2021-02", "count": 1},
    ]
    assert "f0ContourVoiced50ms" not in stringify(trends)
    assert "vibratoSegments" not in stringify(trends)


def test_build_voice_trends_unavailable_without_features_still_carries_cadence() -> None:
    trends = build_voice_trends(None, [{"capturedAt": "2020-05-01T00:00:00Z"}])
    assert trends["available"] is False
    assert trends["reason"] == "features_manifest_not_loaded"
    assert trends["monthlyCadence"] == [{"month": "2020-05", "count": 1}]


def test_build_voice_trends_spreads_all_takes_vibrato_points_eras_and_threads() -> None:
    rows = [
        {"capturedAt": f"{month}-10T00:00:00Z"}
        for month, count in (("2021-01", 50), ("2021-02", 45), ("2021-04", 44), ("2022-06", 5))
        for _ in range(count)
    ]
    trends = build_voice_trends(fake_features(), rows, lyrics_manifest=fake_lyrics())
    q1 = next(item for item in trends["quarters"] if item["quarter"] == "2021-Q1")
    q1_all = next(item for item in trends["quartersAllTakes"] if item["quarter"] == "2021-Q1")
    assert q1["nReliable"] == 6 and q1_all["nReliable"] == 7
    assert isinstance(q1["spread"]["rate"], list) and len(q1["spread"]["rate"]) == 2
    assert isinstance(q1["spread"]["cpps"], list)
    assert len(trends["vibratoTakes"]) == 25
    assert (
        next(take for take in trends["vibratoTakes"] if take["id"] == "flagged")["reliable"]
        is False
    )
    assert len(trends["eras"]) == 1
    era = trends["eras"][0]
    assert (era["start"], era["end"], era["totalTakes"]) == ("2021-01", "2021-04", 139)
    assert len(era["sampleRecordingIds"]) == 3
    assert len(trends["clusters"]) == 1
    thread = trends["clusters"][0]
    assert (thread["id"], thread["size"], thread["first"]) == ("lyric-cluster-001", 2, "2019-01")
    assert thread["perYear"] == {"2019": 1, "2020": 1}


def test_build_referee_pairs_is_seeded_cross_year_reliable_and_capped() -> None:
    pool_a = build_referee_pairs(referee_lyrics(), fake_features(), seed=7, pairs_per_cluster=4)
    pool_b = build_referee_pairs(referee_lyrics(), fake_features(), seed=7, pairs_per_cluster=4)
    assert pool_a == pool_b
    assert 2 <= len(pool_a) <= 5
    for pair in pool_a:
        assert pair["aYear"] != pair["bYear"]
        assert pair["aId"] not in {"flagged", "failed"}
        assert pair["bId"] not in {"flagged", "failed"}
        assert len(pair["pairId"]) == 16 and int(pair["pairId"], 16) >= 0
    assert len([pair for pair in pool_a if pair["clusterId"] == "cluster-song-1"]) <= 4
    pool_c = build_referee_pairs(referee_lyrics(), fake_features(), seed=8, pairs_per_cluster=4)
    assert [pair["pairId"] for pair in pool_a] != [pair["pairId"] for pair in pool_c]


def test_bradley_terry_by_year_ranks_the_winner_and_flags_chained_clusters() -> None:
    judgments: list[dict[str, Any]] = []
    judgments += [
        {"pairId": f"p{i}", "aYear": "2021", "bYear": "2019", "clusterId": "c1", "choice": "a"}
        for i in range(8)
    ]
    judgments += [
        {"pairId": f"q{i}", "aYear": "2019", "bYear": "2021", "clusterId": "c1", "choice": "a"}
        for i in range(2)
    ]
    judgments.append(
        {"pairId": "t1", "aYear": "2021", "bYear": "2019", "clusterId": "c1", "choice": "too_close"}
    )
    judgments.append(
        {
            "pairId": "n1",
            "aYear": "2021",
            "bYear": "2019",
            "clusterId": "cluster-chained",
            "choice": "not_same_song",
        }
    )
    judgments.append(
        {"pairId": "s1", "aYear": "2021", "bYear": "2019", "clusterId": "c1", "choice": "skip"}
    )
    fit = bradley_terry_by_year(judgments)
    y2021 = next(row for row in fit["years"] if row["year"] == "2021")
    y2019 = next(row for row in fit["years"] if row["year"] == "2019")
    assert y2021["strengthLog2"] > 0.5
    assert y2019["strengthLog2"] == 0
    assert (y2021["wins"], y2021["losses"], y2021["ties"]) == (8, 2, 1)
    assert fit["notSameSongFlags"] == [{"clusterId": "cluster-chained", "count": 1}]
    assert fit["byChoice"]["skip"] == 1
    again = bradley_terry_by_year(judgments)
    assert (
        next(row for row in again["years"] if row["year"] == "2021")["strengthLog2"]
        == (y2021["strengthLog2"])
    )


# ---- the port's remaining branches ----------------------------------------------------

NODE = shutil.which("node")
MULBERRY_JS = """
function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const out = [];
for (const seed of [0, 1, 7, 2026, 4294967295, 123456789]) {
  const r = mulberry32(seed);
  for (let i = 0; i < 50; i += 1) out.push(r());
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_mulberry32_matches_node_bit_for_bit() -> None:
    assert NODE is not None
    node = json.loads(
        subprocess.run([NODE, "-e", MULBERRY_JS], check=True, capture_output=True, text=True).stdout
    )
    mine = []
    for seed in (0, 1, 7, 2026, 4294967295, 123456789):
        random = mulberry32(seed)
        mine += [random() for _ in range(50)]
    assert mine == node


def test_mulberry32_first_values() -> None:
    random = mulberry32(2026)
    values = [random() for _ in range(3)]
    assert all(0 <= value < 1 for value in values)
    assert len(set(values)) == 3


def test_compare_values_follows_the_js_comparator() -> None:
    assert compare_values(1, 1.0) == 0
    assert compare_values("a", "a") == 0
    assert compare_values(None, None) == 0
    assert compare_values(MISSING, MISSING) == 0
    assert compare_values(None, 1) == 1 and compare_values(MISSING, "a") == 1
    assert compare_values(1, None) == -1
    assert compare_values(1, 3) == -2
    assert compare_values("b", "a") == 1
    assert compare_values(True, 1) == 1  # String(true) vs "1"
    marker: dict[str, Any] = {}
    assert compare_values(marker, marker) == 0
    assert compare_values({}, {}) == 0  # "[object Object]" both


def test_rows_query_sorts_with_first_values_and_filters_with_last() -> None:
    rows = build_corpus_rows(fake_index(), fake_results(), queue_size=5)["rows"]
    found = query_rows(rows, _params("bucket=non_singing&bucket=clean_singing&sort=bogus"))
    assert [row["recordingId"] for row in found] == ["a", "g"]
    found = query_rows(rows, _params("sort=year&sort=filename&direction=desc&q=%20RATIONALE"))
    assert [row["year"] for row in found][:2] == ["2024", "2023"]
    assert filter_rows(rows, {"q": "zz-nothing"}) == []
    assert filter_rows(rows, {"bucket": ""}) == rows
    assert paginate_rows(rows, _params("limit=1e3"))[1] == {
        "offset": 0,
        "limit": 1,
        "returned": 1,
        "totalFiltered": 7,
    }


def test_rows_tolerate_sparse_manifests() -> None:
    index = {
        "recordings": [
            {"recordingId": "x", "capturedAt": None},
            {"recordingId": "y", "filename": "y.m4a", "capturedAt": "2020-03-01T00:00:00Z"},
        ]
    }
    results = {"results": [{"recordingId": "y", "year": 2020}, {"capturedAt": "no id"}]}
    transcripts = {
        "schemaVersion": "t",
        "sourceTranscripts": {"recordingCount": 9},
        "results": [{"recordingId": "x", "status": "completed", "counts": {"words": 3}}],
    }
    payload = build_corpus_rows(index, results, transcripts_manifest=transcripts)
    x, y = payload["rows"]
    assert "filename" not in json.loads(stringify(x))
    assert x["year"] == "unknown" and x["transcriptHasWords"] is True
    assert x["wordlessVocaliseMarked"] is False and x["transcriptTextLocalOnly"] is False
    assert y["year"] == 2020 and y["bucket"] == "unclassified"
    assert payload["source"]["index"]["recordingCount"] == 2
    assert payload["source"]["transcripts"]["recordingCount"] == 1
    assert payload["totals"]["years"] == {"2020": 1, "unknown": 1}
    summary = browser.manifest_source_summary({"sourceTranscripts": {"recordingCount": 9}})
    assert summary == {"available": True, "schemaVersion": None, "recordingCount": 9}
    with pytest.raises(TypeError):
        browser.year_for(2020)


def test_review_queue_budget_controls_and_backfill() -> None:
    rows = build_corpus_rows(fake_index(), fake_results(), queue_size=56)["rows"]
    reasons = {reason for _, reason in select_review_queue(rows, 56)}
    assert "music_contaminated_singing_control" in reasons
    small = select_review_queue(rows, 3)
    assert (
        sorted(reason for _, reason in small)
        == ["backfill_stratified"] + ["uncertain_year_confidence_stratified"] * 2
    )
    assert select_review_queue([], 5) == []
    assert browser.stratified_pick(rows, 0) == []
    many = [
        {"recordingId": f"r{i:02d}", "year": "2020", "confidence": 0.5, "bucket": "x"}
        for i in range(10)
    ]
    assert len(browser.stratified_pick(many, 4)) == 4
    duplicates = [many[0], many[0], many[1]]
    assert [row["recordingId"] for row in browser.stratified_pick(duplicates, 2)] == [
        "r00",
        "r01",
    ]


def test_filter_options_are_unique_truthy_and_default_sorted() -> None:
    rows = build_corpus_rows(fake_index(), fake_results(), queue_size=5)["rows"]
    options = browser.filter_options_for(rows)
    assert options["years"] == ["2019", "2020", "2021", "2022", "2023", "2024"]
    assert options["spotCheckStatuses"] == ["not_reviewed", "reviewed"]
    assert options["lyricMatchStatuses"] == ["not_available"]


def test_merge_keeps_unreviewed_results_and_tolerates_odd_spot_checks() -> None:
    results = {
        "schemaVersion": "r",
        "results": [
            {"recordingId": "b", "humanSpotCheck": "odd"},
            {"recordingId": "z"},
        ],
    }
    state = {
        "schemaVersion": browser.STATE_SCHEMA_VERSION,
        "verdicts": [{"recordingId": "b", "verdict": "non_singing"}, {"verdict": "x"}],
    }
    merged = merge_verdicts_into_results(results, state)
    assert merged["results"][1] == {"recordingId": "z"}
    check = json.loads(stringify(merged["results"][0]["humanSpotCheck"]))
    assert check == {"recommended": True, "status": "reviewed", "verdict": "non_singing"}
    assert list(merged) == ["schemaVersion", "results", "updatedAt", "humanSpotCheckMerge"]


def test_voice_trends_edge_cases() -> None:
    assert browser.quarter_for(None) is None
    assert browser.quarter_for("2020-1") is None
    assert browser.quarter_for("2020-13-01") is None
    assert browser.quarter_for("2020-xx-01") is None
    assert browser.quarter_for("2020-12-01") == "2020-Q4"
    assert browser.round_to("1") is None and browser.round_to(math.inf) is None
    assert browser.quantile_of([None, "x", 3, 1], 0.5) == 1
    assert browser._div(1, 0) == math.inf and browser._div(-1, 0) == -math.inf
    assert math.isnan(browser._div(0, 0))
    features = {
        "results": [
            {
                "recordingId": "a",
                "capturedAt": "2019-01-01T00:00:00Z",
                "status": "completed",
                "features": {"pitch": {"f0Hz": {"p95": 0, "p50": 250}}},
            }
            for _ in range(10)
        ]
        + [
            {
                "recordingId": "b",
                "capturedAt": "2020-01-01T00:00:00Z",
                "year": "2020",
                "status": "completed",
                "features": {"pitch": {"f0Hz": {"p95": 300, "p50": 250}}},
            }
            for _ in range(10)
        ]
        + [{"recordingId": "c", "capturedAt": None, "status": "completed", "features": {}}]
    }
    trends = build_voice_trends(features, [{"capturedAt": None}, {"capturedAt": "2020-01-01"}])
    assert trends["headline"]["workingTop"]["gainSemitones"] is None
    assert trends["headline"]["workingTop"]["peakYear"] == "2020"
    assert trends["quarters"][0]["registerShares"]["mid"] == 1
    assert trends["vibratoTakes"] == []
    assert trends["clusters"] == []
    assert browser.cluster_threads({"clusters": "nope"}) == []
    threads = browser.cluster_threads(
        {
            "clusters": [
                {"clusterId": "b", "recordingIds": ["1", "2"]},
                {"clusterId": "a", "recordingIds": ["1", "2"]},
                {"clusterId": "c", "recordingIds": ["9"]},
                {"clusterId": "d"},
            ],
            "results": [
                {"recordingId": "1", "capturedAt": "2020-01-01"},
                {"recordingId": "2", "capturedAt": "2021-06-01"},
            ],
        }
    )
    assert [thread["id"] for thread in threads] == ["a", "b"]
    assert threads[0]["label"] == "a" and threads[0]["last"] == "2021-06"
    cadence = [{"month": "2021-01", "count": 100}, {"month": "2021-09", "count": 100}]
    eras = browser.build_eras(cadence, [{"capturedAt": None}])
    assert [(era["start"], era["sampleRecordingIds"]) for era in eras] == [
        ("2021-01", []),
        ("2021-09", []),
    ]


def test_referee_pairs_skip_unusable_takes() -> None:
    features = {
        "results": [
            {
                "recordingId": "x",
                "capturedAt": "2020-01-01T00:00:00Z",
                "status": "completed",
                "features": {"voicing": {"rejectedOutlierShare": 0.5}},
            },
            {"recordingId": "y", "capturedAt": None, "status": "completed", "features": {}},
            {"recordingId": "z", "capturedAt": "2021-01-01T00:00:00Z", "status": "failed"},
        ]
    }
    lyrics = {"clusters": [{"clusterId": "c", "recordingIds": ["x", "y", "z"]}, {}]}
    assert build_referee_pairs(lyrics, features) == []
    assert build_referee_pairs(None, None) == []


def test_bradley_terry_edge_cases() -> None:
    assert bradley_terry_by_year([])["years"] == []
    single = bradley_terry_by_year(
        [
            {"aYear": "2020", "bYear": "2021", "choice": "b"},
            {"aYear": "2020", "bYear": "2020", "choice": "a"},
            {"aYear": None, "bYear": "2020", "choice": "a"},
            {"aYear": "2020", "bYear": "2021", "choice": "bogus"},
        ]
    )
    assert [row["trials"] for row in single["years"]] == [2, 2]
    assert single["byChoice"]["b"] == 1
    three = bradley_terry_by_year(
        [
            {"aYear": "2019", "bYear": "2020", "choice": "a"},
            {"aYear": "2021", "bYear": "2022", "choice": "b"},
        ]
    )
    assert [row["year"] for row in three["years"]] == ["2019", "2020", "2021", "2022"]
    lone = bradley_terry_by_year([{"aYear": "2019", "bYear": "2019", "choice": "a"}])
    assert lone["years"] == []
    one = bradley_terry_by_year([{"aYear": "2019", "bYear": "2020", "choice": "skip"}])
    assert one["judgedTotal"] == 1 and one["years"] == []


def test_state_files_fall_back_to_empty_when_absent_or_foreign(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert read_state(missing)["verdicts"] == []
    assert read_referee_state(missing)["judgments"] == []
    assert read_goals_state(missing) == {"version": "voice-journey.goals-state.v1", "goals": []}
    foreign = tmp_path / "foreign.json"
    foreign.write_text('{"schemaVersion": "other", "verdicts": []}', encoding="utf-8")
    assert read_state(foreign)["schemaVersion"] == browser.STATE_SCHEMA_VERSION
    foreign.write_text("null", encoding="utf-8")
    assert read_goals_state(foreign)["goals"] == []
    foreign.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        read_state(foreign)


def test_watch_status_reads_the_job_runners_state_file(tmp_path: Path) -> None:
    path = tmp_path / "watchd-state.json"
    assert read_watch_status(path) == {"enabled": False, "reason": "watch_not_enabled"}
    runs = [{"ok": True, "signature": f"s{i}"} for i in range(7)]
    path.write_text(
        json.dumps(
            {
                "version": "voice-journey.watchd-state.v1",
                "lastRun": runs[-1],
                "recentRuns": runs,
                "nextDigestAt": 1790000000000,
            }
        ),
        encoding="utf-8",
    )
    status = read_watch_status(path)
    assert status["enabled"] is True and status["lastRun"] == runs[-1]
    assert status["recentRuns"] == runs[-5:]
    assert status["nextDigestAt"] == "2026-09-21T14:13:20.000Z"
    path.write_text(
        json.dumps({"version": "voice-journey.watchd-state.v1", "recentRuns": "x"}),
        encoding="utf-8",
    )
    assert read_watch_status(path)["recentRuns"] == []
    assert read_watch_status(path)["nextDigestAt"] is None


def test_parse_range_follows_the_js() -> None:
    assert parse_range(None, 10) is None
    assert parse_range("bytes=2-4", 10) == (2, 4)
    assert parse_range("bytes=-4", 10) == (0, 4)
    assert parse_range("bytes=3-", 10) == (3, 9)
    assert parse_range("bytes=4-2", 10) is None
    assert parse_range("bytes=0-10", 10) is None
    assert parse_range("bytes=1-2-3", 10) is None
    assert parse_range("bytes=a-2", 10) is None
    assert parse_range("bytes=1", 10) is None
    assert parse_range("items=1-2", 10) is None
    assert parse_range("bytes=١-2", 10) is None
    assert parse_range("bytes=0-0", 0) is None


def _phone(tmp_path: Path, name: str = "20190101 000000-A.m4a") -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "phone"
    root.mkdir(exist_ok=True)
    (root / name).write_bytes(b"fixture-audio")
    return root, {"recordingId": recording_id(name), "filename": name}


def test_open_recording_audio_reads_only_what_the_row_names(tmp_path: Path) -> None:
    root, row = _phone(tmp_path)
    audio = open_recording_audio(row, corpus_root=root, measurement_dir=None, approval="ok")
    assert (audio.size, audio.content_type) == (13, "audio/mp4")
    assert audio.handle.read() == b"fixture-audio"
    audio.close()
    with pytest.raises(ValueError):
        open_recording_audio(row, corpus_root=root, measurement_dir=None, approval=None)
    with pytest.raises(SeamMismatch):
        open_recording_audio(
            {**row, "recordingId": "0" * 16}, corpus_root=root, measurement_dir=None, approval="ok"
        )
    for bad in ({"filename": None}, {"filename": ".hidden.m4a"}, {"filename": "a/../b.wav"}):
        with pytest.raises(FileNotFoundError):
            open_recording_audio(
                {**row, **bad}, corpus_root=root, measurement_dir=tmp_path, approval="ok"
            )
    with pytest.raises(FileNotFoundError):
        open_recording_audio(row, corpus_root=None, measurement_dir=None, approval="ok")
    gone = {"recordingId": recording_id("gone.m4a"), "filename": "gone.m4a"}
    with pytest.raises(FileNotFoundError):
        open_recording_audio(gone, corpus_root=root, measurement_dir=None, approval="ok")
    (root / "link.m4a").symlink_to(root / "20190101 000000-A.m4a")
    link = {"recordingId": recording_id("link.m4a"), "filename": "link.m4a"}
    with pytest.raises(FileNotFoundError):
        open_recording_audio(link, corpus_root=root, measurement_dir=None, approval="ok")
    (root / "dir.m4a").mkdir()
    directory = {"recordingId": recording_id("dir.m4a"), "filename": "dir.m4a"}
    with pytest.raises(FileNotFoundError):
        open_recording_audio(directory, corpus_root=root, measurement_dir=None, approval="ok")


def test_open_recording_audio_reads_measurement_segments(tmp_path: Path) -> None:
    session = tmp_path / "measurement" / "session-0001"
    session.mkdir(parents=True)
    (session / "seg.wav").write_bytes(b"RIFF")
    name = "session-0001/seg.wav"
    row = {"recordingId": recording_id(f"measurement/{name}"), "filename": name}
    audio = open_recording_audio(
        row, corpus_root=None, measurement_dir=tmp_path / "measurement", approval="ok"
    )
    assert (audio.size, audio.content_type) == (4, "audio/wav")
    audio.close()
    with pytest.raises(SeamMismatch):
        open_recording_audio(
            {**row, "recordingId": recording_id(name)},
            corpus_root=None,
            measurement_dir=tmp_path / "measurement",
            approval="ok",
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through any mode")
def test_open_recording_audio_maps_denials_to_corpus_access_denied(tmp_path: Path) -> None:
    root, row = _phone(tmp_path)
    root.chmod(0)
    try:
        with pytest.raises(CorpusAccessDenied):
            open_recording_audio(row, corpus_root=root, measurement_dir=None, approval="ok")
    finally:
        root.chmod(0o755)


def test_local_paths_stay_inside_their_directory(tmp_path: Path) -> None:
    assert browser.transcript_path(tmp_path, "a") == tmp_path.resolve() / "a.txt"
    assert browser.transcript_path(tmp_path, "../secret") is None
    assert browser.feature_detail_path(tmp_path, "/etc/passwd") is None
    assert browser.feature_detail_path(tmp_path, "..x") is None


def test_config_from_paths_follows_the_data_root(tmp_path: Path) -> None:
    rooted = JourneyConfig.from_paths(resolve_paths({"VOICE_JOURNEY_DATA": str(tmp_path)}))
    assert rooted.allow_empty_manifests is True
    assert rooted.transcript_dir == tmp_path / "artifacts" / "stt-transcripts"
    assert rooted.state == tmp_path / "artifacts" / "corpus-browser-state.json"
    assert rooted.playback_enabled is False
    local = JourneyConfig.from_paths(
        resolve_paths({"VOICE_JOURNEY_STATE_DIR": str(tmp_path / "state")}),
        playback_approval="approved",
    )
    assert local.allow_empty_manifests is False and local.transcript_dir is None
    assert local.goals_state == tmp_path / "state" / "goals-state.json"
    assert local.playback_enabled is True
    described = local.describe()
    assert described["playback"]["enabled"] is True
    assert described["watch"]["statePath"].endswith("watchd-state.json")


def test_store_reloads_on_change_and_keeps_serving_on_a_bad_manifest(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_manifests(data, index=fake_index(), results=fake_results())
    messages: list[str] = []
    store = JourneyStore(journey_config(data), log=messages.append)
    first = store.current()
    assert store.current() is first
    (data / "manifests" / "recording-index.json").write_text("{torn", encoding="utf-8")
    assert store.current() is first
    assert messages[-1].startswith("journey reload failed: JSONDecodeError")
    grown = fake_index()
    grown["recordings"] = grown["recordings"][:3]
    write_manifests(data, index=grown)
    assert len(store.current().rows_payload["rows"]) == 3
    assert messages[-1] == "journey serve state reloaded"


def test_store_without_a_data_root_needs_its_manifests(tmp_path: Path) -> None:
    config = journey_config(tmp_path / "data", allow_empty_manifests=False)
    with pytest.raises(FileNotFoundError):
        JourneyStore(config)
    empty = JourneyStore(journey_config(tmp_path / "data"))
    assert empty.current().rows_payload["rows"] == []


def test_store_writes_go_to_the_state_directory_only(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_manifests(data, index=fake_index(), results=fake_results(), journeys={"journeys": []})
    before = sorted((data / "manifests").iterdir())
    config = journey_config(data, state_dir=tmp_path / "state")
    store = JourneyStore(config)
    verdict = browser.make_verdict(
        "b", "non_singing", note=None, reviewed_by=None, reviewed_at=None
    )
    assert verdict["reviewedBy"] == "operator"
    totals = store.record_verdict(verdict)
    assert totals["reviewed"] == 2
    unknown = browser.make_verdict(
        "nope", "non_singing", note="n", reviewed_by="r", reviewed_at="t"
    )
    assert store.record_verdict(unknown)["reviewed"] == 2
    assert store.current().song_journeys == {"available": True, "journeys": []}
    goal = store.create_goal({"metric": "cpps", "direction": "at_least", "target": 10})
    assert store.abandon_goal(goal["goalId"])["status"] == "abandoned"
    assert store.abandon_goal("missing") is None
    assert sorted((data / "manifests").iterdir()) == before
    assert sorted(path.name for path in (tmp_path / "state").iterdir()) == [
        "corpus-browser-state.json",
        "goals-state.json",
    ]


def test_command_line_refusals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert browser.main(["help"]) == 0
    assert "merge-verdicts" in capsys.readouterr().out
    assert browser.main(["--help"]) == 0
    capsys.readouterr()
    assert browser.main([]) == 1
    assert "unsupported command: serve" in capsys.readouterr().err
    assert browser.main(["merge-verdicts", "--bogus"]) == 1
    assert "unsupported argument: --bogus" in capsys.readouterr().err
    assert browser.main(["merge-verdicts", "--results"]) == 1
    assert "--results requires a value" in capsys.readouterr().err
    assert browser.main(["merge-verdicts", "--results", str(tmp_path / "none.json")]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_merge_verdicts_defaults_to_rewriting_the_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(fake_results()), encoding="utf-8")
    assert browser.main(["merge-verdicts", "--results", str(results_path), "--out", "-"]) == 0
    out = capsys.readouterr().out
    assert '"humanSpotCheckMerge"' in out and out.rstrip().endswith('"out": "-"\n}')
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(fake_state()), encoding="utf-8")
    code = browser.main(
        ["merge-verdicts", "--results", str(results_path), "--state", str(state_path)]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["out"] == str(results_path)
    assert read_json(results_path)["humanSpotCheckMerge"]["mergedCount"] == 1


def test_pair_ids_are_the_js_digest() -> None:
    pool = build_referee_pairs(referee_lyrics(), fake_features(), seed=7, pairs_per_cluster=4)
    pair = pool[0]
    identity = "+".join(sorted((pair["aId"], pair["bId"])))
    expected = hashlib.sha256(f"{pair['clusterId']}:{identity}".encode()).hexdigest()[:16]
    assert pair["pairId"] == expected


def test_stratified_pick_skips_repeated_ids_and_backfills() -> None:
    def rows(*ids: str) -> list[dict[str, Any]]:
        return [{"recordingId": key, "year": "2020", "confidence": 0.5} for key in ids]

    picked = browser.stratified_pick(rows("a", "a", "a", "b", "c"), 3)
    assert [row["recordingId"] for row in picked] == ["a", "c", "b"]
    assert [row["recordingId"] for row in browser.stratified_pick(rows("a", "a", "a"), 2)] == ["a"]
    uncertain = [{**row, "bucket": "uncertain_manual_review"} for row in rows("a", "b", "c", "d")]
    assert {reason for _, reason in select_review_queue(uncertain, 3)} == {
        "uncertain_year_confidence_stratified"
    }


def test_voice_trends_without_two_yearly_tops_and_repeated_era_samples() -> None:
    features = {
        "results": [
            {
                "recordingId": "a",
                "capturedAt": "2019-01-01T00:00:00Z",
                "status": "completed",
                "features": {"pitch": {"f0Hz": {}}},
            }
            for _ in range(10)
        ]
    }
    trends = build_voice_trends(features, [])
    assert trends["yearlyTops"] == [] and trends["headline"]["workingTop"] is None
    cadence = [{"month": "2021-01", "count": 100}]
    reliable = [{"recordingId": "same", "capturedAt": "2021-01-05"} for _ in range(2)]
    assert browser.build_eras(cadence, reliable)[0]["sampleRecordingIds"] == ["same"]

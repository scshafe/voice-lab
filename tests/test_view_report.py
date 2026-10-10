"""evidence-report.test.mjs, translated (design §5.3), plus the cases that hold the rest of
``view/report.py``'s branches (the command line's refusals, the default paths, the
escaping, the verdict sentences and the charts' edge cases)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.view import report
from cole_voice_lab.view.report import Column, Point

Json = dict[str, Any]


def feature_row(recording_id: str, captured_at: str, p95: float) -> Json:
    return {
        "recordingId": recording_id,
        "capturedAt": captured_at,
        "year": captured_at[:4],
        "status": "completed",
        "features": {
            "durationSeconds": 60,
            "voicing": {
                "voicedShare": 0.7,
                "frameCount": 6000,
                "rejectedOutlierShare": 0.01,
                "rejectedGlobalOutlierShare": 0,
            },
            "pitch": {
                "f0Hz": {"p05": 100, "p25": 140, "p50": 190, "p75": 240, "p95": p95},
                "rangeSemitonesP05P95": 11,
                "madSemitones": 1,
            },
            "tuning": {"offsetCents": 0, "medianAbsCentError": 21, "within25CentsShare": 0.55},
            "vibrato": {
                "sustainedSegmentCount": 3,
                "vibratoSegmentCount": 1,
                "meanRateHz": 4.1,
                "meanExtentCents": 45,
                "vibratoTimeShare": 0.3,
            },
            "quality": {"jitterLocal": 0.01, "shimmerLocal": 0.07, "meanHnrDb": 15, "cpps": 11},
            "spectral": {
                "centroidHzMean": 1400,
                "centroidHzSd": 300,
                "rolloffHzMean": 2800,
                "singerFormantRatio": 0.04,
            },
            "dynamics": {
                "voicedRmsDbP10": -32,
                "voicedRmsDbP50": -25,
                "voicedRmsDbP90": -19,
                "dynamicSpreadDb": 13,
            },
            "phrasing": {
                "longestSustainedSeconds": 2.8,
                "meanVoicedSegmentSeconds": 1.7,
                "pauseRatePerMinute": 9,
            },
        },
    }


def fixture_files() -> dict[str, Json]:
    results = {
        "schemaVersion": "voice-journey.full-corpus-filter-results.v1",
        "results": [
            {
                "recordingId": "a",
                "capturedAt": "2019-02-01T00:00:00Z",
                "classification": {"finalBucket": "clean_singing"},
            },
            {
                "recordingId": "b",
                "capturedAt": "2021-02-01T00:00:00Z",
                "classification": {"finalBucket": "clean_singing"},
            },
            {
                "recordingId": "c",
                "capturedAt": "2021-03-01T00:00:00Z",
                "classification": {"finalBucket": "non_singing"},
            },
        ],
    }
    features: Json = {
        "schemaVersion": "voice-journey.local-voice-features.v1",
        "generatedAt": "2026-07-31T00:00:00.000Z",
        "results": [],
    }
    for i in range(12):
        features["results"].append(feature_row(f"a{i}", f"2019-0{(i % 6) + 1}-10T00:00:00Z", 260))
        features["results"].append(feature_row(f"b{i}", f"2021-0{(i % 6) + 1}-10T00:00:00Z", 390))
    journeys = {
        "schemaVersion": "voice-journey.song-journeys.v1",
        "totals": {"reliableTakes": 24, "takesWithNoteCore": 24, "clustersEligible": 1},
        "method": {
            "slopes": {"id": "theil_sen_per_cluster.v1"},
            "bootstrap": {"resamples": 1000, "seed": 2026},
        },
        "noteCore": {
            "takes": [
                {
                    "id": "a0",
                    "year": "2019",
                    "quarter": "2019-Q1",
                    "centErrorMedian": 20.1,
                    "inTuneShare": 0.6,
                },
                {
                    "id": "b0",
                    "year": "2021",
                    "quarter": "2021-Q1",
                    "centErrorMedian": 17.4,
                    "inTuneShare": 0.66,
                },
            ]
        },
        "journeys": [
            {
                "clusterId": "lyric-cluster-001",
                "label": "recurring_lyric_cluster_001",
                "takesUsed": 12,
                "firstYear": "2019",
                "lastYear": "2021",
                "perYear": {
                    "2019": {"n": 6, "noteCoreCentError": 20.1},
                    "2021": {"n": 6, "noteCoreCentError": 17.4},
                },
                "slopesPerYear": {"noteCoreCentError": -1.3, "cpps": 0.2},
            }
        ],
        "improvementIndex": [
            {
                "key": "noteCoreCentError",
                "label": "note-core tuning error (cents)",
                "direction": "down_good",
                "medianSlopePerYear": -1.3,
                "ci95": [-2.1, -0.5],
                "clustersUsed": 1,
                "verdict": "improving",
            },
            {
                "key": "cpps",
                "label": "voice clarity (CPPS dB)",
                "direction": "up_good",
                "medianSlopePerYear": 0.05,
                "ci95": [-0.2, 0.3],
                "clustersUsed": 1,
                "verdict": "flat",
            },
        ],
    }
    referee_state = {
        "schemaVersion": "voice-journey.referee-state.v1",
        "updatedAt": "2026-07-31T00:00:00.000Z",
        "judgments": [
            {
                "pairId": f"p{index}",
                "aId": "x",
                "bId": "y",
                "clusterId": "lyric-cluster-001",
                "aYear": "2021",
                "bYear": "2019",
                "choice": "too_close" if index % 4 == 3 else "a",
                "judgedAt": "2026-07-31T00:00:00.000Z",
            }
            for index in range(32)
        ],
    }
    return {
        "results": results,
        "features": features,
        "journeys": journeys,
        "refereeState": referee_state,
    }


def write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def capture_run(capsys: pytest.CaptureFixture[str], args: list[str]) -> tuple[int, str]:
    code = report.main(args)
    return code, capsys.readouterr().out


# ---- evidence-report.test.mjs ------------------------------------------------------------


def test_executive_verdict_names_improving_dimensions_and_the_ear_verdict() -> None:
    sentences = report.executive_verdict(
        trends={
            "headline": {
                "workingTop": {
                    "baselineYear": "2019",
                    "baselineTopHz": 265,
                    "peakYear": "2021",
                    "peakTopHz": 392,
                    "gainSemitones": 6.7,
                }
            }
        },
        journeys={
            "totals": {"clustersEligible": 3},
            "improvementIndex": [
                {"verdict": "improving", "label": "note-core tuning error (cents)"}
            ],
        },
        bt={
            "judgedTotal": 40,
            "years": [{"year": "2019", "strengthLog2": 0}, {"year": "2021", "strengthLog2": 0.6}],
        },
    )
    joined = " ".join(sentences)
    assert "C4 to G4" in joined
    assert "improvement in note-core tuning error" in joined
    assert "blind ear disagrees" in joined


def test_generate_writes_a_self_contained_report_and_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = fixture_files()
    paths = {name: write(tmp_path / f"{name}.json", payload) for name, payload in files.items()}

    def args_for(out: Path) -> list[str]:
        return [
            "generate",
            "--results",
            str(paths["results"]),
            "--features",
            str(paths["features"]),
            "--lyrics",
            str(tmp_path / "missing-lyrics.json"),
            "--journeys",
            str(paths["journeys"]),
            "--referee-state",
            str(paths["refereeState"]),
            "--out",
            str(out),
            "--generated-at",
            "2026-07-31T12:00:00.000Z",
        ]

    code, stdout = capture_run(capsys, args_for(tmp_path / "report-a.html"))
    assert code == 0
    assert stdout == f"{tmp_path / 'report-a.html'}\n"
    document = (tmp_path / "report-a.html").read_text(encoding="utf-8")
    assert "Evidence Report" in document
    assert "Did I get better" in document
    assert "same-song controlled" in document
    assert "note-core tuning error (cents)" in document
    assert "chip improving" in document
    assert "32 blind judgments" in document
    assert "Perceived quality by year" in document
    assert "<script>" not in document
    assert "f0ContourVoiced50ms" not in document
    capture_run(capsys, args_for(tmp_path / "report-b.html"))
    assert (tmp_path / "report-b.html").read_text(encoding="utf-8") == document


def test_generate_without_journeys_or_judgments_degrades_to_honest_placeholders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = fixture_files()
    results = write(tmp_path / "results.json", files["results"])
    features = write(tmp_path / "features.json", files["features"])
    out = tmp_path / "report.html"
    code, _ = capture_run(
        capsys,
        [
            "generate",
            "--results",
            str(results),
            "--features",
            str(features),
            "--lyrics",
            str(tmp_path / "missing.json"),
            "--journeys",
            str(tmp_path / "missing2.json"),
            "--referee-state",
            str(tmp_path / "missing3.json"),
            "--out",
            str(out),
            "--generated-at",
            "2026-07-31T12:00:00.000Z",
        ],
    )
    assert code == 0
    document = out.read_text(encoding="utf-8")
    assert "No judgments yet" in document
    assert "npm run journeys" in document


def test_report_dry_run_reports_inputs_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = fixture_files()
    results = write(tmp_path / "results.json", files["results"])
    features = write(tmp_path / "features.json", files["features"])
    out = tmp_path / "report.html"
    code, stdout = capture_run(
        capsys,
        [
            "generate",
            "--dry-run",
            "--results",
            str(results),
            "--features",
            str(features),
            "--lyrics",
            str(tmp_path / "m1.json"),
            "--journeys",
            str(tmp_path / "m2.json"),
            "--referee-state",
            str(tmp_path / "m3.json"),
            "--out",
            str(out),
        ],
    )
    payload = json.loads(stdout)
    assert code == 0
    assert payload["operation"] == "evidence-report-generation"
    assert payload["inputs"]["refereeJudgments"] == 0
    assert payload["readScope"]["audioBytes"] is False
    assert not out.exists()


# ---- the rest of view/report.py ----------------------------------------------------------


def test_the_command_line_refuses_and_helps_as_the_js_did(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for args in (["help"], ["--help"], ["generate", "--help"]):
        code, stdout = capture_run(capsys, args)
        assert (code, stdout) == (0, report.HELP)
    for args, message in (
        (["bogus"], "unsupported command: bogus"),
        (["generate", "--out"], "--out requires a value"),
        (["generate", "--out", "--dry-run"], "--out requires a value"),
        (["generate", "--frob"], "unsupported argument: --frob"),
    ):
        assert report.main(args) == 1
        assert capsys.readouterr().err == f"error: {message}\n"
    missing = str(tmp_path / "nowhere.json")
    assert report.main(["generate", "--results", missing, "--dry-run"]) == 1
    assert "error:" in capsys.readouterr().err


def test_the_defaults_come_from_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOICE_JOURNEY_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("VOICE_JOURNEY_STATE_DIR", str(tmp_path / "state"))
    files = fixture_files()
    manifests = tmp_path / "data" / "manifests"
    manifests.mkdir(parents=True)
    write(manifests / "full-corpus-filter-results.json", files["results"])
    (tmp_path / "state").mkdir()
    write(tmp_path / "state" / "referee-state.json", files["refereeState"])
    options = report.parse_args([])
    assert options["command"] == "generate"
    assert options["referee_state"] == str(tmp_path / "state" / "referee-state.json")
    code, stdout = capture_run(capsys, ["--generated-at", "2026-07-31T12:00:00.000Z"])
    out = tmp_path / "data" / "artifacts" / "evidence-report.html"
    assert (code, stdout) == (0, f"{out}\n")
    document = out.read_text(encoding="utf-8")
    assert "0 feature takes · 0 song journeys · 32 listening judgments" in document
    assert "The Referee page serves the experiment" not in document
    code, stdout = capture_run(capsys, ["generate"])
    assert code == 0 and "Generated 20" in out.read_text(encoding="utf-8")


def _inputs(**changes: Any) -> report.ReportInputs:
    files = fixture_files()
    fields: dict[str, Any] = {
        "results_manifest": files["results"],
        "features_manifest": files["features"],
        "lyrics_manifest": None,
        "journeys_manifest": files["journeys"],
        "referee_state": files["refereeState"],
        **changes,
    }
    return report.ReportInputs(**fields)


def test_manifest_values_are_escaped() -> None:
    hostile = "<script>alert(1)</script>"
    files = fixture_files()
    journeys = files["journeys"]
    journeys["improvementIndex"][0]["label"] = hostile
    journeys["improvementIndex"][0]["verdict"] = '" onclick="x'
    journeys["journeys"][0]["clusterId"] = hostile
    journeys["method"]["bootstrap"]["seed"] = hostile
    inputs = _inputs(journeys_manifest=journeys)
    sections = report.report_sections(inputs.model(), journeys)
    assert "<script" not in sections
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in sections
    assert 'class="chip &quot; onclick=&quot;x"' in sections
    assert report.esc("a&b<c>\"d'") == "a&amp;b&lt;c&gt;&quot;d'"


def test_verdict_sentences_cover_every_branch() -> None:
    flat = {"improvementIndex": [{"verdict": "flat", "label": "x"}]}
    sentences = report.executive_verdict(trends=None, journeys=flat, bt=None)
    assert "the eligible recurring songs" in sentences[0]
    assert "still open" in sentences[1]
    declining = {
        "totals": {"clustersEligible": 2},
        "improvementIndex": [{"verdict": "declining", "label": "clarity"}, {"verdict": "x"}],
    }

    def bt(latest: float, total: int = 40) -> Json:
        return {
            "judgedTotal": total,
            "years": [{"strengthLog2": 0}, {"strengthLog2": latest}],
        }

    sentences = report.executive_verdict(trends={}, journeys=declining, bt=bt(-0.5))
    assert sentences[0] == "Same-song evidence shows decline in clarity."
    assert "earlier years currently win" in sentences[1]
    sentences = report.executive_verdict(trends={}, journeys=None, bt=bt(0.1))
    assert "your ear agrees with the metrics" in sentences[0]
    sentences = report.executive_verdict(trends={}, journeys=None, bt=bt(0.9, total=29))
    assert "still open" in sentences[0]
    top = {"baselineYear": "2019", "baselineTopHz": 392, "peakYear": "2020"}
    sentences = report.executive_verdict(
        trends={"headline": {"workingTop": {**top, "peakTopHz": 262, "gainSemitones": -7}}},
        journeys=None,
        bt=None,
    )
    assert "from G4 to C4 — -7 semitones" in sentences[0]


def test_the_model_counts_buckets_and_note_core_years() -> None:
    files = fixture_files()
    files["results"]["results"].append({"recordingId": "d"})
    journeys = files["journeys"]
    journeys["noteCore"]["takes"].append({"year": "2019", "centErrorMedian": None})
    model = _inputs(results_manifest=files["results"], journeys_manifest=journeys).model()
    assert model.buckets == {"clean_singing": 2, "non_singing": 1, "unclassified": 1}
    assert model.note_core_by_year == [
        {"year": "2019", "centError": 20.1, "n": 2},
        {"year": "2021", "centError": 17.4, "n": 1},
    ]
    empty = _inputs(features_manifest=None, journeys_manifest=None, referee_state=None).model()
    assert (empty.trends, empty.bt, empty.note_core_by_year) == (None, None, [])
    assert report.median_of([None, "x"]) is None
    assert report.round_to(None) is None


def test_scorecards_sort_by_takes_and_survive_empty_years() -> None:
    journeys = fixture_files()["journeys"]
    journeys["journeys"].append(
        {
            "clusterId": "lyric-cluster-002",
            "takesUsed": 30,
            "firstYear": "2020",
            "lastYear": "2020",
            "perYear": {},
            "slopesPerYear": {},
        }
    )
    sections = report.report_sections(_inputs(journeys_manifest=journeys).model(), journeys)
    assert sections.index("lyric-cluster-002") < sections.index("lyric-cluster-001")
    assert '<td class="num">— → —</td>' in sections
    assert "Chained-cluster flags" not in sections


def test_charts_keep_the_js_geometry() -> None:
    assert report.line_chart_svg(label="x", points=[Point("a", 1)]) == ""
    assert report.columns_svg(label="x", columns=[]) == ""
    assert report.range_bars_svg(label="x", years=[{"year": "2019", "topHz": 300}]) == ""
    chart = report.line_chart_svg(
        label="zero", points=[Point("a", -1), Point("b", None), Point("c", 2)], zero_line=True
    )
    assert '<text x="48" y="' in chart and ">0</text>" in chart
    assert chart.count("<circle") == 2
    columns = report.columns_svg(label="c", columns=[Column(0, "2019"), Column(4, None)])
    assert columns.count("<rect") == 1 and ">2019</text>" in columns
    bars = report.range_bars_svg(
        label="r", years=[{"year": "2019", "topHz": 262}, {"year": "2021", "topHz": 523.3}]
    )
    assert ">C4</text>" in bars and ">C5</text>" in bars and ">G4</text>" in bars
    assert report.note_name(0) == "" and report.note_name(-3) == ""
    assert report.note_name(440) == "A4" and report.note_name(27.5) == "A0"
    assert report.verdict_chip_html("trending_up") == (
        '<span class="chip trending_up">trending up</span>'
    )

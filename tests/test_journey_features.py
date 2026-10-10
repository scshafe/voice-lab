"""features.mjs's tests (features.test.mjs), translated case for case, then the CLI, the
worker protocol against fake analysis pythons and the ffmpeg seam."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.journey import features
from cole_voice_lab.journey.features import (
    DecodedRecording,
    FeatureHooks,
    FeaturesError,
    FeaturesOptions,
    ManifestExtras,
    build_feature_row,
    build_features_dry_run,
    build_features_manifest,
    extract_index_manifest,
    main,
    parse_args,
    read_tool_version,
    run,
    select_recordings,
)
from cole_voice_lab.resources import resource_path

METHOD = "voice-journey.feature-extract.v1"


@pytest.fixture
def local_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """withLocalArtifactDir: a directory inside ``./local-artifacts`` of the cwd."""

    monkeypatch.chdir(tmp_path)
    for name in ("VOICE_JOURNEY_DATA", "VOICE_JOURNEY_PYTHON", "FFMPEG_BIN"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "local-artifacts").mkdir()
    yield Path(tempfile.mkdtemp(prefix="features-test-", dir="local-artifacts"))


def recording(recording_id: str, filename: str, captured_at: str) -> dict[str, Any]:
    return {
        "recordingId": recording_id,
        "filename": filename,
        "capturedAt": captured_at,
        "sourceRef": {
            "seam": "voice-journey-corpus",
            "selector": recording_id,
            "filename": filename,
        },
    }


def fake_index() -> dict[str, Any]:
    return {
        "schemaVersion": "voice-journey.recording-index.v1",
        "generatedAt": "2026-07-16T00:00:00.000Z",
        "totals": {"recordings": 3},
        "recordings": [
            recording("a", "20210101 000000-A.m4a", "2021-01-01T00:00:00Z"),
            recording("b", "20220101 000000-B.m4a", "2022-01-01T00:00:00Z"),
            recording("c", "20230101 000000-C.m4a", "2023-01-01T00:00:00Z"),
        ],
    }


def fake_results() -> dict[str, Any]:
    return {
        "schemaVersion": "voice-journey.full-corpus-filter-results.v1",
        "generatedAt": "2026-07-16T00:00:00.000Z",
        "updatedAt": "2026-07-30T00:00:00.000Z",
        "results": [
            {"recordingId": "a", "classification": {"finalBucket": "clean_singing"}},
            {"recordingId": "b", "classification": {"finalBucket": "clean_singing"}},
            {"recordingId": "c", "classification": {"finalBucket": "non_singing"}},
        ],
    }


def fake_summary(recording_id: str) -> dict[str, Any]:
    return {
        "durationSeconds": 42,
        "voicing": {"voicedShare": 0.7, "frameCount": 4200},
        "pitch": {
            "f0Hz": {"p05": 180, "p25": 210, "p50": 240, "p75": 280, "p95": 330},
            "rangeSemitonesP05P95": 10.5,
            "madSemitones": 1.2,
        },
        "tuning": {"offsetCents": -12.5, "medianAbsCentError": 18.2, "within25CentsShare": 0.64},
        "vibrato": {
            "sustainedSegmentCount": 4,
            "vibratoSegmentCount": 2,
            "meanRateHz": 5.4,
            "meanExtentCents": 32.1,
            "vibratoTimeShare": 0.41,
        },
        "quality": {"jitterLocal": 0.012, "shimmerLocal": 0.08, "meanHnrDb": 14.2, "cpps": 11.8},
        "spectral": {
            "centroidHzMean": 1450.2,
            "centroidHzSd": 310.5,
            "rolloffHzMean": 2900.1,
            "singerFormantRatio": 0.041,
        },
        "dynamics": {
            "voicedRmsDbP10": -32.1,
            "voicedRmsDbP50": -24.6,
            "voicedRmsDbP90": -18.9,
            "dynamicSpreadDb": 13.2,
        },
        "phrasing": {
            "longestSustainedSeconds": 6.4,
            "meanVoicedSegmentSeconds": 1.8,
            "pauseRatePerMinute": 9.5,
        },
        "marker": recording_id,
    }


@dataclass
class FakeWorker:
    extract_log: list[str]
    fail_ids: set[str]
    versions: Any = field(
        default_factory=lambda: {
            "python": "3.12-fixture",
            "parselmouth": "0.4.5-fixture",
            "librosa": "0.11-fixture",
        }
    )
    config: Any = field(default_factory=lambda: {"methodId": METHOD, "fixture": True})
    closed: bool = False

    def extract(self, task: dict[str, Any]) -> Any:
        self.extract_log.append(task["recordingId"])
        if task["recordingId"] in self.fail_ids:
            return {
                "recordingId": task["recordingId"],
                "status": "failed",
                "error": "FixtureError: synthetic failure",
            }
        return {
            "recordingId": task["recordingId"],
            "status": "completed",
            "summary": fake_summary(task["recordingId"]),
            "detail": {
                "methodId": METHOD,
                "f0ContourVoiced50ms": [[0, 240.1]],
                "vibratoSegments": [],
                "config": {},
            },
        }

    def close(self) -> None:
        self.closed = True


def fake_worker_hooks(extract_log: list[str], fail_ids: set[str] | None = None) -> FeatureHooks:
    def decode(entry: dict[str, Any], options: FeaturesOptions) -> DecodedRecording:
        return DecodedRecording(
            wav_path=f"/fixture/{entry['recordingId']}.wav", cleanup=lambda: None
        )

    return FeatureHooks(
        tool_versions={"ffmpeg": "ffmpeg fixture"},
        create_worker=lambda options: FakeWorker(extract_log, fail_ids or set()),
        decode_recording=decode,
    )


def base_options(root: Path, **overrides: Any) -> FeaturesOptions:
    values: dict[str, Any] = {
        "approval": "release-gate-test",
        "buckets": "clean_singing",
        "corpus_root": "/tmp/corpus",
        "feature_dir": str(root / "features"),
        "ffmpeg_bin": "/opt/homebrew/bin/ffmpeg",
        "generated_at": "2026-07-30T00:00:00.000Z",
        "index": "unused",
        "limit": None,
        "out": str(root / "voice-features.json"),
        "results": "unused",
        "sample_rate": 22050,
        "venv_python": "analysis/.venv/bin/python",
        "worker": "analysis/extract_features.py",
    }
    values.update(overrides)
    return FeaturesOptions(**values)


def test_select_recordings_filters_by_bucket_and_honors_limit() -> None:
    selected = select_recordings(
        fake_index(), fake_results(), FeaturesOptions(buckets="clean_singing", limit=None)
    )
    assert [item.entry["recordingId"] for item in selected] == ["a", "b"]
    limited = select_recordings(
        fake_index(), fake_results(), FeaturesOptions(buckets="clean_singing,non_singing", limit=2)
    )
    assert [item.entry["recordingId"] for item in limited] == ["a", "b"]


def test_features_dry_run_reports_selection_and_scope_without_reading_audio(
    local_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = local_root / "index.json"
    results_path = local_root / "results.json"
    index_path.write_text(json.dumps(fake_index()), encoding="utf-8")
    results_path.write_text(json.dumps(fake_results()), encoding="utf-8")
    code = run(
        ["extract-index", "--index", str(index_path), "--results", str(results_path), "--dry-run"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["operation"] == "voice-feature-extraction"
    assert payload["selection"]["selectedCount"] == 2
    assert payload["selection"]["byBucket"] == {"clean_singing": 2}
    assert payload["readScope"]["audioBytes"] is True
    assert payload["readScope"]["featureArraysCommittedToRepo"] is False
    assert payload["resumeBehavior"]["failedRowsRetriedOnRerun"] is True


def test_build_features_dry_run_widens_selection_with_buckets_flag() -> None:
    payload = build_features_dry_run(
        fake_index(),
        fake_results(),
        FeaturesOptions(
            buckets="clean_singing,non_singing",
            limit=None,
            out="out.json",
            feature_dir="local-artifacts/voice-features",
            ffmpeg_bin="ffmpeg",
            venv_python="py",
            worker="worker.py",
            sample_rate=22050,
        ),
    )
    assert payload["selection"]["selectedCount"] == 3
    assert payload["selection"]["byBucket"]["non_singing"] == 1


def test_extract_index_writes_local_detail_repo_safe_rows_and_continues_past_failures(
    local_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extract_log: list[str] = []
    hooks = fake_worker_hooks(extract_log, {"b"})
    options = base_options(local_root)
    manifest = extract_index_manifest(fake_index(), fake_results(), options, hooks)

    assert extract_log == ["a", "b"]
    assert len(manifest["results"]) == 2
    row_a = next(row for row in manifest["results"] if row["recordingId"] == "a")
    row_b = next(row for row in manifest["results"] if row["recordingId"] == "b")
    assert row_a["status"] == "completed"
    assert row_a["features"]["pitch"]["rangeSemitonesP05P95"] == 10.5
    assert row_a["localArtifacts"]["arraysIncludedInManifest"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", row_a["featuresDigest"])
    assert row_b["status"] == "failed"
    assert re.search(r"FixtureError", row_b["error"])
    assert row_b["featuresDigest"] is None
    assert manifest["resume"]["completedCount"] == 1
    assert manifest["resume"]["failedCount"] == 1

    detail = json.loads((Path(options.feature_dir) / "a.json").read_text(encoding="utf-8"))
    assert len(detail["detail"]["f0ContourVoiced50ms"]) == 1

    written = Path(options.out).read_text(encoding="utf-8")
    assert "f0ContourVoiced50ms" not in written
    assert "vibratoSegments" not in written
    assert json.loads(written)["analysisConfig"]["configFingerprint"] == row_a["configFingerprint"]


def test_extract_index_resumes_skipping_completed_and_retrying_failed_rows(
    local_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first_log: list[str] = []
    options = base_options(local_root)
    extract_index_manifest(
        fake_index(), fake_results(), options, fake_worker_hooks(first_log, {"b"})
    )
    assert first_log == ["a", "b"]

    second_log: list[str] = []
    manifest = extract_index_manifest(
        fake_index(), fake_results(), options, fake_worker_hooks(second_log)
    )
    assert second_log == ["b"]
    assert manifest["resume"]["skippedCompletedCount"] == 1
    assert manifest["resume"]["completedCount"] == 2
    assert manifest["resume"]["failedCount"] == 0
    assert manifest["resume"]["remainingCount"] == 0


def test_extract_index_runs_are_byte_deterministic_with_a_pinned_generated_at(
    local_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    options_a = base_options(local_root, out=str(local_root / "out-a.json"))
    options_b = base_options(local_root, out=str(local_root / "out-b.json"))
    extract_index_manifest(fake_index(), fake_results(), options_a, fake_worker_hooks([]))
    extract_index_manifest(fake_index(), fake_results(), options_b, fake_worker_hooks([]))
    assert Path(options_a.out).read_bytes() == Path(options_b.out).read_bytes()


def test_extract_index_requires_approval_and_corpus_root_for_non_dry_run(
    local_root: Path,
) -> None:
    with pytest.raises(FeaturesError, match="requires --approval"):
        extract_index_manifest(
            fake_index(),
            fake_results(),
            base_options(local_root, approval=None),
            fake_worker_hooks([]),
        )
    with pytest.raises(FeaturesError, match="requires --corpus-root"):
        extract_index_manifest(
            fake_index(),
            fake_results(),
            base_options(local_root, corpus_root=None),
            fake_worker_hooks([]),
        )


# --- beyond the JS cases: the manifest's exact shape -------------------------------


def test_the_manifest_and_rows_have_the_js_shape_and_numbers(
    local_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    options = base_options(local_root)
    extract_index_manifest(fake_index(), fake_results(), options, fake_worker_hooks([], {"b"}))
    text = Path(options.out).read_text(encoding="utf-8")
    manifest = json.loads(text)
    assert list(manifest) == [
        "schemaVersion",
        "generatedAt",
        "updatedAt",
        "sourceIndex",
        "sourceResults",
        "releaseGateApproval",
        "readScope",
        "localFeatureStore",
        "repoSafeManifest",
        "analysisConfig",
        "resume",
        "toolChoices",
        "results",
    ]
    assert manifest["sourceIndex"] == {
        "schemaVersion": "voice-journey.recording-index.v1",
        "generatedAt": "2026-07-16T00:00:00.000Z",
        "updatedAt": None,
        "recordingCount": 3,
    }
    assert manifest["sourceResults"]["selectedBuckets"] == ["clean_singing"]
    assert manifest["localFeatureStore"]["gitignoredExpected"] is True
    config = manifest["analysisConfig"]
    assert config["sampleRateHz"] == 22050
    without_fingerprint = {
        key: value for key, value in config.items() if key != "configFingerprint"
    }
    compact = json.dumps(without_fingerprint, separators=(",", ":"), ensure_ascii=False)
    assert config["configFingerprint"] == hashlib.sha256(compact.encode()).hexdigest()
    assert manifest["toolChoices"][0]["version"] == "ffmpeg fixture"
    assert manifest["toolChoices"][1]["version"] == json.dumps(
        config["versions"], separators=(",", ":")
    )
    row_a, row_b = manifest["results"]
    assert list(row_a) == [
        "recordingId",
        "filename",
        "capturedAt",
        "year",
        "sourceRef",
        "bucket",
        "status",
        "configFingerprint",
        "toolRun",
        "features",
        "featuresDigest",
        "localArtifacts",
    ]
    assert row_a["year"] == "2021"
    assert row_a["toolRun"]["audioPrep"] == (
        "ffmpeg decode to 22050 Hz mono wav in a temporary directory"
    )
    detail_path = Path(options.feature_dir) / "a.json"
    assert row_a["localArtifacts"]["featureJsonPath"] == os.path.relpath(detail_path)
    assert row_a["featuresDigest"] == hashlib.sha256(detail_path.read_bytes()).hexdigest()
    assert '"durationSeconds": 42,' in detail_path.read_text(encoding="utf-8")
    assert list(row_b)[-4:] == ["error", "features", "featuresDigest", "localArtifacts"]
    assert manifest["resume"]["lastProcessedRecordingId"] == "b"
    assert manifest["resume"]["remainingCount"] == 1
    assert capsys.readouterr().out == f"{options.out}\n" * 3


def test_feature_store_outside_the_cwd_keeps_absolute_paths(
    tmp_path: Path, local_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    options = base_options(local_root, feature_dir=str(outside / "features"))
    manifest = extract_index_manifest(fake_index(), fake_results(), options, fake_worker_hooks([]))
    artifacts = manifest["results"][0]["localArtifacts"]
    assert artifacts["featureJsonPath"] == str(outside / "features" / "a.json")
    assert artifacts["gitignoredExpected"] is False
    assert manifest["localFeatureStore"]["gitignoredExpected"] is False


def test_build_features_manifest_defaults_and_a_clock_timestamp() -> None:
    manifest = build_features_manifest(
        {}, {"results": "ab"}, [], FeaturesOptions(buckets=" x , ,x")
    )
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", manifest["generatedAt"])
    assert manifest["sourceIndex"]["recordingCount"] is None
    assert manifest["sourceResults"]["recordingCount"] == 2
    assert manifest["sourceResults"]["selectedBuckets"] == ["x"]
    assert manifest["resume"]["lastProcessedRecordingId"] is None
    assert manifest["resume"]["remainingCount"] == 0
    assert manifest["toolChoices"][0]["version"] is None
    assert manifest["toolChoices"][1]["version"] is None
    falsy = build_features_manifest(
        {"recordings": [1, 2]},
        {},
        [{"status": "completed"}],
        FeaturesOptions(generated_at=""),
        ManifestExtras(versions=0, tool_versions={}),
    )
    assert falsy["generatedAt"] == ""
    assert falsy["sourceIndex"]["recordingCount"] == 2
    assert falsy["toolChoices"][1]["version"] is None
    assert falsy["resume"]["lastProcessedRecordingId"] is None


def test_build_feature_row_failure_omits_what_the_worker_did_not_send() -> None:
    entry: dict[str, Any] = {"recordingId": "x", "capturedAt": ""}
    row = build_feature_row(entry, "clean_singing", {}, FeaturesOptions(sample_rate=10**30 * 1.0))
    assert list(row) == [
        "recordingId",
        "capturedAt",
        "year",
        "bucket",
        "configFingerprint",
        "toolRun",
        "error",
        "features",
        "featuresDigest",
        "localArtifacts",
    ]
    assert row["year"] == "unknown"
    assert row["error"] == "unknown extraction failure"
    assert row["toolRun"]["audioPrep"].startswith("ffmpeg decode to 1e+30 Hz")
    no_summary = build_feature_row(entry, "b", {"status": "completed"}, FeaturesOptions())
    assert "features" not in no_summary
    assert no_summary["featuresDigest"] is None


def test_select_recordings_without_results_or_with_odd_buckets() -> None:
    index = {"recordings": [{"recordingId": "a"}, {"recordingId": "b"}]}
    unclassified = select_recordings(index, {}, FeaturesOptions(buckets="unclassified"))
    assert [item.bucket for item in unclassified] == ["unclassified", "unclassified"]
    results = {
        "results": [
            {"recordingId": "a", "classification": {"finalBucket": ["list"]}},
            {"recordingId": "b", "classification": "flat"},
        ]
    }
    odd = select_recordings(index, results, FeaturesOptions(buckets="unclassified"))
    assert [item.entry["recordingId"] for item in odd] == ["b"]
    assert select_recordings({}, results, FeaturesOptions()) == []


def test_extract_index_refuses_stdout_output_and_a_corrupt_existing_manifest(
    local_root: Path,
) -> None:
    with pytest.raises(FeaturesError, match="requires a file output"):
        extract_index_manifest(
            fake_index(), fake_results(), base_options(local_root, out="-"), fake_worker_hooks([])
        )
    corrupt = local_root / "corrupt.json"
    corrupt.write_text('{"results": NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="NaN"):
        extract_index_manifest(
            fake_index(),
            fake_results(),
            base_options(local_root, out=str(corrupt)),
            fake_worker_hooks([]),
        )


def test_the_worker_is_closed_when_a_decode_fails(local_root: Path) -> None:
    workers: list[FakeWorker] = []

    def create(options: FeaturesOptions) -> FakeWorker:
        workers.append(FakeWorker([], set()))
        return workers[-1]

    def decode(entry: dict[str, Any], options: FeaturesOptions) -> DecodedRecording:
        raise FeaturesError("ffmpeg failed for x: boom")

    hooks = FeatureHooks(tool_versions={}, create_worker=create, decode_recording=decode)
    with pytest.raises(FeaturesError, match="boom"):
        extract_index_manifest(fake_index(), fake_results(), base_options(local_root), hooks)
    assert workers[0].closed


# --- the command line ----------------------------------------------------------------


def test_parse_args_reads_every_flag_and_defaults_follow_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_JOURNEY_DATA", "/data")
    monkeypatch.setenv("VOICE_JOURNEY_PYTHON", "/runtime/bin/python")
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    defaults = parse_args([])
    assert defaults.command == "extract-index"
    assert defaults.index == "/data/manifests/recording-index.json"
    assert defaults.results == "/data/manifests/full-corpus-filter-results.json"
    assert defaults.out == "/data/manifests/local-voice-features.json"
    assert defaults.feature_dir == "/data/artifacts/voice-features"
    assert defaults.venv_python == "/runtime/bin/python"
    assert defaults.ffmpeg_bin == "ffmpeg"
    assert defaults.worker == str(resource_path("journey_extract_features.py"))
    assert Path(defaults.worker).is_file()
    assert (defaults.sample_rate, defaults.buckets, defaults.limit) == (
        22050,
        "clean_singing",
        None,
    )
    options = parse_args(
        [
            "extract-index",
            "--approval",
            "ok",
            "--buckets",
            "a,b",
            "--corpus-root",
            "/c",
            "--dry-run",
            "--feature-dir",
            "/f",
            "--ffmpeg-bin",
            "/ff",
            "--generated-at",
            "2026-01-01T00:00:00.000Z",
            "--index",
            "/i",
            "--limit",
            " 0x10 ",
            "--out",
            "/o",
            "--results",
            "/r",
            "--sample-rate",
            "1e3",
            "--venv-python",
            "/p",
            "--worker",
            "/w",
        ]
    )
    assert options == FeaturesOptions(
        approval="ok",
        buckets="a,b",
        command="extract-index",
        corpus_root="/c",
        dry_run=True,
        feature_dir="/f",
        ffmpeg_bin="/ff",
        generated_at="2026-01-01T00:00:00.000Z",
        index="/i",
        limit=16,
        out="/o",
        results="/r",
        sample_rate=1000,
        venv_python="/p",
        worker="/w",
    )
    assert parse_args(["help"]).help
    assert parse_args(["other", "--help"]).command == "other"
    assert parse_args(["--limit", "9007199254740993"]).limit == 2**53
    assert isinstance(parse_args(["--limit", "1e30"]).limit, float)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--index"], "--index requires a value"),
        (["--index", ""], "--index requires a value"),
        (["--out", "--dry-run"], "--out requires a value"),
        (["--limit", "0"], "--limit must be a positive integer"),
        (["--limit", "-3"], "--limit must be a positive integer"),
        (["--limit", "2.5"], "--limit must be a positive integer"),
        (["--limit", "Infinity"], "--limit must be a positive integer"),
        (["--limit", "1_000"], "--limit must be a positive integer"),
        (["--limit", "0xZZ"], "--limit must be a positive integer"),
        (["--sample-rate", "abc"], "--sample-rate must be a positive integer"),
        (["--bogus"], "unsupported argument: --bogus"),
        ([""], "unsupported argument: "),
        (["--help", "--bogus"], "unsupported argument: --bogus"),
    ],
)
def test_parse_args_refusals(argv: list[str], message: str) -> None:
    with pytest.raises(FeaturesError) as caught:
        parse_args(argv)
    assert str(caught.value) == message


def test_js_number_follows_number_of_a_string() -> None:
    cases = {
        "": 0.0,
        " \t7\n": 7.0,
        "+5": 5.0,
        ".5e1": 5.0,
        "12.": 12.0,
        "0b11": 3.0,
        "0O17": 15.0,
        "0x1f": 31.0,
        "Infinity": math.inf,
        "+Infinity": math.inf,
        "-Infinity": -math.inf,
    }
    for text, expected in cases.items():
        assert features._js_number(text) == expected
    for text in ["inf", "nan", "1_0", "-0x10", "0b2", "abc", "1e"]:
        assert math.isnan(features._js_number(text))


def test_help_and_errors_through_main(local_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert capsys.readouterr().out == features.HELP
    assert main(["other", "--help"]) == 0
    capsys.readouterr()
    assert main(["other"]) == 1
    assert capsys.readouterr().err == "error: unsupported command: other\n"
    assert main(["--bogus"]) == 1
    assert capsys.readouterr().err == "error: unsupported argument: --bogus\n"
    assert main([]) == 1
    assert "manifests/recording-index.json" in capsys.readouterr().err
    (local_root / "index.json").write_text("\ufeff{}", encoding="utf-8")
    assert main(["--index", str(local_root / "index.json")]) == 1
    assert "BOM" in capsys.readouterr().err


def test_run_without_argv_reads_sys_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["features", "--help"])
    assert run() == 0
    assert capsys.readouterr().out.startswith("Voice Journey per-recording feature extraction")


def test_the_module_runs_as_a_command() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "cole_voice_lab.journey.features", "--bogus"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stderr == "error: unsupported argument: --bogus\n"


def test_stringify_compact_is_json_stringify() -> None:
    value = {"a": [1, 2.0, -0.0, 1e21, None, True, False], "é": {"s": "x\u2028"}, "n": 0.1}
    assert features._stringify_compact(value) == (
        '{"a":[1,2,0,1e+21,null,true,false],"é":{"s":"x\u2028"},"n":0.1}'
    )
    with pytest.raises(TypeError, match="not JSON-serializable: object"):
        features._stringify_compact(object())


def test_js_truthiness_and_length() -> None:
    assert not features._truthy(math.nan)
    assert not features._truthy("")
    assert features._truthy({})
    assert features._truthy([])
    assert features._js_length("a\U0001f600") == 3
    assert features._js_length({}) is None


# --- the worker process, against fake analysis pythons ------------------------------


def write_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


FAKE_FFMPEG = """
import json, sys
from pathlib import Path
args = sys.argv[1:]
if args == ["-version"]:
    print("")
    print("  ffmpeg version fixture  ")
    print("built with fixtures")
    raise SystemExit(0)
log = Path(__file__).with_suffix(".log")
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if "missing" in args[args.index("-i") + 1]:
    sys.stderr.write("No such file\\n")
    raise SystemExit(1)
Path(args[-1]).write_bytes(b"RIFF")
"""

FAKE_ANALYSIS_PYTHON = """
import json, os, sys
worker = sys.argv[1]
print()
print(json.dumps({"note": "before hello"}))
print(json.dumps({"hello": {"versions": {"python": "fake", "worker": os.path.basename(worker)},
                            "methodId": "voice-journey.feature-extract.v1",
                            "config": {"sampleRate": 22050, "f0": {"floorHz": 75.0}}}}))
sys.stdout.flush()
for line in sys.stdin:
    task = json.loads(line)
    exists = os.path.exists(task["wavPath"])
    print(json.dumps({"hello": {"again": True}}))
    print("   ")
    if task["recordingId"] == "b":
        result = {"recordingId": "b", "status": "failed", "error": "ValueError: synthetic"}
    else:
        result = {"recordingId": task["recordingId"], "status": "completed",
                  "summary": {"durationSeconds": 2.0, "wavExisted": exists},
                  "detail": {"f0ContourVoiced50ms": [[0.0, 220.0]]}}
    print(json.dumps(result))
    sys.stdout.flush()
"""


def test_a_full_command_line_run_with_fake_ffmpeg_and_analysis_python(
    local_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ffmpeg = write_executable(local_root / "ffmpeg", FAKE_FFMPEG)
    python = write_executable(local_root / "python", FAKE_ANALYSIS_PYTHON)
    index_path = local_root / "index.json"
    results_path = local_root / "results.json"
    index_path.write_text(json.dumps(fake_index()), encoding="utf-8")
    results_path.write_text(json.dumps(fake_results()), encoding="utf-8")
    out = local_root / "features.json"
    argv = [
        "extract-index",
        "--index",
        str(index_path),
        "--results",
        str(results_path),
        "--corpus-root",
        "/corpus/",
        "--approval",
        "gate",
        "--out",
        str(out),
        "--feature-dir",
        str(local_root / "store"),
        "--ffmpeg-bin",
        str(ffmpeg),
        "--venv-python",
        str(python),
        "--generated-at",
        "2026-08-01T00:00:00.000Z",
        "--sample-rate",
        "16000",
    ]
    assert main(argv) == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["toolChoices"][0]["version"] == "ffmpeg version fixture"
    assert manifest["toolChoices"][1]["workerScript"] == str(
        resource_path("journey_extract_features.py")
    )
    assert manifest["analysisConfig"]["versions"] == {
        "python": "fake",
        "worker": "journey_extract_features.py",
    }
    assert '"floorHz": 75\n' in out.read_text(encoding="utf-8")
    row_a, row_b = manifest["results"]
    assert row_a["features"] == {"durationSeconds": 2, "wavExisted": True}
    assert row_b["status"] == "failed"
    assert row_b["error"] == "ValueError: synthetic"
    calls = [
        json.loads(line)
        for line in (local_root / "ffmpeg.log").read_text(encoding="utf-8").splitlines()
    ]
    assert [call[call.index("-i") + 1] for call in calls] == [
        "/corpus/20210101 000000-A.m4a",
        "/corpus/20220101 000000-B.m4a",
    ]
    assert calls[0][:6] == ["-hide_banner", "-nostdin", "-v", "error", "-y", "-i"]
    assert calls[0][7:11] == ["-ac", "1", "-ar", "16000"]
    for call in calls:
        assert not Path(call[-1]).exists()
        assert not Path(call[-1]).parent.exists()
    assert capsys.readouterr().out == f"{out}\n" * 3

    resumed = main(argv)
    assert resumed == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["resume"]["skippedCompletedCount"] == 1
    assert [row["recordingId"] for row in manifest["results"]] == ["a", "b"]


def run_worker(local_root: Path, body: str, **overrides: Any) -> dict[str, Any]:
    python = write_executable(local_root / "python", body)
    options = base_options(local_root, venv_python=str(python), **overrides)
    hooks = FeatureHooks(
        tool_versions={},
        decode_recording=lambda entry, options: DecodedRecording(
            wav_path="/fixture.wav", cleanup=lambda: None
        ),
    )
    return extract_index_manifest(fake_index(), fake_results(), options, hooks)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "print('not json', flush=True)\nimport time; time.sleep(30)\n",
            "feature worker emitted unparseable output: not json",
        ),
        (
            "print('{\"hello\": NaN}', flush=True)\n",
            'feature worker emitted unparseable output: {"hello": NaN}',
        ),
        (
            "import sys\nfor n in range(7): sys.stderr.write(f'line {n}\\n')\n"
            "raise SystemExit(3)\n",
            "feature worker exited (code 3): line 2\nline 3\nline 4\nline 5\nline 6",
        ),
        ("print('{\"hello\": 0}')\n", "feature worker exited (code 0)"),
        (
            "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n",
            "feature worker exited (code null)",
        ),
    ],
)
def test_worker_failures_before_hello(local_root: Path, body: str, message: str) -> None:
    with pytest.raises(FeaturesError) as caught:
        run_worker(local_root, body)
    assert str(caught.value) == message


HELLO = 'import json, sys\nprint(json.dumps({"hello": {"versions": None}}), flush=True)\n'


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            HELLO + "sys.stdin.readline()\nprint('x' * 300, flush=True)\n",
            "feature worker emitted unparseable output: " + "x" * 200,
        ),
        (
            HELLO + "sys.stdin.readline()\nsys.stderr.write('died\\n')\nraise SystemExit(5)\n",
            "feature worker exited (code 5): died",
        ),
        (HELLO + "sys.stdin.close()\nraise SystemExit(0)\n", "feature worker exited (code 0)"),
        (
            "import sys\nsys.stdout.write('{\"hello\": true}')\n",
            "feature worker exited (code 0)",
        ),
    ],
)
def test_worker_failures_during_extraction(
    local_root: Path, body: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(FeaturesError) as caught:
        run_worker(local_root, body)
    assert str(caught.value) == message


def test_a_missing_analysis_python_is_a_spawn_error(local_root: Path) -> None:
    options = base_options(local_root, venv_python=str(local_root / "absent" / "python"))
    with pytest.raises(FeaturesError) as caught:
        extract_index_manifest(fake_index(), fake_results(), options, FeatureHooks())
    assert str(caught.value) == f"spawn {local_root / 'absent' / 'python'} ENOENT"


def test_a_closed_stdin_pipe_is_reported_as_the_worker_exit(
    local_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = features.ProcessFeatureWorker(
        base_options(local_root, venv_python=str(write_executable(local_root / "py", HELLO)))
    )

    class Broken:
        def write(self, text: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

        def close(self) -> None:
            raise BrokenPipeError

    monkeypatch.setattr(worker, "_stdin", Broken())
    with pytest.raises(FeaturesError, match=r"feature worker exited \(code 0\)"):
        worker.extract({"recordingId": "a", "wavPath": "/x"})
    worker.close()


# --- ffmpeg ----------------------------------------------------------------------------


def test_ffmpeg_failures_and_tool_versions(local_root: Path) -> None:
    ffmpeg = write_executable(local_root / "ffmpeg", FAKE_FFMPEG)
    options = base_options(local_root, ffmpeg_bin=str(ffmpeg), corpus_root="/c")
    before = set(Path(tempfile.gettempdir()).glob("voice-journey-features-*"))
    with pytest.raises(FeaturesError) as caught:
        features.decode_recording_to_wav({"recordingId": "m", "filename": "missing.m4a"}, options)
    assert str(caught.value) == "ffmpeg failed for missing.m4a: No such file"
    assert set(Path(tempfile.gettempdir()).glob("voice-journey-features-*")) == before

    silent = write_executable(local_root / "silent", "raise SystemExit(4)\n")
    with pytest.raises(FeaturesError) as caught:
        features._capture_command(str(silent), [])
    assert str(caught.value) == "silent failed: exit code 4"
    stderr_only = write_executable(
        local_root / "stderr-only", "import sys\nsys.stderr.write('  v9 on stderr \\n')\n"
    )
    assert read_tool_version(str(stderr_only), ["-version"]) == "v9 on stderr"
    assert read_tool_version(str(silent), ["-version"]) is None
    assert read_tool_version(str(local_root / "absent"), ["-version"]) is None
    blank = write_executable(local_root / "blank", "print('   ')\n")
    assert read_tool_version(str(blank), ["-version"]) is None
    with pytest.raises(FeaturesError, match=r"^spawn .*absent ENOENT$"):
        features.decode_recording_to_wav(
            {"recordingId": "m", "filename": "x.m4a"},
            base_options(local_root, ffmpeg_bin=str(local_root / "absent")),
        )


def test_js_join_appends_every_part() -> None:
    assert features._js_join("/corpus/", "/abs/name.m4a") == "/corpus/abs/name.m4a"
    assert features._js_join("", "x.json") == "x.json"
    assert features._js_join("./a/../b", "c") == "b/c"
    assert features._js_join("", "") == "."


# --- the extractor resource without its runtime ----------------------------------------


def _analysis_runtime_installed() -> bool:
    return any(importlib.util.find_spec(name) is not None for name in ("librosa", "parselmouth"))


needs_absent_runtime: Callable[[Any], Any] = pytest.mark.skipif(
    _analysis_runtime_installed(), reason="the analysis runtime is installed here"
)


@needs_absent_runtime
@pytest.mark.parametrize("name", ["journey_extract_features.py", "journey_extract_selftest.py"])
def test_the_extractor_resources_refuse_clearly_without_their_runtime(name: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(resource_path(name))],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "the isolated analysis runtime is missing" in completed.stderr
    assert "requirements/runtimes/journey-analysis.txt" in completed.stderr


@needs_absent_runtime
def test_the_packaged_worker_under_the_wrong_python_fails_the_run_clearly(
    local_root: Path,
) -> None:
    options = base_options(
        local_root,
        venv_python=sys.executable,
        worker=str(resource_path("journey_extract_features.py")),
    )
    with pytest.raises(FeaturesError) as caught:
        extract_index_manifest(fake_index(), fake_results(), options, FeatureHooks())
    assert str(caught.value).startswith(
        "feature worker exited (code 2): journey_extract_features: "
        "the isolated analysis runtime is missing"
    )

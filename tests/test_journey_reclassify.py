"""reclassify.test.mjs, translated (design §5.3), then the cases that hold the port's
remaining branches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.journey import reclassify
from cole_voice_lab.journey.filter import classify_features
from cole_voice_lab.journey.reclassify import (
    ReclassifyError,
    ReclassifyOptions,
    apply_reclassification,
    build_reclassify_dry_run,
    main,
    match_rule,
    music_score_for,
    noise_score_for,
    reclassify_row,
)

OPTIONS = ReclassifyOptions(
    results="r.json", lyrics="l.json", reclassified_at="2026-07-30T00:00:00.000Z"
)


def capture_run(capsys: pytest.CaptureFixture[str], args: list[str]) -> tuple[int, str]:
    code = main(args)
    return code, capsys.readouterr().out


def features(**overrides: Any) -> dict[str, Any]:
    return {
        "durationSeconds": 60,
        "activeRatio": 0.6,
        "voicedRatio": 0.35,
        "meanRmsDb": -25,
        "meanZeroCrossingRate": 0.1,
        "meanPitchHz": 220,
        "meanPitchConfidence": 0.4,
        "pitchStability": 0.5,
        "clippingRatio": 0,
        "frameCount": 1875,
        **overrides,
    }


def result_row(
    recording_id: str,
    bucket: str,
    *,
    content_label: str | None = None,
    contamination_label: str = "uncertain",
    confidence: float = 0.45,
    feature_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    default_content = "uncertain" if bucket == "uncertain_manual_review" else "singing"
    return {
        "recordingId": recording_id,
        "filename": f"{recording_id}.m4a",
        "capturedAt": "2021-01-01T00:00:00Z",
        "year": "2021",
        "features": features(**(feature_overrides or {})),
        "classification": {
            "method": "voice-journey.local-heuristic-proof.v1",
            "contentLabel": content_label if content_label is not None else default_content,
            "contaminationLabel": contamination_label,
            "finalBucket": bucket,
            "confidence": confidence,
            "rationale": "fixture",
        },
        "humanSpotCheck": {
            "recommended": True,
            "status": "not_reviewed",
            "reviewedBy": None,
            "notes": None,
        },
    }


def fake_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": "voice-journey.full-corpus-filter-results.v1",
        "generatedAt": "2026-07-16T00:00:00.000Z",
        "updatedAt": "2026-07-16T00:00:00.000Z",
        "results": rows,
    }


def lyric_row(
    recording_id: str,
    *,
    cluster: dict[str, Any] | None = None,
    wordless: dict[str, Any] | None = None,
    repetition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "recordingId": recording_id,
        "lyricMatch": {
            "marked": bool(cluster or wordless or repetition),
            "signals": {
                "crossRecordingCluster": cluster
                or {"marked": False, "clusterId": None, "clusterSize": 0, "confidence": 0},
                "repetitionStructure": repetition
                or {"marked": False, "repeatedNgramCount": 0, "confidence": 0.35},
                "wordlessVocalise": wordless
                or {"marked": False, "wordCount": 10, "detectedLanguage": "en"},
            },
        },
    }


def fake_lyrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": "voice-journey.local-lyric-indicators.v1",
        "generatedAt": "2026-07-16T00:00:00.000Z",
        "results": rows,
    }


def cluster_signal(number: int, size: int) -> dict[str, Any]:
    return {
        "marked": True,
        "clusterId": f"lyric-cluster-{number:03d}",
        "clusterLabel": f"recurring_lyric_cluster_{number:03d}",
        "clusterSize": size,
        "confidence": 0.99,
    }


def test_recurring_cluster_evidence_resolves_uncertain_rows_and_preserves_v1() -> None:
    results = fake_results(
        [
            result_row(
                "a",
                "uncertain_manual_review",
                content_label="singing",
                feature_overrides={"meanPitchConfidence": 0.45, "meanZeroCrossingRate": 0.16},
            )
        ]
    )
    lyrics = fake_lyrics([lyric_row("a", cluster=cluster_signal(7, 30))])
    _, counters = apply_reclassification(results, lyrics, OPTIONS)
    row = results["results"][0]
    assert row["classification"]["method"] == "voice-journey.evidence-join-reclassify.v2"
    assert row["classification"]["contentLabel"] == "singing"
    assert row["classification"]["finalBucket"] == "clean_singing"
    assert row["previousClassification"]["method"] == "voice-journey.local-heuristic-proof.v1"
    assert row["previousClassification"]["finalBucket"] == "uncertain_manual_review"
    assert row["evidence"]["rule"] == "recurring_cluster"
    assert row["evidence"]["signals"]["clusterId"] == "lyric-cluster-007"
    assert row["evidence"]["sources"]["audioBytes"] is False
    assert counters["resolvedByRule"]["recurring_cluster"] == 1
    assert counters["uncertainAfter"] == 0
    assert results["reclassification"]["counters"]["resolvedToBucket"]["clean_singing"] == 1


def test_evidence_backed_rows_keep_v1_noise_music_contamination_flags() -> None:
    results = fake_results(
        [
            result_row(
                "noisy",
                "uncertain_manual_review",
                content_label="singing",
                feature_overrides={"meanZeroCrossingRate": 0.35, "meanPitchConfidence": 0.4},
            )
        ]
    )
    lyrics = fake_lyrics([lyric_row("noisy", cluster=cluster_signal(1, 60))])
    apply_reclassification(results, lyrics, OPTIONS)
    row = results["results"][0]
    assert row["classification"]["contaminationLabel"] == "noise_contaminated"
    assert row["classification"]["finalBucket"] == "noise_contaminated_singing"
    assert row["evidence"]["contaminationScores"]["noiseScore"] >= 0.55


def test_wordless_vocalise_and_repetition_rules_resolve_when_voiced_enough() -> None:
    results = fake_results(
        [
            result_row(
                "hum",
                "uncertain_manual_review",
                content_label="uncertain",
                feature_overrides={"voicedRatio": 0.5, "meanPitchConfidence": 0.5},
            ),
            result_row(
                "chorus",
                "uncertain_manual_review",
                content_label="uncertain",
                feature_overrides={"voicedRatio": 0.4, "meanPitchConfidence": 0.45},
            ),
            result_row(
                "quiet",
                "uncertain_manual_review",
                content_label="uncertain",
                feature_overrides={"voicedRatio": 0.25},
            ),
        ]
    )
    vocalise = {"marked": True, "wordCount": 0, "detectedLanguage": "unknown"}
    lyrics = fake_lyrics(
        [
            lyric_row("hum", wordless=vocalise),
            lyric_row(
                "chorus",
                repetition={"marked": True, "repeatedNgramCount": 5, "confidence": 0.75},
            ),
            lyric_row("quiet", wordless=vocalise),
        ]
    )
    _, counters = apply_reclassification(results, lyrics, OPTIONS)
    assert results["results"][0]["classification"]["finalBucket"] == "clean_singing"
    assert results["results"][0]["evidence"]["rule"] == "wordless_vocalise"
    assert results["results"][1]["evidence"]["rule"] == "repetition_structure"
    assert results["results"][2]["classification"]["finalBucket"] == "uncertain_manual_review"
    assert "previousClassification" not in results["results"][2]
    assert counters["uncertainBefore"] == 3
    assert counters["uncertainAfter"] == 1


def test_decided_rows_are_untouched_and_non_singing_rows_in_clusters_get_flags() -> None:
    results = fake_results(
        [
            result_row(
                "kept",
                "clean_singing",
                content_label="singing",
                contamination_label="clean",
                confidence=0.8,
            ),
            result_row(
                "spoken",
                "non_singing",
                content_label="non_singing",
                contamination_label="not_applicable",
                confidence=0.7,
            ),
        ]
    )
    lyrics = fake_lyrics(
        [
            lyric_row("kept", cluster=cluster_signal(2, 57)),
            lyric_row("spoken", cluster=cluster_signal(3, 53)),
        ]
    )
    _, counters = apply_reclassification(results, lyrics, OPTIONS)
    kept, spoken = results["results"]
    assert kept["classification"]["method"] == "voice-journey.local-heuristic-proof.v1"
    assert "evidenceConflict" not in kept
    assert spoken["classification"]["finalBucket"] == "non_singing"
    assert spoken["evidenceConflict"]["type"] == "non_singing_in_recurring_cluster"
    assert spoken["humanSpotCheck"]["recommended"] is True
    assert counters["conflictsFlagged"] == 1
    assert counters["decidedRowsUntouched"] == 2


def test_contamination_score_formulas_agree_with_the_v1_classifier() -> None:
    noisy = features(
        voicedRatio=0.5, meanPitchConfidence=0.5, pitchStability=0.2, meanZeroCrossingRate=0.4
    )
    v1_noisy = classify_features(noisy)
    assert v1_noisy["contentLabel"] == "singing"
    assert v1_noisy["contaminationLabel"] == "noise_contaminated"
    assert noise_score_for(noisy) >= 0.55

    clean = features(
        voicedRatio=0.5, meanPitchConfidence=0.7, pitchStability=0.2, meanZeroCrossingRate=0.1
    )
    v1_clean = classify_features(clean)
    assert v1_clean["contaminationLabel"] == "clean"
    assert noise_score_for(clean) < 0.55
    assert music_score_for(clean) < 0.62


def test_apply_command_dry_run_counts_without_writing_and_runs_are_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results_path = tmp_path / "results.json"
    lyrics_path = tmp_path / "lyrics.json"
    results_path.write_text(
        json.dumps(
            fake_results(
                [
                    result_row("a", "uncertain_manual_review", content_label="singing"),
                    result_row("b", "uncertain_manual_review", content_label="uncertain"),
                ]
            )
        ),
        encoding="utf-8",
    )
    lyrics_path.write_text(
        json.dumps(fake_lyrics([lyric_row("a", cluster=cluster_signal(5, 37)), lyric_row("b")])),
        encoding="utf-8",
    )
    before = results_path.read_text(encoding="utf-8")

    common = ["apply", "--results", str(results_path), "--lyrics", str(lyrics_path)]
    code, stdout = capture_run(capsys, [*common, "--dry-run"])
    payload = json.loads(stdout)
    assert code == 0
    assert payload["dryRun"] is True
    assert payload["counters"]["uncertainBefore"] == 2
    assert payload["counters"]["uncertainAfter"] == 1
    assert payload["readScope"]["audioBytes"] is False
    assert results_path.read_text(encoding="utf-8") == before

    out_a = tmp_path / "out-a.json"
    out_b = tmp_path / "out-b.json"
    stamp = ["--reclassified-at", "2026-07-30T00:00:00.000Z"]
    code_a, _ = capture_run(capsys, [*common, "--out", str(out_a), *stamp])
    code_b, _ = capture_run(capsys, [*common, "--out", str(out_b), *stamp])
    assert code_a == 0
    assert code_b == 0
    written_a = out_a.read_text(encoding="utf-8")
    assert written_a == out_b.read_text(encoding="utf-8")
    written = json.loads(written_a)
    assert written["reclassification"]["method"] == "voice-journey.evidence-join-reclassify.v2"
    assert written["reclassification"]["counters"]["resolvedByRule"]["recurring_cluster"] == 1
    assert written["updatedAt"] == "2026-07-30T00:00:00.000Z"


def test_dry_run_builder_leaves_the_input_manifest_object_unmodified() -> None:
    results = fake_results([result_row("a", "uncertain_manual_review", content_label="singing")])
    lyrics = fake_lyrics([lyric_row("a", cluster=cluster_signal(9, 26))])
    snapshot = json.dumps(results)
    payload = build_reclassify_dry_run(
        results, lyrics, ReclassifyOptions(results="r.json", lyrics="l.json", out="r.json")
    )
    assert payload["counters"]["uncertainBefore"] == 1
    assert json.dumps(results) == snapshot


# The port's own cases: the command line, refusals and JavaScript's coercions.


def test_cli_help_defaults_and_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VOICE_JOURNEY_DATA", raising=False)
    assert main(["help"]) == 0
    assert capsys.readouterr().out.startswith("Voice Journey evidence-join reclassifier.")
    assert main(["apply", "--help"]) == 0
    capsys.readouterr()
    options = reclassify.parse_args([])
    assert options.results == "manifests/full-corpus-filter-results.json"
    assert options.lyrics == "manifests/local-lyric-indicators.json"
    assert reclassify._target_for(options) == options.results
    for args, message in {
        ("--lyrics",): "--lyrics requires a value",
        ("--bogus",): "unsupported argument: --bogus",
    }.items():
        with pytest.raises(ReclassifyError, match=message):
            reclassify.parse_args(list(args))
    assert main(["scan"]) == 1
    assert capsys.readouterr().err == "error: unsupported command: scan\n"
    (tmp_path / "bad.json").write_text("NaN", encoding="utf-8")
    bad = str(tmp_path / "bad.json")
    assert main(["--results", bad, "--lyrics", bad, "--dry-run"]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_out_dash_writes_the_manifest_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps({"results": []}), encoding="utf-8")
    code, stdout = capture_run(
        capsys,
        ["--results", str(results_path), "--lyrics", str(results_path), "--out", "-"],
    )
    payload = json.loads(stdout)
    assert code == 0
    assert payload["reclassification"]["sources"]["results"] == {
        "path": str(results_path),
        "schemaVersion": None,
        "generatedAt": None,
        "updatedAt": None,
    }
    assert payload["updatedAt"].endswith("Z")


def test_missing_and_null_features_coerce_as_javascript_does() -> None:
    row: dict[str, Any] = {
        "recordingId": "x",
        "features": None,
        "classification": {"finalBucket": "uncertain_manual_review"},
    }
    resolved = reclassify_row(row, lyric_row("x", cluster=cluster_signal(1, 3))["lyricMatch"])
    assert resolved is not None
    assert resolved["evidence"]["contaminationScores"] == {"noiseScore": None, "musicScore": None}
    assert resolved["classification"]["contaminationLabel"] == "clean"
    assert resolved["classification"]["rationale"].endswith(
        "musicScore=null); priorContent=null; voicedRatio=undefined; "
        "pitchConfidence=undefined; zcr=undefined"
    )
    nulls = {"meanZeroCrossingRate": 0.5, "meanPitchConfidence": None, "clippingRatio": None}
    assert noise_score_for(nulls) == 1
    music = {"activeRatio": 1.0, "voicedRatio": 1.0, "pitchStability": None}
    assert music_score_for(music) == 0.5569999999999999
    assert music_score_for({**music, "pitchStability": 0.01}) == 0.8069999999999999


def test_rules_need_voicing_and_counts() -> None:
    voiced = {"voicedRatio": 0.4}
    assert match_rule(None, voiced) is None
    assert match_rule({}, voiced) is None
    wordless = {"signals": {"wordlessVocalise": {"marked": True, "wordCount": "0"}}}
    matched = match_rule(wordless, voiced)
    assert matched is not None
    assert matched["contentConfidence"] == 0.62
    assert matched["signals"] == {"wordCount": "0", "detectedLanguage": None}
    assert match_rule(wordless, {"voicedRatio": None}) is None
    repetition = {"signals": {"repetitionStructure": {"marked": True, "repeatedNgramCount": 2}}}
    assert match_rule(repetition, voiced) is None
    repetition["signals"]["repetitionStructure"]["repeatedNgramCount"] = 40
    matched = match_rule(repetition, voiced)
    assert matched is not None
    assert matched["contentConfidence"] == 0.7
    cluster = {"signals": {"crossRecordingCluster": {"marked": True}}}
    matched = match_rule(cluster, {})
    assert matched is not None
    assert matched["contentConfidence"] == 0.6
    assert matched["signals"]["clusterSize"] is None


def test_music_contamination_and_low_confidence_recommend_a_spot_check() -> None:
    loud = result_row(
        "m",
        "uncertain_manual_review",
        feature_overrides={"activeRatio": 1.0, "voicedRatio": 1.0, "pitchStability": 0.01},
    )
    del loud["humanSpotCheck"]
    results = fake_results([loud, {"recordingId": "n", "features": {}}])
    lyrics = fake_lyrics([lyric_row("m", cluster=cluster_signal(4, 2)), {"recordingId": None}, {}])
    _, counters = apply_reclassification(results, lyrics, OPTIONS)
    row = results["results"][0]
    assert row["classification"]["contaminationLabel"] == "music_contaminated"
    assert row["humanSpotCheck"] == {"recommended": False}
    assert counters["resolvedToBucket"]["music_contaminated_singing"] == 1
    assert counters["decidedRowsUntouched"] == 1
    weak = fake_results([result_row("w", "uncertain_manual_review")])
    apply_reclassification(
        weak,
        fake_lyrics([lyric_row("w", cluster=cluster_signal(4, 2))]),
        ReclassifyOptions(results="r", lyrics="l"),
    )
    assert weak["results"][0]["humanSpotCheck"]["recommended"] is True
    assert weak["reclassification"]["reclassifiedAt"].endswith("Z")
    assert reclassify._final_bucket_for("non_singing", "clean") == "non_singing"
    assert reclassify._final_bucket_for("uncertain", "clean") == "uncertain_manual_review"
    assert reclassify._final_bucket_for("singing", "other") == "uncertain_manual_review"

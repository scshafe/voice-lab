"""lyric-indicators.test.mjs, translated (design §5.3), then the cases that hold the
port's remaining branches."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.journey import lyrics
from cole_voice_lab.journey.lyrics import (
    LyricIndicatorError,
    LyricOptions,
    analyze_lyric_indicators,
    build_dry_run,
    main,
    repetition_signal,
    tokenize,
    wordless_signal,
)


def capture_run(capsys: pytest.CaptureFixture[str], args: list[str]) -> tuple[int, str]:
    code = main(args)
    return code, capsys.readouterr().out


def fake_transcript_manifest() -> dict[str, Any]:
    return {
        "schemaVersion": "voice-journey.local-stt-transcripts.v1",
        "generatedAt": "2026-07-16T00:00:00.000Z",
        "updatedAt": "2026-07-16T01:00:00.000Z",
        "localTranscriptStore": {
            "root": "local-artifacts/stt-transcripts",
            "committedToRepo": False,
        },
        "results": [
            transcript_row("a", "2019", 17, "en"),
            transcript_row("b", "2020", 16, "en"),
            transcript_row("c", "2021", 14, "en"),
            transcript_row("d", "2022", 0, "en"),
            transcript_row("e", "2023", 4, "ko"),
        ],
    }


def transcript_row(recording_id: str, year: str, words: int, language: str) -> dict[str, Any]:
    return {
        "recordingId": recording_id,
        "filename": f"{year}0101 000000-{recording_id}.m4a",
        "capturedAt": f"{year}-01-01T00:00:00Z",
        "year": year,
        "status": "completed",
        "language": {"requested": "auto", "detected": language, "probability": None},
        "counts": {"words": words, "segments": 1, "characters": words * 5},
        "localArtifacts": {
            "transcriptTextPath": f"local-artifacts/stt-transcripts/{recording_id}.txt",
            "transcriptJsonPath": f"local-artifacts/stt-transcripts/{recording_id}.json",
            "textIncludedInManifest": False,
        },
    }


def test_lyric_dry_run_declares_local_only_scope_without_reading_text() -> None:
    dry_run = build_dry_run(
        fake_transcript_manifest(),
        LyricOptions(
            out="manifests/local-lyric-indicators.json",
            transcript_dir="local-artifacts/stt-transcripts",
            cluster_threshold=0.22,
            min_cluster_size=2,
        ),
    )
    assert dry_run["operation"] == "local-lyric-indicators"
    assert dry_run["recordingCount"] == 5
    assert dry_run["readScope"]["localTranscriptText"] is True
    assert dry_run["readScope"]["localTranscriptTextReadInDryRun"] is False
    assert dry_run["readScope"]["externalLyricDatabases"] is False
    assert dry_run["strategy"]["rawLyricTextInOutput"] is False


def test_dry_run_command_succeeds_even_when_transcript_files_are_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "stt.json"
    manifest_path.write_text(json.dumps(fake_transcript_manifest()), encoding="utf-8")
    code, stdout = capture_run(
        capsys, ["analyze", "--transcripts", str(manifest_path), "--dry-run"]
    )
    payload = json.loads(stdout)
    assert code == 0
    assert payload["dryRun"] is True
    assert payload["readScope"]["localTranscriptTextReadInDryRun"] is False


def test_analyze_marks_recurring_local_transcript_clusters_without_raw_lyric_text(
    tmp_path: Path,
) -> None:
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    manifest = fake_transcript_manifest()
    texts = {
        "a": "silver river carries me home bright moon silver river carries me home",
        "b": "bright moon silver river carries me home tonight",
        "c": "plain spoken memo about errands and groceries",
        "d": "",
        "e": "la la vocal tone",
    }
    for name, text in texts.items():
        (transcript_dir / f"{name}.txt").write_text(text, encoding="utf-8")

    output = analyze_lyric_indicators(
        manifest,
        LyricOptions(
            cluster_threshold=0.2,
            min_cluster_size=2,
            transcript_dir=str(transcript_dir),
            generated_at="2026-07-16T00:00:00.000Z",
        ),
    )

    by_id = {row["recordingId"]: row for row in output["results"]}
    assert output["schemaVersion"] == "voice-journey.local-lyric-indicators.v1"
    assert output["readScope"]["externalLyricDatabases"] is False
    assert output["privacy"]["containsRawLyricText"] is False
    assert len(output["clusters"]) == 1
    assert output["clusters"][0]["recordingIds"] == ["a", "b"]
    assert by_id["a"]["lyricMatch"]["matchedCluster"] == "lyric-cluster-001"
    assert by_id["c"]["lyricMatch"]["marked"] is False
    assert by_id["d"]["lyricMatch"]["signals"]["wordlessVocalise"]["marked"] is True
    assert by_id["e"]["lyricMatch"]["signals"]["wordlessVocalise"]["marked"] is True
    assert not re.search(r"silver river|bright moon|groceries|vocal tone", json.dumps(output))


def test_repetition_and_wordless_helpers_expose_indicator_signals() -> None:
    tokens = tokenize(
        "turn around sing it again turn around sing it again turn around sing it again"
    )
    repetition = repetition_signal(tokens)
    assert repetition["marked"] is True
    assert repetition["repeatedNgramCount"] > 0
    assert wordless_signal({"counts": {"words": 0}, "language": {"detected": "en"}})["marked"]
    assert wordless_signal({"counts": {"words": 4}, "language": {"detected": "haw"}})["marked"]
    assert not wordless_signal({"counts": {"words": 4}, "language": {"detected": "en"}})["marked"]


def test_analyze_command_writes_repo_safe_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "stt.json"
    out = tmp_path / "lyric.json"
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    manifest = fake_transcript_manifest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for row in manifest["results"]:
        (transcript_dir / f"{row['recordingId']}.txt").write_text(
            "same local refrain same local refrain same local refrain", encoding="utf-8"
        )

    code, _ = capture_run(
        capsys,
        [
            "analyze",
            "--transcripts",
            str(manifest_path),
            "--transcript-dir",
            str(transcript_dir),
            "--out",
            str(out),
            "--cluster-threshold",
            "0.2",
        ],
    )
    assert code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["privacy"]["containsTranscriptText"] is False
    assert written["readScope"]["audioBytes"] is False
    assert written["readScope"]["upload"] is False
    assert "same local refrain" not in json.dumps(written)


# The port's own cases: the command line, refusals and the manifest's edges.


def test_cli_flags_help_and_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VOICE_JOURNEY_DATA", raising=False)
    assert main(["help"]) == 0
    assert capsys.readouterr().out.startswith("Voice Journey local lyric indicators.")
    assert main(["--help"]) == 0
    capsys.readouterr()
    options = lyrics.parse_args(
        ["--min-cluster-size", "0x3", "--generated-at", "g", "--cluster-threshold", " 1e-1 "]
    )
    assert (options.command, options.min_cluster_size, options.cluster_threshold) == (
        "analyze",
        3,
        0.1,
    )
    assert options.generated_at == "g"
    assert options.transcripts == "manifests/local-stt-transcripts.json"
    assert options.transcript_dir == "local-artifacts/stt-transcripts"
    refusals = {
        ("--out",): "--out requires a value",
        ("--out", "--dry-run"): "--out requires a value",
        ("--min-cluster-size", "2.5"): "--min-cluster-size must be a positive integer",
        ("--min-cluster-size", "0"): "--min-cluster-size must be a positive integer",
        ("--cluster-threshold", "1.5"): "--cluster-threshold must be a ratio in (0, 1]",
        ("--cluster-threshold", "Infinity"): "--cluster-threshold must be a ratio in (0, 1]",
        ("--bogus",): "unsupported argument: --bogus",
        ("",): "unsupported argument: ",
    }
    for args, message in refusals.items():
        with pytest.raises(LyricIndicatorError, match=re.escape(message)):
            lyrics.parse_args(list(args))
    assert main(["--bogus"]) == 1
    assert capsys.readouterr().err == "error: unsupported argument: --bogus\n"
    assert main(["scan"]) == 1
    assert "unsupported command: scan" in capsys.readouterr().err
    missing = tmp_path / "missing.json"
    assert main(["analyze", "--transcripts", str(missing), "--dry-run"]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_unreadable_transcript_is_refused(tmp_path: Path) -> None:
    manifest = {"results": [transcript_row("a", "2019", 3, "en")]}
    with pytest.raises(LyricIndicatorError, match="failed to read local transcript for a: "):
        analyze_lyric_indicators(manifest, LyricOptions(transcript_dir=str(tmp_path)))


def test_manifest_edges_follow_the_javascript(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = [
        {"recordingId": "z1", "status": "completed", "counts": {"words": 9}},
        {"recordingId": "z2", "status": "completed", "capturedAt": "2024-05-01T00:00:00Z"},
        {"recordingId": "y1", "status": "completed", "year": None, "capturedAt": None},
        {"recordingId": "y2", "status": "failed", "filename": None},
    ]
    text = "one two three four five six seven eight nine ten"

    def reader(row: dict[str, Any], options: LyricOptions) -> str:
        return text if row["recordingId"] != "z1" else text + " eleven"

    output = analyze_lyric_indicators(
        {"sourceIndex": {"recordingCount": 7}, "results": rows},
        LyricOptions(min_cluster_size=3, generated_at="t"),
        read_transcript=reader,
    )
    assert output["sourceTranscripts"] == {"recordingCount": 4, "localTranscriptStore": None}
    assert [cluster["recordingIds"] for cluster in output["clusters"]] == [["y1", "z1", "z2"]]
    assert output["clusters"][0]["years"] == ["2024", "unknown"]
    by_id = {row["recordingId"]: row for row in output["results"]}
    assert "filename" not in by_id["z1"]
    assert by_id["y2"]["filename"] is None
    assert by_id["y1"]["year"] == "unknown"
    assert by_id["z1"]["lyricMatch"]["rationaleCodes"] == ["recurs_across_recordings"]
    assert by_id["y2"]["lyricMatch"]["rationaleCodes"] == ["zero_word_transcript"]
    assert output["totals"]["methodCounts"] == {
        "cross_recording_similarity": 3,
        "wordless_vocalise_signal": 3,
    }
    assert build_dry_run({"results": None, "sourceIndex": {"recordingCount": 2}}, LyricOptions())[
        "sourceTranscripts"
    ] == {"recordingCount": 2, "localTranscriptStore": None}
    assert "outputPath" not in build_dry_run({}, LyricOptions())
    assert lyrics._resolve_transcript_path(
        {"recordingId": "r"}, LyricOptions(transcript_dir="d")
    ) == Path("d/r.txt")
    assert lyrics._resolve_transcript_path(
        {"localArtifacts": {"transcriptTextPath": "x/y/z.txt/"}}, LyricOptions(transcript_dir="d")
    ) == Path("d/z.txt")


def test_clusters_order_by_size_then_seed_and_merge_transitively() -> None:
    base = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    other = "red orange yellow green blue indigo violet black white grey"
    texts = {
        "c": base,
        "a": base + " lambda",
        "q": other,
        "p": other + " brown",
        "b": "zeta eta theta iota kappa lambda mu nu xi omicron",
        "solo": "nothing here repeats at all in any way whatsoever",
    }
    rows = [{"recordingId": key, "status": "completed"} for key in texts]
    output = analyze_lyric_indicators(
        {"results": rows},
        LyricOptions(cluster_threshold=0.1, generated_at="t"),
        read_transcript=lambda row, options: texts[row["recordingId"]],
    )
    assert [cluster["recordingIds"] for cluster in output["clusters"]] == [
        ["a", "b", "c"],
        ["p", "q"],
    ]
    assert [cluster["clusterId"] for cluster in output["clusters"]] == [
        "lyric-cluster-001",
        "lyric-cluster-002",
    ]


def test_tokenize_folds_accents_and_drops_punctuation() -> None:
    assert tokenize("Café 'Naïve' x I'm ROCK-n-roll ﬁne") == [
        "cafe",
        "naive",
        "i'm",
        "rock",
        "roll",
        "fine",
    ]
    assert repetition_signal(["a"] * 11)["marked"] is False
    assert wordless_signal({})["detectedLanguage"] == "unknown"
    assert (
        wordless_signal({"language": {"detected": None}, "counts": {"words": "0"}})["marked"]
        is False
    )

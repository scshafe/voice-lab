"""local-stt.test.mjs and whisper-threads.test.mjs, translated (design §5.3), then the
cases that hold the port to the JS rules it reproduces.

ffmpeg and whisper-cli are fake executables written into ``tmp_path``; no audio and no
model are read. Transcript text in these tests is synthetic."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.journey import stt
from cole_voice_lab.journey.stt import (
    LocalSttError,
    SttOptions,
    TranscriptionResult,
    backfill_manifest,
    build_backfill_dry_run,
    build_stt_dry_run,
    build_stt_manifest,
    main,
    normalize_transcript,
    parse_args,
    run,
    transcribe_index_manifest,
    transcribe_recording_entry,
)

ENV_KEYS = (
    "VOICE_JOURNEY_DATA",
    "WHISPER_CPP_THREADS",
    "WHISPER_CPP_BIN",
    "WHISPER_CPP_MODEL",
    "FFMPEG_BIN",
    "WHISPER_ARGS_LOG",
)

FAKE_FFMPEG = """#!/bin/sh
for a; do last="$a"; done
case "$1" in -version) echo fake-ffmpeg; exit 0;; esac
: > "$last"
"""
FAKE_WHISPER = """#!/bin/sh
case "$1" in --version) echo fake-whisper; exit 0;; esac
printf '%s\\n' "$*" >> "$WHISPER_ARGS_LOG"
while [ $# -gt 0 ]; do [ "$1" = "-of" ] && base="$2"; shift; done
printf 'x' > "$base.txt"
segment='{"text":"x","offsets":{"from":0,"to":1000}}'
printf '{"result":{"language":"en"},"transcription":[%s]}' "$segment" > "$base.json"
"""


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory of its own (local-dev layout: ``local-artifacts/`` under it)
    and no journey environment."""

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield work


@pytest.fixture
def local_artifact_dir(cwd: Path) -> Path:
    root = cwd / "local-artifacts" / "stt-test-root"
    root.mkdir(parents=True)
    return root


def whisper_segment(text: str, from_ms: int, to_ms: int) -> dict[str, Any]:
    return {
        "text": text,
        "offsets": {"from": from_ms, "to": to_ms},
        "timestamps": {"from": ms_to_comma_timestamp(from_ms), "to": ms_to_comma_timestamp(to_ms)},
    }


def ms_to_comma_timestamp(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d},{ms % 1000:03d}"


def capture_run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    code = run(args)
    return code, capsys.readouterr().out


def fake_index() -> dict[str, Any]:
    return {
        "schemaVersion": "voice-journey.recording-index.v1",
        "generatedAt": "2026-07-16T00:00:00.000Z",
        "totals": {"recordings": 2},
        "recordings": [
            recording("a", "20190101 000000-A.m4a", "2019-01-01T00:00:00Z"),
            recording("b", "20200101 000000-B.m4a", "2020-01-01T00:00:00Z"),
        ],
    }


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


def write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def stt_options(**changes: Any) -> SttOptions:
    base = SttOptions(
        corpus_root="/tmp/corpus",
        ffmpeg_bin="/opt/homebrew/bin/ffmpeg",
        language="auto",
        model="/opt/homebrew/opt/whisper-cpp/share/whisper-cpp/models/ggml-medium.bin",
        out="manifests/local-stt-transcripts.json",
        transcript_dir="local-artifacts/stt-transcripts",
        whisper_bin="/opt/homebrew/bin/whisper-cli",
    )
    return replace(base, **changes)


def executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def tmp_allows_exec(root: Path) -> bool:
    probe = executable(root / "exec-probe", "#!/bin/sh\n")
    try:
        return subprocess.run([str(probe)], check=False).returncode == 0
    except OSError:
        return False


@pytest.fixture
def fakes(tmp_path: Path, cwd: Path) -> dict[str, Path]:
    if not tmp_allows_exec(tmp_path):
        pytest.skip("temp dir is mounted noexec")
    return {
        "ffmpeg": executable(tmp_path / "ffmpeg", FAKE_FFMPEG),
        "whisper": executable(tmp_path / "whisper-cli", FAKE_WHISPER),
    }


def run_stage(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra_env: Mapping[str, str],
    extra_args: list[str] | None = None,
) -> tuple[int, str, str | None]:
    ffmpeg = executable(root / "ffmpeg", FAKE_FFMPEG)
    whisper = executable(root / "whisper-cli", FAKE_WHISPER)
    index = root / "index.json"
    write(
        index,
        {
            "schemaVersion": "voice-journey.recording-index.v1",
            "generatedAt": "2026-07-16T00:00:00.000Z",
            "totals": {"recordings": 1},
            "recordings": [
                {
                    "recordingId": "a",
                    "filename": "a.m4a",
                    "capturedAt": "2019-01-01T00:00:00Z",
                    "sourceRef": {"seam": "s", "selector": "a", "filename": "a.m4a"},
                }
            ],
        },
    )
    args_log = root / "args.log"
    monkeypatch.setenv("WHISPER_CPP_BIN", str(whisper))
    monkeypatch.setenv("FFMPEG_BIN", str(ffmpeg))
    monkeypatch.setenv("WHISPER_ARGS_LOG", str(args_log))
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    capsys.readouterr()
    code = main(
        [
            "transcribe-index",
            "--index",
            str(index),
            "--corpus-root",
            str(root),
            "--approval",
            "test",
            "--out",
            str(root / "out.json"),
            "--transcript-dir",
            str(root / "t"),
            "--model",
            "m.bin",
            *(extra_args or []),
        ]
    )
    stderr = capsys.readouterr().err
    args = args_log.read_text(encoding="utf-8").strip() if args_log.exists() else None
    return code, stderr, args


# --- local-stt.test.mjs -----------------------------------------------------------------


def test_stt_dry_run_reports_full_corpus_scope_without_reading_audio() -> None:
    dry_run = build_stt_dry_run(fake_index(), stt_options())
    assert dry_run["operation"] == "local-stt-transcription"
    assert dry_run["recordingCount"] == 2
    assert dry_run["readScope"]["audioBytes"] is True
    assert dry_run["readScope"]["transcriptTextCommittedToRepo"] is False
    assert dry_run["sttTool"]["name"] == "whisper.cpp"
    assert dry_run["resumeBehavior"]["key"] == "recordingId"


def test_stt_dry_run_command_prints_repo_safe_scope(
    local_artifact_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = local_artifact_dir / "index.json"
    write(index_path, fake_index())
    code, stdout = capture_run(
        ["transcribe-index", "--index", str(index_path), "--dry-run"], capsys
    )
    payload = json.loads(stdout)
    assert code == 0
    assert payload["dryRun"] is True
    assert payload["recordingCount"] == 2
    assert payload["readScope"]["upload"] is False
    assert payload["localTranscriptDir"] == "local-artifacts/stt-transcripts"


def test_normalize_transcript_extracts_aggregate_metadata_without_text() -> None:
    normalized = normalize_transcript(
        {
            "result": {"language": "en", "language_probability": 0.91},
            "transcription": [
                {
                    "text": "hello world",
                    "timestamps": {"from": "00:00:00.000", "to": "00:00:02.000"},
                    "avg_logprob": -0.2,
                    "no_speech_prob": 0.01,
                    "compression_ratio": 1.1,
                },
                {
                    "text": "again",
                    "timestamps": {"from": "00:00:02.000", "to": "00:00:03.000"},
                    "avg_logprob": -0.3,
                    "no_speech_prob": 0.02,
                    "compression_ratio": 1.2,
                },
            ],
        },
        "auto",
    )
    assert normalized["language"]["detected"] == "en"
    assert normalized["durationProcessedSeconds"] == 3
    assert normalized["counts"]["words"] == 3
    assert normalized["confidenceStats"]["meanNoSpeechProbability"] == 0.015
    assert normalized["derivedFeatures"]["hasTranscribedWords"] is True


def test_normalize_transcript_parses_comma_timestamps_and_prefers_ms_offsets() -> None:
    comma_only = normalize_transcript(
        {
            "result": {"language": "en"},
            "transcription": [
                {
                    "text": "one two three",
                    "timestamps": {"from": "00:00:00,000", "to": "00:01:05,500"},
                }
            ],
        },
        "auto",
    )
    assert comma_only["durationProcessedSeconds"] == 65.5
    assert comma_only["derivedFeatures"]["wordsPerMinute"] == 2.75
    assert comma_only["derivedFeatures"]["segmentDensityPerMinute"] == 0.92

    offsets_win = normalize_transcript(
        {
            "result": {"language": "en"},
            "transcription": [
                {
                    "text": "la la",
                    "offsets": {"from": 0, "to": 15500},
                    "timestamps": {"from": "00:00:00,000", "to": "00:00:99,000"},
                }
            ],
        },
        "auto",
    )
    assert offsets_win["durationProcessedSeconds"] == 15.5


def test_backfill_manifest_fills_durations_and_rates_without_rerunning_stt(
    local_artifact_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = local_artifact_dir
    transcript_dir = root / "transcripts"
    transcript_dir.mkdir()
    write(
        transcript_dir / "a.json",
        {
            "result": {"language": "en"},
            "transcription": [
                whisper_segment("silver river fixture phrase", 0, 20000),
                whisper_segment("silver river fixture phrase again", 20000, 30000),
            ],
        },
    )
    manifest_path = root / "stt-manifest.json"
    out_path = root / "stt-manifest-backfilled.json"
    null_stats = {
        "meanLogProbability": None,
        "meanNoSpeechProbability": None,
        "meanCompressionRatio": None,
    }
    write(
        manifest_path,
        {
            "schemaVersion": "voice-journey.local-stt-transcripts.v1",
            "updatedAt": "2026-07-16T00:00:00.000Z",
            "repoSafeManifest": {"containsTranscriptText": False},
            "results": [
                {
                    "recordingId": "a",
                    "status": "completed",
                    "durationProcessedSeconds": None,
                    "confidenceStats": null_stats,
                    "counts": {"words": 30, "segments": 2, "characters": 100},
                    "derivedFeatures": {
                        "wordsPerMinute": None,
                        "averageWordsPerSegment": 15,
                        "segmentDensityPerMinute": None,
                        "hasTranscribedWords": True,
                    },
                },
                {
                    "recordingId": "b",
                    "status": "completed",
                    "durationProcessedSeconds": None,
                    "confidenceStats": null_stats,
                    "counts": {"words": 5, "segments": 1, "characters": 20},
                    "derivedFeatures": {
                        "wordsPerMinute": None,
                        "averageWordsPerSegment": 5,
                        "segmentDensityPerMinute": None,
                        "hasTranscribedWords": True,
                    },
                },
            ],
        },
    )

    def backfill(out: Path) -> int:
        code, _ = capture_run(
            [
                "backfill-manifest",
                "--manifest",
                str(manifest_path),
                "--transcript-dir",
                str(transcript_dir),
                "--out",
                str(out),
                "--backfilled-at",
                "2026-07-30T00:00:00.000Z",
            ],
            capsys,
        )
        return code

    assert backfill(out_path) == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    row_a = next(row for row in written["results"] if row["recordingId"] == "a")
    assert row_a["durationProcessedSeconds"] == 30
    assert row_a["derivedFeatures"]["wordsPerMinute"] == 60
    assert row_a["derivedFeatures"]["segmentDensityPerMinute"] == 4
    assert row_a["derivedFeatures"]["averageWordsPerSegment"] == 15
    assert row_a["confidenceStats"] == null_stats
    row_b = next(row for row in written["results"] if row["recordingId"] == "b")
    assert row_b["durationProcessedSeconds"] is None
    assert written["durationBackfill"]["rowsExamined"] == 2
    assert written["durationBackfill"]["rowsUpdatedDuration"] == 1
    assert written["durationBackfill"]["rowsMissingTranscriptJson"] == 1
    assert written["durationBackfill"]["missingRecordingIds"] == ["b"]
    assert written["durationBackfill"]["transcriptTextWrittenToManifest"] is False
    assert written["updatedAt"] == "2026-07-30T00:00:00.000Z"
    assert not re.search("silver river fixture", json.dumps(written))

    first_bytes = out_path.read_text(encoding="utf-8")
    out_path2 = root / "stt-manifest-backfilled-2.json"
    assert backfill(out_path2) == 0
    assert out_path2.read_text(encoding="utf-8") == first_bytes


def test_backfill_manifest_dry_run_reads_only_the_manifest_and_reports_null_fields(
    local_artifact_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = local_artifact_dir
    manifest_path = root / "stt-manifest.json"
    write(
        manifest_path,
        {
            "schemaVersion": "voice-journey.local-stt-transcripts.v1",
            "results": [
                {
                    "recordingId": "a",
                    "status": "completed",
                    "durationProcessedSeconds": None,
                    "confidenceStats": {
                        "meanLogProbability": None,
                        "meanNoSpeechProbability": None,
                        "meanCompressionRatio": None,
                    },
                    "derivedFeatures": {"wordsPerMinute": None, "segmentDensityPerMinute": None},
                }
            ],
        },
    )
    code, stdout = capture_run(
        [
            "backfill-manifest",
            "--manifest",
            str(manifest_path),
            "--transcript-dir",
            str(root / "missing-dir"),
            "--dry-run",
        ],
        capsys,
    )
    payload = json.loads(stdout)
    assert code == 0
    assert payload["dryRun"] is True
    assert payload["operation"] == "local-stt-duration-backfill"
    assert payload["rows"]["total"] == 1
    assert payload["rows"]["durationProcessedSecondsNull"] == 1
    assert payload["rows"]["confidenceStatsAllNull"] == 1
    assert payload["readScope"]["audioBytes"] is False
    assert payload["method"]["rerunsStt"] is False


def test_transcribe_index_resumes_skipping_completed_ids_and_keeps_text_local(
    local_artifact_dir: Path,
) -> None:
    root = local_artifact_dir
    index = fake_index()
    out = root / "stt-manifest.json"
    transcript_dir = root / "transcripts"
    write(
        out,
        {
            "schemaVersion": "voice-journey.local-stt-transcripts.v1",
            "results": [
                {
                    "recordingId": "a",
                    "filename": "20190101 000000-A.m4a",
                    "status": "completed",
                    "localArtifacts": {
                        "transcriptTextPath": "local-artifacts/stt-test/a.txt",
                        "textIncludedInManifest": False,
                    },
                }
            ],
        },
    )

    analyzed: list[str] = []

    def transcribe(entry: dict[str, Any], options: SttOptions) -> TranscriptionResult:
        analyzed.append(entry["recordingId"])
        return {
            "transcriptText": "secret lyric phrase stays local",
            "rawTranscript": {
                "text": "secret lyric phrase stays local",
                "language": "en",
                "durationSeconds": 4,
                "segments": [
                    {
                        "text": "secret lyric phrase stays local",
                        "start": 0,
                        "end": 4,
                        "avg_logprob": -0.1,
                        "no_speech_prob": 0.01,
                        "compression_ratio": 1.05,
                    }
                ],
            },
        }

    manifest = transcribe_index_manifest(
        index,
        stt_options(approval="release-gate-test", out=str(out), transcript_dir=str(transcript_dir)),
        tool_versions={"ffmpeg": "ffmpeg fixture", "whisperCpp": "whisper.cpp fixture"},
        transcribe_recording=transcribe,
    )

    assert analyzed == ["b"]
    assert len(manifest["results"]) == 2
    assert manifest["resume"]["skippedCompletedCount"] == 1
    assert manifest["resume"]["completedCount"] == 2
    assert manifest["resume"]["remainingCount"] == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["repoSafeManifest"]["containsTranscriptText"] is False
    assert not re.search("secret lyric phrase", json.dumps(written))
    assert re.search(
        "secret lyric phrase stays local", (transcript_dir / "b.txt").read_text(encoding="utf-8")
    )


# --- whisper-threads.test.mjs -----------------------------------------------------------


def test_whisper_cpp_threads_unset_whisper_cli_gets_no_t(
    tmp_path: Path,
    fakes: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stderr, args = run_stage(tmp_path, monkeypatch, capsys, {})
    assert code == 0, stderr
    assert args
    assert not re.search(r"(^| )-t( |$)", args)


def test_whisper_cpp_threads_2_whisper_cli_is_invoked_with_t_2(
    tmp_path: Path,
    fakes: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stderr, args = run_stage(tmp_path, monkeypatch, capsys, {"WHISPER_CPP_THREADS": "2"})
    assert code == 0, stderr
    assert args is not None and re.search(r"(^| )-t 2( |$)", args)


def test_threads_flag_wins_over_whisper_cpp_threads(
    tmp_path: Path,
    fakes: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stderr, args = run_stage(
        tmp_path, monkeypatch, capsys, {"WHISPER_CPP_THREADS": "2"}, ["--threads", "3"]
    )
    assert code == 0, stderr
    assert args is not None and re.search(r"(^| )-t 3( |$)", args)
    assert not re.search("-t 2", args)


def test_a_non_integer_whisper_cpp_threads_is_refused_before_whisper_runs(
    tmp_path: Path,
    cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stderr, args = run_stage(tmp_path, monkeypatch, capsys, {"WHISPER_CPP_THREADS": "four"})
    assert code != 0
    assert re.search("WHISPER_CPP_THREADS must be a positive integer", stderr)
    assert args is None


# --- the port's own cases ---------------------------------------------------------------


def test_the_real_stage_writes_text_only_to_the_transcript_dir_and_metadata_to_the_manifest(
    tmp_path: Path,
    fakes: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stderr, _ = run_stage(tmp_path, monkeypatch, capsys, {})
    assert code == 0, stderr
    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert [choice["version"] for choice in written["toolChoices"]] == [
        "fake-ffmpeg",
        "fake-whisper",
    ]
    row = written["results"][0]
    assert row["durationProcessedSeconds"] == 1
    assert row["year"] == "2019"
    assert row["localArtifacts"]["transcriptTextPath"] == str(tmp_path / "t" / "a.txt")
    assert row["localArtifacts"]["gitignoredExpected"] is False
    assert written["localTranscriptStore"]["gitignoredExpected"] is False
    assert (tmp_path / "t" / "a.txt").read_text(encoding="utf-8") == "x"
    assert written["resume"] == {
        "enabled": True,
        "key": "recordingId",
        "outputWrittenAfterEachRecording": True,
        "existingCompletedResultsSkipped": True,
        "skippedCompletedCount": 0,
        "completedCount": 1,
        "remainingCount": 0,
        "lastCompletedRecordingId": "a",
    }


def test_manifest_and_dry_run_fields_follow_the_js_rules(local_artifact_dir: Path) -> None:
    manifest = build_stt_manifest(
        {"recordings": [{"recordingId": "a"}]},
        [],
        stt_options(generated_at="2026-07-16T00:00:00.000Z", transcript_dir="local-artifacts/t"),
    )
    assert manifest["generatedAt"] == "2026-07-16T00:00:00.000Z"
    assert manifest["sourceIndex"] == {"recordingCount": 1}
    assert manifest["localTranscriptStore"]["gitignoredExpected"] is True
    assert manifest["resume"]["remainingCount"] == 1
    assert manifest["resume"]["lastCompletedRecordingId"] is None
    assert manifest["toolChoices"][0]["version"] is None
    assert build_stt_dry_run({}, stt_options(model="models/"))["model"]["defaultSelection"] == (
        "models"
    )
    assert build_stt_dry_run({}, stt_options())["sourceIndex"] == {"recordingCount": None}
    assert build_stt_dry_run([], stt_options())["recordingCount"] == 0


def test_hook_results_are_materialized_into_the_transcript_dir(local_artifact_dir: Path) -> None:
    root = local_artifact_dir
    out = root / "out.json"
    transcript_dir = root / "t"
    elsewhere = root / "given.txt"
    index = {
        "recordings": [
            {"recordingId": "m", "filename": "m.wav", "source": {"type": "measurement"}},
            {"recordingId": "c", "capturedAt": None},
            {"recordingId": "d", "filename": "d.m4a", "capturedAt": "2021-01-01"},
            {"recordingId": "e", "filename": "e.m4a"},
        ]
    }
    results: dict[str, TranscriptionResult] = {
        "c": {"transcriptText": "plain words", "language": "de", "durationSeconds": 2},
        "d": {"rawTranscript": {"segments": [{"text": "one"}, {"text": 2}]}},
        "e": {
            "textPath": str(elsewhere),
            "jsonPath": str(root / "e-given.json"),
            "rawTranscript": {},
        },
    }
    manifest = transcribe_index_manifest(
        index,
        stt_options(approval="ok", out=str(out), transcript_dir=str(transcript_dir)),
        tool_versions={},
        transcribe_recording=lambda entry, options: results[entry["recordingId"]],
    )
    rows = {row["recordingId"]: row for row in manifest["results"]}
    assert list(rows) == ["c", "d", "e"]
    assert "filename" not in rows["c"] and "sourceRef" not in rows["c"]
    assert rows["c"]["year"] == "unknown"
    assert rows["c"]["language"]["requested"] == "auto"
    assert rows["c"]["durationProcessedSeconds"] == 2
    assert (
        rows["c"]["localArtifacts"]["transcriptTextPath"] == "local-artifacts/stt-test-root/t/c.txt"
    )
    assert rows["c"]["localArtifacts"]["gitignoredExpected"] is True
    assert json.loads((transcript_dir / "c.json").read_text(encoding="utf-8")) == {
        "text": "plain words",
        "language": "de",
        "segments": [],
        "durationSeconds": 2,
    }
    assert (transcript_dir / "d.txt").read_text(encoding="utf-8") == "one 2\n"
    assert rows["d"]["year"] == "2021"
    assert not (transcript_dir / "e.txt").exists() and not elsewhere.exists()
    assert manifest["resume"]["remainingCount"] == 1
    assert "plain words" not in out.read_text(encoding="utf-8")


def test_transcribe_index_refuses_without_approval_corpus_root_or_a_file_output(
    cwd: Path,
) -> None:
    with pytest.raises(LocalSttError, match="requires --approval"):
        transcribe_index_manifest(fake_index(), stt_options())
    with pytest.raises(LocalSttError, match="requires --corpus-root"):
        transcribe_index_manifest(fake_index(), stt_options(approval="ok", corpus_root=None))
    with pytest.raises(LocalSttError, match="requires a file output"):
        transcribe_index_manifest(fake_index(), stt_options(approval="ok", out="-"))


def test_tool_failures_are_reported_and_versions_of_missing_tools_are_null(
    tmp_path: Path, fakes: dict[str, Path], cwd: Path
) -> None:
    failing = executable(tmp_path / "failing", "#!/bin/sh\necho boom >&2\nexit 3\n")
    silent = executable(tmp_path / "silent", "#!/bin/sh\nexit 3\n")
    killed = executable(tmp_path / "killed", "#!/bin/sh\nkill -9 $$\n")
    entry = {"recordingId": "a", "filename": "a.m4a"}
    base = stt_options(corpus_root=str(tmp_path), transcript_dir=str(tmp_path / "t"))
    cases = [
        (failing, "ffmpeg failed for a.m4a: boom"),
        (silent, "ffmpeg failed for a.m4a: exit code 3"),
        (killed, "ffmpeg failed for a.m4a: exit code null"),
        (tmp_path / "absent", f"spawn {tmp_path / 'absent'} ENOENT"),
    ]
    for binary, message in cases:
        with pytest.raises(LocalSttError) as raised:
            transcribe_recording_entry(entry, replace(base, ffmpeg_bin=str(binary)))
        assert str(raised.value) == message
    assert stt._read_tool_version(str(tmp_path / "absent"), ["--version"]) is None
    assert stt._read_tool_version(str(silent), ["--version"]) is None
    blank = executable(tmp_path / "blank", "#!/bin/sh\necho '  '\n")
    assert stt._read_tool_version(str(blank), []) is None


def test_backfill_counts_every_kind_of_row(
    local_artifact_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = local_artifact_dir
    transcripts = root / "t"
    transcripts.mkdir()
    (transcripts / "bad.json").write_text("{not json", encoding="utf-8")
    (transcripts / "nan.json").write_text('{"durationSeconds": NaN}', encoding="utf-8")
    write(transcripts / "none.json", {"transcription": [{"text": "x"}]})
    write(
        transcripts / "refresh.json",
        {"segments": [{"end": 12, "avg_logprob": -0.5}, {"t1": "00:10", "avg_logprob": -0.25}]},
    )
    write(transcripts / "same.json", {"duration": 7})
    write(transcripts / "zero.json", {"durationSeconds": 0})
    write(transcripts / "flag.json", {"durationSeconds": 1})
    write(transcripts / "undefined.json", {"durationSeconds": 3})
    rows: list[Any] = [
        {"recordingId": "skipped", "status": "failed"},
        {"recordingId": "bad", "status": "completed"},
        {"recordingId": "nan", "status": "completed"},
        {"recordingId": "none", "status": "completed"},
        {"recordingId": "refresh", "status": "completed", "durationProcessedSeconds": 5},
        {"recordingId": "same", "status": "completed", "durationProcessedSeconds": 7},
        {"recordingId": "zero", "status": "completed"},
        {"recordingId": "flag", "status": "completed", "durationProcessedSeconds": True},
        {"status": "completed"},
    ]
    manifest_path = root / "m.json"
    write(manifest_path, {"results": rows})
    capsys.readouterr()
    assert (
        run(
            [
                "backfill-manifest",
                "--manifest",
                str(manifest_path),
                "--transcript-dir",
                str(transcripts),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == f"{manifest_path}\n"
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = written["durationBackfill"]
    assert {key: value for key, value in report.items() if key.startswith("rows")} == {
        "rowsExamined": 9,
        "rowsSkippedNotCompleted": 1,
        "rowsUpdatedDuration": 2,
        "rowsRefreshedDuration": 2,
        "rowsNoDurationAvailable": 1,
        "rowsMissingTranscriptJson": 0,
        "rowsUnparseableTranscriptJson": 2,
        "rowsConfidenceStatsUpdated": 1,
    }
    by_id = {row.get("recordingId"): row for row in written["results"]}
    assert by_id["refresh"]["durationProcessedSeconds"] == 12
    assert by_id["refresh"]["derivedFeatures"] == {
        "wordsPerMinute": 0,
        "segmentDensityPerMinute": 10,
    }
    assert by_id["refresh"]["confidenceStats"]["meanLogProbability"] == -0.375
    assert by_id["zero"]["derivedFeatures"] == {
        "wordsPerMinute": None,
        "segmentDensityPerMinute": None,
    }
    assert by_id[None]["durationProcessedSeconds"] == 3
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", written["updatedAt"])

    (transcripts / "dir.json").mkdir()
    with pytest.raises(IsADirectoryError):
        backfill_manifest(
            {"results": [{"recordingId": "dir", "status": "completed"}]},
            stt_options(transcript_dir=str(transcripts), out=str(root / "x.json"), out_set=True),
        )


def test_backfill_dry_run_counts_missing_fields_as_null(cwd: Path) -> None:
    payload = build_backfill_dry_run(
        {
            "results": [
                {"status": "completed", "derivedFeatures": {"wordsPerMinute": 3}},
                {
                    "status": "failed",
                    "durationProcessedSeconds": 2,
                    "confidenceStats": {"meanLogProbability": -1},
                },
            ]
        },
        stt_options(manifest="m.json", out="o.json", out_set=True),
    )
    assert payload["outputPath"] == "o.json"
    assert payload["rows"] == {
        "total": 2,
        "completed": 1,
        "durationProcessedSecondsNull": 1,
        "wordsPerMinuteNull": 1,
        "segmentDensityPerMinuteNull": 2,
        "confidenceStatsAllNull": 1,
    }
    assert build_backfill_dry_run({}, stt_options())["rows"]["total"] == 0
    with pytest.raises(TypeError):
        build_backfill_dry_run({"results": [None]}, stt_options())


def test_timestamps_numbers_and_text_follow_javascripts_rules() -> None:
    def duration(segment: Any) -> Any:
        return normalize_transcript({"segments": [segment]})["durationProcessedSeconds"]

    assert duration({"timestamps": {"to": "01:30"}}) == 90
    assert duration({"timestamps": {"to": " 42 "}}) == 42
    assert duration({"timestamps": {"to": "0x10"}}) == 16
    assert duration({"timestamps": {"to": ""}}) == 0
    assert duration({"timestamps": {"to": "1:2:3:4"}}) == 1
    assert duration({"timestamps": {"to": "abc"}}) is None
    assert duration({"timestamps": {"to": "Infinity"}}) is None
    assert duration({"timestamps": {"to": "Infinity:-Infinity:0"}}) is None
    assert duration({"end": 2.5, "t1": 9}) == 2.5
    assert duration({"end": True}) is None
    assert duration({"offsets": {"to": 1e400}, "t1": 4}) == 4
    assert duration({"offsets": {"to": 10**400}, "t1": 5}) == 5
    assert duration({"end": float("inf")}) is None
    assert duration({"end": "1e1"}) == 10
    assert duration({"end": "0b11"}) == 3
    assert duration({"end": "0o17"}) == 15
    assert duration({"end": "+.5e1"}) == 5
    assert (
        normalize_transcript({"durationSeconds": "4", "duration": 9})["durationProcessedSeconds"]
        is None
    )
    with pytest.raises(TypeError):
        normalize_transcript({"segments": [None]})

    texts = normalize_transcript(
        {
            "transcription": [
                {"text": None},
                {"text": True},
                {"text": [1, None, "a"]},
                {"text": {"k": 1}},
                {"text": 1.5},
            ],
            "languageProbability": 0.5,
        },
        None,
    )
    assert texts["counts"] == {"words": 5, "segments": 5, "characters": 29}
    assert texts["language"] == {"requested": "auto", "detected": None, "probability": 0.5}
    assert texts["derivedFeatures"]["averageWordsPerSegment"] == 1

    spaced = normalize_transcript({"text": "\ufeffa\u0085b\u3000c \U0001f3b5 "})
    assert spaced["counts"] == {"words": 3, "segments": 0, "characters": 10}
    assert spaced["derivedFeatures"]["averageWordsPerSegment"] is None
    assert spaced["durationProcessedSeconds"] is None
    assert normalize_transcript("text")["counts"]["words"] == 0


def test_local_path_refs_are_relative_only_under_the_working_directory(cwd: Path) -> None:
    assert stt._local_path_ref(str(cwd / "local-artifacts" / "a.txt")) == "local-artifacts/a.txt"
    assert stt._local_path_ref("/elsewhere/a.txt") == "/elsewhere/a.txt"
    assert stt._local_path_ref(str(cwd)) == ""


def test_arguments_follow_the_node_command_line(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    defaults = parse_args([])
    assert defaults.command == "transcribe-index"
    assert defaults.index == "manifests/recording-index.json"
    assert defaults.transcript_dir == "local-artifacts/stt-transcripts"
    assert defaults.model == "models/ggml-medium.bin"
    assert defaults.threads is None

    data = parse_args(
        ["backfill-manifest"], {"VOICE_JOURNEY_DATA": "/data", "WHISPER_CPP_THREADS": "4"}
    )
    assert data.command == "backfill-manifest"
    assert data.manifest == "/data/manifests/local-stt-transcripts.json"
    assert data.transcript_dir == "/data/artifacts/stt-transcripts"
    assert data.model == "/data/models/ggml-medium.bin"
    assert data.threads == 4

    every = parse_args(
        [
            "--approval", "a", "--backfilled-at", "b", "--corpus-root", "c", "--dry-run",
            "--ffmpeg-bin", "f", "--generated-at", "g", "--index", "i", "--language", "en",
            "--manifest", "m", "--model", "mo", "--out", "o", "--threads", "0x10",
            "--transcript-dir", "t", "--whisper-bin", "w",
        ]
    )  # fmt: skip
    assert (every.approval, every.backfilled_at, every.corpus_root, every.dry_run) == (
        "a", "b", "c", True,
    )  # fmt: skip
    assert (every.ffmpeg_bin, every.generated_at, every.index, every.language) == (
        "f", "g", "i", "en",
    )  # fmt: skip
    assert (every.manifest, every.model, every.out, every.out_set, every.threads) == (
        "m", "mo", "o", True, 16,
    )  # fmt: skip
    assert (every.transcript_dir, every.whisper_bin) == ("t", "w")
    assert parse_args(["--threads", "3.0"]).threads == 3

    for argv, message in [
        (["--threads", "0"], "--threads must be a positive integer"),
        (["--threads", "1.5"], "--threads must be a positive integer"),
        (["--threads", "x"], "--threads must be a positive integer"),
        (["--threads", "Infinity"], "--threads must be a positive integer"),
        (["--index"], "--index requires a value"),
        (["--index", "--dry-run"], "--index requires a value"),
        (["--index", ""], "--index requires a value"),
        (["--nope"], "unsupported argument: --nope"),
        ([""], "unsupported argument: "),
    ]:
        with pytest.raises(LocalSttError, match=re.escape(message)):
            parse_args(argv)

    assert parse_args(["help"]).help is True
    capsys.readouterr()
    assert main(["help"]) == 0
    assert main(["transcribe-index", "--help"]) == 0
    assert capsys.readouterr().out.startswith("Voice Journey local STT tooling.\n")

    assert main(["frobnicate"]) == 1
    assert capsys.readouterr().err == "error: unsupported command: frobnicate\n"
    assert main(["--index", str(cwd / "absent.json"), "--dry-run"]) == 1
    assert "No such file" in capsys.readouterr().err
    (cwd / "nan.json").write_text("[NaN]", encoding="utf-8")
    assert main(["--index", str(cwd / "nan.json"), "--dry-run"]) == 1
    assert "NaN is not valid JSON" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["stt", "--help"])
    assert main() == 0


def test_the_module_runs_as_a_program(cwd: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "cole_voice_lab.journey.stt", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )
    assert completed.returncode == 0, completed.stderr
    assert "backfill-manifest" in completed.stdout

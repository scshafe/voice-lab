"""filter-proof.test.mjs, translated (design §5.3), then the port's own tests: the
vectorized frame analysis against a direct transcription of the JavaScript loops, V8's
``Math.log10``, ICU ``localeCompare`` for ASCII, and the command line with a fake ffmpeg."""

from __future__ import annotations

import functools
import json
import math
import os
import random
import re
import struct
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.journey import filter as vj_filter
from cole_voice_lab.journey.filter import (
    FilterProofError,
    analyze_dry_run,
    analyze_index_dry_run,
    analyze_index_manifest,
    build_results_manifest,
    classify_features,
    main,
    select_sample,
)

GENERATED_AT = "2026-07-16T00:00:00.000Z"


def fake_index() -> dict[str, Any]:
    recordings = []
    for year in ["2019", "2020", "2021"]:
        for suffix, size_bytes in [("A", 100), ("B", 500), ("C", 900)]:
            recordings.append(
                {
                    "recordingId": f"{year}-{suffix}",
                    "filename": f"{year}0101 010203-{suffix}.m4a",
                    "capturedAt": f"{year}-01-01T01:02:03Z",
                    "file": {
                        "sizeBytes": size_bytes,
                        "extension": ".m4a",
                        "modifiedAt": "2026-01-01T00:00:00.000Z",
                    },
                    "sourceRef": {"seam": "voice-journey-corpus", "selector": f"{year}-{suffix}"},
                    "contentClassification": {"singingStatus": "unknown"},
                }
            )
    return {
        "schemaVersion": "voice-journey.recording-index.v1",
        "totals": {"recordings": len(recordings)},
        "recordings": recordings,
    }


def two_recording_index() -> dict[str, Any]:
    index = fake_index()
    recordings = index["recordings"][:2]
    return {**index, "totals": {"recordings": len(recordings)}, "recordings": recordings}


def test_select_sample_chooses_year_and_size_representatives_without_audio() -> None:
    sample = select_sample(fake_index(), generated_at=GENERATED_AT, max=6, per_year=2)
    assert sample["schemaVersion"] == "voice-journey.filtering-sample.v1"
    assert len(sample["sample"]) == 6
    assert [entry["recordingId"] for entry in sample["sample"]] == [
        "2019-A",
        "2019-C",
        "2020-A",
        "2020-C",
        "2021-A",
        "2021-C",
    ]
    assert sample["selectionStrategy"]["audioBytesRead"] is False
    assert sample["releaseGateRequiredForAnalysis"] is True


def test_classify_features_covers_non_singing_clean_singing_and_contamination_buckets() -> None:
    def bucket(**features: Any) -> str:
        result: str = classify_features(features)["finalBucket"]
        return result

    assert (
        bucket(
            activeRatio=0.01,
            voicedRatio=0,
            meanPitchConfidence=0,
            pitchStability=None,
            meanZeroCrossingRate=0.01,
            clippingRatio=0,
        )
        == "non_singing"
    )
    assert (
        bucket(
            activeRatio=0.5,
            voicedRatio=0.5,
            meanPitchConfidence=0.7,
            pitchStability=0.2,
            meanZeroCrossingRate=0.08,
            clippingRatio=0,
        )
        == "clean_singing"
    )
    assert (
        bucket(
            activeRatio=0.5,
            voicedRatio=0.5,
            meanPitchConfidence=0.45,
            pitchStability=0.2,
            meanZeroCrossingRate=0.35,
            clippingRatio=0,
        )
        == "noise_contaminated_singing"
    )
    assert (
        bucket(
            activeRatio=0.95,
            voicedRatio=0.9,
            meanPitchConfidence=0.75,
            pitchStability=0.03,
            meanZeroCrossingRate=0.08,
            clippingRatio=0,
        )
        == "music_contaminated_singing"
    )


def test_analyze_dry_run_reports_sample_only_audio_read_scope() -> None:
    sample = select_sample(fake_index(), generated_at=GENERATED_AT, max=2, per_year=1)
    dry_run = analyze_dry_run(
        sample,
        corpus_root="/tmp/corpus",
        ffmpeg_bin="/opt/homebrew/bin/ffmpeg",
        out="manifests/filter-results.json",
    )
    assert dry_run["sampleCount"] == 2
    assert dry_run["readScope"]["sampleRecordingsOnly"] is True
    assert dry_run["readScope"]["audioBytes"] is True
    assert dry_run["readScope"]["retainedAudioBytes"] is False
    assert dry_run["readScope"]["waveformImages"] is False
    assert dry_run["readScope"]["upload"] is False


def test_select_sample_command_writes_a_repo_safe_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = tmp_path / "index.json"
    out = tmp_path / "sample.json"
    index_path.write_text(json.dumps(fake_index()), encoding="utf-8")
    code = main(
        [
            "select-sample",
            "--index",
            str(index_path),
            "--out",
            str(out),
            "--generated-at",
            GENERATED_AT,
            "--max",
            "4",
        ]
    )
    assert code == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert len(manifest["sample"]) == 4
    assert manifest["selectionStrategy"]["audioBytesRead"] is False
    assert capsys.readouterr().out == f"{out}\n"


def test_results_manifest_records_tool_choices_and_no_retained_audio() -> None:
    sample = select_sample(fake_index(), generated_at=GENERATED_AT, max=1, per_year=1)
    manifest = build_results_manifest(
        sample,
        [],
        generated_at=GENERATED_AT,
        approval="release-gate-test",
        tool_versions={"ffmpeg": "ffmpeg version test"},
    )
    assert manifest["schemaVersion"] == "voice-journey.filter-results.v1"
    assert manifest["readScope"]["audioBytes"] is True
    assert manifest["readScope"]["retainedAudioBytes"] is False
    assert any(
        tool["name"] == "ffmpeg" and tool["openSource"] and tool["version"] == "ffmpeg version test"
        for tool in manifest["toolChoices"]
    )
    assert any(
        tool["name"] == "afinfo/afconvert" and not tool["openSource"]
        for tool in manifest["toolChoices"]
    )


def test_analyze_index_dry_run_reports_full_corpus_resumable_scope() -> None:
    dry_run = analyze_index_dry_run(
        two_recording_index(),
        corpus_root="/tmp/corpus",
        ffmpeg_bin="/opt/homebrew/bin/ffmpeg",
        out="manifests/full-corpus-filter-results.json",
    )
    assert dry_run["operation"] == "full-corpus-classification"
    assert dry_run["recordingCount"] == 2
    assert dry_run["readScope"]["allIndexedRecordings"] is True
    assert dry_run["readScope"]["audioBytes"] is True
    assert dry_run["readScope"]["retainedAudioBytes"] is False
    assert dry_run["resumeBehavior"]["enabled"] is True
    assert dry_run["resumeBehavior"]["key"] == "recordingId"
    assert "frameCount" in dry_run["extractedFeatures"]


def test_analyze_index_resumes_by_skipping_completed_recording_ids(tmp_path: Path) -> None:
    index = two_recording_index()
    out = tmp_path / "full-results.json"
    out.write_text(
        json.dumps(
            {
                "schemaVersion": "voice-journey.full-corpus-filter-results.v1",
                "results": [
                    {
                        "recordingId": index["recordings"][0]["recordingId"],
                        "filename": index["recordings"][0]["filename"],
                        "classification": {"finalBucket": "non_singing"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    analyzed: list[str] = []
    quiet = {
        "activeRatio": 0.01,
        "voicedRatio": 0,
        "meanPitchConfidence": 0,
        "pitchStability": None,
        "meanZeroCrossingRate": 0.01,
        "clippingRatio": 0,
    }

    def analyze_recording(entry: dict[str, Any]) -> dict[str, Any]:
        analyzed.append(entry["recordingId"])
        return {
            "recordingId": entry["recordingId"],
            "filename": entry["filename"],
            "capturedAt": entry["capturedAt"],
            "year": entry["capturedAt"][:4],
            "sourceRef": entry["sourceRef"],
            "features": {
                "durationSeconds": 1,
                "activeRatio": 0.01,
                "voicedRatio": 0,
                "meanRmsDb": -40,
                "meanZeroCrossingRate": 0.01,
                "meanPitchHz": None,
                "meanPitchConfidence": 0,
                "pitchStability": None,
                "clippingRatio": 0,
                "frameCount": 1,
            },
            "classification": classify_features(quiet),
            "humanSpotCheck": {
                "recommended": False,
                "status": "not_reviewed",
                "reviewedBy": None,
                "notes": None,
            },
        }

    manifest = analyze_index_manifest(
        index,
        approval="release-gate-test",
        corpus_root="/tmp/corpus",
        ffmpeg_bin="/opt/homebrew/bin/ffmpeg",
        out=str(out),
        tool_versions={"ffmpeg": "ffmpeg version test"},
        analyze_recording=analyze_recording,
    )

    assert analyzed == [index["recordings"][1]["recordingId"]]
    assert len(manifest["results"]) == 2
    assert manifest["resume"]["skippedCompletedCount"] == 1
    assert manifest["resume"]["completedCount"] == 2
    assert manifest["resume"]["remainingCount"] == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert len(written["results"]) == 2
    assert written["readScope"]["retainedAudioBytes"] is False


# ---------------------------------------------------------------------------- the port


def _js_sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _js_estimate_pitch(frame: list[int], sample_rate: float) -> tuple[float | None, float]:
    """estimatePitch, transcribed loop for loop with double accumulators."""

    min_lag = math.floor(sample_rate / 400)
    max_lag = math.floor(sample_rate / 80)
    best_lag = 0
    best_correlation = 0.0
    energy = 0.0
    for sample in frame:
        energy += sample * sample
    if energy == 0:
        return None, 0.0
    for lag in range(min_lag, max_lag + 1):
        correlation = 0.0
        for index in range(len(frame) - lag):
            correlation += frame[index] * frame[index + lag]
        normalized = correlation / energy
        if normalized > best_correlation:
            best_correlation = normalized
            best_lag = lag
    return (
        sample_rate / best_lag if best_lag else None,
        max(0.0, min(1.0, best_correlation)),
    )


def _js_analyze_frame(frame: list[int], sample_rate: float) -> tuple[Any, ...]:
    """analyzeFrame, transcribed loop for loop."""

    sum_squares = 0.0
    zero_crossings = 0
    for index in range(len(frame)):
        value = frame[index] / 32768
        sum_squares += value * value
        if index > 0 and _js_sign(frame[index - 1]) != _js_sign(frame[index]):
            zero_crossings += 1
    pitch_hz, confidence = _js_estimate_pitch(frame, sample_rate)
    return (math.sqrt(sum_squares / len(frame)), zero_crossings / len(frame), pitch_hz, confidence)


def _js_frames(samples: list[int], sample_rate: float) -> list[tuple[Any, ...]]:
    frames = []
    offset = 0
    while offset + 1024 <= len(samples):
        frames.append(_js_analyze_frame(samples[offset : offset + 1024], sample_rate))
        offset += 512
    return frames


def _bits(values: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(
        struct.pack("<d", value) if isinstance(value, float) else value for value in values
    )


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _clip(value: float) -> int:
    return max(-32768, min(32767, round(value)))


def _sweep(count: int, start_hz: float, end_hz: float, amplitude: float, rate: int) -> list[int]:
    seconds = count / rate
    return [
        _clip(
            amplitude
            * math.sin(
                2
                * math.pi
                * (start_hz * (i / rate) + (end_hz - start_hz) * (i / rate) ** 2 / (2 * seconds))
            )
        )
        for i in range(count)
    ]


def _signals() -> Iterator[tuple[str, list[int], float]]:
    rng = random.Random(20261009)
    count = 512 * 7 + 300
    yield "sine sweep", _sweep(count, 110, 440, 12000, 16000), 16000
    yield "quiet sweep", _sweep(count, 300, 90, 200, 16000), 16000
    yield "full-scale noise", [rng.randint(-32768, 32767) for _ in range(count)], 16000
    yield "silence", [0] * count, 16000
    yield (
        "clipped square",
        [32767 if math.sin(2 * math.pi * 220 * i / 16000) >= 0 else -32768 for i in range(count)],
        16000,
    )
    noisy = _sweep(count, 150, 600, 30000, 16000)
    yield "noisy overdriven sweep", [_clip(v + rng.gauss(0, 4000) * 2) for v in noisy], 16000
    gated = _sweep(count, 200, 250, 9000, 16000)
    yield "gated", [v if (i // 700) % 2 else 0 for i, v in enumerate(gated)], 16000
    yield "dither", [rng.choice((-1, 0, 1)) for _ in range(count)], 16000
    yield "dc", [1200] * count, 16000
    yield "impulses", [32767 if i % 97 == 0 else 0 for i in range(count)], 16000
    yield "8 kHz sweep", _sweep(count, 100, 500, 8000, 8000), 8000
    yield "300 Hz rate (lag 0)", _sweep(2048, 10, 40, 8000, 300), 300
    yield "44.1 kHz sweep", _sweep(2048, 100, 300, 8000, 44100), 44100


@pytest.mark.parametrize(
    ("name", "samples", "rate"), [pytest.param(*case, id=case[0]) for case in _signals()]
)
def test_frame_analysis_is_bit_exact_with_the_javascript_loops(
    name: str, samples: list[int], rate: float
) -> None:
    analysis = vj_filter.analyze_pcm(_pcm(samples) + b"\x01", rate)
    expected = _js_frames(samples, rate)
    assert analysis.sample_count == len(samples)
    assert analysis.clipped == sum(1 for s in samples if abs(s) >= 32760)
    assert len(analysis.frames) == len(expected) > 0
    assert [_bits(tuple(frame)) for frame in analysis.frames] == [_bits(f) for f in expected]


def test_extract_features_matches_a_transcription_of_the_javascript() -> None:
    samples = _sweep(16000 * 2, 140, 320, 14000, 16000)
    samples[5000:9000] = [0] * 4000
    features = vj_filter.extract_features_from_pcm(_pcm(samples))
    assert list(features) == [
        "durationSeconds",
        "activeRatio",
        "voicedRatio",
        "meanRmsDb",
        "meanZeroCrossingRate",
        "meanPitchHz",
        "meanPitchConfidence",
        "pitchStability",
        "clippingRatio",
        "frameCount",
    ]
    frames = vj_filter.analyze_pcm(_pcm(samples)).frames
    active = [f for f in frames if f.rms > 0.01]
    voiced = [f for f in active if f.pitch_confidence >= 0.45]
    pitches = [f.pitch_hz for f in voiced if f.pitch_hz]
    assert features["durationSeconds"] == 2
    assert features["frameCount"] == len(frames) == 61
    assert features["activeRatio"] == len(active) / len(frames)
    assert features["voicedRatio"] == len(voiced) / len(frames)
    assert 0 < len(active) < len(frames)
    assert 1 < len(pitches)
    assert features["clippingRatio"] == 0
    assert features["meanPitchHz"] is not None and 140 <= features["meanPitchHz"] <= 400
    assert classify_features(features)["finalBucket"] in {
        "clean_singing",
        "music_contaminated_singing",
    }


def test_extract_features_of_audio_shorter_than_one_frame() -> None:
    features = vj_filter.extract_features_from_pcm(_pcm([32767, -32768, 5]) + b"\x00")
    assert features == {
        "durationSeconds": 0,
        "activeRatio": 0,
        "voicedRatio": 0,
        "meanRmsDb": 0,
        "meanZeroCrossingRate": 0,
        "meanPitchHz": None,
        "meanPitchConfidence": 0,
        "pitchStability": None,
        "clippingRatio": 0.666667,
        "frameCount": 0,
    }


# V8's Math.log10 (Node 24), as hex: where the C library's log10 differs in the last bit,
# and every branch of fdlibm's log and log10.
LOG10_VECTORS = [
    ("0x1.2b92ebf2bb5cdp-1", "-0x1.dcb4678844f28p-3"),
    ("0x1.b7773b028db5fp-1", "-0x1.0fbfb456a00dap-4"),
    ("0x1.9f656de75fb0fp-2", "-0x1.913dd4b614431p-2"),
    ("0x1.416eebe085123p-1", "-0x1.9e10150ddfdddp-3"),
    ("0x1.4d90c33dd0ef0p-1", "-0x1.7d1c28448454bp-3"),
    ("0x1.6ef2e6fe3bc5bp-1", "-0x1.2845d5275b927p-3"),
    ("0x1.a30c3eae09e0fp-1", "-0x1.6460c8d9cc336p-4"),
    ("0x1.65ce3dba482e7p-1", "-0x1.3eb7298d9ca6fp-3"),
    ("0x1.0000000000000p-1", "-0x1.34413509f79ffp-2"),
    ("0x1.0000000000001p-1", "-0x1.34413509f79fdp-2"),
    ("0x1.fffffffffffffp-1", "-0x1.bcb7b1526e50ep-55"),
    ("0x1.0000000000001p+0", "0x1.bcb7b1526e50dp-54"),
    ("0x1.fffffffffffffp+0", "0x1.34413509f79fep-2"),
    ("0x1.0000000000001p+1", "0x1.34413509f7a01p-2"),
    ("0x1.6a09e667f3bcdp-1", "-0x1.34413509f79fep-3"),
    ("0x1.6a09e667f3bcdp+0", "0x1.34413509f79ffp-3"),
    ("0x0.0000000000001p-1022", "-0x1.434e6420f4374p+8"),
    ("0x1.0000000000000p-1022", "-0x1.33a7146f72a42p+8"),
    ("0x1.7e43c8800759cp+996", "0x1.2c00000000000p+8"),
    ("0x1.47ae147ae147cp-7", "-0x1.0000000000000p+1"),
    ("0x1.4000000000000p+0", "0x1.8cf183886480dp-4"),
    ("0x1.8000000000000p-1", "-0x1.ffbfc2bbc7802p-4"),
    ("0x1.3333333333333p-1", "-0x1.c658a32216008p-3"),
    ("0x1.999999999999ap+0", "0x1.a209a84fbcff8p-3"),
    ("0x1.d65c995d9999ap+0", "0x1.0e885900280eep-2"),
    ("0x1.56b3841b9999ap-1", "-0x1.6513dcf17e8b1p-3"),
    ("0x1.05d4a6f233334p+0", "0x1.407d3f74abd63p-7"),
    ("0x1.4b23378666666p+0", "0x1.c9cdc0bef9d52p-4"),
    ("0x1.47a4c83600000p+0", "0x1.b6ef52a70aa91p-4"),
    ("0x1.3d171ede66667p-1", "-0x1.aa298233889dbp-3"),
    ("0x1.6a7c2a30ccccdp-1", "-0x1.3328a9417969bp-3"),
    ("0x1.0000000000000p+0", "0x0.0p+0"),
    ("0x1.0000000000000p+1", "0x1.34413509f79ffp-2"),
]


def test_log10_is_v8s_fdlibm_log10_bit_for_bit() -> None:
    for argument, expected in LOG10_VECTORS:
        assert vj_filter.js_log10(float.fromhex(argument)).hex() == float.fromhex(expected).hex()
    assert math.isnan(vj_filter.js_log10(math.nan))
    assert math.isnan(vj_filter.js_log10(-1.0))
    assert vj_filter.js_log10(0.0) == -math.inf
    assert vj_filter.js_log10(math.inf) == math.inf


def _node(script: str, payload: Any) -> Any:
    """Run ``script`` with ``input`` (the JSON payload) and return what it prints."""

    source = f"const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n{script}"
    completed = subprocess.run(
        ["node", "-e", source],
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


node_missing = subprocess.run(["which", "node"], capture_output=True).returncode != 0


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_log10_matches_node_on_frame_rms_values() -> None:
    rng = random.Random(7)
    values = [math.sqrt(rng.randrange(1, 2**40) / 2**30 / 1024) for _ in range(20000)]
    node = _node("console.log(JSON.stringify(input.map(Math.log10)))", values)
    assert [vj_filter.js_log10(value) for value in values] == node


def test_locale_compare_is_icu_root_collation_for_ascii() -> None:
    order = "\t _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789aAbBzZ"
    chars = sorted(order, key=lambda c: (ord(c) * 7919) % 101)
    assert "".join(sorted(chars, key=functools.cmp_to_key(vj_filter.locale_compare))) == order
    assert vj_filter.locale_compare("ab", "Aa") == 1
    assert vj_filter.locale_compare("a-b", "ab") == -1
    assert vj_filter.locale_compare("a", "A") == -1
    assert vj_filter.locale_compare("a\x01b", "ab") == 0
    assert vj_filter.locale_compare("é", "é") == 0


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_locale_compare_matches_node_for_ascii_filenames() -> None:
    rng = random.Random(11)
    alphabet = "aAbBzZ09 -_.()'~$"
    names = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 5))) for _ in range(300)]
    node = _node("console.log(JSON.stringify(input.sort((a, b) => a.localeCompare(b))))", names)
    assert sorted(names, key=functools.cmp_to_key(vj_filter.locale_compare)) == node


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4", 4.0),
        (" 4\n", 4.0),
        ("", 0.0),
        ("4.0", 4.0),
        ("1e3", 1000.0),
        ("+.5", 0.5),
        ("0x10", 16.0),
        ("0B11", 3.0),
        ("0o17", 15.0),
        ("0x", math.nan),
        ("0x 1", math.nan),
        ("-0x10", math.nan),
        ("1_000", math.nan),
        ("inf", math.nan),
        ("Infinity", math.inf),
        ("-Infinity", -math.inf),
        ("four", math.nan),
        (None, 0.0),
        (True, 1.0),
        (False, 0.0),
        (vj_filter.UNDEFINED, math.nan),
        ([], math.nan),
    ],
)
def test_js_number(text: Any, expected: float) -> None:
    result = vj_filter.js_number(text)
    assert result == expected or (math.isnan(result) and math.isnan(expected))


def test_js_string() -> None:
    values = [vj_filter.UNDEFINED, None, True, False, 0.5, 1.0, math.nan, math.inf, -math.inf]
    assert [vj_filter.js_string(v) for v in values] == [
        "undefined",
        "null",
        "true",
        "false",
        "0.5",
        "1",
        "NaN",
        "Infinity",
        "-Infinity",
    ]
    assert vj_filter.js_string(["a", None, 2]) == "a,,2"
    assert vj_filter.js_string({"a": 1}) == "[object Object]"
    assert vj_filter.js_string("text") == "text"
    assert repr(vj_filter.UNDEFINED) == "undefined"


def test_classify_features_remaining_branches() -> None:
    uncertain = classify_features(
        {"activeRatio": 0.5, "voicedRatio": 0.3, "meanPitchConfidence": 0.3}
    )
    assert uncertain["contentLabel"] == "uncertain"
    assert uncertain["contaminationLabel"] == "uncertain"
    assert uncertain["finalBucket"] == "uncertain_manual_review"
    assert uncertain["confidence"] == 0.4
    assert uncertain["rationale"] == (
        "content=uncertain; contamination=uncertain; voicedRatio=0.3; pitchConfidence=0.3; "
        "zcr=undefined; activeRatio=0.5"
    )
    singing_unsure = classify_features(
        {
            "activeRatio": 0.5,
            "voicedRatio": 0.5,
            "meanPitchConfidence": 0.5,
            "pitchStability": 0.2,
            "meanZeroCrossingRate": 0.13,
            "clippingRatio": 0,
        }
    )
    assert singing_unsure["finalBucket"] == "uncertain_manual_review"
    assert singing_unsure["contaminationLabel"] == "uncertain"
    speech = classify_features({"activeRatio": 0.5, "voicedRatio": 0.1, "meanPitchConfidence": 0.5})
    assert speech["finalBucket"] == "non_singing"
    assert speech["confidence"] == 0.4
    zero = classify_features({"activeRatio": 0.5, "voicedRatio": 1.5, "meanPitchConfidence": 0.1})
    assert zero["contentLabel"] == "non_singing"
    assert zero["confidence"] == 0
    saturated = classify_features(
        {
            "activeRatio": 1,
            "voicedRatio": 1,
            "meanPitchConfidence": 2,
            "pitchStability": 0,
            "meanZeroCrossingRate": 0,
            "clippingRatio": 0,
        }
    )
    assert saturated["contentLabel"] == "singing"
    assert saturated["confidence"] == 0.807
    broken = classify_features({"activeRatio": 0.5, "voicedRatio": 0.5, "meanPitchConfidence": "x"})
    assert broken["contentLabel"] == "uncertain"
    assert broken["confidence"] == 0.4
    nan_conf = classify_features({"activeRatio": 0.5, "meanPitchConfidence": 0.1})
    assert nan_conf["finalBucket"] == "non_singing"
    assert nan_conf["confidence"] is None
    assert vj_filter._spot_check_recommended(nan_conf) is True


def test_select_sample_edge_cases() -> None:
    index = {
        "schemaVersion": "voice-journey.recording-index.v1",
        "recordings": [
            {"recordingId": "m", "filename": "m.m4a", "source": {"type": "measurement"}},
            {"recordingId": "b", "filename": "b take.m4a", "file": {"sizeBytes": 5}},
            {"recordingId": "a", "filename": "B_take.m4a", "file": None},
            {"recordingId": "c", "filename": "a-take.m4a", "capturedAt": ""},
            {"recordingId": "d", "filename": "x.m4a", "capturedAt": "2020-02-02", "file": {}},
            {"recordingId": "e", "filename": "y.m4a", "capturedAt": "2020-02-02"},
            {"recordingId": "f", "filename": "z.m4a", "capturedAt": "2020-02-02"},
            {"filename": "q.m4a", "capturedAt": "2021-01-01"},
            {"filename": "r.m4a", "capturedAt": "2021-01-01"},
        ],
    }
    sample = select_sample(index, max=8, per_year=3)
    assert sample["sourceIndex"] == {
        "schemaVersion": "voice-journey.recording-index.v1",
        "recordingCount": 9,
    }
    rows = sample["sample"]
    assert [(row.get("recordingId"), row["selectionReason"], row["year"]) for row in rows] == [
        ("d", "small_size_representative_for_year", "2020"),
        ("e", "mid_size_representative_for_year", "2020"),
        ("f", "large_size_representative_for_year", "2020"),
        (None, "small_size_representative_for_year", "2021"),
        ("c", "small_size_representative_for_year", "unknown"),
        ("a", "mid_size_representative_for_year", "unknown"),
        ("b", "large_size_representative_for_year", "unknown"),
    ]
    assert "recordingId" not in rows[3] and rows[4]["capturedAt"] == ""
    assert "capturedAt" not in rows[5]
    assert rows[0]["sizeBytes"] is None and "sourceRef" not in rows[0]
    assert sample["generatedAt"].endswith("Z")
    assert select_sample({"recordings": [index["recordings"][1]]}, per_year=0)["sample"] == []
    only = select_sample({}, max=1, per_year=2)
    assert only["sample"] == [] and only["sourceIndex"] == {"recordingCount": None}
    single = select_sample({"recordings": [index["recordings"][1]]}, per_year=2)
    assert single["sample"][0]["selectionReason"] == "only_recording_for_year"


def test_year_and_stddev_helpers() -> None:
    assert vj_filter._year_for({"capturedAt": 0}) == "unknown"
    assert vj_filter._year_for({"capturedAt": None}) == "unknown"
    assert vj_filter._year_for({"capturedAt": "2024-05-06"}) == "2024"
    for value in (2020, {}):
        with pytest.raises(TypeError, match="slice is not a function"):
            vj_filter._year_for({"capturedAt": value})
    assert vj_filter._stddev([3.0]) == 0
    assert vj_filter._stddev([1.0, 3.0]) == 1.0


def test_dry_runs_without_a_sample_or_recordings() -> None:
    dry = analyze_dry_run({}, corpus_root=None, ffmpeg_bin="ffmpeg", out="-")
    assert dry["sampleCount"] == 0 and dry["sample"] == []
    index_dry = analyze_index_dry_run(
        {"generatedAt": "g"}, corpus_root=None, ffmpeg_bin="ffmpeg", out="-"
    )
    assert index_dry["recordingCount"] == 0
    assert index_dry["sourceIndex"] == {"generatedAt": "g", "recordingCount": None}


def test_path_join_is_node_path_join() -> None:
    join = vj_filter._join
    assert join("/corpus", "a b.m4a") == "/corpus/a b.m4a"
    assert join("corpus/", "/x/../y.m4a") == "corpus/y.m4a"
    assert join("//corpus", "y") == "/corpus/y"
    assert join("", "") == "."


# ---------------------------------------------------------------------------- command line

FAKE_FFMPEG = """#!{python}
import os, sys
log = os.environ.get("FAKE_FFMPEG_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(repr(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["-version"]:
    if os.environ.get("FAKE_FFMPEG_VERSION_FAIL"):
        sys.stderr.write("  no version today \\n")
        sys.exit(2)
    print("ffmpeg version test-1\\nconfiguration: fake")
    sys.exit(0)
source = sys.argv[sys.argv.index("-i") + 1]
if "broken" in source:
    sys.stderr.write("Invalid data found when processing input\\n")
    sys.exit(1)
with open(os.path.splitext(source)[0] + ".pcm", "rb") as pcm:
    sys.stdout.buffer.write(pcm.read())
"""


@pytest.fixture
def fake_ffmpeg(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "ffmpeg"
    path.parent.mkdir()
    path.write_text(FAKE_FFMPEG.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def _corpus(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    singing = _sweep(16000, 180, 260, 12000, 16000)
    (corpus / "2019 song.pcm").write_bytes(_pcm(singing))
    (corpus / "2020 quiet.pcm").write_bytes(_pcm([0] * 4000))
    index = fake_index()
    index["generatedAt"] = "2026-01-01T00:00:00.000Z"
    index["recordings"] = [
        {**index["recordings"][0], "filename": "2019 song.m4a"},
        {**index["recordings"][3], "filename": "2020 quiet.m4a"},
        {**index["recordings"][4], "source": {"type": "measurement"}},
    ]
    index["totals"] = {"recordings": 3}
    return corpus, index


def test_analyze_command_decodes_with_ffmpeg_and_writes_results(
    tmp_path: Path,
    fake_ffmpeg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus, index = _corpus(tmp_path)
    log = tmp_path / "ffmpeg.log"
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(
        json.dumps(select_sample(index, generated_at=GENERATED_AT, per_year=1)), encoding="utf-8"
    )
    out = tmp_path / "results.json"
    code = main(
        [
            "analyze",
            "--sample",
            str(sample_path),
            "--corpus-root",
            str(corpus),
            "--approval",
            "release-gate-test",
            "--ffmpeg-bin",
            str(fake_ffmpeg),
            "--out",
            str(out),
            "--generated-at",
            GENERATED_AT,
        ]
    )
    assert code == 0
    assert capsys.readouterr().out == f"{out}\n"
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["generatedAt"] == GENERATED_AT
    assert manifest["releaseGateApproval"] == "release-gate-test"
    assert manifest["sourceSample"]["sampleCount"] == 2
    assert manifest["toolChoices"][0]["version"] == "ffmpeg version test-1"
    rows = manifest["results"]
    assert [row["recordingId"] for row in rows] == ["2019-A", "2020-A"]
    assert list(rows[0]) == [
        "recordingId",
        "filename",
        "year",
        "sourceRef",
        "features",
        "classification",
        "humanSpotCheck",
    ]
    assert rows[0]["features"]["frameCount"] == 30
    assert rows[1]["classification"]["finalBucket"] == "non_singing"
    assert rows[1]["humanSpotCheck"] == {
        "recommended": False,
        "status": "not_reviewed",
        "notes": None,
    }
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == repr(["-version"])
    assert calls[1] == repr(
        [
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            f"{corpus}/2019 song.m4a",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
        ]
    )


def test_analyze_index_command_analyzes_resumes_and_skips_measurements(
    tmp_path: Path, fake_ffmpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus, index = _corpus(tmp_path)
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    out = tmp_path / "manifests" / "full.json"
    argv = [
        "analyze-index",
        "--index",
        str(index_path),
        "--corpus-root",
        str(corpus),
        "--approval",
        "release-gate-test",
        "--ffmpeg-bin",
        str(fake_ffmpeg),
        "--out",
        str(out),
    ]
    assert main([*argv, "--generated-at", GENERATED_AT]) == 0
    assert capsys.readouterr().out == f"{out}\n" * 3
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["generatedAt"] == GENERATED_AT
    assert manifest["sourceIndex"] == {
        "schemaVersion": "voice-journey.recording-index.v1",
        "generatedAt": "2026-01-01T00:00:00.000Z",
        "recordingCount": 3,
    }
    assert [row["recordingId"] for row in manifest["results"]] == ["2019-A", "2020-A"]
    assert manifest["resume"]["remainingCount"] == 1
    assert manifest["resume"]["lastCompletedRecordingId"] == "2020-A"
    assert list(manifest["resume"])[-1] == "lastCompletedRecordingId"
    first = manifest["results"][0]
    assert first["capturedAt"] == "2019-01-01T01:02:03Z"
    assert first["humanSpotCheck"]["reviewedBy"] is None
    assert first["features"]["meanPitchHz"] is not None

    assert main(argv) == 0
    assert capsys.readouterr().out == f"{out}\n"
    again = json.loads(out.read_text(encoding="utf-8"))
    assert again["resume"]["skippedCompletedCount"] == 2
    assert again["results"] == manifest["results"]
    assert again["generatedAt"] != GENERATED_AT


def test_analyze_index_row_without_a_recording_id(tmp_path: Path) -> None:
    out = tmp_path / "full.json"
    manifest = analyze_index_manifest(
        {"recordings": [{"filename": "a.m4a"}]},
        approval="ok",
        corpus_root="/c",
        ffmpeg_bin="ffmpeg",
        out=str(out),
        tool_versions={},
        analyze_recording=lambda entry: vj_filter.build_result_row(
            entry, {"activeRatio": 0, "voicedRatio": 0, "meanPitchConfidence": 0}
        ),
    )
    assert "lastCompletedRecordingId" not in manifest["resume"]
    assert manifest["results"][0]["capturedAt"] is None
    assert manifest["results"][0]["year"] == "unknown"
    assert manifest["toolChoices"][0]["version"] is None
    again = analyze_index_manifest(
        {"recordings": [{"filename": "a.m4a"}]},
        approval="ok",
        corpus_root="/c",
        ffmpeg_bin="ffmpeg",
        out=str(out),
        tool_versions={},
    )
    assert again["resume"]["lastCompletedRecordingId"] is None


def test_analyze_index_refusals(tmp_path: Path) -> None:
    def call(**overrides: Any) -> dict[str, Any]:
        options: dict[str, Any] = {
            "approval": "ok",
            "corpus_root": "/c",
            "ffmpeg_bin": "ffmpeg",
            "out": str(tmp_path / "o.json"),
        }
        options.update(overrides)
        return analyze_index_manifest({}, **options)

    with pytest.raises(FilterProofError, match="requires --approval"):
        call(approval="")
    with pytest.raises(FilterProofError, match="requires --corpus-root"):
        call(corpus_root=None)
    with pytest.raises(FilterProofError, match="requires a file output"):
        call(out="-")
    with pytest.raises(IsADirectoryError):
        call(out=str(tmp_path))


def test_analyze_refusals_and_tool_failures(
    tmp_path: Path, fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = {"sample": [{"recordingId": "x", "filename": "broken.m4a"}]}
    with pytest.raises(FilterProofError, match="analyze requires --approval"):
        vj_filter.analyze_sample(sample, approval=None, corpus_root="/c", ffmpeg_bin="ffmpeg")
    with pytest.raises(FilterProofError, match="analyze requires --corpus-root"):
        vj_filter.analyze_sample(sample, approval="ok", corpus_root="", ffmpeg_bin="ffmpeg")
    with pytest.raises(
        FilterProofError,
        match="^ffmpeg failed for broken.m4a: Invalid data found when processing input$",
    ):
        vj_filter.analyze_sample(
            sample, approval="ok", corpus_root=str(tmp_path), ffmpeg_bin=str(fake_ffmpeg)
        )
    missing = str(tmp_path / "nowhere" / "ffmpeg")
    with pytest.raises(FilterProofError, match=f"^spawn {re.escape(missing)} ENOENT$"):
        vj_filter.analyze_sample(sample, approval="ok", corpus_root="/c", ffmpeg_bin=missing)
    monkeypatch.setenv("FAKE_FFMPEG_VERSION_FAIL", "1")
    with pytest.raises(FilterProofError, match="^ffmpeg version probe failed: no version today$"):
        vj_filter.read_tool_version(str(fake_ffmpeg), ["-version"])


def test_command_line_help_and_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out == vj_filter.HELP
    assert main(["select-sample", "--help"]) == 0
    assert capsys.readouterr().out.startswith("Voice Journey filtering proof tooling.\n")
    cases = [
        (["--help"], "unsupported command: --help"),
        (["select-sample", "--bogus"], "unsupported argument: --bogus"),
        (["select-sample", "--max"], "--max requires a value"),
        (["select-sample", "--out", "--max"], "--out requires a value"),
        (["select-sample", "--max", "-1"], "--max must be a non-negative integer"),
        (["select-sample", "--per-year", "1.5"], "--per-year must be a non-negative integer"),
        (["select-sample", "--per-year", "Infinity"], "--per-year must be a non-negative integer"),
        (["analyze", "--sample", str(tmp_path / "none.json")], "No such file or directory"),
    ]
    for argv, message in cases:
        assert main(argv) == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("error: ") and message in captured.err, argv
    monkeypatch.setattr(sys, "argv", ["filter", "nothing"])
    assert main() == 1
    assert capsys.readouterr().err == "error: unsupported command: nothing\n"


def test_command_line_options_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICE_JOURNEY_DATA", raising=False)
    monkeypatch.setenv("FFMPEG_BIN", "/opt/ffmpeg")
    options = vj_filter.parse_args(["analyze-index"])
    assert options.out == os.path.join("manifests", "full-corpus-filter-results.json")
    assert options.index == os.path.join("manifests", "recording-index.json")
    assert options.ffmpeg_bin == "/opt/ffmpeg"
    assert vj_filter.parse_args(["analyze"]).out.endswith("filter-results.json")
    assert vj_filter.parse_args([]).out.endswith("filtering-sample.json")
    parsed = vj_filter.parse_args(
        [
            "select-sample",
            "--max",
            "0x3",
            "--per-year",
            " 2 ",
            "--dry-run",
            "--sample",
            "s.json",
            "--approval",
            "a",
            "--corpus-root",
            "c",
            "--ffmpeg-bin",
            "f",
            "help",
        ]
    )
    assert (parsed.max, parsed.per_year, parsed.dry_run, parsed.help) == (3, 2, True, True)
    assert (parsed.sample, parsed.approval, parsed.corpus_root, parsed.ffmpeg_bin) == (
        "s.json",
        "a",
        "c",
        "f",
    )


def test_dry_run_commands_print_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(two_recording_index()), encoding="utf-8")
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(
        json.dumps(select_sample(two_recording_index(), generated_at=GENERATED_AT)),
        encoding="utf-8",
    )
    assert main(["analyze", "--dry-run", "--sample", str(sample_path), "--out", "o.json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["outputPath"] == "o.json" and dry["corpusRoot"] is None
    assert dry["sample"][0] == {
        "recordingId": "2019-A",
        "filename": "20190101 010203-A.m4a",
        "year": "2019",
        "sizeBytes": 100,
    }
    assert main(["analyze-index", "--dry-run", "--index", str(index_path)]) == 0
    assert json.loads(capsys.readouterr().out)["recordingCount"] == 2
    assert main(["select-sample", "--index", str(index_path), "--out", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["selectionStrategy"]["max"] == 18


def test_module_runs_as_a_program() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "cole_voice_lab.journey.filter", "bogus"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stderr == "error: unsupported command: bogus\n"


def test_dsp_tool_choice_without_numpy_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise vj_filter.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(vj_filter.metadata, "version", missing)
    assert vj_filter._dsp_version().endswith("NumPy not installed")

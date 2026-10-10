"""The singing filter: sample selection, frame heuristic and classification (filter-proof.mjs).

Commands, flags, exit codes, refusals and manifests are filter-proof.mjs's. The frame
heuristic (``extractFeaturesFromPcm``, ``analyzeFrame``, ``estimatePitch``) is
vectorized with NumPy (the ``journey`` extra, imported inside :func:`analyze_pcm` only)
and is bit-exact with the JavaScript:

- Every per-frame sum is a sum of int16 products. NumPy computes them in int64, which is
  exact. The JavaScript double sums are exact too: each partial sum is an integer of
  magnitude at most ``1024 * 2**30 = 2**40 < 2**53``.
- ``sumSquares`` adds ``(s / 32768) ** 2 = s*s / 2**30`` sequentially. Each term and each
  partial sum is an integer over ``2**30`` with a numerator below ``2**53``, so every
  addition is exact and the JavaScript sum equals ``energy / 2**30`` (``energy`` the
  integer sum of squares); the RMS is ``sqrt(energy / 2**30 / 1024)``, two exact
  power-of-two divisions and one correctly rounded square root.
- ``correlation / energy`` is one correctly rounded division of exactly represented
  integers, compared lag by lag with ``>`` in lag order, exactly as the JavaScript loop.

Everything accumulated over frames (means, the standard deviation) is plain Python in
the JavaScript order (never ``sum()``, which compensates its rounding on Python 3.12).
``Math.log10`` is V8's fdlibm ``log10`` (``jscompat.log10``); the C library's ``log10``
differs from it in the last bit for about 6% of the RMS values (measured against Node 24).
The other JavaScript value rules (``Number()``, ``String()``, ``??``, ``undefined``) are
``journey/jscompat.py``'s.

Differences from the JavaScript, all deliberate:

- The DSP entry of ``toolChoices`` names this Python implementation and its versions
  instead of ``Node.js standard library DSP heuristics`` and ``process.version``.
- ``localeCompare`` (the tie-break between recordings of equal size in a year) is ICU
  root collation, exact for ASCII (``jscompat.locale_compare``). Other characters sort
  after ASCII by code point, where ICU would interleave them.
- Error messages from failures outside this module (an unreadable or invalid JSON file)
  are Python's; the exit code is the same.
"""

from __future__ import annotations

import errno
import functools
import math
import platform
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from typing import Any, NamedTuple

from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey import jsonio
from cole_voice_lab.journey.paths import resolve_paths, resolve_tools

SAMPLE_SCHEMA_VERSION = "voice-journey.filtering-sample.v1"
RESULTS_SCHEMA_VERSION = "voice-journey.filter-results.v1"
FULL_CORPUS_RESULTS_SCHEMA_VERSION = "voice-journey.full-corpus-filter-results.v1"
SAMPLE_RATE = 16000
FRAME_SIZE = 1024
HOP_SIZE = 512
_FRAMES_PER_CHUNK = 128

Json = dict[str, Any]
AnalyzeRecording = Callable[[Json], Json]


class FilterProofError(Exception):
    """A refusal; the command line prints ``error: <message>`` and exits 1."""


def _clamp(value: float) -> float:
    """``Math.max(0, Math.min(1, value))``."""

    if value != value:
        return value
    if value >= 1:
        return 1.0
    if value <= 0:
        return 0.0
    return value


def _round(value: float | int, places: int = 3) -> float | None:
    return jsonio.to_fixed(value, places)


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0
    total = 0.0
    for value in values:
        total += value
    return total / len(values)


def _stddev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0
    average = _mean(values)
    return math.sqrt(_mean([(value - average) * (value - average) for value in values]))


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0


# ---------------------------------------------------------------------------- options


@dataclass
class Options:
    command: str | None
    approval: str | None = None
    corpus_root: str | None = None
    dry_run: bool = False
    ffmpeg_bin: str = "ffmpeg"
    generated_at: str | None = None
    index: str = ""
    max: int = 18
    out: str = ""
    per_year: int = 2
    sample: str = ""
    help: bool = False


def parse_args(argv: Sequence[str]) -> Options:
    paths = resolve_paths()
    tools = resolve_tools(None, paths)
    command = argv[0] if argv else None
    if command == "analyze-index":
        out = paths.manifest("full-corpus-filter-results.json")
    elif command == "analyze":
        out = paths.manifest("filter-results.json")
    else:
        out = paths.manifest("filtering-sample.json")
    options = Options(
        command=command,
        ffmpeg_bin=tools.ffmpeg_bin,
        index=str(paths.manifest("recording-index.json")),
        out=str(out),
        sample=str(paths.manifest("filtering-sample.json")),
    )
    args = list(argv[1:])
    while args:
        flag = args.pop(0)
        if flag == "--approval":
            options.approval = _require_value(args, flag)
        elif flag == "--corpus-root":
            options.corpus_root = _require_value(args, flag)
        elif flag == "--dry-run":
            options.dry_run = True
        elif flag == "--ffmpeg-bin":
            options.ffmpeg_bin = _require_value(args, flag)
        elif flag == "--generated-at":
            options.generated_at = _require_value(args, flag)
        elif flag == "--index":
            options.index = _require_value(args, flag)
        elif flag == "--max":
            options.max = _parse_non_negative_integer(_require_value(args, flag), flag)
        elif flag == "--out":
            options.out = _require_value(args, flag)
        elif flag == "--per-year":
            options.per_year = _parse_non_negative_integer(_require_value(args, flag), flag)
        elif flag == "--sample":
            options.sample = _require_value(args, flag)
        elif flag in ("--help", "help"):
            options.help = True
        else:
            raise FilterProofError(f"unsupported argument: {flag}")
    return options


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else ""
    if not value or value.startswith("--"):
        raise FilterProofError(f"{flag} requires a value")
    return value


def _parse_non_negative_integer(value: str, flag: str) -> int:
    parsed = js.to_number(value)
    if not math.isfinite(parsed) or not parsed.is_integer() or parsed < 0:
        raise FilterProofError(f"{flag} must be a non-negative integer")
    return int(parsed)


HELP = """Voice Journey filtering proof tooling.

Usage:
  voice-journey-filter select-sample [--index PATH] [--out PATH] [--per-year N] [--max N]
  voice-journey-filter analyze --dry-run [--sample PATH] [--corpus-root PATH]
  voice-journey-filter analyze --sample PATH --corpus-root PATH --approval TEXT [--out PATH] [--ffmpeg-bin PATH]
  voice-journey-filter analyze-index --dry-run [--index PATH] [--corpus-root PATH]
  voice-journey-filter analyze-index --index PATH --corpus-root PATH --approval TEXT [--out PATH] [--ffmpeg-bin PATH]

select-sample and analyze-index --dry-run read only committed manifests. analyze
and analyze-index non-dry-run read audio bytes through the approved host
execution path and therefore require release-gate approval before use.
"""  # noqa: E501


# ---------------------------------------------------------------------------- selection


def _year_for(entry: Json) -> str:
    captured_at = js.get(entry, "capturedAt")
    if not js.truthy(captured_at):
        return "unknown"
    if not isinstance(captured_at, str):
        raise TypeError("entry.capturedAt.slice is not a function")
    return captured_at[:4]


def _compare_by_size_then_name(left: Json, right: Json) -> int:
    difference = js.to_number(
        js.coalesce(js.get(js.get(left, "file"), "sizeBytes"), 0)
    ) - js.to_number(js.coalesce(js.get(js.get(right, "file"), "sizeBytes"), 0))
    if difference == difference and difference != 0:
        return -1 if difference < 0 else 1
    return js.locale_compare(left["filename"], right["filename"])


def select_sample(
    index_manifest: Json,
    *,
    generated_at: str | None = None,
    max: int = 18,
    per_year: int = 2,
) -> Json:
    if generated_at is None:
        generated_at = jsonio.iso_timestamp()
    by_year: dict[str, list[Json]] = {}
    recordings = js.coalesce(js.get(index_manifest, "recordings"), [])
    for recording in recordings:
        if js.get(js.get(recording, "source"), "type") == "measurement":
            continue
        by_year.setdefault(_year_for(recording), []).append(recording)

    selected: list[tuple[Json, str]] = []
    seen: set[Any] = set()
    for year in sorted(by_year, key=js.utf16_key):
        entries = sorted(by_year[year], key=functools.cmp_to_key(_compare_by_size_then_name))
        for index in _candidate_indexes_for(len(entries), per_year):
            entry = entries[index]
            recording_id = js.get(entry, "recordingId")
            if recording_id in seen:
                continue
            seen.add(recording_id)
            selected.append((entry, _sample_reason(index, len(entries))))

    return {
        "schemaVersion": SAMPLE_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "sourceIndex": js.defined(
            schemaVersion=js.get(index_manifest, "schemaVersion"),
            recordingCount=js.coalesce(
                js.get(js.get(index_manifest, "totals"), "recordings"),
                js.length(js.get(index_manifest, "recordings")),
                None,
            ),
        ),
        "selectionStrategy": {
            "kind": "metadata_only_year_size_stratified",
            "perYear": per_year,
            "max": max,
            "inputs": ["capturedAt year", "file.sizeBytes", "filename"],
            "audioBytesRead": False,
            "notes": (
                "For each year, select small/large size representatives from the committed "
                "index only."
            ),
        },
        "releaseGateRequiredForAnalysis": True,
        "sample": [
            js.defined(
                sampleOrder=order + 1,
                recordingId=js.get(entry, "recordingId"),
                filename=js.get(entry, "filename"),
                capturedAt=js.get(entry, "capturedAt"),
                year=_year_for(entry),
                sizeBytes=js.coalesce(js.get(js.get(entry, "file"), "sizeBytes"), None),
                selectionReason=reason,
                sourceRef=js.get(entry, "sourceRef"),
                indexClassification=js.get(entry, "contentClassification"),
            )
            for order, (entry, reason) in enumerate(selected[:max])
        ],
    }


def _candidate_indexes_for(length: int, per_year: int) -> list[int]:
    if length <= 0 or per_year <= 0:
        return []
    if per_year == 1 or length == 1:
        return [0]
    last = length - 1
    indexes = {
        int(jsonio.js_round((position * last) / (per_year - 1))) for position in range(per_year)
    }
    return sorted(indexes)


def _sample_reason(index: int, length: int) -> str:
    if length <= 1:
        return "only_recording_for_year"
    if index == 0:
        return "small_size_representative_for_year"
    if index == length - 1:
        return "large_size_representative_for_year"
    return "mid_size_representative_for_year"


# ---------------------------------------------------------------------------- dry runs


def _architecture() -> Json:
    return {
        "slug": "kickoff-corpus-flow",
        "reads": [
            "recording-index-manifest",
            "index-feeds-filtering",
            "host-access-seam",
            "seam-provides-filter-audio",
        ],
        "writes": ["filter-results-manifest", "filtering-writes-results"],
    }


def _feature_names() -> list[str]:
    return [
        "durationSeconds",
        "activeRatio",
        "voicedRatio",
        "meanRmsDb",
        "meanZeroCrossingRate",
        "meanPitchHz",
        "meanPitchConfidence",
        "pitchStability",
        "clippingRatio",
    ]


def extracted_feature_names() -> list[str]:
    return [*_feature_names(), "frameCount"]


def analyze_dry_run(
    sample_manifest: Json, *, corpus_root: str | None, ffmpeg_bin: str, out: str
) -> Json:
    sample = js.get(sample_manifest, "sample")
    return {
        "dryRun": True,
        "operation": "filtering-proof-sample-analysis",
        "architecture": _architecture(),
        "corpusRoot": corpus_root,
        "sampleCount": js.coalesce(js.length(sample), 0),
        "ffmpegBin": ffmpeg_bin,
        "outputPath": out,
        "readScope": {
            "sampleRecordingsOnly": True,
            "audioBytes": True,
            "decodeToMonoPcm": True,
            "retainedAudioBytes": False,
            "waveformImages": False,
            "spectrogramImages": False,
            "transcription": False,
            "upload": False,
            "corpusMutation": False,
        },
        "extractedFeatures": _feature_names(),
        "sample": [
            js.defined(
                recordingId=js.get(entry, "recordingId"),
                filename=js.get(entry, "filename"),
                year=js.get(entry, "year"),
                sizeBytes=js.get(entry, "sizeBytes"),
            )
            for entry in (sample if isinstance(sample, list) else [])
        ],
    }


def analyze_index_dry_run(
    index_manifest: Json, *, corpus_root: str | None, ffmpeg_bin: str, out: str
) -> Json:
    recordings = js.length(js.get(index_manifest, "recordings"))
    return {
        "dryRun": True,
        "operation": "full-corpus-classification",
        "architecture": _architecture(),
        "corpusRoot": corpus_root,
        "recordingCount": js.coalesce(recordings, 0),
        "sourceIndex": js.defined(
            schemaVersion=js.get(index_manifest, "schemaVersion"),
            generatedAt=js.get(index_manifest, "generatedAt"),
            recordingCount=js.coalesce(
                js.get(js.get(index_manifest, "totals"), "recordings"), recordings, None
            ),
        ),
        "ffmpegBin": ffmpeg_bin,
        "outputPath": out,
        "expectedRuntime": (
            "approximately one hour for 2,433 recordings on the current host inventory"
        ),
        "resumeBehavior": {
            "enabled": True,
            "key": "recordingId",
            "outputWrittenAfterEachRecording": True,
            "existingResultsSkipped": True,
            "rerunCommandIsSameAsInitialCommand": True,
        },
        "readScope": {
            "allIndexedRecordings": True,
            "audioBytes": True,
            "decodeToMonoPcm": True,
            "retainedAudioBytes": False,
            "waveformImages": False,
            "spectrogramImages": False,
            "transcription": False,
            "upload": False,
            "corpusMutation": False,
        },
        "extractedFeatures": extracted_feature_names(),
    }


# ---------------------------------------------------------------------------- analysis


def _spawn(command: str, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [command, *args], stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "UNKNOWN")
        raise FilterProofError(f"spawn {command} {code}") from error


def read_tool_version(command: str, args: Sequence[str]) -> str:
    completed = _spawn(command, args)
    if completed.returncode != 0:
        stderr = js.trim(completed.stderr.decode("utf-8", "replace"))
        raise FilterProofError(f"{js.path_basename(command)} version probe failed: {stderr}")
    return completed.stdout.decode("utf-8", "replace").split("\n")[0]


def decode_pcm_with_ffmpeg(audio_path: str, ffmpeg_bin: str) -> bytes:
    completed = _spawn(
        ffmpeg_bin,
        [
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            audio_path,
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ],
    )
    if completed.returncode != 0:
        stderr = js.trim(completed.stderr.decode("utf-8", "replace"))
        raise FilterProofError(f"ffmpeg failed for {js.path_basename(audio_path)}: {stderr}")
    return completed.stdout


class Frame(NamedTuple):
    """``analyzeFrame``'s result for one 1024-sample frame."""

    rms: float
    zero_crossing_rate: float
    pitch_hz: float | None
    pitch_confidence: float


class PcmAnalysis(NamedTuple):
    sample_count: int
    clipped: int
    frames: list[Frame]


def analyze_pcm(buffer: bytes, sample_rate: float = SAMPLE_RATE) -> PcmAnalysis:
    """Decode s16le PCM and analyze every frame (1024 samples, hop 512), bit-exact with
    filter-proof.mjs's ``analyzeFrame``/``estimatePitch`` (see the module docstring)."""

    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    sample_count = len(buffer) // 2
    samples = np.frombuffer(buffer, dtype="<i2", count=sample_count).astype(np.int64)
    clipped = int(np.count_nonzero(np.abs(samples) >= 32760))
    if sample_count < FRAME_SIZE:
        return PcmAnalysis(sample_count, clipped, [])
    frame_count = (sample_count - FRAME_SIZE) // HOP_SIZE + 1
    offsets = np.arange(frame_count, dtype=np.int64) * HOP_SIZE

    squares = np.zeros(sample_count + 1, dtype=np.int64)
    np.cumsum(samples * samples, out=squares[1:])
    energy = squares[offsets + FRAME_SIZE] - squares[offsets]
    rms = np.sqrt(energy.astype(np.float64) / 2.0**30 / FRAME_SIZE)

    signs = np.sign(samples)
    changes = np.zeros(sample_count, dtype=np.int64)
    np.cumsum(signs[1:] != signs[:-1], dtype=np.int64, out=changes[1:])
    zero_crossing_rate = (changes[offsets + FRAME_SIZE - 1] - changes[offsets]) / FRAME_SIZE

    min_lag = math.floor(sample_rate / 400)
    max_lag = math.floor(sample_rate / 80)
    lags = range(max(min_lag, 0), min(max_lag, FRAME_SIZE - 1) + 1)
    best = np.zeros(frame_count, dtype=np.float64)
    best_lag = np.zeros(frame_count, dtype=np.int64)
    divisor = np.where(energy == 0, 1, energy).astype(np.float64)
    windows = sliding_window_view(samples, FRAME_SIZE)[::HOP_SIZE]
    for start in range(0, frame_count, _FRAMES_PER_CHUNK):
        stop = min(start + _FRAMES_PER_CHUNK, frame_count)
        block = np.ascontiguousarray(windows[start:stop])
        chunk_best = best[start:stop]
        chunk_lag = best_lag[start:stop]
        chunk_divisor = divisor[start:stop]
        for lag in lags:
            correlation = np.einsum("ij,ij->i", block[:, : FRAME_SIZE - lag], block[:, lag:])
            normalized = correlation.astype(np.float64) / chunk_divisor
            better = normalized > chunk_best
            chunk_best[better] = normalized[better]
            chunk_lag[better] = lag

    frames = [
        Frame(
            rms=frame_rms,
            zero_crossing_rate=frame_zcr,
            pitch_hz=sample_rate / lag if lag else None,
            pitch_confidence=_clamp(correlation),
        )
        for frame_rms, frame_zcr, lag, correlation in zip(
            rms.tolist(),
            zero_crossing_rate.tolist(),
            best_lag.tolist(),
            best.tolist(),
            strict=True,
        )
    ]
    return PcmAnalysis(sample_count, clipped, frames)


def extract_features_from_pcm(buffer: bytes, sample_rate: float = SAMPLE_RATE) -> Json:
    """``extractFeaturesFromPcm``."""

    analysis = analyze_pcm(buffer, sample_rate)
    frames = analysis.frames
    active = [frame for frame in frames if frame.rms > 0.01]
    voiced = [frame for frame in active if frame.pitch_confidence >= 0.45]
    pitch_values = [frame.pitch_hz for frame in voiced if frame.pitch_hz]
    return {
        "durationSeconds": _round(analysis.sample_count / sample_rate, 3),
        "activeRatio": _ratio(len(active), len(frames)),
        "voicedRatio": _ratio(len(voiced), len(frames)),
        "meanRmsDb": _round(
            _mean([20 * js.log10(frame.rms if frame.rms else 1e-9) for frame in active]), 2
        ),
        "meanZeroCrossingRate": _round(_mean([frame.zero_crossing_rate for frame in active]), 4),
        "meanPitchHz": _round(_mean(pitch_values), 1) if pitch_values else None,
        "meanPitchConfidence": _round(_mean([frame.pitch_confidence for frame in voiced]), 3),
        "pitchStability": (
            _round(_stddev(pitch_values) / max(_mean(pitch_values), 1), 3)
            if len(pitch_values) > 1
            else None
        ),
        "clippingRatio": _round(_ratio(analysis.clipped, analysis.sample_count), 6),
        "frameCount": len(frames),
    }


def classify_features(features: Mapping[str, Any]) -> Json:
    """``classifyFeatures``: the content, contamination and final bucket."""

    active_ratio = js.to_number(js.get(features, "activeRatio"))
    voiced_ratio = js.to_number(js.get(features, "voicedRatio"))
    pitch_confidence = js.to_number(js.get(features, "meanPitchConfidence"))
    zero_crossing_rate = js.to_number(js.get(features, "meanZeroCrossingRate"))
    clipping_ratio = js.to_number(js.get(features, "clippingRatio"))
    pitch_stability = js.to_number(js.coalesce(js.get(features, "pitchStability"), 1))

    content_label = "uncertain"
    content_confidence = 0.45
    if active_ratio < 0.04:
        content_label = "non_singing"
        content_confidence = 0.7
    elif voiced_ratio >= 0.42 and pitch_confidence >= 0.45 and pitch_stability <= 0.38:
        content_label = "singing"
        content_confidence = _clamp(
            0.55 + voiced_ratio * 0.25 + pitch_confidence * 0.2 - pitch_stability * 0.15
        )
    elif voiced_ratio < 0.22 or pitch_confidence < 0.25:
        content_label = "non_singing"
        content_confidence = _clamp(0.5 + (0.25 - pitch_confidence) + (0.25 - voiced_ratio))

    contamination_label = "not_applicable"
    contamination_confidence = content_confidence if content_label == "non_singing" else 0.4
    if content_label == "singing":
        noise_score = _clamp(
            (zero_crossing_rate - 0.12) * 3 + (1 - pitch_confidence) * 0.4 + clipping_ratio * 10
        )
        music_score = _clamp(
            (active_ratio - 0.78) * 1.1
            + (voiced_ratio - 0.65) * 0.9
            + (0.25 if pitch_stability < 0.08 else 0)
        )
        if music_score >= 0.62:
            contamination_label = "music_contaminated"
            contamination_confidence = music_score
        elif noise_score >= 0.55:
            contamination_label = "noise_contaminated"
            contamination_confidence = noise_score
        elif pitch_confidence >= 0.55 and zero_crossing_rate <= 0.14:
            contamination_label = "clean"
            contamination_confidence = _clamp(0.55 + pitch_confidence * 0.3 - zero_crossing_rate)
        else:
            contamination_label = "uncertain"
            contamination_confidence = 0.45
    elif content_label == "uncertain":
        contamination_label = "uncertain"

    confidence = js.js_min(
        content_confidence,
        content_confidence if contamination_confidence == 0 else contamination_confidence,
    )
    return {
        "method": "voice-journey.local-heuristic-proof.v1",
        "contentLabel": content_label,
        "contaminationLabel": contamination_label,
        "finalBucket": _final_bucket_for(content_label, contamination_label),
        "confidence": _round(confidence, 3),
        "rationale": (
            f"content={content_label}; contamination={contamination_label}; "
            f"voicedRatio={js.to_string(js.get(features, 'voicedRatio'))}; "
            f"pitchConfidence={js.to_string(js.get(features, 'meanPitchConfidence'))}; "
            f"zcr={js.to_string(js.get(features, 'meanZeroCrossingRate'))}; "
            f"activeRatio={js.to_string(js.get(features, 'activeRatio'))}"
        ),
    }


def _final_bucket_for(content_label: str, contamination_label: str) -> str:
    if content_label == "non_singing":
        return "non_singing"
    if content_label != "singing":
        return "uncertain_manual_review"
    if contamination_label == "clean":
        return "clean_singing"
    if contamination_label == "noise_contaminated":
        return "noise_contaminated_singing"
    if contamination_label == "music_contaminated":
        return "music_contaminated_singing"
    return "uncertain_manual_review"


def _spot_check_recommended(classification: Json) -> bool:
    return (
        classification["finalBucket"] == "uncertain_manual_review"
        or js.to_number(classification["confidence"]) < 0.7
    )


# ---------------------------------------------------------------------------- manifests


def _dsp_version() -> str:
    try:
        numpy_version = metadata.version("numpy")
    except metadata.PackageNotFoundError:
        numpy_version = "not installed"
    return f"Python {platform.python_version()}; NumPy {numpy_version}"


def _ffmpeg_choice(version: Any, scope: str) -> Json:
    return {
        "name": "ffmpeg",
        "version": version,
        "stage": "decode-to-mono-pcm-feature-input",
        "openness": "open-source",
        "openSource": True,
        "openSwapCandidate": None,
        "notes": (
            f"Host-provided ffmpeg decodes {scope} to transient PCM for aggregate feature "
            "extraction only."
        ),
    }


def _dsp_choice(scope: str) -> Json:
    return {
        "name": "Python standard library and NumPy DSP heuristics",
        "version": _dsp_version(),
        "stage": "feature-extraction-and-classification-proof",
        "openness": "open-source-runtime-project-code",
        "openSource": True,
        "openSwapCandidate": (
            "Essentia, librosa, or pyAudioAnalysis local classifier if the proof needs "
            "stronger accuracy."
        ),
        "notes": (
            "Bit-exact port of the Node.js frame heuristic; no closed-source analyzer is "
            f"required for this {scope}."
        ),
    }


def _afinfo_choice() -> Json:
    return {
        "name": "afinfo/afconvert",
        "version": None,
        "stage": "not-used-fallback",
        "openness": "closed-source-macos-coreaudio",
        "openSource": False,
        "openSwapCandidate": "ffmpeg/ffprobe",
        "notes": "Available on host but intentionally not used in the preferred open-source path.",
    }


def _buckets() -> Json:
    return {
        "content": ["singing", "non_singing", "uncertain"],
        "contamination": [
            "clean",
            "noise_contaminated",
            "music_contaminated",
            "not_applicable",
            "uncertain",
        ],
        "final": [
            "non_singing",
            "clean_singing",
            "noise_contaminated_singing",
            "music_contaminated_singing",
            "uncertain_manual_review",
        ],
    }


def build_results_manifest(
    sample_manifest: Json,
    results: list[Json],
    *,
    generated_at: str | None = None,
    approval: str | None = None,
    tool_versions: Mapping[str, Any] | None = None,
) -> Json:
    return {
        "schemaVersion": RESULTS_SCHEMA_VERSION,
        "generatedAt": generated_at if generated_at is not None else jsonio.iso_timestamp(),
        "sourceSample": js.defined(
            schemaVersion=js.get(sample_manifest, "schemaVersion"),
            generatedAt=js.get(sample_manifest, "generatedAt"),
            sampleCount=js.coalesce(js.length(js.get(sample_manifest, "sample")), 0),
        ),
        "releaseGateApproval": approval,
        "readScope": {
            "sampleRecordingsOnly": True,
            "audioBytes": True,
            "retainedAudioBytes": False,
            "audibleDerivedArtifacts": False,
            "upload": False,
            "corpusMutation": False,
        },
        "toolChoices": [
            _ffmpeg_choice(js.coalesce(js.get(tool_versions, "ffmpeg"), None), "sample audio"),
            _dsp_choice("sample proof implementation"),
            _afinfo_choice(),
        ],
        "buckets": _buckets(),
        "results": results,
    }


def build_full_corpus_results_manifest(
    index_manifest: Json,
    results: list[Json],
    *,
    generated_at: str | None = None,
    approval: str | None = None,
    tool_versions: Mapping[str, Any] | None = None,
    skipped_completed: int | None = None,
) -> Json:
    recording_count = js.coalesce(js.length(js.get(index_manifest, "recordings")), 0)
    return {
        "schemaVersion": FULL_CORPUS_RESULTS_SCHEMA_VERSION,
        "generatedAt": generated_at if generated_at is not None else jsonio.iso_timestamp(),
        "updatedAt": jsonio.iso_timestamp(),
        "sourceIndex": js.defined(
            schemaVersion=js.get(index_manifest, "schemaVersion"),
            generatedAt=js.get(index_manifest, "generatedAt"),
            recordingCount=js.coalesce(
                js.get(js.get(index_manifest, "totals"), "recordings"), recording_count
            ),
        ),
        "releaseGateApproval": approval,
        "readScope": {
            "allIndexedRecordings": True,
            "audioBytes": True,
            "retainedAudioBytes": False,
            "audibleDerivedArtifacts": False,
            "upload": False,
            "corpusMutation": False,
        },
        "resume": {
            "enabled": True,
            "key": "recordingId",
            "outputWrittenAfterEachRecording": True,
            "existingResultsSkipped": True,
            "skippedCompletedCount": skipped_completed if skipped_completed is not None else 0,
            "completedCount": len(results),
            "remainingCount": max(recording_count - len(results), 0),
            "lastCompletedRecordingId": (
                js.coalesce(js.get(results[-1], "recordingId"), None) if results else None
            ),
        },
        "toolChoices": [
            _ffmpeg_choice(
                js.coalesce(js.get(tool_versions, "ffmpeg"), None), "approved local audio"
            ),
            _dsp_choice("classifier implementation"),
            _afinfo_choice(),
        ],
        "buckets": _buckets(),
        "results": results,
    }


def build_result_row(entry: Json, features: Json) -> Json:
    classification = classify_features(features)
    return js.defined(
        recordingId=js.get(entry, "recordingId"),
        filename=js.get(entry, "filename"),
        capturedAt=js.coalesce(js.get(entry, "capturedAt"), None),
        year=_year_for(entry),
        sourceRef=js.get(entry, "sourceRef"),
        features=features,
        classification=classification,
        humanSpotCheck={
            "recommended": _spot_check_recommended(classification),
            "status": "not_reviewed",
            "reviewedBy": None,
            "notes": None,
        },
    )


def analyze_sample(
    sample_manifest: Json,
    *,
    approval: str | None,
    corpus_root: str | None,
    ffmpeg_bin: str,
    generated_at: str | None = None,
) -> Json:
    if not approval:
        raise FilterProofError("analyze requires --approval for non-dry-run audio reads")
    if not corpus_root:
        raise FilterProofError("analyze requires --corpus-root")
    tool_versions = {"ffmpeg": read_tool_version(ffmpeg_bin, ["-version"])}
    results: list[Json] = []
    for entry in js.coalesce(js.get(sample_manifest, "sample"), []):
        audio_path = js.path_join(corpus_root, entry["filename"])
        features = extract_features_from_pcm(decode_pcm_with_ffmpeg(audio_path, ffmpeg_bin))
        classification = classify_features(features)
        results.append(
            js.defined(
                recordingId=js.get(entry, "recordingId"),
                filename=js.get(entry, "filename"),
                year=js.get(entry, "year"),
                sourceRef=js.get(entry, "sourceRef"),
                features=features,
                classification=classification,
                humanSpotCheck={
                    "recommended": _spot_check_recommended(classification),
                    "status": "not_reviewed",
                    "notes": None,
                },
            )
        )
    return build_results_manifest(
        sample_manifest,
        results,
        generated_at=generated_at,
        approval=approval,
        tool_versions=tool_versions,
    )


def _read_existing(path: str) -> Any:
    try:
        return jsonio.read_json(path)
    except FileNotFoundError:
        return None


def analyze_index_manifest(
    index_manifest: Json,
    *,
    approval: str | None,
    corpus_root: str | None,
    ffmpeg_bin: str,
    out: str,
    generated_at: str | None = None,
    tool_versions: Mapping[str, Any] | None = None,
    analyze_recording: AnalyzeRecording | None = None,
) -> Json:
    """``analyzeIndexManifest``: resumable by ``recordingId``; the manifest is rewritten
    after every recording. ``tool_versions`` and ``analyze_recording`` are the hooks."""

    if not approval:
        raise FilterProofError("analyze-index requires --approval for non-dry-run audio reads")
    if not corpus_root:
        raise FilterProofError("analyze-index requires --corpus-root")
    if out == "-":
        raise FilterProofError(
            "analyze-index requires a file output for incremental resume support"
        )

    existing = _read_existing(out)
    existing_by_id: dict[Any, Json] = {}
    for row in js.coalesce(js.get(existing, "results"), []):
        existing_by_id[js.get(row, "recordingId")] = row
    if tool_versions is None:
        tool_versions = {"ffmpeg": read_tool_version(ffmpeg_bin, ["-version"])}
    manifest = build_full_corpus_results_manifest(
        index_manifest,
        list(existing_by_id.values()),
        generated_at=generated_at,
        approval=approval,
        tool_versions=tool_versions,
        skipped_completed=len(existing_by_id),
    )
    jsonio.write_json(out, manifest)

    def analyze_entry(entry: Json) -> Json:
        audio_path = js.path_join(corpus_root, entry["filename"])
        pcm = decode_pcm_with_ffmpeg(audio_path, ffmpeg_bin)
        return build_result_row(entry, extract_features_from_pcm(pcm))

    analyze = analyze_recording if analyze_recording is not None else analyze_entry
    recordings = js.coalesce(js.get(index_manifest, "recordings"), [])
    for entry in recordings:
        if js.get(entry, "recordingId") in existing_by_id:
            continue
        # Measurement-chain takes are not Voice Memos: the singing filter does not apply.
        if js.get(js.get(entry, "source"), "type") == "measurement":
            continue
        row = analyze(entry)
        results: list[Json] = manifest["results"]
        results.append(row)
        resume: Json = manifest["resume"]
        resume["completedCount"] = len(results)
        resume["remainingCount"] = max(js.coalesce(js.length(recordings), 0) - len(results), 0)
        resume.pop("lastCompletedRecordingId", None)
        last = js.get(row, "recordingId")
        if last is not js.MISSING:
            resume["lastCompletedRecordingId"] = last
        manifest["updatedAt"] = jsonio.iso_timestamp()
        jsonio.write_json(out, manifest)
    return manifest


# ---------------------------------------------------------------------------- command line


def run(argv: Sequence[str]) -> int:
    """``run``: refusals raise :class:`FilterProofError`."""

    options = parse_args(argv)
    if not options.command or options.help:
        sys.stdout.write(HELP)
        return 0
    if options.command == "select-sample":
        index = jsonio.read_json(options.index)
        sample = select_sample(
            index,
            generated_at=options.generated_at,
            max=options.max,
            per_year=options.per_year,
        )
        jsonio.write_json(options.out, sample)
        return 0
    if options.command == "analyze":
        sample = jsonio.read_json(options.sample)
        if options.dry_run:
            jsonio.write_json(
                "-",
                analyze_dry_run(
                    sample,
                    corpus_root=options.corpus_root,
                    ffmpeg_bin=options.ffmpeg_bin,
                    out=options.out,
                ),
            )
            return 0
        results = analyze_sample(
            sample,
            approval=options.approval,
            corpus_root=options.corpus_root,
            ffmpeg_bin=options.ffmpeg_bin,
            generated_at=options.generated_at,
        )
        jsonio.write_json(options.out, results)
        return 0
    if options.command == "analyze-index":
        index = jsonio.read_json(options.index)
        if options.dry_run:
            jsonio.write_json(
                "-",
                analyze_index_dry_run(
                    index,
                    corpus_root=options.corpus_root,
                    ffmpeg_bin=options.ffmpeg_bin,
                    out=options.out,
                ),
            )
            return 0
        analyze_index_manifest(
            index,
            approval=options.approval,
            corpus_root=options.corpus_root,
            ffmpeg_bin=options.ffmpeg_bin,
            out=options.out,
            generated_at=options.generated_at,
        )
        return 0
    raise FilterProofError(f"unsupported command: {options.command}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except Exception as error:  # the JavaScript catches every Error
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

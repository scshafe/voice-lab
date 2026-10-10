"""Local speech-to-text over the recording index (voice-journey's ``local-stt.mjs``).

Still whisper.cpp (the medium ggml model) on the CPU (design §5.3): every indexed phone
recording is decoded by ffmpeg into a temporary 16 kHz mono wav, transcribed by
``whisper-cli``, and the temporary wav is deleted. Two commands:

- ``transcribe-index`` writes ``voice-journey.local-stt-transcripts.v1``, resumable by
  ``recordingId`` (the manifest is rewritten after every recording, and completed rows
  are skipped on the next run). ``--dry-run`` reads only the index.
- ``backfill-manifest`` re-derives ``durationProcessedSeconds`` and the duration-based
  rates from the local transcript JSONs; it reads no audio and re-runs no STT.

Transcript text is private. It is written only to the local transcript store
(``--transcript-dir``, by default ``<artifacts>/stt-transcripts``: the gitignored
``local-artifacts/`` in local development, ``$VOICE_JOURNEY_DATA/artifacts`` under a data
root, never the repository). The manifest carries only aggregate metadata (language,
duration, confidence means, word/segment/character counts, derived rates), local path
references, tool and model provenance, and whether those paths are inside the artifacts
directory (``gitignoredExpected``). As in the JS, a transcript directory elsewhere is
recorded as such, not refused.

Differences from the JS:

- The environment (``FFMPEG_BIN``, ``WHISPER_CPP_*``, ``VOICE_JOURNEY_DATA``) is read when
  the command line is parsed, not when the module is imported. A malformed
  ``WHISPER_CPP_THREADS`` is therefore an ``error: ...`` line and exit code 1, where Node
  died at import with a stack trace (also a non-zero exit).
- The hooks (``tool_versions``, ``transcribe_recording``) are keyword arguments and
  synchronous.
- Error messages from the operating system and from the JSON parser are Python's
  (``error: [Errno 2] ...`` rather than ``error: ENOENT: ...``); a binary that cannot
  be started is reported as ``spawn <bin> <ERRNO>`` as Node did.
- The help text names ``python -m cole_voice_lab.journey.stt``.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypedDict

from cole_voice_lab.journey.jsonio import (
    format_number,
    iso_timestamp,
    manifest_text,
    to_fixed,
    write_json,
)
from cole_voice_lab.journey.paths import is_inside_artifacts, resolve_paths, resolve_tools

MANIFEST_SCHEMA_VERSION = "voice-journey.local-stt-transcripts.v1"
SAMPLE_RATE = 16000

# ECMAScript WhiteSpace and LineTerminator: what String.prototype.trim, /\s/u and
# Number() skip. Python's str.isspace differs (it has U+001C-U+001F and U+0085, it
# lacks U+FEFF).
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_JS_WORD_SPLIT = re.compile(f"[{_JS_SPACE}]+")
_JS_TRIM = re.compile(f"^[{_JS_SPACE}]+|[{_JS_SPACE}]+$")
_JS_DECIMAL = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
_JS_RADIX = re.compile(r"0([xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)")
_RADIX_BASE = {"x": 16, "o": 8, "b": 2}

JsonObject = dict[str, Any]


class LocalSttError(Exception):
    """A refusal or a failed tool run; the CLI prints ``error: <message>``."""


class TranscriptionResult(TypedDict, total=False):
    """What a transcription (the real one or a test's hook) returns for one recording."""

    transcriptText: str
    rawTranscript: Any
    textPath: str
    jsonPath: str
    language: str
    segments: list[Any]
    durationSeconds: float | None


@dataclass(frozen=True)
class SttOptions:
    """The command line. The defaults are the local-development ones (no data root, no
    tool overrides); :func:`parse_args` fills them from the environment instead."""

    command: str = "transcribe-index"
    approval: str | None = None
    backfilled_at: str | None = None
    corpus_root: str | None = None
    dry_run: bool = False
    ffmpeg_bin: str = "ffmpeg"
    generated_at: str | None = None
    help: bool = False
    index: str = "manifests/recording-index.json"
    language: str = "auto"
    manifest: str = "manifests/local-stt-transcripts.json"
    model: str = "models/ggml-medium.bin"
    out: str = "manifests/local-stt-transcripts.json"
    out_set: bool = False
    threads: int | None = None
    transcript_dir: str = "local-artifacts/stt-transcripts"
    whisper_bin: str = "whisper-cli"


TranscribeRecording = Callable[[JsonObject, SttOptions], TranscriptionResult]


# --- JavaScript value rules -------------------------------------------------------------


def _get(value: Any, key: str) -> Any:
    """``value?.[key]``: undefined (None) unless ``value`` is an object that has it."""

    return value.get(key) if isinstance(value, dict) else None


def _member(value: Any, key: str) -> Any:
    """``value.key``: reading a property of null or undefined throws."""

    if value is None:
        raise TypeError(f"Cannot read properties of null (reading '{key}')")
    return _get(value, key)


def _coalesce(*values: Any) -> Any:
    """``a ?? b ?? ...``: the first value that is not null or undefined."""

    for value in values:
        if value is not None:
            return value
    return None


def _js_string(value: Any) -> str:
    """``String(value)`` for JSON values."""

    if value is None:
        return "null"
    if isinstance(value, bool | int | float):
        return format_number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else _js_string(item) for item in value)
    return "[object Object]"


def _js_trim(text: str) -> str:
    return _JS_TRIM.sub("", text)


def _js_number(text: str) -> float:
    """``Number(text)`` for a string: trimmed, empty is 0, decimal, Infinity, 0x/0o/0b."""

    trimmed = _js_trim(text)
    if trimmed == "":
        return 0.0
    if _JS_DECIMAL.fullmatch(trimmed):
        return float(trimmed.replace("Infinity", "inf"))
    radix = _JS_RADIX.fullmatch(trimmed)
    if radix:
        digits = radix.group(1)
        return float(int(digits[1:], _RADIX_BASE[digits[0].lower()]))
    return math.nan


def _js_max(values: Sequence[float]) -> float:
    """``Math.max(...values)`` for a non-empty list: NaN wins."""

    if any(math.isnan(value) for value in values):
        return math.nan
    return max(values)


def _utf16_length(text: str) -> int:
    """``text.length``: UTF-16 code units."""

    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _js_join(*parts: str) -> str:
    """``path.join``: joined and normalized."""

    return os.path.normpath(os.path.join(*parts))


def _js_basename(file_path: str) -> str:
    """``path.basename``: trailing separators are ignored."""

    return os.path.basename(file_path.rstrip("/"))


def number_or_null(value: Any) -> float | None:
    """``typeof value === "number" && Number.isFinite(value) ? value : null``."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


# --- files and tools --------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise json.JSONDecodeError(f"{name} is not valid JSON", name, 0)


def read_json(file_path: str) -> Any:
    """``JSON.parse(await readFile(path, "utf8"))``: invalid UTF-8 is replaced, and
    ``NaN``/``Infinity`` literals are refused as JavaScript refuses them."""

    text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    return json.loads(text, parse_constant=_reject_constant)


def _read_existing_manifest(file_path: str) -> Any:
    try:
        return read_json(file_path)
    except FileNotFoundError:
        return None


def _capture_command(command: str, args: Sequence[str], message: str | None = None) -> str:
    failure = message if message is not None else f"{_js_basename(command)} failed"
    try:
        completed = subprocess.run(
            [command, *args], stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "UNKNOWN")
        raise LocalSttError(f"spawn {command} {code}") from error
    out = completed.stdout.decode("utf-8", "replace")
    err = _js_trim(completed.stderr.decode("utf-8", "replace"))
    if completed.returncode != 0:
        exit_code = "null" if completed.returncode < 0 else str(completed.returncode)
        raise LocalSttError(f"{failure}: {err or f'exit code {exit_code}'}")
    return out or err


def _read_tool_version(command: str, args: Sequence[str]) -> str | None:
    try:
        result = _capture_command(command, args)
    except Exception:
        return None
    for line in result.split("\n"):
        if _js_trim(line):
            return _js_trim(line)
    return None


def _local_path_ref(file_path: str) -> str:
    """``path.relative(cwd, p)`` when ``p`` is under the working directory, else ``p``."""

    relative = os.path.relpath(os.path.abspath(file_path), os.getcwd())
    if relative == ".":
        relative = ""
    return file_path if relative.startswith("..") or os.path.isabs(relative) else relative


def _is_gitignored_local_path(file_path: str) -> bool:
    return is_inside_artifacts(file_path)


# --- transcript metadata ----------------------------------------------------------------


def _segments_from_transcript(raw_transcript: Any) -> list[Any]:
    segments = _get(raw_transcript, "segments")
    if isinstance(segments, list):
        return segments
    transcription = _get(raw_transcript, "transcription")
    if isinstance(transcription, list):
        return transcription
    return []


def _text_from_transcript(raw_transcript: Any) -> str:
    text = _get(raw_transcript, "text")
    if isinstance(text, str):
        return text
    pieces = []
    for segment in _segments_from_transcript(raw_transcript):
        piece = _coalesce(_member(segment, "text"), "")
        pieces.append(_js_string(piece))
    return _js_trim(" ".join(pieces))


def _timestamp_seconds(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = number_or_null(value)
        if number is not None:
            return number
    if not isinstance(value, str):
        return None
    parts = [_js_number(part.replace(",", ".", 1)) for part in value.split(":")]
    if any(math.isnan(part) for part in parts):
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def _segment_end_seconds(segment: Any) -> float | None:
    offset_ms = number_or_null(_get(_member(segment, "offsets"), "to"))
    if offset_ms is not None:
        return offset_ms / 1000
    return _timestamp_seconds(
        _coalesce(
            _member(segment, "end"),
            _member(segment, "t1"),
            _get(_member(segment, "timestamps"), "to"),
        )
    )


def _duration_from_transcript(raw_transcript: Any, segments: list[Any]) -> float | None:
    explicit = number_or_null(
        _coalesce(
            _get(raw_transcript, "durationSeconds"),
            _get(raw_transcript, "duration_seconds"),
            _get(raw_transcript, "duration"),
        )
    )
    if explicit is not None:
        return to_fixed(explicit, 3)
    ends = [
        end for end in (_segment_end_seconds(segment) for segment in segments) if end is not None
    ]
    return to_fixed(_js_max(ends), 3) if ends else None


def _mean_or_null(values: list[float | None]) -> float | None:
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    total = 0.0
    for value in numbers:
        total += value
    return to_fixed(total / len(numbers), 6)


def _confidence_stats_for(segments: list[Any]) -> JsonObject:
    def mean_of(snake: str, camel: str) -> float | None:
        return _mean_or_null(
            [
                number_or_null(_coalesce(_member(segment, snake), _member(segment, camel)))
                for segment in segments
            ]
        )

    return {
        "meanLogProbability": mean_of("avg_logprob", "meanLogProbability"),
        "meanNoSpeechProbability": mean_of("no_speech_prob", "noSpeechProbability"),
        "meanCompressionRatio": mean_of("compression_ratio", "compressionRatio"),
    }


def _confidence_stats_all_null(stats: Any) -> bool:
    return all(
        _get(stats, key) is None
        for key in ("meanLogProbability", "meanNoSpeechProbability", "meanCompressionRatio")
    )


def _word_count_for(text: str) -> int:
    trimmed = _js_trim(text)
    return len(_JS_WORD_SPLIT.split(trimmed)) if trimmed else 0


def normalize_transcript(raw_transcript: Any, language: str | None = "auto") -> JsonObject:
    """Aggregate metadata of one whisper.cpp transcript; never its text."""

    segments = _segments_from_transcript(raw_transcript)
    text = _text_from_transcript(raw_transcript)
    word_count = _word_count_for(text)
    duration = _duration_from_transcript(raw_transcript, segments)
    result = _get(raw_transcript, "result")
    return {
        "language": {
            "requested": _coalesce(language, "auto"),
            "detected": _coalesce(_get(raw_transcript, "language"), _get(result, "language"), None),
            "probability": number_or_null(
                _coalesce(
                    _get(raw_transcript, "languageProbability"),
                    _get(result, "language_probability"),
                )
            ),
        },
        "durationProcessedSeconds": duration,
        "confidenceStats": _confidence_stats_for(segments),
        "counts": {
            "words": word_count,
            "segments": len(segments),
            "characters": _utf16_length(text),
        },
        "derivedFeatures": {
            "wordsPerMinute": to_fixed(word_count / (duration / 60), 2) if duration else None,
            "averageWordsPerSegment": to_fixed(word_count / len(segments), 2) if segments else None,
            "segmentDensityPerMinute": to_fixed(len(segments) / (duration / 60), 2)
            if duration
            else None,
            "hasTranscribedWords": word_count > 0,
        },
    }


# --- manifests --------------------------------------------------------------------------


def _recordings(index_manifest: Any) -> list[Any] | None:
    recordings = _get(index_manifest, "recordings")
    return recordings if isinstance(recordings, list) else None


def _recording_count(index_manifest: Any) -> int:
    recordings = _recordings(index_manifest)
    return len(recordings) if recordings is not None else 0


def _source_index_for(index_manifest: Any) -> JsonObject:
    source: JsonObject = {}
    for key in ("schemaVersion", "generatedAt"):
        if isinstance(index_manifest, dict) and key in index_manifest:
            source[key] = index_manifest[key]
    recordings = _recordings(index_manifest)
    source["recordingCount"] = _coalesce(
        _get(_get(index_manifest, "totals"), "recordings"),
        len(recordings) if recordings is not None else None,
    )
    return source


def _read_scope() -> JsonObject:
    return {
        "allIndexedRecordings": True,
        "audioBytes": True,
        "decodeToTemporaryWav": True,
        "retainedAudioBytes": False,
        "transcriptTextRetainedLocally": True,
        "transcriptTextCommittedToRepo": False,
        "upload": False,
        "corpusMutation": False,
    }


def _ffmpeg_tool_choice(options: SttOptions, tool_versions: Mapping[str, Any]) -> JsonObject:
    return {
        "name": "ffmpeg",
        "version": _coalesce(tool_versions.get("ffmpeg"), None),
        "stage": "decode-to-temporary-16khz-mono-wav",
        "openness": "open-source",
        "openSource": True,
        "path": options.ffmpeg_bin,
        "notes": (
            "Host ffmpeg decodes approved local audio into a temporary wav that is deleted "
            "after each transcription."
        ),
    }


def _stt_tool_choice(options: SttOptions, tool_versions: Mapping[str, Any]) -> JsonObject:
    return {
        "name": "whisper.cpp",
        "version": _coalesce(tool_versions.get("whisperCpp"), None),
        "stage": "local-speech-to-text",
        "openness": "open-source",
        "openSource": True,
        "path": options.whisper_bin,
        "modelPath": options.model,
        "modelFamily": "whisper.cpp ggml",
        "language": options.language,
        "metalAcceleration": "host Homebrew whisper.cpp uses Metal on supported macOS hardware",
        "notes": (
            "Transcript text is written only to the local transcript store; the repo manifest "
            "stores aggregate metadata and local path references only."
        ),
    }


def build_stt_dry_run(index_manifest: Any, options: SttOptions) -> JsonObject:
    """What ``transcribe-index`` would read and write; reads no audio."""

    return {
        "dryRun": True,
        "operation": "local-stt-transcription",
        "architecture": {
            "slug": "kickoff-corpus-flow",
            "reads": [
                "recording-index-manifest",
                "index-feeds-stt",
                "host-access-seam",
                "seam-provides-stt-audio",
            ],
            "writes": [
                "local-transcript-store",
                "transcript-manifest",
                "stt-writes-local-transcripts",
                "stt-writes-transcript-manifest",
            ],
        },
        "recordingCount": _recording_count(index_manifest),
        "sourceIndex": _source_index_for(index_manifest),
        "corpusRoot": options.corpus_root,
        "outputPath": options.out,
        "localTranscriptDir": options.transcript_dir,
        "sttTool": _stt_tool_choice(options, {}),
        "audioPrepTool": _ffmpeg_tool_choice(options, {}),
        "model": {
            "path": options.model,
            "family": "whisper.cpp ggml",
            "defaultSelection": _js_basename(options.model),
            "openSource": True,
        },
        "language": options.language,
        "expectedRuntime": (
            "overnight-scale for about 66 hours of indexed audio on the current host inventory"
        ),
        "resumeBehavior": {
            "enabled": True,
            "key": "recordingId",
            "outputWrittenAfterEachRecording": True,
            "existingCompletedResultsSkipped": True,
            "rerunCommandIsSameAsInitialCommand": True,
        },
        "readScope": _read_scope(),
        "repoSafeManifestFields": [
            "status",
            "language",
            "durationProcessedSeconds",
            "confidenceStats",
            "wordCount",
            "segmentCount",
            "derivedFeatures",
            "local transcript path references",
            "tool/model provenance",
        ],
    }


def _completed(rows: list[Any]) -> list[Any]:
    return [row for row in rows if _member(row, "status") == "completed"]


def build_stt_manifest(
    index_manifest: Any,
    results: list[Any],
    options: SttOptions,
    *,
    tool_versions: Mapping[str, Any] | None = None,
    skipped_completed: int | None = None,
) -> JsonObject:
    versions: Mapping[str, Any] = tool_versions if tool_versions is not None else {}
    completed_count = len(_completed(results))
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "generatedAt": _coalesce(options.generated_at, iso_timestamp()),
        "updatedAt": iso_timestamp(),
        "sourceIndex": _source_index_for(index_manifest),
        "releaseGateApproval": options.approval,
        "readScope": _read_scope(),
        "localTranscriptStore": {
            "root": options.transcript_dir,
            "gitignoredExpected": _is_gitignored_local_path(options.transcript_dir),
            "containsTranscriptText": True,
            "committedToRepo": False,
        },
        "repoSafeManifest": {
            "containsTranscriptText": False,
            "containsAudioBytes": False,
            "containsAudibleDerivedArtifacts": False,
        },
        "resume": {
            "enabled": True,
            "key": "recordingId",
            "outputWrittenAfterEachRecording": True,
            "existingCompletedResultsSkipped": True,
            "skippedCompletedCount": _coalesce(skipped_completed, 0),
            "completedCount": completed_count,
            "remainingCount": max(_recording_count(index_manifest) - completed_count, 0),
            "lastCompletedRecordingId": _get(results[-1], "recordingId") if results else None,
        },
        "toolChoices": [
            _ffmpeg_tool_choice(options, versions),
            _stt_tool_choice(options, versions),
        ],
        "results": results,
    }


@dataclass(frozen=True)
class MaterializedTranscript:
    text_path: str
    json_path: str
    raw_transcript: Any


def build_transcript_row(
    entry: JsonObject, materialized: MaterializedTranscript, options: SttOptions
) -> JsonObject:
    """One repo-safe manifest row: metadata and local path references, never text."""

    normalized = normalize_transcript(materialized.raw_transcript, options.language)
    row: JsonObject = {"recordingId": entry.get("recordingId")}
    if "filename" in entry:
        row["filename"] = entry["filename"]
    captured_at = entry.get("capturedAt")
    row["capturedAt"] = captured_at
    row["year"] = captured_at[:4] if captured_at else "unknown"
    if "sourceRef" in entry:
        row["sourceRef"] = entry["sourceRef"]
    row.update(
        {
            "status": "completed",
            "language": normalized["language"],
            "durationProcessedSeconds": normalized["durationProcessedSeconds"],
            "confidenceStats": normalized["confidenceStats"],
            "counts": normalized["counts"],
            "derivedFeatures": normalized["derivedFeatures"],
            "localArtifacts": {
                "transcriptTextPath": _local_path_ref(materialized.text_path),
                "transcriptJsonPath": _local_path_ref(materialized.json_path),
                "localOnly": True,
                "gitignoredExpected": _is_gitignored_local_path(materialized.text_path)
                and _is_gitignored_local_path(materialized.json_path),
                "textIncludedInManifest": False,
            },
            "toolRun": {
                "sttTool": "whisper.cpp",
                "whisperBin": options.whisper_bin,
                "modelPath": options.model,
                "audioPrep": "ffmpeg decode to 16 kHz mono wav in a temporary directory",
            },
        }
    )
    return row


def transcribe_recording_entry(entry: JsonObject, options: SttOptions) -> TranscriptionResult:
    """ffmpeg to a temporary wav, then whisper-cli writing ``<id>.txt``/``<id>.json`` into
    the transcript directory (the only place transcript text is written)."""

    corpus_root = options.corpus_root or ""
    filename = _js_string(entry.get("filename"))
    recording_id = _js_string(entry.get("recordingId"))
    audio_path = _js_join(corpus_root, filename)
    transcript_base = _js_join(options.transcript_dir, recording_id)
    temp_root = tempfile.mkdtemp(prefix="voice-journey-stt-")
    wav_path = os.path.join(temp_root, f"{recording_id}.wav")
    try:
        _capture_command(
            options.ffmpeg_bin,
            [
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                audio_path,
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                wav_path,
            ],
            f"ffmpeg failed for {filename}",
        )
        args = [
            "-m",
            options.model,
            "-f",
            wav_path,
            "-l",
            options.language,
            "-otxt",
            "-oj",
            "-of",
            transcript_base,
        ]
        if options.threads:
            args += ["-t", format_number(float(options.threads))]
        _capture_command(options.whisper_bin, args, f"whisper.cpp failed for {filename}")
        return {
            "textPath": f"{transcript_base}.txt",
            "jsonPath": f"{transcript_base}.json",
            "rawTranscript": read_json(f"{transcript_base}.json"),
        }
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _materialize_transcript_result(
    entry: JsonObject, result: TranscriptionResult, options: SttOptions
) -> MaterializedTranscript:
    transcript_base = _js_join(options.transcript_dir, _js_string(entry.get("recordingId")))
    raw_transcript = result.get("rawTranscript")
    if raw_transcript is None:
        raw_transcript = {
            "text": _coalesce(result.get("transcriptText"), ""),
            "language": _coalesce(result.get("language"), options.language),
            "segments": _coalesce(result.get("segments"), []),
            "durationSeconds": result.get("durationSeconds"),
        }
    text_path = _coalesce(result.get("textPath"), f"{transcript_base}.txt")
    json_path = _coalesce(result.get("jsonPath"), f"{transcript_base}.json")
    if not result.get("textPath"):
        text = result.get("transcriptText")
        body = text if text is not None else _text_from_transcript(raw_transcript)
        Path(text_path).write_text(f"{_js_string(body)}\n", encoding="utf-8")
    if not result.get("jsonPath"):
        Path(json_path).write_text(manifest_text(raw_transcript), encoding="utf-8")
    return MaterializedTranscript(text_path, json_path, raw_transcript)


def transcribe_index_manifest(
    index_manifest: Any,
    options: SttOptions,
    *,
    tool_versions: Mapping[str, Any] | None = None,
    transcribe_recording: TranscribeRecording | None = None,
) -> JsonObject:
    """Transcribe every indexed phone recording not completed yet, rewriting the manifest
    at ``options.out`` after each one."""

    if not options.approval:
        raise LocalSttError("transcribe-index requires --approval for non-dry-run audio reads")
    if not options.corpus_root:
        raise LocalSttError("transcribe-index requires --corpus-root")
    if options.out == "-":
        raise LocalSttError(
            "transcribe-index requires a file output for incremental resume support"
        )

    Path(options.transcript_dir).mkdir(parents=True, exist_ok=True)
    existing = _read_existing_manifest(options.out)
    completed_by_id: dict[Any, Any] = {}
    for row in _completed(_coalesce(_get(existing, "results"), [])):
        completed_by_id[_get(row, "recordingId")] = row
    versions = (
        tool_versions
        if tool_versions is not None
        else {
            "ffmpeg": _read_tool_version(options.ffmpeg_bin, ["-version"]),
            "whisperCpp": _read_tool_version(options.whisper_bin, ["--version"]),
        }
    )
    manifest = build_stt_manifest(
        index_manifest,
        list(completed_by_id.values()),
        options,
        tool_versions=versions,
        skipped_completed=len(completed_by_id),
    )
    write_json(options.out, manifest)

    transcribe = transcribe_recording or transcribe_recording_entry
    results: list[Any] = manifest["results"]
    resume: JsonObject = manifest["resume"]
    for entry in _coalesce(_recordings(index_manifest), []):
        if entry.get("recordingId") in completed_by_id:
            continue
        # Measurement-chain takes are indexed but not transcribed yet (Phase B).
        if _get(entry.get("source"), "type") == "measurement":
            continue
        result = transcribe(entry, options)
        materialized = _materialize_transcript_result(entry, result, options)
        row = build_transcript_row(entry, materialized, options)
        results.append(row)
        resume["completedCount"] = len(_completed(results))
        resume["remainingCount"] = max(
            _recording_count(index_manifest) - resume["completedCount"], 0
        )
        resume["lastCompletedRecordingId"] = row["recordingId"]
        manifest["updatedAt"] = iso_timestamp()
        write_json(options.out, manifest)
    return manifest


# --- duration backfill ------------------------------------------------------------------


def _backfill_method() -> JsonObject:
    return {
        "id": "whisper_cpp_offsets_ms.v1",
        "prefersSegmentOffsetsMilliseconds": True,
        "timestampFallbackNormalizesCommaDecimals": True,
        "rereadsAudio": False,
        "rerunsStt": False,
        "confidenceStatsNote": (
            "extracted only when transcript JSON segments carry "
            "avg_logprob/no_speech_prob/compression_ratio; whisper-cli -oj output typically "
            "does not"
        ),
    }


def _backfill_read_scope() -> JsonObject:
    return {
        "audioBytes": False,
        "transcriptJsonParsedForTimingMetadataOnly": True,
        "transcriptTextCommittedToRepo": False,
        "upload": False,
        "corpusMutation": False,
    }


def _backfill_target_for(options: SttOptions) -> str:
    return options.out if options.out_set else options.manifest


def build_backfill_dry_run(manifest: Any, options: SttOptions) -> JsonObject:
    """What ``backfill-manifest`` would change; reads only the manifest."""

    rows: list[Any] = _coalesce(_get(manifest, "results"), [])
    return {
        "dryRun": True,
        "operation": "local-stt-duration-backfill",
        "architecture": {
            "slug": "kickoff-corpus-flow",
            "reads": ["transcript-manifest", "local-transcript-store"],
            "writes": ["transcript-manifest"],
        },
        "manifestPath": options.manifest,
        "outputPath": _backfill_target_for(options),
        "localTranscriptDir": options.transcript_dir,
        "rows": {
            "total": len(rows),
            "completed": len(_completed(rows)),
            "durationProcessedSecondsNull": sum(
                1 for row in rows if _member(row, "durationProcessedSeconds") is None
            ),
            "wordsPerMinuteNull": sum(
                1 for row in rows if _get(_member(row, "derivedFeatures"), "wordsPerMinute") is None
            ),
            "segmentDensityPerMinuteNull": sum(
                1
                for row in rows
                if _get(_member(row, "derivedFeatures"), "segmentDensityPerMinute") is None
            ),
            "confidenceStatsAllNull": sum(
                1 for row in rows if _confidence_stats_all_null(_member(row, "confidenceStats"))
            ),
        },
        "method": _backfill_method(),
        "readScope": _backfill_read_scope(),
    }


def backfill_manifest(manifest: JsonObject, options: SttOptions) -> JsonObject:
    """Re-derive durations and duration-based rates from the local transcript JSONs and
    write only numbers back to the manifest."""

    rows: list[Any] = _coalesce(manifest.get("results"), [])
    counters = {
        "rowsExamined": 0,
        "rowsSkippedNotCompleted": 0,
        "rowsUpdatedDuration": 0,
        "rowsRefreshedDuration": 0,
        "rowsNoDurationAvailable": 0,
        "rowsMissingTranscriptJson": 0,
        "rowsUnparseableTranscriptJson": 0,
        "rowsConfidenceStatsUpdated": 0,
    }
    missing_recording_ids: list[Any] = []
    for row in rows:
        counters["rowsExamined"] += 1
        if _member(row, "status") != "completed":
            counters["rowsSkippedNotCompleted"] += 1
            continue
        recording_id = row["recordingId"] if "recordingId" in row else None
        name = _js_string(recording_id) if "recordingId" in row else "undefined"
        json_path = _js_join(options.transcript_dir, f"{name}.json")
        try:
            raw_transcript = read_json(json_path)
        except FileNotFoundError:
            counters["rowsMissingTranscriptJson"] += 1
            missing_recording_ids.append(recording_id)
            continue
        except ValueError:
            counters["rowsUnparseableTranscriptJson"] += 1
            continue
        segments = _segments_from_transcript(raw_transcript)
        duration = _duration_from_transcript(raw_transcript, segments)
        if duration is None:
            counters["rowsNoDurationAvailable"] += 1
        else:
            previous = row.get("durationProcessedSeconds")
            if previous is None:
                counters["rowsUpdatedDuration"] += 1
            elif isinstance(previous, bool) or previous != duration:
                counters["rowsRefreshedDuration"] += 1
            row["durationProcessedSeconds"] = duration
            counts = row.get("counts")
            words = _coalesce(_get(counts, "words"), 0)
            segment_count = _coalesce(_get(counts, "segments"), len(segments))
            existing = row.get("derivedFeatures")
            row["derivedFeatures"] = {
                **(existing if isinstance(existing, dict) else {}),
                "wordsPerMinute": to_fixed(words / (duration / 60), 2) if duration > 0 else None,
                "segmentDensityPerMinute": to_fixed(segment_count / (duration / 60), 2)
                if duration > 0
                else None,
            }
        confidence_stats = _confidence_stats_for(segments)
        if not _confidence_stats_all_null(confidence_stats):
            row["confidenceStats"] = confidence_stats
            counters["rowsConfidenceStatsUpdated"] += 1
    backfilled_at = _coalesce(options.backfilled_at, iso_timestamp())
    manifest["durationBackfill"] = {
        "method": _backfill_method(),
        "backfilledAt": backfilled_at,
        "localTranscriptDir": options.transcript_dir,
        **counters,
        "missingRecordingIds": missing_recording_ids,
        "fieldsUpdated": [
            "durationProcessedSeconds",
            "derivedFeatures.wordsPerMinute",
            "derivedFeatures.segmentDensityPerMinute",
            "confidenceStats (only when present in transcript JSON)",
        ],
        "transcriptTextWrittenToManifest": False,
    }
    manifest["updatedAt"] = backfilled_at
    write_json(_backfill_target_for(options), manifest)
    return manifest


# --- command line -----------------------------------------------------------------------

HELP = """Voice Journey local STT tooling.

Usage:
  python -m cole_voice_lab.journey.stt transcribe-index --dry-run [--index PATH] [--out PATH]
  python -m cole_voice_lab.journey.stt transcribe-index --index PATH --corpus-root PATH \
--approval TEXT [--out PATH] [--transcript-dir PATH] [--whisper-bin PATH] [--model PATH] \
[--ffmpeg-bin PATH] [--language LANG] [--threads N (default $WHISPER_CPP_THREADS)]
  python -m cole_voice_lab.journey.stt backfill-manifest --dry-run [--manifest PATH]
  python -m cole_voice_lab.journey.stt backfill-manifest [--manifest PATH] \
[--transcript-dir PATH] [--out PATH] [--backfilled-at ISO]

Dry-run reads only committed manifests. Non-dry-run reads approved local audio,
writes transcript text under the local transcript directory, and writes only
repo-safe aggregate metadata plus local path references to the manifest.

backfill-manifest re-derives durationProcessedSeconds (and the duration-based
derived rates) from the local transcript JSON segment offsets. It reads no
audio, re-runs no STT, and writes only numeric metadata back to the repo-safe
manifest. Its dry-run reads only the manifest.
"""

_VALUE_FLAGS = {
    "--approval": "approval",
    "--backfilled-at": "backfilled_at",
    "--corpus-root": "corpus_root",
    "--ffmpeg-bin": "ffmpeg_bin",
    "--generated-at": "generated_at",
    "--index": "index",
    "--language": "language",
    "--manifest": "manifest",
    "--model": "model",
    "--transcript-dir": "transcript_dir",
    "--whisper-bin": "whisper_bin",
}


def default_options(env: Mapping[str, str] | None = None) -> SttOptions:
    """The defaults the JS computed at import, from the environment."""

    paths = resolve_paths(env)
    tools = resolve_tools(env, paths)
    out = str(paths.manifest("local-stt-transcripts.json"))
    return SttOptions(
        ffmpeg_bin=tools.ffmpeg_bin,
        index=str(paths.manifest("recording-index.json")),
        manifest=out,
        model=tools.whisper_model,
        out=out,
        threads=tools.whisper_threads,
        transcript_dir=str(paths.artifact("stt-transcripts")),
        whisper_bin=tools.whisper_bin,
    )


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else None
    if not value or value.startswith("--"):
        raise LocalSttError(f"{flag} requires a value")
    return value


def _parse_positive_integer(value: str, flag: str) -> int:
    parsed = _js_number(value)
    if not math.isfinite(parsed) or parsed != math.floor(parsed) or parsed <= 0:
        raise LocalSttError(f"{flag} must be a positive integer")
    return int(parsed)


def parse_args(argv: Sequence[str], env: Mapping[str, str] | None = None) -> SttOptions:
    argv = list(argv)
    maybe_command = argv[0] if argv else None
    has_command = (
        bool(maybe_command) and not str(maybe_command).startswith("--") and maybe_command != "help"
    )
    command = str(maybe_command) if has_command else "transcribe-index"
    args = argv[1:] if has_command else argv
    options = replace(default_options(env), command=command)
    changes: dict[str, Any] = {}
    while args:
        flag = args.pop(0)
        if flag in _VALUE_FLAGS:
            changes[_VALUE_FLAGS[flag]] = _require_value(args, flag)
        elif flag == "--dry-run":
            changes["dry_run"] = True
        elif flag == "--out":
            changes["out"] = _require_value(args, flag)
            changes["out_set"] = True
        elif flag == "--threads":
            changes["threads"] = _parse_positive_integer(_require_value(args, flag), flag)
        elif flag in ("--help", "help"):
            changes["help"] = True
        else:
            raise LocalSttError(f"unsupported argument: {flag}")
    return replace(options, **changes)


def run(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv, env)
    if options.help:
        sys.stdout.write(HELP)
        return 0
    if options.command == "transcribe-index":
        index = read_json(options.index)
        if options.dry_run:
            write_json("-", build_stt_dry_run(index, options))
            return 0
        transcribe_index_manifest(index, options)
        return 0
    if options.command == "backfill-manifest":
        manifest = read_json(options.manifest)
        if options.dry_run:
            write_json("-", build_backfill_dry_run(manifest, options))
            return 0
        backfill_manifest(manifest, options)
        return 0
    raise LocalSttError(f"unsupported command: {options.command}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except Exception as error:
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

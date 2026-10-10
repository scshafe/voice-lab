"""Local lyric indicators (port of voice-journey's ``lyric-indicators.mjs``).

Reads the local speech-to-text manifest and the gitignored transcript texts, and marks
each recording with repo-safe lyric indicators: membership of a cross-recording cluster
(Jaccard similarity over hashed five-word shingles, joined by union-find), repeated
four-gram structure, and a zero-word or non-English (wordless vocalise) signal. The
manifest it writes holds no transcript or lyric text.

Same manifest bytes as the JavaScript. Differences: the defaults are resolved when the
command runs (the JavaScript resolved them at import); a transcript that cannot be read
is refused with Python's error text after the same prefix. Cluster seeds are ordered
with ``jscompat.locale_compare_key``, which is ``localeCompare`` (ICU root collation)
exactly on ASCII ids (recording ids are 16 lower-case hex digits) and an approximation
beyond ASCII.
"""

from __future__ import annotations

import hashlib
import re
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey.jsonio import (
    iso_timestamp,
    read_json,
    read_text,
    to_fixed,
    write_json,
)
from cole_voice_lab.journey.paths import resolve_paths

SCHEMA_VERSION = "voice-journey.local-lyric-indicators.v1"
MIN_SHINGLE_WORDS = 5
DEFAULT_CLUSTER_THRESHOLD = 0.22
DEFAULT_MIN_CLUSTER_SIZE = 2
WORDLESS_LANGUAGE_EXCLUSIONS = frozenset({"en", "unknown", "und"})

_COMBINING = re.compile("[\u0300-\u036f]")
_WORD = re.compile(r"[a-z0-9']+")

HELP = """Voice Journey local lyric indicators.

Usage:
  voice-journey-lyric analyze --dry-run [--transcripts PATH] [--out PATH]
  voice-journey-lyric analyze --transcripts PATH --transcript-dir PATH [--out PATH] [--cluster-threshold RATIO] [--min-cluster-size N]

analyze reads local gitignored transcript text, computes local-first lyric
indicator marks, and writes a repo-safe manifest with no raw lyric text.
No external lyric databases or network services are used.
"""  # noqa: E501

Json = dict[str, Any]


class LyricIndicatorError(Exception):
    pass


@dataclass
class LyricOptions:
    cluster_threshold: float = DEFAULT_CLUSTER_THRESHOLD
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE
    transcript_dir: str | None = None
    out: str | None = None
    transcripts: str | None = None
    generated_at: str | None = None
    dry_run: bool = False
    command: str = "analyze"
    help: bool = False


@dataclass
class _Analyzed:
    row: Json
    shingles: set[str]
    repeated: Json
    wordless: Json
    tokens: list[str]


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else ""
    if not value or value.startswith("--"):
        raise LyricIndicatorError(f"{flag} requires a value")
    return value


def _parse_positive_integer(value: str, flag: str) -> int:
    parsed = js.string_to_number(value)
    if not (js.is_finite(parsed) and parsed.is_integer() and parsed > 0):
        raise LyricIndicatorError(f"{flag} must be a positive integer")
    return int(parsed)


def _parse_ratio(value: str, flag: str) -> float:
    parsed = js.string_to_number(value)
    if not js.is_finite(parsed) or parsed <= 0 or parsed > 1:
        raise LyricIndicatorError(f"{flag} must be a ratio in (0, 1]")
    return parsed


def parse_args(argv: Sequence[str]) -> LyricOptions:
    argv = list(argv)
    maybe_command = argv[0] if argv else ""
    has_command = bool(maybe_command) and not maybe_command.startswith("--")
    has_command = has_command and maybe_command != "help"
    paths = resolve_paths()
    options = LyricOptions(
        command=maybe_command if has_command else "analyze",
        out=str(paths.manifest("local-lyric-indicators.json")),
        transcripts=str(paths.manifest("local-stt-transcripts.json")),
        transcript_dir=str(paths.artifact("stt-transcripts")),
    )
    args = argv[1:] if has_command else argv
    while args:
        flag = args.pop(0)
        if flag == "--cluster-threshold":
            options.cluster_threshold = _parse_ratio(_require_value(args, flag), flag)
        elif flag == "--dry-run":
            options.dry_run = True
        elif flag == "--generated-at":
            options.generated_at = _require_value(args, flag)
        elif flag == "--min-cluster-size":
            options.min_cluster_size = _parse_positive_integer(_require_value(args, flag), flag)
        elif flag == "--out":
            options.out = _require_value(args, flag)
        elif flag == "--transcripts":
            options.transcripts = _require_value(args, flag)
        elif flag == "--transcript-dir":
            options.transcript_dir = _require_value(args, flag)
        elif flag in ("--help", "help"):
            options.help = True
        else:
            raise LyricIndicatorError(f"unsupported argument: {flag}")
    return options


def _results(manifest: Json) -> list[Any]:
    rows = js.get(manifest, "results")
    return rows if isinstance(rows, list) else []


def _put(target: Json, key: str, value: Any) -> None:
    if value is not js.MISSING:
        target[key] = value


def build_dry_run(transcript_manifest: Json, options: LyricOptions) -> Json:
    payload: Json = {
        "dryRun": True,
        "operation": "local-lyric-indicators",
        "architecture": {
            "slug": "kickoff-corpus-flow",
            "reads": ["transcript-manifest", "local-transcript-store"],
            "writes": ["transcript-manifest"],
        },
        "sourceTranscripts": _source_transcripts_for(transcript_manifest),
        "recordingCount": _results_length(transcript_manifest, 0),
    }
    if options.transcript_dir is not None:
        payload["transcriptDir"] = options.transcript_dir
    if options.out is not None:
        payload["outputPath"] = options.out
    payload["strategy"] = _strategy(options)
    payload["readScope"] = {
        "committedTranscriptManifest": True,
        "localTranscriptText": True,
        "localTranscriptTextReadInDryRun": False,
        "transcriptTextCommittedToRepo": False,
        "audioBytes": False,
        "upload": False,
        "externalLyricDatabases": False,
        "corpusMutation": False,
    }
    return payload


def analyze_lyric_indicators(
    transcript_manifest: Json,
    options: LyricOptions,
    read_transcript: Callable[[Json, LyricOptions], str] | None = None,
) -> Json:
    reader = read_transcript or read_transcript_for_row
    analyzed = []
    for row in _results(transcript_manifest):
        text = reader(row, options) if js.get(row, "status") == "completed" else ""
        analyzed.append(_analyze_transcript_row(row, text))
    return build_lyric_manifest(transcript_manifest, analyzed, options)


def read_transcript_for_row(row: Json, options: LyricOptions) -> str:
    file_path = _resolve_transcript_path(row, options)
    try:
        return read_text(file_path)
    except OSError as error:
        recording_id = js.to_string(js.get(row, "recordingId"))
        raise LyricIndicatorError(
            f"failed to read local transcript for {recording_id}: {error}"
        ) from error


def _resolve_transcript_path(row: Json, options: LyricOptions) -> Path:
    directory = options.transcript_dir or ""
    text_path = js.get(js.get(row, "localArtifacts"), "transcriptTextPath")
    if js.truthy(text_path):
        return Path(js.path_join(directory, js.path_basename(js.to_string(text_path))))
    return Path(js.path_join(directory, f"{js.to_string(js.get(row, 'recordingId'))}.txt"))


def _analyze_transcript_row(row: Json, text: str) -> _Analyzed:
    tokens = tokenize(text)
    return _Analyzed(
        row=row,
        tokens=tokens,
        shingles=_shingle_hashes(tokens, MIN_SHINGLE_WORDS),
        repeated=repetition_signal(tokens),
        wordless=wordless_signal(row),
    )


def build_lyric_manifest(
    transcript_manifest: Json, analyzed: list[_Analyzed], options: LyricOptions
) -> Json:
    clusters = _cluster_transcripts(analyzed, options)
    cluster_by_id: dict[Any, Json] = {}
    for cluster in clusters:
        for recording_id in cluster["recordingIds"]:
            cluster_by_id[recording_id] = cluster
    results = [_result_for(entry, cluster_by_id.get(_recording_id(entry))) for entry in analyzed]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": options.generated_at
        if options.generated_at is not None
        else iso_timestamp(),
        "sourceTranscripts": _source_transcripts_for(transcript_manifest),
        "readScope": {
            "transcriptManifest": True,
            "localTranscriptText": True,
            "transcriptTextCommittedToRepo": False,
            "rawLyricTextInManifest": False,
            "audioBytes": False,
            "upload": False,
            "externalLyricDatabases": False,
            "corpusMutation": False,
        },
        "strategy": _strategy(options),
        "privacy": {
            "containsRawLyricText": False,
            "containsTranscriptText": False,
            "clusterLabelsAreNonIdentifying": True,
        },
        "totals": _summarize(results, clusters),
        "clusters": clusters,
        "results": results,
    }


def _recording_id(entry: _Analyzed) -> Any:
    value = js.get(entry.row, "recordingId")
    return None if value is js.MISSING else value


def _year_of(row: Json) -> Any:
    year = js.get(row, "year")
    return year if not js.nullish(year) else _year_for(js.get(row, "capturedAt"))


def _result_for(entry: _Analyzed, cluster: Json | None) -> Json:
    if cluster is not None:
        cross: Json = {
            "marked": True,
            "clusterId": cluster["clusterId"],
            "clusterLabel": cluster["label"],
            "clusterSize": len(cluster["recordingIds"]),
            "confidence": cluster["confidence"],
        }
    else:
        cross = {
            "marked": False,
            "clusterId": None,
            "clusterLabel": None,
            "clusterSize": 0,
            "confidence": 0,
        }
    signals: Json = {
        "crossRecordingCluster": cross,
        "repetitionStructure": entry.repeated,
        "wordlessVocalise": entry.wordless,
    }
    methods = []
    if cross["marked"]:
        methods.append("cross_recording_similarity")
    if entry.repeated["marked"]:
        methods.append("repetition_structure")
    if entry.wordless["marked"]:
        methods.append("wordless_vocalise_signal")
    confidence = js.js_max(
        cross["confidence"], entry.repeated["confidence"], entry.wordless["confidence"]
    )
    result: Json = {}
    _put(result, "recordingId", js.get(entry.row, "recordingId"))
    _put(result, "filename", js.get(entry.row, "filename"))
    result["capturedAt"] = js.coalesce(js.get(entry.row, "capturedAt"), None)
    result["year"] = _year_of(entry.row)
    result["lyricMatch"] = {
        "marked": len(methods) > 0,
        "status": "marked_indicator" if methods else "not_marked",
        "method": "+".join(methods) or "none",
        "confidence": to_fixed(confidence),
        "matchedCluster": cluster["clusterId"] if cluster is not None else None,
        "matchedClusterLabel": cluster["label"] if cluster is not None else None,
        "rationaleCodes": _rationale_codes_for(signals),
        "signals": signals,
    }
    return result


class _UnionFind:
    def __init__(self, values: list[Any]) -> None:
        self.parents: dict[Any, Any] = {value: value for value in values}

    def find(self, value: Any) -> Any:
        root = value
        while (parent := self.parents.get(root, root)) != root:
            root = parent
        while value != root:
            parent = self.parents.get(value, value)
            self.parents[value] = root
            value = parent
        return root

    def union(self, left: Any, right: Any) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root


def _cluster_transcripts(analyzed: list[_Analyzed], options: LyricOptions) -> list[Json]:
    candidates = [entry for entry in analyzed if entry.shingles]
    union = _UnionFind([_recording_id(entry) for entry in candidates])
    edges: list[tuple[Any, Any, float]] = []
    for left_index, left in enumerate(candidates):
        for right in candidates[left_index + 1 :]:
            similarity = _jaccard(left.shingles, right.shingles)
            if similarity >= options.cluster_threshold:
                union.union(_recording_id(left), _recording_id(right))
                edges.append((_recording_id(left), _recording_id(right), similarity))

    groups: dict[Any, list[_Analyzed]] = {}
    for entry in candidates:
        groups.setdefault(union.find(_recording_id(entry)), []).append(entry)

    clusters: list[Json] = []
    for entries in groups.values():
        if len(entries) < options.min_cluster_size:
            continue
        ids = sorted((_recording_id(entry) for entry in entries), key=_id_key)
        members = set(ids)
        related = [edge[2] for edge in edges if edge[0] in members and edge[1] in members]
        confidence = js.js_max(*related) if related else DEFAULT_CLUSTER_THRESHOLD
        clusters.append(
            {
                "seed": ids[0],
                "ids": ids,
                "years": _unique_sorted([_year_of(entry.row) for entry in entries]),
                "confidence": to_fixed(
                    js.js_min(0.99, 0.45 + confidence * 0.7 + min(len(entries), 8) * 0.025)
                ),
            }
        )

    clusters.sort(
        key=lambda cluster: (
            -len(cluster["ids"]),
            js.locale_compare_key(js.to_string(cluster["seed"])),
        )
    )
    return [
        {
            "clusterId": f"lyric-cluster-{index:03d}",
            "label": f"recurring_lyric_cluster_{index:03d}",
            "method": "cross_recording_shingle_similarity.v1",
            "recordingIds": cluster["ids"],
            "years": cluster["years"],
            "size": len(cluster["ids"]),
            "confidence": cluster["confidence"],
            "containsRawLyricText": False,
        }
        for index, cluster in enumerate(clusters, start=1)
    ]


def _id_key(value: Any) -> bytes:
    return js.utf16_key(js.to_string(js.MISSING if value is None else value))


def repetition_signal(tokens: list[str]) -> Json:
    if len(tokens) < 12:
        return {
            "marked": False,
            "method": "repeated_ngram_density.v1",
            "confidence": 0,
            "repeatedNgramCount": 0,
        }
    counts: dict[str, int] = {}
    for gram in _ngrams(tokens, 4):
        counts[gram] = counts.get(gram, 0) + 1
    repeated = sum(1 for count in counts.values() if count >= 2)
    density = repeated / max(len(counts), 1)
    return {
        "marked": repeated >= 2 or density >= 0.12,
        "method": "repeated_ngram_density.v1",
        "confidence": to_fixed(min(0.9, 0.35 + repeated * 0.08 + density)),
        "repeatedNgramCount": repeated,
    }


def _strict_zero(value: Any) -> bool:
    return js.is_number(value) and value == 0


def wordless_signal(row: Json) -> Json:
    words = js.coalesce(js.get(js.get(row, "counts"), "words"), 0)
    language = js.coalesce(js.get(js.get(row, "language"), "detected"), "unknown")
    non_english = not (isinstance(language, str) and language in WORDLESS_LANGUAGE_EXCLUSIONS)
    marked = _strict_zero(words) or non_english
    return {
        "marked": marked,
        "method": "zero_word_or_non_en_stt_language.v1",
        "confidence": (0.8 if _strict_zero(words) else 0.62) if marked else 0,
        "wordCount": words,
        "detectedLanguage": language,
    }


def _rationale_codes_for(signals: Json) -> list[str]:
    codes = []
    wordless = signals["wordlessVocalise"]
    if signals["crossRecordingCluster"]["marked"]:
        codes.append("recurs_across_recordings")
    if signals["repetitionStructure"]["marked"]:
        codes.append("repetition_structure_detected")
    if _strict_zero(wordless["wordCount"]):
        codes.append("zero_word_transcript")
    if wordless["marked"] and js.to_number(wordless["wordCount"]) > 0:
        codes.append("non_en_language_candidate_vocalise")
    return codes


def _summarize(results: list[Json], clusters: list[Json]) -> Json:
    method_counts: dict[str, int] = {}
    marked = wordless = repetition = cross = 0
    for result in results:
        match = result["lyricMatch"]
        if match["marked"]:
            marked += 1
        for method in match["method"].split("+"):
            if method and method != "none":
                method_counts[method] = method_counts.get(method, 0) + 1
        if match["signals"]["wordlessVocalise"]["marked"]:
            wordless += 1
        if match["signals"]["repetitionStructure"]["marked"]:
            repetition += 1
        if match["signals"]["crossRecordingCluster"]["marked"]:
            cross += 1
    return {
        "recordings": len(results),
        "marked": marked,
        "notMarked": len(results) - marked,
        "clusters": len(clusters),
        "crossRecording": cross,
        "repetition": repetition,
        "wordlessVocalise": wordless,
        "methodCounts": method_counts,
    }


def _results_length(manifest: Json, default: Any) -> Any:
    rows = js.get(manifest, "results")
    if isinstance(rows, list):
        return len(rows)
    return default


def _source_transcripts_for(manifest: Json) -> Json:
    source: Json = {}
    _put(source, "schemaVersion", js.get(manifest, "schemaVersion"))
    _put(source, "generatedAt", js.get(manifest, "generatedAt"))
    _put(source, "updatedAt", js.get(manifest, "updatedAt"))
    fallback = js.coalesce(js.get(js.get(manifest, "sourceIndex"), "recordingCount"), None)
    source["recordingCount"] = _results_length(manifest, fallback)
    source["localTranscriptStore"] = js.coalesce(
        js.get(js.get(manifest, "localTranscriptStore"), "root"), None
    )
    return source


def _strategy(options: LyricOptions) -> Json:
    return {
        "methods": [
            "cross_recording_shingle_similarity.v1",
            "repeated_ngram_density.v1",
            "zero_word_or_non_en_stt_language.v1",
        ],
        "clusterThreshold": options.cluster_threshold,
        "minClusterSize": options.min_cluster_size,
        "externalLyricDatabases": False,
        "rawLyricTextInOutput": False,
    }


def tokenize(text: str) -> list[str]:
    """Lower-case, NFKD, strip combining marks, keep runs of ``[a-z0-9']``, trim
    apostrophes, drop one-character tokens."""

    folded = _COMBINING.sub("", unicodedata.normalize("NFKD", text.lower()))
    tokens = (token.strip("'") for token in _WORD.findall(folded))
    return [token for token in tokens if len(token) > 1]


def _shingle_hashes(tokens: list[str], size: int) -> set[str]:
    return {hashlib.sha256(js.utf8(gram)).hexdigest()[:16] for gram in _ngrams(tokens, size)}


def _ngrams(tokens: list[str], size: int) -> list[str]:
    return [" ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1)]


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0
    intersection = len(left & right)
    return intersection / (len(left) + len(right) - intersection)


def _year_for(captured_at: Any) -> Any:
    return js.slice_utf16(captured_at, 4) if js.truthy(captured_at) else "unknown"


def _unique_sorted(values: list[Any]) -> list[Any]:
    unique = list(dict.fromkeys(value for value in values if js.truthy(value)))
    return sorted(unique, key=lambda value: js.utf16_key(js.to_string(value)))


def run(argv: Sequence[str]) -> int:
    options = parse_args(argv)
    if options.help:
        sys.stdout.write(HELP)
        return 0
    if options.command == "analyze":
        transcript_manifest = read_json(options.transcripts or "")
        if options.dry_run:
            write_json("-", build_dry_run(transcript_manifest, options))
            return 0
        manifest = analyze_lyric_indicators(transcript_manifest, options)
        write_json(options.out or "", manifest)
        return 0
    raise LyricIndicatorError(f"unsupported command: {options.command}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except Exception as error:  # the JavaScript reports every Error the same way
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

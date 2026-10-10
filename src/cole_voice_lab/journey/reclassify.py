"""Evidence-join reclassifier (port of voice-journey's ``reclassify.mjs``).

Joins the full-corpus filter results with the lyric indicator manifest and resolves
``uncertain_manual_review`` rows that carry singing evidence (a recurring lyric cluster,
a wordless vocalise signal, repeated-phrase structure). Contamination of a resolved row
uses the v1 heuristic's noise and music score formulas. Decided rows are never
relabelled; a ``non_singing`` row inside a recurring cluster gets an
``evidenceConflict`` flag. Reads manifests only: no audio and no transcript text.

Same manifest bytes as the JavaScript, including its coercions of missing (``NaN``) and
``null`` (``0``) features in the score formulas and the rationale text. The defaults are
resolved when the command runs (the JavaScript resolved them at import).
"""

from __future__ import annotations

import copy
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey.jsonio import iso_timestamp, to_fixed, write_json
from cole_voice_lab.journey.paths import resolve_paths

RECLASSIFY_METHOD = "voice-journey.evidence-join-reclassify.v2"

# Contamination score constants mirror the v1 classifier (filter-proof.mjs,
# voice-journey.local-heuristic-proof.v1). A test cross-checks agreement.
NOISE_SCORE_THRESHOLD = 0.55
MUSIC_SCORE_THRESHOLD = 0.62

RULES: list[dict[str, str]] = [
    {
        "id": "recurring_cluster",
        "description": "Recording sits in a cross-recording lyric cluster (recurring repertoire across the archive) — strong singing evidence.",  # noqa: E501
    },
    {
        "id": "wordless_vocalise",
        "description": "Wordless/vocalise STT signal (zero words or non-English detection) with voicedRatio >= 0.3 — sung vocalise evidence.",  # noqa: E501
    },
    {
        "id": "repetition_structure",
        "description": "Repeated-phrase transcript structure (>= 3 repeated 4-grams) with voicedRatio >= 0.3 — chorus-like singing evidence.",  # noqa: E501
    },
]

HELP = """Voice Journey evidence-join reclassifier.

Usage:
  voice-journey-reclassify apply --dry-run [--results PATH] [--lyrics PATH]
  voice-journey-reclassify apply [--results PATH] [--lyrics PATH] [--out PATH] [--reclassified-at ISO]

Joins the committed full-corpus filter results with the committed lyric
indicator manifest and resolves uncertain_manual_review rows that carry
singing evidence (recurring clusters, wordless vocalise, repetition
structure). Contamination for resolved rows uses the same noise/music score
formulas as the v1 heuristic; rows without evidence stay in the review
bucket. Decided rows are never changed, but non_singing rows that sit in a
recurring cluster get a repo-safe evidenceConflict flag for review.

Reads committed repo-safe manifests only: no audio, no transcript text, no
host-only paths. Dry-run computes the same result and prints counters
without writing.
"""  # noqa: E501

Json = dict[str, Any]


class ReclassifyError(Exception):
    pass


@dataclass
class ReclassifyOptions:
    results: str
    lyrics: str
    out: str | None = None
    out_set: bool = False
    reclassified_at: str | None = None
    dry_run: bool = False
    command: str = "apply"
    help: bool = False


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else ""
    if not value or value.startswith("--"):
        raise ReclassifyError(f"{flag} requires a value")
    return value


def parse_args(argv: Sequence[str]) -> ReclassifyOptions:
    argv = list(argv)
    maybe_command = argv[0] if argv else ""
    has_command = bool(maybe_command) and not maybe_command.startswith("--")
    has_command = has_command and maybe_command != "help"
    paths = resolve_paths()
    default_results = str(paths.manifest("full-corpus-filter-results.json"))
    options = ReclassifyOptions(
        command=maybe_command if has_command else "apply",
        results=default_results,
        lyrics=str(paths.manifest("local-lyric-indicators.json")),
        out=default_results,
    )
    args = argv[1:] if has_command else argv
    while args:
        flag = args.pop(0)
        if flag == "--dry-run":
            options.dry_run = True
        elif flag == "--lyrics":
            options.lyrics = _require_value(args, flag)
        elif flag == "--out":
            options.out = _require_value(args, flag)
            options.out_set = True
        elif flag == "--reclassified-at":
            options.reclassified_at = _require_value(args, flag)
        elif flag == "--results":
            options.results = _require_value(args, flag)
        elif flag in ("--help", "help"):
            options.help = True
        else:
            raise ReclassifyError(f"unsupported argument: {flag}")
    return options


def _clamp(value: float, low: float = 0, high: float = 1) -> float:
    return js.js_min(js.js_max(value, low), high)


def _feature(features: Json, key: str) -> float:
    return js.to_number(js.get(features, key))


def noise_score_for(features: Json) -> float:
    return _clamp(
        (_feature(features, "meanZeroCrossingRate") - 0.12) * 3
        + (1 - _feature(features, "meanPitchConfidence")) * 0.4
        + _feature(features, "clippingRatio") * 10
    )


def music_score_for(features: Json) -> float:
    pitch_stability = js.to_number(js.coalesce(js.get(features, "pitchStability"), 1))
    return _clamp(
        (_feature(features, "activeRatio") - 0.78) * 1.1
        + (_feature(features, "voicedRatio") - 0.65) * 0.9
        + (0.25 if pitch_stability < 0.08 else 0)
    )


def _final_bucket_for(content_label: str, contamination_label: str) -> str:
    if content_label == "non_singing":
        return "non_singing"
    if content_label != "singing":
        return "uncertain_manual_review"
    return {
        "clean": "clean_singing",
        "noise_contaminated": "noise_contaminated_singing",
        "music_contaminated": "music_contaminated_singing",
    }.get(contamination_label, "uncertain_manual_review")


def _lyric_signals_by_recording_id(lyrics_manifest: Json) -> dict[Any, Any]:
    signals: dict[Any, Any] = {}
    rows = js.coalesce(js.get(lyrics_manifest, "results"), [])
    for row in rows:
        recording_id = js.get(row, "recordingId")
        if js.truthy(recording_id):
            signals[recording_id] = js.coalesce(js.get(row, "lyricMatch"), None)
    return signals


def _voiced(features: Json) -> float:
    return js.to_number(js.coalesce(js.get(features, "voicedRatio"), 0))


def _or_null(value: Any) -> Any:
    return js.coalesce(value, None)


def match_rule(lyric_match: Any, features: Json) -> Json | None:
    if not js.truthy(lyric_match):
        return None
    signals = js.get(lyric_match, "signals")
    cluster = js.get(signals, "crossRecordingCluster")
    if js.truthy(js.get(cluster, "marked")):
        size = js.to_number(js.coalesce(js.get(cluster, "clusterSize"), 0))
        return {
            "rule": "recurring_cluster",
            "contentConfidence": _clamp(
                0.6 + js.js_min(size, 10) * 0.02 + js.js_max(_voiced(features) - 0.22, 0) * 0.25,
                0,
                0.9,
            ),
            "signals": {
                "clusterId": _or_null(js.get(cluster, "clusterId")),
                "clusterLabel": _or_null(js.get(cluster, "clusterLabel")),
                "clusterSize": _or_null(js.get(cluster, "clusterSize")),
                "clusterConfidence": _or_null(js.get(cluster, "confidence")),
            },
        }
    wordless = js.get(signals, "wordlessVocalise")
    if js.truthy(js.get(wordless, "marked")) and _voiced(features) >= 0.3:
        word_count = js.get(wordless, "wordCount")
        return {
            "rule": "wordless_vocalise",
            "contentConfidence": 0.7 if js.is_number(word_count) and word_count == 0 else 0.62,
            "signals": {
                "wordCount": _or_null(word_count),
                "detectedLanguage": _or_null(js.get(wordless, "detectedLanguage")),
            },
        }
    repetition = js.get(signals, "repetitionStructure")
    count = js.coalesce(js.get(repetition, "repeatedNgramCount"), 0)
    if (
        js.truthy(js.get(repetition, "marked"))
        and js.to_number(count) >= 3
        and _voiced(features) >= 0.3
    ):
        return {
            "rule": "repetition_structure",
            "contentConfidence": _clamp(0.55 + js.js_min(js.to_number(count), 10) * 0.015, 0, 0.7),
            "signals": {"repeatedNgramCount": _or_null(js.get(repetition, "repeatedNgramCount"))},
        }
    return None


def _score_number(score: float | None) -> float:
    return 0.0 if score is None else score


def reclassify_row(row: Json, lyric_match: Any) -> Json | None:
    features = js.coalesce(js.get(row, "features"), {})
    matched = match_rule(lyric_match, features)
    if matched is None:
        return None

    prior_content_label = _or_null(js.get(js.get(row, "classification"), "contentLabel"))
    content_confidence = _clamp(
        matched["contentConfidence"] + (0.05 if prior_content_label == "singing" else 0), 0, 0.9
    )
    noise_score = to_fixed(noise_score_for(features), 3)
    music_score = to_fixed(music_score_for(features), 3)
    noise = _score_number(noise_score)
    music = _score_number(music_score)
    contamination_label = "clean"
    contamination_confidence = _clamp(
        0.55 + (NOISE_SCORE_THRESHOLD - js.js_max(noise, music)) * 0.3, 0, 0.75
    )
    if music >= MUSIC_SCORE_THRESHOLD:
        contamination_label = "music_contaminated"
        contamination_confidence = music
    elif noise >= NOISE_SCORE_THRESHOLD:
        contamination_label = "noise_contaminated"
        contamination_confidence = noise
    final_bucket = _final_bucket_for("singing", contamination_label)
    confidence = to_fixed(js.js_min(content_confidence, contamination_confidence), 3)
    text = js.to_string
    rationale = (
        f"content=singing(evidence:{matched['rule']}); "
        f"contamination={contamination_label}(noiseScore={text(noise_score)}; "
        f"musicScore={text(music_score)}); priorContent={text(prior_content_label)}; "
        f"voicedRatio={text(js.get(features, 'voicedRatio'))}; "
        f"pitchConfidence={text(js.get(features, 'meanPitchConfidence'))}; "
        f"zcr={text(js.get(features, 'meanZeroCrossingRate'))}"
    )
    return {
        "classification": {
            "method": RECLASSIFY_METHOD,
            "contentLabel": "singing",
            "contaminationLabel": contamination_label,
            "finalBucket": final_bucket,
            "confidence": confidence,
            "rationale": rationale,
        },
        "evidence": {
            "rule": matched["rule"],
            "signals": matched["signals"],
            "priorContentLabel": prior_content_label,
            "contaminationScores": {"noiseScore": noise_score, "musicScore": music_score},
            "sources": {
                "lyricIndicators": True,
                "v1Features": True,
                "audioBytes": False,
                "transcriptText": False,
            },
        },
    }


def _conflict_for(row: Json, lyric_match: Any, reclassified_at: str) -> Json | None:
    if js.get(js.get(row, "classification"), "finalBucket") != "non_singing":
        return None
    cluster = js.get(js.get(lyric_match, "signals"), "crossRecordingCluster")
    if not js.truthy(js.get(cluster, "marked")):
        return None
    return {
        "type": "non_singing_in_recurring_cluster",
        "clusterId": _or_null(js.get(cluster, "clusterId")),
        "clusterSize": _or_null(js.get(cluster, "clusterSize")),
        "flaggedAt": reclassified_at,
        "note": "Classified non_singing but its transcript recurs across recordings; verify by listening.",  # noqa: E501
    }


def _spot_check_recommended(classification: Json) -> bool:
    return bool(
        classification["finalBucket"] == "uncertain_manual_review"
        or js.to_number(classification["confidence"]) < 0.7
    )


def _with_recommended(spot_check: Any, recommended: bool) -> Json:
    base = dict(spot_check) if isinstance(spot_check, dict) else {}
    base["recommended"] = recommended
    return base


def apply_reclassification(
    results_manifest: Json, lyrics_manifest: Json, options: ReclassifyOptions
) -> tuple[Json, Json]:
    """Reclassify in place; returns ``(manifest, counters)`` (the JS ``{manifest,
    counters}``)."""

    reclassified_at = (
        options.reclassified_at if options.reclassified_at is not None else iso_timestamp()
    )
    lyric_by_id = _lyric_signals_by_recording_id(lyrics_manifest)
    resolved_by_rule = {"recurring_cluster": 0, "wordless_vocalise": 0, "repetition_structure": 0}
    resolved_to_bucket = {
        "clean_singing": 0,
        "noise_contaminated_singing": 0,
        "music_contaminated_singing": 0,
    }
    counters: Json = {
        "rowsExamined": 0,
        "uncertainBefore": 0,
        "uncertainAfter": 0,
        "resolvedByRule": resolved_by_rule,
        "resolvedToBucket": resolved_to_bucket,
        "decidedRowsUntouched": 0,
        "conflictsFlagged": 0,
    }

    for row in js.coalesce(js.get(results_manifest, "results"), []):
        counters["rowsExamined"] += 1
        lyric_match = js.coalesce(lyric_by_id.get(js.get(row, "recordingId")), None)
        if js.get(js.get(row, "classification"), "finalBucket") != "uncertain_manual_review":
            counters["decidedRowsUntouched"] += 1
            conflict = _conflict_for(row, lyric_match, reclassified_at)
            if conflict is not None:
                row["evidenceConflict"] = conflict
                row["humanSpotCheck"] = _with_recommended(row.get("humanSpotCheck"), True)
                counters["conflictsFlagged"] += 1
            continue
        counters["uncertainBefore"] += 1
        resolved = reclassify_row(row, lyric_match)
        if resolved is None:
            counters["uncertainAfter"] += 1
            continue
        row["previousClassification"] = row["classification"]
        row["classification"] = resolved["classification"]
        row["evidence"] = resolved["evidence"]
        row["humanSpotCheck"] = _with_recommended(
            row.get("humanSpotCheck"), _spot_check_recommended(resolved["classification"])
        )
        resolved_by_rule[resolved["evidence"]["rule"]] += 1
        resolved_to_bucket[resolved["classification"]["finalBucket"]] += 1

    results_manifest["reclassification"] = {
        "method": RECLASSIFY_METHOD,
        "reclassifiedAt": reclassified_at,
        "rules": RULES,
        "scope": "uncertain_manual_review rows only; decided rows are never relabelled (non_singing rows in recurring clusters get evidenceConflict flags)",  # noqa: E501
        "contamination": {
            "formulas": "noiseScore/musicScore mirror voice-journey.local-heuristic-proof.v1",
            "noiseScoreThreshold": NOISE_SCORE_THRESHOLD,
            "musicScoreThreshold": MUSIC_SCORE_THRESHOLD,
            "cleanRule": "evidence-backed singing with no contamination flag resolves to clean",
        },
        "sources": {
            "results": _source_provenance(results_manifest, options.results),
            "lyrics": _source_provenance(lyrics_manifest, options.lyrics),
        },
        "counters": counters,
        "readScope": _read_scope(),
        "repoSafe": {
            "containsTranscriptText": False,
            "containsAudioBytes": False,
            "containsLyricText": False,
        },
    }
    results_manifest["updatedAt"] = reclassified_at
    return results_manifest, counters


def _source_provenance(manifest: Json, manifest_path: str) -> Json:
    return {
        "path": manifest_path,
        "schemaVersion": _or_null(js.get(manifest, "schemaVersion")),
        "generatedAt": _or_null(js.get(manifest, "generatedAt")),
        "updatedAt": _or_null(js.get(manifest, "updatedAt")),
    }


def _read_scope() -> Json:
    return {
        "committedManifestsOnly": True,
        "audioBytes": False,
        "transcriptText": False,
        "hostOnlyPaths": False,
        "upload": False,
        "corpusMutation": False,
    }


def build_reclassify_dry_run(
    results_manifest: Json, lyrics_manifest: Json, options: ReclassifyOptions
) -> Json:
    cloned = copy.deepcopy(results_manifest)
    dry_options = copy.copy(options)
    if dry_options.reclassified_at is None:
        dry_options.reclassified_at = "dry-run"
    _, counters = apply_reclassification(cloned, lyrics_manifest, dry_options)
    return {
        "dryRun": True,
        "operation": "evidence-join-reclassification",
        "architecture": {
            "slug": "kickoff-corpus-flow",
            "reads": ["filter-results-manifest", "lyric-indicator-manifest"],
            "writes": ["filter-results-manifest"],
        },
        "resultsPath": options.results,
        "lyricsPath": options.lyrics,
        "outputPath": _target_for(options),
        "method": RECLASSIFY_METHOD,
        "rules": RULES,
        "counters": counters,
        "readScope": _read_scope(),
    }


def _target_for(options: ReclassifyOptions) -> str:
    return options.out if options.out_set and options.out is not None else options.results


def run(argv: Sequence[str]) -> int:
    options = parse_args(argv)
    if options.help:
        sys.stdout.write(HELP)
        return 0
    if options.command == "apply":
        results_manifest = js.read_json(options.results)
        lyrics_manifest = js.read_json(options.lyrics)
        if options.dry_run:
            write_json("-", build_reclassify_dry_run(results_manifest, lyrics_manifest, options))
            return 0
        manifest, _ = apply_reclassification(results_manifest, lyrics_manifest, options)
        write_json(_target_for(options), manifest)
        return 0
    raise ReclassifyError(f"unsupported command: {options.command}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except Exception as error:  # the JavaScript reports every Error the same way
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

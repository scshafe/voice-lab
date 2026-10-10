"""Per-recording voice-feature extraction (features.mjs, Arc 2 feature contract v1).

Dry-run reads only the index and filter-results manifests and reports the selection,
tools and resume plan. A real run decodes each selected recording with ffmpeg to a
temporary mono WAV (deleted afterwards), hands it to the feature worker, writes the
detailed per-recording feature JSON to the out-of-tree feature store and appends only
summary aggregates plus a content digest to the repo-safe manifest. Resumable by
``recordingId``; a failed recording records a failed row and the run continues.

The worker is ``resources/journey_extract_features.py`` (voice-journey's
``analysis/extract_features.py``, moved in as is), spawned as ``<venv python> <worker>``
with the isolated analysis runtime's python (``resolve_tools().venv_python``); the
stdin/stdout protocol is unchanged: a ``{"hello": ...}`` line, then one JSON result
line per JSON task line.

Differences from the JS: the default ``--worker`` is the packaged resource's absolute
path (it was ``analysis/extract_features.py``), so ``toolRun.extractor`` and
``workerScript`` name it; JSON files are written atomically; a failed ffmpeg decode
removes its temporary directory; a worker that dies or misbehaves is reaped instead of
leaving the process waiting on it. ``JSON.parse`` is followed (``NaN``/``Infinity`` are
refused; ``jsonio.read_json``), and the JavaScript value rules (``?.``, ``??``,
``Number()``, ``path.join``) are ``journey/jscompat.py``'s. The constant strings that
feed the configuration fingerprint and the dry-run text (``workerConfigAuthority``,
``provisioning``, the analysis tool's name) are kept verbatim so manifests and
fingerprints match voice-journey's.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Any, Protocol

from ..resources import resource_path
from . import jscompat as js
from .jsonio import (
    format_number,
    iso_timestamp,
    manifest_text,
    parse_json,
    read_json,
    stringify_compact,
    write_json,
)
from .paths import is_inside_artifacts, resolve_paths, resolve_tools

MANIFEST_SCHEMA_VERSION = "voice-journey.local-voice-features.v1"
METHOD_ID = "voice-journey.feature-extract.v1"
WORKER_RESOURCE = "journey_extract_features.py"
DEFAULT_SAMPLE_RATE = 22050
DEFAULT_BUCKETS = "clean_singing"

_MAX_SAFE_INTEGER = 2**53 - 1

HELP = """Voice Journey per-recording feature extraction (Arc 2, feature contract v1).

Usage:
  voice-journey-features extract-index --dry-run [--index PATH] [--results PATH] [--buckets CSV]
  voice-journey-features extract-index --index PATH --results PATH --corpus-root PATH --approval TEXT [--buckets CSV] [--out PATH] [--feature-dir PATH] [--venv-python PATH] [--worker PATH] [--ffmpeg-bin PATH] [--sample-rate HZ] [--limit N]

Dry-run reads only committed manifests and reports the selection, tools, and
resume plan. Non-dry-run decodes each selected recording to a temporary mono
WAV (deleted afterwards), runs the local Python analysis worker, writes the
detailed per-recording feature JSON to the gitignored feature store, and
appends only summary aggregates plus a content digest to the repo-safe
manifest. Resumable by recordingId; failed recordings record a failed row and
the run continues.
"""  # noqa: E501


class FeaturesError(Exception):
    """A refusal or failure of the feature extraction (``error: <message>``, exit 1)."""


def _default_manifest(name: str) -> str:
    return str(resolve_paths().manifest(name))


def _default_feature_dir() -> str:
    return str(resolve_paths().artifact("voice-features"))


def _default_ffmpeg() -> str:
    return resolve_tools().ffmpeg_bin


def _default_venv_python() -> str:
    return resolve_tools().venv_python


def _default_worker() -> str:
    return str(resource_path(WORKER_RESOURCE))


@dataclass
class FeaturesOptions:
    """The parsed command line (parseArgs' options); the path defaults follow the
    environment when the options are made."""

    approval: str | None = None
    buckets: str = DEFAULT_BUCKETS
    command: str = "extract-index"
    corpus_root: str | None = None
    dry_run: bool = False
    feature_dir: str = field(default_factory=_default_feature_dir)
    ffmpeg_bin: str = field(default_factory=_default_ffmpeg)
    generated_at: str | None = None
    index: str = field(default_factory=lambda: _default_manifest("recording-index.json"))
    limit: int | float | None = None
    out: str = field(default_factory=lambda: _default_manifest("local-voice-features.json"))
    results: str = field(
        default_factory=lambda: _default_manifest("full-corpus-filter-results.json")
    )
    sample_rate: int | float = DEFAULT_SAMPLE_RATE
    venv_python: str = field(default_factory=_default_venv_python)
    worker: str = field(default_factory=_default_worker)
    help: bool = False


@dataclass(frozen=True)
class Selection:
    """One selected recording: its index entry and its filter bucket."""

    entry: dict[str, Any]
    bucket: Any


@dataclass(frozen=True)
class DecodedRecording:
    """A temporary WAV and the callable that removes it."""

    wav_path: str
    cleanup: Callable[[], None]


class FeatureWorker(Protocol):
    """The feature worker's interface (createFeatureWorker's object)."""

    @property
    def versions(self) -> Any: ...

    @property
    def config(self) -> Any: ...

    def extract(self, task: dict[str, Any]) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class FeatureHooks:
    """Test seams (the JS ``hooks``): the worker factory, the decoder, tool versions."""

    create_worker: Callable[[FeaturesOptions], FeatureWorker] | None = None
    decode_recording: Callable[[dict[str, Any], FeaturesOptions], DecodedRecording] | None = None
    tool_versions: Mapping[str, Any] | None = None


def _generated_at(options: FeaturesOptions) -> str:
    """``options.generatedAt ?? new Date().toISOString()``."""

    return options.generated_at if options.generated_at is not None else iso_timestamp()


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(js.utf8(text)).hexdigest()


def _spawn_message(command: str, error: OSError) -> str:
    """Node's spawn error text, ``spawn <command> <CODE>``."""

    code = errno.errorcode.get(error.errno or 0, "UNKNOWN")
    return f"spawn {command} {code}"


def _exit_code_text(returncode: int) -> str:
    """Node reports a signal-terminated child's exit code as ``null``."""

    return str(returncode) if returncode >= 0 else "null"


# --- command line ------------------------------------------------------------------


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else None
    if not value or value.startswith("--"):
        raise FeaturesError(f"{flag} requires a value")
    return value


def _parse_positive_integer(value: str, flag: str) -> int | float:
    parsed = js.string_to_number(value)
    if not math.isfinite(parsed) or not parsed.is_integer() or parsed <= 0:
        raise FeaturesError(f"{flag} must be a positive integer")
    return int(parsed) if parsed <= _MAX_SAFE_INTEGER else parsed


def parse_args(argv: Sequence[str]) -> FeaturesOptions:
    arguments = list(argv)
    maybe_command = arguments[0] if arguments else None
    has_command = bool(
        maybe_command and not maybe_command.startswith("--") and maybe_command != "help"
    )
    command = maybe_command if has_command and maybe_command else "extract-index"
    args = arguments[1:] if has_command else arguments
    options = FeaturesOptions(command=command)

    while args:
        flag = args.pop(0)
        if flag == "--approval":
            options.approval = _require_value(args, flag)
        elif flag == "--buckets":
            options.buckets = _require_value(args, flag)
        elif flag == "--corpus-root":
            options.corpus_root = _require_value(args, flag)
        elif flag == "--dry-run":
            options.dry_run = True
        elif flag == "--feature-dir":
            options.feature_dir = _require_value(args, flag)
        elif flag == "--ffmpeg-bin":
            options.ffmpeg_bin = _require_value(args, flag)
        elif flag == "--generated-at":
            options.generated_at = _require_value(args, flag)
        elif flag == "--index":
            options.index = _require_value(args, flag)
        elif flag == "--limit":
            options.limit = _parse_positive_integer(_require_value(args, flag), flag)
        elif flag == "--out":
            options.out = _require_value(args, flag)
        elif flag == "--results":
            options.results = _require_value(args, flag)
        elif flag == "--sample-rate":
            options.sample_rate = _parse_positive_integer(_require_value(args, flag), flag)
        elif flag == "--venv-python":
            options.venv_python = _require_value(args, flag)
        elif flag == "--worker":
            options.worker = _require_value(args, flag)
        elif flag in ("--help", "help"):
            options.help = True
        else:
            raise FeaturesError(f"unsupported argument: {flag}")
    return options


def _write_json(file_path: str, payload: Any) -> None:
    """writeJson: ``-`` is stdout; a file is announced by the path as given."""

    if file_path == "-":
        write_json("-", payload)
        return
    write_json(file_path, payload, announce=False)
    sys.stdout.write(f"{file_path}\n")


# --- manifests ---------------------------------------------------------------------


def _year_for(entry: dict[str, Any]) -> Any:
    captured_at: Any = entry.get("capturedAt")
    return captured_at[:4] if js.truthy(captured_at) else "unknown"


def _bucket_set_for(options: FeaturesOptions) -> list[str]:
    """The bucket Set, in insertion order."""

    buckets: dict[str, None] = {}
    for bucket in options.buckets.split(","):
        trimmed = js.trim(bucket)
        if trimmed:
            buckets[trimmed] = None
    return list(buckets)


def select_recordings(
    index_manifest: Any, results_manifest: Any, options: FeaturesOptions
) -> list[Selection]:
    buckets = set(_bucket_set_for(options))
    bucket_by_id: dict[Any, Any] = {}
    for row in js.coalesce(js.get(results_manifest, "results"), []):
        bucket_by_id[js.get(row, "recordingId")] = js.coalesce(
            js.get(js.get(row, "classification"), "finalBucket"), "unclassified"
        )
    selected: list[Selection] = []
    for entry in js.coalesce(js.get(index_manifest, "recordings"), []):
        bucket = js.coalesce(bucket_by_id.get(js.get(entry, "recordingId")), "unclassified")
        if not isinstance(bucket, str) or bucket not in buckets:
            continue
        selected.append(Selection(entry=entry, bucket=bucket))
        if options.limit and len(selected) >= options.limit:
            break
    return selected


def _features_read_scope() -> dict[str, Any]:
    return {
        "selectedBucketsOnly": True,
        "audioBytes": True,
        "decodeToTemporaryWav": True,
        "retainedAudioBytes": False,
        "featureArraysRetainedLocally": True,
        "featureArraysCommittedToRepo": False,
        "transcriptText": False,
        "upload": False,
        "corpusMutation": False,
    }


def _analysis_config_for(
    options: FeaturesOptions, versions: Any = None, worker_config: Any = None
) -> dict[str, Any]:
    return {
        "methodId": METHOD_ID,
        "sampleRateHz": options.sample_rate,
        "decode": "ffmpeg to temporary mono WAV at sampleRateHz, deleted after each recording",
        "workerConfigAuthority": (
            "analysis/extract_features.py CONFIG governs detailed parameters; the worker "
            "reports it in its hello line and it is embedded here"
        ),
        "workerConfig": worker_config,
        "versions": versions,
    }


def config_fingerprint_for(
    options: FeaturesOptions, versions: Any, worker_config: Any = None
) -> str:
    return _sha256_hex(stringify_compact(_analysis_config_for(options, versions, worker_config)))


def _ffmpeg_tool_choice(
    options: FeaturesOptions, tool_versions: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "name": "ffmpeg",
        "version": tool_versions.get("ffmpeg") if tool_versions else None,
        "stage": "decode-to-temporary-mono-wav",
        "openness": "open-source",
        "openSource": True,
        "path": options.ffmpeg_bin,
        "notes": (
            f"Decodes approved local audio to {format_number(options.sample_rate)} Hz mono WAV "
            "in a temporary directory, deleted after each recording."
        ),
    }


def _analysis_tool_choice(options: FeaturesOptions, versions: Any = None) -> dict[str, Any]:
    return {
        "name": "praat-parselmouth + librosa (analysis/.venv)",
        "version": stringify_compact(versions) if js.truthy(versions) else None,
        "stage": "per-recording-voice-feature-extraction",
        "openness": "open-source",
        "openSource": True,
        "path": options.venv_python,
        "workerScript": options.worker,
        "methodId": METHOD_ID,
        "openSwapCandidate": "Essentia; torchcrepe planned as optional --f0-engine upgrade",
        "notes": (
            "Praat autocorrelation f0 + Praat voice-quality metrics + librosa spectral "
            "features. Detailed arrays stay in the gitignored local feature store."
        ),
    }


def _resume_behavior() -> dict[str, Any]:
    return {
        "enabled": True,
        "key": "recordingId",
        "outputWrittenAfterEachRecording": True,
        "existingCompletedResultsSkipped": True,
        "failedRowsRetriedOnRerun": True,
    }


def build_features_dry_run(
    index_manifest: Any, results_manifest: Any, options: FeaturesOptions
) -> dict[str, Any]:
    selected = select_recordings(index_manifest, results_manifest, options)
    by_bucket: dict[str, int] = {}
    for item in selected:
        by_bucket[item.bucket] = by_bucket.get(item.bucket, 0) + 1
    return {
        "dryRun": True,
        "operation": "voice-feature-extraction",
        "architecture": {
            "slug": "kickoff-corpus-flow",
            "reads": ["recording-index-manifest", "filter-results-manifest", "host-access-seam"],
            "writes": ["local-voice-feature-store", "voice-feature-manifest"],
        },
        "selection": {
            "buckets": _bucket_set_for(options),
            "selectedCount": len(selected),
            "byBucket": by_bucket,
            "limit": options.limit,
        },
        "sourceIndex": _source_provenance(index_manifest),
        "sourceResults": _source_provenance(results_manifest),
        "outputPath": options.out,
        "localFeatureDir": options.feature_dir,
        "analysisConfig": _analysis_config_for(options),
        "tools": [_ffmpeg_tool_choice(options), _analysis_tool_choice(options)],
        "provisioning": [
            "python3.12 -m venv analysis/.venv",
            "analysis/.venv/bin/pip install -r analysis/requirements.txt",
            "analysis/.venv/bin/python analysis/selftest.py",
        ],
        "resumeBehavior": _resume_behavior(),
        "readScope": _features_read_scope(),
    }


def _source_provenance(manifest: Any) -> dict[str, Any]:
    return {
        "schemaVersion": js.coalesce(js.get(manifest, "schemaVersion"), None),
        "generatedAt": js.coalesce(js.get(manifest, "generatedAt"), None),
        "updatedAt": js.coalesce(js.get(manifest, "updatedAt"), None),
        "recordingCount": js.coalesce(
            js.get(js.get(manifest, "totals"), "recordings"),
            js.length(js.get(manifest, "results")),
            js.length(js.get(manifest, "recordings")),
            None,
        ),
    }


@dataclass
class ManifestExtras:
    """buildFeaturesManifest's ``extras``."""

    versions: Any = None
    worker_config: Any = None
    config_fingerprint: str | None = None
    tool_versions: Mapping[str, Any] | None = None
    skipped_completed: int = 0
    selected_count: int | None = None


def build_features_manifest(
    index_manifest: Any,
    results_manifest: Any,
    results: list[Any],
    options: FeaturesOptions,
    extras: ManifestExtras | None = None,
) -> dict[str, Any]:
    more = extras if extras is not None else ManifestExtras()
    timestamp = _generated_at(options)
    completed_count = sum(1 for row in results if js.get(row, "status") == "completed")
    selected_count = more.selected_count if more.selected_count is not None else len(results)
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "generatedAt": timestamp,
        "updatedAt": timestamp,
        "sourceIndex": _source_provenance(index_manifest),
        "sourceResults": {
            **_source_provenance(results_manifest),
            "selectedBuckets": _bucket_set_for(options),
        },
        "releaseGateApproval": options.approval,
        "readScope": _features_read_scope(),
        "localFeatureStore": {
            "root": options.feature_dir,
            "gitignoredExpected": _is_gitignored_local_path(options.feature_dir),
            "containsFeatureArrays": True,
            "committedToRepo": False,
        },
        "repoSafeManifest": {
            "containsAudioBytes": False,
            "containsTranscriptText": False,
            "containsFeatureArrays": False,
        },
        "analysisConfig": {
            **_analysis_config_for(options, more.versions, more.worker_config),
            "configFingerprint": more.config_fingerprint,
        },
        "resume": {
            **_resume_behavior(),
            "skippedCompletedCount": more.skipped_completed,
            "completedCount": completed_count,
            "failedCount": sum(1 for row in results if js.get(row, "status") == "failed"),
            "remainingCount": max(selected_count - completed_count, 0),
            "lastProcessedRecordingId": js.coalesce(js.get(results[-1], "recordingId"), None)
            if results
            else None,
        },
        "toolChoices": [
            _ffmpeg_tool_choice(options, more.tool_versions),
            _analysis_tool_choice(options, more.versions),
        ],
        "results": results,
    }


@dataclass
class RowExtras:
    """buildFeatureRow's ``extras``."""

    config_fingerprint: str | None = None
    feature_json_path: str | None = None
    features_digest: str | None = None


def _copy_present(target: dict[str, Any], key: str, source: Any, source_key: str) -> None:
    """``{key: source[sourceKey]}``: JSON.stringify drops a key whose value is undefined."""

    if isinstance(source, dict) and source_key in source:
        target[key] = source[source_key]


def build_feature_row(
    entry: dict[str, Any],
    bucket: Any,
    worker_result: Any,
    options: FeaturesOptions,
    extras: RowExtras | None = None,
) -> dict[str, Any]:
    more = extras if extras is not None else RowExtras()
    base: dict[str, Any] = {}
    _copy_present(base, "recordingId", entry, "recordingId")
    _copy_present(base, "filename", entry, "filename")
    base["capturedAt"] = entry.get("capturedAt")
    base["year"] = _year_for(entry)
    _copy_present(base, "sourceRef", entry, "sourceRef")
    base["bucket"] = bucket
    _copy_present(base, "status", worker_result, "status")
    base["configFingerprint"] = more.config_fingerprint
    base["toolRun"] = {
        "extractor": options.worker,
        "python": options.venv_python,
        "methodId": METHOD_ID,
        "audioPrep": (
            f"ffmpeg decode to {format_number(options.sample_rate)} Hz mono wav "
            "in a temporary directory"
        ),
    }
    if js.get(worker_result, "status") != "completed":
        return {
            **base,
            "error": js.coalesce(js.get(worker_result, "error"), "unknown extraction failure"),
            "features": None,
            "featuresDigest": None,
            "localArtifacts": None,
        }
    row = dict(base)
    _copy_present(row, "features", worker_result, "summary")
    row["featuresDigest"] = more.features_digest
    feature_json_path = more.feature_json_path or ""
    row["localArtifacts"] = {
        "featureJsonPath": _local_path_ref(feature_json_path),
        "localOnly": True,
        "gitignoredExpected": _is_gitignored_local_path(feature_json_path),
        "arraysIncludedInManifest": False,
    }
    return row


def _detail_body(entry: dict[str, Any], worker_result: Any) -> str:
    detail: dict[str, Any] = {}
    _copy_present(detail, "recordingId", entry, "recordingId")
    detail["methodId"] = METHOD_ID
    _copy_present(detail, "summary", worker_result, "summary")
    _copy_present(detail, "detail", worker_result, "detail")
    return manifest_text(detail)


def _update_resume(manifest: dict[str, Any], selected_count: int, row: dict[str, Any]) -> None:
    results = manifest["results"]
    resume = manifest["resume"]
    resume["completedCount"] = sum(1 for item in results if js.get(item, "status") == "completed")
    resume["failedCount"] = sum(1 for item in results if js.get(item, "status") == "failed")
    resume["remainingCount"] = max(selected_count - resume["completedCount"], 0)
    resume["lastProcessedRecordingId"] = row.get("recordingId")


def extract_index_manifest(
    index_manifest: Any,
    results_manifest: Any,
    options: FeaturesOptions,
    hooks: FeatureHooks | None = None,
) -> dict[str, Any]:
    seams = hooks if hooks is not None else FeatureHooks()
    if not options.approval:
        raise FeaturesError("extract-index requires --approval for non-dry-run audio reads")
    if not options.corpus_root:
        raise FeaturesError("extract-index requires --corpus-root")
    if options.out == "-":
        raise FeaturesError("extract-index requires a file output for incremental resume support")

    os.makedirs(options.feature_dir, exist_ok=True)
    selected = select_recordings(index_manifest, results_manifest, options)
    existing = _read_existing_manifest(options.out)
    completed_by_id: dict[Any, Any] = {}
    for row in js.coalesce(js.get(existing, "results"), []):
        if js.get(row, "status") == "completed":
            completed_by_id[js.get(row, "recordingId")] = row

    create_worker = seams.create_worker or create_feature_worker
    worker = create_worker(options)
    try:
        versions = worker.versions
        worker_config = worker.config
        config_fingerprint = config_fingerprint_for(options, versions, worker_config)
        tool_versions = (
            seams.tool_versions
            if seams.tool_versions is not None
            else {"ffmpeg": read_tool_version(options.ffmpeg_bin, ["-version"])}
        )
        manifest = build_features_manifest(
            index_manifest,
            results_manifest,
            list(completed_by_id.values()),
            options,
            ManifestExtras(
                versions=versions,
                worker_config=worker_config,
                config_fingerprint=config_fingerprint,
                tool_versions=tool_versions,
                skipped_completed=len(completed_by_id),
                selected_count=len(selected),
            ),
        )
        _write_json(options.out, manifest)

        decode_recording = seams.decode_recording or decode_recording_to_wav
        for item in selected:
            entry = item.entry
            recording_id = entry.get("recordingId")
            if recording_id in completed_by_id:
                continue
            decoded = decode_recording(entry, options)
            try:
                worker_result = worker.extract(
                    {"recordingId": recording_id, "wavPath": decoded.wav_path}
                )
            finally:
                decoded.cleanup()
            extras = RowExtras(config_fingerprint=config_fingerprint)
            if js.get(worker_result, "status") == "completed":
                feature_json_path = js.path_join(options.feature_dir, f"{recording_id}.json")
                body = _detail_body(entry, worker_result)
                with open(feature_json_path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(body)
                extras.feature_json_path = feature_json_path
                extras.features_digest = _sha256_hex(body)
            row = build_feature_row(entry, item.bucket, worker_result, options, extras)
            manifest["results"].append(row)
            _update_resume(manifest, len(selected), row)
            manifest["updatedAt"] = _generated_at(options)
            _write_json(options.out, manifest)
    finally:
        worker.close()
    return manifest


# --- external processes ------------------------------------------------------------


def decode_recording_to_wav(entry: dict[str, Any], options: FeaturesOptions) -> DecodedRecording:
    temp_root = tempfile.mkdtemp(prefix="voice-journey-features-")
    wav_path = os.path.join(temp_root, f"{entry.get('recordingId')}.wav")
    filename = entry.get("filename")
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
                js.path_join(options.corpus_root or "", str(filename)),
                "-ac",
                "1",
                "-ar",
                format_number(options.sample_rate),
                wav_path,
            ],
            f"ffmpeg failed for {filename}",
        )
    except BaseException:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return DecodedRecording(
        wav_path=wav_path, cleanup=lambda: shutil.rmtree(temp_root, ignore_errors=True)
    )


def _capture_command(command: str, args: Sequence[str], message: str | None = None) -> str:
    """captureCommand: stdout, or the trimmed stderr when stdout is empty."""

    failure = message if message is not None else f"{js.path_basename(command)} failed"
    try:
        completed = subprocess.run(
            [command, *args], stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    except OSError as error:
        raise FeaturesError(_spawn_message(command, error)) from error
    out = completed.stdout.decode("utf-8", errors="replace")
    err = js.trim(completed.stderr.decode("utf-8", errors="replace"))
    if completed.returncode != 0:
        detail = err or f"exit code {_exit_code_text(completed.returncode)}"
        raise FeaturesError(f"{failure}: {detail}")
    return out or err


def read_tool_version(command: str, args: Sequence[str]) -> str | None:
    """The first non-blank line a tool prints for its version, or None."""

    try:
        result = _capture_command(command, args)
    except FeaturesError:
        return None
    for line in result.split("\n"):
        if js.trim(line):
            return js.trim(line)
    return None


class ProcessFeatureWorker:
    """The analysis worker as a child process (createFeatureWorker).

    ``<venv python> <worker>`` with piped stdin, stdout and stderr. Blank stdout lines
    are skipped; a ``hello`` line carries the versions and configuration; every other
    line answers the oldest outstanding task. Lines before the hello answer nothing
    and are dropped, as in the JS.
    """

    def __init__(self, options: FeaturesOptions) -> None:
        try:
            self._child = subprocess.Popen(
                [options.venv_python, options.worker],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            raise FeaturesError(_spawn_message(options.venv_python, error)) from error
        self._stdin: IO[str] = _pipe(self._child.stdin)
        self._stdout: IO[str] = _pipe(self._child.stdout)
        self._stderr_chunks: list[str] = []
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, args=(_pipe(self._child.stderr),), daemon=True
        )
        self._stderr_reader.start()
        try:
            while True:
                payload = self._next_payload()
                if _is_hello(payload):
                    hello = payload["hello"]
                    break
        except BaseException:
            self._child.kill()
            self.close()
            raise
        self.versions: Any = js.coalesce(js.get(hello, "versions"), None)
        self.config: Any = js.coalesce(js.get(hello, "config"), None)

    def _drain_stderr(self, stream: IO[str]) -> None:
        for chunk in iter(lambda: stream.read(65536), ""):
            self._stderr_chunks.append(chunk)

    def _exited(self) -> FeaturesError:
        returncode = self._child.wait()
        self._stderr_reader.join()
        lines = js.trim("".join(self._stderr_chunks)).split("\n")
        tail = "\n".join(lines[-5:])
        suffix = f": {tail}" if tail else ""
        return FeaturesError(f"feature worker exited (code {_exit_code_text(returncode)}){suffix}")

    def _next_payload(self) -> Any:
        while True:
            line = self._stdout.readline()
            if line == "":
                raise self._exited()
            if line.endswith("\n"):
                line = line[:-1]
            if not js.trim(line):
                continue
            try:
                return parse_json(line)
            except ValueError:
                raise FeaturesError(
                    f"feature worker emitted unparseable output: {line[:200]}"
                ) from None

    def extract(self, task: dict[str, Any]) -> Any:
        try:
            self._stdin.write(stringify_compact(task) + "\n")
            self._stdin.flush()
        except BrokenPipeError:
            pass  # the worker is gone; reading reports how it exited
        while True:
            payload = self._next_payload()
            if not _is_hello(payload):
                return payload

    def close(self) -> None:
        try:
            self._stdin.close()
        except BrokenPipeError:
            pass
        self._child.wait()
        self._stderr_reader.join()
        self._stdout.close()


def _pipe(stream: IO[str] | None) -> IO[str]:
    if stream is None:  # pragma: no cover - every stream is piped
        raise FeaturesError("feature worker pipe missing")
    return stream


def _is_hello(payload: Any) -> bool:
    return isinstance(payload, dict) and js.truthy(payload.get("hello"))


def create_feature_worker(options: FeaturesOptions) -> FeatureWorker:
    return ProcessFeatureWorker(options)


def _read_existing_manifest(file_path: str) -> Any:
    try:
        return read_json(file_path)
    except FileNotFoundError:
        return None


def _local_path_ref(file_path: str) -> str:
    relative = os.path.relpath(os.path.abspath(file_path), os.getcwd())
    return file_path if relative.startswith("..") or os.path.isabs(relative) else relative


def _is_gitignored_local_path(file_path: str) -> bool:
    return is_inside_artifacts(file_path)


# --- entry points ------------------------------------------------------------------


def run(argv: Sequence[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    if not options.command or options.help:
        sys.stdout.write(HELP)
        return 0
    if options.command == "extract-index":
        index_manifest = read_json(options.index)
        results_manifest = read_json(options.results)
        if options.dry_run:
            _write_json("-", build_features_dry_run(index_manifest, results_manifest, options))
            return 0
        extract_index_manifest(index_manifest, results_manifest, options)
        return 0
    raise FeaturesError(f"unsupported command: {options.command}")


def main(argv: Sequence[str] | None = None) -> int:
    """The command line; every error is ``error: <message>`` and exit code 1, as the
    JS caught every ``Error``."""

    try:
        return run(argv)
    except Exception as error:
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

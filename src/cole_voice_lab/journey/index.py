"""The repo-safe recording index (index-recordings.mjs), ``voice-journey.recording-index.v1``.

Ports ``index-recordings.mjs``: the recordings are listed through the configured sources
(``journey/sources.py``: the phone drop, then the measurement sessions, through
``journey/corpus.py``; directory entries and stat metadata only, never audio bytes),
phone rows are marked ``present`` / ``deleted`` / ``unverified`` against the drop's
mirror manifest and the previous index, and the manifest is written as the Node pipeline
wrote it (``--compact`` is ``JSON.stringify(manifest, null, 0)``).

One deliberate difference in the manifest (D-051, Recording v1 step 7): every row says
its ``source``. Node gave only measurement rows one; phone rows now carry
``{"type": "voice-memos"}`` (in the row's existing ``source`` slot, after ``file``), and the
dry run lists ``source`` among the row fields. The schema stays
``voice-journey.recording-index.v1``: ``source`` was already a v1 row field, the change
only adds it where it was absent, and a reader that branches on ``source.type`` keeps its
path for every row. The parity harness counts this addition apart, by a named rule.

Differences from the JS: the defaults (``resolve_paths()``) are read when the command
runs, not when the module is imported; the manifest file is replaced atomically
(``jsonio.write_json``); error messages that come from the operating system are
Python's. Ordering uses ``jscompat.locale_compare_key`` (``localeCompare``, exact for ASCII).
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import jscompat as js
from .corpus import Entry
from .jsonio import iso_timestamp, read_json, write_json
from .paths import resolve_paths
from .sources import RecordingSource, configured_sources, list_source_entries

SCHEMA_VERSION = "voice-journey.recording-index.v1"

HELP = """Build the repo-safe Voice Journey recording index.

Usage:
  voice-journey-index [--corpus-root PATH] [--measurement-root PATH] [--out PATH] \
[--generated-at ISO] [--limit N]
  voice-journey-index --dry-run [--corpus-root PATH] [--out PATH]

The indexer reads recording metadata through the host-access seam only. It never
reads audio bytes, writes to the corpus, uploads data, or scans arbitrary paths.
"""


class IndexArgumentError(Exception):
    """A refused command line (``IndexError`` in the JS)."""


# ---- the command line -----------------------------------------------------------------


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else None
    if not value or value.startswith("--"):
        raise IndexArgumentError(f"{flag} requires a value")
    return value


def parse_args(argv: Sequence[str]) -> dict[str, Any]:
    paths = resolve_paths()
    args = list(argv)
    options: dict[str, Any] = {
        "corpusRoot": str(paths.phone_corpus_root),
        "dryRun": False,
        "generatedAt": None,
        "limit": None,
        "measurementRoot": str(paths.measurement_dir),
        "out": str(paths.manifest("recording-index.json")),
        "pretty": True,
        "help": False,
    }
    while args:
        arg = args.pop(0)
        if arg == "--corpus-root":
            options["corpusRoot"] = _require_value(args, arg)
        elif arg == "--dry-run":
            options["dryRun"] = True
        elif arg == "--generated-at":
            options["generatedAt"] = _require_value(args, arg)
        elif arg == "--limit":
            value = js.string_to_number(_require_value(args, arg))
            if not math.isfinite(value) or not value.is_integer() or value < 0:
                raise IndexArgumentError("--limit must be a non-negative integer")
            options["limit"] = int(value)
        elif arg == "--measurement-root":
            options["measurementRoot"] = _require_value(args, arg)
        elif arg == "--out":
            options["out"] = _require_value(args, arg)
        elif arg == "--compact":
            options["pretty"] = False
        elif arg in ("--help", "help"):
            options["help"] = True
        else:
            raise IndexArgumentError(f"unsupported argument: {arg}")
    return options


# ---- the manifest ---------------------------------------------------------------------


def build_dry_run(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "dryRun": True,
        "operation": "rebuild-recording-index",
        "architecture": {
            "slug": "kickoff-corpus-flow",
            "readsVia": ["host-access-seam", "seam-feeds-indexer"],
            "writes": ["recording-index-manifest", "indexer-writes-manifest"],
        },
        "corpusRoot": js.path_resolve(options["corpusRoot"]),
        "measurementRoot": js.path_resolve(options["measurementRoot"]),
        "outputPath": options["out"],
        "readScope": {
            "throughSeamOnly": True,
            "directoryEntries": True,
            "fileStatMetadata": True,
            "audioBytes": False,
            "durationExtraction": False,
            "mutation": False,
            "upload": False,
        },
        "manifestShape": {
            "schemaVersion": SCHEMA_VERSION,
            "recordingFields": [
                "recordingId",
                "sourceRef",
                "filename",
                "capturedAt",
                "durationSeconds",
                "file",
                "source",
                "indexingStatus",
                "contentClassification",
                "contaminationClassification",
            ],
            "classificationFields": [
                "contentClassification.singingStatus",
                "contentClassification.confidence",
                "contentClassification.classifier",
                "contaminationClassification.noiseMusicStatus",
            ],
            "toolChoiceFields": ["name", "stage", "openness", "openSource", "openSwapCandidate"],
        },
    }


def read_previous_index(out: str) -> Any:
    """The index the last run wrote, or None (``-``, missing, unreadable or not JSON)."""

    if out == "-":
        return None
    try:
        return read_json(out)
    except (OSError, ValueError, RecursionError):
        return None


def _previous_phone_states(previous: Any) -> dict[str, Any]:
    rows = previous.get("recordings") if isinstance(previous, dict) else None
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise TypeError("(previous?.recordings ?? []).map is not a function")
    states: dict[str, Any] = {}
    for row in rows:
        if row is None:
            raise TypeError("Cannot read properties of null (reading 'recordingId')")
        if isinstance(row, dict) and isinstance(row.get("recordingId"), str):
            states[row["recordingId"]] = row.get("phoneState")
    return states


def mark_phone_state(entries: Sequence[Entry], previous: Any) -> list[Entry]:
    """A recording deleted on the phone stays on the server and is marked, never removed.

    The mirror never deletes, and its manifest lists what the phone side saw at the last
    run, so a file the manifest no longer lists is ``deleted`` only if the previous index
    had seen it listed (``present``) or already marked it (sticky). A file the manifest
    has not caught up with yet is ``unverified``. Without a manifest (no drop run yet, a
    local corpus) no state is recorded.
    """

    before = _previous_phone_states(previous)
    marked: list[Entry] = []
    for item in entries:
        entry = {key: value for key, value in item.items() if key != "inManifest"}
        in_manifest = item.get("inManifest")
        if in_manifest is None:
            marked.append(entry)
        elif in_manifest:
            marked.append({**entry, "phoneState": "present"})
        else:
            seen = before.get(entry["recordingId"])
            state = "deleted" if seen in ("present", "deleted") else "unverified"
            marked.append({**entry, "phoneState": state})
    return marked


def _captured_key(entry: Entry) -> Any:
    captured = entry.get("capturedAt")
    if captured is None:
        captured = ""
    if not isinstance(captured, str):
        raise TypeError("leftCaptured.localeCompare is not a function")
    return js.locale_compare_key(captured)


def _sort_recordings(recordings: Sequence[Entry]) -> list[Entry]:
    if len(recordings) < 2:
        return list(recordings)
    return sorted(
        recordings,
        key=lambda entry: (_captured_key(entry), js.locale_compare_key(entry["filename"])),
    )


def _row(entry: Entry) -> Entry:
    recording_id = entry.get("recordingId", js.MISSING)
    row: Entry = js.defined(
        {
            "recordingId": recording_id,
            "sourceRef": js.defined(
                {
                    "seam": "voice-journey-corpus",
                    "selector": recording_id,
                    "filename": entry["filename"],
                }
            ),
            "filename": entry["filename"],
            "capturedAt": entry.get("capturedAt", js.MISSING),
            "durationSeconds": None,
            "durationSource": "not_read_metadata_only_index",
            "file": js.defined(
                {
                    "extension": js.path_extname(entry["filename"]).lower(),
                    "sizeBytes": entry.get("sizeBytes", js.MISSING),
                    "modifiedAt": entry.get("modifiedAt", js.MISSING),
                }
            ),
        }
    )
    if entry.get("source"):
        row["source"] = entry["source"]
    if entry.get("phoneState"):
        row["phoneState"] = entry["phoneState"]
    row["indexingStatus"] = (
        "indexed_metadata" if entry.get("capturedAt") else "indexed_metadata_filename_unparsed"
    )
    row["contentClassification"] = {
        "singingStatus": "unknown",
        "confidence": None,
        "classifier": None,
        "reviewedBy": None,
        "notes": "Pending later singing/non-singing classification pass.",
    }
    row["contaminationClassification"] = {
        "noiseMusicStatus": "not_evaluated",
        "confidence": None,
        "classifier": None,
        "reviewedBy": None,
        "notes": "Pending later noise/music filtering pass for recordings classified as singing.",
    }
    return row


def build_manifest(recordings: Sequence[Entry], options: dict[str, Any]) -> dict[str, Any]:
    generated_at = options.get("generatedAt")
    if generated_at is None:
        generated_at = iso_timestamp()
    rows = _sort_recordings(recordings)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "source": {
            "kind": "voice-memos-host-access-seam",
            "architecture": "kickoff-corpus-flow",
            "seamNodeKey": "host-access-seam",
            "indexerNodeKey": "recording-indexer",
            "manifestNodeKey": "recording-index-manifest",
            "readScope": {
                "directoryEntries": True,
                "fileStatMetadata": True,
                "audioBytes": False,
                "durationExtraction": False,
                "mutation": False,
                "upload": False,
            },
        },
        "toolChoices": [
            {
                "name": "Node.js standard library fs/stat",
                "stage": "metadata-index",
                "openness": "open-source-runtime",
                "openSource": True,
                "openSwapCandidate": None,
                "notes": (
                    "Used only for directory enumeration and file stat metadata through the "
                    "approved seam."
                ),
            },
            {
                "name": "Singing/non-singing classifier not selected in this phase",
                "stage": "content-classification-placeholder",
                "openness": "not-selected",
                "openSource": None,
                "openSwapCandidate": (
                    "Essentia, librosa, or pyAudioAnalysis-based local classifier in the "
                    "filtering phase"
                ),
                "notes": (
                    "The index carries classification fields now; later phases populate "
                    "them from approved local analysis."
                ),
            },
        ],
        "classificationSchema": {
            "singingStatusValues": ["unknown", "singing", "non_singing", "uncertain"],
            "noiseMusicStatusValues": [
                "not_evaluated",
                "clean",
                "noise_contaminated",
                "music_contaminated",
                "uncertain",
            ],
        },
        "totals": {"recordings": len(rows), "audioBytesIndexed": 0},
        "recordings": [_row(entry) for entry in rows],
    }


def write_manifest(manifest: dict[str, Any], out: str, *, pretty: bool) -> None:
    write_json(out, manifest, announce=False, compact=not pretty)
    if out != "-":
        sys.stdout.write(f"{out}\n")


# ---- entry points ---------------------------------------------------------------------


def index_sources(options: Mapping[str, Any]) -> tuple[RecordingSource, ...]:
    """The configured sources for these options: the phone drop (``--corpus-root``,
    ``--limit``), then the measurement sessions (``--measurement-root``)."""

    return configured_sources(
        js.path_resolve(options["corpusRoot"]),
        js.path_resolve(options["measurementRoot"]),
        limit=options["limit"],
    )


def run(
    argv: Sequence[str],
    *,
    sources: Callable[[Mapping[str, Any]], Sequence[RecordingSource]] = index_sources,
) -> int:
    """The command; refusals and read errors raise. ``sources`` builds the sources from the
    parsed options: a new kind of recording is an adapter in
    ``sources.configured_sources``, not a change here."""

    options = parse_args(argv)
    if options["help"]:
        sys.stdout.write(HELP)
        return 0
    if options["dryRun"]:
        write_json("-", build_dry_run(options))
        return 0
    entries = list_source_entries(sources(options))
    # Phone states come from the drop's mirror manifest (`inManifest`); an entry without
    # that flag (a measurement session's) passes through unchanged.
    recordings = mark_phone_state(entries, read_previous_index(options["out"]))
    manifest = build_manifest(recordings, options)
    write_manifest(manifest, options["out"], pretty=options["pretty"])
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except Exception as error:  # every JS Error ends the run with exit 1
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())

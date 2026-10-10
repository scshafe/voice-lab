"""The repo-safe recording index (index-recordings.mjs), ``voice-journey.recording-index.v1``.

Ports ``index-recordings.mjs`` unchanged: the phone drop and the measurement sessions are
listed through ``journey/corpus.py`` (directory entries and stat metadata only, never
audio bytes), phone rows are marked ``present`` / ``deleted`` / ``unverified`` against
the drop's mirror manifest and the previous index, and the manifest is written as the
Node pipeline wrote it (``--compact`` is ``JSON.stringify(manifest, null, 0)``).

Differences from the JS: the defaults (``resolve_paths()``) are read when the command
runs, not when the module is imported; the manifest file is replaced atomically
(``jsonio.write_json``); error messages that come from the operating system are
Python's. Ordering uses ``corpus.locale_key`` (``localeCompare`` for ASCII).
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .corpus import (
    Entry,
    js_extname,
    js_resolve,
    list_corpus_entries,
    list_measurement_entries,
    locale_key,
    parse_json,
)
from .jsonio import format_number, iso_timestamp, write_json
from .paths import resolve_paths

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

_JS_SPACE = (
    "\t\n\x0b\x0c\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_JS_DECIMAL = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
_JS_RADIX = re.compile(r"0(?:([xX])([0-9a-fA-F]+)|([oO])([0-7]+)|([bB])([01]+))")


def js_number(text: str) -> float:
    """``Number(text)`` for a string (``NaN`` when it is not a numeric literal)."""

    body = text.strip(_JS_SPACE)
    if not body:
        return 0.0
    radix = _JS_RADIX.fullmatch(body)
    if radix:
        digits = radix[2] or radix[4] or radix[6]
        base = 16 if radix[1] else 8 if radix[3] else 2
        try:
            return float(int(digits, base))
        except OverflowError:
            return float("inf")
    if _JS_DECIMAL.fullmatch(body):
        return float(body.replace("Infinity", "inf"))
    return float("nan")


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
            value = js_number(_require_value(args, arg))
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
        "corpusRoot": js_resolve(options["corpusRoot"]),
        "measurementRoot": js_resolve(options["measurementRoot"]),
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
        with open(out, encoding="utf-8", errors="replace", newline="") as handle:
            return parse_json(handle.read())
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
    return locale_key(captured)


def _sort_recordings(recordings: Sequence[Entry]) -> list[Entry]:
    if len(recordings) < 2:
        return list(recordings)
    return sorted(
        recordings, key=lambda entry: (_captured_key(entry), locale_key(entry["filename"]))
    )


_UNDEFINED = object()


def _defined(fields: dict[str, Any]) -> Entry:
    """``JSON.stringify`` drops a key whose value is ``undefined``."""

    return {key: value for key, value in fields.items() if value is not _UNDEFINED}


def _row(entry: Entry) -> Entry:
    recording_id = entry.get("recordingId", _UNDEFINED)
    row: Entry = _defined(
        {
            "recordingId": recording_id,
            "sourceRef": _defined(
                {
                    "seam": "voice-journey-corpus",
                    "selector": recording_id,
                    "filename": entry["filename"],
                }
            ),
            "filename": entry["filename"],
            "capturedAt": entry.get("capturedAt", _UNDEFINED),
            "durationSeconds": None,
            "durationSource": "not_read_metadata_only_index",
            "file": _defined(
                {
                    "extension": js_extname(entry["filename"]).lower(),
                    "sizeBytes": entry.get("sizeBytes", _UNDEFINED),
                    "modifiedAt": entry.get("modifiedAt", _UNDEFINED),
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


def compact_text(value: Any) -> str:
    """``JSON.stringify(value, null, 0)``: no whitespace at all."""

    if value is None:
        return "null"
    if isinstance(value, bool | int | float):
        return format_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        members = (
            f"{json.dumps(str(key), ensure_ascii=False)}:{compact_text(item)}"
            for key, item in value.items()
        )
        return "{" + ",".join(members) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(compact_text(item) for item in value) + "]"
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def _write_compact(out: str, manifest: dict[str, Any]) -> None:
    body = compact_text(manifest) + "\n"
    if out == "-":
        sys.stdout.write(body)
        return
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    sys.stdout.write(f"{out}\n")


def write_manifest(manifest: dict[str, Any], out: str, *, pretty: bool) -> None:
    if not pretty:
        _write_compact(out, manifest)
    elif out == "-":
        write_json(out, manifest)
    else:
        write_json(out, manifest, announce=False)
        sys.stdout.write(f"{out}\n")


# ---- entry points ---------------------------------------------------------------------


def run(argv: Sequence[str]) -> int:
    """The command; refusals and read errors raise."""

    options = parse_args(argv)
    if options["help"]:
        sys.stdout.write(HELP)
        return 0
    if options["dryRun"]:
        write_json("-", build_dry_run(options))
        return 0
    phone = list_corpus_entries(
        js_resolve(options["corpusRoot"]), limit=options["limit"], mirror_manifest=True
    )
    recordings = [
        *mark_phone_state(phone, read_previous_index(options["out"])),
        *list_measurement_entries(js_resolve(options["measurementRoot"])),
    ]
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

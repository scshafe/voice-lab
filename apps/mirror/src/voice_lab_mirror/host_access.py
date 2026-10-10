"""The read-only host-access seam for the Mac's Voice Memos corpus (host-access.mjs).

The mirror reads the corpus ONLY through this module: a directory listing, ``stat``,
and read-only byte streams of allowlisted files (bare ``.m4a`` names and the Voice
Memos metadata database with its SQLite sidecars). It never writes, deletes or
uploads, and its command line (``python -m voice_lab_mirror.host_access``, wrapper
``bin/voice-lab-corpus``) prints metadata, never audio bytes.

Differences from the JavaScript module:

- Only the Mac's half is here. The drop's server-side helpers of host-access.mjs
  (``listMeasurementEntries``, ``readMirrorManifest`` and the ``mirrorManifest`` option
  of ``listCorpusEntries``) belong to the PC's ingest (design §5.3, ``memos/`` and
  ``journey/index.py``), which cannot import this macOS package.
- The default corpus root is the Voice Memos group container, overridden by
  ``VOICE_LAB_MIRROR_CORPUS_ROOT`` (the mirror's variable) instead of paths.mjs's
  ``VOICE_JOURNEY_CORPUS_ROOT``; the command is named ``voice-lab-corpus``.
- ``localeCompare`` (ICU root collation) is emulated for ASCII names by
  :func:`collation_key`; Voice Memos names (``YYYYMMDD HHMMSS-XXXXXXXX.m4a``) are ASCII.
  A non-ASCII character sorts after every ASCII one, by code point.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

ALLOWED_EXTENSIONS = frozenset({".m4a"})
# The Voice Memos metadata database (and its SQLite sidecars) sits beside the
# recordings. The mirror is the only caller that reads it, and only as a copy.
METADATA_DB_NAMES: tuple[str, ...] = (
    "CloudRecordings.db",
    "CloudRecordings.db-wal",
    "CloudRecordings.db-shm",
)
FORBIDDEN_COMMANDS = frozenset({"write", "delete", "remove", "rm", "upload", "copy-in", "mutate"})
OPERATIONS: tuple[str, ...] = ("describe", "list", "metadata", "read-handle")
VOICE_MEMOS_CORPUS = os.path.join(
    os.path.expanduser("~"),
    "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings",
)
CORPUS_ROOT_ENV = "VOICE_LAB_MIRROR_CORPUS_ROOT"

# JavaScript's \s and String.prototype.trim set (WhiteSpace and LineTerminator).
JS_WHITESPACE = "\t\n\v\f\r                  　﻿"
# JavaScript's \d is ASCII only and "." stops at any line terminator.
_FILENAME_STEM = re.compile(r"(?P<date>[0-9]{8}) (?P<time>[0-9]{6})-(?P<suffix>[^\n\r  ]+)\Z")
_JS_DECIMAL = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\Z")
_JS_PREFIXED = re.compile(r"0(?:[xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)\Z")

# ICU root collation of the printable ASCII characters that are not letters or
# digits (alternate=non-ignorable, V8's default): whitespace, then punctuation,
# then symbols, then currency, then digits, then letters (case is tertiary).
_ICU_ASCII_VARIABLES = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"


class HostAccessError(Exception):
    """The corpus cannot be read as asked."""


class RefusedOperationError(HostAccessError):
    """An operation the seam never performs (a mutation, upload, or path escape)."""


def collation_key(name: str) -> tuple[tuple[int, ...], tuple[int, ...], str]:
    """A sort key equal to ``String.prototype.localeCompare`` order for ASCII strings.

    Primary weights compare the whole string first (case-insensitive, punctuation
    before digits before letters), then tertiary weights (lowercase before uppercase),
    as ICU does; the code points break any remaining tie.
    """

    primary: list[int] = []
    tertiary: list[int] = []
    for char in name:
        lowered = char.lower() if char.isascii() else char
        if char.isascii() and char.isalpha():
            primary.append(1000 + ord(lowered))
            tertiary.append(1 if char.isupper() else 0)
        elif char.isascii() and char.isdigit():
            primary.append(500 + ord(char))
        elif char in "\t\n\v\f\r":
            primary.append(1 + "\t\n\v\f\r".index(char))
        elif char in _ICU_ASCII_VARIABLES:
            primary.append(10 + _ICU_ASCII_VARIABLES.index(char))
        elif char.isascii():
            continue  # other control characters are completely ignorable
        else:
            primary.append(0x10000 + ord(char))
        if not (char.isascii() and char.isalpha()):
            tertiary.append(0)
    return tuple(primary), tuple(tertiary), name


def node_extname(name: str) -> str:
    """Node's ``path.extname`` for a name without a slash (``"..m4a"`` -> ``".m4a"``)."""

    dot = name.rfind(".")
    if dot <= 0 or name == "..":
        return ""
    return name[dot:]


def js_resolve(value: str) -> str:
    """Node's ``path.resolve`` of one path: absolute, normalized, no symlinks resolved."""

    resolved = os.path.abspath(value)
    if resolved.startswith("//"):
        resolved = "/" + resolved.lstrip("/")
    return resolved


def iso_timestamp_ms(milliseconds: int) -> str:
    """``new Date(ms).toISOString()``."""

    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def iso_mtime(stats: os.stat_result) -> str:
    """Node's ``stats.mtime.toISOString()``.

    Node builds ``mtimeMs`` as ``sec * 1000 + nsec / 1e6`` in doubles and rounds it to
    the nearest millisecond for the Date; the same operations are done here.
    """

    seconds, nanoseconds = divmod(stats.st_mtime_ns, 1_000_000_000)
    milliseconds = float(seconds) * 1000.0 + float(nanoseconds) / 1e6
    return iso_timestamp_ms(math.floor(milliseconds + 0.5))


def recording_id(filename: str) -> str:
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]


def captured_at_from_filename(filename: str) -> str | None:
    """The capture time a Voice Memos name encodes, or ``None``.

    As ``Date.UTC`` does, an hour, minute or second past its range carries into the
    next unit; the result counts only when the calendar date is unchanged.
    """

    match = _FILENAME_STEM.match(_stem(filename))
    if match is None:
        return None
    date, time = match.group("date"), match.group("time")
    year, month, day = int(date[0:4]), int(date[4:6]), int(date[6:8])
    if year < 100:
        return None  # Date.UTC maps years 0-99 to 1900-1999, so the year check fails
    try:
        midnight = datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None
    moment = midnight + timedelta(
        hours=int(time[0:2]), minutes=int(time[2:4]), seconds=int(time[4:6])
    )
    if moment.date() != midnight.date():
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _stem(filename: str) -> str:
    """``path.parse(filename).name``."""

    base = filename.rstrip("/").rsplit("/", 1)[-1]
    extension = node_extname(base)
    return base[: len(base) - len(extension)] if extension else base


@dataclass(frozen=True)
class _Entry:
    recording_id: str
    filename: str
    host_path: str
    captured_at: str | None
    size_bytes: int
    modified_at: str


def _assert_corpus_root(root: str) -> None:
    try:
        stats = os.stat(root)
    except OSError:
        raise HostAccessError(f"corpus root does not exist: {root}") from None
    if not stat.S_ISDIR(stats.st_mode):
        raise HostAccessError(f"corpus root is not a directory: {root}")


def _list_entries(root: str) -> list[_Entry]:
    _assert_corpus_root(root)
    with os.scandir(root) as iterator:
        dirents = sorted(iterator, key=lambda dirent: collation_key(dirent.name))
    entries: list[_Entry] = []
    for dirent in dirents:
        if (
            not dirent.is_file(follow_symlinks=False)
            or dirent.name.startswith(".")
            or node_extname(dirent.name).lower() not in ALLOWED_EXTENSIONS
        ):
            continue
        host_path = os.path.join(root, dirent.name)
        stats = os.stat(host_path)
        entries.append(
            _Entry(
                recording_id=recording_id(dirent.name),
                filename=dirent.name,
                host_path=host_path,
                captured_at=captured_at_from_filename(dirent.name),
                size_bytes=stats.st_size,
                modified_at=iso_mtime(stats),
            )
        )
    return entries


def _public_entry(entry: _Entry, *, include_path: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "recordingId": entry.recording_id,
        "filename": entry.filename,
        "capturedAt": entry.captured_at,
        "sizeBytes": entry.size_bytes,
        "modifiedAt": entry.modified_at,
    }
    if include_path:
        payload["hostPath"] = entry.host_path
    return payload


def list_corpus_entries(root: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Every ``.m4a`` recording (name order), as public metadata without host paths."""

    entries = _list_entries(root)
    limited = entries if limit is None else entries[:limit]
    return [_public_entry(entry) for entry in limited]


@dataclass(frozen=True)
class DatabaseFile:
    name: str
    size_bytes: int


def list_metadata_database_files(root: str) -> list[DatabaseFile]:
    """Metadata database files present beside the recordings."""

    _assert_corpus_root(root)
    files: list[DatabaseFile] = []
    for name in METADATA_DB_NAMES:
        try:
            stats = os.stat(os.path.join(root, name))
        except OSError:
            continue  # absent sidecars are normal
        if stat.S_ISREG(stats.st_mode):
            files.append(DatabaseFile(name=name, size_bytes=stats.st_size))
    return files


def open_corpus_file_for_read(root: str, name: str) -> BinaryIO:
    """A read-only byte stream for one allowlisted corpus file.

    Only a bare ``.m4a`` name or a metadata database name is accepted; anything else
    (a path, another extension) is refused.
    """

    allowed = "/" not in name and (
        node_extname(name).lower() in ALLOWED_EXTENSIONS or name in METADATA_DB_NAMES
    )
    if not allowed:
        raise RefusedOperationError("refusing to read a file outside the corpus allowlist")
    return open(os.path.join(root, name), "rb")


def _find_recording(root: str, selector: str) -> _Entry:
    for entry in _list_entries(root):
        if entry.recording_id == selector or entry.filename == selector:
            return entry
    raise HostAccessError(f"recording not found by id or filename: {selector}")


def _print_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _dry_run_payload(operation: str, root: str, **extra: Any) -> dict[str, Any]:
    return {
        "dryRun": True,
        "operation": operation,
        "corpusRoot": root,
        "allowedExtensions": sorted(ALLOWED_EXTENSIONS),
        "willReadAudioBytes": False,
        **extra,
    }


def js_number(text: str) -> float:
    """JavaScript's ``Number(string)``: ``NaN`` when the text is not a number literal."""

    stripped = text.strip(JS_WHITESPACE)
    if stripped == "":
        return 0.0
    if _JS_PREFIXED.match(stripped):
        return float(int(stripped, 0))
    if _JS_DECIMAL.match(stripped):
        return float(stripped.replace("Infinity", "inf"))
    return math.nan


@dataclass
class _Options:
    corpus_root: str | None = None
    dry_run: bool = False
    limit: int | None = None
    approval: str | None = None
    positional: list[str] = field(default_factory=list)


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else ""
    if not value or value.startswith("--"):
        raise HostAccessError(f"{flag} requires a value")
    return value


def _parse_args(argv: Sequence[str]) -> _Options:
    args = list(argv)
    options = _Options()
    while args:
        argument = args.pop(0)
        if argument == "--corpus-root":
            options.corpus_root = _require_value(args, argument)
        elif argument == "--dry-run":
            options.dry_run = True
        elif argument == "--limit":
            value = js_number(_require_value(args, argument))
            if not (math.isfinite(value) and value.is_integer()) or value < 0:
                raise HostAccessError("--limit must be a non-negative integer")
            options.limit = int(value)
        elif argument == "--approval":
            options.approval = _require_value(args, argument)
        else:
            options.positional.append(argument)
    return options


HELP = f"""Read-only host-access seam for the local Voice Memos corpus.

Usage:
  voice-lab-corpus [--corpus-root PATH] describe
  voice-lab-corpus [--corpus-root PATH] list [--dry-run] [--limit N]
  voice-lab-corpus [--corpus-root PATH] metadata RECORDING [--dry-run]
  voice-lab-corpus [--corpus-root PATH] read-handle RECORDING --approval TEXT [--dry-run]

Allowed operations: {", ".join(OPERATIONS)}.
Refused operations: {", ".join(sorted(FORBIDDEN_COMMANDS))}.
The seam never writes, deletes, uploads, or prints audio bytes.
The corpus root defaults to the Voice Memos container ({CORPUS_ROOT_ENV} overrides it).
"""


def _refuse(command: str) -> RefusedOperationError:
    return RefusedOperationError(
        f"refusing unsupported corpus mutation/upload operation: {command}"
    )


def run(argv: Sequence[str]) -> int:
    """The seam's command line. Raises :class:`HostAccessError` on a refusal or failure."""

    if argv and argv[0] in FORBIDDEN_COMMANDS:
        raise _refuse(argv[0])
    options = _parse_args(argv)
    command = options.positional[0] if options.positional else None
    rest = options.positional[1:]
    if command in FORBIDDEN_COMMANDS:
        raise _refuse(command)
    root = js_resolve(
        options.corpus_root
        if options.corpus_root is not None
        else os.environ.get(CORPUS_ROOT_ENV, VOICE_MEMOS_CORPUS)
    )
    if command is None or command in ("--help", "help"):
        sys.stdout.write(HELP)
        return 0
    if command == "describe":
        _print_json(
            {
                "command": "voice-lab-corpus",
                "corpusRoot": root,
                "operations": list(OPERATIONS),
                "refusedOperations": sorted(FORBIDDEN_COMMANDS),
                "privacy": {
                    "localOnly": True,
                    "uploadsAudio": False,
                    "mutatesCorpus": False,
                    "readsAudioBytes": False,
                    "audioCommittedToRepo": False,
                },
            }
        )
        return 0
    if command == "list":
        if options.dry_run:
            _print_json(
                _dry_run_payload("list", root, wouldReadDirectory=True, limit=options.limit)
            )
            return 0
        recordings = list_corpus_entries(root, limit=options.limit)
        _print_json({"corpusRoot": root, "count": len(recordings), "recordings": recordings})
        return 0
    if command == "metadata":
        recording = rest[0] if rest else ""
        if not recording:
            raise HostAccessError("metadata requires a recording id or filename")
        if options.dry_run:
            _print_json(
                _dry_run_payload(
                    "metadata",
                    root,
                    wouldReadDirectory=True,
                    wouldStatMatchedFile=True,
                    recording=recording,
                )
            )
            return 0
        entry = _find_recording(root, recording)
        _print_json({"corpusRoot": root, "recording": _public_entry(entry)})
        return 0
    if command == "read-handle":
        recording = rest[0] if rest else ""
        if not recording:
            raise HostAccessError("read-handle requires a recording id or filename")
        if options.dry_run:
            payload = _dry_run_payload(
                "read-handle",
                root,
                wouldReadDirectory=True,
                wouldStatMatchedFile=True,
                recording=recording,
                approval=options.approval,
            )
            _print_json(payload)
            return 0
        if not options.approval:
            raise HostAccessError("read-handle requires --approval describing operator approval")
        entry = _find_recording(root, recording)
        _print_json(
            {
                "corpusRoot": root,
                "handle": {
                    **_public_entry(entry, include_path=True),
                    "readOnly": True,
                    "approval": options.approval,
                    "audioBytesIncluded": False,
                },
            }
        )
        return 0
    raise HostAccessError(f"unsupported operation: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the seam; a refusal exits 2, any other seam failure 1."""

    try:
        return run(sys.argv[1:] if argv is None else argv)
    except HostAccessError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2 if isinstance(error, RefusedOperationError) else 1


if __name__ == "__main__":
    raise SystemExit(main())

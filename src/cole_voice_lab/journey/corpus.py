"""The server-side, read-only listing of the corpus (host-access.mjs, its listing half).

Ports the functions of ``host-access.mjs`` that the server pipeline uses: the listing of
the phone drop (``listCorpusEntries``, with the drop's mirror manifest) and of accepted
measurement sessions (``listMeasurementEntries``), with what they need
(``recordingId``, ``capturedAtFromFilename``, ``readMirrorManifest``, ``publicEntry``).
The indexer (``journey/index.py``) and, later, the watcher use them. The macOS read seam
(the ``voice-journey-corpus`` command line, read handles, the metadata database) is not
here: it moves with the mirror to ``apps/mirror/``.

The listing reads directory entries, file stat metadata and two small JSON files (the
drop's ``_mirror/latest.json`` and each session's ``session.json``); never audio bytes,
and it writes nothing. Drop rules, as in the JS: only regular ``.m4a`` files directly in
the drop count; dot files (rsync's ``.name.XXXXXX`` temporaries, ``.rsync-partial/``),
directories (``_mirror/``) and other files are ignored.

Where JavaScript semantics needed a translation (the shared rules are
``journey/jscompat.py``'s and ``journey/jsonio.py``'s):

- ``localeCompare`` (ICU root collation) orders the listings:
  :func:`jscompat.locale_compare_key`, exact for ASCII. Voice Memos names and session
  directory names are ASCII; a non-ASCII character sorts after every ASCII one by code
  point, which may differ from ICU for such names.
- ``stats.mtime.toISOString()``: Node rounds ``mtimeMs`` (``sec * 1e3 + nsec / 1e6``)
  to the nearest millisecond; :func:`mtime_iso` does the same arithmetic.
- ``path.join`` / ``path.extname`` are Node's lexical rules (``jscompat.path_join``,
  ``jscompat.path_extname``), not ``pathlib``'s; ``Buffer.from(name)`` is
  ``jscompat.utf8``.
- ``JSON.parse(readFile(...))`` is ``jsonio.read_json`` (``NaN``/``Infinity`` refused,
  invalid UTF-8 replaced).
- A file name that is not valid UTF-8 is decoded with replacement characters, as
  Node's ``readdir`` does (its stat then fails, as in Node). Errors that Node raises
  (a missing file between listing and stat, a malformed ``session.json`` shape) are
  raised here too; their messages are Python's.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
from datetime import UTC, datetime, timedelta
from typing import Any

from . import jscompat as js
from .jsonio import js_round, read_json

Entry = dict[str, Any]

ALLOWED_EXTENSIONS = frozenset({".m4a"})
# The mirror's pass-2 manifest (written by the mirror, pushed last): size and sha256 per
# file. The mirrored drop also holds rsync's hidden partial dir and `.name.XXXXXX` temp
# files; none of those are recordings.
MIRROR_MANIFEST_PATH = posixpath.join("_mirror", "latest.json")
MIRROR_MANIFEST_SCHEMA = "voice-journey-mirror-manifest/1"

_FILENAME_STEM = re.compile(
    r"(?P<date>[0-9]{8}) (?P<time>[0-9]{6})-(?P<suffix>[^\n\r\u2028\u2029]+)\Z"
)
_SESSION_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}\Z")


class HostAccessError(Exception):
    """A refusal of the read layer (a missing or non-directory corpus root)."""


# ---- JavaScript semantics -------------------------------------------------------------


def mtime_iso(stats: os.stat_result) -> str:
    """``stats.mtime.toISOString()``: Node rounds ``mtimeMs`` (``sec * 1e3 + nsec / 1e6``)
    to the nearest millisecond."""

    seconds, nanoseconds = divmod(stats.st_mtime_ns, 1_000_000_000)
    return js.iso_from_ms(js_round(seconds * 1e3 + nanoseconds / 1e6))


def _node_name(name: str) -> str:
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _sorted_dirents(root: str) -> list[tuple[str, os.DirEntry[str]]]:
    with os.scandir(root) as iterator:
        dirents = [(_node_name(entry.name), entry) for entry in iterator]
    return sorted(dirents, key=lambda pair: js.locale_compare_key(pair[0]))


# ---- host-access.mjs ------------------------------------------------------------------


def recording_id(filename: str) -> str:
    """The first 16 hex digits of the SHA-256 of the UTF-8 file name."""

    return hashlib.sha256(js.utf8(filename)).hexdigest()[:16]


def captured_at_from_filename(filename: str) -> str | None:
    """The capture time a Voice Memos name carries (``YYYYMMDD HHMMSS-<id>.m4a``), as
    ``Date.UTC`` reads it: the time fields may overflow within the day, the date may not.
    """

    base = filename.rsplit("/", 1)[-1]
    stem = base[: len(base) - len(js.path_extname(base))]
    match = _FILENAME_STEM.match(stem)
    if match is None:
        return None
    date, time = match["date"], match["time"]
    year, month, day = int(date[0:4]), int(date[4:6]), int(date[6:8])
    if year < 100:  # Date.UTC maps years 0-99 to 1900-1999, so the year never matches
        return None
    try:
        midnight = datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None
    moment = midnight + timedelta(
        hours=int(time[0:2]), minutes=int(time[2:4]), seconds=int(time[4:6])
    )
    if moment.date() != midnight.date():
        return None
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}Z"
    )


def _assert_corpus_root(root: str) -> None:
    try:
        stats = os.stat(root)
    except (OSError, ValueError):
        raise HostAccessError(f"corpus root does not exist: {root}") from None
    if not stat.S_ISDIR(stats.st_mode):
        raise HostAccessError(f"corpus root is not a directory: {root}")


def _list_entries(root: str) -> list[Entry]:
    _assert_corpus_root(root)
    entries: list[Entry] = []
    for name, dirent in _sorted_dirents(root):
        if (
            not dirent.is_file(follow_symlinks=False)
            or name.startswith(".")
            or js.path_extname(name).lower() not in ALLOWED_EXTENSIONS
        ):
            continue
        host_path = js.path_join(root, name)
        stats = os.stat(host_path)
        entries.append(
            {
                "recordingId": recording_id(name),
                "filename": name,
                "hostPath": host_path,
                "capturedAt": captured_at_from_filename(name),
                "sizeBytes": stats.st_size,
                "modifiedAt": mtime_iso(stats),
            }
        )
    return entries


def read_mirror_manifest(root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The drop's mirror manifest as ``{"runId", "files": {path: file}}``, or None when
    there is none (a corpus the mirror does not feed, or a drop it has not completed).
    Read-only; a torn or foreign file counts as absent."""

    try:
        manifest = read_json(js.path_join(os.fspath(root), MIRROR_MANIFEST_PATH))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(manifest, dict) or manifest.get("schema") != MIRROR_MANIFEST_SCHEMA:
        return None
    files = manifest.get("files")
    if not isinstance(files, list):
        return None
    run_id = manifest.get("runId")
    return {
        "runId": run_id,
        "files": {
            file["path"]: file
            for file in files
            if isinstance(file, dict) and isinstance(file.get("path"), str)
        },
    }


def public_entry(entry: Entry) -> Entry:
    """The fields a listing returns (no host path)."""

    payload: Entry = {
        "recordingId": entry["recordingId"],
        "filename": entry["filename"],
        "capturedAt": entry["capturedAt"],
        "sizeBytes": entry["sizeBytes"],
        "modifiedAt": entry["modifiedAt"],
    }
    if entry.get("source"):
        payload["source"] = entry["source"]
    if "inManifest" in entry:
        payload["inManifest"] = entry["inManifest"]
    return payload


def list_corpus_entries(
    root: str | os.PathLike[str], *, limit: int | None = None, mirror_manifest: bool = False
) -> list[Entry]:
    """The recordings of a drop, ordered by name (ICU collation).

    ``mirror_manifest=True`` applies the drop's manifest. A file the manifest lists with
    a different size is held back (a newer copy landed and its manifest has not, or the
    copy is not the one that was hashed), so only verified bytes are ingested. A file the
    manifest does not list is kept: rsync renames files in whole, and the manifest follows
    the recordings by design. Each kept entry says whether the latest manifest lists it
    (``inManifest``); ``None`` means no manifest exists.
    """

    directory = os.fspath(root)
    entries = _list_entries(directory)
    if mirror_manifest:
        manifest = read_mirror_manifest(directory)
        kept: list[Entry] = []
        for entry in entries:
            listed = manifest["files"].get(entry["filename"]) if manifest else None
            if listed is not None and not _same_size(listed.get("sizeBytes"), entry):
                continue
            kept.append({**entry, "inManifest": listed is not None if manifest else None})
        entries = kept
    limited = entries if limit is None else entries[:limit]
    return [public_entry(entry) for entry in limited]


def _same_size(listed: Any, entry: Entry) -> bool:
    return js.is_number(listed) and bool(listed == entry["sizeBytes"])


def _segment_keys(session: dict[str, Any]) -> dict[str, Any]:
    segments = session.get("segments")
    if segments is None:
        return {}
    if not isinstance(segments, list):
        raise TypeError("session.segments.map is not a function")
    by_file: dict[str, Any] = {}
    for segment in segments:
        if segment is None:
            raise TypeError("Cannot read properties of null (reading 'file')")
        if isinstance(segment, dict) and isinstance(segment.get("file"), str):
            by_file[segment["file"]] = segment
    return by_file


def _session_files(session: dict[str, Any]) -> list[str]:
    files = session.get("files")
    if files is None or files == "":
        return []
    if not isinstance(files, list | str):
        raise TypeError("session.files is not iterable")
    names: list[str] = []
    for file in files:
        if file is None:
            raise TypeError("Cannot read properties of null (reading 'name')")
        name = file.get("name") if isinstance(file, dict) else None
        if not isinstance(name, str):
            raise TypeError('The "path" argument must be of type string')
        names.append(name)
    return names


def list_measurement_entries(root: str | os.PathLike[str]) -> list[Entry]:
    """Accepted measurement sessions (``<root>/<sessionId>/session.json`` plus the segment
    files). The intake publishes a session directory with one atomic rename, so a
    directory that has a ``session.json`` is whole. Each segment file is one entry,
    tagged with its source so the pipeline can tell the chains apart. A missing directory
    is an empty list, not an error. Entries keep their ``hostPath``."""

    directory = os.fspath(root)
    try:
        dirents = _sorted_dirents(directory)
    except FileNotFoundError:
        return []
    entries: list[Entry] = []
    for name, dirent in dirents:
        if not dirent.is_dir(follow_symlinks=False) or not _SESSION_DIR.match(name):
            continue
        try:
            session = read_json(js.path_join(directory, name, "session.json"))
        except (OSError, ValueError, RecursionError):
            continue
        if session is None:
            raise TypeError("Cannot read properties of null (reading 'segments')")
        fields: dict[str, Any] = session if isinstance(session, dict) else {}
        segment_by_file = _segment_keys(fields)
        for file_name in _session_files(fields):
            host_path = js.path_join(directory, name, file_name)
            try:
                stats = os.stat(host_path)
            except (OSError, ValueError):
                continue
            filename = f"{name}/{file_name}"
            segment = segment_by_file.get(file_name)
            source: Entry = {"type": "measurement"}
            if "chainId" in fields:
                source["chainId"] = fields["chainId"]
            source["sessionId"] = name
            source["segmentKey"] = segment.get("key") if segment is not None else None
            entries.append(
                {
                    "recordingId": recording_id(f"measurement/{filename}"),
                    "filename": filename,
                    "hostPath": host_path,
                    "capturedAt": fields.get("startedAt"),
                    "sizeBytes": stats.st_size,
                    "modifiedAt": mtime_iso(stats),
                    "source": source,
                }
            )
    return entries


__all__ = [
    "ALLOWED_EXTENSIONS",
    "MIRROR_MANIFEST_PATH",
    "Entry",
    "HostAccessError",
    "captured_at_from_filename",
    "list_corpus_entries",
    "list_measurement_entries",
    "mtime_iso",
    "public_entry",
    "read_mirror_manifest",
    "recording_id",
]

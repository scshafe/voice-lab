"""The Voice Memos mirror sender: the Mac's corpus -> the voice-lab drop (mirror.mjs).

Reads the corpus ONLY through the host-access seam (directory listing, stat, and
read-only streams of allowlisted files), and pushes new or changed recordings plus the
Voice Memos metadata database with rsync. The receiving end is an ``rrsync -wo -no-del``
restricted key: write-only, rooted at the drop, so every remote path here is relative
and nothing is ever read back (infra ``replication/voice-journey-mirror``; design §5.5).

Never deletes remotely (no ``--delete*``). Never prints recording names: logs and
rsync's own output carry counts only.

The target is configuration, not code: ``--dest`` or ``VOICE_LAB_MIRROR_DEST`` (the
launchd plist sets it), likewise the key (``VOICE_LAB_MIRROR_SSH_KEY``), the rsync
binary (``VOICE_LAB_MIRROR_RSYNC``), the state directory (``VOICE_LAB_MIRROR_STATE``)
and the corpus root (``VOICE_LAB_MIRROR_CORPUS_ROOT``).

Differences from the JavaScript module:

- The environment variables are ``VOICE_LAB_MIRROR_*`` instead of ``VOICE_JOURNEY_*``,
  the default target is ``vj-mirror@cole-laptop:`` (the laptop's current name; the
  infra contract) and the default state directory ``~/.local/state/voice-lab-mirror``.
- ssh also gets ``-o StrictHostKeyChecking=yes``, as the infra contract specifies
  (under ``BatchMode=yes`` an unknown host already failed; now it fails explicitly).
- An ``OSError`` during a locked run (a recording vanishing between the listing and
  the hash, an unwritable state directory) exits 1 with the error number only. The
  JavaScript crashed with Node's message, which quotes the file name.
- A state file whose JSON is not an object counts as an empty hash cache (the
  JavaScript crashed on ``null``).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO

from voice_lab_mirror.host_access import (
    CORPUS_ROOT_ENV,
    JS_WHITESPACE,
    VOICE_MEMOS_CORPUS,
    DatabaseFile,
    HostAccessError,
    RefusedOperationError,
    iso_mtime,
    iso_timestamp_ms,
    js_resolve,
    list_corpus_entries,
    list_metadata_database_files,
    open_corpus_file_for_read,
)

# Relative to the rrsync root (the drop on the receiver): "host:" is the root.
# Absolute remote paths are deliberately not the default; rrsync resolves every
# path under its own root.
DEFAULT_DEST = "vj-mirror@cole-laptop:"
# Homebrew rsync 3.x by full path: macOS 26's /usr/bin/rsync is openrsync
# (protocol 29), untested against rrsync.
DEFAULT_RSYNC = "/opt/homebrew/bin/rsync"
DEFAULT_STATE_DIR = os.path.join(os.path.expanduser("~"), ".local/state/voice-lab-mirror")
DEST_ENV = "VOICE_LAB_MIRROR_DEST"
STATE_ENV = "VOICE_LAB_MIRROR_STATE"
SSH_KEY_ENV = "VOICE_LAB_MIRROR_SSH_KEY"
RSYNC_ENV = "VOICE_LAB_MIRROR_RSYNC"
PARTIAL_DIR = ".rsync-partial"
MANIFEST_DIR = "_mirror"
MANIFEST_SCHEMA = "voice-journey-mirror-manifest/1"
STALE_LOCK_MS = 6 * 60 * 60 * 1000
_CHUNK = 1024 * 1024
_DROP_LOCKED = "skipped: the server drop is locked by another rrsync run; the next period retries"


class EXIT:
    ok = 0
    failure = 1
    usage = 2
    locked = 3
    transfer = 4


class UsageError(Exception):
    """The command line is malformed."""


@dataclass
class Options:
    corpus_root: str
    dest: str
    state_dir: str
    ssh_key: str | None
    rsync_bin: str
    approval: str | None = None
    dry_run: bool = False
    help: bool = False


def parse_args(argv: Sequence[str]) -> Options:
    options = Options(
        corpus_root=os.environ.get(CORPUS_ROOT_ENV, VOICE_MEMOS_CORPUS),
        dest=os.environ.get(DEST_ENV, DEFAULT_DEST),
        state_dir=os.environ.get(STATE_ENV, DEFAULT_STATE_DIR),
        ssh_key=os.environ.get(SSH_KEY_ENV),
        rsync_bin=os.environ.get(RSYNC_ENV, DEFAULT_RSYNC),
    )
    args = list(argv)

    def value(flag: str) -> str:
        following = args.pop(0) if args else ""
        if not following or following.startswith("--"):
            raise UsageError(f"{flag} requires a value")
        return following

    while args:
        flag = args.pop(0)
        if flag == "--corpus-root":
            options.corpus_root = value(flag)
        elif flag == "--dest":
            options.dest = value(flag)
        elif flag == "--state-dir":
            options.state_dir = value(flag)
        elif flag == "--ssh-key":
            options.ssh_key = value(flag)
        elif flag == "--rsync-bin":
            options.rsync_bin = value(flag)
        elif flag == "--approval":
            options.approval = value(flag)
        elif flag == "--dry-run":
            options.dry_run = True
        elif flag in ("--help", "help"):
            options.help = True
        else:
            raise UsageError(f"unknown argument: {flag}")
    options.corpus_root = js_resolve(options.corpus_root)
    options.state_dir = js_resolve(options.state_dir)
    return options


def _is_inside(parent: str, child: str) -> bool:
    relative = os.path.relpath(child, parent)
    if relative == ".":
        return True
    return not relative.startswith("..") and not os.path.isabs(relative)


_REMOTE = re.compile(rf"(?:rsync://|[^/:{JS_WHITESPACE}]+:)")


def validate_options(options: Options) -> None:
    """The destination must be remote and the state must live outside the corpus.

    A remote target is ``[user@]host:path`` or ``rsync://``, so rsync can never be
    pointed at a local directory, least of all the corpus.
    """

    if not _REMOTE.match(options.dest):
        raise RefusedOperationError(
            "refusing a local destination: the mirror only pushes to a remote rsync target"
        )
    if _is_inside(options.corpus_root, options.state_dir):
        raise RefusedOperationError("refusing to keep mirror state inside the corpus")


def _ssh_command(options: Options) -> str:
    parts = [
        "ssh",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=15",
    ]
    if options.ssh_key:
        parts += ["-i", options.ssh_key]
    return " ".join(parts)


def rsync_base_args(options: Options) -> list[str]:
    """The flags both passes share.

    Why they work under ``rrsync -wo``: this host is always the sender, the receiver
    only receives. There is no ``--delete*``, no pull, and the receiver's quick check
    (size+mtime) is answered by the receiver itself, which ``-wo`` allows. rsync writes
    each file to a temp name (inside ``--partial-dir``, a hidden directory) and renames
    it into place only when it is complete. ``--delay-updates`` is NOT used: rrsync
    rewrites ``--partial-dir`` to an absolute path, which rsync cannot combine with
    ``--delay-updates`` (it silently discards the file, still exit 0).
    """

    return [
        "-rt",
        "--no-perms",
        "--no-owner",
        "--no-group",
        "--omit-dir-times",
        # The Mac's files may be 0600; the receiver reads through the vj-mirror group.
        "--chmod=D2750,F0640",
        f"--partial-dir={PARTIAL_DIR}",
        "--timeout=300",
        "--no-motd",
        "-e",
        _ssh_command(options),
    ]


def recordings_rsync_args(options: Options, list_path: str) -> list[str]:
    """Pass 1: recordings, from the corpus root, driven by a NUL-separated list."""

    return [
        *rsync_base_args(options),
        "--from0",
        f"--files-from={list_path}",
        f"{options.corpus_root}/",
        options.dest,
    ]


def staging_rsync_args(options: Options, staging_dir: str, list_path: str) -> list[str]:
    """Pass 2: the database snapshot and the manifest, only after pass 1 succeeded,
    so the manifest's arrival means its files are complete."""

    return [
        *rsync_base_args(options),
        "--from0",
        f"--files-from={list_path}",
        f"{staging_dir}/",
        options.dest,
    ]


def _sha256_stream(readable: BinaryIO, target: str | None = None) -> str:
    digest = hashlib.sha256()
    with readable, open(target, "wb") if target else _NoSink() as sink:
        while chunk := readable.read(_CHUNK):
            digest.update(chunk)
            sink.write(chunk)
    return digest.hexdigest()


class _NoSink:
    def __enter__(self) -> _NoSink:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def write(self, _chunk: bytes) -> int:
        return 0


def _read_json(path: str, fallback: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def _js_json(payload: Any, *, indent: int | None = None) -> str:
    """``JSON.stringify`` of strings, integers, booleans, lists and objects."""

    if indent is None:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _parse_date_ms(value: str) -> float | None:
    """``Date.parse`` of an ISO string: a date alone is UTC, a naive date-time local."""

    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None and "T" not in value and " " not in value:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp() * 1000.0


def _process_alive(pid: object) -> bool:
    """``process.kill(pid, 0)`` succeeding, or failing only with EPERM."""

    if isinstance(pid, bool) or not isinstance(pid, int | float):
        return False
    if isinstance(pid, float) and not pid.is_integer():
        return False
    number = int(pid)
    if not -(2**31) <= number < 2**31:
        return False
    try:
        os.kill(number, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_lock(lock_path: str) -> bool:
    payload = _js_json({"pid": os.getpid(), "startedAt": iso_timestamp_ms(_now_ms())})
    for _attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            holder = _read_json(lock_path, None)
            holder = holder if isinstance(holder, dict) else {}
            started = holder.get("startedAt")
            started_ms = _parse_date_ms(started) if isinstance(started, str) and started else 0.0
            age = None if started_ms is None else _now_ms() - started_ms
            pid = holder.get("pid")
            alive = bool(pid) and _process_alive(pid)
            if alive and age is not None and age < STALE_LOCK_MS:
                return False
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return True
    return False


@dataclass(frozen=True)
class RsyncResult:
    code: int
    stderr: str
    signal: str | None = None


def _run_rsync(binary: str, args: Sequence[str]) -> RsyncResult:
    try:
        completed = subprocess.run(
            [binary, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        name = errno.errorcode.get(error.errno or 0, "spawn error")
        return RsyncResult(code=127, stderr=name)
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode < 0:
        return RsyncResult(
            code=128, stderr=stderr, signal=signal.Signals(-completed.returncode).name
        )
    return RsyncResult(code=completed.returncode, stderr=stderr)


def is_drop_locked(result: RsyncResult) -> bool:
    """rrsync serializes the drop with a lock; a concurrent run is told so. The text
    is matched here and never logged."""

    return "Another instance of rrsync is already accessing this directory" in result.stderr


def _summarize_failure(result: RsyncResult) -> str:
    """rsync error lines can quote file names: keep only a count."""

    lines = sum(1 for line in result.stderr.split("\n") if line.strip(JS_WHITESPACE) != "")
    signal_note = f" ({result.signal})" if result.signal else ""
    return f"rsync exited {result.code}{signal_note}; {lines} diagnostic line(s) suppressed"


@dataclass(frozen=True)
class Plan:
    recordings: list[dict[str, Any]]
    db_files: list[DatabaseFile]


def plan_mirror(options: Options) -> Plan:
    recordings = list_corpus_entries(options.corpus_root)
    db_files = list_metadata_database_files(options.corpus_root)
    return Plan(recordings=recordings, db_files=db_files)


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _same_number(left: object, right: object) -> bool:
    numbers = (int, float)
    return (
        isinstance(left, numbers)
        and isinstance(right, numbers)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
        and left == right
    )


def _mirror_locked(options: Options, log: Callable[[str], None]) -> int:
    started_ms = _now_ms()
    started_at = iso_timestamp_ms(started_ms)
    run_id = re.sub(r"\.\d+Z$", "Z", re.sub(r"[-:]", "", started_at))
    try:
        plan = plan_mirror(options)
    except HostAccessError as error:
        log(f"error: {str(error).replace(options.corpus_root, '<corpus>', 1)}")
        return EXIT.failure

    # Hash cache: only files whose size+mtime moved since the last run are re-read.
    state_path = os.path.join(options.state_dir, "hashes.json")
    cache = _read_json(state_path, {"files": {}})
    cached_files = cache.get("files") if isinstance(cache, dict) else None
    cached_files = cached_files if isinstance(cached_files, dict) else {}
    next_files: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    rehashed = 0
    for entry in plan.recordings:
        filename = entry["filename"]
        cached = cached_files.get(filename)
        sha256 = (
            cached.get("sha256")
            if isinstance(cached, dict)
            and _same_number(cached.get("sizeBytes"), entry["sizeBytes"])
            and cached.get("modifiedAt") == entry["modifiedAt"]
            else None
        )
        if not sha256:
            sha256 = _sha256_stream(open_corpus_file_for_read(options.corpus_root, filename))
            rehashed += 1
        next_files[filename] = {
            "sizeBytes": entry["sizeBytes"],
            "modifiedAt": entry["modifiedAt"],
            "sha256": sha256,
        }
        files.append(
            {
                "path": filename,
                "sizeBytes": entry["sizeBytes"],
                "modifiedAt": entry["modifiedAt"],
                "sha256": sha256,
            }
        )

    # Database: snapshot into staging so what is hashed is what is sent.
    staging_dir = os.path.join(options.state_dir, "staging")
    if os.path.isdir(staging_dir) and not os.path.islink(staging_dir):
        shutil.rmtree(staging_dir)
    elif os.path.lexists(staging_dir):
        os.unlink(staging_dir)
    os.makedirs(os.path.join(staging_dir, MANIFEST_DIR), exist_ok=True)
    db_paths: list[str] = []
    for db in plan.db_files:
        target = os.path.join(staging_dir, db.name)
        sha256 = _sha256_stream(open_corpus_file_for_read(options.corpus_root, db.name), target)
        info = os.stat(target)
        files.append(
            {
                "path": db.name,
                "sizeBytes": info.st_size,
                "modifiedAt": iso_mtime(info),
                "sha256": sha256,
                "snapshot": True,
            }
        )
        db_paths.append(db.name)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "runId": run_id,
        "startedAt": started_at,
        "recordingCount": len(plan.recordings),
        "databaseFileCount": len(plan.db_files),
        "note": (
            "Deletions are never propagated; files absent here may still exist on the server."
        ),
        "files": files,
    }
    manifest_name = f"manifest-{run_id}.json"
    manifest_body = _js_json(manifest, indent=2) + "\n"
    _write_text(os.path.join(staging_dir, MANIFEST_DIR, manifest_name), manifest_body)
    _write_text(os.path.join(staging_dir, MANIFEST_DIR, "latest.json"), manifest_body)
    manifests_dir = os.path.join(options.state_dir, "manifests")
    os.makedirs(manifests_dir, exist_ok=True)
    _write_text(os.path.join(manifests_dir, manifest_name), manifest_body)

    recordings_list = os.path.join(options.state_dir, "recordings.list")
    _write_text(recordings_list, "".join(f"{entry['filename']}\0" for entry in plan.recordings))
    staging_list = os.path.join(options.state_dir, "staging.list")
    staged = [*db_paths, f"{MANIFEST_DIR}/{manifest_name}", f"{MANIFEST_DIR}/latest.json"]
    _write_text(staging_list, "".join(f"{name}\0" for name in staged))

    log(
        f"run {run_id}: {len(plan.recordings)} recording(s), "
        f"{len(plan.db_files)} database file(s), {rehashed} hashed"
    )

    first = (
        _run_rsync(options.rsync_bin, recordings_rsync_args(options, recordings_list))
        if plan.recordings
        else RsyncResult(code=0, stderr="")
    )
    if first.code != 0:
        if is_drop_locked(first):
            log(_DROP_LOCKED)
            return EXIT.locked
        log(
            f"transfer failed: {_summarize_failure(first)}; manifest withheld, the next run retries"
        )
        return EXIT.transfer
    second = _run_rsync(options.rsync_bin, staging_rsync_args(options, staging_dir, staging_list))
    if second.code != 0:
        if is_drop_locked(second):
            log(_DROP_LOCKED)
            return EXIT.locked
        log(f"manifest transfer failed: {_summarize_failure(second)}")
        return EXIT.transfer
    _write_text(f"{state_path}.tmp", _js_json({"files": next_files}))
    os.replace(f"{state_path}.tmp", state_path)
    log(f"run {run_id}: done")
    return EXIT.ok


HELP = f"""Mirror the Voice Memos corpus to the voice-lab drop (write-only rsync).

Usage:
  voice-lab-mirror --approval TEXT [--dest TARGET] [--corpus-root PATH]
                   [--state-dir PATH] [--ssh-key PATH] [--rsync-bin PATH] [--dry-run]

  --dest        rsync target, relative to the receiver's rrsync root
                (default {DEFAULT_DEST}; env {DEST_ENV})
  --ssh-key     the drop's ssh key (env {SSH_KEY_ENV})
  --rsync-bin   rsync binary (default {DEFAULT_RSYNC}; env {RSYNC_ENV})
  --corpus-root the Voice Memos recordings (env {CORPUS_ROOT_ENV})
  --state-dir   lock, hash cache, staging (default {DEFAULT_STATE_DIR}; env {STATE_ENV})
  --approval    release-gate approval; required, the mirror reads audio bytes
  --dry-run     print the plan and rsync command lines; reads nothing

Exit codes: 0 ok, 1 corpus/unexpected failure, 2 usage or refused, 3 locked
(another local run, or rrsync's drop lock; try the next period), 4 rsync failed
(the next run retries).
Never deletes on the server. Logs carry counts, never recording names.
"""


def _stderr_log(line: str) -> None:
    sys.stderr.write(f"[mirror] {line}\n")
    sys.stderr.flush()


def run_mirror(argv: Sequence[str], *, log: Callable[[str], None] = _stderr_log) -> int:
    try:
        options = parse_args(argv)
        if options.help:
            sys.stdout.write(HELP)
            return EXIT.ok
        validate_options(options)
    except (UsageError, RefusedOperationError) as error:
        log(f"error: {error}")
        return EXIT.usage

    if options.dry_run:
        # Reads nothing: no corpus, no network. Shows what a run would do.
        plan = {
            "dryRun": True,
            "operation": "mirror",
            "corpusRoot": options.corpus_root,
            "dest": options.dest,
            "stateDir": options.state_dir,
            "willReadAudioBytes": False,
            "wouldHashAndPushAudio": True,
            "neverDeletesRemotely": True,
            "recordingsRsync": [
                options.rsync_bin,
                *recordings_rsync_args(options, "<recordings-list>"),
            ],
            "stagingRsync": [
                options.rsync_bin,
                *staging_rsync_args(options, "<staging-dir>", "<staging-list>"),
            ],
        }
        sys.stdout.write(_js_json(plan, indent=2) + "\n")
        return EXIT.ok
    if not options.approval:
        log(
            "error: mirroring reads audio bytes; "
            "pass --approval TEXT recording the release-gate decision"
        )
        return EXIT.usage

    try:
        os.makedirs(options.state_dir, exist_ok=True)
        lock_path = os.path.join(options.state_dir, "mirror.lock")
        if not _acquire_lock(lock_path):
            log("skipped: another mirror run holds the lock")
            return EXIT.locked
        try:
            return _mirror_locked(options, log)
        finally:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
    except OSError as error:
        # The exception's text can quote a recording's path: log the number only.
        name = errno.errorcode.get(error.errno or 0, "unknown")
        log(f"error: {name} during the run; details withheld, the next run retries")
        return EXIT.failure


def main(argv: Sequence[str] | None = None) -> int:
    return run_mirror(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())

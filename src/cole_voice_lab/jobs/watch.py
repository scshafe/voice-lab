"""The journey pipeline's watcher (voice-journey's ``watchd.mjs``, the breathing loop).

It lists the corpus (the phone drop through its mirror manifest, plus accepted measurement
sessions), and when two scans agree and the agreed state differs from the recording index
it runs the incremental pipeline, in order, as child processes::

    index -> filter -> stt -> lyric -> reclassify -> features -> journeys

Each stage is ``python -P -m cole_voice_lab.journey.<module> <args>`` with the arguments
the JS passed to the Node script (``index-recordings.mjs`` is ``index``,
``filter-proof.mjs`` ``filter``, ``local-stt.mjs`` ``stt``, ``lyric-indicators.mjs``
``lyrics``, ``reclassify.mjs`` ``reclassify``, ``features.mjs`` ``features``,
``journeys.mjs`` ``journeys``). The operator's approval is passed only to the stages that
read audio bytes (filter, stt, features). Every stage is incremental, so a whole re-run is
safe. A failing stage aborts the run, notifies, and backs off for 30 minutes unless the
corpus changes. A lock beside the state file (``<state>.lock``, holding the runner's pid)
keeps two runs apart; a lock whose process is gone is reclaimed. A weekly digest of counts
goes out on the configured local weekday and hour.

Where it runs (design §3.2, §5.3). The JS lived inside the HTTP process (for macOS's Full
Disk Access grant). On the PC it is a job of the job runner: a systemd host unit with no
network (``PrivateNetwork=yes``), as user ``voice-lab``, started by a timer. So:

- **One-shot mode** (``--once``, :meth:`Watchd.run_once`): one scan per process. The scan
  signature and the scan-error streak are kept in the state file (``pendingSignature``,
  ``scanErrorStreak``, beside the JS fields, with ``lastTick``), so "two agreeing scans"
  holds across timer runs: a run starts only when this scan agrees with the previous run's
  and differs from the index. The loop mode (no ``--once``) is kept and uses the same
  persisted fields.
- **No serve-state reload.** The API reloads manifests itself when they change. The
  ``on_ingested`` hook (tests use it) is called after a successful run.
- **Notifications** carry titles, stage keys, counts and durations only, never transcript
  or lyric text or stage output (full stage stderr is not kept; a 2000-character tail of a
  failing stage goes to the local log only). They go to the injected ``notify`` hook if one
  is given; otherwise to each configured channel:

  - ``--notify-outbox DIR`` (``VOICE_JOURNEY_NOTIFY_OUTBOX``), the channel for the job
    runner: each notification is one JSON file in ``DIR``, written to a dot-named temporary
    file in the same directory and renamed into place, so a reader never sees a partial
    file. The name is ``<ms since epoch, 13 digits>-<pid>-<8 hex>.json`` (name order is
    creation order); the file is mode 0600 and holds ``{"schemaVersion":
    "voice-lab.journey-notification.v1", "createdAt", "title", "body", "priority",
    "tags"}``. A separate sender, outside this unit, delivers the files whose names do not
    start with a dot, in name order, and removes each once delivered.
  - ``--ntfy URL`` (``VOICE_JOURNEY_NTFY``): an HTTP POST to ntfy as the JS did (``Title``,
    ``Priority``, ``Tags`` headers, 10 s timeout, failures logged). It needs network, so it
    is for a run outside the job runner.

Differences from the JS, besides the above:

- The state file is replaced atomically (``jsonio.write_json``); the lock file is created
  whole (written to a temporary file, then hard-linked into place), so a crash never
  leaves a partial lock or state.
- On SIGTERM/SIGINT the running stage is terminated and the process exits 143/130 as in
  the JS, but the run's ``finally`` still runs: the partial run is recorded as
  ``lastRun`` and the lock released (the JS's ``process.exit`` skipped both).
- A stage that outlives its timeout gets SIGTERM as in Node, and SIGKILL 30 s later if it
  is still running (Node waited for it indefinitely).
- A scan failure is logged with its message (the JS only counted it); the third in a row
  notifies, as in the JS, without the macOS Full Disk Access hint.
- Stage stdout/stderr tails are cut by Python characters, not UTF-16 code units (log text
  only). Node's 64 MiB output cap is not applied.
"""

from __future__ import annotations

import argparse
import errno
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, NotRequired, TypedDict

from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey.corpus import list_corpus_entries, list_measurement_entries
from cole_voice_lab.journey.jsonio import (
    MISSING,
    format_number,
    js_round,
    object_key_order,
    read_json,
    stringify,
    stringify_compact,
    write_json,
)
from cole_voice_lab.journey.paths import JourneyPaths, resolve_paths

DAY_INDEX: Mapping[str, int] = {
    "sunday": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
}
STATE_VERSION = "voice-journey.watchd-state.v1"
STATE_FILE = "watchd-state.json"
NOTIFICATION_SCHEMA = "voice-lab.journey-notification.v1"
MAX_RECENT_RUNS = 10
FAILURE_BACKOFF_MS = 30 * 60 * 1000
DEFAULT_INTERVAL_SECONDS = 60
STAGE_PACKAGE = "cole_voice_lab.journey"
STDERR_TAIL_CHARS = 2000
STDOUT_TAIL_CHARS = 400
TERMINATE_GRACE_SECONDS = 30.0
NTFY_TIMEOUT_SECONDS = 10.0
FAILING_STATUSES = frozenset({"failed", "scan-error"})

Entry = dict[str, Any]
State = dict[str, Any]


class Notification(TypedDict):
    title: str
    body: str
    priority: NotRequired[str]
    tags: NotRequired[str]


class WatchError(ValueError):
    """A refused watcher configuration."""


class StageFailure(Exception):
    """A pipeline stage that did not exit 0; ``stderr_tail`` goes to the local log only."""

    def __init__(self, message: str, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


@dataclass(frozen=True)
class Stage:
    key: str
    module: str
    args: tuple[str, ...]
    timeout_ms: int

    def command(self, python: str) -> list[str]:
        return [python, "-P", "-m", f"{STAGE_PACKAGE}.{self.module}", *self.args]


@dataclass
class StageContext:
    """What a stage runner gets besides the stage (``repoRoot``, ``registerChild``)."""

    work_dir: str
    register_child: Callable[[subprocess.Popen[bytes] | None], None]
    python: str = field(default_factory=lambda: sys.executable)


RunStage = Callable[[Stage, StageContext], Mapping[str, Any] | None]
Notify = Callable[[Notification], None]
Log = Callable[[str], None]


# ---- pure parts -------------------------------------------------------------------------


def scan_signature(entries: Sequence[Mapping[str, Any]] | None) -> str:
    """``recordingId:sizeBytes`` per entry, in JavaScript's default (UTF-16) order."""

    parts = [
        f"{js.to_string(js.prop(entry, 'recordingId'))}:{js.to_string(js.prop(entry, 'sizeBytes'))}"
        for entry in (entries or [])
    ]
    return "|".join(sorted(parts, key=js.utf16_key))


def _strict_equal(left: Any, right: Any) -> bool:
    """``left === right`` for JSON values."""

    if js.is_number(left) and js.is_number(right):
        return bool(left == right)
    return type(left) is type(right) and bool(left == right)


def count_new_since_index(entries: Sequence[Mapping[str, Any]] | None, index_manifest: Any) -> int:
    """Entries the index lacks, or lists with another size."""

    rows = js.coalesce(js.get(index_manifest, "recordings"), [])
    if not isinstance(rows, list):
        raise TypeError("indexManifest.recordings.map is not a function")
    indexed: dict[Any, Any] = {}
    for row in rows:
        indexed[js.prop(row, "recordingId")] = js.coalesce(
            js.get(js.prop(row, "file"), "sizeBytes"), js.prop(row, "sizeBytes")
        )
    count = 0
    for entry in entries or []:
        recording = js.prop(entry, "recordingId")
        if recording not in indexed or not _strict_equal(
            indexed[recording], js.prop(entry, "sizeBytes")
        ):
            count += 1
    return count


def build_stages(
    corpus_root: str, approval: str, measurement_root: str | None = None
) -> list[Stage]:
    """The pipeline in dependency order; the approval only on stages that read audio."""

    minute = 60_000
    measurement = ("--measurement-root", measurement_root) if measurement_root else ()
    return [
        Stage("index", "index", ("--corpus-root", corpus_root, *measurement), 10 * minute),
        Stage(
            "filter",
            "filter",
            ("analyze-index", "--corpus-root", corpus_root, "--approval", approval),
            4 * 60 * minute,
        ),
        Stage(
            "stt",
            "stt",
            ("transcribe-index", "--corpus-root", corpus_root, "--approval", approval),
            12 * 60 * minute,
        ),
        Stage("lyric", "lyrics", ("analyze",), 30 * minute),
        Stage("reclassify", "reclassify", ("apply",), 30 * minute),
        Stage(
            "features",
            "features",
            ("extract-index", "--corpus-root", corpus_root, "--approval", approval),
            12 * 60 * minute,
        ),
        Stage("journeys", "journeys", ("analyze",), 60 * minute),
    ]


def _local_ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def _js_day(moment: datetime) -> int:
    return (moment.weekday() + 1) % 7


def next_digest_at(from_ms: int, day: str = "sunday", hour: int = 18) -> int:
    """The first local ``day`` at ``hour``:00 strictly after ``from_ms``.

    As ``Date.setHours``/``setDate``: local wall-clock arithmetic; a local time that does
    not exist or exists twice (a DST change) takes the offset in effect before the change
    (``datetime.timestamp`` with ``fold=0``, V8's rule too)."""

    day_index = DAY_INDEX.get(str(day).lower())
    if day_index is None:
        raise ValueError(f"unknown digest day: {day}")
    start = datetime.fromtimestamp(from_ms / 1000)
    midnight = datetime(start.year, start.month, start.day)
    candidate = datetime.fromtimestamp(_local_ms(midnight + timedelta(hours=hour)) / 1000)
    for _ in range(8):
        candidate_ms = _local_ms(candidate)
        if _js_day(candidate) == day_index and candidate_ms > from_ms:
            return candidate_ms
        candidate = datetime.fromtimestamp(_local_ms(candidate + timedelta(days=1)) / 1000)
    raise RuntimeError("nextDigestAt failed to converge")  # pragma: no cover - unreachable


def _iterate(value: Any) -> list[Any]:
    if isinstance(value, list | tuple):
        return list(value)
    if isinstance(value, str):
        return list(value)
    raise TypeError(f"{js.to_string(value)} is not iterable")


def _count_by(rows: Any, pick: Callable[[Any], Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _iterate(rows):
        key = js.to_string(js.coalesce(pick(row), "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return {key: counts[key] for key in object_key_order(counts)}


def _bucket_of(row: Any) -> Any:
    return js.coalesce(
        js.get(js.prop(row, "classification"), "finalBucket"), js.prop(row, "bucket")
    )


def _length(manifest: Any, key: str) -> Any:
    return js.length(js.get(manifest, key))


def digest_snapshot_from_manifests(
    *,
    results: Any = None,
    transcripts: Any = None,
    lyrics: Any = None,
    journeys: Any = None,
) -> dict[str, Any]:
    """Counts only: the digest is deliberately incapable of carrying content."""

    return {
        "rows": js.coalesce(_length(results, "results"), 0),
        "buckets": _count_by(js.coalesce(js.get(results, "results"), []), _bucket_of),
        "transcribed": js.coalesce(
            _length(transcripts, "transcripts"),
            _length(transcripts, "results"),
            _length(transcripts, "rows"),
            0,
        ),
        "clusters": js.coalesce(_length(lyrics, "clusters"), 0),
        "songJourneys": js.coalesce(_length(journeys, "journeys"), 0),
        "verdicts": _count_by(
            js.coalesce(js.get(journeys, "improvementIndex"), []),
            lambda dim: js.prop(dim, "verdict"),
        ),
    }


def digest_diff_lines(previous: Any, next_snapshot: Mapping[str, Any]) -> list[str]:
    """The digest's lines, with ``(+n)``/``(-n)`` against the previous snapshot."""

    has_previous = js.truthy(previous)

    def delta(before: Any, after: Any) -> str:
        diff = after - js.coalesce(before, 0)
        if not has_previous or diff == 0:
            return ""
        return f" ({'+' if diff > 0 else ''}{format_number(diff)})"

    def line(label: str, key: str) -> str:
        after = next_snapshot[key]
        return f"{label}: {format_number(after)}{delta(js.get(previous, key), after)}"

    lines = [
        line("takes classified", "rows"),
        line("transcribed", "transcribed"),
        line("lyric clusters", "clusters"),
        line("song journeys", "songJourneys"),
    ]
    buckets: Mapping[str, Any] = next_snapshot["buckets"]
    ordered = sorted(
        buckets.items(), key=lambda pair: js.utf16_key(f"{pair[0]},{js.to_string(pair[1])}")
    )
    previous_buckets = js.get(previous, "buckets")
    for bucket, count in ordered:
        before = js.get(previous_buckets, bucket)
        if has_previous and _strict_equal(before, count):
            continue
        lines.append(f"  {bucket}: {format_number(count)}{delta(before, count)}")
    return lines


# ---- effects ------------------------------------------------------------------------------


def _tail(data: bytes | None, chars: int) -> str:
    return (data or b"").decode("utf-8", errors="replace")[-chars:]


def _exit_reason(returncode: int) -> str:
    if returncode > 0:
        return str(returncode)
    try:
        name = signal.Signals(-returncode).name
    except ValueError:
        name = "?"
    return f"signal {name}"


def default_run_stage(stage: Stage, context: StageContext) -> dict[str, str]:
    """Run one stage as a child process in ``context.work_dir``; SIGTERM on timeout."""

    try:
        child = subprocess.Popen(
            stage.command(context.python),
            cwd=context.work_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, str(error.errno))
        raise StageFailure(f"{stage.key}: exit {code}") from error
    context.register_child(child)
    try:
        try:
            stdout, stderr = child.communicate(timeout=stage.timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                stdout, stderr = child.communicate(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate()
    finally:
        context.register_child(None)
    if child.returncode != 0:
        raise StageFailure(
            f"{stage.key}: exit {_exit_reason(child.returncode)}",
            _tail(stderr, STDERR_TAIL_CHARS),
        )
    return {"stdoutTail": _tail(stdout, STDOUT_TAIL_CHARS)}


def _message_fields(message: Notification) -> tuple[str, str, str, str]:
    return (
        message["title"],
        message["body"],
        message.get("priority", "default"),
        message.get("tags", ""),
    )


def send_ntfy(url: str, message: Notification, log: Log) -> None:
    """POST one notification to ntfy; a failure is logged, never raised."""

    title, body, priority, tags = _message_fields(message)
    headers = {"Title": title, "Priority": priority, "Content-Type": "text/plain;charset=UTF-8"}
    if tags:
        headers["Tags"] = tags
    request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=NTFY_TIMEOUT_SECONDS):
            pass
    except (OSError, ValueError) as error:
        log(f"ntfy delivery failed: {error}")


def write_outbox(directory: str, message: Notification, now_ms: int) -> Path:
    """Write one notification as one JSON file, atomically (see the module docstring)."""

    title, body, priority, tags = _message_fields(message)
    payload = {
        "schemaVersion": NOTIFICATION_SCHEMA,
        "createdAt": js.iso_from_ms(now_ms),
        "title": title,
        "body": body,
        "priority": priority,
        "tags": tags,
    }
    outbox = Path(directory)
    outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = outbox / f"{now_ms:013d}-{os.getpid()}-{secrets.token_hex(4)}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=outbox)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(stringify(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def _read_optional_json(path: str | Path) -> Any:
    try:
        return read_json(path)
    except FileNotFoundError:
        return None


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, float) and pid.is_integer():
        pid = int(pid)
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # EPERM: it exists but is not ours to signal
    except (OSError, OverflowError):
        return False
    return True


def _stderr_log(line: str) -> None:
    sys.stderr.write(f"[watchd] {line}\n")
    sys.stderr.flush()


# ---- the watcher --------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchOptions:
    corpus_root: str
    approval: str
    measurement_root: str | None = None
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    state_path: str | None = None
    ntfy_url: str | None = None
    notify_outbox: str | None = None
    digest_day: str = "sunday"
    digest_hour: int = 18
    work_dir: str | None = None
    manifests: Mapping[str, str] = field(default_factory=dict)
    paths: JourneyPaths | None = None


class Watchd:
    """``createWatchd``: :meth:`tick`, :meth:`run_once`, :meth:`run_forever`,
    :meth:`stop`, :meth:`status_snapshot`."""

    def __init__(
        self,
        options: WatchOptions,
        *,
        list_entries: Callable[[], list[Entry]] | None = None,
        run_stage: RunStage | None = None,
        notify: Notify | None = None,
        now: Callable[[], int] | None = None,
        on_ingested: Callable[[], None] | None = None,
        log: Log | None = None,
    ) -> None:
        if not options.corpus_root:
            raise WatchError("watchd requires corpusRoot")
        if not options.approval:
            raise WatchError("watchd requires an operator approval string for audio-reading stages")
        if options.ntfy_url and urllib.parse.urlsplit(options.ntfy_url).scheme not in (
            "http",
            "https",
        ):
            raise WatchError("the ntfy URL must be http or https")
        paths = options.paths or resolve_paths()
        self.options = options
        self.corpus_root = options.corpus_root
        self.measurement_root = (
            options.measurement_root
            if options.measurement_root is not None
            else str(paths.measurement_dir)
        )
        self.work_dir = os.path.abspath(options.work_dir or os.getcwd())
        self.state_path = options.state_path or str(paths.state_dir / STATE_FILE)
        self.lock_path = f"{self.state_path}.lock"
        defaults = {
            "index": "recording-index.json",
            "results": "full-corpus-filter-results.json",
            "transcripts": "local-stt-transcripts.json",
            "lyrics": "local-lyric-indicators.json",
            "journeys": "song-journeys.json",
        }
        self.manifest_paths = {
            key: os.path.normpath(
                os.path.join(self.work_dir, options.manifests.get(key) or paths.manifest(name))
            )
            for key, name in defaults.items()
        }
        self.stages = build_stages(options.corpus_root, options.approval, self.measurement_root)
        self._log = log or _stderr_log
        self._list_entries = list_entries or self._default_list_entries
        self._run_stage = run_stage or default_run_stage
        self._injected_notify = notify
        self._now = now or js.now_ms
        self._on_ingested = on_ingested
        self._busy = False
        self._stop = threading.Event()
        self._state: State | None = None
        self.current_child: subprocess.Popen[bytes] | None = None
        self.last_tick: dict[str, Any] | None = None

    # -- collaborators

    def _default_list_entries(self) -> list[Entry]:
        # Phone recordings and accepted measurement sessions are scanned together.
        return [
            *list_corpus_entries(self.corpus_root, mirror_manifest=True),
            *list_measurement_entries(self.measurement_root),
        ]

    @property
    def notifies(self) -> bool:
        return bool(self._injected_notify or self.options.ntfy_url or self.options.notify_outbox)

    def notify(self, message: Notification) -> None:
        if self._injected_notify is not None:
            self._injected_notify(message)
            return
        if self.options.notify_outbox:
            try:
                write_outbox(self.options.notify_outbox, message, self._now())
            except OSError as error:
                self._log(f"notification outbox write failed: {error}")
        if self.options.ntfy_url:
            send_ntfy(self.options.ntfy_url, message, self._log)

    def _iso_now(self) -> str:
        return js.iso_from_ms(self._now())

    def _next_digest(self) -> int:
        return next_digest_at(
            self._now(), day=self.options.digest_day, hour=self.options.digest_hour
        )

    # -- state

    def _load_state(self) -> State:
        if self._state is not None:
            return self._state
        persisted = _read_optional_json(self.state_path)
        if isinstance(persisted, dict) and _strict_equal(persisted.get("version"), STATE_VERSION):
            state: State = persisted
        else:
            state = {
                "version": STATE_VERSION,
                "lastRun": None,
                "recentRuns": [],
                "lastDigest": None,
                "nextDigestAt": None,
                "failure": None,
            }
        if not js.truthy(state.get("nextDigestAt", MISSING)):
            state["nextDigestAt"] = self._next_digest()
        state.setdefault("pendingSignature", None)
        state.setdefault("scanErrorStreak", 0)
        state.setdefault("lastTick", None)
        self._state = state
        return state

    def _persist_state(self) -> None:
        write_json(self.state_path, self._state, announce=False)

    # -- the lock

    def _create_lock(self, payload: str) -> bool:
        lock = Path(self.lock_path)
        lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{lock.name}.", dir=lock.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.link(temporary, lock)
        except FileExistsError:
            return False
        finally:
            Path(temporary).unlink(missing_ok=True)
        return True

    def _acquire_lock(self) -> bool:
        payload = stringify_compact({"pid": os.getpid(), "startedAt": self._iso_now()}) + "\n"
        for _ in range(2):
            if self._create_lock(payload):
                return True
            holder_pid = js.get(_read_optional_json(self.lock_path), "pid")
            if (
                js.truthy(holder_pid)
                and not _strict_equal(holder_pid, os.getpid())
                and _pid_alive(holder_pid)
            ):
                return False
            Path(self.lock_path).unlink(missing_ok=True)
        return False

    def _release_lock(self) -> None:
        Path(self.lock_path).unlink(missing_ok=True)

    # -- the pipeline

    def _register_child(self, child: subprocess.Popen[bytes] | None) -> None:
        self.current_child = child

    def _run_pipeline(self, new_count: int, signature: str) -> dict[str, Any]:
        if not self._acquire_lock():
            self._log("pipeline skipped: another run holds the lock")
            return {"ok": False, "skipped": "locked"}
        state = self._load_state()
        stages = self.stages
        run: dict[str, Any] = {
            "startedAt": self._iso_now(),
            "signature": signature,
            "newCount": new_count,
            "ok": False,
            "stages": [],
        }
        try:
            for stage in stages:
                stage_start = self._now()
                try:
                    result = self._run_stage(
                        stage, StageContext(self.work_dir, self._register_child)
                    )
                    run["stages"].append(
                        {"key": stage.key, "ok": True, "ms": self._now() - stage_start}
                    )
                    if result and result.get("stdoutTail"):
                        seconds = js_round((self._now() - stage_start) / 1000)
                        self._log(f"stage {stage.key} ok ({format_number(seconds)}s)")
                except Exception as error:
                    message = str(error)
                    run["stages"].append(
                        {
                            "key": stage.key,
                            "ok": False,
                            "ms": self._now() - stage_start,
                            "error": message,
                        }
                    )
                    tail = getattr(error, "stderr_tail", "")
                    if tail:
                        self._log(f"stage {stage.key} stderr tail: {tail}")
                    self.notify(
                        {
                            "title": "Voice Journey watchd: stage failed",
                            "body": (
                                f"{message} — pipeline aborted after {len(run['stages'])}/"
                                f"{len(stages)} stages; will retry when the corpus changes "
                                "or after backoff."
                            ),
                            "priority": "high",
                            "tags": "warning",
                        }
                    )
                    state["failure"] = {
                        "signature": signature,
                        "until": self._now() + FAILURE_BACKOFF_MS,
                    }
                    return run
            run["ok"] = True
            state["failure"] = None
            total_seconds = js_round(sum(stage["ms"] for stage in run["stages"]) / 1000)
            self.notify(
                {
                    "title": "Voice Journey watchd: ingested",
                    "body": (
                        f"{new_count} new take(s) processed through {len(stages)} stages in "
                        f"{format_number(total_seconds)}s."
                    ),
                    "tags": "white_check_mark",
                }
            )
            if self._on_ingested is not None:
                try:
                    self._on_ingested()
                except Exception as error:
                    self._log(f"on-ingested hook failed: {error}")
            return run
        finally:
            run["endedAt"] = self._iso_now()
            state["lastRun"] = run
            recent = state.get("recentRuns")
            state["recentRuns"] = [*(recent if isinstance(recent, list) else []), run][
                -MAX_RECENT_RUNS:
            ]
            self.current_child = None
            self._release_lock()
            self._persist_state()

    def _maybe_digest(self) -> None:
        if not self.notifies:
            return
        state = self._load_state()
        if self._now() < state["nextDigestAt"]:
            return
        paths = self.manifest_paths
        snapshot = digest_snapshot_from_manifests(
            results=_read_optional_json(paths["results"]),
            transcripts=_read_optional_json(paths["transcripts"]),
            lyrics=_read_optional_json(paths["lyrics"]),
            journeys=_read_optional_json(paths["journeys"]),
        )
        previous = js.coalesce(js.get(state.get("lastDigest"), "snapshot"), None)
        lines = digest_diff_lines(previous, snapshot)
        self.notify(
            {
                "title": "Voice Journey weekly digest",
                "body": "\n".join(lines),
                "tags": "musical_note",
            }
        )
        state["lastDigest"] = {"snapshot": snapshot, "sentAt": self._iso_now()}
        state["nextDigestAt"] = self._next_digest()
        self._persist_state()

    # -- ticks

    def _finish_tick(self, state: State, tick: dict[str, Any]) -> dict[str, Any]:
        self.last_tick = tick
        state["lastTick"] = tick
        self._persist_state()
        return tick

    def tick(self) -> dict[str, Any]:
        """One scan; runs the pipeline when two scans agree and differ from the index."""

        if self._busy:
            return {"status": "busy"}
        self._busy = True
        try:
            state = self._load_state()
            try:
                entries = self._list_entries()
            except Exception as error:
                streak = state.get("scanErrorStreak")
                state["scanErrorStreak"] = (streak if isinstance(streak, int) else 0) + 1
                self._log(f"scan failed: {error}")
                if state["scanErrorStreak"] == 3:
                    self.notify(
                        {
                            "title": "Voice Journey watchd: corpus scan failing",
                            "body": (
                                f"Three consecutive scan failures ({error}). Check corpus access."
                            ),
                            "priority": "high",
                            "tags": "rotating_light",
                        }
                    )
                return self._finish_tick(state, {"at": self._iso_now(), "status": "scan-error"})
            state["scanErrorStreak"] = 0

            signature = scan_signature(entries)
            if not _strict_equal(signature, state.get("pendingSignature")):
                # First sight of this corpus state: wait for a second agreeing scan so
                # mid-sync partial files never trigger a run.
                state["pendingSignature"] = signature
                return self._finish_tick(state, {"at": self._iso_now(), "status": "unstable"})

            index_manifest = _read_optional_json(self.manifest_paths["index"])
            new_count = count_new_since_index(entries, index_manifest)
            status = "idle"
            if new_count > 0:
                failure = js.get(state, "failure")
                last_run = js.get(state, "lastRun")
                failed_recently = _strict_equal(
                    js.get(failure, "signature"), signature
                ) and self._now() < js.prop(failure, "until")
                already_processed = js.truthy(js.get(last_run, "ok")) and _strict_equal(
                    js.prop(last_run, "signature"), signature
                )
                if not failed_recently and not already_processed:
                    run = self._run_pipeline(new_count, signature)
                    status = run.get("skipped") or ("ran" if run["ok"] else "failed")
                else:
                    status = "backoff" if failed_recently else "idle"
            self._maybe_digest()
            return self._finish_tick(
                state, {"at": self._iso_now(), "status": status, "newCount": new_count}
            )
        finally:
            self._busy = False

    def run_once(self) -> dict[str, Any]:
        """One timer-driven tick: the state is read from the state file, so the scan is
        compared with the previous run's persisted scan."""

        self._state = None
        return self.tick()

    def run_forever(self) -> None:
        """The loop mode: a tick now, then one every ``interval_seconds`` until stopped."""

        self._stop.clear()
        self._log(
            f"watching {self.corpus_root} every "
            f"{format_number(js_round(self.options.interval_seconds))}s"
        )
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:
                self._log(f"tick failed: {error}")
            self._stop.wait(self.options.interval_seconds)

    def stop(self) -> None:
        self._stop.set()

    def handle_shutdown_signal(self, signum: int, frame: FrameType | None = None) -> None:
        """SIGTERM/SIGINT: terminate the running stage, stop, exit 143/130."""

        if self.current_child is not None:
            self._log("shutdown: terminating active pipeline stage")
            self.current_child.terminate()
        self.stop()
        raise SystemExit(130 if signum == signal.SIGINT else 143)

    def status_snapshot(self) -> dict[str, Any]:
        state = self._state or {}
        recent = state.get("recentRuns")
        next_digest = state.get("nextDigestAt")
        return {
            "enabled": True,
            "intervalSeconds": js_round(self.options.interval_seconds),
            "corpusRoot": self.corpus_root,
            "approvalRecorded": bool(self.options.approval),
            "ntfy": bool(self.options.ntfy_url),
            "notifyOutbox": bool(self.options.notify_outbox),
            "lastTick": self.last_tick,
            "lastRun": state.get("lastRun"),
            "recentRuns": (recent if isinstance(recent, list) else [])[-5:],
            "nextDigestAt": js.iso_from_ms(next_digest) if next_digest else None,
            "stages": [stage.key for stage in self.stages],
        }


# ---- the command line ---------------------------------------------------------------------


def _positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError:
        seconds = 0
    if seconds < 1:
        raise argparse.ArgumentTypeError("--interval must be a positive integer of seconds")
    return seconds


def _digest_hour(value: str) -> int:
    try:
        hour = int(value)
    except ValueError:
        hour = -1
    if not 0 <= hour <= 23:
        raise argparse.ArgumentTypeError("--digest-hour must be an integer from 0 to 23")
    return hour


def build_parser(prog: str = "voice-lab journey watch") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Watch the journey corpus and run the pipeline when two scans agree and differ "
            "from the recording index. Paths default from VOICE_JOURNEY_* (journey/paths.py)."
        ),
    )
    parser.add_argument(
        "--once", action="store_true", help="one scan, for a timer (state in the state file)"
    )
    parser.add_argument(
        "--interval", type=_positive_seconds, default=DEFAULT_INTERVAL_SECONDS, metavar="SECONDS"
    )
    parser.add_argument(
        "--approval",
        metavar="TEXT",
        help="the operator approval for the audio-reading stages (else VOICE_JOURNEY_APPROVAL)",
    )
    parser.add_argument(
        "--notify-outbox",
        metavar="DIR",
        help="write each notification as one JSON file here (else VOICE_JOURNEY_NOTIFY_OUTBOX)",
    )
    parser.add_argument(
        "--ntfy", metavar="URL", help="POST notifications to ntfy (else VOICE_JOURNEY_NTFY)"
    )
    parser.add_argument("--corpus-root", metavar="PATH")
    parser.add_argument("--measurement-root", metavar="PATH")
    parser.add_argument("--state", metavar="PATH", help="default: <state dir>/watchd-state.json")
    parser.add_argument("--digest-day", type=str.lower, choices=tuple(DAY_INDEX), default="sunday")
    parser.add_argument("--digest-hour", type=_digest_hour, default=18, metavar="HOUR")
    for key in ("index", "results", "transcripts", "lyrics", "journeys"):
        parser.add_argument(f"--{key}", metavar="PATH", help=f"the {key} manifest")
    return parser


def _environment(name: str) -> str | None:
    return os.environ.get(name) or None


def main(argv: Sequence[str] | None = None, *, prog: str = "voice-lab journey watch") -> int:
    """``--once``: exit 0 after a tick, 1 when it failed (a stage or the scan); 2 for a
    refused configuration. The loop runs until SIGTERM (143) or SIGINT (130)."""

    options = build_parser(prog).parse_args(sys.argv[1:] if argv is None else argv)
    paths = resolve_paths()
    manifests = {
        key: value
        for key in ("index", "results", "transcripts", "lyrics", "journeys")
        if (value := getattr(options, key))
    }
    try:
        watcher = Watchd(
            WatchOptions(
                corpus_root=options.corpus_root or str(paths.phone_corpus_root),
                approval=options.approval or _environment("VOICE_JOURNEY_APPROVAL") or "",
                measurement_root=options.measurement_root,
                interval_seconds=options.interval,
                state_path=options.state,
                ntfy_url=options.ntfy or _environment("VOICE_JOURNEY_NTFY"),
                notify_outbox=options.notify_outbox or _environment("VOICE_JOURNEY_NOTIFY_OUTBOX"),
                digest_day=options.digest_day,
                digest_hour=options.digest_hour,
                manifests=manifests,
                paths=paths,
            )
        )
    except WatchError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2
    previous = {
        signum: signal.signal(signum, watcher.handle_shutdown_signal)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        if not options.once:
            watcher.run_forever()
            return 0
        try:
            result = watcher.run_once()
        except Exception as error:
            _stderr_log(f"tick failed: {error}")
            return 1
        sys.stdout.write(stringify_compact(result) + "\n")
        return 1 if result["status"] in FAILING_STATUSES else 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())

"""voice-journey's intake API (``intake.mjs``) as a compatibility path of voice-lab's API.

@canonical api.journey_intake -- the port of voice-journey's ``intake-session.v1`` protocol
for the separate capture client ``voice-journey-capture`` (design §3.4: during phase 3 the
API also answers voice-journey's unversioned ``/api/intake/…`` paths). The routes, their
response shapes and refusal codes are voice-journey's::

    POST /api/intake/sessions                       open (or resume) a session with its manifest
    GET  /api/intake/sessions/<id>                  status: per-file bytes received
    PUT  /api/intake/sessions/<id>/files/<name>     upload one file (whole, or Content-Range)
    POST /api/intake/sessions/<id>/finalize         verify everything, then hand it to intake

What changed is where a session goes (design §5.4, D-024). voice-journey renamed a finished
session into its own ``corpus/measurement/<id>/``. Here the API writes only ``incoming/``:
uploads are staged under ``incoming/.journey-uploads/<sessionId>/`` (a dot entry, which the
intake publisher never takes, and which cannot collide with ``/api/v1``'s
``incoming/.uploads/``), and finalize re-verifies every byte, converts the session into a
voice-lab capture package and renames it, in one step, to ``incoming/vj-<sessionId>``. The
network-less intake publisher (``intake_publisher.py``) validates it with the unchanged strict
validator and publishes it into ``raw/`` with a v2 receipt; it is the only publisher of raw
audio. A session is ``accepted`` only once that receipt exists.

The package is the one ``journey/measurement_package.py`` defines (the desktop app builds
the same package itself for ``/api/v1/intake``, phase 4b): ``vj-<sessionId>``, suite
``voice-journey-measurement``, subject ``cole-self`` by default, ``capture_host``
``voice-journey-capture`` here, the chain as the device, one file per WAV under its
segment's key, ``room-silence`` as the room tone and every other take ``calibration``,
``qc.json`` and ``READY`` last.

What does not fit the capture manifest (the input level, the distance, the segment labels
and modes, the client's own level statistics, who finalized it) stays in the session's
record, ``incoming/.journey-uploads/<sessionId>/session.json``: voice-journey's
``session.json`` plus an ``intake`` member naming the package. Those records live beside
the uploads and are not part of ``raw/``.

So that every session can be expressed, ``open`` refuses up front (422) what the package
cannot hold, beyond voice-journey's rules: a session id that is not lower case or ends in
``-``/``_`` (``invalid_session_id``), a measurement chain that is not a UMIK-1
(``chain_not_accepted``), a declared format other than mono 48 kHz 24-bit
(``format_not_accepted``), an ``endedAt`` before ``startedAt`` or outside the years 1-9999
(``invalid_timestamp``), a segment key that is not a lower-case slug
(``invalid_segment_key``), and a session without a ``room-silence`` segment
(``room_tone_required``). Finalize refuses WAVs that are not canonical mono 48 kHz s24le
(``audio_not_accepted``) and publishes nothing.

Other differences from the JavaScript, each deliberate:

- Identity: voice-journey trusted the door's ``X-Forwarded-User``/``X-Forwarded-Email``.
  This API verifies the ``Authorization: Bearer`` token itself (``api.auth``, as
  ``/api/v1`` does; design §3.5) and never reads the door's headers. No verified principal:
  ``401 identity_required``. The principal's subject is recorded as ``finalizedBy``.
- States: besides ``open`` and ``accepted``, a handed-off session is ``finalized`` (waiting
  for the publisher) or ``refused`` (the publisher refused it; ``reason`` says why). Finalize
  waits up to ``publish_wait_seconds`` for the receipt; still waiting, it answers
  ``503 publication_pending``, which the capture client retries later; a refused package is
  ``422 session_refused`` (to ``open`` and ``finalize``).
- A PUT and a finalize of the same session exclude each other (``409 session_busy``);
  voice-journey locked a file's PUT and the finalize separately.
- A body shorter than its ``Content-Length`` is ``incomplete_body``: Python's server cannot
  tell a client abort (voice-journey's ``upload_aborted``) from an early end, and never
  reads past ``Content-Length`` (``body_exceeds_content_length`` cannot happen).
- A staging volume other than ``incoming/``'s fails the rename (``500 internal_error``)
  instead of ``staging_not_on_corpus_volume``; staging lives inside ``incoming/``.
- Dot segments in the request path are not resolved (WHATWG ``URL`` would): such a path is
  refused as an invalid id or name.

Known policy gap (documented, not closed here): voice-lab's own uploads write the D-021
home-host ledger entry before the first byte leaves the capturing host
(``intake_client.py``). The separate capture client does not; this closes when capture
moves into the desktop app (phase 4).

Logs carry ids and counts only: never a file name, an identity or a transcript.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Final

from ..canonical import (
    fsync_directory,
    open_regular_no_follow,
    rename_no_replace,
)
from ..errors import SecurityError
from ..intake import RECEIPT_NAME, IntakeReceipt
from ..intake_publisher import read_refusal
from ..journey import jscompat as js
from ..journey import measurement_package as package
from ..journey.jsonio import MISSING, parse_json, read_json, stringify_compact, write_json
from ..journey.measurement_package import (
    DEFAULT_SUBJECT_ID,
    package_layout,
)
from ..journey.measurement_package import strict_equal as _strict_equal
from ..journey.paths import packaged_chains
from ..paths import AssetLayout
from .auth import AuthError, TokenVerifier
from .staging import ByteSource

INTAKE_SCHEMA_VERSION: Final = "voice-journey.intake-session.v1"
PATH_PREFIX: Final = "/api/intake/"
STAGING_NAME: Final = ".journey-uploads"
RECORD_NAME: Final = "session.json"
MANIFEST_FILE: Final = "manifest.json"
FILES_NAME: Final = "files"
CAPTURE_HOST: Final = "voice-journey-capture"
PUBLISH_WAIT_SECONDS: Final = 120.0
POLL_SECONDS: Final = 1.0
MAX_MANIFEST_BYTES: Final = 256 * 1024
MAX_RECORD_BYTES: Final = 1024 * 1024
COPY_CHUNK_BYTES: Final = 1024 * 1024

# JavaScript's /…$/u matches only at the very end and \d only ASCII digits.
SESSION_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}\Z")
FILE_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.wav\Z")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_CONTENT_RANGE: Final = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)\Z")
_UMIK: Final = re.compile(r"umik", re.IGNORECASE)
_PERCENT: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
RESERVED_NAMES: Final = frozenset({"session.json"})
_MAX_SAFE_INTEGER: Final = 2**53 - 1

Json = dict[str, Any]


@dataclass(frozen=True, slots=True)
class IntakeLimits:
    """voice-journey's ``DEFAULT_LIMITS``."""

    max_files: int = 64
    max_file_bytes: int = 1024**3
    max_session_bytes: int = 4 * 1024**3
    max_open_sessions: int = 8
    stale_ms: int = 7 * 24 * 60 * 60 * 1000


DEFAULT_LIMITS: Final = IntakeLimits()


class IntakeError(Exception):
    """A refusal: an HTTP status, a stable code and the detail members of the answer."""

    def __init__(self, status: int, code: str, detail: Json | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail: Json = detail or {}


# ---- JavaScript value rules ---------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """voice-journey's ``canonicalJson``: keys sorted by UTF-16 code units, compact."""

    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict) and value:
        keys = sorted(value, key=js.utf16_key)
        return (
            "{" + ",".join(f"{stringify_compact(k)}:{canonical_json(value[k])}" for k in keys) + "}"
        )
    return stringify_compact(value)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(js.utf8(text)).hexdigest()


def is_integer(value: Any) -> bool:
    """``Number.isInteger(value) && Number.isSafeInteger(value)``."""

    if not js.is_finite(value):
        return False
    number = float(value) if isinstance(value, float) else value
    if isinstance(number, float) and not number.is_integer():
        return False
    return bool(abs(number) <= _MAX_SAFE_INTEGER)


def decode_uri_component(text: str) -> str:
    """``decodeURIComponent``; ``ValueError`` where JavaScript throws ``URIError``."""

    if _PERCENT.search(text):
        raise ValueError("URI malformed")
    raw = bytearray()
    index = 0
    while index < len(text):
        if text[index] == "%":
            raw.append(int(text[index + 1 : index + 3], 16))
            index += 3
        else:
            raw.extend(text[index].encode("utf-8", "surrogatepass"))
            index += 1
    return raw.decode("utf-8")


# ---- the manifest -------------------------------------------------------------------------


def validate_manifest(value: Any, registry: Any, limits: IntakeLimits = DEFAULT_LIMITS) -> Json:
    """voice-journey's ``validateManifest``: the normalized manifest that is stored.

    Raises: ``IntakeError`` (422) naming the first defect, in voice-journey's order.
    """

    def bad(code: str, detail: Json | None = None) -> IntakeError:
        return IntakeError(422, code, detail)

    if not isinstance(value, dict):
        raise bad("manifest_not_object")
    session_id, chain_id = js.get(value, "sessionId"), js.get(value, "chainId")
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise bad("invalid_session_id")
    chains = js.coalesce(js.get(registry, "chains"), [])
    chain = next(
        (
            candidate
            for candidate in chains
            if _strict_equal(js.get(candidate, "chainId"), chain_id)
        ),
        None,
    )
    if chain is None:
        raise bad("unknown_chain", {"chainId": js.slice_utf16(js.to_string(chain_id), 64)})
    device = js.to_string(js.coalesce(js.get(chain, "deviceName"), ""))
    if js.get(chain, "kind") != "measurement" or not _UMIK.search(device):
        raise bad(
            "chain_not_accepted",
            {
                "chainId": chain_id,
                "reason": "only UMIK-1 measurement chains are accepted at launch",
            },
        )
    for key in ("startedAt", "endedAt"):
        if not isinstance(js.get(value, key), str) or js.date_parse(value[key]) is None:
            raise bad("invalid_timestamp", {"field": key})
    capture = js.get(value, "capture")
    if not js.truthy(capture) or not isinstance(capture, dict | list):
        raise bad("capture_required")
    if not js.is_finite(js.get(capture, "inputVolume")):
        raise bad("input_level_required")  # an array has no inputVolume: capture is an object
    distance = js.get(capture, "distanceCm")
    if not js.is_finite(distance) or distance <= 0:
        raise bad("distance_required")
    cal = js.get(capture, "calFileSha256")
    if cal is not None and not (isinstance(cal, str) and SHA256_RE.match(cal)):
        raise bad(
            "invalid_cal_file_hash",
            {"hint": "64 lowercase hex, or null when the cal file is missing"},
        )
    sample_rate, chain_rate = js.get(capture, "sampleRate"), js.get(chain, "sampleRate")
    if (
        sample_rate is not MISSING
        and js.truthy(chain_rate)
        and not _strict_equal(sample_rate, chain_rate)
    ):
        raise bad("sample_rate_mismatch", {"expected": chain_rate})
    files = js.get(value, "files")
    if not isinstance(files, list) or not files or len(files) > limits.max_files:
        raise bad("invalid_file_list", {"max": limits.max_files})
    names: set[str] = set()
    total: float = 0
    normalized_files: list[Json] = []
    for file in files:
        name = js.get(file, "name")
        if not isinstance(name, str) or not FILE_NAME_RE.match(name) or name in RESERVED_NAMES:
            raise bad("invalid_file_name")
        if name in names:
            raise bad("duplicate_file_name")
        names.add(name)
        digest, size = js.get(file, "sha256"), js.get(file, "sizeBytes")
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise bad("invalid_file_sha256")
        if not is_integer(size) or size <= 0:
            raise bad("invalid_file_size")
        if size > limits.max_file_bytes:
            raise bad("file_too_large", {"maxFileBytes": limits.max_file_bytes})
        total += size
        normalized_files.append({"name": name, "sha256": digest, "sizeBytes": size})
    if total > limits.max_session_bytes:
        raise bad("session_too_large", {"maxSessionBytes": limits.max_session_bytes})
    segments = js.get(value, "segments")
    if not isinstance(segments, list) or not segments:
        raise bad("segment_map_required")
    normalized_segments: list[Json] = []
    for segment in segments:
        key, file_name = js.get(segment, "key"), js.get(segment, "file")
        if not isinstance(key, str) or not isinstance(file_name, str):
            raise bad("invalid_segment")
        if file_name not in names:
            raise bad("segment_file_not_listed", {"key": js.slice_utf16(key, 64)})
        normalized_segments.append(dict(segment))
    return {
        "schemaVersion": INTAKE_SCHEMA_VERSION,
        "sessionId": session_id,
        "chainId": chain_id,
        "chainKind": chain["kind"],
        "controlled": js.truthy(js.get(chain, "controlled")),
        "startedAt": value["startedAt"],
        "endedAt": value["endedAt"],
        "capture": dict(capture),
        "segments": normalized_segments,
        "files": normalized_files,
    }


def require_package_mapping(manifest: Json, registry: Any) -> None:
    """The rules a voice-lab capture package adds to voice-journey's
    (``journey.measurement_package``). Raises: ``IntakeError`` (422)."""

    try:
        package.require_package_mapping(manifest, registry)
    except package.PackageRefusal as refusal:
        raise IntakeError(422, refusal.code, refusal.detail) from refusal


def parse_content_range(header: str, declared_size: int) -> tuple[int, int, int]:
    match = _CONTENT_RANGE.match(header)
    if match is None:
        raise IntakeError(400, "invalid_content_range")
    start, end, total = (int(group) for group in match.groups())
    if total != declared_size or end < start or end >= total:
        raise IntakeError(416, "content_range_out_of_bounds", {"sizeBytes": declared_size})
    return start, end, total


# ---- files --------------------------------------------------------------------------------


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open_regular_no_follow(path) as stream:
        while chunk := stream.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _stream_to(path: Path, body: ByteSource, *, append: bool, expected: int) -> int:
    """Write at most ``expected`` bytes of ``body`` to ``path``; the bytes written."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= os.O_APPEND if append else os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    received = 0
    try:
        while received < expected:
            chunk = body.read(min(COPY_CHUNK_BYTES, expected - received))
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                view = view[os.write(descriptor, view) :]
            received += len(chunk)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return received


def _answer(status: int, payload: Json) -> tuple[int, bytes]:
    return status, (stringify_compact(payload) + "\n").encode("utf-8")


# ---- the service --------------------------------------------------------------------------


class JourneyIntake:
    """voice-journey's ``createIntake``, staging under ``incoming/`` of one asset root."""

    def __init__(
        self,
        layout: AssetLayout,
        *,
        verifier: TokenVerifier,
        chains_path: Path | None = None,
        subject_id: str = DEFAULT_SUBJECT_ID,
        limits: IntakeLimits = DEFAULT_LIMITS,
        log: Callable[[str], None] | None = None,
        now: Callable[[], float] = js.now_ms,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        publish_wait_seconds: float = PUBLISH_WAIT_SECONDS,
    ) -> None:
        self.layout = layout
        self.incoming = layout.directory("incoming")
        self.staging_dir = self.incoming / STAGING_NAME
        self.chains_path = chains_path or packaged_chains()
        self.subject_id = subject_id
        self.limits = limits
        self._verifier = verifier
        self._log = log or (lambda line: sys.stderr.write(f"[intake] {line}\n"))
        self._now = now
        self._sleep = sleep
        self._monotonic = monotonic
        self.publish_wait_seconds = publish_wait_seconds
        self._busy: set[str] = set()
        self._guard = threading.Lock()

    # -- paths and records ------------------------------------------------------------------

    def _session_dir(self, session_id: str) -> Path:
        return self.staging_dir / session_id

    def _files_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id) / FILES_NAME

    def _read(self, path: Path) -> Json | None:
        try:
            value = read_json(path)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise SecurityError(f"a staged record is not an object: {path}")
        return value

    def _read_staged(self, session_id: str) -> Json | None:
        return self._read(self._session_dir(session_id) / MANIFEST_FILE)

    def _read_record(self, session_id: str) -> Json | None:
        return self._read(self._session_dir(session_id) / RECORD_NAME)

    def _handed_off(self, session_id: str, record: Json | None) -> bool:
        return record is not None and not os.path.lexists(self._files_dir(session_id))

    def _located(self, session_id: str, manifest: Json, name: str) -> Path | None:
        """Where a received file is: as uploaded, or already in its package directory."""

        files = self._files_dir(session_id)
        path = next(path for listed, path, _ in package_layout(manifest) if listed == name)
        for candidate in (files / name, files / path):
            if _stat_or_none(candidate) is not None:
                return candidate
        return None

    def _progress(self, manifest: Json) -> list[Json]:
        files: list[Json] = []
        session_id = manifest["sessionId"]
        for file in manifest["files"]:
            done = self._located(session_id, manifest, file["name"])
            part = (
                None
                if done is not None
                else _stat_or_none(self._files_dir(session_id) / f"{file['name']}.part")
            )
            received = done.lstat().st_size if done is not None else part.st_size if part else 0
            files.append(
                {
                    "name": file["name"],
                    "sizeBytes": file["sizeBytes"],
                    "receivedBytes": received,
                    "complete": done is not None,
                }
            )
        return files

    def _publication(self, record: Json) -> tuple[str, str | None]:
        """``accepted``, ``refused`` (with the reason) or ``finalized`` (still waiting)."""

        package = record["intake"]["package"]
        package_id, manifest_sha256 = package["sessionId"], package["manifestSha256"]
        receipt_path = self.layout.directory("raw") / manifest_sha256 / RECEIPT_NAME
        if receipt_path.exists():
            with open_regular_no_follow(receipt_path) as stream:
                receipt = IntakeReceipt.model_validate_json(stream.read(MAX_RECORD_BYTES))
            if receipt.session_id != package_id or receipt.manifest_sha256 != manifest_sha256:
                raise SecurityError("the raw receipt names another session or manifest")
            return "accepted", None
        refusal = read_refusal(self.layout, package_id)
        if refusal is not None and refusal.manifest_sha256 in {None, manifest_sha256}:
            return "refused", refusal.error
        return "finalized", None

    def _settled(self, session_id: str, record: Json) -> Json:
        state, reason = self._publication(record)
        if state == "refused":
            raise IntakeError(422, "session_refused", {"sessionId": session_id, "reason": reason})
        return {"sessionId": session_id, "state": state}

    # -- locking ----------------------------------------------------------------------------

    @contextmanager
    def _lock(self, key: str) -> Iterator[None]:
        """voice-journey's ``withLock``: a held key is ``409 session_busy``. A file's key
        (``<id>/<name>``) and its session's key also exclude each other."""

        session, _, _ = key.partition("/")
        with self._guard:
            held = key in self._busy or (
                session in self._busy
                if key != session
                else any(other.startswith(f"{session}/") for other in self._busy)
            )
            if held:
                raise IntakeError(409, "session_busy")
            self._busy.add(key)
        try:
            yield
        finally:
            with self._guard:
                self._busy.discard(key)

    # -- the routes -------------------------------------------------------------------------

    def purge_stale(self) -> int:
        """Remove open sessions untouched (by their directory's time) for ``stale_ms``;
        returns how many open sessions remain. Handed-off sessions are records: kept."""

        try:
            entries = sorted(self.staging_dir.iterdir())
        except FileNotFoundError:
            return 0
        open_sessions = 0
        for entry in entries:
            info = _stat_or_none(entry)
            if info is None or not stat.S_ISDIR(info.st_mode):
                continue
            if (entry / RECORD_NAME).exists():
                continue
            stale = self._now() - info.st_mtime_ns / 1e6 > self.limits.stale_ms
            with self._guard:
                busy = entry.name in self._busy
            if stale and not busy:
                shutil.rmtree(entry)
            else:
                open_sessions += 1
        return open_sessions

    def _open_session(self, headers: Message, body: ByteSource) -> tuple[int, Json]:
        length = js.string_to_number(headers.get("Content-Length") or "")
        if length > MAX_MANIFEST_BYTES:
            raise IntakeError(413, "manifest_too_large")
        text = body.read(int(length) if is_integer(length) and length > 0 else 0)
        try:
            parsed = parse_json(text.decode("utf-8", "replace"))
        except (ValueError, RecursionError) as error:
            raise IntakeError(400, "invalid_json") from error
        registry = read_json(self.chains_path)
        manifest = validate_manifest(parsed, registry, self.limits)
        require_package_mapping(manifest, registry)
        digest = sha256_hex(canonical_json(manifest))
        session_id = manifest["sessionId"]
        with self._lock(session_id):
            staged = self._read_staged(session_id)
            if staged is not None:
                record = self._read_record(session_id)
                handed_off = self._handed_off(session_id, record)
                if sha256_hex(canonical_json(staged)) != digest:
                    code = "session_already_accepted" if handed_off else "session_manifest_conflict"
                    raise IntakeError(409, code)
                if handed_off:
                    assert record is not None
                    return 200, self._settled(session_id, record)
                return 200, {
                    "sessionId": session_id,
                    "state": "open",
                    "files": self._progress(staged),
                }
            if self.purge_stale() >= self.limits.max_open_sessions:
                raise IntakeError(429, "too_many_open_sessions")
            self.staging_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(self.staging_dir, 0o700)
            self._session_dir(session_id).mkdir(mode=0o700, exist_ok=True)
            self._files_dir(session_id).mkdir(mode=0o700, exist_ok=True)
            write_json(
                self._session_dir(session_id) / MANIFEST_FILE,
                manifest,
                announce=False,
                compact=True,
            )
            self._log(f"session {session_id} opened files={len(manifest['files'])}")
            return 201, {
                "sessionId": session_id,
                "state": "open",
                "files": self._progress(manifest),
                "limits": {"maxFileBytes": self.limits.max_file_bytes},
            }

    def _status(self, session_id: str) -> tuple[int, Json]:
        staged = self._read_staged(session_id)
        if staged is None:
            raise IntakeError(404, "session_not_found")
        record = self._read_record(session_id)
        if self._handed_off(session_id, record):
            assert record is not None
            state, reason = self._publication(record)
            payload: Json = {"sessionId": session_id, "state": state}
            if reason is not None:
                payload["reason"] = reason
            return 200, payload
        return 200, {"sessionId": session_id, "state": "open", "files": self._progress(staged)}

    def _put_file(
        self, headers: Message, body: ByteSource, session_id: str, name: str
    ) -> tuple[int, Json]:
        staged = self._read_staged(session_id)
        if staged is None:
            raise IntakeError(404, "session_not_found")
        if self._read_record(session_id) is not None:
            raise IntakeError(409, "session_already_accepted")
        file = next((item for item in staged["files"] if item["name"] == name), None)
        if file is None:
            raise IntakeError(404, "file_not_in_manifest")
        with self._lock(f"{session_id}/{name}"):
            directory = self._files_dir(session_id)
            part = directory / f"{name}.part"
            existing = self._located(session_id, staged, name)
            if existing is not None:
                # Idempotent re-upload: a verified file is never rewritten.
                size = existing.lstat().st_size
                return 200, {"name": name, "state": "already_received", "sizeBytes": size}
            declared = js.string_to_number(headers.get("Content-Length") or "")
            if not is_integer(declared) or declared <= 0:
                raise IntakeError(411, "content_length_required")
            declared_length = int(declared)
            if declared_length > file["sizeBytes"]:
                raise IntakeError(413, "file_too_large", {"sizeBytes": file["sizeBytes"]})
            start = 0
            content_range = headers.get("Content-Range")
            if content_range is not None:
                first, last, _ = parse_content_range(content_range, file["sizeBytes"])
                info = _stat_or_none(part)
                have = info.st_size if info else 0
                if first != have:
                    raise IntakeError(409, "range_not_contiguous", {"receivedBytes": have})
                if last - first + 1 != declared_length:
                    raise IntakeError(400, "content_length_range_mismatch")
                start = first
            else:
                if declared_length != file["sizeBytes"]:
                    raise IntakeError(422, "size_mismatch", {"sizeBytes": file["sizeBytes"]})
                part.unlink(missing_ok=True)
            written = _stream_to(part, body, append=start > 0, expected=declared_length)
            if written != declared_length:
                if start == 0:
                    part.unlink(missing_ok=True)
                held = part.lstat().st_size if start > 0 else 0
                raise IntakeError(400, "incomplete_body", {"receivedBytes": held})
            part_size = part.lstat().st_size
            if part_size < file["sizeBytes"]:
                return 202, {"name": name, "state": "partial", "receivedBytes": part_size}
            if _sha256_file(part) != file["sha256"]:
                part.unlink()
                raise IntakeError(422, "checksum_mismatch", {"name": name})
            os.rename(part, directory / name)
            fsync_directory(directory)
            return 200, {"name": name, "state": "received", "sizeBytes": file["sizeBytes"]}

    def _finalize(self, session_id: str, principal: str) -> tuple[int, Json]:
        with self._lock(session_id):
            staged = self._read_staged(session_id)
            if staged is None:
                raise IntakeError(404, "session_not_found")
            record = self._read_record(session_id)
            if self._handed_off(session_id, record):
                assert record is not None
                return self._await_publication(session_id, record, {})
            record = self._hand_off(staged, principal)
            count = len(staged["files"])
            size = sum(file["sizeBytes"] for file in staged["files"])
            self._log(f"session {session_id} finalized files={count} bytes={size}")
            return self._await_publication(session_id, record, {"files": count, "bytes": size})

    def _hand_off(self, staged: Json, principal: str) -> Json:
        """Verify, convert to a capture package and rename it into ``incoming/``."""

        session_id = staged["sessionId"]
        directory = self._files_dir(session_id)
        missing: list[str] = []
        mismatched: list[str] = []
        located: dict[str, Path] = {}
        for file in staged["files"]:
            path = self._located(session_id, staged, file["name"])
            if path is None:
                missing.append(file["name"])
            elif path.lstat().st_size != file["sizeBytes"] or _sha256_file(path) != file["sha256"]:
                mismatched.append(file["name"])
            else:
                located[file["name"]] = path
        if mismatched:
            # Bytes on disk disagree with the manifest: drop them so the client re-sends.
            for name in mismatched:
                path = self._located(session_id, staged, name)
                assert path is not None
                path.unlink()
            raise IntakeError(422, "checksum_mismatch", {"files": mismatched})
        if missing:
            raise IntakeError(409, "incomplete", {"missing": missing})
        self._tidy(directory, package_layout(staged))
        try:
            manifest, manifest_sha256 = package.write_package(
                staged,
                located,
                directory,
                subject_id=self.subject_id,
                capture_host=CAPTURE_HOST,
                move=True,
            )
        except package.PackageRefusal as refusal:
            raise IntakeError(422, refusal.code, refusal.detail) from refusal
        record = {
            **staged,
            "intake": {
                "finalizedAt": js.iso_from_ms(self._now()),
                "finalizedBy": principal,
                "manifestSha256": sha256_hex(canonical_json(staged)),
                "package": {"sessionId": manifest.session_id, "manifestSha256": manifest_sha256},
            },
        }
        write_json(self._session_dir(session_id) / RECORD_NAME, record, announce=False)
        try:
            rename_no_replace(directory, self.incoming / manifest.session_id)
        except FileExistsError as error:
            raise IntakeError(409, "session_already_accepted") from error
        fsync_directory(self._session_dir(session_id))
        fsync_directory(self.incoming)
        return record

    def _tidy(self, directory: Path, layout: Sequence[tuple[str, str, str]]) -> None:
        """Remove everything in ``files/`` that is neither a declared file nor its
        package directory (partial uploads, a crashed finalize's control files)."""

        expected = {name for name, _, _ in layout} | {path for _, path, _ in layout}
        expected |= {path.partition("/")[0] for _, path, _ in layout if "/" in path}
        for root, directories, files in os.walk(directory, topdown=True):
            for entry in [*directories, *files]:
                relative = (Path(root) / entry).relative_to(directory).as_posix()
                if relative not in expected:
                    _remove(Path(root) / entry)
                    if entry in directories:
                        directories.remove(entry)

    def _await_publication(self, session_id: str, record: Json, counts: Json) -> tuple[int, Json]:
        deadline = self._monotonic() + self.publish_wait_seconds
        while True:
            answer = self._settled(session_id, record)
            if answer["state"] == "accepted":
                if counts:
                    self._log(f"session {session_id} accepted files={counts['files']}")
                return 200, {**answer, **counts}
            if self._monotonic() >= deadline:
                raise IntakeError(503, "publication_pending", answer)
            self._sleep(POLL_SECONDS)

    # -- dispatch ---------------------------------------------------------------------------

    def handle(
        self, method: str, path: str, headers: Message, body: ByteSource
    ) -> tuple[int, bytes]:
        """Answer one request under ``/api/intake/``: the status and the JSON body."""

        try:
            try:
                principal = self._verifier.verify_header(headers.get("Authorization"))
            except AuthError as error:
                raise IntakeError(401, "identity_required") from error
            parts = path.split("/")[3:]  # after /api/intake/
            if parts[0] != "sessions":
                raise IntakeError(404, "not_found")
            session_id = None if len(parts) < 2 else decode_uri_component(parts[1])
            if session_id is not None and not SESSION_ID_RE.match(session_id):
                raise IntakeError(400, "invalid_session_id")
            if len(parts) == 1:
                self._only(method, "POST")
                return _answer(*self._open_session(headers, body))
            assert session_id is not None
            if len(parts) == 2:
                self._only(method, "GET")
                return _answer(*self._status(session_id))
            if len(parts) == 3 and parts[2] == "finalize":
                self._only(method, "POST")
                return _answer(*self._finalize(session_id, principal.subject))
            if len(parts) == 4 and parts[2] == "files":
                self._only(method, "PUT")
                name = decode_uri_component(parts[3])
                if not FILE_NAME_RE.match(name):
                    raise IntakeError(400, "invalid_file_name")
                return _answer(*self._put_file(headers, body, session_id, name))
            raise IntakeError(404, "not_found")
        except IntakeError as error:
            return _answer(error.status, {"error": error.code, **error.detail})
        except Exception as error:  # noqa: BLE001 (any other failure is the server's)
            self._log(f"internal error: {type(error).__name__}")
            return _answer(500, {"error": "internal_error"})

    @staticmethod
    def _only(method: str, allowed: str) -> None:
        if method != allowed:
            raise IntakeError(405, "method_not_allowed")

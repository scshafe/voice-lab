"""Resumable upload sessions under ``incoming/.uploads/`` (D-024).

@canonical api.staging -- the port of voice-journey's ``intake-session.v1`` protocol to
voice-lab capture packages. A client declares every file of its package (path, SHA-256,
size); the server stores each one as ``<path>.part``, appending only contiguous ranges, and
renames it into place once its bytes hash to the declaration. Finalizing re-hashes every
file and renames the complete package to ``incoming/<session_id>``, in one step on one
filesystem, where the intake publisher (no network) validates and publishes it. The API
process never writes ``raw/`` and never decides acceptance: the strict validator does.

Layout, all on the asset root's filesystem and private to the service account::

    incoming/.uploads/<session_id>/request.json     the canonical declaration
    incoming/.uploads/<session_id>/package/...      received files and .part files
    incoming/.uploads/<session_id>/FINALIZED        written once the package moved on
    incoming/<session_id>/                          the finalized package, for the publisher

The publisher skips dot entries of ``incoming/`` (``intake scan`` always has). Status after
finalizing is read from the publisher's records: the receipt in ``raw/<manifest>/`` or a
refusal under ``provenance/intake/refusals/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from pydantic import ValidationError

from ..canonical import (
    canonical_json_bytes,
    fsync_directory,
    open_regular_no_follow,
    rename_no_replace,
    write_new_private_file,
)
from ..capture import MANIFEST_NAME
from ..errors import SecurityError
from ..intake import MAX_CONTROL_FILE_BYTES, MIN_FREE_BYTES, RECEIPT_NAME, IntakeReceipt
from ..intake_publisher import read_refusal
from ..paths import AssetLayout
from .contracts import (
    PART_SUFFIX,
    ApiErrorCode,
    FileProgress,
    IntakeSessionRequest,
    IntakeSessionStatus,
    validate_package_path,
)

UPLOADS_NAME: Final = ".uploads"
REQUEST_NAME: Final = "request.json"
PACKAGE_NAME: Final = "package"
FINALIZED_NAME: Final = "FINALIZED"
MAX_OPEN_SESSIONS: Final = 8
# A session nobody has touched for this long is removed when another one opens.
STALE_SESSION_SECONDS: Final = 7 * 24 * 3600
COPY_CHUNK_BYTES: Final = 1024 * 1024
MAX_REQUEST_FILE_BYTES: Final = 1024 * 1024


class ByteSource(Protocol):
    """Where a chunk's bytes come from: the request body, or a file in tests."""

    def read(self, size: int = -1, /) -> bytes: ...


class StagingError(Exception):
    """A refusal with a stable code, an HTTP status and a message without secrets."""

    def __init__(
        self, status: int, code: ApiErrorCode, detail: str, *, received_bytes: int | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code: ApiErrorCode = code
        self.detail = detail
        self.received_bytes = received_bytes


@dataclass(frozen=True, slots=True)
class ChunkResult:
    """What a ``PUT`` did: the bytes now held, and whether the file is complete."""

    received_bytes: int
    complete: bool


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open_regular_no_follow(path) as stream:
        while chunk := stream.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _regular_size(path: Path) -> int | None:
    """The size of a regular file at ``path``; ``None`` when nothing is there."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SecurityError(f"upload staging holds something that is not a plain file: {path}")
    return metadata.st_size


def _require_profile_take(package: Path, profile: str) -> None:
    """The finalized package is a v2 take of ``profile`` (its manifest says so).

    Raises: ``StagingError`` 422 ``take_profile_not_allowed``."""
    from ..corpus_takes import TAKE_V2_SCHEMA, manifest_schema

    refusal = StagingError(
        422,
        "take_profile_not_allowed",
        f"this client sends only takes of capture profile {profile} "
        "(cole-voice-lab.corpus-take/v2)",
    )
    try:
        with open_regular_no_follow(package / MANIFEST_NAME, nonblocking=True) as stream:
            payload = stream.read(MAX_CONTROL_FILE_BYTES + 1)
    except (SecurityError, OSError) as error:
        raise refusal from error
    if len(payload) > MAX_CONTROL_FILE_BYTES or manifest_schema(payload) != TAKE_V2_SCHEMA:
        raise refusal
    captured = json.loads(payload).get("capture_profile")
    if not isinstance(captured, dict) or captured.get("profile") != profile:
        raise refusal


class IntakeStaging:
    """Upload sessions for one asset root; safe for the server's threads.

    Every operation on one session holds that session's lock; ``open_session`` also holds a
    lock across sessions, so the open-session limit cannot be raced.
    """

    def __init__(
        self,
        layout: AssetLayout,
        *,
        min_free_bytes: int = MIN_FREE_BYTES,
        free_bytes: Callable[[Path], int] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.layout = layout
        self.incoming = layout.directory("incoming")
        self.uploads = self.incoming / UPLOADS_NAME
        self.min_free_bytes = min_free_bytes
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._clock = clock
        self._open_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        if self.uploads.is_symlink():
            raise SecurityError(f"upload staging must be a real directory: {self.uploads}")
        self.uploads.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.uploads, 0o700)

    # -- locking and paths ------------------------------------------------------------------

    @contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        with self._locks_guard:
            lock = self._locks.setdefault(session_id, threading.Lock())
        if not lock.acquire(timeout=30):
            raise StagingError(409, "session_busy", "another request holds this session")
        try:
            yield
        finally:
            lock.release()

    def _session_directory(self, session_id: str) -> Path:
        return self.uploads / session_id

    def _read_request(self, session_id: str) -> IntakeSessionRequest:
        path = self._session_directory(session_id) / REQUEST_NAME
        try:
            with open_regular_no_follow(path) as stream:
                data = stream.read(MAX_REQUEST_FILE_BYTES + 1)
        except SecurityError as error:
            if not path.exists():
                raise StagingError(404, "session_not_found", "no such upload session") from error
            raise
        try:
            return IntakeSessionRequest.model_validate_json(data)
        except ValidationError as error:
            raise SecurityError(f"stored upload request is invalid: {path}") from error

    def _member(self, session_id: str, path: str) -> Path:
        validate_package_path(path)
        return self._session_directory(session_id).joinpath(
            PACKAGE_NAME, *PurePosixPath(path).parts
        )

    # -- status -----------------------------------------------------------------------------

    def _progress(self, session_id: str, request: IntakeSessionRequest) -> tuple[FileProgress, ...]:
        progress: list[FileProgress] = []
        for declared in request.files:
            member = self._member(session_id, declared.path)
            complete = _regular_size(member)
            partial = _regular_size(member.with_name(member.name + PART_SUFFIX))
            received = complete if complete is not None else (partial or 0)
            progress.append(
                FileProgress(
                    path=declared.path,
                    size_bytes=declared.size_bytes,
                    received_bytes=received,
                    complete=complete is not None,
                )
            )
        return tuple(progress)

    def _finalized_status(
        self, session_id: str, request: IntakeSessionRequest
    ) -> IntakeSessionStatus:
        files = tuple(
            FileProgress(
                path=declared.path,
                size_bytes=declared.size_bytes,
                received_bytes=declared.size_bytes,
                complete=True,
            )
            for declared in request.files
        )
        receipt_path = self.layout.directory("raw") / request.manifest_sha256 / RECEIPT_NAME
        if receipt_path.exists():
            with open_regular_no_follow(receipt_path) as stream:
                receipt = IntakeReceipt.model_validate_json(stream.read(MAX_REQUEST_FILE_BYTES))
            if (
                receipt.session_id != session_id
                or receipt.manifest_sha256 != request.manifest_sha256
            ):
                raise SecurityError("the raw receipt names another session or manifest")
            return IntakeSessionStatus(
                session_id=session_id,
                manifest_sha256=request.manifest_sha256,
                state="accepted",
                files=files,
                receipt=receipt,
            )
        refusal = read_refusal(self.layout, session_id)
        if refusal is not None and refusal.manifest_sha256 in {None, request.manifest_sha256}:
            return IntakeSessionStatus(
                session_id=session_id,
                manifest_sha256=request.manifest_sha256,
                state="refused",
                files=files,
                refusal=refusal.error,
            )
        return IntakeSessionStatus(
            session_id=session_id,
            manifest_sha256=request.manifest_sha256,
            state="finalized",
            files=files,
        )

    def _status(self, session_id: str, request: IntakeSessionRequest) -> IntakeSessionStatus:
        if (self._session_directory(session_id) / FINALIZED_NAME).exists():
            return self._finalized_status(session_id, request)
        return IntakeSessionStatus(
            session_id=session_id,
            manifest_sha256=request.manifest_sha256,
            state="open",
            files=self._progress(session_id, request),
        )

    def status(self, session_id: str) -> IntakeSessionStatus:
        """The session's state; ``session_not_found`` when there is none."""
        with self._session_lock(session_id):
            return self._status(session_id, self._read_request(session_id))

    # -- open -------------------------------------------------------------------------------

    def _open_sessions(self) -> list[Path]:
        sessions: list[Path] = []
        for candidate in self.uploads.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                raise SecurityError(f"upload staging holds an unexpected entry: {candidate}")
            if not (candidate / FINALIZED_NAME).exists():
                sessions.append(candidate)
        return sessions

    def _last_touched(self, directory: Path) -> float:
        newest = directory.lstat().st_mtime
        for root, directories, files in os.walk(directory, followlinks=False):
            for name in (*directories, *files):
                newest = max(newest, (Path(root) / name).lstat().st_mtime)
        return newest

    def _remove_stale(self) -> None:
        now = self._clock()
        for session in self._open_sessions():
            if now - self._last_touched(session) > STALE_SESSION_SECONDS:
                shutil.rmtree(session)

    def open_session(self, request: IntakeSessionRequest) -> tuple[IntakeSessionStatus, bool]:
        """Open a session, or return the existing one if its declaration is identical.

        Returns: the status and whether the session was created by this call.
        Raises: ``StagingError`` — ``session_conflict`` (409) when the id is in use with
        another declaration, or a package of that id already waits in ``incoming/``;
        ``too_many_open_sessions`` (429); ``insufficient_storage`` (507) when the declared
        bytes would take the disk below the intake floor.
        """
        session_id = request.session_id
        with self._open_lock, self._session_lock(session_id):
            directory = self._session_directory(session_id)
            if directory.exists():
                existing = self._read_request(session_id)
                if canonical_json_bytes(existing) != canonical_json_bytes(request):
                    raise StagingError(
                        409, "session_conflict", "this session id was opened with other files"
                    )
                return self._status(session_id, existing), False
            if os.path.lexists(self.incoming / session_id):
                raise StagingError(
                    409, "session_conflict", "a package with this session id awaits intake"
                )
            self._remove_stale()
            if len(self._open_sessions()) >= MAX_OPEN_SESSIONS:
                raise StagingError(
                    429, "too_many_open_sessions", f"at most {MAX_OPEN_SESSIONS} open sessions"
                )
            declared = sum(item.size_bytes for item in request.files)
            if self._free_bytes(self.uploads) < self.min_free_bytes + declared:
                raise StagingError(
                    507, "insufficient_storage", "the session would take the disk below its floor"
                )
            directory.mkdir(mode=0o700)
            package = directory / PACKAGE_NAME
            package.mkdir(mode=0o700)
            for parent in sorted(
                {
                    parent
                    for item in request.files
                    for parent in PurePosixPath(item.path).parents
                    if parent != PurePosixPath(".")
                },
                key=lambda value: len(value.parts),
            ):
                package.joinpath(*parent.parts).mkdir(mode=0o700)
            write_new_private_file(
                directory / REQUEST_NAME, canonical_json_bytes(request) + b"\n", mode=0o600
            )
            fsync_directory(self.uploads)
            return self._status(session_id, request), True

    # -- receive ----------------------------------------------------------------------------

    def put_chunk(
        self,
        session_id: str,
        path: str,
        stream: ByteSource,
        *,
        length: int,
        start: int,
        total: int | None,
    ) -> ChunkResult:
        """Append ``length`` bytes at offset ``start`` of a declared file.

        ``start`` must equal the bytes already held (``range_not_contiguous`` otherwise,
        carrying the server's count so the client can resume from it). ``total``, when the
        request named one, must equal the declared size. When the file is complete its
        digest is checked: a mismatch removes it (``checksum_mismatch``).
        """
        with self._session_lock(session_id):
            request = self._read_request(session_id)
            if (self._session_directory(session_id) / FINALIZED_NAME).exists():
                raise StagingError(409, "session_finalized", "the session is already finalized")
            declared = request.declared(path)
            if declared is None:
                raise StagingError(404, "file_not_declared", "the session does not declare it")
            if total is not None and total != declared.size_bytes:
                raise StagingError(
                    416, "content_range_out_of_bounds", "the range's total is not the file's size"
                )
            if length < 0 or start < 0 or start + length > declared.size_bytes:
                raise StagingError(
                    416, "content_range_out_of_bounds", "the range runs past the declared size"
                )
            member = self._member(session_id, path)
            part = member.with_name(member.name + PART_SUFFIX)
            if _regular_size(member) is not None:
                raise StagingError(
                    409, "file_already_received", "this file was already received and checked"
                )
            held = _regular_size(part) or 0
            if start != held:
                raise StagingError(
                    409,
                    "range_not_contiguous",
                    f"the server holds {held} bytes of this file",
                    received_bytes=held,
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(part, flags, 0o600)
            written = 0
            try:
                while written < length:
                    chunk = stream.read(min(COPY_CHUNK_BYTES, length - written))
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(descriptor, view) :]
                    written += len(chunk)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            held += written
            if written != length:
                raise StagingError(
                    400,
                    "bad_request",
                    "the body ended before its Content-Length",
                    received_bytes=held,
                )
            if held < declared.size_bytes:
                return ChunkResult(received_bytes=held, complete=False)
            digest, size = _hash_file(part)
            if digest != declared.sha256 or size != declared.size_bytes:
                part.unlink()
                raise StagingError(
                    422,
                    "checksum_mismatch",
                    "the file's bytes do not hash to its declaration; it was discarded",
                    received_bytes=0,
                )
            rename_no_replace(part, member)
            fsync_directory(member.parent)
            return ChunkResult(received_bytes=held, complete=True)

    # -- finalize ---------------------------------------------------------------------------

    def finalize(self, session_id: str, *, profile: str | None = None) -> IntakeSessionStatus:
        """Check every file again and hand the package to the publisher.

        Idempotent: a finalized session returns its status. Raises ``incomplete`` (409)
        while a file is missing, and ``checksum_mismatch`` (422) when a stored file no
        longer hashes to its declaration (that file is discarded, so it can be sent again).
        ``profile`` is a corpus client's (D-056): its package must then be a v2 take of that
        capture profile, read from the checked manifest, or finalize raises
        ``take_profile_not_allowed`` (422), so a client's token never adds to another
        profile's corpus. The publisher still validates the whole package.
        """
        with self._session_lock(session_id):
            request = self._read_request(session_id)
            directory = self._session_directory(session_id)
            if (directory / FINALIZED_NAME).exists():
                return self._status(session_id, request)
            missing = [
                item.path for item in self._progress(session_id, request) if not item.complete
            ]
            if missing:
                raise StagingError(409, "incomplete", f"{len(missing)} files are not complete")
            for declared in request.files:
                member = self._member(session_id, declared.path)
                digest, size = _hash_file(member)
                if digest != declared.sha256 or size != declared.size_bytes:
                    member.unlink()
                    raise StagingError(
                        422,
                        "checksum_mismatch",
                        f"{declared.path} changed in staging; it was discarded",
                    )
            package = directory / PACKAGE_NAME
            self._require_exact_package(package, request)
            if profile is not None:
                _require_profile_take(package, profile)
            target = self.incoming / session_id
            try:
                rename_no_replace(package, target)
            except FileExistsError as error:
                raise StagingError(
                    409, "session_conflict", "a package with this session id awaits intake"
                ) from error
            fsync_directory(directory)
            fsync_directory(self.incoming)
            write_new_private_file(directory / FINALIZED_NAME, b"finalized\n", mode=0o600)
            return self._status(session_id, request)

    def _require_exact_package(self, package: Path, request: IntakeSessionRequest) -> None:
        """Nothing but the declared files (and their directories) is in the package."""
        expected = {item.path for item in request.files}
        found: set[str] = set()
        for root, directories, files in os.walk(package, followlinks=False):
            for name in directories:
                child = Path(root) / name
                if child.is_symlink():
                    raise SecurityError(f"upload staging holds a link: {child}")
            for name in files:
                child = Path(root) / name
                if _regular_size(child) is None:
                    raise SecurityError(f"upload staging holds an unexpected entry: {child}")
                found.add(child.relative_to(package).as_posix())
        if found != expected:
            raise SecurityError("upload staging holds files that were not declared")

    # -- receipt ----------------------------------------------------------------------------

    def receipt(self, session_id: str) -> IntakeReceipt:
        """The accepted session's intake receipt; ``not_accepted`` (409) before that."""
        status = self.status(session_id)
        if status.receipt is None:
            raise StagingError(409, "not_accepted", f"the session is {status.state}")
        return status.receipt

"""The vault host's HTTP face over one local blob and ref store per namespace.

@canonical storage_server -- the vault server is the only process that serves a namespace's
LocalBlobStore and LocalRefStore over HTTP: every request is answered in the fixed order
(exactly one Host, path grammar, one Bearer token compared as hashes in constant time, the
namespace's optional user logins, the method, the scope, the framing, then the store), every
answer is one JSON refusal or one exact payload whose message names no server path, token or
content, every answered request appends one VaultAccessRecord to the access log, and nothing
outside the data root and that log is ever written.
Related: storage_local.py (the stores it serves), storage_ports.py (the STORAGE.* codes),
storage_contracts.py (VaultServerConfig, VaultAccessRecord, BlobRef), storage_http_client.py
(the Mac's client), deploy/vault/ (the image and runbook), tests/test_storage_server.py.

Entry point (there is no console script: a second one breaks real capture-kit installs)::

    python -I -B -m cole_voice_lab.storage_server serve \\
        --config <file> --data-root <dir> --access-log <file> [--provision]
        [--listen-internal] [--port 8080]
    python -I -B -m cole_voice_lab.storage_server hash-token   # one token on stdin
    python -I -B -m cole_voice_lab.storage_server new-token

``serve`` binds ``127.0.0.1`` unless ``--listen-internal`` is given, which binds every
interface so the Tailscale sidecar on the container's internal network can reach it; the
tailnet, not this server, terminates TLS. Data layout, all owned by this process's user and
0700: ``<data-root>/namespaces/<namespace>/`` is the root of both stores of one namespace. The
data root is an absolute, symlink-free path and is not an ``AssetLayout`` (the container runs
with ``HOME=/nonexistent``). Without ``--provision`` every directory must already exist with
that ownership and mode; ``--provision`` makes what is missing and is the operator's one-time
setup step.

The API, one request per connection, every answer ``Connection: close``:

* ``GET /healthz`` (no credential, nothing else) answers 200 ``ok``;
* ``PUT /v1/<ns>/blobs/<sha256>`` (write) stores the body, which must hash to the digest in
  the path, and answers the ``BlobRef`` with 201 when it is new and 200 when it was stored;
* ``HEAD /v1/<ns>/blobs/<sha256>?size=<n>`` (read) answers 200 or 404;
* ``GET /v1/<ns>/blobs/<sha256>?size=<n>`` (read) answers the bytes the store verified;
* ``GET /v1/<ns>/refs/<name>`` (read) answers the ``RefValue`` or 404;
* ``POST /v1/<ns>/refs/<name>`` (write) with ``{"expected_version": int|null, "target":
  BlobRef}`` answers the new ``RefValue`` or 409.

Every other answer is ``{"code": "STORAGE.…", "message": "…"}`` with the status
``_REFUSAL`` cases and ``_STORE_STATUS`` fix: 400 for a request the protocol refuses and for
an upload whose bytes miss their digest, 401 for every credential failure (identical for a
missing, repeated or malformed token, an unknown namespace, a token of another namespace, a
scope that does not cover the method and a login the namespace does not allow, so a refusal
reveals nothing about which namespaces exist), 404, 405 with ``Allow``, 409 for a swap that
lost the compare-and-set, 411 and 413 for a body without or beyond its limit, 500 for a
stored-bytes or internal protocol failure, 503 when every worker is busy or the request timed
out, and 507 for a full quota. A refusal whose request declared a body within the limit drains
it first, so the tailnet proxy delivers the refusal instead of a connection reset; a body
beyond the limit is refused with 413 and the connection closes unread. The request line, the
headers, a body and a drained refusal share one deadline, taken when a worker picks the
connection up, so a client that dribbles its bytes holds its worker for the configured
request timeout and no longer.

This module imports only the standard library, pydantic and the package's ``canonical``,
``storage_contracts``, ``storage_ports`` and ``storage_local`` modules, so importing it loads
no recording, capture, catalog, consent, fine-tune, dataset, worker, sealing, binding or
client module. ``_ascii_digest_equal``, the Content-Length-as-a-string limit check, the
silenced ``log_message``, the ``send_error`` override that sends every refusal through the
dispatcher and the security headers are copied from ``capture_console.py`` (the lab's other
HTTP server), which cannot be imported here because it pulls in the capture stack.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import signal
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import Any, Final, Literal, cast

from pydantic import ValidationError

from .canonical import canonical_json_bytes
from .storage_contracts import (
    MAX_BLOB_BYTES,
    MAX_FORWARDED_FOR_LENGTH,
    MAX_USER_LOGIN_LENGTH,
    NAMESPACE_PATTERN,
    VAULT_TOKEN_PATTERN,
    VAULT_TOKEN_PREFIX,
    BlobRef,
    StorageNameError,
    VaultAccessRecord,
    VaultNamespaceConfig,
    VaultOperation,
    VaultScope,
    VaultServerConfig,
    validate_ref_name,
)
from .storage_local import BLOBS_DIRECTORY, LocalBlobStore, LocalRefStore
from .storage_ports import StorageError, StorageErrorCode, storage_error

__all__ = [
    "DEFAULT_PORT",
    "INTERNAL_HOST",
    "LOOPBACK_HOST",
    "MAX_CONFIG_BYTES",
    "MAX_JSON_BODY_BYTES",
    "MAX_TOKEN_INPUT_BYTES",
    "NAMESPACES_DIRECTORY",
    "VaultServer",
    "hash_token",
    "load_server_configuration",
    "main",
    "new_token",
    "serve_vault",
]

NAMESPACES_DIRECTORY: Final = "namespaces"
LOOPBACK_HOST: Final = "127.0.0.1"
# Every interface: the container's internal network is the only one the vault is reached on.
INTERNAL_HOST: Final = "0.0.0.0"
DEFAULT_PORT: Final = 8080
MAX_PORT: Final = 65535
MAX_JSON_BODY_BYTES: Final = 64 * 1024
MAX_CONFIG_BYTES: Final = 64 * 1024
MAX_TOKEN_INPUT_BYTES: Final = 1024
_READ_CHUNK_BYTES: Final = 1024 * 1024
_HEALTH_PATH: Final = "/healthz"
_HEALTH_PAYLOAD: Final = b"ok"
_BEARER: Final = "Bearer "
_BLOBS_SEGMENT: Final = "blobs"
_REFS_SEGMENT: Final = "refs"
_JSON_CONTENT_TYPE: Final = "application/json; charset=utf-8"
_TEXT_CONTENT_TYPE: Final = "text/plain; charset=utf-8"
_BLOB_CONTENT_TYPE: Final = "application/octet-stream"
_SATURATED_WRITE_TIMEOUT_SECONDS: Final = 1.0
_SHUTDOWN_POLL_SECONDS: Final = 0.1

_DIGEST = re.compile(r"[0-9a-f]{64}")
_NAMESPACE = re.compile(NAMESPACE_PATTERN)
_TOKEN = re.compile(VAULT_TOKEN_PATTERN)
_SIZE_QUERY = re.compile(r"size=([0-9]{1,32})")

# Copied from capture_console.py:33-40, without the console's HTML policy: the vault serves
# JSON and opaque bytes, so every source is denied.
_SECURITY_HEADERS: Final = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
)

# One message for every credential failure, so a refusal tells a caller nothing about which
# namespaces exist or whether its token is known to another one.
_UNAUTHORIZED_MESSAGE: Final = "one Authorization: Bearer token with this scope is required"
_NOT_FOUND_MESSAGE: Final = "this vault serves /healthz and /v1/<namespace>/{blobs,refs}/…"
# What a client is told about a failure a store reported. The store's own message names paths
# inside the vault host, so it is written to stderr for the operator and never sent.
_STORE_MESSAGES: Final[Mapping[StorageErrorCode, str]] = {
    "STORAGE.UNAVAILABLE": "the vault cannot serve this request now",
    "STORAGE.TIMEOUT": "the vault did not finish this request in time",
    "STORAGE.UNAUTHORIZED": "the vault refused this request",
    "STORAGE.NOT_FOUND": "this namespace holds no such blob",
    "STORAGE.CONFLICT": "the ref is not at the expected version",
    "STORAGE.TOO_LARGE": f"a blob is at most {MAX_BLOB_BYTES} bytes",
    "STORAGE.QUOTA": "this namespace has no room left",
    "STORAGE.INTEGRITY": "the vault's stored bytes failed their check",
    "STORAGE.IDENTITY_MISMATCH": "the vault refused this request",
    "STORAGE.NOT_APPROVED": "the vault refused this request",
    "STORAGE.NOT_BOUND": "the vault refused this request",
    "STORAGE.PROTOCOL": "the vault could not serve this request",
}
# The status of every code a store can raise. A digest, size or privacy failure of stored
# bytes is the server's own (500), unlike an upload whose bytes miss their digest (400).
_STORE_STATUS: Final[Mapping[StorageErrorCode, int]] = {
    "STORAGE.UNAVAILABLE": 503,
    "STORAGE.TIMEOUT": 503,
    "STORAGE.UNAUTHORIZED": 500,
    "STORAGE.NOT_FOUND": 404,
    "STORAGE.CONFLICT": 409,
    "STORAGE.TOO_LARGE": 413,
    "STORAGE.QUOTA": 507,
    "STORAGE.INTEGRITY": 500,
    "STORAGE.IDENTITY_MISMATCH": 500,
    "STORAGE.NOT_APPROVED": 500,
    "STORAGE.NOT_BOUND": 500,
    "STORAGE.PROTOCOL": 500,
}
# What each endpoint is called in the access log, by path kind and method.
_OPERATIONS: Final[Mapping[tuple[str, str], VaultOperation]] = {
    ("health", "GET"): "health",
    ("blob", "PUT"): "put-blob",
    ("blob", "HEAD"): "has-blob",
    ("blob", "GET"): "get-blob",
    ("ref", "GET"): "read-ref",
    ("ref", "POST"): "swap-ref",
}
_ALLOWED_METHODS: Final[Mapping[str, str]] = {
    "health": "GET",
    "blob": "GET, HEAD, PUT",
    "ref": "GET, POST",
}
_WRITE_OPERATIONS: Final = frozenset({"put-blob", "swap-ref"})
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LOG_FLAGS: Final = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
# Non-blocking, so a FIFO planted at the configuration's name cannot block the open.
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_SATURATED_BODY: Final = (
    b'{"code":"STORAGE.UNAVAILABLE","message":"every vault worker is busy; try again"}\n'
)
# A fixed, tiny answer for a connection the accept loop can hand to no worker.
_SATURATED_RESPONSE: Final = b"".join(
    (
        b"HTTP/1.1 503 Service Unavailable\r\n",
        f"Content-Type: {_JSON_CONTENT_TYPE}\r\n".encode("ascii"),
        f"Content-Length: {len(_SATURATED_BODY)}\r\n".encode("ascii"),
        b"Connection: close\r\n",
        *(f"{name}: {value}\r\n".encode("ascii") for name, value in _SECURITY_HEADERS),
        b"\r\n",
        _SATURATED_BODY,
    )
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _emit_line(line: str) -> None:
    """Print one line and flush it, so a reader of a pipe sees it at once."""
    print(line, flush=True)


# Copied from capture_console.py:59-64: an ASCII-only constant-time compare, so a non-ASCII
# value is unequal instead of raising.
def _ascii_digest_equal(value: str, expected: str) -> bool:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(encoded, expected.encode("ascii"))


def _over_limit(digits: str, limit: int) -> bool:
    """Whether the decimal ``digits`` name more than ``limit``, compared as strings.

    Copied from ``capture_console._read_json_body`` (capture_console.py:288-295): the
    comparison happens before ``int()``, so a header of a thousand digits never becomes a
    thousand-digit integer.
    """
    normalized = digits.lstrip("0") or "0"
    maximum = str(limit)
    return len(normalized) > len(maximum) or (
        len(normalized) == len(maximum) and normalized > maximum
    )


def _decimal_value(digits: str) -> int:
    return int(digits.lstrip("0") or "0")


def _one_decimal_length(values: list[str]) -> str | None:
    """The one ASCII-decimal value of a repeated header; ``None`` when there is not one."""
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        return None
    return values[0]


def new_token() -> str:
    """One fresh vault access token, printed only to the operator's own terminal.

    Returns: ``cvlv_`` followed by the 43 base64url characters of 256 random bits. The server
    stores only ``hash_token`` of it; the Mac keeps the token in its login Keychain.
    Raises: nothing.
    """
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return f"{VAULT_TOKEN_PREFIX}{body}"


def hash_token(text: str) -> str:
    """The SHA-256 hex of the one token ``text`` holds; the token itself is never echoed.

    Params: ``text``, exactly one token as it arrived on standard input, with at most one
    trailing newline.
    Returns: 64 lowercase hex characters, which is what a ``VaultServerConfig`` stores.
    Raises: ``StorageError`` (``STORAGE.PROTOCOL``) for empty input (a closed or empty
    standard input included) or for anything that is not one token in ``new_token``'s format;
    no message repeats the input.
    """
    token = text[:-1] if text.endswith("\n") else text
    token = token[:-1] if token.endswith("\r") else token
    if not token:
        raise storage_error(
            "STORAGE.PROTOCOL", "hash-token reads one token from standard input; nothing arrived"
        )
    if _TOKEN.fullmatch(token) is None:
        raise storage_error(
            "STORAGE.PROTOCOL",
            f"a vault token is '{VAULT_TOKEN_PREFIX}' and 43 base64url characters on one line, "
            "as new-token prints it",
        )
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _token_digest(token: str) -> str | None:
    """The SHA-256 hex of a presented token; ``None`` when it cannot be a token at all."""
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(encoded).hexdigest()


def _checked_path(path: Path, label: str) -> Path:
    """``path`` as an absolute, normalized, symlink-free path.

    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) for anything else.
    """
    text = os.fspath(path)
    if not os.path.isabs(text) or os.path.normpath(text) != text:
        raise storage_error("STORAGE.INTEGRITY", f"{label} is an absolute, normalized path: {text}")
    resolved = os.path.realpath(text)
    if resolved != text:
        raise storage_error(
            "STORAGE.INTEGRITY",
            f"{label} has no symlink in its path (it resolves to {resolved}): {text}",
        )
    return Path(text)


def _private_problem(metadata: os.stat_result, *, directory: bool) -> str | None:
    """Why ``metadata`` is not an owned private entry of its kind; ``None`` when it is."""
    if not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)):
        return "is not a " + ("directory" if directory else "regular file")
    if metadata.st_uid != os.getuid():
        return f"is owned by uid {metadata.st_uid}, not {os.getuid()}"
    if metadata.st_mode & 0o077:
        return f"has group or other permissions (mode {stat.S_IMODE(metadata.st_mode):#o})"
    if not directory and metadata.st_nlink != 1:
        return f"has {metadata.st_nlink} hard links"
    return None


def _require_private(metadata: os.stat_result, label: Path, *, directory: bool) -> None:
    """Fail unless ``metadata`` is an owned private entry of its kind.

    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) naming the problem and the path.
    """
    problem = _private_problem(metadata, directory=directory)
    if problem is not None:
        raise storage_error(
            "STORAGE.INTEGRITY", f"the vault server refuses a path that {problem}: {label}"
        )


def _private_directory(path: Path, *, create: bool) -> None:
    """Check (or with ``create`` make) one owned private 0700 directory.

    Raises: ``StorageError`` (``STORAGE.UNAVAILABLE``) when it is missing and ``create`` is not
    set, or on any other filesystem failure; ``StorageIntegrityError``
    (``STORAGE.INTEGRITY``) when it is a symlink, not a directory, not owned by this user or
    not private.
    """
    made = False
    if create:
        try:
            os.mkdir(path, 0o700)
            made = True
        except FileExistsError:
            made = False
        except OSError as error:
            raise storage_error(
                "STORAGE.UNAVAILABLE", f"the vault server cannot make {path}: {error}"
            ) from error
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except FileNotFoundError as error:
        raise storage_error(
            "STORAGE.UNAVAILABLE",
            f"the vault server needs an existing directory at {path} (--provision makes one)",
        ) from error
    except OSError as error:
        raise storage_error(
            "STORAGE.INTEGRITY",
            f"the vault server refuses a symlink or non-directory at {path}: {error}",
        ) from error
    try:
        if made:
            os.fchmod(descriptor, 0o700)
        _require_private(os.fstat(descriptor), path, directory=True)
    finally:
        os.close(descriptor)


def _open_access_log(path: Path) -> int:
    """Open the append-only access log, making it 0600 when it does not exist.

    Returns: a write descriptor opened ``O_APPEND|O_CREAT|O_NOFOLLOW``, so every record is
    appended whole and no line already written is ever rewritten.
    Raises: ``StorageIntegrityError`` when the path is a symlink or not an owned private
    regular file with one link; ``StorageError`` (``STORAGE.UNAVAILABLE``) on any other
    filesystem failure.
    """
    checked = _checked_path(path, "the vault access log")
    try:
        descriptor = os.open(checked, _LOG_FLAGS, 0o600)
    except OSError as error:
        code: StorageErrorCode = (
            "STORAGE.INTEGRITY" if os.path.lexists(checked) else "STORAGE.UNAVAILABLE"
        )
        raise storage_error(
            code, f"the vault server cannot append to its access log {checked}: {error}"
        ) from error
    try:
        _require_private(os.fstat(descriptor), checked, directory=False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_small_file(path: Path, limit: int, label: str) -> bytes:
    """Read at most ``limit`` bytes of one regular file, following no symlink.

    Raises: ``StorageError`` (``STORAGE.UNAVAILABLE``) when it cannot be opened or read;
    ``StorageIntegrityError`` when it is not a regular file or holds more than ``limit``
    bytes.
    """
    checked = _checked_path(path, label)
    try:
        descriptor = os.open(checked, _FILE_FLAGS)
    except OSError as error:
        raise storage_error(
            "STORAGE.UNAVAILABLE", f"{label} cannot be read at {checked}: {error}"
        ) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise storage_error("STORAGE.INTEGRITY", f"{label} is not a regular file: {checked}")
        os.set_blocking(descriptor, True)
        payload = os.read(descriptor, limit + 1)
    except OSError as error:
        raise storage_error(
            "STORAGE.UNAVAILABLE", f"{label} cannot be read at {checked}: {error}"
        ) from error
    finally:
        os.close(descriptor)
    if len(payload) > limit:
        raise storage_error("STORAGE.INTEGRITY", f"{label} is larger than {limit} bytes: {checked}")
    return payload


def load_server_configuration(path: Path) -> VaultServerConfig:
    """Read and validate the server's namespace configuration.

    Params: ``path``, a JSON file of at most ``MAX_CONFIG_BYTES`` (a read-only mount or a
    Docker secret; it holds token hashes only, so its mode is the operator's choice).
    Returns: the validated ``VaultServerConfig``.
    Raises: ``StorageError`` (``STORAGE.PROTOCOL``) when it is not a valid configuration, with
    a message naming each field and rule but no value; ``StorageError``
    (``STORAGE.UNAVAILABLE``) or ``StorageIntegrityError`` as ``_read_small_file``.
    """
    payload = _read_small_file(path, MAX_CONFIG_BYTES, "the vault server configuration")
    try:
        return VaultServerConfig.model_validate_json(payload)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '(root)'}: {item['msg']}"
            for item in error.errors()
        )
        raise storage_error(
            "STORAGE.PROTOCOL", f"the vault server configuration is not valid: {problems}"
        ) from error


def _stored_blob_bytes(root: Path) -> int:
    """How many bytes a namespace's blobs occupy, walked once at startup.

    Raises: ``StorageIntegrityError`` when ``blobs/`` holds an entry no store writes;
    ``StorageError`` (``STORAGE.UNAVAILABLE``) on a filesystem failure.
    """
    blobs = root / BLOBS_DIRECTORY
    total = 0
    try:
        with os.scandir(blobs) as shards:
            for shard in shards:
                if not shard.is_dir(follow_symlinks=False):
                    raise storage_error(
                        "STORAGE.INTEGRITY",
                        f"the vault's blob directory holds an entry no store writes: {shard.path}",
                    )
                with os.scandir(shard.path) as entries:
                    for entry in entries:
                        metadata = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(metadata.st_mode):
                            raise storage_error(
                                "STORAGE.INTEGRITY",
                                "the vault's blob directory holds an entry no store writes: "
                                f"{entry.path}",
                            )
                        total += metadata.st_size
    except OSError as error:
        raise storage_error(
            "STORAGE.UNAVAILABLE", f"the vault cannot measure {blobs}: {error}"
        ) from error
    return total


class _Namespace:
    """One served namespace: its two stores, its token hashes and its quota accounting.

    ``stored_bytes`` starts as the bytes this namespace's blobs already occupy and grows by
    every accepted new blob. Two writers that put the same new bytes at the same moment each
    account for them, so the count can over-report until the next start but never
    under-reports, and the quota therefore never overruns.
    """

    def __init__(self, configuration: VaultNamespaceConfig, root: Path, *, create: bool) -> None:
        self.name = configuration.name
        self.quota_bytes = configuration.quota_bytes
        self.allowed_user_logins = configuration.allowed_user_logins
        self.root = root
        self._read_token_sha256 = configuration.read_token_sha256
        self._write_token_sha256 = configuration.write_token_sha256
        self.blobs = LocalBlobStore(root, create=create)
        self.refs = LocalRefStore(root, create=create)
        self._lock = threading.Lock()
        self._stored_bytes = _stored_blob_bytes(root)

    @property
    def stored_bytes(self) -> int:
        """The bytes this namespace's blobs occupy, as the server has accounted for them."""
        with self._lock:
            return self._stored_bytes

    def scope_for(self, token_sha256: str) -> VaultScope:
        """The scope a presented token's hash grants here, compared in constant time."""
        write = _ascii_digest_equal(token_sha256, self._write_token_sha256)
        read = _ascii_digest_equal(token_sha256, self._read_token_sha256)
        if write:
            return "write"
        return "read" if read else "none"

    def admit(self, size: int) -> None:
        """Account ``size`` new bytes against the quota before they are stored.

        Raises: ``StorageError`` (``STORAGE.QUOTA``) when they would not fit.
        """
        with self._lock:
            if self._stored_bytes + size > self.quota_bytes:
                raise storage_error(
                    "STORAGE.QUOTA",
                    f"namespace {self.name} has {self.quota_bytes - self._stored_bytes} of "
                    f"{self.quota_bytes} bytes left, and this blob needs {size}",
                )
            self._stored_bytes += size

    def release(self, size: int) -> None:
        """Give back bytes ``admit`` accounted for that were then never stored."""
        with self._lock:
            self._stored_bytes -= size


def _open_namespaces(
    data_root: Path, config: VaultServerConfig, *, create: bool
) -> dict[str, _Namespace]:
    """Open one pair of stores per configured namespace, once, at startup."""
    root = _checked_path(data_root, "the vault data root")
    _private_directory(root, create=create)
    namespaces_root = root / NAMESPACES_DIRECTORY
    _private_directory(namespaces_root, create=create)
    return {
        configuration.name: _Namespace(
            configuration, namespaces_root / configuration.name, create=create
        )
        for configuration in config.namespaces
    }


@dataclass(frozen=True, slots=True)
class _Route:
    """What a request target named: the endpoint kind, its namespace and its name."""

    kind: Literal["health", "blob", "ref"]
    namespace: str | None = None
    ref: str | None = None
    size: int | None = None


class _Refused(Exception):
    """One refusal: the status, the ``STORAGE.*`` code, the client's message and its headers.

    ``drain`` is ``False`` for a refusal after which an unread body must not be read: a body
    beyond its limit, and any framing failure that already left the stream unusable.
    """

    def __init__(
        self,
        status: int,
        code: StorageErrorCode,
        message: str,
        *,
        drain: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code: StorageErrorCode = code
        self.drain = drain
        self.headers = headers


def _refuse_protocol(message: str, *, drain: bool = True) -> _Refused:
    return _Refused(int(HTTPStatus.BAD_REQUEST), "STORAGE.PROTOCOL", message, drain=drain)


def _refuse_too_large(limit: int) -> _Refused:
    return _Refused(
        int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
        "STORAGE.TOO_LARGE",
        f"a request body is at most {limit} bytes",
        drain=False,
    )


def _unauthorized() -> _Refused:
    return _Refused(int(HTTPStatus.UNAUTHORIZED), "STORAGE.UNAUTHORIZED", _UNAUTHORIZED_MESSAGE)


def _not_found(message: str) -> _Refused:
    return _Refused(int(HTTPStatus.NOT_FOUND), "STORAGE.NOT_FOUND", message)


@contextmanager
def _store_errors() -> Iterator[None]:
    """Turn a ``StorageError`` a store raised into a ``_Refused`` with a safe message.

    The store's own message names paths inside the vault host, so a failure the server is
    answerable for (a 500) is written to stderr instead, where only the operator sees it.
    """
    try:
        yield
    except StorageError as error:
        code: StorageErrorCode = error.code
        status = _STORE_STATUS[code]
        if status >= 500:
            print(
                json.dumps(
                    {"service": "vault", "error_code": code, "message": str(error)}, sort_keys=True
                ),
                file=sys.stderr,
                flush=True,
            )
        raise _Refused(status, code, _STORE_MESSAGES[code], drain=False) from error


def _parse_size(query: str) -> int:
    """The ``size=<decimal>`` query a blob path may carry.

    Raises: ``_Refused`` (400) for any other query, or 413 for a size beyond a blob's limit.
    """
    matched = _SIZE_QUERY.fullmatch(query)
    if matched is None:
        raise _refuse_protocol("a blob path takes exactly one query: size=<decimal bytes>")
    digits = matched.group(1)
    if _over_limit(digits, MAX_BLOB_BYTES):
        raise _Refused(
            int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
            "STORAGE.TOO_LARGE",
            f"a blob is at most {MAX_BLOB_BYTES} bytes",
        )
    return _decimal_value(digits)


def _parse_target(target: str) -> _Route:
    """The route a request target names.

    Raises: ``_Refused`` with 404 for a target that is no endpoint of this API, or 400 for one
    whose namespace, digest, ref name or query is not well formed.
    """
    raw, separator, query = target.partition("?")
    if raw == _HEALTH_PATH:
        if separator:
            raise _refuse_protocol("the health endpoint takes no query")
        return _Route(kind="health")
    segments = raw.split("/")
    if (
        len(segments) < 5
        or segments[0] != ""
        or segments[1] != "v1"
        or segments[3] not in (_BLOBS_SEGMENT, _REFS_SEGMENT)
    ):
        raise _not_found(_NOT_FOUND_MESSAGE)
    namespace = segments[2]
    if _NAMESPACE.fullmatch(namespace) is None:
        raise _refuse_protocol(
            "a namespace is 1 to 63 characters of [a-z0-9-] starting with a letter"
        )
    if segments[3] == _BLOBS_SEGMENT:
        if len(segments) != 5 or _DIGEST.fullmatch(segments[4]) is None:
            raise _refuse_protocol("a blob path names one lowercase SHA-256 digest")
        return _Route(
            kind="blob",
            namespace=namespace,
            ref=segments[4],
            size=_parse_size(query) if separator else None,
        )
    name = "/".join(segments[4:])
    try:
        validate_ref_name(name)
    except StorageNameError as error:
        raise _refuse_protocol(
            "a ref path names 1 to 8 segments of [a-z0-9._-], each starting with a letter or digit"
        ) from error
    if separator:
        raise _refuse_protocol("a ref path takes no query")
    return _Route(kind="ref", namespace=namespace, ref=name)


def _loggable(value: str | None, limit: int) -> str | None:
    """A proxy-set header value as it can be logged; ``None`` when it cannot be.

    A value that is absent, empty, longer than ``limit`` or not printable one-line text is
    dropped rather than allowed to break the access log's contract.
    """
    if value is None or not 0 < len(value) <= limit or not value.isprintable():
        return None
    return value


@dataclass(slots=True)
class _AccessEntry:
    """The access-log line one request is building; ``record`` validates it as a contract.

    A request that reaches no answer at all still has the status and code it starts with, so
    an unanswered request is recorded as this server's own failure.
    """

    time: datetime
    namespace: str | None = None
    scope: VaultScope = "none"
    operation: VaultOperation = "refused"
    ref: str | None = None
    bytes: int = 0
    status: int = int(HTTPStatus.INTERNAL_SERVER_ERROR)
    code: str | None = "STORAGE.PROTOCOL"
    forwarded_for: str | None = None
    user_login: str | None = None

    def answered(self, status: int, code: StorageErrorCode | None = None) -> None:
        self.status = status
        self.code = code

    def record(self) -> VaultAccessRecord:
        return VaultAccessRecord(
            time=self.time,
            namespace=self.namespace,
            scope=self.scope,
            operation=self.operation,
            ref=self.ref,
            bytes=self.bytes,
            status=self.status,
            code=self.code,
            forwarded_for=self.forwarded_for,
            user_login=self.user_login,
        )


class VaultServer(ThreadingHTTPServer):
    """One vault host: bounded workers over one pair of stores per configured namespace.

    Every connection is handed to a ``ThreadPoolExecutor`` of ``max_workers`` threads through
    a non-blocking semaphore; a connection that finds every worker busy is answered with a
    fixed 503 from the accept loop and closed, so the server never queues work without bound.
    Each answered request appends one ``VaultAccessRecord`` to the access log.
    """

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        config: VaultServerConfig,
        data_root: Path,
        access_log: Path,
        provision: bool = False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Open the stores and the access log, then bind ``address``.

        Params: ``address``, the host and port to bind (port 0 binds an unused one);
        ``config``; ``data_root``, an absolute symlink-free 0700 directory holding
        ``namespaces/<name>/`` for each configured namespace; ``access_log``, the file every
        answered request is appended to; ``provision``, whether missing directories are made;
        ``clock``, the timezone-aware time each record carries.
        Raises: ``StorageError`` or ``StorageIntegrityError`` when the data root, a namespace
        root or the access log is missing, unsafe or unreadable; ``OSError`` when the address
        cannot be bound.
        """
        self.config = config
        self.clock = clock
        self._log_lock = threading.Lock()
        self._log_closed = True
        self.namespaces = _open_namespaces(data_root, config, create=provision)
        self._access_log = _open_access_log(access_log)
        self._log_closed = False
        self._slots = threading.BoundedSemaphore(config.max_workers)
        self._workers = ThreadPoolExecutor(
            max_workers=config.max_workers, thread_name_prefix="vault"
        )
        try:
            super().__init__(address, _VaultHandler)
        except BaseException:
            self.server_close()
            raise

    @property
    def address(self) -> str:
        """The bound ``host:port``."""
        host, port = cast(tuple[str, int], self.server_address[:2])
        return f"{host}:{port}"

    def process_request(self, request: Any, client_address: Any) -> None:
        """Hand one connection to a worker, or refuse it when every worker is busy."""
        if not self._slots.acquire(blocking=False):
            self._refuse_saturated(request)
            return
        try:
            self._workers.submit(self._work, request, client_address)
        except RuntimeError:  # the executor is already shutting down
            self._slots.release()
            self.shutdown_request(request)

    def _work(self, request: Any, client_address: Any) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _refuse_saturated(self, request: Any) -> None:
        entry = _AccessEntry(time=self.clock())
        entry.answered(int(HTTPStatus.SERVICE_UNAVAILABLE), "STORAGE.UNAVAILABLE")
        self.record(entry)
        try:
            request.settimeout(_SATURATED_WRITE_TIMEOUT_SECONDS)
            request.sendall(_SATURATED_RESPONSE)
        except OSError:
            pass  # the caller hung up or stalled; the refusal is already recorded
        finally:
            self.shutdown_request(request)

    def record(self, entry: _AccessEntry) -> None:
        """Append one access-log line, written whole in one call.

        Raises: ``pydantic.ValidationError`` when the entry is not a valid
        ``VaultAccessRecord``; ``OSError`` when the log cannot be written.
        """
        line = canonical_json_bytes(entry.record()) + b"\n"
        with self._log_lock:
            os.write(self._access_log, line)

    def server_close(self) -> None:
        """Stop accepting, wait for the workers and close the access log."""
        try:
            super().server_close()
        finally:
            self._workers.shutdown(wait=True)
            if not self._log_closed:
                self._log_closed = True
                os.close(self._access_log)


class _DeadlineReader(io.RawIOBase):
    """One request's reads, bounded by a whole-request deadline, not by a per-recv idle wait.

    A socket timeout bounds one ``recv``, so a client that sends a byte just before each one
    expires would hold its worker for as long as it liked, through the request line, the
    headers, a body or a drained refusal alike. Every read here gets instead what is left of
    one deadline, taken when the worker picked the connection up, so a request costs at most
    ``request_timeout_seconds`` of a worker's time however slowly its bytes arrive.
    """

    def __init__(self, connection: socket.socket, seconds: float) -> None:
        self._connection = connection
        self._seconds = seconds
        self._deadline = time.monotonic() + seconds

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        """Fill part of ``buffer`` from the connection, within what is left of the deadline.

        Returns: the bytes read, and 0 at the end of the stream.
        Raises: ``TimeoutError`` once the deadline has passed, after giving the socket the
        answer's own budget back so the refusal can still be written; ``OSError`` from the
        connection itself.
        """
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self._connection.settimeout(self._seconds)
            raise TimeoutError("the request did not arrive within its deadline")
        self._connection.settimeout(remaining)
        return self._connection.recv_into(buffer)


class _VaultHandler(BaseHTTPRequestHandler):
    """One request per connection; every method goes through ``_dispatch``."""

    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self._body_limit = MAX_JSON_BODY_BYTES
        self._body_taken = False
        self._answer_timeout = float(self.vault.config.request_timeout_seconds)
        self.connection.settimeout(self._answer_timeout)
        # Everything this handler reads goes through one deadline: the stock reader would
        # bound each recv only, which a dribbling client never trips.
        self.rfile.close()
        self.rfile = io.BufferedReader(_DeadlineReader(self.connection, self._answer_timeout))

    @property
    def vault(self) -> VaultServer:
        return cast(VaultServer, self.server)

    # Copied from capture_console.py:188-189: the default stderr access log repeats the
    # request line, and this server keeps its own JSON log instead.
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Answer every error ``http.server`` raises itself as this API's JSON refusal.

        Copied from capture_console.py:191-202: an unsupported method reaches ``_dispatch``,
        so it is authorized, logged and answered like any other request; every other error
        (an unparsable request line, a target that is too long, an unsupported version) is
        answered as a protocol refusal and recorded.
        """
        del explain
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._dispatch(self.command)
            return
        entry = _AccessEntry(time=self.vault.clock())
        entry.answered(code, "STORAGE.PROTOCOL")
        status = HTTPStatus(code)
        try:
            self._error(status, "STORAGE.PROTOCOL", message or status.phrase)
        finally:
            self.vault.record(entry)

    # -- answers ---------------------------------------------------------------------------

    def _respond(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str,
        length: int | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        """Send one complete answer and close the connection.

        Copied from capture_console.py:208-229, with ``Connection: close`` on every answer
        (one request per connection) and a ``length`` a ``HEAD`` answer declares without
        sending the body.
        """
        # Reading shrank the socket's timeout towards the request's deadline; writing the
        # answer gets its own budget, so a refusal the deadline caused still reaches the client.
        self.connection.settimeout(self._answer_timeout)
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self.close_connection = True
        self.send_response_only(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload) if length is None else length))
        self.send_header("Connection", "close")
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(
        self, status: HTTPStatus, value: object, *, extra_headers: Mapping[str, str] | None = None
    ) -> None:
        self._respond(
            status,
            canonical_json_bytes(value) + b"\n",
            content_type=_JSON_CONTENT_TYPE,
            extra_headers=extra_headers,
        )

    def _error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._json(status, {"code": code, "message": message}, extra_headers=extra_headers)

    # -- framing ---------------------------------------------------------------------------

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        return values[0] if len(values) == 1 else None

    def _require_single_host(self) -> None:
        """Require exactly one ``Host``; its value is never compared.

        The tailnet proxy passes the client's own ``Host`` through, so any value is accepted.
        """
        if len(self.headers.get_all("Host", failobj=[])) != 1:
            raise _refuse_protocol("exactly one Host header is required")

    def _reject_transfer_encoding(self) -> None:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            raise _refuse_protocol("transfer-encoded request bodies are forbidden", drain=False)

    def _declared_length(self, limit: int) -> int | None:
        """The one ASCII-decimal ``Content-Length``, checked against ``limit`` as a string.

        Raises: ``_Refused`` (400) for a repeated, empty or non-decimal header, or 413 for a
        length beyond ``limit`` (the body is never read).
        """
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if not lengths:
            return None
        digits = _one_decimal_length(lengths)
        if digits is None:
            raise _refuse_protocol("exactly one decimal Content-Length is required", drain=False)
        if _over_limit(digits, limit):
            raise _refuse_too_large(limit)
        return _decimal_value(digits)

    def _require_no_body(self) -> None:
        self._reject_transfer_encoding()
        if self._declared_length(self._body_limit):
            raise _refuse_protocol("this request takes no body")

    def _read_body(self, limit: int) -> bytes:
        """Exactly the bytes ``Content-Length`` declares, read under the request's deadline.

        Raises: ``_Refused`` (411) when no length was declared, 400 when the body ends before
        it, 413 when it is beyond ``limit``, 503 when it does not arrive in time.
        """
        self._reject_transfer_encoding()
        length = self._declared_length(limit)
        if length is None:
            raise _Refused(
                int(HTTPStatus.LENGTH_REQUIRED),
                "STORAGE.TOO_LARGE",
                "a request body needs exactly one Content-Length",
                drain=False,
            )
        self._body_taken = True
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining:
                chunk = self.rfile.read(min(remaining, _READ_CHUNK_BYTES))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except TimeoutError as error:
            raise _Refused(
                int(HTTPStatus.SERVICE_UNAVAILABLE),
                "STORAGE.TIMEOUT",
                "the request body did not arrive in time",
                drain=False,
            ) from error
        if remaining:
            raise _refuse_protocol("the request body ended before Content-Length", drain=False)
        return b"".join(chunks)

    def _drained(self, refusal: _Refused) -> _Refused:
        """Read and discard a refused request's body, so the refusal reaches the client.

        A body beyond the route's limit is not read: the refusal becomes 413 and the
        connection closes, which is what the client's error mapping expects. The drain is
        bounded by that limit and by the request's deadline, whose expiry ends it like any
        other read failure: the refusal is sent with the rest of the body unread.
        """
        if not refusal.drain or self._body_taken:
            return refusal
        digits = _one_decimal_length(self.headers.get_all("Content-Length", failobj=[]))
        if digits is None:
            return refusal
        if _over_limit(digits, self._body_limit):
            return _refuse_too_large(self._body_limit)
        remaining = _decimal_value(digits)
        self._body_taken = True
        with suppress(OSError):
            while remaining:
                chunk = self.rfile.read(min(remaining, _READ_CHUNK_BYTES))
                if not chunk:
                    break
                remaining -= len(chunk)
        return refusal

    # -- authorization ---------------------------------------------------------------------

    def _authorize(self, name: str) -> tuple[_Namespace, VaultScope]:
        """The namespace and scope the request's one Bearer token grants.

        Raises: ``_Refused`` (401) with one message for a missing, repeated or malformed
        ``Authorization`` header, an unknown namespace, a token of another namespace and a
        login the namespace does not allow.
        """
        header = self._single_header("Authorization")
        if header is None or not header.startswith(_BEARER):
            raise _unauthorized()
        digest = _token_digest(header[len(_BEARER) :])
        namespace = self.vault.namespaces.get(name)
        if namespace is None or digest is None:
            raise _unauthorized()
        scope = namespace.scope_for(digest)
        if scope == "none":
            raise _unauthorized()
        if namespace.allowed_user_logins:
            login = self._single_header("Tailscale-User-Login")
            if login is None or not any(
                _ascii_digest_equal(login, allowed) for allowed in namespace.allowed_user_logins
            ):
                raise _unauthorized()
        return namespace, scope

    def _require_method(self, kind: str, method: str) -> VaultOperation:
        """The operation ``method`` names on a route of ``kind``.

        Raises: ``_Refused`` (405) with an ``Allow`` header for any other method.
        """
        operation = _OPERATIONS.get((kind, method))
        if operation is None:
            raise _Refused(
                int(HTTPStatus.METHOD_NOT_ALLOWED),
                "STORAGE.PROTOCOL",
                f"this endpoint allows {_ALLOWED_METHODS[kind]}",
                headers={"Allow": _ALLOWED_METHODS[kind]},
            )
        return operation

    # -- endpoints -------------------------------------------------------------------------

    def _blob_ref(self, digest: str, size: int) -> BlobRef:
        try:
            return BlobRef(sha256=digest, size=size)
        except ValidationError as error:
            raise _refuse_protocol(
                "a blob of size 0 is the one with the empty SHA-256, and no other"
            ) from error

    def _put_blob(self, namespace: _Namespace, digest: str, entry: _AccessEntry) -> None:
        data = self._read_body(MAX_BLOB_BYTES)
        entry.bytes = len(data)
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
            raise _Refused(
                int(HTTPStatus.BAD_REQUEST),
                "STORAGE.INTEGRITY",
                "the uploaded bytes do not match the digest in the path",
            )
        ref = self._blob_ref(digest, len(data))
        with _store_errors():
            stored = namespace.blobs.has(ref)
            if not stored:
                namespace.admit(len(data))
            try:
                namespace.blobs.put(data)
            except BaseException:
                if not stored:
                    namespace.release(len(data))
                raise
        status = HTTPStatus.OK if stored else HTTPStatus.CREATED
        entry.answered(int(status))
        self._json(status, ref)

    def _has_blob(self, namespace: _Namespace, ref: BlobRef, entry: _AccessEntry) -> None:
        with _store_errors():
            stored = namespace.blobs.has(ref)
        if not stored:
            raise _not_found("this namespace holds no such blob")
        entry.answered(int(HTTPStatus.OK))
        self._respond(HTTPStatus.OK, b"", content_type=_BLOB_CONTENT_TYPE, length=ref.size)

    def _get_blob(self, namespace: _Namespace, ref: BlobRef, entry: _AccessEntry) -> None:
        with _store_errors():
            data = namespace.blobs.get(ref)
        entry.bytes = len(data)
        entry.answered(int(HTTPStatus.OK))
        self._respond(HTTPStatus.OK, data, content_type=_BLOB_CONTENT_TYPE)

    def _read_ref(self, namespace: _Namespace, name: str, entry: _AccessEntry) -> None:
        with _store_errors():
            value = namespace.refs.read(name)
        if value is None:
            raise _not_found("this namespace holds no such ref")
        entry.answered(int(HTTPStatus.OK))
        self._json(HTTPStatus.OK, value)

    def _swap_ref(self, namespace: _Namespace, name: str, entry: _AccessEntry) -> None:
        payload = self._read_body(MAX_JSON_BODY_BYTES)
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _refuse_protocol("a swap body is one UTF-8 JSON object") from error
        if not isinstance(body, dict) or set(body) != {"expected_version", "target"}:
            raise _refuse_protocol("a swap body names exactly expected_version and target")
        request = cast(dict[str, Any], body)
        expected = request["expected_version"]
        if expected is not None and type(expected) is not int:
            raise _refuse_protocol("expected_version is an integer or null")
        try:
            target = BlobRef.model_validate(request["target"])
        except ValidationError as error:
            raise _refuse_protocol("target is a blob ref: a SHA-256 digest and a size") from error
        with _store_errors():
            value = namespace.refs.swap(name, expected_version=expected, target=target)
        entry.answered(int(HTTPStatus.OK))
        self._json(HTTPStatus.OK, value)

    # -- dispatch --------------------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        entry = _AccessEntry(time=self.vault.clock())
        try:
            entry.forwarded_for = _loggable(
                self._single_header("X-Forwarded-For"), MAX_FORWARDED_FOR_LENGTH
            )
            entry.user_login = _loggable(
                self._single_header("Tailscale-User-Login"), MAX_USER_LOGIN_LENGTH
            )
            self._require_single_host()
            route = _parse_target(self.path)
            if route.kind == "blob":
                self._body_limit = MAX_BLOB_BYTES
            if route.kind == "health":
                entry.operation = self._require_method(route.kind, method)
                self._require_no_body()
                entry.answered(int(HTTPStatus.OK))
                self._respond(HTTPStatus.OK, _HEALTH_PAYLOAD, content_type=_TEXT_CONTENT_TYPE)
                return
            name = cast(str, route.ref)
            entry.namespace = cast(str, route.namespace)
            entry.ref = name
            namespace, scope = self._authorize(entry.namespace)
            entry.scope = scope
            operation = self._require_method(route.kind, method)
            entry.operation = operation
            if operation in _WRITE_OPERATIONS and scope != "write":
                raise _unauthorized()
            if operation == "put-blob":
                self._put_blob(namespace, name, entry)
                return
            if operation == "swap-ref":
                self._swap_ref(namespace, name, entry)
                return
            self._require_no_body()
            if operation == "read-ref":
                self._read_ref(namespace, name, entry)
                return
            if route.size is None:
                raise _refuse_protocol("reading a blob needs its size: ?size=<decimal bytes>")
            ref = self._blob_ref(name, route.size)
            if operation == "has-blob":
                self._has_blob(namespace, ref, entry)
            else:
                self._get_blob(namespace, ref, entry)
        except _Refused as refusal:
            answered = self._drained(refusal)
            entry.answered(answered.status, answered.code)
            self._error(
                HTTPStatus(answered.status),
                answered.code,
                str(answered),
                extra_headers=answered.headers,
            )
        finally:
            self.vault.record(entry)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def do_TRACE(self) -> None:
        self._dispatch("TRACE")

    def do_CONNECT(self) -> None:
        self._dispatch("CONNECT")


def serve_vault(server: VaultServer, *, emit: Callable[[str], None] = _emit_line) -> None:
    """Serve until SIGTERM, SIGINT or ``shutdown``, then close the server.

    Params: ``server``, already bound; ``emit``, where the one startup line naming the bound
    address is printed, so an operator or a test knows which port was taken.
    Returns: nothing.
    Raises: ``ValueError`` when it is not called from the main thread (signal handlers are
    installed there only); whatever ``serve_forever`` raises. The server is closed either way.
    """
    previous: dict[int, Any] = {}

    def stop(signal_number: int, frame: FrameType | None) -> None:
        del signal_number, frame
        # shutdown() waits for the serve_forever loop, which is this thread, so it cannot be
        # called here (capture_console.py:368 starts the same one-shot thread).
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            previous[int(signal_number)] = signal.signal(signal_number, stop)
        emit(json.dumps({"listening": server.address}, sort_keys=True))
        server.serve_forever(poll_interval=_SHUTDOWN_POLL_SECONDS)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        server.server_close()


def _serve(options: argparse.Namespace) -> int:
    config = load_server_configuration(options.config)
    if not 0 <= options.port <= MAX_PORT:
        raise storage_error("STORAGE.PROTOCOL", f"a port is 0 to {MAX_PORT}, not {options.port}")
    host = INTERNAL_HOST if options.listen_internal else LOOPBACK_HOST
    serve_vault(
        VaultServer(
            (host, options.port),
            config=config,
            data_root=options.data_root,
            access_log=options.access_log,
            provision=options.provision,
        )
    )
    return 0


def _hash_token_command() -> int:
    text = sys.stdin.read(MAX_TOKEN_INPUT_BYTES + 1)
    if len(text) > MAX_TOKEN_INPUT_BYTES:
        raise storage_error(
            "STORAGE.PROTOCOL", f"hash-token reads at most {MAX_TOKEN_INPUT_BYTES} bytes"
        )
    print(hash_token(text))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand: ``serve``, ``hash-token`` or ``new-token``.

    Params: ``argv``, the arguments after ``-m cole_voice_lab.storage_server``.
    Returns: 0 once ``serve`` has shut down or a helper has printed its line, and 2 for any
    refusal (its message names the failure, never a token).
    Raises: ``SystemExit`` from argparse for unusable arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -I -B -m cole_voice_lab.storage_server",
        description="Serve one vault host's namespaces, or make and hash an access token.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="serve the configured namespaces")
    serve_parser.add_argument("--config", type=Path, required=True)
    serve_parser.add_argument("--data-root", type=Path, required=True)
    serve_parser.add_argument("--access-log", type=Path, required=True)
    serve_parser.add_argument(
        "--provision",
        action="store_true",
        help="make the data root and the namespace directories that are missing",
    )
    serve_parser.add_argument(
        "--listen-internal",
        action="store_true",
        help=f"bind {INTERNAL_HOST} instead of {LOOPBACK_HOST} (the container's own network)",
    )
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    commands.add_parser("hash-token", help="hash the one token read from standard input")
    commands.add_parser("new-token", help="print one fresh token to standard output")
    options = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        if options.command == "new-token":
            print(new_token())
            return 0
        if options.command == "hash-token":
            return _hash_token_command()
        return _serve(options)
    except (StorageError, OSError, ValueError) as failure:
        code = failure.code if isinstance(failure, StorageError) else "STORAGE.UNAVAILABLE"
        print(
            json.dumps(
                {"service": "vault", "error_code": code, "message": str(failure)}, sort_keys=True
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())

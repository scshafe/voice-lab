"""The Mac's vault client: one namespace over TLS, at a node the tailnet identity check named.

@canonical storage_http_client -- HttpBlobStore and HttpRefStore implement BlobStore and
RefStore against a storage_server over the tailnet: every session resolves the endpoint
through an injected TailnetResolver and requires the approved node's stable id, its tag, its
DNS name and the tailnet suffix before a byte is sent, connects to exactly the address that
check returned while the certificate is verified for the endpoint name, sends the token — once
it is one token in its own format — in one Authorization header and never in a URL, a log, an
exception or a repr, reads every answer bounded and within one deadline and re-checks it (a
blob against its digest and size, a JSON record against its contract), maps the server's
status and code back to one STORAGE.* failure, and retries only what may pass by itself.
Related: storage_server.py (the vault host this speaks to), storage_ports.py (the ports and
the STORAGE.* errors), storage_contracts.py (BlobRef, RefValue, VaultRequestRecord),
storage_binding.py (the composition root that builds this), tests/storage_conformance.py (the
checks every store passes), tests/test_storage_http_client.py (this module's own checks).

Identity. A session is one ``VaultConnection``. Before a request whose check is older than
``IDENTITY_MAX_AGE_SECONDS`` (and before the first one), the resolver is asked again. The peer
it returns must carry the approved ``node_stable_id``, the approved tag among its tags, a DNS
name equal to the endpoint (its trailing dot removed), and the endpoint must end in the
approved tailnet suffix; anything else is ``STORAGE.IDENTITY_MISMATCH`` and nothing is sent.
The address is then an IP literal the client connects to directly, so no name is resolved
twice and a stolen MagicDNS name reaches nothing; the certificate is verified for the endpoint
name with ``ssl.create_default_context`` (no pinning), and ``Host`` is the endpoint.

Answers. A blob is read as exactly the ``Content-Length`` the request asked for and is
accepted only when its digest and size match the ref; an answer that frames that length and
then ends short is a dropped connection (``STORAGE.UNAVAILABLE``), not a damaged copy. JSON is
read within ``MAX_JSON_BODY_BYTES`` and validated as its contract, and a ``RefValue`` must name
the ref that was asked for. A refusal's ``code`` decides the failure (a code this client does
not know is ``STORAGE.PROTOCOL``), and a refusal with no code is mapped from its status, so a
proxy's 502 or a reset while uploading is ``STORAGE.UNAVAILABLE`` and the retry gets the real
answer; a codeless 500 is read as ``STORAGE.INTEGRITY`` only for a ``HEAD``, whose body the
HTTP rules never send, which is the fail-closed reading and the only failure a store raises for
a ``has``. One exchange is bounded by one deadline — the connection, the request body and
every read of the answer together — so a vault that drips a byte at a time holds nothing
longer than ``timeout``. Retries are made only for a retryable code, at most ``attempts``
times, with exponential backoff and jitter through an injected ``sleep``. A ``swap`` is
retried too: a ``CONFLICT`` after a retry is reconciled by reading the ref, because the lost
answer may have been the winning one, and only a ref that is not at the intended version and
target is a conflict. One connection is used by one caller at a time (the Mac pushes from one
worker process): the identity check it remembers is not guarded against concurrent use.

This module imports only the standard library, pydantic and the package's
``storage_contracts`` and ``storage_ports`` modules; it never imports ``storage_sealing``,
``storage_local``, ``storage_server`` or any recording or catalog module, so the client can be
built (and the tailnet checked) without loading the encryption, the server or the lab.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import json
import os
import pwd
import random
import re
import socket
import ssl
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast

from pydantic import ValidationError

from .storage_contracts import (
    MAX_BLOB_BYTES,
    MAX_VAULT_ATTEMPTS,
    NAMESPACE_PATTERN,
    NODE_STABLE_ID_PATTERN,
    TAILSCALE_TAG_PATTERN,
    VAULT_TOKEN_PATTERN,
    VAULT_TOKEN_PREFIX,
    BlobRef,
    RefValue,
    StorageNameError,
    VaultRequestOperation,
    VaultRequestRecord,
    validate_ref_name,
    validate_tailnet_endpoint,
)
from .storage_ports import (
    STORAGE_RETRYABLE,
    StorageError,
    StorageErrorCode,
    storage_error,
)

__all__ = [
    "DEFAULT_ATTEMPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "HTTPS_PORT",
    "IDENTITY_MAX_AGE_SECONDS",
    "LOOPBACK_ADDRESSES",
    "MAX_JSON_BODY_BYTES",
    "MAX_TAILSCALE_OUTPUT_BYTES",
    "TAILSCALE_BINARY",
    "TAILSCALE_TIMEOUT_SECONDS",
    "ExpectedIdentity",
    "HttpBlobStore",
    "HttpRefStore",
    "ResolvedPeer",
    "TailnetResolver",
    "TailscaleCliResolver",
    "VaultConnection",
]

HTTPS_PORT: Final = 443
# One answer's JSON is a BlobRef, a RefValue or a refusal: none is anywhere near this.
MAX_JSON_BODY_BYTES: Final = 64 * 1024
DEFAULT_TIMEOUT_SECONDS: Final = 120.0
DEFAULT_ATTEMPTS: Final = 4
# The first wait after a retryable failure; each further wait doubles it, with jitter.
BACKOFF_SECONDS: Final = 0.5
MAX_BACKOFF_SECONDS: Final = 30.0
IDENTITY_MAX_AGE_SECONDS: Final = 60.0
# The app binary, not /usr/local/bin/tailscale (a shell wrapper), and not a PATH lookup: the
# service process has no HOME and no /usr/local/bin on its PATH.
TAILSCALE_BINARY: Final = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
TAILSCALE_TIMEOUT_SECONDS: Final = 10.0
# A tailnet of any size answers ``status --json`` well inside this; more is not read as JSON.
MAX_TAILSCALE_OUTPUT_BYTES: Final = 4 * 1024 * 1024
TAILSCALE_ENVIRONMENT_PATH: Final = "/usr/bin:/bin"
# The only addresses the test-only loopback constructor accepts.
LOOPBACK_ADDRESSES: Final = ("127.0.0.1", "::1")
# Tailscale's own range (100.64.0.0/10): the CLI resolver takes the peer's address from it.
TAILNET_RANGE: Final = ipaddress.ip_network("100.64.0.0/10")

_BEARER: Final = "Bearer "
_JSON_CONTENT_TYPE: Final = "application/json; charset=utf-8"
_BLOB_CONTENT_TYPE: Final = "application/octet-stream"
_MAX_DETAIL_LENGTH: Final = 200
# A body is written in pieces so the whole write, not each socket send, is under the deadline.
_SEND_CHUNK_BYTES: Final = 256 * 1024
# What ``VaultRequestRecord.status`` and the HTTP rules alike admit; ``http.client`` reads a
# status line up to 999, and a record of one outside this range cannot even be written.
_MIN_STATUS: Final = 100
_MAX_STATUS: Final = 599
_NAMESPACE = re.compile(NAMESPACE_PATTERN)
_NODE_STABLE_ID = re.compile(NODE_STABLE_ID_PATTERN)
_TAILSCALE_TAG = re.compile(TAILSCALE_TAG_PATTERN)
_TOKEN = re.compile(VAULT_TOKEN_PATTERN)
# The failure a status means when the answer names no code of its own: a proxy's answer, or a
# refusal of a HEAD request, which carries its code in a body the HTTP rules never send.
_STATUS_CODES: Final[Mapping[int, StorageErrorCode]] = {
    400: "STORAGE.PROTOCOL",
    401: "STORAGE.UNAUTHORIZED",
    404: "STORAGE.NOT_FOUND",
    405: "STORAGE.PROTOCOL",
    408: "STORAGE.TIMEOUT",
    409: "STORAGE.CONFLICT",
    411: "STORAGE.PROTOCOL",
    413: "STORAGE.TOO_LARGE",
    500: "STORAGE.PROTOCOL",
    502: "STORAGE.UNAVAILABLE",
    503: "STORAGE.UNAVAILABLE",
    504: "STORAGE.UNAVAILABLE",
    507: "STORAGE.QUOTA",
}
# A HEAD is the one request whose refusal can carry no code at all, so its status must be read
# alone. The vault answers 500 for stored bytes that failed their check and for its own
# protocol failure alike, and for a HEAD this reads it as the check failure: the fail-closed
# reading, and the only failure a store raises for a ``has``. Every other method's refusal does
# carry a body, so a 500 whose body names no code decided nothing about the stored bytes and
# stays a protocol failure rather than a claim that the vault's copy is damaged.
_HEAD_STATUS_CODES: Final[Mapping[int, StorageErrorCode]] = {500: "STORAGE.INTEGRITY"}

type RequestLog = Callable[[VaultRequestRecord], None]
type CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- the tailnet identity -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedIdentity:
    """The node a destination approval named: nothing else may answer for the endpoint.

    Raises (on construction): ``StorageNameError`` when the node id, the tag or the tailnet
    suffix is not of its form.
    """

    node_stable_id: str
    tag: str
    tailnet_suffix: str

    def __post_init__(self) -> None:
        if _NODE_STABLE_ID.fullmatch(self.node_stable_id) is None:
            raise StorageNameError(
                f"a node's stable id is 1 to 64 letters and digits: {self.node_stable_id!r:.120}"
            )
        if _TAILSCALE_TAG.fullmatch(self.tag) is None:
            raise StorageNameError(f"a tailnet tag is tag:<name>: {self.tag!r:.120}")
        validate_tailnet_endpoint(self.tailnet_suffix)


@dataclass(frozen=True, slots=True)
class ResolvedPeer:
    """What a resolver found for an endpoint: where it is, and who it says it is.

    ``address`` is the IP literal the client connects to (the tailnet address of the node);
    ``port`` is where it listens, which only a test resolver ever changes. ``dns_name`` is the
    node's own name, which Tailscale reports with a trailing dot.
    """

    address: str
    node_stable_id: str
    tags: tuple[str, ...]
    dns_name: str
    port: int = HTTPS_PORT


class TailnetResolver(Protocol):
    """Whatever can say where an endpoint is and what node answers there."""

    def resolve(self, endpoint: str) -> ResolvedPeer:
        """The peer that answers for ``endpoint`` right now.

        Params: ``endpoint``, the vault's DNS name.
        Returns: the ``ResolvedPeer``; the caller, not the resolver, decides whether it is
        the approved node.
        Raises: ``StorageError`` with ``STORAGE.UNAVAILABLE`` or ``STORAGE.TIMEOUT`` when the
        tailnet cannot be asked, ``STORAGE.PROTOCOL`` when the answer is not what it must be,
        or ``StorageIntegrityError`` (``STORAGE.IDENTITY_MISMATCH``) when the tailnet knows no
        such node.
        """


def _identity_problem(endpoint: str, identity: ExpectedIdentity, peer: ResolvedPeer) -> str | None:
    """What makes ``peer`` not the approved node for ``endpoint``; ``None`` when it is."""
    if not endpoint.endswith(f".{identity.tailnet_suffix}"):
        return f"{endpoint} is not a name in the tailnet {identity.tailnet_suffix}"
    if peer.node_stable_id != identity.node_stable_id:
        return (
            f"the node answering is {peer.node_stable_id!r:.80}, not the approved "
            f"{identity.node_stable_id!r:.80}"
        )
    if identity.tag not in peer.tags:
        return f"the node does not carry {identity.tag}, only {list(peer.tags)!r:.200}"
    if peer.dns_name.rstrip(".") != endpoint:
        return f"the node calls itself {peer.dns_name!r:.120}, not {endpoint}"
    try:
        ipaddress.ip_address(peer.address)
    except ValueError:
        return f"the node's address is not an IP address: {peer.address!r:.80}"
    return None


def _run_tailscale(argv: Sequence[str], **options: Any) -> subprocess.CompletedProcess[str]:
    """Run the Tailscale binary as the resolver asks; the only runner that runs anything."""
    return subprocess.run(list(argv), **options)


def _tailscale_environment() -> dict[str, str]:
    """A fixed environment with the passwd database's home, since the service has none."""
    return {"PATH": TAILSCALE_ENVIRONMENT_PATH, "HOME": pwd.getpwuid(os.getuid()).pw_dir}


@dataclass(frozen=True)
class TailscaleCliResolver:
    """The tailnet as the local Tailscale app reports it, over two read-only commands.

    ``status --json`` gives the tailnet's MagicDNS suffix and the peer whose ``DNSName`` is
    the endpoint (with the trailing dot Tailscale writes), from which the first IPv4 address
    in ``TAILNET_RANGE`` is taken; ``whois --json <address>`` then gives that node's stable
    id, tags and name. Both run the binary by absolute path with ``PATH=/usr/bin:/bin`` and
    the passwd database's ``HOME`` (the app's service process has none), a timeout and a cap
    on the output that is read; no message, log or exception carries the command line or a
    byte of its output, so nothing the tailnet says can leak through a refusal.
    Raises (on construction): ``StorageNameError`` for a relative binary path or a tailnet
    suffix that is not a DNS name.
    """

    tailnet_suffix: str
    binary: Path = TAILSCALE_BINARY
    runner: CommandRunner = _run_tailscale
    timeout: float = TAILSCALE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        validate_tailnet_endpoint(self.tailnet_suffix)
        if not self.binary.is_absolute():
            raise StorageNameError(f"the tailscale binary needs an absolute path: {self.binary}")

    def resolve(self, endpoint: str) -> ResolvedPeer:
        """The peer that answers for ``endpoint``; see ``TailnetResolver.resolve``."""
        status = self._json(("status", "--json"), "status")
        tailnet = _member(status, "CurrentTailnet", "status")
        suffix = _text(tailnet, "MagicDNSSuffix", "status")
        if suffix != self.tailnet_suffix:
            raise storage_error(
                "STORAGE.IDENTITY_MISMATCH",
                f"this Mac is on the tailnet {suffix!r:.120}, not {self.tailnet_suffix}",
            )
        address = self._peer_address(status, endpoint)
        node = _member(self._json(("whois", "--json", address), "whois"), "Node", "whois")
        return ResolvedPeer(
            address=address,
            node_stable_id=_text(node, "StableID", "whois"),
            tags=_texts(node, "Tags", "whois"),
            dns_name=_text(node, "Name", "whois"),
        )

    def _peer_address(self, status: Mapping[str, Any], endpoint: str) -> str:
        """The tailnet address of the one peer named ``endpoint``."""
        peers = _member(status, "Peer", "status")
        named = [
            peer
            for key in sorted(peers)
            for peer in (_member(peers, key, "status"),)
            if _text(peer, "DNSName", "status") == f"{endpoint}."
        ]
        if len(named) != 1:
            raise storage_error(
                "STORAGE.IDENTITY_MISMATCH",
                f"the tailnet has {len(named)} nodes named {endpoint}, not one",
            )
        for candidate in _texts(named[0], "TailscaleIPs", "status"):
            try:
                parsed = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if parsed.version == 4 and parsed in TAILNET_RANGE:
                return candidate
        raise storage_error(
            "STORAGE.IDENTITY_MISMATCH",
            f"the node named {endpoint} has no {TAILNET_RANGE} address",
        )

    def _json(self, arguments: Sequence[str], label: str) -> Mapping[str, Any]:
        """One command's output as a JSON object; nothing of it reaches a message."""
        argv = [str(self.binary), *arguments]
        try:
            completed = self.runner(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
                env=_tailscale_environment(),
            )
        except subprocess.TimeoutExpired as error:
            raise storage_error(
                "STORAGE.TIMEOUT",
                f"tailscale {label} did not answer within {self.timeout} seconds",
            ) from error
        except OSError as error:
            raise storage_error(
                "STORAGE.UNAVAILABLE",
                f"tailscale {label} could not be run from {self.binary}: {error.strerror}",
            ) from error
        if completed.returncode != 0:
            raise storage_error(
                "STORAGE.UNAVAILABLE",
                f"tailscale {label} answered with status {completed.returncode}",
            )
        if len(completed.stdout) > MAX_TAILSCALE_OUTPUT_BYTES:
            raise storage_error(
                "STORAGE.PROTOCOL",
                f"tailscale {label} answered with more than {MAX_TAILSCALE_OUTPUT_BYTES} "
                "characters, which is no answer this reads",
            )
        try:
            document = json.loads(completed.stdout)
        except ValueError as error:
            raise storage_error(
                "STORAGE.PROTOCOL", f"tailscale {label} answered with something that is not JSON"
            ) from error
        if not isinstance(document, dict):
            raise storage_error(
                "STORAGE.PROTOCOL", f"tailscale {label} answered with JSON that is no object"
            )
        return cast(Mapping[str, Any], document)


def _member(document: Mapping[str, Any], key: str, label: str) -> Mapping[str, Any]:
    """The JSON object at ``key``; nothing of the value reaches the message."""
    value = document.get(key)
    if not isinstance(value, dict):
        raise storage_error(
            "STORAGE.PROTOCOL", f"tailscale {label} named no {key} object in its answer"
        )
    return cast(Mapping[str, Any], value)


def _text(document: Mapping[str, Any], key: str, label: str) -> str:
    """The string at ``key``; nothing of the value reaches the message."""
    value = document.get(key)
    if not isinstance(value, str):
        raise storage_error(
            "STORAGE.PROTOCOL", f"tailscale {label} named no {key} string in its answer"
        )
    return value


def _texts(document: Mapping[str, Any], key: str, label: str) -> tuple[str, ...]:
    """The strings at ``key`` (an absent key is none); nothing of them reaches the message."""
    value = document.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise storage_error(
            "STORAGE.PROTOCOL", f"tailscale {label} named no {key} list of strings in its answer"
        )
    return tuple(cast(list[str], value))


# -- one request ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Call:
    """One request this client makes, and what an answer to it may be.

    ``accepted`` are the statuses the caller handles itself; every other status is a failure.
    ``response_limit`` bounds a successful answer's body, and ``expected_length`` is the
    ``Content-Length`` it must declare (a blob's size).
    """

    operation: VaultRequestOperation
    method: str
    target: str
    ref: str
    accepted: frozenset[int]
    body: bytes | None = None
    content_type: str = _JSON_CONTENT_TYPE
    response_limit: int = MAX_JSON_BODY_BYTES
    expected_length: int | None = None
    sent_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _Answer:
    """One answer, read whole and within its limit."""

    status: int
    body: bytes


@dataclass
class _Attempts:
    """How many times one call was sent, so a caller can tell a retry's answer apart."""

    made: int = 0


@dataclass(frozen=True, slots=True)
class _LoopbackRoute:
    """Where a test-only connection sends: a literal loopback address, in the clear."""

    address: str
    port: int
    host: str

    def destination(self) -> tuple[ResolvedPeer | None, str, int]:
        return None, self.address, self.port

    def secure(self, opened: socket.socket) -> socket.socket:
        return opened


@dataclass
class _TailnetRoute:
    """Where a vault connection sends: the checked node's address, under TLS for the name."""

    endpoint: str
    identity: ExpectedIdentity
    resolver: TailnetResolver
    tls_context: Callable[[], ssl.SSLContext]
    monotonic: Callable[[], float]
    max_age: float
    peer: ResolvedPeer | None = None
    checked_at: float = 0.0

    @property
    def host(self) -> str:
        return self.endpoint

    def destination(self) -> tuple[ResolvedPeer | None, str, int]:
        """The checked peer, re-checked when the last check is older than ``max_age``.

        Raises: ``StorageIntegrityError`` (``STORAGE.IDENTITY_MISMATCH``) when the node
        answering for the endpoint is not the approved one; whatever the resolver raises.
        """
        now = self.monotonic()
        if self.peer is None or now - self.checked_at >= self.max_age:
            found = self.resolver.resolve(self.endpoint)
            problem = _identity_problem(self.endpoint, self.identity, found)
            if problem is not None:
                raise storage_error(
                    "STORAGE.IDENTITY_MISMATCH",
                    f"the node answering for {self.endpoint} is not the approved vault: {problem}",
                )
            self.peer = found
            self.checked_at = now
        return self.peer, self.peer.address, self.peer.port

    def secure(self, opened: socket.socket) -> socket.socket:
        """Wrap the connected socket in TLS verified for the endpoint name."""
        try:
            return self.tls_context().wrap_socket(opened, server_hostname=self.endpoint)
        except BaseException:
            opened.close()
            raise


class _Route(Protocol):
    """Where one connection's requests go, and how the socket to there is secured."""

    @property
    def host(self) -> str:
        """The name or address every request sends as its ``Host``, and records."""

    def destination(self) -> tuple[ResolvedPeer | None, str, int]: ...

    def secure(self, opened: socket.socket) -> socket.socket: ...


def _detail(error: BaseException) -> str:
    """One transport failure as a message may carry it: its class and its own text."""
    return f"{type(error).__name__}: {str(error)[:_MAX_DETAIL_LENGTH]}"


@dataclass(frozen=True, slots=True)
class _Deadline:
    """One exchange's whole budget, so no byte that arrives restarts its clock.

    A socket timeout bounds one send or one recv, and one ``HTTPResponse.read`` is as many
    recvs as it takes, so a vault that hands over a byte just before each timeout expires
    would hold this caller for as long as it liked: a 16 MiB blob read a byte at a time is
    unbounded time on a 120-second timeout. Every wait is given instead what is left of one
    deadline, taken when the socket was made — the shape ``storage_server``'s
    ``_DeadlineReader`` uses for the inbound side of a request. The clock is the real one:
    this bounds real waiting, not a test's.
    """

    at: float

    @classmethod
    def starting(cls, seconds: float) -> _Deadline:
        return cls(at=time.monotonic() + seconds)

    def bound(self, opened: socket.socket) -> None:
        """Give the socket what is left of the budget, before it may wait on it.

        Raises: ``TimeoutError`` once the budget is spent.
        """
        remaining = self.at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the vault did not finish this exchange within its deadline")
        opened.settimeout(remaining)


class _DeadlineReader(io.RawIOBase):
    """Every recv of one answer, bounded by what is left of the exchange's deadline."""

    def __init__(self, opened: socket.socket, deadline: _Deadline) -> None:
        self._socket = opened
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        """Fill part of ``buffer`` from the socket, within what is left of the deadline.

        Returns: the bytes read, and 0 at the end of the stream.
        Raises: ``TimeoutError`` once the deadline has passed; ``OSError`` from the socket.
        """
        self._deadline.bound(self._socket)
        return self._socket.recv_into(buffer)


class _BoundedSocket:
    """The exchange's socket as ``http.client`` uses it: sendall, makefile and close.

    Every write goes out in pieces and every read through a ``_DeadlineReader``, so one
    exchange costs one deadline however slowly its bytes move. ``close`` does nothing, because
    ``http.client`` closes the socket as soon as an answer says ``Connection: close`` — which
    every answer of the vault does — and the answer it framed has still to be read; the
    exchange closes the real socket itself when it is done with it.
    """

    def __init__(self, opened: socket.socket, deadline: _Deadline) -> None:
        self._socket = opened
        self._deadline = deadline

    def sendall(self, data: bytes) -> None:
        for start in range(0, len(data), _SEND_CHUNK_BYTES):
            self._deadline.bound(self._socket)
            self._socket.sendall(data[start : start + _SEND_CHUNK_BYTES])

    def makefile(self, mode: str = "rb", *_arguments: Any, **_options: Any) -> io.BufferedReader:
        return io.BufferedReader(_DeadlineReader(self._socket, self._deadline))

    def close(self) -> None:
        """Nothing: the exchange owns the socket until it has read the answer."""


def _read_bounded(response: http.client.HTTPResponse, limit: int) -> bytes:
    """At most ``limit + 1`` bytes of the answer, so one byte too many is visible."""
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = response.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _document(body: bytes) -> Mapping[str, Any] | None:
    """The JSON object a refusal carries, or ``None`` when it carries none.

    A body this cannot parse is no document, whether it is not UTF-8, not JSON, or nested far
    deeper than the parser goes (``RecursionError``, which a hostile vault reaches well inside
    ``MAX_JSON_BODY_BYTES``); the status then decides the failure, as for any other body.
    """
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return cast(Mapping[str, Any], value) if isinstance(value, dict) else None


def _answer_code(answer: _Answer, method: str) -> StorageErrorCode:
    """The failure an answer names, or the one its status means; unknown names are protocol."""
    document = _document(answer.body)
    named = None if document is None else document.get("code")
    if isinstance(named, str):
        return cast(StorageErrorCode, named) if named in STORAGE_RETRYABLE else "STORAGE.PROTOCOL"
    if method == "HEAD" and answer.status in _HEAD_STATUS_CODES:
        return _HEAD_STATUS_CODES[answer.status]
    return _STATUS_CODES.get(answer.status, "STORAGE.PROTOCOL")


def _answer_message(answer: _Answer) -> str:
    """The refusal's own sentence as it can be repeated, or an empty string."""
    document = _document(answer.body)
    message = None if document is None else document.get("message")
    if not isinstance(message, str) or not message.isprintable() or not message:
        return ""
    return f": {message[:_MAX_DETAIL_LENGTH]}"


# -- the connection and its two stores -------------------------------------------------------


class VaultConnection:
    """One vault namespace at one endpoint, with the token and the identity it is reached by.

    Params: ``endpoint``, the vault's DNS name (its certificate is verified for this name and
    it is the ``Host`` every request sends); ``namespace``; ``token``, called for the raw
    token before each request, so it is never held here and never appears in a repr;
    ``resolver``; ``identity``, the approved node; ``request_log``, called with one
    ``VaultRequestRecord`` per attempt; ``timeout``, the whole budget of one exchange;
    ``attempts``, how many times one call may be sent; ``sleep``, ``clock``, ``monotonic``,
    the time seams; ``identity_max_age``, how long an identity check stands; ``tls_context``,
    which only a test ever changes (the default verifies against the system trust store).
    Raises (on construction): ``StorageNameError`` for an endpoint that is not a lowercase
    DNS name (an ``http://`` string or an IP literal is refused here) or a namespace that is
    not one; ``ValueError`` for an attempt count, timeout or maximum age out of range.
    """

    _route: _Route
    _namespace: str
    _token: Callable[[], str]
    _request_log: RequestLog | None
    _timeout: float
    _attempts: int
    _sleep: Callable[[float], None]
    _clock: Callable[[], datetime]

    def __init__(
        self,
        endpoint: str,
        namespace: str,
        token: Callable[[], str],
        resolver: TailnetResolver,
        identity: ExpectedIdentity,
        *,
        request_log: RequestLog | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        attempts: int = DEFAULT_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        identity_max_age: float = IDENTITY_MAX_AGE_SECONDS,
        tls_context: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    ) -> None:
        validate_tailnet_endpoint(endpoint)
        if identity_max_age < 0:
            raise ValueError(f"an identity check cannot stand for {identity_max_age} seconds")
        self._configure(
            route=_TailnetRoute(
                endpoint=endpoint,
                identity=identity,
                resolver=resolver,
                tls_context=tls_context,
                monotonic=monotonic,
                max_age=identity_max_age,
            ),
            namespace=namespace,
            token=token,
            request_log=request_log,
            timeout=timeout,
            attempts=attempts,
            sleep=sleep,
            clock=clock,
        )

    @classmethod
    def loopback_for_tests(
        cls,
        port: int,
        namespace: str,
        token: Callable[[], str],
        *,
        address: str = LOOPBACK_ADDRESSES[0],
        request_log: RequestLog | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        attempts: int = DEFAULT_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ) -> VaultConnection:
        """A connection to a vault server on this machine's loopback, for tests only.

        It speaks plain HTTP and checks no tailnet identity, so it can never reach anything
        but a server this machine runs: ``address`` must be a literal loopback address. No
        binding, command or environment variable leads here; only tests and the native
        fixture wrapper call it, which a test proves by reading every source file.
        Params: ``port``; ``namespace``; ``token``; ``address``, ``127.0.0.1`` or ``::1``;
        the seams ``VaultConnection`` takes.
        Returns: the connection.
        Raises: ``StorageNameError`` for any other address or a port outside 1-65535.
        """
        if address not in LOOPBACK_ADDRESSES:
            raise StorageNameError(
                f"a loopback connection reaches {LOOPBACK_ADDRESSES} only: {address!r:.80}"
            )
        if not 1 <= port <= 65535:
            raise StorageNameError(f"a loopback connection needs a port, not {port}")
        host = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        connection = cls.__new__(cls)
        connection._configure(
            route=_LoopbackRoute(address=address, port=port, host=host),
            namespace=namespace,
            token=token,
            request_log=request_log,
            timeout=timeout,
            attempts=attempts,
            sleep=sleep,
            clock=clock,
        )
        return connection

    def _configure(
        self,
        *,
        route: _Route,
        namespace: str,
        token: Callable[[], str],
        request_log: RequestLog | None,
        timeout: float,
        attempts: int,
        sleep: Callable[[float], None],
        clock: Callable[[], datetime],
    ) -> None:
        if _NAMESPACE.fullmatch(namespace) is None:
            raise StorageNameError(
                "a namespace is 1 to 63 characters of [a-z0-9-] starting with a letter: "
                f"{namespace!r:.80}"
            )
        if not 1 <= attempts <= MAX_VAULT_ATTEMPTS:
            raise ValueError(f"a call is sent 1 to {MAX_VAULT_ATTEMPTS} times, not {attempts}")
        if timeout <= 0:
            raise ValueError(f"a request needs a positive timeout, not {timeout}")
        self._route = route
        self._namespace = namespace
        self._token = token
        self._request_log = request_log
        self._timeout = timeout
        self._attempts = attempts
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:
        """The endpoint and namespace alone: no token ever reaches a repr or a log."""
        return (
            f"{type(self).__name__}(endpoint={self._route.host!r}, namespace={self._namespace!r})"
        )

    @property
    def endpoint(self) -> str:
        """The name this connection speaks to, as every record of it says."""
        return self._route.host

    @property
    def namespace(self) -> str:
        """The one namespace this connection reaches."""
        return self._namespace

    def blob_store(self) -> HttpBlobStore:
        """The ``BlobStore`` of this namespace."""
        return HttpBlobStore(self)

    def ref_store(self) -> HttpRefStore:
        """The ``RefStore`` of this namespace."""
        return HttpRefStore(self)

    # -- sending ---------------------------------------------------------------------------

    def _perform(self, call: _Call, attempts: _Attempts | None = None) -> _Answer:
        """Send ``call``, retrying only a retryable failure, and return an accepted answer.

        Params: ``call``; ``attempts``, filled in with how many sends were made.
        Returns: the answer, whose status is one of ``call.accepted``.
        Raises: ``StorageError`` (``StorageIntegrityError`` for the integrity codes) with the
        failure of the last attempt.
        """
        counted = _Attempts() if attempts is None else attempts
        while True:
            counted.made += 1
            try:
                return self._attempt(call, counted.made)
            except StorageError as error:
                if not error.retryable or counted.made >= self._attempts:
                    raise
            self._sleep(_backoff(counted.made))

    def _attempt(self, call: _Call, attempt: int) -> _Answer:
        """Send ``call`` once, record the attempt and refuse anything but an accepted answer."""
        node: str | None = None
        try:
            peer, address, port = self._route.destination()
            node = None if peer is None else peer.node_stable_id
            answer = self._exchange(call, address, port)
        except StorageError as error:
            self._record(call, attempt, status=None, code=error.code, count=0, node=node)
            raise
        code = _answer_code(answer, call.method) if answer.status >= 400 else None
        # Blob bytes alone: a refused ``get-blob`` carries the vault's JSON refusal, not one
        # byte of the blob, and the vault's own access record counts none for it either.
        received = call.operation == "get-blob" and answer.status < 400
        count = len(answer.body) if received else call.sent_bytes
        self._record(call, attempt, status=answer.status, code=code, count=count, node=node)
        if answer.status in call.accepted:
            return answer
        failure = code if code is not None else "STORAGE.PROTOCOL"
        raise storage_error(
            failure,
            f"the vault at {self._route.host} answered {answer.status} ({failure}) for "
            f"{call.operation} {call.ref}{_answer_message(answer)}",
        )

    def _exchange(self, call: _Call, address: str, port: int) -> _Answer:
        """One request and its answer on a fresh connection, read within the call's limits.

        The token is checked for its own form before it can become a header, so a value that
        is no token — one read with the trailing newline a Keychain or a secret file gives it,
        say — is refused here instead of reaching an exception that would repeat it. The whole
        exchange is spent against one deadline rather than a timeout each byte restarts.
        Raises: ``StorageError`` with ``STORAGE.TIMEOUT`` (the exchange did not finish in
        time), ``STORAGE.UNAVAILABLE`` (the connection failed, was refused or was dropped, the
        TLS handshake did not succeed, or the answer ended before its declared length),
        ``STORAGE.PROTOCOL`` (the answer is longer than it declared, declares a length a blob
        answer may not, carries a status no answer may carry, or could not be written as one
        request at all); ``StorageIntegrityError`` with ``STORAGE.UNAUTHORIZED`` when the token
        seam gave something that is no vault token.
        """
        token = self._token()
        if _TOKEN.fullmatch(token) is None:
            # Nothing of the value is repeated, here or anywhere: it may be a live token.
            raise storage_error(
                "STORAGE.UNAUTHORIZED",
                f"the token this connection holds for {self._route.host} is not one "
                f"'{VAULT_TOKEN_PREFIX}' vault token on one line, so nothing was sent",
            )
        deadline = _Deadline.starting(self._timeout)
        try:
            with self._connected(address, port, deadline) as connection:
                connection.putrequest(
                    call.method, call.target, skip_host=True, skip_accept_encoding=True
                )
                connection.putheader("Host", self._route.host)
                connection.putheader("Authorization", f"{_BEARER}{token}")
                if call.body is not None:
                    connection.putheader("Content-Length", str(len(call.body)))
                    connection.putheader("Content-Type", call.content_type)
                connection.endheaders(call.body)
                response = connection.getresponse()
                status = response.status
                if not _MIN_STATUS <= status <= _MAX_STATUS:
                    raise storage_error(
                        "STORAGE.PROTOCOL",
                        f"the vault at {self._route.host} answered {call.operation} "
                        f"{call.ref} with status {status}, which is no HTTP status",
                    )
                declared = response.getheader("Content-Length")
                limit = MAX_JSON_BODY_BYTES if status >= 400 else call.response_limit
                body = _read_bounded(response, limit)
        except TimeoutError as error:
            raise storage_error(
                "STORAGE.TIMEOUT",
                f"the vault at {self._route.host} did not answer {call.operation} "
                f"{call.ref} within {self._timeout} seconds",
            ) from error
        except (OSError, http.client.HTTPException) as error:
            raise storage_error(
                "STORAGE.UNAVAILABLE",
                f"the vault at {self._route.host} could not be reached for {call.operation} "
                f"{call.ref}: {_detail(error)}",
            ) from error
        except ValueError as error:
            # The class alone, and no cause: a ValueError from the request writer quotes what
            # it refused, and the Authorization header is one of the things it may have been
            # given, so neither the message nor a traceback of this failure may carry it.
            raise storage_error(
                "STORAGE.PROTOCOL",
                f"the vault at {self._route.host} could not be sent {call.operation} "
                f"{call.ref}: {type(error).__name__}",
            ) from None
        if len(body) > limit:
            raise storage_error(
                "STORAGE.PROTOCOL",
                f"the vault at {self._route.host} answered {call.operation} {call.ref} with "
                f"more than {limit} bytes",
            )
        if status < 400 and call.expected_length is not None:
            expected = str(call.expected_length)
            if declared != expected:
                raise storage_error(
                    "STORAGE.PROTOCOL",
                    f"the vault at {self._route.host} answered {call.operation} {call.ref} "
                    f"with Content-Length {declared!r:.40}, not {expected}",
                )
            # A HEAD's answer never carries the body it frames. For anything else, framing the
            # right length and then sending less is a connection that ended, which may pass by
            # itself; only a whole answer whose bytes are wrong is a failure of the stored copy.
            if call.method != "HEAD" and len(body) < call.expected_length:
                raise storage_error(
                    "STORAGE.UNAVAILABLE",
                    f"the vault at {self._route.host} answered {call.operation} {call.ref} "
                    f"with {len(body)} of the {expected} bytes it declared, so the answer "
                    "ended before its declared length",
                )
        return _Answer(status=status, body=body)

    @contextmanager
    def _connected(
        self, address: str, port: int, deadline: _Deadline
    ) -> Iterator[http.client.HTTPConnection]:
        """A connection whose socket is already made and secured; closed on the way out."""
        connection = http.client.HTTPConnection(address, port, timeout=self._timeout)
        opened = socket.create_connection((address, port), timeout=self._timeout)
        try:
            deadline.bound(opened)
            # A failed handshake closes what it was given; closing it again is harmless.
            opened = self._route.secure(opened)
            # ``http.client`` asks its socket only for sendall, makefile and close, and this
            # is where one deadline reaches every one of them.
            connection.sock = cast(socket.socket, _BoundedSocket(opened, deadline))
            yield connection
        finally:
            connection.close()
            opened.close()

    def _record(
        self,
        call: _Call,
        attempt: int,
        *,
        status: int | None,
        code: StorageErrorCode | None,
        count: int,
        node: str | None,
    ) -> None:
        """Write one attempt to the request log; the token is in nothing it holds."""
        if self._request_log is None:
            return
        self._request_log(
            VaultRequestRecord(
                time=self._clock(),
                endpoint=self._route.host,
                namespace=self._namespace,
                operation=call.operation,
                ref=call.ref,
                bytes=count,
                status=status,
                code=code,
                attempt=attempt,
                node_stable_id=node,
            )
        )

    def _blob_target(self, ref: BlobRef) -> str:
        return f"/v1/{self._namespace}/blobs/{ref.sha256}?size={ref.size}"

    def _ref_target(self, name: str) -> str:
        return f"/v1/{self._namespace}/refs/{name}"


def _backoff(attempt: int) -> float:
    """How long to wait after ``attempt`` failed: doubling, capped, with jitter."""
    delay: float = min(BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
    return delay * (0.5 + random.random() / 2)


def _checked_ref(ref: BlobRef) -> BlobRef:
    """``ref`` validated again, so a ``BlobRef`` built without validation is never sent.

    Raises: ``StorageError`` (``STORAGE.PROTOCOL``) when ``ref`` is not a valid ``BlobRef``.
    """
    if isinstance(ref, BlobRef):
        try:
            return BlobRef.model_validate(dict(ref))
        except ValidationError:
            pass
    sha256 = getattr(ref, "sha256", None)
    size = getattr(ref, "size", None)
    raise storage_error(
        "STORAGE.PROTOCOL",
        f"not a valid blob ref (a SHA-256 digest and a size of 0 to {MAX_BLOB_BYTES} bytes): "
        f"sha256={sha256!r:.80}, size={size!r:.40}",
    )


def _validated[Record: (BlobRef, RefValue)](
    model: type[Record], answer: _Answer, endpoint: str
) -> Record:
    """One answer's JSON as the contract it must be.

    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when it is not that contract.
    """
    try:
        return model.model_validate_json(answer.body)
    except ValidationError as error:
        raise storage_error(
            "STORAGE.INTEGRITY",
            f"the vault at {endpoint} answered with something that is not a "
            f"{model.__name__}: {_detail(error)}",
        ) from error


@dataclass(frozen=True, slots=True)
class HttpBlobStore:
    """``BlobStore`` over one vault namespace; every answer is checked before it is believed."""

    connection: VaultConnection

    def put(self, data: bytes) -> BlobRef:
        """Store ``data`` once; see ``BlobStore.put``.

        Raises: ``StorageError`` with ``STORAGE.TOO_LARGE`` (before anything is sent),
        ``STORAGE.QUOTA``, ``STORAGE.UNAVAILABLE``, ``STORAGE.TIMEOUT`` or
        ``STORAGE.PROTOCOL``; ``StorageIntegrityError`` with ``STORAGE.UNAUTHORIZED``,
        ``STORAGE.IDENTITY_MISMATCH`` or ``STORAGE.INTEGRITY`` (the vault stored other bytes
        than these, or answered with something that is no ``BlobRef``).
        """
        if len(data) > MAX_BLOB_BYTES:
            raise storage_error(
                "STORAGE.TOO_LARGE", f"a blob is at most {MAX_BLOB_BYTES} bytes, not {len(data)}"
            )
        ref = BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        answer = self.connection._perform(
            _Call(
                operation="put-blob",
                method="PUT",
                target=f"/v1/{self.connection.namespace}/blobs/{ref.sha256}",
                ref=ref.sha256,
                accepted=frozenset({200, 201}),
                body=data,
                content_type=_BLOB_CONTENT_TYPE,
                sent_bytes=len(data),
            )
        )
        stored = _validated(BlobRef, answer, self.connection.endpoint)
        if stored != ref:
            raise storage_error(
                "STORAGE.INTEGRITY",
                f"the vault at {self.connection.endpoint} stored {stored.sha256} of "
                f"{stored.size} bytes, not {ref.sha256} of {ref.size}",
            )
        return ref

    def has(self, ref: BlobRef) -> bool:
        """Whether a blob of this ref is stored; see ``BlobStore.has``.

        Raises: ``StorageError`` with ``STORAGE.PROTOCOL`` (a ref that is not a valid
        ``BlobRef``, before anything is sent, or an answer that declares another size),
        ``STORAGE.UNAVAILABLE`` or ``STORAGE.TIMEOUT``; ``StorageIntegrityError`` with
        ``STORAGE.UNAUTHORIZED``, ``STORAGE.IDENTITY_MISMATCH`` or ``STORAGE.INTEGRITY``
        (the vault's stored entry is unsafe).
        """
        checked = _checked_ref(ref)
        answer = self.connection._perform(
            _Call(
                operation="has-blob",
                method="HEAD",
                target=self.connection._blob_target(checked),
                ref=checked.sha256,
                accepted=frozenset({200, 404}),
                expected_length=checked.size,
            )
        )
        return answer.status == 200

    def get(self, ref: BlobRef) -> bytes:
        """Read one blob and check it before returning it; see ``BlobStore.get``.

        Raises: ``StorageError`` with ``STORAGE.PROTOCOL`` (a ref that is not a valid
        ``BlobRef``, before anything is sent, or an answer outside its declared length),
        ``STORAGE.NOT_FOUND``, ``STORAGE.UNAVAILABLE`` or ``STORAGE.TIMEOUT``;
        ``StorageIntegrityError`` with ``STORAGE.UNAUTHORIZED``,
        ``STORAGE.IDENTITY_MISMATCH`` or ``STORAGE.INTEGRITY`` (the bytes that arrived are
        not the ref's).
        """
        checked = _checked_ref(ref)
        answer = self.connection._perform(
            _Call(
                operation="get-blob",
                method="GET",
                target=self.connection._blob_target(checked),
                ref=checked.sha256,
                accepted=frozenset({200}),
                response_limit=checked.size,
                expected_length=checked.size,
            )
        )
        data = answer.body
        if len(data) != checked.size or hashlib.sha256(data).hexdigest() != checked.sha256:
            raise storage_error(
                "STORAGE.INTEGRITY",
                f"the vault at {self.connection.endpoint} answered blob {checked.sha256} with "
                f"{len(data)} bytes that are not it",
            )
        return data


@dataclass(frozen=True, slots=True)
class HttpRefStore:
    """``RefStore`` over one vault namespace; every value is checked before it is believed."""

    connection: VaultConnection

    def read(self, name: str) -> RefValue | None:
        """The current value of a ref; see ``RefStore.read``.

        Raises: ``StorageNameError`` for an invalid name (before anything is sent);
        ``StorageError`` with ``STORAGE.UNAVAILABLE``, ``STORAGE.TIMEOUT`` or
        ``STORAGE.PROTOCOL``; ``StorageIntegrityError`` with ``STORAGE.UNAUTHORIZED``,
        ``STORAGE.IDENTITY_MISMATCH`` or ``STORAGE.INTEGRITY`` (the value is not a
        ``RefValue``, or names another ref).
        """
        validated = validate_ref_name(name)
        answer = self.connection._perform(
            _Call(
                operation="read-ref",
                method="GET",
                target=self.connection._ref_target(validated),
                ref=validated,
                accepted=frozenset({200, 404}),
            )
        )
        if answer.status == 404:
            return None
        return self._value(answer, validated)

    def swap(self, name: str, *, expected_version: int | None, target: BlobRef) -> RefValue:
        """Point a ref at ``target`` if nobody changed it; see ``RefStore.swap``.

        A swap that was sent more than once and is then refused as a conflict may have been
        won by an earlier attempt whose answer was lost, so the ref is read: one that is at
        the intended version with the intended target is that win, and anything else is the
        conflict it was reported as.
        Raises: ``StorageNameError`` for an invalid name (before anything is sent);
        ``StorageError`` with ``STORAGE.CONFLICT``, ``STORAGE.PROTOCOL`` (a ``target`` that
        is not a valid ``BlobRef``, before anything is sent), ``STORAGE.UNAVAILABLE`` or
        ``STORAGE.TIMEOUT``; ``StorageIntegrityError`` with ``STORAGE.UNAUTHORIZED``,
        ``STORAGE.IDENTITY_MISMATCH`` or ``STORAGE.INTEGRITY`` (the value that came back is
        not the swap that was asked for).
        """
        validated = validate_ref_name(name)
        checked = _checked_ref(target)
        body = json.dumps(
            {
                "expected_version": expected_version,
                "target": {"sha256": checked.sha256, "size": checked.size},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        call = _Call(
            operation="swap-ref",
            method="POST",
            target=self.connection._ref_target(validated),
            ref=validated,
            accepted=frozenset({200}),
            body=body,
        )
        attempts = _Attempts()
        try:
            answer = self.connection._perform(call, attempts)
        except StorageError as error:
            if error.code != "STORAGE.CONFLICT" or attempts.made == 1:
                raise
            won = self._won_after_retry(validated, expected_version, checked)
            if won is None:
                raise
            return won
        value = self._value(answer, validated)
        return self._expected(value, validated, expected_version, checked)

    def _won_after_retry(
        self, name: str, expected_version: int | None, target: BlobRef
    ) -> RefValue | None:
        """The value a retried swap turns out to have written, or ``None`` for a real conflict."""
        current = self.read(name)
        if current is None:
            return None
        version = 1 if expected_version is None else expected_version + 1
        if current.version != version or current.target != target:
            return None
        return current

    def _value(self, answer: _Answer, name: str) -> RefValue:
        """One answer as the ``RefValue`` of the ref that was asked for."""
        value = _validated(RefValue, answer, self.connection.endpoint)
        if value.name != name:
            raise storage_error(
                "STORAGE.INTEGRITY",
                f"the vault at {self.connection.endpoint} answered for ref {name} with the "
                f"value of {value.name}",
            )
        return value

    def _expected(
        self, value: RefValue, name: str, expected_version: int | None, target: BlobRef
    ) -> RefValue:
        """The swap's answer, once it is the swap that was asked for."""
        version = 1 if expected_version is None else expected_version + 1
        if value.version != version or value.target != target:
            raise storage_error(
                "STORAGE.INTEGRITY",
                f"the vault at {self.connection.endpoint} answered the swap of ref {name} to "
                f"version {version} with version {value.version} of another target",
            )
        return value

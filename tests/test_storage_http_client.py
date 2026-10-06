"""The vault client: the identity check, the TLS path, every refusal and every retry.

Scenario tests for ``src/cole_voice_lab/storage_http_client.py``. Three kinds of peer are used
and no other: a real ``storage_server`` in a thread on the IPv4 loopback (the happy paths, the
credential refusals and the request log), a scripted raw-socket server that answers exactly
the bytes a check needs (dropped connections, wrong bytes, a slow answer, a lost swap), and a
TLS server whose certificate comes from a throwaway authority made in ``tmp_path``. No test
runs ``tailscale``: the CLI resolver is driven by an injected runner over recorded JSON, and
the connection's own resolver is a fake. No test reaches the network, the real library, the
Keychain or a real token; tokens are made by ``storage_server.new_token`` or are the synthetic
constant here, and both live only in memory.
"""

from __future__ import annotations

import ast
import hashlib
import http.client
import inspect
import io
import json
import socket
import ssl
import subprocess
import threading
import time
import traceback
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

from cole_voice_lab import storage_http_client, tailnet
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.storage_contracts import (
    MAX_BLOB_BYTES,
    BlobRef,
    RefValue,
    StorageNameError,
    VaultNamespaceConfig,
    VaultRequestRecord,
    VaultServerConfig,
)
from cole_voice_lab.storage_http_client import (
    LOOPBACK_ADDRESSES,
    MAX_JSON_BODY_BYTES,
    MAX_TAILSCALE_OUTPUT_BYTES,
    TAILSCALE_BINARY,
    ExpectedIdentity,
    HttpBlobStore,
    ResolvedPeer,
    TailscaleCliResolver,
    VaultConnection,
)
from cole_voice_lab.storage_ports import StorageError, StorageIntegrityError
from cole_voice_lab.storage_server import (
    LOOPBACK_HOST,
    NAMESPACES_DIRECTORY,
    VaultServer,
    hash_token,
    new_token,
)

REPOSITORY = Path(__file__).resolve().parents[1]
NAMESPACE = "cole"
OTHER_NAMESPACE = "spare"
SUFFIX = "example-test.ts.net"
ENDPOINT = f"voice-lab-vault.{SUFFIX}"
NODE_ID = "nodeVault123"
TAG = "tag:voice-lab-vault"
PEER_ADDRESS = "100.64.12.34"
# A synthetic token of the right shape; no vault ever accepted it and none ever will.
SYNTHETIC_TOKEN = "cvlv_" + "s" * 43
HELLO = b"hello vault"
HELLO_REF = BlobRef(sha256=hashlib.sha256(HELLO).hexdigest(), size=len(HELLO))
HEAD_REF = "libraries/cole-self/head"
QUOTA = 4 * 1024 * 1024
INTEGRITY_CODES = {"STORAGE.INTEGRITY", "STORAGE.UNAUTHORIZED", "STORAGE.IDENTITY_MISMATCH"}
FIXED_TIME = datetime(2026, 9, 17, 12, tzinfo=UTC)


# -- recorders ------------------------------------------------------------------------------


@dataclass
class Recorder:
    """The request log: every attempt, in order."""

    records: list[VaultRequestRecord] = field(default_factory=list)

    def __call__(self, record: VaultRequestRecord) -> None:
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(canonical_json_bytes(record).decode() for record in self.records)


@dataclass
class Waits:
    """The backoff the client asked for, instead of spending it."""

    seconds: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


@dataclass
class FakeResolver:
    """A resolver that answers from memory; the real one is never run in a test."""

    peer: ResolvedPeer
    endpoints: list[str] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return len(self.endpoints)

    def resolve(self, endpoint: str) -> ResolvedPeer:
        self.endpoints.append(endpoint)
        return self.peer


def peer(**changes: Any) -> ResolvedPeer:
    fields: dict[str, Any] = {
        "address": PEER_ADDRESS,
        "node_stable_id": NODE_ID,
        "tags": (TAG,),
        "dns_name": f"{ENDPOINT}.",
    }
    return ResolvedPeer(**(fields | changes))


def identity(**changes: Any) -> ExpectedIdentity:
    fields: dict[str, Any] = {
        "node_stable_id": NODE_ID,
        "tag": TAG,
        "tailnet_suffix": SUFFIX,
    }
    return ExpectedIdentity(**(fields | changes))


# -- the scripted raw-socket server ---------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """What the scripted server does with one connection.

    ``raw`` is answered whole, or the connection is closed unanswered when it is ``None``.
    ``read_body`` reads the declared body first; ``drop_after`` reads only that many of its
    bytes and then closes, which is a connection dropped mid-upload. ``delay`` waits before
    answering, which is a server too slow for the client's timeout. ``drip_after`` sends that
    many bytes at once and then one byte every ``drip`` seconds, which is a server that never
    stops answering and never finishes: each byte would restart a per-socket timeout.
    """

    raw: bytes | None = None
    read_body: bool = True
    drop_after: int | None = None
    delay: float = 0.0
    drip_after: int | None = None
    drip: float = 0.0


@dataclass(frozen=True)
class Received:
    """One request as the scripted server saw it."""

    line: str
    headers: tuple[tuple[str, str], ...]
    body_bytes: int

    def header(self, name: str) -> str | None:
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)

    @property
    def target(self) -> str:
        return self.line.split(" ")[1]


class ScriptedServer:
    """A socket that answers each connection from a script, however malformed the answer.

    Connections are served one at a time, in the order the script names; a connection beyond
    the script is closed unanswered and counted in ``overruns``.
    """

    def __init__(self, replies: Sequence[Reply], *, context: ssl.SSLContext | None = None) -> None:
        self._replies = list(replies)
        self._context = context
        self.requests: list[Received] = []
        self.server_names: list[str | None] = []
        self.overruns = 0
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((LOOPBACK_HOST, 0))
        self._listener.listen(8)
        self._listener.settimeout(0.05)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:  # pragma: no cover - the listener was closed under the accept
                return
            with closing(connection), suppress(OSError, ssl.SSLError):
                self._answer(connection)

    def _answer(self, connection: socket.socket) -> None:
        connection.settimeout(10.0)
        if self._context is not None:
            connection = self._context.wrap_socket(connection, server_side=True)
        if not self._replies:
            self.overruns += 1
            return
        reply = self._replies.pop(0)
        self.requests.append(self._read(connection, reply))
        if reply.delay:
            time.sleep(reply.delay)
        if reply.raw is None:
            return
        if reply.drip_after is None:
            connection.sendall(reply.raw)
            return
        connection.sendall(reply.raw[: reply.drip_after])
        for index in range(reply.drip_after, len(reply.raw)):
            time.sleep(reply.drip)
            connection.sendall(reply.raw[index : index + 1])

    def _read(self, connection: socket.socket, reply: Reply) -> Received:
        stream = connection.makefile("rb")
        line = stream.readline().decode("latin-1").rstrip("\r\n")
        headers: list[tuple[str, str]] = []
        while True:
            raw = stream.readline().decode("latin-1").rstrip("\r\n")
            if not raw:
                break
            name, _, value = raw.partition(":")
            headers.append((name.strip(), value.strip()))
        declared = next(
            (int(value) for name, value in headers if name.lower() == "content-length"), 0
        )
        wanted = declared if reply.drop_after is None else min(declared, reply.drop_after)
        read = 0
        while reply.read_body and read < wanted:
            chunk = stream.read(min(wanted - read, 65536))
            if not chunk:
                break
            read += len(chunk)
        return Received(line=line, headers=tuple(headers), body_bytes=read)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._listener.close()

    def __enter__(self) -> ScriptedServer:
        return self

    def __exit__(self, *_details: object) -> None:
        self.close()


def answer(status: int, body: bytes, *, content_type: str, length: int | None = None) -> bytes:
    """One whole HTTP answer, framed exactly as ``length`` (or the body) says."""
    head = "\r\n".join(
        (
            f"HTTP/1.1 {status} X",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body) if length is None else length}",
            "Connection: close",
            "",
            "",
        )
    )
    return head.encode("ascii") + body


def json_answer(status: int, payload: object) -> bytes:
    return answer(
        status,
        json.dumps(payload).encode("utf-8"),
        content_type="application/json; charset=utf-8",
    )


def refusal(status: int, code: str, message: str = "no") -> bytes:
    return json_answer(status, {"code": code, "message": message})


def blob_answer(body: bytes, *, length: int | None = None) -> bytes:
    return answer(200, body, content_type="application/octet-stream", length=length)


def ref_answer(name: str, version: int, target: BlobRef) -> bytes:
    value = RefValue(name=name, version=version, target=target, updated_at=FIXED_TIME)
    return answer(
        200,
        canonical_json_bytes(value),
        content_type="application/json; charset=utf-8",
    )


def scripted_client(
    server: ScriptedServer,
    *,
    log: Recorder | None = None,
    waits: Waits | None = None,
    attempts: int = 4,
    timeout: float = 5.0,
) -> VaultConnection:
    """A loopback client for the scripted server, with its backoff recorded, not spent."""
    return VaultConnection.loopback_for_tests(
        server.port,
        NAMESPACE,
        lambda: SYNTHETIC_TOKEN,
        request_log=log,
        attempts=attempts,
        timeout=timeout,
        sleep=(waits or Waits()),
        clock=lambda: FIXED_TIME,
    )


# -- a real vault server on the loopback ----------------------------------------------------


@dataclass(frozen=True)
class Vault:
    """One running vault server, its namespace roots and the raw tokens of this process."""

    server: VaultServer
    tokens: Mapping[str, tuple[str, str]]
    data_root: Path

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def write_token(self, namespace: str = NAMESPACE) -> str:
        return self.tokens[namespace][1]

    def root(self, namespace: str = NAMESPACE) -> Path:
        return self.data_root / NAMESPACES_DIRECTORY / namespace


@contextmanager
def running_vault(tmp_path: Path, *, quota: int = QUOTA) -> Iterator[Vault]:
    """A real ``storage_server`` with two namespaces, in a thread on the loopback."""
    tokens = {name: (new_token(), new_token()) for name in (NAMESPACE, OTHER_NAMESPACE)}
    config = VaultServerConfig(
        namespaces=tuple(
            VaultNamespaceConfig(
                name=name,
                read_token_sha256=hash_token(tokens[name][0]),
                write_token_sha256=hash_token(tokens[name][1]),
                quota_bytes=quota,
            )
            for name in tokens
        ),
        max_workers=4,
        request_timeout_seconds=30,
    )
    data_root = tmp_path / "vault-data"
    server = VaultServer(
        (LOOPBACK_HOST, 0),
        config=config,
        data_root=data_root,
        access_log=tmp_path / "access.jsonl",
        provision=True,
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield Vault(server=server, tokens=tokens, data_root=data_root)
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
        server.server_close()


def vault_client(
    vault: Vault,
    *,
    token: str | None = None,
    namespace: str = NAMESPACE,
    log: Recorder | None = None,
    attempts: int = 4,
) -> VaultConnection:
    return VaultConnection.loopback_for_tests(
        vault.port,
        namespace,
        lambda: token if token is not None else vault.write_token(),
        request_log=log,
        attempts=attempts,
        timeout=5.0,
        sleep=Waits(),
    )


def refused(code: str, action: Any, *, integrity: bool = False) -> StorageError:
    """``action`` must raise the named storage failure, of the class the code belongs to."""
    with pytest.raises(StorageIntegrityError if integrity else StorageError) as caught:
        action()
    assert caught.value.code == code, str(caught.value)
    return caught.value


# -- the identity check ---------------------------------------------------------------------


def tailnet_client(
    resolver: FakeResolver,
    *,
    expected: ExpectedIdentity | None = None,
    endpoint: str = ENDPOINT,
    log: Recorder | None = None,
    attempts: int = 1,
    context: Any = None,
    monotonic: Any = None,
    timeout: float = 5.0,
) -> VaultConnection:
    options: dict[str, Any] = {}
    if context is not None:
        options["tls_context"] = context
    if monotonic is not None:
        options["monotonic"] = monotonic
    return VaultConnection(
        endpoint,
        NAMESPACE,
        lambda: SYNTHETIC_TOKEN,
        resolver,
        expected or identity(),
        request_log=log,
        attempts=attempts,
        timeout=timeout,
        sleep=Waits(),
        clock=lambda: FIXED_TIME,
        **options,
    )


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"node_stable_id": "nodeOther999"}, "not the approved"),
        ({"tags": ()}, "does not carry tag:voice-lab-vault"),
        ({"tags": ("tag:other",)}, "does not carry tag:voice-lab-vault"),
        ({"dns_name": "other.example-test.ts.net."}, "calls itself"),
        ({"address": "vault.example-test.ts.net"}, "not an IP address"),
    ],
)
def test_a_node_that_is_not_the_approved_one_is_refused_before_anything_is_sent(
    changes: dict[str, Any], problem: str
) -> None:
    resolver = FakeResolver(peer(**changes))
    log = Recorder()
    connection = tailnet_client(resolver, log=log)

    error = refused(
        "STORAGE.IDENTITY_MISMATCH", lambda: connection.ref_store().read(HEAD_REF), integrity=True
    )

    assert problem in str(error)
    assert resolver.calls == 1
    # The attempt is recorded, with no answer and no node: nothing was sent.
    assert [(record.status, record.code, record.node_stable_id) for record in log.records] == [
        (None, "STORAGE.IDENTITY_MISMATCH", None)
    ]


def test_an_endpoint_outside_the_approved_tailnet_is_refused() -> None:
    resolver = FakeResolver(peer(dns_name="voice-lab-vault.other-tailnet.ts.net."))
    connection = tailnet_client(
        resolver,
        endpoint="voice-lab-vault.other-tailnet.ts.net",
        expected=identity(),
    )

    error = refused(
        "STORAGE.IDENTITY_MISMATCH",
        lambda: connection.blob_store().has(HELLO_REF),
        integrity=True,
    )

    assert f"is not a name in the tailnet {SUFFIX}" in str(error)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://voice-lab-vault.example-test.ts.net",
        "https://voice-lab-vault.example-test.ts.net",
        "100.64.12.34",
        "127.0.0.1",
        "Voice-Lab-Vault.example-test.ts.net",
        "voice-lab-vault.example-test.ts.net.",
        "voice-lab-vault.example-test.ts.net:443",
        "vault",
        "",
    ],
)
def test_an_endpoint_that_is_not_a_tailnet_name_builds_no_connection(endpoint: str) -> None:
    with pytest.raises(StorageNameError, match="tailnet-vault endpoint"):
        tailnet_client(FakeResolver(peer()), endpoint=endpoint)


def test_the_identity_is_checked_again_only_once_it_is_old() -> None:
    with ScriptedServer([]) as closed:
        port = closed.port
    resolver = FakeResolver(peer(address=LOOPBACK_HOST, port=port))
    clock = iter([0.0, 10.0, 59.9, 60.0, 60.0])
    connection = tailnet_client(resolver, monotonic=lambda: next(clock), timeout=1.0)
    store = connection.blob_store()

    # Nothing listens at the checked address, so each call fails after its check.
    for _ in range(4):
        refused("STORAGE.UNAVAILABLE", lambda: store.has(HELLO_REF))

    assert resolver.calls == 2
    assert resolver.endpoints == [ENDPOINT, ENDPOINT]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"node_stable_id": "node!"}, "stable id"),
        ({"node_stable_id": ""}, "stable id"),
        ({"tag": "voice-lab-vault"}, "tailnet tag"),
        ({"tailnet_suffix": "ts"}, "tailnet-vault endpoint"),
        ({"tailnet_suffix": "100.64.0.1"}, "tailnet-vault endpoint"),
    ],
)
def test_an_identity_that_is_not_of_its_form_is_refused(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(StorageNameError, match=message):
        identity(**changes)


# -- the TLS path ---------------------------------------------------------------------------


def certificate_authority(directory: Path, names: Sequence[str]) -> tuple[Path, dict[str, Path]]:
    """A throwaway authority and one server certificate per name, written under ``directory``.

    Nothing here is a credential of anything: the keys live for one test in ``tmp_path``.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(UTC)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "throwaway test CA")])
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(authority_key, hashes.SHA256())
    )
    authority_path = directory / "throwaway-ca.pem"
    authority_path.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    authority_path.chmod(0o600)
    chains: dict[str, Path] = {}
    for name in names:
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(authority_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(authority_key, hashes.SHA256())
        )
        chain = directory / f"{name}.pem"
        chain.write_bytes(
            certificate.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        chain.chmod(0o600)
        chains[name] = chain
    return authority_path, chains


def tls_servers(chain: Path, seen: list[str | None]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain)

    def record(connection: ssl.SSLObject, name: str | None, _context: ssl.SSLContext) -> None:
        seen.append(name)

    context.sni_callback = record
    return context


def test_the_certificate_is_verified_for_the_endpoint_while_the_socket_goes_to_the_address(
    tmp_path: Path,
) -> None:
    authority, chains = certificate_authority(tmp_path, [ENDPOINT])
    seen: list[str | None] = []
    with ScriptedServer(
        [Reply(raw=refusal(404, "STORAGE.NOT_FOUND"))], context=tls_servers(chains[ENDPOINT], seen)
    ) as server:
        resolver = FakeResolver(peer(address=LOOPBACK_HOST, port=server.port))
        connection = tailnet_client(
            resolver, context=lambda: ssl.create_default_context(cafile=str(authority))
        )

        assert connection.ref_store().read(HEAD_REF) is None

    # The name the certificate was verified for is the endpoint, not the address dialled.
    assert seen == [ENDPOINT]
    assert server.requests[0].header("Host") == ENDPOINT
    assert server.requests[0].target == f"/v1/{NAMESPACE}/refs/{HEAD_REF}"


def test_a_certificate_for_another_name_is_refused(tmp_path: Path) -> None:
    other = f"other.{SUFFIX}"
    authority, chains = certificate_authority(tmp_path, [other])
    with ScriptedServer(
        [Reply(raw=refusal(404, "STORAGE.NOT_FOUND"))], context=tls_servers(chains[other], [])
    ) as server:
        resolver = FakeResolver(peer(address=LOOPBACK_HOST, port=server.port))
        connection = tailnet_client(
            resolver, context=lambda: ssl.create_default_context(cafile=str(authority))
        )

        error = refused("STORAGE.UNAVAILABLE", lambda: connection.ref_store().read(HEAD_REF))

    assert "SSLCertVerificationError" in str(error)
    assert error.retryable
    assert SYNTHETIC_TOKEN not in str(error)


def test_a_certificate_from_an_unknown_authority_is_refused(tmp_path: Path) -> None:
    _authority, chains = certificate_authority(tmp_path, [ENDPOINT])
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    stranger, _chains = certificate_authority(elsewhere, [ENDPOINT])
    with ScriptedServer(
        [Reply(raw=refusal(404, "STORAGE.NOT_FOUND"))], context=tls_servers(chains[ENDPOINT], [])
    ) as server:
        resolver = FakeResolver(peer(address=LOOPBACK_HOST, port=server.port))
        connection = tailnet_client(
            resolver, context=lambda: ssl.create_default_context(cafile=str(stranger))
        )

        error = refused("STORAGE.UNAVAILABLE", lambda: connection.ref_store().read(HEAD_REF))

    assert "SSLCertVerificationError" in str(error)


# -- the loopback constructor ---------------------------------------------------------------


@pytest.mark.parametrize("address", ["10.0.0.1", "localhost", "", "127.0.0.2"])
def test_a_loopback_connection_reaches_nothing_but_the_loopback(address: str) -> None:
    with pytest.raises(StorageNameError, match="loopback connection reaches"):
        VaultConnection.loopback_for_tests(
            8080, NAMESPACE, lambda: SYNTHETIC_TOKEN, address=address
        )


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_a_loopback_connection_needs_a_port(port: int) -> None:
    with pytest.raises(StorageNameError, match="needs a port"):
        VaultConnection.loopback_for_tests(port, NAMESPACE, lambda: SYNTHETIC_TOKEN)


def test_an_ipv6_loopback_connection_brackets_its_host() -> None:
    connection = VaultConnection.loopback_for_tests(
        8080, NAMESPACE, lambda: SYNTHETIC_TOKEN, address=LOOPBACK_ADDRESSES[1]
    )

    assert connection.endpoint == "[::1]:8080"
    assert connection.namespace == NAMESPACE


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"namespace": "Cole"}, "a namespace is"),
        ({"namespace": ""}, "a namespace is"),
        ({"attempts": 0}, "is sent 1 to 16 times"),
        ({"attempts": 17}, "is sent 1 to 16 times"),
        ({"timeout": 0.0}, "positive timeout"),
    ],
)
def test_a_connection_outside_its_bounds_is_refused(options: dict[str, Any], message: str) -> None:
    arguments: dict[str, Any] = {"namespace": NAMESPACE} | options
    namespace = cast(str, arguments.pop("namespace"))
    with pytest.raises((StorageNameError, ValueError), match=message):
        VaultConnection.loopback_for_tests(8080, namespace, lambda: SYNTHETIC_TOKEN, **arguments)


def test_a_negative_identity_age_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot stand for"):
        VaultConnection(
            ENDPOINT,
            NAMESPACE,
            lambda: SYNTHETIC_TOKEN,
            FakeResolver(peer()),
            identity(),
            identity_max_age=-1.0,
        )


def test_a_connection_shows_its_endpoint_and_namespace_and_never_its_token() -> None:
    connection = VaultConnection.loopback_for_tests(8080, NAMESPACE, lambda: SYNTHETIC_TOKEN)

    assert repr(connection) == "VaultConnection(endpoint='127.0.0.1:8080', namespace='cole')"
    assert SYNTHETIC_TOKEN not in repr(connection)


# -- the happy paths against a real vault ---------------------------------------------------


def test_blobs_and_refs_travel_whole_over_the_wire(tmp_path: Path) -> None:
    with running_vault(tmp_path) as vault:
        connection = vault_client(vault)
        blobs = connection.blob_store()
        refs = connection.ref_store()

        ref = blobs.put(HELLO)
        assert blobs.has(ref)
        assert blobs.get(ref) == HELLO
        assert blobs.put(HELLO) == ref
        assert refs.read(HEAD_REF) is None
        first = refs.swap(HEAD_REF, expected_version=None, target=ref)
        assert (first.name, first.version, first.target) == (HEAD_REF, 1, ref)
        assert refs.read(HEAD_REF) == first
        empty = blobs.put(b"")
        second = refs.swap(HEAD_REF, expected_version=1, target=empty)
        assert second.version == 2
        assert blobs.get(empty) == b""


def test_a_blob_beyond_the_limit_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    with running_vault(tmp_path) as vault:
        log = Recorder()
        blobs = vault_client(vault, log=log).blob_store()

        error = refused("STORAGE.TOO_LARGE", lambda: blobs.put(b"\x5a" * (MAX_BLOB_BYTES + 1)))

    assert str(MAX_BLOB_BYTES) in str(error)
    assert log.records == []


def test_a_quota_a_blob_does_not_fit_is_reported_as_such(tmp_path: Path) -> None:
    with running_vault(tmp_path, quota=16) as vault:
        blobs = vault_client(vault).blob_store()

        refused("STORAGE.QUOTA", lambda: blobs.put(b"\x5a" * 32))


def test_a_stale_expected_version_is_a_conflict_that_names_the_ref(tmp_path: Path) -> None:
    with running_vault(tmp_path) as vault:
        connection = vault_client(vault)
        refs = connection.ref_store()
        target = connection.blob_store().put(HELLO)
        refs.swap(HEAD_REF, expected_version=None, target=target)

        error = refused(
            "STORAGE.CONFLICT",
            lambda: refs.swap(HEAD_REF, expected_version=None, target=target),
        )

    assert HEAD_REF in str(error)


def test_a_missing_blob_is_not_found_and_names_its_digest(tmp_path: Path) -> None:
    with running_vault(tmp_path) as vault:
        blobs = vault_client(vault).blob_store()
        missing = BlobRef(sha256="a" * 64, size=7)

        assert not blobs.has(missing)
        error = refused("STORAGE.NOT_FOUND", lambda: blobs.get(missing))

    assert "a" * 64 in str(error)


@pytest.mark.parametrize("name", ["", "Libraries/x", "libraries//head", "libraries/../head"])
def test_an_invalid_ref_name_never_reaches_the_wire(tmp_path: Path, name: str) -> None:
    with running_vault(tmp_path) as vault:
        log = Recorder()
        connection = vault_client(vault, log=log)
        refs = connection.ref_store()
        target = connection.blob_store().put(HELLO)
        sent = len(log.records)

        with pytest.raises(StorageNameError):
            refs.read(name)
        with pytest.raises(StorageNameError):
            refs.swap(name, expected_version=None, target=target)

    assert len(log.records) == sent


def test_a_forged_blob_ref_never_reaches_the_wire(tmp_path: Path) -> None:
    with running_vault(tmp_path) as vault:
        log = Recorder()
        connection = vault_client(vault, log=log)
        blobs = connection.blob_store()
        forged = BlobRef.model_construct(sha256="../../outside", size=1)

        refused("STORAGE.PROTOCOL", lambda: blobs.get(forged))
        refused("STORAGE.PROTOCOL", lambda: blobs.has(forged))
        refused(
            "STORAGE.PROTOCOL",
            lambda: connection.ref_store().swap(HEAD_REF, expected_version=None, target=forged),
        )

    assert log.records == []


def test_a_ref_that_is_no_blob_ref_at_all_is_refused() -> None:
    class NotARef:
        sha256 = "a" * 64
        size = 1

    connection = VaultConnection.loopback_for_tests(8080, NAMESPACE, lambda: SYNTHETIC_TOKEN)
    store = connection.blob_store()

    error = refused("STORAGE.PROTOCOL", lambda: store.has(cast(BlobRef, NotARef())))

    assert "not a valid blob ref" in str(error)


# -- credentials ----------------------------------------------------------------------------


def test_a_token_the_vault_does_not_know_is_refused_once(tmp_path: Path) -> None:
    with running_vault(tmp_path) as vault:
        log = Recorder()
        blobs = vault_client(vault, token=new_token(), log=log).blob_store()

        error = refused("STORAGE.UNAUTHORIZED", lambda: blobs.put(HELLO), integrity=True)

    assert not error.retryable
    assert [record.status for record in log.records] == [401]  # never retried


def test_a_token_of_another_namespace_is_refused_once(tmp_path: Path) -> None:
    with running_vault(tmp_path) as vault:
        log = Recorder()
        blobs = vault_client(vault, token=vault.write_token(OTHER_NAMESPACE), log=log).blob_store()

        refused("STORAGE.UNAUTHORIZED", lambda: blobs.put(HELLO), integrity=True)

    assert [(record.status, record.attempt) for record in log.records] == [(401, 1)]


def test_a_read_token_cannot_write(tmp_path: Path) -> None:
    with running_vault(tmp_path) as vault:
        read_token = vault.tokens[NAMESPACE][0]
        connection = vault_client(vault, token=read_token)

        assert connection.ref_store().read(HEAD_REF) is None
        refused("STORAGE.UNAUTHORIZED", lambda: connection.blob_store().put(HELLO), integrity=True)


def test_the_token_reaches_the_authorization_header_and_nothing_else(tmp_path: Path) -> None:
    log = Recorder()
    with ScriptedServer([Reply(raw=json_answer(201, HELLO_REF.model_dump(mode="json")))]) as server:
        blobs = scripted_client(server, log=log).blob_store()
        with suppress(StorageError):
            blobs.put(HELLO)

    request = server.requests[0]
    assert request.header("Authorization") == f"Bearer {SYNTHETIC_TOKEN}"
    assert SYNTHETIC_TOKEN not in request.target
    assert SYNTHETIC_TOKEN not in log.text
    assert "Authorization" not in log.text


@pytest.mark.parametrize(
    "token",
    [
        # The shape a token read from the Keychain or a secret file has before it is stripped.
        SYNTHETIC_TOKEN + "\n",
        SYNTHETIC_TOKEN + "\r\n",
        # A character no header value may carry, and one no latin-1 header encoding accepts.
        SYNTHETIC_TOKEN[:-1] + "\x00",
        SYNTHETIC_TOKEN[:-1] + "é",
        f"Bearer {SYNTHETIC_TOKEN}",
        SYNTHETIC_TOKEN + SYNTHETIC_TOKEN,
        "",
    ],
)
def test_a_token_that_is_not_one_token_is_refused_before_it_can_become_a_header(
    token: str,
) -> None:
    """A value the header writer would quote back verbatim never reaches it."""
    log = Recorder()
    with ScriptedServer([Reply(raw=refusal(404, "STORAGE.NOT_FOUND"))]) as server:
        refs = VaultConnection.loopback_for_tests(
            server.port,
            NAMESPACE,
            lambda: token,
            request_log=log,
            attempts=1,
            timeout=5.0,
            sleep=Waits(),
            clock=lambda: FIXED_TIME,
        ).ref_store()

        error = refused("STORAGE.UNAUTHORIZED", lambda: refs.read(HEAD_REF), integrity=True)

        assert server.requests == []

    assert not error.retryable
    assert not token or (token not in str(error) and token not in repr(error))
    assert SYNTHETIC_TOKEN not in str(error) and SYNTHETIC_TOKEN not in log.text
    assert [(record.status, record.code) for record in log.records] == [
        (None, "STORAGE.UNAUTHORIZED")
    ]


# -- faults ---------------------------------------------------------------------------------


def test_a_connection_dropped_mid_upload_is_retried_and_then_reported(tmp_path: Path) -> None:
    waits = Waits()
    log = Recorder()
    payload = b"\x5a" * (256 * 1024)
    with ScriptedServer([Reply(drop_after=4096) for _ in range(4)]) as server:
        blobs = scripted_client(server, log=log, waits=waits).blob_store()

        error = refused("STORAGE.UNAVAILABLE", lambda: blobs.put(payload))

    assert error.retryable
    assert [record.attempt for record in log.records] == [1, 2, 3, 4]
    assert {record.status for record in log.records} == {None}
    assert {record.code for record in log.records} == {"STORAGE.UNAVAILABLE"}
    assert len(waits.seconds) == 3 and all(0 < wait <= 4 for wait in waits.seconds)
    assert len(server.requests) == 4


def test_a_server_failure_that_passes_by_itself_is_retried_until_it_does(tmp_path: Path) -> None:
    waits = Waits()
    log = Recorder()
    with ScriptedServer(
        [
            Reply(raw=refusal(503, "STORAGE.UNAVAILABLE")),
            Reply(raw=refusal(503, "STORAGE.UNAVAILABLE")),
            Reply(raw=json_answer(201, HELLO_REF.model_dump(mode="json"))),
        ]
    ) as server:
        blobs = scripted_client(server, log=log, waits=waits).blob_store()

        assert blobs.put(HELLO) == HELLO_REF

    assert [(record.attempt, record.status) for record in log.records] == [
        (1, 503),
        (2, 503),
        (3, 201),
    ]
    # The body was sent whole on every attempt, refused or not.
    assert [record.bytes for record in log.records] == [len(HELLO)] * 3
    assert len(waits.seconds) == 2


def test_a_refusal_that_will_not_pass_by_itself_is_never_retried() -> None:
    log = Recorder()
    with ScriptedServer([Reply(raw=refusal(507, "STORAGE.QUOTA"))]) as server:
        blobs = scripted_client(server, log=log).blob_store()

        refused("STORAGE.QUOTA", lambda: blobs.put(HELLO))

    assert len(log.records) == 1


def test_a_vault_that_answers_other_bytes_than_the_blob_is_refused() -> None:
    with ScriptedServer([Reply(raw=blob_answer(b"x" * len(HELLO)))]) as server:
        blobs = scripted_client(server).blob_store()

        error = refused("STORAGE.INTEGRITY", lambda: blobs.get(HELLO_REF), integrity=True)

    assert "bytes that are not it" in str(error)


def test_a_blob_answer_that_stops_early_is_a_dropped_connection_not_a_damaged_copy() -> None:
    """A connection that ended mid-answer may pass by itself; the vault's copy is not judged."""
    with ScriptedServer([Reply(raw=blob_answer(HELLO[:3], length=len(HELLO)))]) as server:
        blobs = scripted_client(server, attempts=1).blob_store()

        error = refused("STORAGE.UNAVAILABLE", lambda: blobs.get(HELLO_REF))

    assert error.retryable
    assert "ended before its declared length" in str(error)


def test_a_blob_answer_that_arrives_whole_and_wrong_is_a_damaged_copy() -> None:
    with ScriptedServer([Reply(raw=blob_answer(b"x" * len(HELLO)))]) as server:
        blobs = scripted_client(server, attempts=1).blob_store()

        error = refused("STORAGE.INTEGRITY", lambda: blobs.get(HELLO_REF), integrity=True)

    assert not error.retryable


def test_a_blob_answer_that_declares_another_length_is_refused() -> None:
    with ScriptedServer([Reply(raw=blob_answer(HELLO, length=len(HELLO) + 10))]) as server:
        blobs = scripted_client(server).blob_store()

        error = refused("STORAGE.PROTOCOL", lambda: blobs.get(HELLO_REF))

    assert "Content-Length" in str(error)


def test_a_head_answer_that_declares_another_size_is_refused() -> None:
    with ScriptedServer([Reply(raw=answer(200, b"", content_type="x", length=1))]) as server:
        blobs = scripted_client(server).blob_store()

        refused("STORAGE.PROTOCOL", lambda: blobs.has(HELLO_REF))


def test_a_json_answer_beyond_its_limit_is_refused() -> None:
    body = json.dumps({"filler": "x" * (MAX_JSON_BODY_BYTES + 1000)}).encode()
    with ScriptedServer(
        [Reply(raw=answer(200, body, content_type="application/json; charset=utf-8"))]
    ) as server:
        refs = scripted_client(server).ref_store()

        error = refused("STORAGE.PROTOCOL", lambda: refs.read(HEAD_REF))

    assert str(MAX_JSON_BODY_BYTES) in str(error)


def test_an_answer_that_is_no_contract_at_all_is_refused() -> None:
    with ScriptedServer(
        [Reply(raw=answer(200, b"{}", content_type="application/json; charset=utf-8"))]
    ) as server:
        refs = scripted_client(server).ref_store()

        error = refused("STORAGE.INTEGRITY", lambda: refs.read(HEAD_REF), integrity=True)

    assert "not a RefValue" in str(error)


def test_a_value_that_names_another_ref_is_refused() -> None:
    with ScriptedServer([Reply(raw=ref_answer("libraries/other/head", 1, HELLO_REF))]) as server:
        refs = scripted_client(server).ref_store()

        error = refused("STORAGE.INTEGRITY", lambda: refs.read(HEAD_REF), integrity=True)

    assert "the value of another ref" in str(error)
    assert "libraries/other/head" not in str(error)


def test_a_put_the_vault_answers_with_another_ref_is_refused() -> None:
    other = BlobRef(sha256="b" * 64, size=len(HELLO))
    with ScriptedServer([Reply(raw=json_answer(201, other.model_dump(mode="json")))]) as server:
        blobs = scripted_client(server).blob_store()

        error = refused("STORAGE.INTEGRITY", lambda: blobs.put(HELLO), integrity=True)

    assert HELLO_REF.sha256 in str(error)
    assert "b" * 64 not in str(error)


def test_a_swap_the_vault_answers_with_another_version_is_refused() -> None:
    with ScriptedServer([Reply(raw=ref_answer(HEAD_REF, 9, HELLO_REF))]) as server:
        refs = scripted_client(server).ref_store()

        error = refused(
            "STORAGE.INTEGRITY",
            lambda: refs.swap(HEAD_REF, expected_version=None, target=HELLO_REF),
            integrity=True,
        )

    assert "version 9" in str(error)


def test_a_slow_vault_times_out_and_the_wait_is_retryable() -> None:
    log = Recorder()
    with ScriptedServer(
        [Reply(raw=refusal(404, "STORAGE.NOT_FOUND"), delay=2.0) for _ in range(2)]
    ) as server:
        refs = scripted_client(server, log=log, attempts=2, timeout=0.2).ref_store()

        error = refused("STORAGE.TIMEOUT", lambda: refs.read(HEAD_REF))

    assert error.retryable
    assert "within 0.2 seconds" in str(error)
    assert [record.code for record in log.records] == ["STORAGE.TIMEOUT"] * 2


def test_a_vault_that_drips_its_answer_runs_out_of_one_budget_not_one_per_byte() -> None:
    """A socket timeout bounds one read; the exchange's own deadline bounds all of them."""
    framed = blob_answer(HELLO)
    drip = 0.3
    timeout = 0.5
    with ScriptedServer(
        [Reply(raw=framed, drip_after=len(framed) - len(HELLO), drip=drip)]
    ) as server:
        blobs = scripted_client(server, attempts=1, timeout=timeout).blob_store()
        started = time.monotonic()

        error = refused("STORAGE.TIMEOUT", lambda: blobs.get(HELLO_REF))

        spent = time.monotonic() - started

    assert error.retryable
    # Well under the drip's own length: the body alone would take len(HELLO) * drip seconds.
    assert spent < len(HELLO) * drip, spent


def test_a_deadline_that_is_already_spent_allows_no_further_wait() -> None:
    spent = storage_http_client._Deadline(at=time.monotonic() - 1.0)

    with socket.socket() as opened, pytest.raises(TimeoutError, match="within its deadline"):
        spent.bound(opened)


def test_a_request_the_writer_refuses_names_its_class_and_repeats_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last guard: what a header writer refuses may be the live token, so it is not said."""
    original = http.client.HTTPConnection.putheader

    def refuse(self: Any, header: str, *values: Any) -> None:
        if header == "Authorization":
            raise ValueError(f"Invalid header value {values[0]!r}")
        original(self, header, *values)

    monkeypatch.setattr(http.client.HTTPConnection, "putheader", refuse)
    with ScriptedServer([Reply(raw=refusal(404, "STORAGE.NOT_FOUND"))]) as server:
        refs = scripted_client(server, attempts=1).ref_store()

        error = refused("STORAGE.PROTOCOL", lambda: refs.read(HEAD_REF))

    assert "ValueError" in str(error)
    assert SYNTHETIC_TOKEN not in str(error) and SYNTHETIC_TOKEN not in repr(error)


def test_a_vault_that_answers_nothing_at_all_is_unreachable() -> None:
    with ScriptedServer([Reply(raw=None)]) as server:
        refs = scripted_client(server, attempts=1).ref_store()

        error = refused("STORAGE.UNAVAILABLE", lambda: refs.read(HEAD_REF))

    assert "could not be reached" in str(error)


def test_a_vault_that_is_not_listening_is_unreachable() -> None:
    with ScriptedServer([]) as server:
        port = server.port
    refs = VaultConnection.loopback_for_tests(
        port, NAMESPACE, lambda: SYNTHETIC_TOKEN, attempts=1, timeout=1.0
    ).ref_store()

    refused("STORAGE.UNAVAILABLE", lambda: refs.read(HEAD_REF))


@pytest.mark.parametrize(
    ("body", "status", "expected"),
    [
        (b"<html>bad gateway</html>", 502, "STORAGE.UNAVAILABLE"),
        (b'{"code": "STORAGE.WEIRD", "message": "no"}', 400, "STORAGE.PROTOCOL"),
        (b'{"code": 7, "message": "no"}', 409, "STORAGE.CONFLICT"),
        # A 500 whose body this cannot read decided nothing about the stored bytes, so it is
        # not read as damage on a method whose refusal does carry a body.
        (b"not json at all", 500, "STORAGE.PROTOCOL"),
        (b'{"code": "STORAGE.INTEGRITY", "message": "no"}', 500, "STORAGE.INTEGRITY"),
        (b'["a list"]', 418, "STORAGE.PROTOCOL"),
        (b"", 204, "STORAGE.PROTOCOL"),
        # Deeper than any JSON parser goes, and well inside the 64 KiB a refusal may carry.
        (b"[" * 40000, 409, "STORAGE.CONFLICT"),
    ],
)
def test_every_answer_maps_to_one_storage_failure(body: bytes, status: int, expected: str) -> None:
    log = Recorder()
    with ScriptedServer([Reply(raw=answer(status, body, content_type="text/plain"))]) as server:
        refs = scripted_client(server, log=log, attempts=1).ref_store()

        error = refused(
            expected, lambda: refs.read(HEAD_REF), integrity=expected in INTEGRITY_CODES
        )

    assert str(status) in str(error)
    # Recorded too: a record of the answer is built for every one of these, log attached.
    assert [record.status for record in log.records] == [status]


@pytest.mark.parametrize("status", [600, 700, 999])
def test_a_status_that_is_no_http_status_is_refused_and_recorded_without_one(status: int) -> None:
    """``http.client`` reads a status line up to 999; a record of one above 599 cannot exist."""
    log = Recorder()
    with ScriptedServer([Reply(raw=answer(status, b"", content_type="text/plain"))]) as server:
        refs = scripted_client(server, log=log, attempts=1).ref_store()

        error = refused("STORAGE.PROTOCOL", lambda: refs.read(HEAD_REF))

    assert f"status {status}" in str(error)
    assert [(record.status, record.code) for record in log.records] == [(None, "STORAGE.PROTOCOL")]


def test_a_refused_head_is_read_from_its_status_because_it_can_carry_no_code() -> None:
    """The one answer with no body: a 500 for a ``has`` is the fail-closed reading."""
    with ScriptedServer([Reply(raw=answer(500, b"", content_type="text/plain"))]) as server:
        blobs = scripted_client(server, attempts=1).blob_store()

        refused("STORAGE.INTEGRITY", lambda: blobs.has(HELLO_REF), integrity=True)


def test_an_answer_too_deep_to_parse_is_no_contract() -> None:
    body = b"[" * 40000
    with ScriptedServer(
        [Reply(raw=answer(200, body, content_type="application/json; charset=utf-8"))]
    ) as server:
        refs = scripted_client(server, attempts=1).ref_store()

        refused("STORAGE.INTEGRITY", lambda: refs.read(HEAD_REF), integrity=True)


def test_a_refusal_omits_the_vaults_own_sentence_when_it_has_one() -> None:
    with ScriptedServer(
        [Reply(raw=refusal(409, "STORAGE.CONFLICT", "the ref is not at the expected version"))]
    ) as server:
        refs = scripted_client(server, attempts=1).ref_store()

        error = refused(
            "STORAGE.CONFLICT",
            lambda: refs.swap(HEAD_REF, expected_version=1, target=HELLO_REF),
        )

    assert "the ref is not at the expected version" not in str(error)
    assert f"swap-ref {HEAD_REF}" in str(error)


@pytest.mark.parametrize(
    ("remote", "code", "operation"),
    [
        (refusal(401, "STORAGE.UNAUTHORIZED", SYNTHETIC_TOKEN), "STORAGE.UNAUTHORIZED", "read"),
        (json_answer(200, {"name": SYNTHETIC_TOKEN}), "STORAGE.INTEGRITY", "read"),
        (ref_answer(SYNTHETIC_TOKEN, 1, HELLO_REF), "STORAGE.INTEGRITY", "read"),
        (
            answer(200, HELLO, content_type="x").replace(
                f"Content-Length: {len(HELLO)}".encode(),
                f"Content-Length: {SYNTHETIC_TOKEN}".encode(),
            ),
            "STORAGE.PROTOCOL",
            "get",
        ),
        (SYNTHETIC_TOKEN.encode() + b"\r\n", "STORAGE.UNAVAILABLE", "read"),
        (OSError(SYNTHETIC_TOKEN), "STORAGE.UNAVAILABLE", "read"),
        (TimeoutError(SYNTHETIC_TOKEN), "STORAGE.TIMEOUT", "read"),
        (ValueError(SYNTHETIC_TOKEN), "STORAGE.PROTOCOL", "read"),
    ],
    ids=["refusal", "invalid-contract", "other-ref", "header", "status", "os", "timeout", "value"],
)
def test_remote_diagnostics_never_reach_errors_tracebacks_or_worker_replies(
    remote: bytes | Exception,
    code: str,
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-memory responses only; the same public failure is serialized by the worker."""
    from cole_voice_lab import storage_binding
    from cole_voice_lab.library_replication_service import run_vault_pass, worker_reply_bytes
    from cole_voice_lab.paths import AssetLayout

    class MemorySocket:
        def sendall(self, _data: bytes) -> None:
            pass

        def makefile(self, *_args: Any, **_options: Any) -> io.BytesIO:
            assert isinstance(remote, bytes)
            return io.BytesIO(remote)

        def close(self) -> None:
            pass

    @contextmanager
    def connected(*_args: Any, **_options: Any) -> Iterator[http.client.HTTPConnection]:
        connection = http.client.HTTPConnection("127.0.0.1")
        connection.sock = cast(socket.socket, MemorySocket())
        if isinstance(remote, Exception):

            def failed_response() -> NoReturn:
                raise remote

            monkeypatch.setattr(connection, "getresponse", failed_response)
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(VaultConnection, "_connected", connected)
    log = Recorder()
    client = VaultConnection.loopback_for_tests(
        1, NAMESPACE, lambda: SYNTHETIC_TOKEN, attempts=1, request_log=log
    )
    action = (
        (lambda: client.blob_store().get(HELLO_REF))
        if operation == "get"
        else (lambda: client.ref_store().read(HEAD_REF))
    )
    error = refused(code, action, integrity=code in INTEGRITY_CODES)

    def refused_open(_layout: AssetLayout) -> NoReturn:
        raise error

    monkeypatch.setattr(storage_binding, "open_library_vault", refused_open)
    layout = AssetLayout.initialize(tmp_path.resolve() / "worker-library" / "assets")
    reply = run_vault_pass(layout, binding_sha256="a" * 64)

    assert reply.code == code
    assert reply.retryable == error.retryable
    assert reply.message == str(error)
    public = str(error) + repr(error) + "".join(traceback.format_exception(error))
    public += worker_reply_bytes(reply).decode() + log.text
    assert SYNTHETIC_TOKEN not in public
    assert client.endpoint in str(error)
    assert log.records[-1].code == code or code == "STORAGE.INTEGRITY"


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "STORAGE.CONFLICT"},
        {"code": "STORAGE.CONFLICT", "message": 7},
        {"code": "STORAGE.CONFLICT", "message": ""},
        {"code": "STORAGE.CONFLICT", "message": "one\nthen another"},
    ],
)
def test_a_refusal_without_a_sentence_of_its_own_still_names_the_call(
    payload: dict[str, Any],
) -> None:
    with ScriptedServer([Reply(raw=json_answer(409, payload))]) as server:
        refs = scripted_client(server, attempts=1).ref_store()

        error = refused(
            "STORAGE.CONFLICT",
            lambda: refs.swap(HEAD_REF, expected_version=1, target=HELLO_REF),
        )

    assert f"swap-ref {HEAD_REF}" in str(error)


# -- the swap that was won by an attempt whose answer was lost -------------------------------


def test_a_retried_swap_that_the_first_attempt_won_is_reported_as_the_win() -> None:
    log = Recorder()
    with ScriptedServer(
        [
            Reply(raw=None),  # the winning attempt, whose answer never arrived
            Reply(raw=refusal(409, "STORAGE.CONFLICT")),
            Reply(raw=ref_answer(HEAD_REF, 3, HELLO_REF)),
        ]
    ) as server:
        refs = scripted_client(server, log=log).ref_store()

        value = refs.swap(HEAD_REF, expected_version=2, target=HELLO_REF)

    assert (value.name, value.version, value.target) == (HEAD_REF, 3, HELLO_REF)
    assert [record.operation for record in log.records] == [
        "swap-ref",
        "swap-ref",
        "read-ref",
    ]


def test_a_retried_swap_that_someone_else_won_is_a_conflict() -> None:
    with ScriptedServer(
        [
            Reply(raw=None),
            Reply(raw=refusal(409, "STORAGE.CONFLICT")),
            Reply(raw=ref_answer(HEAD_REF, 3, BlobRef(sha256="c" * 64, size=2))),
        ]
    ) as server:
        refs = scripted_client(server).ref_store()

        refused(
            "STORAGE.CONFLICT",
            lambda: refs.swap(HEAD_REF, expected_version=2, target=HELLO_REF),
        )


def test_a_retried_swap_whose_ref_is_gone_is_a_conflict() -> None:
    with ScriptedServer(
        [
            Reply(raw=None),
            Reply(raw=refusal(409, "STORAGE.CONFLICT")),
            Reply(raw=refusal(404, "STORAGE.NOT_FOUND")),
        ]
    ) as server:
        refs = scripted_client(server).ref_store()

        refused(
            "STORAGE.CONFLICT",
            lambda: refs.swap(HEAD_REF, expected_version=2, target=HELLO_REF),
        )


def test_a_swap_refused_for_another_reason_after_a_retry_is_that_reason() -> None:
    with ScriptedServer(
        [Reply(raw=None), Reply(raw=refusal(401, "STORAGE.UNAUTHORIZED"))]
    ) as server:
        refs = scripted_client(server).ref_store()

        refused(
            "STORAGE.UNAUTHORIZED",
            lambda: refs.swap(HEAD_REF, expected_version=2, target=HELLO_REF),
            integrity=True,
        )


def test_a_swap_a_first_attempt_won_from_nothing_is_reported_as_the_win() -> None:
    with ScriptedServer(
        [
            Reply(raw=None),
            Reply(raw=refusal(409, "STORAGE.CONFLICT")),
            Reply(raw=ref_answer(HEAD_REF, 1, HELLO_REF)),
        ]
    ) as server:
        refs = scripted_client(server).ref_store()

        value = refs.swap(HEAD_REF, expected_version=None, target=HELLO_REF)

    assert value.version == 1


# -- the request log ------------------------------------------------------------------------


def test_every_attempt_is_recorded_and_no_record_holds_a_token(tmp_path: Path) -> None:
    log = Recorder()
    with running_vault(tmp_path) as vault:
        connection = vault_client(vault, log=log)
        blobs = connection.blob_store()
        ref = blobs.put(HELLO)
        blobs.has(ref)
        blobs.get(ref)
        connection.ref_store().swap(HEAD_REF, expected_version=None, target=ref)
        connection.ref_store().read(HEAD_REF)
        token = vault.write_token()

    assert [record.operation for record in log.records] == [
        "put-blob",
        "has-blob",
        "get-blob",
        "swap-ref",
        "read-ref",
    ]
    assert [record.bytes for record in log.records] == [len(HELLO), 0, len(HELLO), 0, 0]
    assert {record.attempt for record in log.records} == {1}
    assert {record.status for record in log.records} == {200, 201}
    assert {record.code for record in log.records} == {None}
    assert {record.namespace for record in log.records} == {NAMESPACE}
    assert {record.node_stable_id for record in log.records} == {None}
    # This client keeps the default clock, so the records carry real instants, in order.
    times = [record.time for record in log.records]
    assert times == sorted(times) and all(time.tzinfo is UTC for time in times)
    assert token not in log.text and hash_token(token) not in log.text


def test_a_refused_blob_call_records_no_blob_bytes(tmp_path: Path) -> None:
    """A refusal's JSON body is not a blob byte, and the vault's own record counts none."""
    log = Recorder()
    with running_vault(tmp_path) as vault:
        blobs = vault_client(vault, log=log).blob_store()
        missing = BlobRef(sha256="a" * 64, size=7)

        refused("STORAGE.NOT_FOUND", lambda: blobs.get(missing))

    assert [(record.operation, record.status, record.bytes) for record in log.records] == [
        ("get-blob", 404, 0)
    ]
    # The Mac's record and the vault's access record are the pair Step 9's audit compares.
    served = [
        json.loads(line)
        for line in (tmp_path / "access.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(entry["status"], entry["bytes"]) for entry in served] == [(404, 0)]


def test_a_record_of_a_tailnet_request_names_the_node_it_was_checked_against() -> None:
    log = Recorder()
    resolver = FakeResolver(peer(address=LOOPBACK_HOST, port=1))
    connection = tailnet_client(resolver, log=log)

    refused("STORAGE.UNAVAILABLE", lambda: connection.ref_store().read(HEAD_REF))

    assert [record.node_stable_id for record in log.records] == [NODE_ID]
    assert [record.endpoint for record in log.records] == [ENDPOINT]


def test_a_connection_without_a_request_log_records_nothing() -> None:
    with ScriptedServer([Reply(raw=refusal(404, "STORAGE.NOT_FOUND"))]) as server:
        refs = VaultConnection.loopback_for_tests(
            server.port, NAMESPACE, lambda: SYNTHETIC_TOKEN, timeout=5.0
        ).ref_store()

        assert refs.read(HEAD_REF) is None


# -- the tailscale resolver, never run ------------------------------------------------------


def tailnet_status(**changes: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "CurrentTailnet": {"Name": "cole", "MagicDNSSuffix": SUFFIX, "MagicDNSEnabled": True},
        "Peer": {
            "nodekey:aaa": {
                "ID": "nodeOther",
                "DNSName": f"mac.{SUFFIX}.",
                "TailscaleIPs": ["100.64.0.2"],
            },
            "nodekey:bbb": {
                "ID": NODE_ID,
                "DNSName": f"{ENDPOINT}.",
                "TailscaleIPs": ["fd7a:115c:a1e0::1234", PEER_ADDRESS],
            },
        },
    }
    return status | changes


def whois_answer(**changes: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "StableID": NODE_ID,
        "Name": f"{ENDPOINT}.",
        "Tags": [TAG],
        "Addresses": [f"{PEER_ADDRESS}/32"],
    }
    return {"Node": node | changes, "UserProfile": {"LoginName": "tagged-devices"}}


@dataclass
class FakeRunner:
    """Recorded answers for the two commands the resolver runs; it runs nothing."""

    answers: list[Any]
    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], **options: Any) -> Any:
        self.calls.append((list(argv), options))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def completed(stdout: object, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return subprocess.CompletedProcess(args=["tailscale"], returncode=returncode, stdout=text)


def cli_resolver(answers: list[Any], **changes: Any) -> tuple[TailscaleCliResolver, FakeRunner]:
    runner = FakeRunner(answers)
    fields: dict[str, Any] = {"tailnet_suffix": SUFFIX, "runner": runner} | changes
    return TailscaleCliResolver(**fields), runner


def test_the_resolver_reads_the_node_out_of_two_recorded_answers() -> None:
    resolver, runner = cli_resolver([completed(tailnet_status()), completed(whois_answer())])

    found = resolver.resolve(ENDPOINT)

    assert found == ResolvedPeer(
        address=PEER_ADDRESS, node_stable_id=NODE_ID, tags=(TAG,), dns_name=f"{ENDPOINT}."
    )
    assert [call[0] for call in runner.calls] == [
        [str(TAILSCALE_BINARY), "status", "--json"],
        [str(TAILSCALE_BINARY), "whois", "--json", PEER_ADDRESS],
    ]
    for _argv, options in runner.calls:
        assert options["env"] == {
            "PATH": "/usr/bin:/bin",
            "HOME": tailnet._tailscale_environment()["HOME"],
        }
        assert options["timeout"] == 10.0
        assert options["check"] is False
        assert options["capture_output"] is True


def test_the_resolver_accepts_a_node_without_tags() -> None:
    resolver, _runner = cli_resolver(
        [completed(tailnet_status()), completed(whois_answer(Tags=None))]
    )

    assert resolver.resolve(ENDPOINT).tags == ()


@pytest.mark.parametrize(
    ("answers", "code", "message"),
    [
        (
            [completed(tailnet_status(CurrentTailnet={"MagicDNSSuffix": "other.ts.net"}))],
            "STORAGE.IDENTITY_MISMATCH",
            "this host is on the tailnet",
        ),
        (
            [completed(tailnet_status(Peer={}))],
            "STORAGE.IDENTITY_MISMATCH",
            "has 0 nodes named",
        ),
        (
            [
                completed(
                    tailnet_status(
                        Peer={
                            "a": {"DNSName": f"{ENDPOINT}.", "TailscaleIPs": [PEER_ADDRESS]},
                            "b": {"DNSName": f"{ENDPOINT}.", "TailscaleIPs": [PEER_ADDRESS]},
                        }
                    )
                )
            ],
            "STORAGE.IDENTITY_MISMATCH",
            "has 2 nodes named",
        ),
        (
            [
                completed(
                    tailnet_status(
                        Peer={
                            "a": {
                                "DNSName": f"{ENDPOINT}.",
                                "TailscaleIPs": ["not-an-address", "10.0.0.1", "fd7a::1"],
                            }
                        }
                    )
                )
            ],
            "STORAGE.IDENTITY_MISMATCH",
            "has no 100.64.0.0/10 address",
        ),
        ([completed("not json")], "STORAGE.PROTOCOL", "not JSON"),
        ([completed("[1, 2]")], "STORAGE.PROTOCOL", "JSON that is no object"),
        ([completed({"Peer": {}})], "STORAGE.PROTOCOL", "no CurrentTailnet object"),
        (
            [completed({"CurrentTailnet": {}, "Peer": {}})],
            "STORAGE.PROTOCOL",
            "no MagicDNSSuffix string",
        ),
        (
            [completed(tailnet_status(Peer={"a": "not an object"}))],
            "STORAGE.PROTOCOL",
            "no a object",
        ),
        (
            [completed(tailnet_status(Peer={"a": {"DNSName": 7}}))],
            "STORAGE.PROTOCOL",
            "no DNSName string",
        ),
        (
            [
                completed(
                    tailnet_status(Peer={"a": {"DNSName": f"{ENDPOINT}.", "TailscaleIPs": [7]}})
                )
            ],
            "STORAGE.PROTOCOL",
            "no TailscaleIPs list of strings",
        ),
        (
            [
                completed(
                    tailnet_status(Peer={"a": {"DNSName": f"{ENDPOINT}.", "TailscaleIPs": "x"}})
                )
            ],
            "STORAGE.PROTOCOL",
            "no TailscaleIPs list of strings",
        ),
        ([completed("x" * (MAX_TAILSCALE_OUTPUT_BYTES + 1))], "STORAGE.PROTOCOL", "more than"),
        ([completed("", returncode=1)], "STORAGE.UNAVAILABLE", "answered with status 1"),
        (
            [subprocess.TimeoutExpired(cmd="tailscale", timeout=10.0)],
            "STORAGE.TIMEOUT",
            "did not answer within",
        ),
        (
            [FileNotFoundError(2, "No such file or directory")],
            "STORAGE.UNAVAILABLE",
            "could not be run from",
        ),
        (
            [completed(tailnet_status()), completed({"UserProfile": {}})],
            "STORAGE.PROTOCOL",
            "no Node object",
        ),
        (
            [completed(tailnet_status()), completed(whois_answer(StableID=None))],
            "STORAGE.PROTOCOL",
            "no StableID string",
        ),
    ],
)
def test_an_answer_the_resolver_cannot_trust_is_refused(
    answers: list[Any], code: str, message: str
) -> None:
    resolver, _runner = cli_resolver(answers)

    error = refused(
        code,
        lambda: resolver.resolve(ENDPOINT),
        integrity=code == "STORAGE.IDENTITY_MISMATCH",
    )

    assert message in str(error)
    # Nothing of the command, its output or its environment reaches the refusal.
    assert "tailscale status --json" not in str(error)
    assert "not json" not in str(error).lower() or "not JSON" in str(error)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tailnet_suffix": "ts"}, "tailnet-vault endpoint"),
        ({"binary": Path("tailscale")}, "absolute path"),
    ],
)
def test_a_resolver_that_could_run_anything_is_refused(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(StorageNameError, match=message):
        cli_resolver([], **changes)


def test_the_default_runner_runs_the_command_it_is_given() -> None:
    """The one runner that runs anything; here it runs /bin/echo, never tailscale."""
    completed_run = tailnet._run_tailscale(
        ["/bin/echo", "recorded"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10.0,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert completed_run.returncode == 0
    assert completed_run.stdout == "recorded\n"


def test_the_tailscale_binary_is_the_platform_client() -> None:
    assert tailnet.platform_tailscale_binary("darwin") == Path(
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    )
    assert tailnet.platform_tailscale_binary("linux") == Path("/usr/bin/tailscale")
    assert TAILSCALE_BINARY == tailnet.platform_tailscale_binary()


def test_the_tailscale_environment_carries_a_home_and_a_fixed_path() -> None:
    environment = tailnet._tailscale_environment()

    assert set(environment) == {"PATH", "HOME"}
    assert environment["PATH"] == "/usr/bin:/bin"
    assert Path(environment["HOME"]).is_absolute()


# -- the loopback constructor reaches no product code ---------------------------------------


def python_sources() -> Iterator[tuple[str, ast.AST]]:
    """Every module and script of the product, parsed."""
    candidates = sorted((REPOSITORY / "src").rglob("*.py"))
    candidates += sorted(path for path in (REPOSITORY / "scripts").rglob("*") if path.is_file())
    for path in candidates:
        if "__pycache__" in path.parts or path.suffix not in ("", ".py"):
            continue
        source = path.read_bytes()
        try:
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError):
            continue
        yield path.relative_to(REPOSITORY).as_posix(), tree


def test_nothing_but_a_test_builds_a_loopback_client_or_changes_its_trust() -> None:
    """Step 12's native fixture wrapper joins ``ALLOWED`` when it is written."""
    allowed = {"src/cole_voice_lab/storage_http_client.py"}
    found: list[tuple[str, int, str]] = []
    for path, tree in python_sources():
        if path in allowed:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "loopback_for_tests":
                found.append((path, node.lineno, node.attr))
            if isinstance(node, ast.keyword) and node.arg == "tls_context":
                found.append((path, node.lineno, str(node.arg)))
    assert found == []
    # Both halves of the scan see what they look for, so an empty list means something. The
    # keyword half has no example in the scanned tree, so it gets one of its own here.
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "loopback_for_tests"
        for node in ast.walk(ast.parse(Path(__file__).read_bytes()))
    )
    assert any(
        isinstance(node, ast.keyword) and node.arg == "tls_context"
        for node in ast.walk(ast.parse("VaultConnection(tls_context=make_context)"))
    )


def test_the_shipped_tls_context_verifies_the_chain_and_the_name() -> None:
    """S8.2's one wire property: the default no test ever replaces is the verifying one."""
    default = inspect.signature(VaultConnection.__init__).parameters["tls_context"].default

    assert default is ssl.create_default_context
    made = default()
    assert made.verify_mode is ssl.CERT_REQUIRED
    assert made.check_hostname is True
    assert made.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_the_client_module_imports_only_the_standard_library_and_its_storage_modules() -> None:
    allowed_package = {"storage_contracts", "storage_ports", "tailnet"}
    allowed_external = set(__import__("sys").stdlib_module_names) | {"__future__", "pydantic"}
    source = Path(storage_http_client.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level:
            assert node.level == 1 and node.module in allowed_package, (node.lineno, node.module)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").partition(".")[0] in allowed_external, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.partition(".")[0] in allowed_external, alias.name
        elif isinstance(node, ast.Call):
            called = getattr(node.func, "id", getattr(node.func, "attr", None))
            assert called not in {"import_module", "__import__", "run_module"}, node.lineno


def test_the_status_table_names_only_codes_the_ports_know() -> None:
    from cole_voice_lab.storage_ports import STORAGE_RETRYABLE

    tables = (storage_http_client._STATUS_CODES, storage_http_client._HEAD_STATUS_CODES)
    for table in tables:
        assert set(table.values()) <= set(STORAGE_RETRYABLE)
        assert all(400 <= status <= 599 for status in table)
    # The HEAD table only ever reads a status differently; it never adds one.
    assert set(storage_http_client._HEAD_STATUS_CODES) <= set(storage_http_client._STATUS_CODES)


def test_a_blob_store_is_the_connections_and_nothing_else() -> None:
    connection = VaultConnection.loopback_for_tests(8080, NAMESPACE, lambda: SYNTHETIC_TOKEN)

    assert HttpBlobStore(connection).connection is connection
    assert connection.blob_store().connection is connection
    assert connection.ref_store().connection is connection

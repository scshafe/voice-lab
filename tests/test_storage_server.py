"""The vault server: every endpoint, every refusal, the access log and the two helpers.

Scenario tests for ``src/cole_voice_lab/storage_server.py``. Every server runs in a thread on
the IPv4 loopback with its data root, its access log and its configuration under ``tmp_path``
(the ``_running_server`` pattern of ``tests/test_capture_console.py``); ``http.client`` sends
the requests, and ``_raw_request`` sends the malformed and repeated headers that a well-behaved
client cannot. No test reaches the network, the real library or a real token: tokens are made
by ``storage_server.new_token`` and live only in memory.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import runpy
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, cast

import pytest

from cole_voice_lab import storage_server
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.storage_contracts import (
    MAX_BLOB_BYTES,
    STORAGE_CODE_PATTERN,
    VAULT_TOKEN_PATTERN,
    BlobRef,
    RefValue,
    VaultAccessRecord,
    VaultNamespaceConfig,
    VaultServerConfig,
)
from cole_voice_lab.storage_local import BLOBS_DIRECTORY, LocalBlobStore
from cole_voice_lab.storage_ports import STORAGE_RETRYABLE, StorageError, StorageIntegrityError
from cole_voice_lab.storage_server import (
    MAX_JSON_BODY_BYTES,
    NAMESPACES_DIRECTORY,
    VaultServer,
    hash_token,
    load_server_configuration,
    main,
    new_token,
)

NAMESPACE = "cole"
OTHER_NAMESPACE = "spare"
QUOTA = 4096
HELLO = b"hello vault"
HELLO_SHA256 = hashlib.sha256(HELLO).hexdigest()
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
HEAD_REF = "libraries/cole-self/head"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
}
FIXED_TIME = datetime(2026, 9, 17, 12, tzinfo=UTC)


# -- fixtures and helpers -----------------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    """The raw tokens of one namespace; they exist only inside a test process."""

    read: str
    write: str


@dataclass(frozen=True)
class Vault:
    """One running server with everything a request needs."""

    server: VaultServer
    tokens: Mapping[str, Tokens]
    data_root: Path
    access_log: Path

    @property
    def address(self) -> tuple[str, int]:
        return cast(tuple[str, int], self.server.server_address[:2])

    def namespace_root(self, name: str = NAMESPACE) -> Path:
        return self.data_root / NAMESPACES_DIRECTORY / name

    def records(self) -> list[VaultAccessRecord]:
        return read_access_log(self.access_log)


def namespace_configuration(
    name: str, tokens: Tokens, *, quota: int = QUOTA, **changes: Any
) -> VaultNamespaceConfig:
    return VaultNamespaceConfig(
        name=name,
        read_token_sha256=hash_token(tokens.read),
        write_token_sha256=hash_token(tokens.write),
        quota_bytes=quota,
        **changes,
    )


def write_configuration(path: Path, config: VaultServerConfig) -> Path:
    path.write_bytes(canonical_json_bytes(config) + b"\n")
    return path


def read_access_log(path: Path) -> list[VaultAccessRecord]:
    """Every line of the access log, validated as the contract it must be."""
    lines = path.read_bytes().decode("utf-8").splitlines()
    return [VaultAccessRecord.model_validate_json(line) for line in lines]


def provisioned(
    tmp_path: Path,
    *,
    names: Sequence[str] = (NAMESPACE,),
    quota: int = QUOTA,
    logins: Sequence[str] = (),
    max_workers: int = 4,
    request_timeout_seconds: int = 30,
) -> tuple[VaultServerConfig, Mapping[str, Tokens], Path, Path]:
    """A configuration with fresh tokens, plus the data root and access-log paths."""
    tokens = {name: Tokens(read=new_token(), write=new_token()) for name in names}
    config = VaultServerConfig(
        namespaces=tuple(
            namespace_configuration(
                name, tokens[name], quota=quota, allowed_user_logins=tuple(logins)
            )
            for name in names
        ),
        max_workers=max_workers,
        request_timeout_seconds=request_timeout_seconds,
    )
    return config, tokens, tmp_path / "vault-data", tmp_path / "access.log"


@contextmanager
def running(
    tmp_path: Path,
    *,
    provision: bool = True,
    clock: Any = None,
    **options: Any,
) -> Iterator[Vault]:
    """Serve one vault on the loopback in a thread, then shut it down and close it."""
    config, tokens, data_root, access_log = provisioned(tmp_path, **options)
    server = VaultServer(
        (storage_server.LOOPBACK_HOST, 0),
        config=config,
        data_root=data_root,
        access_log=access_log,
        provision=provision,
        **({} if clock is None else {"clock": clock}),
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield Vault(server=server, tokens=tokens, data_root=data_root, access_log=access_log)
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
        server.server_close()
        assert not thread.is_alive()


@dataclass(frozen=True)
class Result:
    """One HTTP answer."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def json(self) -> dict[str, Any]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return cast(dict[str, Any], value)

    @property
    def code(self) -> str | None:
        return None if not self.body else cast(str | None, self.json().get("code"))


def request(
    vault: Vault,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
) -> Result:
    """One request on a fresh connection, with the usual Authorization header."""
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    host, port = vault.address
    connection = HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        result = Result(response.status, tuple(response.getheaders()), response.read())
    finally:
        connection.close()
    for name, value in SECURITY_HEADERS.items():
        assert result.header(name) == value, name
    assert result.header("Connection") == "close"
    return result


def raw_request(
    vault: Vault,
    method: str,
    path: str,
    headers: Sequence[tuple[str, str]],
    *,
    body: bytes = b"",
    timeout: float = 5.0,
) -> Result:
    """One request whose headers are sent exactly as given (repeated or malformed)."""
    host, port = vault.address
    connection = HTTPConnection(host, port, timeout=timeout)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(body if body else None)
        response = connection.getresponse()
        return Result(response.status, tuple(response.getheaders()), response.read())
    finally:
        connection.close()


def blob_path(digest: str, *, size: int | None = None, namespace: str = NAMESPACE) -> str:
    query = "" if size is None else f"?size={size}"
    return f"/v1/{namespace}/blobs/{digest}{query}"


def ref_path(name: str = HEAD_REF, *, namespace: str = NAMESPACE) -> str:
    return f"/v1/{namespace}/refs/{name}"


def put_blob(vault: Vault, data: bytes, *, namespace: str = NAMESPACE) -> Result:
    return request(
        vault,
        "PUT",
        blob_path(hashlib.sha256(data).hexdigest(), namespace=namespace),
        token=vault.tokens[namespace].write,
        body=data,
    )


def swap_body(target: BlobRef, expected_version: int | None) -> bytes:
    return json.dumps(
        {"expected_version": expected_version, "target": target.model_dump(mode="json")}
    ).encode("utf-8")


@contextmanager
def dribbling(stream: socket.socket, interval: float) -> Iterator[None]:
    """Send one byte every ``interval`` seconds: the client that trips no per-recv timeout."""
    stopped = threading.Event()

    def drip() -> None:
        while not stopped.is_set():
            try:
                stream.sendall(b"x")
            except OSError:
                return  # the server answered and closed, which is what the test is waiting for
            stopped.wait(interval)

    dripper = threading.Thread(target=drip, daemon=True)
    dripper.start()
    try:
        yield
    finally:
        stopped.set()
        dripper.join(timeout=10.0)


def refused_after_a_whole_body(
    vault: Vault, method: str, path: str, *, token: str, payload: bytes
) -> str:
    """The answer to a refused request whose body is written in full, from a raw socket.

    A payload past every loopback buffer is the only way to tell a drained refusal from an
    undrained one: ``sendall`` finishes only because the server read the body it was going to
    refuse, and a server that answered without draining resets the connection instead.
    """
    host, port = vault.address
    with closing(socket.create_connection((host, port), timeout=10.0)) as stream:
        stream.sendall(
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: vault\r\n"
            f"Authorization: Bearer {token}\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        )
        stream.sendall(payload)
        return drain(stream)


def health_within(vault: Vault, seconds: float) -> Result:
    """The first health answer that is not a saturation refusal, or the last one tried."""
    deadline = time.monotonic() + seconds
    while True:
        answer = request(vault, "GET", "/healthz")
        if answer.status == 200 or time.monotonic() >= deadline:
            return answer
        time.sleep(0.05)


def drain(stream: socket.socket) -> str:
    """Everything the server sends before it closes the connection."""
    received = b""
    while chunk := stream.recv(65536):
        received += chunk
    return received.decode("utf-8")


def free_port() -> int:
    with closing(socket.socket()) as probe:
        probe.bind((storage_server.LOOPBACK_HOST, 0))
        return cast(int, probe.getsockname()[1])


def tree(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under ``root`` with its mode and size, for an "it wrote nothing" check."""
    found: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        found[str(path.relative_to(root))] = (stat.S_IMODE(metadata.st_mode), metadata.st_size)
    return found


# -- configuration ------------------------------------------------------------------------


def test_a_configuration_names_namespaces_tokens_and_bounds(tmp_path: Path) -> None:
    config, tokens, _, _ = provisioned(tmp_path, names=(NAMESPACE, OTHER_NAMESPACE))
    path = write_configuration(tmp_path / "vault.json", config)

    loaded = load_server_configuration(path)

    assert loaded == config
    assert [namespace.name for namespace in loaded.namespaces] == [NAMESPACE, OTHER_NAMESPACE]
    assert loaded.max_workers == 4
    assert loaded.namespaces[0].read_token_sha256 == hash_token(tokens[NAMESPACE].read)
    text = path.read_text(encoding="utf-8")
    for namespace in tokens.values():
        assert namespace.read not in text and namespace.write not in text


def test_the_defaults_are_eight_workers_and_two_minutes(tmp_path: Path) -> None:
    tokens = Tokens(read=new_token(), write=new_token())
    minimal = {
        "namespaces": [
            {
                "name": NAMESPACE,
                "read_token_sha256": hash_token(tokens.read),
                "write_token_sha256": hash_token(tokens.write),
                "quota_bytes": 1024,
            }
        ]
    }
    path = tmp_path / "vault.json"
    path.write_text(json.dumps(minimal), encoding="utf-8")

    loaded = load_server_configuration(path)

    assert (loaded.max_workers, loaded.request_timeout_seconds) == (8, 120)
    assert loaded.namespaces[0].allowed_user_logins == ()


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        pytest.param({"namespaces": []}, "namespaces: Tuple should have at least 1", id="none"),
        pytest.param({"max_workers": 0}, "max_workers: Input should be greater", id="workers"),
        pytest.param(
            {"max_workers": 65}, "max_workers: Input should be less than or equal to 64", id="many"
        ),
        pytest.param(
            {"request_timeout_seconds": 601},
            "request_timeout_seconds: Input should be less than or equal to 600",
            id="timeout",
        ),
        pytest.param({"quota": 0}, "quota_bytes: Input should be greater", id="quota"),
        pytest.param({"same-token": True}, "read and write token hashes must differ", id="shared"),
        pytest.param({"repeat-name": True}, "namespace names must be unique", id="repeat"),
        pytest.param(
            {"shared-token": True}, "no token hash may serve two namespaces", id="crossover"
        ),
        pytest.param({"login": "cole\nrogue"}, "hidden, control, newline", id="login"),
        pytest.param({"login-twice": True}, "allowed_user_logins must not repeat", id="logins"),
        pytest.param({"name": "Cole"}, "name: String should match pattern", id="name"),
        pytest.param({"extra": 1}, "Extra inputs are not permitted", id="extra"),
    ],
)
def test_an_invalid_configuration_is_refused_naming_the_rule(
    tmp_path: Path, changes: dict[str, Any], problem: str
) -> None:
    read, write = new_token(), new_token()
    namespace: dict[str, Any] = {
        "name": changes.get("name", NAMESPACE),
        "read_token_sha256": hash_token(read),
        "write_token_sha256": hash_token(write),
        "quota_bytes": changes.get("quota", 1024),
    }
    if changes.get("same-token"):
        namespace["write_token_sha256"] = namespace["read_token_sha256"]
    if "login" in changes:
        namespace["allowed_user_logins"] = [changes["login"]]
    if changes.get("login-twice"):
        namespace["allowed_user_logins"] = ["cole@github", "cole@github"]
    if changes.get("extra"):
        namespace["token"] = "cvlv_not-a-field"
    payload: dict[str, Any] = {"namespaces": [namespace]}
    if changes.get("repeat-name") or changes.get("shared-token"):
        second = dict(namespace)
        if changes.get("shared-token"):
            second["name"] = OTHER_NAMESPACE
        else:
            second["read_token_sha256"] = hash_token(new_token())
            second["write_token_sha256"] = hash_token(new_token())
        payload["namespaces"] = [namespace, second]
    for key in ("namespaces", "max_workers", "request_timeout_seconds"):
        if key in changes:
            payload[key] = changes[key]
    path = tmp_path / "vault.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StorageError, match=re.escape(problem)) as refused:
        load_server_configuration(path)

    assert refused.value.code == "STORAGE.PROTOCOL"
    assert read not in str(refused.value) and write not in str(refused.value)


def test_a_configuration_that_is_not_json_or_not_an_object_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(StorageError, match="Invalid JSON") as refused:
        load_server_configuration(path)

    assert refused.value.code == "STORAGE.PROTOCOL"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(StorageError, match=r"\(root\): Input should be"):
        load_server_configuration(path)


def test_a_configuration_that_is_missing_oversized_or_not_a_file_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(StorageError, match="cannot be read") as missing:
        load_server_configuration(tmp_path / "absent.json")
    assert missing.value.code == "STORAGE.UNAVAILABLE"

    oversized = tmp_path / "big.json"
    oversized.write_bytes(b"{" + b" " * storage_server.MAX_CONFIG_BYTES)
    with pytest.raises(StorageIntegrityError, match="larger than"):
        load_server_configuration(oversized)

    directory = tmp_path / "directory.json"
    directory.mkdir()
    with pytest.raises(StorageIntegrityError, match="not a regular file"):
        load_server_configuration(directory)

    with pytest.raises(StorageIntegrityError, match="absolute, normalized path"):
        load_server_configuration(Path("vault.json"))

    linked = tmp_path / "link.json"
    linked.symlink_to(tmp_path / "big.json")
    with pytest.raises(StorageIntegrityError, match="no symlink in its path"):
        load_server_configuration(linked)


# -- startup ------------------------------------------------------------------------------


def test_provisioning_makes_one_private_root_per_namespace(tmp_path: Path) -> None:
    with running(tmp_path, names=(NAMESPACE, OTHER_NAMESPACE)) as vault:
        for name in (NAMESPACE, OTHER_NAMESPACE):
            root = vault.namespace_root(name)
            assert stat.S_IMODE(root.lstat().st_mode) == 0o700
            assert sorted(item.name for item in root.iterdir()) == [
                ".refs.lock",
                ".tmp.lock",
                "blobs",
                "refs",
                "tmp",
            ]
        assert stat.S_IMODE(vault.data_root.lstat().st_mode) == 0o700
        assert stat.S_IMODE(vault.access_log.lstat().st_mode) == 0o600
        assert vault.access_log.read_bytes() == b""


def test_without_provisioning_a_missing_root_is_refused(tmp_path: Path) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)

    with pytest.raises(StorageError, match="needs an existing directory") as refused:
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=access_log,
        )

    assert refused.value.code == "STORAGE.UNAVAILABLE"
    assert not access_log.exists()


def test_a_namespace_root_with_group_permissions_is_refused_at_startup(tmp_path: Path) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)
    (data_root / NAMESPACES_DIRECTORY / NAMESPACE).mkdir(parents=True)
    (data_root / NAMESPACES_DIRECTORY / NAMESPACE).chmod(0o750)

    with pytest.raises(StorageIntegrityError, match="group or other permissions"):
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=access_log,
        )


def test_a_data_root_that_is_a_symlink_a_file_or_relative_is_refused(tmp_path: Path) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    data_root.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(StorageIntegrityError, match="no symlink in its path"):
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=access_log,
        )

    with pytest.raises(StorageIntegrityError, match="absolute, normalized path"):
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=Path("vault-data"),
            access_log=access_log,
        )

    plain = tmp_path / "plain"
    plain.write_text("", encoding="utf-8")
    with pytest.raises(StorageIntegrityError, match="symlink or non-directory"):
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=plain,
            access_log=access_log,
        )


def test_provisioning_a_root_whose_parent_is_missing_is_refused(tmp_path: Path) -> None:
    config, _, _, access_log = provisioned(tmp_path)

    with pytest.raises(StorageError, match="cannot make") as refused:
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=tmp_path / "absent" / "vault-data",
            access_log=access_log,
            provision=True,
        )

    assert refused.value.code == "STORAGE.UNAVAILABLE"


def test_provisioning_twice_keeps_the_same_private_roots(tmp_path: Path) -> None:
    with running(tmp_path) as first:
        put_blob(first, HELLO)
        before = tree(first.data_root)
    with running(tmp_path) as second:
        assert tree(second.data_root) == before
        stored = request(
            second,
            "GET",
            blob_path(HELLO_SHA256, size=len(HELLO)),
            token=second.tokens[NAMESPACE].read,
        )
        assert stored.body == HELLO


def test_an_access_log_that_is_a_symlink_or_readable_by_others_is_refused(
    tmp_path: Path,
) -> None:
    config, _, data_root, _ = provisioned(tmp_path)
    shared = tmp_path / "shared.log"
    shared.write_text("", encoding="utf-8")
    shared.chmod(0o644)

    with pytest.raises(StorageIntegrityError, match="group or other permissions"):
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=shared,
            provision=True,
        )

    linked = tmp_path / "link.log"
    linked.symlink_to(shared)
    with pytest.raises(StorageIntegrityError, match="no symlink in its path"):
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=linked,
            provision=True,
        )

    with pytest.raises(StorageError, match="cannot append") as missing:
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=tmp_path / "absent" / "access.log",
            provision=True,
        )
    assert missing.value.code == "STORAGE.UNAVAILABLE"


def test_an_access_log_in_place_of_a_directory_is_refused(tmp_path: Path) -> None:
    config, _, data_root, _ = provisioned(tmp_path)
    directory = tmp_path / "logs"
    directory.mkdir()

    with pytest.raises(StorageIntegrityError, match="cannot append") as refused:
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=directory,
            provision=True,
        )

    assert refused.value.code == "STORAGE.INTEGRITY"


def test_an_address_that_cannot_be_bound_closes_everything_it_opened(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        host, port = vault.address
        config, _, data_root, access_log = provisioned(tmp_path / "second")
        (tmp_path / "second").mkdir()

        with pytest.raises(OSError, match="ddress"):
            VaultServer(
                (host, port),
                config=config,
                data_root=data_root,
                access_log=access_log,
                provision=True,
            )


def test_a_blob_directory_with_a_foreign_entry_is_refused_at_startup(tmp_path: Path) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)
    data_root.mkdir(mode=0o700)
    (data_root / NAMESPACES_DIRECTORY).mkdir(mode=0o700)
    root = data_root / NAMESPACES_DIRECTORY / NAMESPACE
    LocalBlobStore(root, create=True)
    (root / BLOBS_DIRECTORY / "ab").mkdir()
    (root / BLOBS_DIRECTORY / "ab" / "blob").symlink_to(access_log)

    with pytest.raises(StorageIntegrityError, match="entry no store writes"):
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=access_log,
        )


def test_a_blob_shard_that_is_not_a_directory_is_refused_at_startup(tmp_path: Path) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)
    data_root.mkdir(mode=0o700)
    (data_root / NAMESPACES_DIRECTORY).mkdir(mode=0o700)
    root = data_root / NAMESPACES_DIRECTORY / NAMESPACE
    LocalBlobStore(root, create=True)
    (root / BLOBS_DIRECTORY / "cd").write_text("", encoding="utf-8")

    with pytest.raises(StorageIntegrityError, match="entry no store writes"):
        VaultServer(
            (storage_server.LOOPBACK_HOST, 0),
            config=config,
            data_root=data_root,
            access_log=access_log,
        )


def test_measuring_a_namespace_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="cannot measure") as refused:
        storage_server._stored_blob_bytes(tmp_path / "absent")

    assert refused.value.code == "STORAGE.UNAVAILABLE"


def test_a_namespace_starts_from_the_bytes_its_blobs_already_hold(tmp_path: Path) -> None:
    with running(tmp_path) as first:
        put_blob(first, HELLO)
        put_blob(first, b"more")
    with running(tmp_path) as second:
        assert second.server.namespaces[NAMESPACE].stored_bytes == len(HELLO) + len(b"more")


# -- health -------------------------------------------------------------------------------


def test_the_health_endpoint_answers_without_a_credential(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        answer = request(vault, "GET", "/healthz")

        assert (answer.status, answer.body) == (200, b"ok")
        assert answer.header("Content-Type") == "text/plain; charset=utf-8"
        record = vault.records()[-1]
        assert (record.operation, record.scope, record.status) == ("health", "none", 200)
        assert (record.namespace, record.ref, record.code) == (None, None, None)


def test_the_health_endpoint_takes_no_query_body_or_other_method(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        assert request(vault, "GET", "/healthz?x=1").code == "STORAGE.PROTOCOL"
        with_body = request(vault, "GET", "/healthz", body=b"hello")
        assert (with_body.status, with_body.code) == (400, "STORAGE.PROTOCOL")
        assert "takes no body" in with_body.json()["message"]
        refused = request(vault, "POST", "/healthz")
        assert (refused.status, refused.header("Allow")) == (405, "GET")
        assert refused.code == "STORAGE.PROTOCOL"


# -- blobs --------------------------------------------------------------------------------


def test_a_blob_is_stored_once_and_read_back_verbatim(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        created = put_blob(vault, HELLO)
        again = put_blob(vault, HELLO)

        assert created.status == 201
        assert created.json() == {"sha256": HELLO_SHA256, "size": len(HELLO)}
        assert again.status == 200 and again.json() == created.json()
        read = request(
            vault,
            "GET",
            blob_path(HELLO_SHA256, size=len(HELLO)),
            token=vault.tokens[NAMESPACE].read,
        )
        assert read.status == 200 and read.body == HELLO
        assert read.header("Content-Type") == "application/octet-stream"
        assert vault.server.namespaces[NAMESPACE].stored_bytes == len(HELLO)
        operations = [record.operation for record in vault.records()]
        assert operations == ["put-blob", "put-blob", "get-blob"]
        assert [record.bytes for record in vault.records()] == [len(HELLO)] * 3


def test_an_empty_blob_is_the_one_with_the_empty_digest(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        stored = put_blob(vault, b"")

        assert stored.status == 201 and stored.json()["size"] == 0
        read = request(
            vault, "GET", blob_path(EMPTY_SHA256, size=0), token=vault.tokens[NAMESPACE].read
        )
        assert (read.status, read.body) == (200, b"")
        mismatched = request(
            vault, "GET", blob_path(HELLO_SHA256, size=0), token=vault.tokens[NAMESPACE].read
        )
        assert (mismatched.status, mismatched.code) == (400, "STORAGE.PROTOCOL")
        assert "empty SHA-256" in mismatched.json()["message"]


def test_has_answers_from_metadata_and_sends_no_body(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        put_blob(vault, HELLO)
        token = vault.tokens[NAMESPACE].read

        found = request(vault, "HEAD", blob_path(HELLO_SHA256, size=len(HELLO)), token=token)
        wrong_size = request(vault, "HEAD", blob_path(HELLO_SHA256, size=1), token=token)
        absent = request(vault, "HEAD", blob_path("b" * 64, size=3), token=token)

        assert (found.status, found.body) == (200, b"")
        assert found.header("Content-Length") == str(len(HELLO))
        assert (wrong_size.status, wrong_size.body) == (404, b"")
        assert (absent.status, absent.body) == (404, b"")
        assert [record.status for record in vault.records()[1:]] == [200, 404, 404]
        assert [record.bytes for record in vault.records()[1:]] == [0, 0, 0]
        assert vault.records()[-1].code == "STORAGE.NOT_FOUND"


def test_reading_a_blob_needs_its_size_and_a_well_formed_query(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        token = vault.tokens[NAMESPACE].read

        missing = request(vault, "GET", blob_path(HELLO_SHA256), token=token)
        wrong = request(vault, "GET", f"{blob_path(HELLO_SHA256)}?length=3", token=token)
        huge = request(vault, "GET", blob_path(HELLO_SHA256, size=MAX_BLOB_BYTES + 1), token=token)
        zeros = request(vault, "GET", f"{blob_path(HELLO_SHA256)}?size=000011", token=token)

        assert (missing.status, missing.code) == (400, "STORAGE.PROTOCOL")
        assert "needs its size" in missing.json()["message"]
        assert (wrong.status, wrong.code) == (400, "STORAGE.PROTOCOL")
        assert (huge.status, huge.code) == (413, "STORAGE.TOO_LARGE")
        assert (zeros.status, zeros.code) == (404, "STORAGE.NOT_FOUND")


def test_a_missing_blob_is_a_404_and_a_corrupt_one_is_a_500(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        put_blob(vault, HELLO)
        token = vault.tokens[NAMESPACE].read
        absent = request(vault, "GET", blob_path("c" * 64, size=3), token=token)

        shard = vault.namespace_root() / BLOBS_DIRECTORY / HELLO_SHA256[:2] / HELLO_SHA256
        shard.chmod(0o600)
        shard.write_bytes(b"x" * len(HELLO))
        corrupt = request(vault, "GET", blob_path(HELLO_SHA256, size=len(HELLO)), token=token)

        assert (absent.status, absent.code) == (404, "STORAGE.NOT_FOUND")
        assert (corrupt.status, corrupt.code) == (500, "STORAGE.INTEGRITY")
        assert str(vault.namespace_root()) not in corrupt.body.decode("utf-8")
        assert vault.records()[-1].status == 500


def test_an_upload_whose_bytes_miss_the_digest_is_refused(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        answer = request(
            vault,
            "PUT",
            blob_path(HELLO_SHA256),
            token=vault.tokens[NAMESPACE].write,
            body=b"other bytes",
        )

        assert (answer.status, answer.code) == (400, "STORAGE.INTEGRITY")
        assert vault.server.namespaces[NAMESPACE].stored_bytes == 0
        assert not (vault.namespace_root() / BLOBS_DIRECTORY / HELLO_SHA256[:2]).exists()
        record = vault.records()[-1]
        assert (record.operation, record.bytes) == ("put-blob", len(b"other bytes"))


def test_an_upload_beyond_the_quota_is_refused_and_keeps_the_room_it_had(
    tmp_path: Path,
) -> None:
    with running(tmp_path, quota=32) as vault:
        first = put_blob(vault, b"a" * 20)
        second = put_blob(vault, b"b" * 20)
        third = put_blob(vault, b"c" * 12)

        assert first.status == 201
        assert (second.status, second.code) == (507, "STORAGE.QUOTA")
        assert third.status == 201
        assert vault.server.namespaces[NAMESPACE].stored_bytes == 32
        assert vault.records()[1].code == "STORAGE.QUOTA"
        stored = sorted(
            path.name
            for path in (vault.namespace_root() / BLOBS_DIRECTORY).rglob("*")
            if path.is_file()
        )
        assert len(stored) == 2


def test_a_put_that_the_store_refuses_gives_back_the_bytes_it_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running(tmp_path) as vault:
        namespace = vault.server.namespaces[NAMESPACE]

        def refuse(_data: bytes) -> BlobRef:
            raise StorageError("STORAGE.UNAVAILABLE", "the disk went away")

        monkeypatch.setattr(namespace.blobs, "put", refuse)
        answer = put_blob(vault, HELLO)

        assert (answer.status, answer.code) == (503, "STORAGE.UNAVAILABLE")
        assert namespace.stored_bytes == 0
        assert "went away" not in answer.body.decode("utf-8")


# -- refs ---------------------------------------------------------------------------------


def test_a_ref_is_swapped_by_compare_and_set_and_read_back(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        put_blob(vault, HELLO)
        target = BlobRef(sha256=HELLO_SHA256, size=len(HELLO))

        created = request(
            vault,
            "POST",
            ref_path(),
            token=vault.tokens[NAMESPACE].write,
            body=swap_body(target, None),
        )
        read = request(vault, "GET", ref_path(), token=vault.tokens[NAMESPACE].read)
        stale = request(
            vault,
            "POST",
            ref_path(),
            token=vault.tokens[NAMESPACE].write,
            body=swap_body(target, None),
        )
        second = request(
            vault,
            "POST",
            ref_path(),
            token=vault.tokens[NAMESPACE].write,
            body=swap_body(target, 1),
        )

        assert created.status == 200
        value = RefValue.model_validate_json(created.body)
        assert (value.name, value.version, value.target) == (HEAD_REF, 1, target)
        assert RefValue.model_validate_json(read.body) == value
        assert (stale.status, stale.code) == (409, "STORAGE.CONFLICT")
        assert RefValue.model_validate_json(second.body).version == 2
        assert [record.ref for record in vault.records()[1:]] == [HEAD_REF] * 4


def test_an_absent_ref_is_a_404(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        answer = request(vault, "GET", ref_path(), token=vault.tokens[NAMESPACE].read)

        assert (answer.status, answer.code) == (404, "STORAGE.NOT_FOUND")
        assert "no such ref" in answer.json()["message"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(b"{", "one UTF-8 JSON object", id="broken"),
        pytest.param(b"\xff\xfe", "one UTF-8 JSON object", id="not-utf8"),
        pytest.param(b"[]", "exactly expected_version and target", id="array"),
        pytest.param(b'{"target": {}}', "exactly expected_version and target", id="short"),
        pytest.param(
            b'{"expected_version": 1, "target": {}, "extra": 1}',
            "exactly expected_version and target",
            id="extra",
        ),
        pytest.param(
            b'{"expected_version": true, "target": {"sha256": "' + b"a" * 64 + b'", "size": 1}}',
            "an integer or null",
            id="boolean",
        ),
        pytest.param(
            b'{"expected_version": "1", "target": {"sha256": "' + b"a" * 64 + b'", "size": 1}}',
            "an integer or null",
            id="string",
        ),
        pytest.param(
            b'{"expected_version": null, "target": {"sha256": "x", "size": 1}}',
            "a SHA-256 digest and a size",
            id="target",
        ),
    ],
)
def test_an_invalid_swap_body_is_refused(tmp_path: Path, body: bytes, message: str) -> None:
    with running(tmp_path) as vault:
        answer = request(vault, "POST", ref_path(), token=vault.tokens[NAMESPACE].write, body=body)

        assert (answer.status, answer.code) == (400, "STORAGE.PROTOCOL")
        assert message in answer.json()["message"]


def test_a_swap_needs_a_body(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        answer = raw_request(
            vault,
            "POST",
            ref_path(),
            [("Host", "vault"), ("Authorization", f"Bearer {vault.tokens[NAMESPACE].write}")],
        )

        assert (answer.status, answer.code) == (411, "STORAGE.TOO_LARGE")
        assert "needs exactly one Content-Length" in answer.json()["message"]


# -- path grammar -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        pytest.param("/", 404, "STORAGE.NOT_FOUND", id="root"),
        pytest.param("/v2/cole/blobs/" + "a" * 64, 404, "STORAGE.NOT_FOUND", id="version"),
        pytest.param("/v1/cole/objects/x", 404, "STORAGE.NOT_FOUND", id="collection"),
        pytest.param("/v1/cole/blobs", 404, "STORAGE.NOT_FOUND", id="short"),
        pytest.param("/v1/Cole/blobs/" + "a" * 64, 400, "STORAGE.PROTOCOL", id="namespace"),
        pytest.param("/v1/cole/blobs/" + "A" * 64, 400, "STORAGE.PROTOCOL", id="uppercase"),
        pytest.param("/v1/cole/blobs/" + "a" * 63, 400, "STORAGE.PROTOCOL", id="short-digest"),
        pytest.param("/v1/cole/blobs/" + "a" * 64 + "/x", 400, "STORAGE.PROTOCOL", id="deep"),
        pytest.param("/v1/cole/refs/Head", 400, "STORAGE.PROTOCOL", id="ref-case"),
        pytest.param("/v1/cole/refs/../head", 400, "STORAGE.PROTOCOL", id="dots"),
        pytest.param("/v1/cole/refs/head?x=1", 400, "STORAGE.PROTOCOL", id="ref-query"),
        pytest.param("/v1/cole/refs/" + "a/" * 9 + "b", 400, "STORAGE.PROTOCOL", id="segments"),
    ],
)
def test_a_target_outside_the_grammar_is_refused_before_any_credential(
    tmp_path: Path, path: str, status: int, code: str
) -> None:
    with running(tmp_path) as vault:
        answer = request(vault, "GET", path)

        assert (answer.status, answer.code) == (status, code)
        record = vault.records()[-1]
        assert (record.operation, record.namespace, record.ref) == ("refused", None, None)


def test_a_valid_namespace_that_is_not_served_is_refused_like_a_bad_token(
    tmp_path: Path,
) -> None:
    with running(tmp_path) as vault:
        token = vault.tokens[NAMESPACE].read

        unknown = request(vault, "GET", ref_path(namespace="absent"), token=token)
        foreign = request(vault, "GET", ref_path(), token=new_token())
        served = request(vault, "GET", ref_path(), token=token)

        assert unknown.status == 401 and unknown.body == foreign.body
        assert NAMESPACE not in unknown.json()["message"]
        assert served.status == 404
        assert [record.namespace for record in vault.records()] == ["absent", NAMESPACE, NAMESPACE]
        assert [record.scope for record in vault.records()] == ["none", "none", "read"]


# -- credentials --------------------------------------------------------------------------


def test_a_request_without_one_bearer_token_is_refused(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        token = vault.tokens[NAMESPACE].read
        authority = ("Host", "vault")
        bearer = ("Authorization", f"Bearer {token}")

        missing = raw_request(vault, "GET", ref_path(), [authority])
        twice = raw_request(vault, "GET", ref_path(), [authority, bearer, bearer])
        basic = raw_request(vault, "GET", ref_path(), [authority, ("Authorization", token)])
        lowercase = raw_request(
            vault, "GET", ref_path(), [authority, ("Authorization", f"bearer {token}")]
        )
        padded = raw_request(
            vault, "GET", ref_path(), [authority, ("Authorization", f"Bearer  {token}")]
        )
        non_ascii = raw_request(
            vault, "GET", ref_path(), [authority, ("Authorization", "Bearer cvlv_\u00e9")]
        )

        for answer in (missing, twice, basic, lowercase, padded, non_ascii):
            assert answer.status == 401, answer.body
            assert answer.code == "STORAGE.UNAUTHORIZED"
        assert {record.scope for record in vault.records()} == {"none"}


def test_a_read_token_cannot_write_and_a_write_token_can_read(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        read_token, write_token = vault.tokens[NAMESPACE].read, vault.tokens[NAMESPACE].write
        target = BlobRef(sha256=EMPTY_SHA256, size=0)

        upload = request(vault, "PUT", blob_path(EMPTY_SHA256), token=read_token, body=b"")
        swap = request(vault, "POST", ref_path(), token=read_token, body=swap_body(target, None))
        allowed = request(vault, "GET", ref_path(), token=write_token)

        assert (upload.status, upload.code) == (401, "STORAGE.UNAUTHORIZED")
        assert (swap.status, swap.code) == (401, "STORAGE.UNAUTHORIZED")
        assert allowed.status == 404
        refusals = vault.records()[:2]
        assert [record.operation for record in refusals] == ["put-blob", "swap-ref"]
        assert [record.scope for record in refusals] == ["read", "read"]
        assert vault.records()[-1].scope == "write"


def test_a_token_of_another_namespace_is_refused(tmp_path: Path) -> None:
    with running(tmp_path, names=(NAMESPACE, OTHER_NAMESPACE)) as vault:
        other = vault.tokens[OTHER_NAMESPACE].write

        answer = request(vault, "GET", ref_path(), token=other)
        own = request(vault, "GET", ref_path(namespace=OTHER_NAMESPACE), token=other)

        assert (answer.status, answer.code) == (401, "STORAGE.UNAUTHORIZED")
        assert own.status == 404


def test_a_configured_user_login_must_arrive_exactly_once(tmp_path: Path) -> None:
    with running(tmp_path, logins=("cole@github", "cole@example.com")) as vault:
        token = vault.tokens[NAMESPACE].read
        authority = [("Host", "vault"), ("Authorization", f"Bearer {token}")]

        allowed = request(
            vault, "GET", ref_path(), token=token, headers={"Tailscale-User-Login": "cole@github"}
        )
        other = request(
            vault,
            "GET",
            ref_path(),
            token=token,
            headers={"Tailscale-User-Login": "cole@example.com"},
        )
        absent = request(vault, "GET", ref_path(), token=token)
        wrong = request(
            vault, "GET", ref_path(), token=token, headers={"Tailscale-User-Login": "someone@else"}
        )
        twice = raw_request(
            vault,
            "GET",
            ref_path(),
            [
                *authority,
                ("Tailscale-User-Login", "cole@github"),
                ("Tailscale-User-Login", "cole@github"),
            ],
        )

        assert allowed.status == 404 and other.status == 404
        for answer in (absent, wrong, twice):
            assert (answer.status, answer.code) == (401, "STORAGE.UNAUTHORIZED")
        assert vault.records()[0].user_login == "cole@github"
        assert vault.records()[2].user_login is None


def test_the_proxy_headers_are_logged_only_when_they_can_be(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        token = vault.tokens[NAMESPACE].read
        request(
            vault,
            "GET",
            ref_path(),
            token=token,
            headers={"X-Forwarded-For": "100.64.0.2", "Tailscale-User-Login": "cole@github"},
        )
        request(vault, "GET", ref_path(), token=token, headers={"X-Forwarded-For": "a" * 300})
        raw_request(
            vault,
            "GET",
            ref_path(),
            [
                ("Host", "vault"),
                ("Authorization", f"Bearer {token}"),
                ("X-Forwarded-For", "100.64.0.2"),
                ("X-Forwarded-For", "100.64.0.3"),
            ],
        )
        request(vault, "GET", ref_path(), token=token, headers={"X-Forwarded-For": "\x7f"})
        request(vault, "GET", ref_path(), token=token, headers={"X-Forwarded-For": ""})

        logged = [(record.forwarded_for, record.user_login) for record in vault.records()]
        assert logged[0] == ("100.64.0.2", "cole@github")
        assert logged[1:] == [(None, None)] * 4


# -- framing ------------------------------------------------------------------------------


def test_a_request_needs_exactly_one_host(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        missing = raw_request(vault, "GET", "/healthz", [])
        twice = raw_request(vault, "GET", "/healthz", [("Host", "a"), ("Host", "b")])

        for answer in (missing, twice):
            assert (answer.status, answer.code) == (400, "STORAGE.PROTOCOL")
            assert "one Host header" in answer.json()["message"]


def test_a_transfer_encoded_body_is_refused(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        answer = raw_request(
            vault,
            "PUT",
            blob_path(HELLO_SHA256),
            [
                ("Host", "vault"),
                ("Authorization", f"Bearer {vault.tokens[NAMESPACE].write}"),
                ("Transfer-Encoding", "chunked"),
            ],
        )

        assert (answer.status, answer.code) == (400, "STORAGE.PROTOCOL")
        assert "transfer-encoded" in answer.json()["message"]


@pytest.mark.parametrize(
    ("lengths", "status"),
    [
        pytest.param(["11", "11"], 400, id="twice"),
        pytest.param([""], 400, id="empty"),
        pytest.param(["11 "], 400, id="padded"),
        pytest.param(["0x11"], 400, id="hex"),
        pytest.param(["-1"], 400, id="negative"),
        pytest.param(["\u00b2"], 400, id="superscript-digit"),
        pytest.param([str(MAX_BLOB_BYTES + 1)], 413, id="over"),
        pytest.param(["9" * 40], 413, id="enormous"),
    ],
)
def test_an_unusable_content_length_is_refused_before_the_body_is_read(
    tmp_path: Path, lengths: list[str], status: int
) -> None:
    with running(tmp_path) as vault:
        answer = raw_request(
            vault,
            "PUT",
            blob_path(HELLO_SHA256),
            [
                ("Host", "vault"),
                ("Authorization", f"Bearer {vault.tokens[NAMESPACE].write}"),
                *(("Content-Length", value) for value in lengths),
            ],
        )

        assert answer.status == status, answer.body
        assert answer.code == ("STORAGE.PROTOCOL" if status == 400 else "STORAGE.TOO_LARGE")
        assert vault.server.namespaces[NAMESPACE].stored_bytes == 0


def test_a_leading_zero_length_is_read_as_its_value(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        answer = raw_request(
            vault,
            "PUT",
            blob_path(HELLO_SHA256),
            [
                ("Host", "vault"),
                ("Authorization", f"Bearer {vault.tokens[NAMESPACE].write}"),
                ("Content-Length", f"000{len(HELLO)}"),
            ],
            body=HELLO,
        )

        assert answer.status == 201


def test_a_body_that_ends_early_is_refused(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        host, port = vault.address
        with closing(socket.create_connection((host, port), timeout=5.0)) as stream:
            stream.sendall(
                b"PUT " + blob_path(HELLO_SHA256).encode("ascii") + b" HTTP/1.1\r\n"
                b"Host: vault\r\n"
                b"Authorization: Bearer " + vault.tokens[NAMESPACE].write.encode("ascii") + b"\r\n"
                b"Content-Length: 11\r\n\r\nhel"
            )
            stream.shutdown(socket.SHUT_WR)
            answer = drain(stream)

        assert "400" in answer.splitlines()[0]
        assert "STORAGE.PROTOCOL" in answer
        assert "ended before Content-Length" in answer


def test_a_body_that_never_arrives_is_refused_when_the_request_times_out(
    tmp_path: Path,
) -> None:
    with running(tmp_path, request_timeout_seconds=1) as vault:
        host, port = vault.address
        with closing(socket.create_connection((host, port), timeout=10.0)) as stream:
            stream.sendall(
                b"PUT " + blob_path(HELLO_SHA256).encode("ascii") + b" HTTP/1.1\r\n"
                b"Host: vault\r\n"
                b"Authorization: Bearer " + vault.tokens[NAMESPACE].write.encode("ascii") + b"\r\n"
                b"Content-Length: 11\r\n\r\nhel"
            )
            answer = drain(stream)

        assert "503" in answer.splitlines()[0]
        assert "STORAGE.TIMEOUT" in answer
        assert vault.records()[-1].code == "STORAGE.TIMEOUT"


def test_a_read_after_the_deadline_times_out_and_leaves_room_to_answer() -> None:
    reading, writing = socket.socketpair()
    with closing(reading), closing(writing):
        reader = storage_server._DeadlineReader(reading, 0.01)
        writing.sendall(b"late")
        time.sleep(0.02)

        with pytest.raises(TimeoutError, match="within its deadline"):
            reader.readinto(bytearray(4))

        assert reading.gettimeout() == pytest.approx(0.01)


@pytest.mark.parametrize("credentialed", [True, False], ids=["authenticated", "unauthenticated"])
def test_a_client_that_dribbles_its_body_gives_its_worker_back_at_the_deadline(
    tmp_path: Path, credentialed: bool
) -> None:
    with running(tmp_path, max_workers=1, request_timeout_seconds=1) as vault:
        host, port = vault.address
        token = vault.tokens[NAMESPACE].write if credentialed else new_token()
        with closing(socket.create_connection((host, port), timeout=10.0)) as stream:
            stream.sendall(
                b"PUT " + blob_path(HELLO_SHA256).encode("ascii") + b" HTTP/1.1\r\n"
                b"Host: vault\r\n"
                b"Authorization: Bearer " + token.encode("ascii") + b"\r\n"
                b"Content-Length: " + str(MAX_BLOB_BYTES).encode("ascii") + b"\r\n\r\n"
            )
            with dribbling(stream, 0.5):
                held = request(vault, "GET", "/healthz")
                freed = health_within(vault, 6.0)

        assert (held.status, held.code) == (503, "STORAGE.UNAVAILABLE")
        assert (freed.status, freed.body) == (200, b"ok")


def test_a_client_that_sends_nothing_is_closed_without_an_answer(tmp_path: Path) -> None:
    with running(tmp_path, request_timeout_seconds=1) as vault:
        host, port = vault.address
        with closing(socket.create_connection((host, port), timeout=10.0)) as stream:
            assert stream.recv(4096) == b""

        assert vault.records() == []


def test_a_refused_request_with_a_body_within_the_limit_has_it_drained(tmp_path: Path) -> None:
    payload = b"x" * (4 * 1024 * 1024)
    with running(tmp_path) as vault:
        unauthorized = refused_after_a_whole_body(
            vault,
            "PUT",
            blob_path(hashlib.sha256(payload).hexdigest()),
            token=new_token(),
            payload=payload,
        )
        wrong_method = refused_after_a_whole_body(
            vault,
            "PATCH",
            blob_path(HELLO_SHA256),
            token=vault.tokens[NAMESPACE].write,
            payload=payload,
        )

        assert "401" in unauthorized.splitlines()[0]
        assert "STORAGE.UNAUTHORIZED" in unauthorized
        assert "405" in wrong_method.splitlines()[0]
        assert "STORAGE.PROTOCOL" in wrong_method
        assert "Allow: GET, HEAD, PUT" in wrong_method
        assert [record.status for record in vault.records()] == [401, 405]


def test_a_refused_request_with_a_body_beyond_the_limit_becomes_a_413(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        answer = raw_request(
            vault,
            "POST",
            ref_path(),
            [("Host", "vault"), ("Content-Length", str(MAX_JSON_BODY_BYTES + 1))],
        )

        assert (answer.status, answer.code) == (413, "STORAGE.TOO_LARGE")
        assert vault.records()[-1].code == "STORAGE.TOO_LARGE"


def test_an_unknown_method_and_a_malformed_request_line_are_answered_as_json(
    tmp_path: Path,
) -> None:
    with running(tmp_path) as vault:
        token = vault.tokens[NAMESPACE].read
        unknown = raw_request(
            vault, "FROB", ref_path(), [("Host", "vault"), ("Authorization", f"Bearer {token}")]
        )

        host, port = vault.address
        with closing(socket.create_connection((host, port), timeout=5.0)) as stream:
            stream.sendall(b"NOT A REQUEST LINE AT ALL\r\n\r\n")
            broken = drain(stream)

        assert (unknown.status, unknown.code) == (405, "STORAGE.PROTOCOL")
        assert unknown.header("Allow") == "GET, POST"
        assert "400" in broken.splitlines()[0]
        assert "STORAGE.PROTOCOL" in broken
        assert vault.records()[-1].operation == "refused"
        assert vault.records()[-1].status == 400


def test_an_unsupported_http_version_is_answered_as_json(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        host, port = vault.address
        with closing(socket.create_connection((host, port), timeout=5.0)) as stream:
            stream.sendall(b"GET /healthz HTTP/9.9\r\nHost: vault\r\n\r\n")
            answer = drain(stream)

        assert "505" in answer.splitlines()[0]
        assert "STORAGE.PROTOCOL" in answer
        assert vault.records()[-1].status == 505


@pytest.mark.parametrize("method", ["DELETE", "OPTIONS", "TRACE", "CONNECT", "PATCH"])
def test_every_other_method_is_refused_with_an_allow_header(tmp_path: Path, method: str) -> None:
    with running(tmp_path) as vault:
        answer = request(vault, method, ref_path(), token=vault.tokens[NAMESPACE].write)

        assert (answer.status, answer.code) == (405, "STORAGE.PROTOCOL")
        assert answer.header("Allow") == "GET, POST"


# -- workers, timeouts and shutdown -------------------------------------------------------


def test_a_connection_that_finds_every_worker_busy_is_refused_at_once(tmp_path: Path) -> None:
    with running(tmp_path, max_workers=1, request_timeout_seconds=5) as vault:
        host, port = vault.address
        with closing(socket.create_connection((host, port), timeout=5.0)) as held:
            held.sendall(b"GET /healthz HTTP/1.1\r\n")  # the worker waits for the headers
            deadline = time.monotonic() + 5.0
            refused = b""
            while time.monotonic() < deadline:
                with closing(socket.create_connection((host, port), timeout=5.0)) as extra:
                    answer = extra.recv(4096)
                if answer.startswith(b"HTTP/1.1 503"):
                    refused = answer
                    break

            assert refused, "a second connection was never refused"
            assert b"STORAGE.UNAVAILABLE" in refused
            assert b"Connection: close" in refused
            held.sendall(b"Host: vault\r\n\r\n")
            assert held.recv(4096).startswith(b"HTTP/1.1 200")

        saturated = [record for record in vault.records() if record.status == 503]
        assert saturated and saturated[0].operation == "refused"
        assert saturated[0].code == "STORAGE.UNAVAILABLE"


def test_a_connection_that_arrives_after_the_workers_stopped_is_closed(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        server = vault.server
        server._workers.shutdown(wait=True)
        first, second = socket.socketpair()
        with closing(first), closing(second):
            server.process_request(first, ("127.0.0.1", 0))

            assert second.recv(16) == b""
        assert server._slots.acquire(blocking=False)
        server._slots.release()


def test_a_server_shuts_down_on_sigterm_and_answers_until_then(tmp_path: Path) -> None:
    config, tokens, data_root, access_log = provisioned(tmp_path)
    configuration = write_configuration(tmp_path / "vault.json", config)
    port = free_port()
    failures: list[str] = []

    def drive() -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                connection = HTTPConnection(storage_server.LOOPBACK_HOST, port, timeout=2.0)
                connection.request("GET", "/healthz")
                if connection.getresponse().read() != b"ok":
                    failures.append("the health endpoint did not answer ok")
                connection.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            failures.append("the server never answered")
        os.kill(os.getpid(), signal.SIGTERM)

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    status = main(
        [
            "serve",
            "--config",
            str(configuration),
            "--data-root",
            str(data_root),
            "--access-log",
            str(access_log),
            "--provision",
            "--port",
            str(port),
        ]
    )
    driver.join(timeout=20.0)

    assert failures == []
    assert status == 0
    assert [record.operation for record in read_access_log(access_log)] == ["health"]
    assert tokens[NAMESPACE].write not in access_log.read_text(encoding="utf-8")


def test_serving_binds_the_loopback_unless_it_is_asked_for_every_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)
    configuration = write_configuration(tmp_path / "vault.json", config)
    asked: list[tuple[str, int]] = []

    def record(address: tuple[str, int], **_options: Any) -> object:
        asked.append(address)
        return object()  # nothing binds: the address the operator asked for is the whole test

    def serve(server: object, **_options: Any) -> None:
        del server

    def run(*flags: str) -> int:
        return main(
            [
                "serve",
                "--config",
                str(configuration),
                "--data-root",
                str(data_root),
                "--access-log",
                str(access_log),
                "--port",
                "0",
                *flags,
            ]
        )

    with monkeypatch.context() as patched:
        patched.setattr(storage_server, "VaultServer", record)
        patched.setattr(storage_server, "serve_vault", serve)
        statuses = [run(), run("--listen-internal")]

    assert statuses == [0, 0]
    assert asked == [(storage_server.LOOPBACK_HOST, 0), (storage_server.INTERNAL_HOST, 0)]
    assert (storage_server.LOOPBACK_HOST, storage_server.INTERNAL_HOST) == ("127.0.0.1", "0.0.0.0")


def test_serving_prints_the_bound_address_and_closes_the_server(tmp_path: Path) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)
    server = VaultServer(
        (storage_server.LOOPBACK_HOST, 0),
        config=config,
        data_root=data_root,
        access_log=access_log,
        provision=True,
    )
    lines: list[str] = []
    stopper = threading.Thread(target=lambda: (time.sleep(0.2), server.shutdown()), daemon=True)

    stopper.start()
    storage_server.serve_vault(server, emit=lines.append)
    stopper.join(timeout=5.0)

    assert json.loads(lines[0]) == {"listening": server.address}
    assert server.address.startswith(f"{storage_server.LOOPBACK_HOST}:")
    assert server._log_closed


def test_closing_a_server_twice_closes_its_log_once(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        vault.server.server_close()

        assert vault.server._log_closed


# -- the access log -----------------------------------------------------------------------


def test_every_answered_request_appends_one_parsable_record(tmp_path: Path) -> None:
    def clock() -> datetime:
        return FIXED_TIME

    with running(tmp_path, clock=clock) as vault:
        seeded = canonical_json_bytes(
            VaultAccessRecord(
                time=FIXED_TIME - timedelta(days=1),
                namespace=None,
                scope="none",
                operation="health",
                ref=None,
                bytes=0,
                status=200,
                code=None,
                forwarded_for=None,
                user_login=None,
            )
        )
        vault.access_log.write_bytes(seeded + b"\n")

        put_blob(vault, HELLO)
        request(vault, "GET", "/healthz")
        request(vault, "GET", ref_path(), token=new_token())

        lines = vault.access_log.read_bytes().splitlines()
        assert lines[0] == seeded  # nothing already written was rewritten
        records = vault.records()
        assert len(records) == 4
        # An unauthorized request never named an endpoint, so it is recorded as refused.
        assert [record.operation for record in records[1:]] == ["put-blob", "health", "refused"]
        assert {record.time for record in records[1:]} == {FIXED_TIME}
        assert [record.status for record in records[1:]] == [201, 200, 401]


def test_the_access_log_holds_no_credential(tmp_path: Path) -> None:
    with running(tmp_path, logins=("cole@github",)) as vault:
        token = vault.tokens[NAMESPACE].write
        request(
            vault,
            "PUT",
            blob_path(HELLO_SHA256),
            token=token,
            body=HELLO,
            headers={"Tailscale-User-Login": "cole@github"},
        )

        text = vault.access_log.read_text(encoding="utf-8")
        assert token not in text
        assert hash_token(token) not in text
        assert "Authorization" not in text and "Bearer" not in text
        assert HELLO.decode("utf-8") not in text


def test_the_server_writes_only_its_data_root_and_access_log(tmp_path: Path) -> None:
    keep = tmp_path / "untouched"
    keep.mkdir()
    (keep / "file.txt").write_text("keep me", encoding="utf-8")
    before = tree(keep)

    with running(tmp_path) as vault:
        put_blob(vault, HELLO)
        request(
            vault,
            "POST",
            ref_path(),
            token=vault.tokens[NAMESPACE].write,
            body=swap_body(BlobRef(sha256=HELLO_SHA256, size=len(HELLO)), None),
        )
        request(vault, "GET", "/healthz")
        request(vault, "GET", "/v1/cole/blobs/nope")

        written = {
            name
            for name in tree(tmp_path)
            if not name.startswith(("vault-data", "access.log", "untouched"))
        }

    assert written == set()
    assert tree(keep) == before


def test_a_record_names_the_endpoint_the_scope_and_the_name(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        put_blob(vault, HELLO)
        request(
            vault,
            "HEAD",
            blob_path(HELLO_SHA256, size=len(HELLO)),
            token=vault.tokens[NAMESPACE].read,
        )

        put_record, has_record = vault.records()
        assert (put_record.namespace, put_record.ref) == (NAMESPACE, HELLO_SHA256)
        assert (put_record.scope, put_record.operation) == ("write", "put-blob")
        assert (has_record.scope, has_record.operation) == ("read", "has-blob")
        assert has_record.bytes == 0


# -- the token helpers --------------------------------------------------------------------


def test_a_new_token_is_a_prefixed_base64url_secret(capsys: pytest.CaptureFixture[str]) -> None:
    tokens = {new_token() for _ in range(32)}

    assert len(tokens) == 32
    for token in tokens:
        assert re.fullmatch(VAULT_TOKEN_PATTERN, token), token
        assert len(token) == len("cvlv_") + 43

    assert main(["new-token"]) == 0
    printed = capsys.readouterr().out.strip()
    assert re.fullmatch(VAULT_TOKEN_PATTERN, printed)


def test_hashing_a_token_is_its_sha256_and_never_echoes_it() -> None:
    token = new_token()
    expected = hashlib.sha256(token.encode("ascii")).hexdigest()

    assert hash_token(token) == expected
    assert hash_token(f"{token}\n") == expected
    assert hash_token(f"{token}\r\n") == expected


@pytest.mark.parametrize(
    "text",
    ["", "\n", "not-a-token\n", "cvlv_short", f"{new_token()} {new_token()}", "cvlv_\u00e9" * 8],
)
def test_hashing_refuses_anything_that_is_not_one_token(text: str) -> None:
    with pytest.raises(StorageError) as refused:
        hash_token(text)

    assert refused.value.code == "STORAGE.PROTOCOL"
    assert not text.strip() or text.strip() not in str(refused.value)


def test_hash_token_reads_one_line_from_standard_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = new_token()
    monkeypatch.setattr("sys.stdin", _Stdin(f"{token}\n"))

    assert main(["hash-token"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == hash_token(token)
    assert token not in captured.out and token not in captured.err


def test_hash_token_refuses_an_empty_or_enormous_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", _Stdin(""))
    assert main(["hash-token"]) == 2
    assert "nothing arrived" in json.loads(capsys.readouterr().err)["message"]

    monkeypatch.setattr("sys.stdin", _Stdin("x" * (storage_server.MAX_TOKEN_INPUT_BYTES + 1)))
    assert main(["hash-token"]) == 2
    assert "at most" in json.loads(capsys.readouterr().err)["message"]


class _Stdin:
    """The only part of ``sys.stdin`` ``hash-token`` uses."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self, size: int = -1) -> str:
        return self._text if size < 0 else self._text[:size]

    def isatty(self) -> bool:
        return False


# -- the command line ---------------------------------------------------------------------


def test_serving_refuses_an_unusable_port(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)
    configuration = write_configuration(tmp_path / "vault.json", config)

    status = main(
        [
            "serve",
            "--config",
            str(configuration),
            "--data-root",
            str(data_root),
            "--access-log",
            str(access_log),
            "--port",
            "70000",
        ]
    )

    assert status == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["error_code"] == "STORAGE.PROTOCOL"
    assert "a port is 0 to 65535" in failure["message"]


def test_serving_an_unusable_configuration_prints_one_json_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(
        [
            "serve",
            "--config",
            str(tmp_path / "absent.json"),
            "--data-root",
            str(tmp_path / "data"),
            "--access-log",
            str(tmp_path / "access.log"),
        ]
    )

    assert status == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure == {
        "service": "vault",
        "error_code": "STORAGE.UNAVAILABLE",
        "message": failure["message"],
    }


def test_a_subcommand_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main([])

    assert exited.value.code == 2
    assert "the following arguments are required" in capsys.readouterr().err


def test_the_module_is_the_entry_point_and_shuts_down_on_sigterm(tmp_path: Path) -> None:
    config, _, data_root, access_log = provisioned(tmp_path)
    configuration = write_configuration(tmp_path / "vault.json", config)
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "cole_voice_lab.storage_server",
            "serve",
            "--config",
            str(configuration),
            "--data-root",
            str(data_root),
            "--access-log",
            str(access_log),
            "--provision",
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=tmp_path,
    )
    try:
        line = cast(Any, process.stdout).readline()
        address = json.loads(line)["listening"]
        host, port = address.rsplit(":", 1)
        connection = HTTPConnection(host, int(port), timeout=5.0)
        connection.request("GET", "/healthz")
        answer = connection.getresponse().read()
        connection.close()
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failed shutdown
            process.kill()
            process.communicate(timeout=30)

    assert answer == b"ok"
    assert process.returncode == 0
    assert output == "" and errors == ""
    assert [record.operation for record in read_access_log(access_log)] == ["health"]


def test_importing_the_server_loads_no_recording_capture_or_catalog_module(
    tmp_path: Path,
) -> None:
    probe = (
        "import sys\n"
        "import cole_voice_lab.storage_server\n"
        "print('\\n'.join(sorted(name for name in sys.modules"
        " if name.partition('.')[0] == 'cole_voice_lab')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.split())
    assert loaded == {
        "cole_voice_lab",
        "cole_voice_lab.canonical",
        "cole_voice_lab.errors",
        "cole_voice_lab.models",
        "cole_voice_lab.paths",
        "cole_voice_lab.storage_contracts",
        "cole_voice_lab.storage_local",
        "cole_voice_lab.storage_ports",
        "cole_voice_lab.storage_server",
    }


def test_the_server_module_imports_only_the_standard_library_and_its_storage_modules() -> None:
    allowed_package = {
        "canonical",
        "errors",
        "models",
        "paths",
        "storage_contracts",
        "storage_local",
        "storage_ports",
    }
    allowed_external = set(sys.stdlib_module_names) | {"__future__", "pydantic"}
    source = Path(storage_server.__file__).read_text(encoding="utf-8")
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


# -- the status and message tables --------------------------------------------------------


def test_every_storage_code_has_one_status_and_one_client_message() -> None:
    assert set(storage_server._STORE_STATUS) == set(STORAGE_RETRYABLE)
    assert set(storage_server._STORE_MESSAGES) == set(STORAGE_RETRYABLE)
    for code, status in storage_server._STORE_STATUS.items():
        assert 400 <= status <= 599, code
        assert re.fullmatch(STORAGE_CODE_PATTERN, code), code
        message = storage_server._STORE_MESSAGES[code]
        assert message and message == message.strip()


def test_every_route_and_method_pair_names_one_operation() -> None:
    kinds = {kind for kind, _ in storage_server._OPERATIONS}
    assert kinds == set(storage_server._ALLOWED_METHODS)
    for kind, allowed in storage_server._ALLOWED_METHODS.items():
        methods = {method for route, method in storage_server._OPERATIONS if route == kind}
        assert methods == set(allowed.split(", "))
    assert storage_server._WRITE_OPERATIONS <= set(storage_server._OPERATIONS.values())


@pytest.mark.parametrize(
    ("metadata", "problem"),
    [
        pytest.param((stat.S_IFREG | 0o600, 1, 0), "is not a directory", id="file-for-directory"),
        pytest.param((stat.S_IFDIR | 0o700, 1, 999_999), "is owned by uid", id="foreign"),
        pytest.param((stat.S_IFDIR | 0o750, 1, None), "group or other", id="group"),
    ],
)
def test_a_path_that_is_not_owned_and_private_is_refused(
    metadata: tuple[int, int, int | None], problem: str
) -> None:
    mode, links, uid = metadata
    synthetic = os.stat_result(
        (mode, 2, 3, links, os.getuid() if uid is None else uid, 20, 4, 5, 6, 7)
    )

    with pytest.raises(StorageIntegrityError, match=problem):
        storage_server._require_private(synthetic, Path("/vault/data"), directory=True)


def test_a_file_with_two_links_is_refused() -> None:
    synthetic = os.stat_result((stat.S_IFREG | 0o600, 2, 3, 2, os.getuid(), 20, 4, 5, 6, 7))

    with pytest.raises(StorageIntegrityError, match="has 2 hard links"):
        storage_server._require_private(synthetic, Path("/vault/access.log"), directory=False)

    private = os.stat_result((stat.S_IFREG | 0o600, 2, 3, 1, os.getuid(), 20, 4, 5, 6, 7))
    assert storage_server._private_problem(private, directory=False) is None


def test_a_non_ascii_value_is_never_equal_to_a_hash() -> None:
    assert storage_server._ascii_digest_equal("a" * 64, "a" * 64)
    assert not storage_server._ascii_digest_equal("é" * 64, "a" * 64)


def compare_digest_calls(node: ast.AST) -> int:
    """How many ``hmac.compare_digest`` calls a piece of the module's source makes."""
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "compare_digest"
    )


def equality_operands(node: ast.AST) -> set[str]:
    """Every name and attribute either side of an ``==`` or ``!=`` under ``node``."""
    found: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Compare) or not any(
            isinstance(operator, ast.Eq | ast.NotEq) for operator in child.ops
        ):
            continue
        for side in (child.left, *child.comparators):
            for part in ast.walk(side):
                if isinstance(part, ast.Name):
                    found.add(part.id)
                elif isinstance(part, ast.Attribute):
                    found.add(part.attr)
    return found


def test_a_secret_is_compared_only_as_a_hash_in_constant_time() -> None:
    """Timing cannot be asserted, so the source is: nothing secret reaches ``==``."""
    module = ast.parse(Path(storage_server.__file__).read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(module) if isinstance(node, ast.FunctionDef)}
    secrets = {
        "_read_token_sha256",
        "_write_token_sha256",
        "read_token_sha256",
        "write_token_sha256",
        "allowed_user_logins",
        "token_sha256",
        "_token_digest",
        "_ascii_digest_equal",
        "hash_token",
    }

    assert compare_digest_calls(functions["_ascii_digest_equal"]) == 1
    assert compare_digest_calls(functions["scope_for"]) == 0
    assert equality_operands(functions["scope_for"]) == set()
    assert {
        call.func.id
        for call in ast.walk(functions["scope_for"])
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    } == {"_ascii_digest_equal"}
    assert compare_digest_calls(functions["_put_blob"]) == 1
    assert equality_operands(module) & secrets == set()


def test_a_configuration_that_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.json"
    path.write_text("{}", encoding="utf-8")

    def unreadable(descriptor: int, size: int) -> bytes:
        raise OSError(5, "input/output error")

    with monkeypatch.context() as patched:
        patched.setattr(storage_server.os, "read", unreadable)
        with pytest.raises(StorageError, match="cannot be read") as refused:
            load_server_configuration(path)

    assert refused.value.code == "STORAGE.UNAVAILABLE"


def test_a_refused_request_whose_body_stops_early_is_still_answered(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        host, port = vault.address
        with closing(socket.create_connection((host, port), timeout=5.0)) as stream:
            stream.sendall(
                b"PUT " + blob_path(HELLO_SHA256).encode("ascii") + b" HTTP/1.1\r\n"
                b"Host: vault\r\n"
                b"Authorization: Bearer " + new_token().encode("ascii") + b"\r\n"
                b"Content-Length: 4096\r\n\r\nhel"
            )
            stream.shutdown(socket.SHUT_WR)
            answer = drain(stream)

        assert "401" in answer.splitlines()[0]
        assert "STORAGE.UNAUTHORIZED" in answer
        assert vault.records()[-1].status == 401


def test_a_put_of_stored_bytes_that_the_store_refuses_reserves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running(tmp_path) as vault:
        put_blob(vault, HELLO)
        namespace = vault.server.namespaces[NAMESPACE]
        before = namespace.stored_bytes

        def refuse(_data: bytes) -> BlobRef:
            raise StorageError("STORAGE.UNAVAILABLE", "the disk went away")

        monkeypatch.setattr(namespace.blobs, "put", refuse)
        answer = put_blob(vault, HELLO)

        assert (answer.status, answer.code) == (503, "STORAGE.UNAVAILABLE")
        assert namespace.stored_bytes == before


def test_a_saturation_refusal_that_cannot_be_written_is_still_recorded(tmp_path: Path) -> None:
    with running(tmp_path) as vault:
        first, second = socket.socketpair()
        with closing(first):
            second.close()

            vault.server._refuse_saturated(first)

        refusals = [record for record in vault.records() if record.status == 503]
        assert len(refusals) == 1
        assert refusals[0].code == "STORAGE.UNAVAILABLE"


def test_the_module_runs_as_a_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["storage_server", "new-token"])

    with warnings.catch_warnings():
        # runpy warns that the module is already imported; running it again is the point.
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as exited:
            runpy.run_module("cole_voice_lab.storage_server", run_name="__main__")

    assert exited.value.code == 0
    assert re.fullmatch(VAULT_TOKEN_PATTERN, capsys.readouterr().out.strip())

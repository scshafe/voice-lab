"""Shared helpers for the Step 9 vault tests: a fake Keychain, a fake tailnet, a loopback vault.

A helper module (not a test module): ``tests/test_storage_binding.py`` and
``tests/test_vault_cli.py`` import it. Nothing here touches the real login Keychain, a real
tailnet peer or a network: the Keychain is a dictionary, the resolver answers from a recorded
value, and the only server is one this process starts on the loopback.

The loopback seam lives here, in the tests, and nowhere else: ``open_library_vault`` takes a
layout and nothing more, so no command, flag or environment variable can reach
``VaultConnection.loopback_for_tests``. ``install_vault_seams`` is what a test uses to put the
fake Keychain, resolver and connection factory in place of the module-level seams
``storage_binding`` calls.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import cole_voice_lab.storage_binding as binding_module
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.storage_contracts import (
    VAULT_NODE_TAG,
    VaultAccessRecord,
    VaultDestinationApproval,
    VaultNamespaceConfig,
    VaultRequestRecord,
    VaultServerConfig,
)
from cole_voice_lab.storage_http_client import (
    ExpectedIdentity,
    ResolvedPeer,
    TailnetResolver,
    VaultConnection,
)
from cole_voice_lab.storage_ports import StorageError, storage_error
from cole_voice_lab.storage_server import VaultServer, hash_token, new_token

LIBRARY_ID = "lib-20260917-0123456789ab"
NAMESPACE = "cole"
TAILNET_SUFFIX = "example-tailnet.ts.net"
ENDPOINT = f"voice-lab-vault.{TAILNET_SUFFIX}"
NODE_STABLE_ID = "nQRJBE7CNTRL"
APPROVAL_ID = "vault-2026-09-17"
ALL_CLASSES = ("derived-edits", "prompt-database", "recordings", "records", "training")
COPIED_CLASSES = ("derived-edits", "prompt-database", "recordings", "records")
QUOTA = 64 * 1024 * 1024
# A synthetic key, in the hex form the Keychain item holds. Never a real library's key.
SYNTHETIC_KEY_HEX = bytes(range(0x20, 0x40)).hex()


# -- the Keychain ----------------------------------------------------------------------------


class MemoryKeychain:
    """A ``KeychainClient`` in memory (copied from ``tests/test_storage_sealing.py``).

    ``failure`` makes every read fail, which is how a Keychain that cannot be reached is
    played; ``items`` is the store a test inspects.
    """

    def __init__(self, *, failure: StorageError | None = None) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []
        self.failure = failure

    def add_generic_password(self, *, service: str, account: str, label: str, secret: str) -> None:
        self.calls.append(f"add {service}")
        if (service, account) in self.items:
            raise storage_error("STORAGE.CONFLICT", f"the Keychain already holds item {service}")
        self.items[(service, account)] = secret

    def find_generic_password(self, *, service: str, account: str) -> str:
        self.calls.append(f"find {service}")
        if self.failure is not None:
            raise self.failure
        if (service, account) not in self.items:
            raise storage_error("STORAGE.NOT_BOUND", f"the Keychain has no item {service}")
        return self.items[(service, account)]

    def has_generic_password(self, *, service: str, account: str) -> bool:
        self.calls.append(f"has {service}")
        if self.failure is not None:
            raise self.failure
        return (service, account) in self.items

    def secrets(self) -> tuple[str, ...]:
        """Every secret the Keychain holds; a test greps output for these."""
        return tuple(self.items.values())


# -- the tailnet -----------------------------------------------------------------------------


@dataclass
class FakeResolver:
    """A ``TailnetResolver`` that answers from a recorded peer; it runs nothing."""

    peer: ResolvedPeer
    endpoints: list[str] = field(default_factory=list)
    failure: StorageError | None = None

    def resolve(self, endpoint: str) -> ResolvedPeer:
        self.endpoints.append(endpoint)
        if self.failure is not None:
            raise self.failure
        return self.peer


def approved_peer(**changes: Any) -> ResolvedPeer:
    """The peer the approval below names; ``changes`` plays a node that is not it."""
    fields: dict[str, Any] = {
        "address": "100.96.1.7",
        "node_stable_id": NODE_STABLE_ID,
        "tags": (VAULT_NODE_TAG,),
        "dns_name": f"{ENDPOINT}.",
    }
    return ResolvedPeer(**(fields | changes))


# -- the seams -------------------------------------------------------------------------------


@dataclass
class ConnectionRequest:
    """What ``storage_binding`` asked its connection factory for."""

    endpoint: str
    namespace: str
    token: Callable[[], str]
    resolver: TailnetResolver
    identity: ExpectedIdentity
    request_log: Callable[[VaultRequestRecord], None]


@dataclass
class VaultSeams:
    """The fakes in place of ``storage_binding``'s Keychain, resolver and connection."""

    keychain: MemoryKeychain
    resolver: FakeResolver
    port: int | None
    connections: list[ConnectionRequest] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)

    def connect(self, **arguments: Any) -> VaultConnection:
        """Build the loopback connection a test's server answers.

        The identity, endpoint and resolver the binding chose are recorded rather than used:
        a loopback client speaks plain HTTP to this process and checks no tailnet identity.
        The production factory (``storage_binding.vault_connection``) builds the real client,
        which does both, and nothing but this test wrapper can reach the loopback one.
        """
        request = ConnectionRequest(**arguments)
        self.connections.append(request)
        if self.port is None:
            raise storage_error("STORAGE.UNAVAILABLE", "no loopback vault is running")
        return VaultConnection.loopback_for_tests(
            self.port, request.namespace, request.token, request_log=request.request_log
        )

    def resolve_for(self, tailnet_suffix: str) -> TailnetResolver:
        self.suffixes.append(tailnet_suffix)
        return self.resolver


def install_vault_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keychain: MemoryKeychain | None = None,
    resolver: FakeResolver | None = None,
    port: int | None = None,
) -> VaultSeams:
    """Put the fake Keychain, resolver and connection factory in place for one test."""
    seams = VaultSeams(
        keychain=MemoryKeychain() if keychain is None else keychain,
        resolver=FakeResolver(approved_peer()) if resolver is None else resolver,
        port=port,
    )
    monkeypatch.setattr(binding_module, "keychain_client", lambda: seams.keychain)
    monkeypatch.setattr(binding_module, "tailnet_resolver", seams.resolve_for)
    monkeypatch.setattr(binding_module, "vault_connection", seams.connect)
    return seams


def store_library_key(keychain: MemoryKeychain, library_id: str = LIBRARY_ID) -> str:
    """Put the synthetic library key in the fake Keychain, as ``vault key create`` would."""
    from cole_voice_lab.storage_contracts import keychain_item_names

    keychain.items[(keychain_item_names(library_id).key, "library-key")] = SYNTHETIC_KEY_HEX
    return SYNTHETIC_KEY_HEX


def store_vault_token(
    keychain: MemoryKeychain,
    scope: str = "write",
    *,
    token: str | None = None,
    library_id: str = LIBRARY_ID,
) -> str:
    """Put one vault token in the fake Keychain, as ``vault token create`` would."""
    from cole_voice_lab.storage_binding import VAULT_TOKEN_ACCOUNTS
    from cole_voice_lab.storage_contracts import keychain_item_names

    names = keychain_item_names(library_id)
    secret = new_token() if token is None else token
    service = names.token if scope == "write" else names.read_token
    keychain.items[(service, VAULT_TOKEN_ACCOUNTS[scope])] = secret
    return secret


# -- records ---------------------------------------------------------------------------------


def approval_record(
    *, now: datetime | None = None, library_id: str = LIBRARY_ID, **changes: Any
) -> VaultDestinationApproval:
    """A standing destination approval that is effective when the test runs."""
    approved_at = (datetime.now(UTC) if now is None else now) - timedelta(hours=1)
    fields: dict[str, Any] = {
        "approval_id": APPROVAL_ID,
        "approved": True,
        "library_id": library_id,
        "endpoint": ENDPOINT,
        "tailnet_suffix": TAILNET_SUFFIX,
        "node_stable_id": NODE_STABLE_ID,
        "namespace": NAMESPACE,
        "classes": COPIED_CLASSES,
        "approved_at": approved_at,
        "expires_at": approved_at + timedelta(days=365),
        "approved_by": "Cole",
        "statement": "Encrypted copies of this library may be kept on the vault node.",
    }
    return VaultDestinationApproval(**(fields | changes))


def approval_arguments(approval: VaultDestinationApproval) -> list[str]:
    """The ``vault approve`` flags that build exactly ``approval``."""
    return [
        "--approval-id",
        approval.approval_id,
        "--endpoint",
        approval.endpoint,
        "--tailnet-suffix",
        approval.tailnet_suffix,
        "--node-stable-id",
        approval.node_stable_id,
        "--namespace",
        approval.namespace,
        "--classes",
        *approval.classes,
        "--approved-at",
        approval.approved_at.isoformat(),
        "--expires-at",
        approval.expires_at.isoformat(),
        "--approved-by",
        approval.approved_by,
        "--statement",
        approval.statement,
    ]


def write_approval(layout: AssetLayout, approval: VaultDestinationApproval) -> tuple[Path, str]:
    """Write one approval the way ``vault approve`` does (write-once, 0400)."""
    from cole_voice_lab.storage_binding import write_destination_approval

    return write_destination_approval(layout, approval)


def approval_file_digest(approval: VaultDestinationApproval) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(approval) + b"\n").hexdigest()


def initialized_library(root: Path, *, library_id: str | None = LIBRARY_ID) -> AssetLayout:
    """An empty asset root with ``provenance/vault/library.json`` already written."""
    from cole_voice_lab.storage_binding import initialize_library

    layout = AssetLayout.initialize(root)
    initialize_library(layout, library_id=library_id)
    return layout


# -- the loopback vault server ----------------------------------------------------------------


@dataclass(frozen=True)
class RunningVault:
    """A vault server answering on this machine's loopback, with its tokens."""

    server: VaultServer
    data_root: Path
    access_log: Path
    write_token: str
    read_token: str

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def access_records(self) -> tuple[VaultAccessRecord, ...]:
        lines = self.access_log.read_bytes().decode("utf-8").splitlines()
        return tuple(VaultAccessRecord.model_validate_json(line) for line in lines)


@contextmanager
def running_vault(
    tmp_path: Path, *, namespaces: Sequence[str] = (NAMESPACE,), quota: int = QUOTA
) -> Iterator[RunningVault]:
    """Serve one vault on the loopback in a thread, then shut it down and close it."""
    tokens: Mapping[str, tuple[str, str]] = {
        name: (new_token(), new_token()) for name in namespaces
    }
    config = VaultServerConfig(
        namespaces=tuple(
            VaultNamespaceConfig(
                name=name,
                read_token_sha256=hash_token(tokens[name][0]),
                write_token_sha256=hash_token(tokens[name][1]),
                quota_bytes=quota,
            )
            for name in namespaces
        ),
        max_workers=4,
        request_timeout_seconds=30,
    )
    data_root = tmp_path / "vault-data"
    access_log = tmp_path / "vault-access.jsonl"
    server = VaultServer(
        ("127.0.0.1", 0),
        config=config,
        data_root=data_root,
        access_log=access_log,
        provision=True,
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        read, write = tokens[namespaces[0]]
        yield RunningVault(
            server=server,
            data_root=data_root,
            access_log=access_log,
            write_token=write,
            read_token=read,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
        server.server_close()
        assert not thread.is_alive()


# -- a synthetic library ------------------------------------------------------------------------


def write_library_file(root: Path, relative: str, data: bytes, mode: int = 0o600) -> Path:
    """One file of a synthetic library, with the mode its writer publishes."""
    path = root / relative
    missing = [
        directory for directory in (path.parent, *path.parent.parents) if not directory.exists()
    ]
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    path.write_bytes(data)
    path.chmod(mode)
    return path


# The synthetic library a Step 9 test pushes: a few copied records of every restore mode.
# A compact stand-in for ``tests/vault_library_fixtures.add_records`` that pulls in none of the
# recording fixtures -- enough for a push, a verify and a restore to have something to carry.
PRIVATE_LIBRARY_FILES: Mapping[str, bytes] = {
    "provenance/consents/cole-self-v1.json": b'{"consent":"synthetic"}\n',
    "provenance/headphone-reviews/review-1.json": b'{"review":1}\n',
    "provenance/transfer-approvals/approval-1.json": b'{"approval":1}\n',
}
READ_ONLY_LIBRARY_FILES: Mapping[str, bytes] = {
    "provenance/pilot-gates/gate-1.json": b'{"gate":1}\n',
    "provenance/diagnostic-root.json": b'{"root":"synthetic"}\n',
    "raw/loose-note.txt": b"a file beside the raw sessions\n",
    "references/loose-note.txt": b"a file beside the references\n",
}
COPIED_LIBRARY_FILES: Mapping[str, bytes] = {
    **PRIVATE_LIBRARY_FILES,
    **READ_ONLY_LIBRARY_FILES,
}
# Mac-only: a waveform cache the vault never carries.
MAC_ONLY_LIBRARY_FILE = f"derived/recording-waveforms/{'0' * 64}-v2.cvlwave"


def add_library_records(layout: AssetLayout) -> Mapping[str, bytes]:
    """Write the synthetic library's files; returns the copied ones by relative path."""
    for relative, data in PRIVATE_LIBRARY_FILES.items():
        write_library_file(layout.root, relative, data)
    for relative, data in READ_ONLY_LIBRARY_FILES.items():
        write_library_file(layout.root, relative, data, mode=0o400)
    write_library_file(layout.root, MAC_ONLY_LIBRARY_FILE, b"\x00")
    return COPIED_LIBRARY_FILES


def restore_permissions(root: Path) -> None:
    """Make every directory 0700 and file 0600 again, so ``tmp_path`` can be removed."""
    import os

    for directory, directories, files in os.walk(root):
        for name in (*directories, *files):
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)

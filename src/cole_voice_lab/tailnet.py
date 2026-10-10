"""Which tailnet node answers for a name: the identity check every tailnet client makes.

@canonical tailnet -- ExpectedIdentity names the node an approval bound (stable id, tag,
tailnet suffix); a TailnetResolver says which node answers for an endpoint right now;
identity_problem compares the two; TailscaleCliResolver asks the local Tailscale client over
two read-only commands (``status --json``, ``whois --json``). The vault client
(storage_http_client.py, D-020) and the home-host intake client (intake_client.py, D-021)
both check the node with it before a byte is sent. Moved here from storage_http_client.py in
phase 2 so that the intake client need not import a storage adapter; the vault client
re-exports every name, so nothing that used it there changes.

It imports only the standard library and the package's ``storage_contracts`` and
``storage_ports`` modules.
"""

from __future__ import annotations

import ipaddress
import json
import os
import pwd
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, cast

from .storage_contracts import (
    NODE_STABLE_ID_PATTERN,
    TAILSCALE_TAG_PATTERN,
    StorageNameError,
    validate_tailnet_endpoint,
)
from .storage_ports import storage_error

__all__ = [
    "HTTPS_PORT",
    "MAX_TAILSCALE_OUTPUT_BYTES",
    "TAILNET_RANGE",
    "TAILSCALE_BINARY",
    "TAILSCALE_TIMEOUT_SECONDS",
    "ExpectedIdentity",
    "ResolvedPeer",
    "TailnetResolver",
    "TailscaleCliResolver",
    "identity_problem",
    "platform_tailscale_binary",
]

HTTPS_PORT: Final = 443
# The macOS app binary, not /usr/local/bin/tailscale (a shell wrapper), and not a PATH
# lookup: the service process has no HOME and no /usr/local/bin on its PATH.
MACOS_TAILSCALE_BINARY: Final = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
# The distribution package's client on Linux (Arch, Debian and Ubuntu all install it here).
LINUX_TAILSCALE_BINARY: Final = Path("/usr/bin/tailscale")
TAILSCALE_TIMEOUT_SECONDS: Final = 10.0
# A tailnet of any size answers ``status --json`` well inside this; more is not read as JSON.
MAX_TAILSCALE_OUTPUT_BYTES: Final = 4 * 1024 * 1024
TAILSCALE_ENVIRONMENT_PATH: Final = "/usr/bin:/bin"
# Tailscale's own range (100.64.0.0/10): the CLI resolver takes the peer's address from it.
TAILNET_RANGE: Final = ipaddress.ip_network("100.64.0.0/10")
_NODE_STABLE_ID = re.compile(NODE_STABLE_ID_PATTERN)
_TAILSCALE_TAG = re.compile(TAILSCALE_TAG_PATTERN)

type CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def platform_tailscale_binary(platform: str = sys.platform) -> Path:
    """The Tailscale client's absolute path on this platform: the macOS app or Linux's."""
    return MACOS_TAILSCALE_BINARY if platform == "darwin" else LINUX_TAILSCALE_BINARY


# The binary the resolver runs unless a caller names another.
TAILSCALE_BINARY: Final = platform_tailscale_binary()


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
    node's own name, which Tailscale reports with a trailing dot. ``online`` is whether the
    tailnet's coordination server last saw the node connected (``None`` when the resolver
    cannot say): a node that sleeps or travels (the Air's vault) is checked for it before a
    copy is attempted, never instead of the identity check.
    """

    address: str
    node_stable_id: str
    tags: tuple[str, ...]
    dns_name: str
    port: int = HTTPS_PORT
    online: bool | None = None


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


def identity_problem(endpoint: str, identity: ExpectedIdentity, peer: ResolvedPeer) -> str | None:
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
    """A fixed environment with the passwd database's home, since the service has none.

    ``SHLVL`` is set because the macOS app's binary answers as the CLI only when it sees
    one: without it, it tries to start the GUI and prints "The Tailscale GUI failed to
    start" instead of JSON (found in phase 2 on Tailscale 1.102.3, from a terminal, an SSH
    session and a launchd job alike). The Linux client ignores it.
    """
    return {
        "PATH": TAILSCALE_ENVIRONMENT_PATH,
        "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        "SHLVL": "1",
    }


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
                f"this host is on the tailnet {suffix!r:.120}, not {self.tailnet_suffix}",
            )
        address, online = self._peer_address(status, endpoint)
        node = _member(self._json(("whois", "--json", address), "whois"), "Node", "whois")
        return ResolvedPeer(
            address=address,
            node_stable_id=_text(node, "StableID", "whois"),
            tags=_texts(node, "Tags", "whois"),
            dns_name=_text(node, "Name", "whois"),
            online=online,
        )

    def _peer_address(self, status: Mapping[str, Any], endpoint: str) -> tuple[str, bool | None]:
        """The tailnet address of the one peer named ``endpoint``, and its ``Online`` flag."""
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
                online = named[0].get("Online")
                return candidate, online if isinstance(online, bool) else None
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

"""Tailscale as Approve uploads to my PC asks it, never run: the real resolver
(``TailscaleCliResolver``) over recorded ``status --json`` and ``whois --json`` answers, as the
backend's resolver tests record them (tests/test_storage_http_client.py). No ``tailscale``
binary runs; the names are the backend's synthetic home host (``home_host_fixtures``)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from home_host_fixtures import ENDPOINT, NODE, TAILNET

from cole_voice_lab.home_host import HOME_HOST_TAG
from cole_voice_lab.tailnet import TailnetResolver, TailscaleCliResolver

#: The home host's tailnet address (Tailscale's 100.64.0.0/10).
PEER_ADDRESS = "100.64.0.7"


def status(**changes: Any) -> dict[str, Any]:
    """``tailscale status --json`` on a machine whose tailnet has the home host and a Mac."""
    answer: dict[str, Any] = {
        "BackendState": "Running",
        "CurrentTailnet": {"Name": "cole", "MagicDNSSuffix": TAILNET, "MagicDNSEnabled": True},
        "Peer": {
            "nodekey:aaa": {
                "ID": "nodeMac",
                "DNSName": f"mac.{TAILNET}.",
                "TailscaleIPs": ["100.64.0.2"],
            },
            "nodekey:bbb": {
                "ID": NODE,
                "DNSName": f"{ENDPOINT}.",
                "TailscaleIPs": ["fd7a:115c:a1e0::7", PEER_ADDRESS],
                "Online": True,
            },
        },
    }
    return answer | changes


def whois(**changes: Any) -> dict[str, Any]:
    """``tailscale whois --json <address>`` of the home host (tagged ``tag:voice-lab``)."""
    node: dict[str, Any] = {
        "StableID": NODE,
        "Name": f"{ENDPOINT}.",
        "Tags": [HOME_HOST_TAG],
        "Addresses": [f"{PEER_ADDRESS}/32"],
    }
    return {"Node": node | changes, "UserProfile": {"LoginName": "tagged-devices"}}


def completed(stdout: object, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return subprocess.CompletedProcess(args=["tailscale"], returncode=returncode, stdout=text)


#: ``tailscale status`` while tailscaled is not running ("failed to connect to local
#: tailscaled"): it exits 1 and prints nothing on stdout.
NOT_RUNNING = completed("", returncode=1)


@dataclass
class FakeTailscale:
    """Recorded answers for the commands the resolver runs, in order; it runs nothing.
    ``factory`` is what ``approval.propose`` takes as its ``resolver_factory``."""

    answers: list[Any] = field(default_factory=lambda: [completed(status()), completed(whois())])
    calls: list[list[str]] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], **_options: Any) -> Any:
        self.calls.append(list(argv)[1:])
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def factory(self, tailnet_suffix: str) -> TailnetResolver:
        self.suffixes.append(tailnet_suffix)
        return TailscaleCliResolver(tailnet_suffix=tailnet_suffix, runner=self)

    def answer(self, *answers: Any) -> FakeTailscale:
        """The answers for the next lookup (each lookup takes ``status`` then ``whois``)."""
        self.answers.extend(answers)
        return self


def again(fake: FakeTailscale) -> FakeTailscale:
    """The same answers for one more lookup."""
    return fake.answer(completed(status()), completed(whois()))

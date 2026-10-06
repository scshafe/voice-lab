"""Shared fixtures of the home-host intake tests: a test issuer, a server, a client root.

Tokens are signed with a throwaway RSA key generated per test session (``cryptography`` is
a dev dependency; ``tests/`` may import it). Packages are synthetic (``synthetic_capture``).
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from cole_voice_lab.api.auth import AuthPolicy, TokenVerifier, TrustedIssuer
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.home_host import HomeHostApproval, approve, record_sha256
from cole_voice_lab.intake_client import HomeHostClient
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.paths import AssetLayout

ISSUER = "https://id.example.invalid"
AUDIENCE = "voice-lab-desktop"
SUBJECT = "subject-cole"
KID = "test-key-1"
ENDPOINT = "voice-lab.example.ts.net"
TAILNET = "example.ts.net"
NODE = "nTESTNODE11CNTRL"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwks(key: rsa.RSAPrivateKey = _KEY, kid: str = KID) -> dict[str, Any]:
    numbers = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": b64url(numbers.e.to_bytes(3, "big")),
            }
        ]
    }


def mint(
    claims: dict[str, Any] | None = None,
    *,
    key: rsa.RSAPrivateKey = _KEY,
    header: dict[str, Any] | None = None,
    now: float | None = None,
) -> str:
    moment = time.time() if now is None else now
    body = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "iat": int(moment),
        "exp": int(moment) + 600,
        **(claims or {}),
    }
    head = {"alg": "RS256", "typ": "JWT", "kid": KID, **(header or {})}
    signing_input = f"{b64url(json.dumps(head).encode())}.{b64url(json.dumps(body).encode())}"
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64url(signature)}"


def other_key() -> rsa.RSAPrivateKey:
    return _OTHER_KEY


def policy(jwks_path: Path) -> AuthPolicy:
    return AuthPolicy(
        issuers=(TrustedIssuer(issuer=ISSUER, audiences=(AUDIENCE,), jwks_path=str(jwks_path)),),
        allowed_subjects=(SUBJECT,),
    )


@dataclass
class Rig:
    """A running API server over a server root, and a client root with a bound approval."""

    server_layout: AssetLayout
    client_layout: AssetLayout
    server: ApiServer
    staging: IntakeStaging
    jwks_path: Path
    work: Path

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def client(self, token: str | None = None) -> HomeHostClient:
        chosen = token or mint()
        return HomeHostClient.loopback_for_tests(self.port, lambda: chosen)

    def publish(self) -> None:
        publish_pending(self.server_layout, min_free_bytes=0)


def make_approval(**changes: Any) -> HomeHostApproval:
    fields: dict[str, Any] = {
        "approval_id": "pc-home-host",
        "approved": True,
        "endpoint": ENDPOINT,
        "tailnet_suffix": TAILNET,
        "node_stable_id": NODE,
        "classes": ("capture",),
        "approved_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=300),
        "approved_by": "Cole",
        "statement": "Uploads of my own captures to my own PC.",
        **changes,
    }
    return HomeHostApproval.model_validate(fields)


def bind_approval(layout: AssetLayout, approval: HomeHostApproval | None = None) -> None:
    chosen = approval or make_approval()
    approve(layout, chosen, reviewed_sha256=record_sha256(chosen), now=NOW)


def start_rig(tmp_path: Path) -> Iterator[Rig]:
    server_layout = AssetLayout.initialize(tmp_path / "srv" / "voice-lab" / "lab")
    client_layout = AssetLayout.initialize(tmp_path / "air" / "voice-lab" / "lab")
    bind_approval(client_layout)
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    staging = IntakeStaging(server_layout, min_free_bytes=0)
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=staging,
        verifier=TokenVerifier(policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    work = tmp_path / "work"
    work.mkdir()
    try:
        yield Rig(server_layout, client_layout, server, staging, jwks_path, work)
    finally:
        server.shutdown()
        server.server_close()

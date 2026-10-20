"""The contract fixture: the PC's API and a fake Pocket ID, for a corpus client's live check.

@canonical contract_fixture -- ``python -m cole_voice_lab.contract_fixture serve --root <empty
dir> [--port 0] [--idp-port 0] [--tls-cert … --tls-key …] [--report <file>]`` (D-056,
docs/design/HOUSE-PANEL.md §4). The house panel's live check starts it, as its lexinet check
starts lexinet, and drives it through the contract bundle (``contracts/corpus-client.v1/``):

- **The real API server** (``api.server``): staging, the routes and the scoped verifier, so a
  corpus client's token reaches exactly what it reaches on the PC (``api.client_scope``).
- **The real validator, admission and publisher pass**, in-process after each finalize and
  each decision (on the PC, intake's units run them).
- **A catalog seeded through the composition root**, as ``voice-lab catalog init --subject-id
  cole-self --from-builtin pilot-v2`` seeds it: the bundle's fixture catalog, so the golden
  take is admitted.
- **A fake Pocket ID that behaves as v2.13.0 does** (HOUSE-PANEL.md §1):
  - device authorization, and polling answered ``authorization_pending`` with no
    ``slow_down`` (the fixture approves a code itself after ``--approve-after`` polls) and
    still ``authorization_pending`` after the code expires;
  - a ``resource`` parameter refused, since the client must send none;
  - access tokens with an array ``aud`` and ``typ: at+jwt``, lasting 15 minutes;
  - refresh rotation with no grace: reusing a rotated refresh token revokes its whole family;
  - a JWKS. Its RS256 key is made at start with the standard library, so no key is in Git.
- **``--admit-simulated``** admits a simulated take: the fixture's own switch
  (``corpus_takes.admitting_simulated_takes``), which no production entry point has.

It is a test double for a client's development: it serves on loopback by default, keeps every
file under ``--root``, and is never deployed.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import secrets
import signal
import ssl
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl

from .api.auth import _SHA256_DIGEST_INFO, AuthPolicy, TokenVerifier, TrustedIssuer
from .api.client_scope import CLIENT_PROFILE
from .api.contracts import ApiMeta, IntakeSessionStatus
from .api.corpus_routes import CorpusRoutes
from .api.routing import RouteRequest, RouteResponse
from .api.server import ApiServer
from .api.staging import IntakeStaging
from .canonical import canonical_json_bytes
from .contract_bundle import CLIENT_AUDIENCE, fixture_catalog
from .corpus_takes import admitting_simulated_takes
from .intake_publisher import publish_pending
from .models import COLE_SUBJECT
from .paths import AssetLayout

DESKTOP_AUDIENCE: Final = "voice-lab-desktop"
#: The fake Pocket ID's user: the token subject the fixture's API allows.
FIXTURE_USER: Final = "fixture-user-cole"
NOMINAL_ISSUER: Final = "https://id.fixture.invalid"
TOKEN_SECONDS: Final = 15 * 60
DEVICE_CODE_SECONDS: Final = 15 * 60
POLL_INTERVAL_SECONDS: Final = 5
_MAX_FORM_BYTES: Final = 16 * 1024
_SMALL_PRIMES: Final = (
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97,
)  # fmt: skip


# -- RS256 with the standard library -------------------------------------------------------------


def _probable_prime(candidate: int, rounds: int = 40) -> bool:
    """Miller-Rabin with random bases: false positives below 4**-rounds."""
    if candidate < 2:
        return False
    for prime in _SMALL_PRIMES:
        if candidate % prime == 0:
            return candidate == prime
    exponent, odd = 0, candidate - 1
    while odd % 2 == 0:
        exponent, odd = exponent + 1, odd // 2
    for _ in range(rounds):
        witness = pow(secrets.randbelow(candidate - 3) + 2, odd, candidate)
        if witness in (1, candidate - 1):
            continue
        for _ in range(exponent - 1):
            witness = pow(witness, 2, candidate)
            if witness == candidate - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _probable_prime(candidate):
            return candidate


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class SigningKey:
    """An RSA key made at start, for the fake Pocket ID's RS256 tokens; never written out."""

    kid: str
    modulus: int
    exponent: int
    private: int = field(repr=False)

    @classmethod
    def generate(cls, bits: int = 2048) -> SigningKey:
        """A new key of ``bits`` bits, public exponent 65537."""
        exponent = 65_537
        while True:
            first, second = _prime(bits // 2), _prime(bits // 2)
            phi = (first - 1) * (second - 1)
            if first != second and phi % exponent and (first * second).bit_length() == bits:
                break
        modulus = first * second
        kid = hashlib.sha256(modulus.to_bytes(bits // 8, "big")).hexdigest()[:16]
        return cls(kid=kid, modulus=modulus, exponent=exponent, private=pow(exponent, -1, phi))

    def jwks(self) -> dict[str, Any]:
        size = (self.modulus.bit_length() + 7) // 8
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.kid,
                    "n": _b64url(self.modulus.to_bytes(size, "big")),
                    "e": _b64url(self.exponent.to_bytes(3, "big")),
                }
            ]
        }

    def sign(self, data: bytes) -> bytes:
        """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 §8.2.1)."""
        size = (self.modulus.bit_length() + 7) // 8
        digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(data).digest()
        encoded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
        signature = pow(int.from_bytes(encoded, "big"), self.private, self.modulus)
        return signature.to_bytes(size, "big")

    def jwt(self, claims: Mapping[str, Any], *, typ: str = "at+jwt") -> str:
        header = {"alg": "RS256", "typ": typ, "kid": self.kid}
        signing_input = (
            f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
            f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
        )
        return f"{signing_input}.{_b64url(self.sign(signing_input.encode('ascii')))}"


# -- The fake Pocket ID --------------------------------------------------------------------------


@dataclass
class _DeviceCode:
    user_code: str
    client_id: str
    scope: str
    expires_at: float
    polls: int = 0
    redeemed: bool = False


@dataclass
class _Family:
    client_id: str
    scope: str
    current: str
    revoked: bool = False


class FakePocketId:
    """Pocket ID v2.13.0's device-code grant and refresh rotation, as the panel meets them."""

    def __init__(
        self,
        *,
        issuer: str,
        subject: str = FIXTURE_USER,
        clients: Sequence[str] = (CLIENT_AUDIENCE,),
        approve_after: int = 1,
        interval: int = POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
        key: SigningKey | None = None,
    ) -> None:
        self.issuer = issuer
        self.subject = subject
        self.clients = frozenset(clients)
        self.approve_after = approve_after
        self.interval = interval
        self.clock = clock
        self.key = key or SigningKey.generate()
        self._lock = threading.Lock()
        self._codes: dict[str, _DeviceCode] = {}
        #: Every refresh token issued, to its family (a rotated one stays, to catch reuse).
        self._refresh: dict[str, str] = {}
        self._families: dict[str, _Family] = {}

    @staticmethod
    def _error(code: str, description: str) -> tuple[int, dict[str, Any]]:
        return 400, {"error": code, "error_description": description}

    def authorize(
        self, form: Mapping[str, str], verification_uri: str
    ) -> tuple[int, dict[str, Any]]:
        """``POST /api/oidc/device/authorize``."""
        client_id = form.get("client_id", "")
        if client_id not in self.clients:
            return self._error("invalid_client", "no such public client")
        if "resource" in form:
            return self._error(
                "invalid_request",
                "the corpus client sends no resource: with one, aud would name it instead",
            )
        user_code = "E" + "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(7)
        )
        device_code = secrets.token_urlsafe(32)
        with self._lock:
            self._codes[device_code] = _DeviceCode(
                user_code=user_code,
                client_id=client_id,
                scope=form.get("scope", ""),
                expires_at=self.clock() + DEVICE_CODE_SECONDS,
            )
        return 200, {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": f"{verification_uri}?user_code={user_code}",
            "expires_in": DEVICE_CODE_SECONDS,
            "interval": self.interval,
        }

    def token(self, form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        """``POST /api/oidc/token``: the device-code grant, then refresh rotation."""
        grant = form.get("grant_type")
        with self._lock:
            if grant == "urn:ietf:params:oauth:grant-type:device_code":
                return self._device_grant(form)
            if grant == "refresh_token":
                return self._refresh_grant(form)
        return self._error("unsupported_grant_type", "the device code or a refresh token")

    def _device_grant(self, form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        code = self._codes.get(form.get("device_code", ""))
        if code is None or code.redeemed or code.client_id != form.get("client_id"):
            return self._error("invalid_grant", "no such device code")
        code.polls += 1
        # v2.13: an expired code keeps answering authorization_pending; the client stops.
        if self.clock() >= code.expires_at or code.polls <= self.approve_after:
            return self._error("authorization_pending", "the user has not approved yet")
        code.redeemed = True
        family = secrets.token_hex(16)
        self._families[family] = _Family(client_id=code.client_id, scope=code.scope, current="")
        return 200, self._issue(family)

    def _refresh_grant(self, form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        presented = form.get("refresh_token", "")
        family_id = self._refresh.get(presented, "")
        family = self._families.get(family_id)
        if family is None or family.revoked or family.client_id != form.get("client_id"):
            return self._error("invalid_grant", "the refresh token is not valid")
        if presented != family.current:
            # v2.13 has no grace period: a rotated token used again revokes the whole family.
            family.revoked = True
            return self._error("invalid_grant", "a rotated refresh token was used again")
        return 200, self._issue(family_id)

    def _issue(self, family_id: str) -> dict[str, Any]:
        family = self._families[family_id]
        now = int(self.clock())
        refresh = secrets.token_urlsafe(32)
        family.current = refresh
        self._refresh[refresh] = family_id
        claims = {
            "iss": self.issuer,
            "sub": self.subject,
            "aud": [family.client_id],
            "exp": now + TOKEN_SECONDS,
            "iat": now,
            "client_id": family.client_id,
            "scope": family.scope,
            "scp": family.scope,
            "jti": secrets.token_hex(16),
        }
        return {
            "access_token": self.key.jwt(claims),
            "token_type": "Bearer",
            "expires_in": TOKEN_SECONDS,
            "refresh_token": refresh,
            "scope": family.scope,
        }

    def discovery(self, base_url: str) -> dict[str, Any]:
        """``/.well-known/openid-configuration``."""
        return {
            "issuer": self.issuer,
            "device_authorization_endpoint": f"{base_url}/api/oidc/device/authorize",
            "token_endpoint": f"{base_url}/api/oidc/token",
            "jwks_uri": f"{base_url}/.well-known/jwks.json",
            "grant_types_supported": [
                "authorization_code",
                "refresh_token",
                "urn:ietf:params:oauth:grant-type:device_code",
            ],
            "id_token_signing_alg_values_supported": ["RS256"],
        }


class _IdpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], idp: FakePocketId) -> None:
        self.idp = idp
        self.base_url = ""
        super().__init__(address, _IdpHandler)


class _IdpHandler(BaseHTTPRequestHandler):
    server: _IdpServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (base signature)
        """Silent: the fixture's output is its report."""

    def _answer(self, status: int, body: Mapping[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's naming)
        idp = self.server.idp
        if self.path == "/.well-known/openid-configuration":
            self._answer(200, idp.discovery(self.server.base_url))
        elif self.path == "/.well-known/jwks.json":
            self._answer(200, idp.key.jwks())
        else:
            self._answer(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_FORM_BYTES:
            self._answer(413, {"error": "invalid_request"})
            return
        form = dict(parse_qsl(self.rfile.read(length).decode("utf-8"), keep_blank_values=True))
        idp = self.server.idp
        if self.path == "/api/oidc/device/authorize":
            self._answer(*idp.authorize(form, f"{self.server.base_url}/device"))
        elif self.path == "/api/oidc/token":
            self._answer(*idp.token(form))
        else:
            self._answer(404, {"error": "not_found"})


# -- The API, publishing in-process --------------------------------------------------------------


class _Publisher:
    """The intake publisher's pass, one at a time, as the PC's units run it."""

    def __init__(self, layout: AssetLayout, *, admit_simulated: bool) -> None:
        self.layout = layout
        self.admit_simulated = admit_simulated
        self._lock = threading.Lock()

    def __call__(self) -> None:
        with self._lock:
            with admitting_simulated_takes() if self.admit_simulated else contextlib.nullcontext():
                publish_pending(self.layout, min_free_bytes=0)


class _PublishingStaging(IntakeStaging):
    """Staging whose finalize is followed by a publisher pass (its answer is unchanged:
    ``finalized``; the client then reads the session's outcome)."""

    def __init__(self, layout: AssetLayout, publish: _Publisher) -> None:
        super().__init__(layout, min_free_bytes=0)
        self._publish = publish

    def finalize(self, session_id: str, *, profile: str | None = None) -> IntakeSessionStatus:
        status = super().finalize(session_id, profile=profile)
        self._publish()
        return status


class _PublishingRoutes:
    """The corpus routes, with a publisher pass after each queued decision."""

    def __init__(self, routes: CorpusRoutes, publish: _Publisher) -> None:
        self._routes = routes
        self._publish = publish

    def __call__(self, request: RouteRequest) -> RouteResponse | None:
        answer = self._routes(request)
        if (
            answer is not None
            and request.method == "POST"
            and request.path.endswith("/decision")
            and answer.status == 202
        ):
            self._publish()
        return answer


@dataclass
class ContractFixture:
    """A running fixture: its API and its fake Pocket ID."""

    root: Path
    layout: AssetLayout
    api: ApiServer
    idp_server: _IdpServer
    idp: FakePocketId
    report: dict[str, Any]
    _threads: list[threading.Thread] = field(default_factory=list)

    def start(self) -> None:
        for server in (self.api, self.idp_server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        """Stop serving (``shutdown`` would wait forever for a server never started) and
        release the sockets."""
        for server in (self.api, self.idp_server):
            if self._threads:
                server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join()
        self._threads.clear()


def _seed_catalog(layout: AssetLayout) -> None:
    """The bundle's fixture catalog, published through the composition root as ``voice-lab
    catalog init`` publishes it."""
    from . import cli

    with contextlib.redirect_stdout(io.StringIO()):
        code = cli.main(
            [
                "catalog",
                "init",
                "--subject-id",
                COLE_SUBJECT,
                "--from-builtin",
                "pilot-v2",
                "--asset-root",
                str(layout.root),
            ]
        )
    if code != 0:
        raise RuntimeError("the fixture catalog could not be seeded")


def _url(scheme: str, host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}:{port}"


def build_fixture(
    root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    idp_port: int = 0,
    tls: tuple[Path, Path] | None = None,
    approve_after: int = 1,
    interval: int = POLL_INTERVAL_SECONDS,
    admit_simulated: bool = False,
    key: SigningKey | None = None,
) -> ContractFixture:
    """A fixture under ``root`` (an empty directory), bound but not serving yet.

    Raises: ``ValueError`` for a ``root`` that is not an empty directory."""
    if not root.is_dir() or root.is_symlink() or any(root.iterdir()):
        raise ValueError(f"the fixture's root must be an empty directory: {root}")
    layout = AssetLayout.initialize(root / "lab")
    _seed_catalog(layout)
    context: ssl.SSLContext | None = None
    if tls is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(tls[0]), keyfile=str(tls[1]))
    scheme = "https" if context is not None else "http"
    idp = FakePocketId(
        issuer=NOMINAL_ISSUER, approve_after=approve_after, interval=interval, key=key
    )
    idp_server = _IdpServer((host, idp_port), idp)
    idp_url = _url(scheme, host, idp_server.server_address[1])
    idp_server.base_url = idp_url
    # Over TLS the issuer is the fake's own URL, as Pocket ID's APP_URL is its issuer; over
    # plain HTTP it stays nominal, since an issuer is an https URL (api.auth.TrustedIssuer).
    issuer = idp.issuer = idp_url if scheme == "https" else NOMINAL_ISSUER
    state = root / "state"
    state.mkdir(mode=0o700)
    jwks = state / "jwks.json"
    jwks.write_bytes(canonical_json_bytes(idp.key.jwks()) + b"\n")
    verifier = TokenVerifier(
        AuthPolicy(
            issuers=(
                TrustedIssuer(
                    issuer=issuer,
                    audiences=(DESKTOP_AUDIENCE,),
                    client_audiences=(CLIENT_AUDIENCE,),
                    jwks_path=str(jwks),
                ),
            ),
            allowed_subjects=(FIXTURE_USER,),
        )
    )
    publish = _Publisher(layout, admit_simulated=admit_simulated)
    api = ApiServer(
        (host, port),
        staging=_PublishingStaging(layout, publish),
        verifier=verifier,
        meta=ApiMeta(server_build="contract-fixture", minimum_client_version="0.1.0"),
        log=False,
        routes=(_PublishingRoutes(CorpusRoutes(layout), publish),),
    )
    if context is not None:
        # The handshake runs in each request's thread, never in the accepting one.
        for server in (api, idp_server):
            server.socket = context.wrap_socket(
                server.socket, server_side=True, do_handshake_on_connect=False
            )
    reference, _ = fixture_catalog()
    report = {
        "contract_id": "corpus-client.v1",
        "api_url": _url(scheme, host, api.server_address[1]),
        "issuer": issuer,
        "oidc": idp.discovery(idp_url),
        "client_id": CLIENT_AUDIENCE,
        "subject": FIXTURE_USER,
        "capture_profile": CLIENT_PROFILE,
        "catalog": reference.model_dump(mode="json"),
        "approve_after_polls": approve_after,
        "admit_simulated": admit_simulated,
        "root": str(root),
        "pid": os.getpid(),
    }
    return ContractFixture(root, layout, api, idp_server, idp, report)


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextlib.contextmanager
def _until_stopped() -> Iterator[threading.Event]:
    stopped = threading.Event()
    previous = {
        signum: signal.signal(signum, lambda *_: stopped.set())
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield stopped
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m cole_voice_lab.contract_fixture serve …``: serve until stopped."""
    parser = argparse.ArgumentParser(prog="python -m cole_voice_lab.contract_fixture")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="the API and a fake Pocket ID, until stopped")
    serve.add_argument("--root", type=Path, required=True, help="an empty directory")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--idp-port", type=int, default=0)
    serve.add_argument("--tls-cert", type=Path)
    serve.add_argument("--tls-key", type=Path)
    serve.add_argument("--report", type=Path, help="where to write the fixture's URLs")
    serve.add_argument("--approve-after", type=int, default=1, help="pending polls first")
    serve.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS)
    serve.add_argument("--admit-simulated", action="store_true")
    serve.add_argument("--once", action="store_true", help="bind, report, and exit")
    options = parser.parse_args(argv)
    if (options.tls_cert is None) != (options.tls_key is None):
        parser.error("--tls-cert and --tls-key go together")
    tls = (options.tls_cert, options.tls_key) if options.tls_cert is not None else None
    try:
        fixture = build_fixture(
            options.root,
            host=options.host,
            port=options.port,
            idp_port=options.idp_port,
            tls=tls,
            approve_after=options.approve_after,
            interval=options.interval,
            admit_simulated=options.admit_simulated,
        )
    except (OSError, ValueError, RuntimeError, ssl.SSLError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    report = json.dumps(fixture.report, indent=2)
    if options.report is not None:
        _write_report(options.report, fixture.report)
    else:
        sys.stdout.write(report + "\n")
    sys.stdout.flush()
    if options.once:
        fixture.close()
        return 0
    fixture.start()
    with _until_stopped() as stopped:
        stopped.wait()
    fixture.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

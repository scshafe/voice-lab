"""Fakes as real HTTP servers on 127.0.0.1: Pocket ID, and a server that answers nothing.

FakeIssuer (as mailroom-desktop's) checks PKCE S256, the exact redirect_uri
and the code, and rotates refresh tokens. QuietServer stands for the settings'
server where a test needs one that exists: it answers ``404`` and records what
it was asked, so a test can say that nothing was sent. (voice-journey's intake
behind its door, which the capture client's compatibility transport spoke, went
with that transport in phase 4.)
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def fake_jwt(claims: dict) -> str:
    signature = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    return f"{b64({'alg': 'RS256', 'kid': 'k1'})}.{b64(claims)}.{signature}"


class FakeIssuer:
    """Discovery, /authorize (records, redirects with the code) and the token endpoint."""

    def __init__(self, client_id: str = "voice-journey-capture") -> None:
        self.client_id = client_id
        self.authorize_params: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, bool] = {}
        self.issued_access: list[str] = []
        self.expires_in = 3600
        self.tamper_state = False
        self.issue_refresh = True
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        )
        self.on_access_token = lambda token: None
        #: Mints the access token instead of a fake one (a test whose API verifies signatures).
        self.access_factory: Any = None

    @property
    def issuer(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> FakeIssuer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _tokens(self, nonce: str | None) -> dict:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": "sub-owner",
            "iat": now,
            "exp": now + self.expires_in,
        }
        access = (
            self.access_factory()
            if self.access_factory
            else fake_jwt({**claims, "jti": secrets.token_hex(8)})
        )
        id_claims = {**claims, "email": "owner@example.test"}
        if nonce is not None:
            id_claims["nonce"] = nonce
        out = {
            "access_token": access,
            "id_token": fake_jwt(id_claims),
            "token_type": "Bearer",
            "expires_in": self.expires_in,
        }
        if self.issue_refresh:
            rt = "rt-" + secrets.token_hex(8)
            self.refresh_tokens[rt] = True
            out["refresh_token"] = rt
        self.issued_access.append(access)
        self.on_access_token(access)
        return out

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def _json(self, status: int, data: dict) -> None:
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                if parts.path == "/.well-known/openid-configuration":
                    return self._json(
                        200,
                        {
                            "issuer": fake.issuer,
                            "authorization_endpoint": f"{fake.issuer}/authorize",
                            "token_endpoint": f"{fake.issuer}/api/oidc/token",
                            "end_session_endpoint": f"{fake.issuer}/api/oidc/end-session",
                            "scopes_supported": [
                                "openid",
                                "profile",
                                "email",
                                "groups",
                                "offline_access",
                            ],
                            "code_challenge_methods_supported": ["plain", "S256"],
                        },
                    )
                if parts.path == "/authorize":
                    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
                    with fake.lock:
                        fake.authorize_params.append(params)
                        code = "code-" + secrets.token_hex(8)
                        fake.codes[code] = params
                    state = "tampered" if fake.tamper_state else params["state"]
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"{params['redirect_uri']}?{urlencode({'code': code, 'state': state})}",
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._json(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                with fake.lock:
                    fake.token_requests.append(form)
                if (
                    urlsplit(self.path).path != "/api/oidc/token"
                    or form.get("client_id") != fake.client_id
                ):
                    return self._json(400, {"error": "invalid_client"})
                if form.get("grant_type") == "authorization_code":
                    params = fake.codes.pop(form.get("code", ""), None)
                    if params is None:
                        return self._json(400, {"error": "invalid_grant"})
                    if form.get("redirect_uri") != params["redirect_uri"]:
                        return self._json(
                            400, {"error": "invalid_grant", "error_description": "redirect_uri"}
                        )
                    challenge = (
                        base64.urlsafe_b64encode(
                            hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                        )
                        .rstrip(b"=")
                        .decode()
                    )
                    if params.get("code_challenge_method") != "S256" or challenge != params.get(
                        "code_challenge"
                    ):
                        return self._json(
                            400, {"error": "invalid_grant", "error_description": "pkce"}
                        )
                    return self._json(200, fake._tokens(params.get("nonce")))
                if form.get("grant_type") == "refresh_token":
                    rt = form.get("refresh_token", "")
                    if not fake.refresh_tokens.pop(rt, False):
                        return self._json(400, {"error": "invalid_grant"})
                    return self._json(200, fake._tokens(None))
                return self._json(400, {"error": "unsupported_grant_type"})

        return Handler


@dataclass
class Seen:
    method: str
    path: str


class QuietServer:
    """A server that answers every request ``404 not_found`` and records what it was asked:
    the settings' server when a test only needs one that exists (and needs to know that
    nothing was sent to it)."""

    def __init__(self) -> None:
        self.requests: list[Seen] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> QuietServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _answer(self) -> None:
                with fake.lock:
                    fake.requests.append(Seen(self.command, urlsplit(self.path).path))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                body = json.dumps({"error": "not_found", "detail": "no such route"}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_HEAD = _answer  # noqa: N815

        return Handler

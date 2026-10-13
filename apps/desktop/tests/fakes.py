"""Fakes as real HTTP servers on 127.0.0.1: Pocket ID, and voice-journey's intake behind its door.

FakeIssuer (as mailroom-desktop's) checks PKCE S256, the exact redirect_uri
and the code, and rotates refresh tokens. FakeIntake follows voice-journey's
README "Intake API" and src/intake.mjs: the door's bearer check (401, or a
302 to a browser sign-in), then manifest / resumable PUT / fail-closed
finalize. Faults can be injected: a dropped connection mid-chunk, a corrupted
chunk, 503s. It never logs bodies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit


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


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.wav$")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    status: int = 0
    error: str | None = None


class FakeIntake:
    """voice-journey's intake API behind a door that accepts the issuer's bearer tokens."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.staging = self.root / "staging"
        self.measurement = self.root / "corpus" / "measurement"
        self.valid_tokens: set[str] = set()
        self.requests: list[Seen] = []
        self.manifests: dict[str, dict] = {}
        self.accepted: dict[str, dict] = {}
        self.door_redirects = False  # answer 302 to a browser sign-in instead of 401
        self.unavailable = 0  # answer this many requests with 503
        self.drop_after: int | None = None  # drop the next PUT after reading this many body bytes
        self.drop_min_start = 0  # ... but only a PUT whose range starts at or after this offset
        self.corrupt_next_put = False  # flip a byte in the next PUT body
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def intake_requests(self) -> list[Seen]:
        """The requests to the measurement intake (``/api/intake/``): what an upload sends.
        Anything else the app asks the same server for (a read of another API) is not."""
        with self.lock:
            return [seen for seen in self.requests if seen.path.startswith("/api/intake/")]

    def start(self) -> FakeIntake:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def files_dir(self, sid: str) -> Path:
        return self.staging / sid / "files"

    def progress(self, sid: str) -> list[dict]:
        out = []
        for f in self.manifests[sid]["files"]:
            done = self.files_dir(sid) / f["name"]
            part = self.files_dir(sid) / (f["name"] + ".part")
            received = (
                done.stat().st_size
                if done.exists()
                else part.stat().st_size
                if part.exists()
                else 0
            )
            out.append(
                {
                    "name": f["name"],
                    "sizeBytes": f["sizeBytes"],
                    "receivedBytes": received,
                    "complete": done.exists(),
                }
            )
        return out

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def _send(
                self, seen: Seen, status: int, data: dict, headers: dict[str, str] | None = None
            ) -> None:
                seen.status, seen.error = status, data.get("error")
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _door(self, seen: Seen) -> bool:
                if fake.unavailable > 0:
                    fake.unavailable -= 1
                    self._body()
                    self._send(seen, 503, {"error": "unavailable"})
                    return False
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in fake.valid_tokens:
                    self._body()
                    if fake.door_redirects:
                        self._send(seen, 302, {}, {"Location": "/oauth2/start?rd=%2F"})
                    else:
                        self._send(seen, 401, {"error": "unauthorized"})
                    return False
                if "cookie" in {k.lower() for k in self.headers.keys()}:
                    self._body()
                    self._send(seen, 400, {"error": "cookies_not_expected"})
                    return False
                return True

            def _route(self) -> None:
                parts = urlsplit(self.path)
                seen = Seen(
                    self.command,
                    parts.path,
                    {k.lower(): v for k, v in self.headers.items() if k.lower() != "authorization"},
                )
                with fake.lock:
                    fake.requests.append(seen)
                if not self._door(seen):
                    return
                segs = parts.path.split("/")[1:]
                if segs[:3] != ["api", "intake", "sessions"]:
                    self._body()
                    return self._send(seen, 404, {"error": "not_found"})
                rest = [unquote(s) for s in segs[3:]]
                with fake.lock:
                    if not rest and self.command == "POST":
                        return self._open(seen)
                    if len(rest) == 1 and self.command == "GET":
                        return self._status(seen, rest[0])
                    if len(rest) == 2 and rest[1] == "finalize" and self.command == "POST":
                        self._body()
                        return self._finalize(seen, rest[0])
                    if len(rest) == 3 and rest[1] == "files" and self.command == "PUT":
                        return self._put(seen, rest[0], rest[2])
                self._body()
                self._send(seen, 405, {"error": "method_not_allowed"})

            do_GET = do_POST = do_PUT = _route  # noqa: N815

            def _open(self, seen: Seen) -> None:
                try:
                    manifest = json.loads(self._body())
                except ValueError:
                    return self._send(seen, 400, {"error": "invalid_json"})
                sid = manifest.get("sessionId", "")
                if not SESSION_ID_RE.match(str(sid)):
                    return self._send(seen, 422, {"error": "invalid_session_id"})
                if manifest.get("chainId") != "umik1":
                    return self._send(seen, 422, {"error": "unknown_chain"})
                kept = {
                    k: manifest[k]
                    for k in (
                        "sessionId",
                        "chainId",
                        "startedAt",
                        "endedAt",
                        "capture",
                        "segments",
                        "files",
                    )
                }
                if sid in fake.accepted:
                    if canonical(fake.accepted[sid]) == canonical(kept):
                        return self._send(seen, 200, {"sessionId": sid, "state": "accepted"})
                    return self._send(seen, 409, {"error": "session_already_accepted"})
                if sid in fake.manifests:
                    if canonical(fake.manifests[sid]) != canonical(kept):
                        return self._send(seen, 409, {"error": "session_manifest_conflict"})
                    return self._send(
                        seen, 200, {"sessionId": sid, "state": "open", "files": fake.progress(sid)}
                    )
                fake.manifests[sid] = kept
                fake.files_dir(sid).mkdir(parents=True)
                self._send(
                    seen, 201, {"sessionId": sid, "state": "open", "files": fake.progress(sid)}
                )

            def _status(self, seen: Seen, sid: str) -> None:
                if sid in fake.accepted:
                    return self._send(seen, 200, {"sessionId": sid, "state": "accepted"})
                if sid not in fake.manifests:
                    return self._send(seen, 404, {"error": "session_not_found"})
                self._send(
                    seen, 200, {"sessionId": sid, "state": "open", "files": fake.progress(sid)}
                )

            def _put(self, seen: Seen, sid: str, name: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if sid not in fake.manifests:
                    self._body()
                    return self._send(seen, 404, {"error": "session_not_found"})
                entry = next((f for f in fake.manifests[sid]["files"] if f["name"] == name), None)
                if entry is None:
                    self._body()
                    return self._send(seen, 404, {"error": "file_not_in_manifest"})
                final = fake.files_dir(sid) / name
                part = fake.files_dir(sid) / (name + ".part")
                if final.exists():
                    self._body()
                    return self._send(
                        seen,
                        200,
                        {
                            "name": name,
                            "state": "already_received",
                            "sizeBytes": final.stat().st_size,
                        },
                    )
                rng = self.headers.get("Content-Range")
                start = 0
                if rng is not None:
                    m = re.match(r"^bytes (\d+)-(\d+)/(\d+)$", rng)
                    if not m:
                        self._body()
                        return self._send(seen, 400, {"error": "invalid_content_range"})
                    start, end, total = map(int, m.groups())
                    if total != entry["sizeBytes"] or end < start or end >= total:
                        self._body()
                        return self._send(seen, 416, {"error": "content_range_out_of_bounds"})
                    have = part.stat().st_size if part.exists() else 0
                    if start != have:
                        self._body()
                        return self._send(
                            seen, 409, {"error": "range_not_contiguous", "receivedBytes": have}
                        )
                else:
                    if length != entry["sizeBytes"]:
                        self._body()
                        return self._send(seen, 422, {"error": "size_mismatch"})
                    part.unlink(missing_ok=True)
                if fake.drop_after is not None and start >= fake.drop_min_start:
                    keep = self.rfile.read(min(fake.drop_after, length))
                    fake.drop_after = None
                    if start > 0:
                        with open(part, "ab") as fh:
                            fh.write(keep)
                    else:
                        part.unlink(missing_ok=True)
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                body = self._body()
                if fake.corrupt_next_put and body:
                    fake.corrupt_next_put = False
                    body = bytes([body[0] ^ 0xFF]) + body[1:]
                with open(part, "ab" if start > 0 else "wb") as fh:
                    fh.write(body)
                size = part.stat().st_size
                if size < entry["sizeBytes"]:
                    return self._send(
                        seen, 202, {"name": name, "state": "partial", "receivedBytes": size}
                    )
                if hashlib.sha256(part.read_bytes()).hexdigest() != entry["sha256"]:
                    part.unlink()
                    return self._send(seen, 422, {"error": "checksum_mismatch", "name": name})
                part.rename(final)
                self._send(
                    seen, 200, {"name": name, "state": "received", "sizeBytes": entry["sizeBytes"]}
                )

            def _finalize(self, seen: Seen, sid: str) -> None:
                if sid in fake.accepted:
                    return self._send(seen, 200, {"sessionId": sid, "state": "accepted"})
                if sid not in fake.manifests:
                    return self._send(seen, 404, {"error": "session_not_found"})
                missing, bad = [], []
                for f in fake.manifests[sid]["files"]:
                    path = fake.files_dir(sid) / f["name"]
                    if not path.exists():
                        missing.append(f["name"])
                    elif hashlib.sha256(path.read_bytes()).hexdigest() != f["sha256"]:
                        bad.append(f["name"])
                if bad:
                    for name in bad:
                        (fake.files_dir(sid) / name).unlink()
                    return self._send(seen, 422, {"error": "checksum_mismatch", "files": bad})
                if missing:
                    return self._send(seen, 409, {"error": "incomplete", "missing": missing})
                fake.measurement.mkdir(parents=True, exist_ok=True)
                fake.files_dir(sid).rename(fake.measurement / sid)
                fake.accepted[sid] = fake.manifests.pop(sid)
                self._send(
                    seen,
                    200,
                    {
                        "sessionId": sid,
                        "state": "accepted",
                        "files": len(fake.accepted[sid]["files"]),
                    },
                )

        return Handler

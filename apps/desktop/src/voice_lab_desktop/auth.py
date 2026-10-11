"""Signing in with Pocket ID: a public OIDC client, PKCE (S256), a loopback redirect (RFC 8252).

``sign_in`` opens the system browser at the issuer's authorization endpoint and
waits for exactly one ``GET /callback`` on ``http://127.0.0.1:<port>``; the
code is exchanged with the PKCE verifier (no client secret). The state and the
ID token's nonce, issuer and audience are checked before anything is kept.
``Session`` holds the tokens, refreshes them (single-flight) and saves them in
the keychain.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .config import FALLBACK_PORT, Config
from .tokens import Tokens, TokenStore, jwt_claims

#: How long the browser has to come back (the person signs in with a passkey).
CALLBACK_TIMEOUT = 300.0
#: Requests the loopback server answers before giving up (favicon, a stray reload, ...).
MAX_LOOPBACK_REQUESTS = 8
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class AuthError(Exception):
    """Signing in or refreshing failed; the message is safe to show (no token, no code)."""


class SignInRequired(AuthError):
    """No usable tokens: the person has to sign in (again)."""


@dataclass(frozen=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    end_session_endpoint: str | None
    scopes_supported: tuple[str, ...]


def discover(config: Config, http: httpx.Client) -> Discovery:
    issuer = config.require_issuer()
    response = http.get(f"{issuer}/.well-known/openid-configuration", timeout=HTTP_TIMEOUT)
    if response.status_code != 200:
        raise AuthError(f"the issuer's discovery document answered HTTP {response.status_code}")
    doc = response.json()
    if doc.get("issuer") != issuer:
        raise AuthError("the discovery document names a different issuer")
    for key in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(doc.get(key), str) or urlsplit(doc[key]).scheme not in ("https", "http"):
            raise AuthError(f"the discovery document has no usable {key}")
    if "S256" not in (doc.get("code_challenge_methods_supported") or ["S256"]):
        raise AuthError("the issuer does not support PKCE S256")
    return Discovery(
        issuer=doc["issuer"],
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        end_session_endpoint=doc.get("end_session_endpoint"),
        scopes_supported=tuple(doc.get("scopes_supported") or ()),
    )


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge): 64 random bytes, base64url without padding (RFC 7636)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    server: LoopbackReceiver

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        self.server.requests_seen += 1
        parts = urlsplit(self.path)
        if parts.path != "/callback" or self.server.result is not None:
            self._reply(404, "Not found.")
            return
        params = {
            k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items() if len(v) == 1
        }
        self.server.result = params
        # Never reflect a parameter back: the page is fixed text.
        self._reply(200, "voice-lab-desktop: you can close this tab and return to the app.")

    def _reply(self, status: int, text: str) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(
        self, format: str, *args: object
    ) -> None:  # the query holds the code: never log
        return


class LoopbackReceiver(HTTPServer):
    """One callback on 127.0.0.1 (never all interfaces), then closed."""

    allow_reuse_address = False

    def __init__(self, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _CallbackHandler)
        self.result: dict[str, str] | None = None
        self.requests_seen = 0

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    def wait(
        self, timeout: float = CALLBACK_TIMEOUT, cancelled: threading.Event | None = None
    ) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        self.timeout = 0.5
        try:
            while self.result is None:
                if cancelled is not None and cancelled.is_set():
                    raise AuthError("sign-in cancelled")
                if time.monotonic() > deadline:
                    raise AuthError("the browser did not come back in time; sign in again")
                if self.requests_seen >= MAX_LOOPBACK_REQUESTS:
                    raise AuthError("too many requests reached the sign-in listener; sign in again")
                self.handle_request()
            return self.result
        finally:
            self.server_close()


def open_receiver(fixed_port: bool) -> LoopbackReceiver:
    """An ephemeral port (Pocket ID's wildcard callback) unless ``fixed_port``; the fixed
    port if an ephemeral one fails."""
    if not fixed_port:
        try:
            return LoopbackReceiver(0)
        except OSError:
            pass
    try:
        return LoopbackReceiver(FALLBACK_PORT)
    except OSError as exc:
        raise AuthError(
            f"cannot listen on 127.0.0.1:{FALLBACK_PORT} for the sign-in callback ({exc.strerror})"
        ) from exc


def sign_in(
    config: Config,
    http: httpx.Client,
    open_browser: Callable[[str], object],
    *,
    fixed_port: bool = False,
    timeout: float = CALLBACK_TIMEOUT,
    cancelled: threading.Event | None = None,
    now: Callable[[], float] = time.time,
) -> Tokens:
    disc = discover(config, http)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    receiver = open_receiver(fixed_port)
    scopes = ["openid", "email", "profile"]
    if "offline_access" in disc.scopes_supported:
        scopes.append("offline_access")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": receiver.redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    try:
        open_browser(f"{disc.authorization_endpoint}?{query}")
        params = receiver.wait(timeout, cancelled)
    finally:
        receiver.server_close()
    if not secrets.compare_digest(params.get("state", ""), state):
        raise AuthError("the sign-in answer does not belong to this sign-in (state mismatch)")
    if "error" in params:
        error = "".join(c for c in params["error"] if c.isalnum() or c in "_-")[:64]
        raise AuthError(f"Pocket ID refused the sign-in ({error})")
    code = params.get("code")
    if not code:
        raise AuthError("the sign-in answer carries no code")
    response = http.post(
        disc.token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": receiver.redirect_uri,
            "client_id": config.client_id,
            "code_verifier": verifier,
        },
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    return _tokens_from(response, config, disc, now(), expected_nonce=nonce)


def refresh(
    config: Config, http: httpx.Client, tokens: Tokens, now: Callable[[], float] = time.time
) -> Tokens:
    if not tokens.refresh_token:
        raise SignInRequired("the session has ended; sign in again")
    disc = discover(config, http)
    response = http.post(
        disc.token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": config.client_id,
        },
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code in (400, 401):
        raise SignInRequired("the session has ended; sign in again")
    fresh = _tokens_from(response, config, disc, now(), expected_nonce=None)
    if fresh.refresh_token is None:  # not rotated: keep the one we have
        fresh = Tokens(fresh.access_token, fresh.id_token, fresh.expires_at, tokens.refresh_token)
    return fresh


def _tokens_from(
    response: httpx.Response,
    config: Config,
    disc: Discovery,
    issued: float,
    expected_nonce: str | None,
) -> Tokens:
    if response.status_code != 200:
        raise AuthError(f"Pocket ID's token endpoint answered HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError("Pocket ID's token endpoint answered something other than JSON") from exc
    if str(body.get("token_type", "")).lower() != "bearer":
        raise AuthError("Pocket ID answered without a bearer token")
    access, id_token = body.get("access_token"), body.get("id_token")
    if not isinstance(access, str) or not isinstance(id_token, str) or not access or not id_token:
        raise AuthError("Pocket ID answered without an access token and an ID token")
    try:
        claims = jwt_claims(id_token)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthError("Pocket ID's ID token is not a JWT") from exc
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get("iss") != disc.issuer or config.client_id not in audiences:
        raise AuthError("the ID token is for a different issuer or client")
    if expected_nonce is not None and not secrets.compare_digest(
        str(claims.get("nonce", "")), expected_nonce
    ):
        raise AuthError("the ID token does not belong to this sign-in (nonce mismatch)")
    lifetime = body.get("expires_in")
    lifetime = float(lifetime) if isinstance(lifetime, (int, float)) and lifetime > 0 else 300.0
    refresh_token = (
        body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
    )
    return Tokens(
        access_token=access,
        id_token=id_token,
        expires_at=issued + lifetime,
        refresh_token=refresh_token,
    )


class Session:
    """The current tokens: loaded from the store, refreshed when expired, saved after every
    change."""

    def __init__(self, config: Config, store: TokenStore, http: httpx.Client) -> None:
        self.config = config
        self.store = store
        self.http = http
        self._lock = threading.Lock()
        self._tokens = store.load()

    @property
    def signed_in(self) -> bool:
        return self._tokens is not None

    @property
    def tokens(self) -> Tokens | None:
        """The current tokens (for display: the account's name); never log them."""
        return self._tokens

    def adopt(self, tokens: Tokens) -> None:
        with self._lock:
            self.store.save(tokens)
            self._tokens = tokens

    def bearer(self) -> str:
        """A bearer token that is not expired, refreshing first if needed."""
        with self._lock:
            if self._tokens is None:
                raise SignInRequired("not signed in")
            if self._tokens.expired():
                self._refresh_locked()
            return self._tokens.bearer_for(self.config.bearer)

    def after_unauthorized(self, used: str) -> str:
        """The server refused ``used``: refresh once (unless another thread already did) and
        return the new bearer."""
        with self._lock:
            if self._tokens is None:
                raise SignInRequired("not signed in")
            if self._tokens.bearer_for(self.config.bearer) == used:
                self._refresh_locked()
            return self._tokens.bearer_for(self.config.bearer)

    def _refresh_locked(self) -> None:
        assert self._tokens is not None
        try:
            fresh = refresh(self.config, self.http, self._tokens)
        except SignInRequired:
            self.store.clear()
            self._tokens = None
            raise
        self.store.save(fresh)
        self._tokens = fresh

    def sign_out(self) -> None:
        with self._lock:
            self.store.clear()
            self._tokens = None

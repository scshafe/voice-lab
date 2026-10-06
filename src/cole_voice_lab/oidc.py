"""Signing in to Pocket ID from a capturing host: PKCE, a loopback redirect, an OS keychain.

@canonical oidc -- the port of voice-journey-capture's ``auth.py`` (design §3.5) to the
standard library. ``sign_in`` runs the authorization-code flow with PKCE S256 and a one-shot
redirect to ``http://127.0.0.1:<port>/callback``, checks ``state``, then the ID token's
issuer, audience, nonce and lifetime, and keeps only the **refresh token**, in the OS
keychain (the macOS login Keychain through ``/usr/bin/security``, or the Secret Service
through ``secret-tool`` on Linux); never in a file. ``bearer`` exchanges it for a fresh
token at the start of each upload, so no access token outlives the command. The public
client (``voice-lab-desktop``) has no secret.

No secret is ever placed in a subprocess argument list, an environment, a log or an error
message.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import pwd
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from .errors import SecurityError, VoiceLabError

CALLBACK_PATH: Final = "/callback"
SIGN_IN_TIMEOUT_SECONDS: Final = 300.0
HTTP_TIMEOUT_SECONDS: Final = 30.0
MAX_ANSWER_BYTES: Final = 256 * 1024
KEYCHAIN_SERVICE: Final = "voice-lab.home-host.refresh-token"
SECURITY_TOOL: Final = Path("/usr/bin/security")
SECRET_TOOL: Final = Path("/usr/bin/secret-tool")
_KEYCHAIN_NOT_FOUND: Final = 44


class SignInError(VoiceLabError):
    """Signing in, or refreshing, did not produce a token; the message holds no secret."""


class TokenStore(Protocol):
    """Where a refresh token lives between commands."""

    def load(self) -> str | None:
        """The stored refresh token, or ``None``."""
        ...

    def save(self, token: str) -> None:
        """Store ``token``, replacing any earlier one."""
        ...


def _plain_token(token: str) -> str:
    if not token or len(token) > 4096 or not all(32 < ord(c) < 127 for c in token):
        raise SignInError("the identity provider returned a refresh token this cannot store")
    if any(c in token for c in "\"'\\"):
        raise SignInError("the identity provider returned a refresh token this cannot store")
    return token


@dataclass(frozen=True, slots=True)
class MacKeychainStore:
    """The login Keychain, through ``security``; the secret goes on standard input only."""

    account: str
    security: Path = SECURITY_TOOL

    def _run(
        self, arguments: list[str], stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        home = pwd.getpwuid(os.getuid()).pw_dir
        return subprocess.run(
            [str(self.security), *arguments],
            input=stdin,
            capture_output=True,
            check=False,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": home},
            cwd="/",
        )

    def load(self) -> str | None:
        completed = self._run(
            ["find-generic-password", "-a", self.account, "-s", KEYCHAIN_SERVICE, "-w"]
        )
        if completed.returncode == _KEYCHAIN_NOT_FOUND:
            return None
        if completed.returncode != 0:
            raise SignInError(f"the Keychain could not be read (status {completed.returncode})")
        return completed.stdout.decode("ascii", errors="strict").rstrip("\n")

    def save(self, token: str) -> None:
        line = (
            f"add-generic-password -U -a {self.account} -s {KEYCHAIN_SERVICE} "
            f'-l {KEYCHAIN_SERVICE} -w "{_plain_token(token)}"\n'
        )
        completed = self._run(["-i"], stdin=line.encode("ascii"))
        if completed.returncode != 0 or self.load() != token:
            raise SignInError("the Keychain did not keep the refresh token")


@dataclass(frozen=True, slots=True)
class SecretServiceStore:
    """The freedesktop Secret Service, through ``secret-tool``; the secret goes on stdin."""

    account: str
    tool: Path = SECRET_TOOL

    def _attributes(self) -> list[str]:
        return ["service", KEYCHAIN_SERVICE, "account", self.account]

    def load(self) -> str | None:
        completed = subprocess.run(
            [str(self.tool), "lookup", *self._attributes()],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0 or not completed.stdout:
            return None
        return completed.stdout.decode("ascii", errors="strict").rstrip("\n")

    def save(self, token: str) -> None:
        completed = subprocess.run(
            [str(self.tool), "store", f"--label={KEYCHAIN_SERVICE}", *self._attributes()],
            input=_plain_token(token).encode("ascii"),
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0 or self.load() != token:
            raise SignInError("the Secret Service did not keep the refresh token")


def platform_token_store(account: str, platform: str = sys.platform) -> TokenStore:
    """The OS keychain of this platform for ``account`` (``<client_id>@<issuer host>``)."""
    if platform == "darwin":
        return MacKeychainStore(account)
    return SecretServiceStore(account)


@dataclass(frozen=True, slots=True)
class Provider:
    """The parts of an OIDC discovery document this client uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str | None = None


type Fetch = Callable[[urllib.request.Request], Mapping[str, Any]]


def _fetch_json(request: urllib.request.Request) -> Mapping[str, Any]:
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(
            request, timeout=HTTP_TIMEOUT_SECONDS, context=context
        ) as answer:
            data = answer.read(MAX_ANSWER_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise SignInError(f"the identity provider answered {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        raise SignInError(
            f"the identity provider is unreachable ({type(error).__name__})"
        ) from None
    if len(data) > MAX_ANSWER_BYTES:
        raise SignInError("the identity provider's answer is too large")
    document = json.loads(data)
    if not isinstance(document, dict):
        raise SignInError("the identity provider's answer is not a JSON object")
    return document


def discover(issuer: str, fetch: Fetch = _fetch_json) -> Provider:
    """Read ``issuer``'s discovery document; it must support S256 and name itself."""
    if not issuer.startswith("https://") or issuer.endswith("/"):
        raise SignInError("an issuer is an https URL without a trailing slash")
    document = fetch(urllib.request.Request(f"{issuer}/.well-known/openid-configuration"))
    if document.get("issuer") != issuer:
        raise SignInError("the discovery document names another issuer")
    if "S256" not in (document.get("code_challenge_methods_supported") or []):
        raise SignInError("the identity provider does not support PKCE S256")
    endpoints = [document.get("authorization_endpoint"), document.get("token_endpoint")]
    if not all(isinstance(value, str) and value.startswith("https://") for value in endpoints):
        raise SignInError("the discovery document has no https endpoints")
    jwks_uri = document.get("jwks_uri")
    return Provider(
        issuer,
        str(endpoints[0]),
        str(endpoints[1]),
        jwks_uri if isinstance(jwks_uri, str) and jwks_uri.startswith("https://") else None,
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _claims(id_token: str) -> Mapping[str, Any]:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise SignInError("the identity provider returned no ID token")
    payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
    claims = json.loads(payload)
    if not isinstance(claims, dict):
        raise SignInError("the ID token's claims are not an object")
    return claims


def check_id_token(
    id_token: str, *, provider: Provider, client_id: str, nonce: str | None, now: float
) -> Mapping[str, Any]:
    """The ID token's claims, after the issuer, audience, nonce and lifetime checks.

    The signature is the API's to verify (it holds the provider's keys); this check only
    makes sure the sign-in returned a token for this client, this session and this time.
    """
    claims = _claims(id_token)
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if claims.get("iss") != provider.issuer:
        raise SignInError("the ID token names another issuer")
    if not isinstance(audiences, list) or client_id not in audiences:
        raise SignInError("the ID token is not for this client")
    if nonce is not None and claims.get("nonce") != nonce:
        raise SignInError("the ID token's nonce does not match this sign-in")
    expiry = claims.get("exp")
    if not isinstance(expiry, int | float) or expiry < now:
        raise SignInError("the ID token has expired")
    return claims


@dataclass(slots=True)
class _Callback:
    code: str | None = None
    state: str | None = None
    error: str | None = None


def _callback_server(result: _Callback, done: threading.Event) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def do_GET(self) -> None:  # noqa: N802
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != CALLBACK_PATH or done.is_set():
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parts.query)
            result.code = next(iter(query.get("code", [])), None)
            result.state = next(iter(query.get("state", [])), None)
            result.error = next(iter(query.get("error", [])), None)
            body = b"voice-lab: signed in. You can close this window.\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

    return http.server.HTTPServer(("127.0.0.1", 0), Handler)


def _token_request(provider: Provider, form: Mapping[str, str], fetch: Fetch) -> Mapping[str, Any]:
    request = urllib.request.Request(
        provider.token_endpoint,
        data=urllib.parse.urlencode(form).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    return fetch(request)


def sign_in(
    *,
    issuer: str,
    client_id: str,
    store: TokenStore,
    fetch: Fetch = _fetch_json,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = SIGN_IN_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.time,
) -> Mapping[str, Any]:
    """Sign in in the browser and keep the refresh token; returns the ID token's claims."""
    provider = discover(issuer, fetch)
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state, nonce = _b64url(secrets.token_bytes(24)), _b64url(secrets.token_bytes(24))
    result, done = _Callback(), threading.Event()
    server = _callback_server(result, done)
    redirect = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": "openid profile email offline_access",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"{provider.authorization_endpoint}?{query}"
        sys.stderr.write(f"Opening the sign-in page; if no browser opens, visit:\n  {url}\n")
        open_browser(url)
        if not done.wait(timeout):
            raise SignInError("the sign-in did not finish in time")
    finally:
        server.shutdown()
        server.server_close()
    if result.error is not None or result.code is None:
        raise SignInError(f"the sign-in was refused ({result.error or 'no code'})")
    if result.state is None or not secrets.compare_digest(result.state, state):
        raise SignInError("the sign-in answer is for another request (state mismatch)")
    tokens = _token_request(
        provider,
        {
            "grant_type": "authorization_code",
            "code": result.code,
            "redirect_uri": redirect,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        fetch,
    )
    id_token, refresh = tokens.get("id_token"), tokens.get("refresh_token")
    if not isinstance(id_token, str) or not isinstance(refresh, str):
        raise SignInError("the identity provider returned no ID token or no refresh token")
    claims = check_id_token(
        id_token, provider=provider, client_id=client_id, nonce=nonce, now=clock()
    )
    store.save(refresh)
    return claims


def bearer(
    *,
    issuer: str,
    client_id: str,
    store: TokenStore,
    fetch: Fetch = _fetch_json,
    clock: Callable[[], float] = time.time,
) -> str:
    """A fresh bearer token for this command, from the stored refresh token.

    The access token is sent when it is a JWT for this client (Pocket ID's are: RS256,
    audience the client id), as voice-journey-capture does; otherwise the ID token. Either
    is checked for issuer, audience and lifetime here, and again by the door and the API.
    A rotated refresh token is stored before anything else, so a failed check never loses it.
    """
    refresh = store.load()
    if refresh is None:
        raise SignInError("not signed in: run `voice-lab home-host sign-in` first")
    provider = discover(issuer, fetch)
    tokens = _token_request(
        provider,
        {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id},
        fetch,
    )
    rotated = tokens.get("refresh_token")
    if isinstance(rotated, str) and rotated != refresh:
        store.save(rotated)
    problem = "the refresh returned no token for this client; sign in again"
    for name in ("access_token", "id_token"):
        candidate = tokens.get(name)
        if not isinstance(candidate, str) or candidate.count(".") != 2:
            continue
        try:
            check_id_token(
                candidate, provider=provider, client_id=client_id, nonce=None, now=clock()
            )
        except (SignInError, ValueError) as error:
            problem = f"the refreshed {name.replace('_', ' ')} is unusable: {error}"
            continue
        return candidate
    raise SignInError(problem)


def read_bearer_file(path: Path) -> str:
    """A bearer token from a private file (tests and acceptance runs only).

    Raises: ``SecurityError`` when the file is not a regular, private (0600 or stricter)
    file of this user, or does not hold one token.
    """
    metadata = path.lstat()
    if (
        not path.is_file()
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise SecurityError(f"a bearer file is a private regular file of this user: {path}")
    token = path.read_text(encoding="ascii").strip()
    if not token or len(token) > 16 * 1024 or any(c.isspace() for c in token):
        raise SecurityError(f"a bearer file holds one token: {path}")
    return token


def fetch_jwks(issuer: str, output: Path, fetch: Fetch = _fetch_json) -> int:
    """Copy ``issuer``'s signing keys to ``output`` for the API (a host unit runs this).

    The document is checked as the API will read it (``api.auth.parse_jwks``) before it
    replaces the previous file atomically; a bad answer leaves the old keys in place.
    Returns: the number of RS256 keys written.
    """
    from .api.auth import parse_jwks

    provider = discover(issuer, fetch)
    if provider.jwks_uri is None:
        raise SignInError("the discovery document names no https jwks_uri")
    document = fetch(urllib.request.Request(provider.jwks_uri))
    data = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        keys = parse_jwks(data)
    except ValueError as error:
        raise SignInError(f"the identity provider's keys are unusable: {error}") from None
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}")
    temporary.write_bytes(data)
    temporary.chmod(0o644)
    os.replace(temporary, output)
    return len(keys)

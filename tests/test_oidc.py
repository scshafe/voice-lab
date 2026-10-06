"""Signing in from a capturing host: PKCE, the loopback callback, the keychain stores."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import AUDIENCE, ISSUER, SUBJECT, mint

from cole_voice_lab import oidc
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.oidc import (
    MacKeychainStore,
    SecretServiceStore,
    SignInError,
    bearer,
    check_id_token,
    discover,
    platform_token_store,
    read_bearer_file,
    sign_in,
)

DISCOVERY = {
    "issuer": ISSUER,
    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/api/oidc/token",
    "code_challenge_methods_supported": ["S256"],
}


class MemoryStore:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def load(self) -> str | None:
        return self.token

    def save(self, token: str) -> None:
        self.token = token


class Provider:
    """A fake identity provider: discovery and the token endpoint."""

    def __init__(self) -> None:
        self.forms: list[dict[str, str]] = []
        self.nonce: str | None = None
        self.refresh = "refresh-1"

    def __call__(self, request: urllib.request.Request) -> Mapping[str, Any]:
        if request.full_url.endswith("/.well-known/openid-configuration"):
            return DISCOVERY
        assert request.data is not None
        form = dict(urllib.parse.parse_qsl(request.data.decode()))  # type: ignore[union-attr]
        self.forms.append(form)
        claims = {"aud": AUDIENCE} | ({"nonce": self.nonce} if self.nonce else {})
        return {"id_token": mint(claims), "refresh_token": self.refresh}


def _browser(provider: Provider, *, state: str | None = None, error: str | None = None):  # type: ignore[no-untyped-def]
    def open_browser(url: str) -> None:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert query["code_challenge_method"] == "S256"
        provider.nonce = query["nonce"]
        answer = {"state": state or query["state"]}
        answer |= {"error": error} if error else {"code": "the-code"}
        callback = f"{query['redirect_uri']}?{urllib.parse.urlencode(answer)}"
        with urllib.request.urlopen(callback, timeout=10) as response:
            assert response.status == 200

    return open_browser


def test_sign_in_keeps_only_the_refresh_token(capsys: pytest.CaptureFixture[str]) -> None:
    provider, store = Provider(), MemoryStore()
    claims = sign_in(
        issuer=ISSUER,
        client_id=AUDIENCE,
        store=store,
        fetch=provider,
        open_browser=_browser(provider),
    )
    assert claims["sub"] == SUBJECT
    assert store.token == "refresh-1"
    form = provider.forms[-1]
    assert form["grant_type"] == "authorization_code" and len(form["code_verifier"]) >= 43
    assert "the-code" not in capsys.readouterr().err


def test_sign_in_refuses_a_foreign_state_or_an_error() -> None:
    provider = Provider()
    with pytest.raises(SignInError, match="state mismatch"):
        sign_in(
            issuer=ISSUER,
            client_id=AUDIENCE,
            store=MemoryStore(),
            fetch=provider,
            open_browser=_browser(provider, state="forged"),
        )
    with pytest.raises(SignInError, match="access_denied"):
        sign_in(
            issuer=ISSUER,
            client_id=AUDIENCE,
            store=MemoryStore(),
            fetch=provider,
            open_browser=_browser(provider, error="access_denied"),
        )
    with pytest.raises(SignInError, match="did not finish"):
        sign_in(
            issuer=ISSUER,
            client_id=AUDIENCE,
            store=MemoryStore(),
            fetch=provider,
            open_browser=lambda _url: None,
            timeout=0.1,
        )


def test_bearer_refreshes_and_follows_rotation() -> None:
    provider, store = Provider(), MemoryStore("refresh-1")
    provider.refresh = "refresh-2"
    token = bearer(issuer=ISSUER, client_id=AUDIENCE, store=store, fetch=provider)
    assert token.count(".") == 2
    assert provider.forms[-1] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": AUDIENCE,
    }
    assert store.token == "refresh-2"
    with pytest.raises(SignInError, match="not signed in"):
        bearer(issuer=ISSUER, client_id=AUDIENCE, store=MemoryStore(), fetch=provider)


def test_bearer_prefers_a_jwt_access_token_and_falls_back_to_the_id_token() -> None:
    access = mint({"aud": [AUDIENCE], "scope": "openid"})
    id_token = mint({"aud": AUDIENCE})

    def answering(answer: dict[str, Any]) -> Any:
        def fetch(request: urllib.request.Request) -> Mapping[str, Any]:
            return DISCOVERY if "openid-configuration" in request.full_url else answer

        return fetch

    store = MemoryStore("refresh-1")
    chosen = bearer(
        issuer=ISSUER,
        client_id=AUDIENCE,
        store=store,
        fetch=answering({"access_token": access, "id_token": id_token}),
    )
    assert chosen == access
    opaque = bearer(
        issuer=ISSUER,
        client_id=AUDIENCE,
        store=store,
        fetch=answering({"access_token": "opaque", "id_token": id_token}),
    )
    assert opaque == id_token
    foreign = mint({"aud": "another-client"})
    assert (
        bearer(
            issuer=ISSUER,
            client_id=AUDIENCE,
            store=store,
            fetch=answering({"access_token": foreign, "id_token": id_token}),
        )
        == id_token
    )
    with pytest.raises(SignInError, match="unusable"):
        bearer(
            issuer=ISSUER,
            client_id=AUDIENCE,
            store=store,
            fetch=answering({"access_token": foreign, "refresh_token": "refresh-9"}),
        )
    assert store.token == "refresh-9"
    with pytest.raises(SignInError, match="no token for this client"):
        bearer(issuer=ISSUER, client_id=AUDIENCE, store=store, fetch=answering({}))


def test_discovery_and_id_token_checks() -> None:
    with pytest.raises(SignInError, match="https"):
        discover("http://id.example.invalid", fetch=lambda _r: DISCOVERY)
    with pytest.raises(SignInError, match="another issuer"):
        discover(ISSUER, fetch=lambda _r: {**DISCOVERY, "issuer": "https://x.invalid"})
    with pytest.raises(SignInError, match="S256"):
        discover(ISSUER, fetch=lambda _r: {**DISCOVERY, "code_challenge_methods_supported": []})
    with pytest.raises(SignInError, match="https endpoints"):
        discover(ISSUER, fetch=lambda _r: {**DISCOVERY, "token_endpoint": "http://x"})
    provider = discover(ISSUER, fetch=lambda _r: DISCOVERY)
    now = time.time()
    for claims, problem in [
        ({"iss": "https://x.invalid"}, "another issuer"),
        ({"aud": "other"}, "not for this client"),
        ({"nonce": "other"}, "nonce"),
        ({"exp": int(now) - 10}, "expired"),
    ]:
        with pytest.raises(SignInError, match=problem):
            check_id_token(
                mint({"nonce": "n", **claims}),
                provider=provider,
                client_id=AUDIENCE,
                nonce="n",
                now=now,
            )
    with pytest.raises(SignInError, match="no ID token"):
        check_id_token("x", provider=provider, client_id=AUDIENCE, nonce=None, now=now)


def _fake_tool(tmp_path: Path, body: str) -> Path:
    tool = tmp_path / "tool"
    tool.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    tool.chmod(0o700)
    return tool


FAKE_SECURITY = r"""
import json, pathlib, re, sys
store = pathlib.Path(__file__).with_name("items.json")
items = json.loads(store.read_text()) if store.exists() else {}
args = sys.argv[1:]
if args == ["-i"]:
    line = sys.stdin.read()
    secret = re.search(r'-w "([^"]*)"', line).group(1)
    items["token"] = secret
    store.write_text(json.dumps(items))
    sys.exit(0)
if args[0] == "find-generic-password":
    if "token" not in items:
        sys.exit(44)
    print(items["token"])
    sys.exit(0)
sys.exit(1)
"""

FAKE_SECRET_TOOL = r"""
import json, pathlib, sys
store = pathlib.Path(__file__).with_name("items.json")
items = json.loads(store.read_text()) if store.exists() else {}
if sys.argv[1] == "store":
    items["token"] = sys.stdin.read()
    store.write_text(json.dumps(items))
elif sys.argv[1] == "lookup" and "token" in items:
    sys.stdout.write(items["token"])
else:
    sys.exit(1)
"""


def test_the_macos_keychain_store_writes_through_stdin(tmp_path: Path) -> None:
    store = MacKeychainStore("client@id", security=_fake_tool(tmp_path, FAKE_SECURITY))
    assert store.load() is None
    store.save("refresh-token.value")
    assert store.load() == "refresh-token.value"
    for unstorable in ('has"quote', "", "with space"):
        with pytest.raises(SignInError, match="cannot store"):
            store.save(unstorable)


def test_the_secret_service_store(tmp_path: Path) -> None:
    store = SecretServiceStore("client@id", tool=_fake_tool(tmp_path, FAKE_SECRET_TOOL))
    assert store.load() is None
    store.save("refresh-token.value")
    assert store.load() == "refresh-token.value"
    assert isinstance(platform_token_store("a", "darwin"), MacKeychainStore)
    assert isinstance(platform_token_store("a", "linux"), SecretServiceStore)


def test_a_failing_keychain_is_reported(tmp_path: Path) -> None:
    failing = MacKeychainStore("a", security=_fake_tool(tmp_path, "import sys; sys.exit(3)"))
    with pytest.raises(SignInError, match="could not be read"):
        failing.load()
    with pytest.raises(SignInError, match="did not keep"):
        failing.save("token")


def test_a_bearer_file_is_private_and_holds_one_token(tmp_path: Path) -> None:
    path = tmp_path / "bearer"
    path.write_text("abc.def.ghi\n", encoding="ascii")
    path.chmod(0o600)
    assert read_bearer_file(path) == "abc.def.ghi"
    path.chmod(0o644)
    with pytest.raises(SecurityError, match="private"):
        read_bearer_file(path)
    path.chmod(0o600)
    path.write_text("two tokens\n", encoding="ascii")
    with pytest.raises(SecurityError, match="one token"):
        read_bearer_file(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_real_fetch_reports_unreachable_providers() -> None:
    with pytest.raises(SignInError, match="unreachable"):
        oidc._fetch_json(urllib.request.Request("https://127.0.0.1:9/nothing"))
    assert json.dumps(DISCOVERY)


def test_fetch_jwks_checks_the_keys_before_replacing_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from home_host_fixtures import jwks

    from cole_voice_lab import cli_home_host
    from cole_voice_lab.cli import main

    def provider(request: urllib.request.Request) -> Mapping[str, Any]:
        return DISCOVERY if "openid-configuration" in request.full_url else jwks()

    output = tmp_path / "auth" / "jwks.json"
    assert oidc.fetch_jwks(ISSUER, output, fetch=provider) == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    before = output.read_bytes()
    with pytest.raises(SignInError, match="unusable"):
        oidc.fetch_jwks(ISSUER, output, fetch=lambda r: DISCOVERY if "openid" in r.full_url else {})
    assert output.read_bytes() == before
    without = {key: value for key, value in DISCOVERY.items() if key != "jwks_uri"}
    with pytest.raises(SignInError, match="jwks_uri"):
        oidc.fetch_jwks(ISSUER, output, fetch=lambda _r: without)
    monkeypatch.setattr(cli_home_host, "fetch_jwks", lambda issuer, path: 2)
    assert main(["home-host", "fetch-jwks", "--issuer", ISSUER, "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["keys"] == 2

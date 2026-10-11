"""Signing in: PKCE S256, state and nonce, the loopback receiver, refresh, the keychain."""

from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time

import httpx
import pytest
from conftest import mint
from voice_lab_desktop import auth
from voice_lab_desktop.auth import AuthError, LoopbackReceiver, Session, SignInRequired, sign_in
from voice_lab_desktop.tokens import KeyringStore, MemoryStore, NoKeychainError, Tokens


def browser(log: list[str] | None = None):
    """A 'browser' that follows the authorize redirect to the loopback callback (on its own
    thread)."""

    def open_url(url: str) -> None:
        if log is not None:
            log.append(url)
        threading.Thread(
            target=lambda: httpx.get(url, follow_redirects=True, timeout=10), daemon=True
        ).start()

    return open_url


def test_sign_in_pkce_s256_state_nonce_and_exact_redirect(config, issuer):
    opened: list[str] = []
    with httpx.Client() as http:
        tokens = sign_in(config, http, browser(opened), timeout=10)
    assert tokens.refresh_token and tokens.bearer == tokens.access_token
    params = issuer.authorize_params[0]
    assert params["response_type"] == "code" and params["client_id"] == "voice-lab-desktop"
    assert params["code_challenge_method"] == "S256" and len(params["code_challenge"]) == 43
    assert params["scope"] == "openid email profile offline_access"
    assert params["redirect_uri"].startswith("http://127.0.0.1:") and params[
        "redirect_uri"
    ].endswith("/callback")
    assert len(params["state"]) >= 40 and len(params["nonce"]) >= 40
    exchange = issuer.token_requests[0]
    assert exchange["redirect_uri"] == params["redirect_uri"]  # exactly the same string
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == params["code_challenge"]
    assert "client_secret" not in exchange


def test_state_mismatch_is_refused(config, issuer):
    issuer.tamper_state = True
    with httpx.Client() as http, pytest.raises(AuthError, match="state mismatch"):
        sign_in(config, http, browser(), timeout=10)
    assert issuer.token_requests == []  # the code was never exchanged


def test_nonce_and_audience_are_checked(config, issuer, monkeypatch):
    real = issuer._tokens
    issuer._tokens = lambda nonce: real("someone-elses-nonce")
    with httpx.Client() as http, pytest.raises(AuthError, match="nonce mismatch"):
        sign_in(config, http, browser(), timeout=10)
    issuer.client_id = "another-client"
    from voice_lab_desktop.config import Config

    other = Config(
        server_url=config.server_url,
        issuer=config.issuer,
        client_id="another-client",
        data_dir=config.data_dir,
    ).validated()
    issuer._tokens = lambda nonce: {
        **real(nonce),
        "id_token": __import__("fakes").fake_jwt(
            {"iss": issuer.issuer, "aud": "voice-lab-desktop", "nonce": nonce}
        ),
    }
    with httpx.Client() as http, pytest.raises(AuthError, match="different issuer or client"):
        sign_in(other, http, browser(), timeout=10)


def test_without_offline_access_no_refresh_scope(config, issuer, monkeypatch):
    real = auth.discover
    monkeypatch.setattr(
        auth,
        "discover",
        lambda c, h: auth.Discovery(**{**real(c, h).__dict__, "scopes_supported": ("openid",)}),
    )
    with httpx.Client() as http:
        sign_in(config, http, browser(), timeout=10)
    assert "offline_access" not in issuer.authorize_params[0]["scope"]


def test_loopback_listens_on_127_0_0_1_only_and_answers_once():
    receiver = LoopbackReceiver(0)
    assert receiver.server_address[0] == "127.0.0.1"
    got: dict = {}
    t = threading.Thread(target=lambda: got.update(receiver.wait(timeout=10)), daemon=True)
    t.start()
    assert httpx.get(f"http://127.0.0.1:{receiver.port}/favicon.ico").status_code == 404
    first = httpx.get(f"http://127.0.0.1:{receiver.port}/callback?code=c1&state=s1")
    assert (
        first.status_code == 200 and "c1" not in first.text and "s1" not in first.text
    )  # never reflected
    t.join(5)
    assert got == {"code": "c1", "state": "s1"}
    with pytest.raises(httpx.ConnectError):  # closed after the one callback
        httpx.get(f"http://127.0.0.1:{receiver.port}/callback?code=c2&state=s2", timeout=2)


def test_loopback_gives_up_after_too_many_stray_requests():
    receiver = LoopbackReceiver(0)
    errors: list[Exception] = []

    def wait():
        try:
            receiver.wait(timeout=10)
        except AuthError as exc:
            errors.append(exc)

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    for _ in range(auth.MAX_LOOPBACK_REQUESTS):
        try:
            httpx.get(f"http://127.0.0.1:{receiver.port}/x", timeout=2)
        except httpx.HTTPError:
            break
    t.join(5)
    assert errors and "too many" in str(errors[0])


def test_sign_in_times_out_and_can_be_cancelled(config, issuer):
    cancel = threading.Event()
    cancel.set()
    with httpx.Client() as http, pytest.raises(AuthError, match="cancelled"):
        sign_in(config, http, lambda url: None, timeout=10, cancelled=cancel)
    with httpx.Client() as http, pytest.raises(AuthError, match="in time"):
        sign_in(config, http, lambda url: None, timeout=0.6)


def test_fixed_port_fallback(monkeypatch):
    monkeypatch.setattr(auth, "FALLBACK_PORT", _free_port())
    receiver = auth.open_receiver(fixed_port=True)
    try:
        assert receiver.port == auth.FALLBACK_PORT
    finally:
        receiver.server_close()


def test_refresh_rotates_and_keeps_old_refresh_if_not_rotated(config, issuer):
    store = MemoryStore(mint(issuer, expires_in=1))  # inside the expiry margin: refreshed on use
    with httpx.Client() as http:
        session = Session(config, store, http)
        before = store.tokens
        bearer = session.bearer()
        assert bearer != before.bearer and store.tokens.refresh_token != before.refresh_token
        issuer.issue_refresh = False
        store.tokens = Tokens(
            store.tokens.access_token,
            store.tokens.id_token,
            time.time() - 10,
            store.tokens.refresh_token,
        )
        session._tokens = store.tokens
        kept = store.tokens.refresh_token
        session.bearer()
        assert store.tokens.refresh_token == kept


def test_refresh_refused_means_sign_in_again_and_clears_the_store(config, issuer):
    store = MemoryStore(Tokens("a.b.c", "a.b.c", time.time() - 10, "rt-unknown"))
    with httpx.Client() as http:
        session = Session(config, store, http)
        with pytest.raises(SignInRequired):
            session.bearer()
    assert store.tokens is None and not session.signed_in


def test_tokens_never_appear_in_repr():
    t = Tokens("secret-access", "secret-id", 0, "secret-refresh")
    assert "secret" not in repr(t) and "secret" not in str(t)


def test_keychain_without_backend_refuses(monkeypatch):
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    with pytest.raises(NoKeychainError, match="no OS keychain"):
        KeyringStore.check_backend()
    with pytest.raises(NoKeychainError):
        KeyringStore("https://id", "c").save(Tokens("a", "b", 0))


def test_keychain_round_trip_with_a_real_backend_shape(monkeypatch):
    import keyring
    from keyring.backend import KeyringBackend

    class Memory(KeyringBackend):
        priority = 1
        items: dict = {}

        def get_password(self, service, username):
            return self.items.get((service, username))

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def delete_password(self, service, username):
            self.items.pop((service, username), None)

    backend = Memory()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    store = KeyringStore("https://id.example", "voice-lab-desktop")
    tokens = Tokens("a.b.c", "d.e.f", 123.0, "r")
    store.save(tokens)
    assert store.load() == tokens
    assert list(backend.items) == [("voice-lab-desktop", "voice-lab-desktop@https://id.example")]
    store.clear()
    assert store.load() is None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_bearer_kind_follows_the_settings(config, issuer):
    from dataclasses import replace

    tokens = mint(issuer)
    with httpx.Client() as http:
        assert Session(config, MemoryStore(tokens), http).bearer() == tokens.access_token
        assert (
            Session(replace(config, bearer="id"), MemoryStore(tokens), http).bearer()
            == tokens.id_token
        )

"""The API's own bearer-token checks (design §3.5): signature, issuer, audience, time, subject."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from home_host_fixtures import (
    AUDIENCE,
    ISSUER,
    KID,
    SUBJECT,
    b64url,
    jwks,
    mint,
    other_key,
    policy,
)

from cole_voice_lab.api.auth import (
    AuthError,
    AuthPolicy,
    RsaKey,
    TokenVerifier,
    TrustedIssuer,
    parse_jwks,
    verify_rs256,
)


@pytest.fixture
def verifier(tmp_path: Path) -> TokenVerifier:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(jwks()), encoding="utf-8")
    return TokenVerifier(policy(path))


def test_a_good_token_names_its_subject(verifier: TokenVerifier) -> None:
    principal = verifier.verify_header(f"Bearer {mint()}")
    assert (principal.issuer, principal.subject) == (ISSUER, SUBJECT)
    assert verifier.verify(mint({"aud": ["other", AUDIENCE]})).subject == SUBJECT


@pytest.mark.parametrize(
    ("token", "problem"),
    [
        (lambda: mint({"sub": "intruder"}), "subject"),
        (lambda: mint({"aud": "another-client"}), "not for this API"),
        (lambda: mint({"aud": 7}), "not for this API"),
        (lambda: mint({"iss": "https://elsewhere.invalid"}), "issuer"),
        (lambda: mint({"exp": int(time.time()) - 3600}), "expired"),
        (lambda: mint({"exp": "soon"}), "no expiry"),
        (lambda: mint({"exp": True}), "no expiry"),
        (lambda: mint({"nbf": int(time.time()) + 3600}), "not valid yet"),
        (lambda: mint({"iat": int(time.time()) + 3600}), "future"),
        (lambda: mint(key=other_key()), "signature"),
        (lambda: mint(header={"kid": "unknown"}), "signature"),
        (lambda: mint(header={"alg": "none"}), "RS256"),
        (lambda: mint(header={"crit": ["exp"]}), "RS256"),
        (lambda: mint()[:-4] + "AAAA", "signature"),
        (lambda: "a.b", "not a signed JWT"),
        (lambda: "", "malformed"),
        (lambda: "é.b.c", "malformed"),
        (lambda: "a=.b.c", "base64url"),
        (lambda: f"{b64url(b'[]')}.{b64url(b'{}')}.AA", "not a JSON object"),
        (lambda: f"{b64url(b'nope')}.{b64url(b'{}')}.AA", "not JSON"),
    ],
)
def test_bad_tokens_are_refused(verifier: TokenVerifier, token: object, problem: str) -> None:
    with pytest.raises(AuthError, match=problem):
        verifier.verify(token())  # type: ignore[operator]


def test_the_header_must_be_a_bearer(verifier: TokenVerifier) -> None:
    for header in (None, "Basic abc", "bearer x"):
        with pytest.raises(AuthError, match="bearer token is required"):
            verifier.verify_header(header)


def test_a_token_without_kid_uses_the_only_key(verifier: TokenVerifier) -> None:
    token = mint(header={"kid": None})
    assert verifier.verify(token).subject == SUBJECT


def test_the_key_file_is_reread_when_it_changes(tmp_path: Path) -> None:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(jwks()), encoding="utf-8")
    verifier = TokenVerifier(policy(path))
    verifier.verify(mint())
    path.write_text(json.dumps(jwks(other_key(), kid="rotated")), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    with pytest.raises(AuthError, match="signature"):
        verifier.verify(mint())
    assert verifier.verify(mint(key=other_key(), header={"kid": "rotated"})).subject == SUBJECT
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(AuthError, match="cannot be read"):
        verifier.verify(mint())
    path.unlink()
    with pytest.raises(AuthError, match="not available"):
        verifier.verify(mint())
    path.write_bytes(b" " * (300 * 1024))
    with pytest.raises(AuthError, match="too large"):
        verifier.verify(mint())


def test_jwks_documents_are_strict() -> None:
    with pytest.raises(ValueError, match="list of keys"):
        parse_jwks(b"{}")
    with pytest.raises(ValueError, match="no RS256"):
        parse_jwks(json.dumps({"keys": [{"kty": "EC"}, {"kty": "RSA", "use": "enc"}]}).encode())
    with pytest.raises(ValueError, match="no RS256"):
        parse_jwks(json.dumps({"keys": [{"kty": "RSA", "kid": 1, "n": "AQ", "e": "AQ"}]}).encode())
    with pytest.raises(ValueError, match="2048"):
        parse_jwks(
            json.dumps({"keys": [{"kty": "RSA", "kid": "k", "n": "AQAB", "e": "AQAB"}]}).encode()
        )
    assert set(parse_jwks(json.dumps(jwks()).encode())) == {KID}


def test_rs256_verification_rejects_malformed_signatures() -> None:
    key = parse_jwks(json.dumps(jwks()).encode())[KID]
    assert not verify_rs256(key, b"x", b"\x00")
    assert not verify_rs256(key, b"x", b"\xff" * key.size_bytes)
    tiny = RsaKey(kid="t", modulus=(1 << 255) + 1, exponent=3)
    assert not verify_rs256(tiny, b"x", b"\x01" * tiny.size_bytes)


def test_the_policy_names_https_issuers_only() -> None:
    with pytest.raises(ValueError):
        TrustedIssuer(issuer="http://id.example.invalid", audiences=("a",), jwks_path="/x")
    with pytest.raises(ValueError):
        TrustedIssuer(issuer="https://id.example.invalid/", audiences=("a",), jwks_path="/x")
    with pytest.raises(ValueError):
        AuthPolicy(issuers=(), allowed_subjects=("a",))

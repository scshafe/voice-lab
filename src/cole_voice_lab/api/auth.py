"""Bearer-token checks inside the API: defence in depth behind the Pocket ID door.

@canonical api.auth -- the door (oauth2-proxy) already admits only Pocket ID tokens, but
the API fronts biometric data, so it checks every ``/api/`` request itself (design §3.5):
an RS256 JWT whose signature verifies against the issuer's published keys, whose ``iss`` is
a trusted issuer, whose ``aud`` names a trusted audience, which is within its lifetime, and
whose ``sub`` is on a short allow-list (one subject: Cole). The keys are read from a JWKS
file that a host unit refreshes, because the API container has no route to the identity
provider; the file is re-read when it changes.

Signature verification is RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 §8.2.2), done with the
standard library's modular exponentiation over public values only; no secret is involved.
Nothing in a refusal message quotes the token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field, field_validator

from ..models import ContractModel

MAX_TOKEN_BYTES: Final = 16 * 1024
MAX_JWKS_BYTES: Final = 256 * 1024
MIN_RSA_BITS: Final = 2048
# DER prefix of DigestInfo for SHA-256 (RFC 8017 §9.2, note 1).
_SHA256_DIGEST_INFO: Final = bytes.fromhex("3031300d060960864801650304020105000420")


class AuthError(Exception):
    """A request that carries no acceptable token; the message never quotes the token."""


class TrustedIssuer(ContractModel):
    """One identity provider whose tokens the API accepts, and for which audiences."""

    issuer: str = Field(min_length=8, max_length=512)
    audiences: tuple[str, ...] = Field(min_length=1, max_length=8)
    jwks_path: str = Field(min_length=1, max_length=4096)

    @field_validator("issuer")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://") or value.endswith("/"):
            raise ValueError("an issuer is an https URL without a trailing slash")
        return value


class AuthPolicy(ContractModel):
    """Which tokens the API accepts."""

    schema_version: Literal["cole-voice-lab.api.auth-policy/v1"] = (
        "cole-voice-lab.api.auth-policy/v1"
    )
    issuers: tuple[TrustedIssuer, ...] = Field(min_length=1, max_length=4)
    allowed_subjects: tuple[str, ...] = Field(min_length=1, max_length=4)
    leeway_seconds: int = Field(default=60, ge=0, le=300)


@dataclass(frozen=True, slots=True)
class Principal:
    """Who a verified token speaks for."""

    issuer: str
    subject: str


@dataclass(frozen=True, slots=True)
class RsaKey:
    """One RSA public key from a JWKS document."""

    kid: str
    modulus: int
    exponent: int

    @property
    def size_bytes(self) -> int:
        return (self.modulus.bit_length() + 7) // 8


def _b64url(value: str) -> bytes:
    if not value or any(character in value for character in "=+/ \n"):
        raise AuthError("the token is not base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as error:
        raise AuthError("the token is not base64url") from error


def _json_object(data: bytes, what: str) -> Mapping[str, Any]:
    try:
        document = json.loads(data)
    except ValueError as error:
        raise AuthError(f"the token's {what} is not JSON") from error
    if not isinstance(document, dict):
        raise AuthError(f"the token's {what} is not a JSON object")
    return document


def parse_jwks(data: bytes) -> dict[str, RsaKey]:
    """The RS256 signing keys of a JWKS document, by key id.

    Raises: ``ValueError`` for a document that is not a JWKS with at least one RSA key of
    at least ``MIN_RSA_BITS`` bits.
    """
    document = json.loads(data)
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list):
        raise ValueError("a JWKS document has a list of keys")
    found: dict[str, RsaKey] = {}
    for entry in keys:
        if not isinstance(entry, dict) or entry.get("kty") != "RSA":
            continue
        if entry.get("use", "sig") != "sig" or entry.get("alg", "RS256") != "RS256":
            continue
        kid, modulus, exponent = entry.get("kid"), entry.get("n"), entry.get("e")
        if not (isinstance(kid, str) and isinstance(modulus, str) and isinstance(exponent, str)):
            continue
        key = RsaKey(
            kid=kid,
            modulus=int.from_bytes(_b64url(modulus), "big"),
            exponent=int.from_bytes(_b64url(exponent), "big"),
        )
        if key.modulus.bit_length() < MIN_RSA_BITS or key.exponent < 3:
            raise ValueError("a JWKS RSA key is at least 2048 bits with a sane exponent")
        found[kid] = key
    if not found:
        raise ValueError("the JWKS document holds no RS256 signing key")
    return found


def verify_rs256(key: RsaKey, signing_input: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 verification with SHA-256 over public values."""
    size = key.size_bytes
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= key.modulus:
        return False
    encoded = pow(value, key.exponent, key.modulus).to_bytes(size, "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
    padding = size - len(digest_info) - 3
    if padding < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


class _KeyFile:
    """A JWKS file, re-read when its size or modification time changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._keys: dict[str, RsaKey] = {}

    def keys(self) -> dict[str, RsaKey]:
        with self._lock:
            try:
                metadata = os.stat(self.path)
            except OSError as error:
                raise AuthError("the identity provider's keys are not available") from error
            stamp = (metadata.st_mtime_ns, metadata.st_size)
            if stamp != self._stamp:
                if metadata.st_size > MAX_JWKS_BYTES:
                    raise AuthError("the identity provider's key file is too large")
                try:
                    self._keys = parse_jwks(self.path.read_bytes())
                except (OSError, ValueError, AuthError) as error:
                    raise AuthError("the identity provider's keys cannot be read") from error
                self._stamp = stamp
            return self._keys


class TokenVerifier:
    """Checks ``Authorization: Bearer`` headers against an ``AuthPolicy``."""

    def __init__(self, policy: AuthPolicy, *, clock: Callable[[], float] = time.time) -> None:
        self.policy = policy
        self._clock = clock
        self._key_files = {
            issuer.issuer: _KeyFile(Path(issuer.jwks_path)) for issuer in policy.issuers
        }

    def verify_header(self, header: str | None) -> Principal:
        """The principal a bearer header speaks for.

        Raises: ``AuthError`` when there is no bearer token, or it fails any check.
        """
        if header is None or not header.startswith("Bearer "):
            raise AuthError("a bearer token is required")
        return self.verify(header.removeprefix("Bearer ").strip())

    def verify(self, token: str) -> Principal:
        """The principal ``token`` speaks for; see the module docstring for the checks."""
        if not token or len(token) > MAX_TOKEN_BYTES or not token.isascii():
            raise AuthError("the bearer token is malformed")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("the bearer token is not a signed JWT")
        header = _json_object(_b64url(parts[0]), "header")
        claims = _json_object(_b64url(parts[1]), "claims")
        signature = _b64url(parts[2])
        if header.get("alg") != "RS256" or header.get("crit") is not None:
            raise AuthError("the token is not signed with RS256")
        issuer_name = claims.get("iss")
        trusted = next(
            (issuer for issuer in self.policy.issuers if issuer.issuer == issuer_name), None
        )
        if trusted is None:
            raise AuthError("the token's issuer is not trusted")
        kid = header.get("kid")
        keys = self._key_files[trusted.issuer].keys()
        candidates = [keys[kid]] if isinstance(kid, str) and kid in keys else []
        if not candidates and kid is None and len(keys) == 1:
            candidates = list(keys.values())
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        if not any(verify_rs256(key, signing_input, signature) for key in candidates):
            raise AuthError("the token's signature does not verify")
        self._check_claims(claims, trusted)
        subject = claims["sub"]
        return Principal(issuer=trusted.issuer, subject=subject)

    def _check_claims(self, claims: Mapping[str, Any], trusted: TrustedIssuer) -> None:
        now = self._clock()
        leeway = self.policy.leeway_seconds
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if not isinstance(audiences, list) or not set(trusted.audiences) & {
            item for item in audiences if isinstance(item, str)
        }:
            raise AuthError("the token is not for this API")
        expiry = claims.get("exp")
        if not isinstance(expiry, int | float) or isinstance(expiry, bool):
            raise AuthError("the token has no expiry")
        if expiry + leeway < now:
            raise AuthError("the token has expired")
        not_before = claims.get("nbf")
        if isinstance(not_before, int | float) and not_before - leeway > now:
            raise AuthError("the token is not valid yet")
        issued = claims.get("iat")
        if isinstance(issued, int | float) and issued - leeway > now:
            raise AuthError("the token was issued in the future")
        subject = claims.get("sub")
        if not isinstance(subject, str) or subject not in self.policy.allowed_subjects:
            raise AuthError("the token's subject may not use this API")

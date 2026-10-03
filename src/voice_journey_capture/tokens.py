"""Tokens: what the sign-in returns, and where they are kept (the OS keychain only).

The keychain is the platform's: Keychain on macOS, the Secret Service (GNOME
Keyring, KeePassXC, KWallet's bridge) on Linux. Without one, the app refuses
to sign in rather than keep tokens in a file.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import asdict, dataclass
from typing import Protocol

SERVICE = "voice-journey-capture"
#: Seconds before ``expires_at`` at which a token counts as expired (clock skew, request time).
EXPIRY_MARGIN = 60


class NoKeychainError(Exception):
    """No usable OS keychain backend (tokens are never stored any other way)."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    id_token: str
    expires_at: float
    refresh_token: str | None = None

    @property
    def bearer(self) -> str:
        """The token the door verifies: the access token when it is a JWT, else the ID token."""
        return self.access_token if looks_like_jwt(self.access_token) else self.id_token

    def bearer_for(self, kind: str) -> str:
        """``kind`` "id" sends the ID token instead (settings ``bearer``), for a door that needs its claims."""
        return self.id_token if kind == "id" else self.bearer

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at - EXPIRY_MARGIN

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> Tokens:
        data = json.loads(raw)
        return cls(
            access_token=str(data["access_token"]),
            id_token=str(data["id_token"]),
            expires_at=float(data["expires_at"]),
            refresh_token=None if data.get("refresh_token") is None else str(data["refresh_token"]),
        )

    def __repr__(self) -> str:  # never let a token reach a log or a traceback
        return f"Tokens(expires_at={self.expires_at:.0f}, refresh={'yes' if self.refresh_token else 'no'})"

    __str__ = __repr__


def looks_like_jwt(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(parts[:2])


def jwt_claims(token: str) -> dict:
    """The payload of a JWT, unverified (the door verifies signatures; the app checks nonce/iss/aud)."""
    if not looks_like_jwt(token):
        raise ValueError("not a JWT")
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not an object")
    return claims


class TokenStore(Protocol):
    def load(self) -> Tokens | None: ...
    def save(self, tokens: Tokens) -> None: ...
    def clear(self) -> None: ...


class KeyringStore:
    """One keychain item per (issuer, client id), holding the token set as JSON."""

    def __init__(self, issuer: str, client_id: str) -> None:
        self._user = f"{client_id}@{issuer}"

    @staticmethod
    def check_backend() -> str:
        """The keychain backend's name; NoKeychainError when there is none usable."""
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        try:
            from keyring.backends import null
            null_types: tuple[type, ...] = (fail.Keyring, null.Keyring)
        except ImportError:  # pragma: no cover - keyring always ships null
            null_types = (fail.Keyring,)
        chained = getattr(backend, "backends", None)
        candidates = list(chained) if chained is not None else [backend]
        usable = [b for b in candidates if not isinstance(b, null_types) and getattr(b, "priority", 0) > 0]
        if not usable:
            raise NoKeychainError(
                "no OS keychain is available, so voice-journey-capture will not store your sign-in. "
                "On Linux start a Secret Service provider (GNOME Keyring: install gnome-keyring and "
                "make sure it runs in your session; or KeePassXC with Secret Service integration "
                "enabled), then try again. On macOS the Keychain is always available."
            )
        return type(usable[0]).__module__ + "." + type(usable[0]).__name__

    def load(self) -> Tokens | None:
        """The stored tokens; None when there are none or no keychain is usable (sign in shows why)."""
        import keyring
        from keyring.errors import KeyringError

        try:
            raw = keyring.get_password(SERVICE, self._user)
        except KeyringError:
            return None
        if raw is None:
            return None
        try:
            return Tokens.from_json(raw)
        except (ValueError, KeyError, TypeError):
            self.clear()  # a damaged item: sign in again
            return None

    def save(self, tokens: Tokens) -> None:
        import keyring

        self.check_backend()
        keyring.set_password(SERVICE, self._user, tokens.to_json())

    def clear(self) -> None:
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.delete_password(SERVICE, self._user)
        except KeyringError:  # nothing stored, or no keychain at all
            pass


class MemoryStore:
    """For tests and ``--smoke``: tokens in memory only."""

    def __init__(self, tokens: Tokens | None = None) -> None:
        self.tokens = tokens

    def load(self) -> Tokens | None:
        return self.tokens

    def save(self, tokens: Tokens) -> None:
        self.tokens = tokens

    def clear(self) -> None:
        self.tokens = None

"""Settings: the voice-journey server, the Pocket ID issuer and client id, the mouth distance.

Precedence: environment, then the settings file, then the defaults (the live
values). The settings file is ``config.toml`` in the config dir
(``$XDG_CONFIG_HOME/voice-journey-capture/``, else ``~/.config/...``; on macOS
``~/Library/Application Support/voice-journey-capture/``). The window's
Settings panel writes the same file; nothing secret ever goes in it (tokens
live in the OS keychain).

    server_url = "https://voice-journey.colobus-stargazer.ts.net"
    issuer = "https://id.colobus-stargazer.ts.net"
    client_id = "voice-journey-capture"
    distance_cm = 35
    bearer = "access"        # or "id": which token the door is sent

Environment overrides (each wins over the file):

    VOICE_JOURNEY_CAPTURE_SERVER     server_url
    VOICE_JOURNEY_CAPTURE_ISSUER     issuer
    VOICE_JOURNEY_CAPTURE_CLIENT_ID  client_id
    VOICE_JOURNEY_CAPTURE_CONFIG     the settings file's path
    VOICE_JOURNEY_CAPTURE_DATA       the data dir (sessions, the level pin, the cal file)
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import chain

DEFAULT_SERVER = "https://voice-journey.colobus-stargazer.ts.net"
DEFAULT_ISSUER = "https://id.colobus-stargazer.ts.net"
DEFAULT_CLIENT_ID = "voice-journey-capture"
#: The fixed loopback port to register with Pocket ID, used when an ephemeral one is refused.
FALLBACK_PORT = 53683

KEYS = ("server_url", "issuer", "client_id", "distance_cm", "bearer")
ENV = {
    "server_url": "VOICE_JOURNEY_CAPTURE_SERVER",
    "issuer": "VOICE_JOURNEY_CAPTURE_ISSUER",
    "client_id": "VOICE_JOURNEY_CAPTURE_CLIENT_ID",
}
ENV_CONFIG = "VOICE_JOURNEY_CAPTURE_CONFIG"
ENV_DATA = "VOICE_JOURNEY_CAPTURE_DATA"
APP = "voice-journey-capture"


class ConfigError(Exception):
    """A settings file or value that cannot be used."""


def config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP


def _check_url(name: str, value: str) -> str:
    parts = urlsplit(value)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ConfigError(f"{name} must be https (http only for a loopback test server): {value!r}")
    if not parts.hostname:
        raise ConfigError(f"{name} has no host: {value!r}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(f"{name} must not carry credentials, a query or a fragment: {value!r}")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    server_url: str = DEFAULT_SERVER
    issuer: str = DEFAULT_ISSUER
    client_id: str = DEFAULT_CLIENT_ID
    distance_cm: int = chain.DEFAULT_DISTANCE_CM
    bearer: str = "access"
    data_dir: Path = field(default_factory=default_data_dir)
    #: Where the settings came from (for --check-config and the Settings panel).
    source: str = "defaults"
    #: The settings file the Settings panel writes.
    file: Path | None = None
    #: Keys pinned by the environment (the Settings panel shows them read-only).
    env_keys: tuple[str, ...] = ()

    def validated(self) -> Config:
        server = _check_url("server_url", self.server_url)
        if urlsplit(server).path not in ("", "/"):
            raise ConfigError(f"server_url is the server's origin, without a path: {self.server_url!r}")
        issuer = _check_url("issuer", self.issuer)
        if not self.client_id or any(c.isspace() for c in self.client_id):
            raise ConfigError("client_id must be a non-empty token")
        if isinstance(self.distance_cm, bool) or not isinstance(self.distance_cm, int) or not 5 <= self.distance_cm <= 300:
            raise ConfigError(f"distance_cm must be a whole number of centimetres, 5 to 300: {self.distance_cm!r}")
        if self.bearer not in ("access", "id"):
            raise ConfigError(f"bearer must be 'access' or 'id': {self.bearer!r}")
        return replace(self, server_url=server, issuer=issuer, data_dir=Path(self.data_dir))


def settings_file(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env[ENV_CONFIG]) if env.get(ENV_CONFIG) else config_dir() / "config.toml"


def read_settings(file: Path) -> dict:
    try:
        data = tomllib.loads(file.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{file}: {exc}") from exc
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise ConfigError(f"{file}: unknown keys {unknown} (known: {list(KEYS)})")
    for key, value in data.items():
        want = int if key == "distance_cm" else str
        if not isinstance(value, want) or isinstance(value, bool):
            raise ConfigError(f"{file}: {key} must be a {'whole number' if want is int else 'string'}")
    return data


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    """Defaults, then the settings file (``path``, or the default one if it exists), then the environment."""
    env = os.environ if env is None else env
    file = path if path is not None else settings_file(env)
    values: dict = {}
    sources: list[str] = []
    if file.exists():
        values.update(read_settings(file))
        sources.append(str(file))
    elif path is not None:
        raise ConfigError(f"no settings file at {file}")
    pinned = []
    for key, var in ENV.items():
        if env.get(var):
            values[key] = env[var]
            pinned.append(key)
    if pinned:
        sources.append("environment (" + ", ".join(ENV[k] for k in pinned) + ")")
    data_dir = Path(env[ENV_DATA]) if env.get(ENV_DATA) else default_data_dir()
    return Config(**values, data_dir=data_dir, source=" + ".join(sources) or "defaults", file=file, env_keys=tuple(pinned)).validated()


def _toml_value(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_settings(config: Config, changes: dict) -> Config:
    """Write ``changes`` into the settings file (keeping its other keys) and return the reloaded config."""
    unknown = sorted(set(changes) - set(KEYS))
    if unknown:
        raise ConfigError(f"unknown settings {unknown}")
    blocked = sorted(set(changes) & set(config.env_keys))
    if blocked:
        raise ConfigError(f"{', '.join(ENV[k] for k in blocked)} is set in the environment, which wins over the settings file")
    # Validate before writing anything.
    replace(config, **changes).validated()
    file = config.file or settings_file()
    current = read_settings(file) if file.exists() else {}
    current.update(changes)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".toml.tmp")
    tmp.write_text("".join(f"{k} = {_toml_value(current[k])}\n" for k in KEYS if k in current))
    tmp.replace(file)
    env = {ENV_DATA: str(config.data_dir), **{ENV[k]: getattr(config, k) for k in config.env_keys}}
    return load_config(file, env)

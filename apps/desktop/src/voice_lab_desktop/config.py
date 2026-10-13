"""Settings: the voice-lab server, the Pocket ID issuer and client id, the mouth distance.

Precedence: environment, then the settings file, then the defaults. The server and the
issuer have no default in the code (the publication screen, §5: a tailnet name is
configuration, not code): until both are set, the app opens on its Status screen and asks
for them. The settings file is ``config.toml`` in the config dir
(``$XDG_CONFIG_HOME/voice-lab-desktop/``, else ``~/.config/...``; on macOS
``~/Library/Application Support/voice-lab-desktop/``). The Settings dialog writes the same
file; nothing secret ever goes in it (tokens live in the OS keychain).

    server_url = "https://voice-lab.<tailnet>.ts.net"
    issuer = "https://id.<tailnet>.ts.net"
    client_id = "voice-lab-desktop"
    distance_cm = 35
    bearer = "access"        # or "id": which token the door is sent
    catalog_file = "/path/to/suite.json"   # Record: prompts may also use this prompt suite
    prompted_uploads = false # send prompted sessions; only once spike S-1 passed (D-042)
    corpus_sync = false      # send corpus takes, decisions and trims to the PC; only once
                             # spike S-1 passed (D-042, D-052)
    prompts_mode = "studio"  # Record → Prompts: the prompt studio (corpus-first, D-051),
                             # or "sessions" for phase 4c's session flow (D-042)

Environment overrides (each wins over the file):

    VOICE_LAB_DESKTOP_SERVER     server_url
    VOICE_LAB_DESKTOP_ISSUER     issuer
    VOICE_LAB_DESKTOP_CLIENT_ID  client_id
    VOICE_LAB_DESKTOP_CONFIG     the settings file's path
    VOICE_LAB_DESKTOP_DATA       the data dir (the measurement sessions, the level pin, the
                                 cal file)
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from .measurement import chain

APP: Final = "voice-lab-desktop"
DEFAULT_CLIENT_ID: Final = "voice-lab-desktop"
#: The fixed loopback port to register with Pocket ID, used when an ephemeral one is refused.
FALLBACK_PORT: Final = 53683

KEYS: Final = (
    "server_url",
    "issuer",
    "client_id",
    "distance_cm",
    "bearer",
    "catalog_file",
    "prompted_uploads",
    "corpus_sync",
    "prompts_mode",
)
#: What Record → Prompts shows: the prompt studio (D-051, RECORDING-V1.md step 6), or the
#: session flow of phase 4c (D-042), kept until the studio reaches parity and retires it.
PROMPTS_MODES: Final = ("studio", "sessions")
#: Each key's TOML type (the others are strings).
_KINDS: Final[dict[str, type]] = {
    "distance_cm": int,
    "prompted_uploads": bool,
    "corpus_sync": bool,
}
ENV: Final = {
    "server_url": "VOICE_LAB_DESKTOP_SERVER",
    "issuer": "VOICE_LAB_DESKTOP_ISSUER",
    "client_id": "VOICE_LAB_DESKTOP_CLIENT_ID",
}
ENV_CONFIG: Final = "VOICE_LAB_DESKTOP_CONFIG"
ENV_DATA: Final = "VOICE_LAB_DESKTOP_DATA"


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
    server_url: str = ""
    issuer: str = ""
    client_id: str = DEFAULT_CLIENT_ID
    distance_cm: int = chain.DEFAULT_DISTANCE_CM
    bearer: str = "access"
    #: Record: prompts may also record from this prompt-suite file (D-042).
    catalog_file: str = ""
    #: Prompted sessions upload only when this is on: after spike S-1 passed (D-042).
    prompted_uploads: bool = False
    #: The corpus client sends takes, decisions and trims to the PC only when this is on:
    #: after spike S-1 passed on this platform (D-042's reason; ``corpus/``, D-052).
    corpus_sync: bool = False
    #: Record → Prompts: ``studio`` (the prompt studio) or ``sessions`` (phase 4c's flow).
    prompts_mode: str = "studio"
    data_dir: Path = field(default_factory=default_data_dir)
    #: Where the settings came from (for --check-config and the Settings dialog).
    source: str = "defaults"
    #: The settings file the Settings dialog writes.
    file: Path | None = None
    #: Keys pinned by the environment (the Settings dialog shows them read-only).
    env_keys: tuple[str, ...] = ()

    def validated(self) -> Config:
        server = _check_url("server_url", self.server_url) if self.server_url else ""
        if urlsplit(server).path not in ("", "/"):
            raise ConfigError(
                f"server_url is the server's origin, without a path: {self.server_url!r}"
            )
        issuer = _check_url("issuer", self.issuer) if self.issuer else ""
        if not self.client_id or any(c.isspace() for c in self.client_id):
            raise ConfigError("client_id must be a non-empty token")
        distance = self.distance_cm
        if isinstance(distance, bool) or not isinstance(distance, int) or not 5 <= distance <= 300:
            raise ConfigError(
                f"distance_cm must be a whole number of centimetres, 5 to 300: {distance!r}"
            )
        if self.bearer not in ("access", "id"):
            raise ConfigError(f"bearer must be 'access' or 'id': {self.bearer!r}")
        if not isinstance(self.prompted_uploads, bool):
            raise ConfigError(f"prompted_uploads must be true or false: {self.prompted_uploads!r}")
        if not isinstance(self.corpus_sync, bool):
            raise ConfigError(f"corpus_sync must be true or false: {self.corpus_sync!r}")
        if self.prompts_mode not in PROMPTS_MODES:
            raise ConfigError(f"prompts_mode must be 'studio' or 'sessions': {self.prompts_mode!r}")
        return replace(self, server_url=server, issuer=issuer, data_dir=Path(self.data_dir))

    @property
    def configured(self) -> bool:
        """Whether the server and the issuer are both set."""
        return bool(self.server_url and self.issuer)

    def require_server(self) -> str:
        if not self.server_url:
            raise ConfigError(
                f"no server is set: set server_url in Settings (or {ENV['server_url']})"
            )
        return self.server_url

    def require_issuer(self) -> str:
        if not self.issuer:
            raise ConfigError(
                f"no Pocket ID issuer is set: set issuer in Settings ({ENV['issuer']})"
            )
        return self.issuer

    @property
    def library_dir(self) -> Path:
        """This machine's private library for the home-host approval and its ledger (D-021)."""
        return Path(self.data_dir) / "library"

    @property
    def prompted_dir(self) -> Path:
        """Record: prompts' sessions, takes and packages (``prompted/store.py``)."""
        return Path(self.data_dir) / "prompted"

    @property
    def corpus_dir(self) -> Path:
        """The corpus client's spool, catalog cache and outbox (``corpus/``, D-052)."""
        return Path(self.data_dir) / "corpus"

    @property
    def measurement_dir(self) -> Path:
        """The measurement capture's own data: its sessions, the level pin, the cal file."""
        return Path(self.data_dir) / "measurement"


def settings_file(env: dict[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    configured = environ.get(ENV_CONFIG)
    return Path(configured) if configured else config_dir() / "config.toml"


def read_settings(file: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(file.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{file}: {exc}") from exc
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise ConfigError(f"{file}: unknown keys {unknown} (known: {list(KEYS)})")
    for key, value in data.items():
        want = _KINDS.get(key, str)
        if not isinstance(value, want) or (want is not bool and isinstance(value, bool)):
            kind = {int: "whole number", bool: "true or false"}.get(want, "string")
            raise ConfigError(f"{file}: {key} must be a {kind}")
    return data


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    """Defaults, then the settings file (``path``, or the default one if it exists), then
    the environment."""
    environ = os.environ if env is None else env
    file = path if path is not None else settings_file(dict(environ))
    values: dict[str, Any] = {}
    sources: list[str] = []
    if file.exists():
        values.update(read_settings(file))
        sources.append(str(file))
    elif path is not None:
        raise ConfigError(f"no settings file at {file}")
    pinned = []
    for key, var in ENV.items():
        if environ.get(var):
            values[key] = environ[var]
            pinned.append(key)
    if pinned:
        sources.append("environment (" + ", ".join(ENV[k] for k in pinned) + ")")
    data = environ.get(ENV_DATA)
    data_dir = Path(data) if data else default_data_dir()
    return Config(
        **values,
        data_dir=data_dir,
        source=" + ".join(sources) or "defaults",
        file=file,
        env_keys=tuple(pinned),
    ).validated()


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_settings(config: Config, changes: dict[str, Any]) -> Config:
    """Write ``changes`` into the settings file (keeping its other keys) and return the
    reloaded config."""
    unknown = sorted(set(changes) - set(KEYS))
    if unknown:
        raise ConfigError(f"unknown settings {unknown}")
    blocked = sorted(set(changes) & set(config.env_keys))
    if blocked:
        names = ", ".join(ENV[k] for k in blocked)
        raise ConfigError(f"{names} is set in the environment, which wins over the settings file")
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

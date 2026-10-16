"""The stack serves the speech API (D-046, docs/SPEECH_API.md §5, §11) behind the same door.

Read as text: the stack's compose file is checked by infra's validator at deploy and by
`dev check`; these tests pin what the speech API needs from it. The API gets its
configuration through a read-only bind of the configuration's directory (never under /etc,
which infra refuses), the door admits the callers' machine tokens through one more extra JWT
issuer, and no route skips the door.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath

REPOSITORY = Path(__file__).resolve().parents[1]
STACK = REPOSITORY / "deploy" / "stack"
COMPOSE = (STACK / "compose.yaml").read_text()
SPEECH_UNIT = REPOSITORY / "deploy" / "host-manual" / "voice-lab-speech.service"


def _service(name: str) -> list[str]:
    lines = COMPOSE.splitlines()
    start = lines.index(f"  {name}:") + 1
    end = next(
        (i for i in range(start, len(lines)) if re.match(r"^  [a-z0-9-]+:$", lines[i])),
        len(lines),
    )
    return lines[start:end]


def _block(lines: list[str], key: str) -> list[str]:
    start = lines.index(f"    {key}:") + 1
    end = next(
        (i for i in range(start, len(lines)) if re.match(r"^    [a-z_]+:", lines[i])), len(lines)
    )
    return [line for line in lines[start:end] if not line.strip().startswith("#")]


def _unit_config_path() -> str:
    (start,) = [line for line in SPEECH_UNIT.read_text().splitlines() if line.startswith("Exec")]
    return start.split("--config ", 1)[1].split()[0]


def test_the_api_serves_speech_from_the_units_configuration() -> None:
    command = [line.strip().removeprefix("- ") for line in _block(_service("api"), "command")]
    at = command.index("--speech-config")
    path = command[at + 1]
    assert path == _unit_config_path() == "/data/voice-lab/config/speech.json"
    # The speech routes check their callers' tokens themselves: the issuer they trust is the
    # API's (--issuer), and the desktop audience stays the global check's.
    assert command[command.index("--audience") + 1] == "voice-lab-desktop"


def test_the_configuration_directory_is_bound_read_only_and_nothing_under_etc() -> None:
    volumes = "\n".join(_block(_service("api"), "volumes"))
    binds = re.findall(
        r"- type: bind\n\s+source: (\S+)\n\s+target: (\S+)\n((?:\s+(?!- )\S.*\n?)*)", volumes
    )
    by_source = {source: (target, rest) for source, target, rest in binds}
    target, rest = by_source["/data/voice-lab/config"]
    assert target == "/data/voice-lab/config"
    assert "read_only: true" in rest and "create_host_path: false" in rest
    config = PurePosixPath(_unit_config_path())
    assert config.parent == PurePosixPath(target)
    for service in ("ts-voice-lab", "oauth2-proxy", "api"):
        for source in re.findall(
            r"^\s+(?:- |source: )\.?(/[^:\s]*)", "\n".join(_service(service)), re.M
        ):
            assert not PurePosixPath(source).is_relative_to("/etc"), (service, source)
    assert set(by_source) >= {"/data/voice-lab/config", "/data/voice-lab/state"}


def test_the_door_admits_the_speech_callers_and_skips_no_speech_route() -> None:
    door = "\n".join(_service("oauth2-proxy"))
    issuers = re.search(r"OAUTH2_PROXY_EXTRA_JWT_ISSUERS: (.+)$", door, re.M)
    assert issuers is not None
    desktop, speech = issuers.group(1).split(",")
    assert desktop == "${OIDC_ISSUER}=voice-lab-desktop"
    assert speech.startswith("${OIDC_ISSUER}=${VOICE_LAB_SPEECH_AUDIENCE:?set ")
    assert speech.endswith("}") and speech.count("}") == 2
    assert 'OAUTH2_PROXY_SKIP_JWT_BEARER_TOKENS: "true"' in door
    assert 'OAUTH2_PROXY_EMAIL_DOMAINS: "*"' in door
    # Bearer sessions take their email from `sub` when the token has none: no claim mapping
    # that would change the browser sign-in, and the door is never skipped for speech.
    assert "OIDC_EMAIL_CLAIM" not in COMPOSE
    assert 'OAUTH2_PROXY_SKIP_AUTH_ROUTES: "GET=^/healthz$$"' in door
    assert "speech" not in door.split("OAUTH2_PROXY_SKIP_AUTH_ROUTES", 1)[1].splitlines()[0]
    assert 'OAUTH2_PROXY_API_ROUTES: "^/api/"' in door


def test_the_speech_audience_is_a_required_name_of_the_stack() -> None:
    stack = tomllib.loads((STACK / "stack.toml").read_text())
    assert "VOICE_LAB_SPEECH_AUDIENCE" in stack["env"]["required"]
    assert "VOICE_LAB_SPEECH_AUDIENCE" not in stack["env"]["generated"]
    example = (STACK / ".env.example").read_text()
    assert re.search(r"^VOICE_LAB_SPEECH_AUDIENCE=\s", example, re.M)


def test_the_override_is_folded_in() -> None:
    assert not (REPOSITORY / "deploy" / "speech" / "compose.speech.yaml").exists()

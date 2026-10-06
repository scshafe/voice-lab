"""The vault copier's host units (``deploy/host/voice-lab-vault@.{service,timer}``, D-034).

Static checks of the unit text: what it runs, as whom, which credentials it imports and what
network it may reach. They hold the units to the item names ``storage_contracts`` derives and
to the hardening the intake publisher has, so a change to one is a failing test until the
other follows. Nothing here starts a unit or reads a credential.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

from cole_voice_lab.storage_contracts import (
    KEYCHAIN_ITEM_PREFIX,
    VAULT_TOKEN_ITEM_PREFIX,
    keychain_item_names,
)
from cole_voice_lab.tailnet import TAILNET_RANGE

HOST = Path(__file__).resolve().parents[1] / "deploy" / "host"
SERVICE = HOST / "voice-lab-vault@.service"
TIMER = HOST / "voice-lab-vault@.timer"
INTAKE = HOST / "voice-lab-intake.service"
BINARY = "/opt/voice-lab/current/bin/voice-lab"
LAB = "/data/voice-lab/lab"


def settings(path: Path) -> list[tuple[str, str, str]]:
    """Every ``Key=value`` line as ``(section, key, value)``, repeated keys kept in order."""
    section = ""
    found: list[tuple[str, str, str]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        found.append((section, key, value))
    return found


def values(path: Path, section: str, key: str) -> list[str]:
    return [value for where, name, value in settings(path) if (where, name) == (section, key)]


def test_the_copier_pushes_then_verifies_the_instances_destination_as_voice_lab() -> None:
    assert values(SERVICE, "Service", "Type") == ["oneshot"]
    assert values(SERVICE, "Service", "User") == ["voice-lab"]
    assert values(SERVICE, "Service", "Group") == ["voice-lab"]
    assert values(SERVICE, "Service", "ExecStart") == [
        f"{BINARY} vault push --asset-root {LAB} --destination %i",
        f"{BINARY} vault verify --asset-root {LAB} --destination %i",
    ]
    for _, key, value in settings(SERVICE):
        if key.startswith("Exec"):
            # No privileged (+, !, !!) or other prefix: nothing here runs as root.
            assert value.startswith("/"), value
    assert values(SERVICE, "Service", "ReadWritePaths") == [LAB]


def test_the_copier_imports_the_key_and_only_its_own_destinations_tokens() -> None:
    imports = values(SERVICE, "Service", "ImportCredential")
    assert imports == [f"{KEYCHAIN_ITEM_PREFIX}*", f"{VAULT_TOKEN_ITEM_PREFIX}%i.*"]
    assert not values(SERVICE, "Service", "LoadCredential")
    assert not values(SERVICE, "Service", "SetCredential")

    def imported(instance: str, item: str) -> bool:
        return any(item.startswith(glob.replace("%i", instance)[:-1]) for glob in imports)

    laptop = keychain_item_names("lib-20261006-0123456789ab", "laptop")
    air = keychain_item_names("lib-20261006-0123456789ab", "air")
    assert all(imported("laptop", item) for item in laptop)
    assert not any(imported("laptop", item) for item in (air.token, air.read_token))
    assert all(imported("air", item) for item in air)


def test_the_copier_reaches_the_tailnet_and_nothing_else() -> None:
    assert values(SERVICE, "Service", "IPAddressDeny") == ["any"]
    allowed = [
        ipaddress.ip_network(value) for value in values(SERVICE, "Service", "IPAddressAllow")
    ]
    assert allowed == [TAILNET_RANGE]
    assert values(SERVICE, "Service", "RestrictAddressFamilies") == ["AF_UNIX AF_INET AF_INET6"]
    assert values(SERVICE, "Unit", "After") == ["network-online.target tailscaled.service"]


def test_the_copier_is_hardened_like_the_intake_publisher_except_for_the_network() -> None:
    network = {"PrivateNetwork", "IPAddressDeny", "IPAddressAllow", "RestrictAddressFamilies"}
    unit_specific = {"ExecStart", "TimeoutStartSec", "ReadWritePaths"}
    intake = {
        (section, key, value)
        for section, key, value in settings(INTAKE)
        if section == "Service" and key not in network | unit_specific
    }
    copier = set(settings(SERVICE))
    assert intake <= copier, sorted(intake - copier)
    assert ("Service", "CapabilityBoundingSet", "") in copier
    assert ("Service", "UMask", "0077") in copier


def test_the_timer_is_nightly_generic_and_catches_up() -> None:
    assert values(TIMER, "Timer", "OnCalendar") == ["*-*-* 02:30:00"]
    assert values(TIMER, "Timer", "Persistent") == ["true"]
    assert not values(TIMER, "Timer", "Unit")
    assert values(TIMER, "Install", "WantedBy") == ["timers.target"]

"""The host units: ``deploy/host/`` (installed by the runner lane) and the vault copier's
``deploy/host-manual/voice-lab-vault-laptop.{service,timer}`` (installed by hand).

Static checks of the unit text: what it runs, as whom, which credentials it imports and what
network it may reach. They hold the units to the item names ``storage_contracts`` derives and
to the hardening the intake publisher has, so a change to one is a failing test until the
other follows; and to infra's host-unit validator, which refuses template units for now
(each destination has a fixed-name pair: the laptop's now, the Air's in phase 3), refuses
``ImportCredential=`` (so the copier stays out of ``deploy/host/``) and requires every service
to be fenced from the network. Nothing here starts a unit or reads a credential.
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
MANUAL = Path(__file__).resolve().parents[1] / "deploy" / "host-manual"
SERVICE = MANUAL / "voice-lab-vault-laptop.service"
TIMER = MANUAL / "voice-lab-vault-laptop.timer"
TAILNET_ALLOWANCE = ["100.64.0.0/10 fd7a:115c:a1e0::/48"]
CREDENTIAL_KEYS = {
    "ImportCredential",
    "LoadCredential",
    "LoadCredentialEncrypted",
    "SetCredential",
    "SetCredentialEncrypted",
}
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


def host_units(directory: Path) -> list[Path]:
    return sorted(
        path for path in directory.iterdir() if path.suffix in {".service", ".path", ".timer"}
    )


def test_every_host_unit_passes_infras_validator_rules() -> None:
    """Names start with voice-lab-, no template, User/Group voice-lab, no privileged Exec."""
    units = host_units(HOST) + host_units(MANUAL)
    assert SERVICE in units and TIMER in units
    for unit in units:
        assert unit.name.startswith("voice-lab-") and "@" not in unit.name, unit.name
        if unit.suffix == ".service":
            assert values(unit, "Service", "User") == ["voice-lab"], unit.name
            assert values(unit, "Service", "Group") == ["voice-lab"], unit.name
            assert not values(unit, "Service", "PermissionsStartOnly"), unit.name
        for _, key, value in settings(unit):
            if key.startswith("Exec"):
                assert value.startswith("/opt/voice-lab/current/"), (unit.name, value)


def test_the_lanes_units_import_no_credentials_and_are_fenced() -> None:
    """The lane's validator refuses credentials and an unfenced service: deploy/host/ has
    neither; the copier, which needs credentials, is installed by hand from host-manual/."""
    units = host_units(HOST)
    assert SERVICE not in units and TIMER not in units
    for unit in units:
        assert not {key for _, key, _ in settings(unit)} & CREDENTIAL_KEYS, unit.name
        if unit.suffix != ".service":
            continue
        if values(unit, "Service", "PrivateNetwork") == ["yes"]:
            continue
        assert values(unit, "Service", "IPAddressDeny") == ["any"], unit.name
    jwks = HOST / "voice-lab-jwks.service"
    assert values(jwks, "Service", "IPAddressAllow") == TAILNET_ALLOWANCE


def test_the_copier_pushes_then_verifies_the_laptop_as_voice_lab() -> None:
    assert values(SERVICE, "Service", "Type") == ["oneshot"]
    assert values(SERVICE, "Service", "User") == ["voice-lab"]
    assert values(SERVICE, "Service", "Group") == ["voice-lab"]
    assert values(SERVICE, "Service", "ExecStart") == [
        f"{BINARY} vault push --asset-root {LAB} --destination laptop",
        f"{BINARY} vault verify --asset-root {LAB} --destination laptop",
    ]
    for _, key, value in settings(SERVICE):
        if key.startswith("Exec"):
            # No privileged (+, !, !!) or other prefix: nothing here runs as root.
            assert value.startswith("/"), value
    assert values(SERVICE, "Service", "ReadWritePaths") == [LAB]


def test_the_copier_imports_the_key_and_only_the_laptops_tokens() -> None:
    imports = values(SERVICE, "Service", "ImportCredential")
    assert imports == [f"{KEYCHAIN_ITEM_PREFIX}*", f"{VAULT_TOKEN_ITEM_PREFIX}laptop.*"]
    assert not values(SERVICE, "Service", "LoadCredential")
    assert not values(SERVICE, "Service", "SetCredential")

    def imported(item: str) -> bool:
        return any(item.startswith(glob[:-1]) for glob in imports)

    laptop = keychain_item_names("lib-20261006-0123456789ab", "laptop")
    air = keychain_item_names("lib-20261006-0123456789ab", "air")
    assert all(imported(item) for item in laptop)
    assert not any(imported(item) for item in (air.token, air.read_token))


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


def test_the_timer_is_nightly_and_catches_up() -> None:
    assert values(TIMER, "Timer", "OnCalendar") == ["*-*-* 02:30:00"]
    assert values(TIMER, "Timer", "Persistent") == ["true"]
    assert not values(TIMER, "Timer", "Unit")
    assert values(TIMER, "Install", "WantedBy") == ["timers.target"]


# -- the Air's pair (phase 3) ------------------------------------------------------------------

AIR_SERVICE = MANUAL / "voice-lab-vault-air.service"
AIR_TIMER = MANUAL / "voice-lab-vault-air.timer"
MEMOS = "/data/voice-lab/memos/phone"


def test_the_air_copier_pushes_weekly_when_reachable_and_verifies_once() -> None:
    assert values(AIR_SERVICE, "Service", "ExecStart") == [
        f"{BINARY} vault push --asset-root {LAB} --destination air --memos-root {MEMOS} "
        "--if-older-than 7d --skip-unreachable",
        f"{BINARY} vault verify --asset-root {LAB} --destination air --if-unverified "
        "--skip-unreachable",
    ]
    # The memos drop is read, never written: only the lab is writable.
    assert values(AIR_SERVICE, "Service", "ReadWritePaths") == [LAB]
    assert values(AIR_SERVICE, "Service", "ProtectSystem") == ["strict"]


def test_the_air_copier_imports_the_key_and_only_the_airs_tokens() -> None:
    imports = values(AIR_SERVICE, "Service", "ImportCredential")
    assert imports == [f"{KEYCHAIN_ITEM_PREFIX}*", f"{VAULT_TOKEN_ITEM_PREFIX}air.*"]

    def imported(item: str) -> bool:
        return any(item.startswith(glob[:-1]) for glob in imports)

    laptop = keychain_item_names("lib-20261006-0123456789ab", "laptop")
    air = keychain_item_names("lib-20261006-0123456789ab", "air")
    assert all(imported(item) for item in air)
    assert not any(imported(item) for item in (laptop.token, laptop.read_token))


def test_the_air_copier_is_the_laptops_but_for_what_it_runs_and_imports() -> None:
    differs = {"ExecStart", "ImportCredential", "Description"}
    laptop = [entry for entry in settings(SERVICE) if entry[1] not in differs]
    assert [entry for entry in settings(AIR_SERVICE) if entry[1] not in differs] == laptop


def test_the_air_timer_probes_hourly_and_catches_up() -> None:
    assert values(AIR_TIMER, "Timer", "OnCalendar") == ["hourly"]
    assert values(AIR_TIMER, "Timer", "Persistent") == ["true"]
    assert not values(AIR_TIMER, "Timer", "Unit")
    assert values(AIR_TIMER, "Install", "WantedBy") == ["timers.target"]


# -- the speech units (D-046, docs/SPEECH_API.md §7, §8) -----------------------------------------

SPEECH = MANUAL / "voice-lab-speech.service"
WEBHOOKS = MANUAL / "voice-lab-speech-webhooks.service"
SPEECH_CONFIG = "/etc/voice-lab/speech.json"


def test_the_speech_units_stay_out_of_the_lanes_directory() -> None:
    lane = {unit.name for unit in host_units(HOST)}
    assert not any(name.startswith("voice-lab-speech") for name in lane)


def test_the_speech_runner_has_the_gpu_and_no_network() -> None:
    assert values(SPEECH, "Service", "ExecStart") == [
        f"{BINARY} speech run --config {SPEECH_CONFIG} --release-link /opt/voice-lab/current"
    ]
    # It writes the speech state, the GPU slot and the render ledger: not the rest of the lab.
    assert values(SPEECH, "Service", "ReadWritePaths") == [
        "/data/voice-lab/state/speech -/data/voice-lab/state/jobs "
        "/data/voice-lab/lab/provenance/speech-renders"
    ]
    assert values(SPEECH, "Service", "ProtectSystem") == ["strict"]
    assert values(SPEECH, "Service", "PrivateNetwork") == ["yes"]
    assert values(SPEECH, "Service", "IPAddressDeny") == ["any"]
    assert values(SPEECH, "Service", "RestrictAddressFamilies") == ["AF_UNIX"]
    assert values(SPEECH, "Service", "DevicePolicy") == ["closed"]
    assert values(SPEECH, "Service", "DeviceAllow") == ["/dev/kfd rw", "char-drm rw"]
    assert values(SPEECH, "Service", "SupplementaryGroups") == ["render video"]
    assert values(SPEECH, "Unit", "ConditionPathExists") == [SPEECH_CONFIG]
    assert not {key for _, key, _ in settings(SPEECH)} & CREDENTIAL_KEYS
    # Long-running: the resident model stays loaded between jobs; it polls its own queue.
    assert values(SPEECH, "Service", "Type") == ["simple"]
    assert values(SPEECH, "Service", "Restart") == ["on-failure"]
    assert values(SPEECH, "Install", "WantedBy") == ["multi-user.target"]
    assert not (MANUAL / "voice-lab-speech.path").exists()


def test_webhook_delivery_reaches_the_tailnet_and_nothing_else() -> None:
    assert values(WEBHOOKS, "Service", "ExecStart") == [
        f"{BINARY} speech deliver --config {SPEECH_CONFIG}"
    ]
    assert values(WEBHOOKS, "Service", "IPAddressDeny") == ["any"]
    assert values(WEBHOOKS, "Service", "IPAddressAllow") == TAILNET_ALLOWANCE
    assert values(WEBHOOKS, "Service", "PrivateDevices") == ["yes"]
    assert values(WEBHOOKS, "Service", "ReadWritePaths") == ["/data/voice-lab/state/speech"]
    # The callers' secrets reach this unit alone, as credentials from a root-only directory.
    assert {key for _, key, _ in settings(WEBHOOKS)} & CREDENTIAL_KEYS == {"LoadCredential"}
    assert values(WEBHOOKS, "Service", "LoadCredential") == [
        "speech-webhook-secrets:/etc/voice-lab/speech-webhook-secrets"
    ]
    assert values(MANUAL / "voice-lab-speech-webhooks.path", "Path", "PathChanged") == [
        "/data/voice-lab/state/speech/outbox"
    ]

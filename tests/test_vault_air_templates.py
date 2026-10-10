"""The Air vault's macOS templates (``deploy/vault/macos/``), checked as text, never installed.

The two LaunchDaemons are rendered with synthetic values and parsed as property lists: what
they run, as whom, and that the server is only ever on the loopback. The durable launcher is
loaded from its file and run with its two effects replaced (the verifier it runs and the
``execve`` it ends with), so nothing is started and no real path is read. No placeholder in
these files names a real tailnet, node or address.
"""

from __future__ import annotations

import importlib.util
import json
import plistlib
import re
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

MACOS = Path(__file__).resolve().parents[1] / "deploy" / "vault" / "macos"
SERVER = MACOS / "com.scshafe.voice-lab-vault-air.server.plist.in"
TAILSCALED = MACOS / "com.scshafe.voice-lab-vault-air.tailscaled.plist.in"
LAUNCHER = MACOS / "run-installed-vault.py"
HOME = "/opt/voice-lab-vault-air"
VALUES = {
    "VAULT_USER": "_voicelabvault",
    "VAULT_HOME": HOME,
    "VAULT_PORT": "8787",
    "TAILSCALED": "/opt/homebrew/opt/tailscale/bin/tailscaled",
    "INSTALL_RECEIPT": f"{HOME}/releases/{'a' * 64}/INSTALL_RECEIPT.json",
    "INSTALL_RECEIPT_SHA256": "b" * 64,
}


def render(template: Path) -> dict[str, Any]:
    text = template.read_text()
    for name, value in VALUES.items():
        text = text.replace(f"@{name}@", value)
    assert not re.search(r"@[A-Z_]+@", text), template.name
    return plistlib.loads(text.encode())


def test_the_server_runs_the_launcher_as_the_vault_account_on_the_loopback() -> None:
    plist = render(SERVER)

    assert plist["Label"] == "com.scshafe.voice-lab-vault-air.server"
    assert plist["UserName"] == plist["GroupName"] == "_voicelabvault"
    arguments = plist["ProgramArguments"]
    assert arguments[:2] == ["/usr/bin/python3", f"{HOME}/launcher/run-installed-vault.py"]
    options = dict(zip(arguments[2::2], arguments[3::2], strict=True))
    assert options == {
        "--receipt": VALUES["INSTALL_RECEIPT"],
        "--expected-receipt-sha256": VALUES["INSTALL_RECEIPT_SHA256"],
        "--config": f"{HOME}/config/vault-server-config.json",
        "--data-root": f"{HOME}/vault/data",
        "--access-log": f"{HOME}/vault/logs/access.jsonl",
        "--port": "8787",
    }
    assert "--listen-internal" not in arguments
    assert plist["Umask"] == 0o077 and plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}


def test_the_second_tailscaled_has_its_own_state_and_socket_in_userspace() -> None:
    plist = render(TAILSCALED)

    assert plist["Label"] == "com.scshafe.voice-lab-vault-air.tailscaled"
    assert plist["UserName"] == "_voicelabvault"
    assert plist["ProgramArguments"] == [
        VALUES["TAILSCALED"],
        "--tun=userspace-networking",
        f"--statedir={HOME}/tailscale",
        f"--socket={HOME}/tailscale/tailscaled.sock",
        "--port=0",
    ]
    assert plist["Umask"] == 0o077


def test_no_template_names_a_real_tailnet_node_or_address() -> None:
    for path in MACOS.iterdir():
        text = path.read_text()
        assert not re.search(r"\b100\.\d+\.\d+\.\d+\b", text), path.name
        assert not re.search(r"[a-z0-9-]+\.ts\.net", text.replace("<tailnet>.ts.net", "")), (
            path.name
        )


def launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_installed_vault", LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments(**changes: str) -> list[str]:
    fields = {
        "--receipt": VALUES["INSTALL_RECEIPT"],
        "--expected-receipt-sha256": VALUES["INSTALL_RECEIPT_SHA256"],
        "--config": f"{HOME}/config/vault-server-config.json",
        "--data-root": f"{HOME}/vault/data",
        "--access-log": f"{HOME}/vault/logs/access.jsonl",
        "--port": "8787",
    } | changes
    return [item for pair in fields.items() for item in pair]


class Effects:
    """The launcher's two effects, recorded instead of run."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.verified: list[list[str]] = []
        self.executed: list[tuple[str, list[str], dict[str, str]]] = []

    def run(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.verified.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode)

    def execve(self, path: str, argv: list[str], environment: dict[str, str]) -> None:
        self.executed.append((path, argv, environment))


@pytest.fixture
def receipt(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "INSTALL_RECEIPT.json"
    path.write_text(
        json.dumps({"python_runtime": {"launcher_path": f"{HOME}/releases/r/bin/python"}})
    )
    return path


def launched(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], effects: Effects
) -> tuple[ModuleType, int]:
    module = launcher()
    monkeypatch.setattr(module.subprocess, "run", effects.run)
    monkeypatch.setattr(module.os, "execve", effects.execve)
    return module, module.main(argv)


def test_the_launcher_verifies_the_release_then_runs_only_the_loopback_server(
    monkeypatch: pytest.MonkeyPatch, receipt: Path
) -> None:
    effects = Effects()
    module, status = launched(monkeypatch, arguments(**{"--receipt": str(receipt)}), effects)

    assert status == 70  # only reached because execve was replaced
    ((verifier),) = effects.verified
    assert verifier[:4] == ["/usr/bin/python3", "-I", "-B", str(MACOS / "run-installed-intake.py")]
    assert verifier[-3:] == ["--asset-root", f"{HOME}/vault/data", "--verify-only"]
    ((path, argv, environment),) = effects.executed
    assert path == f"{HOME}/releases/r/bin/python"
    assert argv == [
        path,
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.storage_server",
        "serve",
        "--config",
        f"{HOME}/config/vault-server-config.json",
        "--data-root",
        f"{HOME}/vault/data",
        "--access-log",
        f"{HOME}/vault/logs/access.jsonl",
        "--port",
        "8787",
    ]
    assert environment == module.server_environment()
    assert environment["PATH"] == "/usr/bin:/bin" and "PYTHONPATH" not in environment


def test_the_launcher_starts_nothing_when_the_release_does_not_verify(
    monkeypatch: pytest.MonkeyPatch, receipt: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    effects = Effects(returncode=1)
    with pytest.raises(SystemExit, match="did not verify"):
        launched(monkeypatch, arguments(**{"--receipt": str(receipt)}), effects)
    assert effects.executed == []

    checked = Effects()
    _, status = launched(
        monkeypatch, [*arguments(**{"--receipt": str(receipt)}), "--verify-only"], checked
    )
    assert status == 0 and checked.executed == []
    assert json.loads(capsys.readouterr().out)["verified"] is True


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"--port": "80"}, "a port from 1024"),
        ({"--port": "70000"}, "a port from 1024"),
        ({"--config": "config.json"}, "config must be an absolute"),
        ({"--data-root": f"{HOME}/vault/../data"}, "data-root must be an absolute"),
    ],
)
def test_the_launcher_refuses_a_relative_path_or_a_privileged_port(
    monkeypatch: pytest.MonkeyPatch, receipt: Path, changes: dict[str, str], message: str
) -> None:
    effects = Effects()
    with pytest.raises(SystemExit, match=message):
        launched(monkeypatch, arguments(**({"--receipt": str(receipt)} | changes)), effects)
    assert effects.verified == [] and effects.executed == []


@pytest.mark.parametrize(
    "document",
    [[], {"python_runtime": None}, {"python_runtime": {"launcher_path": "bin/python"}}],
)
def test_the_launcher_needs_the_receipts_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, document: object
) -> None:
    path = tmp_path.resolve() / "INSTALL_RECEIPT.json"
    path.write_text(json.dumps(document))
    effects = Effects()
    with pytest.raises(SystemExit, match="JSON object|no Python launcher"):
        launched(monkeypatch, arguments(**{"--receipt": str(path)}), effects)
    assert effects.executed == []

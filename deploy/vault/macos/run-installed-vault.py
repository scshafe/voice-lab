#!/usr/bin/python3
"""Verify one sealed release, then run the Air's vault server from it (design §4.3).

launchd runs this durable copy with /usr/bin/python3, as the vault's own non-admin account
(the plist's UserName). It first runs the mini intake's durable launcher, copied beside it,
in ``--verify-only`` mode: that re-hashes the install receipt, the installed console script,
the resolved Python runtime and the complete sealed release tree (deploy/README.md, "Mini
intake", steps 4 and 6). Only then does it execute exactly::

    <release>/bin/python -I -B -m cole_voice_lab.storage_server serve \\
        --config <file> --data-root <dir> --access-log <file> --port <port>

with a fixed environment. The server binds 127.0.0.1 (``serve`` without
``--listen-internal``); the Air's second, userspace tailscaled serves HTTPS on the tailnet
to that loopback port and is its only way in. Like the intake launcher this is a
stable-state check, not a sandbox against a same-account writer.

This file intentionally uses only Python 3.9-compatible standard-library APIs, because
macOS launchd invokes it with /usr/bin/python3.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

VERIFIER_NAME = "run-installed-intake.py"
SYSTEM_PYTHON = "/usr/bin/python3"
ENVIRONMENT_PATH = "/usr/bin:/bin"
MIN_PORT = 1024
MAX_PORT = 65535


def _fail(message: str) -> None:
    raise SystemExit(message)


def _absolute(value: str, label: str) -> Path:
    if not value.startswith("/") or os.path.normpath(value) != value:
        _fail(f"{label} must be an absolute, normalized path")
    return Path(value)


def verifier_command(options: argparse.Namespace, verifier: Path) -> list[str]:
    """The intake launcher's own check of the sealed release (it starts nothing)."""
    return [
        SYSTEM_PYTHON,
        "-I",
        "-B",
        str(verifier),
        "--receipt",
        str(options.receipt),
        "--expected-receipt-sha256",
        options.expected_receipt_sha256,
        "--asset-root",
        str(options.data_root),
        "--verify-only",
    ]


def server_command(options: argparse.Namespace, receipt: dict) -> list[str]:
    """The vault server, run by the sealed release's own Python, on the loopback only."""
    runtime = receipt.get("python_runtime")
    launcher = runtime.get("launcher_path") if isinstance(runtime, dict) else None
    if not isinstance(launcher, str) or not launcher.startswith("/"):
        _fail("the install receipt names no Python launcher")
    return [
        launcher,
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.storage_server",
        "serve",
        "--config",
        str(options.config),
        "--data-root",
        str(options.data_root),
        "--access-log",
        str(options.access_log),
        "--port",
        str(options.port),
    ]


def server_environment() -> dict:
    return {
        "PATH": ENVIRONMENT_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C.UTF-8",
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--expected-receipt-sha256", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--access-log", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--verify-only", action="store_true")
    options = parser.parse_args(argv)
    for name in ("receipt", "config", "data_root", "access_log"):
        setattr(options, name, _absolute(getattr(options, name), name.replace("_", "-")))
    if not MIN_PORT <= options.port <= MAX_PORT:
        _fail(f"the vault listens on a port from {MIN_PORT} to {MAX_PORT}")

    verifier = Path(os.path.abspath(__file__)).parent / VERIFIER_NAME
    checked = subprocess.run(
        verifier_command(options, verifier), env={"PATH": ENVIRONMENT_PATH}, check=False
    )
    if checked.returncode != 0:
        _fail("the sealed release did not verify; the vault server was not started")
    receipt = json.loads(options.receipt.read_bytes())
    if not isinstance(receipt, dict):
        _fail("the install receipt is not a JSON object")
    command = server_command(options, receipt)
    if options.verify_only:
        print(json.dumps({"verified": True, "command": command}, sort_keys=True))
        return 0
    os.execve(command[0], command, server_environment())
    return 70


if __name__ == "__main__":
    sys.exit(main())

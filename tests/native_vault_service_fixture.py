"""@canonical native_vault_service_fixture -- loopback-only native vault service and worker.

Copied only into an isolated synthetic runtime. The service itself never loads a key or
token: its worker reads the private fixture credentials through the existing fake Keychain
and connection seams. The explicit worker bootstrap preserves that isolation under ``-I``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cole_voice_lab import library_replication_service, recording_service
from cole_voice_lab.paths import AssetLayout

MODULE = "cole_voice_lab._synthetic_vault_fixture"


def isolated_command(runtime: Path, *arguments: str) -> list[str]:
    """Run this fixture module from its copied runtime without PYTHONPATH or user imports."""
    return [
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import runpy,sys; sys.path.insert(0, sys.argv.pop(1)); "
        f"runpy.run_module('{MODULE}', run_name='__main__')",
        str(runtime),
        *arguments,
    ]


def main() -> int:
    """Run the real service or worker with test-only loopback seams; no hosted I/O."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-vault-worker", action="store_true")
    parser.add_argument("--test-vault-credentials", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    options, remaining = parser.parse_known_args()
    if options.test_vault_worker:
        import pytest
        from vault_binding_fixtures import (
            MemoryKeychain,
            install_vault_seams,
            store_vault_token,
        )

        from cole_voice_lab.storage_contracts import library_key_item

        credentials = json.loads(options.test_vault_credentials.read_text())
        keychain = MemoryKeychain()
        keychain.items[(library_key_item(credentials["library_id"]), "library-key")] = credentials[
            "key"
        ]
        store_vault_token(
            keychain, token=credentials["token"], library_id=credentials["library_id"]
        )
        with pytest.MonkeyPatch.context() as monkeypatch:
            seams = install_vault_seams(monkeypatch, keychain=keychain, port=credentials["port"])
            # Fail fast so the suite observes a real outage without production retry delays.
            from cole_voice_lab.storage_http_client import VaultConnection

            def connect(**arguments: Any) -> VaultConnection:
                return VaultConnection.loopback_for_tests(
                    seams.port,
                    arguments["namespace"],
                    arguments["token"],
                    request_log=arguments["request_log"],
                    timeout=0.5,
                    attempts=1,
                )

            from cole_voice_lab import storage_binding

            monkeypatch.setattr(storage_binding, "vault_connection", connect)
            return library_replication_service.main(
                ["--worker", "--asset-root", str(options.asset_root), *remaining]
            )

    shipped = library_replication_service.VaultSupervisor

    class FixtureVaultSupervisor(shipped):
        def __init__(self, layout: AssetLayout, **arguments: Any) -> None:
            runtime = Path(__file__).resolve().parents[1]
            runner = library_replication_service.WorkerPassRunner(
                layout,
                command=lambda _: isolated_command(
                    runtime,
                    "--test-vault-worker",
                    "--test-vault-credentials",
                    str(options.test_vault_credentials),
                    "--asset-root",
                    str(layout.root),
                ),
            )
            super().__init__(
                layout,
                processes=runner,
                quiet_period=0.5,
                rescan_interval=1.0,
                backoff=(0.5,),
                publish_interval=0.05,
                poll=0.02,
                **arguments,
            )

    with patch.object(library_replication_service, "VaultSupervisor", FixtureVaultSupervisor):
        return recording_service.main(["--asset-root", str(options.asset_root), *remaining])


if __name__ == "__main__":
    raise SystemExit(main())

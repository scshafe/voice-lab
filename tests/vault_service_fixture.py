"""Test-only wrapper: the real recording service with a local-directory vault it can reach.

The shipped service opens its vault through the composition root and nothing else: no flag,
argument or environment variable of the service chooses a destination, a key or a way of
connecting. So a test that wants a real service to copy has to stand where the composition
root's seams are, which is here.

Two roles, one module, because the worker must be a standalone script (the supervisor starts
it with ``-I``, which drops every search path but the interpreter's own):

- ``--test-vault-service``: run ``recording_service.main`` with a vault supervisor whose
  intervals are seconds rather than minutes and whose passes run this file as their worker.
- ``--test-vault-worker``: run ``library_replication_service.main`` with a Keychain that is a
  dictionary holding one synthetic key, and a blob store that waits while a named file exists
  (which is how "the upload is stalled" is played without a network).

Nothing here touches the login Keychain, a tailnet peer, a network or the real library, and
none of it is reachable from the shipped code.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cole_voice_lab import library_replication_service, recording_service
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.storage_ports import BlobRef, BlobStore, storage_error

WRAPPER = str(Path(__file__).resolve())
# A synthetic library key, in the hex form the Keychain item holds. Never a real library's.
SYNTHETIC_KEY_HEX = bytes(range(0x20, 0x40)).hex()
# How long a stalled upload waits before it gives up, so a broken test cannot hang a machine.
STALL_DEADLINE_SECONDS = 120.0
_STALLED_MARKER = "stalled"
_STALL_MARKER = "stall"


def worker_command(asset_root: Path, stall_directory: Path) -> list[str]:
    """The command one fixture pass runs: this file, as its own isolated script."""
    return [
        sys.executable,
        "-I",
        "-B",
        WRAPPER,
        "--test-vault-worker",
        "--asset-root",
        str(asset_root),
        "--test-stall-directory",
        str(stall_directory),
    ]


def stall(directory: Path) -> None:
    """Make every later upload wait; the pass that meets it leaves a ``stalled`` marker."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / _STALL_MARKER).write_bytes(b"")


def release(directory: Path) -> None:
    """Let uploads through again."""
    (directory / _STALL_MARKER).unlink(missing_ok=True)


def stalled(directory: Path) -> bool:
    """Whether a pass has actually reached an upload and is waiting there."""
    return (directory / _STALLED_MARKER).exists()


class _StalledBlobStore:
    """A blob store that waits before each write while the stall file is there."""

    def __init__(self, inner: BlobStore, directory: Path) -> None:
        self._inner = inner
        self._directory = directory

    def _wait(self) -> None:
        deadline = time.monotonic() + STALL_DEADLINE_SECONDS
        while (self._directory / _STALL_MARKER).exists():
            (self._directory / _STALLED_MARKER).write_bytes(b"")
            if time.monotonic() > deadline:
                raise storage_error("STORAGE.TIMEOUT", "the stalled fixture upload gave up")
            time.sleep(0.02)

    def put(self, data: bytes) -> BlobRef:
        self._wait()
        return self._inner.put(data)

    def has(self, ref: BlobRef) -> bool:
        return self._inner.has(ref)

    def get(self, ref: BlobRef) -> bytes:
        return self._inner.get(ref)


class _MemoryKeychain:
    """The Keychain seam as a dictionary (copied from tests/vault_binding_fixtures.py)."""

    def __init__(self, items: dict[tuple[str, str], str]) -> None:
        self.items = items

    def add_generic_password(self, *, service: str, account: str, label: str, secret: str) -> None:
        if (service, account) in self.items:
            raise storage_error("STORAGE.CONFLICT", f"the Keychain already holds item {service}")
        self.items[(service, account)] = secret

    def find_generic_password(self, *, service: str, account: str) -> str:
        if (service, account) not in self.items:
            raise storage_error("STORAGE.NOT_BOUND", f"the Keychain has no item {service}")
        return self.items[(service, account)]

    def has_generic_password(self, *, service: str, account: str) -> bool:
        return (service, account) in self.items


def _run_worker(asset_root: Path, stall_directory: Path, remaining: list[str]) -> int:
    from cole_voice_lab import storage_binding, storage_local
    from cole_voice_lab.storage_contracts import library_key_item

    identity = storage_binding.require_library_identity(AssetLayout.validate(asset_root))
    item = library_key_item(identity.library_id)
    keychain = _MemoryKeychain({(item, "library-key"): SYNTHETIC_KEY_HEX})
    local = storage_local.LocalBlobStore

    def stalled_store(root: Path, **arguments: Any) -> BlobStore:
        return _StalledBlobStore(local(root, **arguments), stall_directory)

    with (
        patch.object(storage_binding, "keychain_client", lambda: keychain),
        patch.object(storage_local, "LocalBlobStore", stalled_store),
    ):
        return library_replication_service.main(
            ["--worker", "--asset-root", str(asset_root), *remaining]
        )


def _run_service(asset_root: Path, stall_directory: Path, remaining: list[str]) -> int:
    shipped = library_replication_service.VaultSupervisor

    class FixtureVaultSupervisor(shipped):  # type: ignore[valid-type,misc]
        def __init__(self, layout: AssetLayout, **arguments: Any) -> None:
            if "processes" in arguments:
                raise ValueError("the recording service must never choose a vault pass runner")
            runner = library_replication_service.WorkerPassRunner(
                layout, command=lambda _layout: worker_command(asset_root, stall_directory)
            )
            super().__init__(
                layout,
                processes=runner,
                quiet_period=1.0,
                rescan_interval=5.0,
                backoff=(1.0,),
                publish_interval=0.2,
                **arguments,
            )

    with patch.object(library_replication_service, "VaultSupervisor", FixtureVaultSupervisor):
        return recording_service.main(remaining)


def main(argv: list[str] | None = None) -> int:
    """Run the fixture service or the fixture vault worker.

    Returns: the wrapped entry point's status.
    Raises: ``ValueError`` when neither role is asked for.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-vault-service", action="store_true")
    parser.add_argument("--test-vault-worker", action="store_true")
    parser.add_argument("--test-stall-directory", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    options, remaining = parser.parse_known_args(argv)
    if options.test_vault_worker == options.test_vault_service:
        raise ValueError("exactly one of --test-vault-service and --test-vault-worker")
    asset_root = options.asset_root
    if options.test_vault_worker:
        return _run_worker(asset_root, options.test_stall_directory, remaining)
    return _run_service(
        asset_root, options.test_stall_directory, ["--asset-root", str(asset_root), *remaining]
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""@canonical native_vault_fixture -- native vault copy, outage, recovery and archive suite.

Only synthetic audio and a loopback listener are used. Fresh fixture credentials live in a
private external file outside the library; the actual isolated worker receives its path.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from native_speak_fixture import LATEST_RUN_ID, prepare_speak_fixture
from verify_native_finetune import _require_no_fixture_processes, _require_saved_original

from cole_voice_lab import storage_binding
from cole_voice_lab.canonical import sha256_file, write_canonical_json
from cole_voice_lab.finetune import FineTuneArchivedFile, FineTuneRunRemoval
from cole_voice_lab.library_generations import VaultRecords, verify_generation
from cole_voice_lab.recording_takes import RecordingTakeStore
from cole_voice_lab.storage_contracts import VaultNamespaceConfig, VaultServerConfig
from cole_voice_lab.storage_server import VaultServer, hash_token, new_token

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "tests"))

from native_vault_service_fixture import isolated_command  # noqa: E402
from vault_binding_fixtures import (  # noqa: E402
    APPROVAL_ID,
    COPIED_CLASSES,
    ENDPOINT,
    LIBRARY_ID,
    NAMESPACE,
    MemoryKeychain,
    approval_record,
    install_vault_seams,
    store_vault_token,
    write_approval,
)

HARNESS_TIMEOUT = 240.0


class NativeVaultListener:
    """An owned loopback listener whose failed startup is safe to close.

    Credentials belong to this fixture only. ``stop`` preserves them across an outage;
    ``close`` always removes them, even when listener or thread cleanup raises.
    """

    def __init__(self, root: Path, config: VaultServerConfig, *, key: str, token: str) -> None:
        self.root = root
        self.config = config
        self.credentials = root / "fixture-credentials.json"
        self._key = key
        self._token = token
        self.server: VaultServer | None = None
        self.thread: threading.Thread | None = None
        self.thread_started = False

    def start(self, *, provision: bool) -> int:
        """Start a fresh listener; propagate startup errors for ``close`` to clean up."""
        if self.server is not None:
            raise RuntimeError("the loopback fixture listener is already constructed")
        self.server = VaultServer(
            ("127.0.0.1", 0),
            config=self.config,
            data_root=self.root / "vault-data",
            access_log=self.root / "vault-access.jsonl",
            provision=provision,
        )
        port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.thread_started = True
        write_canonical_json(
            self.credentials,
            {"library_id": LIBRARY_ID, "key": self._key, "token": self._token, "port": port},
        )
        return port

    def stop(self) -> None:
        """Stop only a started serving loop; always close the constructed server."""
        server, thread, started = self.server, self.thread, self.thread_started
        self.server, self.thread, self.thread_started = None, None, False
        if server is None:
            return
        try:
            if started:
                server.shutdown()
        finally:
            try:
                server.server_close()
            finally:
                if started:
                    assert thread is not None
                    thread.join(timeout=5)
                    assert not thread.is_alive(), "loopback vault thread outlived shutdown"

    def close(self) -> None:
        """Stop the listener and delete fixture credentials even if stopping raises."""
        try:
            self.stop()
        finally:
            self.credentials.unlink(missing_ok=True)


def run_vault_suite(root: Path, *, package: Path, binary_directory: Path) -> None:
    """Run actual native peers; independently decrypt and verify the take afterwards.

    Raises: subprocess errors, timeout, or AssertionError for a missing durable result.
    Every listener, thread and harness process is stopped on success and failure.
    """
    prepared = prepare_speak_fixture(
        root / "fixture", package=package, binary_directory=binary_directory
    )
    fixture = prepared.fixture
    native = root / "native"
    native.mkdir(mode=0o700)
    # Archived-run UI needs only a removed checkpoint and the canonical removal record.
    # Training is deliberately excluded from this fixture's binding.
    run_root = fixture.layout.root / "derived/finetune" / LATEST_RUN_ID
    checkpoint = run_root / "checkpoints/model_last.pt"
    digest, size = sha256_file(checkpoint)
    removal = FineTuneRunRemoval(
        run_id=LATEST_RUN_ID,
        generation=1,
        manifest_sha256="a" * 64,
        removed_at=datetime.now(UTC),
        files=(
            FineTuneArchivedFile(
                path="checkpoints/model_last.pt", sha256=digest, size=size, kind="file"
            ),
        ),
    )
    write_canonical_json(run_root / "local-removal.json", removal)
    checkpoint.unlink()

    token, read_token = new_token(), new_token()
    key = secrets.token_bytes(32).hex()
    credentials = root / "fixture-credentials.json"
    server_config = VaultServerConfig(
        namespaces=(
            VaultNamespaceConfig(
                name=NAMESPACE,
                read_token_sha256=hash_token(read_token),
                write_token_sha256=hash_token(token),
                quota_bytes=64 * 1024 * 1024,
            ),
        ),
        max_workers=4,
        request_timeout_seconds=5,
    )
    listener = NativeVaultListener(root, server_config, key=key, token=token)

    harness: subprocess.Popen[bytes] | None = None
    runtime = fixture.root / "python-runtime"
    try:
        port = listener.start(provision=True)
        storage_binding.initialize_library(fixture.layout, library_id=LIBRARY_ID)
        _, approval_sha = write_approval(fixture.layout, approval_record())
        storage_binding.bind_library(
            fixture.layout,
            backend="tailnet-vault",
            endpoint=ENDPOINT,
            namespace=NAMESPACE,
            classes=COPIED_CLASSES,
            approval_id=APPROVAL_ID,
            approval_sha256=approval_sha,
        )
        shutil.copy2(
            REPOSITORY / "tests/native_vault_service_fixture.py",
            runtime / "cole_voice_lab/_synthetic_vault_fixture.py",
        )
        shutil.copy2(
            REPOSITORY / "tests/vault_binding_fixtures.py", runtime / "vault_binding_fixtures.py"
        )
        configuration = json.loads(fixture.configuration.read_text())
        environment = configuration["service"]["environment"]
        # Recompute the identity after the test-only wrapper joins the copied package.
        identity = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from cole_voice_lab.recording_service import recording_service_build_identity "
                    "as identity; print(identity().model_dump_json())",
                    str(runtime),
                ],
                env=environment,
            )
        )
        configuration["service"]["build"] = identity
        write_canonical_json(fixture.root / "service-build.json", identity)
        configuration["service"]["arguments"] = isolated_command(
            runtime,
            "--test-vault-credentials",
            str(credentials),
            "--native-workspace",
            "--subject-id",
            "synthetic-subject",
            "--finetune-runtime-root",
            str(fixture.runtime_root),
        )[1:]
        write_canonical_json(fixture.configuration, configuration)
        harness = subprocess.Popen(
            [str(fixture.harness), "vault", str(fixture.configuration), str(native)]
        )
        deadline = time.monotonic() + HARNESS_TIMEOUT
        stopped = restarted = False
        while harness.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(harness.args, HARNESS_TIMEOUT)
            if not stopped and (native / "stop-vault").exists():
                listener.stop()
                stopped = True
            if stopped and not restarted and (native / "start-vault").exists():
                port = listener.start(provision=False)
                restarted = True
            time.sleep(0.02)
        if harness.returncode:
            raise subprocess.CalledProcessError(harness.returncode, harness.args)
        assert stopped and restarted, "the native harness did not exercise outage and recovery"
        _require_no_fixture_processes(fixture.layout.root, runtime, credentials)
        report = json.loads((native / "vault-report.json").read_text())
        store = RecordingTakeStore(fixture.layout, read_only=True)
        _require_saved_original(store, fixture.layout, report["take_id"])
        path = f"tmp/recording-takes/{report['take_id']}/original.wav"
        keychain = MemoryKeychain()
        from cole_voice_lab.storage_contracts import keychain_item_names

        keychain.items[(keychain_item_names(LIBRARY_ID).key, "library-key")] = key
        store_vault_token(keychain, token=token)
        with pytest.MonkeyPatch.context() as monkeypatch:
            install_vault_seams(monkeypatch, keychain=keychain, port=port)
            vault = storage_binding.open_library_vault(fixture.layout)
            assert vault is not None
            verified = verify_generation(
                fixture.layout,
                blob_store=vault.blob_store,
                ref_store=vault.ref_store,
                library_id=LIBRARY_ID,
            )
        assert verified.ok, verified
        assert any(entry.path == path and entry.state == "present" for entry in verified.entries)
        manifest = VaultRecords(fixture.layout).read_manifest_copy(verified.generation)
        assert manifest is not None
        entry = next(entry for entry in manifest.entries if entry.path == path)
        assert (entry.sha256, entry.size) == sha256_file(fixture.layout.root / path)
        assert report["visible_windows"] == 0 and report["fixed_geometry"]
        write_canonical_json(
            root / "python-vault-evidence.json",
            {
                "result": "passed",
                "take_id": report["take_id"],
                "original_sha256": entry.sha256,
                "verified_generation": verified.generation,
                "verified_entries": len(verified.entries),
                "outage_and_recovery": True,
                "archived_run": LATEST_RUN_ID,
            },
        )
    finally:
        try:
            if harness is not None and harness.poll() is None:
                harness.terminate()
                try:
                    harness.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    harness.kill()
                    harness.wait(timeout=10)
        finally:
            try:
                listener.close()
            finally:
                _require_no_fixture_processes(fixture.layout.root, runtime, credentials)

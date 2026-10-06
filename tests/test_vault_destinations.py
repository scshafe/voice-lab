"""Per-destination vault records and the Linux credential keychain, end to end (D-034).

The PC copies one library to several vaults (the laptop now, the Air in phase 3). Each
destination has its own binding, approvals, state, manifest chain, ledger, push lock, request
log and its own pair of vault tokens; the library identity and the library key are the
library's. On Linux the key and tokens are systemd credentials: here a private staging
directory under ``tmp_path`` is ``$CREDENTIALS_DIRECTORY`` while the operator creates them,
and is then made read-only the way systemd hands it to the vault unit. The real
``storage_binding.keychain_client`` is used throughout (with ``sys.platform`` read as
``linux``); only the tailnet resolver and the connection are the loopback seams of
``tests/vault_binding_fixtures.py``. One vault server on the loopback serves both
destinations' namespaces, each with its own tokens, as two servers would. Everything is
synthetic: no real library, credential, unit or tailnet peer is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from vault_binding_fixtures import (
    COPIED_CLASSES,
    ENDPOINT,
    LIBRARY_ID,
    NODE_STABLE_ID,
    TAILNET_SUFFIX,
    VaultSeams,
    add_library_records,
    approval_arguments,
    approval_record,
    install_vault_seams,
    restore_permissions,
    write_library_file,
)

import cole_voice_lab.storage_binding as binding_module
from cole_voice_lab.cli import main
from cole_voice_lab.library_generations import (
    PushRunningError,
    VaultRecords,
    recorded_destinations,
    rehash_restored_library,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.storage_contracts import (
    LibraryManifest,
    VaultNamespaceConfig,
    VaultServerConfig,
    keychain_item_names,
    library_key_item,
)
from cole_voice_lab.storage_server import VaultServer

REAL_KEYCHAIN_CLIENT = binding_module.keychain_client
DESTINATIONS = ("laptop", "air")


@pytest.fixture(autouse=True)
def _linux_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")


@pytest.fixture(autouse=True)
def _writable_tmp_path(tmp_path: Path) -> Iterator[None]:
    yield
    restore_permissions(tmp_path)


@dataclass
class Cli:
    """Runs ``voice-lab`` in this process and keeps every byte it printed."""

    capsys: pytest.CaptureFixture[str]
    output: list[str]

    def __call__(self, *arguments: str) -> tuple[int, str, str]:
        status = main(list(arguments))
        captured = self.capsys.readouterr()
        self.output.extend((captured.out, captured.err))
        return status, captured.out, captured.err

    def json(self, *arguments: str) -> Any:
        status, out, err = self(*arguments)
        assert status == 0, err
        return json.loads(out)

    def refused(self, *arguments: str, code: str) -> str:
        status, out, err = self(*arguments)
        assert status == 2, out
        assert err.startswith(f"voice-lab: {code}: "), err
        return err


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Cli:
    return Cli(capsys, [])


@dataclass(frozen=True)
class Host:
    """The PC as a test plays it: its lab, its credential staging directory and its seams."""

    root: Path
    credentials: Path
    sheets: Path
    seams: VaultSeams

    def vault(self, *arguments: str, destination: str | None = None) -> list[str]:
        command = ["vault", *arguments, "--asset-root", str(self.root)]
        return command if destination is None else [*command, "--destination", destination]

    def secrets(self) -> dict[str, str]:
        return {
            item.name: item.read_text().strip()
            for item in self.credentials.iterdir()
            if item.name.startswith("private.")
        }

    def as_systemd_gives_it(self) -> None:
        """What the vault unit sees: the same items, 0400, in a read-only directory."""
        for item in self.credentials.iterdir():
            item.chmod(0o400)
        self.credentials.chmod(0o500)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    layout = AssetLayout.initialize(tmp_path.resolve() / "lab")
    add_library_records(layout)
    credentials = tmp_path.resolve() / "credstage"
    credentials.mkdir(mode=0o700)
    sheets = tmp_path.resolve() / "sheets"
    sheets.mkdir(mode=0o700)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    seams = install_vault_seams(monkeypatch)
    # The keychain is the real one: on Linux, the credential directory.
    monkeypatch.setattr(binding_module, "keychain_client", REAL_KEYCHAIN_CLIENT)
    return Host(layout.root, credentials, sheets, seams)


@contextmanager
def serving(tmp_path: Path, hashes: Mapping[str, tuple[str, str]]) -> Iterator[VaultServer]:
    """One loopback vault serving one namespace per destination, configured by token hashes."""
    config = VaultServerConfig(
        namespaces=tuple(
            VaultNamespaceConfig(
                name=name,
                read_token_sha256=read,
                write_token_sha256=write,
                quota_bytes=64 * 1024 * 1024,
            )
            for name, (read, write) in hashes.items()
        ),
        max_workers=4,
        request_timeout_seconds=30,
    )
    server = VaultServer(
        ("127.0.0.1", 0),
        config=config,
        data_root=tmp_path.resolve() / "vault-data",
        access_log=tmp_path.resolve() / "vault-access.jsonl",
        provision=True,
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
        server.server_close()


def create_credentials(cli: Cli, host: Host) -> dict[str, tuple[str, str]]:
    """The operator's one-time ceremony: library, key with its sheet, each destination's tokens."""
    cli.json(*host.vault("init"), "--library-id", LIBRARY_ID)
    created = cli.json(*host.vault("key", "create"), "--sheet-out", str(host.sheets / "sheet.txt"))
    assert created["library_id"] == LIBRARY_ID
    hashes: dict[str, tuple[str, str]] = {}
    for destination in DESTINATIONS:
        tokens = cli.json(
            *host.vault("token", "create", destination=destination), "--namespace", destination
        )
        names = keychain_item_names(LIBRARY_ID, destination)
        assert (tokens["write_token_item"], tokens["read_token_item"]) == (
            names.token,
            names.read_token,
        )
        hashes[destination] = (tokens["read_token_sha256"], tokens["write_token_sha256"])
    return hashes


def approve_and_bind(cli: Cli, host: Host, destination: str) -> str:
    approval = approval_record(
        approval_id=f"{destination}-2026-10-06", destination=destination, namespace=destination
    )
    fields = [*host.vault("approve"), *approval_arguments(approval)]
    digest = cli.json(*fields, "--preview")["approval_sha256"]
    cli.json(*fields, "--reviewed-approval-sha256", digest, "--i-approve-this-destination")
    bound = cli.json(
        *host.vault("bind", destination=destination),
        "--backend",
        "tailnet-vault",
        "--endpoint",
        ENDPOINT,
        "--namespace",
        destination,
        "--classes",
        *COPIED_CLASSES,
        "--approval-id",
        approval.approval_id,
        "--approval-sha256",
        digest,
    )
    assert bound["binding"]["destination"] == destination
    return str(digest)


def manifest_copy(root: Path, destination: str, generation: int) -> LibraryManifest:
    path = root / f"provenance/vault/destinations/{destination}/manifests/{generation:08d}.json"
    return LibraryManifest.model_validate_json(path.read_bytes())


def rehash_independently(target: Path, manifest: LibraryManifest) -> int:
    """Re-hash every restored file against the manifest, without the library's own code."""
    checked = 0
    for entry in manifest.entries:
        if entry.kind != "file":
            continue
        data = (target / entry.path).read_bytes()
        assert (hashlib.sha256(data).hexdigest(), len(data)) == (entry.sha256, entry.size)
        assert (target / entry.path).stat().st_mode & 0o777 == entry.mode
        checked += 1
    return checked


# -- the whole flow on Linux -----------------------------------------------------------------


def test_two_destinations_copy_verify_and_drill_from_systemd_credentials(
    cli: Cli, host: Host, tmp_path: Path
) -> None:
    hashes = create_credentials(cli, host)
    secrets = host.secrets()
    expected = {library_key_item(LIBRARY_ID)} | {
        name
        for destination in DESTINATIONS
        for name in keychain_item_names(LIBRARY_ID, destination)[1:]
    }
    assert set(secrets) == expected
    for item in host.credentials.iterdir():
        assert item.stat().st_mode & 0o777 == 0o600
    # Two vaults never share a token, and the server only ever learns hashes.
    assert len(set(secrets.values())) == len(secrets)
    for destination in DESTINATIONS:
        names = keychain_item_names(LIBRARY_ID, destination)
        read, write = hashes[destination]
        assert hashlib.sha256(secrets[names.token].encode()).hexdigest() == write
        assert hashlib.sha256(secrets[names.read_token].encode()).hexdigest() == read

    for destination in DESTINATIONS:
        approve_and_bind(cli, host, destination)
    host.as_systemd_gives_it()

    with serving(tmp_path, hashes) as server:
        host.seams.port = int(server.server_address[1])
        first = {
            destination: cli.json(*host.vault("push", destination=destination))
            for destination in DESTINATIONS
        }
        assert {result["generation"] for result in first.values()} == {1}
        assert {result["destination"] for result in first.values()} == set(DESTINATIONS)
        # Each push sent its own destination's write token, and only that.
        sent = [request.token() for request in host.seams.connections]
        laptop, air = (keychain_item_names(LIBRARY_ID, name) for name in DESTINATIONS)
        assert secrets[laptop.token] in sent and secrets[air.token] in sent

        # A new take reaches the laptop tonight; the Air is asleep and keeps its generation.
        write_library_file(host.root, "provenance/consents/cole-self-v2.json", b'{"v":2}\n')
        second = cli.json(*host.vault("push", destination="laptop"))
        assert second["generation"] == 2
        layout = AssetLayout.validate(host.root)
        assert VaultRecords(layout, "laptop").read_state(LIBRARY_ID).generation == 2  # type: ignore[union-attr]
        assert VaultRecords(layout, "air").read_state(LIBRARY_ID).generation == 1  # type: ignore[union-attr]
        assert recorded_destinations(layout) == ("air", "laptop")
        for destination in DESTINATIONS:
            records = host.root / "provenance/vault/destinations" / destination
            assert sorted(path.name for path in records.iterdir()) == [
                ".push.lock",
                "approvals",
                "binding.json",
                "ledger",
                "manifests",
                "requests",
                "state.json",
            ]
        assert sorted(path.name for path in (host.root / "provenance/vault").iterdir()) == [
            "destinations",
            "library.json",
        ]

        verified = cli.json(*host.vault("verify", destination="laptop"))
        assert verified["ok"] and verified["generation"] == 2

        # The drill: restore through the binding, then re-hash every file from disk.
        drills = tmp_path.resolve() / "drills"
        drills.mkdir(mode=0o700)
        drill = cli.json(
            *host.vault("drill", destination="laptop"),
            "--target",
            str(drills / "laptop"),
            "--accept-latest",
        )
        manifest = manifest_copy(host.root, "laptop", 2)
        assert drill["ok"] and drill["problems"] == []
        assert drill["generation"] == 2 and drill["rehashed_files"] == drill["restored_files"]
        assert rehash_independently(drills / "laptop", manifest) == drill["rehashed_files"]
        assert drill["rehashed_files"] >= 8
        assert (drills / "laptop/provenance/consents/cole-self-v2.json").read_bytes() == (
            b'{"v":2}\n'
        )
        assert not (drills / "laptop/provenance/vault").exists()

        # A named generation drills the same way, and a half-named one is a usage refusal.
        named = cli.json(
            *host.vault("drill", destination="air"),
            "--target",
            str(drills / "air"),
            "--generation",
            "1",
            "--manifest-sha256",
            first["air"]["manifest_sha256"],
        )
        assert named["ok"] and named["generation"] == 1
        assert rehash_independently(drills / "air", manifest_copy(host.root, "air", 1)) > 0
        cli.refused(
            *host.vault("drill", destination="air"),
            "--target",
            str(drills / "half"),
            "--generation",
            "1",
            code="VAULT_CLI.USAGE_INVALID",
        )

        # A host with no binding restores with the Air's read token, by naming the vault.
        host.seams.connections.clear()
        restored = cli.json(
            *host.vault("restore", destination="air"),
            "--target",
            str(drills / "by-endpoint"),
            "--endpoint",
            ENDPOINT,
            "--namespace",
            "air",
            "--node-stable-id",
            NODE_STABLE_ID,
            "--tailnet-suffix",
            TAILNET_SUFFIX,
            "--accept-latest",
        )
        assert restored["generation"] == 1
        assert [request.token() for request in host.seams.connections] == [secrets[air.read_token]]

        shown = cli.json(*host.vault("show", destination="laptop"), "--json")
        assert shown["destinations"] == ["air", "laptop"]
        assert shown["secrets"]["write_token"] and shown["secrets"]["read_token"]
        assert shown["secrets"]["key_fingerprint"] == json.loads(cli.output[2])["key_fingerprint"]

    # Under systemd the credentials are read-only: nothing can add an item.
    cli.refused(
        *host.vault("token", "create", destination="usb"),
        "--namespace",
        "usb",
        code="STORAGE.PROTOCOL",
    )
    everything = "\n".join(cli.output)
    for secret in secrets.values():
        assert secret not in everything


def test_the_drill_finds_a_restored_file_that_is_not_what_the_manifest_names(
    cli: Cli, host: Host, tmp_path: Path
) -> None:
    hashes = create_credentials(cli, host)
    approve_and_bind(cli, host, "laptop")
    with serving(tmp_path, hashes) as server:
        host.seams.port = int(server.server_address[1])
        cli.json(*host.vault("push", destination="laptop"))
        target = tmp_path.resolve() / "drill"
        assert cli.json(
            *host.vault("drill", destination="laptop"), "--target", str(target), "--accept-latest"
        )["ok"]
    manifest = manifest_copy(host.root, "laptop", 1)
    report = rehash_restored_library(target, manifest)
    assert report.ok and report.exit_status == 0 and not report.problems
    assert report.checked_bytes == sum(entry.size for entry in manifest.entries)

    restore_permissions(target)
    changed, shortened, moded, removed = (
        "provenance/consents/cole-self-v1.json",
        "provenance/headphone-reviews/review-1.json",
        "provenance/transfer-approvals/approval-1.json",
        "raw/loose-note.txt",
    )
    original = (target / changed).read_bytes()
    (target / changed).write_bytes(original[:-1] + b"X")
    (target / shortened).write_bytes(b"{}")
    (target / moded).chmod(0o644)
    (target / removed).unlink()
    for entry in manifest.entries:
        if entry.path not in (changed, shortened, moded, removed):
            (target / entry.path).chmod(entry.mode)

    report = rehash_restored_library(target, manifest)
    problems = {entry.path: (entry.state, entry.detail) for entry in report.problems}
    assert problems[changed][0] == "mismatch" and "SHA-256" in str(problems[changed][1])
    assert problems[shortened] == ("mismatch", "2 bytes, not 13")
    assert problems[moded] == ("mismatch", "mode 0644, not 0600")
    assert problems[removed] == ("missing", "no regular file is at this path")
    assert set(problems) == {changed, shortened, moded, removed}
    assert not report.ok and report.exit_status == 1

    # A link where a directory was is never followed: the drill refuses the tree.
    elsewhere = tmp_path.resolve() / "elsewhere"
    (target / "references").rename(elsewhere)
    (target / "references").symlink_to(elsewhere)
    from cole_voice_lab.library_generations import RestoreRefusedError

    with pytest.raises(RestoreRefusedError, match="symlink or not a directory"):
        rehash_restored_library(target, manifest)


# -- nothing crosses between destinations --------------------------------------------------


def test_an_approval_or_a_binding_never_serves_another_destination(cli: Cli, host: Host) -> None:
    cli.json(*host.vault("init"), "--library-id", LIBRARY_ID)
    digest = approve_and_bind(cli, host, "laptop")
    laptop = host.root / "provenance/vault/destinations/laptop"

    # The laptop's approval is not the Air's, by id ...
    assert "does not exist" in cli.refused(
        *host.vault("bind", destination="air"),
        "--backend",
        "tailnet-vault",
        "--endpoint",
        ENDPOINT,
        "--namespace",
        "laptop",
        "--classes",
        *COPIED_CLASSES,
        "--approval-id",
        "laptop-2026-10-06",
        "--approval-sha256",
        digest,
        code="STORAGE.NOT_APPROVED",
    )
    # ... nor when its file is copied under the Air's name.
    air = host.root / "provenance/vault/destinations/air"
    (air / "approvals").mkdir(parents=True, mode=0o700)
    air.chmod(0o700)
    approval = (laptop / "approvals/laptop-2026-10-06.json").read_bytes()
    (air / "approvals/laptop-2026-10-06.json").write_bytes(approval)
    (air / "approvals/laptop-2026-10-06.json").chmod(0o400)
    assert "approves destination laptop, not air" in cli.refused(
        *host.vault("bind", destination="air"),
        "--backend",
        "tailnet-vault",
        "--endpoint",
        ENDPOINT,
        "--namespace",
        "laptop",
        "--classes",
        *COPIED_CLASSES,
        "--approval-id",
        "laptop-2026-10-06",
        "--approval-sha256",
        digest,
        code="STORAGE.NOT_APPROVED",
    )
    # A binding copied under another destination's name binds nothing there.
    (air / "binding.json").write_bytes((laptop / "binding.json").read_bytes())
    (air / "binding.json").chmod(0o600)
    assert "binds destination laptop, not air" in cli.refused(
        *host.vault("push", destination="air"), code="STORAGE.NOT_BOUND"
    )
    # An approval naming one destination cannot cover a binding of another.
    from cole_voice_lab.storage_binding import _require_approval_covers, read_storage_binding

    bound = read_storage_binding(AssetLayout.validate(host.root), "laptop")
    assert bound is not None
    from cole_voice_lab.storage_ports import StorageError

    with pytest.raises(StorageError, match="approves destination air, not laptop"):
        _require_approval_covers(approval_record(destination="air"), bound)


def test_pushes_to_two_destinations_never_wait_for_each_other(
    cli: Cli, host: Host, tmp_path: Path
) -> None:
    hashes = create_credentials(cli, host)
    for destination in DESTINATIONS:
        approve_and_bind(cli, host, destination)
    layout = AssetLayout.validate(host.root)
    with serving(tmp_path, hashes) as server:
        host.seams.port = int(server.server_address[1])
        with VaultRecords(layout, "air").push_lock():
            # The Air's push is stuck mid-upload: the laptop's copy goes ahead.
            assert cli.json(*host.vault("push", destination="laptop"))["generation"] == 1
            err = cli.refused(
                *host.vault("push", destination="air"), code="REPLICATION.PUSH_RUNNING"
            )
            assert "a push of this library to air is already running" in err
        assert cli.json(*host.vault("push", destination="air"))["generation"] == 1
    with pytest.raises(PushRunningError):
        with VaultRecords(layout, "laptop").push_lock():
            with VaultRecords(layout, "laptop").push_lock():
                pass


@pytest.mark.parametrize("name", ["Laptop", "lap_top", "1air", "a" * 33])
def test_a_destination_flag_takes_a_short_stable_id(
    cli: Cli, host: Host, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["vault", "push", "--asset-root", str(host.root), "--destination", name])
    assert stopped.value.code == 2
    assert "a vault destination is 1-32 characters" in capsys.readouterr().err


def test_outside_a_unit_on_linux_the_secrets_are_refused_with_the_way_in(
    cli: Cli, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.json(*host.vault("init"), "--library-id", LIBRARY_ID)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    err = cli.refused(*host.vault("key", "show"), code="STORAGE.PROTOCOL")
    assert "systemd-run -p ImportCredential=" in err
    assert os.environ.get("CREDENTIALS_DIRECTORY") is None


def test_only_destination_directories_are_listed_as_destinations(host: Host) -> None:
    layout = AssetLayout.validate(host.root)
    assert recorded_destinations(layout) == ()
    VaultRecords(layout, "laptop").make_directory()
    destinations = host.root / "provenance/vault/destinations"
    (destinations / "Not_A_Destination").mkdir(mode=0o700)
    (destinations / "notes").write_bytes(b"not a directory\n")
    assert recorded_destinations(layout) == ("laptop",)

"""``voice-lab vault …``: the ceremony, the flows and the secrets that never reach the output.

Scenario tests for the ``vault`` command group in ``src/cole_voice_lab/cli.py``: naming the
library, the preview-then-approve digest ceremony, binding (and rebinding) a destination, the
whole local-directory flow (init, key, bind, push, verify, restore into a scratch root, audit)
and the tailnet flow against a vault server this process starts on the loopback. Every refusal
the composition root makes is checked here as the operator sees it, and one test greps every
command's output and every refusal for the library key, the recovery sheet and the tokens.
The Keychain is a dictionary, the tailnet is a recorded answer, and the loopback seam lives in
``tests/vault_binding_fixtures.py``: no command can reach it.
"""

from __future__ import annotations

import io
import json
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from vault_binding_fixtures import (
    ALL_CLASSES,
    APPROVAL_ID,
    COPIED_CLASSES,
    COPIED_LIBRARY_FILES,
    ENDPOINT,
    LIBRARY_ID,
    NAMESPACE,
    NODE_STABLE_ID,
    TAILNET_SUFFIX,
    MemoryKeychain,
    add_library_records,
    approval_arguments,
    approval_record,
    install_vault_seams,
    restore_permissions,
    running_vault,
    store_vault_token,
)

import cole_voice_lab.cli as cli_module
from cole_voice_lab.cli import build_parser, main
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.storage_binding import VAULT_TOKEN_ACCOUNTS, binding_path
from cole_voice_lab.storage_contracts import VAULT_NODE_TAG, keychain_item_names

NAMES = keychain_item_names(LIBRARY_ID)


@pytest.fixture(autouse=True)
def _no_ambient_asset_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)


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

    def refused(self, *arguments: str, code: str | None = None) -> str:
        """The refusal's stderr; with ``code``, it must lead with that code."""
        status, out, err = self(*arguments)
        assert status == 2, out
        if code is not None:
            assert err.startswith(f"voice-lab: {code}: "), err
        return err

    def failed(self, *arguments: str) -> Any:
        """A completed check that found problems: status 1 and its JSON report."""
        status, out, err = self(*arguments)
        assert status == 1, err
        return json.loads(out)

    def everything(self) -> str:
        return "\n".join(self.output)


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Cli:
    return Cli(capsys, [])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    add_library_records(layout)
    return layout.root


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    directory = tmp_path.resolve() / "vault"
    directory.mkdir(mode=0o700)
    return directory


def feed_stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Put ``text`` on standard input, the only way a secret ever reaches a vault command."""

    class Stdin:
        buffer = io.BytesIO(text.encode("utf-8"))

    monkeypatch.setattr(cli_module.sys, "stdin", Stdin())


def initialize(cli: Cli, root: Path, library_id: str = LIBRARY_ID) -> Any:
    return cli.json("vault", "init", "--asset-root", str(root), "--library-id", library_id)


def create_key(cli: Cli, root: Path, tmp_path: Path, name: str = "recovery-sheet.txt") -> Any:
    sheets = tmp_path.resolve() / "sheets"
    sheets.mkdir(mode=0o700, exist_ok=True)
    return cli.json(
        "vault",
        "key",
        "create",
        "--asset-root",
        str(root),
        "--sheet-out",
        str(sheets / name),
    )


def bind_local(cli: Cli, root: Path, endpoint: Path, *extra: str) -> Any:
    return cli.json(
        "vault",
        "bind",
        "--asset-root",
        str(root),
        "--backend",
        "local-directory",
        "--endpoint",
        str(endpoint),
        "--namespace",
        NAMESPACE,
        "--classes",
        *COPIED_CLASSES,
        *extra,
    )


def approve(cli: Cli, root: Path, approval: Any) -> str:
    """Run the preview, then the approval it printed the digest of; returns that digest."""
    fields = ["vault", "approve", "--asset-root", str(root), *approval_arguments(approval)]
    preview = cli.json(*fields, "--preview")
    digest = preview["approval_sha256"]
    written = cli.json(
        *fields, "--reviewed-approval-sha256", digest, "--i-approve-this-destination"
    )
    assert written["sha256"] == digest
    return str(digest)


# -- init ------------------------------------------------------------------------------------------


def test_a_library_is_named_once_and_a_second_init_is_refused(cli: Cli, root: Path) -> None:
    fresh = cli.json("vault", "init", "--asset-root", str(root))

    assert fresh["library_id"].startswith("lib-")
    assert fresh["adopted"] is False
    assert fresh["path"].endswith("provenance/vault/library.json")
    assert stat.S_IMODE(Path(fresh["path"]).stat().st_mode) == 0o600

    cli.refused(
        "vault",
        "init",
        "--asset-root",
        str(root),
        "--library-id",
        LIBRARY_ID,
        code="STORAGE.CONFLICT",
    )


def test_another_mac_adopts_the_library_it_is_about_to_restore(cli: Cli, root: Path) -> None:
    adopted = initialize(cli, root)

    assert (adopted["library_id"], adopted["adopted"]) == (LIBRARY_ID, True)
    assert cli.json("vault", "show", "--asset-root", str(root), "--json")["library_id"] == (
        LIBRARY_ID
    )


def test_a_vault_command_needs_an_asset_root(cli: Cli) -> None:
    assert "an explicit --asset-root" in cli.refused("vault", "show")


# -- approve ---------------------------------------------------------------------------------------


def test_the_preview_writes_nothing_and_the_approval_needs_its_exact_digest(
    cli: Cli, root: Path
) -> None:
    initialize(cli, root)
    approval = approval_record()
    fields = ["vault", "approve", "--asset-root", str(root), *approval_arguments(approval)]

    preview = cli.json(*fields, "--preview")

    assert preview["approved"] is False
    assert preview["approval"]["tag"] == VAULT_NODE_TAG
    assert preview["approval"]["library_id"] == LIBRARY_ID
    assert not (root / "provenance/vault/approvals").exists()

    assert "--i-approve-this-destination" in cli.refused(
        *fields,
        "--reviewed-approval-sha256",
        preview["approval_sha256"],
        code="VAULT_CLI.USAGE_INVALID",
    )
    assert "the exact --reviewed-approval-sha256" in cli.refused(
        *fields,
        "--reviewed-approval-sha256",
        "0" * 64,
        "--i-approve-this-destination",
        code="VAULT_CLI.USAGE_INVALID",
    )
    assert "--preview writes nothing" in cli.refused(
        *fields, "--preview", "--i-approve-this-destination", code="VAULT_CLI.USAGE_INVALID"
    )
    assert not (root / "provenance/vault/approvals").exists()

    written = cli.json(
        *fields,
        "--reviewed-approval-sha256",
        preview["approval_sha256"],
        "--i-approve-this-destination",
    )

    assert written["approved"] is True
    assert written["sha256"] == preview["approval_sha256"]
    assert written["approval"] == preview["approval"]
    path = Path(written["path"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    cli.refused(
        *fields,
        "--reviewed-approval-sha256",
        preview["approval_sha256"],
        "--i-approve-this-destination",
        code="STORAGE.CONFLICT",
    )


def test_an_approval_that_stands_too_long_is_refused_before_anything_is_written(
    cli: Cli, root: Path
) -> None:
    initialize(cli, root)
    approval = approval_record()
    fields = approval_arguments(approval)
    fields[fields.index("--expires-at") + 1] = (
        approval.approved_at + timedelta(days=400)
    ).isoformat()

    error = cli.refused("vault", "approve", "--asset-root", str(root), *fields, "--preview")

    assert "at most 366 days" in error
    assert not (root / "provenance/vault/approvals").exists()


def test_the_approval_parser_refuses_a_digest_that_is_not_one() -> None:
    with pytest.raises(SystemExit) as refused:
        build_parser().parse_args(
            ["vault", "approve", "--reviewed-approval-sha256", "not-a-digest"]
        )
    assert refused.value.code == 2


# -- bind ------------------------------------------------------------------------------------------


def test_binding_a_local_directory_creates_its_store_and_names_the_relaunch(
    cli: Cli, root: Path, elsewhere: Path
) -> None:
    initialize(cli, root)

    bound = bind_local(cli, root, elsewhere)

    assert bound["takes_effect"] == "at the next launch of the recording app"
    assert bound["replaced"] is False
    assert bound["binding"]["backend"] == "local-directory"
    assert bound["binding"]["key_item"] == NAMES.key
    assert bound["binding"]["token_item"] is None
    assert (elsewhere / "blobs").is_dir()
    assert stat.S_IMODE(binding_path(AssetLayout.validate(root)).stat().st_mode) == 0o600


def test_a_local_endpoint_inside_the_library_or_too_broad_is_refused(cli: Cli, root: Path) -> None:
    initialize(cli, root)

    assert "is the library" in cli.refused(
        "vault",
        "bind",
        "--asset-root",
        str(root),
        "--backend",
        "local-directory",
        "--endpoint",
        str(root / "tmp" / "vault"),
        "--namespace",
        NAMESPACE,
        "--classes",
        *COPIED_CLASSES,
        code="STORAGE.NOT_BOUND",
    )
    assert "is not specific enough" in cli.refused(
        "vault",
        "bind",
        "--asset-root",
        str(root),
        "--backend",
        "local-directory",
        "--endpoint",
        "/Volumes",
        "--namespace",
        NAMESPACE,
        "--classes",
        *COPIED_CLASSES,
        code="STORAGE.NOT_BOUND",
    )
    assert binding_path(AssetLayout.validate(root)).exists() is False


def test_a_tailnet_binding_names_an_approval_the_operator_reviewed(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_vault_seams(monkeypatch)
    initialize(cli, root)
    digest = approve(cli, root, approval_record())

    bound = cli.json(
        "vault",
        "bind",
        "--asset-root",
        str(root),
        "--backend",
        "tailnet-vault",
        "--endpoint",
        ENDPOINT,
        "--namespace",
        NAMESPACE,
        "--classes",
        *COPIED_CLASSES,
        "--approval-id",
        APPROVAL_ID,
        "--approval-sha256",
        digest,
    )

    assert bound["binding"]["approval_sha256"] == digest
    assert bound["binding"]["token_item"] == NAMES.token

    shown = cli.json("vault", "show", "--asset-root", str(root), "--json")
    assert shown["approval"]["effective"] is True
    assert shown["approval"]["matches_binding"] is True
    assert shown["approval"]["node_stable_id"] == NODE_STABLE_ID
    assert shown["secrets"] == {"key_fingerprint": None, "write_token": False, "read_token": False}


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"--classes": list(ALL_CLASSES)}, "does not cover training"),
        ({"--namespace": ["spare"]}, "approves namespace cole, not spare"),
        ({"--endpoint": [f"other.{TAILNET_SUFFIX}"]}, f"approves {ENDPOINT}"),
        ({"--approval-sha256": ["0" * 64]}, "the approval changed"),
        ({"--approval-id": ["vault-missing"]}, "does not exist"),
    ],
)
def test_a_tailnet_binding_the_approval_does_not_cover_is_refused(
    cli: Cli, root: Path, changes: dict[str, list[str]], problem: str
) -> None:
    initialize(cli, root)
    digest = approve(cli, root, approval_record())
    fields: dict[str, list[str]] = {
        "--backend": ["tailnet-vault"],
        "--endpoint": [ENDPOINT],
        "--namespace": [NAMESPACE],
        "--classes": list(COPIED_CLASSES),
        "--approval-id": [APPROVAL_ID],
        "--approval-sha256": [digest],
    } | changes
    arguments = ["vault", "bind", "--asset-root", str(root)]
    for flag, values in fields.items():
        arguments += [flag, *values]

    assert problem in cli.refused(*arguments, code="STORAGE.NOT_APPROVED")
    assert not binding_path(AssetLayout.validate(root)).exists()


def test_a_binding_without_an_approval_never_reaches_the_tailnet(cli: Cli, root: Path) -> None:
    initialize(cli, root)

    assert "needs --approval-id" in cli.refused(
        "vault",
        "bind",
        "--asset-root",
        str(root),
        "--backend",
        "tailnet-vault",
        "--endpoint",
        ENDPOINT,
        "--namespace",
        NAMESPACE,
        "--classes",
        *COPIED_CLASSES,
        code="STORAGE.NOT_APPROVED",
    )


def test_rebinding_needs_replace_and_moving_a_copied_library_needs_a_reset(
    cli: Cli, root: Path, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    create_key(cli, root, tmp_path)
    bind_local(cli, root, elsewhere)
    assert cli.json("vault", "push", "--asset-root", str(root))["generation"] == 1
    second = tmp_path.resolve() / "second-vault"
    second.mkdir(mode=0o700)

    assert "pass --replace" in cli.refused(
        *[
            "vault",
            "bind",
            "--asset-root",
            str(root),
            "--backend",
            "local-directory",
            "--endpoint",
            str(second),
            "--namespace",
            NAMESPACE,
            "--classes",
            *COPIED_CLASSES,
        ],
        code="STORAGE.CONFLICT",
    )
    assert "--reset-state" in cli.refused(
        *[
            "vault",
            "bind",
            "--asset-root",
            str(root),
            "--backend",
            "local-directory",
            "--endpoint",
            str(second),
            "--namespace",
            NAMESPACE,
            "--classes",
            *COPIED_CLASSES,
            "--replace",
        ],
        code="STORAGE.CONFLICT",
    )

    moved = bind_local(cli, root, second, "--replace", "--reset-state")

    assert moved["replaced"] is True
    assert moved["superseded_state"].endswith("state-superseded-1.json")
    assert moved["superseded_manifests"].endswith("manifests-superseded-1")
    assert Path(moved["superseded_manifests"], "00000001.json").exists()
    assert not (root / "provenance/vault/manifests").exists()
    assert seams.connections == []


# -- the local-directory flow ----------------------------------------------------------------------


def test_a_library_is_copied_verified_restored_and_audited_through_the_commands(
    cli: Cli, root: Path, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    copied = COPIED_LIBRARY_FILES

    assert cli.json("vault", "status", "--asset-root", str(root), "--json")["state"] == "off"
    assert cli("vault", "status", "--asset-root", str(root))[1] == "Vault · off\n"
    initialize(cli, root)
    key = create_key(cli, root, tmp_path)
    bind_local(cli, root, elsewhere)

    waiting = cli.json("vault", "status", "--asset-root", str(root), "--json")
    assert (waiting["state"], waiting["generation"], waiting["waiting_files"]) == (
        "waiting",
        0,
        None,
    )

    planned = cli.json("vault", "push", "--asset-root", str(root), "--dry-run")
    assert planned["outcome"] == "dry-run" and planned["generation"] == 0
    assert planned["uploaded_files"] == len(copied)
    assert not (root / "provenance/vault/ledger").exists()

    pushed = cli.json("vault", "push", "--asset-root", str(root))
    assert (pushed["outcome"], pushed["generation"], pushed["head_version"]) == ("pushed", 1, 1)
    assert pushed["uploaded_files"] == len(copied) and pushed["waiting_paths"] == []

    up_to_date = cli.json("vault", "status", "--asset-root", str(root), "--json")
    assert up_to_date["state"] == "up-to-date" and up_to_date["generation"] == 1
    assert cli("vault", "status", "--asset-root", str(root))[1] == "Vault · up to date\n"

    report = cli.json("vault", "verify", "--asset-root", str(root))
    assert report["ok"] is True and report["missing"] == 0
    assert report["checked"] == len(copied) and report["generation"] == 1
    assert report["manifest_sha256"] == pushed["manifest_sha256"]

    narrow = cli.json(
        "vault", "verify", "--asset-root", str(root), "--class", "records", "--path", "provenance"
    )
    assert narrow["checked"] < report["checked"] and narrow["ok"] is True

    target = tmp_path.resolve() / "restored" / "assets"
    target.parent.mkdir(mode=0o700)
    restored = cli.json(
        "vault", "restore", "--asset-root", str(root), "--target", str(target), "--accept-latest"
    )
    assert restored["generation"] == 1 and restored["restored_files"] == len(copied)
    assert restored["library_id"] == LIBRARY_ID
    for relative, data in copied.items():
        assert (target / relative).read_bytes() == data

    log = tmp_path / "access.jsonl"
    log.write_bytes(b"")
    audit = cli.json("vault", "audit", "--asset-root", str(root), "--access-log", str(log))
    assert audit == {
        "namespace": NAMESPACE,
        "server_accesses": 0,
        "mac_requests": 0,
        "matched": 0,
        "unanswered": 0,
        "refused_at_server": 0,
        "unexplained": [],
        "ok": True,
    }
    assert (
        key["key_fingerprint"]
        == cli.json("vault", "key", "show", "--asset-root", str(root))["key_fingerprint"]
    )
    assert seams.connections == []


def test_a_named_generation_restores_only_when_its_digest_matches(
    cli: Cli, root: Path, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_vault_seams(monkeypatch)
    initialize(cli, root)
    create_key(cli, root, tmp_path)
    bind_local(cli, root, elsewhere)
    pushed = cli.json("vault", "push", "--asset-root", str(root))
    target = tmp_path.resolve() / "restored" / "assets"
    target.parent.mkdir(mode=0o700)

    assert "both --generation and --manifest-sha256" in cli.refused(
        "vault",
        "restore",
        "--asset-root",
        str(root),
        "--target",
        str(target),
        "--generation",
        "1",
        code="VAULT_CLI.USAGE_INVALID",
    )
    assert "digest-mismatch" in cli.refused(
        "vault",
        "restore",
        "--asset-root",
        str(root),
        "--target",
        str(target),
        "--generation",
        "1",
        "--manifest-sha256",
        "0" * 64,
        code="STORAGE.INTEGRITY",
    )

    restored = cli.json(
        "vault",
        "restore",
        "--asset-root",
        str(root),
        "--target",
        str(target),
        "--generation",
        "1",
        "--manifest-sha256",
        pushed["manifest_sha256"],
    )
    assert restored["manifest_sha256"] == pushed["manifest_sha256"]
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["vault", "restore", "--target", "/x", "--accept-latest", "--generation", "2"]
        )


def test_a_full_push_re_uploads_everything_after_a_blob_goes_missing(
    cli: Cli, root: Path, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_vault_seams(monkeypatch)
    initialize(cli, root)
    create_key(cli, root, tmp_path)
    bind_local(cli, root, elsewhere)
    cli.json("vault", "push", "--asset-root", str(root))
    # One chunk of one copied file, not the manifest's own blobs: the head still resolves.
    manifest = json.loads((root / "provenance/vault/manifests/00000001.json").read_bytes())
    chunk = manifest["entries"][0]["chunks"][0]["sha256"]
    (elsewhere / "blobs" / chunk[:2] / chunk).unlink()

    broken = cli.failed("vault", "verify", "--asset-root", str(root))
    assert broken["ok"] is False
    assert broken["missing"] + broken["mismatch"] >= 1
    assert broken["problems"][0]["state"] in ("missing", "mismatch")

    repaired = cli.json("vault", "push", "--asset-root", str(root), "--full")

    assert repaired["outcome"] == "pushed" and repaired["generation"] == 2
    assert repaired["reused_files"] == 0
    assert cli.json("vault", "verify", "--asset-root", str(root))["ok"] is True


def test_a_library_that_is_not_bound_refuses_to_copy_anything(cli: Cli, root: Path) -> None:
    initialize(cli, root)

    for command in ("push", "verify"):
        assert "not bound to a vault" in cli.refused(
            "vault", command, "--asset-root", str(root), code="STORAGE.NOT_BOUND"
        )
    assert "not bound to a vault" in cli.refused(
        "vault",
        "restore",
        "--asset-root",
        str(root),
        "--target",
        str(root / "x"),
        "--accept-latest",
        code="STORAGE.NOT_BOUND",
    )
    assert "bind the vault first" in cli.refused(
        "vault",
        "audit",
        "--asset-root",
        str(root),
        "--access-log",
        str(root / "log"),
        code="VAULT_CLI.USAGE_INVALID",
    )
    assert "bind the vault first" in cli.refused(
        "vault", "token", "create", "--asset-root", str(root), code="VAULT_CLI.USAGE_INVALID"
    )


def test_a_vault_that_cannot_be_reached_reads_as_unreachable_not_broken(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    create_key(cli, root, tmp_path)
    digest = approve(cli, root, approval_record())
    store_vault_token(seams.keychain, "write")
    cli.json(
        "vault",
        "bind",
        "--asset-root",
        str(root),
        "--backend",
        "tailnet-vault",
        "--endpoint",
        ENDPOINT,
        "--namespace",
        NAMESPACE,
        "--classes",
        *COPIED_CLASSES,
        "--approval-id",
        APPROVAL_ID,
        "--approval-sha256",
        digest,
    )

    # No loopback server is running, so the connection factory reports the vault as away.
    status = cli.json("vault", "status", "--asset-root", str(root), "--json")

    assert status["state"] == "unreachable" and status["code"] == "STORAGE.UNAVAILABLE"
    assert status["line"] == "Vault unreachable"
    assert cli("vault", "status", "--asset-root", str(root))[1] == "Vault unreachable\n"

    # A vault whose key is gone is a failure, not an outage: it never becomes reachable again.
    seams.keychain.items.pop((NAMES.key, "library-key"))
    failed = cli.json("vault", "status", "--asset-root", str(root), "--json")
    assert failed["state"] == "failed" and failed["code"] == "STORAGE.NOT_BOUND"
    assert failed["line"].startswith("Vault · the login Keychain has no library key item")


# -- the tailnet flow ------------------------------------------------------------------------------


def test_a_library_is_copied_over_the_tailnet_and_the_two_logs_agree(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_vault(tmp_path) as server:
        seams = install_vault_seams(monkeypatch, port=server.port)
        initialize(cli, root)
        create_key(cli, root, tmp_path)
        digest = approve(cli, root, approval_record())
        store_vault_token(seams.keychain, "write", token=server.write_token)
        cli.json(
            "vault",
            "bind",
            "--asset-root",
            str(root),
            "--backend",
            "tailnet-vault",
            "--endpoint",
            ENDPOINT,
            "--namespace",
            NAMESPACE,
            "--classes",
            *COPIED_CLASSES,
            "--approval-id",
            APPROVAL_ID,
            "--approval-sha256",
            digest,
        )

        pushed = cli.json("vault", "push", "--asset-root", str(root))
        assert pushed["outcome"] == "pushed" and pushed["generation"] == 1
        assert cli.json("vault", "verify", "--asset-root", str(root))["ok"] is True
        assert cli.json("vault", "status", "--asset-root", str(root), "--json")["state"] == (
            "up-to-date"
        )

        audit = cli.json(
            "vault", "audit", "--asset-root", str(root), "--access-log", str(server.access_log)
        )

        assert audit["ok"] is True and audit["unexplained"] == []
        assert audit["matched"] == audit["server_accesses"] > 0
        assert audit["mac_requests"] >= audit["matched"]
        (request,) = {connection.endpoint for connection in seams.connections}
        assert request == ENDPOINT
        assert all(
            connection.identity.tailnet_suffix == TAILNET_SUFFIX for connection in seams.connections
        )


def test_an_audit_fails_on_a_read_the_mac_never_asked_for(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from http.client import HTTPConnection

    with running_vault(tmp_path) as server:
        seams = install_vault_seams(monkeypatch, port=server.port)
        initialize(cli, root)
        create_key(cli, root, tmp_path)
        digest = approve(cli, root, approval_record())
        store_vault_token(seams.keychain, "write", token=server.write_token)
        cli.json(
            "vault",
            "bind",
            "--asset-root",
            str(root),
            "--backend",
            "tailnet-vault",
            "--endpoint",
            ENDPOINT,
            "--namespace",
            NAMESPACE,
            "--classes",
            *COPIED_CLASSES,
            "--approval-id",
            APPROVAL_ID,
            "--approval-sha256",
            digest,
        )
        cli.json("vault", "push", "--asset-root", str(root))

        # Someone else, holding the read token, fetches a blob the Mac never asked for.
        connection = HTTPConnection("127.0.0.1", server.port, timeout=5.0)
        head = json.loads((root / "provenance/vault/state.json").read_bytes())["head"]["target"]
        connection.request(
            "GET",
            f"/v1/{NAMESPACE}/blobs/{head['sha256']}?size={head['size']}",
            headers={"Authorization": f"Bearer {server.read_token}"},
        )
        assert connection.getresponse().status == 200
        connection.close()

        audit = cli.failed(
            "vault", "audit", "--asset-root", str(root), "--access-log", str(server.access_log)
        )

        assert audit["ok"] is False
        (unexplained,) = audit["unexplained"]
        assert (unexplained["operation"], unexplained["status"]) == ("get-blob", 200)
        assert unexplained["scope"] == "read"


def test_a_show_before_anything_is_bound_says_so_in_both_shapes(cli: Cli, root: Path) -> None:
    document = cli.json("vault", "show", "--asset-root", str(root), "--json")

    assert document == {
        "library_id": None,
        "binding": None,
        "approval": None,
        "secrets": {"key_fingerprint": None, "write_token": False, "read_token": False},
        "state": None,
    }
    _, text, _ = cli("vault", "show", "--asset-root", str(root))
    assert text.splitlines() == [
        "library: not initialized",
        "binding: none (Vault · off)",
        "approval: none",
        'secrets: {"key_fingerprint": null, "read_token": false, "write_token": false}',
        "state: none",
    ]


def test_a_show_reports_the_problems_it_finds_instead_of_refusing(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.storage_ports import storage_error

    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    digest = approve(cli, root, approval_record())
    cli.json(
        "vault",
        "bind",
        "--asset-root",
        str(root),
        "--backend",
        "tailnet-vault",
        "--endpoint",
        ENDPOINT,
        "--namespace",
        NAMESPACE,
        "--classes",
        *COPIED_CLASSES,
        "--approval-id",
        APPROVAL_ID,
        "--approval-sha256",
        digest,
    )
    approval = root / "provenance/vault/approvals" / f"{APPROVAL_ID}.json"
    approval.chmod(0o600)
    approval.unlink()

    document = cli.json("vault", "show", "--asset-root", str(root), "--json")

    assert document["approval"]["code"] == "STORAGE.NOT_APPROVED"
    assert "does not exist" in document["approval"]["problem"]
    assert document["approval"]["approval_id"] == APPROVAL_ID

    seams.keychain.failure = storage_error("STORAGE.PROTOCOL", "the Keychain tool failed")
    broken = cli.json("vault", "show", "--asset-root", str(root), "--json")
    assert broken["secrets"] == {
        "code": "STORAGE.PROTOCOL",
        "problem": "the Keychain tool failed",
    }
    _, text, _ = cli("vault", "show", "--asset-root", str(root))
    assert text.startswith(f"library: {LIBRARY_ID}\nbinding: tailnet-vault {ENDPOINT}\n")


def test_a_secret_that_is_not_text_is_refused_before_it_is_used(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_vault_seams(monkeypatch)
    initialize(cli, root)

    class Stdin:
        buffer = io.BytesIO(b"\xff\xfe not text\n")

    monkeypatch.setattr(cli_module.sys, "stdin", Stdin())
    assert "must arrive as UTF-8 text" in cli.refused(
        "vault", "token", "import", "--asset-root", str(root), "--scope", "write"
    )

    sheets = tmp_path.resolve() / "sheets"
    sheets.mkdir(mode=0o700)
    binary = sheets / "binary-sheet.txt"
    binary.write_bytes(b"\xff\xfe not text\n")
    assert "must be UTF-8 text" in cli.refused(
        "vault", "key", "import", "--asset-root", str(root), "--sheet", str(binary)
    )
    huge = sheets / "huge-sheet.txt"
    huge.write_bytes(b"x" * (64 * 1024 + 1))
    assert "exceeds" in cli.refused(
        "vault", "key", "import", "--asset-root", str(root), "--sheet", str(huge)
    )


def test_another_mac_restores_the_library_with_only_the_id_and_the_key(
    cli: Cli, root: Path, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    created = create_key(cli, root, tmp_path)
    bind_local(cli, root, elsewhere)
    cli.json("vault", "push", "--asset-root", str(root))

    fresh = AssetLayout.initialize(tmp_path.resolve() / "fresh").root
    install_vault_seams(monkeypatch, keychain=MemoryKeychain())
    initialize(cli, fresh)
    cli.json(
        "vault",
        "key",
        "import",
        "--asset-root",
        str(fresh),
        "--sheet",
        created["recovery_sheet"],
    )
    target = tmp_path.resolve() / "restored" / "assets"
    target.parent.mkdir(mode=0o700)

    restored = cli.json(
        "vault",
        "restore",
        "--asset-root",
        str(fresh),
        "--target",
        str(target),
        "--accept-latest",
        "--backend",
        "local-directory",
        "--endpoint",
        str(elsewhere),
        "--namespace",
        NAMESPACE,
    )

    assert restored["library_id"] == LIBRARY_ID and restored["generation"] == 1
    for relative, data in COPIED_LIBRARY_FILES.items():
        assert (target / relative).read_bytes() == data
    assert seams.keychain.items != {}


def test_restoring_from_a_named_endpoint_is_refused_without_its_namespace(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new-Mac path: a forgotten flag is a named refusal, never a Python traceback."""
    install_vault_seams(monkeypatch)
    initialize(cli, root)
    target = tmp_path.resolve() / "restored" / "assets"
    target.parent.mkdir(mode=0o700)

    assert "needs --namespace" in cli.refused(
        "vault",
        "restore",
        "--asset-root",
        str(root),
        "--target",
        str(target),
        "--accept-latest",
        "--endpoint",
        ENDPOINT,
        "--node-stable-id",
        NODE_STABLE_ID,
        "--tailnet-suffix",
        TAILNET_SUFFIX,
        code="VAULT_CLI.USAGE_INVALID",
    )
    assert not target.exists()


# -- secrets ---------------------------------------------------------------------------------------


def test_creating_tokens_prints_only_their_hashes_and_the_namespace(
    cli: Cli, root: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    bind_local(cli, root, elsewhere)

    created = cli.json("vault", "token", "create", "--asset-root", str(root))

    assert created["namespace"] == NAMESPACE and created["library_id"] == LIBRARY_ID
    write = seams.keychain.items[(NAMES.token, VAULT_TOKEN_ACCOUNTS["write"])]
    read = seams.keychain.items[(NAMES.read_token, VAULT_TOKEN_ACCOUNTS["read"])]
    assert created["write_token_sha256"] == hashlib.sha256(write.encode()).hexdigest()
    assert created["read_token_sha256"] == hashlib.sha256(read.encode()).hexdigest()
    assert re.search(r"cvlv_[A-Za-z0-9_-]{43}", cli.everything()) is None
    cli.refused("vault", "token", "create", "--asset-root", str(root), code="STORAGE.CONFLICT")

    shown = cli.json("vault", "show", "--asset-root", str(root), "--json")
    assert shown["secrets"]["write_token"] is True and shown["secrets"]["read_token"] is True
    assert re.search(r"cvlv_[A-Za-z0-9_-]{43}", cli.everything()) is None


def test_a_token_arrives_on_standard_input_and_never_in_an_argument(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.storage_server import new_token

    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    token = new_token()

    feed_stdin(monkeypatch, f"{token}\n")
    imported = cli.json(
        "vault",
        "token",
        "import",
        "--asset-root",
        str(root),
        "--scope",
        "read",
    )

    assert imported["scope"] == "read"
    assert seams.keychain.items[(NAMES.read_token, VAULT_TOKEN_ACCOUNTS["read"])] == token
    assert token not in cli.everything()

    feed_stdin(monkeypatch, "")
    assert "nothing arrived" in cli.refused(
        "vault", "token", "import", "--asset-root", str(root), "--scope", "write"
    )
    feed_stdin(monkeypatch, "hunter2\n")
    error = cli.refused(
        "vault",
        "token",
        "import",
        "--asset-root",
        str(root),
        "--scope",
        "write",
        code="STORAGE.PROTOCOL",
    )
    assert "hunter2" not in error
    feed_stdin(monkeypatch, "a" * (64 * 1024 + 1))
    assert "exceeds" in cli.refused(
        "vault", "token", "import", "--asset-root", str(root), "--scope", "write"
    )


def test_a_key_is_written_to_a_new_private_file_outside_the_library(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    sheets = tmp_path.resolve() / "sheets"
    sheets.mkdir(mode=0o700)
    sheet = sheets / "recovery-sheet.txt"

    created = cli.json(
        "vault", "key", "create", "--asset-root", str(root), "--sheet-out", str(sheet)
    )

    assert created["recovery_sheet"] == str(sheet)
    assert stat.S_IMODE(sheet.stat().st_mode) == 0o600
    secret = seams.keychain.items[(NAMES.key, "library-key")]
    assert created["key_fingerprint"] and secret not in cli.everything()
    assert sheet.read_text().count("\n") > 5
    cli.refused(
        "vault",
        "key",
        "create",
        "--asset-root",
        str(root),
        "--sheet-out",
        str(sheets / "second.txt"),
        code="STORAGE.CONFLICT",
    )
    # The key itself is only on the sheet and in the Keychain; the summary shows a fingerprint.
    assert cli.json("vault", "key", "show", "--asset-root", str(root)) == {
        "library_id": LIBRARY_ID,
        "key_fingerprint": created["key_fingerprint"],
    }
    assert secret not in cli.everything()


def test_a_key_output_is_required_and_a_sheet_never_lands_where_it_must_not(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)

    assert "exactly one of --sheet-out" in cli.refused(
        "vault", "key", "create", "--asset-root", str(root), code="VAULT_CLI.USAGE_INVALID"
    )
    assert "exactly one of --sheet-out" in cli.refused(
        "vault",
        "key",
        "create",
        "--asset-root",
        str(root),
        "--sheet-out",
        str(tmp_path / "s.txt"),
        "--print-to-terminal",
        code="VAULT_CLI.USAGE_INVALID",
    )
    assert "never written inside the library" in cli.refused(
        "vault",
        "key",
        "create",
        "--asset-root",
        str(root),
        "--sheet-out",
        str(root / "sheet.txt"),
        code="STORAGE.NOT_APPROVED",
    )
    existing = tmp_path.resolve() / "sheets"
    existing.mkdir(mode=0o700)
    (existing / "taken.txt").write_bytes(b"")
    assert "refusing to overwrite" in cli.refused(
        "vault",
        "key",
        "create",
        "--asset-root",
        str(root),
        "--sheet-out",
        str(existing / "taken.txt"),
    )
    assert seams.keychain.items == {}


def test_a_sheet_the_filesystem_refuses_says_the_key_is_already_in_the_keychain(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full or read-only stick fails after the key exists: the refusal must say what to do."""
    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    stick = tmp_path.resolve() / "stick"
    stick.mkdir(mode=0o700)
    sheet = stick / "recovery-sheet.txt"
    stick.chmod(0o500)

    refusal = cli.refused(
        "vault", "key", "create", "--asset-root", str(root), "--sheet-out", str(sheet)
    )

    assert not sheet.exists()
    # The key is in the Keychain now, so the message names it, the item and the way back.
    assert seams.keychain.items != {}
    assert f"item {NAMES.key}" in refusal and "nothing has been copied to a vault yet" in refusal
    assert f"security delete-generic-password -s {NAMES.key}" in refusal
    assert "vault key create` again" in refusal
    secret = seams.keychain.items[(NAMES.key, "library-key")]
    assert secret not in cli.everything()
    # And the refusal is honest about the way back being the only one: a retry is refused.
    stick.chmod(0o700)
    cli.refused(
        "vault",
        "key",
        "create",
        "--asset-root",
        str(root),
        "--sheet-out",
        str(sheet),
        code="STORAGE.CONFLICT",
    )


def test_printing_a_recovery_sheet_needs_a_terminal_nothing_is_capturing(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    monkeypatch.setattr(cli_module, "_stdout_is_a_terminal", lambda: False)

    refusal = cli.refused(
        "vault",
        "key",
        "create",
        "--asset-root",
        str(root),
        "--print-to-terminal",
        code="VAULT_CLI.NOT_A_TERMINAL",
    )

    assert "use --sheet-out PATH instead" in refusal
    assert seams.keychain.items == {}

    monkeypatch.setattr(cli_module, "_stdout_is_a_terminal", lambda: True)
    status, out, err = cli(
        "vault", "key", "create", "--asset-root", str(root), "--print-to-terminal"
    )

    assert status == 0 and err == ""
    secret = seams.keychain.items[(NAMES.key, "library-key")]
    # The sheet is the whole output: no JSON document carries it alongside.
    assert out.startswith("cole-voice-lab") or LIBRARY_ID in out
    assert re.sub(r"\s", "", out).count(secret[:8]) >= 1


def test_a_key_comes_back_from_its_sheet_on_another_mac(
    cli: Cli, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    created = create_key(cli, root, tmp_path)
    sheet = Path(created["recovery_sheet"])
    other = AssetLayout.initialize(tmp_path.resolve() / "other").root
    install_vault_seams(monkeypatch)
    initialize(cli, other)

    imported = cli.json("vault", "key", "import", "--asset-root", str(other), "--sheet", str(sheet))

    assert imported["key_fingerprint"] == created["key_fingerprint"]
    assert sheet.read_text() not in cli.everything()

    third = AssetLayout.initialize(tmp_path.resolve() / "third").root
    install_vault_seams(monkeypatch)
    initialize(cli, third)
    feed_stdin(monkeypatch, sheet.read_text())
    from_stdin = cli.json("vault", "key", "import", "--asset-root", str(third), "--sheet", "-")
    assert from_stdin["key_fingerprint"] == created["key_fingerprint"]
    assert seams.keychain.items != {}


def test_no_command_and_no_refusal_ever_repeats_a_secret(
    cli: Cli, root: Path, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every vault command is run, and every byte it printed is searched for the secrets."""
    from cole_voice_lab.storage_server import new_token

    seams = install_vault_seams(monkeypatch)
    initialize(cli, root)
    created = create_key(cli, root, tmp_path)
    bind_local(cli, root, elsewhere)
    cli.json("vault", "token", "create", "--asset-root", str(root))
    given = new_token()
    feed_stdin(monkeypatch, f"{given}\n")
    cli.refused("vault", "token", "import", "--asset-root", str(root), "--scope", "read")
    cli.json("vault", "push", "--asset-root", str(root))
    cli.json("vault", "verify", "--asset-root", str(root))
    cli.json("vault", "show", "--asset-root", str(root), "--json")
    cli("vault", "show", "--asset-root", str(root))
    cli.json("vault", "status", "--asset-root", str(root), "--json")
    cli.json("vault", "key", "show", "--asset-root", str(root))
    log = tmp_path / "access.jsonl"
    log.write_bytes(b"")
    cli.json("vault", "audit", "--asset-root", str(root), "--access-log", str(log))
    target = tmp_path.resolve() / "restored" / "assets"
    target.parent.mkdir(mode=0o700)
    cli.json(
        "vault", "restore", "--asset-root", str(root), "--target", str(target), "--accept-latest"
    )
    # Refusals too: their text is printed by ``cli.main`` just as an answer is.
    cli.refused("vault", "key", "create", "--asset-root", str(root), "--print-to-terminal")
    cli.refused("vault", "init", "--asset-root", str(root))
    feed_stdin(monkeypatch, "not-a-token\n")
    cli.refused("vault", "token", "import", "--asset-root", str(root), "--scope", "write")

    printed = cli.everything()
    sheet = Path(created["recovery_sheet"]).read_text()

    assert re.search(r"cvlv_[A-Za-z0-9_-]{43}", printed) is None
    assert given not in printed
    assert len(seams.keychain.secrets()) == 3
    for secret in seams.keychain.secrets():
        assert secret not in printed
        assert secret not in printed.replace(" ", "").replace("\n", "")
    for line in sheet.splitlines():
        stripped = line.strip()
        if len(stripped) > 16 and any(character.isdigit() for character in stripped):
            assert stripped not in printed
    assert created["key_fingerprint"] in printed

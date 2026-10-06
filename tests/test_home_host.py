"""The standing home-host approval (D-021), its ledger, the publisher and their CLI."""

from __future__ import annotations

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from home_host_fixtures import ENDPOINT, NODE, NOW, TAILNET, bind_approval, make_approval

from cole_voice_lab import cli_home_host, intake_publisher
from cole_voice_lab.cli import main
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.home_host import (
    HomeHostLedger,
    HomeHostSignIn,
    approve,
    load_approval,
    load_sign_in,
    record_sha256,
    save_sign_in,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.synthetic_capture import build_synthetic_session


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "air" / "voice-lab" / "lab")


def test_an_approval_binds_one_node_for_at_most_a_year() -> None:
    approval = make_approval()
    assert approval.identity().node_stable_id == NODE
    assert approval.effective_at(NOW) and not approval.effective_at(NOW + timedelta(days=301))
    for changes, problem in [
        ({"endpoint": "voice-lab.other.ts.net"}, "approved tailnet"),
        ({"endpoint": "Voice-Lab." + TAILNET}, "lowercase"),
        ({"expires_at": NOW - timedelta(days=2)}, "follow"),
        ({"expires_at": NOW + timedelta(days=400)}, "at most 366"),
        ({"classes": ("measurement", "capture")}, "sorted"),
        ({"tag": "tag:container"}, "tag"),
    ]:
        with pytest.raises(ValueError, match=problem):
            make_approval(**changes)


def test_approve_needs_the_reviewed_digest_and_never_replaces(layout: AssetLayout) -> None:
    approval = make_approval()
    with pytest.raises(SecurityError, match="reviewed digest"):
        approve(layout, approval, reviewed_sha256="0" * 64, now=NOW)
    with pytest.raises(SecurityError, match="does not stand"):
        approve(
            layout,
            approval,
            reviewed_sha256=record_sha256(approval),
            now=NOW + timedelta(days=400),
        )
    with pytest.raises(ContractError, match="no such home-host record"):
        load_approval(layout, now=NOW)
    bind_approval(layout, approval)
    loaded, digest = load_approval(layout, now=NOW)
    assert loaded == approval and digest == record_sha256(approval)
    path = layout.directory("provenance") / "home-host" / "approvals" / "pc-home-host.json"
    assert path.stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        bind_approval(layout, approval)


def test_a_changed_approval_file_is_not_the_bound_one(layout: AssetLayout) -> None:
    bind_approval(layout)
    path = layout.directory("provenance") / "home-host" / "approvals" / "pc-home-host.json"
    os.chmod(path, 0o600)
    document = json.loads(path.read_text())
    document["node_stable_id"] = "nOTHER1CNTRL"
    path.write_text(json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n")
    with pytest.raises(SecurityError, match="not the one that was bound"):
        load_approval(layout, now=NOW)
    path.write_text(json.dumps(document, indent=2))
    with pytest.raises(SecurityError, match="canonical"):
        load_approval(layout, now=NOW)
    path.write_text("{}")
    with pytest.raises(ContractError, match="invalid home-host record"):
        load_approval(layout, now=NOW)


def _entry(layout: AssetLayout, event: str = "upload-started", **extra: object) -> None:
    HomeHostLedger(layout).append(
        event=event,
        at=NOW,
        approval_id="pc-home-host",
        approval_sha256="a" * 64,
        endpoint=ENDPOINT,
        node_stable_id=NODE,
        session_id="s-1",
        manifest_sha256="b" * 64,
        **extra,
    )


def test_the_ledger_is_chained_and_checked(layout: AssetLayout) -> None:
    _entry(layout)
    _entry(layout, "upload-completed", receipt_sha256="c" * 64)
    entries = HomeHostLedger(layout).entries()
    assert [entry.sequence for entry, _ in entries] == [1, 2]
    assert entries[1][0].previous_sha256 == entries[0][1]
    with pytest.raises(ValueError, match="names its receipt"):
        _entry(layout, "upload-completed")
    directory = layout.directory("provenance") / "home-host" / "ledger"
    (directory / "00000001.json").rename(directory / "00000003.json")
    with pytest.raises(SecurityError, match="gap"):
        HomeHostLedger(layout).entries()
    (directory / "00000003.json").rename(directory / "00000001.json")
    first = directory / "00000001.json"
    os.chmod(first, 0o600)
    document = json.loads(first.read_text())
    document["session_id"] = "s-2"
    first.write_text(json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n")
    with pytest.raises(SecurityError, match="chain is broken"):
        HomeHostLedger(layout).entries()


def test_sign_in_settings_round_trip(layout: AssetLayout) -> None:
    settings = HomeHostSignIn(issuer="https://id.example.invalid", client_id="voice-lab-desktop")
    save_sign_in(layout, settings)
    save_sign_in(layout, settings)
    assert load_sign_in(layout) == settings
    assert settings.keychain_account() == "voice-lab-desktop@id.example.invalid"
    with pytest.raises(ValueError):
        HomeHostSignIn(issuer="http://id.example.invalid", client_id="x")


def test_the_approve_and_show_commands(
    layout: AssetLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    approved_at = NOW.isoformat()
    base = [
        "home-host",
        "approve",
        "--asset-root",
        str(layout.root),
        "--approval-id",
        "pc-home-host",
        "--endpoint",
        ENDPOINT,
        "--tailnet-suffix",
        TAILNET,
        "--node-stable-id",
        NODE,
        "--classes",
        "capture",
        "--approved-at",
        approved_at,
        "--expires-at",
        (NOW + timedelta(days=365)).isoformat(),
        "--approved-by",
        "Cole",
        "--statement",
        "My captures to my PC.",
    ]
    assert main([*base, "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert main([*base, "--preview", "--i-approve-this-destination"]) == 2
    assert main(base) == 2
    capsys.readouterr()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli_home_host, "utc_now", lambda: NOW)
        assert (
            main(
                [
                    *base,
                    "--reviewed-approval-sha256",
                    preview["approval_sha256"],
                    "--i-approve-this-destination",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["approved"] is True
        assert main(["home-host", "show", "--asset-root", str(layout.root)]) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["approval_sha256"] == preview["approval_sha256"]
        assert shown["ledger_entries"] == 0


def test_the_publisher_defers_operating_failures_and_respects_the_disk_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path / "srv" / "voice-lab" / "lab")
    with pytest.raises(SecurityError, match="disk floor"):
        intake_publisher.publish_pending(layout, min_free_bytes=1 << 62)
    session = build_synthetic_session(
        tmp_path / "synthetic-001", session_id="synthetic-001", seconds=0.2
    )
    shutil.copytree(session, layout.directory("incoming") / "synthetic-001")
    (layout.directory("incoming") / ".uploads").mkdir()

    def broken(*_args: object, **_options: object) -> object:
        raise OSError("the disk hiccuped")

    monkeypatch.setattr(intake_publisher, "accept_capture_session", broken)
    summary = intake_publisher.publish_pending(layout, min_free_bytes=0)
    assert summary.deferred == [{"session": "synthetic-001", "error": "the disk hiccuped"}]
    monkeypatch.undo()
    real = intake_publisher.publish_pending
    monkeypatch.setattr(
        cli_home_host, "publish_pending", lambda chosen: real(chosen, min_free_bytes=0)
    )
    assert main(["intake", "publish", "--asset-root", str(layout.root)]) == 0
    published = json.loads(capsys.readouterr().out)
    assert [item["session_id"] for item in published["accepted"]] == ["synthetic-001"]
    assert (layout.directory("provenance") / "intake/sessions/synthetic-001.json").exists()


def test_the_synthetic_command_writes_a_valid_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "synthetic-777"
    assert main(["intake", "synthetic", str(target), "--session-id", "synthetic-777"]) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "synthetic-777"
    assert (target / "READY").exists()

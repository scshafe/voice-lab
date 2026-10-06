from __future__ import annotations

import subprocess
import wave
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cole_voice_lab.capture import expected_recording_paths, finalize_capture_session
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.models import CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.sync import (
    PRIMARY_MINI_ALIAS,
    REMOTE_INTAKE_SUFFIX,
    RsyncPass,
    SyncPlan,
    build_sync_plan,
    execute_sync_plan,
)

NOW = datetime(2026, 8, 17, 19, 0, tzinfo=UTC)
REMOTE_USER = "operator"
REMOTE_ROOT = f"/Users/{REMOTE_USER}/{REMOTE_INTAKE_SUFFIX}"


def _plan(session: Path, **options: object) -> SyncPlan:
    """A plan to the test account's macOS intake root unless a test overrides it."""
    options.setdefault("remote_user", REMOTE_USER)
    options.setdefault("remote_root", REMOTE_ROOT)
    return build_sync_plan(session, **options)  # type: ignore[arg-type]


def _suite() -> PromptSuite:
    return PromptSuite(
        suite_id="pilot-v1",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="room-tone-01",
                text="Remain silent.",
                category="room-tone",
                style="Silent",
                expected_seconds=10.0,
            ),
            Prompt(
                prompt_id="neutral-01",
                text="This is the exact sync sentence.",
                category="neutral",
                style="Neutral",
                expected_seconds=10.0,
            ),
        ),
    )


def _package(tmp_path: Path) -> Path:
    suite = _suite()
    session = tmp_path / "private" / "sessions" / "pilot-sync"
    session.mkdir(mode=0o700, parents=True)
    for relative in expected_recording_paths(suite):
        audio = session / relative
        audio.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with wave.open(str(audio), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(3)
            writer.setframerate(48_000)
            samples = [1_000_000, -1_000_000] * 480
            writer.writeframes(
                b"".join(value.to_bytes(3, "little", signed=True) for value in samples)
            )
    finalize_capture_session(
        session,
        session_id="pilot-sync",
        subject_id="cole",
        prompt_suite=suite,
        device=CaptureDevice(device_id="4", display_name="miniDSP UMIK-1"),
        captured_at=NOW,
        finalized_at=NOW,
        capture_host="coles-macbook-pro",
    )
    return session


def test_default_plan_is_two_pass_dry_run_to_the_mini_alias(tmp_path: Path) -> None:
    plan = _plan(
        _package(tmp_path),
        identity_file=tmp_path / "intake-key",
        known_hosts_file=tmp_path / "known-hosts",
    )

    assert plan.dry_run
    assert plan.target == PRIMARY_MINI_ALIAS
    assert len(plan.approval_sha256()) == 64
    assert plan.approval_sha256() == plan.approval_sha256()
    approval = plan.approval_payload()
    assert approval["schema_version"] == "cole-voice-lab.sync-plan-approval/v2"
    assert approval["identity_file"] == str(tmp_path / "intake-key")
    assert approval["known_hosts_file"] == str(tmp_path / "known-hosts")
    assert approval["package_snapshot_sha256"] == plan.package_snapshot.sha256()
    assert approval["package_snapshot"] == plan.package_snapshot.approval_payload()
    assert plan.package_snapshot.manifest_sha256 == next(
        member.sha256 for member in plan.package_snapshot.files if member.path == "manifest.json"
    )
    assert plan.package_snapshot.ready_sha256 == next(
        member.sha256 for member in plan.package_snapshot.files if member.path == "READY"
    )
    rebound = replace(
        plan,
        package_snapshot=replace(plan.package_snapshot, ready_sha256="f" * 64),
    )
    assert rebound.approval_sha256() != plan.approval_sha256()
    assert [item.name for item in plan.passes] == [
        "payload-without-ready",
        "ready-marker-last",
    ]
    payload, ready = (item.argv for item in plan.passes)
    assert "--exclude=/READY" in payload
    assert "--include=/READY" in ready
    assert "--exclude=*" in ready
    assert "--exclude=*" in payload
    assert "--include=/audio/" in payload
    for member in plan.package_snapshot.files:
        include = f"--include=/{member.path}"
        if member.path == "READY":
            assert include not in payload
        else:
            assert include in payload
    assert "--dry-run" in payload and "--dry-run" in ready
    for command in (payload, ready):
        assert not {"--delete", "--remove-source-files", "--inplace"}.intersection(command)
        ssh = command[command.index("-e") + 1]
        assert "BatchMode=yes" in ssh
        assert "StrictHostKeyChecking=yes" in ssh
        assert "PasswordAuthentication=no" in ssh
        assert "ClearAllForwardings=yes" in ssh
    assert plan.passes[0].argv[-1] == (
        f"{REMOTE_USER}@{PRIMARY_MINI_ALIAS}:{REMOTE_ROOT}/pilot-sync/"
    )


@pytest.mark.parametrize("target", ["openclaw", "OpenClaw.example.ts.net"])
def test_openclaw_gateway_target_is_rejected(tmp_path: Path, target: str) -> None:
    with pytest.raises(SecurityError, match="OpenClaw gateway"):
        _plan(_package(tmp_path), target=target)


def test_only_the_configured_private_alias_is_a_target(tmp_path: Path) -> None:
    assert PRIMARY_MINI_ALIAS == "cole-voice-lab-mini"
    plan = _plan(_package(tmp_path), target=PRIMARY_MINI_ALIAS.upper())

    assert plan.target == PRIMARY_MINI_ALIAS
    assert PRIMARY_MINI_ALIAS in plan.passes[0].argv[-1]


def test_a_linux_home_intake_root_is_accepted(tmp_path: Path) -> None:
    root = f"/home/{REMOTE_USER}/{REMOTE_INTAKE_SUFFIX}"
    plan = _plan(_package(tmp_path), remote_root=root, dry_run=False)

    assert plan.remote_directory == f"{root}/pilot-sync"
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    execute_sync_plan(
        plan, allow_network=True, approved_plan_sha256=plan.approval_sha256(), runner=runner
    )
    assert calls[0][-1] == f"{REMOTE_USER}@{PRIMARY_MINI_ALIAS}:{root}/pilot-sync/"


@pytest.mark.parametrize(
    "target",
    [
        "bad host",
        "user@host",
        "host/path",
        "example.com",
        "8.8.8.8",
        "192.168.1.25",
        "100.64.0.7",
    ],
)
def test_unapproved_or_public_target_is_rejected(tmp_path: Path, target: str) -> None:
    with pytest.raises(SecurityError, match="bound to the configured mini SSH alias"):
        _plan(_package(tmp_path), target=target)


@pytest.mark.parametrize("remote_root", ["relative/incoming", "/", "/tmp/../incoming", "bad\\path"])
def test_unsafe_remote_root_is_rejected(tmp_path: Path, remote_root: str) -> None:
    with pytest.raises(Exception, match="remote landing root"):
        _plan(_package(tmp_path), remote_root=remote_root)


def test_well_formed_but_unapproved_remote_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SecurityError, match="remote account's private intake root"):
        _plan(
            _package(tmp_path),
            remote_root=f"/Users/{REMOTE_USER}/.local/share/another-private-project/incoming",
        )


def test_invalid_remote_user_and_symlink_source_are_rejected(tmp_path: Path) -> None:
    package = _package(tmp_path)
    with pytest.raises(Exception, match="remote user"):
        _plan(package, remote_user="Cole Admin")
    with pytest.raises(SecurityError, match="remote account's private intake root"):
        _plan(package, remote_user="someone_else")
    link = tmp_path / "package-link"
    link.symlink_to(package, target_is_directory=True)
    with pytest.raises(SecurityError, match="real capture directory"):
        _plan(link)


def test_execution_requires_live_plan_and_separate_network_approval(tmp_path: Path) -> None:
    dry_plan = _plan(_package(tmp_path))
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(SecurityError, match="dry-run plan"):
        execute_sync_plan(dry_plan, allow_network=True, runner=runner)
    assert not calls

    live_plan = _plan(_package(tmp_path / "second"), dry_run=False)
    assert live_plan.shell_display().startswith("LIVE PLAN")
    assert live_plan.approval_sha256() in live_plan.shell_display()
    with pytest.raises(SecurityError, match="allow_network"):
        execute_sync_plan(live_plan, runner=runner)
    assert not calls

    with pytest.raises(SecurityError, match="approved_plan_sha256"):
        execute_sync_plan(live_plan, allow_network=True, runner=runner)
    with pytest.raises(SecurityError, match="does not match"):
        execute_sync_plan(
            live_plan,
            allow_network=True,
            approved_plan_sha256="f" * 64,
            runner=runner,
        )
    assert not calls

    results = execute_sync_plan(
        live_plan,
        allow_network=True,
        approved_plan_sha256=live_plan.approval_sha256(),
        runner=runner,
    )
    assert len(results) == 2
    assert calls == [live_plan.passes[0].argv, live_plan.passes[1].argv]


def test_approval_is_invalidated_by_added_changed_or_replaced_package_member(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)

    added_plan = _plan(package, dry_run=False)
    (package / "undeclared.txt").write_text("not approved", encoding="utf-8")
    with pytest.raises(SecurityError, match="payload pass revalidation"):
        execute_sync_plan(
            added_plan,
            allow_network=True,
            approved_plan_sha256=added_plan.approval_sha256(),
        )
    (package / "undeclared.txt").unlink()

    changed_plan = _plan(package, dry_run=False)
    manifest = package / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(SecurityError, match="payload pass revalidation"):
        execute_sync_plan(
            changed_plan,
            allow_network=True,
            approved_plan_sha256=changed_plan.approval_sha256(),
        )

    package = _package(tmp_path / "replacement")
    replaced_plan = _plan(package, dry_run=False)
    ready = package / "READY"
    replacement = package / ".READY.replacement"
    replacement.write_bytes(ready.read_bytes())
    replacement.replace(ready)
    with pytest.raises(SecurityError, match="changed after approval before payload pass"):
        execute_sync_plan(
            replaced_plan,
            allow_network=True,
            approved_plan_sha256=replaced_plan.approval_sha256(),
        )


def test_execution_revalidates_after_payload_before_ready_and_after_ready(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    plan = _plan(package, dry_run=False)
    calls: list[tuple[str, ...]] = []

    def mutate_after_payload(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        (package / "late-extra.txt").write_text("not in approval", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(SecurityError, match="READY pass revalidation"):
        execute_sync_plan(
            plan,
            allow_network=True,
            approved_plan_sha256=plan.approval_sha256(),
            runner=mutate_after_payload,
        )
    assert calls == [plan.passes[0].argv]
    (package / "late-extra.txt").unlink()

    plan = _plan(package, dry_run=False)
    calls.clear()

    def mutate_during_ready(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) == 2:
            ready = package / "READY"
            ready.write_bytes(ready.read_bytes() + b" ")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(SecurityError, match="post-transfer audit revalidation"):
        execute_sync_plan(
            plan,
            allow_network=True,
            approved_plan_sha256=plan.approval_sha256(),
            runner=mutate_during_ready,
        )
    assert calls == [plan.passes[0].argv, plan.passes[1].argv]


def test_live_execution_rejects_reordered_or_broadened_passes(tmp_path: Path) -> None:
    plan = _plan(_package(tmp_path), dry_run=False)
    reordered = replace(plan, passes=(plan.passes[1], plan.passes[0]))
    with pytest.raises(SecurityError, match="payload-then-READY pass order"):
        execute_sync_plan(
            reordered,
            allow_network=True,
            approved_plan_sha256=reordered.approval_sha256(),
        )

    broadened_payload = tuple(
        argument for argument in plan.passes[0].argv if argument != "--exclude=*"
    )
    broadened = replace(
        plan,
        passes=(replace(plan.passes[0], argv=broadened_payload), plan.passes[1]),
    )
    with pytest.raises(SecurityError, match="exact canonical rsync command"):
        execute_sync_plan(
            broadened,
            allow_network=True,
            approved_plan_sha256=broadened.approval_sha256(),
        )


@pytest.mark.parametrize("injected_option", ["--include=/***", "--rsync-path=/bin/sh"])
def test_live_execution_rejects_injected_rsync_prefix_options(
    tmp_path: Path,
    injected_option: str,
) -> None:
    plan = _plan(_package(tmp_path), dry_run=False)
    injected_payload = replace(
        plan.passes[0],
        argv=(plan.passes[0].argv[0], injected_option, *plan.passes[0].argv[1:]),
    )
    injected = replace(plan, passes=(injected_payload, plan.passes[1]))
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(SecurityError, match="exact canonical rsync command"):
        execute_sync_plan(
            injected,
            allow_network=True,
            approved_plan_sha256=injected.approval_sha256(),
            runner=runner,
        )
    assert calls == []


def test_live_execution_revalidates_destination_and_nondestructive_commands(
    tmp_path: Path,
) -> None:
    plan = _plan(_package(tmp_path), dry_run=False)

    wrong_target = replace(plan, target="example.com")
    with pytest.raises(SecurityError, match="configured mini SSH alias"):
        execute_sync_plan(
            wrong_target,
            allow_network=True,
            approved_plan_sha256=wrong_target.approval_sha256(),
        )

    for directory in (
        "/tmp/private/pilot-sync",
        "/",
        f"{REMOTE_ROOT}/another-session",
        f"/Users/Not A User/{REMOTE_INTAKE_SUFFIX}/pilot-sync",
    ):
        wrong_root = replace(plan, remote_directory=directory)
        with pytest.raises(SecurityError, match="private mini intake root"):
            execute_sync_plan(
                wrong_root,
                allow_network=True,
                approved_plan_sha256=wrong_root.approval_sha256(),
            )

    destructive = replace(
        plan,
        passes=(
            RsyncPass(name="payload-without-ready", argv=("rsync", "--delete")),
            plan.passes[1],
        ),
    )
    with pytest.raises(SecurityError, match="destructive rsync options"):
        execute_sync_plan(
            destructive,
            allow_network=True,
            approved_plan_sha256=destructive.approval_sha256(),
        )

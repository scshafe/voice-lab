"""Non-destructive, dry-run-by-default Tailnet rsync planning."""

from __future__ import annotations

import re
import shlex
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Self

from .canonical import canonical_sha256
from .capture import READY_NAME
from .errors import ContractError, SecurityError
from .intake import DirectorySnapshot, FileSnapshot, ValidatedPackage, validate_capture_package

PRIMARY_MINI_TARGET = "100.123.230.91"
PRIMARY_MINI_ALIAS = "cole-voice-lab-mini"
OPENCLAW_GATEWAY_TARGET = "100.91.94.21"
DEFAULT_REMOTE_ROOT = "/Users/cole/.local/share/cole-voice-lab/incoming"
DEFAULT_IDENTITY_NAME = "cole_voice_lab_intake_ed25519"

_APPROVED_TARGETS = frozenset({PRIMARY_MINI_TARGET, PRIMARY_MINI_ALIAS})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_FORBIDDEN_RSYNC_OPTIONS = frozenset({"--delete", "--remove-source-files", "--inplace"})


@dataclass(frozen=True, slots=True)
class RsyncPass:
    name: str
    argv: tuple[str, ...]

    def shell_display(self) -> str:
        return shlex.join(self.argv)


@dataclass(frozen=True, slots=True)
class SyncFileSnapshot:
    """Approval-safe identity and byte evidence for one package member."""

    path: str
    device: int
    inode: int
    mode: int
    links: int
    owner: int
    group: int
    size_bytes: int
    modified_ns: int
    changed_ns: int
    sha256: str

    @classmethod
    def from_intake_snapshot(cls, path: str, snapshot: FileSnapshot) -> Self:
        return cls(
            path=path,
            device=snapshot.device,
            inode=snapshot.inode,
            mode=snapshot.mode,
            links=snapshot.links,
            owner=snapshot.owner,
            group=snapshot.group,
            size_bytes=snapshot.size_bytes,
            modified_ns=snapshot.modified_ns,
            changed_ns=snapshot.changed_ns,
            sha256=snapshot.sha256,
        )

    def approval_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "links": self.links,
            "owner": self.owner,
            "group": self.group,
            "size_bytes": self.size_bytes,
            "modified_ns": self.modified_ns,
            "changed_ns": self.changed_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class SyncDirectorySnapshot:
    """Approval-safe identity evidence for one package directory."""

    path: str
    device: int
    inode: int
    mode: int
    owner: int
    group: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_intake_snapshot(cls, path: str, snapshot: DirectorySnapshot) -> Self:
        return cls(
            path=path,
            device=snapshot.device,
            inode=snapshot.inode,
            mode=snapshot.mode,
            owner=snapshot.owner,
            group=snapshot.group,
            modified_ns=snapshot.modified_ns,
            changed_ns=snapshot.changed_ns,
        )

    def approval_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "owner": self.owner,
            "group": self.group,
            "modified_ns": self.modified_ns,
            "changed_ns": self.changed_ns,
        }


@dataclass(frozen=True, slots=True)
class SyncPackageSnapshot:
    """Validated package state whose exact identities and bytes are approved."""

    session_id: str
    manifest_sha256: str
    source_qc_report_sha256: str
    ready_sha256: str
    files: tuple[SyncFileSnapshot, ...]
    directories: tuple[SyncDirectorySnapshot, ...]

    @classmethod
    def from_validated_package(cls, package: ValidatedPackage) -> Self:
        snapshot = package._snapshot
        try:
            ready_sha256 = snapshot.files[READY_NAME].sha256
        except KeyError as error:  # pragma: no cover - intake validation guarantees this
            raise SecurityError("validated capture package has no READY snapshot") from error
        return cls(
            session_id=package.manifest.session_id,
            manifest_sha256=package.manifest_sha256,
            source_qc_report_sha256=package.source_qc_report_sha256,
            ready_sha256=ready_sha256,
            files=tuple(
                SyncFileSnapshot.from_intake_snapshot(path, member)
                for path, member in sorted(snapshot.files.items())
            ),
            directories=tuple(
                SyncDirectorySnapshot.from_intake_snapshot(path, member)
                for path, member in sorted(snapshot.directories.items())
            ),
        )

    def approval_payload(self) -> dict[str, object]:
        return {
            "schema_version": "cole-voice-lab.sync-package-snapshot/v1",
            "session_id": self.session_id,
            "manifest_sha256": self.manifest_sha256,
            "source_qc_report_sha256": self.source_qc_report_sha256,
            "ready_sha256": self.ready_sha256,
            "files": [member.approval_payload() for member in self.files],
            "directories": [member.approval_payload() for member in self.directories],
        }

    def sha256(self) -> str:
        return canonical_sha256(self.approval_payload())


@dataclass(frozen=True, slots=True)
class SyncPlan:
    session_id: str
    source_directory: Path
    target: str
    remote_directory: str
    identity_file: Path
    known_hosts_file: Path
    dry_run: bool
    package_snapshot: SyncPackageSnapshot
    passes: tuple[RsyncPass, RsyncPass]

    def approval_payload(self) -> dict[str, object]:
        """Return the complete command plan covered by operator approval."""

        return {
            "schema_version": "cole-voice-lab.sync-plan-approval/v2",
            "session_id": self.session_id,
            "source_directory": str(self.source_directory),
            "target": self.target,
            "remote_directory": self.remote_directory,
            "identity_file": str(self.identity_file),
            "known_hosts_file": str(self.known_hosts_file),
            "dry_run": self.dry_run,
            "package_snapshot_sha256": self.package_snapshot.sha256(),
            "package_snapshot": self.package_snapshot.approval_payload(),
            "passes": [
                {"name": transfer_pass.name, "argv": list(transfer_pass.argv)}
                for transfer_pass in self.passes
            ],
        }

    def approval_sha256(self) -> str:
        """Digest every execution-relevant field for copy/paste approval."""

        return canonical_sha256(self.approval_payload())

    def shell_display(self) -> str:
        heading = (
            "DRY RUN — commands are a plan and have not been executed"
            if self.dry_run
            else (
                "LIVE PLAN — construction did not execute; explicit network approval is required"
                f"\nAPPROVAL SHA-256: {self.approval_sha256()}"
            )
        )
        snapshot = f"PACKAGE SNAPSHOT SHA-256: {self.package_snapshot.sha256()}"
        return (
            heading
            + "\n"
            + snapshot
            + "\n"
            + "\n".join(item.shell_display() for item in self.passes)
        )


def _validate_target(target: str) -> str:
    lowered = target.casefold()
    if (
        lowered == OPENCLAW_GATEWAY_TARGET
        or lowered == "openclaw"
        or lowered.startswith("openclaw.")
    ):
        raise SecurityError("the OpenClaw gateway node is not a voice-lab transfer target")
    if lowered not in _APPROVED_TARGETS:
        raise SecurityError(
            "voice-lab sync is bound to the primary mini Tailnet target or its approved alias"
        )
    return lowered


def _validate_remote_root(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ContractError("remote landing root must be one absolute POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError("remote landing root must be canonical and absolute")
    if path.as_posix() != value.rstrip("/") or len(path.parts) < 6:
        raise ContractError("remote landing root is too broad or not canonical")
    canonical = path.as_posix()
    if canonical != DEFAULT_REMOTE_ROOT:
        raise SecurityError("voice-lab sync is bound to the private mini intake root")
    return canonical


def _ssh_command(
    *,
    identity_file: Path,
    known_hosts_file: Path,
) -> tuple[str, ...]:
    return (
        "/usr/bin/ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "CheckHostIP=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "RequestTTY=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-i",
        str(identity_file),
    )


def _rsync_base(*, ssh_command: tuple[str, ...], dry_run: bool) -> tuple[str, ...]:
    command = (
        "/usr/bin/rsync",
        "-rltp",
        "--checksum",
        "--partial-dir=.rsync-partial",
        "--delay-updates",
        "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=",
        "--timeout=60",
        "--no-motd",
        "-e",
        shlex.join(ssh_command),
    )
    return command + (("--dry-run",) if dry_run else ())


def _assert_nondestructive(command: tuple[str, ...]) -> None:
    forbidden = _FORBIDDEN_RSYNC_OPTIONS.intersection(command)
    if forbidden:
        raise SecurityError(f"destructive rsync options are forbidden: {sorted(forbidden)}")


def _payload_filter_arguments(snapshot: SyncPackageSnapshot) -> tuple[str, ...]:
    """Allow only the approved directories and non-READY package members."""

    directories = tuple(
        f"--include=/{member.path}/" for member in snapshot.directories if member.path != "."
    )
    files = tuple(
        f"--include=/{member.path}" for member in snapshot.files if member.path != READY_NAME
    )
    return ("--exclude=/READY", *directories, *files, "--exclude=*")


def _ready_filter_arguments() -> tuple[str, ...]:
    return ("--include=/READY", "--exclude=*")


def _assert_live_pass_shape(plan: SyncPlan) -> None:
    """Require the exact payload-first, marker-last commands produced by the planner."""

    if tuple(transfer_pass.name for transfer_pass in plan.passes) != (
        "payload-without-ready",
        "ready-marker-last",
    ):
        raise SecurityError("live sync requires the fixed payload-then-READY pass order")
    source_argument = f"{plan.source_directory}/"
    destination = f"cole@{plan.target}:{plan.remote_directory}/"
    ssh_command = _ssh_command(
        identity_file=plan.identity_file,
        known_hosts_file=plan.known_hosts_file,
    )
    base = _rsync_base(ssh_command=ssh_command, dry_run=False)
    expected_commands = (
        base
        + (*_payload_filter_arguments(plan.package_snapshot), "--", source_argument, destination),
        base + (*_ready_filter_arguments(), "--", source_argument, destination),
    )
    for transfer_pass, expected_command in zip(plan.passes, expected_commands, strict=True):
        command = transfer_pass.argv
        _assert_nondestructive(command)
        if command != expected_command:
            raise SecurityError("live sync pass is not the exact canonical rsync command")


def _revalidate_plan_source(plan: SyncPlan, *, phase: str) -> None:
    """Fail if package membership, identities, metadata, or bytes differ from approval."""

    try:
        metadata = plan.source_directory.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SecurityError("sync source is no longer a real capture directory")
        package = validate_capture_package(plan.source_directory)
        snapshot = SyncPackageSnapshot.from_validated_package(package)
    except (ContractError, OSError, SecurityError) as error:
        raise SecurityError(f"capture package failed {phase} revalidation: {error}") from error
    if package.manifest.session_id != plan.session_id or snapshot != plan.package_snapshot:
        raise SecurityError(f"capture package changed after approval before {phase}")


def build_sync_plan(
    session_directory: Path,
    *,
    target: str = PRIMARY_MINI_TARGET,
    remote_user: str = "cole",
    remote_root: str = DEFAULT_REMOTE_ROOT,
    identity_file: Path | None = None,
    known_hosts_file: Path | None = None,
    dry_run: bool = True,
) -> SyncPlan:
    """Return the payload-then-READY transfer plan without contacting a peer."""

    source = session_directory.expanduser()
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"sync source must be a real capture directory: {source}")
    package = validate_capture_package(source)
    package_snapshot = SyncPackageSnapshot.from_validated_package(package)
    host = _validate_target(target)
    if not _USER.fullmatch(remote_user):
        raise ContractError(f"remote user is invalid: {remote_user!r}")
    if remote_user != "cole":
        raise SecurityError("voice-lab sync is bound to the private mini intake account")
    landing_root = _validate_remote_root(remote_root)
    identity = (identity_file or Path.home() / ".ssh" / DEFAULT_IDENTITY_NAME).expanduser()
    known_hosts = (known_hosts_file or Path.home() / ".ssh" / "known_hosts").expanduser()
    ssh_command = _ssh_command(identity_file=identity, known_hosts_file=known_hosts)
    base = _rsync_base(ssh_command=ssh_command, dry_run=dry_run)

    source_argument = f"{source}/"
    remote_directory = f"{landing_root}/{package.manifest.session_id}"
    destination = f"{remote_user}@{host}:{remote_directory}/"
    payload_command = base + (
        *_payload_filter_arguments(package_snapshot),
        "--",
        source_argument,
        destination,
    )
    ready_command = base + (
        *_ready_filter_arguments(),
        "--",
        source_argument,
        destination,
    )
    _assert_nondestructive(payload_command)
    _assert_nondestructive(ready_command)
    return SyncPlan(
        session_id=package.manifest.session_id,
        source_directory=source,
        target=host,
        remote_directory=remote_directory,
        identity_file=identity,
        known_hosts_file=known_hosts,
        dry_run=dry_run,
        package_snapshot=package_snapshot,
        passes=(
            RsyncPass(name="payload-without-ready", argv=payload_command),
            RsyncPass(name="ready-marker-last", argv=ready_command),
        ),
    )


def execute_sync_plan(
    plan: SyncPlan,
    *,
    allow_network: bool = False,
    approved_plan_sha256: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Execute only the exact live plan whose digest an operator approved."""

    if plan.dry_run:
        raise SecurityError("refusing to execute a dry-run plan; rebuild it with dry_run=False")
    if not allow_network:
        raise SecurityError("network execution requires allow_network=True")
    if approved_plan_sha256 is None or not _SHA256.fullmatch(approved_plan_sha256):
        raise SecurityError("network execution requires an exact approved_plan_sha256")
    actual_plan_sha256 = plan.approval_sha256()
    if approved_plan_sha256 != actual_plan_sha256:
        raise SecurityError("approved plan SHA-256 does not match the exact live sync plan")
    _validate_target(plan.target)
    expected_remote_directory = f"{DEFAULT_REMOTE_ROOT}/{plan.session_id}"
    if plan.remote_directory != expected_remote_directory:
        raise SecurityError("live sync plan is not bound to the private mini intake root")
    if plan.package_snapshot.session_id != plan.session_id:
        raise SecurityError("live sync package snapshot disagrees with the session ID")
    _assert_live_pass_shape(plan)
    _revalidate_plan_source(plan, phase="payload pass")
    first = runner(plan.passes[0].argv, text=True, capture_output=True, check=True)
    _revalidate_plan_source(plan, phase="READY pass")
    second = runner(plan.passes[1].argv, text=True, capture_output=True, check=True)
    _revalidate_plan_source(plan, phase="post-transfer audit")
    return first, second


# Stable short alias for CLI/library callers.
sync_plan = build_sync_plan

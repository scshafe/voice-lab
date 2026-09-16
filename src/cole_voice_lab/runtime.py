"""Opt-in runtime provisioning and one-request worker isolation."""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from . import __version__
from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .errors import ContractError, ExternalToolError, RuntimeUnavailableError, SecurityError
from .models import ContractModel, ExperimentRequest, ExperimentResult
from .paths import (
    _reject_existing_symlink_components,
    confined_path,
    reject_symlink_components,
    validate_relative_path,
)
from .registry import (
    PythonRuntimeLock,
    ResolvedCandidate,
    ReviewedBuiltWheelLock,
    RuntimeLock,
    RuntimeSanitizedFileLock,
    SourceLock,
    canonical_package_name,
    runtime_artifact_versions,
    verify_runtime_requirements,
)

MINIMUM_FREE_BYTES = 10 * 1024**3
SOURCE_STAGING_MARGIN_BYTES = 256 * 1024**2
SOURCE_MANIFEST_NAME = ".cole-voice-lab-source.json"
RUNTIME_RECEIPT_NAME = ".cole-voice-lab-runtime-install.json"
REVIEWED_WHEELHOUSE_PLACEHOLDER = "/REVIEWED-WHEELHOUSE-REQUIRED"
LAB_WHEEL_PLACEHOLDER = "/REVIEWED-COLE-VOICE-LAB-WHEEL-REQUIRED.whl"
_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class LocalSourceManifest(ContractModel):
    schema_version: str = "cole-voice-lab.local-source/v1"
    source_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern=r"^(git|huggingface)$")
    repository: str = Field(min_length=1, max_length=1_024)
    revision: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    files: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def _portable_files(cls, value: dict[str, str]) -> dict[str, str]:
        for path, digest in value.items():
            validate_relative_path(path)
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("source manifest file digests must be lowercase SHA-256")
        return value


@dataclass(frozen=True, slots=True)
class SourceVerification:
    source_id: str
    path: Path
    manifest_sha256: str | None
    file_sha256: dict[str, str]
    integrity_sha256: str
    integrity_record: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ProvisionPlan:
    source: SourceLock
    destination: Path
    estimated_download_bytes: int
    required_free_bytes: int
    commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class RuntimeInstallPlan:
    runtime: RuntimeLock
    repository_root: Path
    environment_path: Path
    reviewed_wheelhouse: Path | None
    lab_wheel: Path | None
    lab_wheel_sha256: str | None
    lab_distribution_version: str | None
    artifact_versions: Mapping[str, str]
    commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class SourceEvictionPlan:
    source_id: str
    path: Path
    manifest_sha256: str
    estimated_bytes: int


@dataclass(frozen=True, slots=True)
class RuntimeEvictionPlan:
    runtime_id: str
    path: Path
    receipt_sha256: str
    estimated_bytes: int


class RuntimeTreeEntry(ContractModel):
    path: str
    kind: Literal["directory", "file", "symlink"]
    mode: int = Field(ge=0, le=0o7777)
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    link_target: str | None = Field(default=None, max_length=4_096)
    resolved_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def _entry_shape(self) -> RuntimeTreeEntry:
        if self.kind == "file":
            if (
                self.sha256 is None
                or self.link_target is not None
                or self.resolved_sha256 is not None
            ):
                raise ValueError("runtime file entries require only a SHA-256 digest")
        elif self.kind == "symlink":
            if self.link_target is None or self.sha256 is not None or self.resolved_sha256 is None:
                raise ValueError(
                    "runtime symlink entries require target text and resolved-byte evidence"
                )
        elif (
            self.sha256 is not None
            or self.link_target is not None
            or self.resolved_sha256 is not None
        ):
            raise ValueError("runtime directory entries cannot carry file/link evidence")
        return self


class RuntimeInstallReceipt(ContractModel):
    schema_version: Literal["cole-voice-lab.runtime-install-receipt/v1"] = (
        "cole-voice-lab.runtime-install-receipt/v1"
    )
    runtime_id: str
    runtime_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirements_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_wheel_sha256: dict[str, str]
    reviewed_build_toolchain_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    lab_wheel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lab_distribution_version: str = Field(min_length=1, max_length=128)
    runtime_python_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_runtime_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sanitized_files: tuple[RuntimeSanitizedFileLock, ...]
    installed_distributions: dict[str, str] = Field(min_length=1)
    tree: tuple[RuntimeTreeEntry, ...] = Field(min_length=1)
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _tree_digest(self) -> RuntimeInstallReceipt:
        if self.tree_sha256 != canonical_sha256(
            [entry.model_dump(mode="json") for entry in self.tree]
        ):
            raise ValueError("runtime receipt tree digest does not match its entries")
        paths = [entry.path for entry in self.tree]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("runtime receipt tree paths must be sorted and unique")
        sanitized_paths = [entry.path for entry in self.sanitized_files]
        if len(sanitized_paths) != len(set(sanitized_paths)):
            raise ValueError("runtime receipt sanitized paths must be unique")
        return self


@dataclass(frozen=True, slots=True)
class WorkerInvocation:
    request: ExperimentRequest
    resolved: ResolvedCandidate
    runtime_python: Path
    model_path: Path
    auxiliary_paths: Mapping[str, Path]
    worker_lock_path: Path
    runtime_root: Path
    code_path: Path | None = None
    timeout_seconds: float = 3_600.0


def assert_disk_floor(path: Path, *, minimum_free_bytes: int = MINIMUM_FREE_BYTES) -> int:
    """Return free bytes or fail before any download/render writes begin."""

    if minimum_free_bytes < 1:
        raise ValueError("minimum free bytes must be positive")
    existing = path.expanduser()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    if free < minimum_free_bytes:
        raise RuntimeUnavailableError(
            f"disk floor not met: {free} free bytes; {minimum_free_bytes} required"
        )
    return free


def _reject_unsafe_source_root(path: Path) -> Path:
    """Return one absolute source root after rejecting every symlink component."""

    return _normalized_real_directory(path, label="model source")


def _git_environment() -> dict[str, str]:
    """Keep verification independent of user/global Git configuration."""

    return {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git_command(path: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(path),
            *arguments,
        ),
        check=False,
        capture_output=True,
        timeout=60,
        env=_git_environment(),
    )


def _required_git_output(path: Path, *arguments: str) -> bytes:
    completed = _git_command(path, *arguments)
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeUnavailableError(
            f"cannot verify Git checkout {path}: {message or 'Git command failed'}"
        )
    return completed.stdout


def _parse_git_record(record: bytes, *, label: str) -> tuple[str, str]:
    metadata, separator, raw_path = record.partition(b"\t")
    if not separator:
        raise SecurityError(f"malformed {label} record in pinned Git checkout")
    try:
        relative = raw_path.decode("utf-8", errors="strict")
        validate_relative_path(relative)
        details = metadata.decode("ascii", errors="strict")
    except (UnicodeError, ValueError) as error:
        raise SecurityError(f"non-portable {label} path in pinned Git checkout") from error
    return details, relative


def verify_git_checkout_integrity(path: Path, expected_revision: str) -> tuple[str, dict[str, str]]:
    """Verify a detached checkout's commit, index, worktree, ignored files, and submodules."""

    revision = _required_git_output(path, "rev-parse", "--verify", "HEAD").decode().strip()
    if revision != expected_revision:
        raise RuntimeUnavailableError(f"Git checkout is not at {expected_revision}")
    symbolic = _git_command(path, "symbolic-ref", "-q", "HEAD")
    if symbolic.returncode == 0:
        raise SecurityError("pinned Git checkout must use a detached HEAD")
    if symbolic.returncode != 1:
        raise RuntimeUnavailableError(f"cannot determine detached HEAD state for {path}")

    status = _required_git_output(
        path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
    )
    if status:
        raise SecurityError(
            "pinned Git checkout contains tracked, staged, untracked, or ignored changes"
        )

    tree_records = _required_git_output(
        path, "ls-tree", "-r", "-z", "--full-tree", expected_revision
    ).split(b"\0")
    index_records = _required_git_output(path, "ls-files", "--stage", "-z").split(b"\0")
    tree: dict[str, tuple[str, str]] = {}
    for raw in tree_records:
        if not raw:
            continue
        details, relative = _parse_git_record(raw, label="tree")
        try:
            mode, kind, object_id = details.split(" ")
        except ValueError as error:
            raise SecurityError("malformed tree entry in pinned Git checkout") from error
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise SecurityError(
                f"pinned Git checkout forbids symlinks and submodules: {relative} ({mode} {kind})"
            )
        tree[relative] = (mode, object_id)

    index: dict[str, tuple[str, str]] = {}
    for raw in index_records:
        if not raw:
            continue
        details, relative = _parse_git_record(raw, label="index")
        try:
            mode, object_id, stage = details.split(" ")
        except ValueError as error:
            raise SecurityError("malformed index entry in pinned Git checkout") from error
        if stage != "0":
            raise SecurityError(f"pinned Git checkout has an unresolved index stage: {relative}")
        index[relative] = (mode, object_id)
    if index != tree:
        raise SecurityError("pinned Git checkout index does not exactly match its commit tree")

    observed: dict[str, str] = {}
    for relative, (_mode, object_id) in sorted(tree.items()):
        candidate = confined_path(path, relative, must_exist=True)
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(f"tracked Git path is not a regular file: {candidate}")
        if metadata.st_nlink != 1:
            raise SecurityError(f"tracked Git path must have exactly one hard link: {candidate}")
        expected_payload = _required_git_output(path, "cat-file", "blob", object_id)
        with candidate.open("rb") as stream:
            actual_payload = stream.read()
        if actual_payload != expected_payload:
            raise SecurityError(f"tracked Git bytes do not match the pinned commit: {relative}")
        observed[relative] = hashlib.sha256(actual_payload).hexdigest()

    tree_id = (
        _required_git_output(path, "rev-parse", f"{expected_revision}^{{tree}}").decode().strip()
    )
    integrity = canonical_sha256(
        {
            "schema_version": "cole-voice-lab.git-checkout-integrity/v1",
            "revision": expected_revision,
            "tree_id": tree_id,
            "files": observed,
        }
    )
    return integrity, observed


def _unexpected_snapshot_files(source: SourceLock, path: Path) -> list[str]:
    allowed = set(source.allow_patterns)
    unexpected: list[str] = []
    for candidate in path.rglob("*"):
        metadata = candidate.lstat()
        relative = candidate.relative_to(path).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(f"model snapshot contains a forbidden symlink: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SecurityError(f"model snapshot contains an unsafe file type: {relative}")
        if metadata.st_nlink != 1:
            raise SecurityError(f"model snapshot file must have exactly one hard link: {relative}")
        if relative == SOURCE_MANIFEST_NAME or relative.startswith(".cache/"):
            continue
        if relative not in allowed:
            unexpected.append(relative)
    return sorted(unexpected)


def verify_local_source(
    source: SourceLock,
    path: Path,
    *,
    require_manifest: bool = True,
    verify_hashes: bool = True,
) -> SourceVerification:
    """Verify an exact local snapshot or checkout without contacting its origin."""

    path = _reject_unsafe_source_root(path)
    if source.kind == "git":
        integrity, files = verify_git_checkout_integrity(path, source.revision)
        tree_id = (
            _required_git_output(path, "rev-parse", f"{source.revision}^{{tree}}").decode().strip()
        )
        integrity_record: dict[str, object] = {
            "schema_version": "cole-voice-lab.git-checkout-integrity/v1",
            "revision": source.revision,
            "tree_id": tree_id,
            "files": files,
        }
        if canonical_sha256(integrity_record) != integrity:
            raise SecurityError("Git checkout integrity preimage changed during verification")
        return SourceVerification(
            source.source_id,
            path,
            None,
            files,
            integrity,
            integrity_record,
        )

    unexpected = _unexpected_snapshot_files(source, path)
    if unexpected:
        raise ContractError(f"snapshot {source.source_id} has non-allow-listed files: {unexpected}")
    observed: dict[str, str] = {}
    for expected in source.files:
        candidate = confined_path(path, expected.path, must_exist=True)
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SecurityError(f"locked model artifact is not a regular file: {candidate}")
        if metadata.st_nlink != 1:
            raise SecurityError(
                f"locked model artifact must have exactly one hard link: {candidate}"
            )
        if metadata.st_size != expected.size_bytes:
            raise ContractError(f"locked model artifact size mismatch: {candidate}")
        if verify_hashes or expected.sha256 is not None:
            digest, _ = sha256_file(candidate)
            observed[expected.path] = digest
            if expected.sha256 is not None and digest != expected.sha256:
                raise ContractError(f"locked model artifact digest mismatch: {candidate}")

    manifest_path = path / SOURCE_MANIFEST_NAME
    manifest_digest: str | None = None
    if require_manifest:
        manifest_metadata = manifest_path.lstat()
        if (
            stat.S_ISLNK(manifest_metadata.st_mode)
            or not stat.S_ISREG(manifest_metadata.st_mode)
            or manifest_metadata.st_nlink != 1
        ):
            raise SecurityError(
                f"local source manifest must be a single-link regular file: {manifest_path}"
            )
        manifest_digest, _ = sha256_file(manifest_path)
        manifest = LocalSourceManifest.model_validate_json(manifest_path.read_bytes())
        expected_identity = (source.source_id, source.kind, source.repository, source.revision)
        actual_identity = (
            manifest.source_id,
            manifest.kind,
            manifest.repository,
            manifest.revision,
        )
        if actual_identity != expected_identity:
            raise ContractError(f"local source manifest identity mismatch: {manifest_path}")
        for relative, digest in observed.items():
            if manifest.files.get(relative) != digest:
                raise ContractError(f"local source manifest digest mismatch for {relative}")
    integrity_record = {
        "schema_version": "cole-voice-lab.snapshot-integrity/v1",
        "source_id": source.source_id,
        "repository": source.repository,
        "revision": source.revision,
        "manifest_sha256": manifest_digest,
        "files": observed,
    }
    integrity = canonical_sha256(integrity_record)
    return SourceVerification(
        source.source_id,
        path,
        manifest_digest,
        observed,
        integrity,
        integrity_record,
    )


def _tree_size_without_following(path: Path) -> int:
    total = 0
    for directory, directory_names, file_names in os.walk(path, followlinks=False):
        parent = Path(directory)
        for name in (*directory_names, *file_names):
            candidate = parent / name
            metadata = candidate.lstat()
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                total += metadata.st_size
            elif not stat.S_ISDIR(metadata.st_mode):
                raise SecurityError(f"model source contains an unsafe file type: {candidate}")
    return total


def build_source_eviction_plan(
    source: SourceLock,
    path: Path,
    *,
    boundary: Path,
) -> SourceEvictionPlan:
    """Verify and describe one exact local model snapshot without deleting it."""

    if source.kind != "huggingface":
        raise ContractError("only downloaded Hugging Face snapshots can be evicted")
    root = boundary.expanduser().resolve(strict=True)
    candidate = path.expanduser().resolve(strict=True)
    if candidate.parent != root or candidate.name != source.source_id:
        raise SecurityError("model eviction target must be one direct, source-ID-named child")
    verification = verify_local_source(source, candidate, verify_hashes=True)
    if verification.manifest_sha256 is None:
        raise ContractError("model eviction requires a checksum-bound local source manifest")
    return SourceEvictionPlan(
        source_id=source.source_id,
        path=candidate,
        manifest_sha256=verification.manifest_sha256,
        estimated_bytes=_tree_size_without_following(candidate),
    )


def execute_source_eviction(
    source: SourceLock,
    plan: SourceEvictionPlan,
    *,
    boundary: Path,
    expected_manifest_sha256: str,
    confirmed: bool = False,
) -> int:
    """Irreversibly remove one reverified snapshot after an exact digest confirmation."""

    if not confirmed:
        raise RuntimeUnavailableError("model eviction requires explicit confirmation")
    if expected_manifest_sha256 != plan.manifest_sha256:
        raise ContractError("expected model manifest digest does not match the eviction plan")
    current = build_source_eviction_plan(source, plan.path, boundary=boundary)
    if current != plan:
        raise SecurityError("model snapshot changed after eviction planning")
    tombstone = plan.path.parent / (
        f".evicting-{plan.source_id}-{plan.manifest_sha256[:12]}-{secrets.token_hex(4)}"
    )
    os.rename(plan.path, tombstone)
    try:
        shutil.rmtree(tombstone)
    except Exception as error:
        raise RuntimeUnavailableError(
            f"model eviction was incomplete; inspect private tombstone {tombstone}: {error}"
        ) from error
    parent_descriptor = os.open(plan.path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return plan.estimated_bytes


def write_source_manifest(source: SourceLock, path: Path) -> str:
    """Hash a newly provisioned curated snapshot and seal its local evidence."""

    verification = verify_local_source(
        source,
        path,
        require_manifest=False,
        verify_hashes=True,
    )
    manifest = LocalSourceManifest(
        source_id=source.source_id,
        kind=source.kind,
        repository=source.repository,
        revision=source.revision,
        files=verification.file_sha256,
    )
    return write_canonical_json(path / SOURCE_MANIFEST_NAME, manifest, mode=0o600)


def build_source_provision_plan(
    source: SourceLock,
    destination: Path,
    *,
    runtime_python: Path,
) -> ProvisionPlan:
    destination = destination.expanduser().resolve(strict=False)
    commands: tuple[tuple[str, ...], ...]
    if source.kind == "huggingface":
        command: tuple[str, ...] = (
            str(runtime_python),
            "-I",
            "-P",
            "-m",
            "cole_voice_lab.workers.snapshot",
            "--repository",
            source.repository,
            "--revision",
            source.revision,
            "--destination",
            str(destination),
        ) + tuple(argument for path in source.allow_patterns for argument in ("--allow-file", path))
        commands = (command,)
    else:
        commands = (
            (
                "git",
                "clone",
                "--no-checkout",
                "--filter=blob:none",
                source.repository,
                str(destination),
            ),
            ("git", "-C", str(destination), "fetch", "--depth", "1", "origin", source.revision),
            ("git", "-C", str(destination), "checkout", "--detach", source.revision),
        )
    estimated_download_bytes = sum(item.size_bytes for item in source.files)
    required_free_bytes = (
        MINIMUM_FREE_BYTES + estimated_download_bytes + SOURCE_STAGING_MARGIN_BYTES
        if source.kind == "huggingface"
        else MINIMUM_FREE_BYTES + SOURCE_STAGING_MARGIN_BYTES
    )
    return ProvisionPlan(
        source,
        destination,
        estimated_download_bytes,
        required_free_bytes,
        commands,
    )


def _provision_environment() -> dict[str, str]:
    environment = {
        key: value
        for key in ("HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
        if (value := os.environ.get(key))
    }
    environment.update(
        {
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONSAFEPATH": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _discard_incomplete_path(path: Path, *, label: str) -> None:
    """Remove only the previously absent path created by a failed plan."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeUnavailableError(f"cannot inspect incomplete {label}: {path}") from error
    try:
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as error:
        raise RuntimeUnavailableError(
            f"cannot remove incomplete {label}; inspect and remove it manually: {path}"
        ) from error


def execute_provision_plan(
    plan: ProvisionPlan,
    *,
    allow_downloads: bool = False,
    runner: CommandRunner = subprocess.run,
) -> SourceVerification:
    """Execute a networked plan only after the caller supplies the explicit flag."""

    if not allow_downloads:
        raise RuntimeUnavailableError(
            "source download is disabled; explicit allow_downloads is required"
        )
    assert_disk_floor(plan.destination, minimum_free_bytes=plan.required_free_bytes)
    if plan.destination.exists():
        raise ContractError(f"refusing to provision over existing path: {plan.destination}")
    plan.destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = _provision_environment()
    try:
        for command in plan.commands:
            completed = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3_600,
                env=environment,
            )
            if completed.returncode != 0:
                raise ExternalToolError(
                    f"source provisioning failed for {plan.source.source_id}: "
                    f"{completed.stderr.strip()}"
                )
            assert_disk_floor(plan.destination)
        if plan.source.kind == "huggingface":
            write_source_manifest(plan.source, plan.destination)
        verification = verify_local_source(plan.source, plan.destination, verify_hashes=True)
        assert_disk_floor(plan.destination)
        return verification
    except Exception:
        _discard_incomplete_path(plan.destination, label="model source")
        raise


def _normalized_real_directory(path: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor /= component
        try:
            metadata = cursor.lstat()
        except OSError as error:
            raise RuntimeUnavailableError(f"cannot inspect {label}: {cursor}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(f"{label} contains a symlink component: {cursor}")
    metadata = absolute.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"{label} is not a real directory: {absolute}")
    return absolute


def _confined_symlink_resolution(
    candidate: Path,
    root: Path,
    *,
    label: str,
    relative: str,
) -> Path:
    """Resolve every symlink hop without permitting escape and re-entry."""

    try:
        pending = list(candidate.relative_to(root).parts)
    except ValueError as error:
        raise SecurityError(
            f"{label} symlink starts outside its reviewed root: {relative}"
        ) from error
    resolved_parts: list[str] = []
    symlink_hops = 0
    while pending:
        component = pending.pop(0)
        if component in {"", ".", ".."}:
            raise SecurityError(f"{label} has an unsafe symlink component: {relative}")
        current = root.joinpath(*resolved_parts, component)
        try:
            metadata = current.lstat()
        except OSError as error:
            raise SecurityError(f"{label} has a dangling symlink path: {relative}") from error
        if stat.S_ISLNK(metadata.st_mode):
            symlink_hops += 1
            if symlink_hops > 64:
                raise SecurityError(f"{label} symlink exceeds its hop bound: {relative}")
            target = Path(os.readlink(current))
            if target.is_absolute():
                if target.parts[: len(root.parts)] != root.parts:
                    raise SecurityError(f"{label} symlink escapes its reviewed root: {relative}")
                target_parts = target.parts[len(root.parts) :]
                target_base: list[str] = []
            else:
                target_parts = target.parts
                target_base = list(resolved_parts)
            for target_component in target_parts:
                if target_component in {"", "."}:
                    continue
                if target_component == "..":
                    if not target_base:
                        raise SecurityError(
                            f"{label} symlink escapes its reviewed root: {relative}"
                        )
                    target_base.pop()
                else:
                    target_base.append(target_component)
            pending = target_base + pending
            resolved_parts = []
            continue
        if pending and not stat.S_ISDIR(metadata.st_mode):
            raise SecurityError(f"{label} symlink traverses a non-directory: {relative}")
        resolved_parts.append(component)
    return root.joinpath(*resolved_parts)


def _immutable_tree_sha256(
    path: Path, *, label: str, excluded_relative_entries: tuple[str, ...] = ()
) -> str:
    """Hash one complete directory tree without following symlinks."""

    root = _normalized_real_directory(path, label=label)
    for relative in excluded_relative_entries:
        validate_relative_path(relative)
    entries: list[dict[str, object]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        parent = Path(directory)
        directory_names[:] = [
            name
            for name in directory_names
            if (parent / name).relative_to(root).as_posix() not in excluded_relative_entries
        ]
        file_names[:] = [
            name
            for name in file_names
            if (parent / name).relative_to(root).as_posix() not in excluded_relative_entries
        ]
        directory_names.sort()
        file_names.sort()
        parent = Path(directory)
        for name in (*directory_names, *file_names):
            candidate = parent / name
            metadata = candidate.lstat()
            relative = candidate.relative_to(root).as_posix()
            base: dict[str, object] = {
                "path": relative,
                "mode": stat.S_IMODE(metadata.st_mode),
                "size_bytes": metadata.st_size,
            }
            if stat.S_ISDIR(metadata.st_mode):
                base["kind"] = "directory"
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise SecurityError(f"{label} contains a hardlinked file: {relative}")
                digest, size = sha256_file(candidate)
                base.update({"kind": "file", "sha256": digest, "size_bytes": size})
            elif stat.S_ISLNK(metadata.st_mode):
                _confined_symlink_resolution(
                    candidate,
                    root,
                    label=label,
                    relative=relative,
                )
                base.update({"kind": "symlink", "link_target": os.readlink(candidate)})
            else:
                raise SecurityError(f"{label} contains an unsafe file type: {relative}")
            entries.append(base)
            if len(entries) > 100_000:
                raise SecurityError(f"{label} exceeds its closure entry bound")
    return canonical_sha256(entries)


RECORDING_PYTHON_DISTRIBUTIONS = (
    "annotated-types",
    "pydantic",
    "pydantic-core",
    "typing-extensions",
    "typing-inspection",
)


@dataclass(frozen=True)
class MachOLoadCommands:
    """Bounded native loader evidence, read without invoking the executable."""

    minimum_macos: tuple[int, int, int] | None
    architectures: tuple[int, ...]
    dependencies: tuple[str, ...]
    install_names: tuple[str, ...]
    rpaths: tuple[str, ...]


def inspect_macho_load_commands(path: Path) -> MachOLoadCommands:
    """Read thin/fat 32/64-bit Mach-O deployment and dependency commands.

    @canonical native executable probe -- version/dependency evidence for release
    packaging and runtime closure. Reject malformed ranges and unbounded headers.
    """
    dependencies: set[str] = set()
    install_names: set[str] = set()
    rpaths: set[str] = set()
    versions: list[tuple[int, int, int]] = []
    architectures: set[int] = set()
    with open_regular_no_follow(path) as stream:
        initial = os.fstat(stream.fileno())
        magic = stream.read(4)
        fat = {
            b"\xca\xfe\xba\xbe": (">", False),
            b"\xbe\xba\xfe\xca": ("<", False),
            b"\xca\xfe\xba\xbf": (">", True),
            b"\xbf\xba\xfe\xca": ("<", True),
        }
        slices: list[tuple[int, int]] = []
        if magic in fat:
            endian, wide = fat[magic]
            raw = stream.read(4)
            if len(raw) != 4:
                raise SecurityError("Mach-O universal header is truncated")
            count = struct.unpack(endian + "I", raw)[0]
            if not 1 <= count <= 32:
                raise SecurityError("Mach-O architecture count exceeds its bound")
            size = 32 if wide else 20
            for _ in range(count):
                raw = stream.read(size)
                if len(raw) != size:
                    raise SecurityError("Mach-O architecture header is truncated")
                offset, length = struct.unpack_from(endian + ("QQ" if wide else "II"), raw, 8)
                if offset < 8 + count * size or length < 28 or offset + length > initial.st_size:
                    raise SecurityError("Mach-O architecture range is invalid")
                slices.append((offset, length))
        else:
            slices.append((0, initial.st_size))
        thin = {
            b"\xce\xfa\xed\xfe": ("<", False),
            b"\xfe\xed\xfa\xce": (">", False),
            b"\xcf\xfa\xed\xfe": ("<", True),
            b"\xfe\xed\xfa\xcf": (">", True),
        }
        for offset, length in slices:
            stream.seek(offset)
            raw = stream.read(32)
            if len(raw) < 28 or raw[:4] not in thin:
                raise SecurityError("native executable is not a supported Mach-O image")
            endian, wide = thin[raw[:4]]
            header_size = 32 if wide else 28
            cpu, _, _, count, command_bytes, _ = struct.unpack_from(endian + "IIIIII", raw, 4)
            if count > 4096 or command_bytes > 8 * 1024**2 or header_size + command_bytes > length:
                raise SecurityError("Mach-O load command range exceeds its bound")
            architectures.add(cpu)
            stream.seek(offset + header_size)
            commands = stream.read(command_bytes)
            cursor = 0
            for _ in range(count):
                if cursor + 8 > len(commands):
                    raise SecurityError("Mach-O load command is truncated")
                kind, size = struct.unpack_from(endian + "II", commands, cursor)
                if size < 8 or cursor + size > len(commands) or size % 4:
                    raise SecurityError("Mach-O load command size is invalid")
                command = commands[cursor : cursor + size]
                if kind in {
                    0xC,
                    0xD,
                    0x18,
                    0x80000018,
                    0x1F,
                    0x8000001F,
                    0x20,
                    0x23,
                    0x80000023,
                    0x8000001C,
                }:
                    minimum = 12 if kind == 0x8000001C else 24
                    if size < minimum:
                        raise SecurityError("Mach-O library command is truncated")
                    start = struct.unpack_from(endian + "I", command, 8)[0]
                    if start < minimum or start >= size or b"\0" not in command[start:]:
                        raise SecurityError("Mach-O library path is invalid")
                    try:
                        value = command[start:].split(b"\0", 1)[0].decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise SecurityError("Mach-O library path is not UTF-8") from error
                    if not value or len(value) > 16_384:
                        raise SecurityError("Mach-O library path exceeds its bound")
                    target = (
                        rpaths
                        if kind == 0x8000001C
                        else install_names
                        if kind == 0xD
                        else dependencies
                    )
                    target.add(value)
                if kind in {0x24, 0x32}:
                    if size < (24 if kind == 0x32 else 16):
                        raise SecurityError("Mach-O deployment command is truncated")
                    if kind == 0x24 or struct.unpack_from(endian + "I", command, 8)[0] == 1:
                        version = struct.unpack_from(
                            endian + "I", command, 12 if kind == 0x32 else 8
                        )[0]
                        versions.append((version >> 16, (version >> 8) & 255, version & 255))
                cursor += size
            if cursor != len(commands):
                raise SecurityError("Mach-O load command byte count differs from its header")
        final = os.fstat(stream.fileno())
        if (initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise SecurityError("Mach-O executable changed during its identity probe")
    return MachOLoadCommands(
        max(versions) if versions else None,
        tuple(sorted(architectures)),
        tuple(sorted(dependencies)),
        tuple(sorted(install_names)),
        tuple(sorted(rpaths)),
    )


def _python_distribution_identity(name: str) -> tuple[dict[str, object], tuple[Path, ...]]:
    distribution = importlib.metadata.distribution(name)
    for requirement in distribution.requires or ():
        # This service intentionally installs no extras. Unknown base dependencies
        # need an explicit closure update rather than an incomplete fingerprint.
        if re.search(r";.*\bextra\s*==", requirement):
            continue
        package = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        if (
            package is None
            or canonical_package_name(package[0]) not in RECORDING_PYTHON_DISTRIBUTIONS
        ):
            raise SecurityError(f"Python service dependency closure changed: {name}: {requirement}")
    root = Path(str(distribution.locate_file(""))).resolve(strict=True)
    declared = distribution.files
    if declared is None or len(declared) > 100_000:
        raise SecurityError(f"Python dependency has no bounded installed file evidence: {name}")
    entries: list[tuple[str, str, int]] = []
    macho: list[Path] = []
    for item in sorted(declared, key=str):
        relative = str(item)
        validate_relative_path(relative)
        candidate = root / relative
        # pip may record caches which were deliberately omitted by a bytecode-free
        # frozen release. Any present bytecode is executable and must be hashed.
        if candidate.suffix == ".pyc" and not candidate.exists():
            continue
        reject_symlink_components(root, candidate)
        with open_regular_no_follow(candidate) as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise SecurityError(f"Python dependency contains a hard link: {candidate}")
            magic = stream.read(4)
        digest, size = sha256_file(candidate)
        entries.append((relative, digest, size))
        if magic in _MACHO_MAGICS:
            macho.append(candidate)
    if len({entry[0] for entry in entries}) != len(entries):
        raise SecurityError(f"Python dependency contains duplicate file declarations: {name}")
    imported = importlib.import_module(name.replace("-", "_"))
    source = Path(str(imported.__file__)).resolve(strict=True)
    if not source.is_relative_to(root) or source.relative_to(root).as_posix() not in {
        entry[0] for entry in entries
    }:
        raise SecurityError(f"Python dependency import differs from its installed files: {name}")
    return {
        "name": canonical_package_name(name),
        "version": distribution.version,
        "files_sha256": canonical_sha256(entries),
        "file_count": len(entries),
    }, tuple(macho)


def _python_linked_library_identity(candidates: Sequence[Path]) -> dict[str, object]:
    pending = list(candidates)
    seen: set[Path] = set()
    libraries: list[tuple[str, str, int]] = []
    system: set[str] = set()
    while pending:
        candidate = pending.pop().resolve(strict=True)
        if candidate in seen:
            continue
        seen.add(candidate)
        if len(seen) > 2048:
            raise SecurityError("Python linked library closure exceeds its bound")
        digest, size = sha256_file(candidate)
        libraries.append((str(candidate), digest, size))
        commands = inspect_macho_load_commands(candidate)
        for dependency in commands.dependencies:
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                system.add(dependency)
            elif dependency:
                pending.extend(
                    _resolve_macho_dependency(dependency, candidate, rpaths=commands.rpaths)
                )
    return {"files": sorted(libraries), "system_library_references": sorted(system)}


def _current_recording_python_environment_identity() -> dict[str, object]:
    """Bind actual service dependency/base/library bytes and the exact macOS system TCB.

    @canonical recording Python environment -- used at process startup and release
    preparation. No model/Qt imports, installs, version-only cache, or audio work.
    The base site-packages path is excluded because the isolated venv cannot
    import it; installed service distributions are independently byte-bound.
    """
    if sys.platform != "darwin":
        raise RuntimeUnavailableError("native recording service runtime identity requires macOS")
    base = Path(sys.base_prefix).resolve(strict=True)
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    excluded = stdlib / "site-packages"
    active_roots = tuple(Path(item).resolve() for item in sys.path if item)
    if any(
        item == excluded.resolve() or item.is_relative_to(excluded.resolve())
        for item in active_roots
    ):
        raise SecurityError("native recording requires an isolated Python environment")
    dependencies: list[dict[str, object]] = []
    macho: list[Path] = [Path(sys.executable).resolve(strict=True)]
    for name in RECORDING_PYTHON_DISTRIBUTIONS:
        identity, extensions = _python_distribution_identity(name)
        dependencies.append(identity)
        macho.extend(extensions)
    # The complete stdlib tree digest includes source and existing bytecode; the
    # extension roots also seed recursive non-system dynamic-library evidence.
    dynamic = stdlib / "lib-dynload"
    for path in sorted(dynamic.glob("*.so")):
        reject_symlink_components(stdlib, path)
        macho.append(path)
    system_version = Path("/System/Library/CoreServices/SystemVersion.plist")
    system_sha, system_size = sha256_file(system_version)
    return {
        "schema_version": "cole-voice-lab.recording-python-environment/v1",
        "python_version": sys.version,
        "implementation": sys.implementation.name,
        "base_prefix": str(Path(sys.base_prefix).resolve()),
        "stdlib_root": str(stdlib),
        "base_runtime_sha256": _immutable_tree_sha256(
            base,
            label="Python base runtime",
            excluded_relative_entries=(excluded.relative_to(base).as_posix(),),
        ),
        "excluded_base_site_packages": {
            "path": str(excluded),
            "link_target": os.readlink(excluded) if excluded.is_symlink() else None,
        },
        "dependencies": dependencies,
        "linked_libraries": _python_linked_library_identity(macho),
        "system_version_plist_sha256": system_sha,
        "system_version_plist_size_bytes": system_size,
        "kernel_build": os.uname().version,
    }


RECORDING_PYTHON_RUNTIME_RECEIPT = ".cole-voice-lab-recording-python-runtime.json"


class RecordingPythonFileStat(ContractModel):
    path: str = Field(min_length=1, max_length=4096)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    mode: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    ctime_ns: int = Field(ge=0)
    link_target: str | None = Field(default=None, max_length=4096)


class RecordingPythonRuntimeReceipt(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-python-runtime-receipt/v1"] = (
        "cole-voice-lab.recording-python-runtime-receipt/v1"
    )
    environment_prefix: str
    environment: dict[str, JsonValue]
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[RecordingPythonFileStat, ...] = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def _bound_identity(self) -> RecordingPythonRuntimeReceipt:
        if self.environment_sha256 != canonical_sha256(self.environment):
            raise ValueError("runtime receipt environment digest changed")
        paths = tuple(entry.path for entry in self.entries)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("runtime receipt entries must be unique and ordered")
        return self


def _recording_runtime_stat(path: Path) -> RecordingPythonFileStat:
    metadata = path.lstat()
    if not (
        stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise SecurityError(f"runtime evidence includes an unsupported entry: {path}")
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise SecurityError(f"runtime evidence includes a hard link: {path}")
    return RecordingPythonFileStat(
        path=str(path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
        link_target=os.readlink(path) if stat.S_ISLNK(metadata.st_mode) else None,
    )


def _recording_runtime_entries(
    environment: dict[str, object],
) -> tuple[RecordingPythonFileStat, ...]:
    base = Path(sys.base_prefix).resolve(strict=True)
    excluded = Path(sysconfig.get_path("stdlib")).resolve(strict=True) / "site-packages"
    paths: set[Path] = {
        base,
        Path(sys.prefix),
        Path(sys.prefix) / "pyvenv.cfg",
        Path(os.path.abspath(sys.executable)),
        Path("/System/Library/CoreServices/SystemVersion.plist"),
    }
    for directory, directories, files in os.walk(base, followlinks=False):
        parent = Path(directory)
        paths.update(parent / name for name in (*directories, *files))
        directories[:] = [name for name in directories if parent / name != excluded]
    for name in RECORDING_PYTHON_DISTRIBUTIONS:
        distribution = importlib.metadata.distribution(name)
        root = Path(str(distribution.locate_file(""))).resolve(strict=True)
        paths.add(root)
        for item in distribution.files or ():
            candidate = root / str(item)
            if candidate.suffix == ".pyc" and not candidate.exists():
                continue
            paths.add(candidate)
            paths.update(parent for parent in candidate.parents if parent.is_relative_to(root))
    # The service's own installed package belongs to the trusted base. Only its source
    # files and packaged SQL are digested elsewhere, and Python resolves an extension
    # module before a source module, so an added .so or .pyc beside a service module
    # would shadow it while every recorded identity still matched. Binding the tree's
    # directory and file identities closes that. A development checkout is never
    # inside the environment prefix and is deliberately untouched.
    package = Path(__file__).resolve().parent
    if package.is_relative_to(Path(sys.prefix).resolve()):
        paths.add(package)
        for directory, directories, files in os.walk(package, followlinks=False):
            parent = Path(directory)
            paths.update(parent / name for name in (*directories, *files))
    linked = environment["linked_libraries"]
    if not isinstance(linked, dict) or not isinstance(linked.get("files"), list):
        raise SecurityError("runtime closure has no declared linked library evidence")
    for item in linked["files"]:
        if not isinstance(item, (tuple, list)) or not isinstance(item[0], str):
            raise SecurityError("runtime linked library evidence is malformed")
        paths.add(Path(item[0]))
    if len(paths) > 50_000:
        raise SecurityError("runtime stat evidence exceeds its bound")
    return tuple(_recording_runtime_stat(path) for path in sorted(paths, key=str))


def freeze_recording_python_runtime_receipt() -> Path:
    """Write one immutable release receipt after two stable full byte verifications.

    The receipt is adjacent to the venv, so creating it cannot invalidate tracked
    import directory identities. This is a build operation, never an interactive
    service mutation. Existing receipts are not replaced.
    """
    prefix = _reject_existing_symlink_components(Path(sys.prefix))
    path = prefix.parent / RECORDING_PYTHON_RUNTIME_RECEIPT
    if os.path.lexists(path):
        raise SecurityError("runtime receipt already exists; rebuild in a new release directory")
    first = _current_recording_python_environment_identity()
    before = _recording_runtime_entries(first)
    second = _current_recording_python_environment_identity()
    after = _recording_runtime_entries(second)
    if first != second or before != after:
        raise SecurityError("Python runtime changed while freezing its byte identity")
    receipt = RecordingPythonRuntimeReceipt.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": "cole-voice-lab.recording-python-runtime-receipt/v1",
                "environment_prefix": str(prefix),
                "environment": second,
                "environment_sha256": canonical_sha256(second),
                "entries": [entry.model_dump(mode="json") for entry in after],
            }
        )
    )
    data = canonical_json_bytes(receipt)
    if len(data) > 32 * 1024**2:
        raise SecurityError("Python runtime receipt exceeds its byte bound")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return path


def recording_python_environment_identity() -> dict[str, object]:
    """Validate a release's previously verified bytes by exact filesystem identity.

    Any changed inode, ctime, mtime, mode, size, link target or import-directory
    identity fails closed. Development without a release receipt hashes all
    current bytes. Source and SQL identity remain independently recomputed.
    """
    prefix = Path(sys.prefix)
    path = prefix.parent / RECORDING_PYTHON_RUNTIME_RECEIPT
    if not os.path.lexists(path):
        return _current_recording_python_environment_identity()
    path = _reject_existing_symlink_components(path)
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if (
            metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
            or metadata.st_size > 32 * 1024**2
        ):
            raise SecurityError("Python runtime receipt must remain owned, private and bounded")
        raw = stream.read(32 * 1024**2 + 1)
    receipt = RecordingPythonRuntimeReceipt.model_validate_json(raw)
    if raw != canonical_json_bytes(receipt) or receipt.environment_prefix != str(prefix):
        raise SecurityError("Python runtime receipt does not bind this canonical environment")
    for entry in receipt.entries:
        candidate = Path(entry.path)
        if not candidate.is_absolute() or os.path.normpath(entry.path) != entry.path:
            raise SecurityError("runtime evidence path is not normalized and absolute")
        if _recording_runtime_stat(candidate) != entry:
            raise SecurityError(
                f"verified Python runtime changed; rebuild the preview: {entry.path}"
            )
    if receipt.environment.get("kernel_build") != os.uname().version:
        raise SecurityError("verified macOS kernel build changed; rebuild the preview")
    return {**receipt.environment, "verified_runtime_receipt_sha256": canonical_sha256(receipt)}


def _verify_opt_alias(alias: str, expected_target: str, expected_prefix: Path) -> None:
    path = Path(alias)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeUnavailableError(f"locked Homebrew alias is missing: {path}") from error
    if not stat.S_ISLNK(metadata.st_mode) or os.readlink(path) != expected_target:
        raise SecurityError(f"locked Homebrew alias changed: {path}")
    if path.resolve(strict=True) != expected_prefix:
        raise SecurityError(f"locked Homebrew alias resolves outside its prefix: {path}")


def _macho_rpaths(candidate: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ("/usr/bin/otool", "-l", str(candidate)),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        raise RuntimeUnavailableError(f"cannot inspect Mach-O rpaths: {candidate}")
    lines = completed.stdout.splitlines()
    rpaths: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for detail in lines[index + 1 : index + 5]:
            stripped = detail.strip()
            if stripped.startswith("path "):
                rpaths.append(stripped.removeprefix("path ").split(" (offset ", 1)[0])
                break
    return tuple(rpaths)


def _expand_loader_reference(value: str, candidate: Path) -> Path:
    if value == "@loader_path":
        return candidate.parent
    if value.startswith("@loader_path/"):
        return candidate.parent / value.removeprefix("@loader_path/")
    if value.startswith("@executable_path"):
        raise SecurityError(
            f"locked runtime uses context-dependent @executable_path: {candidate}: {value}"
        )
    return Path(value)


def _resolve_macho_dependency(
    dependency: str, candidate: Path, *, rpaths: tuple[str, ...] | None = None
) -> tuple[Path, ...]:
    if dependency.startswith("@rpath/"):
        suffix = dependency.removeprefix("@rpath/")
        resolved: list[Path] = []
        for raw_rpath in _macho_rpaths(candidate) if rpaths is None else rpaths:
            expanded = _expand_loader_reference(raw_rpath, candidate)
            target = expanded / suffix
            if target.exists():
                resolved.append(target.resolve(strict=True))
        if not resolved:
            raise SecurityError(
                f"locked runtime has an unresolved @rpath dependency: {candidate}: {dependency}"
            )
        return tuple(sorted(set(resolved)))
    expanded = _expand_loader_reference(dependency, candidate)
    if not expanded.is_absolute():
        raise SecurityError(
            f"locked runtime contains a non-absolute dependency: {candidate}: {dependency}"
        )
    return (expanded.resolve(strict=True),)


def _macho_system_references(roots: Sequence[Path]) -> tuple[str, ...]:
    """Reject linked libraries outside locked formula roots or the macOS TCB."""

    allowed = tuple(root.resolve(strict=True) for root in roots)
    system: set[str] = set()
    for root in allowed:
        for directory, _directory_names, file_names in os.walk(root, followlinks=False):
            parent = Path(directory)
            for name in file_names:
                candidate = parent / name
                metadata = candidate.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                with candidate.open("rb") as stream:
                    if stream.read(4) not in _MACHO_MAGICS:
                        continue
                completed = subprocess.run(
                    ("/usr/bin/otool", "-L", str(candidate)),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={"PATH": "/usr/bin:/bin"},
                )
                if completed.returncode != 0:
                    raise RuntimeUnavailableError(f"cannot inspect Mach-O closure: {candidate}")
                for raw in completed.stdout.splitlines()[1:]:
                    dependency = raw.strip().split(" ", 1)[0]
                    if not dependency:
                        continue
                    if dependency.startswith(("/usr/lib/", "/System/Library/")):
                        system.add(dependency)
                        continue
                    for resolved in _resolve_macho_dependency(dependency, candidate):
                        if not any(
                            resolved == root or resolved.is_relative_to(root) for root in allowed
                        ):
                            raise SecurityError(
                                "Mach-O dependency escapes the locked runtime closure: "
                                f"{candidate}: {dependency} -> {resolved}"
                            )
    return tuple(sorted(system))


def _validate_runtime_pth(environment: Path) -> None:
    """Reject executable or external import-path additions before a normal site probe."""

    for path in sorted(environment.rglob("*.pth")):
        try:
            metadata = path.lstat()
        except OSError as error:
            raise RuntimeUnavailableError(f"cannot inspect runtime .pth file: {path}") from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SecurityError(f"runtime .pth evidence is not a single-link regular file: {path}")
        if metadata.st_size > 1024 * 1024:
            raise SecurityError(f"runtime .pth file exceeds its size bound: {path}")
        with open_regular_no_follow(path) as stream:
            try:
                lines = stream.read(1024 * 1024 + 1).decode("utf-8").splitlines()
            except UnicodeDecodeError as error:
                raise SecurityError(f"runtime .pth file is not UTF-8: {path}") from error
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("import ", "import\t")):
                raise SecurityError(f"runtime .pth file contains executable code: {path}")
            candidate = Path(stripped)
            lexical = candidate if candidate.is_absolute() else path.parent / candidate
            normalized = Path(os.path.abspath(lexical))
            if not normalized.is_relative_to(environment):
                raise SecurityError(
                    f"runtime .pth import path escapes the sealed environment: {path}"
                )
            if normalized.exists():
                resolved = normalized.resolve(strict=True)
                if not resolved.is_relative_to(environment):
                    raise SecurityError(
                        f"runtime .pth import path resolves outside the sealed environment: {path}"
                    )


def verify_python_runtime(
    lock: PythonRuntimeLock,
    python: Path,
    *,
    environment_path: Path | None = None,
) -> str:
    """Recompute the complete Homebrew CPython/formula closure and isolated import roots."""

    prefix = _normalized_real_directory(Path(lock.prefix_path), label="Python runtime prefix")
    _verify_opt_alias(lock.opt_alias_path, lock.opt_alias_target, prefix)
    executable = prefix / lock.executable_relative_path
    resolved_executable = executable.resolve(strict=True)
    if not resolved_executable.is_relative_to(prefix):
        raise SecurityError("locked runtime Python alias escapes its immutable prefix")
    resolved_metadata = resolved_executable.lstat()
    if not stat.S_ISREG(resolved_metadata.st_mode) or resolved_metadata.st_nlink != 1:
        raise SecurityError("locked runtime Python must resolve to a single-link regular file")
    if python.expanduser().resolve(strict=True) != resolved_executable:
        raise SecurityError("runtime Python does not resolve to the locked interpreter entrypoint")
    executable_sha256, _ = sha256_file(resolved_executable)
    if executable_sha256 != lock.executable_sha256:
        raise SecurityError("runtime Python executable differs from its lock")
    if _immutable_tree_sha256(prefix, label="Python runtime prefix") != lock.prefix_tree_sha256:
        raise SecurityError("Python runtime prefix tree differs from its lock")

    dependency_roots: list[Path] = []
    for dependency in lock.dependency_prefixes:
        dependency_root = _normalized_real_directory(
            Path(dependency.prefix_path),
            label=f"Python dependency prefix {dependency.formula}",
        )
        _verify_opt_alias(
            dependency.opt_alias_path,
            dependency.opt_alias_target,
            dependency_root,
        )
        if (
            _immutable_tree_sha256(
                dependency_root,
                label=f"Python dependency prefix {dependency.formula}",
            )
            != dependency.tree_sha256
        ):
            raise SecurityError(f"Python dependency prefix changed: {dependency.formula}")
        dependency_roots.append(dependency_root)
    system_references = _macho_system_references((prefix, *dependency_roots))
    if system_references != lock.system_library_references:
        raise SecurityError("Python runtime system-library closure differs from its lock")

    product = subprocess.run(
        ("/usr/bin/sw_vers", "-productVersion"),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout.strip()
    build_version = subprocess.run(
        ("/usr/bin/sw_vers", "-buildVersion"),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout.strip()
    if (product, build_version) != (lock.macos_product_version, lock.macos_build_version):
        raise SecurityError("macOS runtime TCB differs from the Python lock")

    normalized_environment: Path | None = None
    if environment_path is not None:
        normalized_environment = _normalized_real_directory(
            environment_path,
            label="installed runtime",
        )
        _validate_runtime_pth(normalized_environment)
        # Third-party extension modules may link only within the sealed venv,
        # the locked formula closure, or the exact-build macOS system TCB.
        _macho_system_references((prefix, *dependency_roots, normalized_environment))

    probe = (
        "import json,platform,sys,sysconfig;"
        "print(json.dumps({'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),'build':platform.python_build(),"
        "'machine':platform.machine(),'base_prefix':sys.base_prefix,"
        "'stdlib':sysconfig.get_path('stdlib'),'path':sys.path}))"
    )
    python_arguments = [str(python), "-I", "-P"]
    if environment_path is None:
        python_arguments.append("-S")
    python_arguments.extend(("-c", probe))
    completed = subprocess.run(
        tuple(python_arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONSAFEPATH": "1",
        },
    )
    if completed.returncode != 0:
        raise RuntimeUnavailableError("cannot probe the locked Python runtime")
    try:
        observed = json.loads(completed.stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeUnavailableError("locked Python emitted invalid identity evidence") from error
    if (
        observed.get("implementation") != lock.implementation
        or observed.get("version") != lock.version
        or tuple(observed.get("build", ())) != lock.build
        or observed.get("machine") != lock.machine
    ):
        raise SecurityError("Python runtime identity differs from its lock")
    base_prefix = Path(str(observed.get("base_prefix", ""))).resolve(strict=True)
    stdlib = Path(str(observed.get("stdlib", ""))).resolve(strict=True)
    if not base_prefix.is_relative_to(prefix) or not stdlib.is_relative_to(prefix):
        raise SecurityError("Python base prefix or standard library escapes its locked keg")
    allowed_import_roots = [prefix]
    if normalized_environment is not None:
        allowed_import_roots.append(normalized_environment)
    for item in observed.get("path", ()):
        candidate = Path(str(item))
        if not candidate.exists():
            continue
        resolved = candidate.resolve(strict=True)
        if not any(
            resolved == root or resolved.is_relative_to(root) for root in allowed_import_roots
        ):
            raise SecurityError(
                f"isolated Python import root escapes its sealed closure: {resolved}"
            )
    return canonical_sha256(lock)


def _verify_reviewed_wheel(
    path: Path,
    expected: ReviewedBuiltWheelLock,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeUnavailableError(f"reviewed wheel is missing: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SecurityError(f"reviewed wheel is not a regular file: {path}")
    if metadata.st_nlink != 1:
        raise SecurityError(f"reviewed wheel must have exactly one hard link: {path}")
    digest, size = sha256_file(path)
    if size != expected.wheel_size_bytes or digest != expected.wheel_sha256:
        raise SecurityError(f"reviewed wheel bytes do not match the lock: {path}")


def verify_reviewed_wheelhouse(runtime: RuntimeLock, wheelhouse: Path) -> Path:
    """Require the exact reviewed local-wheel set; extras and substitutions fail."""

    directory = _normalized_real_directory(wheelhouse, label="reviewed wheelhouse")
    expected = {item.wheel_filename: item for item in runtime.reviewed_built_wheels}
    try:
        with os.scandir(directory) as entries:
            actual = {entry.name for entry in entries}
    except OSError as error:
        raise RuntimeUnavailableError(f"cannot scan reviewed wheelhouse: {directory}") from error
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        raise SecurityError(
            f"reviewed wheelhouse membership mismatch; missing={missing}, extra={extra}"
        )
    for filename, evidence in expected.items():
        _verify_reviewed_wheel(directory / filename, evidence)
    return directory


def _verify_lab_wheel(path: Path, expected_sha256: str) -> Path:
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise ContractError("lab wheel digest must be lowercase SHA-256")
    absolute = Path(os.path.abspath(path.expanduser()))
    if not absolute.name.endswith(".whl"):
        raise ContractError("lab runtime artifact must be a wheel")
    try:
        metadata = absolute.lstat()
    except OSError as error:
        raise RuntimeUnavailableError(f"reviewed lab wheel is missing: {absolute}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SecurityError(f"reviewed lab wheel is not a regular file: {absolute}")
    if metadata.st_nlink != 1:
        raise SecurityError(f"reviewed lab wheel must have exactly one hard link: {absolute}")
    digest, _ = sha256_file(absolute)
    if digest != expected_sha256:
        raise SecurityError("reviewed lab wheel digest does not match the approved bytes")
    return absolute


def _distribution_identity(metadata: bytes, *, label: str) -> tuple[str, str]:
    if len(metadata) > 2 * 1024 * 1024:
        raise SecurityError(f"{label} metadata exceeds its size bound")
    names = [
        line[5:].strip()
        for line in metadata.decode("utf-8").splitlines()
        if line.startswith("Name:")
    ]
    versions = [
        line[8:].strip()
        for line in metadata.decode("utf-8").splitlines()
        if line.startswith("Version:")
    ]
    if len(names) != 1 or len(versions) != 1 or not versions[0]:
        raise ContractError(f"{label} has ambiguous package identity metadata")
    return canonical_package_name(names[0]), versions[0]


def _lab_wheel_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if info.filename.endswith(".dist-info/METADATA") and "/" in info.filename
            ]
            if len(candidates) != 1:
                raise ContractError("reviewed lab wheel must contain one distribution METADATA")
            info = candidates[0]
            if info.file_size > 2 * 1024 * 1024:
                raise SecurityError("reviewed lab wheel METADATA exceeds its size bound")
            name, version = _distribution_identity(
                archive.read(info),
                label="reviewed lab wheel",
            )
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise ContractError("cannot inspect reviewed lab wheel metadata") from error
    if name != "cole-voice-lab":
        raise ContractError("reviewed lab wheel does not contain cole-voice-lab")
    return version


def _runtime_tree_entries(environment_path: Path) -> tuple[RuntimeTreeEntry, ...]:
    root = _normalized_real_directory(environment_path, label="installed runtime")
    entries: list[RuntimeTreeEntry] = []
    entry_count = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        parent = Path(directory)
        for name in tuple(directory_names):
            candidate = parent / name
            metadata = candidate.lstat()
            relative = candidate.relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                if relative not in {"bin/python", "bin/python3", "bin/python3.12"}:
                    raise SecurityError(
                        f"installed runtime contains a forbidden symlink: {candidate}"
                    )
                directory_names.remove(name)
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file():
                    raise SecurityError(f"runtime Python alias is not a regular file: {candidate}")
                resolved_sha256, _ = sha256_file(resolved)
                entries.append(
                    RuntimeTreeEntry(
                        path=relative,
                        kind="symlink",
                        mode=stat.S_IMODE(metadata.st_mode),
                        size_bytes=metadata.st_size,
                        link_target=os.readlink(candidate),
                        resolved_sha256=resolved_sha256,
                    )
                )
            elif stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    RuntimeTreeEntry(
                        path=relative,
                        kind="directory",
                        mode=stat.S_IMODE(metadata.st_mode),
                        size_bytes=metadata.st_size,
                    )
                )
            else:
                raise SecurityError(f"installed runtime contains an unsafe entry: {candidate}")
            entry_count += 1
        for name in file_names:
            candidate = parent / name
            if candidate == root / RUNTIME_RECEIPT_NAME:
                continue
            metadata = candidate.lstat()
            relative = candidate.relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                if relative not in {"bin/python", "bin/python3", "bin/python3.12"}:
                    raise SecurityError(
                        f"installed runtime contains a forbidden symlink: {candidate}"
                    )
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file():
                    raise SecurityError(f"runtime Python alias is not a regular file: {candidate}")
                resolved_sha256, _ = sha256_file(resolved)
                entry = RuntimeTreeEntry(
                    path=relative,
                    kind="symlink",
                    mode=stat.S_IMODE(metadata.st_mode),
                    size_bytes=metadata.st_size,
                    link_target=os.readlink(candidate),
                    resolved_sha256=resolved_sha256,
                )
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise SecurityError(
                        f"installed runtime file must have exactly one hard link: {candidate}"
                    )
                digest, size = sha256_file(candidate)
                entry = RuntimeTreeEntry(
                    path=relative,
                    kind="file",
                    mode=stat.S_IMODE(metadata.st_mode),
                    size_bytes=size,
                    sha256=digest,
                )
            else:
                raise SecurityError(f"installed runtime contains an unsafe entry: {candidate}")
            entries.append(entry)
            entry_count += 1
        if entry_count > 500_000:
            raise SecurityError("installed runtime tree exceeds its entry bound")
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _installed_distributions(
    environment_path: Path,
    tree: tuple[RuntimeTreeEntry, ...],
) -> dict[str, str]:
    distributions: dict[str, str] = {}
    for entry in tree:
        if entry.kind != "file" or not entry.path.endswith(".dist-info/METADATA"):
            continue
        metadata_path = confined_path(environment_path, entry.path, must_exist=True)
        name, version = _distribution_identity(
            metadata_path.read_bytes(),
            label=f"installed distribution {entry.path}",
        )
        if name in distributions:
            raise ContractError(f"installed runtime repeats distribution metadata for {name}")
        distributions[name] = version
    if not distributions:
        raise ContractError("installed runtime contains no distribution metadata")
    return dict(sorted(distributions.items()))


def _expected_runtime_distributions(plan: RuntimeInstallPlan) -> dict[str, str]:
    if plan.lab_distribution_version is None:
        raise RuntimeUnavailableError("runtime install plan has no reviewed lab distribution")
    expected = dict(plan.artifact_versions)
    if "cole-voice-lab" in expected:
        raise ContractError("runtime artifact lock must not replace the reviewed lab wheel")
    expected["cole-voice-lab"] = plan.lab_distribution_version
    return dict(sorted(expected.items()))


def _runtime_receipt(
    plan: RuntimeInstallPlan,
    tree: tuple[RuntimeTreeEntry, ...],
) -> RuntimeInstallReceipt:
    installed = _installed_distributions(plan.environment_path, tree)
    expected = _expected_runtime_distributions(plan)
    if installed != expected:
        raise ContractError(
            "installed runtime distribution graph differs from the complete artifact lock; "
            f"missing={sorted(set(expected) - set(installed))}, "
            f"extra={sorted(set(installed) - set(expected))}"
        )
    runtime_python = plan.environment_path / "bin/python"
    try:
        resolved_python = runtime_python.resolve(strict=True)
    except OSError as error:
        raise RuntimeUnavailableError("installed runtime Python cannot be resolved") from error
    runtime_python_sha256, _ = sha256_file(resolved_python)
    if any(
        entry.kind == "symlink" and entry.resolved_sha256 != runtime_python_sha256 for entry in tree
    ):
        raise SecurityError("runtime Python aliases do not resolve to one bound interpreter")
    toolchain = plan.runtime.reviewed_build_toolchain
    python_runtime_lock_sha256 = verify_python_runtime(
        plan.runtime.python_runtime,
        runtime_python,
        environment_path=plan.environment_path,
    )
    return RuntimeInstallReceipt(
        runtime_id=plan.runtime.runtime_id,
        runtime_lock_sha256=canonical_sha256(plan.runtime),
        requirements_sha256=plan.runtime.requirements_sha256,
        artifact_lock_sha256=plan.runtime.artifact_lock_sha256,
        reviewed_wheel_sha256={
            item.wheel_filename: item.wheel_sha256 for item in plan.runtime.reviewed_built_wheels
        },
        reviewed_build_toolchain_sha256=(
            canonical_sha256(toolchain) if toolchain is not None else None
        ),
        lab_wheel_sha256=plan.lab_wheel_sha256 or "",
        lab_distribution_version=plan.lab_distribution_version or "",
        runtime_python_sha256=runtime_python_sha256,
        python_runtime_lock_sha256=python_runtime_lock_sha256,
        sanitized_files=plan.runtime.sanitized_files,
        installed_distributions=installed,
        tree=tree,
        tree_sha256=canonical_sha256([entry.model_dump(mode="json") for entry in tree]),
    )


def write_runtime_install_receipt(plan: RuntimeInstallPlan) -> tuple[Path, str]:
    """Seal the complete installed tree after its locked installation succeeds."""

    _verify_runtime_install_artifacts(plan)
    tree = _runtime_tree_entries(plan.environment_path)
    receipt = _runtime_receipt(plan, tree)
    path = plan.environment_path / RUNTIME_RECEIPT_NAME
    digest = write_canonical_json(path, receipt, mode=0o600)
    return path, digest


def _read_runtime_receipt(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[RuntimeInstallReceipt, str]:
    with open_regular_no_follow(path) as stream:
        before = os.fstat(stream.fileno())
        if before.st_nlink != 1 or before.st_size > 128 * 1024 * 1024:
            raise SecurityError("runtime install receipt has unsafe link count or size")
        payload = stream.read(128 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_nlink,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_nlink,
    )
    if len(payload) > 128 * 1024 * 1024 or identity_before != identity_after:
        raise SecurityError("runtime install receipt changed during its bounded read")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise SecurityError("runtime install receipt digest differs from approved evidence")
    receipt = RuntimeInstallReceipt.model_validate_json(payload)
    if payload != canonical_json_bytes(receipt) + b"\n":
        raise SecurityError("runtime install receipt is not canonical")
    return receipt, digest


def load_persisted_runtime_receipt(
    path: Path,
    *,
    expected_sha256: str,
    runtime_id: str,
    runtime_lock_sha256: str,
    runtime: RuntimeLock | None = None,
    repository_root: Path | None = None,
) -> RuntimeInstallReceipt:
    """Validate a copied receipt without requiring its runtime to remain resident."""

    receipt, _ = _read_runtime_receipt(path, expected_sha256=expected_sha256)
    if receipt.runtime_id != runtime_id or receipt.runtime_lock_sha256 != runtime_lock_sha256:
        raise ContractError("persisted runtime receipt differs from the audition trial binding")
    if (runtime is None) != (repository_root is None):
        raise ContractError("runtime and repository root must be supplied together")
    if runtime is not None and repository_root is not None:
        _verify_runtime_receipt_bindings(runtime, receipt, repository_root)
    return receipt


def _verify_runtime_receipt_bindings(
    runtime: RuntimeLock,
    receipt: RuntimeInstallReceipt,
    repository_root: Path,
) -> None:
    """Bind a resident or copied receipt to the complete current platform lock."""

    expected_distributions = runtime_artifact_versions(runtime, repository_root)
    expected_distributions["cole-voice-lab"] = __version__
    expected_toolchain_sha256 = (
        canonical_sha256(runtime.reviewed_build_toolchain)
        if runtime.reviewed_build_toolchain is not None
        else None
    )
    expected_bindings: dict[str, object] = {
        "runtime_id": runtime.runtime_id,
        "runtime_lock_sha256": canonical_sha256(runtime),
        "requirements_sha256": runtime.requirements_sha256,
        "artifact_lock_sha256": runtime.artifact_lock_sha256,
        "reviewed_wheel_sha256": {
            item.wheel_filename: item.wheel_sha256 for item in runtime.reviewed_built_wheels
        },
        "reviewed_build_toolchain_sha256": expected_toolchain_sha256,
        "python_runtime_lock_sha256": canonical_sha256(runtime.python_runtime),
        "sanitized_files": runtime.sanitized_files,
        "lab_distribution_version": __version__,
        "installed_distributions": dict(sorted(expected_distributions.items())),
    }
    for field, expected in expected_bindings.items():
        if getattr(receipt, field) != expected:
            raise ContractError(f"runtime install receipt has stale {field} evidence")


def verify_runtime_install_receipt(
    runtime: RuntimeLock,
    environment_path: Path,
    *,
    repository_root: Path,
    expected_receipt_sha256: str | None = None,
) -> tuple[RuntimeInstallReceipt, str]:
    """Rehash a resident runtime against the repository's complete artifact lock.

    ``repository_root`` is deliberately mandatory: a receipt is not an authority for
    its own distribution graph or lab version.  Approval-sensitive callers must also
    supply ``expected_receipt_sha256`` so that the externally reviewed receipt, the
    current lock, and the resident bytes all agree.
    """

    environment = _normalized_real_directory(environment_path, label="installed runtime")
    receipt, digest = _read_runtime_receipt(
        environment / RUNTIME_RECEIPT_NAME,
        expected_sha256=expected_receipt_sha256,
    )
    _verify_runtime_receipt_bindings(runtime, receipt, repository_root)
    if receipt.sanitized_files != runtime.sanitized_files:
        raise ContractError("runtime install receipt has stale sanitized_files evidence")
    for sanitized in runtime.sanitized_files:
        path = confined_path(environment, sanitized.path)
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raise SecurityError(f"reviewed executable startup file was not removed: {path}")
    tree = _runtime_tree_entries(environment)
    if tree != receipt.tree:
        raise SecurityError("installed runtime tree differs from its sealed receipt")
    if _installed_distributions(environment, tree) != receipt.installed_distributions:
        raise SecurityError("installed runtime distributions differ from their sealed receipt")
    runtime_python_sha256, _ = sha256_file((environment / "bin/python").resolve(strict=True))
    if runtime_python_sha256 != receipt.runtime_python_sha256:
        raise SecurityError("installed runtime Python differs from its sealed receipt")
    if (
        verify_python_runtime(
            runtime.python_runtime,
            environment / "bin/python",
            environment_path=environment,
        )
        != receipt.python_runtime_lock_sha256
    ):
        raise SecurityError("installed runtime base-interpreter closure differs from its receipt")
    return receipt, digest


def build_runtime_eviction_plan(
    runtime: RuntimeLock,
    environment_path: Path,
    *,
    boundary: Path,
    repository_root: Path,
) -> RuntimeEvictionPlan:
    """Verify and preview deletion of one direct sealed runtime child only."""

    root = boundary.expanduser().resolve(strict=True)
    candidate = environment_path.expanduser().resolve(strict=True)
    if candidate.parent != root or candidate.name != runtime.runtime_id:
        raise SecurityError("runtime eviction target must be one direct runtime-ID-named child")
    _, receipt_sha256 = verify_runtime_install_receipt(
        runtime,
        candidate,
        repository_root=repository_root,
    )
    return RuntimeEvictionPlan(
        runtime_id=runtime.runtime_id,
        path=candidate,
        receipt_sha256=receipt_sha256,
        estimated_bytes=_tree_size_without_following(candidate),
    )


def execute_runtime_eviction(
    runtime: RuntimeLock,
    plan: RuntimeEvictionPlan,
    *,
    boundary: Path,
    repository_root: Path,
    expected_receipt_sha256: str,
    orchestration_lock_path: Path,
    confirmed: bool = False,
) -> int:
    """Delete only a revalidated sealed runtime under the global audition lock."""

    if not confirmed:
        raise RuntimeUnavailableError(
            "runtime eviction requires explicit confirmation after reviewing the receipt digest"
        )
    if expected_receipt_sha256 != plan.receipt_sha256:
        raise ContractError("runtime eviction receipt digest does not match the reviewed plan")
    root = boundary.expanduser().resolve(strict=True)
    expected_lock = root / "audition-orchestrator.lock"
    lock_path = Path(os.path.abspath(orchestration_lock_path.expanduser()))
    if lock_path != expected_lock or lock_path.is_symlink():
        raise SecurityError("runtime eviction requires the fixed audition orchestration lock")
    with exclusive_worker_slot(lock_path, boundary=root):
        current = build_runtime_eviction_plan(
            runtime,
            plan.path,
            boundary=boundary,
            repository_root=repository_root,
        )
        if current != plan:
            raise SecurityError("runtime changed after eviction planning")
        tombstone = plan.path.parent / (
            f".evicting-runtime-{plan.runtime_id}-{plan.receipt_sha256[:12]}-{secrets.token_hex(4)}"
        )
        os.rename(plan.path, tombstone)
        try:
            shutil.rmtree(tombstone)
        except Exception as error:
            raise RuntimeUnavailableError(
                f"runtime eviction was incomplete; inspect private tombstone {tombstone}: {error}"
            ) from error
        parent_descriptor = os.open(plan.path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    return plan.estimated_bytes


def _verify_runtime_install_artifacts(plan: RuntimeInstallPlan) -> None:
    if plan.lab_wheel is None or plan.lab_wheel_sha256 is None:
        raise RuntimeUnavailableError(
            "runtime installation requires an exact reviewed cole-voice-lab wheel and digest"
        )
    _verify_lab_wheel(plan.lab_wheel, plan.lab_wheel_sha256)
    if plan.runtime.reviewed_built_wheels:
        if plan.reviewed_wheelhouse is None:
            raise RuntimeUnavailableError(
                "runtime installation requires its exact reviewed local-wheel wheelhouse"
            )
        verify_reviewed_wheelhouse(plan.runtime, plan.reviewed_wheelhouse)
    if plan.commands:
        verify_python_runtime(plan.runtime.python_runtime, Path(plan.commands[0][0]))


def build_runtime_install_plan(
    runtime: RuntimeLock,
    environment_path: Path,
    *,
    base_python: Path,
    repository_root: Path,
    reviewed_wheelhouse: Path | None = None,
    lab_wheel: Path | None = None,
    lab_wheel_sha256: str | None = None,
) -> RuntimeInstallPlan:
    repository_root = repository_root.expanduser().resolve(strict=True)
    verify_runtime_requirements(runtime, repository_root)
    verify_python_runtime(runtime.python_runtime, base_python)
    locked_base_python = (
        Path(runtime.python_runtime.prefix_path) / runtime.python_runtime.executable_relative_path
    ).resolve(strict=True)
    artifact_lock = repository_root / runtime.artifact_lock_path
    environment_path = environment_path.expanduser().resolve(strict=False)
    if (lab_wheel is None) != (lab_wheel_sha256 is None):
        raise ContractError("lab wheel path and digest must be supplied together")
    normalized_lab_wheel = (
        _verify_lab_wheel(lab_wheel, lab_wheel_sha256)
        if lab_wheel is not None and lab_wheel_sha256 is not None
        else None
    )
    normalized_wheelhouse = (
        verify_reviewed_wheelhouse(runtime, reviewed_wheelhouse)
        if reviewed_wheelhouse is not None
        else None
    )
    artifact_versions = runtime_artifact_versions(runtime, repository_root)
    lab_distribution_version = (
        _lab_wheel_version(normalized_lab_wheel) if normalized_lab_wheel is not None else None
    )
    runtime_python = environment_path / "bin/python"
    wheelhouse_path = str(normalized_wheelhouse or Path(REVIEWED_WHEELHOUSE_PLACEHOLDER))
    lab_path = normalized_lab_wheel or Path(LAB_WHEEL_PLACEHOLDER)
    lab_digest = lab_wheel_sha256 or ("0" * 64)
    locked_install = [
        str(runtime_python),
        "-I",
        "-P",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-compile",
        "--only-binary=:all:",
        "--require-hashes",
    ]
    if runtime.reviewed_built_wheels:
        locked_install.extend(("--find-links", wheelhouse_path))
    locked_install.extend(("-r", str(artifact_lock)))
    commands = (
        (str(locked_base_python), "-I", "-P", "-S", "-m", "venv", str(environment_path)),
        tuple(locked_install),
        (
            str(runtime_python),
            "-I",
            "-P",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-compile",
            "--only-binary=:all:",
            "--no-deps",
            "--require-hashes",
            f"cole-voice-lab @ {lab_path.as_uri()}#sha256={lab_digest}",
        ),
        (str(runtime_python), "-I", "-P", "-m", "pip", "check"),
    )
    return RuntimeInstallPlan(
        runtime,
        repository_root,
        environment_path,
        normalized_wheelhouse,
        normalized_lab_wheel,
        lab_wheel_sha256,
        lab_distribution_version,
        artifact_versions,
        commands,
    )


def _sanitize_reviewed_runtime_files(plan: RuntimeInstallPlan) -> None:
    """Remove only exact locked executable startup preimages after artifact install."""

    verified: list[Path] = []
    for expected in plan.runtime.sanitized_files:
        try:
            path = confined_path(plan.environment_path, expected.path, must_exist=True)
        except FileNotFoundError as error:
            raise RuntimeUnavailableError(
                f"reviewed runtime sanitizer preimage is missing: {expected.path}"
            ) from error
        reject_symlink_components(plan.environment_path, path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SecurityError(f"reviewed runtime sanitizer target is unsafe: {path}")
        digest, size = sha256_file(path)
        if (digest, size) != (expected.sha256, expected.size_bytes):
            raise SecurityError(f"reviewed runtime sanitizer preimage changed: {path}")
        verified.append(path)
    for path in verified:
        path.unlink()
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _validate_runtime_pth(plan.environment_path)


def execute_runtime_install_plan(
    plan: RuntimeInstallPlan,
    *,
    allow_downloads: bool = False,
    runner: CommandRunner = subprocess.run,
) -> tuple[Path, str]:
    if not allow_downloads:
        raise RuntimeUnavailableError(
            "runtime installation is disabled; explicit allow_downloads is required"
        )
    _verify_runtime_install_artifacts(plan)
    assert_disk_floor(
        plan.environment_path,
        minimum_free_bytes=MINIMUM_FREE_BYTES + plan.runtime.estimated_install_bytes,
    )
    if plan.environment_path.exists():
        raise ContractError(f"refusing to install over existing runtime: {plan.environment_path}")
    plan.environment_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = _provision_environment()
    environment.update(
        {
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_ONLY_BINARY": ":all:",
        }
    )
    try:
        for index, command in enumerate(plan.commands):
            _verify_runtime_install_artifacts(plan)
            completed = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3_600,
                env=environment,
            )
            if completed.returncode != 0:
                raise ExternalToolError(
                    f"runtime installation failed for {plan.runtime.runtime_id}: "
                    f"{completed.stderr.strip()}"
                )
            if index == 1:
                _sanitize_reviewed_runtime_files(plan)
            assert_disk_floor(plan.environment_path)
        _verify_runtime_install_artifacts(plan)
        assert_disk_floor(plan.environment_path)
        receipt_path, receipt_sha256 = write_runtime_install_receipt(plan)
        verify_runtime_install_receipt(
            plan.runtime,
            plan.environment_path,
            repository_root=plan.repository_root,
            expected_receipt_sha256=receipt_sha256,
        )
        return receipt_path, receipt_sha256
    except Exception:
        _discard_incomplete_path(plan.environment_path, label="runtime environment")
        raise


def _validate_invocation(invocation: WorkerInvocation) -> None:
    request = invocation.request
    resolved = invocation.resolved
    if request.candidate_id != resolved.candidate.candidate_id:
        raise ContractError("worker request candidate does not match resolved candidate")
    if request.track != resolved.candidate.track:
        raise ContractError("worker request track does not match candidate track")
    expected = (resolved.model_id, resolved.model_revision, resolved.code_revision)
    actual = (request.model_id, request.model_revision, request.code_revision)
    if actual != expected:
        raise ContractError("worker request does not match immutable model/code resolution")
    expected_auxiliary = {source.repository for source in resolved.auxiliary_sources}
    if set(invocation.auxiliary_paths) != expected_auxiliary:
        raise ContractError("worker auxiliary paths do not match resolved source set")


@contextmanager
def exclusive_worker_slot(path: Path, *, boundary: Path) -> Iterator[None]:
    """Serialize work through one fixed private direct-child runtimes lock."""

    root = _normalized_real_directory(boundary, label="runtime lock boundary")
    root_metadata = root.lstat()
    if root_metadata.st_uid != os.getuid() or stat.S_IMODE(root_metadata.st_mode) & 0o077:
        raise SecurityError("runtime lock boundary must be owner-controlled and private")
    absolute = Path(os.path.abspath(path.expanduser()))
    if absolute.parent != root or absolute.name not in {
        "worker.lock",
        "audition-orchestrator.lock",
    }:
        raise SecurityError("runtime lock must be one fixed direct child of the runtimes root")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(absolute, flags, 0o600)
    except OSError as error:
        raise SecurityError(f"cannot safely open runtime lock: {absolute}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            raise SecurityError("runtime lock must be an owner-controlled single-link regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecurityError("runtime lock permissions must deny group and other access")
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            stream.truncate()
            stream.write(f"pid={os.getpid()}\n")
            stream.flush()
            yield
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        # fdopen owns the descriptor unless it failed before entering.
        try:
            os.close(descriptor)
        except OSError:
            pass


def worker_environment(
    invocation: WorkerInvocation, *, code_integrity_sha256: str | None = None
) -> dict[str, str]:
    environment = {
        key: value
        for key in ("HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
        if (value := os.environ.get(key))
    }
    environment["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    openssl = next(
        dependency
        for dependency in invocation.resolved.runtime.python_runtime.dependency_prefixes
        if dependency.formula == "openssl@3"
    )
    code_path = str(invocation.code_path) if invocation.code_path is not None else ""
    environment.update(
        {
            "COLE_VOICE_LAB_MODEL_PATH": str(invocation.model_path),
            "COLE_VOICE_LAB_AUXILIARY_PATHS": json.dumps(
                {key: str(path) for key, path in sorted(invocation.auxiliary_paths.items())},
                sort_keys=True,
            ),
            "COLE_VOICE_LAB_CODE_PATH": code_path,
            "COLE_VOICE_LAB_CODE_INTEGRITY_SHA256": code_integrity_sha256 or "",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTORCH_ENABLE_MPS_FALLBACK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "OPENSSL_CONF": str(Path(openssl.prefix_path) / "etc/openssl@3/openssl.cnf"),
            "OPENSSL_MODULES": str(Path(openssl.prefix_path) / "lib/ossl-modules"),
        }
    )
    return environment


def _verify_invocation_sources(
    invocation: WorkerInvocation,
) -> tuple[SourceVerification, dict[str, SourceVerification], SourceVerification | None]:
    model = verify_local_source(invocation.resolved.model_source, invocation.model_path)
    auxiliary = {
        source.repository: verify_local_source(
            source, invocation.auxiliary_paths[source.repository]
        )
        for source in invocation.resolved.auxiliary_sources
    }
    code = (
        verify_local_source(invocation.resolved.code_source, invocation.code_path)
        if invocation.code_path is not None
        else None
    )
    return model, auxiliary, code


def _normalized_invocation(
    invocation: WorkerInvocation,
    verified: tuple[
        SourceVerification,
        dict[str, SourceVerification],
        SourceVerification | None,
    ],
) -> WorkerInvocation:
    model, auxiliary, code = verified
    return replace(
        invocation,
        model_path=model.path,
        auxiliary_paths={repository: item.path for repository, item in auxiliary.items()},
        code_path=code.path if code is not None else None,
    )


def _cleanup_rejected_worker_artifacts(invocation: WorkerInvocation) -> None:
    output = Path(invocation.request.output_path)
    for candidate in (
        output,
        output.parent / "evidence" / f"{invocation.request.trial_id}.pip-freeze.txt",
        output.parent / "evidence" / f"{invocation.request.trial_id}.model-evidence.json",
        output.parent / "evidence" / f"{invocation.request.trial_id}.executed-code.json",
    ):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
        except OSError:
            pass


def _assert_successful_result_evidence(
    result: ExperimentResult,
    model: SourceVerification,
    auxiliary: Mapping[str, SourceVerification],
    code: SourceVerification | None,
) -> None:
    if not result.ok:
        return
    if result.metadata.get("model_manifest_sha256") != model.manifest_sha256:
        raise SecurityError("worker result does not bind the verified model manifest")
    expected_auxiliary = {
        repository: verification.manifest_sha256 for repository, verification in auxiliary.items()
    }
    if result.metadata.get("auxiliary_manifest_sha256") != expected_auxiliary:
        raise SecurityError("worker result does not bind verified auxiliary manifests")
    expected_code = code.integrity_sha256 if code is not None else None
    if result.metadata.get("code_checkout_integrity_sha256") != expected_code:
        raise SecurityError("worker result does not bind the verified executable checkout")


def run_worker(invocation: WorkerInvocation) -> ExperimentResult:
    """Run exactly one JSON request in a fresh, serialized subprocess."""

    _validate_invocation(invocation)
    assert_disk_floor(Path(invocation.request.output_path).parent)
    if not invocation.runtime_python.is_file():
        raise RuntimeUnavailableError(f"runtime Python is missing: {invocation.runtime_python}")
    command = (
        str(invocation.runtime_python),
        "-I",
        "-P",
        "-m",
        invocation.resolved.candidate.worker_module,
    )
    started = time.monotonic()
    with exclusive_worker_slot(
        invocation.worker_lock_path,
        boundary=invocation.runtime_root,
    ):
        before = _verify_invocation_sources(invocation)
        verified_invocation = _normalized_invocation(invocation, before)
        completed = subprocess.run(
            command,
            input=invocation.request.model_dump_json(),
            check=False,
            capture_output=True,
            text=True,
            timeout=invocation.timeout_seconds,
            env=worker_environment(
                verified_invocation,
                code_integrity_sha256=(
                    before[2].integrity_sha256 if before[2] is not None else None
                ),
            ),
            cwd=None,
        )
        try:
            after = _verify_invocation_sources(invocation)
            if after != before:
                raise SecurityError("model or executable source changed during worker execution")
        except Exception:
            _cleanup_rejected_worker_artifacts(invocation)
            raise
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        raise ExternalToolError(
            f"worker {invocation.request.candidate_id} exited {completed.returncode}: "
            f"{completed.stderr[-2_000:].strip()}"
        )
    try:
        result = ExperimentResult.model_validate_json(completed.stdout)
    except ValueError as error:
        raise ExternalToolError("worker did not emit exactly one valid result object") from error
    if (result.trial_id, result.candidate_id) != (
        invocation.request.trial_id,
        invocation.request.candidate_id,
    ):
        raise ExternalToolError("worker result identity does not match request")
    try:
        _assert_successful_result_evidence(result, before[0], before[1], before[2])
    except Exception:
        _cleanup_rejected_worker_artifacts(invocation)
        raise
    metadata = dict(result.metadata)
    metadata.update(
        {
            "worker_process_seconds": elapsed,
            "worker_process_model": "fresh-subprocess-one-request",
            "worker_stderr_tail": completed.stderr[-1_000:],
        }
    )
    return result.model_copy(update={"metadata": metadata})


def plan_commands(plan: ProvisionPlan | RuntimeInstallPlan) -> tuple[tuple[str, ...], ...]:
    """Expose immutable argv vectors for dry-run/UI presentation."""

    return plan.commands


def source_paths_by_repository(
    sources: Sequence[SourceLock], paths_by_source_id: Mapping[str, Path]
) -> dict[str, Path]:
    try:
        return {source.repository: paths_by_source_id[source.source_id] for source in sources}
    except KeyError as error:
        raise ContractError(f"missing local path for source {error.args[0]}") from error

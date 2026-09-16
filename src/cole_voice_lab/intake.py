"""Fail-closed, content-addressed intake for transferred capture packages."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import ValidationError, field_validator

from .audio import WavInspection, inspect_canonical_wav
from .canonical import (
    canonical_json_bytes,
    open_regular_no_follow,
    sha256_file,
    sha256_stream,
    write_canonical_json,
)
from .capture import MANIFEST_NAME, QC_REPORT_NAME, READY_NAME, ReadyMarker
from .diagnostic_roots import (
    DIAGNOSTIC_SUBJECT_REFUSAL,
    is_physical_test_suite_id,
    read_diagnostic_root_marker,
)
from .errors import ContractError, SecurityError
from .models import CaptureManifest, ContractModel, QcReport, Sha256, StableId, utc_now
from .paths import AssetLayout, confined_path, validate_relative_path

MIN_FREE_BYTES = 10 * 1024**3
MAX_CONTROL_FILE_BYTES = 1024 * 1024
MAX_AUDIO_FILE_BYTES = 8 * 1024**3
RECEIPT_NAME = "intake-receipt.json"
INTAKE_QC_REPORT_NAME = "intake-qc.json"

_SHA256_NAME = re.compile(r"^[0-9a-f]{64}$")


class IntakeReceipt(ContractModel):
    schema_version: Literal["cole-voice-lab.intake-receipt/v2"] = "cole-voice-lab.intake-receipt/v2"
    session_id: StableId
    manifest_sha256: Sha256
    source_qc_report_sha256: Sha256
    intake_qc_report_sha256: Sha256
    accepted_at: datetime
    raw_path: str

    @field_validator("accepted_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acceptance time requires an explicit timezone")
        return value.astimezone(UTC)

    @field_validator("raw_path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)


@dataclass(frozen=True, slots=True)
class ValidatedPackage:
    manifest: CaptureManifest
    manifest_sha256: str
    source_qc_report: QcReport
    source_qc_report_sha256: str
    qc_report: QcReport
    qc_report_sha256: str
    ready: ReadyMarker
    _snapshot: PackageSnapshot


@dataclass(frozen=True, slots=True)
class FileSnapshot:
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


@dataclass(frozen=True, slots=True)
class DirectorySnapshot:
    device: int
    inode: int
    mode: int
    owner: int
    group: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class PackageSnapshot:
    files: dict[str, FileSnapshot]
    directories: dict[str, DirectorySnapshot]


def _read_canonical_model[ContractT: ContractModel](
    path: Path, model_type: type[ContractT]
) -> ContractT:
    metadata = path.lstat()
    if metadata.st_size > MAX_CONTROL_FILE_BYTES:
        raise ContractError(f"control file exceeds {MAX_CONTROL_FILE_BYTES} bytes: {path}")
    try:
        with open_regular_no_follow(path) as stream:
            actual = stream.read(MAX_CONTROL_FILE_BYTES + 1)
        model = model_type.model_validate_json(actual)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"invalid contract file {path}: {error}") from error
    expected = canonical_json_bytes(model) + b"\n"
    if actual != expected:
        raise ContractError(f"JSON evidence is not in canonical form: {path}")
    return model


def _walk_package(package: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for root, child_directories, child_files in os.walk(package, followlinks=False):
        root_path = Path(root)
        for name in child_directories:
            child = root_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SecurityError(f"package directory must not be a link: {child}")
            relative = validate_relative_path(child.relative_to(package).as_posix())
            directories.add(relative)
        for name in child_files:
            child = root_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SecurityError(f"package member is not a regular file: {child}")
            if metadata.st_nlink != 1:
                raise SecurityError(f"hard-linked package member is forbidden: {child}")
            relative = validate_relative_path(child.relative_to(package).as_posix())
            files.add(relative)
    return files, directories


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _snapshot_file(path: Path) -> FileSnapshot:
    before_path = path.lstat()
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise SecurityError(f"package member is not a regular file: {path}")
    if before_path.st_nlink != 1:
        raise SecurityError(f"hard-linked package member is forbidden: {path}")
    with open_regular_no_follow(path) as stream:
        before_descriptor = os.fstat(stream.fileno())
        digest, size_bytes = sha256_stream(stream)
        after_descriptor = os.fstat(stream.fileno())
    after_path = path.lstat()
    expected = _file_identity(before_path)
    if (
        _file_identity(before_descriptor) != expected
        or _file_identity(after_descriptor) != expected
        or _file_identity(after_path) != expected
        or size_bytes != after_path.st_size
    ):
        raise SecurityError(f"package member changed while it was being snapshotted: {path}")
    return FileSnapshot(
        device=after_path.st_dev,
        inode=after_path.st_ino,
        mode=after_path.st_mode,
        links=after_path.st_nlink,
        owner=after_path.st_uid,
        group=after_path.st_gid,
        size_bytes=size_bytes,
        modified_ns=after_path.st_mtime_ns,
        changed_ns=after_path.st_ctime_ns,
        sha256=digest,
    )


def _snapshot_directory(path: Path) -> DirectorySnapshot:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"package directory must not be a link: {path}")
    return DirectorySnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        owner=metadata.st_uid,
        group=metadata.st_gid,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _snapshot_package(package: Path) -> PackageSnapshot:
    files, directories = _walk_package(package)
    directory_snapshots = {".": _snapshot_directory(package)}
    directory_snapshots.update(
        {relative: _snapshot_directory(package / relative) for relative in sorted(directories)}
    )
    file_snapshots = {relative: _snapshot_file(package / relative) for relative in sorted(files)}
    files_after, directories_after = _walk_package(package)
    if files_after != files or directories_after != directories:
        raise SecurityError("capture package membership changed while it was snapshotted")
    if file_snapshots != {
        relative: _snapshot_file(package / relative) for relative in sorted(files_after)
    }:
        raise SecurityError("capture package files changed while it was snapshotted")
    if directory_snapshots != {
        ".": _snapshot_directory(package),
        **{
            relative: _snapshot_directory(package / relative)
            for relative in sorted(directories_after)
        },
    }:
        raise SecurityError("capture package directories changed while it was snapshotted")
    return PackageSnapshot(files=file_snapshots, directories=directory_snapshots)


def _expected_directories(files: set[str]) -> set[str]:
    expected: set[str] = set()
    for relative in files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected.add(parent.as_posix())
            parent = parent.parent
    return expected


def _report_sha256(report: QcReport) -> str:
    return hashlib.sha256(canonical_json_bytes(report) + b"\n").hexdigest()


def _recomputed_qc_report(
    manifest: CaptureManifest,
    manifest_sha256: str,
    inspections: dict[str, WavInspection],
    *,
    generated_at: datetime,
) -> QcReport:
    # Capture applies the first declared room-tone RMS to every utterance. Repeat
    # that exact portable algorithm here; source qc.json is never authoritative.
    room_noise: float | None = None
    for evidence in manifest.files:
        if evidence.kind == "room-tone":
            room_noise = inspections[evidence.path].rms_dbfs
            break
    metrics = tuple(
        inspections[evidence.path].qc_metrics(
            evidence.path,
            noise_rms_dbfs=(room_noise if evidence.kind == "utterance" else None),
            allow_quiet=evidence.kind == "room-tone",
        )
        for evidence in manifest.files
    )
    return QcReport(
        session_id=manifest.session_id,
        manifest_sha256=manifest_sha256,
        generated_at=generated_at,
        status="pass" if all(metric.passed for metric in metrics) else "review",
        metrics=metrics,
    )


def validate_capture_package(
    package: Path, *, allow_intake_receipt: bool = False
) -> ValidatedPackage:
    """Independently verify every byte and contract in a complete package.

    ``allow_intake_receipt`` is reserved for re-validating an already published
    raw package. Incoming packages fail closed if they try to supply that
    intake-owned evidence themselves.
    """

    snapshot_before = _snapshot_package(package)
    files_before = set(snapshot_before.files)
    directories_before = set(snapshot_before.directories) - {"."}
    if READY_NAME not in files_before:
        raise ContractError(f"partial capture has no {READY_NAME} marker: {package}")

    marker = _read_canonical_model(package / READY_NAME, ReadyMarker)
    manifest = _read_canonical_model(package / marker.manifest_path, CaptureManifest)
    source_report = _read_canonical_model(package / marker.qc_report_path, QcReport)

    manifest_sha256, _ = sha256_file(package / marker.manifest_path)
    source_report_sha256, _ = sha256_file(package / marker.qc_report_path)
    if manifest_sha256 != marker.manifest_sha256:
        raise SecurityError("READY manifest digest does not match manifest.json")
    if source_report_sha256 != marker.qc_report_sha256:
        raise SecurityError("READY QC digest does not match qc.json")
    if marker.session_id != manifest.session_id or source_report.session_id != manifest.session_id:
        raise ContractError("session IDs disagree across capture evidence")
    if source_report.manifest_sha256 != manifest_sha256:
        raise SecurityError("QC report is not bound to the accepted manifest")
    if "umik-1" not in manifest.capture_device.display_name.casefold():
        raise ContractError("capture manifest does not identify the required UMIK-1 microphone")

    declared_files = {entry.path for entry in manifest.files}
    expected_files = declared_files | {MANIFEST_NAME, QC_REPORT_NAME, READY_NAME}
    if allow_intake_receipt:
        expected_files.update({INTAKE_QC_REPORT_NAME, RECEIPT_NAME})
    if files_before != expected_files:
        missing_files = sorted(expected_files - files_before)
        undeclared_files = sorted(files_before - expected_files)
        raise SecurityError(
            f"package files differ from declarations; missing={missing_files}, "
            f"undeclared={undeclared_files}"
        )
    expected_directories = _expected_directories(expected_files)
    if directories_before != expected_directories:
        raise SecurityError(
            "package directories differ from declared path parents; "
            f"missing={sorted(expected_directories - directories_before)}, "
            f"undeclared={sorted(directories_before - expected_directories)}"
        )

    metric_paths = {metric.path for metric in source_report.metrics}
    if metric_paths != declared_files or len(metric_paths) != len(source_report.metrics):
        raise ContractError("QC metrics must cover every declared recording exactly once")
    inspections: dict[str, WavInspection] = {}
    for evidence in manifest.files:
        recording = confined_path(package, evidence.path, must_exist=True)
        if evidence.size_bytes > MAX_AUDIO_FILE_BYTES:
            raise ContractError(f"recording exceeds intake size limit: {evidence.path}")
        inspection = inspect_canonical_wav(recording)
        inspections[evidence.path] = inspection
        if inspection.sha256 != evidence.sha256 or inspection.size_bytes != evidence.size_bytes:
            raise SecurityError(f"recording hash or byte count was tampered: {evidence.path}")
        if inspection.audio != evidence.audio:
            raise ContractError(f"recording format evidence disagrees: {evidence.path}")

    persisted_intake_report: QcReport | None = None
    if allow_intake_receipt:
        persisted_intake_report = _read_canonical_model(package / INTAKE_QC_REPORT_NAME, QcReport)
        report = _recomputed_qc_report(
            manifest,
            manifest_sha256,
            inspections,
            generated_at=persisted_intake_report.generated_at,
        )
        if persisted_intake_report != report:
            raise SecurityError(
                "persisted intake QC does not match independently recomputed WAV metrics"
            )
        report_sha256, _ = sha256_file(package / INTAKE_QC_REPORT_NAME)
        receipt = _read_receipt(package)
        if (
            receipt.session_id != manifest.session_id
            or receipt.manifest_sha256 != manifest_sha256
            or receipt.source_qc_report_sha256 != source_report_sha256
            or receipt.intake_qc_report_sha256 != report_sha256
        ):
            raise SecurityError("published intake receipt disagrees with raw capture evidence")
    else:
        report = _recomputed_qc_report(
            manifest,
            manifest_sha256,
            inspections,
            generated_at=marker.finalized_at,
        )
        report_sha256 = _report_sha256(report)

    snapshot_after = _snapshot_package(package)
    if snapshot_after != snapshot_before:
        raise SecurityError("capture package identities or bytes changed while being validated")
    return ValidatedPackage(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        source_qc_report=source_report,
        source_qc_report_sha256=source_report_sha256,
        qc_report=report,
        qc_report_sha256=report_sha256,
        ready=marker,
        _snapshot=snapshot_after,
    )


def _same_filesystem(layout: AssetLayout) -> None:
    devices = {layout.directory(name).stat().st_dev for name in ("incoming", "tmp", "raw")}
    if len(devices) != 1:
        raise SecurityError("incoming, staging, and raw directories must share one filesystem")


def _existing_session_digest(raw_directory: Path, session_id: str) -> str | None:
    found: str | None = None
    for candidate in raw_directory.iterdir():
        if not candidate.is_dir() or candidate.is_symlink():
            raise SecurityError(f"unsafe member in content-addressed raw store: {candidate}")
        if not _SHA256_NAME.fullmatch(candidate.name):
            raise SecurityError(f"unexpected directory in content-addressed raw store: {candidate}")
        manifest_path = candidate / MANIFEST_NAME
        manifest = _read_canonical_model(manifest_path, CaptureManifest)
        if manifest.session_id == session_id:
            if found is not None and found != candidate.name:
                raise SecurityError(f"multiple raw manifests claim session ID {session_id}")
            found = candidate.name
    return found


def _read_receipt(raw_package: Path) -> IntakeReceipt:
    return _read_canonical_model(raw_package / RECEIPT_NAME, IntakeReceipt)


def _make_read_only(root: Path, *, protect_root: bool = True) -> None:
    for current_root, directories, files in os.walk(root, topdown=False, followlinks=False):
        current = Path(current_root)
        for name in files:
            child = current / name
            metadata = child.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise SecurityError(f"cannot protect unsafe raw member: {child}")
            os.chmod(child, 0o400)
        for name in directories:
            child = current / name
            if child.is_symlink() or not child.is_dir():
                raise SecurityError(f"cannot protect unsafe raw directory: {child}")
            os.chmod(child, 0o500)
    os.chmod(root, 0o500 if protect_root else 0o700)


def _matches_file_snapshot(metadata: os.stat_result, expected: FileSnapshot) -> bool:
    return _file_identity(metadata) == (
        expected.device,
        expected.inode,
        expected.mode,
        expected.links,
        expected.owner,
        expected.group,
        expected.size_bytes,
        expected.modified_ns,
        expected.changed_ns,
    )


def _copy_to_fresh_inodes(
    source: Path,
    destination: Path,
    snapshot: PackageSnapshot,
) -> None:
    """Materialize a validated package without retaining uploader-owned inodes."""

    destination_metadata = destination.lstat()
    if (
        stat.S_ISLNK(destination_metadata.st_mode)
        or not stat.S_ISDIR(destination_metadata.st_mode)
        or any(destination.iterdir())
    ):
        raise SecurityError(f"fresh intake staging directory is unsafe: {destination}")
    for relative in sorted(
        (path for path in snapshot.directories if path != "."),
        key=lambda path: (len(PurePosixPath(path).parts), path),
    ):
        (destination / relative).mkdir(mode=0o700)

    for relative, expected in sorted(snapshot.files.items()):
        source_path = source / relative
        if _snapshot_file(source_path) != expected:
            raise SecurityError(f"source package changed before secure copy: {relative}")
        destination_path = destination / relative
        digest = hashlib.sha256()
        size_bytes = 0
        with open_regular_no_follow(source_path) as reader:
            before = os.fstat(reader.fileno())
            if not _matches_file_snapshot(before, expected):
                raise SecurityError(f"source identity changed before secure copy: {relative}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(destination_path, flags, 0o600)
            descriptor_owned = True
            try:
                writer = os.fdopen(descriptor, "wb", closefd=True)
                descriptor_owned = False
                with writer:
                    while chunk := reader.read(1024 * 1024):
                        digest.update(chunk)
                        size_bytes += len(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
            except Exception:
                if descriptor_owned:
                    os.close(descriptor)
                destination_path.unlink(missing_ok=True)
                raise
            after = os.fstat(reader.fileno())
        if (
            not _matches_file_snapshot(after, expected)
            or _snapshot_file(source_path) != expected
            or digest.hexdigest() != expected.sha256
            or size_bytes != expected.size_bytes
        ):
            raise SecurityError(f"source package changed during secure copy: {relative}")
    if _snapshot_package(source) != snapshot:
        raise SecurityError("source package changed across secure materialization")


def _make_cleanup_writable(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise SecurityError(f"cleanup root must be a real directory: {root}")
    os.chmod(root, 0o700)
    for current_root, directories, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in directories:
            child = current / name
            if child.is_symlink() or not child.is_dir():
                raise SecurityError(f"unsafe cleanup directory: {child}")
            os.chmod(child, 0o700)
        for name in files:
            child = current / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SecurityError(f"unsafe cleanup member: {child}")
            os.chmod(child, 0o600)


def _discard_verified_duplicate(stage: Path, tmp_root: Path) -> None:
    try:
        stage.relative_to(tmp_root)
    except ValueError as error:
        raise SecurityError(f"refusing to remove staging path outside tmp: {stage}") from error
    _walk_package(stage)
    shutil.rmtree(stage)


def _require_intake_root(layout: AssetLayout, manifest: CaptureManifest) -> None:
    """Guard #5: physical-test packages enter only their own marked diagnostic root."""
    marker = read_diagnostic_root_marker(layout)
    physical_test = is_physical_test_suite_id(manifest.prompt_suite_id)
    if marker is None:
        if physical_test:
            raise ContractError(
                "physical-test capture packages are accepted only by their marked diagnostic root"
            )
        return
    if not physical_test:
        raise ContractError(
            "this asset root is marked for physical tests; it accepts no other packages"
        )
    if manifest.subject_id != marker.subject_id:
        raise ContractError(DIAGNOSTIC_SUBJECT_REFUSAL)


def accept_capture_session(
    layout: AssetLayout,
    incoming_session: Path,
    *,
    min_free_bytes: int = MIN_FREE_BYTES,
    available_bytes: int | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> IntakeReceipt:
    """Atomically claim, validate, and publish one complete capture package."""

    if min_free_bytes < 0:
        raise ValueError("minimum free bytes cannot be negative")
    incoming_root = layout.directory("incoming").resolve(strict=True)
    raw_root = layout.directory("raw").resolve(strict=True)
    tmp_root = layout.directory("tmp").resolve(strict=True)
    source = incoming_session.expanduser()
    try:
        if source.parent.resolve(strict=True) != incoming_root:
            raise SecurityError("intake accepts only direct children of the incoming directory")
    except FileNotFoundError as error:
        raise SecurityError(f"incoming session path is unavailable: {source}") from error
    source_metadata = source.lstat()
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISDIR(source_metadata.st_mode):
        raise SecurityError(f"incoming session must be a real directory: {source}")
    _same_filesystem(layout)
    free_bytes = shutil.disk_usage(layout.root).free if available_bytes is None else available_bytes
    if free_bytes < min_free_bytes:
        raise SecurityError(
            f"intake disk floor not met: {free_bytes} bytes free, {min_free_bytes} required"
        )

    stage = tmp_root / f".intake-{source.name}-{os.getpid()}"
    if stage.exists():
        raise SecurityError(f"intake staging path already exists: {stage}")
    os.replace(source, stage)
    published = False
    publish_stage: Path | None = None
    try:
        package = validate_capture_package(stage)
        _require_intake_root(layout, package.manifest)
        if source.name != package.manifest.session_id:
            raise ContractError(
                "incoming directory name must exactly match the manifest session ID"
            )
        collision = _existing_session_digest(raw_root, package.manifest.session_id)
        if collision is not None and collision != package.manifest_sha256:
            raise SecurityError(
                f"session ID collision: {package.manifest.session_id} "
                f"already has manifest {collision}"
            )
        destination = raw_root / package.manifest_sha256
        if destination.exists():
            existing = validate_capture_package(destination, allow_intake_receipt=True)
            receipt = _read_receipt(destination)
            if (
                existing.manifest != package.manifest
                or existing.source_qc_report_sha256 != package.source_qc_report_sha256
                or existing.ready != package.ready
            ):
                raise SecurityError("existing raw evidence disagrees with transport duplicate")
            _discard_verified_duplicate(stage, tmp_root)
            published = True
            return receipt

        accepted_at = clock()
        intake_report = package.qc_report.model_copy(update={"generated_at": accepted_at})
        publish_stage = Path(tempfile.mkdtemp(prefix=".intake-publish-", dir=tmp_root))
        os.chmod(publish_stage, 0o700)
        _copy_to_fresh_inodes(stage, publish_stage, package._snapshot)
        intake_report_sha256 = write_canonical_json(
            publish_stage / INTAKE_QC_REPORT_NAME,
            intake_report,
        )
        receipt = IntakeReceipt(
            session_id=package.manifest.session_id,
            manifest_sha256=package.manifest_sha256,
            source_qc_report_sha256=package.source_qc_report_sha256,
            intake_qc_report_sha256=intake_report_sha256,
            accepted_at=accepted_at,
            raw_path=f"raw/{package.manifest_sha256}",
        )
        write_canonical_json(publish_stage / RECEIPT_NAME, receipt)
        final_package = validate_capture_package(
            publish_stage,
            allow_intake_receipt=True,
        )
        if (
            final_package.manifest != package.manifest
            or final_package.source_qc_report != package.source_qc_report
            or final_package.qc_report != intake_report
        ):
            raise SecurityError("secure intake copy disagrees with validated source package")
        # Darwin requires the moved directory itself to remain writable for the
        # cross-parent rename. Protect every child first, publish atomically, then
        # remove write permission from the published root immediately afterward.
        _make_read_only(publish_stage, protect_root=False)
        validate_capture_package(publish_stage, allow_intake_receipt=True)
        os.rename(publish_stage, destination)
        publish_stage = None
        published = True
        _make_read_only(destination)
        directory_descriptor = os.open(raw_root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        _discard_verified_duplicate(stage, tmp_root)
        return receipt
    finally:
        if publish_stage is not None and publish_stage.exists():
            _make_cleanup_writable(publish_stage)
            _discard_verified_duplicate(publish_stage, tmp_root)
        if not published and stage.exists() and not source.exists():
            os.replace(stage, source)


# Stable short alias for CLI/library callers.
intake_session = accept_capture_session

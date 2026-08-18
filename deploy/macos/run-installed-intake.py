#!/usr/bin/python3
"""Verify one sealed mini release before every intake scan.

This file intentionally uses only Python 3.9-compatible standard-library APIs
because macOS launchd invokes the durable deployment copy with /usr/bin/python3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

SCHEMA_VERSION = "cole-voice-lab.mini-install-receipt/v2"
TREE_SCHEMA_VERSION = "cole-voice-lab.mini-install-tree/v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_NAME = "INSTALL_RECEIPT.json"
EXPECTED_RECEIPT_KEYS = {
    "distribution",
    "editable",
    "executable",
    "install_tree",
    "python_runtime",
    "release_root",
    "schema_version",
    "wheel_sha256",
}
EXPECTED_EXECUTABLE_KEYS = {"path", "sha256", "size_bytes"}
EXPECTED_TREE_KEYS = {"entry_count", "schema_version", "sha256"}
EXPECTED_RUNTIME_KEYS = {"launcher_path", "resolved_path", "sha256", "size_bytes"}
PYTHON_LAUNCHER_NAME = re.compile(r"^python(?:3(?:\.\d+)?)?$")


def _fail(message: str) -> None:
    raise SystemExit(message)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _absolute_without_symlinks(path: Path, label: str) -> Path:
    if not path.is_absolute():
        _fail(f"{label} must be absolute")
    absolute = Path(os.path.abspath(path))
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor /= component
        try:
            metadata = os.lstat(cursor)
        except OSError as error:
            _fail(f"cannot inspect {label} path component {cursor}: {error}")
        if stat.S_ISLNK(metadata.st_mode):
            _fail(f"{label} contains a symlink component: {cursor}")
    return absolute


def _regular_owned_file(path: Path, label: str, *, executable: bool) -> os.stat_result:
    absolute = _absolute_without_symlinks(path, label)
    try:
        metadata = os.lstat(absolute)
    except OSError as error:
        _fail(f"cannot inspect {label}: {error}")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"{label} must be a regular file")
    if metadata.st_uid != os.getuid():
        _fail(f"{label} must be owned by the current user")
    if metadata.st_nlink != 1:
        _fail(f"{label} must not be hardlinked")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022:
        _fail(f"{label} must not be group/world writable")
    if executable and (not mode & 0o100 or not os.access(absolute, os.X_OK)):
        _fail(f"{label} must be executable by its owner")
    return metadata


def _owned_directory(path: Path, label: str) -> Path:
    absolute = _absolute_without_symlinks(path, label)
    metadata = os.lstat(absolute)
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(f"{label} must be a real directory")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        _fail(f"{label} must be user-owned and not group/world writable")
    return absolute


def _digest_file(path: Path, label: str, *, executable: bool) -> tuple[str, int]:
    before = _regular_owned_file(path, label, executable=executable)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        _fail(f"cannot open {label}: {error}")
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            _fail(f"{label} changed before it could be verified")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_opened = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if (
        identity_before != identity_opened
        or identity_opened != identity_after
        or size != after.st_size
    ):
        _fail(f"{label} changed while it was being verified")
    return digest.hexdigest(), size


def _external_runtime_digest(path: Path) -> tuple[str, int]:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        _fail("installed Python runtime must be a regular executable")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        _fail("installed Python runtime must not be group/world writable")
    descriptor = os.open(path, os.O_RDONLY)
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or size != after.st_size:
        _fail("installed Python runtime changed while it was being verified")
    return digest.hexdigest(), size


def _runtime_symlink_entry(
    path: Path,
    relative: str,
    *,
    release: Path,
    runtime_resolved: Path,
) -> dict[str, object]:
    metadata = os.lstat(path)
    if metadata.st_uid != os.getuid():
        _fail(f"release symlink must be user-owned: {relative}")
    if path.parent != release / "bin" or PYTHON_LAUNCHER_NAME.fullmatch(path.name) is None:
        _fail(f"release symlink is forbidden outside the Python launcher set: {relative}")
    try:
        actual_resolved = path.resolve(strict=True)
    except OSError as error:
        _fail(f"cannot resolve Python launcher symlink {relative}: {error}")
    if actual_resolved != runtime_resolved:
        _fail(f"Python launcher symlink does not resolve to the sealed runtime: {relative}")
    return {"kind": "symlink", "path": relative, "target": os.readlink(path)}


def _tree_evidence(release: Path, *, runtime_resolved: Path) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for directory, names, files in os.walk(release, topdown=True, followlinks=False):
        base = Path(directory)
        kept_directories: list[str] = []
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(release).as_posix()
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode):
                entries.append(
                    _runtime_symlink_entry(
                        path,
                        relative,
                        release=release,
                        runtime_resolved=runtime_resolved,
                    )
                )
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                _fail(f"unsupported release entry type: {relative}")
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid != os.getuid() or mode & 0o022:
                _fail(
                    f"release directory must be user-owned and not group/world writable: {relative}"
                )
            entries.append({"kind": "directory", "mode": mode, "path": relative})
            kept_directories.append(name)
        names[:] = kept_directories

        for name in sorted(files):
            path = base / name
            relative = path.relative_to(release).as_posix()
            if relative == RECEIPT_NAME:
                continue
            if relative.startswith(f".{RECEIPT_NAME}.tmp-"):
                _fail("stale install-receipt temporary file is forbidden")
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode):
                entries.append(
                    _runtime_symlink_entry(
                        path,
                        relative,
                        release=release,
                        runtime_resolved=runtime_resolved,
                    )
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _fail(f"unsupported release entry type: {relative}")
            digest, size = _digest_file(path, f"release file {relative}", executable=False)
            entries.append(
                {
                    "kind": "file",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "path": relative,
                    "sha256": digest,
                    "size_bytes": size,
                }
            )

    entries.sort(key=lambda item: str(item["path"]))
    return {
        "schema_version": TREE_SCHEMA_VERSION,
        "entry_count": len(entries),
        "sha256": _canonical_sha256(entries),
    }


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _valid_positive_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _load_receipt(path: Path, expected_sha256: str) -> tuple[Path, Path]:
    if SHA256.fullmatch(expected_sha256) is None:
        _fail("expected install-receipt SHA-256 must be 64 lowercase hexadecimal characters")
    actual_sha256, size = _digest_file(path, "install receipt", executable=False)
    if actual_sha256 != expected_sha256:
        _fail("install-receipt SHA-256 mismatch")
    if size > 1024 * 1024:
        _fail("install receipt exceeds 1 MiB")
    try:
        payload = path.read_bytes()
        receipt = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(f"install receipt is not valid UTF-8 JSON: {error}")
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != expected_sha256:
        _fail("install receipt changed after it was verified")
    if not isinstance(receipt, dict) or set(receipt) != EXPECTED_RECEIPT_KEYS:
        _fail("install receipt has an unexpected key set")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        _fail("install receipt schema is unsupported")
    if receipt.get("distribution") != "cole-voice-lab" or receipt.get("editable") is not False:
        _fail("install receipt must describe a non-editable cole-voice-lab install")
    wheel_sha256 = receipt.get("wheel_sha256")
    if not _valid_sha256(wheel_sha256):
        _fail("install receipt wheel SHA-256 is invalid")

    receipt_path = Path(os.path.abspath(path))
    release = receipt_path.parent
    release_root = receipt.get("release_root")
    if (
        receipt_path.name != RECEIPT_NAME
        or release.name != wheel_sha256
        or release.parent.name != "releases"
        or not isinstance(release_root, str)
        or release_root != str(release)
    ):
        _fail("install receipt is not bound to its content-addressed release root")
    _owned_directory(release, "release directory")
    _owned_directory(release / "bin", "release executable directory")

    executable_record = receipt.get("executable")
    if (
        not isinstance(executable_record, dict)
        or set(executable_record) != EXPECTED_EXECUTABLE_KEYS
    ):
        _fail("install receipt executable evidence is invalid")
    executable_path = executable_record.get("path")
    executable_sha256 = executable_record.get("sha256")
    executable_size = executable_record.get("size_bytes")
    if not isinstance(executable_path, str):
        _fail("install receipt executable path is invalid")
    executable = _absolute_without_symlinks(Path(executable_path), "voice-lab executable")
    if executable != release / "bin" / "voice-lab":
        _fail("install receipt executable must be releases/<wheel-sha256>/bin/voice-lab")
    if not _valid_sha256(executable_sha256) or not _valid_positive_int(executable_size):
        _fail("install receipt executable digest or size is invalid")
    actual_executable_sha256, actual_executable_size = _digest_file(
        executable,
        "voice-lab executable",
        executable=True,
    )
    if actual_executable_sha256 != executable_sha256 or actual_executable_size != executable_size:
        _fail("installed voice-lab executable does not match its receipt")

    runtime_record = receipt.get("python_runtime")
    if not isinstance(runtime_record, dict) or set(runtime_record) != EXPECTED_RUNTIME_KEYS:
        _fail("install receipt Python-runtime evidence is invalid")
    launcher_path = runtime_record.get("launcher_path")
    resolved_path = runtime_record.get("resolved_path")
    runtime_sha256 = runtime_record.get("sha256")
    runtime_size = runtime_record.get("size_bytes")
    if not isinstance(launcher_path, str) or not isinstance(resolved_path, str):
        _fail("install receipt Python-runtime paths are invalid")
    launcher_python = Path(launcher_path)
    if not launcher_python.is_absolute() or launcher_python.parent != release / "bin":
        _fail("installed Python launcher must be inside the sealed release bin directory")
    try:
        actual_resolved = launcher_python.resolve(strict=True)
    except OSError as error:
        _fail(f"cannot resolve installed Python runtime: {error}")
    if str(actual_resolved) != resolved_path:
        _fail("installed Python runtime resolves to a different executable")
    if not _valid_sha256(runtime_sha256) or not _valid_positive_int(runtime_size):
        _fail("install receipt Python-runtime digest or size is invalid")
    actual_runtime_sha256, actual_runtime_size = _external_runtime_digest(actual_resolved)
    if actual_runtime_sha256 != runtime_sha256 or actual_runtime_size != runtime_size:
        _fail("installed Python runtime does not match its receipt")

    tree_record = receipt.get("install_tree")
    if not isinstance(tree_record, dict) or set(tree_record) != EXPECTED_TREE_KEYS:
        _fail("install receipt tree evidence is invalid")
    if tree_record.get("schema_version") != TREE_SCHEMA_VERSION:
        _fail("install receipt tree schema is unsupported")
    entry_count = tree_record.get("entry_count")
    tree_sha256 = tree_record.get("sha256")
    if not _valid_positive_int(entry_count) or not _valid_sha256(tree_sha256):
        _fail("install receipt tree digest or entry count is invalid")
    actual_tree = _tree_evidence(release, runtime_resolved=actual_resolved)
    if actual_tree != tree_record:
        _fail("installed release tree does not match its receipt")
    return release, executable


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-receipt-sha256", required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    options = parser.parse_args(argv)

    _, executable = _load_receipt(options.receipt, options.expected_receipt_sha256)
    asset_root = _absolute_without_symlinks(options.asset_root, "asset root")
    if options.verify_only:
        return 0
    for name in (
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    ):
        os.environ.pop(name, None)
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.execv(
        executable,
        [str(executable), "intake", "scan", "--asset-root", str(asset_root)],
    )
    return 70


if __name__ == "__main__":
    sys.exit(main())

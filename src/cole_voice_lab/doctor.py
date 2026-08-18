"""Read-only host and asset-root readiness diagnostics."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import JsonValue

from .canonical import sha256_file
from .errors import VoiceLabError
from .paths import AssetLayout


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    check_id: str
    status: str
    message: str
    details: dict[str, JsonValue]

    def to_json(self) -> dict[str, JsonValue]:
        return asdict(self)


def _tool_version(binary: str, arguments: tuple[str, ...]) -> DoctorCheck:
    path = shutil.which(binary)
    if path is None:
        return DoctorCheck(binary, "fail", f"{binary} is not on PATH", {})
    try:
        completed = subprocess.run(
            (path, *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return DoctorCheck(binary, "fail", f"cannot execute {binary}: {error}", {"path": path})
    output = (completed.stdout or completed.stderr).splitlines()
    first_line = output[0] if output else "version output unavailable"
    status = "pass" if completed.returncode == 0 else "review"
    return DoctorCheck(binary, status, first_line, {"path": path})


def _rsync_features() -> DoctorCheck:
    path = shutil.which("rsync")
    if path is None:
        return DoctorCheck("rsync", "fail", "rsync is not on PATH", {})
    try:
        version = subprocess.run(
            (path, "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        help_result = subprocess.run(
            (path, "--help"),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return DoctorCheck("rsync", "fail", f"cannot inspect rsync: {error}", {"path": path})
    output = f"{help_result.stdout}\n{help_result.stderr}"
    required = ("--partial-dir", "--delay-updates", "--checksum", "--chmod", "--timeout")
    missing = tuple(option for option in required if option not in output)
    version_line = (version.stdout or version.stderr).splitlines()
    identity = version_line[0] if version_line else "version output unavailable"
    ok = version.returncode == 0 and help_result.returncode == 0 and not missing
    return DoctorCheck(
        "rsync",
        "pass" if ok else "fail",
        identity
        if ok
        else f"sender lacks required features: {', '.join(missing) or 'probe failed'}",
        {"path": path, "required_options": list(required), "missing_options": list(missing)},
    )


def _rrsync_receiver() -> DoctorCheck:
    path = shutil.which("rrsync")
    if path is None:
        return DoctorCheck(
            "rrsync",
            "fail",
            "write-only receiver is not installed; transfer deployment is not ready",
            {},
        )
    candidate = Path(path)
    if not candidate.is_file() or not candidate.stat().st_mode & 0o111:
        return DoctorCheck("rrsync", "fail", "rrsync is not an executable file", {"path": path})
    try:
        resolved = candidate.resolve(strict=True)
        help_result = subprocess.run(
            (path, "-h"),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        digest, _ = sha256_file(resolved)
    except (OSError, subprocess.TimeoutExpired, VoiceLabError) as error:
        return DoctorCheck("rrsync", "fail", f"cannot inspect rrsync: {error}", {"path": path})
    help_output = f"{help_result.stdout}\n{help_result.stderr}"
    required = ("-wo", "-no-del")
    missing = tuple(option for option in required if option not in help_output)
    if help_result.returncode != 0 or missing:
        return DoctorCheck(
            "rrsync",
            "fail",
            f"receiver lacks required restrictions: {', '.join(missing) or 'probe failed'}",
            {
                "path": path,
                "resolved_path": str(resolved),
                "sha256": digest,
                "missing_options": list(missing),
            },
        )
    return DoctorCheck(
        "rrsync",
        "review",
        "rrsync exists; pin and denial-test it before enabling the intake key",
        {
            "path": path,
            "resolved_path": str(resolved),
            "sha256": digest,
            "required_options": list(required),
            "missing_options": [],
        },
    )


def disk_check(path: Path, *, minimum_free_gib: float = 10.0) -> DoctorCheck:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    usage = shutil.disk_usage(existing)
    free_gib = usage.free / (1024**3)
    status = "pass" if free_gib >= minimum_free_gib else "fail"
    return DoctorCheck(
        "disk",
        status,
        f"{free_gib:.1f} GiB free; required floor is {minimum_free_gib:.1f} GiB",
        {
            "path": str(existing),
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "minimum_free_bytes": int(minimum_free_gib * (1024**3)),
        },
    )


def run_doctor(asset_root: Path | None = None) -> tuple[DoctorCheck, ...]:
    checks: list[DoctorCheck] = []
    python_ok = sys.version_info >= (3, 12)
    checks.append(
        DoctorCheck(
            "python",
            "pass" if python_ok else "fail",
            platform.python_version(),
            {"executable": sys.executable},
        )
    )
    checks.extend(
        (
            _tool_version("ffmpeg", ("-version",)),
            _tool_version("ffprobe", ("-version",)),
            _rsync_features(),
            _rrsync_receiver(),
            _tool_version("ssh", ("-V",)),
        )
    )
    checks.append(disk_check(asset_root or Path.cwd()))
    checks.append(
        DoctorCheck(
            "host",
            "pass" if platform.system() == "Darwin" and platform.machine() == "arm64" else "review",
            f"{platform.system()} {platform.release()} on {platform.machine()}",
            {"processor": platform.processor()},
        )
    )
    if asset_root is None:
        checks.append(
            DoctorCheck(
                "asset-root",
                "review",
                "no asset root supplied; private-data commands require one",
                {},
            )
        )
    else:
        try:
            layout = AssetLayout.validate(asset_root)
        except (ValueError, OSError, VoiceLabError) as error:
            checks.append(DoctorCheck("asset-root", "fail", str(error), {"path": str(asset_root)}))
        else:
            checks.append(
                DoctorCheck(
                    "asset-root",
                    "pass",
                    "private asset layout is present and permission-restricted",
                    {"path": str(layout.root)},
                )
            )
    return tuple(checks)

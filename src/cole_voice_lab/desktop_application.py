"""Create an exclusive local macOS launcher for the native recording library."""

from __future__ import annotations

import os
import plistlib
import shlex
from pathlib import Path

from .capture import require_legacy_capture_root, require_legacy_capture_suite
from .errors import ContractError
from .paths import AssetLayout, reject_symlink_components
from .resources import load_prompt_suite, resource_path


def create_desktop_application(
    output: Path,
    *,
    python: Path,
    asset_root: Path,
    suite: str = "pilot",
    device: str | None = None,
) -> Path:
    """Create a new .app bound to an existing interpreter and private asset root.

    This is a local launcher, not a portable bundled Python distribution.
    Existing targets are never overwritten. Raises ContractError for invalid
    paths, missing or malformed packaged artwork, or an existing target;
    AssetLayout enforces private-data restrictions.
    """
    layout = require_legacy_capture_root(AssetLayout.validate(asset_root))
    target = Path(os.path.abspath(output.expanduser()))
    interpreter = Path(os.path.abspath(python.expanduser()))
    if target.suffix != ".app" or not target.parent.is_dir():
        raise ContractError("desktop launcher needs a new .app path in an existing directory")
    reject_symlink_components(Path(target.anchor), target)
    if os.path.lexists(target):
        raise ContractError(f"desktop launcher already exists; choose a new path: {target}")
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ContractError(f"desktop launcher Python is not executable: {interpreter}")
    require_legacy_capture_suite(load_prompt_suite(suite))
    icon_path = resource_path("desktop-icon.icns")
    try:
        icon_bytes = icon_path.read_bytes()
    except OSError as error:
        raise ContractError(f"cannot read desktop launcher icon: {icon_path}") from error
    if (
        len(icon_bytes) <= 8
        or icon_bytes[:4] != b"icns"
        or int.from_bytes(icon_bytes[4:8], "big") != len(icon_bytes)
    ):
        raise ContractError(f"desktop launcher icon has an invalid ICNS container: {icon_path}")
    suite_argument = str(Path(suite).resolve()) if Path(suite).is_file() else suite
    command = [
        str(interpreter),
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.cli",
        "desktop",
        "--asset-root",
        str(layout.root),
        "--suite",
        suite_argument,
    ]
    if device is not None:
        command.extend(("--device", device))
    # Shell text is code: quote every argument, including paths chosen by users.
    launcher = "#!/bin/sh\nset -eu\nexport PYTHONDONTWRITEBYTECODE=1\nexec "
    launcher += shlex.join(command) + "\n"
    target.mkdir(mode=0o700)
    contents = target / "Contents"
    executable_directory = contents / "MacOS"
    executable_directory.mkdir(mode=0o700, parents=True)
    resource_directory = contents / "Resources"
    resource_directory.mkdir(mode=0o700)
    with (resource_directory / "ColeVoiceLab.icns").open("xb") as stream:
        stream.write(icon_bytes)
    metadata = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": "ColeVoiceLab",
        "CFBundleIdentifier": "private.cole.voice-lab.desktop",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleIconFile": "ColeVoiceLab.icns",
        "CFBundleName": "Cole Voice Lab",
        "CFBundleDisplayName": "Cole Voice Lab",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Record your selected voice prompts with the UMIK-1.",
    }
    with (contents / "Info.plist").open("xb") as stream:
        plistlib.dump(metadata, stream)
    executable = executable_directory / "ColeVoiceLab"
    with executable.open("x", encoding="utf-8") as stream:
        stream.write(launcher)
    executable.chmod(0o700)
    return target

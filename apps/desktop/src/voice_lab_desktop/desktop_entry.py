"""The Linux desktop entry: a ``.desktop`` file and its icon (design §6.3, D-041).

On Linux the app is the uv tool (``uv tool install … ./apps/desktop``); this adds it to the
desktop's application menu for the user who runs it, nothing system-wide:

- ``$XDG_DATA_HOME/applications/voice-lab-desktop.desktop`` (``~/.local/share`` by default),
  whose ``Exec`` is the installed ``voice-lab-desktop`` by its absolute path, and
- ``$XDG_DATA_HOME/icons/hicolor/256x256/apps/voice-lab-desktop.png``, the lab's icon
  (``cole_voice_lab``'s ``desktop-icon.png`` at 256 px, shipped in this package).

The entry's id is the app's desktop file name (``QGuiApplication.setDesktopFileName``), so a
Wayland compositor matches the window to it, and ``StartupWMClass`` matches X11's. Both
files are written atomically (mode 0644); an existing file with other content is refused
unless ``replace`` is set, and identical content is left alone. macOS has the app bundle
instead (docs/INSTALL-MACOS.md).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final

DESKTOP_ID: Final = "voice-lab-desktop"
ICON_NAME: Final = "voice-lab-desktop"
ICON_SIZE: Final = 256
ICON_RESOURCE: Final = "resources/voice-lab-desktop.png"
#: Characters the Desktop Entry spec reserves in an ``Exec`` argument (it is then quoted).
_RESERVED: Final = set(" \t\n\"'\\><~|&;$*?#()`")


class DesktopEntryError(Exception):
    """The entry cannot be installed; the message says why and what to do."""


@dataclass(frozen=True)
class Written:
    path: Path
    changed: bool


def data_home(environ: Mapping[str, str] = os.environ) -> Path:
    """``$XDG_DATA_HOME`` when it is an absolute path, else ``~/.local/share``."""
    value = environ.get("XDG_DATA_HOME", "")
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / ".local" / "share"


def entry_path(home: Path) -> Path:
    return home / "applications" / f"{DESKTOP_ID}.desktop"


def icon_path(home: Path) -> Path:
    return home / "icons" / "hicolor" / f"{ICON_SIZE}x{ICON_SIZE}" / "apps" / f"{ICON_NAME}.png"


def icon_file() -> Path:
    """The icon this package ships (also the window icon)."""
    return Path(str(files(__package__) / ICON_RESOURCE))


def installed_executable(argv0: str | None = None) -> Path:
    """The ``voice-lab-desktop`` command the entry should run, by its absolute path.

    The one on ``PATH`` (the uv tool's link in ``~/.local/bin``), else the command that is
    running when it is ``voice-lab-desktop``. Raises: ``DesktopEntryError``."""
    found = shutil.which(DESKTOP_ID)
    if found:
        return Path(found).absolute()
    running = Path(argv0 if argv0 is not None else sys.argv[0])
    if running.name == DESKTOP_ID and running.exists():
        return running.absolute()
    raise DesktopEntryError(
        f"{DESKTOP_ID} is not on PATH: install the uv tool first "
        "(uv tool install --python 3.12 --with-executables-from cole-voice-lab ./apps/desktop), "
        "then run voice-lab-desktop --install-desktop-entry"
    )


def exec_argument(path: Path) -> str:
    """``path`` as an ``Exec`` argument: quoted when it holds a reserved character, ``%``
    doubled (the Desktop Entry spec, "The Exec key")."""
    text = str(path)
    if any(ord(char) < 0x20 or char == "\x7f" for char in text):
        raise DesktopEntryError(f"the command's path has a control character: {text!r}")
    if any(char in _RESERVED for char in text):
        escaped = "".join(f"\\{char}" if char in '"`$\\' else char for char in text)
        text = f'"{escaped}"'
    text = text.replace("%", "%%")
    # The value is a string in the file's escaping too: a backslash is written twice.
    return text.replace("\\", "\\\\")


def render_entry(executable: Path) -> str:
    """The ``.desktop`` file for ``executable``."""
    command = exec_argument(executable)
    return "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.5",
            "Name=voice-lab",
            "GenericName=Voice lab",
            "Comment=Record, upload and study your voice recordings with voice-lab",
            f"Exec={command}",
            f"TryExec={command}",
            f"Icon={ICON_NAME}",
            "Terminal=false",
            "Categories=AudioVideo;Audio;Recorder;",
            "Keywords=voice;recording;microphone;UMIK-1;",
            "StartupNotify=true",
            f"StartupWMClass={DESKTOP_ID}",
            "",
        )
    )


def _write(path: Path, payload: bytes, *, replace: bool) -> Written:
    if path.is_symlink() and not replace:
        raise DesktopEntryError(f"{path} is a symbolic link; pass --replace to replace it")
    if path.exists() and not path.is_symlink():
        if not path.is_file():
            raise DesktopEntryError(f"{path} exists and is not a file")
        if path.read_bytes() == payload:
            return Written(path, changed=False)
        if not replace:
            raise DesktopEntryError(
                f"{path} exists with other content; pass --replace to overwrite it"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return Written(path, changed=True)


def install(
    *, home: Path, executable: Path, replace: bool = False, platform: str = sys.platform
) -> tuple[Written, Written]:
    """Write the icon, then the entry that names it. Raises: ``DesktopEntryError``."""
    if platform == "darwin":
        raise DesktopEntryError(
            "on macOS the app is a bundle, not a desktop entry: see docs/INSTALL-MACOS.md"
        )
    if not executable.is_absolute():
        raise DesktopEntryError(f"the command must be an absolute path: {executable}")
    icon = _write(icon_path(home), icon_file().read_bytes(), replace=replace)
    entry = _write(entry_path(home), render_entry(executable).encode("utf-8"), replace=replace)
    return icon, entry


def main(*, replace: bool, environ: Mapping[str, str] = os.environ) -> int:
    """``voice-lab-desktop --install-desktop-entry [--replace]``."""
    try:
        written = install(
            home=data_home(environ), executable=installed_executable(), replace=replace
        )
    except (DesktopEntryError, OSError) as exc:
        print(f"{DESKTOP_ID}: {exc}", file=sys.stderr)
        return 1
    for item in written:
        print(f"{'wrote' if item.changed else 'unchanged'} {item.path}")
    paths = " ".join(f"'{item.path}'" for item in written)
    print("The menu picks it up at the next login (or now, in most desktops).")
    print(f"To remove it: rm {paths}")
    return 0

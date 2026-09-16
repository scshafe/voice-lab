"""Native desktop application entrypoint; Qt remains an optional dependency."""

from __future__ import annotations

import sys
from pathlib import Path

from .capture import require_legacy_capture_root, require_legacy_capture_suite
from .errors import RuntimeUnavailableError
from .paths import AssetLayout
from .resources import load_prompt_suite, resource_path


def run_desktop(
    *,
    asset_root: Path,
    suite: str = "pilot",
    subject_id: str = "cole-self",
    device: str | None = None,
    ffmpeg: str = "/opt/homebrew/bin/ffmpeg",
    pilot_gate_path: str | None = None,
    pilot_gate_sha256: str | None = None,
) -> int:
    """Open the native recording library for an explicit private asset root.

    Imports Qt only when launching the desktop. Raises RuntimeUnavailableError
    when its optional dependency or packaged artwork is unavailable, or a domain
    error for unsafe assets.
    Opening the window never starts a microphone recording.
    """
    layout = require_legacy_capture_root(AssetLayout.validate(asset_root))
    prompt_suite = require_legacy_capture_suite(load_prompt_suite(suite))
    try:
        from PySide6.QtGui import QIcon, QImage, QPixmap
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        raise RuntimeUnavailableError(
            "native desktop widgets are not installed in this Python environment; "
            "run .venv/bin/python scripts/bootstrap-desktop --execute --allow-downloads "
            "from the development checkout, or install the desktop extra"
        ) from error

    from .desktop_window import DesktopWindow
    from .recording_library import RecordingLibrary

    icon_path = resource_path("desktop-icon.png")
    icon_image = QImage(str(icon_path))
    if icon_image.isNull():
        raise RuntimeUnavailableError(
            f"native desktop icon is missing or invalid: {icon_path}; "
            "restore the packaged desktop artwork before launching"
        )
    application = QApplication(["Cole Voice Lab"])
    application.setApplicationName("Cole Voice Lab")
    application.setApplicationDisplayName("Cole Voice Lab")
    application.setOrganizationName("Cole Voice Lab")
    application_icon = QIcon(QPixmap.fromImage(icon_image))
    application.setWindowIcon(application_icon)
    application.setQuitOnLastWindowClosed(True)
    library = RecordingLibrary(
        layout,
        prompt_suite,
        subject_id,
        ffmpeg=ffmpeg,
        pilot_gate_path=pilot_gate_path,
        pilot_gate_sha256=pilot_gate_sha256,
    )
    window = DesktopWindow(library, requested_device=device)
    # The launcher executes Python, so set the running Dock icon explicitly in
    # addition to the Finder/prelaunch icon stored in the .app's Info.plist.
    window.setWindowIcon(application_icon)
    window.show()
    window.raise_()
    window.activateWindow()
    return application.exec()


if __name__ == "__main__":
    from .cli import main

    raise SystemExit(main(["desktop", *sys.argv[1:]]))

from __future__ import annotations

import builtins
import json
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from cole_voice_lab import desktop, desktop_application
from cole_voice_lab.cli import main
from cole_voice_lab.desktop_application import create_desktop_application
from cole_voice_lab.errors import ContractError, RuntimeUnavailableError, SecurityError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.resources import resource_path


def test_packaged_icon_has_transparent_corners_and_opaque_artwork():
    QtGui = pytest.importorskip("PySide6.QtGui")
    icon = QtGui.QImage(str(resource_path("desktop-icon.png")))
    assert not icon.isNull() and icon.hasAlphaChannel()
    corners = (
        (0, 0),
        (0, icon.height() - 1),
        (icon.width() - 1, 0),
        (icon.width() - 1, icon.height() - 1),
    )
    assert all(icon.pixelColor(x, y).alpha() == 0 for x, y in corners)
    assert icon.pixelColor(icon.width() // 2, icon.height() // 2).alpha() == 255


ROOT = Path(__file__).resolve().parents[1]
INVALID_DESKTOP_ICNS = (
    pytest.param(b"", id="empty"),
    pytest.param(b"icns\x00\x00\x00\x08", id="header-only"),
    pytest.param(b"nope\x00\x00\x00\x09x", id="wrong-magic"),
    pytest.param(b"icns\x00\x00\x00\x10x", id="wrong-container-length"),
)
INVALID_DESKTOP_PNG = b"invalid PNG fixture"


def test_desktop_cli_opens_explicit_library_without_a_session_or_browser(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(desktop, "run_desktop", lambda **kwargs: calls.append(kwargs) or 0)
    before = sys.dont_write_bytecode
    assert (
        main(
            [
                "desktop",
                "--asset-root",
                str(tmp_path),
                "--suite",
                "custom.json",
                "--device",
                "UMIK-1",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "asset_root": tmp_path,
            "suite": "custom.json",
            "subject_id": "cole-self",
            "device": "UMIK-1",
            "ffmpeg": "/opt/homebrew/bin/ffmpeg",
            "pilot_gate_path": None,
            "pilot_gate_sha256": None,
        }
    ]
    assert sys.dont_write_bytecode == before


def test_desktop_cli_restores_bytecode_policy_after_failure(monkeypatch, tmp_path, capsys):
    def fail(**kwargs):
        raise ContractError("cannot open recording library")

    monkeypatch.setattr(desktop, "run_desktop", fail)
    before = sys.dont_write_bytecode
    assert main(["desktop", "--asset-root", str(tmp_path)]) == 2
    assert "cannot open recording library" in capsys.readouterr().err
    assert sys.dont_write_bytecode == before


def test_desktop_missing_optional_dependency_explains_installation(monkeypatch, tmp_path):
    layout = AssetLayout.initialize(tmp_path / "private")
    original_import = builtins.__import__

    def import_without_qt(name, *args, **kwargs):
        if name == "PySide6.QtWidgets":
            raise ImportError("test fixture has no Qt")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_qt)
    with pytest.raises(RuntimeUnavailableError, match="--execute --allow-downloads"):
        desktop.run_desktop(asset_root=layout.root)


def test_desktop_entrypoint_shows_native_window_without_starting_capture(monkeypatch, tmp_path):
    from PySide6 import QtGui, QtWidgets

    from cole_voice_lab import desktop_window

    events = []
    application_icon = object()
    layout = AssetLayout.initialize(tmp_path / "private")

    class Pixmap:
        @staticmethod
        def fromImage(image):
            assert not image.isNull()
            events.append("decoded-icon")
            return image

    def icon_from_pixmap(image):
        assert not image.isNull()
        return application_icon

    class Application:
        def __init__(self, arguments):
            events.append(arguments)

        def setApplicationName(self, value):
            events.append(value)

        def setApplicationDisplayName(self, value):
            assert value == "Cole Voice Lab"
            events.append("display-name")

        def setOrganizationName(self, value):
            events.append(value)

        def setWindowIcon(self, value):
            assert value is application_icon
            events.append("application-icon")

        def setQuitOnLastWindowClosed(self, value):
            assert value is True

        def exec(self):
            return 7

    class Window:
        def __init__(self, library, *, requested_device):
            assert library.layout.root == layout.root
            assert len(library.prompt_suite.prompts) == 36
            assert requested_device == "UMIK-1"

        def show(self):
            events.append("show")

        def setWindowIcon(self, value):
            assert value is application_icon
            events.append("window-icon")

        def raise_(self):
            events.append("raise")

        def activateWindow(self):
            events.append("activate")

    monkeypatch.setattr(QtWidgets, "QApplication", Application)
    monkeypatch.setattr(QtGui, "QPixmap", Pixmap)
    monkeypatch.setattr(QtGui, "QIcon", icon_from_pixmap)
    monkeypatch.setattr(desktop_window, "DesktopWindow", Window)
    assert desktop.run_desktop(asset_root=layout.root, device="UMIK-1") == 7
    assert events.index("display-name") < events.index("show")
    assert events.index("decoded-icon") < events.index("application-icon")
    assert events.index("application-icon") < events.index("window-icon")
    assert events.index("window-icon") < events.index("show")
    assert events[-3:] == ["show", "raise", "activate"]
    assert not tuple(layout.directory("tmp").iterdir())


@pytest.mark.parametrize("missing", (True, False), ids=("missing", "invalid"))
def test_desktop_entrypoint_rejects_missing_or_invalid_artwork_before_gui(
    monkeypatch, tmp_path, missing
):
    from PySide6 import QtWidgets

    layout = AssetLayout.initialize(tmp_path / "private")
    icon_path = tmp_path / "desktop-icon.png"
    if not missing:
        icon_path.write_bytes(INVALID_DESKTOP_PNG)
    monkeypatch.setattr(desktop, "resource_path", lambda name: icon_path)

    def unexpected_application(arguments):
        pytest.fail("invalid artwork must fail before creating the GUI application")

    monkeypatch.setattr(QtWidgets, "QApplication", unexpected_application)
    with pytest.raises(RuntimeUnavailableError, match="desktop icon is missing or invalid"):
        desktop.run_desktop(asset_root=layout.root)
    assert not tuple(layout.directory("tmp").iterdir())


def test_desktop_application_quotes_paths_and_never_overwrites(tmp_path):
    layout = AssetLayout.initialize(tmp_path / "private assets")
    interpreter = tmp_path / "python ' quoted $name `literal`"
    interpreter.write_text("fixture", encoding="utf-8")
    interpreter.chmod(0o700)
    target = tmp_path / "Voice Library.app"
    create_desktop_application(
        target,
        python=interpreter,
        asset_root=layout.root,
        device="UMIK-1",
        suite="pilot",
    )
    with (target / "Contents/Info.plist").open("rb") as stream:
        metadata = plistlib.load(stream)
    executable = target / "Contents/MacOS" / metadata["CFBundleExecutable"]
    command = shlex.split(executable.read_text().split("\nexec ", 1)[1])
    assert command == [
        str(interpreter),
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.cli",
        "desktop",
        "--asset-root",
        str(layout.root),
        "--suite",
        "pilot",
        "--device",
        "UMIK-1",
    ]
    assert metadata["CFBundlePackageType"] == "APPL"
    assert metadata["CFBundleIconFile"] == "ColeVoiceLab.icns"
    assert (target / "Contents/Resources/ColeVoiceLab.icns").read_bytes() == resource_path(
        "desktop-icon.icns"
    ).read_bytes()
    assert executable.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ContractError, match="already exists"):
        create_desktop_application(target, python=interpreter, asset_root=layout.root)


def test_qt_launcher_and_terminal_capture_refuse_v2_catalogs_before_gui_or_devices(
    monkeypatch, tmp_path
):
    import argparse

    from cole_voice_lab import cli

    layout = AssetLayout.initialize(tmp_path / "private")
    catalog = tmp_path / "synthetic-v2.json"
    catalog.write_text(
        '{"schema_version":"cole-voice-lab.prompt-suite/v2","suite_id":"synthetic-v2",'
        '"purpose":"capture-pilot","prompts":[{"prompt_id":"room","category":"room-tone",'
        '"instructions":"Remain silent.","style":"Silence","expected_seconds":10.0}]}'
    )
    original_import = builtins.__import__

    def import_without_qt(name, *args, **kwargs):
        if name.startswith("PySide6"):
            raise AssertionError("v2 refusal must precede Qt")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_qt)
    with pytest.raises(ContractError, match="native recording application"):
        desktop.run_desktop(asset_root=layout.root, suite=str(catalog))
    monkeypatch.setattr(builtins, "__import__", original_import)
    target = tmp_path / "Cole Voice Lab.app"
    with pytest.raises(ContractError, match="native recording application"):
        create_desktop_application(
            target, python=Path(sys.executable), asset_root=layout.root, suite=str(catalog)
        )
    assert not target.exists()
    monkeypatch.setattr(
        cli,
        "list_avfoundation_audio_devices",
        lambda **_: pytest.fail("v2 refusal must precede device discovery"),
    )
    options = argparse.Namespace(
        asset_root=str(layout.root),
        suite=str(catalog),
        ffmpeg="/test/ffmpeg",
        device=None,
        microphone_serial=None,
    )
    with pytest.raises(ContractError, match="native recording application"):
        cli._capture_inputs(options)


def test_desktop_application_rejects_missing_artwork_before_creating_target(monkeypatch, tmp_path):
    layout = AssetLayout.initialize(tmp_path / "private")
    target = tmp_path / "Cole Voice Lab.app"
    icon_path = tmp_path / "missing.icns"
    monkeypatch.setattr(desktop_application, "resource_path", lambda name: icon_path)
    with pytest.raises(ContractError, match="cannot read desktop launcher icon") as failure:
        create_desktop_application(target, python=Path(sys.executable), asset_root=layout.root)
    assert isinstance(failure.value.__cause__, FileNotFoundError)
    assert not target.exists()


@pytest.mark.parametrize("icon_bytes", INVALID_DESKTOP_ICNS)
def test_desktop_application_rejects_malformed_artwork_before_creating_target(
    monkeypatch, tmp_path, icon_bytes
):
    layout = AssetLayout.initialize(tmp_path / "private")
    target = tmp_path / "Cole Voice Lab.app"
    icon_path = tmp_path / "invalid.icns"
    icon_path.write_bytes(icon_bytes)
    monkeypatch.setattr(desktop_application, "resource_path", lambda name: icon_path)
    with pytest.raises(ContractError, match="invalid ICNS container"):
        create_desktop_application(target, python=Path(sys.executable), asset_root=layout.root)
    assert not target.exists()


def test_desktop_application_rejects_unsafe_or_unusable_targets(tmp_path):
    layout = AssetLayout.initialize(tmp_path / "assets")
    python = Path(sys.executable)
    with pytest.raises(ContractError, match=".app path"):
        create_desktop_application(tmp_path / "not-app", python=python, asset_root=layout.root)
    with pytest.raises(ContractError, match="not executable"):
        create_desktop_application(
            tmp_path / "new.app",
            python=tmp_path / "missing",
            asset_root=layout.root,
        )
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SecurityError, match="symlink"):
        create_desktop_application(alias / "new.app", python=python, asset_root=layout.root)


def test_desktop_bootstrap_requires_explicit_download_gate():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/bootstrap-desktop"), "--execute"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--execute --allow-downloads" in result.stderr


def test_desktop_bootstrap_is_a_hash_pinned_preview_by_default():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/bootstrap-desktop")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert '"execution_requested": false' in result.stdout
    assert '"--require-hashes"' in result.stdout
    assert "vault-macos.txt" not in result.stdout


def test_desktop_bootstrap_adds_the_vault_lock_only_when_asked():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/bootstrap-desktop"), "--with-vault"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    command = json.loads(result.stdout)["command"]
    assert command[-2:] == ["--requirement", str(ROOT / "requirements/vault-macos.txt")]
    assert "--require-hashes" in command and "--no-deps" in command
    assert "--only-binary=:all:" in command


def test_native_desktop_module_exposes_cli_help_without_opening_window():
    result = subprocess.run(
        [sys.executable, "-B", "-m", "cole_voice_lab.desktop", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--asset-root" in result.stdout
    assert "--open" not in result.stdout

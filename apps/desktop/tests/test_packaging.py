"""Packaging (design §6.3, D-041): the Linux desktop entry and the macOS bundle's build.

The bundle is built on macOS only (the owner's, on the Air); here the build script's plan,
refusals, generated spec and Info.plist, and its build sequence over a stand-in runner are
checked, and the hash-locked requirements file's format.
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from voice_lab_desktop import cli, desktop_entry
from voice_lab_desktop.desktop_entry import DesktopEntryError

from conftest import REPOSITORY

BUILD_SCRIPT = REPOSITORY / "apps" / "desktop" / "packaging" / "macos" / "build_bundle.py"
WRAPPER = REPOSITORY / "scripts" / "build-desktop-bundle"
COMMAND = Path("/home/someone/.local/bin/voice-lab-desktop")


def _png_size(payload: bytes) -> tuple[int, int]:
    assert payload[:8] == b"\x89PNG\r\n\x1a\n" and payload[12:16] == b"IHDR"
    width, height = struct.unpack(">II", payload[16:24])
    return width, height


# -- Linux: the desktop entry ----------------------------------------------------------------


def test_the_entry_runs_the_installed_command_and_names_the_shipped_icon(tmp_path: Path) -> None:
    text = desktop_entry.render_entry(COMMAND)
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    assert f"Exec={COMMAND}" in lines and f"TryExec={COMMAND}" in lines
    assert "Icon=voice-lab-desktop" in lines and "Terminal=false" in lines
    assert "StartupWMClass=voice-lab-desktop" in lines
    assert "Categories=AudioVideo;Audio;Recorder;" in lines
    assert _png_size(desktop_entry.icon_file().read_bytes()) == (256, 256)
    validate = shutil.which("desktop-file-validate")
    if validate is None:
        pytest.skip("desktop-file-validate is not installed")
    path = tmp_path / "voice-lab-desktop.desktop"
    path.write_text(text)
    done = subprocess.run([validate, str(path)], capture_output=True, text=True)
    assert done.returncode == 0 and not done.stdout.strip(), done.stdout


@pytest.mark.parametrize(
    ("path", "argument"),
    [
        ("/opt/voice lab/bin/voice-lab-desktop", '"/opt/voice lab/bin/voice-lab-desktop"'),
        ("/opt/100%/voice-lab-desktop", "/opt/100%%/voice-lab-desktop"),
        ('/opt/a"b$c/voice-lab-desktop', '"/opt/a\\\\"b\\\\$c/voice-lab-desktop"'),
    ],
)
def test_exec_quotes_reserved_characters_as_the_spec_says(path: str, argument: str) -> None:
    assert desktop_entry.exec_argument(Path(path)) == argument


def test_a_control_character_in_the_path_is_refused() -> None:
    with pytest.raises(DesktopEntryError, match="control character"):
        desktop_entry.exec_argument(Path("/opt/a\nb/voice-lab-desktop"))


def test_install_writes_both_files_0644_once_and_refuses_to_overwrite(tmp_path: Path) -> None:
    home = tmp_path / "share"
    icon, entry = desktop_entry.install(home=home, executable=COMMAND, platform="linux")
    assert icon.changed and entry.changed
    assert icon.path == home / "icons/hicolor/256x256/apps/voice-lab-desktop.png"
    assert entry.path == home / "applications/voice-lab-desktop.desktop"
    for item in (icon, entry):
        assert stat.S_IMODE(item.path.stat().st_mode) == 0o644
    assert icon.path.read_bytes() == desktop_entry.icon_file().read_bytes()
    assert entry.path.read_text() == desktop_entry.render_entry(COMMAND)
    assert sorted(p.name for p in entry.path.parent.iterdir()) == [entry.path.name]

    again = desktop_entry.install(home=home, executable=COMMAND, platform="linux")
    assert [item.changed for item in again] == [False, False]

    other = Path("/usr/local/bin/voice-lab-desktop")
    with pytest.raises(DesktopEntryError, match="pass --replace"):
        desktop_entry.install(home=home, executable=other, platform="linux")
    assert entry.path.read_text() == desktop_entry.render_entry(COMMAND)
    replaced = desktop_entry.install(home=home, executable=other, replace=True, platform="linux")
    assert [item.changed for item in replaced] == [False, True]
    assert f"Exec={other}" in entry.path.read_text()


def test_install_refuses_links_directories_relative_commands_and_macos(tmp_path: Path) -> None:
    home = tmp_path / "share"
    target = desktop_entry.entry_path(home)
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "elsewhere.desktop")
    with pytest.raises(DesktopEntryError, match="symbolic link"):
        desktop_entry.install(home=home, executable=COMMAND, platform="linux")
    desktop_entry.install(home=home, executable=COMMAND, replace=True, platform="linux")
    assert not target.is_symlink() and not (tmp_path / "elsewhere.desktop").exists()

    blocked = tmp_path / "blocked"
    desktop_entry.icon_path(blocked).mkdir(parents=True)
    with pytest.raises(DesktopEntryError, match="not a file"):
        desktop_entry.install(home=blocked, executable=COMMAND, platform="linux")
    with pytest.raises(DesktopEntryError, match="absolute"):
        desktop_entry.install(
            home=tmp_path / "x", executable=Path("voice-lab-desktop"), platform="linux"
        )
    with pytest.raises(DesktopEntryError, match="bundle"):
        desktop_entry.install(home=tmp_path / "y", executable=COMMAND, platform="darwin")
    assert not (tmp_path / "x").exists() and not (tmp_path / "y").exists()


def test_data_home_follows_xdg_when_it_is_absolute(tmp_path: Path) -> None:
    assert desktop_entry.data_home({"XDG_DATA_HOME": str(tmp_path)}) == tmp_path
    fallback = Path.home() / ".local" / "share"
    assert desktop_entry.data_home({"XDG_DATA_HOME": "relative/dir"}) == fallback
    assert desktop_entry.data_home({}) == fallback


def test_the_command_is_found_on_path_or_is_the_running_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    running = tmp_path / "venv" / "voice-lab-desktop"
    running.parent.mkdir()
    running.write_text("#!/bin/sh\n")
    assert desktop_entry.installed_executable(str(running)) == running
    with pytest.raises(DesktopEntryError, match="not on PATH"):
        desktop_entry.installed_executable(str(tmp_path / "python"))
    linked = bin_dir / "voice-lab-desktop"
    linked.write_text("#!/bin/sh\n")
    linked.chmod(0o755)
    assert desktop_entry.installed_executable(str(running)) == linked


def test_the_command_line_installs_into_xdg_data_home_and_says_how_to_remove_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "voice-lab-desktop"
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert cli.main(["--install-desktop-entry"]) == 0
    out = capsys.readouterr().out
    entry = tmp_path / "data" / "applications" / "voice-lab-desktop.desktop"
    assert f"wrote {entry}" in out and "To remove it: rm " in out
    assert f"Exec={command}" in entry.read_text()
    assert cli.main(["--install-desktop-entry"]) == 0
    assert "unchanged" in capsys.readouterr().out
    entry.write_text("[Desktop Entry]\n")
    assert cli.main(["--install-desktop-entry"]) == 1
    assert "pass --replace" in capsys.readouterr().err
    assert cli.main(["--install-desktop-entry", "--replace"]) == 0
    with pytest.raises(SystemExit) as exited:
        cli.main(["--replace"])
    assert exited.value.code == 2


def test_the_application_carries_the_desktop_file_name_and_the_icon(qapp: Any) -> None:
    app = cli._application()
    assert app is qapp
    assert app.desktopFileName() == desktop_entry.DESKTOP_ID
    assert not app.windowIcon().isNull()


# -- macOS: the bundle's build script --------------------------------------------------------


@pytest.fixture(scope="module")
def bundle() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_bundle", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_bundle"] = module
    spec.loader.exec_module(module)
    return module


def test_the_preview_prints_the_whole_plan_and_builds_nothing(
    bundle: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out"
    assert bundle.main(["--output", str(output)], platform="linux") == 0
    out = capsys.readouterr().out
    assert not output.exists()
    assert "nothing is built" in out and "private.cole.voice-lab-desktop" in out
    assert "--require-hashes" in out and "--only-binary=:all:" in out
    assert "uv export --frozen --no-dev" in out
    assert "/usr/bin/codesign --force --deep --sign - --options runtime --entitlements" in out
    assert "/usr/bin/codesign --verify --strict --deep --verbose=2" in out
    assert "/usr/bin/plutil -lint" in out and "/usr/bin/ditto -c -k --keepParent" in out
    output.mkdir()
    bundle.main(["--output", str(output)], platform="linux")
    assert "(exists; --replace needed)" in capsys.readouterr().out


def test_the_build_refuses_off_macos_without_a_wheel_source_or_tools(
    bundle: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out"
    execute = ["--execute", "--output", str(output)]
    assert bundle.main([*execute, "--allow-network"], platform="linux") == 69
    assert "macOS only" in capsys.readouterr().err
    assert bundle.main(execute, platform="darwin", exists=lambda _p: True) == 64
    assert "--allow-network" in capsys.readouterr().err
    missing = [*execute, "--allow-network", "--uv", str(tmp_path / "no-uv")]
    assert bundle.main(missing, platform="darwin", exists=lambda _p: False) == 69
    err = capsys.readouterr().err
    assert "/usr/bin/codesign" in err and "no-uv" in err
    assert not output.exists()


@pytest.mark.parametrize(
    "argv", [["--bogus"], ["--allow-network", "--wheelhouse", "/tmp/wheels"], ["--output"]]
)
def test_bad_usage_exits_64(bundle: ModuleType, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        bundle.main(argv)
    assert exited.value.code == 64


class FakeMac:
    """Stands in for macOS's tools: records each step and leaves what a real one would."""

    def __init__(self, bundle: ModuleType, *, plist_changes: dict[str, Any] | None = None):
        self.bundle = bundle
        self.steps: list[Any] = []
        self.plist_changes = plist_changes or {}

    def __call__(self, step: Any) -> None:
        self.steps.append(step)
        argv = list(step.argv)
        if step.output is not None:
            step.output.write_text("numpy==2.5.3 --hash=sha256:" + "0" * 64 + "\n")
        elif "PyInstaller" in argv:
            spec = Path(argv[-1])
            compile(spec.read_text(), str(spec), "exec")
            app = Path(argv[argv.index("--distpath") + 1]) / "voice-lab.app"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            version = self.bundle.app_version()
            keys = {
                **self.bundle.info_plist(version),
                "CFBundleIdentifier": self.bundle.BUNDLE_ID,
                "CFBundleExecutable": "voice-lab-desktop",
                **self.plist_changes,
            }
            keys = {key: value for key, value in keys.items() if value is not None}
            (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(keys))
        elif argv[0] == "/usr/bin/ditto":
            Path(argv[-1]).write_bytes(b"PK\x05\x06" + bytes(18))


def _darwin(bundle: ModuleType, argv: list[str], runner: FakeMac) -> int:
    return int(
        bundle.main(
            ["--execute", "--allow-network", "--uv", sys.executable, *argv],
            platform="darwin",
            exists=lambda _p: True,
            run=runner,
        )
    )


def test_the_build_signs_checks_zips_and_records(
    bundle: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out"
    runner = FakeMac(bundle)
    assert _darwin(bundle, ["--output", str(output)], runner) == 0
    whats = [step.what for step in runner.steps]
    assert whats[0] == "the build environment" and whats[-1] == "the zip to copy elsewhere"
    sign = next(step for step in runner.steps if step.what == "ad-hoc signing")
    assert sign.argv[:5] == ("/usr/bin/codesign", "--force", "--deep", "--sign", "-")
    assert "--options" in sign.argv and str(output / "entitlements.plist") in sign.argv
    entitlements = plistlib.loads((output / "entitlements.plist").read_bytes())
    assert entitlements["com.apple.security.device.audio-input"] is True
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    record = __import__("json").loads((output / "build-desktop-bundle.json").read_text())
    archive = output / "voice-lab-desktop-0.1.0-macos.zip"
    assert record["archive_sha256"] == bundle.sha256_file(archive)
    assert record["hardened_runtime"] is True and record["bundle_identifier"] == bundle.BUNDLE_ID
    assert set(record["requirements_sha256"]) == {
        "development-build.txt",
        "desktop-bundle-macos.txt",
        "app-runtime.txt",
    }
    assert record["archive_sha256"] in capsys.readouterr().out

    # A rebuild needs --replace, and replaces only a build of this script.
    assert _darwin(bundle, ["--output", str(output)], FakeMac(bundle)) == 64
    plain = FakeMac(bundle)
    args = ["--output", str(output), "--replace", "--no-hardened-runtime"]
    assert _darwin(bundle, args, plain) == 0
    sign = next(step for step in plain.steps if step.what == "ad-hoc signing")
    assert "--options" not in sign.argv and "--entitlements" not in sign.argv
    stranger = tmp_path / "someone-elses"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("mine")
    assert _darwin(bundle, ["--output", str(stranger), "--replace"], FakeMac(bundle)) == 64
    assert (stranger / "keep.txt").read_text() == "mine"


def test_a_bundle_without_the_microphone_text_fails_the_build(
    bundle: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeMac(bundle, plist_changes={"NSMicrophoneUsageDescription": None})
    assert _darwin(bundle, ["--output", str(tmp_path / "out")], runner) == 1
    assert "NSMicrophoneUsageDescription" in capsys.readouterr().err
    assert runner.steps[-1].what == "the Info.plist, linted"


def test_the_generated_spec_and_info_plist(bundle: ModuleType, tmp_path: Path) -> None:
    layout = bundle.Layout(tmp_path)
    spec = bundle.render_spec(layout, "0.1.0")
    compile(spec, "voice-lab.spec", "exec")
    for needle in (
        'collect_data_files("voice_lab_desktop")',
        'collect_data_files("scshafe_qt")',
        'collect_data_files("cole_voice_lab")',
        "bundle_identifier='private.cole.voice-lab-desktop'",
        "console=False",
        "name='voice-lab-desktop'",
        str(bundle.ICON),
    ):
        assert needle in spec
    keys = plistlib.loads(plistlib.dumps(bundle.info_plist("0.1.0")))
    assert keys["NSMicrophoneUsageDescription"].startswith("Record your voice")
    assert keys["CFBundleName"] == "voice-lab" and keys["LSMinimumSystemVersion"] == "13.0"
    assert bundle.ICON.read_bytes()[:4] == b"icns"
    assert "raise SystemExit(main())" in bundle.render_entry()


def test_a_failing_step_is_reported_with_its_name(bundle: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    bundle.run_step(bundle.Step("echo", ("/bin/echo", "hi"), output=out))
    assert out.read_text() == "hi\n"
    with pytest.raises(bundle.Refusal, match="the failing step") as refused:
        bundle.run_step(bundle.Step("the failing step", ("/bin/false",)))
    assert refused.value.status == 1


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==(\S+) --hash=sha256:[0-9a-f]{64}", line)
        assert match, line
        pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def test_the_bundle_tool_is_hash_pinned_and_overlaps_nothing_else_installed(
    tmp_path: Path,
) -> None:
    tool = _pins(REPOSITORY / "requirements" / "desktop-bundle-macos.txt")
    assert {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "macholib"} <= set(tool)
    build = _pins(REPOSITORY / "requirements" / "development-build.txt")
    assert not set(tool) & set(build)  # one pip call installs both, with --require-hashes
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    exported = subprocess.run(
        [uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--no-emit-package"]
        + ["cole-voice-lab", "--format", "requirements-txt", "--no-header"],
        cwd=REPOSITORY / "apps" / "desktop",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    runtime = {
        line.split("==")[0].lower() for line in exported.splitlines() if "==" in line[:80]
    }
    assert "pyside6-essentials" in runtime and "sounddevice" in runtime
    assert not set(tool) & runtime and not set(build) & runtime


def test_the_wrapper_is_a_posix_sh_entry_point(tmp_path: Path) -> None:
    assert stat.S_IMODE(WRAPPER.stat().st_mode) == 0o755
    assert WRAPPER.read_text().startswith("#!/bin/sh\n")
    subprocess.run(["/bin/sh", "-n", str(WRAPPER)], check=True)
    shellcheck = shutil.which("shellcheck")
    if shellcheck is not None:
        subprocess.run([shellcheck, str(WRAPPER)], check=True)
    if not (REPOSITORY / ".venv" / "bin" / "python").exists():
        pytest.skip("this checkout has no .venv")
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    done = subprocess.run(
        [str(WRAPPER), "--help"], capture_output=True, text=True, env=env, check=False
    )
    assert done.returncode == 0 and done.stdout.startswith("usage:"), done.stderr

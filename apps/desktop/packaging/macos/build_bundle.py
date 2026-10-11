"""Build voice-lab-desktop as a macOS app bundle, ad-hoc signed (design §6.3, S-7, D-041).

    scripts/build-desktop-bundle                                the plan; nothing is built
    scripts/build-desktop-bundle --execute --allow-network      build, wheels from PyPI
    scripts/build-desktop-bundle --execute --wheelhouse DIR     build, wheels from DIR only

Microphone permission (TCC) attaches to an app bundle with an
``NSMicrophoneUsageDescription``, not to a Python started from a terminal, so macOS needs a
``.app``. This builds one with PyInstaller (a onedir bundle from a generated spec) in a
separate build environment, on macOS only (the Air, until the repository is public):

1. a venv from Homebrew's Python 3.12;
2. hash-checked wheels only (``--require-hashes --only-binary=:all: --no-deps``): the build
   tool (``requirements/development-build.txt``, ``requirements/desktop-bundle-macos.txt``)
   and the app's runtime closure exported from ``apps/desktop/uv.lock`` (``uv export
   --frozen``); from PyPI with ``--allow-network``, or from ``--wheelhouse`` alone;
3. ``cole-voice-lab`` and ``voice-lab-desktop`` from this checkout (built here, offline);
4. PyInstaller; the capture worker is the same executable (``--capture-worker``);
5. ad-hoc signing (``codesign --sign -``: free; no Developer ID, no notarization; decision
   13, D-028) with the hardened runtime and the entitlements a Python bundle needs, then
   ``codesign --verify --strict --deep``; the Info.plist checked with ``plutil -lint`` and
   key by key;
6. a zip (``ditto``) and its SHA-256, and ``build-desktop-bundle.json``, the build's record.

Everything goes into ``--output`` (default ``apps/desktop/dist/macos``, ignored by Git):
a new directory, or with ``--replace`` one this script made. Exit status: 0 done, 1 a step
failed, 64 usage, 69 not on macOS or a tool is missing. The first run of an ad-hoc-signed
app is in ``apps/desktop/docs/INSTALL-MACOS.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NoReturn

REPO: Final = Path(__file__).resolve().parents[4]
APP_DIR: Final = REPO / "apps" / "desktop"
DEFAULT_OUTPUT: Final = APP_DIR / "dist" / "macos"
DEFAULT_PYTHON: Final = Path("/opt/homebrew/bin/python3.12")
BUILD_REQUIREMENTS: Final = REPO / "requirements" / "development-build.txt"
BUNDLE_REQUIREMENTS: Final = REPO / "requirements" / "desktop-bundle-macos.txt"
ICON: Final = REPO / "src" / "cole_voice_lab" / "resources" / "desktop-icon.icns"

APP_NAME: Final = "voice-lab"
EXECUTABLE: Final = "voice-lab-desktop"
BUNDLE_ID: Final = "private.cole.voice-lab-desktop"
MICROPHONE_USE: Final = "Record your voice prompts and measurement sessions with the UMIK-1."
MARKER: Final = ".build-desktop-bundle"
RECORD: Final = "build-desktop-bundle.json"
RECORD_SCHEMA: Final = "voice-lab-desktop.macos-bundle-build/v1"

EX_FAILED: Final = 1
EX_USAGE: Final = 64
EX_UNAVAILABLE: Final = 69

#: With the hardened runtime: the microphone; ad-hoc signed libraries have no Team ID, which
#: library validation would refuse (Qt, PortAudio, FFmpeg are separate dylibs); ctypes and
#: cffi callbacks (sounddevice's audio callback) need executable memory they write.
ENTITLEMENTS: Final = {
    "com.apple.security.device.audio-input": True,
    "com.apple.security.cs.disable-library-validation": True,
    "com.apple.security.cs.allow-unsigned-executable-memory": True,
}


class Refusal(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Step:
    """One command of the build; ``output`` is a file its stdout goes to."""

    what: str
    argv: tuple[str, ...]
    cwd: Path | None = None
    output: Path | None = None

    def describe(self) -> str:
        line = shlex.join(self.argv)
        if self.output is not None:
            line += f" > {shlex.quote(str(self.output))}"
        if self.cwd is not None:
            line = f"(cd {shlex.quote(str(self.cwd))} && {line})"
        return f"# {self.what}\n{line}"


@dataclass(frozen=True)
class Options:
    output: Path
    python: Path
    uv: str
    execute: bool = False
    allow_network: bool = False
    wheelhouse: Path | None = None
    replace: bool = False
    hardened_runtime: bool = True


@dataclass
class Layout:
    root: Path
    venv: Path = field(init=False)
    app: Path = field(init=False)

    def __post_init__(self) -> None:
        self.venv = self.root / "venv"
        self.app = self.root / "dist" / f"{APP_NAME}.app"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def runtime_requirements(self) -> Path:
        return self.root / "app-runtime.txt"

    @property
    def spec(self) -> Path:
        return self.root / f"{APP_NAME}.spec"

    @property
    def entry(self) -> Path:
        return self.root / "voice_lab_desktop_main.py"

    @property
    def entitlements(self) -> Path:
        return self.root / "entitlements.plist"

    @property
    def info_plist(self) -> Path:
        return self.app / "Contents" / "Info.plist"

    def archive(self, version: str) -> Path:
        return self.root / f"{EXECUTABLE}-{version}-macos.zip"


def app_version() -> str:
    with (APP_DIR / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def info_plist(version: str) -> dict[str, Any]:
    """The keys this bundle's Info.plist must carry (PyInstaller adds the executable's)."""
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": MICROPHONE_USE,
    }


def entitlements_plist() -> bytes:
    return plistlib.dumps(ENTITLEMENTS, sort_keys=True)


def render_entry() -> str:
    return (
        "# Generated by apps/desktop/packaging/macos/build_bundle.py; do not edit.\n"
        "from voice_lab_desktop.cli import main\n\n"
        "raise SystemExit(main())\n"
    )


def render_spec(layout: Layout, version: str) -> str:
    """The PyInstaller spec: onedir, windowed, one bundle.

    Data files: the app's QML and icon, the backend's resources (the prompt suites), and
    scshafe-qt's QML module; PyInstaller's PySide6 hooks bring Qt Quick's plugins and
    pyinstaller-hooks-contrib's bring PortAudio (sounddevice), FFmpeg (av) and keyring's
    backends. The capture worker is this executable run with ``--capture-worker``, so
    nothing it needs is excluded."""
    return f"""# Generated by apps/desktop/packaging/macos/build_bundle.py; do not edit.
# PyInstaller spec for voice-lab-desktop {version} (design §6.3, S-7, D-041).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = (
    collect_data_files("voice_lab_desktop")
    + collect_data_files("cole_voice_lab")
    + collect_data_files("scshafe_qt")
)
hiddenimports = collect_submodules("voice_lab_desktop") + [
    "sounddevice",
    "av",
    "numpy",
    "keyring.backends.macOS",
]

a = Analysis(
    [{str(layout.entry)!r}],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name={EXECUTABLE!r},
    console=False,
    argv_emulation=False,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name={APP_NAME!r})
app = BUNDLE(
    coll,
    name={APP_NAME + ".app"!r},
    icon={str(ICON)!r},
    bundle_identifier={BUNDLE_ID!r},
    version={version!r},
    info_plist={info_plist(version)!r},
)
"""


def plan(options: Options, layout: Layout) -> list[Step]:
    """The build's commands, in order (the files it writes are listed by ``describe``)."""
    pip = (str(layout.python), "-m", "pip", "install", "--disable-pip-version-check")
    source = (
        ("--no-index", "--find-links", str(options.wheelhouse))
        if options.wheelhouse is not None
        else ()
    )
    signing: tuple[str, ...] = ("/usr/bin/codesign", "--force", "--deep", "--sign", "-")
    if options.hardened_runtime:
        signing += ("--options", "runtime", "--entitlements", str(layout.entitlements))
    return [
        Step("the build environment", (str(options.python), "-m", "venv", str(layout.venv))),
        Step(
            "the app's runtime closure, exported from apps/desktop/uv.lock with its hashes",
            (
                options.uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--no-emit-package",
                "cole-voice-lab",
                "--format",
                "requirements-txt",
            ),
            cwd=APP_DIR,
            output=layout.runtime_requirements,
        ),
        Step(
            "hash-checked wheels only: the build tool and the app's runtime",
            (
                *pip,
                "--only-binary=:all:",
                "--no-deps",
                "--require-hashes",
                *source,
                "-r",
                str(BUILD_REQUIREMENTS),
                "-r",
                str(BUNDLE_REQUIREMENTS),
                "-r",
                str(layout.runtime_requirements),
            ),
        ),
        Step(
            "cole-voice-lab and voice-lab-desktop from this checkout, built here",
            (*pip, "--no-deps", "--no-build-isolation", "--no-index", str(REPO), str(APP_DIR)),
        ),
        Step(
            "the bundle",
            (
                str(layout.python),
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--distpath",
                str(layout.root / "dist"),
                "--workpath",
                str(layout.root / "work"),
                str(layout.spec),
            ),
        ),
        Step("ad-hoc signing", (*signing, "--timestamp=none", str(layout.app))),
        Step(
            "the signature, checked",
            ("/usr/bin/codesign", "--verify", "--strict", "--deep", "--verbose=2", str(layout.app)),
        ),
        Step("the Info.plist, linted", ("/usr/bin/plutil", "-lint", str(layout.info_plist))),
        Step(
            "the zip to copy elsewhere",
            (
                "/usr/bin/ditto",
                "-c",
                "-k",
                "--keepParent",
                str(layout.app),
                str(layout.archive(app_version())),
            ),
        ),
    ]


def _refuse(status: int, message: str) -> NoReturn:
    raise Refusal(status, message)


def check_output(options: Options) -> None:
    """The output must be new, or (with ``--replace``) one this script made."""
    root = options.output
    if not root.exists() and not root.is_symlink():
        return
    if not options.replace:
        _refuse(EX_USAGE, f"{root} exists; pass --replace to rebuild a previous build there")
    if root.is_symlink() or not root.is_dir() or not (root / MARKER).is_file():
        _refuse(EX_USAGE, f"--replace: {root} is not a previous build of this script")


def check_tools(
    options: Options,
    *,
    platform: str,
    exists: Callable[[Path], bool],
) -> None:
    if platform != "darwin":
        _refuse(EX_UNAVAILABLE, "the app bundle is built on macOS only (the Air); this is not")
    if not options.allow_network and options.wheelhouse is None:
        _refuse(
            EX_USAGE,
            "the build installs pinned wheels: pass --allow-network to download them from "
            "PyPI (hash-checked), or --wheelhouse DIR",
        )
    tools = [
        options.python,
        Path("/usr/bin/codesign"),
        Path("/usr/bin/plutil"),
        Path("/usr/bin/ditto"),
    ]
    missing = [str(tool) for tool in tools if not exists(tool)]
    if not shutil.which(options.uv) and not exists(Path(options.uv)):
        missing.append(options.uv)
    if missing:
        _refuse(
            EX_UNAVAILABLE,
            "missing: "
            + ", ".join(missing)
            + " (Homebrew's python@3.12, uv, and the Xcode command line tools)",
        )


def run_step(step: Step) -> None:
    """Run one step; a failure raises ``Refusal`` naming it."""
    print(step.describe(), flush=True)
    try:
        if step.output is None:
            subprocess.run(step.argv, cwd=step.cwd, check=True)
        else:
            with step.output.open("xb") as sink:
                subprocess.run(step.argv, cwd=step.cwd, check=True, stdout=sink)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Refusal(EX_FAILED, f"{step.what}: {exc}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def check_info_plist(path: Path, version: str) -> None:
    """The built bundle's Info.plist carries every key this build asked for."""
    with path.open("rb") as stream:
        found = plistlib.load(stream)
    wrong = {key for key, value in info_plist(version).items() if found.get(key) != value}
    if found.get("CFBundleIdentifier") != BUNDLE_ID:
        wrong.add("CFBundleIdentifier")
    if wrong:
        raise Refusal(EX_FAILED, f"the bundle's Info.plist differs: {', '.join(sorted(wrong))}")


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ("git", "-C", str(REPO), *args), check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout.strip()


def build(
    options: Options,
    *,
    run: Callable[[Step], None] = run_step,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Build, sign, check and zip; returns the build's record (also written to the output)."""
    layout = Layout(options.output)
    version = app_version()
    if options.output.exists():
        shutil.rmtree(options.output)  # check_output: a previous build of this script
    layout.root.mkdir(mode=0o700, parents=True)
    (layout.root / MARKER).write_text("voice-lab-desktop macOS bundle build\n")
    layout.entry.write_text(render_entry())
    layout.spec.write_text(render_spec(layout, version))
    layout.entitlements.write_bytes(entitlements_plist())
    for step in plan(options, layout):
        run(step)
        if step.what == "the Info.plist, linted":
            check_info_plist(layout.info_plist, version)
    archive = layout.archive(version)
    record = {
        "schema_version": RECORD_SCHEMA,
        "created_at": clock().isoformat(),
        "version": version,
        "bundle_identifier": BUNDLE_ID,
        "git_revision": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "hardened_runtime": options.hardened_runtime,
        "entitlements": sorted(ENTITLEMENTS) if options.hardened_runtime else [],
        "requirements_sha256": {
            path.name: sha256_file(path)
            for path in (BUILD_REQUIREMENTS, BUNDLE_REQUIREMENTS, layout.runtime_requirements)
        },
        "app": str(layout.app),
        "archive": str(archive),
        "archive_sha256": sha256_file(archive),
    }
    (layout.root / RECORD).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(EX_USAGE)


def parse(argv: Sequence[str] | None) -> Options:
    ap = _Parser(
        prog="build-desktop-bundle",
        description="Build voice-lab-desktop as an ad-hoc-signed macOS app bundle "
        "(preview unless --execute).",
    )
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="the build directory")
    ap.add_argument("--python", type=Path, default=DEFAULT_PYTHON, help="Python 3.12 to use")
    ap.add_argument("--uv", default="uv", help="uv, to export the app's locked runtime")
    ap.add_argument("--execute", action="store_true", help="build (macOS only)")
    source = ap.add_mutually_exclusive_group()
    source.add_argument(
        "--allow-network", action="store_true", help="download the pinned wheels from PyPI"
    )
    source.add_argument("--wheelhouse", type=Path, help="install the pinned wheels from DIR only")
    ap.add_argument(
        "--replace", action="store_true", help="rebuild over a previous build in --output"
    )
    ap.add_argument(
        "--no-hardened-runtime",
        action="store_true",
        help="sign without the hardened runtime (if the hardened build will not start)",
    )
    args = ap.parse_args(argv)
    return Options(
        output=args.output.absolute(),
        python=args.python,
        uv=args.uv,
        execute=args.execute,
        allow_network=args.allow_network,
        wheelhouse=args.wheelhouse.absolute() if args.wheelhouse else None,
        replace=args.replace,
        hardened_runtime=not args.no_hardened_runtime,
    )


def preview(options: Options) -> None:
    layout = Layout(options.output)
    version = app_version()
    print(f"voice-lab-desktop {version}: macOS app bundle build plan (nothing is built).")
    print(f"output: {layout.root}{' (exists; --replace needed)' if layout.root.exists() else ''}")
    print(f"writes: {layout.entry.name}, {layout.spec.name}, {layout.entitlements.name}")
    print(f"bundle: {layout.app} ({BUNDLE_ID})")
    for step in plan(options, layout):
        print(step.describe())
    print("then: the Info.plist checked key by key; the zip's SHA-256; " + RECORD)
    print("Run on macOS with --execute and --allow-network (or --wheelhouse DIR).")


def main(
    argv: Sequence[str] | None = None,
    *,
    platform: str = sys.platform,
    exists: Callable[[Path], bool] = os.path.exists,
    run: Callable[[Step], None] = run_step,
) -> int:
    options = parse(argv)
    try:
        if not options.execute:
            preview(options)
            return 0
        check_tools(options, platform=platform, exists=exists)
        check_output(options)
        record = build(options, run=run)
    except Refusal as exc:
        print(f"build-desktop-bundle: {exc}", file=sys.stderr)
        return exc.status
    print(f"built {record['app']}")
    print(f"{record['archive_sha256']}  {record['archive']}")
    print("First run of an ad-hoc-signed app: apps/desktop/docs/INSTALL-MACOS.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

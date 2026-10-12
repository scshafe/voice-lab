"""The Mac's own tools: ``scripts/mac-refresh`` and ``scripts/mac-signing-identity`` (D-043).

Both drive macOS commands (git, the builders, ``security``, ``codesign``, ``ditto``); here a
fake Mac answers them, so the order, the refusals, the install guards and the report are
checked on any host. What the real tools do on a Mac is the Mac agent's first run
(docs/mac-agent/README.md).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
COMMIT = "c0ffee" + "1" * 34
OLDER = "0ldbad" + "2" * 34
ORIGINAL = "aaaaaa" + "3" * 34
IDENTITY = "AB" * 20


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPOSITORY / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def refresh() -> ModuleType:
    return _load("mac_refresh")


@pytest.fixture(scope="module")
def signing() -> ModuleType:
    return _load("mac_signing_identity")


# ---- mac-refresh ----------------------------------------------------------------------------


class FakeMac:
    """macOS as mac-refresh sees it: git, the verify run, the builders, the signing tools."""

    def __init__(
        self,
        module: ModuleType,
        *,
        commit: str = COMMIT,
        dirty: str = "",
        on_main: bool = True,
        newer_than: dict[str, bool] | None = None,
        verify_status: int = 0,
        identity: bool = True,
    ) -> None:
        self.module = module
        self.commit = commit
        self.dirty = dirty
        self.on_main = on_main
        self.newer_than = newer_than or {}
        self.verify_status = verify_status
        self.identity = identity
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, log: Path | None) -> Any:
        result = self.module.Result
        argv = tuple(argv)
        self.calls.append(argv)
        if log is not None:
            with log.open("a") as sink:
                sink.write("$ " + " ".join(argv) + "\n")
        match argv:
            case ("git", "status", "--porcelain"):
                return result(0, self.dirty)
            case ("git", "symbolic-ref", *_):
                return result(0, "main\n")
            case ("git", "rev-parse", "HEAD"):
                return result(0, ORIGINAL + "\n")
            case ("git", "rev-parse", "--verify", "--quiet", _):
                return result(0, self.commit + "\n")
            case ("git", "merge-base", "--is-ancestor", _older, newer):
                if newer == "origin/main":
                    return result(0 if self.on_main else 1)
                return result(0 if self.newer_than.get(newer, False) else 1)
            case ("git", *_):
                return result(0)
            case ("scripts/verify",):
                assert log is not None
                with log.open("a") as sink:
                    sink.write("=========== 6450 passed, 151 skipped in 1326.20s ===========\n")
                    sink.write("============== 408 passed in 219.68s (0:03:39) ==============\n")
                return result(self.verify_status)
            case ("/usr/bin/security", "find-identity", *_):
                line = f'  1) {IDENTITY} "voice-lab local signing"\n' if self.identity else ""
                return result(0, line + "     1 valid identities found\n")
            case ("scripts/build-native-recording-app", *rest):
                app = Path(rest[rest.index("--output") + 1])
                (app / "Contents" / "MacOS").mkdir(parents=True)
                runtime = app.with_suffix(".runtime")
                runtime.mkdir()
                (runtime / "release.json").write_text("{}")
                return result(0)
            case (_, "-I", "-B", "-c", script, release) if "verify_native" in script:
                assert Path(release).name == "release.json" and Path(release).is_file()
                return result(0)
            case ("scripts/build-desktop-bundle", *rest):
                output = Path(rest[rest.index("--output") + 1])
                (output / "dist" / "voice-lab.app" / "Contents").mkdir(parents=True)
                record = {"archive": str(output / "x.zip"), "archive_sha256": "f" * 64}
                (output / "build-desktop-bundle.json").write_text(json.dumps(record))
                return result(0)
            case ("/usr/bin/codesign", "-dv", "--verbose=2", app):
                signed = (
                    "Signature=adhoc\n"
                    if not self.identity
                    else "Authority=voice-lab local signing\nTeamIdentifier=not set\n"
                )
                return result(0, "", f"Executable={app}\nIdentifier=private.test\n{signed}")
            case ("/usr/bin/ditto", source, target):
                shutil.copytree(source, target)
                return result(0)
            case (_, "-m", "pip", *_) | (_, "-m", "venv", *_):
                return result(0)
        raise AssertionError(f"unexpected command: {argv}")

    def builder(self, name: str) -> tuple[str, ...]:
        return next(call for call in self.calls if call[0] == name)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    settings = home / ".config" / "voice-lab" / "native-app-settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")
    return home


def _run(module: ModuleType, mac: FakeMac, home: Path, *argv: str) -> int:
    builds, applications = home / "builds", home / "Applications"
    applications.mkdir(exist_ok=True)
    common = ["--execute", "--builds", str(builds), "--applications", str(applications)]
    return int(
        module.main(
            [*common, *argv],
            platform="darwin",
            run=mac,
            home=home,
            clock=lambda: NOW,
            exists=lambda _path: True,
        )
    )


def _report(home: Path) -> dict[str, Any]:
    (path,) = (home / "builds" / "runs").glob("*/report.json")
    return dict(json.loads(path.read_text()))


def test_the_plan_runs_nothing(
    refresh: ModuleType, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def nothing(*_args: Any) -> Any:
        raise AssertionError("the plan must not run anything")

    assert refresh.main(["--install", "--pr", "38"], run=nothing, home=home) == 0
    out = capsys.readouterr().out
    assert "the plan for origin/pr/38" in out and "no fetch (no --allow-network)" in out
    assert "native-app-settings.json (found)" in out
    assert "never an older Swift app over a newer one" in out


def test_off_macos_or_without_its_tools_nothing_runs(refresh: ModuleType, home: Path) -> None:
    mac = FakeMac(refresh)
    assert refresh.main(["--execute"], platform="linux", run=mac, home=home) == 69
    assert refresh.main(["--execute"], platform="darwin", run=mac, exists=lambda _p: False) == 69
    assert mac.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--pr", "0"],
        ["--ref", "main", "--pr", "3"],
        ["--ref", "-x"],
        ["--signing-identity=--deep"],
        ["--only", "everything"],
        ["--bogus"],
    ],
)
def test_bad_usage_exits_64(refresh: ModuleType, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        refresh.main(argv)
    assert exited.value.code == 64


def test_a_checkout_with_changes_is_not_built(refresh: ModuleType, home: Path) -> None:
    mac = FakeMac(refresh, dirty=" M scripts/verify\n")
    assert _run(refresh, mac, home, "--allow-network") == 64
    assert not any(call[:2] == ("git", "checkout") for call in mac.calls)


def test_main_is_fetched_verified_built_signed_and_installed(
    refresh: ModuleType, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = FakeMac(refresh)
    assert _run(refresh, mac, home, "--allow-network", "--install") == 0
    assert mac.calls[0] == ("git", "status", "--porcelain")
    assert ("git", "fetch", "--prune", "origin") in mac.calls
    assert ("git", "checkout", "--quiet", "--detach", COMMIT) in mac.calls
    assert mac.calls[-1] == ("git", "checkout", "--quiet", "main")  # back where it was
    # The pinned build tools, then the checkout itself.
    pips = [call for call in mac.calls if "pip" in call]
    assert "--require-hashes" in pips[0] and pips[-1][-2:] == ("-e", ".[dev]")
    # Both builders sign with the stable identity (D-043).
    for name in ("scripts/build-native-recording-app", "scripts/build-desktop-bundle"):
        call = mac.builder(name)
        assert call[call.index("--signing-identity") + 1] == IDENTITY
    native = mac.builder("scripts/build-native-recording-app")
    assert native[native.index("--settings") + 1].endswith("native-app-settings.json")
    report = _report(home)
    steps = report["steps"]
    assert report["commit"] == COMMIT and report["on_main"] is True
    assert steps["verify"]["state"] == "passed"
    assert steps["verify"]["summaries"] == [
        "6450 passed, 151 skipped in 1326.20s",
        "408 passed in 219.68s (0:03:39)",
    ]
    assert steps["native"]["signature"]["authority"] == "voice-lab local signing"
    assert steps["desktop"]["archive_sha256"] == "f" * 64
    applications = home / "Applications"
    assert (applications / "Cole Voice Recorder Preview.app" / "Contents").is_dir()
    assert (applications / "voice-lab.app" / "Contents").is_dir()
    installed = json.loads((home / "builds" / "installed.json").read_text())
    assert installed["native"]["commit"] == COMMIT == installed["desktop"]["commit"]
    run_dir = Path(report["run_directory"])
    assert run_dir.name == f"20261011T180000Z-{COMMIT[:12]}"
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    markdown = (run_dir / "report.md").read_text()
    assert f"`origin/main` at `{COMMIT[:12]}`" in markdown
    assert "signed by voice-lab local signing" in markdown
    assert "6450 passed" in markdown and "voice-lab.app" in markdown
    assert markdown in capsys.readouterr().out

    # The next run replaces the installed apps and keeps them in its own directory.
    later = FakeMac(refresh, commit="d" * 40, newer_than={COMMIT: False})
    (home / "builds" / "runs").rename(home / "builds" / "first-runs")
    assert _run(refresh, later, home, "--allow-network", "--install") == 0
    second = _report(home)
    replaced = Path(second["steps"]["install"]["apps"]["native"]["replaced"])
    assert replaced.parent == Path(second["run_directory"]) / "replaced"
    assert (replaced / "Contents").is_dir()


def test_without_the_identity_it_signs_ad_hoc_and_says_so(refresh: ModuleType, home: Path) -> None:
    mac = FakeMac(refresh, identity=False)
    assert _run(refresh, mac, home, "--allow-network", "--skip-verify") == 0
    call = mac.builder("scripts/build-desktop-bundle")
    assert call[call.index("--signing-identity") + 1] == "-"
    report = _report(home)
    assert report["signing_identity"]["source"] == "ad-hoc fallback"
    assert report["steps"]["verify"] == {"state": "skipped", "reason": "--skip-verify"}
    assert "macOS asks for the microphone again" in render(refresh, report)
    assert not any(call[0] == "scripts/verify" for call in mac.calls)


def render(module: ModuleType, report: dict[str, Any]) -> str:
    return str(module.render_markdown(report))


def test_without_build_settings_the_swift_app_is_skipped(refresh: ModuleType, home: Path) -> None:
    (home / ".config" / "voice-lab" / "native-app-settings.json").unlink()
    mac = FakeMac(refresh)
    assert _run(refresh, mac, home, "--allow-network") == 0
    steps = _report(home)["steps"]
    assert (
        steps["native"]["state"] == "skipped" and "no build settings" in steps["native"]["reason"]
    )
    assert steps["desktop"]["state"] == "built"
    assert not any(call[0] == "scripts/build-native-recording-app" for call in mac.calls)


def test_a_failed_verify_builds_nothing_and_restores_the_checkout(
    refresh: ModuleType, home: Path
) -> None:
    mac = FakeMac(refresh, verify_status=1)
    assert _run(refresh, mac, home, "--allow-network", "--install") == 1
    assert not any(call[0].startswith("scripts/build") for call in mac.calls)
    assert mac.calls[-1] == ("git", "checkout", "--quiet", "main")
    report = _report(home)
    assert report["steps"]["verify"]["state"] == "failed"
    assert "scripts/verify (on macOS: the Swift app too) failed" in report["failure"]
    assert not (home / "Applications" / "voice-lab.app").exists()


def test_a_pull_request_is_built_but_installed_only_when_the_owner_says(
    refresh: ModuleType, home: Path
) -> None:
    mac = FakeMac(refresh, on_main=False)
    assert _run(refresh, mac, home, "--allow-network", "--pr", "38", "--install") == 64
    assert ("git", "fetch", "origin", "+refs/pull/38/head:refs/remotes/origin/pr/38") in mac.calls
    report = _report(home)
    assert report["target"] == "origin/pr/38" and report["on_main"] is False
    assert report["steps"]["native"]["state"] == "built"
    assert "is not on origin/main" in report["failure"]
    assert not any((home / "Applications").iterdir())
    shutil.rmtree(home / "builds" / "runs")
    mac = FakeMac(refresh, on_main=False)
    args = ("--allow-network", "--pr", "38", "--install", "--allow-non-main-install")
    assert _run(refresh, mac, home, *args) == 0
    assert (home / "Applications" / "voice-lab.app").is_dir()


def test_an_older_swift_app_never_replaces_a_newer_one(refresh: ModuleType, home: Path) -> None:
    builds = home / "builds"
    builds.mkdir()
    (builds / "installed.json").write_text(json.dumps({"native": {"commit": COMMIT}}))
    older = FakeMac(refresh, commit=OLDER, newer_than={COMMIT: True})
    assert _run(refresh, older, home, "--allow-network", "--install") == 64
    assert "is newer than" in _report(home)["failure"]
    assert not any((home / "Applications").iterdir())
    shutil.rmtree(builds / "runs")
    older = FakeMac(refresh, commit=OLDER, newer_than={COMMIT: True})
    assert _run(refresh, older, home, "--allow-network", "--install", "--allow-downgrade") == 0
    assert json.loads((builds / "installed.json").read_text())["native"]["commit"] == OLDER


def test_codesign_and_identity_listings_are_read_as_macos_prints_them(
    refresh: ModuleType, signing: ModuleType
) -> None:
    listing = (
        f'  1) {IDENTITY} "voice-lab local signing"\n'
        f'  2) {"cd" * 20} "Apple Development: someone"\n'
        "     2 valid identities found\n"
    )
    for module in (refresh, signing):
        assert module.valid_identities(listing) == {
            "voice-lab local signing": IDENTITY,
            "Apple Development: someone": "CD" * 20,
        }
    assert refresh.signature("Identifier=x\nSignature=adhoc\n") == {
        "ad_hoc": True,
        "authority": None,
        "identifier": "x",
    }


# ---- mac-signing-identity -------------------------------------------------------------------


class Keychain:
    """``security``, ``openssl`` and ``codesign`` as the identity script sees them."""

    def __init__(self, module: ModuleType, *, has: bool = False, stray: bool = False) -> None:
        self.module = module
        self.has = has
        self.stray = stray
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.fail: str | None = None

    def __call__(self, argv: Sequence[str], extra: Mapping[str, str] | None) -> Any:
        result = self.module.Result
        argv = tuple(argv)
        self.calls.append((argv, dict(extra or {})))
        if self.fail and self.fail in argv:
            return result(1, "", f"{self.fail} refused")
        match argv:
            case ("/usr/bin/security", "find-identity", *_):
                line = f'  1) {IDENTITY} "voice-lab local signing"\n' if self.has else ""
                return result(0, line)
            case ("/usr/bin/security", "find-certificate", *_):
                return result(0 if self.stray else 44)
            case ("/usr/bin/security", "login-keychain"):
                return result(0, '    "/Users/cole/Library/Keychains/login.keychain-db"\n')
            case ("/usr/bin/security", "add-trusted-cert", *_):
                self.has = True
                return result(0)
            case ("/usr/bin/openssl", "req", *rest):
                config = Path(rest[rest.index("-config") + 1]).read_text()
                assert "extendedKeyUsage = critical, codeSigning" in config
                assert "CN = voice-lab local signing" in config
                assert (
                    stat.S_IMODE(Path(rest[rest.index("-config") + 1]).parent.stat().st_mode)
                    == 0o700
                )
                return result(0)
            case ("/usr/bin/openssl" | "/usr/bin/security" | "/usr/bin/codesign", *_):
                return result(0)
        raise AssertionError(f"unexpected command: {argv}")

    def commands(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls]


def test_the_identity_plan_changes_nothing(
    signing: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def nothing(*_args: Any) -> Any:
        raise AssertionError("the plan must not run anything")

    assert signing.main([], run=nothing) == 0
    out = capsys.readouterr().out
    assert "add-trusted-cert -r trustRoot -p codeSign" in out
    assert "-passout env:VOICE_LAB_SIGNING_P12" in out


def test_the_identity_is_created_trusted_and_tried_once(
    signing: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    keychain = Keychain(signing)
    assert signing.main(["--execute"], platform="darwin", run=keychain, exists=lambda _p: True) == 0
    commands = keychain.commands()
    steps = [argv[:2] for argv in commands]
    assert steps == [
        ("/usr/bin/security", "find-identity"),
        ("/usr/bin/security", "find-certificate"),
        ("/usr/bin/security", "login-keychain"),
        ("/usr/bin/openssl", "req"),
        ("/usr/bin/openssl", "pkcs12"),
        ("/usr/bin/security", "import"),
        ("/usr/bin/security", "add-trusted-cert"),
        ("/usr/bin/security", "find-identity"),
        ("/usr/bin/codesign", "--force"),
        ("/usr/bin/codesign", "--verify"),
    ]
    pkcs12 = next(argv for argv in commands if argv[1] == "pkcs12")
    environment = next(extra for argv, extra in keychain.calls if argv[1] == "pkcs12")
    password = environment["VOICE_LAB_SIGNING_P12"]
    assert "env:VOICE_LAB_SIGNING_P12" in pkcs12 and password not in pkcs12
    imported = next(argv for argv in commands if argv[1] == "import")
    assert imported[-1] == password and ("-T", "/usr/bin/codesign") == imported[7:9]
    assert "/Users/cole/Library/Keychains/login.keychain-db" in imported
    trusted = next(argv for argv in commands if argv[1] == "add-trusted-cert")
    assert trusted[2:6] == ("-r", "trustRoot", "-p", "codeSign")
    signed = next(argv for argv in commands if argv[:2] == ("/usr/bin/codesign", "--force"))
    assert signed[3] == IDENTITY
    work = Path(pkcs12[pkcs12.index("-out") + 1]).parent
    assert not work.exists()  # the key never stays on disk
    assert f"created 'voice-lab local signing': {IDENTITY}" in capsys.readouterr().out


def test_an_existing_identity_is_left_alone_and_shown(
    signing: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    keychain = Keychain(signing, has=True)
    common: dict[str, Any] = {"platform": "darwin", "run": keychain, "exists": lambda _p: True}
    assert signing.main(["--execute"], **common) == 0
    assert signing.main(["--show"], **common) == 0
    assert all(argv[1] == "find-identity" for argv in keychain.commands())
    assert capsys.readouterr().out.splitlines()[-1] == IDENTITY
    assert signing.main(["--show"], **{**common, "run": Keychain(signing)}) == 1


def test_a_stray_certificate_a_failed_step_or_another_os_is_refused(
    signing: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    common: dict[str, Any] = {"platform": "darwin", "exists": lambda _p: True}
    assert signing.main(["--execute"], run=Keychain(signing, stray=True), **common) == 1
    assert "exists but is not a valid code-signing identity" in capsys.readouterr().err
    failing = Keychain(signing)
    failing.fail = "add-trusted-cert"
    assert signing.main(["--execute"], run=failing, **common) == 1
    pkcs12 = next(argv for argv in failing.commands() if argv[1] == "pkcs12")
    assert not Path(pkcs12[pkcs12.index("-out") + 1]).parent.exists()
    assert signing.main(["--execute"], platform="linux", run=Keychain(signing)) == 69
    assert signing.main(["--execute"], platform="darwin", exists=lambda _p: False) == 69


# ---- the wrappers ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mac-refresh", "mac-signing-identity"])
def test_the_wrappers_are_posix_sh_entry_points_for_homebrews_python(name: str) -> None:
    script = REPOSITORY / "scripts" / name
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert script.read_text().startswith("#!/bin/sh\nset -eu\n")
    assert "/opt/homebrew/bin/python3.12" in script.read_text()
    subprocess.run(["/bin/sh", "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(script)], check=True)
    done = subprocess.run([str(script)], capture_output=True, text=True, check=False)
    if os.path.exists("/opt/homebrew/bin/python3.12"):
        assert done.returncode == 0 and "the plan" in done.stdout
    else:
        assert done.returncode == 69 and "brew install python@3.12" in done.stderr

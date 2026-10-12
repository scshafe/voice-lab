"""Refresh voice-lab's macOS apps on this Mac from a commit of scshafe/voice-lab.

    scripts/mac-refresh                                       the plan; nothing changes
    scripts/mac-refresh --execute --allow-network             main: fetch, verify, build
    scripts/mac-refresh --execute --allow-network --install   ... and install both apps
    scripts/mac-refresh --execute --allow-network --pr 38     a pull request's head

One run, from a clean checkout (docs/mac-agent/README.md):

1. fetch (``--allow-network``) and check out the commit, detached: ``origin/main`` by default,
   ``--ref REF`` or ``--pr N``; the checkout returns to where it was afterwards;
2. the checkout's ``.venv`` (Homebrew's Python 3.12, ``pip install -e '.[dev]'``);
3. ``scripts/verify``, which on macOS also builds and tests the Swift app (skip with
   ``--skip-verify``); a failure stops the run;
4. the signing identity: ``voice-lab local signing`` when the keychains hold it
   (``scripts/mac-signing-identity``, D-043), so microphone permission survives rebuilds;
   else ad-hoc (macOS asks again after each build); or ``--signing-identity``;
5. the Swift app (``scripts/build-native-recording-app``, with the private build settings
   from ``--native-settings``; skipped when there are none), then its release verified;
6. voice-lab-desktop's bundle (``scripts/build-desktop-bundle``);
7. with ``--install``: both apps copied into ``/Applications`` (the app they replace is kept
   in the run's directory). Installing needs a commit on ``origin/main``
   (``--allow-non-main-install`` overrides), and never puts an older Swift app over a newer
   one: an older native build cannot open a library a newer one has opened
   (docs/NATIVE_RECORDING_APPLICATION.md; ``--allow-downgrade`` overrides);
8. ``report.json`` and ``report.md`` (for a pull request comment) in the run's directory.

Builds live in ``~/Library/Application Support/voice-lab/mac-refresh/runs/<UTC>-<commit>``:
the installed Swift app reads its configuration from there, so keep the run of an installed
app. Nothing is launched and no recording is read.

Exit status: 0 done, 1 a step failed (the report says which), 64 usage or a refusal, 69 not
on macOS or a tool is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NoReturn

REPO: Final = Path(__file__).resolve().parents[1]
HOMEBREW_PYTHON: Final = Path("/opt/homebrew/bin/python3.12")
SECURITY: Final = "/usr/bin/security"
CODESIGN: Final = "/usr/bin/codesign"
DITTO: Final = "/usr/bin/ditto"
IDENTITY_NAME: Final = "voice-lab local signing"
AD_HOC: Final = "-"
NATIVE_APP: Final = "Cole Voice Recorder Preview.app"
DESKTOP_APP: Final = "voice-lab.app"
DEFAULT_BUILDS: Final = Path("~/Library/Application Support/voice-lab/mac-refresh")
DEFAULT_SETTINGS: Final = Path("~/.config/voice-lab/native-app-settings.json")
REPORT_SCHEMA: Final = "voice-lab.mac-refresh/v1"
INSTALLED: Final = "installed.json"

EX_FAILED: Final = 1
EX_USAGE: Final = 64
EX_UNAVAILABLE: Final = 69

_IDENTITY_LINE: Final = re.compile(r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"(.*)"\s*$')
_SIGNING_IDENTITY: Final = re.compile(r"-|[0-9A-Fa-f]{40}|[A-Za-z0-9][A-Za-z0-9 ._()-]{0,127}")
_SUMMARY: Final = re.compile(r"^=+ (.*\b(?:passed|failed|errors?)\b.*?) =+$")
_VERIFY_NATIVE: Final = (
    "import sys; from pathlib import Path; "
    "from cole_voice_lab.native_recording_application import "
    "verify_native_recording_application as verify; "
    "print(verify(Path(sys.argv[1])).application)"
)


class Refusal(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


#: ``run(argv, cwd, log)``: one command; with ``log`` its output is appended there instead
#: of being returned.
type Runner = Callable[[Sequence[str], Path, Path | None], Result]


def run_command(argv: Sequence[str], cwd: Path, log: Path | None) -> Result:
    if log is None:
        done = subprocess.run(tuple(argv), cwd=cwd, capture_output=True, text=True, check=False)
        return Result(done.returncode, done.stdout, done.stderr)
    with log.open("a") as sink:
        sink.write(f"$ {shlex.join(argv)}\n")
        sink.flush()
        done = subprocess.run(
            tuple(argv), cwd=cwd, stdout=sink, stderr=subprocess.STDOUT, text=True, check=False
        )
    return Result(done.returncode)


@dataclass(frozen=True)
class Options:
    ref: str
    pr: int | None
    execute: bool
    allow_network: bool
    install: bool
    only: str | None
    skip_verify: bool
    native_settings: Path
    builds: Path
    applications: Path
    signing_identity: str | None
    wheelhouse: Path | None
    allow_non_main_install: bool
    allow_downgrade: bool

    @property
    def target(self) -> str:
        return f"origin/pr/{self.pr}" if self.pr is not None else self.ref

    @property
    def native(self) -> bool:
        return self.only in (None, "native")

    @property
    def desktop(self) -> bool:
        return self.only in (None, "desktop")


def valid_identities(output: str) -> dict[str, str]:
    """``security find-identity -v -p codesigning``'s valid identities: name -> SHA-1."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        match = _IDENTITY_LINE.match(line)
        if match:
            found.setdefault(match.group(2), match.group(1).upper())
    return found


def summaries_in(log: Path) -> list[str]:
    """pytest's closing lines in a verify log ("6450 passed, 151 skipped in 1326.2s")."""
    if not log.is_file():
        return []
    return [
        match.group(1)
        for line in log.read_text(errors="replace").splitlines()
        if (match := _SUMMARY.match(line.strip()))
    ]


def tail(log: Path, lines: int = 25) -> str:
    if not log.is_file():
        return ""
    return "\n".join(log.read_text(errors="replace").splitlines()[-lines:])


def signature(stderr: str) -> dict[str, Any]:
    """What ``codesign -dv --verbose=2`` says about a signature."""
    fields: dict[str, Any] = {"ad_hoc": False, "authority": None, "identifier": None}
    for line in stderr.splitlines():
        key, _, value = line.partition("=")
        if key == "Signature" and value.strip() == "adhoc":
            fields["ad_hoc"] = True
        elif key == "Authority" and fields["authority"] is None:
            fields["authority"] = value.strip()
        elif key == "Identifier":
            fields["identifier"] = value.strip()
    return fields


def _refuse(status: int, message: str) -> NoReturn:
    raise Refusal(status, message)


@dataclass
class Refresh:
    options: Options
    run: Runner
    home: Path
    clock: Callable[[], datetime]
    report: dict[str, Any] = field(default_factory=dict)
    run_dir: Path | None = None

    # -- plumbing -----------------------------------------------------------------------------

    def _do(self, argv: Sequence[str], what: str, log: Path | None = None) -> Result:
        print(f"# {what}", flush=True)
        result = self.run(argv, REPO, log)
        if result.returncode != 0:
            detail = tail(log) if log is not None else (result.stderr or result.stdout)
            raise Refusal(EX_FAILED, f"{what} failed:\n{detail.strip()[-3000:]}")
        return result

    def _git(self, *args: str) -> Result:
        return self.run(("git", *args), REPO, None)

    def _git_out(self, *args: str) -> str:
        result = self._git(*args)
        if result.returncode != 0:
            raise Refusal(EX_FAILED, f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip()

    def _is_ancestor(self, older: str, newer: str) -> bool | None:
        code = self._git("merge-base", "--is-ancestor", older, newer).returncode
        return {0: True, 1: False}.get(code)

    def _expand(self, path: Path) -> Path:
        text = str(path)
        return self.home / text[2:] if text.startswith("~/") else path

    def _log(self, name: str) -> Path:
        assert self.run_dir is not None
        return self.run_dir / f"{name}.log"

    # -- the run ------------------------------------------------------------------------------

    def execute(self) -> int:
        options = self.options
        if self._git_out("status", "--porcelain"):
            _refuse(EX_USAGE, "the checkout has changes; mac-refresh builds clean commits only")
        branch = self._git("symbolic-ref", "-q", "--short", "HEAD")
        original = (
            branch.stdout.strip() if branch.returncode == 0 else self._git_out("rev-parse", "HEAD")
        )
        try:
            return self._execute(options)
        finally:
            detach: tuple[str, ...] = () if branch.returncode == 0 else ("--detach",)
            self._git("checkout", "--quiet", *detach, original)
            self._write_report()

    def _execute(self, options: Options) -> int:
        if options.allow_network:
            self._do(("git", "fetch", "--prune", "origin"), "fetching origin")
            if options.pr is not None:
                spec = f"+refs/pull/{options.pr}/head:refs/remotes/origin/pr/{options.pr}"
                self._do(("git", "fetch", "origin", spec), f"fetching pull request #{options.pr}")
        commit = self._git_out("rev-parse", "--verify", "--quiet", f"{options.target}^{{commit}}")
        on_main = self._is_ancestor(commit, "origin/main") is True
        stamp = self.clock().strftime("%Y%m%dT%H%M%SZ")
        builds = self._expand(options.builds)
        builds.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run_dir = builds / "runs" / f"{stamp}-{commit[:12]}"
        self.run_dir.mkdir(mode=0o700, parents=True)
        self.report.update(
            {
                "schema_version": REPORT_SCHEMA,
                "started_at": self.clock().isoformat(),
                "target": options.target,
                "commit": commit,
                "on_main": on_main,
                "run_directory": str(self.run_dir),
                "steps": {},
            }
        )
        self._do(("git", "checkout", "--quiet", "--detach", commit), f"checking out {commit[:12]}")
        self._environment()
        if options.skip_verify:
            self._step("verify", "skipped", reason="--skip-verify")
        else:
            self._verify()
        identity = self._identity()
        built: dict[str, Path] = {}
        if options.native:
            if (app := self._native(identity)) is not None:
                built["native"] = app
        if options.desktop:
            if (app := self._desktop(identity)) is not None:
                built["desktop"] = app
        if options.install and built:
            self._install(built, commit, on_main, builds)
        elif options.install:
            self._step("install", "skipped", reason="nothing was built")
        return 0

    def _step(self, name: str, state: str, **details: Any) -> None:
        self.report["steps"][name] = {"state": state, **details}

    def _environment(self) -> None:
        python = REPO / ".venv" / "bin" / "python"
        log = self._log("environment")
        if not python.exists():
            if not self.options.allow_network:
                _refuse(EX_USAGE, "no .venv yet: the first run needs --allow-network")
            self._do((str(HOMEBREW_PYTHON), "-m", "venv", str(REPO / ".venv")), "the .venv", log)
            self._do((str(python), "-m", "pip", "install", "-U", "pip"), "pip, updated", log)
        if self.options.allow_network:
            pip = (str(python), "-m", "pip", "install", "--disable-pip-version-check")
            self._do(
                (
                    *pip,
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--no-deps",
                    "-r",
                    "requirements/development-build.txt",
                ),
                "the pinned build tools scripts/verify builds wheels with",
                log,
            )
            self._do(
                (*pip, "-e", ".[dev]"),
                "the checkout's development environment (pip install -e '.[dev]')",
                log,
            )
            self._step("environment", "done", log=str(log))
        else:
            self._step("environment", "kept", reason="no --allow-network: the .venv as it is")

    def _verify(self) -> None:
        log = self._log("verify")
        try:
            self._do(("scripts/verify",), "scripts/verify (on macOS: the Swift app too)", log)
        except Refusal:
            self._step("verify", "failed", log=str(log), summaries=summaries_in(log))
            raise
        self._step("verify", "passed", log=str(log), summaries=summaries_in(log))

    def _identity(self) -> str:
        chosen = self.options.signing_identity
        if chosen is not None:
            self.report["signing_identity"] = {"identity": chosen, "source": "--signing-identity"}
            return chosen
        listed = self.run((SECURITY, "find-identity", "-v", "-p", "codesigning"), REPO, None)
        identity = valid_identities(listed.stdout).get(IDENTITY_NAME)
        if identity is None:
            print(
                f"# no {IDENTITY_NAME!r} in the keychains: signing ad-hoc, so macOS asks for the "
                "microphone again after this build (scripts/mac-signing-identity --execute)",
                flush=True,
            )
            self.report["signing_identity"] = {"identity": AD_HOC, "source": "ad-hoc fallback"}
            return AD_HOC
        self.report["signing_identity"] = {
            "identity": identity,
            "name": IDENTITY_NAME,
            "source": "keychain",
        }
        return identity

    def _signature(self, app: Path) -> dict[str, Any]:
        result = self.run((CODESIGN, "-dv", "--verbose=2", str(app)), REPO, None)
        return signature(result.stderr) if result.returncode == 0 else {"error": result.stderr}

    def _native(self, identity: str) -> Path | None:
        assert self.run_dir is not None
        settings = self._expand(self.options.native_settings)
        if not settings.is_file():
            self._step(
                "native",
                "skipped",
                reason=f"no build settings at {settings} (docs/mac-agent/README.md)",
            )
            return None
        (self.run_dir / "native").mkdir(mode=0o700)
        app = self.run_dir / "native" / NATIVE_APP
        log = self._log("native")
        argv = (
            "scripts/build-native-recording-app",
            "--output",
            str(app),
            "--settings",
            str(settings),
            "--source-root",
            str(REPO),
            "--python",
            str(REPO / ".venv" / "bin" / "python"),
            "--signing-identity",
            identity,
        )
        try:
            self._do(argv, "the Swift app (scripts/build-native-recording-app)", log)
            release = app.with_suffix(".runtime") / "release.json"
            verify = (str(REPO / ".venv" / "bin" / "python"), "-I", "-B", "-c", _VERIFY_NATIVE)
            self._do((*verify, str(release)), "its release, verified", log)
        except Refusal:
            self._step("native", "failed", log=str(log))
            raise
        self._step("native", "built", app=str(app), log=str(log), signature=self._signature(app))
        return app

    def _desktop(self, identity: str) -> Path | None:
        assert self.run_dir is not None
        options = self.options
        if options.wheelhouse is not None:
            source: tuple[str, ...] = ("--wheelhouse", str(options.wheelhouse))
        elif options.allow_network:
            source = ("--allow-network",)
        else:
            self._step("desktop", "skipped", reason="its pinned wheels need --allow-network")
            return None
        output = self.run_dir / "desktop"
        log = self._log("desktop")
        argv = (
            "scripts/build-desktop-bundle",
            "--execute",
            *source,
            "--output",
            str(output),
            "--signing-identity",
            identity,
        )
        try:
            self._do(argv, "voice-lab-desktop's bundle (scripts/build-desktop-bundle)", log)
        except Refusal:
            self._step("desktop", "failed", log=str(log))
            raise
        app = output / "dist" / DESKTOP_APP
        record_path = output / "build-desktop-bundle.json"
        record = json.loads(record_path.read_text()) if record_path.is_file() else {}
        self._step(
            "desktop",
            "built",
            app=str(app),
            log=str(log),
            archive=record.get("archive"),
            archive_sha256=record.get("archive_sha256"),
            signature=self._signature(app),
        )
        return app

    def _install(self, built: dict[str, Path], commit: str, on_main: bool, builds: Path) -> None:
        assert self.run_dir is not None
        options = self.options
        if not on_main and not options.allow_non_main_install:
            _refuse(
                EX_USAGE,
                f"{commit[:12]} is not on origin/main: not installed (built in "
                f"{self.run_dir}; --allow-non-main-install, with the owner's go-ahead)",
            )
        record_path = builds / INSTALLED
        installed: dict[str, Any] = (
            json.loads(record_path.read_text()) if record_path.is_file() else {}
        )
        previous = installed.get("native", {}).get("commit")
        if "native" in built and previous and previous != commit and not options.allow_downgrade:
            newer = self._is_ancestor(commit, previous)
            if newer is not False:
                _refuse(
                    EX_USAGE,
                    f"the installed Swift app is from {previous[:12]}, which "
                    + ("is newer than" if newer else "cannot be compared with")
                    + f" {commit[:12]}: an older native build cannot open a library a newer "
                    "one has opened. Not installed (--allow-downgrade, with the owner's "
                    "go-ahead).",
                )
        applications = self._expand(options.applications)
        done: dict[str, Any] = {}
        for kind, app in built.items():
            print(f"# installing {app.name} into {applications}", flush=True)
            done[kind] = self._install_app(app, applications)
            installed[kind] = {
                "commit": commit,
                "run_directory": str(self.run_dir),
                "app": done[kind]["installed"],
                "installed_at": self.clock().isoformat(),
            }
        record_path.write_text(json.dumps(installed, indent=2, sort_keys=True) + "\n")
        self._step("install", "done", apps=done)

    def _install_app(self, built: Path, applications: Path) -> dict[str, Any]:
        """Copy ``built`` into ``applications`` (signature intact); the app it replaces is
        moved into this run's ``replaced/``."""
        assert self.run_dir is not None
        target = applications / built.name
        staging = applications / f".{built.name}.installing"
        if staging.exists():
            shutil.rmtree(staging)
        self._do((DITTO, str(built), str(staging)), f"copying {built.name}")
        replaced = None
        if target.exists() or target.is_symlink():
            aside = self.run_dir / "replaced"
            aside.mkdir(mode=0o700, exist_ok=True)
            replaced = aside / built.name
            shutil.move(str(target), str(replaced))
        os.rename(staging, target)
        return {"installed": str(target), "replaced": str(replaced) if replaced else None}

    # -- the report ---------------------------------------------------------------------------

    def _write_report(self) -> None:
        if self.run_dir is None:
            return
        self.report["finished_at"] = self.clock().isoformat()
        (self.run_dir / "report.json").write_text(
            json.dumps(self.report, indent=2, sort_keys=True) + "\n"
        )
        (self.run_dir / "report.md").write_text(render_markdown(self.report))


def render_markdown(report: dict[str, Any]) -> str:
    """The report as a short Markdown summary (what ``gh pr comment --body-file`` posts)."""
    commit = str(report.get("commit", ""))
    lines = [
        f"**mac-refresh** on the Mac: `{report.get('target')}` at `{commit[:12]}`"
        + ("" if report.get("on_main") else " (not on `main`)"),
        "",
    ]
    identity = report.get("signing_identity", {})
    for name, step in report.get("steps", {}).items():
        state = step.get("state")
        line = f"- **{name}**: {state}"
        if step.get("reason"):
            line += f" ({step['reason']})"
        if step.get("summaries"):
            line += ": " + "; ".join(step["summaries"])
        sig = step.get("signature")
        if isinstance(sig, dict) and "ad_hoc" in sig:
            line += ", signed " + ("ad-hoc" if sig["ad_hoc"] else f"by {sig.get('authority')}")
        if step.get("archive_sha256"):
            line += f", zip sha256 `{step['archive_sha256']}`"
        if name == "install" and isinstance(step.get("apps"), dict):
            line += ": " + ", ".join(f"`{app['installed']}`" for app in step["apps"].values())
        if state == "failed" and step.get("log"):
            line += f" (log: `{step['log']}`)"
        lines.append(line)
    if identity.get("source") == "ad-hoc fallback":
        lines.append(
            "- signing: ad-hoc (no `voice-lab local signing` identity yet), so macOS asks for "
            "the microphone again"
        )
    if report.get("failure"):
        lines += ["", "```", str(report["failure"])[-1500:], "```"]
    lines += ["", f"Run directory: `{report.get('run_directory')}`", ""]
    return "\n".join(lines)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(EX_USAGE)


def parse(argv: Sequence[str] | None) -> Options:
    ap = _Parser(
        prog="mac-refresh",
        description="Refresh voice-lab's macOS apps from a commit (the plan unless --execute).",
    )
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--ref", default="origin/main", help="the commit (default origin/main)")
    target.add_argument("--pr", type=int, help="a pull request's head")
    ap.add_argument("--execute", action="store_true", help="run it (macOS only)")
    ap.add_argument(
        "--allow-network",
        action="store_true",
        help="fetch, pip install -e '.[dev]', and the bundle's pinned wheels",
    )
    ap.add_argument("--wheelhouse", type=Path, help="the bundle's pinned wheels from DIR only")
    ap.add_argument("--install", action="store_true", help="install the apps it built")
    ap.add_argument("--only", choices=("native", "desktop"), help="build one app only")
    ap.add_argument("--skip-verify", action="store_true", help="do not run scripts/verify")
    ap.add_argument("--native-settings", type=Path, default=DEFAULT_SETTINGS)
    ap.add_argument("--builds", type=Path, default=DEFAULT_BUILDS)
    ap.add_argument("--applications", type=Path, default=Path("/Applications"))
    ap.add_argument(
        "--signing-identity",
        help=f"'-' (ad-hoc), or a certificate's SHA-1 or name (default: {IDENTITY_NAME!r} "
        "when the keychains hold it, else ad-hoc)",
    )
    ap.add_argument("--allow-non-main-install", action="store_true")
    ap.add_argument("--allow-downgrade", action="store_true")
    args = ap.parse_args(argv)
    if args.pr is not None and args.pr <= 0:
        ap.error("--pr: a pull request number")
    if args.signing_identity is not None and not _SIGNING_IDENTITY.fullmatch(args.signing_identity):
        ap.error("--signing-identity: '-', a certificate's SHA-1 or a plain certificate name")
    if args.ref.startswith("-"):
        ap.error("--ref: a commit, branch or tag")
    return Options(
        ref=args.ref,
        pr=args.pr,
        execute=args.execute,
        allow_network=args.allow_network,
        install=args.install,
        only=args.only,
        skip_verify=args.skip_verify,
        native_settings=args.native_settings,
        builds=args.builds,
        applications=args.applications,
        signing_identity=args.signing_identity,
        wheelhouse=args.wheelhouse.absolute() if args.wheelhouse else None,
        allow_non_main_install=args.allow_non_main_install,
        allow_downgrade=args.allow_downgrade,
    )


def preview(options: Options, home: Path) -> None:
    def expand(path: Path) -> Path:
        text = str(path)
        return home / text[2:] if text.startswith("~/") else path

    settings = expand(options.native_settings)
    print(f"mac-refresh: the plan for {options.target} (nothing changes without --execute).")
    fetch = "fetch origin" if options.allow_network else "no fetch (no --allow-network)"
    print(f"1. {fetch}; check out the commit, detached; return to this checkout afterwards")
    print("2. the checkout's .venv (Homebrew's Python 3.12, pip install -e '.[dev]')")
    print(
        "3. " + ("no scripts/verify (--skip-verify)" if options.skip_verify else "scripts/verify")
    )
    identity = options.signing_identity or (
        f"{IDENTITY_NAME!r} if the keychains hold it, else ad-hoc"
    )
    print(f"4. signing identity: {identity}")
    if options.native:
        state = "found" if settings.is_file() else "missing: the Swift app would be skipped"
        print(f"5. the Swift app, with the build settings {settings} ({state})")
    if options.desktop:
        print("6. voice-lab-desktop's bundle (scripts/build-desktop-bundle)")
    if options.install:
        print(
            f"7. install into {expand(options.applications)}: main commits only; never an "
            "older Swift app over a newer one"
        )
    print(f"builds: {expand(options.builds)}/runs/<UTC>-<commit>")
    print("Run with --execute --allow-network (docs/mac-agent/README.md).")


def main(
    argv: Sequence[str] | None = None,
    *,
    platform: str = sys.platform,
    run: Runner = run_command,
    home: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    exists: Callable[[Path], bool] = os.path.exists,
) -> int:
    options = parse(argv)
    home = home or Path.home()
    if not options.execute:
        preview(options, home)
        return 0
    refresh = Refresh(options, run, home, clock)
    try:
        if platform != "darwin":
            _refuse(EX_UNAVAILABLE, "mac-refresh builds macOS apps on macOS only; this is not")
        missing = [
            str(tool)
            for tool in (HOMEBREW_PYTHON, Path(SECURITY), Path(CODESIGN), Path(DITTO))
            if not exists(tool)
        ]
        if missing:
            _refuse(
                EX_UNAVAILABLE,
                "missing: " + ", ".join(missing) + " (brew install python@3.12; xcode-select "
                "--install)",
            )
        status = refresh.execute()
    except Refusal as exc:
        refresh.report["failure"] = str(exc)
        refresh._write_report()
        print(f"mac-refresh: {exc}", file=sys.stderr)
        if refresh.run_dir is not None:
            print(f"report: {refresh.run_dir / 'report.md'}", file=sys.stderr)
        return exc.status
    assert refresh.run_dir is not None
    print(render_markdown(refresh.report))
    print(f"report: {refresh.run_dir / 'report.md'}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

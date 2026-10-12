"""Create, once, the local code-signing identity this Mac's builds of voice-lab use (D-043).

    scripts/mac-signing-identity              what it would do; nothing changes
    scripts/mac-signing-identity --execute    create it in the login keychain (once)
    scripts/mac-signing-identity --show       print its SHA-1 (exit 1 when there is none)

The identity is a self-signed certificate for code signing named ``voice-lab local signing``:
RSA 3072, ten years, key usage digitalSignature, extended key usage codeSigning, not a CA.
Its private key is generated in a private temporary directory, imported into the login
keychain (usable by ``codesign`` without asking), and deleted from disk; it is never
exported and never leaves this Mac. macOS asks once for the login password, to trust the
certificate for code signing (the user's trust settings only, not the system's); then a test
signature checks the identity works (macOS may ask to let ``codesign`` use the key: choose
Always Allow).

Why: macOS binds a microphone grant (TCC) to the app's designated requirement. An ad-hoc
signature's requirement is its code hash, which every build changes, so macOS asks again
after each build. A certificate's requirement is the bundle identifier and the certificate,
which survive rebuilds. ``scripts/mac-refresh`` signs with this identity when it exists;
the builders' default stays ad-hoc. Nothing here costs money: no Developer ID, no
notarization (D-028).

Exit status: 0 done (or already there), 1 a step failed, 64 usage, 69 not on macOS or a tool
is missing.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

NAME: Final = "voice-lab local signing"
SECURITY: Final = "/usr/bin/security"
#: macOS's own LibreSSL: the PKCS#12 it writes is one ``security import`` reads.
OPENSSL: Final = "/usr/bin/openssl"
CODESIGN: Final = "/usr/bin/codesign"
PASSWORD_VARIABLE: Final = "VOICE_LAB_SIGNING_P12"

EX_FAILED: Final = 1
EX_USAGE: Final = 64
EX_UNAVAILABLE: Final = 69

OPENSSL_CONFIG: Final = f"""[req]
distinguished_name = subject
x509_extensions = codesign
prompt = no

[subject]
CN = {NAME}

[codesign]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
subjectKeyIdentifier = hash
"""

_IDENTITY_LINE: Final = re.compile(r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"(.*)"\s*$')


class Failure(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


#: ``run(argv, extra_environment)``: one command, its output captured.
type Runner = Callable[[Sequence[str], Mapping[str, str] | None], Result]


def run_command(argv: Sequence[str], extra: Mapping[str, str] | None = None) -> Result:
    environment = {**os.environ, **extra} if extra else None
    done = subprocess.run(tuple(argv), capture_output=True, text=True, check=False, env=environment)
    return Result(done.returncode, done.stdout, done.stderr)


def valid_identities(output: str) -> dict[str, str]:
    """``security find-identity -v -p codesigning``'s valid identities: name -> SHA-1."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        match = _IDENTITY_LINE.match(line)
        if match:
            found.setdefault(match.group(2), match.group(1).upper())
    return found


def _check(result: Result, what: str) -> Result:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise Failure(EX_FAILED, f"{what} failed: {detail}")
    return result


def find_identity(run: Runner) -> str | None:
    """The identity's SHA-1, when the keychains hold it as a valid code-signing identity."""
    listed = _check(
        run((SECURITY, "find-identity", "-v", "-p", "codesigning"), None),
        "listing the code-signing identities",
    )
    return valid_identities(listed.stdout).get(NAME)


def login_keychain(run: Runner) -> str:
    out = _check(run((SECURITY, "login-keychain"), None), "finding the login keychain").stdout
    path = out.strip().strip('"')
    if not path.startswith("/"):
        raise Failure(EX_FAILED, f"unexpected login keychain: {out.strip()!r}")
    return path


def creation_steps(keychain: str, work: Path) -> list[tuple[str, tuple[str, ...]]]:
    """The commands that create and trust the identity, in order (``work`` is private)."""
    key, cert, bundle = work / "key.pem", work / "cert.pem", work / "identity.p12"
    return [
        (
            "the key and the self-signed certificate",
            (
                OPENSSL,
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-sha256",
                "-days",
                "3650",
                "-nodes",
                "-config",
                str(work / "openssl.cnf"),
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ),
        ),
        (
            "the PKCS#12 bundle (its password from the environment, not the command line)",
            (
                OPENSSL,
                "pkcs12",
                "-export",
                "-inkey",
                str(key),
                "-in",
                str(cert),
                "-name",
                NAME,
                "-out",
                str(bundle),
                "-passout",
                f"env:{PASSWORD_VARIABLE}",
            ),
        ),
        (
            "the identity, into the login keychain (codesign may use its key)",
            (
                SECURITY,
                "import",
                str(bundle),
                "-k",
                keychain,
                "-f",
                "pkcs12",
                "-T",
                CODESIGN,
                "-P",
                "<the bundle's one-time password>",
            ),
        ),
        (
            "trusted for code signing, for this user (macOS asks for the login password)",
            (
                SECURITY,
                "add-trusted-cert",
                "-r",
                "trustRoot",
                "-p",
                "codeSign",
                "-k",
                keychain,
                str(cert),
            ),
        ),
    ]


def create(
    run: Runner,
    *,
    make_workdir: Callable[[], Path] = lambda: Path(tempfile.mkdtemp(prefix="voice-lab-sign-")),
) -> str:
    """Create the identity and return its SHA-1. The work directory is always removed."""
    keychain = login_keychain(run)
    work = make_workdir()
    password = secrets.token_urlsafe(24)
    try:
        os.chmod(work, 0o700)
        (work / "openssl.cnf").write_text(OPENSSL_CONFIG)
        for what, argv in creation_steps(keychain, work):
            print(f"# {what}", flush=True)
            if argv[:2] == (SECURITY, "import"):
                argv = (*argv[:-1], password)
            _check(run(argv, {PASSWORD_VARIABLE: password}), what)
        identity = find_identity(run)
        if identity is None:
            raise Failure(EX_FAILED, f"{NAME!r} was imported but is not a valid identity")
        probe = work / "probe"
        shutil.copyfile("/usr/bin/true", probe)
        print("# a test signature (macOS may ask to let codesign use the key)", flush=True)
        _check(run((CODESIGN, "--force", "--sign", identity, str(probe)), None), "test signing")
        _check(run((CODESIGN, "--verify", "--strict", str(probe)), None), "checking it")
        return identity
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _refuse(status: int, message: str) -> NoReturn:
    raise Failure(status, message)


def certificate_exists(run: Runner) -> bool:
    return run((SECURITY, "find-certificate", "-c", NAME), None).returncode == 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(EX_USAGE)


def preview() -> None:
    print(f"mac-signing-identity: the plan for {NAME!r} (nothing changes without --execute).")
    print("If the login keychain already holds it as a valid identity: nothing to do.")
    work = Path("<a private temporary directory>")
    for what, argv in creation_steps("<the login keychain>", work):
        print(f"# {what}\n{shlex.join(argv)}")
    print("then: a test signature with the new identity; the temporary directory is deleted.")


def main(
    argv: Sequence[str] | None = None,
    *,
    platform: str = sys.platform,
    run: Runner = run_command,
    exists: Callable[[str], bool] = os.path.exists,
) -> int:
    ap = _Parser(
        prog="mac-signing-identity",
        description=f"Create the local code-signing identity {NAME!r} (D-043).",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="create it (once)")
    mode.add_argument("--show", action="store_true", help="print its SHA-1")
    args = ap.parse_args(argv)
    if not args.execute and not args.show:
        preview()
        return 0
    try:
        if platform != "darwin":
            _refuse(EX_UNAVAILABLE, "the signing identity lives in a macOS keychain; not macOS")
        missing = [tool for tool in (SECURITY, OPENSSL, CODESIGN) if not exists(tool)]
        if missing:
            _refuse(EX_UNAVAILABLE, "missing: " + ", ".join(missing))
        identity = find_identity(run)
        if args.show:
            if identity is None:
                _refuse(EX_FAILED, f"no valid identity {NAME!r} (scripts/mac-signing-identity)")
            print(identity)
            return 0
        if identity is not None:
            print(f"{NAME!r} is already here: {identity}")
            return 0
        if certificate_exists(run):
            _refuse(
                EX_FAILED,
                f"a certificate {NAME!r} exists but is not a valid code-signing identity "
                "(untrusted, or its key is missing). Trust it for code signing in Keychain "
                f"Access, or remove it (security delete-identity -c '{NAME}') and run this "
                "again.",
            )
        identity = create(run)
    except Failure as exc:
        print(f"mac-signing-identity: {exc}", file=sys.stderr)
        return exc.status
    print(f"created {NAME!r}: {identity}")
    print("scripts/mac-refresh signs with it from now on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

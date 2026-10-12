"""Offline vault image staging and disposable loopback verification.

@canonical vault_container -- shared implementation of the two operator scripts.
Only the verifier creates Docker resources, all uniquely named and removed on exit.
Neither command downloads dependencies, pulls images, nor starts a tailnet sidecar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.storage_contracts import VaultNamespaceConfig, VaultServerConfig
from cole_voice_lab.storage_server import hash_token, new_token

REPOSITORY = Path(__file__).resolve().parents[1]


def cache_root(home: Path) -> Path:
    """The vault build cache, ``~/.cache/voice-lab``.

    Before the repository was renamed (2026-10) it was ``~/.cache/cole-voice-lab``.
    A host that has only that directory keeps using it, so its reviewed wheelhouse and
    base image stay found; ``private_cache`` checks whichever is chosen.
    """
    current = home / ".cache" / "voice-lab"
    previous = home / ".cache" / "cole-voice-lab"
    if not os.path.lexists(current) and os.path.lexists(previous):
        return previous
    return current


CACHE = cache_root(Path.home())
WHEELHOUSE = CACHE / "vault-linux-wheels"
PYTHON = REPOSITORY / ".venv/bin/python"
DATA_MOUNT = "/var/lib/voice-lab-vault"
CONFIG_MOUNT = "/run/secrets"
EXPECTED_PACKAGES = {
    "annotated_types": "0.8.0",
    "pydantic": "2.13.5",
    "pydantic_core": "2.46.5",
    "typing_extensions": "4.16.0",
    "typing_inspection": "0.4.4",
}
HEALTH_TIMEOUT = 60.0
COMMAND_TIMEOUT = 60.0
BUILD_TIMEOUT = 600.0
TEST_TIMEOUT = 180.0
CLEANUP_TIMEOUT = 10.0
DOCKER_SELECTORS = frozenset(
    {
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    }
)
CONTAINER_LIMITS = (
    "--pull=never",
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges:true",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
    "--user",
    "10001:10001",
    "--memory",
    "512m",
    "--cpus",
    "2",
    "--pids-limit",
    "128",
    "--log-driver",
    "json-file",
    "--log-opt",
    "max-size=10m",
    "--log-opt",
    "max-file=2",
)


class ContainerFailure(RuntimeError):
    """A local packaging or verification failure; messages never contain credentials."""


class ContainerInterrupted(KeyboardInterrupt):
    """A deferred termination, reported after all owned cleanup attempts finish."""

    def __init__(self, number: int, failures: list[str] | None = None) -> None:
        self.number = number
        detail = "; ".join(failures or [])
        super().__init__(
            f"VAULT.CONTAINER_INTERRUPTED: signal {number}"
            + (f"; cleanup failures: {detail}" if detail else "")
        )


def run(
    arguments: list[str],
    *,
    input_text: str | None = None,
    environment: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = COMMAND_TIMEOUT,
) -> str:
    """Run a bounded local command; failures never echo stdin or timeout output."""
    try:
        result = subprocess.run(
            arguments,
            input=input_text,
            text=True,
            capture_output=True,
            env=environment,
            cwd=cwd,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ContainerFailure(
            f"VAULT.CONTAINER_TIMEOUT: {arguments[0]} exceeded {timeout:g} seconds"
        ) from None
    if result.returncode:
        # Config stdin holds hashes only; credentials are never command arguments or output.
        detail = re.sub(r"cvlv_[A-Za-z0-9_-]{43}", "[REDACTED]", result.stderr + result.stdout)
        raise ContainerFailure(
            f"VAULT.CONTAINER_COMMAND: {arguments[0]} exited {result.returncode}: {detail.strip()}"
        )
    return result.stdout


@dataclass(frozen=True)
class LocalDocker:
    """The validated Colima endpoint and environment snapshot for one verifier run."""

    host: str
    environment: tuple[tuple[str, str], ...]

    def run(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> str:
        """Pin every daemon call to the accepted socket, including build and cleanup."""
        if arguments[0] != "docker":
            raise ContainerFailure("VAULT.DOCKER_COMMAND: expected a Docker command")
        return run(
            ["docker", "--host", self.host, *arguments[1:]],
            input_text=input_text,
            environment=dict(self.environment),
            timeout=timeout,
        )


def require_colima_socket(path: Path) -> None:
    """Refuse absent, linked, foreign-owned, or non-socket Colima endpoints."""
    metadata = path.lstat()
    if (
        path.resolve() != path
        or not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise ContainerFailure("VAULT.DOCKER_ENDPOINT: require the owned local Colima socket")


def resolve_local_docker(
    environment: Mapping[str, str] | None = None, home: Path | None = None
) -> LocalDocker:
    """Resolve only local metadata, before daemon contact; never invoke Docker discovery.

    The verifier accepts the current default Colima profile only. Environment selectors
    must be unset, so a remote or conflicting caller choice fails visibly rather than
    being silently redirected. Later config/environment changes cannot redirect calls.
    """
    snapshot = dict(os.environ if environment is None else environment)
    if any(snapshot.get(name) for name in DOCKER_SELECTORS):
        raise ContainerFailure(
            "VAULT.DOCKER_SELECTION: unset Docker host/context/config/TLS "
            "overrides; verification requires the local colima context"
        )
    docker_home = (Path.home() if home is None else home) / ".docker"
    socket_path = docker_home.parent / ".colima/default/docker.sock"
    expected = f"unix://{socket_path}"
    context_file = (
        docker_home / "contexts/meta" / hashlib.sha256(b"colima").hexdigest() / "meta.json"
    )
    try:
        configuration = json.loads((docker_home / "config.json").read_bytes())
        if configuration.get("currentContext", "default") != "colima":
            raise ContainerFailure("VAULT.DOCKER_SELECTION: current Docker context must be colima")
        context = json.loads(context_file.read_bytes())
        if context["Name"] != "colima" or context["Endpoints"]["docker"]["Host"] != expected:
            raise ContainerFailure(
                "VAULT.DOCKER_ENDPOINT: colima must name its exact local Unix socket"
            )
        require_colima_socket(socket_path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ContainerFailure(
            "VAULT.DOCKER_SELECTION: local Colima metadata/socket is unavailable "
            "or invalid; no daemon operation was attempted"
        ) from None
    frozen = {name: value for name, value in snapshot.items() if not name.startswith("DOCKER_")}
    frozen.update({"DOCKER_CONFIG": str(docker_home), "DOCKER_BUILDKIT": "0"})
    return LocalDocker(expected, tuple(sorted(frozen.items())))


def private_cache() -> Path:
    """Create the user-owned 0700 cache; refuse a linked or shared staging parent."""
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = CACHE.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or CACHE.resolve() != CACHE
    ):
        raise ContainerFailure("VAULT.STAGING_PATH: cache must be owned, symlink-free and 0700")
    return CACHE


def verified_wheels(wheelhouse: Path) -> list[Path]:
    """Validate both Linux architectures against the lock, with no network access.

    Raises ContainerFailure for missing, additional, linked, or hash-mismatched wheels.
    """
    if not wheelhouse.is_dir():
        raise ContainerFailure(
            "VAULT.WHEELS_MISSING: offline mode requires --wheelhouse. With explicit network "
            "approval, run: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pip download "
            "--require-hashes --only-binary=:all: --no-deps --implementation cp --python-version "
            "3.12 --abi cp312 --platform manylinux2014_x86_64 --platform manylinux2014_aarch64 "
            "-r requirements/vault-linux.txt --dest <private-wheelhouse>; download each core "
            "architecture separately if pip selects only one. See deploy/vault/README.md."
        )
    if wheelhouse.resolve().is_relative_to(REPOSITORY):
        raise ContainerFailure("VAULT.WHEELS_PATH: wheels must stay outside the repository")
    lock = (REPOSITORY / "requirements/vault-linux.txt").read_text().replace("\\\n", " ")
    hashes: dict[str, set[str]] = {}
    for line in lock.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([\w-]+)==([\w.]+)\s+((?:--hash=sha256:[0-9a-f]{64}\s*)+)", line)
        if match is None:
            raise ContainerFailure("VAULT.WHEEL_LOCK: expected exact hashed requirements")
        name, version, pins = match.groups()
        name = name.replace("-", "_")
        if EXPECTED_PACKAGES.get(name) != version or name in hashes:
            raise ContainerFailure("VAULT.WHEEL_LOCK: dependency set or version differs")
        hashes[name] = set(re.findall(r"sha256:([0-9a-f]{64})", pins))
    if set(hashes) != set(EXPECTED_PACKAGES):
        raise ContainerFailure("VAULT.WHEEL_LOCK: dependency closure is incomplete")
    wheels = sorted(wheelhouse.glob("*.whl"))
    found: dict[str, set[str]] = {name: set() for name in EXPECTED_PACKAGES}
    for wheel in wheels:
        metadata = wheel.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ContainerFailure("VAULT.WHEEL_FILE: wheels must be regular single-link files")
        name, version, *tags = wheel.name[:-4].split("-")
        if EXPECTED_PACKAGES.get(name) != version or len(tags) != 3:
            raise ContainerFailure("VAULT.WHEEL_FILE: unexpected dependency wheel")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest not in hashes[name]:
            raise ContainerFailure(f"VAULT.WHEEL_HASH: {wheel.name} differs from the lock")
        if name == "pydantic_core":
            if tags[:2] != ["cp312", "cp312"] or "manylinux" not in tags[2]:
                raise ContainerFailure("VAULT.WHEEL_PLATFORM: core must be CPython 3.12 manylinux")
            architecture = next(
                (arch for arch in ("aarch64", "x86_64") if tags[2].endswith(arch)), ""
            )
        else:
            architecture = "pure" if tags[1:] == ["none", "any"] else ""
        if not architecture or architecture in found[name]:
            raise ContainerFailure("VAULT.WHEEL_PLATFORM: duplicate or unsupported wheel")
        found[name].add(architecture)
    expected = {
        name: {"aarch64", "x86_64"} if name == "pydantic_core" else {"pure"}
        for name in EXPECTED_PACKAGES
    }
    if found != expected:
        raise ContainerFailure(
            "VAULT.WHEELS_MISSING: cache needs pure wheels and BOTH "
            "CPython 3.12 manylinux core architectures; see deploy/vault/README.md"
        )
    return wheels


def copy_build_source(destination: Path) -> None:
    """Copy only package inputs; refuse linked source files and omit generated caches."""
    destination.mkdir(mode=0o700)
    for name in (
        "pyproject.toml",
        "setup.py",
        "MANIFEST.in",
        "README.md",
        "LICENSE",
        "src",
        "migrations",
    ):
        source = REPOSITORY / name
        paths = [source, *source.rglob("*")] if source.is_dir() else [source]
        for path in paths:
            relative = path.relative_to(REPOSITORY)
            if any(part == "__pycache__" or part.endswith(".egg-info") for part in relative.parts):
                continue
            metadata = path.lstat()
            target = destination / relative
            if stat.S_ISDIR(metadata.st_mode):
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            else:
                raise ContainerFailure("VAULT.BUILD_SOURCE: source contains a link or special file")


def stage_image(wheelhouse: Path) -> Path:
    """Build the current source offline in a private cache; return its staging root.

    The caller owns removal after success. Any failure removes the partial staging root.
    """
    wheels = verified_wheels(wheelhouse)
    stage = Path(tempfile.mkdtemp(prefix="vault-image-", dir=private_cache()))
    try:
        context = stage / "context"
        output = context / "wheels"
        output.mkdir(mode=0o700, parents=True)
        source = stage / "source"
        copy_build_source(source)
        run(
            [
                str(PYTHON),
                "-I",
                "-B",
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(output),
            ],
            cwd=source,
            timeout=BUILD_TIMEOUT,
            environment={
                **os.environ,
                "PIP_NO_INDEX": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        built = list(output.glob("cole_voice_lab-*.whl"))
        if len(built) != 1:
            raise ContainerFailure("VAULT.BUILD_WHEEL: expected one application wheel")
        application = built[0]
        (output / "wheel.sha256").write_text(
            f"{hashlib.sha256(application.read_bytes()).hexdigest()}  {application.name}\n"
        )
        for wheel in wheels:
            shutil.copyfile(wheel, output / wheel.name)
        shutil.copyfile(REPOSITORY / "requirements/vault-linux.txt", output / "vault-linux.txt")
        shutil.copyfile(REPOSITORY / "deploy/vault/Dockerfile", context / "Dockerfile")
        shutil.rmtree(source)
        return stage
    except BaseException:
        shutil.rmtree(stage)
        raise


def wait_healthy(url: str, *, timeout: float = HEALTH_TIMEOUT) -> None:
    """Wait for delayed Docker forwarding; raise ContainerFailure at the deadline."""
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        try:
            with opener.open(f"{url}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise ContainerFailure(
                    "VAULT.CONTAINER_HEALTH: loopback health deadline expired"
                ) from None
        if time.monotonic() >= deadline:
            raise ContainerFailure("VAULT.CONTAINER_HEALTH: loopback health deadline expired")
        time.sleep(0.1)


def check_hardening(inspection: dict[str, Any], port: int) -> None:
    """Refuse a running container whose actual Docker controls differ from the verifier."""
    host = inspection["HostConfig"]
    bindings = host["PortBindings"]
    if not (
        inspection["Config"]["User"] == "10001:10001"
        and host["ReadonlyRootfs"]
        and host["CapDrop"] == ["ALL"]
        and not host["Privileged"]
        and not host.get("CapAdd")
        and "no-new-privileges:true" in host["SecurityOpt"]
        and bindings == {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
        and host["Memory"] == 512 * 1024 * 1024
        and host["PidsLimit"] == 128
        and host["NanoCpus"] == 2_000_000_000
    ):
        raise ContainerFailure("VAULT.CONTAINER_HARDENING: running Docker controls differ")


def cleanup(
    commands: list[list[str]], stage: Path | None, *, run_command: Callable[..., str] | None = None
) -> None:
    """Delete private staging first; bound every cleanup and defer termination until done.

    SIGINT/SIGTERM received during cleanup are recorded, then raised after all attempts.
    An injected KeyboardInterrupt is handled equivalently. Other BaseExceptions retain
    their ordinary behavior. A daemon timeout cannot delay private token deletion.
    """
    failures: list[str] = []
    interrupted: int | None = None
    command_runner = run if run_command is None else run_command

    def defer(number: int, _frame: Any) -> None:
        nonlocal interrupted
        if interrupted is None:
            interrupted = number

    previous = {number: signal.signal(number, defer) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        if stage is not None:
            try:
                shutil.rmtree(stage)
            except OSError as error:
                failures.append(f"staging cleanup failed: {error}")
        for command in reversed(commands):
            try:
                command_runner(command, timeout=CLEANUP_TIMEOUT)
            except KeyboardInterrupt as error:
                defer(
                    error.number if isinstance(error, ContainerInterrupted) else signal.SIGINT, None
                )
                failures.append(f"removal interrupted: {' '.join(command)}")
            except (OSError, ContainerFailure) as error:
                # Allocations are registered before execution. An allocation that failed,
                # or a finished --rm helper, is already absent.
                if isinstance(error, ContainerFailure) and any(
                    marker in str(error).lower()
                    for marker in ("no such container:", "no such image:", "no such volume")
                ):
                    continue
                failures.append(str(error))
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    if interrupted is not None:
        raise ContainerInterrupted(interrupted, failures)
    if failures:
        raise ContainerFailure("VAULT.CONTAINER_CLEANUP: " + "; ".join(failures))


def verify_container(wheelhouse: Path) -> None:
    """Verify the installed image against portable conformance and remove owned resources.

    Raises ContainerFailure for any build, runtime, conformance or cleanup failure.
    Raw synthetic tokens exist only in memory and a private external staging file.
    """
    stage: Path | None = None
    removals: list[list[str]] = []
    suffix = uuid.uuid4().hex
    tag = f"cole-voice-lab-vault-test:{suffix}"
    container = f"cole-vault-test-{suffix}"
    data_volume, config_volume = f"{container}-data", f"{container}-config"
    verified_wheels(wheelhouse)
    docker = resolve_local_docker()
    try:
        base = (REPOSITORY / "deploy/vault/Dockerfile").read_text().split("FROM ", 1)[1].split()[0]
        docker.run(["docker", "image", "inspect", base])
        stage = stage_image(wheelhouse)
        removals.append(["docker", "image", "rm", tag])
        docker.run(
            [
                "docker",
                "build",
                "--network",
                "none",
                "--pull=false",
                "--force-rm",
                "-t",
                tag,
                str(stage / "context"),
            ],
            timeout=BUILD_TIMEOUT,
        )
        for volume in (data_volume, config_volume):
            removals.append(["docker", "volume", "rm", volume])
            docker.run(["docker", "volume", "create", volume])
        token = new_token()
        token_file = stage / "write-token"
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(token)
        config = VaultServerConfig(
            namespaces=(
                VaultNamespaceConfig(
                    name="test",
                    read_token_sha256=hash_token(new_token()),
                    write_token_sha256=hash_token(token),
                    quota_bytes=256 * 1024 * 1024,
                ),
            ),
            max_workers=8,
            request_timeout_seconds=30,
        )
        initialize = (
            "import os, pathlib, sys; "
            "root=pathlib.Path('/var/lib/voice-lab-vault'); "
            "paths=[root,root/'data',root/'logs',pathlib.Path('/run/secrets')]; "
            "[(p.mkdir(mode=0o700,exist_ok=True),os.chmod(p,0o700),os.chown(p,10001,10001)) "
            "for p in paths]; "
            "p=pathlib.Path('/run/secrets/vault_config'); p.write_bytes(sys.stdin.buffer.read()); "
            "os.chmod(p,0o600); os.chown(p,10001,10001)"
        )
        mounts = [
            "--mount",
            f"type=volume,src={data_volume},dst={DATA_MOUNT}",
            "--mount",
            f"type=volume,src={config_volume},dst={CONFIG_MOUNT},readonly",
        ]
        initializer = f"{container}-initialize"
        removals.append(["docker", "rm", "-f", initializer])
        docker.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--name",
                initializer,
                "--network",
                "none",
                "--read-only",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "FOWNER",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges:true",
                "--mount",
                f"type=volume,src={data_volume},dst={DATA_MOUNT}",
                "--mount",
                f"type=volume,src={config_volume},dst={CONFIG_MOUNT}",
                "-i",
                "--entrypoint",
                "python",
                tag,
                "-I",
                "-B",
                "-c",
                initialize,
            ],
            input_text=canonical_json_bytes(config).decode(),
        )
        # Provision without serving or exposing a port. Normal image CMD never provisions.
        provision = (
            "from pathlib import Path; from cole_voice_lab.storage_server import "
            "VaultServer,load_server_configuration; "
            "s=VaultServer(('127.0.0.1',0),config=load_server_configuration("
            "Path('/run/secrets/vault_config')),data_root=Path('/var/lib/voice-lab-vault/data'),"
            "access_log=Path('/var/lib/voice-lab-vault/logs/access.jsonl'),provision=True); "
            "s.server_close()"
        )
        provisioner = f"{container}-provision"
        removals.append(["docker", "rm", "-f", provisioner])
        docker.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                provisioner,
                "--network",
                "none",
                *CONTAINER_LIMITS,
                *mounts,
                "--entrypoint",
                "python",
                tag,
                "-I",
                "-B",
                "-c",
                provision,
            ]
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        removals.append(["docker", "rm", "-f", container])
        docker.run(
            [
                "docker",
                "create",
                "--name",
                container,
                *CONTAINER_LIMITS,
                *mounts,
                "-p",
                f"127.0.0.1:{port}:8080",
                tag,
            ]
        )
        docker.run(["docker", "start", container])
        url = f"http://127.0.0.1:{port}"
        wait_healthy(url)
        check_hardening(json.loads(docker.run(["docker", "inspect", container]))[0], port)
        docker.run(
            [
                "docker",
                "exec",
                container,
                "python",
                "-I",
                "-B",
                "-c",
                "import importlib.util,sys; import cole_voice_lab.storage_server; "
                "assert all(importlib.util.find_spec(n) is None for n in "
                "('cryptography','torch','PySide6')); "
                "assert not any(n.startswith(('cole_voice_lab.recording','cole_voice_lab.capture',"
                "'cole_voice_lab.finetune','cole_voice_lab.dataset',"
                "'cole_voice_lab.corpus','cole_voice_lab.worker',"
                "'cole_voice_lab.storage_sealing')) "
                "for n in sys.modules)",
            ]
        )
        result = run(
            [
                str(PYTHON),
                "-I",
                "-B",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/storage_conformance.py",
                "-k",
                "container-http",
            ],
            cwd=REPOSITORY,
            timeout=TEST_TIMEOUT,
            environment={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "COLE_VOICE_LAB_VAULT_CONTAINER_URL": url,
                "COLE_VOICE_LAB_VAULT_CONTAINER_TOKEN_FILE": str(token_file),
                "COLE_VOICE_LAB_VAULT_CONTAINER_NAMESPACE": "test",
            },
        )
        print(result, end="")
        print(
            json.dumps(
                {
                    "service": "vault-container-verify",
                    "status": "passed",
                    "hardening": "verified",
                    "namespace": "test",
                },
                sort_keys=True,
            )
        )
    finally:
        cleanup(removals, stage, run_command=docker.run)


def main(argv: list[str] | None = None) -> int:
    """Run stage, verify or image-pin validation; return 2 for a safe structured refusal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage", "verify", "check-tailscale-image"))
    parser.add_argument("--wheelhouse", type=Path, default=WHEELHOUSE)
    parser.add_argument("--image", help="digest-pinned tailscale/tailscale:v1.102.2 image")
    options = parser.parse_args(argv)
    previous_umask = os.umask(0o077)

    def stop(number: int, _frame: Any) -> None:
        raise ContainerInterrupted(number)

    previous_handler = signal.signal(signal.SIGTERM, stop)
    try:
        if options.command == "check-tailscale-image":
            value = options.image or os.environ.get("TAILSCALE_IMAGE", "")
            if re.fullmatch(r"tailscale/tailscale:v1\.102\.2@sha256:[0-9a-f]{64}", value) is None:
                raise ContainerFailure(
                    "VAULT.SIDECAR_PIN: require tailscale/tailscale:v1.102.2@sha256:<64hex>"
                )
            print(json.dumps({"status": "valid", "image": value}, sort_keys=True))
        elif options.command == "stage":
            stage = stage_image(options.wheelhouse)
            print(
                json.dumps(
                    {"stage": str(stage), "build_context": str(stage / "context")}, sort_keys=True
                )
            )
        else:
            verify_container(options.wheelhouse)
        return 0
    except KeyboardInterrupt as error:
        number = error.number if isinstance(error, ContainerInterrupted) else signal.SIGINT
        print(
            json.dumps(
                {
                    "service": "vault-container",
                    "error": str(error) or "VAULT.CONTAINER_INTERRUPTED: cleanup completed",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 128 + number
    except (ContainerFailure, OSError) as error:
        print(
            json.dumps({"service": "vault-container", "error": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline packaging boundary and failure cleanup; no Docker or network is used here."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def container_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "vault_container_test_subject", REPOSITORY / "scripts/vault_container.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _inspection(port: int) -> dict[str, Any]:
    return {
        "Config": {"User": "10001:10001"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "Privileged": False,
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]},
            "Memory": 512 * 1024 * 1024,
            "PidsLimit": 128,
            "NanoCpus": 2_000_000_000,
        },
    }


def test_missing_wheelhouse_refuses_before_docker_or_staging(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = []
    monkeypatch.setattr(container_module, "run", lambda *args, **kwargs: called.append(args))
    with pytest.raises(container_module.ContainerFailure, match="offline mode requires"):
        container_module.verify_container(tmp_path / "missing")
    assert called == []


@pytest.fixture
def wheel_cache(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    repository = tmp_path / "repository"
    requirements = repository / "requirements"
    requirements.mkdir(parents=True)
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir(mode=0o700)
    lines = []
    for name, version in container_module.EXPECTED_PACKAGES.items():
        tags = (
            ("cp312-cp312-manylinux2014_aarch64", "cp312-cp312-manylinux2014_x86_64")
            if name == "pydantic_core"
            else ("py3-none-any",)
        )
        hashes = []
        for tag in tags:
            wheel = wheelhouse / f"{name}-{version}-{tag}.whl"
            wheel.write_bytes(f"synthetic {wheel.name}".encode())
            hashes.append("--hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest())
        lines.append(f"{name}=={version} {' '.join(hashes)}")
    (requirements / "vault-linux.txt").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(container_module, "REPOSITORY", repository)
    return wheelhouse


def test_wheel_cache_requires_the_exact_closure_for_both_architectures(
    container_module: ModuleType, wheel_cache: Path
) -> None:
    wheels = container_module.verified_wheels(wheel_cache)
    assert len(wheels) == 6
    next(wheel_cache.glob("*x86_64.whl")).unlink()
    with pytest.raises(container_module.ContainerFailure, match="BOTH"):
        container_module.verified_wheels(wheel_cache)


def test_wheel_cache_rejects_changed_bytes(container_module: ModuleType, wheel_cache: Path) -> None:
    next(wheel_cache.glob("pydantic-*.whl")).write_bytes(b"changed synthetic wheel")
    with pytest.raises(container_module.ContainerFailure, match="WHEEL_HASH"):
        container_module.verified_wheels(wheel_cache)


def test_wheel_cache_rejects_undeclared_dependencies(
    container_module: ModuleType, wheel_cache: Path
) -> None:
    (wheel_cache / "surprise-1-py3-none-any.whl").write_bytes(b"synthetic extra dependency")
    with pytest.raises(container_module.ContainerFailure, match="unexpected dependency"):
        container_module.verified_wheels(wheel_cache)


def test_wheel_cache_rejects_links(
    container_module: ModuleType, wheel_cache: Path, tmp_path: Path
) -> None:
    wheel = next(wheel_cache.glob("pydantic-*.whl"))
    target = tmp_path / "outside-wheel"
    wheel.rename(target)
    wheel.symlink_to(target)
    with pytest.raises(container_module.ContainerFailure, match="regular single-link"):
        container_module.verified_wheels(wheel_cache)


def test_cleanup_treats_an_unallocated_registered_container_as_absent(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()

    def missing(_command: list[str], **_kwargs: Any) -> str:
        raise container_module.ContainerFailure("docker: No such container: synthetic")

    monkeypatch.setattr(container_module, "run", missing)
    container_module.cleanup([["docker", "rm", "synthetic"]], stage)
    assert not stage.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ReadonlyRootfs", False),
        ("CapDrop", []),
        ("CapAdd", ["SYS_ADMIN"]),
        ("Privileged", True),
        ("SecurityOpt", []),
        ("Memory", 0),
        ("NanoCpus", 0),
        ("PidsLimit", 0),
        ("PortBindings", {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "1234"}]}),
    ],
)
def test_actual_container_hardening_rejects_a_relaxed_control(
    container_module: ModuleType, field: str, value: Any
) -> None:
    inspection = _inspection(1234)
    container_module.check_hardening(inspection, 1234)
    inspection["HostConfig"][field] = value
    with pytest.raises(container_module.ContainerFailure, match="HARDENING"):
        container_module.check_hardening(inspection, 1234)


def test_actual_container_hardening_rejects_root(container_module: ModuleType) -> None:
    inspection = _inspection(1234)
    inspection["Config"]["User"] = "0:0"
    with pytest.raises(container_module.ContainerFailure, match="HARDENING"):
        container_module.check_hardening(inspection, 1234)


def test_cleanup_removes_credentials_even_when_docker_cleanup_fails(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "write-token").write_text("synthetic private placeholder")
    commands = [["image"], ["volume"], ["container"]]
    called = []

    def fail_first(command: list[str], **_kwargs: Any) -> str:
        called.append(command)
        if command == ["container"]:
            raise container_module.ContainerFailure("synthetic cleanup refusal")
        return ""

    monkeypatch.setattr(container_module, "run", fail_first)
    with pytest.raises(container_module.ContainerFailure, match="CLEANUP"):
        container_module.cleanup(commands, stage)
    assert called == list(reversed(commands))
    assert not stage.exists()


@pytest.mark.parametrize(
    "failing_command", ["initialize", "provision", "start", "health", "pytest"]
)
def test_verifier_failure_cleans_owned_resources_and_private_token(
    container_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failing_command: str,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    commands: list[list[str]] = []
    forwarded_port = 0

    def fake_run(command: list[str], **kwargs: Any) -> str:
        nonlocal forwarded_port
        commands.append(command)
        if command[:2] == ["docker", "run"]:
            operation = "initialize" if "0:0" in command else "provision"
        elif command[:2] == ["docker", "start"]:
            operation = "start"
        elif "pytest" in command:
            operation = "pytest"
        else:
            operation = "other"
        if operation == failing_command:
            raise container_module.ContainerFailure("synthetic failure")
        if command[:2] == ["docker", "create"]:
            forwarded_port = int(command[command.index("-p") + 1].split(":")[1])
        if command[:2] == ["docker", "inspect"]:
            return json.dumps([_inspection(forwarded_port)])
        return ""

    def fake_health(_url: str) -> None:
        if failing_command == "health":
            raise container_module.ContainerFailure("synthetic health failure")

    monkeypatch.setattr(container_module, "run", fake_run)
    monkeypatch.setattr(
        container_module, "resolve_local_docker", lambda: SimpleNamespace(run=fake_run)
    )
    monkeypatch.setattr(container_module, "verified_wheels", lambda _: [])
    monkeypatch.setattr(container_module, "stage_image", lambda _: stage)
    monkeypatch.setattr(container_module, "wait_healthy", fake_health)
    with pytest.raises(container_module.ContainerFailure, match="synthetic"):
        container_module.verify_container(tmp_path)
    assert not stage.exists()
    assert len([command for command in commands if command[:3] == ["docker", "volume", "rm"]]) == 2
    assert any(command[:3] == ["docker", "image", "rm"] for command in commands)
    assert not any("pull" in command or "tailscale" in command for command in commands)


def test_subprocess_failure_redacts_synthetic_token(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "cvlv_" + "a" * 43
    monkeypatch.setattr(
        container_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, token, token),
    )
    with pytest.raises(container_module.ContainerFailure) as raised:
        container_module.run(["synthetic"])
    assert token not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_health_deadline_is_bounded_without_network(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unavailable:
        def open(self, *_args: Any, **_kwargs: Any) -> None:
            raise OSError("synthetic not forwarded yet")

    monkeypatch.setattr(container_module.urllib.request, "build_opener", lambda *_: Unavailable())
    with pytest.raises(container_module.ContainerFailure, match="HEALTH"):
        container_module.wait_healthy("http://127.0.0.1:1234", timeout=0)


def test_staging_failure_removes_partial_private_source(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(container_module, "verified_wheels", lambda _: [])
    monkeypatch.setattr(container_module, "private_cache", lambda: tmp_path)

    def fail_copy(_destination: Path) -> None:
        raise container_module.ContainerFailure("synthetic unsafe source")

    monkeypatch.setattr(container_module, "copy_build_source", fail_copy)
    with pytest.raises(container_module.ContainerFailure, match="unsafe source"):
        container_module.stage_image(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_staging_rejects_linked_source(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "pyproject.toml").symlink_to(REPOSITORY / "pyproject.toml")
    monkeypatch.setattr(container_module, "REPOSITORY", repository)
    with pytest.raises(container_module.ContainerFailure, match="BUILD_SOURCE"):
        container_module.copy_build_source(tmp_path / "source")


@pytest.mark.parametrize(
    "image",
    [
        "tailscale/tailscale:latest",
        "tailscale/tailscale:v1.102.2",
        "evil/tailscale:v1.102.2@sha256:" + "a" * 64,
        "tailscale/tailscale:v1.102.3@sha256:" + "a" * 64,
    ],
)
def test_sidecar_pin_refuses_mutable_wrong_owner_or_wrong_version(
    container_module: ModuleType, image: str
) -> None:
    assert container_module.main(["check-tailscale-image", "--image", image]) == 2


def test_sidecar_pin_accepts_reviewed_version_with_digest(container_module: ModuleType) -> None:
    assert (
        container_module.main(
            ["check-tailscale-image", "--image", "tailscale/tailscale:v1.102.2@sha256:" + "a" * 64]
        )
        == 0
    )


def test_vault_image_runs_the_module_without_automatic_provisioning() -> None:
    dockerfile = (REPOSITORY / "deploy/vault/Dockerfile").read_text()
    entry = next(line for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT "))
    command = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    assert json.loads(entry.removeprefix("ENTRYPOINT ")) == [
        "python",
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.storage_server",
    ]
    assert "--provision" not in json.loads(command.removeprefix("CMD "))
    assert "--require-hashes" in dockerfile and "sha256sum --check wheel.sha256" in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_serve_configuration_exposes_only_https_and_never_funnel() -> None:
    configuration = json.loads((REPOSITORY / "deploy/vault/tailscale/serve.json").read_bytes())
    assert configuration == {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {"${TS_CERT_DOMAIN}:443": {"Handlers": {"/": {"Proxy": "http://vault:8080"}}}},
    }


def test_compose_keeps_join_secrets_and_node_state_out_of_vault_service() -> None:
    compose = (REPOSITORY / "deploy/vault/compose.yaml").read_text()
    vault, tail = compose.split("  tailscale:", 1)
    assert "ports:" not in compose and "network_mode:" not in compose
    assert "networks: [vault-internal]" in vault
    assert "VAULT_TS_STATE_DIR" not in vault and "TS_AUTHKEY" not in compose
    assert "networks: [vault-internal, egress]" in tail
    assert 'TS_AUTH_ONCE: "true"' in tail and 'TS_USERSPACE: "true"' in tail
    assert "TS_LOCAL_ADDR_PORT: 127.0.0.1:9002" in tail
    assert "vault-internal: {internal: true}" in tail
    assert compose.count("create_host_path: false") == 3
    join = (REPOSITORY / "deploy/vault/compose.join.yaml").read_text()
    assert "TS_AUTHKEY: file:/run/secrets/ts_authkey" in join
    assert "  vault:" not in join


@pytest.fixture
def colima_home(
    container_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    home = tmp_path / "synthetic-home"
    docker_home = home / ".docker"
    metadata = docker_home / "contexts/meta" / hashlib.sha256(b"colima").hexdigest()
    metadata.mkdir(parents=True)
    (docker_home / "config.json").write_text(json.dumps({"currentContext": "colima"}))
    (metadata / "meta.json").write_text(
        json.dumps(
            {
                "Name": "colima",
                "Endpoints": {"docker": {"Host": f"unix://{home}/.colima/default/docker.sock"}},
            }
        )
    )
    # Socket existence is a separate local filesystem check. Endpoint selection is
    # tested here entirely through synthetic metadata, before any socket/CLI access.
    monkeypatch.setattr(container_module, "require_colima_socket", lambda _path: None)
    return home


@pytest.mark.parametrize(
    "selectors",
    [
        {"DOCKER_HOST": "ssh://synthetic-remote"},
        {"DOCKER_HOST": "tcp://synthetic-remote:2376"},
        {"DOCKER_CONTEXT": "remote"},
        {"DOCKER_HOST": "unix:///synthetic/local.sock", "DOCKER_CONTEXT": "remote"},
        {"DOCKER_CONFIG": "/synthetic/alternate-config"},
        {"DOCKER_TLS_VERIFY": "1"},
    ],
)
def test_docker_selector_override_refuses_before_any_command_or_stage(
    container_module: ModuleType,
    colima_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    selectors: dict[str, str],
) -> None:
    for name in container_module.DOCKER_SELECTORS:
        monkeypatch.delenv(name, raising=False)
    for name, value in selectors.items():
        monkeypatch.setenv(name, value)
    resolve = container_module.resolve_local_docker
    monkeypatch.setattr(container_module, "resolve_local_docker", lambda: resolve(home=colima_home))
    monkeypatch.setattr(container_module, "verified_wheels", lambda _: [])
    monkeypatch.setattr(
        container_module, "stage_image", lambda _: pytest.fail("staged before refusal")
    )
    monkeypatch.setattr(
        container_module.subprocess, "run", lambda *a, **k: pytest.fail("command before refusal")
    )
    with pytest.raises(container_module.ContainerFailure, match="DOCKER_SELECTION"):
        container_module.verify_container(colima_home)


@pytest.mark.parametrize(
    "current,endpoint",
    [
        ("remote", "ssh://synthetic-remote"),
        ("colima", "ssh://synthetic-remote"),
        ("colima", "tcp://127.0.0.1:2375"),
        ("colima", "unix:///synthetic/unapproved.sock"),
        ("default", "unix:///var/run/docker.sock"),
    ],
)
def test_docker_active_context_or_endpoint_refuses_before_any_command_or_stage(
    container_module: ModuleType,
    colima_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: str,
    endpoint: str,
) -> None:
    docker_home = colima_home / ".docker"
    (docker_home / "config.json").write_text(json.dumps({"currentContext": current}))
    metadata = docker_home / "contexts/meta" / hashlib.sha256(b"colima").hexdigest() / "meta.json"
    metadata.write_text(json.dumps({"Name": "colima", "Endpoints": {"docker": {"Host": endpoint}}}))
    resolve = container_module.resolve_local_docker
    monkeypatch.setattr(
        container_module, "resolve_local_docker", lambda: resolve(environment={}, home=colima_home)
    )
    monkeypatch.setattr(container_module, "verified_wheels", lambda _: [])
    monkeypatch.setattr(
        container_module, "stage_image", lambda _: pytest.fail("staged before refusal")
    )
    monkeypatch.setattr(
        container_module.subprocess, "run", lambda *a, **k: pytest.fail("command before refusal")
    )
    with pytest.raises(container_module.ContainerFailure, match="DOCKER_(SELECTION|ENDPOINT)"):
        container_module.verify_container(colima_home)


def test_docker_endpoint_stays_pinned_through_build_and_cleanup_after_ambient_change(
    container_module: ModuleType,
    colima_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment = {"PATH": "/synthetic/bin", "DOCKER_BUILDKIT": "1"}
    endpoint = container_module.resolve_local_docker(environment=environment, home=colima_home)
    expected = f"unix://{colima_home}/.colima/default/docker.sock"
    environment["DOCKER_CONTEXT"] = "remote"
    monkeypatch.setenv("DOCKER_HOST", "ssh://synthetic-later-change")
    (colima_home / ".docker/config.json").write_text('{"currentContext":"remote"}')
    calls = []

    def record(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(container_module.subprocess, "run", record)
    endpoint.run(["docker", "image", "inspect", "synthetic-image"])
    endpoint.run(["docker", "build", "synthetic-context"], timeout=container_module.BUILD_TIMEOUT)
    stage = tmp_path / "stage"
    stage.mkdir()
    container_module.cleanup(
        [["docker", "rm", "synthetic-container"]], stage, run_command=endpoint.run
    )
    assert len(calls) == 3 and not stage.exists()
    for command, arguments in calls:
        assert command[:3] == ["docker", "--host", expected]
        assert arguments["env"]["PATH"] == "/synthetic/bin"
        assert arguments["env"]["DOCKER_BUILDKIT"] == "0"
        assert arguments["env"]["DOCKER_CONFIG"] == str(colima_home / ".docker")
        assert not any(name in arguments["env"] for name in ("DOCKER_HOST", "DOCKER_CONTEXT"))
    assert [arguments["timeout"] for _, arguments in calls] == [
        container_module.COMMAND_TIMEOUT,
        container_module.BUILD_TIMEOUT,
        container_module.CLEANUP_TIMEOUT,
    ]


def test_colima_endpoint_must_be_an_owned_socket_not_a_file_or_link(
    container_module: ModuleType,
    tmp_path: Path,
) -> None:
    regular = tmp_path / "not-a-socket"
    regular.write_text("synthetic")
    linked = tmp_path / "linked"
    linked.symlink_to(regular)
    for path in (regular, linked):
        with pytest.raises(container_module.ContainerFailure, match="DOCKER_ENDPOINT"):
            container_module.require_colima_socket(path)


def test_stage_and_pin_check_do_not_resolve_docker(
    container_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        container_module,
        "resolve_local_docker",
        lambda: pytest.fail("Docker resolution outside verify"),
    )
    monkeypatch.setattr(container_module, "stage_image", lambda _: tmp_path)
    assert container_module.main(["stage"]) == 0
    assert (
        container_module.main(
            ["check-tailscale-image", "--image", "tailscale/tailscale:v1.102.2@sha256:" + "a" * 64]
        )
        == 0
    )


def test_cleanup_interrupt_attempts_remaining_removals_after_deleting_private_stage(
    container_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "write-token").write_text("synthetic private placeholder")
    commands = [["image"], ["volume"], ["container"]]
    called = []

    def interrupt_first(command: list[str], **kwargs: Any) -> str:
        assert not stage.exists(), "credentials must be gone before daemon cleanup"
        assert kwargs["timeout"] == container_module.CLEANUP_TIMEOUT
        called.append(command)
        if len(called) == 1:
            raise KeyboardInterrupt
        return ""

    monkeypatch.setattr(container_module, "run", interrupt_first)
    with pytest.raises(container_module.ContainerInterrupted) as raised:
        container_module.cleanup(commands, stage)
    assert raised.value.number == signal.SIGINT
    assert called == list(reversed(commands))
    assert not stage.exists()


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_cleanup_defers_real_signal_through_stage_removal_and_restores_handler(
    container_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    number: int,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "write-token").write_text("synthetic private placeholder")
    previous = signal.getsignal(number)
    remove = container_module.shutil.rmtree

    def interrupt_removal(path: Path) -> None:
        os.kill(os.getpid(), number)
        remove(path)

    monkeypatch.setattr(container_module.shutil, "rmtree", interrupt_removal)
    called = []
    monkeypatch.setattr(container_module, "run", lambda command, **kwargs: called.append(command))
    with pytest.raises(container_module.ContainerInterrupted) as raised:
        container_module.cleanup([["image"], ["container"]], stage)
    assert raised.value.number == number
    assert not stage.exists() and called == [["container"], ["image"]]
    assert signal.getsignal(number) == previous


def test_cleanup_timeout_is_translated_and_remaining_commands_still_run(
    container_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "write-token").write_text("synthetic private placeholder")
    calls = []

    def time_out_first(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert not stage.exists()
        calls.append(command)
        assert kwargs["timeout"] == container_module.CLEANUP_TIMEOUT
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="untrusted output")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(container_module.subprocess, "run", time_out_first)
    with pytest.raises(container_module.ContainerFailure, match="CONTAINER_TIMEOUT") as raised:
        container_module.cleanup([["image"], ["volume"], ["container"]], stage)
    assert calls == [["container"], ["volume"], ["image"]]
    assert "untrusted output" not in str(raised.value)
    assert not stage.exists()


def test_main_preserves_interrupt_exit_after_cleanup(
    container_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()

    def interrupted(_command: list[str], **_kwargs: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(container_module, "run", interrupted)
    monkeypatch.setattr(
        container_module,
        "verify_container",
        lambda _: container_module.cleanup([["container"]], stage),
    )
    assert container_module.main(["verify"]) == 130
    assert not stage.exists()

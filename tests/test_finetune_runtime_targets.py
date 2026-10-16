"""The F5-TTS runtime's targets: the Mac's MPS runtime and the PC's hash-locked ROCm one.

Nothing here downloads or runs pip: the runner and the downloads are fakes. The lock itself is
checked as a file: every package pinned and hash-bound, the S-2 stack (D-046) at its roots,
PyPI as its one index, and the header naming the commands provisioning runs.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

import cole_voice_lab.finetune as finetune_module
from cole_voice_lab.cli import build_parser, main
from cole_voice_lab.finetune import (
    FINETUNE_ROCM_INDEX_URLS,
    FINETUNE_ROCM_RUNTIME_PYTHON,
    FINETUNE_ROCM_SOURCE_BUILDS,
    FINETUNE_RUNTIME_TARGET_NAMES,
    FINETUNE_RUNTIME_TARGETS,
    FineTuneError,
    FineTuneRuntimeTarget,
    default_finetune_requirements,
    execute_finetune_runtime_provisioning,
    host_finetune_target,
    plan_finetune_runtime_provisioning,
)

REPOSITORY = Path(__file__).resolve().parents[1]
ROCM_LOCK = REPOSITORY / "requirements/runtimes/f5-tts-finetune-1.1.22-rocm7.2.txt"
ROCM_INPUT = REPOSITORY / "requirements/runtimes/f5-tts-finetune-1.1.22-rocm7.2/pyproject.toml"
MPS_LOCK = REPOSITORY / "requirements/runtimes/f5-tts-finetune-1.1.22-mps.txt"
BUILD_TOOLS = REPOSITORY / "requirements/development-build.txt"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$")
TORCH_FAMILY = ("torch", "torchaudio", "torchcodec", "triton-rocm")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock(text: str) -> tuple[list[str], dict[str, tuple[str, list[str]]], list[str]]:
    """The header's comment lines, each package's version and hashes, and the option lines."""
    lines = text.splitlines()
    header: list[str] = []
    while lines and lines[0].startswith("#"):
        header.append(lines.pop(0))
    packages: dict[str, tuple[str, list[str]]] = {}
    options: list[str] = []
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("--"):
            options.append(stripped)
            current = None
        elif line.startswith(" "):
            assert current is not None, line
            hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", stripped)
            assert hashes, line
            packages[current][1].extend(hashes)
        else:
            pin = _PIN.match(stripped.removesuffix(" \\").strip())
            assert pin is not None, f"not an exact pin: {line}"
            current = _canonical(pin.group(1))
            assert current not in packages, current
            packages[current] = (pin.group(2), [])
    return header, packages, options


def _mps_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in MPS_LOCK.read_text().splitlines():
        pin = _PIN.match(line.strip())
        if pin:
            versions[_canonical(pin.group(1))] = pin.group(2)
    return versions


# -- targets --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "target"),
    [("darwin", "macos-mps"), ("linux", "linux-rocm"), ("linux2", "linux-rocm")],
)
def test_each_host_has_its_target(platform: str, target: str) -> None:
    assert host_finetune_target(platform) == target


def test_another_host_must_name_its_target() -> None:
    with pytest.raises(FineTuneError) as refused:
        host_finetune_target("win32")
    assert refused.value.code == "FINETUNE.RUNTIME_UNAVAILABLE"
    assert "--target macos-mps or linux-rocm" in str(refused.value)


def test_this_host_has_a_target() -> None:
    expected = "macos-mps" if sys.platform == "darwin" else "linux-rocm"
    assert host_finetune_target() == expected


def test_the_cli_offers_exactly_the_targets() -> None:
    parser = build_parser()
    options = parser.parse_args(
        ["finetune", "provision-runtime", "--runtime-root", "/x", "--target", "linux-rocm"]
    )
    assert options.target == "linux-rocm" and options.build_requirements is None
    assert tuple(FINETUNE_RUNTIME_TARGETS) == FINETUNE_RUNTIME_TARGET_NAMES
    for name in FINETUNE_RUNTIME_TARGET_NAMES:
        assert (
            parser.parse_args(
                ["finetune", "provision-runtime", "--runtime-root", "/x", "--target", name]
            ).target
            == name
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["finetune", "provision-runtime", "--runtime-root", "/x", "--target", "cuda"]
        )


# -- plans ----------------------------------------------------------------------------------


def test_the_rocm_plan_installs_the_build_tools_then_the_hash_locked_lock(tmp_path: Path) -> None:
    plan = plan_finetune_runtime_provisioning(tmp_path / "rt", target="linux-rocm")
    root = (tmp_path / "rt").resolve()
    pip = (
        str(root / "bin/python"),
        "-B",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--require-virtualenv",
    )
    assert plan.target == "linux-rocm" and plan.create_venv is True
    assert plan.python == Path(FINETUNE_ROCM_RUNTIME_PYTHON)
    assert plan.requirements == ROCM_LOCK and plan.build_requirements == BUILD_TOOLS
    assert plan.install_commands == (
        (*pip, "--require-hashes", "--only-binary=:all:", "--no-deps", "-r", str(BUILD_TOOLS)),
        (
            *pip,
            "--require-hashes",
            "--no-build-isolation",
            "--only-binary=:all:",
            "--no-binary=antlr4-python3-runtime,encodec,transformers-stream-generator",
            "--extra-index-url",
            "https://download.pytorch.org/whl/rocm7.2",
            "--extra-index-url",
            "https://download.pytorch.org/whl/cpu",
            "-r",
            str(ROCM_LOCK),
        ),
    )
    # The same pinned weights and vocoder as the Mac's runtime.
    mac = plan_finetune_runtime_provisioning(
        tmp_path / "rt", target="macos-mps", requirements=ROCM_LOCK
    )
    assert plan.downloads == mac.downloads
    shown = json.loads(json.dumps(plan.to_json()))
    assert shown["target"] == "linux-rocm"
    assert shown["build_requirements"] == str(BUILD_TOOLS)
    assert shown["install_commands"] == [list(command) for command in plan.install_commands]
    assert [item["present"] for item in shown["downloads"]] == [False] * 4


def test_the_mac_plan_keeps_its_one_install(tmp_path: Path) -> None:
    (tmp_path / "rt/bin").mkdir(parents=True)
    (tmp_path / "rt/bin/python").write_text("")
    plan = plan_finetune_runtime_provisioning(tmp_path / "rt", target="macos-mps")
    assert plan.create_venv is False and plan.build_requirements is None
    assert plan.requirements == MPS_LOCK
    assert plan.python == Path("/opt/homebrew/opt/python@3.12/bin/python3.12")
    (command,) = plan.install_commands
    assert command[-2:] == ("-r", str(MPS_LOCK)) and "--require-hashes" not in command
    assert plan.to_json()["build_requirements"] is None


def test_the_default_target_is_the_hosts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(finetune_module.sys, "platform", "darwin")
    assert plan_finetune_runtime_provisioning(tmp_path / "rt").target == "macos-mps"
    monkeypatch.setattr(finetune_module.sys, "platform", "linux")
    assert plan_finetune_runtime_provisioning(tmp_path / "rt").target == "linux-rocm"


def test_explicit_files_replace_the_checkouts(tmp_path: Path) -> None:
    pins = tmp_path / "pins.txt"
    tools = tmp_path / "tools.txt"
    pins.write_text("f5-tts==1.1.22 --hash=sha256:" + "0" * 64 + "\n")
    tools.write_text("setuptools==80.9.0 --hash=sha256:" + "1" * 64 + "\n")
    plan = plan_finetune_runtime_provisioning(
        tmp_path / "rt",
        target="linux-rocm",
        python=Path("/usr/bin/python3.12"),
        requirements=pins,
        build_requirements=tools,
    )
    assert plan.python == Path("/usr/bin/python3.12")
    assert [command[-1] for command in plan.install_commands] == [str(tools), str(pins)]


def test_a_hash_locked_target_without_source_builds_or_indexes_names_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        finetune_module.FINETUNE_RUNTIME_TARGETS,
        "linux-rocm",
        FineTuneRuntimeTarget(
            name="linux-rocm",
            python=FINETUNE_ROCM_RUNTIME_PYTHON,
            requirements=ROCM_LOCK.relative_to(REPOSITORY).as_posix(),
            build_requirements=BUILD_TOOLS.relative_to(REPOSITORY).as_posix(),
        ),
    )
    plan = plan_finetune_runtime_provisioning(tmp_path / "rt", target="linux-rocm")
    lock = plan.install_commands[1]
    assert lock[-4:] == ("--no-build-isolation", "--only-binary=:all:", "-r", str(ROCM_LOCK))
    assert not [item for item in lock if item.startswith(("--no-binary", "--extra-index-url"))]


def test_missing_files_are_refused_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(FineTuneError) as refused:
        plan_finetune_runtime_provisioning(
            tmp_path / "rt", target="linux-rocm", build_requirements=tmp_path / "none.txt"
        )
    assert refused.value.code == "FINETUNE.REQUIREMENTS_MISSING"
    assert "none.txt" in str(refused.value)
    with pytest.raises(FineTuneError) as refused:
        plan_finetune_runtime_provisioning(
            tmp_path / "rt", target="linux-rocm", requirements=tmp_path / "gone.txt"
        )
    assert refused.value.code == "FINETUNE.REQUIREMENTS_MISSING"
    # A wheel install has no checkout: the defaults are missing, and the message names the option.
    monkeypatch.setitem(
        finetune_module.FINETUNE_RUNTIME_TARGETS,
        "linux-rocm",
        FineTuneRuntimeTarget(
            name="linux-rocm",
            python=FINETUNE_ROCM_RUNTIME_PYTHON,
            requirements="requirements/runtimes/absent.txt",
            build_requirements="requirements/absent-build.txt",
        ),
    )
    with pytest.raises(FineTuneError) as refused:
        default_finetune_requirements("linux-rocm")
    assert "pass --requirements" in str(refused.value)
    with pytest.raises(FineTuneError) as refused:
        plan_finetune_runtime_provisioning(tmp_path / "rt", requirements=ROCM_LOCK)
    assert "pass --build-requirements" in str(refused.value)


# -- execution with fakes -------------------------------------------------------------------


def _fake_pins(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    import hashlib

    served = {
        "model_1250000.safetensors": b"w" * 10,
        "vocab.txt": b"v" * 4,
        "config.yaml": b"c" * 3,
        "pytorch_model.bin": b"m" * 7,
    }
    for prefix, name in (
        ("FINETUNE_PRETRAINED", "model_1250000.safetensors"),
        ("FINETUNE_PRETRAINED_VOCAB", "vocab.txt"),
        ("FINETUNE_VOCODER_CONFIG", "config.yaml"),
        ("FINETUNE_VOCODER_MODEL", "pytorch_model.bin"),
    ):
        monkeypatch.setattr(
            finetune_module, f"{prefix}_SHA256", hashlib.sha256(served[name]).hexdigest()
        )
        monkeypatch.setattr(finetune_module, f"{prefix}_SIZE_BYTES", len(served[name]))
    return served


def test_execution_runs_the_venv_then_each_install_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = _fake_pins(monkeypatch)
    described: list[Path] = []

    def describe(root: Path) -> str:
        described.append(root)
        return "the runtime"

    monkeypatch.setattr(finetune_module, "describe_finetune_runtime", describe)
    plan = plan_finetune_runtime_provisioning(tmp_path / "rt", target="linux-rocm")
    ran: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> int:
        ran.append(command)
        if command[1:4] == ("-B", "-m", "venv"):
            Path(command[4]).mkdir(parents=True)
        return 0

    runtime = execute_finetune_runtime_provisioning(
        plan,
        allow_downloads=True,
        runner=runner,
        opener=lambda url: io.BytesIO(served[url.rsplit("/", 1)[1]]),
    )
    assert runtime == "the runtime" and described == [plan.runtime_root]
    assert ran == [
        (FINETUNE_ROCM_RUNTIME_PYTHON, "-B", "-m", "venv", str(plan.runtime_root)),
        *plan.install_commands,
    ]
    assert (plan.runtime_root / "pretrained/vocos-mel-24khz/pytorch_model.bin").is_file()


def test_a_failed_install_stops_before_the_lock_and_the_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_finetune_runtime_provisioning(tmp_path / "rt", target="linux-rocm")
    ran: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> int:
        ran.append(command)
        if command[1:4] == ("-B", "-m", "venv"):
            Path(command[4]).mkdir(parents=True)
            return 0
        return 1

    def no_downloads(url: str) -> io.BytesIO:
        raise AssertionError(url)

    with pytest.raises(FineTuneError) as failed:
        execute_finetune_runtime_provisioning(
            plan, allow_downloads=True, runner=runner, opener=no_downloads
        )
    assert failed.value.code == "FINETUNE.RUNTIME_UNAVAILABLE"
    assert ran[1:] == [plan.install_commands[0]]  # the build tools, never the lock


def test_execution_needs_the_download_flag_before_anything_runs(tmp_path: Path) -> None:
    plan = plan_finetune_runtime_provisioning(tmp_path / "rt", target="linux-rocm")

    def runner(command: tuple[str, ...]) -> int:
        raise AssertionError(command)

    with pytest.raises(FineTuneError) as refused:
        execute_finetune_runtime_provisioning(plan, allow_downloads=False, runner=runner)
    assert refused.value.code == "FINETUNE.DOWNLOADS_NOT_ALLOWED"
    assert not (tmp_path / "rt").exists()


def test_the_cli_prints_the_plan_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "rt"
    assert (
        main(
            [
                "finetune",
                "provision-runtime",
                "--runtime-root",
                str(root),
                "--target",
                "linux-rocm",
                "--execute",
            ]
        )
        != 0
    )  # --execute alone refuses: installing downloads
    capsys.readouterr()
    assert (
        main(
            ["finetune", "provision-runtime", "--runtime-root", str(root), "--target", "linux-rocm"]
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["executed"] is False and shown["plan"]["target"] == "linux-rocm"
    assert shown["plan"]["requirements"] == str(ROCM_LOCK)
    assert not root.exists()


# -- the lock -------------------------------------------------------------------------------


def test_the_rocm_lock_pins_and_hashes_every_package() -> None:
    _, packages, options = _lock(ROCM_LOCK.read_text())
    assert len(packages) > 100
    for name, (version, hashes) in packages.items():
        assert hashes, name
        assert not version.startswith(("=", "<", ">", "~")), name
    # The S-2 stack (D-046), from PyTorch's indexes.
    assert packages["f5-tts"][0] == "1.1.22"
    assert packages["torch"][0] == "2.14.0+rocm7.2"
    assert packages["torchaudio"][0] == "2.11.0+rocm7.2"
    assert packages["torchcodec"][0] == "0.16.0+cpu"
    assert "triton-rocm" in packages
    # No CUDA build of anything, and the source builds are the lock's own packages.
    assert not [name for name in packages if name.startswith("nvidia-") or "cuda" in name]
    assert set(FINETUNE_ROCM_SOURCE_BUILDS) <= set(packages)
    # PyPI is the one index the file names; pip's format options are the command's, never the
    # file's (pip applies a file's in order, and `--only-binary :all:` last would cancel the
    # source builds' `--no-binary`).
    assert options == ["--index-url https://pypi.org/simple"]


def test_the_rocm_lock_keeps_the_macs_versions_beside_its_torch() -> None:
    _, packages, _ = _lock(ROCM_LOCK.read_text())
    mac = _mps_versions()
    differs = {
        name: (version, mac[name])
        for name, (version, _) in packages.items()
        if name in mac and version != mac[name]
    }
    assert set(differs) == {"torch", "torchaudio", "torchcodec"}
    assert {name: pair[0].split("+")[0] == pair[1] for name, pair in differs.items()} == {
        "torch": True,
        "torchaudio": True,
        "torchcodec": True,
    }


def test_the_lock_input_routes_only_the_torch_family_to_pytorchs_indexes() -> None:
    project = tomllib.loads(ROCM_INPUT.read_text())
    indexes = {item["name"]: item for item in project["tool"]["uv"]["index"]}
    assert all(item["explicit"] is True for item in indexes.values())
    assert tuple(item["url"] for item in indexes.values()) == FINETUNE_ROCM_INDEX_URLS
    sources = project["tool"]["uv"]["sources"]
    assert set(sources) == set(TORCH_FAMILY)
    assert {name: sources[name]["index"] for name in TORCH_FAMILY} == {
        "torch": "pytorch-rocm7.2",
        "torchaudio": "pytorch-rocm7.2",
        "torchcodec": "pytorch-cpu",
        "triton-rocm": "pytorch-rocm7.2",
    }
    roots = {
        _canonical(re.split(r"[=<>~ ]", item, maxsplit=1)[0]): item
        for item in project["project"]["dependencies"]
    }
    _, packages, _ = _lock(ROCM_LOCK.read_text())
    for name, requirement in roots.items():
        if "==" in requirement:
            assert requirement.split("==", 1)[1] == packages[name][0], name


def test_the_lock_header_names_the_compile_and_install_commands(tmp_path: Path) -> None:
    header, _, _ = _lock(ROCM_LOCK.read_text())
    text = " ".join(line.lstrip("#").strip().removesuffix("\\") for line in header)
    relative_input = ROCM_INPUT.relative_to(REPOSITORY).as_posix()
    assert f"uv pip compile {relative_input}" in text
    assert "-c requirements/runtimes/f5-tts-finetune-1.1.22-mps.txt" in text
    assert "--generate-hashes" in text and "--no-header" in text
    assert "voice-lab finetune provision-runtime --target linux-rocm" in text
    plan = plan_finetune_runtime_provisioning(tmp_path / "rt", target="linux-rocm")
    for command in plan.install_commands:
        for argument in command[5:]:  # after `<venv>/bin/python -B -m pip install`
            if argument.startswith("/"):
                argument = Path(argument).relative_to(REPOSITORY).as_posix()
            assert argument in text

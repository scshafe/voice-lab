from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import macos_only
import pytest

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.errors import ContractError, RuntimeUnavailableError, SecurityError
from cole_voice_lab.native_recording_application import (
    NativeRecordingApplicationSettings,
    _install_frozen_wheel,
    _private_tree_copy,
    create_native_recording_application,
    native_service_arguments,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.runtime import (
    RECORDING_PYTHON_DISTRIBUTIONS,
    VAULT_REQUIREMENTS_LOCK,
    _immutable_tree_sha256,
    _python_distribution_identity,
    _python_linked_library_identity,
    inspect_macho_load_commands,
    missing_recording_python_distribution_message,
    recording_distribution_files,
    recording_marker_environment,
    recording_python_distribution,
    requirement_marker_applies,
)


def macho_image(
    *,
    endian: str = "<",
    wide: bool = True,
    minimum: int = 0x000D0000,
    dependencies: tuple[str, ...] = (),
    rpaths: tuple[str, ...] = (),
    install_name: str | None = None,
    legacy: bool = False,
) -> bytes:
    commands = (
        [struct.pack(endian + "IIII", 0x24, 16, minimum, 0)]
        if legacy
        else [struct.pack(endian + "IIIIII", 0x32, 24, 1, minimum, 0, 0)]
    )
    for kind, value in (
        [(0xC, value) for value in dependencies]
        + [(0x8000001C, value) for value in rpaths]
        + ([(0xD, install_name)] if install_name else [])
    ):
        start = 12 if kind == 0x8000001C else 24
        encoded = value.encode() + b"\0"
        size = (start + len(encoded) + 7) // 8 * 8
        commands.append(
            struct.pack(endian + "III", kind, size, start)
            + bytes(start - 12)
            + encoded
            + bytes(size - start - len(encoded))
        )
    payload = b"".join(commands)
    magic = 0xFEEDFACF if wide else 0xFEEDFACE
    header = struct.pack(
        endian + "IIIIIII", magic, 0x0100000C, 0, 2, len(commands), len(payload), 0
    )
    return header + (bytes(4) if wide else b"") + payload


@pytest.mark.parametrize(
    "endian,wide,legacy",
    [("<", True, False), (">", True, True), ("<", False, True), (">", False, False)],
)
def test_macho_reads_actual_loader_commands_without_execution(
    tmp_path: Path, endian: str, wide: bool, legacy: bool
) -> None:
    path = tmp_path / "fixture"
    path.write_bytes(
        macho_image(
            endian=endian,
            wide=wide,
            legacy=legacy,
            minimum=0x001A0102,
            dependencies=("/usr/lib/libSystem.B.dylib",),
            rpaths=("@loader_path/lib",),
            install_name="@rpath/self",
        )
    )
    result = inspect_macho_load_commands(path)
    assert result.minimum_macos == (26, 1, 2)
    assert result.dependencies == ("/usr/lib/libSystem.B.dylib",)
    assert result.install_names == ("@rpath/self",)
    assert result.rpaths == ("@loader_path/lib",)


@pytest.mark.parametrize("wide,endian", [(False, ">"), (True, ">"), (False, "<"), (True, "<")])
def test_universal_probe_uses_maximum_deployment_floor(
    tmp_path: Path, wide: bool, endian: str
) -> None:
    first = macho_image(minimum=0x000D0000)
    second = macho_image(minimum=0x001A0000)
    arch_size = 32 if wide else 20
    start = 8 + 2 * arch_size
    fmt = endian + ("IIQQII" if wide else "IIIII")
    a = (0x0100000C, 0, start, len(first), 0, 0) if wide else (0x0100000C, 0, start, len(first), 0)
    b = (
        (0x0100000C, 0, start + len(first), len(second), 0, 0)
        if wide
        else (0x0100000C, 0, start + len(first), len(second), 0)
    )
    raw = (
        struct.pack(endian + "II", 0xCAFEBABF if wide else 0xCAFEBABE, 2)
        + struct.pack(fmt, *a)
        + struct.pack(fmt, *b)
        + first
        + second
    )
    path = tmp_path / "fat"
    path.write_bytes(raw)
    assert inspect_macho_load_commands(path).minimum_macos == (26, 0, 0)


@pytest.mark.parametrize(
    "payload",
    [
        b"bad",
        b"\xca\xfe\xba\xbe",
        struct.pack(">II", 0xCAFEBABE, 33),
        struct.pack(">II", 0xCAFEBABE, 1) + bytes(20),
        struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 8, 0, 0)
        + struct.pack("<II", 0xC, 7),
        struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 4097, 0, 0, 0),
    ],
)
def test_malformed_macho_fails_closed(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "malformed"
    path.write_bytes(payload)
    with pytest.raises(SecurityError):
        inspect_macho_load_commands(path)


@macos_only.MACHO_IMAGES
def test_real_installed_python_deployment_floor_is_measured() -> None:
    result = inspect_macho_load_commands(Path(sys.executable).resolve())
    assert result.minimum_macos is not None and result.architectures


def test_linked_library_closure_hashes_actual_bytes_and_skips_install_id(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    dependent = library / "fixture.dylib"
    dependent.write_bytes(
        macho_image(
            install_name="@rpath/fixture.dylib", dependencies=("/usr/lib/libSystem.B.dylib",)
        )
    )
    executable = tmp_path / "executable"
    executable.write_bytes(
        macho_image(dependencies=("@rpath/fixture.dylib",), rpaths=("@loader_path/lib",))
    )
    identity = _python_linked_library_identity((executable,))
    assert len(identity["files"]) == 2  # type: ignore[arg-type]
    assert identity["system_library_references"] == ["/usr/lib/libSystem.B.dylib"]
    before = canonical_sha256(identity)
    dependent.write_bytes(
        macho_image(
            minimum=0x000E0000,
            install_name="@rpath/fixture.dylib",
            dependencies=("/usr/lib/libSystem.B.dylib",),
        )
    )
    assert canonical_sha256(_python_linked_library_identity((executable,))) != before


def test_dependency_identity_detects_equal_size_preserved_mtime_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.runtime as runtime

    path = tmp_path / "typing_extensions.py"
    path.write_text("VALUE = 1\n")
    fake = SimpleNamespace(
        files=[Path(path.name)],
        version="unchanged",
        requires=None,
        locate_file=lambda _: str(tmp_path),
    )
    monkeypatch.setattr(runtime.importlib.metadata, "distribution", lambda _: fake)
    monkeypatch.setattr(
        runtime.importlib, "import_module", lambda _: SimpleNamespace(__file__=str(path))
    )
    first, _ = _python_distribution_identity("typing-extensions")
    metadata = path.stat()
    path.write_text("VALUE = 2\n")
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    second, _ = _python_distribution_identity("typing-extensions")
    assert first["version"] == second["version"] and first != second


def test_dependency_import_shadow_and_symlink_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.runtime as runtime

    path = tmp_path / "typing_extensions.py"
    path.write_text("VALUE = 1\n")
    fake = SimpleNamespace(
        files=[Path(path.name)],
        version="unchanged",
        requires=None,
        locate_file=lambda _: str(tmp_path),
    )
    monkeypatch.setattr(runtime.importlib.metadata, "distribution", lambda _: fake)
    shadow = tmp_path / "shadow.py"
    shadow.write_text("pass\n")
    monkeypatch.setattr(
        runtime.importlib, "import_module", lambda _: SimpleNamespace(__file__=str(shadow))
    )
    with pytest.raises(SecurityError, match="import differs"):
        _python_distribution_identity("typing-extensions")
    path.unlink()
    path.symlink_to(shadow)
    with pytest.raises(SecurityError, match="symlink"):
        _python_distribution_identity("typing-extensions")


# -- the service closure: environment markers, launcher rows, missing distributions ------

DARWIN = {
    "extra": "",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "platform_system": "Darwin",
    "python_full_version": "3.12.14",
    "python_version": "3.12",
    "sys_platform": "darwin",
}
LINUX_310 = DARWIN | {
    "platform_system": "Linux",
    "python_full_version": "3.10.4",
    "python_version": "3.10",
    "sys_platform": "linux",
}
PYPY = DARWIN | {"implementation_name": "pypy", "platform_python_implementation": "PyPy"}


def test_the_service_closure_is_the_pydantic_family_and_the_sealing_closure() -> None:
    assert RECORDING_PYTHON_DISTRIBUTIONS == (
        "annotated-types",
        "cffi",
        "cryptography",
        "pycparser",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
        "typing-inspection",
    )
    assert VAULT_REQUIREMENTS_LOCK == "requirements/vault-macos.txt"


def test_the_marker_environment_is_this_interpreters_with_no_extra() -> None:
    assert recording_marker_environment() == {
        "extra": "",
        "implementation_name": sys.implementation.name,
        "platform_python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "python_full_version": platform.python_version(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "sys_platform": sys.platform,
    }


def test_the_installed_closure_markers_select_only_what_this_interpreter_needs() -> None:
    environment = recording_marker_environment()
    selected: dict[str, bool] = {}
    for name in RECORDING_PYTHON_DISTRIBUTIONS:
        for requirement in importlib.metadata.distribution(name).requires or ():
            _, separator, marker = requirement.partition(";")
            if separator:
                selected[requirement] = requirement_marker_applies(marker, environment)
    assert selected, "the closure has no marker rows to check"
    for requirement, applies in selected.items():
        name = requirement.split(";")[0].strip()
        if "extra ==" in requirement:
            assert not applies, requirement
        elif name.startswith(("cffi", "pycparser")):
            assert applies, requirement
        elif name.startswith("typing-extensions"):
            assert not applies, requirement  # needed only before Python 3.11
    assert any(item.startswith("cffi") for item in selected)
    assert any(item.startswith("pycparser") for item in selected)


@pytest.mark.parametrize(
    ("marker", "environment", "expected"),
    [
        ("extra == 'ssh'", DARWIN, False),
        ("extra != 'ssh'", DARWIN, True),
        ("extra == 'ssh'", DARWIN | {"extra": "ssh"}, True),
        ('extra == "Tz.Data"', DARWIN | {"extra": "tz_data"}, True),
        ("platform_python_implementation != 'PyPy'", DARWIN, True),
        ("platform_python_implementation != 'PyPy'", PYPY, False),
        ('implementation_name != "PyPy"', DARWIN, True),
        ('implementation_name == "pypy"', PYPY, True),
        ("python_full_version < '3.11'", DARWIN, False),
        ("python_full_version < '3.11'", LINUX_310, True),
        ("python_full_version > '3.12.13'", DARWIN, True),
        ("python_full_version >= '3.12.15'", DARWIN, False),
        ("python_version == '3.12.0'", DARWIN, True),
        ("python_version != '3.12'", DARWIN, False),
        ("python_version <= '3.12'", DARWIN, True),
        ("python_version > '3.12'", DARWIN, False),
        ("python_version >= '3.9' and platform_system == 'Windows'", LINUX_310, False),
        (
            "(python_version >= '3.9' and platform_system == 'Windows') and extra == 'timezone'",
            DARWIN | {"platform_system": "Windows", "extra": "timezone"},
            True,
        ),
        (
            "(python_version >= '3.9' and platform_system == 'Windows') and extra == 'timezone'",
            DARWIN | {"platform_system": "Windows"},
            False,
        ),
        ("sys_platform == 'darwin' or sys_platform == 'linux'", LINUX_310, True),
        ("sys_platform == 'win32' or sys_platform == 'cygwin'", DARWIN, False),
        (
            "sys_platform == 'win32' or (platform_system == 'Linux' and python_version < '3.11')",
            LINUX_310,
            True,
        ),
        ("extra == 'a' or extra == 'b' and extra == 'c'", DARWIN | {"extra": "a"}, True),
        ("  sys_platform=='darwin'  ", DARWIN, True),
    ],
)
def test_a_supported_marker_is_evaluated_as_pep_508_says(
    marker: str, environment: dict[str, str], expected: bool
) -> None:
    assert requirement_marker_applies(marker, environment) is expected


@pytest.mark.parametrize(
    ("marker", "problem"),
    [
        ("os_name == 'nt'", "unsupported variable"),
        ("platform_release == '1'", "unsupported variable"),
        ("python_version in '3.11 3.12'", "unsupported operator"),
        ("python_version not in '3.11'", "unsupported operator"),
        ("not extra == 'x'", "unsupported operator"),
        ("extra 'x'", "unsupported operator"),
        ("'3.11' < python_version", "does not start with a variable"),
        ("python_version ~= '3.11'", "unsupported character"),
        ("python_version === '3.12'", "unsupported character"),
        ('extra == "a\\\\b"', "unsupported character"),
        ("extra == 'x' ; extra == 'y'", "unsupported character"),
        ("python_version >= '3.12rc1'", "not a plain release"),
        ("python_version >= '3.*'", "not a plain release"),
        ("python_version >= '1111111'", "not a plain release"),
        ("platform_system >= 'Darwin'", "version operator on platform_system"),
        ("extra == x", "non-string value"),
        ("(extra == 'x'", "missing term"),
        ("(extra == 'x' extra", "unbalanced parenthesis"),
        ("extra == 'x')", "trailing terms"),
        ("extra == 'x' extra == 'y'", "trailing terms"),
        ("extra ==", "missing term"),
        ("extra == 'x' or", "missing term"),
        ("", "missing term"),
    ],
)
def test_an_unknown_marker_is_refused_rather_than_guessed(marker: str, problem: str) -> None:
    with pytest.raises(SecurityError, match=re.escape(problem)) as caught:
        requirement_marker_applies(marker, DARWIN)
    assert str(caught.value).startswith("Python service dependency marker has ")
    assert repr(marker) in str(caught.value)


def fake_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    requires: list[str] | None = None,
    rows: tuple[str, ...] = (),
    entry_points: tuple[SimpleNamespace, ...] = (),
) -> None:
    """Install a one-module fake of typing-extensions that ``runtime`` sees."""
    import cole_voice_lab.runtime as runtime

    path = tmp_path / "typing_extensions.py"
    path.write_text("VALUE = 1\n")
    fake = SimpleNamespace(
        files=[Path(path.name), *(Path(row) for row in rows)],
        version="unchanged",
        requires=requires,
        locate_file=lambda _: str(tmp_path),
        entry_points=entry_points,
    )
    monkeypatch.setattr(runtime.importlib.metadata, "distribution", lambda _: fake)
    monkeypatch.setattr(
        runtime.importlib, "import_module", lambda _: SimpleNamespace(__file__=str(path))
    )


def test_a_dependency_that_markers_exclude_here_is_not_part_of_the_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_distribution(
        tmp_path,
        monkeypatch,
        requires=[
            "cffi>=2.0.0 ; platform_python_implementation != 'PyPy'",
            'pycparser; implementation_name != "PyPy"',
            "zlib-ng>=1 ; python_full_version < '3.11'",
            "pywin32 ; sys_platform == 'win32'",
            "bcrypt>=3.1.5 ; extra == 'ssh'",
            "tzdata; (python_version >= '3.9' and platform_system == 'Windows') and "
            "extra == 'timezone'",
            "; extra == 'nameless'",
        ],
    )
    identity, _ = _python_distribution_identity("typing-extensions")
    assert identity["file_count"] == 1


@pytest.mark.parametrize(
    "requirement",
    ["requests>=2", f"requests ; sys_platform == '{sys.platform}'", "@nonsense", " ; extra != 'x'"],
)
def test_a_dependency_selected_here_outside_the_closure_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirement: str
) -> None:
    fake_distribution(tmp_path, monkeypatch, requires=["pydantic-core", requirement])
    with pytest.raises(SecurityError, match="dependency closure changed"):
        _python_distribution_identity("typing-extensions")


def test_a_closure_marker_outside_the_supported_subset_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_distribution(tmp_path, monkeypatch, requires=["cffi ; os_name == 'nt'"])
    with pytest.raises(SecurityError, match="unsupported variable"):
        _python_distribution_identity("typing-extensions")


def test_only_declared_launcher_rows_are_left_out_of_a_dependency_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launchers = (
        SimpleNamespace(name="fake-tool", group="console_scripts"),
        SimpleNamespace(name="fake-window", group="gui_scripts"),
        SimpleNamespace(name="elsewhere", group="distutils.setup_keywords"),
    )
    rows = ("../../../bin/fake-tool", "../../../bin/fake-window")
    fake_distribution(tmp_path, monkeypatch, rows=rows, entry_points=launchers)
    identity, _ = _python_distribution_identity("typing-extensions")
    assert identity["file_count"] == 1
    fake_distribution(
        tmp_path, monkeypatch, rows=("../../../bin/elsewhere",), entry_points=launchers
    )
    with pytest.raises(ValueError, match="parent path components"):
        _python_distribution_identity("typing-extensions")
    fake_distribution(tmp_path, monkeypatch, rows=("../outside.py",))
    with pytest.raises(ValueError, match="parent path components"):
        _python_distribution_identity("typing-extensions")


def test_launcher_rows_are_dropped_by_name_and_unsafe_names_are_refused() -> None:
    rows = ["pkg/__init__.py", "../../../bin/tool", "../../../bin/other", "../../bin/tool"]
    assert recording_distribution_files("pkg", rows, ["tool"]) == (
        "pkg/__init__.py",
        "../../../bin/other",
        "../../bin/tool",
    )
    assert recording_distribution_files("pkg", rows, []) == tuple(rows)
    for unsafe in ("../evil", "", "two words", "x" * 129, ".hidden"):
        with pytest.raises(SecurityError, match="unsafe launcher"):
            recording_distribution_files("pkg", rows, ["tool", unsafe])


def test_a_dependency_without_bounded_file_evidence_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.runtime as runtime

    fake_distribution(tmp_path, monkeypatch)
    missing = SimpleNamespace(files=None, requires=None, locate_file=lambda _: str(tmp_path))
    monkeypatch.setattr(runtime.importlib.metadata, "distribution", lambda _: missing)
    with pytest.raises(SecurityError, match="no bounded installed file evidence"):
        _python_distribution_identity("typing-extensions")


@macos_only.DARWIN_WHEELS
def test_the_installed_cffi_is_fingerprinted_without_its_launcher() -> None:
    distribution = importlib.metadata.distribution("cffi")
    root = Path(str(distribution.locate_file(""))).resolve()
    rows = [str(item) for item in distribution.files or ()]
    assert "../../../bin/cffi-gen-src" in rows
    counted = [
        row
        for row in rows
        if not row.startswith("../") and not (row.endswith(".pyc") and not (root / row).exists())
    ]
    identity, extensions = _python_distribution_identity("cffi")
    assert identity["file_count"] == len(counted)
    assert [path.name for path in extensions] == ["_cffi_backend.cpython-312-darwin.so"]
    _, extensions = _python_distribution_identity("cryptography")
    assert [path.name for path in extensions] == ["_rust.abi3.so"]


@pytest.mark.parametrize(
    ("name", "route"),
    [
        ("cryptography", "install the reviewed vault lock requirements/vault-macos.txt"),
        ("cffi", "-r requirements/vault-macos.txt"),
        ("pycparser", "--require-hashes --no-deps --only-binary=:all:"),
        ("pydantic", "pip install -e '.[dev]'"),
    ],
)
def test_a_missing_service_distribution_names_its_install_route(
    monkeypatch: pytest.MonkeyPatch, name: str, route: str
) -> None:
    import cole_voice_lab.runtime as runtime

    def missing(requested: str) -> object:
        raise importlib.metadata.PackageNotFoundError(requested)

    monkeypatch.setattr(runtime.importlib.metadata, "distribution", missing)
    message = missing_recording_python_distribution_message(name)
    assert name in message and route in message
    assert ("vault-macos" in message) is (name != "pydantic")
    for action in (
        lambda: recording_python_distribution(name),
        lambda: _python_distribution_identity(name),
    ):
        with pytest.raises(RuntimeUnavailableError, match=f"has no installed {name};") as caught:
            action()
        assert str(caught.value) == message
    with pytest.raises(RuntimeUnavailableError, match="has no installed annotated-types"):
        runtime._recording_runtime_entries({"linked_libraries": {"files": []}})


def freeze_probe_answer(
    monkeypatch: pytest.MonkeyPatch, destination: Path, answer: dict[str, object]
) -> None:
    import cole_voice_lab.native_recording_application as application

    def run(arguments: Any, **_options: object) -> str:
        if "venv" in arguments:
            (destination / "lib/python3.12/site-packages").mkdir(parents=True)
            return ""
        assert "PackageNotFoundError" in arguments[-1] and "console_scripts" in arguments[-1]
        return json.dumps(answer)

    monkeypatch.setattr(application, "_run", run)


def test_freezing_refuses_a_missing_service_distribution_by_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.native_recording_application import _freeze_dependencies

    destination = tmp_path / "runtime"
    answer: dict[str, object] = {
        name: None if name == "cryptography" else [str(tmp_path), ["x.py"], []]
        for name in RECORDING_PYTHON_DISTRIBUTIONS
    }
    freeze_probe_answer(monkeypatch, destination, answer)
    with pytest.raises(RuntimeUnavailableError, match="requirements/vault-macos.txt"):
        _freeze_dependencies(Path(sys.executable), destination)
    assert not list((destination / "lib/python3.12/site-packages").iterdir())


def test_freezing_copies_no_launcher_and_refuses_other_relocated_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.native_recording_application import _freeze_dependencies

    source = tmp_path / "source"
    source.mkdir()
    for name in RECORDING_PYTHON_DISTRIBUTIONS:
        (source / f"{name}.py").write_text("VALUE = 1\n")
    answer: dict[str, object] = {
        name: [str(source), [f"{name}.py", "../../../bin/tool"], ["tool"]]
        for name in RECORDING_PYTHON_DISTRIBUTIONS
    }
    destination = tmp_path / "runtime"
    freeze_probe_answer(monkeypatch, destination, answer)
    _freeze_dependencies(Path(sys.executable), destination)
    site = destination / "lib/python3.12/site-packages"
    assert sorted(path.name for path in site.iterdir()) == sorted(
        f"{name}.py" for name in RECORDING_PYTHON_DISTRIBUTIONS
    )
    assert not (destination / "bin").exists()
    undeclared = {name: [str(source), [f"{name}.py", "../../../bin/tool"], []] for name in answer}
    destination = tmp_path / "second runtime"
    freeze_probe_answer(monkeypatch, destination, undeclared)
    with pytest.raises(ValueError, match="parent path components"):
        _freeze_dependencies(Path(sys.executable), destination)


def test_immutable_tree_explicit_site_exclusion_does_not_follow_global_packages(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    library = base / "lib"
    library.mkdir()
    external = tmp_path / "global"
    external.mkdir()
    (library / "site-packages").symlink_to(external, target_is_directory=True)
    (base / "python").write_bytes(b"base")
    with pytest.raises(SecurityError, match="symlink escapes"):
        _immutable_tree_sha256(base, label="base")
    before = _immutable_tree_sha256(
        base, label="base", excluded_relative_entries=("lib/site-packages",)
    )
    (external / "irrelevant.py").write_bytes(b"unimportable")
    assert (
        _immutable_tree_sha256(base, label="base", excluded_relative_entries=("lib/site-packages",))
        == before
    )
    (base / "python").write_bytes(b"new!")
    assert (
        _immutable_tree_sha256(base, label="base", excluded_relative_entries=("lib/site-packages",))
        != before
    )


def test_private_code_copy_rejects_symlinks_and_keeps_cache_out(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("pass\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/cache.pyc").write_bytes(b"cache")
    target = tmp_path / "frozen"
    _private_tree_copy(source, target)
    assert set(path.name for path in target.iterdir()) == {"module.py"}
    assert target.stat().st_mode & 0o077 == 0 and (target / "module.py").stat().st_mode & 0o077 == 0
    (source / "linked.py").symlink_to(source / "module.py")
    with pytest.raises(SecurityError):
        _private_tree_copy(source, tmp_path / "rejected")


def test_frozen_wheel_rejects_path_escape_and_duplicate_members(tmp_path: Path) -> None:
    import zipfile

    environment = tmp_path / "env"
    (environment / "lib/python3.12/site-packages").mkdir(parents=True)
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("../escape", "bad")
    with pytest.raises(ValueError):
        _install_frozen_wheel(wheel, environment)


@macos_only.APPLICATION_PACKAGING
def test_preview_preflight_preserves_existing_output_and_requires_native_device(
    tmp_path: Path,
) -> None:
    from test_recording_takes import DEVICE

    root = AssetLayout.initialize(tmp_path.resolve() / "assets").root
    settings = NativeRecordingApplicationSettings(
        asset_root=str(root),
        purpose="capture-pilot",
        subject_id="synthetic",
        capture_device=DEVICE,
        capture_host="synthetic-host",
    )
    output = tmp_path.resolve() / "Existing Preview.app"
    output.mkdir()
    sentinel = output / "keep"
    sentinel.write_bytes(b"untouched")
    with pytest.raises(ContractError, match="already exists"):
        create_native_recording_application(
            output, source_root=tmp_path, python=Path(sys.executable), settings=settings
        )
    assert sentinel.read_bytes() == b"untouched"
    with pytest.raises(ValueError, match="Core Audio"):
        NativeRecordingApplicationSettings.model_validate(
            {
                **settings.model_dump(),
                "capture_device": DEVICE.model_copy(update={"backend": "avfoundation"}),
            }
        )


def test_settings_declare_a_purpose_and_embed_no_catalog(tmp_path: Path) -> None:
    from recording_fixtures import synthetic_catalog
    from test_recording_contracts import synthetic_contracts
    from test_recording_takes import DEVICE

    from cole_voice_lab.native_recording_application import (
        NativeRecordingApplicationConfiguration,
        NativeRecordingPeerConfiguration,
    )

    base = {
        "asset_root": str(tmp_path.resolve() / "assets"),
        "subject_id": "synthetic",
        "capture_device": DEVICE.model_dump(mode="json"),
        "capture_host": "synthetic-host",
    }
    settings = NativeRecordingApplicationSettings.model_validate(
        base | {"purpose": "capture-pilot"}
    )
    assert settings.schema_version == "cole-voice-lab.native-application-settings/v2"
    # No default purpose: a physical-test build must say so.
    with pytest.raises(ValueError, match="purpose"):
        NativeRecordingApplicationSettings.model_validate(base)
    catalog = synthetic_catalog(3)
    # A v1 settings file that still embeds its catalog is refused, not silently narrowed.
    for legacy in (
        base
        | {
            "schema_version": "cole-voice-lab.native-application-settings/v1",
            "catalog": catalog.model_dump(mode="json"),
        },
        base | {"purpose": "capture-pilot", "catalog": catalog.model_dump(mode="json")},
        base | {"purpose": "capture-pilot", "catalog_sha256": canonical_sha256(catalog)},
    ):
        with pytest.raises(ValueError):
            NativeRecordingApplicationSettings.model_validate(legacy)
    # The open-context rules still bind the settings through the declared purpose.
    approval = {
        "pilot_approval_path": "provenance/pilot-gates/g.json",
        "pilot_approval_sha256": "1" * 64,
    }
    with pytest.raises(ValueError, match="corpus catalog"):
        NativeRecordingApplicationSettings.model_validate(
            base | {"purpose": "capture-pilot"} | approval
        )
    corpus = NativeRecordingApplicationSettings.model_validate(
        base | {"purpose": "capture-corpus"} | approval
    )
    assert corpus.pilot_approval_sha256 == "1" * 64
    with pytest.raises(ValueError, match="paired"):
        NativeRecordingApplicationSettings.model_validate(
            base | {"purpose": "capture-corpus", "pilot_approval_sha256": "1" * 64}
        )
    with pytest.raises(ValueError):
        NativeRecordingApplicationSettings.model_validate(
            base
            | {"purpose": "capture-corpus", "pilot_approval_path": "../outside.json"}
            | {"pilot_approval_sha256": "1" * 64}
        )

    build = synthetic_contracts()["capture-seal"].recorder_build
    runtime = tmp_path.resolve() / "runtime"

    def peer(*arguments: str) -> dict[str, Any]:
        return {
            "executable": str(runtime / "python/bin/python"),
            "arguments": arguments,
            "environment": None,
            "build": build.model_dump(mode="json"),
            "build_identity_path": str(runtime / "service-build.json"),
        }

    configuration = {
        "asset_root": base["asset_root"],
        "schema_directory": str(runtime / "schemas"),
        "client_build": build.model_dump(mode="json"),
        "client_build_path": str(runtime / "client-build.json"),
        "purpose": "capture-pilot",
        "subject_id": "synthetic",
        "capture_device": base["capture_device"],
        "capture_host": "synthetic-host",
        "pilot_approval_path": None,
        "pilot_approval_sha256": None,
        "recorder": peer(),
        "service": peer("-m", "cole_voice_lab.recording_service"),
    }
    accepted = NativeRecordingApplicationConfiguration.model_validate(configuration)
    assert accepted.schema_version == "cole-voice-lab.recording-application/v2"
    assert set(accepted.model_dump()) == {"schema_version", *configuration}
    for refused in (
        configuration | {"schema_version": "cole-voice-lab.recording-application/v1"},
        configuration
        | {"catalog": catalog.model_dump(mode="json"), "catalog_sha256": canonical_sha256(catalog)},
        {key: value for key, value in configuration.items() if key != "purpose"},
        configuration | {"purpose": "capture-pilot", "pilot_approval_sha256": "1" * 64},
    ):
        with pytest.raises(ValueError):
            NativeRecordingApplicationConfiguration.model_validate(refused)
    # A configured peer can never name a catalog digest; only the service's store answers it.
    for flag in ("--catalog-sha256", "--asset-root", "--schema-directory"):
        with pytest.raises(ValueError, match="canonical identity"):
            NativeRecordingPeerConfiguration.model_validate(peer("-m", "x", flag, "a" * 64))


def test_settings_name_an_optional_finetune_runtime_that_the_service_is_launched_with(
    tmp_path: Path,
) -> None:
    from test_recording_contracts import synthetic_contracts
    from test_recording_takes import DEVICE

    from cole_voice_lab.native_recording_application import NativeRecordingPeerConfiguration

    base = {
        "asset_root": str(tmp_path.resolve() / "assets"),
        "purpose": "capture-pilot",
        "subject_id": "synthetic",
        "capture_device": DEVICE.model_dump(mode="json"),
        "capture_host": "synthetic-host",
    }
    unconfigured = NativeRecordingApplicationSettings.model_validate(base)
    assert unconfigured.finetune_runtime_root is None
    service = (
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.recording_service",
        "--native-workspace",
        "--subject-id",
        "synthetic",
    )
    assert native_service_arguments(unconfigured) == service
    runtime_root = str(tmp_path.resolve() / "finetune runtime" / "venv")
    configured = NativeRecordingApplicationSettings.model_validate(
        base | {"finetune_runtime_root": runtime_root}
    )
    assert configured.schema_version == "cole-voice-lab.native-application-settings/v2"
    arguments = native_service_arguments(configured)
    assert arguments == (*service, "--finetune-runtime-root", runtime_root)
    # The flag is an ordinary configured argument, not one the app reserves for itself.
    build = synthetic_contracts()["capture-seal"].recorder_build
    NativeRecordingPeerConfiguration(
        executable=str(tmp_path.resolve() / "runtime/python/bin/python"),
        arguments=arguments,
        environment=None,
        build=build,
        build_identity_path=str(tmp_path.resolve() / "runtime/service-build.json"),
    )
    for refused in (
        "venv",
        "~/.local/share/voice-lab/finetune-f5-01/venv",
        f"{runtime_root}/",
        f"{runtime_root}/../venv",
        f"{tmp_path.resolve()}/./venv",
        f"{tmp_path.resolve()}//venv",
        f"/{runtime_root}",
        f"{runtime_root}\nother",
        "",
        "/" + "a" * 4096,
    ):
        with pytest.raises(ValueError):
            NativeRecordingApplicationSettings.model_validate(
                base | {"finetune_runtime_root": refused}
            )


def test_builder_and_verify_refuse_a_physical_test_purpose_before_release_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_recording_contracts import synthetic_contracts
    from test_recording_takes import DEVICE

    import cole_voice_lab.native_recording_application as application
    from cole_voice_lab.canonical import canonical_json_bytes, sha256_file
    from cole_voice_lab.native_recording_application import (
        NativeRecordingApplicationConfiguration,
        NativeRecordingApplicationRelease,
        NativeRecordingPeerConfiguration,
        verify_native_recording_application,
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("physical-test refusal must precede release commands")

    monkeypatch.setattr(application, "_run", forbidden)
    private = tmp_path.resolve() / "private"
    private.mkdir(mode=0o700)
    root = AssetLayout.initialize(private / "assets").root
    settings = NativeRecordingApplicationSettings(
        asset_root=str(root),
        purpose="physical-test",
        subject_id="cole-physical-diagnostic-synthetic",
        capture_device=DEVICE,
        capture_host="synthetic-host",
    )
    output = private / "Physical Preview.app"
    with pytest.raises(ContractError, match="physical-test catalogs"):
        create_native_recording_application(
            output, source_root=tmp_path, python=Path(sys.executable), settings=settings
        )
    assert not output.exists() and not output.with_suffix(".runtime").exists()

    build = synthetic_contracts()["capture-seal"].recorder_build
    runtime = private / "Physical Preview.runtime"
    runtime.mkdir(mode=0o700)
    peer = NativeRecordingPeerConfiguration(
        executable=str(runtime / "python/bin/python"),
        arguments=(),
        environment=None,
        build=build,
        build_identity_path=str(runtime / "service-build.json"),
    )
    configuration = NativeRecordingApplicationConfiguration(
        asset_root=str(root),
        schema_directory=str(runtime / "schemas"),
        client_build=build,
        client_build_path=str(runtime / "client-build.json"),
        purpose="physical-test",
        subject_id=settings.subject_id,
        capture_device=DEVICE,
        capture_host="synthetic-host",
        pilot_approval_path=None,
        pilot_approval_sha256=None,
        recorder=peer,
        service=peer,
    )
    configuration_path = runtime / "configuration.json"
    configuration_path.write_bytes(canonical_json_bytes(configuration))
    configuration_path.chmod(0o600)
    release = NativeRecordingApplicationRelease(
        application=str(output),
        runtime_directory=str(runtime),
        configuration=str(configuration_path),
        configuration_sha256=sha256_file(configuration_path)[0],
        minimum_macos="13.0",
        service_wheel_sha256="0" * 64,
        client_build=build,
        recorder_build=build,
        service_build=build,
        synthetic_only=True,
    )
    release_path = runtime / "release.json"
    release_path.write_bytes(canonical_json_bytes(release))
    release_path.chmod(0o600)
    with pytest.raises(ContractError, match="physical-test catalogs"):
        verify_native_recording_application(release_path)


def test_frozen_runtime_receipt_reuses_verified_bytes_and_binds_all_stat_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.runtime as runtime

    prefix = tmp_path.resolve() / "python"
    prefix.mkdir()
    source = prefix / "dependency.py"
    source.write_text("VALUE = 1\n")
    calls: list[str] = []
    environment = {"kernel_build": os.uname().version, "verified_source_sha256": "a" * 64}

    def current() -> dict[str, object]:
        calls.append("bytes")
        return environment

    monkeypatch.setattr(runtime.sys, "prefix", str(prefix))
    monkeypatch.setattr(runtime, "_current_recording_python_environment_identity", current)
    monkeypatch.setattr(
        runtime,
        "_recording_runtime_entries",
        lambda _: tuple(runtime._recording_runtime_stat(path) for path in (prefix, source)),
    )
    receipt = runtime.freeze_recording_python_runtime_receipt()
    assert calls == ["bytes", "bytes"] and receipt.stat().st_mode & 0o077 == 0
    first = runtime.recording_python_environment_identity()
    assert calls == ["bytes", "bytes"]
    assert first["verified_runtime_receipt_sha256"]
    before = source.stat()
    source.write_text("VALUE = 2\n")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(SecurityError, match="runtime changed"):
        runtime.recording_python_environment_identity()
    assert calls == ["bytes", "bytes"]


def test_runtime_receipt_directory_identity_detects_added_import_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.runtime as runtime

    prefix = tmp_path.resolve() / "python"
    prefix.mkdir()
    environment = {"kernel_build": os.uname().version}
    monkeypatch.setattr(runtime.sys, "prefix", str(prefix))
    monkeypatch.setattr(
        runtime, "_current_recording_python_environment_identity", lambda: environment
    )
    monkeypatch.setattr(
        runtime, "_recording_runtime_entries", lambda _: (runtime._recording_runtime_stat(prefix),)
    )
    runtime.freeze_recording_python_runtime_receipt()
    (prefix / "new_import.py").write_text("pass\n")
    with pytest.raises(SecurityError, match="runtime changed"):
        runtime.recording_python_environment_identity()


def test_changed_runtime_receipt_changes_expected_environment_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.runtime as runtime
    from cole_voice_lab.canonical import canonical_json_bytes

    prefix = tmp_path.resolve() / "python"
    prefix.mkdir()
    source = prefix / "source.py"
    source.write_bytes(b"pass\n")
    environment = {"kernel_build": os.uname().version}
    monkeypatch.setattr(runtime.sys, "prefix", str(prefix))
    monkeypatch.setattr(
        runtime, "_current_recording_python_environment_identity", lambda: environment
    )
    monkeypatch.setattr(
        runtime,
        "_recording_runtime_entries",
        lambda _: tuple(runtime._recording_runtime_stat(path) for path in (prefix, source)),
    )
    receipt_path = runtime.freeze_recording_python_runtime_receipt()
    original = canonical_sha256(runtime.recording_python_environment_identity())
    receipt = runtime.RecordingPythonRuntimeReceipt.model_validate_json(receipt_path.read_bytes())
    receipt_path.write_bytes(
        canonical_json_bytes(receipt.model_copy(update={"entries": receipt.entries[:1]}))
    )
    assert canonical_sha256(runtime.recording_python_environment_identity()) != original
    receipt_path.chmod(0o644)
    with pytest.raises(SecurityError, match="private"):
        runtime.recording_python_environment_identity()


def test_runtime_receipt_refuses_unstable_bytes_and_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.runtime as runtime

    prefix = tmp_path.resolve() / "python"
    prefix.mkdir()
    calls: list[int] = []

    def changed() -> dict[str, object]:
        calls.append(1)
        return {"version": len(calls)}

    monkeypatch.setattr(runtime.sys, "prefix", str(prefix))
    monkeypatch.setattr(runtime, "_current_recording_python_environment_identity", changed)
    monkeypatch.setattr(
        runtime, "_recording_runtime_entries", lambda _: (runtime._recording_runtime_stat(prefix),)
    )
    with pytest.raises(SecurityError, match="changed while freezing"):
        runtime.freeze_recording_python_runtime_receipt()
    path = prefix.parent / runtime.RECORDING_PYTHON_RUNTIME_RECEIPT
    path.write_bytes(b"untouched")
    with pytest.raises(SecurityError, match="already exists"):
        runtime.freeze_recording_python_runtime_receipt()
    assert path.read_bytes() == b"untouched"


@pytest.mark.skipif(
    not os.environ.get("COLE_VOICE_ROLLBACK_RELEASE"),
    reason="requires an explicitly named frozen rollback artifact",
)
def test_frozen_rollback_after_disposable_primary_loses_qt(tmp_path: Path) -> None:
    from native_recording_rollback_fixture import replay_frozen_rollback

    evidence = replay_frozen_rollback(
        Path(os.environ["COLE_VOICE_ROLLBACK_RELEASE"]), tmp_path / "rollback replay"
    )
    assert (
        evidence["retained_release_unchanged"]
        and not evidence["retained_real_root_launcher_executed"]
    )


@pytest.fixture(scope="module")
def native_release_catalog(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A synthetic asset root whose corpus store serves pilot-v2's prompts as `cole-corpus`.

    The release pins no catalog, so the launched app can only reach a useful state through
    the subject's active store version. Returns the root and that version's reference.
    """
    from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
    from cole_voice_lab.storage_binding import open_catalog_editor

    root = tmp_path_factory.mktemp("native release spaces").resolve()
    assets = AssetLayout.initialize(root / "synthetic assets")
    pilot = load_builtin_prompt_suite("pilot-v2.json")
    with open_catalog_editor(assets, create=True) as corpus:
        corpus.import_prompt_suite(pilot, source="pilot-v2")
        corpus.create_catalog("cole-corpus", purpose=pilot.purpose)
        reference = corpus.publish_version("cole-corpus")
        corpus.activate("synthetic-subject", reference)
        assert corpus.materialize(reference).suite.prompts == pilot.prompts
    return SimpleNamespace(layout=assets, reference=reference)


@pytest.fixture(scope="module")
def signed_native_release(native_release_catalog: Any) -> Any:
    macos_only.skip_off_macos(macos_only.APPLICATION_PACKAGING)
    from cole_voice_lab.models import CaptureDevice

    assets = native_release_catalog.layout
    root = assets.root.parent
    pcm = root / "synthetic.pcm"
    pcm.write_bytes(bytes(1440))
    pcm.chmod(0o600)
    settings = NativeRecordingApplicationSettings(
        asset_root=str(assets.root),
        purpose=native_release_catalog.reference.purpose,
        subject_id="synthetic-subject",
        capture_device=CaptureDevice(
            backend="coreaudio", device_id="synthetic-device", display_name="Synthetic UMIK-1"
        ),
        capture_host="synthetic-host",
        # Never created: the launched service only parses it (fine-tune jobs check it).
        finetune_runtime_root=str(root / "finetune runtime" / "venv"),
    )
    release = create_native_recording_application(
        root / "Cole Voice Recorder Preview.app",
        source_root=Path(__file__).resolve().parents[1],
        python=Path(sys.executable),
        settings=settings,
        synthetic_pcm=pcm,
    )
    return release


def test_actual_signed_preview_has_frozen_service_and_permissions(
    signed_native_release: Any,
) -> None:
    import json
    import plistlib
    import subprocess

    from cole_voice_lab.native_recording_application import verify_native_recording_application

    release = signed_native_release
    app = Path(release.application)
    runtime = Path(release.runtime_directory)
    assert verify_native_recording_application(runtime / "release.json") == release
    metadata = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert metadata["LSMinimumSystemVersion"] == release.minimum_macos
    assert metadata["NSMicrophoneUsageDescription"] and "preview" in metadata["CFBundleIdentifier"]
    assert (
        not list(app.rglob("*.wav")) and not (app / "Contents/Resources/application.json").exists()
    )
    configuration = json.loads(Path(release.configuration).read_text())
    assert configuration["service"]["executable"] == str(runtime / "python/bin/python")
    assert Path(release.configuration).stat().st_mode & 0o077 == 0
    # The release pins no catalog; the service serves the store's and is launched without one.
    assert configuration["schema_version"] == "cole-voice-lab.recording-application/v2"
    assert configuration["purpose"] == "capture-pilot"
    assert not {"catalog", "catalog_sha256"} & set(configuration)
    assert "--catalog-sha256" not in configuration["service"]["arguments"]
    # The settings' fine-tune runtime reaches the service as a launch argument, not a key.
    assert configuration["service"]["arguments"][-2:] == [
        "--finetune-runtime-root",
        str(Path(release.application).parent / "finetune runtime" / "venv"),
    ]
    assert "finetune_runtime_root" not in configuration
    result = subprocess.run(
        (
            str(runtime / "python/bin/python"),
            "-I",
            "-B",
            "-c",
            "import importlib.util,json,cole_voice_lab,cryptography; "
            "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; "
            "from cole_voice_lab.storage_sealing import LibraryKey,open_sealed_blob,seal_blob; "
            "key=LibraryKey(bytes(range(32))); "
            "sealed=seal_blob(key,'frozen-library',b'frozen service seals'); "
            "print(json.dumps({'qt':importlib.util.find_spec('PySide6') is None,"
            "'source':cole_voice_lab.__file__,'cryptography':cryptography.__file__,"
            "'aead':AESGCM.__module__,"
            "'opened':open_sealed_blob(key,'frozen-library',sealed,label='x').decode()}))",
        ),
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    identity = json.loads(result.stdout)
    assert identity["qt"] and Path(identity["source"]).is_relative_to(runtime / "python")
    # The frozen service carries the vault's sealing closure, and seals with it.
    assert Path(identity["cryptography"]).is_relative_to(runtime / "python")
    assert identity["aead"].startswith("cryptography.")
    assert identity["opened"] == "frozen service seals"
    assert not (runtime / "python/bin/cffi-gen-src").exists()
    frozen_site = runtime / "python/lib/python3.12/site-packages"
    assert (frozen_site / "_cffi_backend.cpython-312-darwin.so").is_file()
    assert (frozen_site / "cryptography/hazmat/bindings/_rust.abi3.so").is_file()
    for executable in (app, app / "Contents/Helpers/ColeVoiceRecorderHelper"):
        signature = subprocess.run(
            ("/usr/bin/codesign", "-d", "--entitlements", ":-", str(executable)),
            capture_output=True,
            check=True,
            timeout=15,
        )
        assert plistlib.loads(signature.stdout)["com.apple.security.device.audio-input"] is True


@pytest.mark.parametrize(
    "changed", ["typing_extensions.py", "cryptography/__about__.py"], ids=["pydantic", "vault"]
)
def test_actual_frozen_service_rejects_changed_dependency_with_preserved_mtime(
    signed_native_release: Any, tmp_path: Path, changed: str
) -> None:
    import shutil
    import subprocess

    original = Path(signed_native_release.runtime_directory) / "python"
    parent = tmp_path.resolve() / "cloned runtime"
    parent.mkdir(mode=0o700)
    clone = parent / "python"
    shutil.copytree(original, clone, symlinks=True)
    python = clone / "bin/python"
    subprocess.run(
        (
            str(python),
            "-I",
            "-B",
            "-c",
            "from cole_voice_lab.runtime import freeze_recording_python_runtime_receipt; "
            "freeze_recording_python_runtime_receipt()",
        ),
        check=True,
        capture_output=True,
        timeout=30,
    )
    dependency = clone / "lib/python3.12/site-packages" / changed
    before = dependency.stat()
    raw = dependency.read_bytes()
    offset = raw.index(b"#") + 1
    replacement = b"x" if raw[offset : offset + 1] != b"x" else b"y"
    dependency.write_bytes(raw[:offset] + replacement + raw[offset + 1 :])
    os.utime(dependency, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (
        dependency.stat().st_size == before.st_size
        and dependency.stat().st_mtime_ns == before.st_mtime_ns
    )
    failed = subprocess.run(
        (
            str(python),
            "-I",
            "-B",
            "-c",
            "from cole_voice_lab.recording_service import recording_service_build_identity; "
            "recording_service_build_identity()",
        ),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert failed.returncode != 0 and "runtime changed; rebuild" in failed.stderr


def _launch_verification(app: Path, report: Path, *flags: str) -> Any:
    """Run the signed preview's verification entry. It opens no window, input or output."""

    import subprocess

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE_", "COVERAGE_"))
    }
    return subprocess.run(
        (str(app / "Contents/MacOS/ColeVoiceRecorderPreview"), *flags, str(report)),
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )


def test_actual_signed_preview_reaches_useful_state_without_window_or_audio(
    signed_native_release: Any, native_release_catalog: Any, tmp_path: Path
) -> None:
    """The step's only permitted launch: a synthetic fixture app, headless and self-exiting."""

    import json

    app = Path(signed_native_release.application)
    private = tmp_path / "verification"
    private.mkdir()
    private.chmod(0o700)
    report = private / "configured-app.json"
    completed = _launch_verification(
        app,
        report,
        "--verification-exit",
        "--synthetic-playback",
        "--verification-report",
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(report.read_bytes())
    assert evidence["schema_version"] == "cole-voice-lab.recording-application-verification/v1"
    assert evidence["result"] == "passed"
    # "Useful" is the readiness the design names, asserted directly rather than inferred
    # from the result word: a real context, a selected prompt and an enabled Record.
    assert evidence["record_enabled"] and evidence["context_ready"] and evidence["service_ready"]
    assert evidence["storage_ready"] and evidence["native_device_ready"]
    assert evidence["prompt_id"] and evidence["group_id"]
    assert evidence["channels_closed"]
    # The configured child peers are the release's own frozen identities, not the checkout's.
    assert evidence["service_build"]["build_id"] and evidence["recorder_build"]["build_id"]
    # The catalog the app recorded against is the one the service pinned from the store.
    assert evidence["catalog_sha256"] == native_release_catalog.reference.catalog_sha256
    # Nothing was shown, activated, recorded or played.
    for forbidden in (
        "shown_window",
        "activated_application",
        "microphone_opened",
        "playback_started",
        "recording_started",
        "installed_app_restarted",
    ):
        assert evidence[forbidden] is False, forbidden
    assert report.stat().st_mode & 0o077 == 0
    # The interval is a model measurement, not a launch-performance claim.
    assert "excludes dynamic loader and display" in evidence["timing_scope"]
    assert evidence["entry_to_useful_ms"] > 0


def test_verification_entry_refuses_incomplete_flags_and_unsafe_report_locations(
    signed_native_release: Any, tmp_path: Path
) -> None:
    import subprocess

    app = Path(signed_native_release.application)
    private = tmp_path / "verification"
    private.mkdir()
    private.chmod(0o700)

    # Every flag is mandatory; a partial invocation must never run a verification.
    partial = _launch_verification(
        app, private / "partial.json", "--verification-exit", "--verification-report"
    )
    assert partial.returncode == 64 and not (private / "partial.json").exists()

    # A relative report path is refused before anything starts.
    relative = subprocess.run(
        (
            str(app / "Contents/MacOS/ColeVoiceRecorderPreview"),
            "--verification-exit",
            "--synthetic-playback",
            "--verification-report",
            "relative.json",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert relative.returncode == 64

    # An existing report is never replaced, and its bytes are left exactly as they were.
    existing = private / "existing.json"
    existing.write_bytes(b"{}")
    existing.chmod(0o600)
    replaced = _launch_verification(
        app, existing, "--verification-exit", "--synthetic-playback", "--verification-report"
    )
    assert replaced.returncode != 0 and existing.read_bytes() == b"{}"

    # A world-readable parent directory is not a private owned location.
    public = tmp_path / "public"
    public.mkdir()
    # mkdir's mode is masked by whatever umask an earlier test left set, so make the
    # directory genuinely group/world readable rather than assuming it.
    public.chmod(0o755)
    assert public.stat().st_mode & 0o077
    exposed = _launch_verification(
        app,
        public / "report.json",
        "--verification-exit",
        "--synthetic-playback",
        "--verification-report",
    )
    assert exposed.returncode != 0 and not (public / "report.json").exists()


def _doctored_configuration(release: Any, private: Path, mutate: Any) -> Path:
    """Copy the release configuration into a private fixture location and change one thing."""

    import json

    configuration = json.loads(Path(release.configuration).read_text())
    mutate(configuration)
    target = private / "configuration.json"
    target.write_text(json.dumps(configuration))
    target.chmod(0o600)
    return target


def test_configured_launch_refuses_a_missing_service_and_a_changed_peer_identity(
    signed_native_release: Any, tmp_path: Path
) -> None:
    """A release whose peers do not match its manifests must never report a useful app."""

    import json
    import subprocess

    release = signed_native_release
    app = Path(release.application)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE_", "COVERAGE_"))
    }

    def launch(configuration: Path, report: Path) -> Any:
        return subprocess.run(
            (
                str(app / "Contents/MacOS/ColeVoiceRecorderPreview"),
                "--configuration",
                str(configuration),
                "--verification-exit",
                "--synthetic-playback",
                "--verification-report",
                str(report),
            ),
            capture_output=True,
            text=True,
            timeout=120,
            env=environment,
        )

    missing = tmp_path / "missing service"
    missing.mkdir()
    missing.chmod(0o700)
    configuration = _doctored_configuration(
        release,
        missing,
        lambda value: value["service"].__setitem__(
            "executable", str(missing / "no-such-service-binary")
        ),
    )
    completed = launch(configuration, missing / "report.json")
    assert completed.returncode != 0
    if (missing / "report.json").exists():
        assert json.loads((missing / "report.json").read_bytes())["result"] != "passed"

    # A peer whose recorded build identity no longer matches its bytes is a version
    # mismatch, and the handshake must refuse it rather than proceed.
    mismatch = tmp_path / "changed identity"
    mismatch.mkdir()
    mismatch.chmod(0o700)

    def change_identity(value: dict[str, Any]) -> None:
        value["service"]["build"] = dict(value["service"]["build"]) | {
            "executable_sha256": "0" * 64
        }

    configuration = _doctored_configuration(release, mismatch, change_identity)
    completed = launch(configuration, mismatch / "report.json")
    assert completed.returncode != 0
    if (mismatch / "report.json").exists():
        assert json.loads((mismatch / "report.json").read_bytes())["result"] != "passed"


def test_actual_frozen_service_rejects_an_extension_module_shadowing_its_own_source(
    signed_native_release: Any, tmp_path: Path
) -> None:
    """Python imports a .so before a .py, so the service's own tree must be bound too.

    Only source files and packaged SQL are digested into the build identity, so without
    the receipt binding this tree an added extension module would shadow a service module
    while every recorded identity still matched.
    """

    import shutil
    import subprocess

    original = Path(signed_native_release.runtime_directory) / "python"
    parent = tmp_path.resolve() / "shadowed runtime"
    parent.mkdir(mode=0o700)
    clone = parent / "python"
    shutil.copytree(original, clone, symlinks=True)
    python = clone / "bin/python"
    subprocess.run(
        (
            str(python),
            "-I",
            "-B",
            "-c",
            "from cole_voice_lab.runtime import freeze_recording_python_runtime_receipt; "
            "freeze_recording_python_runtime_receipt()",
        ),
        check=True,
        capture_output=True,
        timeout=30,
    )
    package = clone / "lib/python3.12/site-packages/cole_voice_lab"
    accepted = subprocess.run(
        (
            str(python),
            "-I",
            "-B",
            "-c",
            "from cole_voice_lab.recording_service import recording_service_build_identity; "
            "recording_service_build_identity()",
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert accepted.returncode == 0, accepted.stderr

    # An extension module that would win the import race against audio.py, planted
    # without touching any .py file the build identity digests. Python resolves
    # .cpython-312-darwin.so before .py, which is exactly what makes this a shadow.
    shadow = package / "audio.cpython-312-darwin.so"
    shadow.write_bytes(b"\xcf\xfa\xed\xfe synthetic shadow")
    shadow.chmod(0o600)
    # Check the runtime receipt directly: a genuine malicious extension would import
    # cleanly, so the rejection has to come from the identity closure rather than from
    # this placeholder failing to load.
    failed = subprocess.run(
        (
            str(python),
            "-I",
            "-B",
            "-c",
            "from cole_voice_lab.runtime import recording_python_environment_identity; "
            "recording_python_environment_identity()",
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert failed.returncode != 0, "a shadowing extension module was accepted as verified"
    assert "runtime changed; rebuild" in failed.stderr

"""Suite-wide test configuration.

On a host that is not macOS, the recording service's build identity cannot bind
its real runtime: ``runtime._current_recording_python_environment_identity``
fingerprints the macOS Python closure (Mach-O load commands and
``SystemVersion.plist``) and refuses elsewhere, as production must. Tests whose
subject is recording logic (analysis, packages, fine-tune jobs, replication)
only need *a* stable identity, so off macOS this conftest substitutes a
deterministic stand-in bound to this interpreter's bytes. Production code is
unchanged and still refuses off macOS; tests that exercise the real identity,
or launch the frozen macOS service, carry ``requires_macos`` skips instead.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator, Iterator
from pathlib import Path

import coverage_files
import pytest

from cole_voice_lab import runtime


def _test_stand_in_environment_identity() -> dict[str, object]:
    executable = Path(sys.executable).resolve(strict=True)
    return {
        "schema_version": "test-only.non-macos-recording-python-environment/v1",
        "python_version": sys.version,
        "implementation": sys.implementation.name,
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }


def pytest_configure(config: pytest.Config) -> None:
    if sys.platform != "darwin":
        runtime._current_recording_python_environment_identity = (  # type: ignore[assignment]
            _test_stand_in_environment_identity
        )


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtestloop(session: pytest.Session) -> Generator[None, object, object]:
    """Before pytest-cov combines (its wrapper is outside this one): drop the coverage data
    files a process left without a schema (``coverage_files``). Only the controller does it,
    once every worker and test process has finished, never an xdist worker."""
    result = yield
    data_file = _coverage_data_file(session.config)
    if data_file is not None and not hasattr(session.config, "workerinput"):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        for path in coverage_files.drop_unreadable(data_file):
            if reporter is not None:
                reporter.write_line(f"coverage: dropped {path.name}, a data file with no schema")
    return result


def _coverage_data_file(config: pytest.Config) -> Path | None:
    """pytest-cov's data file, when it measures this run (its controller's ``topdir`` is the
    directory it combines in; under xdist only the workers set ``COV_CORE_DATAFILE``)."""
    controller = getattr(config.pluginmanager.get_plugin("_cov"), "cov_controller", None)
    cov = getattr(controller, "cov", None)
    if cov is None:
        return None
    return Path(getattr(controller, "topdir", os.getcwd())) / cov.config.data_file


def _filesystem(path: Path) -> str:
    if sys.platform != "linux":
        return ""
    found = subprocess.run(
        ["stat", "--file-system", "--format=%T", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return found.stdout.strip()


@pytest.fixture
def btrfs_directory(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Path]:
    """A new private directory on btrfs, or a skip where there is none.

    btrfs lists a directory through a descriptor only as far as the directory was when that
    descriptor was opened or last rewound (#112). The directory is the test's own temporary
    directory when that is on btrfs, else one in the pytest cache under the checkout: on the
    PC, /tmp is tmpfs and /home (and /data, production's) is btrfs.
    """
    for parent in (tmp_path, request.config.cache.mkdir("btrfs")):
        if _filesystem(parent) == "btrfs":
            directory = Path(tempfile.mkdtemp(dir=parent))
            yield directory
            shutil.rmtree(directory)
            return
    pytest.skip("needs a btrfs directory: neither the temporary directory nor the checkout")

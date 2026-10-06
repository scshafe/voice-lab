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
import sys
from pathlib import Path

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

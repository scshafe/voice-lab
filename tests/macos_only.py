"""Skip markers for tests that need macOS itself.

The lab's capture and recording side is a macOS application: the recording
service binds the macOS Python runtime, capture uses Core Audio, and the capture
host is bootstrapped from Homebrew and launchd. On any other host (the Arch PC's
``scripts/verify``) these tests skip with the macOS-only thing they need named in
the reason; ``pytest -ra`` lists them. Nothing here weakens an assertion: on
macOS every marked test runs unchanged.
"""

from __future__ import annotations

import sys

import pytest

OFF_MACOS = sys.platform != "darwin"


def _needs(thing: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(OFF_MACOS, reason=f"needs macOS: {thing}")


REAL_RECORDING_SERVICE = _needs(
    "launches the real recording service, analysis worker or fine-tune worker, whose "
    "build identity binds the macOS Python runtime (Mach-O closure, SystemVersion.plist)"
)
APPLICATION_PACKAGING = _needs("packages or preflights the native macOS recording application")
MACHO_IMAGES = _needs("fingerprints the installed Python's Mach-O images")
DARWIN_WHEELS = _needs("fingerprints the installed macOS (darwin) wheels of the vault lock")
HOMEBREW_PYTHON = _needs("bootstraps the capture host from the Homebrew Python under /opt/homebrew")
LAUNCHD_PLUTIL = _needs("renders a launchd agent and checks it with /usr/bin/plutil")
DESKTOP_MACOS_LOCK = _needs("installs the hash-locked macOS desktop artifact lock")
CASE_INSENSITIVE_VOLUME = _needs(
    "spells a forbidden root in another case, which only a case-insensitive APFS volume "
    "resolves to the same directory"
)
ZOMBIE_GROUP_EPERM = _needs(
    "macOS answers EPERM to killpg(group, 0) for a group of zombies; Linux reports it present"
)


def skip_off_macos(marker: pytest.MarkDecorator) -> None:
    """Skip from inside a fixture (fixtures cannot carry marks) for one of the markers."""
    if OFF_MACOS:
        pytest.skip(str(marker.kwargs["reason"]))

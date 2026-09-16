"""One asset-root capture ownership boundary shared by native, desktop and console.

@canonical recording_ownership -- retain the established lock inode/path while
the installed Qt application is live. Never unlink an advisory ownership lock.
"""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import ContractError, SecurityError
from .paths import AssetLayout, confined_path, ensure_private_directory

CAPTURE_OWNERSHIP_RELATIVE_PATH = "provenance/capture-desktop/.capture.lock"


class RecordingOwnershipConflict(ContractError):
    """Another live capture owns the stable asset-root lock."""


def validate_capture_ownership(layout: AssetLayout, descriptor: int) -> None:
    """Verify a borrowed descriptor still binds the private shared ownership inode."""
    path = confined_path(layout.root, CAPTURE_OWNERSHIP_RELATIVE_PATH, must_exist=True)
    metadata = os.fstat(descriptor)
    current = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise SecurityError("capture ownership must bind one unchanged private regular file")


def acquire_capture_ownership(layout: AssetLayout) -> int:
    """Acquire one nonblocking exclusive descriptor; caller must close after capture stops.

    Returns an uninheritable descriptor. A child that shares ownership must receive
    an explicitly passed duplicate of this same open-file description, not reopen
    the path. Raises ContractError for another owner and SecurityError for unsafe evidence.
    """
    AssetLayout.validate(layout.root)
    directory = ensure_private_directory(confined_path(layout.root, "provenance/capture-desktop"))
    path = confined_path(directory, ".capture.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        validate_capture_ownership(layout, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        validate_capture_ownership(layout, descriptor)
    except BlockingIOError as error:
        os.close(descriptor)
        raise RecordingOwnershipConflict(
            "another desktop recording session is already open"
        ) from error
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def capture_ownership(layout: AssetLayout) -> Iterator[int]:
    """Hold the shared asset lock until the enclosing capture has stopped, including on failure."""
    descriptor = acquire_capture_ownership(layout)
    try:
        yield descriptor
    finally:
        os.close(descriptor)

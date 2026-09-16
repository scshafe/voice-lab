"""Synthetic-only delay adapter around the real durable workspace service.

No successful result is fabricated. Save delegates to the real backend after its
private FIFO releases; analysis uses the real worker's existing inherited barrier.
The explicit crash hook kills this process after a durable Save has committed and
before its reply is written, which is the boundary where a lost reply could otherwise
be mistaken for a lost take.
"""

from __future__ import annotations

import argparse
import os
import select
import stat
import time
from pathlib import Path
from unittest.mock import patch

from cole_voice_lab import recording_service
from cole_voice_lab.recording_contracts import ReliableEvent, SaveTakeCommand, ServiceRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-barriers", type=Path, required=True)
    parser.add_argument("--test-crash-after-save", action="store_true")
    options, remaining = parser.parse_known_args()
    root = options.test_barriers
    if root != root.resolve() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError("Synthetic barriers require one resolved private directory")
    readers: dict[str, int] = {}
    keepers: list[int] = []
    for name in ["save", "analysis"]:
        reader = os.open(root / f"{name}-release.fifo", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(reader)
        if not stat.S_ISFIFO(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("Synthetic barrier must be a private single-link FIFO")
        keepers.append(
            os.open(root / f"{name}-release.fifo", os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        )
        os.set_blocking(reader, True)
        readers[name] = reader
    original_execute = recording_service.RecordingWorkspaceBackend.execute

    def execute(
        self: recording_service.RecordingWorkspaceBackend,
        request: ServiceRequest,
        cancellation: recording_service.RecordingCancellation,
    ) -> ReliableEvent:
        if isinstance(request.data, SaveTakeCommand):
            entered = os.open(
                root / "save-entered", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.write(entered, b"1")
            os.close(entered)
            until = time.monotonic() + 10
            while time.monotonic() < until:
                cancellation.check()
                if select.select([readers["save"]], [], [], 0.02)[0]:
                    if os.read(readers["save"], 1) != b"1":
                        raise ValueError("Synthetic Save barrier closed")
                    break
            else:
                raise ValueError("Synthetic Save barrier exceeded its deadline")
        result = original_execute(self, request, cancellation)
        if options.test_crash_after_save and isinstance(request.data, SaveTakeCommand):
            # The commit is durable; the reply is not. Exit without unwinding anything so
            # the caller sees a dead peer exactly as it would after a real crash.
            marker = os.open(
                root / "save-committed", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.write(marker, request.data.selection.take_id.encode())
            os.close(marker)
            os._exit(137)
        return result

    try:
        with patch.object(recording_service.RecordingWorkspaceBackend, "execute", execute):
            return recording_service.main(
                [*remaining, "--analysis-test-block-fd", str(readers["analysis"])]
            )
    finally:
        for descriptor in [*readers.values(), *keepers]:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())

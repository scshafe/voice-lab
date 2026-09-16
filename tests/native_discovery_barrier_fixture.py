"""Hold one real discovery child's metadata scan after its acknowledged Begin event."""

from __future__ import annotations

import argparse
import os
import select
import sys
from collections.abc import Iterator

from cole_voice_lab import recording_discovery
from cole_voice_lab.recording_contracts import RecordingTake
from cole_voice_lab.recording_takes import RecordingTakeStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-entered-fd", type=int, required=True)
    parser.add_argument("--test-release-fd", type=int, required=True)
    options, remaining = parser.parse_known_args()
    original = recording_discovery._take_pages

    def held(store: RecordingTakeStore, subject_id: str) -> Iterator[RecordingTake]:
        os.write(options.test_entered_fd, b"1")
        if not select.select([options.test_release_fd], [], [], 5)[0]:
            raise RuntimeError("synthetic discovery barrier exceeded its bound")
        if os.read(options.test_release_fd, 1) != b"1":
            raise RuntimeError("synthetic discovery barrier closed")
        yield from original(store, subject_id)

    recording_discovery._take_pages = held
    sys.argv = [sys.argv[0], *remaining]
    try:
        return recording_discovery.main()
    finally:
        os.close(options.test_entered_fd)
        os.close(options.test_release_fd)


if __name__ == "__main__":
    raise SystemExit(main())

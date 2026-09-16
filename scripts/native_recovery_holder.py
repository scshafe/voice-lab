"""Hold capture ownership so another recorder observes a live owner.

A second application really owning capture is the only honest way to exercise the
`busy` recovery result: it is a terminal answer for that page, never a retry loop.
The holder announces itself with a marker file and releases when that file is removed.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_ownership import acquire_capture_ownership


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--held-marker", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    options = parser.parse_args()
    os.umask(0o077)
    layout = AssetLayout.validate(options.asset_root)
    descriptor = acquire_capture_ownership(layout)
    try:
        options.held_marker.write_bytes(b"\x01")
        deadline = time.monotonic() + options.timeout_seconds
        while options.held_marker.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("capture ownership holder was never released")
            time.sleep(0.05)
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

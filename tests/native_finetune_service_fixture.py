"""Test-only wrapper: the real recording service, checking a stub runtime's pretrained pin.

The shipped service never overrides the F5-TTS pretrained-weights pin, so a stub runtime's
few-byte weights would be refused FINETUNE.PRETRAINED_MISMATCH. The native ``finetune``
suite copies this module into its isolated fixture runtime (never into a build) and starts
the unchanged service through it; the only difference is the pin its fine-tune supervisor
hands to each worker. No flag or environment variable of the service itself changes the pin.
"""

from __future__ import annotations

import argparse
import re
from typing import Any
from unittest.mock import patch

from cole_voice_lab import recording_finetune_jobs, recording_service

_PIN = re.compile(r"^([0-9a-f]{64}):([1-9][0-9]{0,15})$")


def main() -> int:
    """Run ``recording_service.main`` with the remaining arguments and the pinned supervisor.

    Raises: ``ValueError`` when ``--test-pretrained-pin`` is not ``SHA256:SIZE``, or when the
    service would set a pin of its own.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-pretrained-pin",
        required=True,
        help="SHA256:SIZE of the stub runtime's pretrained weights",
    )
    options, remaining = parser.parse_known_args()
    match = _PIN.fullmatch(options.test_pretrained_pin)
    if match is None:
        raise ValueError("--test-pretrained-pin must be SHA256:SIZE")
    pin = (match.group(1), int(match.group(2)))
    shipped = recording_finetune_jobs.FineTuneSupervisor

    class PinnedFineTuneSupervisor(shipped):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("pretrained_pin") is not None:
                raise ValueError("the recording service must never choose a pretrained pin")
            super().__init__(*args, **(kwargs | {"pretrained_pin": pin}))

    # The service imports the supervisor when it starts, so the substitute is what it builds.
    with patch.object(recording_finetune_jobs, "FineTuneSupervisor", PinnedFineTuneSupervisor):
        return recording_service.main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())

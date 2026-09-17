"""Test-only wrapper: the real recording service, checking a stub runtime's pretrained pins.

The shipped service never overrides the pretrained-file pins (the F5-TTS v1 Base weights and
the Vocos vocoder), so a stub runtime's few-byte files would be refused
FINETUNE.PRETRAINED_MISMATCH. The native ``finetune`` and ``speak`` suites copy this module
into their isolated fixture runtime (never into a build) and start the unchanged service
through it; the only differences are the pins its fine-tune supervisor hands to each worker
and, when ``--test-synthesis-deadline`` is given, a shorter Speak deadline (so the native
suite can show a deadline failure in seconds). No flag or environment variable of the
service itself changes either.
"""

from __future__ import annotations

import argparse
import math
import re
from typing import Any
from unittest.mock import patch

from cole_voice_lab import recording_finetune_jobs, recording_service
from cole_voice_lab.finetune import FineTunePinnedFile, FineTunePretrainedPins

_PIN = r"([0-9a-f]{64}):([1-9][0-9]{0,15})"
_PINS = re.compile(rf"^{_PIN},{_PIN},{_PIN}$")


def format_pins(pins: FineTunePretrainedPins) -> str:
    """``--test-pretrained-pins``' value: ``SHA:SIZE`` for the weights, vocoder config and
    vocoder weights, comma-separated."""

    return ",".join(
        f"{pin.sha256}:{pin.size_bytes}"
        for pin in (pins.weights, pins.vocoder_config, pins.vocoder_model)
    )


def main() -> int:
    """Run ``recording_service.main`` with the remaining arguments and the pinned supervisor.

    Raises: ``ValueError`` when ``--test-pretrained-pins`` is not three ``SHA256:SIZE``
    values (``format_pins``), when ``--test-synthesis-deadline`` is not a positive number of
    seconds, or when the service would set pins or a Speak deadline of its own.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-pretrained-pins",
        required=True,
        help="SHA256:SIZE of the stub runtime's weights, vocoder config and vocoder weights",
    )
    parser.add_argument(
        "--test-synthesis-deadline",
        type=float,
        help="seconds a Speak worker may run (default: the service's own deadline)",
    )
    options, remaining = parser.parse_known_args()
    match = _PINS.fullmatch(options.test_pretrained_pins)
    if match is None:
        raise ValueError("--test-pretrained-pins must be three comma-separated SHA256:SIZE")
    deadline = options.test_synthesis_deadline
    if deadline is not None and not (math.isfinite(deadline) and deadline > 0):
        raise ValueError("--test-synthesis-deadline must be a positive number of seconds")
    values = match.groups()
    weights, config, model = (
        FineTunePinnedFile(sha256=values[index], size_bytes=int(values[index + 1]))
        for index in (0, 2, 4)
    )
    pin = FineTunePretrainedPins(weights=weights, vocoder_config=config, vocoder_model=model)
    shipped = recording_finetune_jobs.FineTuneSupervisor

    overrides: dict[str, Any] = {"pretrained_pin": pin}
    if deadline is not None:
        overrides["synthesis_deadline"] = deadline

    class PinnedFineTuneSupervisor(shipped):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("pretrained_pin") is not None:
                raise ValueError("the recording service must never choose pretrained pins")
            if "synthesis_deadline" in kwargs:
                raise ValueError("the recording service must never choose a Speak deadline")
            super().__init__(*args, **(kwargs | overrides))

    # The service imports the supervisor when it starts, so the substitute is what it builds.
    with patch.object(recording_finetune_jobs, "FineTuneSupervisor", PinnedFineTuneSupervisor):
        return recording_service.main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())

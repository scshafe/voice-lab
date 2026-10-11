"""The continuity rule of the PortAudio capture worker (D-039; spike S-1).

D-015's Audio Queue worker checks that every buffer's device sample time is exactly the
previous one plus its frames. PortAudio gives no device sample counter: its callback gets
``inputBufferAdcTime``, the stream-clock time (seconds) at which the block's first frame
was captured, derived by the host API (Core Audio: the buffer's host time; ALSA: the
status timestamp and the capture delay). The worker therefore checks, for every block
after the first, that its ADC time is the previous block's ADC time plus that block's
frames at 48 kHz, within a declared tolerance in frames; that every block has an ADC time
at all (finite and positive: PortAudio reports 0 when the host API has none); and that
PortAudio never flagged an input overflow or underflow. The first failure stops the take:
samples are never inserted or dropped to cover a gap.

The tolerance absorbs timestamp jitter, not lost audio: a lost host buffer moves the ADC
time by a whole buffer (hundreds of frames at least), far beyond the default 64 frames
(1.3 ms). Jitter does not accumulate, because each block is compared with the block
before it. The largest deviation seen is kept in the evidence, so the tolerance can be
judged afterwards from data (that is what spike S-1 is for).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

DEFAULT_TOLERANCE_FRAMES: Final = 64.0


@dataclass(frozen=True)
class BlockTiming:
    """What PortAudio says about one input block (callback ``time`` and ``status``)."""

    frames: int
    #: ``time.inputBufferAdcTime``: seconds on the stream clock; 0 when unavailable.
    adc_time: float
    #: ``time.currentTime``: the stream clock when the callback ran.
    current_time: float
    input_overflow: bool = False
    input_underflow: bool = False


class ContinuityError(Exception):
    """A block broke the continuity rule; the message names the block and the numbers."""


class ContinuityTracker:
    """Applies the rule block by block and keeps what the evidence reports."""

    def __init__(
        self, sample_rate: int = 48_000, tolerance_frames: float = DEFAULT_TOLERANCE_FRAMES
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("the sample rate must be positive")
        if not math.isfinite(tolerance_frames) or tolerance_frames <= 0:
            raise ValueError("the tolerance must be a positive number of frames")
        self.sample_rate = sample_rate
        self.tolerance_frames = float(tolerance_frames)
        #: Blocks accepted (a refused block is not counted: it is never written).
        self.callback_count = 0
        self.received_frames = 0
        self.first_adc_time: float | None = None
        #: The ADC time the next block must start at (the last block's end).
        self.last_adc_end: float | None = None
        self.max_abs_deviation_frames: float | None = None
        self.adc_time_valid = True
        self.input_overflows = 0
        self.input_underflows = 0

    def receive(self, timing: BlockTiming) -> None:
        """Account one block. Raises: ``ContinuityError`` when it breaks the rule."""
        index = self.callback_count
        if timing.input_overflow:
            self.input_overflows += 1
        if timing.input_underflow:
            self.input_underflows += 1
        if timing.input_overflow or timing.input_underflow:
            flag = "input overflow" if timing.input_overflow else "input underflow"
            raise ContinuityError(
                f"block {index}: PortAudio reported an {flag}; samples were lost before they "
                "reached the worker"
            )
        if timing.frames <= 0:
            raise ContinuityError(f"block {index}: PortAudio delivered no frames")
        adc = float(timing.adc_time)
        if not math.isfinite(adc) or adc <= 0:
            self.adc_time_valid = False
            raise ContinuityError(
                f"block {index}: PortAudio gave no ADC time ({adc!r}); continuity cannot be checked"
            )
        expected = self.last_adc_end
        if expected is None:
            self.first_adc_time = adc
        else:
            deviation = (adc - expected) * self.sample_rate
            magnitude = abs(deviation)
            if self.max_abs_deviation_frames is None or magnitude > self.max_abs_deviation_frames:
                self.max_abs_deviation_frames = magnitude
            if magnitude > self.tolerance_frames:
                raise ContinuityError(
                    f"block {index}: discontinuity: expected ADC time {expected:.6f} s, got "
                    f"{adc:.6f} s ({deviation:+.1f} frames; tolerance "
                    f"±{self.tolerance_frames:g} frames)"
                )
        self.last_adc_end = adc + timing.frames / self.sample_rate
        self.callback_count += 1
        self.received_frames += timing.frames

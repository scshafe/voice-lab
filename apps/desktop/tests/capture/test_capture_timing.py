"""The PortAudio worker's continuity rule (D-039): ADC times, overflows, tolerance."""

from __future__ import annotations

import math

import pytest
from voice_lab_desktop.capture.timing import BlockTiming, ContinuityError, ContinuityTracker

RATE = 48_000


def block(adc: float, frames: int = 1024, **flags: bool) -> BlockTiming:
    return BlockTiming(frames=frames, adc_time=adc, current_time=adc + 0.01, **flags)


def blocks(count: int, *, start: float = 10.0, frames: int = 1024) -> list[BlockTiming]:
    return [block(start + index * frames / RATE, frames) for index in range(count)]


def test_contiguous_blocks_pass_and_are_accounted():
    tracker = ContinuityTracker(RATE, 64)
    for timing in blocks(5):
        tracker.receive(timing)
    assert tracker.callback_count == 5
    assert tracker.received_frames == 5 * 1024
    assert tracker.first_adc_time == 10.0
    assert tracker.last_adc_end == pytest.approx(10.0 + 5 * 1024 / RATE)
    assert tracker.max_abs_deviation_frames == pytest.approx(0.0, abs=1e-6)
    assert tracker.adc_time_valid and tracker.input_overflows == tracker.input_underflows == 0


def test_one_block_has_no_deviation_yet():
    tracker = ContinuityTracker()
    tracker.receive(block(3.0))
    assert tracker.max_abs_deviation_frames is None and tracker.callback_count == 1


def test_jitter_within_the_tolerance_passes_and_is_reported():
    tracker = ContinuityTracker(RATE, 64)
    for index, timing in enumerate(blocks(6)):
        wobble = (20 if index % 2 else -20) / RATE
        tracker.receive(block(timing.adc_time + wobble))
    assert tracker.callback_count == 6
    # Each block is compared with the one before it: +-20 frames is a 40-frame step.
    assert tracker.max_abs_deviation_frames == pytest.approx(40.0, abs=1e-6)


def test_a_gap_beyond_the_tolerance_fails_naming_the_numbers():
    tracker = ContinuityTracker(RATE, 64)
    first, second = blocks(2)
    tracker.receive(first)
    late = block(second.adc_time + 512 / RATE)
    with pytest.raises(ContinuityError) as caught:
        tracker.receive(late)
    message = str(caught.value)
    assert message.startswith("block 1: discontinuity: expected ADC time 10.021333 s")
    assert "+512.0 frames; tolerance ±64 frames" in message
    # A refused block is not counted: it is never written.
    assert tracker.callback_count == 1 and tracker.received_frames == 1024
    assert tracker.max_abs_deviation_frames == pytest.approx(512.0)


def test_blocks_that_arrive_early_fail_too():
    tracker = ContinuityTracker(RATE, 8)
    first, second = blocks(2)
    tracker.receive(first)
    with pytest.raises(ContinuityError, match=r"-100\.0 frames"):
        tracker.receive(block(second.adc_time - 100 / RATE))


@pytest.mark.parametrize("adc", [0.0, -1.0, math.nan, math.inf])
def test_a_block_without_an_adc_time_fails(adc):
    tracker = ContinuityTracker()
    with pytest.raises(ContinuityError, match="block 0: PortAudio gave no ADC time"):
        tracker.receive(block(adc))
    assert not tracker.adc_time_valid


def test_overflow_and_underflow_flags_fail_and_are_counted():
    tracker = ContinuityTracker()
    with pytest.raises(ContinuityError, match="block 0: PortAudio reported an input overflow"):
        tracker.receive(block(1.0, input_overflow=True))
    with pytest.raises(ContinuityError, match="input underflow"):
        tracker.receive(block(1.0, input_underflow=True))
    assert tracker.input_overflows == 1 and tracker.input_underflows == 1


def test_an_empty_block_fails():
    with pytest.raises(ContinuityError, match="delivered no frames"):
        ContinuityTracker().receive(block(1.0, frames=0))


@pytest.mark.parametrize(("rate", "tolerance"), [(0, 64.0), (RATE, 0.0), (RATE, math.nan)])
def test_settings_are_checked(rate, tolerance):
    with pytest.raises(ValueError):
        ContinuityTracker(rate, tolerance)

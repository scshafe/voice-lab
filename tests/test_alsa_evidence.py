"""``cole-voice-lab.alsa-continuity/v1``: the panel's evidence, and the rule the PC recomputes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from panel_take_fixtures import (
    FIRST_TIMESTAMP_NS,
    PERIOD_FRAMES,
    PERIOD_NS,
    RATE,
    evidence,
    periods,
    validated,
)
from pydantic import ValidationError

from cole_voice_lab.alsa_evidence import (
    MAX_EVIDENCE_BYTES,
    MAX_PERIODS,
    MAX_TOLERANCE_NS,
    MIN_PERIOD_FRAMES,
    AlsaContinuityEvidence,
    period_deviations_ns,
    wav_size_bytes,
)
from cole_voice_lab.canonical import canonical_json_bytes


def _floats(value: object) -> list[float]:
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [found for item in value.values() for found in _floats(item)]
    if isinstance(value, list):
        return [found for item in value for found in _floats(item)]
    return []


def test_a_successful_take_validates_and_is_canonical() -> None:
    record = validated(evidence())
    assert record.success and record.xruns == 0
    assert record.frames == 8 * PERIOD_FRAMES
    # 0, +120,000, -80,000, +40,000 ns of jitter: the steps deviate by at most 200,000 ns.
    assert record.max_abs_deviation_ns == 200_000
    assert record.wav is not None and record.wav.size_bytes == 44 + 8 * PERIOD_FRAMES * 6 * 4
    payload = canonical_json_bytes(record)
    assert AlsaContinuityEvidence.model_validate_json(payload) == record
    # Integers only: the client formats no float in it.
    assert not _floats(json.loads(payload))


def test_the_rule_is_each_period_against_the_one_before() -> None:
    # 16 kHz is 62,500 ns a frame, so every figure is an exact integer.
    pairs = [(1_024, 1_000), (2_048, 1_000 + PERIOD_NS + 7), (3_072, 1_000 + 2 * PERIOD_NS)]
    assert period_deviations_ns(pairs, RATE) == (7, -7)
    assert period_deviations_ns(pairs[:1], RATE) == ()
    # Drift does not accumulate: a slow device clock deviates by the same small step each time.
    drifting = [(n * 1_024, n * (PERIOD_NS + 3)) for n in range(1, 100)]
    assert set(period_deviations_ns(drifting, RATE)) == {3}
    with pytest.raises(ValueError, match="whole nanoseconds"):
        period_deviations_ns(pairs, 48_000)


def test_a_lost_period_is_beyond_every_tolerance_the_pc_takes() -> None:
    # The device's timestamps go on while the stream delivers nothing for one period.
    pairs = periods(8, jitter_ns=(0,))
    lost = pairs[:4] + [(position, timestamp + PERIOD_NS) for position, timestamp in pairs[4:]]
    assert max(period_deviations_ns(lost, RATE)) == PERIOD_NS
    assert MIN_PERIOD_FRAMES * 62_500 > MAX_TOLERANCE_NS
    with pytest.raises(ValidationError, match="beyond the tolerance"):
        validated(evidence(pairs=lost, tolerance_ns=MAX_TOLERANCE_NS))


def test_the_largest_file_fits_its_bound() -> None:
    # MAX_PERIODS pairs of a 10-digit position and a 19-digit timestamp.
    frames = MAX_PERIODS * MIN_PERIOD_FRAMES
    pairs = [
        (9_000_000_000 + n * MIN_PERIOD_FRAMES, 9_000_000_000_000_000_000 + n * 16_000_000)
        for n in range(1, MAX_PERIODS + 1)
    ]
    document = evidence(
        pairs=pairs,
        period_count=MAX_PERIODS,
        period_frames=MIN_PERIOD_FRAMES,
        buffer_frames=1 << 20,
        frames=frames,
        max_abs_deviation_ns=0,
        tolerance_ns=MAX_TOLERANCE_NS,
        wav={"name": "original.wav", "sha256": "a" * 64, "size_bytes": 44 + frames * 24},
    )
    # The last position is far beyond the frames read: only the size is measured here.
    payload = canonical_json_bytes(document)
    assert len(payload) + 1 <= MAX_EVIDENCE_BYTES
    assert MAX_PERIODS == 660 * RATE // MIN_PERIOD_FRAMES


def test_an_unsuccessful_take_names_its_error() -> None:
    failed = validated(
        evidence(success=False, error="snd_pcm_readi: -32 (Broken pipe)", xruns=1, wav=None)
    )
    assert not failed.success and failed.xruns == 1
    with pytest.raises(ValidationError, match="names its error"):
        validated(evidence(success=False, error=None))


def _shifted_last(document: dict[str, Any], frames: int) -> list[list[int]]:
    pairs = [list(pair) for pair in document["periods"]]
    pairs[-1][0] = frames
    return pairs


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"error": "it failed"}, "has no error"),
        ({"xruns": 1}, "no xrun"),
        ({"frames": 8 * PERIOD_FRAMES - 1}, "its periods' frames"),
        ({"periods": []}, "its periods' frames"),
        ({"wav": None}, "describes its WAV"),
        (
            {"wav": {"name": "original.wav", "sha256": "a" * 64, "size_bytes": 1_000}},
            "describes its WAV",
        ),
        ({"started_at": None}, "its start and its stop reason"),
        ({"stop_reason": None}, "its start and its stop reason"),
        ({"max_abs_deviation_ns": 199_999}, "not the 199999 the client measured"),
        ({"max_abs_deviation_ns": None}, "not the None the client measured"),
        ({"tolerance_ns": 199_999}, "beyond the tolerance"),
        ({"tolerance_ns": MAX_TOLERANCE_NS + 1}, "less than or equal to 4000000"),
        ({"tolerance_ns": 0}, "greater than or equal to 1"),
        ({"period_frames": MIN_PERIOD_FRAMES - 1}, "greater than or equal to 256"),
        ({"buffer_frames": PERIOD_FRAMES}, "at least two periods"),
        ({"rate": 48_000}, "whole nanoseconds"),
        ({"format": "S24_LE"}, "S16_LE"),
        ({"tstamp_type": "gettimeofday"}, "monotonic"),
        ({"started_at": "2026-10-19T12:00:00"}, "explicit timezone"),
        ({"schema_version": "voice-lab-desktop.portaudio-continuity/v1"}, "alsa-continuity"),
        ({"unexpected": 1}, "Extra inputs"),
    ],
)
def test_each_rule_is_checked(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        validated(evidence(**changes))


@pytest.mark.parametrize(
    "pairs",
    [
        # A position that does not advance.
        [(2_048, FIRST_TIMESTAMP_NS), (2_048, FIRST_TIMESTAMP_NS + PERIOD_NS)],
        # A timestamp that goes back.
        [(2_048, FIRST_TIMESTAMP_NS), (3_072, FIRST_TIMESTAMP_NS - 1)],
    ],
)
def test_positions_and_timestamps_strictly_increase(pairs: list[tuple[int, int]]) -> None:
    with pytest.raises(ValidationError, match="strictly increase"):
        validated(evidence(pairs=pairs, period_count=2))


def test_the_last_position_is_the_frames_read_plus_at_most_a_buffer() -> None:
    document = evidence()
    frames = document["frames"]
    behind = _shifted_last(document, frames - 1)
    ahead = _shifted_last(document, frames + document["buffer_frames"] + 1)
    for pairs in (behind, ahead):
        with pytest.raises(ValidationError):
            validated(evidence(pairs=[tuple(pair) for pair in pairs]))


def test_a_take_lasts_at_most_660_seconds() -> None:
    count = 660 * RATE // PERIOD_FRAMES + 1
    with pytest.raises(ValidationError, match="at most 660 s"):
        validated(evidence(period_count=count, buffer_frames=1 << 20))


def test_the_wav_size_is_a_plain_header_and_the_frames() -> None:
    assert wav_size_bytes(16_000, 6, "S32_LE") == 44 + 16_000 * 24
    assert wav_size_bytes(16_000, 2, "S16_LE") == 44 + 16_000 * 4
    assert wav_size_bytes(16_000, 6, "S24_3LE") == 44 + 16_000 * 18
    for alsa_format in ("S16_LE", "S24_3LE"):
        assert validated(evidence(alsa_format=alsa_format)).format == alsa_format

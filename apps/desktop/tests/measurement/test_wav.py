"""24-bit WAV writing and the meter arithmetic."""

from __future__ import annotations

import math
import struct
import wave

import pytest
from voice_lab_desktop.measurement.simulated import tone_block
from voice_lab_desktop.measurement.wav import BlockStats, WavWriter, int32_to_s24le, meter_fraction


def int32s(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}i", *values)


def test_int32_to_s24le_keeps_the_top_24_bits():
    raw = int32s(0x12345600, -256, 0x7FFFFF00, -(2**31))
    assert int32_to_s24le(raw) == bytes(
        [0x56, 0x34, 0x12, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F, 0x00, 0x00, 0x80]
    )
    with pytest.raises(ValueError):
        int32_to_s24le(b"\x00\x01\x02")


def test_wav_header_and_samples_read_back(tmp_path):
    path = tmp_path / "x.wav"
    writer = WavWriter(path, 48000)
    samples = [(v << 8) for v in (0, 1, -1, 2**23 - 1, -(2**23), 12345)]
    writer.write_int32(int32s(*samples))
    size = writer.close()
    assert size == path.stat().st_size == 44 + 6 * 3
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (
            1,
            3,
            48000,
            6,
        )
        frames = w.readframes(6)
    got = [int.from_bytes(frames[i : i + 3], "little", signed=True) for i in range(0, 18, 3)]
    assert got == [s >> 8 for s in samples]


def test_odd_data_is_padded_but_not_counted(tmp_path):
    path = tmp_path / "odd.wav"
    writer = WavWriter(path, 48000)
    writer.write_int32(int32s(256))
    assert writer.close() == 44 + 3 + 1
    with wave.open(str(path)) as w:
        assert w.getnframes() == 1


def test_block_stats_of_a_sine():
    stats = BlockStats().add(tone_block(0, 48000, 48000, dbfs=-24.0, hz=1000.0, noise_dbfs=-140))
    assert stats.peak_dbfs == pytest.approx(-24.0, abs=0.05)
    assert stats.rms_dbfs == pytest.approx(-24.0 - 10 * math.log10(2), abs=0.05)
    assert stats.clipped == 0 and not stats.digital_silence


def test_clipping_and_digital_silence():
    stats = BlockStats().add(int32s(0x7FFFFF00, 0, -(2**31), 5 << 8))
    assert stats.clipped == 2
    zeros = BlockStats().add(bytes(400))
    assert zeros.digital_silence and zeros.peak_dbfs is None and zeros.rms_dbfs is None
    assert not BlockStats().digital_silence  # nothing heard yet is not "silence"


def test_meter_fraction():
    assert meter_fraction(None) == 0.0
    assert (
        meter_fraction(0.0) == 1.0 and meter_fraction(-72.0) == 0.0 and meter_fraction(-36.0) == 0.5
    )
    assert meter_fraction(-200.0) == 0.0

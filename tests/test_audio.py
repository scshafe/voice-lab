from __future__ import annotations

import math
import os
import wave
from pathlib import Path

import pytest

from cole_voice_lab.audio import analyze_pcm_wav, inspect_canonical_wav, inspect_pcm_wav
from cole_voice_lab.errors import ContractError, SecurityError


def _write_wav(
    path: Path,
    samples: list[int],
    *,
    sample_rate: int = 48_000,
    sample_width: int = 3,
    channels: int = 1,
) -> None:
    payload = b"".join(value.to_bytes(sample_width, "little", signed=True) for value in samples)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(payload)


def test_inspect_canonical_24_bit_wav_and_qc(tmp_path: Path) -> None:
    audio = tmp_path / "voice.wav"
    samples = [-4_000_000, -2_000_000, 0, 2_000_000, 4_000_000] * 100
    _write_wav(audio, samples)

    inspection = inspect_canonical_wav(audio)

    assert inspection.audio.sample_rate_hz == 48_000
    assert inspection.audio.channels == 1
    assert inspection.audio.sample_width_bytes == 3
    assert inspection.audio.encoding == "pcm_s24le"
    assert inspection.audio.frames == len(samples)
    assert inspection.size_bytes == audio.stat().st_size
    assert len(inspection.sha256) == 64
    assert -7.0 < inspection.peak_dbfs < -6.0
    assert math.isfinite(inspection.rms_dbfs)
    metrics = analyze_pcm_wav(audio, relative_path="audio/voice.wav", noise_rms_dbfs=-50.0)
    assert metrics.path == "audio/voice.wav"
    assert metrics.signal_to_noise_db is not None
    assert metrics.signal_to_noise_db > 40.0
    assert metrics.passed


@pytest.mark.parametrize(
    ("sample_rate", "sample_width", "channels"),
    [(44_100, 3, 1), (48_000, 2, 1), (48_000, 3, 2), (48_000, 4, 1)],
)
def test_canonical_format_is_exact(
    tmp_path: Path,
    sample_rate: int,
    sample_width: int,
    channels: int,
) -> None:
    audio = tmp_path / "wrong.wav"
    _write_wav(
        audio,
        [1_000] * (100 * channels),
        sample_rate=sample_rate,
        sample_width=sample_width,
        channels=channels,
    )

    with pytest.raises(ContractError, match="mono 48000 Hz 24-bit"):
        inspect_canonical_wav(audio)
    assert inspect_pcm_wav(audio).audio.sample_rate_hz == sample_rate


def test_truncated_pcm_payload_fails_closed(tmp_path: Path) -> None:
    audio = tmp_path / "truncated.wav"
    _write_wav(audio, [100, -100] * 100)
    audio.write_bytes(audio.read_bytes()[:-1])

    with pytest.raises(ContractError, match="data chunk|data length"):
        inspect_canonical_wav(audio)


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target.wav"
    link = tmp_path / "link.wav"
    _write_wav(target, [100, -100] * 100)
    os.symlink(target, link)

    with pytest.raises(SecurityError, match="safely open"):
        inspect_canonical_wav(link)


def test_quality_findings_cover_clipping_dc_and_dropout(tmp_path: Path) -> None:
    audio = tmp_path / "bad.wav"
    full_scale_negative = -(1 << 23)
    samples = [full_scale_negative] * 10 + [7_000_000] * 500 + [0] * 14_000
    _write_wav(audio, samples)

    metrics = analyze_pcm_wav(audio)

    assert not metrics.passed
    assert metrics.clipped_samples == 10
    assert "clipped-samples" in metrics.findings
    assert "dc-offset" in metrics.findings
    assert "digital-silence-or-dropout" in metrics.findings


def test_room_tone_can_be_quiet_without_being_failed_for_level(tmp_path: Path) -> None:
    audio = tmp_path / "room.wav"
    _write_wav(audio, [1, -1] * 1_000)

    metrics = analyze_pcm_wav(audio, allow_quiet=True)

    assert "peak-too-quiet" not in metrics.findings
    assert "peak-too-quiet" in analyze_pcm_wav(audio).findings


def test_digital_silence_has_finite_serializable_floor(tmp_path: Path) -> None:
    audio = tmp_path / "silence.wav"
    _write_wav(audio, [0] * 100)

    inspection = inspect_canonical_wav(audio)

    assert inspection.peak_dbfs == -300.0
    assert inspection.rms_dbfs == -300.0


def test_empty_and_malformed_wav_files_fail(tmp_path: Path) -> None:
    empty = tmp_path / "empty.wav"
    _write_wav(empty, [])
    malformed = tmp_path / "malformed.wav"
    malformed.write_bytes(b"not-a-wave")

    with pytest.raises(ContractError, match="invalid channel|frame evidence"):
        inspect_pcm_wav(empty)
    with pytest.raises(ContractError, match="invalid PCM WAV"):
        inspect_pcm_wav(malformed)

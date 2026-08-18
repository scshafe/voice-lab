from __future__ import annotations

import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from cole_voice_lab.errors import ContractError, ExternalToolError
from cole_voice_lab.normalization import (
    NORMALIZATION_ID,
    TARGET_SAMPLE_RATE_HZ,
    build_normalization_plan,
    execute_normalization,
)


def write_tone(path: Path, *, sample_rate: int = 16_000, seconds: float = 0.1) -> None:
    frames = int(sample_rate * seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(
            b"".join(
                struct.pack("<h", int(4_000 * math.sin(2 * math.pi * 220 * i / sample_rate)))
                for i in range(frames)
            )
        )


def test_normalization_plan_is_non_mutating_and_exact(tmp_path: Path) -> None:
    native = tmp_path / "native.wav"
    normalized = tmp_path / "normalized.wav"
    plan = build_normalization_plan(native, normalized, ffmpeg="/usr/local/bin/ffmpeg")
    assert plan.native_path == native
    assert plan.normalized_path == normalized
    assert plan.command[0] == "/usr/local/bin/ffmpeg"
    assert "-n" in plan.command
    assert "loudnorm=I=-18:TP=-1.5:LRA=11" in plan.command
    assert plan.command[-1] == str(normalized)
    assert not native.exists() and not normalized.exists()
    with pytest.raises(ContractError, match="distinct engine-native"):
        build_normalization_plan(native, native)


def test_execute_normalization_retains_native_and_records_common_pcm(
    tmp_path: Path, monkeypatch
) -> None:
    native = tmp_path / "native.wav"
    normalized = tmp_path / "normalized.wav"
    write_tone(native)
    original = native.read_bytes()
    plan = build_normalization_plan(native, normalized)

    def fake_run(command, **kwargs):
        assert kwargs["check"] is False
        write_tone(Path(command[-1]), sample_rate=TARGET_SAMPLE_RATE_HZ)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cole_voice_lab.normalization.subprocess.run", fake_run)
    evidence = execute_normalization(plan)
    assert evidence.normalization_id == NORMALIZATION_ID
    assert evidence.inspection.audio.sample_rate_hz == TARGET_SAMPLE_RATE_HZ
    assert evidence.inspection.audio.channels == 1
    assert evidence.inspection.audio.sample_width_bytes == 2
    assert evidence.normalized_bytes == normalized.stat().st_size
    assert native.read_bytes() == original


def test_execute_normalization_never_overwrites_and_reports_ffmpeg_failure(
    tmp_path: Path, monkeypatch
) -> None:
    native = tmp_path / "native.wav"
    normalized = tmp_path / "normalized.wav"
    write_tone(native)
    normalized.write_bytes(b"already exists")
    plan = build_normalization_plan(native, normalized)
    with pytest.raises(ContractError, match="overwrite"):
        execute_normalization(plan)

    normalized.unlink()
    monkeypatch.setattr(
        "cole_voice_lab.normalization.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "bad filter"),
    )
    with pytest.raises(ExternalToolError, match="bad filter"):
        execute_normalization(plan)


def test_normalization_detects_native_mutation_and_wrong_output_format(
    tmp_path: Path, monkeypatch
) -> None:
    native = tmp_path / "native.wav"
    normalized = tmp_path / "normalized.wav"
    write_tone(native)
    plan = build_normalization_plan(native, normalized)

    def mutate_native(command, **kwargs):
        write_tone(native, sample_rate=12_000)
        write_tone(Path(command[-1]), sample_rate=TARGET_SAMPLE_RATE_HZ)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cole_voice_lab.normalization.subprocess.run", mutate_native)
    with pytest.raises(ContractError, match="native output changed"):
        execute_normalization(plan)
    normalized.unlink()
    write_tone(native)

    def wrong_rate(command, **kwargs):
        write_tone(Path(command[-1]), sample_rate=16_000)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cole_voice_lab.normalization.subprocess.run", wrong_rate)
    with pytest.raises(ContractError, match="common PCM"):
        execute_normalization(plan)

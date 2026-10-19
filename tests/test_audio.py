from __future__ import annotations

import math
import os
import random
import wave
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

import pytest

import cole_voice_lab.audio as audio_module
from cole_voice_lab.audio import (
    analyze_pcm_wav,
    identify_canonical_wav,
    inspect_canonical_wav,
    inspect_pcm_wav,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import AudioSpec


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
    with pytest.raises(ContractError, match="mono 48000 Hz 24-bit"):
        identify_canonical_wav(audio)
    assert inspect_pcm_wav(audio).audio.sample_rate_hz == sample_rate


def test_truncated_pcm_payload_fails_closed(tmp_path: Path) -> None:
    audio = tmp_path / "truncated.wav"
    _write_wav(audio, [100, -100] * 100)
    audio.write_bytes(audio.read_bytes()[:-1])

    with pytest.raises(ContractError, match="data chunk|data length"):
        inspect_canonical_wav(audio)


def test_identify_is_the_inspection_without_measuring_a_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "voice.wav"
    _write_wav(audio, [-4_000_000, 0, 4_000_000] * 1_000)
    inspection = inspect_canonical_wav(audio)

    def measured(payload: bytes, sample_width: int) -> list[int]:
        raise AssertionError("a sample was measured")

    monkeypatch.setattr(audio_module, "_sample_values", measured)
    identity = identify_canonical_wav(audio)

    assert identity.sha256 == inspection.sha256
    assert identity.size_bytes == inspection.size_bytes == audio.stat().st_size
    assert identity.audio == inspection.audio


@pytest.mark.parametrize(
    ("cut_bytes", "message"),
    [(1, "data chunk ends inside a PCM frame"), (3, "data length disagrees with header")],
)
def test_identify_refuses_data_shorter_than_its_header(
    tmp_path: Path, cut_bytes: int, message: str
) -> None:
    audio = tmp_path / "truncated.wav"
    _write_wav(audio, [100, -100] * 100)
    audio.write_bytes(audio.read_bytes()[:-cut_bytes])

    with pytest.raises(ContractError, match=message):
        identify_canonical_wav(audio)


@pytest.mark.parametrize("fault", ["grows", "unreadable"])
def test_identify_refuses_a_file_that_changes_or_fails_while_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    audio = tmp_path / "voice.wav"
    _write_wav(audio, [100, -100] * 100)
    read = audio_module._identify_open_wav

    def interfering(stream: BinaryIO) -> AudioSpec:
        if fault == "unreadable":
            raise OSError("input/output error")
        with audio.open("ab") as appended:
            appended.write(b"\0")
        return read(stream)

    monkeypatch.setattr(audio_module, "_identify_open_wav", interfering)
    if fault == "grows":
        with pytest.raises(SecurityError, match="changed while it was being inspected"):
            identify_canonical_wav(audio)
    else:
        with pytest.raises(ContractError, match="cannot inspect WAV .*input/output error"):
            identify_canonical_wav(audio)


def test_identify_refuses_a_symlink_and_a_malformed_file(tmp_path: Path) -> None:
    target = tmp_path / "target.wav"
    _write_wav(target, [100, -100] * 100)
    link = tmp_path / "link.wav"
    os.symlink(target, link)
    malformed = tmp_path / "malformed.wav"
    malformed.write_bytes(b"not-a-wave")

    with pytest.raises(SecurityError, match="safely open"):
        identify_canonical_wav(link)
    with pytest.raises(ContractError, match="invalid PCM WAV"):
        identify_canonical_wav(malformed)


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


def _sine_samples(
    frames: int = 96_000, *, frequency: float = 137.0, amplitude: float = 1_000_000.0
) -> list[int]:
    """Generate non-human PCM with a known uninterrupted sample clock."""
    return [
        round(amplitude * math.sin(2 * math.pi * frequency * frame / 48_000))
        for frame in range(frames)
    ]


def _removed_chunk_samples(*, offset: int = 0) -> list[int]:
    samples = _sine_samples()
    removed = {8, 21, 35, 48, 61, 79, 99, 125, 141, 160}
    return samples[:offset] + [
        value for frame, value in enumerate(samples[offset:]) if frame // 512 not in removed
    ]


def _harmonic_samples() -> list[int]:
    """Smooth, amplitude-modulated harmonics approximate benign voiced structure."""
    samples = []
    for frame in range(96_000):
        seconds = frame / 48_000
        envelope = (0.51 + 0.49 * math.cos(2 * math.pi * 2.3 * seconds)) ** 2
        harmonics = sum(
            math.sin(2 * math.pi * 107 * harmonic * seconds) / harmonic for harmonic in range(1, 6)
        )
        samples.append(round(500_000 * envelope * harmonics))
    return samples


def _quiet_noise_samples(*, filtered: bool) -> list[int]:
    generator = random.Random(381)
    previous = 0.0
    samples = []
    for _ in range(96_000):
        noise = generator.gauss(0, 350)
        previous = 0.92 * previous + 0.08 * noise if filtered else noise
        samples.append(round(previous))
    return samples


def test_removed_whole_chunks_produce_repeated_grid_finding_without_editing_audio(
    tmp_path: Path,
) -> None:
    path = tmp_path / "generated-missing-chunks.wav"
    samples = _removed_chunk_samples()
    _write_wav(path, samples)
    original = path.read_bytes()

    inspection = inspect_canonical_wav(path)
    metrics = inspection.qc_metrics(path.name)

    assert inspection.boundary_discontinuity_count >= 4
    assert inspection.interior_discontinuity_count == 0
    assert "repeated-buffer-boundary-discontinuities" in metrics.findings
    assert not metrics.passed
    assert inspection.audio.frames == len(samples)
    assert path.read_bytes() == original


@pytest.mark.parametrize("read_frames", [127, 512, 513, 65_536])
def test_continuity_evidence_is_independent_of_decoder_read_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_frames: int
) -> None:
    path = tmp_path / "generated-read-boundaries.wav"
    _write_wav(path, _removed_chunk_samples())
    expected = inspect_canonical_wav(path)
    monkeypatch.setattr(audio_module, "_READ_FRAMES", read_frames)

    assert inspect_canonical_wav(path) == expected


def test_midpoint_steps_are_not_misrepresented_as_buffer_boundary_faults(tmp_path: Path) -> None:
    path = tmp_path / "generated-midpoint-joins.wav"
    _write_wav(path, _removed_chunk_samples(offset=256))

    inspection = inspect_canonical_wav(path)

    assert inspection.interior_discontinuity_count >= 4
    assert inspection.boundary_discontinuity_count == 0
    assert (
        "repeated-buffer-boundary-discontinuities" not in inspection.qc_metrics(path.name).findings
    )


@pytest.mark.parametrize(
    ("boundary_count", "interior_count", "flagged"),
    [(3, 0, False), (4, 0, True), (7, 2, False), (8, 2, True)],
)
def test_repeated_discontinuity_qc_requires_minimum_count_and_grid_enrichment(
    tmp_path: Path, boundary_count: int, interior_count: int, flagged: bool
) -> None:
    path = tmp_path / "generated-qc-count-contract.wav"
    _write_wav(path, _sine_samples(frames=100))
    inspection = replace(
        inspect_canonical_wav(path),
        boundary_discontinuity_count=boundary_count,
        interior_discontinuity_count=interior_count,
    )

    assert (
        "repeated-buffer-boundary-discontinuities" in inspection.qc_metrics(path.name).findings
    ) is flagged


@pytest.mark.parametrize(
    ("jump", "neighbor", "adjacent", "flagged"),
    [(31, 1, 1, False), (32, 4, 4, True), (32, 5, 5, False), (32, 1, 9, False)],
)
def test_isolated_jump_thresholds_are_unrounded_and_reject_quantization_and_impulses(
    jump: int, neighbor: int, adjacent: int, flagged: bool
) -> None:
    differences = [neighbor] * 481
    differences[240] = jump
    differences[241] = adjacent

    assert audio_module._BoundaryContinuityScan._isolated_jump(differences, 240) is flagged


@pytest.mark.parametrize("kind", ["sine", "harmonics", "white-noise", "room-tone", "impulses"])
def test_benign_synthetic_signal_does_not_receive_continuity_findings(
    tmp_path: Path, kind: str
) -> None:
    if kind == "sine":
        samples = _sine_samples()
    elif kind == "harmonics":
        samples = _harmonic_samples()
    else:
        samples = _quiet_noise_samples(filtered=kind == "room-tone")
        if kind == "impulses":
            for frame in range(2_048, len(samples) - 512, 4_096):
                samples[frame] = 1_000_000
    path = tmp_path / f"generated-{kind}.wav"
    _write_wav(path, samples)

    metrics = analyze_pcm_wav(path, allow_quiet=True)

    assert "repeated-buffer-boundary-discontinuities" not in metrics.findings
    assert "short-digital-silence-or-dropout" not in metrics.findings


@pytest.mark.parametrize("quantum", [1, 256])
def test_near_silence_quantization_zero_runs_are_not_short_dropouts(
    tmp_path: Path, quantum: int
) -> None:
    # Includes native 24-bit quantization and 16-bit-resolution PCM in a 24-bit
    # container. Several naturally quantized zero crossings exceed 2 ms.
    samples = [value * quantum for value in _sine_samples(frequency=23.0, amplitude=2.0)]
    path = tmp_path / "generated-low-resolution-quiet.wav"
    _write_wav(path, samples)

    inspection = inspect_canonical_wav(path)

    assert inspection.longest_zero_run_ms >= 2
    assert inspection.short_zero_run_count == 0
    assert (
        "repeated-buffer-boundary-discontinuities"
        not in inspection.qc_metrics(path.name, allow_quiet=True).findings
    )


@pytest.mark.parametrize(("zero_frames", "flagged"), [(95, False), (96, True), (512, True)])
def test_short_internal_exact_zero_run_requires_two_ms_and_nontrivial_edge_levels(
    tmp_path: Path, zero_frames: int, flagged: bool
) -> None:
    samples = _sine_samples()
    samples[1_003:1_003] = [0] * zero_frames
    path = tmp_path / "generated-interior-zeros.wav"
    _write_wav(path, samples)

    inspection = inspect_canonical_wav(path)

    assert bool(inspection.short_zero_run_count) is flagged
    assert (
        "short-digital-silence-or-dropout" in inspection.qc_metrics(path.name).findings
    ) is flagged


@pytest.mark.parametrize("position", ["leading", "trailing", "quiet-fade"])
def test_short_zero_run_ignores_edges_and_aperiodic_naturally_quiet_passage(
    tmp_path: Path, position: str
) -> None:
    samples = _sine_samples()
    if position == "leading":
        samples[:0] = [0] * 512
    elif position == "trailing":
        samples.extend([0] * 512)
    else:
        # A non-grid-aligned quiet passage with smooth raised-cosine fades.
        for frame in range(12_319, 13_519):
            amount = (frame - 12_319) / 1_199
            samples[frame] = round(samples[frame] * (0.5 + 0.5 * math.cos(math.pi * amount)))
        samples[13_519:14_831] = [0] * (14_831 - 13_519)
        for frame in range(14_831, 16_031):
            amount = (frame - 14_831) / 1_199
            samples[frame] = round(samples[frame] * (0.5 - 0.5 * math.cos(math.pi * amount)))
    path = tmp_path / f"generated-{position}-silence.wav"
    _write_wav(path, samples)

    inspection = inspect_canonical_wav(path)

    assert inspection.short_zero_run_count == 0
    assert inspection.qc_metrics(path.name).passed


def test_short_zero_run_is_counted_across_decoder_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples = _sine_samples()
    samples[65_500:65_500] = [0] * 512
    path = tmp_path / "generated-zero-run-read-boundary.wav"
    _write_wav(path, samples)
    expected = inspect_canonical_wav(path)
    monkeypatch.setattr(audio_module, "_READ_FRAMES", 127)

    assert expected.short_zero_run_count == 1
    assert inspect_canonical_wav(path) == expected


def test_continuity_grid_heuristic_does_not_claim_unsupported_formats(tmp_path: Path) -> None:
    path = tmp_path / "generated-noncanonical.wav"
    _write_wav(path, _removed_chunk_samples(), sample_rate=44_100)

    inspection = inspect_pcm_wav(path)

    assert inspection.boundary_discontinuity_count == 0
    assert inspection.interior_discontinuity_count == 0
    assert inspection.short_zero_run_count == 0


def _write_columns(path: Path, columns: list[list[int]], *, sample_width: int) -> None:
    """A 16 kHz WAV whose channel ``c`` is ``columns[c]``, interleaved."""
    frames = len(columns[0])
    _write_wav(
        path,
        [column[frame] for frame in range(frames) for column in columns],
        sample_rate=16_000,
        sample_width=sample_width,
        channels=len(columns),
    )


@pytest.mark.parametrize("sample_width", [2, 3, 4])
def test_one_channel_of_an_interleaved_wav_is_measured_alone(
    tmp_path: Path, sample_width: int
) -> None:
    # A v2 take is judged by its primary channel (D-056): each channel's figures are its own.
    full = (1 << (sample_width * 8 - 1)) - 1
    frames = 6_400
    loud = [full // 2 * (1 if frame % 2 else -1) for frame in range(frames)]
    quiet = [full // 1_000 * (1 if frame % 2 else -1) for frame in range(frames)]
    silent = [0] * frames
    clipped = [full] * 10 + loud[10:]
    audio = tmp_path / "panel.wav"
    _write_columns(audio, [loud, quiet, silent, clipped, loud, quiet], sample_width=sample_width)

    whole = inspect_pcm_wav(audio)
    first, second, third, fourth = (inspect_pcm_wav(audio, channel=n) for n in (1, 2, 3, 4))
    assert whole.channel is None and first.channel == 1
    assert first.audio == whole.audio and first.audio.channels == 6
    assert first.sha256 == whole.sha256
    assert -6.1 < first.peak_dbfs < -6.0
    assert -60.5 < second.peak_dbfs < -59.9
    assert third.peak_dbfs == -300.0 and third.longest_zero_run_ms == 400.0
    assert fourth.clipped_samples == 10 and first.clipped_samples == 0
    # Interleaved with louder channels, the silent one's zeros never make a long run.
    assert whole.longest_zero_run_ms < 1.0
    assert "peak-too-quiet" in second.qc_metrics("original.wav").findings
    assert "digital-silence-or-dropout" in third.qc_metrics("original.wav").findings
    assert first.qc_metrics("original.wav").passed
    # The canonical format's heuristics do not run on a multichannel take.
    for inspection in (whole, first, second, third, fourth):
        assert inspection.boundary_discontinuity_count == 0
        assert inspection.short_zero_run_count == 0


@pytest.mark.parametrize("channel", [0, 3])
def test_a_channel_the_wav_does_not_have_is_refused(tmp_path: Path, channel: int) -> None:
    audio = tmp_path / "stereo.wav"
    _write_columns(audio, [[1, 2, 3], [4, 5, 6]], sample_width=2)
    with pytest.raises(ContractError, match=f"not a channel {channel}"):
        inspect_pcm_wav(audio, channel=channel)


@pytest.mark.parametrize("sample_width", [2, 4])
def test_the_native_slice_reads_what_the_generic_decoder_reads(
    sample_width: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = [
        [(frame * 37 + channel * 1_009) % 30_000 - 15_000 for frame in range(97)]
        for channel in range(6)
    ]
    payload = b"".join(
        columns[channel][frame].to_bytes(sample_width, "little", signed=True)
        for frame in range(97)
        for channel in range(6)
    )
    native = [audio_module._channel_values(payload, sample_width, 6, n) for n in range(1, 7)]
    monkeypatch.setattr(audio_module, "_ITEM_SIZES", {"h": 0, "i": 0})
    generic = [audio_module._channel_values(payload, sample_width, 6, n) for n in range(1, 7)]
    assert native == generic == columns
    with pytest.raises(ContractError, match="aligned to its frames"):
        audio_module._channel_values(payload[:-1], sample_width, 6, 1)

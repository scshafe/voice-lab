"""The worker's evidence contract and the parent's check before a take exists (D-039)."""

from __future__ import annotations

import json
import os
import stat

import pytest
from capture_helpers import STARTED, evidence_for, make_take, write_wav
from pydantic import ValidationError
from voice_lab_desktop.capture.evidence import (
    MAX_EVIDENCE_BYTES,
    PortAudioContinuityEvidence,
    read_evidence,
    validate_take,
    write_evidence,
)
from voice_lab_desktop.capture.types import (
    EVIDENCE_NAME,
    ORIGINAL_NAME,
    PARTIAL_NAME,
    CaptureFailed,
)

from cole_voice_lab.errors import ContractError, SecurityError


def test_a_good_take_is_published_with_its_numbers(tmp_path):
    take = tmp_path / "take"
    evidence = make_take(take, frames=4800)
    captured = validate_take(take, progress_frames=4800, requested_at=STARTED)
    assert captured.path == take / ORIGINAL_NAME and not (take / PARTIAL_NAME).exists()
    assert captured.frames == 4800 and captured.size_bytes == 44 + 4800 * 3
    assert captured.sha256 == evidence.wav_sha256
    assert captured.device == "Umik-1  Gain: 18dB: USB Audio (hw:2,0) [ALSA]"
    assert captured.requested_at == STARTED
    assert stat.S_IMODE((take / ORIGINAL_NAME).stat().st_mode) == 0o600
    assert (take / ORIGINAL_NAME).stat().st_nlink == 1


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"error": "boom"}, "an error"),
        ({"continuity_checked": False}, "no continuity check"),
        ({"adc_time_valid": False}, "an invalid ADC time"),
        ({"frames": 0, "received_frames": 0, "wav_size_bytes": 44}, "no frames"),
        ({"received_frames": 4801}, "unwritten frames"),
        ({"input_overflows": 1}, "an overflow"),
        ({"input_underflows": 1}, "an underflow"),
        ({"stop_reason": None}, "no stop reason"),
        ({"started_at": None}, "no start time"),
        ({"wav_sha256": None}, "no WAV digest"),
        ({"wav_size_bytes": 100}, "a WAV size that is not 44 \\+ 3 x frames"),
        ({"first_adc_time": None}, "no ADC span"),
        ({"max_abs_deviation_frames": 65.0}, "a deviation beyond the tolerance"),
        ({"max_abs_deviation_frames": None}, "no deviation measured"),
    ],
)
def test_successful_evidence_must_be_complete(tmp_path, change, problem):
    write_wav(tmp_path / PARTIAL_NAME, 4800)
    frames = change.pop("frames", 4800)
    with pytest.raises(ValidationError, match=f"successful evidence with .*{problem}"):
        evidence_for(tmp_path / PARTIAL_NAME, frames, **change)
    # The same facts describe a failed take without complaint.
    failed = evidence_for(
        tmp_path / PARTIAL_NAME,
        frames,
        **{**change, "success": False, "error": change.get("error") or "x"},
    )
    assert not failed.success


def test_the_contract_is_strict(tmp_path):
    write_wav(tmp_path / PARTIAL_NAME, 480)
    good = evidence_for(tmp_path / PARTIAL_NAME, 480)
    data = json.loads(good.model_dump_json())
    for bad in (
        {**data, "extra": 1},
        {**data, "backend": "coreaudio-audioqueue"},
        {**data, "sample_rate": 44100},
        {**data, "output_name": "../x.wav"},
        {**data, "queue_high_water": 65},
        {**data, "schema_version": "cole-voice-lab.capture-continuity/v1"},
    ):
        with pytest.raises(ValidationError):
            PortAudioContinuityEvidence.model_validate_json(json.dumps(bad))
    assert good.device.endswith("[ALSA]")


def test_evidence_is_written_once_and_privately(tmp_path):
    write_wav(tmp_path / PARTIAL_NAME, 480)
    evidence = evidence_for(tmp_path / PARTIAL_NAME, 480)
    path = tmp_path / EVIDENCE_NAME
    write_evidence(path, evidence)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_evidence(path) == evidence
    with pytest.raises(FileExistsError):
        write_evidence(path, evidence)


def _refused(take, match, *, progress=4800):
    with pytest.raises(CaptureFailed, match=match) as caught:
        validate_take(take, progress_frames=progress, requested_at=STARTED)
    failure = caught.value
    assert not (take / ORIGINAL_NAME).exists()
    return failure


def test_a_failed_take_is_kept_not_published(tmp_path):
    take = tmp_path / "take"
    make_take(take, success=False, error="block 7: discontinuity", stop_reason=None)
    failure = _refused(take, "the capture failed: block 7: discontinuity")
    assert failure.partial == take / PARTIAL_NAME and failure.evidence == take / EVIDENCE_NAME
    assert failure.partial.exists() and failure.evidence.exists()


def test_a_tampered_wav_is_refused(tmp_path):
    take = tmp_path / "take"
    make_take(take)
    with open(take / PARTIAL_NAME, "r+b") as stream:
        stream.seek(100)
        stream.write(b"\x01\x02\x03")
    _refused(take, "SHA-256 is not the one the evidence names")


def test_evidence_of_another_take_is_refused(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    make_take(first, frames=4800)
    make_take(second, frames=9600)
    os.replace(second / EVIDENCE_NAME, first / EVIDENCE_NAME)
    _refused(first, "SHA-256 is not the one")


def test_evidence_naming_another_file_is_refused(tmp_path):
    take = tmp_path / "take"
    make_take(take, output_name="other.wav")
    _refused(take, "describes other.wav")


def test_progress_that_disagrees_with_the_wav_is_refused(tmp_path):
    take = tmp_path / "take"
    make_take(take)
    _refused(take, "reported 4797 frames written; the WAV holds 4800", progress=4797)


@pytest.mark.parametrize("name", [EVIDENCE_NAME, PARTIAL_NAME])
def test_files_that_are_not_private_are_refused(tmp_path, name):
    take = tmp_path / "take"
    make_take(take)
    os.chmod(take / name, 0o644)
    _refused(take, "is not private")


@pytest.mark.parametrize("name", [EVIDENCE_NAME, PARTIAL_NAME])
def test_hard_links_are_refused(tmp_path, name):
    take = tmp_path / "take"
    make_take(take)
    os.link(take / name, tmp_path / "elsewhere")
    _refused(take, "has another hard link")


def test_a_symlinked_evidence_file_is_refused(tmp_path):
    take = tmp_path / "take"
    make_take(take)
    os.replace(take / EVIDENCE_NAME, tmp_path / "real.json")
    os.symlink(tmp_path / "real.json", take / EVIDENCE_NAME)
    _refused(take, "cannot be opened safely")


def test_oversized_or_missing_or_malformed_evidence_is_refused(tmp_path):
    take = tmp_path / "big"
    make_take(take)
    with open(take / EVIDENCE_NAME, "ab") as stream:
        stream.write(b" " * MAX_EVIDENCE_BYTES)
    _refused(take, "larger than 16384 bytes")

    take = tmp_path / "missing"
    make_take(take)
    os.unlink(take / EVIDENCE_NAME)
    failure = _refused(take, "continuity.json is missing")
    assert failure.evidence is None and failure.partial is not None

    take = tmp_path / "malformed"
    make_take(take)
    (take / EVIDENCE_NAME).unlink()
    descriptor = os.open(take / EVIDENCE_NAME, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(descriptor, b'{"schema_version": "voice-lab-desktop.portaudio-continuity/v1"}')
    os.close(descriptor)
    _refused(take, "continuity evidence is not valid")


def test_a_wav_that_is_not_canonical_is_refused(tmp_path):
    take = tmp_path / "take"
    take.mkdir()
    import wave

    descriptor = os.open(take / PARTIAL_NAME, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "wb") as stream, wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48_000)
        writer.writeframes(b"\x00\x01" * 480)
    evidence = evidence_for(take / PARTIAL_NAME, 480, wav_size_bytes=44 + 480 * 3)
    write_evidence(take / EVIDENCE_NAME, evidence)
    with pytest.raises(CaptureFailed):
        validate_take(take, progress_frames=480, requested_at=STARTED)


def test_an_existing_original_is_never_overwritten(tmp_path):
    take = tmp_path / "take"
    make_take(take)
    (take / ORIGINAL_NAME).write_bytes(b"older")
    with pytest.raises(CaptureFailed, match="File exists"):
        validate_take(take, progress_frames=4800, requested_at=STARTED)
    assert (take / ORIGINAL_NAME).read_bytes() == b"older"
    assert (take / PARTIAL_NAME).exists()


def test_read_evidence_reports_the_kind_of_problem(tmp_path):
    with pytest.raises(SecurityError):
        read_evidence(tmp_path / "absent.json")
    descriptor = os.open(tmp_path / "bad.json", os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(descriptor, b"not json")
    os.close(descriptor)
    with pytest.raises(ContractError):
        read_evidence(tmp_path / "bad.json")

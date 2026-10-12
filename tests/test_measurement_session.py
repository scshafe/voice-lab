"""A measurement session's own record travels in its capture package (``measurement``).

The distance, the input level and its pin, the calibration file, the client and each
segment's label, start, duration and levels were kept only in ``session.json`` on the
recording machine once uploads went through ``/api/v1``. They now ride in the package's
manifest, checksum-bound with the takes. A value the contract cannot hold is null, never a
refusal; a manifest without the block keeps its exact bytes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_api_journey_intake import RITUAL, client_session, make_manifest

from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.journey import measurement_package as package
from cole_voice_lab.models import (
    AudioFileEvidence,
    AudioSpec,
    CaptureDevice,
    CaptureManifest,
    MeasurementSegment,
    MeasurementSession,
)


def written(tmp_path: Path, manifest: dict[str, Any], payloads: dict[str, bytes]) -> Any:
    source = tmp_path / "session"
    source.mkdir(exist_ok=True)
    for name, data in payloads.items():
        (source / name).write_bytes(data)
    target = tmp_path / "package"
    target.mkdir()
    located = {name: source / name for name in payloads}
    package.write_package(
        manifest, located, target, subject_id="cole-self", capture_host="test", move=False
    )
    return validate_capture_package(target).manifest


def test_a_ritual_session_keeps_its_distance_level_and_segments(tmp_path: Path) -> None:
    body, payloads = client_session(tmp_path / "recorded")
    manifest = json.loads(body)
    kept = written(tmp_path, manifest, payloads).measurement
    assert kept is not None
    assert kept.source_session_id == "s-20261009-080000"
    assert kept.distance_cm == 35.0 and kept.input_volume == 62.0
    assert kept.input_level_muted is False and kept.input_level_source is None
    assert kept.level_pinned_at == "2026-10-01T00:00:00.000Z"
    assert kept.controlled is True and kept.calibration_file == "7000000.txt"
    assert kept.client == "voice-journey-capture 0.1.0 linux"
    assert [segment.key for segment in kept.segments] == [key for key, *_ in RITUAL]
    first = kept.segments[0]
    assert first.path == "room-silence/01-room-silence.wav"
    assert (first.label, first.mode) == ("room silence", "timed")
    assert first.started_at == "2026-10-09T08:00:01.000Z"
    assert (first.peak_dbfs, first.rms_dbfs, first.clipped_samples) == (-12.0, -30.0, 0)
    assert first.duration_seconds == manifest["segments"][0]["durationSeconds"]


def test_values_the_contract_cannot_hold_are_null_never_a_refusal() -> None:
    manifest = make_manifest()
    manifest["controlled"] = "yes"
    manifest["capture"] |= {
        "distanceCm": 50_000,
        "inputLevel": "loud",
        "levelPinnedAt": 17,
        "calFile": "x" * 600,
        "client": "a string, not an object",
    }
    manifest["segments"][0] |= {
        "label": "y" * 600,
        "durationSeconds": "10.0",
        "peakDbfs": float("inf"),
        "clippedSamples": -1,
        "inputOverflows": True,
    }
    manifest["segments"].append({"key": "stray", "file": "not-in-the-package.wav"})
    kept = package.measurement_session(manifest)
    assert kept.distance_cm is None and kept.input_volume == 62.0
    assert kept.input_level_source is None and kept.input_level_muted is None
    assert kept.level_pinned_at is None and kept.controlled is None
    assert kept.calibration_file is None and kept.client is None
    room = kept.segments[0]
    assert room.label is None and room.duration_seconds is None and room.peak_dbfs is None
    assert room.clipped_samples is None and room.input_overflows is None
    # A segment that names no file of the package is left out, not refused.
    assert [segment.key for segment in kept.segments] == ["room-silence", "anchor-song"]


def manifest(**changes: Any) -> CaptureManifest:
    spec = AudioSpec(
        sample_rate_hz=48_000,
        channels=1,
        sample_width_bytes=3,
        encoding="pcm_s24le",
        frames=48_000,
        duration_seconds=1.0,
    )
    moment = datetime(2026, 10, 12, tzinfo=UTC)
    fields: dict[str, Any] = {
        "session_id": "vj-s-1",
        "subject_id": "cole-self",
        "prompt_suite_id": "voice-journey-measurement",
        "capture_host": "test",
        "captured_at": moment,
        "finalized_at": moment,
        "capture_device": CaptureDevice(device_id="umik1", display_name="miniDSP UMIK-1"),
        "files": (
            AudioFileEvidence(
                path="room-silence/a.wav",
                kind="room-tone",
                sha256="0" * 64,
                size_bytes=144_044,
                audio=spec,
            ),
        ),
        **changes,
    }
    return CaptureManifest(**fields)


def test_a_manifest_without_the_block_keeps_its_exact_bytes() -> None:
    plain = manifest()
    data = canonical_json_bytes(plain)
    # Manifests written before the block existed never carried the key, and still do not.
    assert b'"measurement":' not in data
    assert canonical_json_bytes(CaptureManifest.model_validate_json(data)) == data
    with_block = manifest(
        measurement=MeasurementSession(
            source_session_id="s-1",
            distance_cm=35.0,
            input_volume=62.0,
            segments=(MeasurementSegment(key="room-silence", path="room-silence/a.wav"),),
        )
    )
    data = canonical_json_bytes(with_block)
    assert b'"distance_cm":35.0' in data
    assert CaptureManifest.model_validate_json(data) == with_block


def test_the_block_names_only_the_manifests_own_files() -> None:
    stray = MeasurementSegment(key="glide", path="glide/elsewhere.wav")
    session = MeasurementSession(
        source_session_id="s-1", distance_cm=35.0, input_volume=62.0, segments=(stray,)
    )
    with pytest.raises(ValidationError, match="name the manifest's own files"):
        manifest(measurement=session)

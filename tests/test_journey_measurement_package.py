"""A UMIK-1 measurement session as a voice-lab capture package (journey.measurement_package).

The mapping the desktop app builds measurement packages with (phase 4b). Until phase 4 it was
also the API's compatibility intake's (voice-journey's ``/api/intake/…``); these cases come
from that path's tests, against the module itself: what a package cannot hold is refused
with a stable code, the package's shape, and audio the lab cannot keep.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.capture import MANIFEST_NAME
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.journey import measurement_package as package
from cole_voice_lab.journey.measurement_package import (
    DEFAULT_SUBJECT_ID,
    PackageRefusal,
    moment,
    package_layout,
    package_session_id,
    require_package_mapping,
)

REGISTRY: dict[str, Any] = {
    "schemaVersion": "voice-journey.chains.v1",
    "chains": [
        {"chainId": "iphone-voicememos", "kind": "phone", "controlled": False},
        {
            "chainId": "umik1",
            "kind": "measurement",
            "controlled": True,
            "deviceName": "umik",
            "sampleRate": 48000,
        },
        {
            "chainId": "other-mic",
            "kind": "measurement",
            "controlled": True,
            "deviceName": "scarlett",
            "sampleRate": 48000,
        },
    ],
}
SESSION = "s-20261003-101500"
STARTED = "2026-10-03T10:15:00.000Z"


def wav(
    seed: int,
    frames: int,
    *,
    rate: int = 48_000,
    width: int = 3,
    amplitude: float | None = None,
) -> bytes:
    """A tone as the capture client writes it: the 44-byte PCM header, then a RIFF pad
    byte when the data has an odd length."""

    level = amplitude if amplitude is not None else 0.05 + 0.02 * seed
    full_scale = (1 << (8 * width - 1)) - 1
    frequency = 110.0 * (1 + seed % 7)
    data = bytearray()
    for index in range(frames):
        value = int(level * full_scale * math.sin(2 * math.pi * frequency * index / rate))
        data += value.to_bytes(width, "little", signed=True)
    header = b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + len(data)),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * width, width, 8 * width),
            b"data",
            struct.pack("<I", len(data)),
        ]
    )
    return header + bytes(data) + (b"\x00" if len(data) % 2 else b"")


ROOM = "01-room-silence.wav"
SONG = "02-anchor-song.wav"
PAYLOADS = {ROOM: wav(1, 1000, amplitude=0.0005), SONG: wav(2, 1667)}


def make_manifest(
    overrides: dict[str, Any] | None = None, payloads: dict[str, bytes] = PAYLOADS
) -> dict[str, Any]:
    files = [
        {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data)}
        for name, data in payloads.items()
    ]
    return {
        "sessionId": SESSION,
        "chainId": "umik1",
        "startedAt": STARTED,
        "endedAt": "2026-10-03T10:25:00.000Z",
        "capture": {
            "sampleRate": 48000,
            "bitDepth": 24,
            "channels": 1,
            "inputVolume": 62,
            "distanceCm": 35,
            "calFileSha256": "a" * 64,
        },
        "segments": [
            {"key": "room-silence", "label": "room silence", "file": ROOM, "seconds": 10},
            {"key": "anchor-song", "label": "anchor song", "file": SONG},
        ],
        "files": files,
        **(overrides or {}),
    }


def refuse_mapping(overrides: dict[str, Any], code: str, registry: Any = REGISTRY) -> None:
    with pytest.raises(PackageRefusal) as caught:
        require_package_mapping(make_manifest(overrides), registry)
    assert caught.value.code == code


def session_files(directory: Path, payloads: dict[str, bytes] = PAYLOADS) -> dict[str, Path]:
    directory.mkdir(mode=0o700)
    located = {}
    for name, data in payloads.items():
        located[name] = directory / name
        located[name].write_bytes(data)
    return located


def new_package(directory: Path) -> Path:
    directory.mkdir(mode=0o700)
    return directory


def test_the_mapping_refuses_what_a_capture_package_cannot_hold() -> None:
    require_package_mapping(make_manifest(), REGISTRY)  # the good session
    refuse_mapping({"sessionId": "S-20261003-101500"}, "invalid_session_id")
    refuse_mapping({"sessionId": "s-20261003-10150_"}, "invalid_session_id")
    refuse_mapping({"chainId": "umik9"}, "chain_not_accepted")  # not in the registry
    umik2 = {
        "chains": [
            {"chainId": "umik2", "kind": "measurement", "deviceName": "umik", "label": "UMIK-2"}
        ]
    }
    refuse_mapping({"chainId": "umik2"}, "chain_not_accepted", umik2)
    refuse_mapping({"chainId": "other-mic"}, "chain_not_accepted")
    capture = make_manifest()["capture"]
    refuse_mapping({"capture": {**capture, "bitDepth": 16}}, "format_not_accepted")
    refuse_mapping({"capture": {**capture, "channels": 2}}, "format_not_accepted")
    slow = {"chains": [{**REGISTRY["chains"][1], "sampleRate": 44100}]}
    refuse_mapping({"capture": {**capture, "sampleRate": 44100}}, "format_not_accepted", slow)
    refuse_mapping({"endedAt": "2026-10-03T10:00:00.000Z"}, "invalid_timestamp")
    refuse_mapping({"endedAt": "+010000-01-01T00:00:00.000Z"}, "invalid_timestamp")
    keyed = make_manifest()["segments"]
    refuse_mapping(
        {"segments": [{**keyed[0], "key": "Room Silence"}, keyed[1]]}, "invalid_segment_key"
    )
    refuse_mapping({"segments": [{**keyed[0], "key": "silence"}, keyed[1]]}, "room_tone_required")
    # A file no segment names sits at the package root; the last segment naming a file wins.
    layout = package_layout(
        make_manifest(
            {
                "segments": [
                    {"key": "anchor-song", "file": ROOM},
                    {"key": "room-silence", "file": ROOM},
                ]
            }
        )
    )
    assert layout == [
        (ROOM, f"room-silence/{ROOM}", "room-tone"),
        (SONG, SONG, "calibration"),
    ]
    with pytest.raises(ValueError, match="not a timestamp"):
        moment("not a timestamp")


def test_a_session_becomes_the_package_the_strict_validator_accepts(tmp_path: Path) -> None:
    manifest = make_manifest()
    located = session_files(tmp_path / "session")
    target = new_package(tmp_path / "package")
    capture, digest = package.write_package(
        manifest, located, target, subject_id=DEFAULT_SUBJECT_ID, capture_host="voice-lab-desktop"
    )
    members = sorted(
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()
    )
    assert members == [
        "READY",
        "anchor-song/02-anchor-song.wav",
        "manifest.json",
        "qc.json",
        "room-silence/01-room-silence.wav",
    ]
    assert (target / "anchor-song" / SONG).read_bytes() == PAYLOADS[SONG]
    assert os.stat(target / "anchor-song" / SONG).st_mode & 0o777 == 0o600
    # The session's own files are copied, never moved.
    assert all(path.read_bytes() == PAYLOADS[name] for name, path in located.items())
    validated = validate_capture_package(target)
    assert validated.manifest == capture and validated.manifest_sha256 == digest
    assert sha256_file(target / MANIFEST_NAME)[0] == digest
    assert capture.session_id == package_session_id(SESSION) == f"vj-{SESSION}"
    assert capture.subject_id == "cole-self"
    assert capture.prompt_suite_id == "voice-journey-measurement"
    assert capture.capture_host == "voice-lab-desktop"
    assert capture.captured_at.isoformat() == "2026-10-03T10:15:00+00:00"
    assert capture.finalized_at.isoformat() == "2026-10-03T10:25:00+00:00"
    assert capture.capture_device.device_id == "voice-journey-chain:umik1"
    assert capture.capture_device.calibration_sha256 == "a" * 64
    assert capture.capture_device.microphone_serial is None
    assert "UMIK-1" in capture.capture_device.display_name
    assert [entry.kind for entry in capture.files] == ["room-tone", "calibration"]
    assert validated.qc_report.status in {"pass", "review"}

    # The same session builds the same bytes again (a retried upload names the same digest).
    again = new_package(tmp_path / "again")
    _, digest_again = package.write_package(
        manifest, located, again, subject_id=DEFAULT_SUBJECT_ID, capture_host="voice-lab-desktop"
    )
    assert digest_again == digest
    assert (again / "qc.json").read_bytes() == (target / "qc.json").read_bytes()
    # Nothing is written over an existing file.
    with pytest.raises(FileExistsError):
        package.write_package(
            manifest, located, target, subject_id=DEFAULT_SUBJECT_ID, capture_host="x"
        )


def test_the_whole_ritual_maps_one_room_tone_and_six_calibration_files(tmp_path: Path) -> None:
    ritual = (
        ("room-silence", 0.0004),
        ("cal-tone", 0.3),
        ("vowel-soft", 0.02),
        ("vowel-loud", 0.4),
        ("glide", 0.2),
        ("anchor-song", 0.25),
        ("free-practice", 0.1),
    )
    payloads = {
        f"{number:02d}-{key}.wav": wav(number, 4800 + number, amplitude=level)
        for number, (key, level) in enumerate(ritual, start=1)
    }
    manifest = make_manifest(
        {
            "sessionId": "s-20261009-080000",
            "capture": {
                **make_manifest()["capture"],
                "calSerial": "7000000",
                "calFileSha256": "b" * 64,
            },
            "segments": [
                {"key": key, "file": f"{number:02d}-{key}.wav"}
                for number, (key, _) in enumerate(ritual, start=1)
            ],
        },
        payloads=payloads,
    )
    require_package_mapping(manifest, REGISTRY)
    target = new_package(tmp_path / "package")
    capture, _ = package.write_package(
        manifest,
        session_files(tmp_path / "session", payloads),
        target,
        subject_id=DEFAULT_SUBJECT_ID,
        capture_host="voice-lab-desktop",
    )
    assert validate_capture_package(target).manifest == capture
    assert capture.session_id == "vj-s-20261009-080000"
    assert capture.capture_device.microphone_serial == "7000000"
    assert capture.capture_device.calibration_sha256 == "b" * 64
    assert [entry.path for entry in capture.files] == [
        f"{key}/{number:02d}-{key}.wav" for number, (key, _) in enumerate(ritual, start=1)
    ]
    assert [entry.kind for entry in capture.files] == ["room-tone"] + ["calibration"] * 6


def test_audio_the_lab_cannot_keep_is_refused_and_nothing_is_placed(tmp_path: Path) -> None:
    payloads = {ROOM: wav(1, 1000, rate=44_100, amplitude=0.0005), SONG: wav(2, 1667, width=2)}
    located = session_files(tmp_path / "session", payloads)
    target = new_package(tmp_path / "package")
    with pytest.raises(PackageRefusal) as caught:
        package.write_package(
            make_manifest(payloads=payloads),
            located,
            target,
            subject_id=DEFAULT_SUBJECT_ID,
            capture_host="voice-lab-desktop",
        )
    assert caught.value.code == "audio_not_accepted"
    assert caught.value.detail == {"files": [ROOM, SONG], "expected": "mono-48000-pcm-s24le"}
    assert list(target.iterdir()) == []


def test_a_session_file_that_is_a_link_is_never_followed(tmp_path: Path) -> None:
    located = session_files(tmp_path / "session")
    real = tmp_path / "elsewhere.wav"
    real.write_bytes(PAYLOADS[SONG])
    located[SONG].unlink()
    located[SONG].symlink_to(real)
    target = new_package(tmp_path / "package")
    with pytest.raises((SecurityError, OSError)):
        package.write_package(
            make_manifest(),
            located,
            target,
            subject_id=DEFAULT_SUBJECT_ID,
            capture_host="voice-lab-desktop",
        )
    assert not (target / "anchor-song" / SONG).exists()
